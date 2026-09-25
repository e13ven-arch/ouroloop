"""M6 offline acceptance: every stop decision gets a probe label, and nothing in the run sees it."""
import shlex
import sys

import pytest

from ouroloop.agent import Agent, AgentConfig, Tools
from ouroloop.agent.suite import Task, run_task
from ouroloop.chat import ChatTurn, ScriptedChat, ToolCall
from ouroloop.points import stop_point
from ouroloop.policy import Policy
from ouroloop.runtime import Runtime
from ouroloop.types import Answer, Question

PY = shlex.quote(sys.executable)
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b  # fixed\n"     # another size: a .pyc from the same second is stale
HIDDEN = "import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n" \
         "        self.assertEqual(calc.add(2, 3), 5)\n"
# The bug fails the hidden test only, so an unfixed state scores 0.5.
TASK = Task("calc", "Fix add() in calc.py.", [f"{PY} -m unittest -q test_hidden",
                                             f"{PY} -c 'import calc; assert calc.add(0, 0) == 0'"],
            files={"calc.py": BUGGY}, hidden={"test_hidden.py": HIDDEN}, held_out=True, max_turns=8,
            verify=f"{PY} -c 'import calc; assert calc.add(2, 3) == 5'")


class Fixed:
    name = "fixed"

    def __init__(self, p_yes):
        self.p_yes = p_yes

    def decide(self, requests):
        if self.p_yes is None:
            raise AssertionError("no model may be called")
        return [Answer({"yes": self.p_yes, "no": 1 - self.p_yes}, "fixed-1") for _ in requests]


def test_every_stop_decision_gets_a_probe_label(tmp_path):
    rt = Runtime(tmp_path / "ws", {"none": Fixed(None)}, "none", seed=0)
    rt.register(stop_point())                    # shadow: StopGuard records, never asks a model
    script = [ChatTurn("done", [], "end"),       # the verification fails: pushed back
              ChatTurn("", [ToolCall("w", "write", {"path": "calc.py", "content": FIXED})], "tool_use"),
              ChatTurn("", [ToolCall("r", "read", {"path": "calc.py"})], "tool_use"),     # changes nothing
              ChatTurn("done", [], "end")]
    hidden_seen, agents = [], []

    def make(workdir, task):
        def chat(_messages):
            hidden_seen.append((workdir / "test_hidden.py").exists())
            return script[len(hidden_seen) - 1]

        agents.append(Agent(ScriptedChat(chat), rt, Tools(workdir), session_meta={"agent": "scripted"}, seed=0,
                            config=AgentConfig(approve="auto", max_turns=task.max_turns, verify_command=task.verify)))
        return agents[-1]

    run = run_task(TASK, make)
    assert (run.score, run.turns, run.stop) == (1.0, 4, "end") and hidden_seen == [False] * 4

    stops = [r for r in rt.ledger.rows() if r.decision["point"] == "stop"]
    assert [(r.decision["phase"], r.decision["default"]) for r in stops] == [
        ("on_finish", "yes"), ("per_turn", "no"), ("per_turn", "no"), ("on_finish", "yes")]
    assert [(r.outcomes[0]["label"], r.outcomes[0]["score"]) for r in stops] == [
        ("no", 0.5), ("yes", 1.0), ("yes", 1.0), ("yes", 1.0)]
    # The probe is the only label: neither the verification command nor the end-of-task checks label stop.
    assert all([(o["source"], o["note"]) for o in r.outcomes] == [("env", "probe")] for r in stops)
    assert all(r.decision["route"] == "shadow" and r.decision["probs"] is None for r in stops)

    views = [r.decision["view"] for r in stops]
    assert not any("verification" in v or "passed" in v or "failed" in v for v in views)
    assert ["moment: after a turn" in v for v in views] == [False, True, True, False]
    assert all("moment: the agent wants to finish" in v for v in (views[0], views[3]))

    (session,) = [e for e in rt.ledger.events() if e["type"] == "session"]
    assert session["session"] == run.session
    assert {k: session[k] for k in ("task", "held_out", "agent")} == {"task": "calc", "held_out": True,
                                                                     "agent": "scripted"}

    probe = agents[0].probe          # the second per_turn and the last on_finish saw the state of the first
    assert [(s["changes"], len(s["decisions"])) for s in probe.snaps] == [(0, 1), (1, 3)]
    assert not any(s["dir"].exists() for s in probe.snaps)


