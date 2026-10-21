"""A scripted R for the improver's tests: the research model behind the round's three kinds of call.

- diagnosis (prompts/diagnose.md): reads the case's features and names a cause — the notify tasks' failure (a failed
  slack_message_contains check) is `missed_action`, anything else `other`; a memory entry about #ops is `missed_action`;
- the coding session (prompts/propose.md): writes the candidate package `mode` asks for into candidates/package and
  finishes with its declaration — "tool": a package tool that posts the phrase in #ops (tools/post_channel_message.py)
  with a toolcheck script per claimed task; "hook": an on_finish hook that pushes back while a requested #ops post is
  missing; "both": the tool and the hook in one package (two components, one mechanism); "two": the tool in
  candidates/package and the hook in candidates/package-2, two packages that stack; "two_conflict": the second package
  carries the hook and its own copy of the tool file, so it collides with the first when stacked; "none": finishes
  with nothing; v1.9: "trial" first hands in a hook whose pushback the actor does not heed (it acts, so V2b passes,
  and fixes nothing in the trial), then, told so by the trial, rewrites it into the hook that works; "trial_bad"
  hands the weak hook in again after every trial; "merge_lost" merges the frontier entry into a hook that never
  acts where the entry fixed a task (the merge's replay sends it back);
- the playbook (prompts/playbook.md): one lesson citing the first ledger row it is shown, and one that cites nothing.

It never looks at a task's split or answer: what it knows is what the host put in its inputs and trajectories.
"""
from __future__ import annotations

import json
import re

from ouroloop.runtime.chat import ChatTurn, ToolCall

SLACK = "https://slack.com/api/chat.postMessage"
HUBSPOT = "https://api.hubapi.com/crm/v3/objects/contacts"
TOOL_CODE = '''import json

SLACK = "https://slack.com/api/chat.postMessage"


def run(ctx, args):
    phrase = str((args or {}).get("phrase") or "").strip()
    if not phrase:
        return {"output": "give the phrase the message must contain", "is_error": True}
    res = ctx.tool("api_fetch", {"method": "POST", "url": SLACK,
                                 "body": json.dumps({"channel": "C1", "text": phrase})})
    return {"output": f"posted to the ops channel: {res.output}", "is_error": bool(res.is_error)}
'''
TOOL_ENTRY = {"name": "post_channel_message", "description": "Post a message containing a given phrase in the ops "
              "channel. Use it whenever the request asks for a message in #ops.",
              "input_schema": {"type": "object", "properties": {"phrase": {"type": "string"}},
                               "required": ["phrase"], "additionalProperties": False},
              "entrypoint": "tools/post_channel_message.py:run", "writes": True}
HOOK_CODE = '''def remind(ctx, event):
    said = (str(event.get("reason") or "") + " " + str(event.get("final_text") or "")).lower()
    if "in #ops" in said and "posted" not in said:
        return {"pushback": "Keep working on the task: check whether the request asks for a message in #ops."}
    return None
'''
# the hook finance-v7 installed, in miniature: it speaks whenever nothing was posted, so on passing tasks that asked for
# no post too — V2b's false-trigger gate rejects it (v1.7)
NOISY_HOOK_CODE = HOOK_CODE.replace('"in #ops" in said and "posted" not in said', '"posted" not in said')
HOOK_ENTRY = {"name": "remind_ops", "site": "on_finish", "entrypoint": "middlewares/remind_ops.py:remind"}
# v1.9: a hook that acts (V2b counts a trigger) and whose pushback the actor does not read as a reminder (tests/rsi.py
# heeds a pushback that starts with PUSHBACK): the trial shows it fixes nothing, and R rewrites it into HOOK_CODE
WEAK_HOOK_CODE = HOOK_CODE.replace(
    '"Keep working on the task: check whether the request asks for a message in #ops."',
    '"Please double-check whether the request asks for a message in #ops."')
