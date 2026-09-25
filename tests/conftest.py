import pytest

from ouroloop.evolve.gate import freeze_template

GATE = "0123456789ab"       # stands in for the hash of the gate record that promoted a model (M8)
# A gate a small synthetic ledger can pass: the data checks and the calibration sessions scaled down.
SMALL_GATE = {"checks": {"min_train": 100, "max_mde": 0.2}, "min_cal_sessions": 10}


@pytest.fixture
def gated():
    """Write thresholds as a promotion through the frozen gate would: for one model id, on the point's current
    spec. A single number applies to every phase of the point."""
    def write(rt, point, model, thresholds, temperature=1.0):
        if not isinstance(thresholds, dict):
            thresholds = {phase: thresholds for phase in rt.points[point].phases}
        rt.set_calibration(point, model, temperature, thresholds, gate=GATE)
    return write


@pytest.fixture
def frozen():
    """Freeze the gate template of a workspace, as a person does with `ouroloop gate freeze`: SMALL_GATE, changed by
    `params`."""
    def freeze(root, **params):
        return freeze_template(root, {**SMALL_GATE, **params})
    return freeze
