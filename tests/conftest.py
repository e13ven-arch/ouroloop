import pytest

GATE = "0123456789ab"       # stands in for the hash of the gate record that promoted a model (M8)


@pytest.fixture
def gated():
    """Write thresholds as a promotion through the frozen gate would: for one model id, on the point's current
    spec. A single number applies to every phase of the point."""
    def write(rt, point, model, thresholds, temperature=1.0):
        if not isinstance(thresholds, dict):
            thresholds = {phase: thresholds for phase in rt.points[point].phases}
        rt.set_calibration(point, model, temperature, thresholds, gate=GATE)
    return write
