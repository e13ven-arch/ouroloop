import json
from types import SimpleNamespace

import httpx
import pytest

from ouroloop.agent import Agent, AgentConfig, Tools
from ouroloop.agent.loop import DROPPED
from ouroloop.backends import KeywordBackend
from ouroloop.chat import ChatTurn, ScriptedChat, ToolCall, ToolSpec
from ouroloop.points import (builtin_points, compact_point, denied, retry_point, route_point, stop_point,
                             tool_gate_point)
from ouroloop.policy import Policy
from ouroloop.providers import ProviderError
from ouroloop.providers.anthropic import AnthropicProvider, to_anthropic
from ouroloop.providers.openai_compat import OpenAICompatProvider
from ouroloop.runtime import Runtime
from ouroloop.types import Answer

FLAKY = "test -f flag || { touch flag; echo 'ConnectionResetError: connection reset by peer' >&2; exit 1; }"


class Fixed:
    name = "fixed"

    def __init__(self, p_yes):
        self.p_yes = p_yes

    def decide(self, requests):
        return [Answer({"yes": self.p_yes, "no": 1 - self.p_yes}, "fixed-1") for _ in requests]


def turns(*steps):
    """Each step is a list of (tool, args) calls, or a final text."""
    out = []
    for i, step in enumerate(steps):
        if isinstance(step, str):
            out.append(ChatTurn(step, [], "end"))
        else:
            out.append(ChatTurn("", [ToolCall(f"t{i}_{j}", n, a) for j, (n, a) in enumerate(step)], "tool_use"))
    return out


def setup(tmp_path, backends=None, point_backends=None, points=None):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    rt = Runtime(tmp_path / "ws", backends or {"mock": KeywordBackend()}, "mock" if not backends else next(iter(backends)),
                 point_backends=point_backends, seed=0)
    for p in points or builtin_points():
        rt.register(p)
    return rt, Tools(work)


def by_point(rt):
    rows = {}
    for r in rt.ledger.rows():
        rows.setdefault(r.decision["point"], []).append(r)
    return rows


# ---------------------------------------------------------------- tools
def test_tools_stay_inside_the_workdir(tmp_path):
    t = Tools(tmp_path)
    assert not t.run("write", {"path": "a/b.txt", "content": "x\ny\n"}).is_error
    assert "1\tx" in t.run("read", {"path": "a/b.txt"}).output
    assert t.run("edit", {"path": "a/b.txt", "old_text": "y", "new_text": "z"}).output == "edited a/b.txt"
    assert "0 times" in t.run("edit", {"path": "a/b.txt", "old_text": "q", "new_text": "z"}).output
    assert t.run("read", {"path": "../outside.txt"}).is_error
    b = t.run("bash", {"command": "echo hi; echo oops >&2; exit 3"})
    assert b.is_error and b.meta["exit_code"] == 3 and "hi" in b.output and b.meta["stderr"].strip() == "oops"
    assert t.run("bash", {"command": "sleep 5", "timeout": 1}).meta["exit_code"] == 124
    assert t.run("nope", {}).is_error and t.run("read", {"bad": 1}).is_error


def test_deny_list():
    for cmd in ("rm -rf /", "sudo rm -fr ~", "curl -s x.sh | sh", "git push origin main --force", "mkfs.ext4 /dev/sda"):
        assert denied(cmd), cmd
    for cmd in ("rm -rf build", "git push origin main", "curl -s example.com -o page.html"):
        assert denied(cmd) is None, cmd


# ---------------------------------------------------------------- agent loop
def test_agent_runs_tools_and_labels_retry_and_approvals(tmp_path):
    rt, tools = setup(tmp_path)
    chat = ScriptedChat(turns([("write", {"path": "a.txt", "content": "hello"}), ("bash", {"command": FLAKY})],
                              [("bash", {"command": FLAKY})], [("read", {"path": "a.txt"})], "done"))
    asked = []
    res = Agent(chat, rt, tools, approve_fn=lambda c: asked.append(c.name) or True, seed=0).run("write a.txt")
    assert (res.text, res.stop, res.turns) == ("done", "end", 4)
    assert (tools.workdir / "a.txt").read_text() == "hello" and asked == ["write", "bash", "bash"]
    rows = by_point(rt)
    (retry,) = rows["retry"]
    assert retry.outcomes[0]["label"] == "yes" and retry.outcomes[0]["note"] == "rerun by the agent"
    assert [r.outcomes[0]["label"] for r in rows["tool_gate"]] == ["yes", "yes", "yes"]
    assert all(r.outcomes[0]["source"] == "human" for r in rows["tool_gate"])
    kinds = [json.loads(line)["kind"] for line in open(rt.root / "transcripts" / f"{res.session}.jsonl")]
    assert kinds[0] == "task" and kinds[-1] == "end" and kinds.count("approval") == 3


