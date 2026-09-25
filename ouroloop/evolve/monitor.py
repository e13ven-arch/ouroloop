"""The post-act risk monitor (PLAN_v0.2 M9): the realized error rate of the decisions each model acted on, judged by
their outcomes, counted one per session by the point's counting rule. A model whose rate is above the risk budget
with confidence (the Clopper-Pearson lower bound is above it) is revoked: from then on it never acts, and the
sessions pinned to it take their defaults."""
from __future__ import annotations

from ..policy import cp_lower, per_session
from ..release import revoke
from .registry import Registry
from .replay import gold


def acted(rt, point: str, sessions=None) -> dict[str, list[tuple[str, bool]]]:
    """model -> (session, correct) of the labelled decisions it acted on at `point`, in time order (within
    `sessions`, if given). Correct: the action is the outcome's label."""
    out: dict[str, list] = {}
    for row in rt.ledger.rows(point):
        d = row.decision
        target = row.target()
        if d.get("route") != "act" or target is None or (sessions is not None and d.get("session") not in sessions):
            continue
        out.setdefault(d.get("model", ""), []).append((d.get("session", ""), d.get("action") == gold(target[0])))
    return out


def realized(rt, point: str, sessions=None, model: str | None = None) -> tuple[int, int]:
    """(errors, sessions) among the acting decisions at `point` (of `model`, or of any), one per session."""
    errors = n = 0
    for m, items in acted(rt, point, sessions).items():
        if model is None or m == model:
            picked = per_session(items, rt.points[point].count)
            errors, n = errors + sum(not ok for ok in picked.values()), n + len(picked)
    return errors, n


def watch(rt, risk: float, alpha: float = 0.05, sessions=None) -> list[dict]:
    """Revoke every model whose acting decisions (within `sessions`, e.g. the latest ones) have an error rate above
    `risk` with confidence 1 - alpha, at any point. -> what was revoked, also written to the ledger for people."""
    registry = Registry(rt.root)
    versions = {}
    for v in registry.versions():
        if (registry.dir / v / "card.json").exists():
            versions[registry.card(v).get("model_id")] = v
    out = []
    for point in rt.points:
        for model, items in acted(rt, point, sessions).items():
            picked = per_session(items, rt.points[point].count)
            errors, n = sum(not ok for ok in picked.values()), len(picked)
            lower = cp_lower(errors, n, alpha)
            if lower <= risk or rt.revoked.hit(versions.get(model), model):
                continue
            reason = (f"realized error {errors}/{n} at {point}: its {1 - alpha:.0%} lower bound {lower:.3f} is above "
                      f"the budget {risk}")
            revoke(rt.root, versions.get(model), model, reason)
            event = {"type": "monitor", "event": "revoked", "point": point, "model": model,
                     "version": versions.get(model), "errors": errors, "sessions": n, "lower": round(lower, 4),
                     "risk": risk}
            rt.ledger.append(event)
            out.append(event)
    return out
