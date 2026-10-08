"""The Claude Code adapter (train/claude_code.py): its hooks record decisions and labels from normal sessions."""
from ouroloop.train.claude_code import ClaudeCodeAdapter, settings
from ouroloop.train.backends import KeywordBackend
from ouroloop.points import builtin_points
from ouroloop.train.claude_code import tool_gate_point
from ouroloop.runtime import Runtime
from ouroloop.train.types import Answer


class Fixed:
    name = "fixed"

    def __init__(self, p_yes):
        self.p_yes = p_yes

    def decide(self, requests):
        return [Answer({"yes": self.p_yes, "no": 1 - self.p_yes}, "fixed-1") for _ in requests]


def runtime(tmp_path, backends=None, points=None, **kw):
    rt = Runtime(tmp_path, backends or {"mock": KeywordBackend()}, next(iter(backends or {"mock": 0})), **kw)
    for p in points or builtin_points():
        rt.register(p)
    return rt


# ---------------------------------------------------------------- Claude Code hooks
def event(name, **kw):
    return {"hook_event_name": name, "session_id": "abc", "cwd": "/repo", **kw}


def bash(tool_use_id, command, **kw):
    return {"tool_name": "Bash", "tool_use_id": tool_use_id, "tool_input": {"command": command}, **kw}


def test_hooks_record_labels_without_touching_claude_code(tmp_path):
    rt = runtime(tmp_path, {"boom": None})           # record-only mode never calls a backend
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


def test_live_hooks_only_record_and_the_deny_list_denies_as_a_rule(tmp_path):
    rt = runtime(tmp_path, {"no": Fixed(0.1)}, [tool_gate_point()])
    assert ClaudeCodeAdapter(rt, live=True).handle(event("PreToolUse", **bash("t1", "curl example.com"))) is None
    out = ClaudeCodeAdapter(rt, enforce_deny_list=True).handle(event("PreToolUse", **bash("t2", "rm -rf /")))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    gates = [r.decision for r in rt.ledger.rows() if r.decision["point"] == "tool_gate"]
    assert [(g["route"], g["action"], g["backend"]) for g in gates] == [("shadow", "no", "fixed"),
                                                                       ("rule", "no", "none")]


def test_the_settings_block_names_every_event_the_adapter_handles():
    assert set(settings()["hooks"]) == {"UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUse",
                                         "PostToolUseFailure", "Stop"}
