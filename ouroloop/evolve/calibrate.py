"""Calibration (PLAN §4.5, PLAN_v0.2 M7): a temperature fitted by NLL on the calibration split, then per phase a
confidence threshold at which the share of harmed sessions (any acted decision wrong) among those the model would act
in keeps a Clopper-Pearson bound below the phase's share of the risk, the cut-offs tested in a fixed sequence
(policy.fit_threshold). Both are keyed by the model id the backend reports and by the point's spec hash."""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..policy import fit_threshold
from ..types import nll, temper
from .replay import answers, gold, labelled, not_held_out, right, routing_default, splits


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
    sessions: int = 0         # distinct sessions of the calibration rows

    def entry(self) -> dict:
        return {"n": self.n, "sessions": self.sessions, "accuracy": round(self.accuracy, 4)}

    def gated(self, spec_hash: str) -> dict:
        """What the gate record that promotes it accepts: the runtime acts on a stored calibration only as this."""
        return {"model": self.model, "spec": spec_hash, "point": self.point, "temperature": self.temperature,
                "thresholds": self.thresholds}


def calibrate_point(rt, point: str, backend=None, test_frac: float = 0.15, cal_frac: float = 0.15,
                    min_items: int = 30, risk: float = 0.05, *, rows: list[tuple] | None = None, spec=None,
                    min_sessions: int = 0) -> PointCalibration | None:
    """Fit on the calibration split: `rows`, as a gate record splits them, or else a split by `test_frac` and
    `cal_frac` of the rows outside held-out tasks (for reports). `backend` defaults to the point's current one
    (without its old temperature), `spec` to the point's. A phase's threshold is fitted only on the decisions that
    would act there: a decisive label other than the row's routing default (replay.routing_default), since only
    those change what happens. The point's risk is monitored and gated per session over all its phases, so the k
    phases that would act each get risk / k at level 0.05 / k (a union bound). A row whose phase cannot be told (no
    phase on a point that has several) counts for the temperature only. With fewer than `min_sessions` calibration
    sessions no phase gets a threshold: the point cannot act (PLAN_v0.2 M8)."""
    p = rt.points[point]
    if rows is None:
        rows = splits(not_held_out(rt, labelled(rt, point)), test_frac, cal_frac)["calibration"]
    if len(rows) < min_items:
        return None
    _, answered = answers(rt, p, spec or p.spec, rows, backend or rt.backend_for(point))
    models = {a.model for a in answered}
    if len(models) != 1:
        raise ValueError(f"{point}: the backend reported {len(models)} model ids {sorted(models)}, not one")
    targets = [t for _, t, _ in rows]
    t = fit_temperature([a.probs for a in answered], targets)
    tempered = [temper(a.probs, t) for a in answered]
    n_sessions = len({row.decision.get("session", "") for row, _, _ in rows})
    acting: dict = {phase: ([], [], []) for phase in p.phases}      # phase -> confidences, correct, sessions
    for (row, target, _), a, pr in zip(rows, answered, tempered):
        try:
            phase, default = p.default_for(row.decision.get("phase"))[0], routing_default(rt, p, row)
        except ValueError:
            continue
        if phase in acting and a.decisive and gold(pr) != default:
            conf, correct, sessions = acting[phase]
            conf.append(max(pr.values()))
            correct.append(gold(pr) in right(target))
            sessions.append(row.decision.get("session", ""))
    k = max(1, sum(bool(conf) for conf, _, _ in acting.values()))    # phases that would act share the risk
    thresholds = {phase: fit_threshold(*acting[phase], risk / k, 0.05 / k) if n_sessions >= min_sessions else None
                  for phase in p.phases}
    accuracy = sum(gold(pr) in right(y) for pr, y in zip(tempered, targets)) / len(rows)
    return PointCalibration(point, models.pop(), t, thresholds, len(rows), accuracy, n_sessions)


def apply(rt, cal: PointCalibration, gate: str | None = None, **extra) -> None:
    """Store a calibration. Its thresholds let the point act only with `gate`, the hash of the gate record that
    promoted the model or the spec (M8); until then they are kept for reporting."""
    rt.set_calibration(cal.point, cal.model, cal.temperature, cal.thresholds, gate=gate, **cal.entry(), **extra)
