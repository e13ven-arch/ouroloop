"""Claude Code hooks adapter: turns ordinary Claude Code sessions into ouroloop decisions and outcomes.

`ouroloop hook print-settings` prints the settings.json block to add. By default the adapter only records: it asks
no model and changes nothing in Claude Code, so it adds almost no latency. Labels:

    tool_gate   a permission prompt was shown and the tool then ran           -> "yes" (human)
                no prompt (your permission rules allowed it) and the tool ran  -> "yes" (human, weight 0.5)
                a prompt was shown and the tool had not run when the turn ended -> "no" (human)
    retry       a failed Bash command rerun unchanged as the very next Bash call -> the rerun's result

With live=True the backend is asked at hook time, and its answers are recorded. The adapter never allows a call: every
call still goes through your own permission rules, so a "yes" above is always a person's, or their rules'. With
enforce_deny_list=True, a command on the deny list is denied, and the veto is recorded on tool_gate (route "rule").
"""
from __future__ import annotations

import fcntl
import json
import re
from contextlib import contextmanager
from pathlib import Path

from ..agent.deny import denied

TOOLS = {"Bash": "bash", "Write": "write", "Edit": "edit", "MultiEdit": "edit"}
EVENTS = ("UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUse", "PostToolUseFailure", "Stop")


def detail(tool: str, args: dict) -> str:
    if tool == "Bash":
        return f"command: {args.get('command', '')}"
    if tool == "Write":
        content = str(args.get("content", ""))
        return f"write {args.get('file_path')} ({len(content)} characters):\n{content[:800]}"
    old = str(args.get("old_string", "")) or json.dumps(args.get("edits", ""))[:400]
    return f"edit {args.get('file_path')}\nreplace:\n{old[:400]}\nwith:\n{str(args.get('new_string', ''))[:400]}"


def deny(event: dict) -> dict | None:
    """The deny list's "deny" for a PreToolUse event of a Bash command on it, else None. It needs no runtime, so the
    hook still denies when the runtime cannot be built or cannot record."""
    command = str((event.get("tool_input") or {}).get("command", ""))
    rule = denied(command) if (event.get("hook_event_name"), event.get("tool_name")) == ("PreToolUse", "Bash") else None
    return rule and {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                            "permissionDecisionReason": f"ouroloop deny list: {rule}"}}


def settings(command: str = "ouroloop hook claude-code") -> dict:
    """The hooks block for .claude/settings.json (or ~/.claude/settings.json)."""
    handler = [{"type": "command", "command": command}]
    tools = "Bash|Write|Edit|MultiEdit"
    return {"hooks": {
        "UserPromptSubmit": [{"hooks": handler}],
        "PreToolUse": [{"matcher": tools, "hooks": handler}],
        "PermissionRequest": [{"matcher": tools, "hooks": handler}],
        "PostToolUse": [{"matcher": tools, "hooks": handler}],
        "PostToolUseFailure": [{"matcher": tools, "hooks": handler}],
        "Stop": [{"hooks": handler}],
    }}


class ClaudeCodeAdapter:
    def __init__(self, runtime, live: bool = False, enforce_deny_list: bool = False):
        self.rt = runtime
        self.live = live
        self.enforce_deny_list = enforce_deny_list
        self.state_dir = runtime.root / "hooks"

    @contextmanager
    def _state(self, session: str):
        """Per-session state shared by concurrent hook processes, under an exclusive file lock."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', session)}.json"
        with open(path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read()
                state = json.loads(raw) if raw.strip() else {"task": "", "gates": {}, "retry": None}
                yield state
                f.seek(0)
                f.truncate()
                f.write(json.dumps(state))
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def handle(self, event: dict) -> dict | None:
        name, session = event.get("hook_event_name"), f"cc_{event.get('session_id', 'unknown')}"
        with self._state(session) as st:
            if name == "UserPromptSubmit":
                st["task"] = str(event.get("prompt", ""))[:300]
            elif name == "PreToolUse":
                return self._pre(event, st, session)
            elif name == "PermissionRequest":
                gate = st["gates"].get(event.get("tool_use_id", ""))
                if gate:
                    gate["asked"] = True
            elif name in ("PostToolUse", "PostToolUseFailure"):
                self._post(event, st, session)
            elif name == "Stop":
                self._stop(st)
        return None

    def _decide(self, point: str, ctx: dict, session: str, veto: str | None = None):
        if self.live:
            return self.rt.ask(point, ctx, session=session, veto=veto)
        return self.rt.record(point, ctx, session=session, veto=veto)

    def _pre(self, event: dict, st: dict, session: str) -> dict | None:
        """Only ever returns the deny list's "deny", or nothing."""
        tool, args = event.get("tool_name", ""), event.get("tool_input") or {}
        if tool not in TOOLS:
            return None
        ctx = {"task": st.get("task", ""), "workdir": event.get("cwd", ""), "tool": TOOLS[tool],
               "detail": detail(tool, args)}
        if self.enforce_deny_list and (out := deny(event)):
            if "tool_gate" in self.rt.points:
                self._decide("tool_gate", ctx, session, veto="no")
            return out
        if "tool_gate" in self.rt.points:
            d = self._decide("tool_gate", ctx, session)
            st["gates"][event.get("tool_use_id", d.id)] = {"decision": d.id, "asked": False}
        return None

    def _post(self, event: dict, st: dict, session: str) -> None:
        gate = st["gates"].pop(event.get("tool_use_id", ""), None)
        if gate:
            self.rt.outcome(gate["decision"], "yes", source="human", weight=1.0 if gate["asked"] else 0.5,
                            note="ran after a permission prompt" if gate["asked"] else "allowed by permission rules")
        if event.get("tool_name") != "Bash":
            st["retry"] = None
            return
        resp = event.get("tool_response") or {}
        if not isinstance(resp, dict) or resp.get("interrupted"):
            st["retry"] = None
            return
        code = resp.get("exit_code", 1 if event.get("hook_event_name") == "PostToolUseFailure" else 0)
        command = str((event.get("tool_input") or {}).get("command", ""))
        pending, st["retry"] = st.get("retry"), None
        if pending and pending["command"] == command:
            self.rt.outcome(pending["decision"], "yes" if code == 0 else "no", note="rerun by Claude Code")
        elif code != 0 and "retry" in self.rt.points:
            d = self._decide("retry", {"command": command, "stderr": resp.get("stderr") or resp.get("stdout", ""),
                                       "exit_code": code}, session)
            st["retry"] = {"command": command, "decision": d.id}

    def _stop(self, st: dict) -> None:
        for gate in st["gates"].values():
            if gate["asked"]:
                self.rt.outcome(gate["decision"], "no", source="human", note="prompted but not run by the end of the turn")
        st["gates"] = {}
        st["retry"] = None


def settings_json(command: str = "ouroloop hook claude-code") -> str:
    return json.dumps(settings(command), indent=2)


__all__ = ["ClaudeCodeAdapter", "EVENTS", "settings", "settings_json"]
