"""M8 offline acceptance: every promotion goes through one gate frozen before the round, on sessions no gate has
used; promotions persist and compare-and-swap; a running session keeps its release (PLAN_v0.2 M8). Plus the M7
safety review's leftovers. The ledger is written the way the M6 collection writes it."""
import json
import re
import threading
from contextlib import contextmanager

import httpx
import pytest

from conftest import SMALL_GATE
from ouroloop import config
from ouroloop.agent.loop import SYSTEM
from ouroloop.agent.prompt import PromptStore
from ouroloop.backends import LlmBackend
from ouroloop.backends.http import HttpBackend
from ouroloop.cli import main
from ouroloop.evolve import experience
from ouroloop.evolve.gate import TEMPLATE, freeze_template, open_record, records, run
from ouroloop.evolve.harness import prompt_evaluator
from ouroloop.evolve.pipeline import evolve_model, prepare_model, promote_model, recipe_evaluator
from ouroloop.evolve.registry import PromotionConflict, Registry
from ouroloop.evolve.replay import EvalResult
from ouroloop.evolve.train import TrainError, TrainResult
from ouroloop.ledger import CalibrationStore
from ouroloop.points import retry_point, stop_point
from ouroloop.policy import Policy
from ouroloop.providers import MockProvider
from ouroloop.release import code_version
from ouroloop.research import Candidate, run_round
from ouroloop.runtime import Runtime
from ouroloop.serve import make_server
from ouroloop.stop import StopGuard
from ouroloop.types import Answer


class Model:
    """Says p(yes) to every question, and reports itself as `model`."""
    name = "model"

    def __init__(self, p_yes: float = 0.5, model: str = "base"):
        self.p_yes, self.model = p_yes, model

    def decide(self, requests):
        return [Answer({"yes": self.p_yes, "no": 1 - self.p_yes}, self.model) for _ in requests]


class Oracle:
    """Reads the label from the view ("say yes" / "say no") and gives it probability q."""
    name = "oracle"

    def __init__(self, model: str = "R1", q: float = 0.7):
        self.model, self.q = model, q

    def decide(self, requests):
        out = []
        for r in requests:
            label = "yes" if re.search(r"say yes", r.state) else "no"
            out.append(Answer({label: self.q, "no" if label == "yes" else "yes": 1 - self.q}, self.model))
        return out


class Down:
    """A serve that cannot be reached."""
    name = "down"

    def decide(self, requests):
        raise ConnectionError("serve is down")


class OracleTrainer:
    """Trains nothing: its model is an Oracle. With `fail`, it raises as a crashed training job does."""

    def __init__(self, model: str = "R1", q: float = 0.7, fail: bool = False):
        self.model, self.q, self.fail = model, q, fail

    def train(self, job):
        if self.fail:
            raise TrainError("CUDA out of memory")
        return TrainResult(f"runs/{job.version}", {"type": "test_gate_record:Oracle", "model": self.model,
                                                   "q": self.q}, self.model)

    def backend(self, result):
        return config.build_backend(result.backend)


class Proposes:
    """A research agent that proposes these (kind, point, change) candidates, in this order."""

    def __init__(self, *candidates):
        self.candidates = candidates

    def step(self, ws):
        return [Candidate(kind, point, change, f"{kind} candidate") for kind, point, change in self.candidates]


CFG = {"workspace": {"root": "ws"}, "registry": {"base": "base"},
       "backends": {"base": {"type": "test_gate_record:Model", "p_yes": 0.5, "model": "base"}},
       "points": {"default_backend": "base", "stop": {"backend": "champion", "act": True, "explore": 0.0}}}
# Scaled to a few hundred synthetic sessions: a looser risk, so that about 20 calibration sessions buy a threshold.
ACT_GATE = {**SMALL_GATE, "risk": 0.2, "checks": {"min_train": 20, "max_mde": 0.5}}


def ctx(label: str = "yes") -> dict:
    return {"task": "t", "turns": 1, "files": "", "actions": "", "message": f"say {label}"}