def test_phases_and_a_confident_yes_after_a_turn(tmp_path):
    rt = Runtime(tmp_path / "ws", {"yes": Fixed(0.9)}, "yes", seed=0)
    rt.register(stop_point(policy=Policy(mode="auto", threshold=0.6, explore=0.0, fallback="default", default="yes")))
    (tmp_path / "work").mkdir()
    chat = ScriptedChat([ChatTurn("", [ToolCall("b", "bash", {"command": "true"})], "tool_use")])
    res = Agent(chat, rt, Tools(tmp_path / "work"), config=AgentConfig(approve="auto"), seed=0).run("t")
    assert (res.stop, res.turns, chat.calls) == ("stop_point", 1, 1)

    ctx = {"task": "t", "turns": 1, "files": "", "actions": "", "message": ""}
    with pytest.raises(ValueError):
        rt.record("stop", ctx, phase="later")
    rt.points["stop"].policy.threshold = 0.95       # not confident enough: the phase's default is the action
    assert [rt.ask("stop", ctx, phase=ph).action for ph in ("per_turn", "on_finish", None)] == ["no", "yes", "yes"]
    rt.ask_question("research.q", Question("noul", "Worth trying?"), "state")
    rows = [r.decision for r in rt.ledger.rows()][-4:]
    assert [(d["phase"], d["default"]) for d in rows] == [("per_turn", "no"), ("on_finish", "yes"), (None, "yes"),
                                                          (None, None)]


def test_a_confident_yes_after_a_turn_needs_the_checker_and_the_last_turn_decides_nothing(tmp_path):
    rt = Runtime(tmp_path / "ws", {"yes": Fixed(0.9)}, "yes", seed=0)
    rt.register(stop_point(policy=Policy(mode="auto", threshold=0.6, explore=0.0, fallback="default", default="yes")))
    for verify, expected in (("test -f f2", ("stop_point", 2)), ("test -f f9", ("max_turns", 3))):
        work = tmp_path / verify[-2:]
        work.mkdir()
        chat = ScriptedChat([ChatTurn("", [ToolCall("w", "write", {"path": f"f{i}", "content": "x"})], "tool_use")
                             for i in (1, 2, 3)])
        res = Agent(chat, rt, Tools(work), seed=0,
                    config=AgentConfig(approve="auto", max_turns=3, verify_command=verify)).run("t")
        assert (res.stop, res.turns) == expected
    # "yes" after every turn: a failing verification overrules it (and labels it), and after the last turn, which
    # nothing follows, there is no decision at all
    stops = [r for r in rt.ledger.rows() if r.decision["point"] == "stop"]
    assert [(r.decision["action"], r.outcomes[0]["label"]) for r in stops] == [("yes", "no"), ("yes", "yes"),
                                                                               ("yes", "no"), ("yes", "no")]


@pytest.mark.parametrize("command", ["mkfifo pipe", "ln -s /dev/null test_hidden.py"])
def test_a_state_the_probe_cannot_copy_or_check_stays_unlabelled(tmp_path, command):
    rt = Runtime(tmp_path / "ws", {"none": Fixed(None)}, "none", seed=0)
    rt.register(stop_point())
    script = [ChatTurn("done", [], "end"), ChatTurn("", [ToolCall("b", "bash", {"command": command})], "tool_use"),
              ChatTurn("done", [], "end"), ChatTurn("done", [], "end")]     # pushed back twice, then let go
    agents = []

    def make(workdir, task):
        agents.append(Agent(ScriptedChat(script), rt, Tools(workdir), seed=0,
                            config=AgentConfig(approve="auto", max_turns=task.max_turns, verify_command=task.verify)))
        return agents[-1]

    run = run_task(TASK, make)      # a pipe cannot be copied; a symlink to /dev/null cannot take the hidden test
    assert (run.score, run.error[:10]) == ((0.5, "") if "pipe" in command else (None, "ValueError"))
    stops = [r for r in rt.ledger.rows() if r.decision["point"] == "stop"]
    assert [[o["label"] for o in r.outcomes] for r in stops] == [["no"], [], [], []]
    probe = agents[0].probe
    assert probe.root is None and not any(s["dir"] and s["dir"].exists() for s in probe.snaps)