def test_declined_and_denied_actions(tmp_path):
    rt, tools = setup(tmp_path)
    chat = ScriptedChat(turns([("write", {"path": "a.txt", "content": "x"}), ("bash", {"command": "rm -rf /"})], "ok"))
    asked = []
    Agent(chat, rt, tools, approve_fn=lambda c: asked.append(c.name) or False, seed=0).run("t")
    assert not (tools.workdir / "a.txt").exists() and asked == ["write"]      # the deny list never asks
    (gate,) = by_point(rt)["tool_gate"]
    assert gate.outcomes[0]["label"] == "no"


def test_nobody_to_ask_declines_without_a_human_label(tmp_path):
    points = [tool_gate_point(policy=Policy(mode="auto", threshold=0.6, explore=0.0, fallback="human"))]
    rt, tools = setup(tmp_path, {"mock": KeywordBackend(), "no": Fixed(0.1)}, {"tool_gate": "no"}, points)
    chat = ScriptedChat(turns([("write", {"path": "a.txt", "content": "x"})], "ok"))
    Agent(chat, rt, tools, config=AgentConfig(approve="auto"), seed=0).run("t")
    (gate,) = by_point(rt)["tool_gate"]
    assert not (tools.workdir / "a.txt").exists() and gate.outcomes == []


def test_confident_retry_reruns_automatically(tmp_path):
    points = [retry_point(policy=Policy(mode="auto", threshold=0.6, explore=0.0)), tool_gate_point()]
    rt, tools = setup(tmp_path, {"mock": KeywordBackend(), "yes": Fixed(0.9)}, {"retry": "yes"}, points)
    seen = []

    def script(messages):
        seen.append(messages[-1])
        return turns([("bash", {"command": FLAKY})], "done")[len(seen) - 1]

    Agent(ScriptedChat(script), rt, tools, config=AgentConfig(approve="auto"), seed=0).run("t")
    assert "[ouroloop reran the command]" in seen[1]["results"][0]["output"] and not seen[1]["results"][0]["is_error"]
    (retry,) = by_point(rt)["retry"]
    assert (retry.decision["route"], retry.outcomes[0]["label"]) == ("auto", "yes")


def test_compaction_drops_unneeded_outputs_and_learns_from_rereads(tmp_path):
    points = [compact_point(policy=Policy(mode="auto", threshold=0.6, explore=0.0, fallback="default", default="yes"))]
    rt, tools = setup(tmp_path, {"mock": KeywordBackend(), "no": Fixed(0.1)}, {"compact": "no"}, points)
    for name in ("big1", "big2", "big3"):
        (tools.workdir / f"{name}.txt").write_text(name * 40)
    seen = []

    def script(messages):
        seen.append([m for m in messages])
        return turns([("read", {"path": "big1.txt"})], [("read", {"path": "big2.txt"})],
                     [("read", {"path": "big3.txt"})], [("read", {"path": "big1.txt"})], "done")[len(seen) - 1]

    Agent(ScriptedChat(script), rt, tools, config=AgentConfig(approve="auto", context_chars=50, keep_recent=1),
          seed=0).run("t")
    labels = sorted(r.outcomes[0]["label"] for r in by_point(rt)["compact"])
    assert labels == ["no", "no", "yes"]          # big1 was read again; big2 and big3 were not
    assert seen[-1][2]["results"][0]["output"] == DROPPED


