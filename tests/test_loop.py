import json
import random

from ouroloop import config
from ouroloop.backends import LlmBackend
from ouroloop.decision import Noul
from ouroloop.demo import run, seed_ledger
from ouroloop.evolve import experience
from ouroloop.evolve.gate import decide
from ouroloop.policy import Policy
from ouroloop.providers import MockProvider
from ouroloop.research import Candidate, run_round
from ouroloop.runtime import Runtime
from ouroloop.types import Answer


# ---------------------------------------------------------------- gate
def test_gate_order_and_outcomes():
    groups = [f"s{i % 12}" for i in range(60)]
    assert decide([0.5] * 60, [0.1] * 60, groups).outcome == "accepted"
    assert decide([0.5] * 60, [0.5] * 60, groups).reasons == ["no change on any item"]
    assert decide([0.5] * 10, [0.1] * 10, groups[:10]).outcome == "insufficient_evidence"
    assert decide([0.5] * 60, [0.1] * 60, ["one"] * 60).outcome == "insufficient_evidence"
    assert decide([0.5] * 60, [0.1] * 60, groups, protected_ok=False).outcome == "rejected"
    worse = {"other": ([0.1] * 60, [0.5] * 60, groups)}
    assert decide([0.5] * 60, [0.1] * 60, groups, regressions=worse).reasons == ["significant regression on other"]


def test_gate_rarely_promotes_noise():
    rng = random.Random(0)
    groups = [f"s{i % 15}" for i in range(90)]
    accepted = 0
    for seed in range(20):
        base = [rng.random() for _ in range(90)]
        noisy = [b + rng.gauss(0, 0.1) for b in base]
        accepted += decide(base, noisy, groups, seed=seed).outcome == "accepted"
    assert accepted <= 2


# ---------------------------------------------------------------- runtime
class Fixed:
    name = "fixed"

    def __init__(self, p_yes):
        self.p_yes = p_yes

    def decide(self, requests):
        return [Answer({"yes": self.p_yes, "no": 1 - self.p_yes}, "fixed-1") for _ in requests]


def test_runtime_routes_logs_and_escalates(tmp_path):
    fallback = LlmBackend(MockProvider(lambda s, p: '{"label": "no"}', source="open_llm"))
    rt = Runtime(tmp_path, {"fixed": Fixed(0.9)}, "fixed", fallback=fallback, seed=0)
    auto = rt.register(Noul("a", "ok?", policy=Policy(mode="auto", threshold=0.8, explore=0.0)))
    d = rt.ask(auto, {"x": 1}, session="s1")
    assert (d.route, d.action, d.model) == ("auto", "yes", "fixed-1")
    strict = rt.register(Noul("b", "ok?", policy=Policy(mode="auto", threshold=0.95)))
    assert rt.ask(strict, {"x": 2}, session="s1").action == "no"          # the fallback LLM decided
    default = rt.register(Noul("c", "ok?", policy=Policy(mode="auto", threshold=0.95, fallback="default",
                                                         default="no")))
    assert rt.ask(default, {"x": 3}, session="s1").action == "no"
    human = rt.register(Noul("d", "ok?", policy=Policy(mode="auto", threshold=0.95, fallback="human")))
    assert rt.ask(human, {"x": 4}, session="s1").action is None
    rows = {r.decision["point"]: r for r in rt.ledger.rows()}
    assert rows["a"].decision["ctx"] == "s1#0" and rt.sessions.get("s1#0") == {"x": 1}
    assert rows["b"].escalations[0]["source"] == "open_llm" and rows["b"].target() == ({"no": 1.0}, 0.5)


def test_register_prefers_the_promoted_spec(tmp_path):
    rt = Runtime(tmp_path, {"fixed": Fixed(0.5)}, "fixed")
    rt.specs.set("a", {"instructions": "promoted", "criteria": None, "view_params": {}})
    assert rt.register(Noul("a", "original")).spec.instructions == "promoted"


# ---------------------------------------------------------------- one research round
def test_demo_round_promotes_the_criteria_rewrite_only(tmp_path):
    root, res = run(tmp_path, seed=0, train=False)
    assert [e["gate"] for e in res.evaluated] == ["accepted", "rejected"]
    assert "criteria" in res.promoted[0]["change"]
    specs = json.loads((tmp_path / "harness" / "specs.json").read_text())
    assert "ModuleNotFoundError" in specs["retry"]["spec"]["criteria"]["false"]
    assert len(experience.read(tmp_path)) == 2
    rows = Runtime(tmp_path, {}, "none").ledger.rows()
    worth = sorted(r.outcomes[0]["label"] for r in rows if r.decision["point"] == "research.worth_trying")
    assert worth == ["no", "yes"]
    (pick,) = [r for r in rows if r.decision["point"] == "research.pick_target"]
    assert pick.outcomes[0]["label"] == pick.decision["action"] and pick.outcomes[0]["reward"] == 1.0


class FixedAgent:
    """A custom research agent loaded from config: always proposes one spec change."""

    def __init__(self, change=None):
        self.change = change or {"instructions": "Rerunning will succeed"}

    def step(self, ws):
        return [Candidate("spec", "retry", self.change, "config-loaded agent")]


def test_example_config_builds_without_network():
    path = __import__("pathlib").Path(__file__).resolve().parents[1] / "examples" / "ouroloop.toml"
    cfg = config.load(path)
    rt = config.build_runtime(cfg, path.parent)
    agent, llm, judge, kw = config.build_research(cfg, rt)
    assert set(rt.points) == {"retry"} and judge.name == "jev" and llm.model == "claude-opus-5"
    assert rt.backend_for("retry").name == "jev"                    # "champion" falls back to [registry] base
    assert agent.budget == 2 and kw["min_items"] == 30 and "recipe" in kw["evaluators"]
    assert kw["context"]["recipe"]["replay"] == "data/general/train.jsonl"


def test_agent_and_backends_are_swapped_by_config_only(tmp_path):
    (tmp_path / "ouroloop.toml").write_text("""
[workspace]
root = "ws"

[backends.mock]
type = "mock"

[backends.judge]
type = "mock"
sharpness = 3.0

[points]
default_backend = "mock"

[points.retry]
define = "ouroloop.points:retry_point"

[research]
agent = "test_loop:FixedAgent"
judge = "judge"
min_items = 20
""")
    cfg = config.load(tmp_path / "ouroloop.toml")
    rt = config.build_runtime(cfg, tmp_path)
    agent, llm, judge, kw = config.build_research(cfg, rt)
    assert isinstance(agent, FixedAgent) and llm is None and judge is rt.backends["judge"] and kw == {"min_items": 20}
    seed_ledger(rt, sessions=40)
    res = run_round(rt, agent, llm, judge, **kw)
    assert [e["hypothesis"] for e in res.evaluated] == ["config-loaded agent"]
    assert rt.root == tmp_path / "ws"
