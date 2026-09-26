"""The M8 gate against someone who wants a candidate promoted without real evidence: an operator or a research agent
that edits or deletes files, reuses test sessions, relabels sessions, promotes by hand, or keeps a session on a model
that should no longer act."""
import json

import pytest

from ouroloop import config
from ouroloop.evolve import experience
from ouroloop.evolve import gate
from ouroloop.evolve.gate import freeze_template, load_template, open_record, records, template_path
from ouroloop.evolve.pipeline import evolve_model, prepare_model, recipe_evaluator
from ouroloop.evolve.registry import Registry
from ouroloop.evolve.train import SignatureTrainer, TrainResult
from ouroloop.ledger import PromotionConflict, write_json
from ouroloop.research import run_round
from test_gate_record import (ACT_GATE, CFG, M6_CFG, SOME, Model, OracleTrainer, Proposes, collect, ctx, held_out,
                              m6_sessions, trained_on, workspace)

pytestmark = pytest.mark.usefixtures("loose_risk")


class Says(OracleTrainer):
    """Trains nothing: its model says yes with probability p to every question."""

    def __init__(self, model: str, p: float = 0.9):
        super().__init__(model)
        self.p = p

    def train(self, job):
        return TrainResult(self.model, {"type": "test_gate_record:Model", "p_yes": self.p, "model": self.model})


# ---------------------------------------------------------------- gate records
def test_deleting_or_editing_the_gate_files_frees_no_session_and_changes_no_record(tmp_path):
    rt = workspace(tmp_path)
    first = evolve_model(rt, OracleTrainer("R1", knows=SOME))
    (r1,) = records(rt.root)
    assert first["gate"] == "accepted"
    for p in (rt.root / "gates").glob("r_0001*"):
        p.unlink()                                  # the record and the mark of its use, deleted
    again = evolve_model(rt, OracleTrainer("R2", 0.99))
    assert again["gate"] == "validated" and Registry(rt.root).champion()["version"] == first["version"]
    assert Registry(rt.root).seen_sessions() >= set(r1.test_sessions)
    # an unused record edited by hand: its parameters and test sessions stay as they were frozen
    collect(rt, 40, start=150)
    record = open_record(rt)
    path = rt.root / "gates" / f"{record.round}.json"
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, "params": {**data["params"], "min_sessions": 2},
                                "test_sessions": data["test_sessions"][:2]}))
    again = open_record(rt)
    assert again.round == record.round and again.params["min_sessions"] == 30
    assert again.test_sessions == data["test_sessions"]


def test_only_the_latest_record_frozen_from_the_current_template_can_decide(tmp_path):
    rt = workspace(tmp_path)
    r1 = open_record(rt)                            # frozen, never used
    freeze_template(rt.root, {**ACT_GATE, "n_boot": 3000}, replace_frozen=True)
    r2 = open_record(rt)                            # a new template: a new record, on the same unused sessions
    assert r2.round == "r_0002" and r2.test_sessions == r1.test_sessions
    with pytest.raises(ValueError, match="superseded"):
        evolve_model(rt, OracleTrainer("R1"), record=r1)       # else r2 would score those sessions a second time
    assert not records(rt.root)[0].used
    assert evolve_model(rt, OracleTrainer("R1"), record=r2)["gate"] == "accepted"
    collect(rt, 40, start=150)
    r3 = open_record(rt)
    freeze_template(rt.root, {**ACT_GATE, "n_boot": 4000}, replace_frozen=True)
    with pytest.raises(ValueError, match="template"):
        evolve_model(rt, OracleTrainer("R2", 0.99), record=r3)
    # replaced while a candidate trains: its record stops before the first test inference, and stays unused
    collect(rt, 40, start=190)
    current = open_record(rt)

    class Refreezes(OracleTrainer):
        def train(self, job):
            freeze_template(rt.root, {**ACT_GATE, "n_boot": 5000}, replace_frozen=True)
            open_record(rt)
            return super().train(job)

    with pytest.raises(ValueError, match="superseded"):
        evolve_model(rt, Refreezes("R2", 0.99), record=current)
    assert not records(rt.root)[-2].used and records(rt.root)[-2].round == current.round