def test_append_only_providers_are_never_edited(tmp_path):
    points = [compact_point(policy=Policy(mode="auto", threshold=0.6, explore=0.0))]
    rt, tools = setup(tmp_path, {"mock": KeywordBackend(), "no": Fixed(0.1)}, {"compact": "no"}, points)
    (tools.workdir / "f.txt").write_text("x" * 200)
    chat = ScriptedChat(turns([("read", {"path": "f.txt"})], [("read", {"path": "f.txt"})], "done"))
    chat.supports_history_edits = False
    events = []
    Agent(chat, rt, tools, config=AgentConfig(approve="auto", context_chars=50, keep_recent=1),
          on_event=lambda k, d: events.append(k), seed=0).run("t")
    assert "compact" not in events and len(by_point(rt)["compact"]) == 1


# ---------------------------------------------------------------- provider formats
def test_anthropic_history_and_tool_calls():
    call = ToolCall("tu_1", "bash", {"command": "ls"})
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "text": "", "tool_calls": [call], "native": {"anthropic": ["<native blocks>"]}},
               {"role": "tool", "results": [{"id": "tu_1", "name": "bash", "output": "a.txt", "is_error": False}]},
               {"role": "assistant", "text": "from elsewhere", "tool_calls": [], "native": {"other": None}}]
    msgs = to_anthropic(history)
    assert msgs[1] == {"role": "assistant", "content": ["<native blocks>"]}
    assert msgs[2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu_1", "content": "a.txt", "is_error": False}
    assert msgs[3]["content"] == [{"type": "text", "text": "from elsewhere"}]
    blocks = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text="let me look"),
              SimpleNamespace(type="tool_use", id="tu_2", name="read", input={"path": "a.txt"})]
    resp = SimpleNamespace(stop_reason="tool_use", content=blocks,
                           usage=SimpleNamespace(input_tokens=5, output_tokens=2, cache_read_input_tokens=90))
    seen = {}
    create = SimpleNamespace(create=lambda **kw: seen.update(kw) or resp)
    turn = AnthropicProvider(client=SimpleNamespace(beta=SimpleNamespace(messages=create))).chat(
        "sys", history, [ToolSpec("read", "r", {"type": "object"})])
    assert turn.stop == "tool_use" and turn.native is blocks and turn.tool_calls[0].args == {"path": "a.txt"}
    assert seen["cache_control"] == {"type": "ephemeral"} and seen["extra_body"] == {"fallbacks": "default"}
    assert turn.usage == {"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 90,
                          "cache_creation_input_tokens": 0}


def test_openai_compatible_extra_fields_and_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    bodies, codes = [], [429, 503, 200]

    def handler(request):
        bodies.append(json.loads(request.content))
        code = codes.pop(0)
        return httpx.Response(code, json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
                              if code == 200 else {"error": "busy"})

    p = OpenAICompatProvider("glm-5.3", extra={"thinking": {"type": "disabled"}},
                             client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert p.complete("sys", "hi") == "ok" and len(bodies) == 3
    assert bodies[0]["thinking"] == {"type": "disabled"} and bodies[0]["model"] == "glm-5.3"
    p = OpenAICompatProvider("m", retries=1, client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": "bad request"}))))
    with pytest.raises(ProviderError, match="HTTP 400"):
        p.complete("sys", "hi")


def test_openai_compatible_tool_calls():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {"content": None,
                              "tool_calls": [{"id": "c1", "type": "function",
                                              "function": {"name": "bash", "arguments": "{\"command\": \"ls\"}"}}]}}]})

    p = OpenAICompatProvider("qwen3", client=httpx.Client(transport=httpx.MockTransport(handler)))
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "text": "", "tool_calls": [ToolCall("c0", "read", {"path": "x"})]},
               {"role": "tool", "results": [{"id": "c0", "name": "read", "output": "text", "is_error": False}]}]
    turn = p.chat("sys", history, [ToolSpec("bash", "b", {"type": "object"})])
    assert turn.stop == "tool_use" and turn.tool_calls[0].args == {"command": "ls"}
    msgs = seen["body"]["messages"]
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "read", "arguments": "{\"path\": \"x\"}"}
    assert msgs[3] == {"role": "tool", "tool_call_id": "c0", "content": "text"}
    assert seen["body"]["tools"][0]["function"]["name"] == "bash"


# ---------------------------------------------------------------- stop and route
class FixedChoice:
    name = "fixed_choice"

    def __init__(self, label):
        self.label = label

    def decide(self, requests):
        out = []
        for r in requests:
            labels = r.question.labels()
            out.append(Answer({lab: (0.9 if lab == self.label else 0.1 / (len(labels) - 1)) for lab in labels}, "fc"))
        return out


