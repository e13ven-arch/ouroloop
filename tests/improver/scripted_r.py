"""A scripted R for the improver's tests: the research model behind the round's three kinds of call.

- diagnosis (prompts/diagnose.md): reads the case's features and names a cause — the notify tasks' failure (a failed
  slack_message_contains check) is `missed_action`, anything else `other`; a memory entry about #ops is `missed_action`;
- the coding session (prompts/propose.md): writes the candidate `mode` asks for and finishes with its declaration —
  "tool": a package tool that posts the phrase in #ops (tools/post_channel_message.py) with a toolcheck script per
  claimed task; "hook": an on_finish hook that pushes back while a requested #ops post is missing; "none": finishes
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
    if "posted" not in said:
        return {"pushback": "Keep working on the task: check whether the request asks for a message in #ops."}
    return None
'''
HOOK_ENTRY = {"name": "remind_ops", "site": "on_finish", "entrypoint": "middlewares/remind_ops.py:remind"}
CONTACTS = {"rsi.notify_after_update": "c2", "rsi.notify_after_update_variant": "c3", "rsi.update_stage": "c1"}


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
        assignment = (first.get("assignments") or [{}])[0]
        if self.mode == "none" or not assignment:
            return self._calls(("finish", {"candidates": []}))
        if turns == 0:
            return self._calls(("read_file", {"path": "package/manifest.json"}),
                               ("read_file", {"path": "inputs/causes.json"}))
        reads = [r for m in messages if m.get("role") == "tool" for r in m.get("results") or ()]
        manifest = json.loads(next(r["output"] for r in reads if r["name"] == "read_file"
                                   and r["output"].lstrip().startswith("{\n \"api_version\"")))
        causes = json.loads(next(r["output"] for r in reads if r["name"] == "read_file"
                                 and r["output"].lstrip().startswith("[")))
        tasks = sorted({t for c in causes for t in c["tasks"]})
        if self.mode == "tool":
            code = TOOL_CODE
            manifest["tools"] = [*manifest.get("tools", []), TOOL_ENTRY]
            files = {"candidates/notify/tools/post_channel_message.py": code}
            decl = {"dir": "notify", "cause_id": assignment["cause_id"], "kind": "tool", "site": TOOL_ENTRY["name"],
                    "predicted_affected": tasks, "trigger": "a request asks for a message in #ops",
                    "scripts": self.scripts if self.scripts is not None else {t: script_for(t) for t in tasks}}
        else:
            manifest["hooks"] = [*manifest.get("hooks", []), HOOK_ENTRY]
            files = {"candidates/notify/middlewares/remind_ops.py": HOOK_CODE}
            decl = {"dir": "notify", "cause_id": assignment["cause_id"], "kind": "hook", "site": "on_finish",
                    "predicted_affected": tasks, "trigger": "the run finishes while #ops was asked for"}
        if turns == 1:
            return self._calls(*[("write_file", {"path": p, "text": t}) for p, t in files.items()],
                               ("write_file", {"path": "candidates/notify/manifest.json",
                                               "text": json.dumps(manifest, indent=1) + "\n"}))
        if turns == 2:
            return self._calls(("run_check", {"dir": "notify", "declaration": decl}))
        return self._calls(("finish", {"candidates": [decl]}))

    def _calls(self, *calls) -> ChatTurn:
        n = len(self.calls)
        return ChatTurn("", [ToolCall(f"r{n}.{i}", name, args) for i, (name, args) in enumerate(calls)], "tool_use",
                        usage=dict(self.usage))