def collect(rt, n: int, start: int = 0, held_every: int = 10) -> None:
    """n sessions as the M6 collection writes them: a session event (task, held_out, agent), then a stop decision
    recorded without asking a model, labelled by the probe. Odd sessions end "yes", even ones "no"; every
    held_every-th is a held-out task's."""
    stop = rt.points["stop"]
    for i in range(start, start + n):
        s, label = f"s_{i:04d}", "yes" if i % 2 else "no"
        rt.ledger.append({"type": "session", "session": s, "task": f"t{i % 7}", "held_out": i % held_every == 0,
                          "agent": "glm/max_turns=4"})
        c = {**ctx(label), "moment": "after a turn"}
        view, _ = stop.render(c)
        did = rt.ledger.decision(
            session=s, ctx=rt.sessions.put(s, c), point="stop", spec_hash=stop.spec_hash, backend="none", model="",
            view=view, view_chars=len(view), truncated=False, question=stop.question(c).to_json(),
            candidates=["yes", "no"], probs=None, route="shadow", explored=False, propensity=1.0, action="no",
            usage={}, phase="per_turn", default="no")
        rt.outcome(did, label, source="env", note="probe", score=1.0)


def workspace(tmp_path, n: int = 150):
    rt = config.build_runtime(CFG, tmp_path)
    collect(rt, n)
    freeze_template(rt.root, ACT_GATE)
    return rt


def held_out(rt) -> set:
    return {s for s, info in rt.ledger.sessions()[0].items() if info.get("held_out")}