TRIAL_MARK = "Trial of candidates/"      # how the host's trial feedback starts (improver/trial.py)
# v1.8 (DESIGN §6.18): the global heuristic — "the request mentions #ops and nothing was posted: post" — passes V2b
# (no studied passing task mentions #ops without a post) and, on the held-out quiet task (rsi.QUIET, whose request
# forbids a post), pushes a suggestible actor into posting: a partial package, one fix per notify task and one
# confirmed regression
PARTIAL_HOOK_CODE = '''def remind(ctx, event):
    said = (str(event.get("reason") or "") + " " + str(event.get("final_text") or "")).lower()
    if "#ops" in said and "posted" not in said:
        return {"pushback": "Keep working on the task: the request mentions #ops and nothing was posted there. "
                            "Post the update in #ops."}
    return None
'''
# a merge that narrows the entry's hook until it never acts where the entry fixed a task
LOST_HOOK_CODE = PARTIAL_HOOK_CODE.replace('if "#ops" in said and "posted" not in said:',
                                          'if "#ops" in said and "posted" not in said and "never" in said:')
# its conditioned version: it acts only where the agent's own plan lists a post in #ops that was not made — the
# condition comes from what the agent read for this task, not from the channel's name
CONDITIONAL_HOOK_CODE = '''import re


def remind(ctx, event):
    said = (str(event.get("reason") or "") + " " + str(event.get("final_text") or "")).lower()
    if re.search(r"post [^;.]+ in #ops", said) and "posted" not in said:
        return {"pushback": "Keep working on the task: a message in #ops that the plan lists was not posted."}
    return None
'''
# a hook whose trigger is a list of words: V0 notes it (heuristic_terms), the ladder does not refuse it
WORDLIST_HOOK_CODE = '''STATUSES = ("pending", "reserved", "on leave", "archived")


def remind(ctx, event):
    said = str(event.get("final_text") or "").lower()
    if any(s in said for s in STATUSES) and "posted" not in said:
        return {"pushback": "Keep working on the task: check the rows with a status in the list."}
    return None
'''
# v1.6: an audit — a hook that asks the aux model what is missing, from the agent's final text and its writes
AUDIT_CODE = '''import json


def audit(ctx, event):
    writes = []
    for e in ctx.view.get("events") or ():
        args = e.get("args") or {}
        if e.get("name") == "api_fetch" and str(args.get("method", "GET")).upper() not in ("GET", "HEAD"):
            writes.append(f"{args.get('method')} {args.get('url')}")
    prompt = "Final text: " + str(event.get("final_text") or "") + "\\nWrites: " + "; ".join(writes)
    reply = ctx.model([{"role": "system", "content": "You audit the agent's work."},
                       {"role": "user", "content": prompt}], role="aux")
    try:
        items = json.loads(reply.text)
    except Exception:
        items = []
    items = [str(i) for i in items if str(i).strip()]
    return {"missing": items} if items else None
'''
AUDIT_ENTRY = {"name": "audit", "site": "on_finish", "entrypoint": "middlewares/audit.py:audit"}
CONTACTS = {"rsi.notify_after_update": "c2", "rsi.notify_after_update_variant": "c3", "rsi.update_stage": "c1",
            "rsi.notify_ticket": "c4"}
MECHANISM = {"tool": "a package tool posts the requested #ops message so the agent does not have to compose the call",
             "hook": "an on_finish hook pushes back while a requested #ops post is missing",
             "both": "a tool posts the requested #ops message and an on_finish hook pushes back while it is missing",
             "audit": "an on_finish audit asks the aux model which planned items have no write and names them",
             "audit_noisy": "an on_finish audit asks the aux model which planned items have no write and names them",
             "partial": "an on_finish hook pushes back whenever the request mentions #ops and nothing was posted",
             "merge": "an on_finish hook pushes back only where the agent's plan lists a post in #ops not yet made",
             "merge_bad": "an on_finish hook pushes back whenever the request mentions #ops and nothing was posted",
             "merge_lost": "an on_finish hook pushes back only where the request says never and #ops",
             "trial": "an on_finish hook asks the agent to double-check the #ops message before it finishes",
             "trial_bad": "an on_finish hook asks the agent to double-check the #ops message before it finishes",
             "debug": "an on_finish hook pushes back while a requested #ops post is missing"}
