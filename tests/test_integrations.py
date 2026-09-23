import io
import json
import threading

import httpx
import pytest

from ouroloop.adapters.claude_code import ClaudeCodeAdapter, settings
from ouroloop.backends import KeywordBackend
from ouroloop.backends.http import HttpBackend
from ouroloop.cli import ledger_stats, main
from ouroloop.points import builtin_points, tool_gate_point
from ouroloop.policy import Policy
from ouroloop.runtime import Runtime
from ouroloop.serve import make_server
from ouroloop.types import Answer, Question, Request


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


# ---------------------------------------------------------------- decision service
def test_serve_and_http_backend(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    with pytest.raises(ValueError, match="needs a token"):
        make_server(rt, host="0.0.0.0", port=0)
    server = make_server(rt, port=0, token="secret")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        assert httpx.get(f"{url}/health").json()["points"] == ["compact", "retry", "route", "stop", "tool_gate"]
        assert httpx.post(f"{url}/decide", json={"requests": []}).status_code == 401
        monkeypatch.setenv("OUROLOOP_TOKEN", "secret")
        q = Question("noul", "transient?", {"true": "network timeout", "false": "syntax error"})
        ans = HttpBackend(url).decide([Request("network timeout", q), Request("syntax error", q)])
        assert [a.label for a in ans] == ["yes", "no"] and ans[0].model == "mock-keyword"
        auth = {"Authorization": "Bearer secret"}
        d = httpx.post(f"{url}/ask", headers=auth, json={"point": "retry", "session": "s1",
                       "ctx": {"command": "pip install x", "stderr": "ConnectionResetError", "exit_code": 1}}).json()
        assert d["point"] == "retry" and d["route"] == "shadow"
        assert httpx.post(f"{url}/outcome", headers=auth, json={"decision": d["id"], "label": "yes"}).json()["ok"]
        assert httpx.post(f"{url}/ask", headers=auth, json={"point": "nope", "ctx": {}}).status_code == 400
        (row,) = [r for r in rt.ledger.rows() if r.decision["point"] == "retry"]
        assert row.target() == ({"yes": 1.0}, 1.0)
        st = ledger_stats(rt.ledger.rows())["retry"]
        assert st["decisions"] == 1 and st["labelled"] == 1 and st["scored"] == 1
        assert st["agree"] == int(row.decision["probs"][0] >= 0.5)
        assert st["degenerate"] and st["baseline"] == 1.0   # one label: a constant answer is always right
    finally:
        server.shutdown()


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


def test_live_gate_mode_asks_a_person_and_the_deny_list_denies(tmp_path):
    gate = tool_gate_point(policy=Policy(mode="gate", threshold=0.6, conservative="no", fallback="human"))
    rt = runtime(tmp_path, {"no": Fixed(0.1)}, [gate])
    out = ClaudeCodeAdapter(rt, live=True).handle(event("PreToolUse", **bash("t1", "curl example.com")))
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    out = ClaudeCodeAdapter(rt, enforce_deny_list=True).handle(event("PreToolUse", **bash("t2", "rm -rf /")))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_command_reads_stdin_and_never_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event("PreToolUse", **bash("t1", "ls")))))
    assert main(["hook", "claude-code"]) == 0
    assert list((tmp_path / ".ouroloop" / "ledger").glob("*.jsonl"))
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert main(["hook", "claude-code"]) == 0 and "ouroloop hook" in capsys.readouterr().err
    assert set(settings()["hooks"]) == {"UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUse",
                                         "PostToolUseFailure", "Stop"}
