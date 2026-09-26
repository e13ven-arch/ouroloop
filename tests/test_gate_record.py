"""M8 offline acceptance: every promotion goes through one gate frozen before the round, on sessions no gate has
used; promotions persist and compare-and-swap; a running session keeps its release (PLAN_v0.2 M8). Plus the M7
safety review's leftovers. The ledger is written the way the M6 collection writes it."""
import json
import random
import re
import threading
from contextlib import contextmanager

import httpx
import pytest

from conftest import SMALL_GATE, accept
from ouroloop import config
from ouroloop.agent.loop import SYSTEM
from ouroloop.agent.prompt import PromptStore
from ouroloop.backends import LlmBackend
from ouroloop.backends.http import HttpBackend
from ouroloop.cli import main
from ouroloop.evolve import experience
from ouroloop.evolve.gate import (TEMPLATE, decide_points, freeze_template, load_template, open_record,
                                  records, run)
from ouroloop.evolve.harness import prompt_evaluator
from ouroloop.evolve.plan import admit, decide, sessions_needed
from ouroloop.evolve.pipeline import default_errors, evolve_model, prepare_model, promote_model, recipe_evaluator
from ouroloop.evolve.registry import Registry
from ouroloop.evolve.replay import EVALUATORS, EvalResult, labelled
from ouroloop.evolve.train import TrainError, TrainResult
from ouroloop.ledger import CalibrationStore, PromotionConflict, SpecStore
from ouroloop.points import retry_point, stop_point
from ouroloop.policy import Policy
from ouroloop.providers import MockProvider
from ouroloop.release import code_version, revoke
from ouroloop.research import Candidate, run_round
from ouroloop.runtime import Runtime
from ouroloop.serve import make_server
from ouroloop.stop import MOMENTS, StopGuard
from ouroloop.trainers.signature import SignatureTrainer
from ouroloop.types import Answer

pytestmark = pytest.mark.usefixtures("loose_risk")


class Model:
    """Says p(yes) to every question, and reports itself as `model`."""
    name = "model"

    def __init__(self, p_yes: float = 0.5, model: str = "base"):
        self.p_yes, self.model = p_yes, model

    def decide(self, requests):
        return [Answer({"yes": self.p_yes, "no": 1 - self.p_yes}, self.model) for _ in requests]


class Oracle:
    """Reads the label from the view ("say yes" / "say no") and gives it probability q; with `knows`, only on the
    tasks it matches, and cannot tell on the others."""
    name = "oracle"

    def __init__(self, model: str = "R1", q: float = 0.7, knows: str | None = None):
        self.model, self.q, self.knows = model, q, knows

    def decide(self, requests):
        out = []
        for r in requests:
            label = "yes" if re.search(r"say yes", r.state) else "no"
            if self.knows and not re.search(rf"task: {self.knows}\n", r.state):
                out.append(Answer({"yes": 0.5, "no": 0.5}, self.model))
                continue
            out.append(Answer({label: self.q, "no" if label == "yes" else "yes": 1 - self.q}, self.model))
        return out


class Heeds(Oracle):
    """An Oracle when asked "Is the task done?", else it cannot tell."""

    def decide(self, requests):
        return [a if r.question.instructions == "Is the task done?" else Answer({"yes": 0.5, "no": 0.5}, self.model)
                for r, a in zip(requests, super().decide(requests))]


class Drifts(Oracle):
    """An Oracle on a session's first turn; after it, the opposite of the label."""

    def decide(self, requests):
        return [a if "turns so far: 1\n" in r.state else Answer({k: 1 - v for k, v in a.probs.items()}, self.model)
                for r, a in zip(requests, super().decide(requests))]


class Down:
    """A serve that cannot be reached."""
    name = "down"

    def decide(self, requests):
        raise ConnectionError("serve is down")


class Trainer:
    """Trains nothing: its model is `backend` (a backend config), as `model`."""

    def __init__(self, model: str, **backend):
        self.model, self.config = model, {**backend, "model": model}

    def train(self, job):
        return TrainResult(self.model, self.config)

    def backend(self, result):
        return config.build_backend(result.backend)


class OracleTrainer:
    """Trains nothing: its model is an Oracle. With `fail`, it raises as a crashed training job does."""

    def __init__(self, model: str = "R1", q: float = 0.7, fail: bool = False, knows: str | None = None):
        self.model, self.q, self.fail, self.knows = model, q, fail, knows

    def train(self, job):
        if self.fail:
            raise TrainError("CUDA out of memory")
        return TrainResult(self.model, {"type": "test_gate_record:Oracle", "model": self.model, "q": self.q,
                                        "knows": self.knows})

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
# The tasks a first model knows (an Oracle there, unsure elsewhere): it fixes some of the default errors, so a later
# model that knows every task still has something to gain.
SOME = "t[0-4]"


