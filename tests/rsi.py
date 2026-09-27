"""The shared fixture of docs/modules/08_INTEGRATION_CASES.md §1: the `rsi` domain of the miniature upstream, a
scripted worker that responds to what it is actually given, and a workspace with W0/R0/K installed.

The scripted worker never looks at a candidate's name. It reads the request, the system prompt and the tool
results, so a test that shows a new policy fixing a task shows the policy changing behaviour — not a fixture
deciding to be nice to a version it recognises. Every report built from these tasks is marked
`synthetic_contract_test`; the numbers are the fixture's, not a measurement.
"""
from __future__ import annotations

import json
import re

from ouroloop.adapters import automationbench as ab
from ouroloop.backends import KeywordBackend
from ouroloop.benchmark import Budget, RunManifest, SplitManifest, digest
from ouroloop.chat import ChatTurn, ToolCall
from ouroloop.evolve import contracts as ct
from ouroloop.points import builtin_points
from ouroloop.runtime import Runtime

DOMAIN = "rsi"
SLACK = "https://slack.com/api/chat.postMessage"
HUBSPOT = "https://api.hubapi.com/crm/v3/objects/contacts"
CONTACTS = {"Dana Reyes": "c1", "Sam Okafor": "c2", "Lea Fontaine": "c3", "Kai Mercer": "c4"}
# Variants of one workflow are one family, declared rather than guessed (docs/DESIGN.md §3.1.2).
FAMILIES = {"rsi.update_stage": "rsi.update",
            "rsi.notify_after_update": "rsi.notify_after_update",
            "rsi.notify_after_update_variant": "rsi.notify_after_update",
            "rsi.policy_hold": "rsi.hold",
            "rsi.notify_once": "rsi.notify_once",
            "rsi.dev_notify_after_update": "rsi.dev_notify_after_update",
            "rsi.dev_update_stage": "rsi.dev_update",
            "rsi.dev_policy_hold": "rsi.dev_hold",
            "rsi.final_notify_after_update": "rsi.final_notify_after_update",
            "rsi.final_update_stage": "rsi.final_update"}
SPLITS = {"rsi.update": "train", "rsi.notify_after_update": "train", "rsi.hold": "train",
          "rsi.notify_once": "train", "rsi.dev_notify_after_update": "dev", "rsi.dev_update": "dev",
          "rsi.dev_hold": "dev", "rsi.final_notify_after_update": "test", "rsi.final_update": "test"}
TRAIN = [t for t, f in FAMILIES.items() if SPLITS[f] == "train"]
DEV = [t for t, f in FAMILIES.items() if SPLITS[f] == "dev"]
FINAL = [t for t, f in FAMILIES.items() if SPLITS[f] == "test"]


def suite(path) -> ab.Suite:
    return ab.load_suite(DOMAIN, path)


def splits(s: ab.Suite) -> SplitManifest:
    fams, rule = ab.families(s, FAMILIES)
    return SplitManifest(DOMAIN, s.versions, rule, s.entries(fams), dict(SPLITS))


def runtime(root) -> Runtime:
    rt = Runtime(root, {"mock": KeywordBackend()}, "mock", seed=0)
    for p in builtin_points():
        rt.register(p)
    return rt


def workspace(root, split_ref: str = "", worker_ref: str | None = None, research_ref: str | None = None,
              **core_kw):
    """A workspace with W0, R0, K and a bootstrap release installed. -> (Releases, release ref, Objects)."""
    from ouroloop import policies as P
    objects = ct.Objects(root)
    releases, rel = P.bootstrap_workspace(root, objects=objects, worker_ref=worker_ref,
                                          research_ref=research_ref, domain=DOMAIN, split_ref=split_ref,
                                          adapter="ouroloop.adapters.automationbench", scorer="rubric.v1",
                                          toolset=ab.TOOLSET, **core_kw)
    return releases, rel, objects


def run_manifest(candidate: str, role: str, split: str, *, budget: Budget | None = None,
                 data: str = "", **v2) -> RunManifest:
    return RunManifest(candidate, role, split, {"provider": "scripted", "model": "scripted-1"},
                       prompt_hash=digest({"system": candidate})[:16],
                       environment={"upstream": "fake", "toolset": ab.TOOLSET},
                       budget=budget or Budget(max_steps=8, max_tool_calls=12), data=data, **v2)


