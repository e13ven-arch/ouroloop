"""A scripted R for the improver's tests: the research model behind the round's three kinds of call.

- diagnosis (prompts/diagnose.md): reads the case's features and names a cause — the notify tasks' failure (a failed
  slack_message_contains check) is `missed_action`, anything else `other`; a memory entry about #ops is `missed_action`;
- the coding session (prompts/propose.md): writes the candidate package `mode` asks for into candidates/package and
  finishes with its declaration — "tool": a package tool that posts the phrase in #ops (tools/post_channel_message.py)
  with a toolcheck script per claimed task; "hook": an on_finish hook that pushes back while a requested #ops post is
  missing; "both": the tool and the hook in one package (two components, one mechanism); "two": the tool in
  candidates/package and the hook in candidates/package-2, two packages that stack; "two_conflict": the second package
  carries the hook and its own copy of the tool file, so it collides with the first when stacked; "none": finishes
  with nothing;
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
             "audit_noisy": "an on_finish audit asks the aux model which planned items have no write and names them"}
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
        files, components = {}, []
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