def ctx(label: str = "yes") -> dict:
    return {"task": "t0", "turns": 1, "files": "", "actions": "", "message": f"say {label}"}


def collect(rt, n: int, start: int = 0, held_every: int = 10, turns: int = 1, acted_by: str = "") -> None:
    """n sessions as the M6 collection writes them: a session event (task, held_out, agent), then a stop decision
    recorded without asking a model, labelled by the probe. Odd sessions end "yes", even ones "no"; every
    held_every-th is a held-out task's (held out is a property of the task, as in the suite). With `acted_by`, that
    model acted on the decision and was right, as the post-act monitor counts an act."""
    stop = rt.points["stop"]
    for i in range(start, start + n):
        s, label, held = f"s_{i:04d}", "yes" if i % 2 else "no", i % held_every == 0
        task = f"h{i % 3}" if held else f"t{i % 7}"
        rt.ledger.append({"type": "session", "session": s, "task": task, "held_out": held, "agent": "glm/max_turns=4"})
        c = {**ctx(label), "task": task, "turns": turns, "moment": "after a turn"}
        view, _ = stop.render(c)
        did = rt.ledger.decision(
            session=s, ctx=rt.sessions.put(s, c), point="stop", spec_hash=stop.spec_hash,
            backend="champion" if acted_by else "none", model=acted_by,
            view=view, view_chars=len(view), truncated=False, question=stop.question(c).to_json(),
            candidates=["yes", "no"], probs=None, route="act" if acted_by else "shadow", explored=False,
            propensity=1.0, action=label if acted_by else "no", usage={}, phase="per_turn", default="no")
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
    first = evolve_model(rt, OracleTrainer("R1", 0.7, knows=SOME))
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
    evaluators = {"recipe": recipe_evaluator(OracleTrainer("R1", knows=SOME))}
    res = run_round(rt, Proposes(spec, ("recipe", "stop", {"seed": 1})), None, Model(), evaluators=evaluators)
    # the spec changes nothing the model sees: rejected, and the round's one verdict ends the round
    assert [(e["kind"], e["gate"], e["record"], e["used"]) for e in res.evaluated] == [("spec", "rejected", "r_0001",
                                                                                         True)]
    collect(rt, 40, start=150)
    res = run_round(rt, Proposes(("recipe", "stop", {"seed": 1}), spec), None, Model(), evaluators=evaluators)
    assert [(e["kind"], e["gate"], e["record"]) for e in res.evaluated] == [("recipe", "accepted", "r_0002")]
    champion = Registry(rt.root).champion()["version"]
    # With no unused record, no candidate of any kind is promoted
    evaluators = {"recipe": recipe_evaluator(OracleTrainer("R3", 0.99))}
    res = run_round(rt, Proposes(spec, ("recipe", "stop", {"seed": 2})), None, Model(), evaluators=evaluators,
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


M6_CFG = {"workspace": {"root": "ws"}, "registry": {"base": "base"},
          "backends": {"base": {"type": "test_gate_record:Model", "p_yes": 0.5, "model": "base"},
                       "mock": {"type": "mock"}},
          "points": {"default_backend": "mock", "stop": {"backend": "champion", "act": True, "explore": 0.0},
                     "tool_gate": {}}}


def m6_sessions(rt, n: int, start: int = 0, crashed: tuple = ()) -> None:
    """n sessions as the M6 collection writes them: a session event (agent, task, held_out); stop decisions after
    two turns and when the agent wants to finish, recorded without a model and labelled by the probe when the session
    ends (a session in `crashed` broke before its probe); an unlabelled tool_gate decision. Tasks t0..t7 of the 29
    are held out. The view's last message tells the label; its actions carry an error name that agrees with the
    label 85% of the time, which is all the signature model reads."""
    rng = random.Random(start)
    stop, tool_gate = rt.points["stop"], rt.points["tool_gate"]
    for i in range(start, start + n):
        s, task, done = f"s_{i:04d}", f"t{i % 29}", rng.random() < 0.5
        rt.ledger.append({"agent": "glm-5.3-flash/max_turns=4", "task": task, "held_out": i % 29 < 8,
                          "type": "session", "session": s})
        for turn, phase, default in ((1, "per_turn", "no"), (2, "per_turn", "no"), (2, "on_finish", "yes")):
            label = "yes" if done and turn == 2 else "no"
            cue = label if rng.random() < 0.85 else {"yes": "no", "no": "yes"}[label]
            c = {"task": task, "turns": turn, "files": "", "message": f"say {label}", "moment": MOMENTS[phase],
                 "actions": "bash make -> TimeoutError" if cue == "yes" else "bash make -> KeyError"}
            view, truncated = stop.render(c)
            did = rt.ledger.decision(
                session=s, ctx=rt.sessions.put(s, c), point="stop", spec_hash=stop.spec_hash, backend="none",
                model="", view=view, view_chars=len(view), truncated=truncated, question=stop.question(c).to_json(),
                candidates=["yes", "no"], probs=None, route="shadow", explored=False, propensity=1.0, action=default,
                usage={}, phase=phase, default=default)
            if i not in crashed:
                rt.outcome(did, label, source="env", note="probe", score=1.0 if done else 0.25)
        c = {"task": task, "workdir": "/tmp/w", "tool": "read", "detail": "read calc.py"}
        view, _ = tool_gate.render(c)
        rt.ledger.decision(session=s, ctx=rt.sessions.put(s, c), point="tool_gate", spec_hash=tool_gate.spec_hash,
                           backend="mock", model="mock-keyword", view=view, route="shadow", action="no",
                           phase="before_call", default="no")


def trained_on(rt, version: str) -> set:
    """Every session in the trainer's directory."""
    return {json.loads(line)["source_id"] for p in (rt.root / "models" / version / "data").glob("*.jsonl")
            for line in p.open()}


def test_an_m6_ledger_through_freeze_evolve_gate_promote_and_the_next_rounds(tmp_path):
    rt = config.build_runtime(M6_CFG, tmp_path)
    m6_sessions(rt, 300, crashed=(299, 298))           # two of the latest broke before their probe
    freeze_template(rt.root, {"checks": {"min_train": 100, "max_mde": 0.2}, "min_cal_sessions": 60, "risk": 0.2})
    sessions, _ = rt.ledger.sessions()
    held = {s for s, info in sessions.items() if info["held_out"]}
    labelled = [s for s in sorted(sessions, key=lambda s: sessions[s]["start"])
                if "stop" in sessions[s]["labelled"] and s not in held]
    assert len(held) == 88 and not {"s_0298", "s_0299"} & set(labelled)

    first = evolve_model(rt, SignatureTrainer())
    (r1,) = records(rt.root)
    # the first record: the latest 30 sessions a gate can score, none held out, none without its labels
    assert r1.fresh and r1.test_sessions == labelled[-30:] and r1.used
    assert (first["model_id"], first["gate_record"], first["test_sessions"]) == ("signature:m_0001", r1.hash,
                                                                                r1.test_sessions)
    # the signature model reads a cue that is wrong 15% of the time: acting on it would harm more sessions than the
    # risk allows, so it gets no threshold, adds nothing over the defaults and is not promoted
    assert first["gate"] == "rejected" and Registry(rt.root).champion() is None
    assert first["calibration"]["stop"]["thresholds"] == {"per_turn": None, "on_finish": None}
    # held-out tasks: never trained or calibrated on, never tested; their scores are on the card beside the verdict
    trained = trained_on(rt, "m_0001")
    assert set(r1.data["held_out_sessions"]) <= held and not held & (set(r1.test_sessions) | trained)
    assert trained and not trained & set(r1.test_sessions)
    metrics = first["metrics"]["stop"]
    assert metrics["sessions"] == 30 and metrics["held_out"]["sessions"] == len(r1.data["held_out_sessions"]) > 0
    assert metrics["default_errors"] > 0 and metrics["challenger"]["acted"] == metrics["champion"]["acted"] == 0
    rt.session("s_live", {"task": "t9", "held_out": False})
    d = rt.ask("stop", {**ctx(), "actions": "TimeoutError"}, session="s_live", phase="per_turn")
    assert (d.model, d.acted) == ("base", False)

    # a second round with no new sessions: the used record's sessions are never scored again for a promotion
    second = evolve_model(rt, SignatureTrainer())
    r2 = records(rt.root)[-1]
    assert r2.round == "r_0002" and r2.used and not r2.fresh and not set(r2.test_sessions) - set(labelled)
    assert second["gate"] in ("validated", "rejected") and Registry(rt.root).champion() is None

    # ten new sessions: still too few. A training crash leaves that record unused, and the rerun takes it again
    m6_sessions(rt, 10, start=300)
    assert evolve_model(rt, OracleTrainer(fail=True))["gate"] == "inconclusive"
    r3 = records(rt.root)[-1]
    assert r3.round == "r_0003" and not r3.fresh and not r3.used and open_record(rt).round == "r_0003"
    # forty more: a record that could only validate gives way to one on the fresh sessions, all of them
    m6_sessions(rt, 40, start=310)
    r4 = open_record(rt)
    m6_sessions(rt, 10, start=350)                  # sessions that start while the candidate trains
    third = evolve_model(rt, OracleTrainer("R2", 0.9), record=r4)
    held = held_out(rt)
    new, later = {f"s_{i:04d}" for i in range(300, 350)} - held, {f"s_{i:04d}" for i in range(350, 360)} - held
    assert r4.round == "r_0004" and r4.fresh and set(r4.test_sessions) == new and not r3.used
    assert (third["gate"], third["parent"]) == ("accepted", None)
    assert Registry(rt.root).champion()["version"] == third["version"]
    assert not trained_on(rt, third["version"]) & (new | held | later)     # later: the next record's to test
    assert Registry(rt.root).seen_sessions() == set(r1.test_sessions) | set(r2.test_sessions) | new
    # the session opened before keeps its model; a new one runs the new champion
    rt.session("s_next", {"task": "t9", "held_out": False})
    assert [rt.ask("stop", ctx(), session=s, phase="per_turn").model for s in ("s_live", "s_next")] == [
        "base", "R2"]


def test_the_gate_counts_sessions_and_calibration_sessions(tmp_path):
    better = ([0] * 60, [-1] * 60)                              # the candidate fixes a default error on every row
    assert decide_points({"stop": (*better, [f"s{i % 20}" for i in range(60)])}, n_boot=200).outcome == \
        "insufficient_evidence"                                 # 60 items, but from 20 sessions
    assert decide_points({"stop": (*better, [f"s{i % 30}" for i in range(60)])}, n_boot=200).outcome == "accepted"
    # fewer calibration sessions than the template asks: a temperature, no threshold, so the candidate could never
    # act and adds nothing over the defaults, however well it answers
    rt = config.build_runtime(CFG, tmp_path)
    collect(rt, 150)
    freeze_template(rt.root, {**ACT_GATE, "min_cal_sessions": 1000})
    card = evolve_model(rt, OracleTrainer("R1"))
    cal = card["calibration"]["stop"]
    assert card["gate"] == "rejected" and cal["temperature"] < 1 and cal["thresholds"] == {"per_turn": None,
                                                                                          "on_finish": None}
    assert card["metrics"]["stop"]["challenger"]["acted"] == 0 and Registry(rt.root).champion() is None
    assert rt.ask("stop", ctx(), session="s_new", phase="per_turn").acted is False


# ---------------------------------------------------------------- the gain objective and the training schedule
def test_the_gate_judges_the_gain_over_the_default_within_the_risk(tmp_path):
    # a model that only says what the default says ("no", confidently) never acts: it gains nothing
    rt = workspace(tmp_path / "agrees")
    card = evolve_model(rt, Trainer("NO", type="test_gate_record:Model", p_yes=0.01))
    m = card["metrics"]["stop"]
    assert (card["gate"], card["reasons"]) == ("rejected", ["no point's gain rose significantly"])
    assert m["challenger"]["acted"] == 0 and m["default_errors"] > 0 and Registry(rt.root).champion() is None
    # one that fixes default errors and harms no session is promoted; its card says how much of the headroom
    rt = workspace(tmp_path / "fixes")
    card = evolve_model(rt, OracleTrainer("R1"))
    m = card["metrics"]["stop"]
    assert card["gate"] == "accepted" and card["reasons"] == ["gain rose significantly on stop"]
    assert m["challenger"]["fixes"] == m["default_errors"] > 0 and m["challenger"]["harms"] == 0
    assert m["challenger"]["fixed_share"] == 1.0 and m["champion"]["acted"] == 0 and card["default_errors"] > 0
    # one right on the calibration sessions that harms most of the test sessions it acts in is not
    rt = config.build_runtime(CFG, tmp_path / "drifts")
    collect(rt, 150)
    collect(rt, 40, start=150, turns=2)             # the test sessions: later turns, where it answers wrong
    freeze_template(rt.root, ACT_GATE)
    card = evolve_model(rt, Trainer("D", type="test_gate_record:Drifts"))
    assert card["calibration"]["stop"]["thresholds"]["per_turn"] is not None and card["gate"] == "rejected"
    assert re.fullmatch(r"stop harms (\d+) of the \1 test sessions it acts in: above the risk 0.2 with confidence",
                        card["reasons"][0])
    # the monitor's rule, before promotion: a candidate that gains overall but harms every session it acts in
    rows = [-1, -1, -1, 1] * 40
    sessions = [f"s{i // 4}" for i in range(160)]
    gains = {"stop": ([0] * 160, rows, sessions)}
    assert decide_points(gains, n_boot=200).outcome == "accepted"
    assert decide_points(gains, n_boot=200, risk=0.2).outcome == "rejected"


def test_no_model_trains_before_the_data_could_buy_a_threshold(tmp_path):
    """The decisive fact, before a GPU minute is spent: with risk r a threshold needs sessions_needed(r) sessions
    with no error at all, and only a session holding a default error can buy one, because only there does a model
    act. With fewer, no model trained on this data could ever act, and the round says so (PLAN_v0.2 M11)."""
    assert (sessions_needed(0.05), sessions_needed(0.2)) == (59, 14)
    rt = workspace(tmp_path / "thin", n=60)
    proposed, train, reasons, d = decide(rt)
    assert proposed is not None and not train and d.points["stop"].error_sessions < 14
    assert reasons[0].startswith("stop: ") and "needs 14" in reasons[0] and "could ever act" in reasons[0]
    # the same data grown: the sessions that hold a default error can buy a threshold, and the plan is admitted
    rt = workspace(tmp_path / "enough", n=150)
    proposed, train, reasons, d = decide(rt)
    assert train and reasons == ["no candidate trained yet"] and d.points["stop"].error_sessions >= 14
    assert proposed.predicted_gain == 0.5 and proposed.cost_minutes is None      # nothing has trained: no wall time
    assert proposed.hypothesis.startswith("no candidate trained yet") and proposed.change == {}


def test_a_revocation_is_evidence_not_a_reason_to_train(tmp_path):
    """A champion revoked after acting in two sessions leaves almost the same data behind: the next candidate would
    see it and fail the same way, so the round trains nothing. Once the revoked model's sessions do add materially
    to the training rows, the revocation is a reason - once. Only the acting sessions a candidate could train on
    count: not this round's test sessions, and not the ones the last candidate already saw, so one revocation
    never proposes a round again and again while the ledger grows around it."""
    rt = workspace(tmp_path, n=150)
    accept(rt.root, {"version": "m_0001", "gate": "accepted", "model_id": "R1", "points": ["stop"],
                     "parent": None, "default_errors": default_errors(rt, ["stop"]),
                     "data": {"stop": {"train": 60, "val": 5}},
                     "backend": {"type": "test_gate_record:Oracle", "model": "R1", "q": 0.7},
                     "metrics": {"stop": {"challenger": {"fixed_share": 0.2}}}})
    Registry(rt.root).promote("m_0001", None)
    collect(rt, 2, start=150, held_every=999, acted_by="R1")          # the two sessions it acted in
    revoke(rt.root, "m_0001", "R1", "the monitor")
    proposed, train, reasons, d = decide(rt)
    assert proposed is None and not train and (d.champion.acted_sessions, d.champion.rows) == (2, 0)
    assert reasons == [f"m_0001 was revoked after acting in 2 sessions, which left 0 labelled rows no candidate has "
                       f"trained on: too little new data to train on again; a revocation is evidence, not a reason "
                       f"to train"]
    collect(rt, 40, start=152, held_every=999, acted_by="R1")         # sessions enough to be worth another try
    proposed, train, reasons, d = decide(rt)
    # the 30 latest of the 42 sessions it acted in are this round's test set: only the other 12 could train anything
    assert proposed is not None and train and (d.champion.acted_sessions, d.champion.rows) == (42, 12)
    assert (d.champion.new_sessions, d.points["stop"].test_sessions) == (12, 30)
    assert "brought 12 labelled rows no candidate has trained on" in reasons[0]
    # a candidate trains on them and is rejected; the same revocation is not a reason a second time
    card = evolve_model(rt, Trainer("NO", type="test_gate_record:Model", p_yes=0.01), plan=proposed)
    assert card["gate"] == "rejected"
    proposed, train, reasons, d = decide(rt)
    assert proposed is None and not train and d.champion.rows == 0 and "too little new data" in reasons[0]


def test_a_hypothesis_that_already_failed_on_as_much_data_is_refused(tmp_path):
    from dataclasses import replace
    rt = workspace(tmp_path, n=150)
    proposed, train, _, d = decide(rt)
    assert train
    experience.append(rt.root, {"kind": "model", "version": "m_0001", "gate": "rejected",
                                "reasons": ["no point's gain rose significantly"], "plan": proposed.to_json()})
    params = load_template(rt.root)[0]
    ok, reasons = admit(rt, proposed, d, params)
    assert not ok and reasons == [f"the same recipe change already failed on as much data (m_0001: rejected - no "
                                  f"point's gain rose significantly); a new hypothesis or more data is needed"]
    grown = replace(proposed, data={**proposed.data, "train_rows": proposed.data["train_rows"] + 100})
    assert admit(rt, grown, d, params) == (True, [])


def test_an_infrastructure_failure_leaves_the_same_plan_to_run_again(tmp_path):
    """A crashed training job is a failure of the machinery, not of the hypothesis (gate.INFRA: "never evidence
    against it"), so the plan is still the right thing to run and the next round runs it, instead of asking for a
    new hypothesis until the ledger has grown."""
    rt = workspace(tmp_path, n=150)
    proposed, train, _, _ = decide(rt)
    assert train
    card = evolve_model(rt, OracleTrainer(fail=True), plan=proposed)
    assert card["gate"] == "inconclusive" and [e["gate"] for e in experience.read(rt.root)] == ["inconclusive"]
    again, train, reasons, _ = decide(rt)
    assert train and reasons == ["no candidate trained yet"] and again.data == proposed.data


def test_no_model_trains_where_the_template_fits_no_threshold_at_all(tmp_path):
    """min_cal_sessions is as hard a precondition as the risk: below it calibrate_point gives every phase a
    threshold of None, so the candidate could never act and the gate could only reject it. The round refuses before
    the GPU minutes, however many calibration sessions hold a default error."""
    rt = config.build_runtime(CFG, tmp_path)
    collect(rt, 150)
    freeze_template(rt.root, {**ACT_GATE, "min_cal_sessions": 1000})
    proposed, train, reasons, d = decide(rt)
    cal = d.points["stop"].calibration_sessions
    assert proposed is not None and not train and d.points["stop"].error_sessions >= sessions_needed(0.2)
    assert reasons == [f"stop: {cal} calibration sessions, the template fits no threshold below 1000; no model "
                       f"trained on this data could ever act, so collect {1000 - cal} more"]


def test_a_risk_no_threshold_could_meet_is_a_reason_not_a_search(tmp_path):
    """A risk of 0 is a template gate.LIMITS still allows, and no number of clean sessions bounds an error rate
    below it: the round says so, instead of counting sessions forever."""
    with pytest.raises(ValueError, match="a risk of 0 lets no threshold be fitted at all"):
        sessions_needed(0)
    rt = config.build_runtime(CFG, tmp_path)
    collect(rt, 150)
    freeze_template(rt.root, {**ACT_GATE, "risk": 0.0})
    proposed, train, reasons, _ = decide(rt)
    assert proposed is not None and not train and len(reasons) == 1
    assert reasons[0].startswith("gate template: a risk of 0.0 lets no threshold be fitted at all")


def test_a_point_that_cannot_decide_is_left_out_of_the_plan_not_a_veto(tmp_path):
    """The gate neither passes nor blocks on a point with too few test sessions (decide_points), so the admission
    drops it and trains on the points that can decide: a rare second point must not keep the main one from ever
    training."""
    rt = config.build_runtime({**CFG, "points": {**CFG["points"], "retry": {"backend": "champion"}}}, tmp_path)
    collect(rt, 150)
    freeze_template(rt.root, ACT_GATE)
    proposed, train, reasons, d = decide(rt)
    assert set(d.points) == {"stop", "retry"} and d.points["retry"].test_sessions == 0
    assert train and proposed.points == ["stop"]
    assert reasons == ["retry is left out of the plan: test items from 0 sessions, the gate needs 30; neither a pass "
                       "nor a block could come from this point"]


def test_the_plan_aims_at_a_gain_the_rounds_test_set_could_show(tmp_path):
    """A standing model near the target leaves a residual the round's test set cannot detect. A plan that predicted
    it could never be admitted, however much data arrived, so the planner aims at the smallest gain that test set
    could confirm and lets the gate measure the real one."""
    rt = workspace(tmp_path, n=150)
    Registry(rt.root).record({"version": "m_0001", "gate": "accepted", "model_id": "R1", "default_errors": 1,
                              "data": {"stop": {"train": 60}},
                              "metrics": {"stop": {"challenger": {"fixed_share": 0.45}}}})
    proposed, train, reasons, d = decide(rt)
    point = d.points["stop"]
    assert (d.reached, d.target) == (0.45, 0.5) and reasons[0].startswith("default errors doubled since m_0001")
    assert train and proposed.predicted_gain > d.target - d.reached       # the residual, 5%, is below the mde
    assert proposed.per_row(point) > point.mde > 0 and f"{proposed.predicted_gain:.0%}" in proposed.hypothesis


def test_the_default_errors_doubling_since_the_last_candidate_is_a_reason_to_train(tmp_path):
    """The other side of the schedule: a candidate trained and was rejected, and the round trains again once the
    data a model could learn from has doubled - not before."""
    rt = workspace(tmp_path, n=150)
    proposed, train, _, _ = decide(rt)
    assert train
    card = evolve_model(rt, Trainer("NO", type="test_gate_record:Model", p_yes=0.01), plan=proposed)
    before = card["default_errors"]
    assert card["gate"] == "rejected" and before > 0
    proposed, train, reasons, d = decide(rt)
    assert proposed is None and not train
    assert reasons == [f"default errors {d.default_errors}, not yet twice the {before} at m_0001"]
    collect(rt, 200, start=150, held_every=999)         # enough new sessions to double them
    proposed, train, reasons, d = decide(rt)
    assert d.default_errors >= 2 * before and train
    assert reasons == [f"default errors doubled since m_0001 ({before} -> {d.default_errors})"]
    assert proposed.data["train_rows"] > card["data"]["stop"]["train"]


def test_the_target_reached_and_the_budget_spent_still_stop_training(tmp_path):
    rt = workspace(tmp_path)
    registry = Registry(rt.root)
    # the other point had no default errors on its test rows (no share): the target is reached where there were
    registry.record({"version": "m_0001", "gate": "accepted", "model_id": "R1", "default_errors": 10,
                     "data": {"stop": {"train": 60}},
                     "metrics": {"stop": {"challenger": {"fixed_share": 0.8}},
                                 "retry": {"challenger": {"fixed_share": None}}}})
    proposed, train, reasons, _ = decide(rt)     # the default errors doubled since it, so there is a plan
    assert proposed is not None and not train
    assert "target reached: m_0001 fixes 80% of the default errors" in reasons
    # with a target it has not reached, the training budget still stops the round
    freeze_template(rt.root, {**ACT_GATE, "target": {"fixed_share": 1.0}, "budget": {"max_trainings": 1}},
                    replace_frozen=True)
    proposed, train, reasons, _ = decide(rt)
    assert proposed is not None and not train and "training budget spent (1 of 1)" in reasons
    # an accepted card from before the gain objective: Brier metrics, no count of default errors
    rt = workspace(tmp_path / "old")
    Registry(rt.root).record({"version": "m_0001", "gate": "accepted", "model_id": "R1",
                              "metrics": {"stop": {"n": 40, "champion_brier": 0.25, "challenger_brier": 0.1}}})
    proposed, _, reasons, _ = decide(rt)
    assert proposed is not None and reasons[0].startswith("m_0001 has no count of default errors")


def test_the_experience_record_pairs_the_plans_prediction_with_the_measured_gain(tmp_path):
    rt = workspace(tmp_path, n=150)
    proposed, train, _, _ = decide(rt)
    assert train
    card = evolve_model(rt, OracleTrainer("R1"), plan=proposed)     # fixes every default error it is tested on
    (record,) = experience.read(rt.root)
    assert card["plan"]["hypothesis"] == proposed.hypothesis and card["train_seconds"] >= 0
    assert record["prediction"] == {"hypothesis": proposed.hypothesis, "change": {}, "predicted_gain": 0.5,
                                    "gain": card["metrics"]["stop"]["challenger"]["fixed_share"],
                                    "verdict": "accepted"}
    assert record["prediction"]["gain"] == 1.0          # it did better than the plan predicted
    # the next round has the wall time and the rows of that job: it can say what another one would cost
    assert decide(rt)[3].minutes_per_row is not None


def test_the_plan_command_prints_the_diagnosis_and_what_is_missing(tmp_path, capsys):
    toml = tmp_path / "ouroloop.toml"
    toml.write_text('[workspace]\nroot = "ws"\n\n[registry]\nbase = "base"\n\n[backends.base]\n'
                    'type = "test_gate_record:Model"\np_yes = 0.5\nmodel = "base"\n\n'
                    '[points]\ndefault_backend = "base"\n\n[points.stop]\nbackend = "champion"\nact = true\n'
                    'explore = 0.0\n')
    rt = config.build_runtime(config.load(toml), tmp_path)
    collect(rt, 60)
    freeze_template(rt.root, ACT_GATE)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert main(["plan", "--config", str(toml)]) == 0
    out = capsys.readouterr().out
    assert "stop: 30 default errors in 54 labelled sessions" in out and "champion: none promoted yet" in out
    assert "plan: recipe {} at stop: no candidate trained yet" in out and "verdict: no training" in out
    assert "no model trained on this data could ever act" in out
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_default_errors_leave_out_points_that_can_never_act(tmp_path):
    rt = workspace(tmp_path)
    assert default_errors(rt, ["stop"]) > 0
    rt.points["stop"].phases = {}               # a contract point: no phase, no default, never acts
    assert default_errors(rt, ["stop"]) == 0


def test_a_prompt_gate_leaves_the_records_ledger_sessions_fresh(tmp_path):
    rt = workspace(tmp_path)
    r1 = open_record(rt)

    def prompt_gate():
        r1.consume("prompt candidate")          # its test sessions are the runs it makes, not the ledger's
        return EvalResult("harness", [1.0] * 12, [0.0] * 12, [f"t{i}" for i in range(12)], lambda: None,
                          sessions=["run_1", "run_2"])

    g, _ = run(r1, prompt_gate)
    assert g.used and g.outcome == "accepted"
    assert Registry(rt.root).seen_sessions() == {"run_1", "run_2"}
    r2 = open_record(rt)
    assert r2.round == "r_0002" and r2.fresh and r2.test_sessions == r1.test_sessions


def test_a_session_answered_by_another_model_than_its_champion_never_acts(tmp_path):
    rt = workspace(tmp_path)
    evolve_model(rt, OracleTrainer("R1"))
    card = Registry(rt.root).champion()
    card["backend"] = {**card["backend"], "model": "R1-retrained"}      # the card's backend now answers as another id
    Registry(rt.root).record(card)
    d = config.build_runtime(CFG, tmp_path).ask("stop", ctx(), session="s_1", phase="per_turn")
    row = rt.ledger.rows()[-1].decision
    assert (d.route, d.acted, d.action) == ("error", False, "no") and "the session runs 'R1'" in row["error"]


def test_spec_and_prompt_promotions_compare_and_swap(tmp_path):
    path = tmp_path / "specs.json"
    a, b = SpecStore(path), SpecStore(path)          # two processes, both loaded before either promoted
    b.set("stop", {"instructions": "b"}, expected=None)
    with pytest.raises(PromotionConflict):
        a.set("stop", {"instructions": "a"}, expected=None)
    a.set("retry", {"instructions": "r"}, expected=None)       # another point: b's promotion stays
    assert (SpecStore(path).get("stop"), SpecStore(path).get("retry")) == ({"instructions": "b"},
                                                                          {"instructions": "r"})
    with pytest.raises(PromotionConflict):                      # a still runs the stop spec it loaded
        a.set("stop", {"instructions": "a"}, expected=a.get("stop"))
    # through the gate: a spec promoted elsewhere while the candidate was evaluated
    rt = workspace(tmp_path / "ws")
    rt.backends["champion"] = Heeds("base")

    def elsewhere(rt_, cand, record):
        SpecStore(rt.root / "harness" / "specs.json").set("stop", rt.points["stop"].spec.to_json(), expected=None)
        return EVALUATORS["spec"](rt_, cand, record)

    res = run_round(rt, Proposes(("spec", "stop", {"instructions": "Is the task done?"})), None, Model(),
                    evaluators={"spec": elsewhere})
    assert [e["gate"] for e in res.evaluated] == ["conflict"]
    x, y = PromptStore(tmp_path), PromptStore(tmp_path)
    y.set(x.get().apply({"system": "y"}))
    with pytest.raises(PromotionConflict):
        x.set(x.get().apply({"system": "x"}))


# ---------------------------------------------------------------- promotion
def test_two_candidates_from_one_parent_cannot_both_be_promoted(tmp_path):
    rt = workspace(tmp_path)
    reg, record = Registry(rt.root), open_record(rt)
    a = prepare_model(rt, OracleTrainer("R1"), record=record, registry=reg)
    b = prepare_model(rt, OracleTrainer("R2", 0.99), record=record, registry=reg)
    assert a.card["parent"] is None and b.card["parent"] is None
    a.card, b.card = accept(rt.root, a.card), accept(rt.root, b.card)     # as if a gate accepted both
    promote_model(rt, a, reg, record.hash)
    with pytest.raises(PromotionConflict):
        promote_model(rt, b, reg, record.hash)
    assert reg.champion()["version"] == a.version
    # Through the gate: another promotion lands while the candidate trains
    accept(rt.root, {"version": "m_0099", "parent": a.version, "points": ["stop"], "model_id": "X",
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
        first = evolve_model(config.build_runtime(CFG, tmp_path), OracleTrainer("R1", knows=SOME))   # elsewhere
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
    trained = trained_on(rt, first["version"])
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
    (record,) = records(rt.root)
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


def test_an_unused_record_gives_way_once_newer_sessions_would_change_its_test_set(tmp_path):
    # The first record tests on the latest sessions; with few sessions that leaves almost nothing to train on, and
    # the round stops before inference. Reusing that record forever would starve every later round: once newer
    # sessions arrive, a new record tests on them, and the old record's never-scored sessions can train.
    rt = workspace(tmp_path, n=40)
    r1 = open_record(rt)
    assert open_record(rt).round == r1.round                        # nothing new: the rerun takes it again
    collect(rt, 40, start=40)
    r2 = open_record(rt)
    newer = {f"s_{i:04d}" for i in range(40, 80)}
    assert r2.round == "r_0002" and r2.fresh and not r1.used
    assert set(r2.test_sessions) <= newer and not set(r2.test_sessions) & set(r1.test_sessions)
