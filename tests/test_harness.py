import json
import shlex
import sys
from pathlib import Path

import pytest

from ouroloop import config
from ouroloop.agent import Agent, AgentConfig, Tools
from ouroloop.agent.loop import SYSTEM
from ouroloop.agent.prompt import HarnessPrompt, PromptStore
from ouroloop.agent.suite import Task, load_suite, run_task, validate
from ouroloop.backends import KeywordBackend
from ouroloop.chat import ChatTurn, ScriptedChat, ToolCall
from ouroloop.evolve import experience
from ouroloop.evolve.harness import SuiteRunner, prompt_evaluator, suite_context
from ouroloop.points import builtin_points
from ouroloop.research.agent import Candidate
from ouroloop.research.basic import BasicResearcher
from ouroloop.research.loop import run_round
from ouroloop.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]
PY = shlex.quote(sys.executable)
FIX = "Always fix the bug."
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
HIDDEN = "import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n" \
         "        self.assertEqual(calc.add(2, 3), 5)\n"


def task(name, held_out=False):
    # The bug fails the first check only, so an unfixed run scores 0.5.
    return Task(name, f"Fix add() in calc.py ({name}).",
                [f"{PY} -c 'import calc; assert calc.add(2, 3) == 5'", f"{PY} -c 'import calc; assert calc.add(0, 0) == 0'"],
                files={"calc.py": BUGGY}, held_out=held_out, max_turns=5)


class Coder:
    """A coding model that fixes the bug only when the system prompt tells it to."""
    name = model = "coder"
    supports_history_edits = True

    def __init__(self):
        self.systems, self.tools = [], []

    def chat(self, system, messages, tools):
        self.systems.append(system)
        self.tools.append({t.name: t.description for t in tools})
        if FIX in system and not any(m["role"] == "tool" for m in messages):
            return ChatTurn("", [ToolCall("w", "write", {"path": "calc.py", "content": FIXED})], "tool_use")
        return ChatTurn("done", [], "end")


class ResearchLLM:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def complete(self, system, prompt):
        self.prompts.append(prompt)
        return self.reply


def setup(tmp_path):
    rt = Runtime(tmp_path / "ws", {"mock": KeywordBackend()}, "mock", seed=0)
    for p in builtin_points():
        rt.register(p)
    coder = Coder()

    def make(workdir, t, prompt):
        return Agent(coder, rt, Tools(workdir, descriptions=prompt.tools), system=prompt.system,
                     config=AgentConfig(approve="auto", max_turns=t.max_turns), seed=0)

    return rt, coder, SuiteRunner(rt.root, make, "coder")


def scripted(rt, *turns):
    return lambda workdir, _t: Agent(ScriptedChat(list(turns)), rt, Tools(workdir), config=AgentConfig(approve="auto"),
                                     seed=0)


# ---------------------------------------------------------------- tasks
def test_starter_tasks_fail_as_given_and_pass_with_their_solution():
    tasks = load_suite(ROOT / "evals" / "tasks")
    assert len(tasks) == 10 and sum(t.held_out for t in tasks) == 3
    for t in tasks:
        before, after = validate(t)
        assert before < 1 and after == 1, t.name


def test_hidden_checks_cannot_be_edited_and_label_the_stop_decision(tmp_path):
    rt, _, _ = setup(tmp_path)
    t = Task("t", "Make the test pass.", [f"{PY} -m unittest -q test_hidden"], files={"calc.py": BUGGY},
             hidden={"test_hidden.py": HIDDEN})
    cheat = run_task(t, scripted(rt, ChatTurn("", [ToolCall("w", "write", {"path": "test_hidden.py",
                                                                           "content": "print('ok')\n"})], "tool_use"),
                                 ChatTurn("done", [], "end")))
    assert cheat.score == 0.0 and "test_add" in cheat.check_output
    fixed = run_task(t, scripted(rt, ChatTurn("", [ToolCall("w", "write", {"path": "calc.py", "content": FIXED})],
                                              "tool_use"), ChatTurn("done", [], "end")))
    assert fixed.score == 1.0 and fixed.turns == 2
    stops = [r for r in rt.ledger.rows() if r.decision["point"] == "stop"]
    assert [(r.outcomes[0]["label"], r.outcomes[0]["source"]) for r in stops] == [("no", "env"), ("yes", "env")]


def test_harness_prompt_changes():
    p = HarnessPrompt()
    assert p.same_as(p.apply({"system": SYSTEM.replace(" ", "  ") + "\n"}))
    assert p.same_as(p.apply({"tools": {"bash": p.descriptions()["bash"] + "  "}}))
    assert not p.same_as(p.apply({"system": f"{SYSTEM} {FIX}"}))
    for bad in ({"model": "x"}, {"tools": {"grep": "search"}}, {"system": "  "}):
        with pytest.raises(ValueError):
            p.apply(bad)
    assert HarnessPrompt('In {workdir}, reply {"ok": true}').render("/w") == 'In /w, reply {"ok": true}'


def test_tool_descriptions_and_the_workdir_reach_the_model(tmp_path):
    _, coder, runner = setup(tmp_path)
    runner.runs(HarnessPrompt("Work in {workdir}.", {"bash": "Run a command."}), [task("a")])
    assert coder.systems[-1].startswith("Work in /") and "{workdir}" not in coder.systems[-1]
    assert coder.tools[-1]["bash"] == "Run a command." and coder.tools[-1]["read"].startswith("Read a text file")


