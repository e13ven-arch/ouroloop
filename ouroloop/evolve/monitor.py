"""The post-act risk monitor (PLAN_v0.2 M9): the realized share of harmed sessions among those each model acted in,
a session harmed when any decision the model acted on in it is wrong by its outcome. A model whose share is above the
risk budget with confidence (the Clopper-Pearson lower bound is above it) is revoked: from then on it never acts, and
the sessions pinned to it take their defaults."""
from __future__ import annotations

from ..policy import cp_lower, harmed
from ..release import revoke
from .registry import Registry
from .replay import right


def acted(rt, point: str, sessions=None) -> dict[str, list[tuple[str, bool]]]:
    """model -> (session, correct) of the labelled decisions it acted on at `point`, in time order (within
    `sessions`, if given). Correct: the action is among the outcomes' right answers (replay.right)."""
    out: dict[str, list] = {}
    for row in rt.ledger.rows(point):
        d = row.decision
        target = row.target()
        if d.get("route") != "act" or target is None or (sessions is not None and d.get("session") not in sessions):
            continue
        out.setdefault(d.get("model", ""), []).append((d.get("session", ""), d.get("action") in right(target[0])))
    return out


def realized(rt, point: str, sessions=None, model: str | None = None) -> tuple[int, int]:
    """(harmed sessions, sessions acted in) at `point`, by `model` or by any (a session per model)."""
    bad = n = 0
    for m, items in acted(rt, point, sessions).items():
        if model is None or m == model:
            by = harmed(items)
            bad, n = bad + sum(by.values()), n + len(by)
    return bad, n


def watch(rt, risk: float, alpha: float = 0.05, sessions=None) -> list[dict]:
    """Revoke every model whose share of harmed sessions (within `sessions`, e.g. the latest ones) is above `risk` with
    confidence 1 - alpha, at any point. -> what was revoked, also written to the ledger for people."""
    registry = Registry(rt.root)
    versions = {}
    for v in registry.versions():
        if (registry.dir / v / "card.json").exists():
            versions[registry.card(v).get("model_id")] = v
    out = []
    for point in rt.points:
        for model, items in acted(rt, point, sessions).items():
            by = harmed(items)
            bad, n = sum(by.values()), len(by)
            lower = cp_lower(bad, n, alpha)
            if lower <= risk or rt.revoked.hit(versions.get(model), model):
                continue
            reason = (f"harmed {bad} of the {n} sessions it acted in at {point}: its {1 - alpha:.0%} lower bound "
                      f"{lower:.3f} is above the budget {risk}")
            revoke(rt.root, versions.get(model), model, reason)
            event = {"type": "monitor", "event": "revoked", "point": point, "model": model,
                     "version": versions.get(model), "harmed": bad, "sessions": n, "lower": round(lower, 4),
                     "risk": risk}
            rt.ledger.append(event)
            out.append(event)
    return out
