import json
from types import SimpleNamespace

import httpx
import pytest

from ouroloop.train import policy
from ouroloop.agent import Agent, AgentConfig, Tools
from ouroloop.agent.loop import DROPPED
from ouroloop.train.backends import KeywordBackend
from ouroloop.chat import ChatTurn, ScriptedChat, ToolCall, ToolSpec
from ouroloop.points import BUILTIN, builtin_points, compact_point, denied, retry_point, route_point, stop_point
from ouroloop.train.claude_code import tool_gate_point
from ouroloop.train.policy import Policy
from ouroloop.providers import ProviderError
from ouroloop.providers.anthropic import AnthropicProvider, to_anthropic
from ouroloop.providers.openai_compat import OpenAICompatProvider
from ouroloop.runtime import Runtime
from ouroloop.train.types import Answer, Question

FLAKY = "test -f flag || { touch flag; echo 'ConnectionResetError: connection reset by peer' >&2; exit 1; }"


@pytest.fixture(autouse=True)
def every_point_may_act(monkeypatch):
    """These tests wire the agent loop for every point acting, beyond v0.2, where only stop may."""
    monkeypatch.setattr(policy, "ACTING", set(BUILTIN))


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
    res = Agent(chat, rt, tools, approve_fn=lambda c: asked.append(c.name) or True).run("write a.txt")
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
    Agent(chat, rt, tools, approve_fn=lambda c: asked.append(c.name) or False).run("t")
    assert not (tools.workdir / "a.txt").exists() and asked == ["write"]      # the deny list never asks
    gate, rule = by_point(rt)["tool_gate"]
    assert gate.outcomes[0]["label"] == "no"
    assert (rule.decision["route"], rule.decision["action"], rule.outcomes) == ("rule", "no", [])


def test_nobody_to_ask_declines_and_a_cleared_call_records_no_human_label(tmp_path, gated):
    rt, tools = setup(tmp_path)
    chat = ScriptedChat(turns([("write", {"path": "a.txt", "content": "x"})], "ok"))
    Agent(chat, rt, tools).run("t")                   # approve="ask", and nobody to ask
    (gate,) = by_point(rt)["tool_gate"]
    assert not (tools.workdir / "a.txt").exists() and gate.outcomes == []
    points = [tool_gate_point(policy=Policy(act=True, explore=0.0))]
    rt, tools = setup(tmp_path / "b", {"yes": Fixed(0.9)}, points=points)
    gated(rt, "tool_gate", "fixed-1", 0.6)
    chat = ScriptedChat(turns([("write", {"path": "a.txt", "content": "x"})], "ok"))
    Agent(chat, rt, tools).run("t")
    (gate,) = by_point(rt)["tool_gate"]
    assert (tools.workdir / "a.txt").exists() and gate.decision["route"] == "act" and gate.outcomes == []


def test_confident_retry_reruns_automatically(tmp_path, gated):
    points = [retry_point(policy=Policy(act=True, explore=0.0)), tool_gate_point()]
    rt, tools = setup(tmp_path, {"mock": KeywordBackend(), "yes": Fixed(0.9)}, {"retry": "yes"}, points)
    gated(rt, "retry", "fixed-1", 0.6)
    seen = []

    def script(messages):
        seen.append(messages[-1])
        return turns([("bash", {"command": FLAKY})], "done")[len(seen) - 1]

    Agent(ScriptedChat(script), rt, tools, config=AgentConfig(approve="auto")).run("t")
    assert "[ouroloop reran the command]" in seen[1]["results"][0]["output"] and not seen[1]["results"][0]["is_error"]
    (retry,) = by_point(rt)["retry"]
    assert (retry.decision["route"], retry.outcomes[0]["label"]) == ("act", "yes")


def test_compaction_drops_unneeded_outputs_and_learns_from_rereads(tmp_path, gated):
    points = [compact_point(policy=Policy(act=True, explore=0.0))]
    rt, tools = setup(tmp_path, {"mock": KeywordBackend(), "no": Fixed(0.1)}, {"compact": "no"}, points)
    gated(rt, "compact", "fixed-1", 0.6)
    for name in ("big1", "big2", "big3"):
        (tools.workdir / f"{name}.txt").write_text(name * 40)
    seen = []

    def script(messages):
        seen.append([m for m in messages])
        return turns([("read", {"path": "big1.txt"})], [("read", {"path": "big2.txt"})],
                     [("read", {"path": "big3.txt"})], [("read", {"path": "big1.txt"})], "done")[len(seen) - 1]

    Agent(ScriptedChat(script), rt, tools, config=AgentConfig(approve="auto", context_chars=50, keep_recent=1)).run("t")
    labels = sorted(r.outcomes[0]["label"] for r in by_point(rt)["compact"])
    assert labels == ["no", "no", "yes"]          # big1 was read again; big2 and big3 were not
    assert seen[-1][2]["results"][0]["output"] == DROPPED


