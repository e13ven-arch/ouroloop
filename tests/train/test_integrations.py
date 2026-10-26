"""The Claude Code adapter (train/claude_code.py): its hooks record decisions and labels from normal sessions, and it
never allows a call."""
from reharness.train.claude_code import ClaudeCodeAdapter, settings, tool_gate_point
from reharness.train.decision import Noul
from reharness.train.runtime import Runtime


def retry_point():
    """A failed command: will rerunning it unchanged succeed? Default "no" (the host does not rerun)."""
    return Noul("retry", "Rerunning this failed command unchanged will succeed",
                {"true": "a transient failure", "false": "a failure that needs a change"},
                view=lambda ctx, params: f"command: {ctx['command']}\nexit code: {ctx['exit_code']}\n{ctx['stderr']}",
                phases={"after_failure": "no"})


def runtime(tmp_path, points=None):
    rt = Runtime(tmp_path)
    for p in points or (tool_gate_point(), retry_point()):
        rt.register(p)
    return rt


def event(name, **kw):
    return {"hook_event_name": name, "session_id": "abc", "cwd": "/repo", **kw}


def bash(tool_use_id, command, **kw):
    return {"tool_name": "Bash", "tool_use_id": tool_use_id, "tool_input": {"command": command}, **kw}


def test_hooks_record_labels_without_touching_claude_code(tmp_path):
    rt = runtime(tmp_path)
    cc = ClaudeCodeAdapter(rt)
    cmd = "pip install -r requirements.txt"
    assert cc.handle(event("UserPromptSubmit", prompt="set up the project")) is None
    assert cc.handle(event("PreToolUse", **bash("t1", cmd))) is None
    cc.handle(event("PermissionRequest", **bash("t1", cmd)))
    cc.handle(event("PostToolUseFailure", **bash("t1", cmd, tool_response={"stderr": "ConnectionResetError",
                                                                             "exit_code": 1})))
    cc.handle(event("PreToolUse", **bash("t2", cmd)))
    cc.handle(event("PostToolUse", **bash("t2", cmd, tool_response={"stdout": "ok", "exit_code": 0})))
    cc.handle(event("PreToolUse", **bash("t3", "rm -rf build")))
    cc.handle(event("PermissionRequest", **bash("t3", "rm -rf build")))
    cc.handle(event("Stop", stop_hook_active=False))
    rows = {}
    for r in rt.ledger.rows():
        rows.setdefault(r.decision["point"], []).append(r)
    gates = [(r.outcomes[0]["label"], r.outcomes[0]["weight"]) for r in rows["tool_gate"]]
    assert gates == [("yes", 1.0), ("yes", 0.5), ("no", 1.0)]
    assert all(r.decision["backend"] == "none" and r.decision["probs"] is None for r in rows["tool_gate"])
    (retry,) = rows["retry"]
    assert retry.outcomes[0]["label"] == "yes" and "set up the project" in rows["tool_gate"][0].decision["view"]


def test_the_adapter_only_ever_denies_and_the_deny_list_denies_as_a_rule(tmp_path):
    rt = runtime(tmp_path, [tool_gate_point()])
    outs = set()
    for adapter in (ClaudeCodeAdapter(rt), ClaudeCodeAdapter(rt, enforce_deny_list=True)):
        for j, command in enumerate(("ls", "touch x; echo reboot")):
            out = adapter.handle(event("PreToolUse", **bash(f"t{j}", command)))
            outs.add((adapter.enforce_deny_list, command, out and out["hookSpecificOutput"]["permissionDecision"]))
    assert outs == {(False, "ls", None), (False, "touch x; echo reboot", None), (True, "ls", None),
                    (True, "touch x; echo reboot", "deny")}
    gates = [r.decision for r in rt.ledger.rows() if r.decision["point"] == "tool_gate"]
    assert [(g["route"], g["action"], g["backend"]) for g in gates] == [("shadow", "no", "none")] * 3 + [
        ("rule", "no", "none")]


def test_the_settings_block_names_every_event_the_adapter_handles():
    assert set(settings("handle-hook")["hooks"]) == {"UserPromptSubmit", "PreToolUse", "PermissionRequest",
                                                     "PostToolUse", "PostToolUseFailure", "Stop"}
    assert settings("handle-hook")["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "handle-hook"}]}]