# ---------------------------------------------------------------- promotions
def test_nothing_the_gate_did_not_accept_answers_or_acts(tmp_path):
    rt = workspace(tmp_path)
    bad = evolve_model(rt, Says("BAD"))
    assert bad["gate"] == "rejected"
    reg = Registry(rt.root)
    with pytest.raises(ValueError, match="not accepted"):
        reg.promote(bad["version"], None)
    # by hand: the champion pointer, and a calibration stored with the rejecting record's hash
    write_json(rt.root / "models" / "champion.json", {"version": bad["version"], "history": [
        {"version": bad["version"], "parent": None}]})
    rt.set_calibration("stop", "BAD", 1.0, {"per_turn": 0.5, "on_finish": 0.5}, gate=bad["gate_record"])
    d = config.build_runtime(CFG, tmp_path).ask("stop", ctx("no"), session="s_bad", phase="per_turn")
    assert (d.route, d.acted, d.action, d.model) == ("error", False, "no", "")
    assert "not promoted through the frozen gate" in rt.ledger.rows()[-1].decision["error"]
    # a promoted champion's calibration rewritten by hand does not act: with a made-up record hash, nor with the
    # hash of the record that promoted it and a lower threshold or a sharper temperature than that record accepted
    rt = workspace(tmp_path / "b")
    first = evolve_model(rt, OracleTrainer("R1"))
    cal = rt.calibration_for("stop", "R1")
    assert rt.threshold("stop", "R1", "per_turn") == cal["thresholds"]["per_turn"] > 0
    assert rt.ask("stop", ctx(), session="s_1", phase="per_turn").acted
    for temperature, thresholds, gate_hash in [(cal["temperature"], {"per_turn": 0.0}, "0123456789ab"),
                                               (cal["temperature"], {"per_turn": 0.0}, cal["gate"]),
                                               (0.01, cal["thresholds"], cal["gate"])]:
        rt.set_calibration("stop", "R1", temperature, thresholds, gate=gate_hash)
        assert rt.threshold("stop", "R1", "per_turn") is None
    assert not rt.ask("stop", ctx(), session="s_2", phase="per_turn").acted
    rt.set_calibration("stop", "R1", cal["temperature"], cal["thresholds"], gate=cal["gate"])     # as promoted
    assert rt.ask("stop", ctx(), session="s_3", phase="per_turn").acted
    # nor can a rolled-back model be promoted again
    reg = Registry(rt.root)
    assert reg.rollback() is None
    with pytest.raises(ValueError, match="rolled back"):
        reg.promote(first["version"], None)


def test_a_verdict_promotes_the_step_it_judged_and_nothing_else(tmp_path):
    rt = workspace(tmp_path)
    first = evolve_model(rt, OracleTrainer("R1", knows=SOME))
    collect(rt, 40, start=150)
    second = evolve_model(rt, OracleTrainer("R2", 0.99))
    reg = Registry(rt.root)
    assert (second["gate"], second["parent"], reg.champion()["version"]) == ("accepted", first["version"],
                                                                            second["version"])
    with pytest.raises(PromotionConflict, match="built on"):
        reg.promote(first["version"], second["version"])      # R1 over R2: no gate judged that step
    assert reg.champion()["version"] == second["version"]


def test_a_rollback_holds_without_the_revocation_list(tmp_path):
    rt = workspace(tmp_path)
    first = evolve_model(rt, OracleTrainer("R1", knows=SOME))
    collect(rt, 40, start=150)
    second = evolve_model(rt, OracleTrainer("R2", 0.99))
    rt.session("s_r2", {"task": "t"})
    assert rt.ask("stop", ctx(), session="s_r2", phase="per_turn").acted
    reg = Registry(rt.root)
    assert reg.rollback() == first["version"]
    (rt.root / "harness" / "revoked.json").unlink()        # the revocation list, deleted
    d = rt.ask("stop", ctx(), session="s_r2", phase="per_turn")
    assert (d.model, d.acted) == ("R2", False)
    with pytest.raises(ValueError, match="rolled back"):
        reg.promote(second["version"], first["version"])
    rt.session("s_r1", {"task": "t"})
    assert rt.ask("stop", ctx(), session="s_r1", phase="per_turn").acted      # the previous champion still acts