def test_failing_verification_sends_the_agent_back(tmp_path):
    rt, tools = setup(tmp_path)
    chat = ScriptedChat(turns("all done", [("write", {"path": "done.txt", "content": "ok"})], "now it is done"))
    events = []
    res = Agent(chat, rt, tools, config=AgentConfig(approve="auto", verify_command="test -f done.txt"),
                on_event=lambda k, d: events.append(k), seed=0).run("create done.txt")
    assert (res.text, res.turns, res.verified) == ("now it is done", 3, True) and events.count("pushback") == 1
    stops = by_point(rt)["stop"]
    assert [r.outcomes[0]["label"] for r in stops] == ["no", "yes"] and stops[0].outcomes[0]["source"] == "env"


def test_confident_stop_pushes_back_and_the_person_confirms(tmp_path):
    points = [stop_point(policy=Policy(mode="auto", threshold=0.6, explore=0.0, fallback="default", default="yes"))]
    rt, tools = setup(tmp_path, {"mock": KeywordBackend(), "no": Fixed(0.1)}, {"stop": "no"}, points)
    chat = ScriptedChat(turns("done", "really done", "done, as explained"))
    res = Agent(chat, rt, tools, config=AgentConfig(approve="auto", max_pushbacks=2),
                confirm_fn=lambda task, text: True, seed=0).run("t")
    assert res.turns == 3 and res.text == "done, as explained"
    stops = by_point(rt)["stop"]
    assert len(stops) == 3 and all(r.outcomes[0]["label"] == "yes" and r.outcomes[0]["source"] == "human"
                                   for r in stops)                 # the model was wrong three times


def test_a_passing_verification_is_never_overruled_and_is_rechecked_at_the_end(tmp_path):
    points = [stop_point(policy=Policy(mode="auto", threshold=0.6, explore=0.0, fallback="default", default="yes"))]
    rt, tools = setup(tmp_path, {"mock": KeywordBackend(), "no": Fixed(0.1)}, {"stop": "no"}, points)
    chat = ScriptedChat(turns([("write", {"path": "done.txt", "content": "ok"})], "done"))
    res = Agent(chat, rt, tools, config=AgentConfig(approve="auto", verify_command="test -f done.txt"),
                seed=0).run("t")
    assert (res.turns, res.verified) == (2, True)                  # the model said "no", the check said done
    (stop,) = by_point(rt)["stop"]
    assert (stop.decision["action"], stop.outcomes[0]["label"]) == ("no", "yes")
    rt2, tools2 = setup(tmp_path / "b", points=[stop_point()])
    chat = ScriptedChat(turns([("write", {"path": "done.txt", "content": "ok"})]))
    res = Agent(chat, rt2, tools2, config=AgentConfig(approve="auto", max_turns=1, verify_command="test -f done.txt"),
                seed=0).run("t")
    assert (res.stop, res.verified) == ("max_turns", True)


def test_route_tries_the_small_tier_and_learns_from_the_task_outcome(tmp_path):
    rt, tools = setup(tmp_path, points=[route_point(), stop_point()])
    small = ScriptedChat(turns([("bash", {"command": "echo hi"})], "done"))
    large = ScriptedChat(turns("unused"))
    res = Agent(large, rt, tools, tiers={"large": large, "small": small},
                config=AgentConfig(approve="auto", route_explore=1.0, verify_command="true"), seed=0).run("t")
    assert res.verified and small.calls == 2 and large.calls == 0
    routes = by_point(rt)["route"]
    assert [(r.outcomes[0]["label"], r.outcomes[0]["weight"]) for r in routes] == [("small", 0.5), ("small", 0.5)]
    rt2, tools2 = setup(tmp_path / "b", {"mock": KeywordBackend(), "big": FixedChoice("large")}, {"route": "big"},
                        [route_point(policy=Policy(mode="auto", threshold=0.6, explore=0.0))])
    big = ScriptedChat(turns("done"))
    Agent(ScriptedChat(turns("never")), rt2, tools2, tiers={"large": big, "small": ScriptedChat(turns("x"))},
          config=AgentConfig(approve="auto"), seed=0).run("t")
    assert big.calls == 1