W0_FINISH = "Report what you did when you are finished."


def w0(objects) -> str:
    """The starting worker version: no finish check, so the scripted agent stops after the update."""
    from ouroloop import policies as P
    return P.default_worker_set(objects, finish_check=W0_FINISH)


def w1(objects) -> str:
    """The same version with a finish-check instruction bound to worker.finish."""
    from ouroloop import policies as P
    return P.replace_policies(objects, w0(objects), [
        P.descriptor(objects, "worker.finish", "workflow",
                     {"builtin": "finish_check.v1",
                      "instruction_ref": objects.put_json("change", {"text": P.FINISH_CHECK})})])


# ---------------------------------------------------------------------------- the scripted worker
PUSHBACK = "Keep working on the task"
FINISH_CHECK_MARK = "name the tool result that shows it is done"


def _needs(text: str) -> dict:
    """What the request asks for, read from the request itself."""
    name = next((n for n in CONTACTS if n in text), None)
    phrase = re.search(r"phrase ([a-z ]+?)(?:,|\.|$)", text)
    return {"update": bool(name) and "opportunity stage" in text and "change nothing" not in text,
            "contact": CONTACTS.get(name or "", ""),
            "notify": "#ops" in text and phrase is not None,
            "phrase": (phrase.group(1).strip() if phrase else ""),
            "hold": "change nothing" in text}


def _done(messages: list[dict]) -> dict:
    """What has already been done, read from the tool calls in the conversation."""
    updated, sent = False, 0
    for m in messages:
        for call in m.get("tool_calls") or ():
            if call.name != "api_fetch":
                continue
            body = json.loads(call.args.get("body") or "{}")
            updated = updated or (call.args.get("url") == HUBSPOT and body.get("stage") == "opportunity")
            sent += int(call.args.get("url") == SLACK)
    return {"updated": updated, "sent": sent}


class BusinessWorker:
    """A scripted agent over the rsi tasks.

    It updates the record, and it posts the notification only when something in front of it says to check the
    request again: a finish-check instruction in the system prompt, or a pushback from the guard. Without either
    it finishes after the update, which is the behaviour gap the loop starts from (IT-01).
    """
    name = "scripted"
    model = "scripted-1"

    def __init__(self, *, repeat_notify: bool = False, ignore_finish_check: bool = False,
                 ignore_pushback: bool = False):
        self.repeat_notify = repeat_notify              # sends twice: the regression on rsi.notify_once
        self.ignore_finish_check = ignore_finish_check
        self.ignore_pushback = ignore_pushback
        self.calls = 0

    def chat(self, system: str, messages: list[dict], specs) -> ChatTurn:
        self.calls += 1
        task = next((m["content"] for m in messages if m.get("role") == "user"), "")
        want, done = _needs(task), _done(messages)
        pushed = any(PUSHBACK in str(m.get("content", "")) for m in messages if m.get("role") == "user")
        reminded = (FINISH_CHECK_MARK in (system or "") and not self.ignore_finish_check) or \
                   (pushed and not self.ignore_pushback)
        usage = {"input_tokens": 100, "output_tokens": 20}
        if want["hold"]:
            return ChatTurn("The request is not allowed by the data policy, so I changed nothing.", [], "end",
                            usage=usage)
        if want["update"] and not done["updated"]:
            return ChatTurn("", [ToolCall("t1", "api_fetch", {
                "method": "PATCH", "url": HUBSPOT,
                "body": json.dumps({"id": want["contact"], "stage": "opportunity"})})], "tool_use", usage=usage)
        if want["notify"] and (reminded or not want["update"]) and done["sent"] == 0:
            return ChatTurn("", [ToolCall("t2", "api_fetch", {
                "method": "POST", "url": SLACK,
                "body": json.dumps({"channel": "C1", "text": f"{want['phrase']} for the contact"})})],
                "tool_use", usage=usage)
        if want["notify"] and self.repeat_notify and done["sent"] == 1:
            return ChatTurn("", [ToolCall("t3", "api_fetch", {
                "method": "POST", "url": SLACK,
                "body": json.dumps({"channel": "C1", "text": f"{want['phrase']} again"})})], "tool_use",
                usage=usage)
        return ChatTurn("Done: the record is updated." + (" Message posted." if done["sent"] else ""), [],
                        "end", usage=usage)