TOOL_CODE_2 = TOOL_CODE.replace("posted to the ops channel", "sent to #ops")     # the same tool, another body


def script_for(task_id: str, phrase: str = "stage updated") -> str:
    contact = CONTACTS.get(task_id, "c1")
    return ("import json\n\ndef check(ctx, tools):\n"
            f"    ctx.tool('api_fetch', {{'method': 'PATCH', 'url': '{HUBSPOT}', "
            f"'body': json.dumps({{'id': '{contact}', 'stage': 'opportunity'}})}})\n"
            f"    tools['post_channel_message']({{'phrase': '{phrase}'}})\n")


class ScriptedR:
    name = "scripted-research"
    model = "scripted-research"
    effort = "medium"
    retries = 0
    timeout = 60

    def __init__(self, mode: str = "tool", *, scripts: dict | None = None, usage: dict | None = None):
        self.mode = mode
        self.scripts = scripts
        self.usage = usage or {"input_tokens": 500, "output_tokens": 100}
        self.calls: list[str] = []
        self.read: list = []            # (file name, text) of every inputs/ file the session read back (v1.8)
        self.trial_feedback: list = []  # v1.9: the host's trial feedback texts, as the session received them
        self.tries: list = []           # v1.11: what try_task answered, in order (mode debug)
        self.system_seen = ""           # the session's system prompt, as last given
        self._read_ids: set = set()

    def chat(self, system: str, messages: list[dict], specs, **_kw) -> ChatTurn:
        system = system or ""
        if system.startswith("You diagnose"):
            self.calls.append("diagnose")
            return self._diagnose(json.loads(messages[0]["content"]))
        if system.startswith("You keep the research playbook"):
            self.calls.append("playbook")
            rows = json.loads(messages[0]["content"]).get("ledger_rows") or []
            first = rows[0]["id"] if rows else "L1"
            return self._text(f"- A tool that posts the requested message fixed missed_action [{first}]\n"
                              f"- An uncited lesson\n")
        if system.startswith("You improve the harness package"):
            self.calls.append("session")
            self.system_seen = system
            self._record_reads(messages)
            self._record_tries(messages)
            return self._session(messages)
        if system.startswith("You audit the agent's work"):          # the audit's dry run (V2d) asks the research model
            import rsi
            self.calls.append("audit")
            return self._text(json.dumps(rsi.audit_reply(messages, noisy=self.mode == "audit_noisy")))
        raise AssertionError(f"a request ScriptedR does not know: {system[:80]!r}")

    # ------------------------------------------------------------------ diagnosis
    def _text(self, text: str) -> ChatTurn:
        return ChatTurn(text, [], "end", usage=dict(self.usage))

    def _diagnose(self, user: dict) -> ChatTurn:
        if "memory_entry" in user:
            text = json.dumps(user["memory_entry"])
            return self._text(json.dumps({"cause": "missed_action" if "#ops" in text else "other", "note": ""}))
        types = (user.get("features") or {}).get("failed_assertion_types") or {}
        tail = user.get("trajectory_tail") or []
        refs = [m["ref"] for m in tail if m.get("ref")][-2:]
        if "slack_message_contains" in types:
            return self._text(json.dumps({"cause": "missed_action", "evidence_refs": refs,
                                          "note": "The agent updated the record and finished without posting the "
                                                  "requested message in #ops."}))
        return self._text(json.dumps({"cause": "other", "proposed": "unclear_request", "evidence_refs": refs,
                                      "note": "Not clear."}))

    # ------------------------------------------------------------------ the session
    def _record_tries(self, messages: list[dict]) -> None:
        for m in messages:
            if m.get("role") != "tool":
                continue
            for r in m.get("results") or ():
                if r.get("name") == "try_task" and r.get("id") not in self._read_ids:
                    self._read_ids.add(r["id"])
                    self.tries.append((bool(r.get("is_error")), r.get("output") or ""))

    def _record_reads(self, messages: list[dict]) -> None:
        """Keep what read_file answered for inputs/ files (a test reads what R was shown)."""
        paths = {c.id: c.args.get("path") for m in messages if m.get("role") == "assistant"
                 for c in m.get("tool_calls") or () if c.name == "read_file"}
        for m in messages:
            if m.get("role") != "tool":
                continue
            for r in m.get("results") or ():
                path = paths.get(r.get("id"))
                if path and str(path).startswith("inputs/") and r.get("id") not in self._read_ids:
                    self._read_ids.add(r["id"])
                    self.read.append((str(path).rpartition("/")[2], r.get("output") or ""))

    def _session(self, messages: list[dict]) -> ChatTurn:
        turns = sum(1 for m in messages if m.get("role") == "assistant")
        first = json.loads(messages[0]["content"])
        ranked = (first.get("focus") or {}).get("ranked") or []
        if self.mode == "none" or not ranked:
            return self._calls(("finish", {"packages": []}))
        if turns == 0:
            return self._calls(("read_file", {"path": "package/manifest.json"}),
                               ("read_file", {"path": "inputs/causes.json"}))
        reads = [r for m in messages if m.get("role") == "tool" for r in m.get("results") or ()]
        manifest = json.loads(next(r["output"] for r in reads if r["name"] == "read_file"
                                   and r["output"].lstrip().startswith("{\n \"api_version\"")))
        causes = json.loads(next(r["output"] for r in reads if r["name"] == "read_file"
                                 and r["output"].lstrip().startswith("[")))
        cause_ids = [c["cause_id"] for c in causes]
        tasks = sorted({t for c in causes for t in c["tasks"]})
        if self.mode in ("two", "two_conflict"):
            return self._two(turns, manifest, cause_ids, tasks)
        if self.mode in ("merge", "merge_bad", "merge_lost"):
            return self._merge(turns, manifest, (first.get("focus") or {}).get("merge") or [], tasks)
        if self.mode in ("trial", "trial_bad"):
            return self._trial(turns, manifest, cause_ids, tasks, messages)
        if self.mode == "debug":
            return self._debug(turns, manifest, cause_ids, tasks)
        files, components = {}, []
        if self.mode == "partial":
            manifest["hooks"] = [*manifest.get("hooks", []), HOOK_ENTRY]
            files["candidates/package/middlewares/remind_ops.py"] = PARTIAL_HOOK_CODE
            components.append({"kind": "hook", "site": "on_finish", "cause_ids": cause_ids,
                               "predicted_affected": tasks, "trigger": "the run finishes with #ops mentioned and "
                                                                       "nothing posted"})
        if self.mode in ("tool", "both"):
            manifest["tools"] = [*manifest.get("tools", []), TOOL_ENTRY]
            files["candidates/package/tools/post_channel_message.py"] = TOOL_CODE
            components.append({"kind": "tool", "site": TOOL_ENTRY["name"], "cause_ids": cause_ids,
                               "predicted_affected": tasks, "trigger": "a request asks for a message in #ops",
                               "scripts": self.scripts if self.scripts is not None
                               else {t: script_for(t) for t in tasks}})
        if self.mode in ("hook", "both"):
            manifest["hooks"] = [*manifest.get("hooks", []), HOOK_ENTRY]
            files["candidates/package/middlewares/remind_ops.py"] = HOOK_CODE
            components.append({"kind": "hook", "site": "on_finish", "cause_ids": cause_ids,
                               "predicted_affected": tasks, "trigger": "the run finishes while #ops was asked for"})
        if self.mode in ("audit", "audit_noisy"):
            manifest["hooks"] = [*manifest.get("hooks", []), AUDIT_ENTRY]
            files["candidates/package/middlewares/audit.py"] = AUDIT_CODE
            components.append({"kind": "hook", "site": "on_finish", "cause_ids": cause_ids,
                               "predicted_affected": tasks, "trigger": "the run finishes", "uses_model": True})
        decl = {"mechanism": MECHANISM[self.mode], "components": components}
        if turns == 1:
            return self._calls(*[("write_file", {"path": p, "text": t}) for p, t in files.items()],
                               ("write_file", {"path": "candidates/package/manifest.json",
                                               "text": json.dumps(manifest, indent=1) + "\n"}))
        if turns == 2:
            return self._calls(("run_check", {"declaration": decl}))
        return self._calls(("finish", {"packages": [decl]}))

    def _trial(self, turns: int, manifest: dict, cause_ids: list, tasks: list, messages: list) -> ChatTurn:
        """v1.9: the weak hook first; after the host's trial feedback, the working hook (mode trial) or the weak one
        again (mode trial_bad), then finish with the same declaration."""
        manifest["hooks"] = [*manifest.get("hooks", []), HOOK_ENTRY]
        decl = {"mechanism": MECHANISM[self.mode], "components": [
            {"kind": "hook", "site": "on_finish", "cause_ids": cause_ids, "predicted_affected": tasks,
             "trigger": "the run finishes while #ops was asked for"}]}
        last = messages[-1] if messages else {}
        if last.get("role") == "user" and str(last.get("content") or "").startswith(TRIAL_MARK):
            self.trial_feedback.append(str(last["content"]))
            n = len(self.trial_feedback)
            code = HOOK_CODE if self.mode == "trial" else WEAK_HOOK_CODE + f"\n# revision {n}\n"
            return self._calls(("write_file", {"path": "candidates/package/middlewares/remind_ops.py",
                                              "text": code}))
        if turns == 1:
            return self._calls(("write_file", {"path": "candidates/package/middlewares/remind_ops.py",
                                              "text": WEAK_HOOK_CODE}),
                               ("write_file", {"path": "candidates/package/manifest.json",
                                              "text": json.dumps(manifest, indent=1) + "\n"}))
        if turns == 2:
            return self._calls(("run_check", {"declaration": decl}))
        return self._calls(("finish", {"packages": [decl]}))

    def _debug(self, turns: int, manifest: dict, cause_ids: list, tasks: list) -> ChatTurn:
        """v1.11 (DESIGN §6.21): the probe — the weak hook, try_task on the studied failure (it acts, the agent does
        not heed it: FAILED), the working hook, try_task again (PASSED, one rollout), then finish with the scope."""
        manifest["hooks"] = [*manifest.get("hooks", []), HOOK_ENTRY]
        task = tasks[0]
        decl = {"mechanism": MECHANISM["debug"], "scope": "invariant", "components": [
            {"kind": "hook", "site": "on_finish", "cause_ids": cause_ids, "predicted_affected": tasks,
             "trigger": "the run finishes while #ops was asked for"}]}
        if turns == 1:
            return self._calls(("write_file", {"path": "candidates/package/middlewares/remind_ops.py",
                                              "text": WEAK_HOOK_CODE}),
                               ("write_file", {"path": "candidates/package/manifest.json",
                                              "text": json.dumps(manifest, indent=1) + "\n"}))
        if turns == 2:
            return self._calls(("try_task", {"task": task}))
        if turns == 3:
            return self._calls(("write_file", {"path": "candidates/package/middlewares/remind_ops.py",
                                              "text": HOOK_CODE}))
        if turns == 4:
            return self._calls(("try_task", {"task": task}))
        if turns == 5:         # a third try on the same task: refused (try_per_task 2), the session goes on
            return self._calls(("try_task", {"task": task}))
        return self._calls(("finish", {"packages": [decl]}))

    def _merge(self, turns: int, manifest: dict, picks: list, tasks: list) -> ChatTurn:
        """v1.8: the frontier entry focus.json names, merged — the conditioned hook in candidates/<candidate_dir>/
        declared merge_of (mode merge); mode merge_bad hands the global hook in again under the same declaration, so
        the merge is rejected for the same regression."""
        if not picks:
            return self._calls(("finish", {"packages": []}))
        pick = picks[0]
        d = pick.get("candidate_dir") or "merge"
        manifest["hooks"] = [*manifest.get("hooks", []), HOOK_ENTRY]
        # merge_bad: the global hook again, worded differently (the same files would be the archive's duplicate and
        # not screened at all)
        code = CONDITIONAL_HOOK_CODE if self.mode == "merge" else LOST_HOOK_CODE if self.mode == "merge_lost" else \
            PARTIAL_HOOK_CODE.replace("Post the update in #ops.", "Post an update in #ops now.")
        decl = {"dir": d, "mechanism": MECHANISM[self.mode], "merge_of": pick["entry"],
                "components": [{"kind": "hook", "site": "on_finish", "cause_ids": list(pick["cause_ids"]),
                                "predicted_affected": tasks,
                                "trigger": "the run finishes with a planned #ops post not made"}]}
        if turns == 1:
            return self._calls(("read_file", {"path": "inputs/frontier.json"}),
                               ("read_file", {"path": "inputs/regressions.json"}),
                               ("write_file", {"path": f"candidates/{d}/middlewares/remind_ops.py", "text": code}),
                               ("write_file", {"path": f"candidates/{d}/manifest.json",
                                               "text": json.dumps(manifest, indent=1) + "\n"}))
        if turns == 2:
            return self._calls(("run_check", {"dir": d, "declaration": decl}))
        return self._calls(("finish", {"packages": [decl]}))

    def _two(self, turns: int, manifest: dict, cause_ids: list, tasks: list) -> ChatTurn:
        """Two packages: the tool in candidates/package, the hook in candidates/package-2 (plus, in two_conflict mode,
        the hook package's own copy of the tool file)."""
        tool_manifest = {**manifest, "tools": [*manifest.get("tools", []), TOOL_ENTRY]}
        hook_manifest = {**manifest, "hooks": [*manifest.get("hooks", []), HOOK_ENTRY]}
        files = {"candidates/package/tools/post_channel_message.py": TOOL_CODE,
                 "candidates/package/manifest.json": json.dumps(tool_manifest, indent=1) + "\n",
                 "candidates/package-2/middlewares/remind_ops.py": HOOK_CODE}
        tool = {"kind": "tool", "site": TOOL_ENTRY["name"], "cause_ids": cause_ids, "predicted_affected": tasks,
                "trigger": "a request asks for a message in #ops",
                "scripts": self.scripts if self.scripts is not None else {t: script_for(t) for t in tasks}}
        hook = {"kind": "hook", "site": "on_finish", "cause_ids": cause_ids, "predicted_affected": tasks,
                "trigger": "the run finishes while #ops was asked for"}
        second = [hook]
        if self.mode == "two_conflict":
            hook_manifest["tools"] = [*hook_manifest.get("tools", []), TOOL_ENTRY]
            files["candidates/package-2/tools/post_channel_message.py"] = TOOL_CODE_2
            second = [hook, tool]
        files["candidates/package-2/manifest.json"] = json.dumps(hook_manifest, indent=1) + "\n"
        decls = [{"dir": "package", "mechanism": MECHANISM["tool"], "components": [tool]},
                 {"dir": "package-2", "mechanism": MECHANISM["hook"] + (" with its own copy of the tool"
                                                                        if self.mode == "two_conflict" else ""),
                  "components": second}]
        if turns == 1:
            return self._calls(*[("write_file", {"path": p, "text": t}) for p, t in files.items()])
        if turns == 2:
            return self._calls(("run_check", {"declaration": decls[0]}),
                               ("run_check", {"dir": "package-2", "declaration": decls[1]}))
        return self._calls(("finish", {"packages": decls}))

    def _calls(self, *calls) -> ChatTurn:
        n = len(self.calls)
        return ChatTurn("", [ToolCall(f"r{n}.{i}", name, args) for i, (name, args) in enumerate(calls)], "tool_use",
                        usage=dict(self.usage))
