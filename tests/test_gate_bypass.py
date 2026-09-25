"""The M8 gate against someone who wants a candidate promoted without real evidence: an operator or a research agent
that edits or deletes files, reuses test sessions, relabels sessions, promotes by hand, or keeps a session on a model
that should no longer act."""
import json

import pytest

from ouroloop.evolve.gate import freeze_template, open_record, records
from ouroloop.evolve.pipeline import evolve_model
from ouroloop.evolve.registry import Registry
from test_gate_record import ACT_GATE, OracleTrainer, collect, workspace


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