# ---------------------------------------------------------------- sessions and data
def test_a_held_out_session_or_task_stays_held_out(tmp_path):
    rt = config.build_runtime(M6_CFG, tmp_path)
    m6_sessions(rt, 300)
    freeze_template(rt.root, {"checks": {"min_train": 100, "max_mde": 0.2}})
    sessions, _ = rt.ledger.sessions()
    held = held_out(rt)
    for s in held:                                  # relabelled by a later session event
        rt.ledger.append({"type": "session", "session": s, "task": sessions[s]["task"], "held_out": False})
    for i in range(20):                             # a held-out task run with the flag off
        s = f"s_again_{i}"
        rt.ledger.append({"type": "session", "session": s, "task": "t0", "held_out": False})
        rt.outcome(rt.record("stop", {**ctx(), "moment": "after a turn"}, session=s, phase="per_turn").id, "yes")
    card = evolve_model(rt, SignatureTrainer())
    again = {f"s_again_{i}" for i in range(20)}
    assert held_out(rt) == held | again and not trained_on(rt, card["version"]) & (held | again)


def test_a_session_opened_again_keeps_its_release(tmp_path):
    rt = workspace(tmp_path)
    assert rt.session("s_1", {"task": "t"}).model is None
    evolve_model(config.build_runtime(CFG, tmp_path), OracleTrainer("R1"))
    assert rt.session("s_1", {"task": "t"}).model is None
    assert config.build_runtime(CFG, tmp_path).session("s_1", {"task": "t"}).model is None     # another process
    assert rt.ask("stop", ctx(), session="s_1", phase="per_turn").model == "base"
    assert rt.session("s_2", {"task": "t"}).model == "R1"


def test_a_session_id_cannot_reach_outside_the_session_store(tmp_path):
    rt = workspace(tmp_path)
    for bad in ("../ledger/2099-01-01", "../models/champion", "/tmp/x", ".hidden", ""):
        with pytest.raises(ValueError, match="session id"):
            rt.ask("stop", ctx(), session=bad, phase="per_turn")
        with pytest.raises(ValueError, match="session id"):
            rt.session(bad, {"task": "t"})
    assert not (rt.root / "ledger" / "2099-01-01.jsonl").exists()
    rt.ledger.rows()                                # the ledger still reads
    assert rt.ask("stop", ctx(), session="0b5e-4c1f_x.y@host:1", phase="per_turn").route != "error"


def test_a_recipe_changes_the_training_rows_only(tmp_path):
    rt = config.build_runtime(CFG, tmp_path)
    for i in range(150):                            # every third session labelled with half weight
        s, label = f"s_{i:04d}", "yes" if i % 2 else "no"
        rt.ledger.append({"type": "session", "session": s, "task": f"t{i % 7}", "held_out": False})
        d = rt.record("stop", {**ctx(label), "moment": "after a turn"}, session=s, phase="per_turn")
        rt.outcome(d.id, label, weight=0.5 if i % 3 == 0 else 1.0)
    freeze_template(rt.root, ACT_GATE)
    record = open_record(rt)

    class Weighs(OracleTrainer):
        supports_weights = True

    usual = prepare_model(rt, Weighs("R1"), record=record)
    picky = prepare_model(rt, Weighs("R2"), {"min_weight": 1.0}, record=record)
    assert picky.per_point["stop"][2] == usual.per_point["stop"][2]           # the same test items
    assert picky.calibrations["stop"].n == usual.calibrations["stop"].n       # and calibration rows
    assert picky.card["data"]["stop"]["train"] < usual.card["data"]["stop"]["train"]
    # a trainer that does not weigh examples never takes a half-weight label for a full one
    unweighted = prepare_model(rt, OracleTrainer("R3"), record=record)
    assert unweighted.card["data"]["stop"] == picky.card["data"]["stop"]


