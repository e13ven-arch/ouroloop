"""Offline replay: re-render logged contexts with a (changed) spec and score it against recorded outcomes."""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from typing import Callable

from ..decision import DecisionPoint, Spec
from ..types import Request, brier, temper

_SIGNATURE = re.compile(r"\b[A-Z][A-Za-z]+(?:Error|Exception)\b|command not found|No such file or directory|"
                        r"timed out|rate limit|Permission denied|refused|Unavailable|Could not get lock|is locked|"
                        r"invalid argument|expected one argument|unrecognized arguments")


def signature(view: str) -> str:
    """A coarse error signature: the last exception name or common failure phrase in the view."""
    hits = _SIGNATURE.findall(view)
    return hits[-1] if hits else "other"


def labelled(rt, point: str) -> list[tuple]:
    """(row, target, weight) for decisions of `point` that have a stored context and a trainable outcome."""
    return [(row, t, w) for row, t, w in rt.ledger.training_rows(point) if row.decision.get("ctx")]


def split(rows: list[tuple], test_frac: float = 0.15) -> tuple[list[tuple], list[tuple]]:
    """Dev / test by whole sessions: the latest sessions are held out, so no session straddles the split."""
    first: dict[str, str] = {}
    for row, *_ in rows:
        s, ts = row.decision.get("session", ""), row.decision["ts"]
        first[s] = min(first.get(s, ts), ts)
    sessions = sorted(first, key=first.get)
    k = max(1, round(len(sessions) * test_frac)) if len(sessions) >= 2 else 0
    test_sessions = set(sessions[len(sessions) - k:])
    dev = [r for r in rows if r[0].decision.get("session", "") not in test_sessions]
    test = [r for r in rows if r[0].decision.get("session", "") in test_sessions]
    return dev, test


def splits(rows: list[tuple], test_frac: float = 0.15, cal_frac: float = 0.15) -> dict[str, list[tuple]]:
    """train / calibration / test: test is the latest sessions; the rest is split by a hash of the session id."""
    dev, test = split(rows, test_frac)
    cal = [r for r in dev if int(hashlib.sha1(r[0].decision.get("session", "").encode()).hexdigest(), 16) % 1000
           < cal_frac * 1000]
    cal_ids = {id(r) for r in cal}
    return {"train": [r for r in dev if id(r) not in cal_ids], "calibration": cal, "test": test}


def predict(rt, point: DecisionPoint, spec: Spec, rows: list[tuple], backend=None,
            temperature: float | None = None) -> tuple[list[str], list[dict]]:
    """Re-render each row's context with `spec` and ask `backend` (default: the point's own, with its calibration)."""
    views, requests = [], []
    for row, *_ in rows:
        ctx = rt.sessions.get(row.decision["ctx"])
        view, _ = point.render(ctx, spec)
        views.append(view)
        # Seeded on the decision id: every spec and model sees one row in the same candidate order, so a
        # comparison measures the change and not the permutation.
        q = point.question(ctx, spec).presented(random.Random(row.decision["id"]))
        requests.append(Request(view, q))
    if backend is None:
        backend, temperature = rt.backend_for(point.name), rt.temperature(point.name)
    answers = backend.decide(requests) if requests else []
    return views, [temper(a.probs, temperature or 1.0) for a in answers]


def gold(target: dict) -> str:
    return max(target, key=target.get)


def protected_ids(rt, point: str) -> set[str]:
    path = rt.root / "harness" / "protected.json"
    return set(json.loads(path.read_text(encoding="utf-8")).get(point, [])) if path.exists() else set()


@dataclass
class EvalResult:
    point: str
    champion: list[float]         # per-item Brier of the current champion on the test split
    challenger: list[float]       # per-item Brier of the candidate on the same items
    groups: list[str]             # session of each item, for the grouped bootstrap
    apply: Callable[[], None]     # promotes the candidate if the gate accepts it
    protected_ok: bool = True
    per_point: dict = field(default_factory=dict)   # model candidates: point -> (champion, challenger, groups)
    blocked: list[str] = field(default_factory=list)       # data checks that stopped the candidate
    insufficient: list[str] = field(default_factory=list)  # not enough data yet; keep collecting
    record: Callable[[str, list], None] | None = None      # called with the gate outcome, e.g. to file a model card
    rejected: list[str] = field(default_factory=list)      # rejected before the gate, e.g. a no-op or failed screening
    gate: dict = field(default_factory=dict)               # gate settings of this kind: min_items, regressions, metric
    details: dict = field(default_factory=dict)            # kept in the experience record, e.g. what changed where


def evaluate_spec(rt, cand, test_frac: float = 0.15) -> EvalResult:
    p = rt.points[cand.point]
    champion, challenger = p.spec, p.spec.apply(cand.change)
    rows = labelled(rt, cand.point)
    _, test = split(rows, test_frac)
    _, pc = predict(rt, p, champion, test)
    _, px = predict(rt, p, challenger, test)
    guarded = protected_ids(rt, cand.point)
    protected = [r for r in rows if r[0].decision["id"] in guarded]
    _, pp = predict(rt, p, challenger, protected)
    protected_ok = all(gold(probs) == gold(t) for probs, (_, t, _) in zip(pp, protected))

    def apply() -> None:
        rt.specs.set(cand.point, challenger.to_json(), note=f"{cand.id}: {cand.hypothesis}")
        p.spec = challenger

    return EvalResult(cand.point, [brier(a, t) for a, (_, t, _) in zip(pc, test)],
                      [brier(b, t) for b, (_, t, _) in zip(px, test)],
                      [r[0].decision.get("session", "") for r in test], apply, protected_ok)


EVALUATORS = {"spec": evaluate_spec}
