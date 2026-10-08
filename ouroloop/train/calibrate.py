"""A temperature fitted by NLL (docs/DESIGN.md invariant 3): the grid search the v1 calibration ran, kept for the
task profile's thresholds (docs/design/V2_TRAINING_PIPELINE.md §3.8)."""
from __future__ import annotations

import math

from .types import nll, temper


def fit_temperature(probs: list[dict], targets: list[dict]) -> float:
    if not probs:
        return 1.0

    def loss(t: float) -> float:
        return sum(nll(temper(p, t), y) for p, y in zip(probs, targets)) / len(probs)

    grid = [math.exp(math.log(0.2) + i * (math.log(10.0) - math.log(0.2)) / 119) for i in range(120)]
    best = min(grid, key=loss)
    fine = [best * math.exp(-0.14 + i * 0.28 / 59) for i in range(60)]
    return min(fine, key=loss)