@contextmanager
def serving(rt):
    """`ouroloop serve` on this runtime; yields ask(session, label) -> the decision served over HTTP."""
    server = make_server(rt, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield lambda session, label="yes": httpx.post(f"{url}/ask", json={
            "point": "stop", "session": session, "phase": "per_turn", "ctx": ctx(label)}).json()
    finally:
        server.shutdown()


# ---------------------------------------------------------------- the frozen template
def test_once_frozen_the_config_cannot_loosen_the_gate(tmp_path):
    rt = workspace(tmp_path)
    for loose in ({"research": {"min_items": 5}}, {"suite": {"min_items": 1}}, {"checks": {"min_train": 1}}):
        with pytest.raises(ValueError, match="the gate is frozen"):
            config.build_runtime({**CFG, **loose}, tmp_path)
    card = evolve_model(rt, OracleTrainer())
    (record,) = records(rt.root)
    assert card["gate"] == "accepted" and record.params["min_sessions"] == 30
    assert record.params["checks"] == ACT_GATE["checks"] and TEMPLATE["checks"] == {"min_train": 200, "max_mde": 0.1}
    # Nor can a candidate: its own gate settings never override the template's (here, 10 prompt runs)
    g, _ = run(open_record(rt), lambda: EvalResult("harness", [1.0] * 3, [0.0] * 3, ["a", "b", "c"], lambda: None,
                                                   gate={"min_items": 1}))
    assert g.outcome == "insufficient_evidence"
    # `ouroloop gate freeze` refuses a config that would not start once frozen, and takes changed parameters
    toml = tmp_path / "ouroloop.toml"
    toml.write_text('[workspace]\nroot = "other"\n\n[checks]\nmin_train = 1\n')
    with pytest.raises(ValueError, match=r"remove \[checks\]"):
        main(["gate", "freeze", "--config", str(toml)])
    toml.write_text('[workspace]\nroot = "other"\n')
    assert main(["gate", "freeze", "--config", str(toml), "--set", "min_sessions=40"]) == 0
    assert json.loads((tmp_path / "other" / "gates" / "template.json").read_text())["min_sessions"] == 40


# ---------------------------------------------------------------- test sessions and verdicts
def test_a_record_is_used_once_and_old_sessions_can_only_validate(tmp_path):
    rt = workspace(tmp_path)
    first = evolve_model(rt, OracleTrainer("R1", 0.7))
    (r1,) = records(rt.root)
    assert first["gate"] == "accepted" and r1.used and r1.fresh
    again = evolve_model(rt, OracleTrainer("R2", 0.99), record=r1)          # the same test sessions again
    assert again["gate"] == "validated" and Registry(rt.root).champion()["version"] == first["version"]
    collect(rt, 10, start=150)                                               # fewer new sessions than a gate needs
    thin = evolve_model(rt, OracleTrainer("R2", 0.99))
    r2 = records(rt.root)[-1]
    new = {f"s_{i:04d}" for i in range(150, 160)}
    assert thin["gate"] == "validated" and not r2.fresh and not set(r2.test_sessions) & new   # only old ones
    collect(rt, 30, start=160)
    fresh = evolve_model(rt, OracleTrainer("R2", 0.99))
    r3 = records(rt.root)[-1]
    assert fresh["gate"] == "accepted" and r3.fresh and Registry(rt.root).champion()["version"] == fresh["version"]
    assert set(r3.test_sessions) == {f"s_{i:04d}" for i in range(150, 190)} - held_out(rt)   # all of them
    assert Registry(rt.root).seen_sessions() >= set(r1.test_sessions) | set(r3.test_sessions)


def test_research_candidates_of_every_kind_go_through_the_record(tmp_path):
    rt = workspace(tmp_path)
    spec = ("spec", "stop", {"instructions": "Is the task complete?"})
    evaluators = {"recipe": recipe_evaluator(OracleTrainer("R1"))}
    res = run_round(rt, Proposes(spec, ("recipe", "stop", {"epochs": 1.0})), None, Model(), evaluators=evaluators)
    # the spec changes nothing the model sees: rejected, and the round's one verdict ends the round
    assert [(e["kind"], e["gate"], e["record"], e["used"]) for e in res.evaluated] == [("spec", "rejected", "r_0001",
                                                                                         True)]
    collect(rt, 40, start=150)
    res = run_round(rt, Proposes(("recipe", "stop", {"epochs": 1.0}), spec), None, Model(), evaluators=evaluators)
    assert [(e["kind"], e["gate"], e["record"]) for e in res.evaluated] == [("recipe", "accepted", "r_0002")]
    champion = Registry(rt.root).champion()["version"]
    # With no unused record, no candidate of any kind is promoted
    evaluators = {"recipe": recipe_evaluator(OracleTrainer("R3", 0.99))}
    res = run_round(rt, Proposes(spec, ("recipe", "stop", {"epochs": 2.0})), None, Model(), evaluators=evaluators,
                    record=records(rt.root)[-1])
    assert [(e["kind"], e["gate"], e["used"]) for e in res.evaluated] == [("spec", "rejected", False),
                                                                          ("recipe", "validated", False)]
    assert Registry(rt.root).champion()["version"] == champion and not rt.specs.get("stop")


def test_a_prompt_gate_runs_the_champion_again(tmp_path, frozen):
    from test_harness import FIX, setup, task
    tasks = [task("a"), task("b"), task("c")]
    rt, _, runner = setup(tmp_path)
    frozen(rt.root, suite={"min_items": 3})
    recorded = runner.runs(PromptStore(rt.root).get(), tasks)            # the champion's runs from earlier rounds
    ev = prompt_evaluator(tasks, runner, repeats=1, screen=0)
    agent = Proposes(("prompt", "harness", {"system": f"{SYSTEM} {FIX}"}))
    used = open_record(rt)
    used.consume("another candidate")
    res = run_round(rt, agent, None, rt.backends["mock"], evaluators={"prompt": ev}, record=used)
    assert [e["gate"] for e in res.evaluated] == ["validated"] and not PromptStore(rt.root).get().system.endswith(FIX)
    res = run_round(rt, agent, None, rt.backends["mock"], evaluators={"prompt": ev})
    record = records(rt.root)[-1]                                       # the next round froze a new record
    assert [(e["gate"], e["record"]) for e in res.evaluated] == [("accepted", record.round)]
    assert record.round != used.round
    ran = record.usage["sessions"]
    assert len(ran) == 6 and not set(ran) & {r["session"] for r in recorded}    # champion and challenger, anew
    assert Registry(rt.root).seen_sessions() >= set(ran) and PromptStore(rt.root).get().system.endswith(FIX)


# ---------------------------------------------------------------- promotion
def test_two_candidates_from_one_parent_cannot_both_be_promoted(tmp_path):
    rt = workspace(tmp_path)
    reg, record = Registry(rt.root), open_record(rt)
    a = prepare_model(rt, OracleTrainer("R1"), record=record, registry=reg)
    b = prepare_model(rt, OracleTrainer("R2", 0.99), record=record, registry=reg)
    assert a.card["parent"] is None and b.card["parent"] is None
    promote_model(rt, a, reg, record.hash)
    with pytest.raises(PromotionConflict):
        promote_model(rt, b, reg, record.hash)
    assert reg.champion()["version"] == a.version
    # Through the gate: another promotion lands while the candidate trains
    reg.record({"version": "m_0099", "parent": a.version, "points": ["stop"], "model_id": "X",
                "backend": {"type": "test_gate_record:Model", "model": "X"}})

    class Racing(OracleTrainer):
        def train(self, job):
            reg.promote("m_0099", a.version)
            return super().train(job)

    collect(rt, 40, start=150)
    card = evolve_model(rt, Racing("R3", 0.99))
    assert card["gate"] == "conflict" and card["parent"] == a.version and reg.champion()["version"] == "m_0099"


def test_a_promotion_reaches_new_sessions_only_and_a_rollback_stops_the_old_ones(tmp_path):
    rt = workspace(tmp_path)                    # held by a long-running serve, never rebuilt below
    harness = {"system": "p", "tools": {}, "stop": {"max_pushbacks": 2}}
    with serving(rt) as ask:
        rt.session("s_before", {"task": "t"}, harness)
        first = evolve_model(config.build_runtime(CFG, tmp_path), OracleTrainer("R1"))   # another process
        (r1,) = records(rt.root)
        assert (first["model_id"], first["gate_record"], first["test_sessions"]) == ("R1", r1.hash,
                                                                                    r1.test_sessions)
        rt.session("s_r1", {"task": "t"}, harness)
        assert [(d["model"], d["acted"]) for d in (ask("s_before"), ask("s_r1"))] == [("base", False), ("R1", True)]
        collect(rt, 40, start=150)
        second = evolve_model(config.build_runtime(CFG, tmp_path), OracleTrainer("R2", 0.99))
        rt.session("s_r2", {"task": "t"}, harness)
        assert [(d["model"], d["acted"]) for d in (ask("s_r1"), ask("s_r2"))] == [("R1", True), ("R2", True)]
        assert ask("s_first_asked")["model"] == "R2"        # a session never opened is fixed at its first ask
        restarted = config.build_runtime(CFG, tmp_path)     # a serve restarted mid-session resumes its release
        assert [restarted.ask("stop", ctx(), session=s, phase="per_turn").model for s in ("s_before", "s_r1")] == [
            "base", "R1"]
        # the release: all three components; a model promotion changes the model only
        releases = [e["release"] for e in rt.ledger.events() if e["type"] == "session" and "release" in e]
        assert [r["model"] for r in releases] == [None, "R1", "R2"] and all(r["spec"] and r["harness"]
                                                                          for r in releases)
        assert len({(r["spec"], r["harness"]) for r in releases}) == 1
        # rebuilt: the mapping and the thresholds come from the registry; the config maps stop to its base
        cfg = {**CFG, "points": {"default_backend": "base", "stop": {"backend": "base", "act": True, "explore": 0.0}}}
        fresh = config.build_runtime(cfg, tmp_path)
        d = fresh.ask("stop", ctx(), session="s_rebuilt", phase="per_turn")
        assert fresh.point_backends["stop"] == "champion" and (d.model, d.acted) == ("R2", True)
        # rollback, in another process: R2's session takes its defaults, and never moves to R1
        assert Registry(rt.root).rollback() == first["version"]
        d = ask("s_r2")
        assert (d["model"], d["acted"], d["action"]) == ("R2", False, "no")
        assert rt.ledger.rows()[-1].decision["revoked"] == second["version"]
        d = config.build_runtime(CFG, tmp_path).ask("stop", ctx(), session="s_r2", phase="per_turn")
        assert (d.model, d.acted) == ("R2", False)          # and so after a restart
        assert rt.calibration_for("stop", "R2")["gate"]       # its gated threshold stays in the store, unused
        rt.session("s_after", {"task": "t"}, harness)
        assert [(d["model"], d["acted"]) for d in (ask("s_r1"), ask("s_after"))] == [("R1", True), ("R1", True)]
    # held-out tasks: never trained or calibrated on, reported beside the verdict
    held = held_out(rt)
    assert r1.data["held_out_sessions"] and set(r1.data["held_out_sessions"]) <= held
    assert not set(r1.test_sessions) & held
    assert first["metrics"]["stop"]["held_out"]["sessions"] == len(r1.data["held_out_sessions"])
    trained = {json.loads(line)["source_id"] for part in ("train", "calibration")
               for line in (rt.root / "models" / first["version"] / "data" / f"{part}.jsonl").open()}
    assert trained and not trained & (held | set(r1.test_sessions))


def test_infrastructure_failures_are_inconclusive_and_use_the_record_only_after_test_inference(tmp_path):
    rt = workspace(tmp_path)
    card = evolve_model(rt, OracleTrainer(fail=True))
    (record,) = records(rt.root)
    assert card["gate"] == "inconclusive" and not record.used and not Registry(rt.root).seen_sessions()
    assert experience.read(rt.root)[-1]["verdict"] == "inconclusive"
    assert open_record(rt).round == record.round                   # the rerun uses the same record
    rt.backends["champion"] = Down()          # the champion's serve fails once the test inference has begun
    card = evolve_model(rt, OracleTrainer())
    assert card["gate"] == "inconclusive" and record.used and Registry(rt.root).champion() is None
    assert Registry(rt.root).seen_sessions() == set(record.test_sessions)
    assert open_record(rt).round == "r_0002"


# ---------------------------------------------------------------- the M7 safety review's leftovers
def test_only_stop_may_be_switched_on_wherever_a_point_is_registered(tmp_path, gated):
    rt = Runtime(tmp_path, {"m": Model(0.99, "A")}, "m", seed=0)
    with pytest.raises(ValueError, match="only"):
        rt.register(retry_point(policy=Policy(act=True)))
    rt.register(retry_point()).policy = Policy(act=True, explore=0.0)      # switched on after registering
    gated(rt, "retry", "A", 0.5)
    d = rt.ask("retry", {"command": "make", "stderr": "x", "exit_code": 1})
    assert (d.route, d.acted, d.action) == ("shadow", False, "no")


def test_a_stale_calibration_store_cannot_bring_back_what_another_process_changed(tmp_path):
    path = tmp_path / "calibration.json"
    serve, promoter = CalibrationStore(path), CalibrationStore(path)
    promoter.set("A", "h", "stop", {"thresholds": {"per_turn": 0.9}, "gate": "g1"})
    opened = serve.entries()                                       # what a session opened with
    promoter.set("A", "h", "stop", {"thresholds": {"per_turn": 0.97}, "gate": "g2"})    # recalibrated elsewhere
    serve.set("B", "h", "stop", {"thresholds": {}, "gate": "g3"})
    now = CalibrationStore(path)
    assert (now.get("A", "h", "stop")["gate"], now.get("B", "h", "stop")["gate"]) == ("g2", "g3")
    assert opened["A"]["h"]["stop"]["gate"] == "g1" and serve.get("A", "h", "stop")["gate"] == "g2"


def test_a_breaking_rule_or_view_leaves_a_row_and_takes_the_default(tmp_path):
    rt = Runtime(tmp_path, {"m": Model(0.99, "A")}, "m", seed=0)
    rt.register(stop_point(policy=Policy(act=True, explore=0.0)))

    def crashes(action):
        raise RuntimeError("the checker crashed")

    for decide in (rt.ask, rt.record):
        d = decide("stop", ctx(), phase="on_finish", veto=crashes)
        assert (d.route, d.action) == ("error", "yes")
    d = rt.record("stop", {}, phase="per_turn")                    # a context the view cannot render
    assert (d.route, d.action) == ("error", "no")
    rows = [r.decision for r in rt.ledger.rows()]
    assert [r["route"] for r in rows] == ["error"] * 3 and all("Error" in r["error"] for r in rows)
    assert rt.sessions.get(rows[-1]["ctx"]) == {}                  # the raw context is kept
    call = StopGuard(rt, "s", checker=lambda: 1 / 0)("on_finish", ctx(), 1)
    assert (call.stop, call.checked) == (True, None) and rt.ledger.rows()[-1].decision["route"] == "error"


def test_serve_forwards_an_abstention_to_its_clients(tmp_path, gated):
    unsure = Runtime(tmp_path / "gpu", {"llm": LlmBackend(MockProvider(lambda s, p: "hard to say"))}, "llm")
    server = make_server(unsure, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        client = Runtime(tmp_path / "harness", {"http": HttpBackend(url, token_env=None)}, "http", seed=0)
        client.register(stop_point(policy=Policy(act=True, explore=0.0)))
        gated(client, "stop", "mock:scripted", 0.0)
        d = client.ask("stop", ctx(), phase="per_turn")
        (row,) = [r.decision for r in client.ledger.rows()]
        assert row["abstained"] and (d.route, d.acted, d.action) == ("default", False, "no")
        assert HttpBackend(url, token_env=None).code_version() == code_version()    # for the gate records
    finally:
        server.shutdown()