def test_append_only_providers_are_never_edited(tmp_path, gated):
    points = [compact_point(policy=Policy(act=True, explore=0.0))]
    rt, tools = setup(tmp_path, {"mock": KeywordBackend(), "no": Fixed(0.1)}, {"compact": "no"}, points)
    gated(rt, "compact", "fixed-1", 0.6)
    (tools.workdir / "f.txt").write_text("x" * 200)
    chat = ScriptedChat(turns([("read", {"path": "f.txt"})], [("read", {"path": "f.txt"})], "done"))
    chat.supports_history_edits = False
    events = []
    Agent(chat, rt, tools, config=AgentConfig(approve="auto", context_chars=50, keep_recent=1),
          on_event=lambda k, d: events.append(k)).run("t")
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
                on_event=lambda k, d: events.append(k)).run("create done.txt")
    assert (res.text, res.turns, res.verified) == ("now it is done", 3, True) and events.count("pushback") == 1
    stops = by_point(rt)["stop"]
    assert [(r.decision["phase"], [o["label"] for o in r.outcomes]) for r in stops] == [
        ("on_finish", ["no"]), ("per_turn", []), ("on_finish", ["yes"])]    # the checker labels on_finish only
    assert stops[0].outcomes[0]["source"] == "env"
    # The checker's verdict is a rule's veto; after a turn the shadow point takes its default, and no check runs
    assert [(r.decision["route"], r.decision["action"]) for r in stops] == [("rule", "no"), ("shadow", "no"),
                                                                            ("rule", "yes")]


def test_confident_stop_pushes_back_and_the_person_confirms(tmp_path, gated):
    points = [stop_point(policy=Policy(act=True, explore=0.0))]
    rt, tools = setup(tmp_path, {"mock": KeywordBackend(), "no": Fixed(0.1)}, {"stop": "no"}, points)
    gated(rt, "stop", "fixed-1", 0.6)
    chat = ScriptedChat(turns("done", "really done", "done, as explained"))
    res = Agent(chat, rt, tools, config=AgentConfig(approve="auto", max_pushbacks=2),
                confirm_fn=lambda task, text: True).run("t")
    assert res.turns == 3 and res.text == "done, as explained"
    stops = by_point(rt)["stop"]
    assert len(stops) == 3 and all(r.outcomes[0]["label"] == "yes" and r.outcomes[0]["source"] == "human"
                                   for r in stops)                 # the model was wrong three times


def test_a_passing_verification_is_never_overruled_and_is_rechecked_at_the_end(tmp_path, gated):
    points = [stop_point(policy=Policy(act=True, explore=0.0))]
    rt, tools = setup(tmp_path, {"mock": KeywordBackend(), "no": Fixed(0.1)}, {"stop": "no"}, points)
    gated(rt, "stop", "fixed-1", 0.6)
    chat = ScriptedChat(turns([("write", {"path": "done.txt", "content": "ok"})], "done"))
    res = Agent(chat, rt, tools, config=AgentConfig(approve="auto", verify_command="test -f done.txt")).run("t")
    assert (res.turns, res.verified) == (2, True)                  # the model said "no", the check said done
    per_turn, stop = by_point(rt)["stop"]
    assert per_turn.decision["action"] == "no" and per_turn.outcomes == []     # "no" after a turn: keep going
    assert (stop.decision["route"], stop.decision["action"], stop.outcomes[0]["label"]) == ("rule", "yes", "yes")
    assert stop.decision["probs"][stop.decision["candidates"].index("no")] == 0.9     # recorded, overruled
    rt2, tools2 = setup(tmp_path / "b", points=[stop_point()])
    chat = ScriptedChat(turns([("write", {"path": "done.txt", "content": "ok"})]))
    res = Agent(chat, rt2, tools2,
                config=AgentConfig(approve="auto", max_turns=1, verify_command="test -f done.txt")).run("t")
    assert (res.stop, res.verified) == ("max_turns", True)


