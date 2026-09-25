"""The manual provider: the API providers' interface and data protocol, answered through files (DEMO_PLAN §5)."""
import json
import threading
import time

import pytest

from ouroloop import config
from ouroloop.agent import Agent, AgentConfig, Tools
from ouroloop.backends import KeywordBackend
from ouroloop.chat import ChatTurn, ScriptedChat, ToolCall, ToolSpec
from ouroloop.points import builtin_points
from ouroloop.providers import ProviderError
from ouroloop.providers.manual import ManualProvider
from ouroloop.runtime import Runtime

WRITE = ToolSpec("write", "write a file", {"type": "object"})


class Operator(threading.Thread):
    """Answers each request in <dir>/requests with the next reply, as a person (or another process) would: the reply is
    written elsewhere and renamed into place. Keeps the requests it saw."""

    def __init__(self, root, replies):
        super().__init__(daemon=True)
        self.root, self.replies, self.seen = root, list(replies), []

    def run(self):
        deadline = time.time() + 20
        while self.replies and time.time() < deadline:
            for path in sorted((self.root / "requests").glob("*.json")) if (self.root / "requests").exists() else []:
                if (self.root / "responses" / path.name).exists():
                    continue
                request = json.loads(path.read_text())
                self.seen.append((path.name, request))
                tmp = self.root / f".{path.name}"
                tmp.write_text(json.dumps(self.replies.pop(0)))
                (self.root / "responses").mkdir(exist_ok=True)
                tmp.rename(self.root / "responses" / path.name)
            time.sleep(0.01)


def as_reply(turn: ChatTurn) -> dict:
    return {"text": turn.text, "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in turn.tool_calls],
            "stop": turn.stop, "usage": turn.usage}


def test_the_agent_loop_runs_on_the_manual_provider_as_on_an_api_provider(tmp_path):
    script = [ChatTurn("", [ToolCall("t1", "write", {"path": "a.txt", "content": "hello"})], "tool_use",
                       usage={"input_tokens": 12, "output_tokens": 3}),
              ChatTurn("done", [], "end", usage={"input_tokens": 20, "output_tokens": 1})]
    results = {}
    for name in ("scripted", "manual"):
        work = tmp_path / name / "work"
        work.mkdir(parents=True)
        rt = Runtime(tmp_path / name / "ws", {"mock": KeywordBackend()}, "mock", seed=0)
        for p in builtin_points():
            rt.register(p)
        if name == "manual":
            provider = config.build_provider({"type": "manual", "dir": str(tmp_path / "llm"), "poll": 0.01})
            operator = Operator(tmp_path / "llm", [as_reply(t) for t in script])
            operator.start()
        else:
            provider = ScriptedChat(list(script))
        res = Agent(provider, rt, Tools(work), config=AgentConfig(approve="auto")).run("write a.txt")
        results[name] = (res.text, res.stop, res.turns, (work / "a.txt").read_text())
    assert results["manual"] == results["scripted"] == ("done", "end", 2, "hello")
    operator.join(1)
    (_, first), (_, second) = operator.seen
    task = first["messages"][0]
    assert first["system"] and task["role"] == "user" and "write a.txt" in task["content"]
    assert {t["name"] for t in first["tools"]} >= {"write", "read", "bash"}
    assert all(set(t) == {"name", "description", "schema"} for t in first["tools"])
    # the history the second request carries: the assistant's tool call as an object, then the tool's result
    assistant, tool = second["messages"][1], second["messages"][2]
    assert assistant["tool_calls"] == [{"id": "t1", "name": "write", "args": {"path": "a.txt", "content": "hello"}}]
    assert "native" not in assistant and tool["results"][0]["id"] == "t1"


def test_a_reply_is_held_to_what_an_api_provider_can_return(tmp_path):
    def ask(reply, tools=(WRITE,), system="s"):
        provider = ManualProvider(str(tmp_path / "llm"), poll=0.01, timeout=5)
        operator = Operator(tmp_path / "llm", [reply])
        operator.start()
        try:
            return provider.chat(system, [{"role": "user", "content": "hi"}], list(tools))
        finally:
            operator.join(1)

    call = {"id": "c1", "name": "write", "args": {"path": "x"}}
    turn = ask({"text": "", "tool_calls": [call], "stop": "tool_use", "usage": {"input_tokens": 5}})
    assert turn == ChatTurn("", [ToolCall("c1", "write", {"path": "x"})], "tool_use", usage={"input_tokens": 5})
    assert ask({"text": "ok", "stop": "end"}) == ChatTurn("ok", [], "end")
    for bad, why in [({"text": "ok", "stop": "finished"}, "stop"),
                     ({"text": 3, "stop": "end"}, "text"),
                     ({"text": "", "tool_calls": [{**call, "name": "bash"}], "stop": "tool_use"}, "not offered"),
                     ({"text": "", "tool_calls": [{**call, "args": "x"}], "stop": "tool_use"}, "args"),
                     ({"text": "", "tool_calls": [call, call], "stop": "tool_use"}, "unique"),
                     ({"text": "", "tool_calls": [call], "stop": "end"}, "tool_use"),
                     ({"text": "", "stop": "tool_use"}, "tool_use"),
                     ({"text": "ok", "stop": "end", "usage": {"input_tokens": "5"}}, "usage"),
                     ({"text": "ok", "stop": "end", "thinking": "..."}, "unknown"),
                     ({"id": "req_other", "text": "ok", "stop": "end"}, "answers")]:
        with pytest.raises(ProviderError, match=why):
            ask(bad)


def test_complete_is_one_user_message_and_no_tools_and_a_missing_reply_times_out(tmp_path):
    provider = ManualProvider(str(tmp_path / "llm"), poll=0.01, timeout=5)
    operator = Operator(tmp_path / "llm", [{"text": '{"label": "yes"}', "stop": "end"}])
    operator.start()
    assert provider.complete("judge", "is it?") == '{"label": "yes"}'
    operator.join(1)
    (_, request), = operator.seen
    assert (request["system"], request["messages"], request["tools"]) == ("judge", [{"role": "user",
                                                                                     "content": "is it?"}], [])
    with pytest.raises(ProviderError, match="no reply"):
        ManualProvider(str(tmp_path / "quiet"), poll=0.01, timeout=0.05).complete("s", "p")
