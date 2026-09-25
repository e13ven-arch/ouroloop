"""Offline replay: re-render logged contexts with a (changed) spec and score it against recorded outcomes."""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from typing import Callable

from ..decision import DecisionPoint, Spec
from ..types import Answer, Request, brier, temper

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


def in_calibration(session: str, cal_frac: float) -> bool:
    """Whether a session goes to the calibration split: by a hash of its id, so it always goes the same way."""
    return int(hashlib.sha1(session.encode()).hexdigest(), 16) % 1000 < cal_frac * 1000


def splits(rows: list[tuple], test_frac: float = 0.15, cal_frac: float = 0.15) -> dict[str, list[tuple]]:
    """train / calibration / test: test is the latest sessions; the rest is split by a hash of the session id.
    Outside a gate only (reports, tests): a gate's test sessions come from its record (gate.GateRecord.split)."""
    dev, test = split(rows, test_frac)
    cal = [r for r in dev if in_calibration(r[0].decision.get("session", ""), cal_frac)]
    cal_ids = {id(r) for r in cal}
    return {"train": [r for r in dev if id(r) not in cal_ids], "calibration": cal, "test": test}


def not_held_out(rt, rows: list[tuple]) -> list[tuple]:
    """The rows outside held-out tasks' sessions: those never enter training or calibration (PLAN_v0.2 M8)."""
    sessions, _ = rt.ledger.sessions()
    return [r for r in rows if not sessions.get(r[0].decision.get("session", ""), {}).get("held_out")]


def answers(rt, point: DecisionPoint, spec: Spec, rows: list[tuple], backend) -> tuple[list[str], list[Answer]]:
    """Re-render each row's context with `spec` and ask `backend`, untempered."""
    views, requests = [], []
    for row, *_ in rows:
        ctx = rt.sessions.get(row.decision["ctx"])
        view, _ = point.render(ctx, spec)
        views.append(view)
        # Seeded on the decision id: every spec and model sees one row in the same candidate order, so a
        # comparison measures the change and not the permutation.
        q = point.question(ctx, spec).presented(random.Random(row.decision["id"]))
        requests.append(Request(view, q))
    return views, backend.decide(requests) if requests else []


def predict(rt, point: DecisionPoint, spec: Spec, rows: list[tuple], backend=None,
            temperature: float | None = None) -> tuple[list[str], list[dict]]:
    """Re-render each row's context with `spec` and ask `backend`. By default the point's own backend, tempered with
    the calibration of the model it reports on this spec; a changed spec without one of its own replays at the current
    spec's temperature, so a comparison with the champion measures the change and not the temperature."""
    views, answered = answers(rt, point, spec, rows, backend or rt.backend_for(point.name))
    if backend is None:
        h = point.hash_of(spec)

        def temperature(model: str) -> float:
            cal = rt.calibration.get(model, h, point.name) or rt.calibration.get(model, point.spec_hash, point.name)
            return cal.get("temperature", 1.0)
        return views, [temper(a.probs, temperature(a.model)) for a in answered]
    return views, [temper(a.probs, temperature or 1.0) for a in answered]


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
    per_point: dict = field(default_factory=dict)   # model and spec candidates: point -> (champion, challenger, groups)
    blocked: list[str] = field(default_factory=list)       # data checks that stopped the candidate
    insufficient: list[str] = field(default_factory=list)  # not enough data yet; keep collecting
    record: Callable[[str, list], None] | None = None      # called with the gate outcome, e.g. to file a model card
    rejected: list[str] = field(default_factory=list)      # rejected before the gate, e.g. a no-op or failed screening
    gate: dict = field(default_factory=dict)               # settings of this kind's gate: regressions, metric
    details: dict = field(default_factory=dict)            # kept in the experience record, e.g. what changed where
    inconclusive: list[str] = field(default_factory=list)  # an infrastructure failure: no evidence either way
    sessions: list[str] = field(default_factory=list)      # new sessions the evaluation ran as its test set (prompts)


def scored(rt, point: DecisionPoint, champion: Spec, challenger: Spec, rows: list[tuple], backend=None,
           temperature: float | None = None) -> tuple[list[float], list[float], list[str]]:
    """(champion Brier, challenger Brier, session) per row: the champion is the point's backend on `champion`, the
    challenger `backend` (at `temperature`) if given, else the point's backend on `challenger`."""
    _, pc = predict(rt, point, champion, rows)
    _, px = predict(rt, point, challenger, rows, backend=backend, temperature=temperature)
    return ([brier(pr, t) for pr, (_, t, _) in zip(pc, rows)], [brier(pr, t) for pr, (_, t, _) in zip(px, rows)],
            [r.decision.get("session", "") for r, _, _ in rows])


def summarize(per_point: dict) -> dict:
    return {name: {"n": len(a), "sessions": len(set(g)), "champion_brier": sum(a) / len(a) if a else None,
                   "challenger_brier": sum(b) / len(b) if b else None} for name, (a, b, g) in per_point.items()}


def evaluate_spec(rt, cand, record) -> EvalResult:
    """A spec candidate on the gate record's test sessions: the point's model on the champion spec and on the
    candidate's. Held-out tasks' sessions are scored beside them, for the record only. Promoted, the spec comes with a
    calibration of that model on it, fitted on the calibration sessions and stored with the record's hash."""
    from .calibrate import apply as store, calibrate_point
    p = rt.points[cand.point]
    champion, challenger = p.spec, p.spec.apply(cand.change)
    rows = labelled(rt, cand.point)
    parts = record.split(rows, rt.ledger.sessions()[0], record.cal_frac(cand.point))
    params = record.params
    cal = calibrate_point(rt, cand.point, rows=parts["calibration"], spec=challenger, risk=params["risk"],
                          min_sessions=params["min_cal_sessions"])
    record.consume(cand.id)             # the first inference request on the test sessions uses the record up
    test, held = (scored(rt, p, champion, challenger, parts[k]) for k in ("test", "held_out"))
    guarded = protected_ids(rt, cand.point)
    protected = [r for r in rows if r[0].decision["id"] in guarded]
    _, pp = predict(rt, p, challenger, protected)
    protected_ok = all(gold(probs) == gold(t) for probs, (_, t, _) in zip(pp, protected))

    def apply() -> None:
        rt.specs.set(cand.point, challenger.to_json(), note=f"{cand.id}: {cand.hypothesis}")
        p.spec = challenger
        if cal is not None:
            store(rt, cal, gate=record.hash)

    return EvalResult(cand.point, *test, apply, protected_ok, per_point={cand.point: test},
                      details={"held_out": summarize({cand.point: held})[cand.point]})


EVALUATORS = {"spec": evaluate_spec}