# the stop point's phases, and a verification that overrules a confident yes (from test_probe, P1 step 3)
def test_phases_and_a_confident_yes_after_a_turn(tmp_path, gated):
    rt = Runtime(tmp_path / "ws", {"yes": Fixed(0.9)}, "yes", seed=0)
    rt.register(stop_point(policy=Policy(act=True, explore=0.0)))
    gated(rt, "stop", "fixed-1", 0.6)
    (tmp_path / "work").mkdir()
    chat = ScriptedChat([ChatTurn("", [ToolCall("b", "bash", {"command": "true"})], "tool_use")])
    res = Agent(chat, rt, Tools(tmp_path / "work"), config=AgentConfig(approve="auto")).run("t")
    assert (res.stop, res.turns, chat.calls) == ("stop_point", 1, 1)

    ctx = {"task": "t", "turns": 1, "files": "", "actions": "", "message": ""}
    for phase in ("later", None):                   # only a declared phase; stop declares two
        with pytest.raises(ValueError):
            rt.record("stop", ctx, phase=phase)
    gated(rt, "stop", "fixed-1", 0.95)              # not confident enough: the phase's default is the action
    assert [rt.ask("stop", ctx, phase=ph).action for ph in ("per_turn", "on_finish")] == ["no", "yes"]
    rt.ask_question("research.q", Question("noul", "Worth trying?"), "state")
    rows = [r.decision for r in rt.ledger.rows()][-3:]
    assert [(d["phase"], d["default"]) for d in rows] == [("per_turn", "no"), ("on_finish", "yes"), (None, None)]


def test_a_confident_yes_after_a_turn_needs_the_checker_and_the_last_turn_decides_nothing(tmp_path, gated):
    rt = Runtime(tmp_path / "ws", {"yes": Fixed(0.9)}, "yes", seed=0)
    rt.register(stop_point(policy=Policy(act=True, explore=0.0)))
    gated(rt, "stop", "fixed-1", 0.6)
    for verify, expected in (("test -f f2", ("stop_point", 2)), ("test -f f9", ("max_turns", 3))):
        work = tmp_path / verify[-2:]
        work.mkdir()
        chat = ScriptedChat([ChatTurn("", [ToolCall("w", "write", {"path": f"f{i}", "content": "x"})], "tool_use")
                             for i in (1, 2, 3)])
        res = Agent(chat, rt, Tools(work),
                    config=AgentConfig(approve="auto", max_turns=3, verify_command=verify)).run("t")
        assert (res.stop, res.turns) == expected
    # "yes" after every turn: a failing verification overrules it (a veto, and a label), and after the last turn,
    # which nothing follows, there is no decision at all
    stops = [r for r in rt.ledger.rows() if r.decision["point"] == "stop"]
    assert [(r.decision["route"], r.decision["action"], r.outcomes[0]["label"]) for r in stops] == [
        ("rule", "no", "no"), ("act", "yes", "yes"), ("rule", "no", "no"), ("rule", "no", "no")]


def test_route_picks_the_small_tier_once_it_acts_and_learns_from_the_task_outcome(tmp_path, gated):
    rt, tools = setup(tmp_path, {"mock": KeywordBackend(), "cheap": FixedChoice("small")}, {"route": "cheap"},
                      [route_point(policy=Policy(act=True, explore=0.0)), stop_point()])
    gated(rt, "route", "fc", 0.6)
    small = ScriptedChat(turns([("bash", {"command": "echo hi"})], "done"))
    large = ScriptedChat(turns("unused"))
    res = Agent(large, rt, tools, tiers={"large": large, "small": small},
                config=AgentConfig(approve="auto", verify_command="true")).run("t")
    assert res.verified and small.calls == 2 and large.calls == 0
    routes = by_point(rt)["route"]
    assert [(r.outcomes[0]["label"], r.outcomes[0]["weight"]) for r in routes] == [("small", 0.5), ("small", 0.5)]
    # Shadow, as built in: every step takes the default tier, whatever the model says
    rt2, tools2 = setup(tmp_path / "b", {"cheap": FixedChoice("small")}, points=[route_point()])
    big = ScriptedChat(turns("done"))
    Agent(ScriptedChat(turns("never")), rt2, tools2, tiers={"large": big, "small": ScriptedChat(turns("x"))},
          config=AgentConfig(approve="auto")).run("t")
    assert big.calls == 1
