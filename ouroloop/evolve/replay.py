"""Offline replay: re-render logged contexts with a (changed) spec and score it against recorded outcomes.

A side (the champion, or a candidate) is scored by its gain over the routing default on each row: +1 where it would
act and be right, -1 where it would act and be wrong, 0 where it would take the default. An answer is right when it is
among the target's right answers (right). Rows where the default is already right can only lose; the default errors
(rows whose routing default is not among the right answers) are the only rows a model can add anything on."""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from typing import Callable

from ..decision import DecisionPoint, Spec
from ..policy import Policy, route
from ..runtime import import_object
from ..types import Answer, Request, temper

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


def predict(rt, point: DecisionPoint, spec: Spec, rows: list[tuple]) -> tuple[list[str], list[dict]]:
    """Re-render each row's context with `spec` and ask the point's own backend, tempered with the calibration of the
    model it reports on this spec; a changed spec without one of its own replays at the current spec's temperature,
    so a comparison with the champion measures the change and not the temperature."""
    views, answered = answers(rt, point, spec, rows, rt.backend_for(point.name))
    h = point.hash_of(spec)

    def temperature(model: str) -> float:
        cal = rt.calibration.get(model, h, point.name) or rt.calibration.get(model, point.spec_hash, point.name)
        return cal.get("temperature", 1.0)
    return views, [temper(a.probs, temperature(a.model)) for a in answered]


def gold(target: dict) -> str:
    """The label of the largest weight, the first on a tie: a model's answer, or one label of a target where a
    statistic needs one. Whether an answer is right is for `right` to say."""
    return max(target, key=target.get)


def right(target: dict) -> set[str]:
    """The right answers of a target: every label of its largest weight, ties included (an outcome may name several
    labels that are all right). Every check of correctness uses it."""
    top = max(target.values())
    return {lab for lab, v in target.items() if top - v < 1e-9}


def protected_ids(rt, point: str) -> set[str]:
    path = rt.root / "harness" / "protected.json"
    return set(json.loads(path.read_text(encoding="utf-8")).get(point, [])) if path.exists() else set()


def routing_default(rt, point: DecisionPoint | str, row, ctx=None) -> str | None:
    """What the execution rule compares the model's answer with on a logged decision: for a phase whose default
    answerer is a rule, the rule's answer on the stored context (`ctx`, if already loaded), or the phase's fixed label
    when the rule fails, as in the runtime; a rule is deterministic, so rows logged before it became the routing
    default read the same. Otherwise the phase's fixed label (an LLM answerer is never asked before routing). Raises
    ValueError when the row's phase cannot be told (no phase on a point that has several)."""
    p = rt.points[point] if isinstance(point, str) else point
    _, label, answerer = p.default_for(row.decision.get("phase"))
    if not (answerer or "").startswith("rule:"):
        return label
    ctx = rt.sessions.get(row.decision["ctx"]) if ctx is None else ctx
    try:
        out = import_object(answerer.partition(":")[2])(ctx)
        return out if out in p.question(ctx).labels() else label
    except Exception:  # noqa: BLE001 - the fixed label is the default of last resort, as in the runtime
        return label


_ACTING = Policy(act=True, explore=0.0)


def gain(probs: dict, target: dict, default: str | None, threshold: float | None) -> int:
    """-1 if the execution rule would act on this answer (at `threshold`, against the routing default) and it is not
    among the right answers; +1 if it would act, is among them and the default is not; else 0 (the default taken, or
    swapped for another right answer)."""
    if route(_ACTING, Answer(probs), default, threshold, random.Random(0)).route != "act":
        return 0
    if gold(probs) not in right(target):
        return -1
    return 0 if default in right(target) else 1


@dataclass
class EvalResult:
    point: str
    champion: list[float]         # per-item loss of the current champion on the test split (-gain, or a failure)
    challenger: list[float]       # per-item loss of the candidate on the same items
    groups: list[str]             # session of each item, for the grouped bootstrap
    apply: Callable[[], None]     # promotes the candidate if the gate accepts it
    protected_ok: bool = True
    per_point: dict = field(default_factory=dict)   # model and spec candidates: point -> scored(...)
    blocked: list[str] = field(default_factory=list)       # data checks that stopped the candidate
    insufficient: list[str] = field(default_factory=list)  # not enough data yet; keep collecting
    record: Callable[[str, list], None] | None = None      # called with the gate outcome, e.g. to file a model card
    rejected: list[str] = field(default_factory=list)      # rejected before the gate, e.g. a no-op or failed screening
    gate: dict = field(default_factory=dict)               # settings of this kind's gate: regressions, metric
    details: dict = field(default_factory=dict)            # kept in the experience record, e.g. what changed where
    inconclusive: list[str] = field(default_factory=list)  # an infrastructure failure: no evidence either way
    sessions: list[str] = field(default_factory=list)      # new sessions the evaluation ran as its test set (prompts)
    calibrations: list[dict] = field(default_factory=list)  # what a promotion stores: model, spec, point, values


