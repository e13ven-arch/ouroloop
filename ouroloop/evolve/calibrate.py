"""Per-point calibration (PLAN §4.5): a temperature fitted by NLL on the calibration split, then the lowest
confidence threshold whose automatic decisions keep a Clopper-Pearson error bound below the point's risk."""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..policy import fit_threshold
from ..types import nll, temper
from .replay import gold, labelled, predict, splits


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
    temperature: float
    threshold: float | None
    n: int
    accuracy: float
    coverage: float           # share of calibration decisions the threshold would act on

    def entry(self) -> dict:
        return {"n": self.n, "accuracy": round(self.accuracy, 4), "coverage": round(self.coverage, 4)}


def calibrate_point(rt, point: str, backend=None, test_frac: float = 0.15, cal_frac: float = 0.15,
                    min_items: int = 30) -> PointCalibration | None:
    """Fit on the calibration split. `backend` defaults to the point's current one (without its old temperature)."""
    p = rt.points[point]
    rows = splits(labelled(rt, point), test_frac, cal_frac)["calibration"]
    if len(rows) < min_items:
        return None
    _, probs = predict(rt, p, p.spec, rows, backend=backend or rt.backend_for(point), temperature=1.0)
    targets = [t for _, t, _ in rows]
    t = fit_temperature(probs, targets)
    tempered = [temper(pr, t) for pr in probs]
    correct = [gold(pr) == gold(y) for pr, y in zip(tempered, targets)]
    conf = [max(pr.values()) for pr in tempered]
    threshold = fit_threshold(conf, correct, risk=p.policy.risk)
    coverage = sum(c >= threshold for c in conf) / len(conf) if threshold is not None else 0.0
    return PointCalibration(point, t, threshold, len(rows), sum(correct) / len(rows), coverage)


def apply(rt, cal: PointCalibration, **extra) -> None:
    rt.set_calibration(cal.point, cal.temperature, cal.threshold, **cal.entry(), **extra)
