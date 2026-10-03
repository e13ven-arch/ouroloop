import json
from pathlib import Path

import pytest

from ouroloop.evolve import gate
from ouroloop.evolve.gate import freeze_template
from ouroloop.evolve.registry import Registry
from ouroloop.ledger import write_json

GATE = "0123456789ab"       # stands in for the hash of the gate record that promoted a model (M8)
# A gate a small synthetic ledger can pass: the data checks and the calibration sessions scaled down.
SMALL_GATE = {"checks": {"min_train": 100, "max_mde": 0.2}, "min_cal_sessions": 10}


def accepted(root, record: str, by: str, calibrations: list[dict] = ()) -> None:
    """Stand in for a gate record that accepted a candidate and the calibrations its promotion stores, as gate.run
    leaves it in <workspace>/gates/."""
    write_json(Path(root) / "gates" / f"r_{by}.used.json", {"record": record, "outcome": "accepted", "by": by,
                                                             "calibrations": list(calibrations)})


def accept(root, card: dict) -> dict:
    """File a model card as one the frozen gate accepted, so that Registry.promote takes it."""
    card = {**card, "gate_record": f"accepted-{card['version']}"}
    accepted(root, card["gate_record"], card["version"])
    Registry(root).record(card)
    return card


@pytest.fixture
def gated():
    """Write thresholds as a promotion through the frozen gate would: for one model id, on the point's current
    spec, with the hash of a gate record (a new one each time) that accepted it. A single number applies to every
    phase of the point."""
    records = iter(range(10 ** 6))

    def write(rt, point, model, thresholds, temperature=1.0):
        if not isinstance(thresholds, dict):
            thresholds = {phase: thresholds for phase in rt.points[point].phases}
        n = next(records)
        accepted(rt.root, f"{GATE}{n}", f"fixture{n}", [{"model": model, "spec": rt.points[point].spec_hash,
                                                         "point": point, "temperature": temperature,
                                                         "thresholds": thresholds}])
        rt.set_calibration(point, model, temperature, thresholds, gate=f"{GATE}{n}")
    return write


@pytest.fixture
def frozen(monkeypatch):
    """Freeze the gate template of a workspace, as a person does with `ouroloop gate freeze`: SMALL_GATE, changed by
    `params`. A prompt gate may then run on a few tasks, fewer than a real template allows."""
    monkeypatch.setitem(gate.LIMITS, "suite.min_items", (1, None))

    def freeze(root, **params):
        return freeze_template(root, {**SMALL_GATE, **params})
    return freeze


@pytest.fixture
def loose_risk(monkeypatch):
    """Let a gate template take a risk of 0.2, looser than a real one may (docs/DESIGN.md, gate.LIMITS: 5%): a few
    hundred synthetic sessions then buy a threshold."""
    monkeypatch.setitem(gate.LIMITS, "risk", (0.0, 0.2))


@pytest.fixture(scope="session")
def fake_upstream():
    """The path of the miniature AutomationBench in tests/fake_upstream, importable as `automationbench`.

    A real checkout installed in the same environment must not win: anything already imported under that name is
    dropped first, so the adapter's tests always run against the miniature.
    """
    import sys
    root = str((Path(__file__).parent / "fake_upstream").resolve())
    for name in [m for m in sys.modules if m == "automationbench" or m.startswith("automationbench.")]:
        del sys.modules[name]
    sys.path.insert(0, root)
    try:
        yield root
    finally:
        sys.path.remove(root)
        for name in [m for m in sys.modules if m == "automationbench" or m.startswith("automationbench.")]:
            del sys.modules[name]