# ---------------------------------------------------------------- prompt candidates
def test_research_round_promotes_a_better_prompt_and_rejects_a_no_op(tmp_path):
    tasks = [task("a"), task("b"), task("c"), task("d", held_out=True)]
    rt, _, runner = setup(tmp_path)
    llm = ResearchLLM(json.dumps({"candidates": [
        {"kind": "prompt", "change": {"system": SYSTEM + "  "}, "hypothesis": "whitespace only"},
        {"kind": "prompt", "change": {"system": f"{SYSTEM} {FIX}"}, "hypothesis": "tell the agent to fix bugs"}]}))
    ev = prompt_evaluator(tasks, runner, repeats=2, screen=2, min_items=4)
    res = run_round(rt, BasicResearcher(budget=2, consult=False), llm, rt.backends["mock"],
                    evaluators={"prompt": ev}, context=suite_context(tasks, runner))
    assert {e["hypothesis"]: e["gate"] for e in res.evaluated} == {"whitespace only": "rejected",
                                                                     "tell the agent to fix bugs": "accepted"}
    assert PromptStore(rt.root).get().system.endswith(FIX)
    assert "Fix add() in calc.py (a)" in llm.prompts[0] and "(d)" not in llm.prompts[0]   # held-out stays hidden
    # context: 3 champion runs; no-op: none; screening: 2 new; full run: 5 champion + 6 challenger runs not yet done
    assert runner.executed == 16
    (good,) = [e for e in experience.read(rt.root) if e["gate"] == "accepted"]
    assert good["kind"] == "prompt" and good["verdict"] == "improved" and good["delta"] == -0.5
    d = good["details"]
    assert d["diff"] == [f"+ {FIX}"] and d["gated_on"] == "dev"
    assert d["effects"] == {"a": 0.5, "b": 0.5, "c": 0.5}          # what the proposer may read: dev only
    assert d["held_out_effects"] == {"d": 0.5} and d["held_out_n"] == 2 and d["held_out_delta"] == -0.5
    assert BasicResearcher._past_prompt(good)[1:] == [f"    + {FIX}", "    effects: a +0.50, b +0.50, c +0.50"]

    before = runner.executed
    worse = ev(rt, Candidate("prompt", "harness", {"system": "Reply done."}, "shorter"))
    assert worse.rejected[0].startswith("screening") and runner.executed == before + 2
    # Every dev task is solved now: nothing to propose, and every champion run comes from the record.
    assert run_round(rt, BasicResearcher(consult=False), llm, rt.backends["mock"], evaluators={"prompt": ev},
                     context=suite_context(tasks, runner)).evaluated == []
    assert runner.executed == before + 2


def test_suite_config_builds_without_network(tmp_path):
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "t1.toml").write_text('prompt = "x"\nchecks = ["true"]\n')
    (tmp_path / "ouroloop.toml").write_text("""
[workspace]
root = "ws"

[llm]
type = "openai_compat"
model = "glm-4.6"
base_url = "https://open.bigmodel.cn/api/paas/v4"
api_key_env = "ZHIPUAI_API_KEY"

[llm_small]
type = "openai_compat"
model = "glm-4.5-air"

[suite]
path = "tasks"
repeats = 1
max_turns = 3
""")
    cfg = config.load(tmp_path / "ouroloop.toml")
    rt = config.build_runtime(cfg, tmp_path)
    agent, llm, judge, kw = config.build_research(cfg, rt, tmp_path)
    assert "prompt" in kw["evaluators"] and callable(kw["context"]) and {"stop", "route"} <= set(rt.points)
    make, identity = config.build_agent_factory(cfg, rt)
    a = make(tmp_path, load_suite(tmp_path / "tasks")[0], HarnessPrompt())
    assert identity == "glm-4.6+glm-4.5-air/max_turns=3" and set(a.tiers) == {"large", "small"}
    assert a.config.approve == "auto" and a.config.max_turns == 3



def test_the_gate_does_not_select_on_held_out_but_records_it(tmp_path):
    """A held-out split that can veto a candidate is part of the selection, not a check on it. The gate reads
    the development split; the held-out result is measured and recorded either way."""
    tasks = [task("a"), task("b"), task("c"), task("d", held_out=True)]
    rt, _, runner = setup(tmp_path)

    class Selective:
        """Fixes the bug on every task except the held-out one, which it breaks."""
        name = model = "selective"
        supports_history_edits = True

        def chat(self, system, messages, tools):
            if FIX in system and not any(m["role"] == "tool" for m in messages):
                broken = "def add(a, b):\n    raise ValueError\n"
                content = broken if "(d)" in messages[0]["content"] else FIXED
                return ChatTurn("", [ToolCall("w", "write", {"path": "calc.py", "content": content})], "tool_use")
            return ChatTurn("done", [], "end")

    runner.make_agent = lambda workdir, t, prompt: Agent(
        Selective(), rt, Tools(workdir, descriptions=prompt.tools), system=prompt.system,
        config=AgentConfig(approve="auto", max_turns=t.max_turns), seed=0)
    ev = prompt_evaluator(tasks, runner, repeats=1, screen=0, min_items=3)
    res = ev(rt, Candidate("prompt", "harness", {"system": f"{SYSTEM} {FIX}"}, "helps dev, hurts held-out"))
    assert res.groups == ["a", "b", "c"]                    # the gate's items are the development split
    assert res.details["held_out_effects"] == {"d": -0.5}   # and the damage is on the record
    assert res.details["held_out_delta"] == 0.5             # positive loss delta: held-out got worse
