"""The shared fixture: the `rsi` domain of the miniature upstream (tests/fake_upstream), a scripted actor that responds to
what it is actually given, and an activity over them with a small preset, the host's ProcessWorker and the scripted
actor in place of the claude CLI.

The scripted actor never looks at a candidate's name. It reads the request, the system prompt and the tool results, so a
test that shows a package changing a task's outcome shows the package changing behaviour — not a fixture deciding to be
nice to a version it recognises. Every number from these tasks is the fixture's, not a measurement.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ouroloop.activity import presets
from ouroloop.activity.workspace import open_activity
from ouroloop.adapters import automationbench as ab
from ouroloop.evaluate.results import Results
from ouroloop.evaluate.splits import SplitManifest, TaskEntry
from ouroloop.runtime.chat import ChatTurn, ToolCall
from ouroloop.runtime.worker import ProcessWorker

DOMAIN = "rsi"
SLACK = "https://slack.com/api/chat.postMessage"
HUBSPOT = "https://api.hubapi.com/crm/v3/objects/contacts"
CONTACTS = {"Dana Reyes": "c1", "Sam Okafor": "c2", "Lea Fontaine": "c3", "Kai Mercer": "c4"}
# Variants of one workflow are one family, declared rather than guessed.
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

# A preset for the miniature domain: the real preset's every rule, with the gate's floors and the limits small enough for
# three dev tasks and a scripted actor. Registered under its own name; nothing outside the tests reads it.
TEST_PRESET = "rsi_test"
RESEARCH_MODEL = "scripted-research"
_preset = json.loads(json.dumps(presets.AUTOMATIONBENCH_V1))
_preset["limits"] = {"max_steps": 12, "max_model_calls": 30, "max_tool_calls": 20, "max_seconds": 120,
                     "max_tokens": 50000, "max_output_chars": 20000}
_preset["gate"].update({"min_groups": 1, "min_tasks": 1, "control_readings": 1, "confirmations": 0})
_preset["bench"]["concurrency"] = 1
# the memory under the fake client's bag-of-words vectors (tests/memory/fake_memory.py), one task per stream step
_preset["memory"].update({"min_score": 0.2, "min_other_tasks": 1, "stream_batch": 1})
presets.PRESETS[TEST_PRESET] = _preset

PRICING = {"input_per_1k": 0.004, "output_per_1k": 0.02, "cache_read_per_1k": 0.0002, "cache_write_per_1k": 0.008,
           "max_input_tokens": 20000, "max_output_tokens": 5000, "source": "test prices"}


def suite(path) -> ab.Suite:
    return ab.load_suite(DOMAIN, path)


def splits(s: ab.Suite) -> SplitManifest:
    fams, rule = ab.families(s, FAMILIES)
    return SplitManifest(DOMAIN, s.versions, rule, [TaskEntry.of(e) for e in s.entries(fams)], dict(SPLITS))


def config(root: Path, upstream: str, **sections) -> dict:
    """A config of the miniature domain under the test preset. `sections` replace or extend tables."""
    cfg = {"_base": str(root), "workspace": {"root": str(Path(root) / "ws")},
           "bench": {"domain": DOMAIN, "upstream_path": upstream, "families": dict(FAMILIES), "smoke_tasks": 1},
           "model": {"type": "claude_code", "model": "scripted-1", "effort": "medium", "max_tokens": 1000},
           "research_model": {"type": "claude_code", "model": RESEARCH_MODEL, "effort": "medium", "max_tokens": 1000},
           "pricing": dict(PRICING), "harness": {"preset": TEST_PRESET}, "memory": {"enabled": False},
           "budget": {"usd": 50.0, "final_usd": 10.0}, "rsi": {"scope_id": "rsi-test"}}
    for name, table in sections.items():
        if table is None:
            cfg.pop(name, None)
        elif isinstance(table, dict) and isinstance(cfg.get(name), dict):
            cfg[name] = {**cfg[name], **table}
        else:
            cfg[name] = table
    return cfg


def write_split(root: Path, s: ab.Suite) -> SplitManifest:
    """The rsi domain's declared split, frozen into the workspace before the activity opens (freeze_split keeps what is
    on disk), so tests read the train/dev/test sets of SPLITS rather than a hash rule's. A split already frozen there is
    kept: the fake upstream's versions name pydantic only once something has imported it, so freezing it again after a
    phase would read as a changed split."""
    path = Results(Path(root) / "ws", DOMAIN).manifest_path()
    if path.exists():
        return SplitManifest.load(path)
    manifest = splits(s)
    manifest.freeze(path)
    return manifest


def activity(root: Path, upstream: str, *, provider=None, worker=None, freeze_k: bool = True, out=None,
             research=None, memory_client=None, **sections):
    """The activity of `config(root, upstream, **sections)` with the declared split, ProcessWorker (a test worker:
    for_tests), `provider` (a BusinessWorker by default) for the actor's calls and `research` for the research model's
    (the actor's provider when none is given). `memory_client` makes the memory's client (a test's in-process stand-in
    for the sidecar's; the real one otherwise)."""
    cfg = config(root, upstream, **sections)
    write_split(root, suite(upstream))
    provider = provider if provider is not None else BusinessWorker()
    research = research if research is not None else provider
    return open_activity(cfg, out=out or (lambda _line: None), freeze_k=freeze_k,
                         worker_factory=(lambda: worker) if worker is not None else
                         (lambda: ProcessWorker(for_tests=True)),
                         provider_factory=lambda table: research if table.get("model") == RESEARCH_MODEL else provider,
                         memory_client_factory=memory_client)


# ---------------------------------------------------------------------------- the scripted actor
PUSHBACK = "Keep working on the task"
FINISH_CHECK_MARK = "name the tool result that shows it is done"
LIBRARY_MARK = "[lib "                  # an entry of the memory's opening block (memory/retrieval.py)
NOTIFY_TOOL = "post_channel_message"    # a package tool for the notification: offered, the actor uses it


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
    """What has been done, read from the calls whose results came back without an error."""
    ok = {r.get("id") for m in messages if m.get("role") == "tool" for r in m.get("results") or ()
          if not r.get("is_error")}
    updated, sent, planned = False, 0, False
    for m in messages:
        for call in m.get("tool_calls") or ():
            if call.id not in ok:
                continue
            if call.name == "plan_changes":
                planned = True
            if call.name == NOTIFY_TOOL:
                sent += 1
            if call.name != "api_fetch":
                continue
            body = json.loads(call.args.get("body") or "{}")
            updated = updated or (call.args.get("url") == HUBSPOT and body.get("stage") == "opportunity")
            sent += int(call.args.get("url") == SLACK)
    return {"updated": updated, "sent": sent, "planned": planned}


class BusinessWorker:
    """A scripted actor over the rsi tasks.

    It plans first when the package offers plan_changes, updates the record, and posts the notification only when
    something in front of it says to check the request again: the finish-check text in the system prompt, a pushback,
    or a note of the memory's opening block about #ops — or when the package offers a tool for it (NOTIFY_TOOL), which
    it then uses. It finishes with the finish tool when one is offered, and
    then names the library entries it was shown in a `library-used:` line. `forget_notify` ignores the finish check,
    which is the behaviour gap a candidate (or a lesson of the memory) can close."""
    name = "scripted"
    model = "scripted-1"

    def __init__(self, *, repeat_notify: bool = False, forget_notify: bool = False, ignore_pushback: bool = False,
                 usage: dict | None = None):
        self.repeat_notify, self.forget_notify, self.ignore_pushback = repeat_notify, forget_notify, ignore_pushback
        self.usage = usage or {"input_tokens": 100, "output_tokens": 20}
        self.calls = 0
        self.seen: list = []

    def chat(self, system: str, messages: list[dict], specs, **_kw) -> ChatTurn:
        self.calls += 1
        self.seen.append({"system": system, "messages": messages, "tools": [s.name for s in specs or ()]})
        names = {s.name for s in specs or ()}
        task = next((m["content"] for m in messages if m.get("role") == "user"), "")
        want, done = _needs(task), _done(messages)
        read = [r.get("output", "") for m in messages if m.get("role") == "tool" for r in m.get("results") or ()]
        # a pushback comes back as a user message or as the finish tool's result (the stirrup seed's on_finish)
        pushed = any(PUSHBACK in str(m.get("content", "")) for m in messages if m.get("role") == "user") or \
            any(str(r).startswith(PUSHBACK) for r in read)
        noted = LIBRARY_MARK in (system or "") and "#ops" in (system or "").split(LIBRARY_MARK, 1)[1]
        reminded = not self.forget_notify and (FINISH_CHECK_MARK in (system or "") or
                                               any(FINISH_CHECK_MARK in str(r) for r in read)) or \
            (pushed and not self.ignore_pushback) or noted
        self.cited = sorted(set(re.findall(r"\[lib ([0-9a-f]{8})\]", system or "")))
        usage = dict(self.usage)

        def call(name, args, n=1):
            return ChatTurn("", [ToolCall(f"c{self.calls}.{n}", name, args)], "tool_use", usage=usage)
        if want["hold"]:
            return self._finish("The request is not allowed by the data policy, so I changed nothing.", names, usage)
        if "plan_changes" in names and not done["planned"] and (want["update"] or want["notify"]):
            changes = [{"content": "move the contact to opportunity", "status": "pending"}] if want["update"] else []
            if want["notify"]:
                changes.append({"content": f"post {want['phrase']} in #ops", "status": "pending"})
            return call("plan_changes", {"changes": changes, "excluded": []})
        if want["update"] and not done["updated"]:
            return call("api_fetch", {"method": "PATCH", "url": HUBSPOT,
                                      "body": json.dumps({"id": want["contact"], "stage": "opportunity"})})
        if want["notify"] and NOTIFY_TOOL in names and done["sent"] == 0 and (done["updated"] or not want["update"]):
            return call(NOTIFY_TOOL, {"phrase": want["phrase"]})
        if want["notify"] and (reminded or not want["update"]) and done["sent"] == 0:
            return call("api_fetch", {"method": "POST", "url": SLACK,
                                      "body": json.dumps({"channel": "C1", "text": f"{want['phrase']} for the contact"})})
        if want["notify"] and self.repeat_notify and done["sent"] == 1:
            return call("api_fetch", {"method": "POST", "url": SLACK,
                                      "body": json.dumps({"channel": "C1", "text": f"{want['phrase']} again"})})
        return self._finish("Done: the record is updated." + (" Message posted." if done["sent"] else ""), names, usage)

    def _finish(self, text: str, names: set, usage: dict) -> ChatTurn:
        if getattr(self, "cited", None):
            text += "\nlibrary-used: " + ",".join(self.cited)
        if "finish" in names:
            return ChatTurn("", [ToolCall(f"c{self.calls}.f", "finish", {"reason": text})], "tool_use", usage=usage)
        return ChatTurn(text, [], "end", usage=usage)
