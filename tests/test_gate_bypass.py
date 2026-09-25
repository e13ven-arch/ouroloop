"""The M8 gate against someone who wants a candidate promoted without real evidence: an operator or a research agent
that edits or deletes files, reuses test sessions, relabels sessions, promotes by hand, or keeps a session on a model
that should no longer act."""
import json

import pytest

from ouroloop import config
from ouroloop.evolve.gate import freeze_template, open_record, records
from ouroloop.evolve.pipeline import evolve_model
from ouroloop.evolve.registry import Registry
from ouroloop.evolve.train import TrainResult
from ouroloop.ledger import write_json
from test_gate_record import ACT_GATE, CFG, OracleTrainer, collect, ctx, workspace


class Says(OracleTrainer):
    """Trains nothing: its model says yes with probability p to every question."""

    def __init__(self, model: str, p: float = 0.9):
        super().__init__(model)
        self.p = p

    def train(self, job):
        return TrainResult(f"runs/{job.version}", {"type": "test_gate_record:Model", "p_yes": self.p,
                                                   "model": self.model}, self.model)


# ---------------------------------------------------------------- gate records
def test_deleting_or_editing_the_gate_files_frees_no_session_and_changes_no_record(tmp_path):
    rt = workspace(tmp_path)
    first = evolve_model(rt, OracleTrainer("R1"))
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
    # a promoted champion's calibration rewritten by hand, with a made-up record hash, does not act either
    rt = workspace(tmp_path / "b")
    first = evolve_model(rt, OracleTrainer("R1"))
    assert rt.ask("stop", ctx(), session="s_1", phase="per_turn").acted
    rt.set_calibration("stop", "R1", 1.0, {"per_turn": 0.0}, gate="0123456789abcdef")
    assert not rt.ask("stop", ctx(), session="s_2", phase="per_turn").acted
    # nor can a rolled-back, revoked model be promoted again
    reg = Registry(rt.root)
    assert reg.rollback() is None
    with pytest.raises(ValueError, match="revoked"):
        reg.promote(first["version"], None)