def scored(rt, point: DecisionPoint, rows: list[tuple], challenger: Spec, cal=None,
           backend=None) -> tuple[list[int], list[int], list[str], list[bool]]:
    """(champion loss, challenger loss, session, default error) per row, a loss being -gain. The champion is the
    point's backend now on its current spec, at the temperature and thresholds the runtime would use for it: a
    gated calibration, so a base, ungated or revoked model never acts. The challenger is `backend` (default: the
    point's) on `challenger`, at the temperature and thresholds of `cal`, its fresh calibration (None: it never
    acts). A row whose phase cannot be told never acts and is no default error."""
    ctxs = [rt.sessions.get(row.decision["ctx"]) for row, _, _ in rows]
    phases, defaults = [], []
    for (row, _, _), ctx in zip(rows, ctxs):
        try:
            phases.append(point.default_for(row.decision.get("phase"))[0])
            defaults.append(routing_default(rt, point, row, ctx))
        except ValueError:
            phases.append(None)
            defaults.append(None)
    gated: dict = {}

    def champion(model: str) -> tuple[float, dict]:
        if model not in gated:
            revoked = rt.revoked.hit(model)
            gated[model] = (rt.temperature(point, model),
                            {ph: None if revoked else rt.threshold(point, model, ph) for ph in point.phases})
        return gated[model]

    def losses(answered: list[Answer], calibration) -> list[int]:
        """-gain of each answer, at calibration(its model) -> (temperature, thresholds by phase)."""
        out = []
        for a, (_, target, _), phase, default in zip(answered, rows, phases, defaults):
            t, thresholds = calibration(a.model)
            out.append(-gain(temper(a.probs, t), target, default, thresholds.get(phase)))
        return out

    a = losses(answers(rt, point, point.spec, rows, rt.backend_for(point.name))[1], champion)
    b = [0] * len(rows)
    if cal is not None:             # a challenger without a calibration never acts: no need to ask it
        b = losses(answers(rt, point, challenger, rows, backend or rt.backend_for(point.name))[1],
                   lambda model: (cal.temperature, cal.thresholds) if model == cal.model else (1.0, {}))
    errors = [phase is not None and default not in right(target) for (_, target, _), phase, default in
              zip(rows, phases, defaults)]
    return a, b, [row.decision.get("session", "") for row, _, _ in rows], errors


def summarize(per_point: dict) -> dict:
    """Per point: the rows and sessions scored, the default errors among them, and for each side the rows it would
    act on, its fixes (acted and right on a default error) and harms (acted and wrong), the sessions it acts in and
    those harmed (any of its acts wrong), and its fixed share of the headroom, (fixes - harms) / default errors. An
    act that swaps a right default for another right answer has no gain, so it is counted in none of these."""
    def side(losses: list, groups: list, errors: int) -> dict:
        fixes, harms = sum(x < 0 for x in losses), sum(x > 0 for x in losses)
        return {"acted": fixes + harms, "fixes": fixes, "harms": harms,
                "harmed_sessions": len({g for x, g in zip(losses, groups) if x > 0}),
                "sessions_acted": len({g for x, g in zip(losses, groups) if x}),
                "fixed_share": round((fixes - harms) / errors, 4) if errors else None}
    out = {}
    for name, (a, b, g, e) in per_point.items():
        out[name] = {"n": len(a), "sessions": len(set(g)), "default_errors": sum(e),
                     "champion": side(a, g, sum(e)), "challenger": side(b, g, sum(e))}
    return out


def evaluate_spec(rt, cand, record) -> EvalResult:
    """A spec candidate on the gate record's test sessions: the point's model on the current spec at its gated
    calibration, and on the candidate's spec at a calibration fitted on the calibration sessions. Held-out tasks'
    sessions are scored beside them, for the record only. Promoted, the spec comes with that calibration, stored with
    the record's hash."""
    from .calibrate import apply as store, calibrate_point
    p = rt.points[cand.point]
    challenger, base = p.spec.apply(cand.change), rt.specs.get(cand.point)
    rows = labelled(rt, cand.point)
    parts = record.split(rows, rt.ledger.sessions()[0], record.cal_frac(cand.point))
    params = record.params
    cal = calibrate_point(rt, cand.point, rows=parts["calibration"], spec=challenger, risk=params["risk"],
                          min_sessions=params["min_cal_sessions"])
    record.consume(cand.id)             # the first inference request on the test sessions uses the record up
    test, held = (scored(rt, p, parts[k], challenger, cal) for k in ("test", "held_out"))
    guarded = protected_ids(rt, cand.point)
    protected = [r for r in rows if r[0].decision["id"] in guarded]
    _, pp = predict(rt, p, challenger, protected)
    protected_ok = all(gold(probs) in right(t) for probs, (_, t, _) in zip(pp, protected))

    def apply() -> None:
        rt.specs.set(cand.point, challenger.to_json(), note=f"{cand.id}: {cand.hypothesis}", expected=base)
        p.spec = challenger
        if cal is not None:
            store(rt, cal, gate=record.hash)

    return EvalResult(cand.point, *test[:3], apply, protected_ok, per_point={cand.point: test},
                      details={"held_out": summarize({cand.point: held})[cand.point]},
                      calibrations=[cal.gated(p.hash_of(challenger))] if cal is not None else [])


EVALUATORS = {"spec": evaluate_spec}