def test_a_research_candidate_cannot_point_training_at_other_data(tmp_path):
    rt = workspace(tmp_path)
    got = []

    class Records(OracleTrainer):
        def train(self, job):
            got.append(job)
            return super().train(job)

    evaluators = {"recipe": recipe_evaluator(Records("R1"), {"replay": "data/general/train.jsonl"})}
    agent = Proposes(("recipe", "stop", {"replay": "ouroloop_jobs/m_0001/data/calibration.jsonl"}),
                     ("recipe", "stop", {"init_from": "runs/trained_on_everything"}))
    res = run_round(rt, agent, None, Model(), evaluators=evaluators)
    assert [(e["gate"], e["used"]) for e in res.evaluated] == [("failed", False), ("failed", False)] and not got
    assert all("a person" in e["reasons"][0] for e in res.evaluated)


def test_the_trainer_never_gets_the_test_or_the_calibration_sessions(tmp_path):
    rt = workspace(tmp_path)
    got = {}

    class Looks(OracleTrainer):
        def train(self, job):
            got["files"] = sorted(p.name for p in job.data_dir.iterdir())
            got["sessions"] = {json.loads(line)["source_id"] for p in job.data_dir.iterdir() for line in p.open()}
            return super().train(job)

    evolve_model(rt, Looks("R1"))
    (record,) = records(rt.root)
    parts = record.split([(r, None, 1.0) for r in rt.ledger.rows("stop")], rt.ledger.sessions()[0],
                         record.cal_frac("stop"))
    sessions = {name: {r.decision["session"] for r, _, _ in rows} for name, rows in parts.items()}
    # val: training sessions the trainer may select its checkpoint on; never the calibration split, which the
    # thresholds are fitted on, nor the test sessions
    assert got["files"] == ["train.jsonl", "val.jsonl"] and got["sessions"] <= sessions["train"]
    assert sessions["calibration"] and sessions["test"] and not got["sessions"] & (sessions["calibration"] |
                                                                                   sessions["test"])


def test_the_research_agent_never_sees_held_out_results(tmp_path):
    rt = workspace(tmp_path)
    experience.append(rt.root, {"kind": "prompt", "gate": "rejected", "details": {
        "effects": {"a": 0.5}, "held_out_effects": {"h": -0.5}, "held_out_delta": -0.5, "held_out_n": 2}})
    experience.append(rt.root, {"kind": "spec", "gate": "rejected", "details": {"held_out": {"n": 3}}})
    seen = []

    class Reads:
        def step(self, ws):
            seen.extend(ws.experience)
            return []

    run_round(rt, Reads(), None, Model())
    assert [e["details"] for e in seen] == [{"effects": {"a": 0.5}}, {}]


# ---------------------------------------------------------------- the template
def test_no_template_loosens_the_risk_or_the_evidence_a_gate_needs(tmp_path, monkeypatch):
    monkeypatch.setitem(gate.LIMITS, "risk", (0.0, 0.05))          # as shipped: PLAN_v0.2 §0's 5%
    root = tmp_path / "ws"
    for loose in ({"risk": 0.2}, {"min_sessions": 5}, {"n_boot": 10}, {"risk": True}, {"suite": {"min_items": 2}}):
        with pytest.raises(ValueError, match="looser"):
            freeze_template(root, loose)
    assert not template_path(root).exists()
    freeze_template(root, {"risk": 0.01, "min_sessions": 40})      # tighter is a person's choice
    path = template_path(root)
    path.write_text(json.dumps({**json.loads(path.read_text()), "risk": 0.5}))        # by hand
    with pytest.raises(ValueError, match="looser"):
        load_template(root)


def test_the_calibration_share_never_grows_onto_sessions_a_model_trained_on(tmp_path):
    rt = workspace(tmp_path)                        # stop's calibration share: 0.4
    evolve_model(rt, OracleTrainer("R1"))
    with pytest.raises(ValueError, match="cal_frac"):
        freeze_template(rt.root, {**ACT_GATE, "cal_frac": {"stop": 0.6, "*": 0.15}}, replace_frozen=True)
    path = template_path(rt.root)
    path.write_text(json.dumps({**json.loads(path.read_text()), "cal_frac": {"*": 0.5}}))    # by hand
    collect(rt, 40, start=150)
    with pytest.raises(ValueError, match="cal_frac"):
        open_record(rt)
    freeze_template(rt.root, {**ACT_GATE, "cal_frac": {"stop": 0.3, "*": 0.1}}, replace_frozen=True)
    assert open_record(rt).cal_frac("stop") == 0.3
