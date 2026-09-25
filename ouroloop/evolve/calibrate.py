"""Calibration (PLAN §4.5, PLAN_v0.2 M7): a temperature fitted by NLL on the calibration split, then per phase the
lowest confidence threshold whose acting decisions keep a Clopper-Pearson error bound below the risk, counted per
session. Both are keyed by the model id the backend reports and by the point's spec hash."""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..policy import fit_threshold
from ..types import nll, temper
from .replay import answers, gold, labelled, splits


def fit_temperature(probs: list[dict], targets: list[dict]) -> float:
    if not probs:
        return 1.0

    def loss(t: float) -> float:
        return sum(nll(temper(p, t), y) for p, y in zip(probs, targets)) / len(probs)

    grid = [math.exp(math.log(0.2) + i * (math.log(10.0) - math.log(0.2)) / 119) for i in range(120)]
    best = min(grid, key=loss)
    fine = [best * math.exp(-0.14 + i * 0.28 / 59) for i in range(60)]
    return min(fine, key=loss)


@dataclass
class PointCalibration:
    point: str
    model: str                # the model id the backend reported: its answers are the only ones this applies to
    temperature: float
    thresholds: dict          # phase -> the lowest confidence that may act there, or None
    n: int
    accuracy: float

    def entry(self) -> dict:
        return {"n": self.n, "accuracy": round(self.accuracy, 4)}


def calibrate_point(rt, point: str, backend=None, test_frac: float = 0.15, cal_frac: float = 0.15,
                    min_items: int = 30, risk: float = 0.05) -> PointCalibration | None:
    """Fit on the calibration split. `backend` defaults to the point's current one (without its old temperature).
    A phase's threshold is fitted only on the decisions that would act there: a decisive label other than the
    phase's default, since only those change what happens. A row whose phase cannot be told (no phase on a point
    that has several) counts for the temperature only."""
    p = rt.points[point]
    rows = splits(labelled(rt, point), test_frac, cal_frac)["calibration"]
    if len(rows) < min_items:
        return None
    _, answered = answers(rt, p, p.spec, rows, backend or rt.backend_for(point))
    models = {a.model for a in answered}
    if len(models) != 1:
        raise ValueError(f"{point}: the backend reported {len(models)} model ids {sorted(models)}, not one")
    targets = [t for _, t, _ in rows]
    t = fit_temperature([a.probs for a in answered], targets)
    tempered = [temper(a.probs, t) for a in answered]
    thresholds = {}
    for phase in p.phases:
        default = p.default_for(phase)[1]
        conf, correct, sessions = [], [], []
        for (row, target, _), a, pr in zip(rows, answered, tempered):
            try:
                in_phase = p.default_for(row.decision.get("phase"))[0] == phase
            except ValueError:
                continue
            if in_phase and a.decisive and gold(pr) != default:
                conf.append(max(pr.values()))
                correct.append(gold(pr) == gold(target))
                sessions.append(row.decision.get("session", ""))
        thresholds[phase] = fit_threshold(conf, correct, sessions, risk)
    accuracy = sum(gold(pr) == gold(y) for pr, y in zip(tempered, targets)) / len(rows)
    return PointCalibration(point, models.pop(), t, thresholds, len(rows), accuracy)


def apply(rt, cal: PointCalibration, gate: str | None = None, **extra) -> None:
    """Store a calibration. Its thresholds let the point act only with `gate`, the hash of the gate record that
    promoted the model (M8); until then they are kept for reporting."""
    rt.set_calibration(cal.point, cal.model, cal.temperature, cal.thresholds, gate=gate, **cal.entry(), **extra)
