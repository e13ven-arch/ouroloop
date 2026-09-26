"""The frozen gate (PLAN §4.6, PLAN_v0.2 M8), the one gate for model, spec, recipe and prompt candidates.

A person freezes the gate template (`ouroloop gate freeze`). Each round the framework derives a gate record from it
and freezes it in the ledger (a copy in <workspace>/gates/<round>.json), which fixes the round's test sessions before
anything is trained; `run(record, ev)` evaluates a candidate against it and gives the verdict. A record is used up by
the first inference request on its test sessions, whatever follows; once used, a verdict on its sessions can only be
"validated". The verdict, in order: evidence complete -> protected samples pass -> no significant regression ->
significant improvement. Model and spec candidates are judged by their gain over the routing default on the test rows
(replay.scored): what a model adds is only where it acts against the default, and a Brier score over every row is
dominated by rows the default already gets right.
"""
from __future__ import annotations

import hashlib
import json
import random
import subprocess
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import httpx

from ..ledger import Ledger, PromotionConflict, locked, now, write_json
from ..policy import cp_lower
from ..release import code_version
from .replay import EvalResult, in_calibration
from .train import TrainError

TEMPLATE = {
    "risk": 0.05,                   # the share of sessions acted in that may be harmed (any acted decision wrong)
    "n_boot": 2000,                 # paired bootstrap resamples, by session
    "min_sessions": 30,             # non-held-out test sessions a model or spec gate needs, per point
    "cal_frac": {"stop": 0.4, "*": 0.15},  # share of the training sessions kept for calibration, per point
    "min_cal_sessions": 120,        # fewer calibration sessions: no thresholds, the point cannot act
    "checks": {"min_train": 200, "max_mde": 0.10},     # data checks before training (evolve/checks.py)
    "suite": {"min_items": 10},     # prompt gates: development-task runs per side
    "test_sessions": "the latest non-held-out sessions with a labelled decision that started after the freezing of "
                     "the last gate record whose fresh sessions a gate used, and never went through a gate, at most "
                     "budget.sessions_per_generation (the first record: the latest min_sessions); fewer than "
                     "min_sessions: the latest older ones, and the verdict can only be validated",
    "target": {"fixed_share": 0.5},     # stop training once the accepted model fixes this share of default errors
    "budget": {"sessions_per_generation": 40, "max_trainings": 10},
}
# What no template may loosen, (lowest, highest): the execution rule's risk (PLAN_v0.2 §0), the sessions each gate
# needs (§2), enough resamples for a 95% interval, and the runs a prompt gate needs (the template's own default).
LIMITS = {"risk": (0.0, 0.05), "min_sessions": (30, None), "n_boot": (1000, None), "suite.min_items": (10, None)}
# Failures of the machinery, not of the candidate: the verdict is "inconclusive", never evidence against it.
INFRA = (TrainError, OSError, subprocess.SubprocessError, httpx.HTTPError)


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def template_path(root: str | Path) -> Path:
    return Path(root) / "gates" / "template.json"


def freeze_template(root: str | Path, params: dict | None = None, replace_frozen: bool = False) -> tuple[dict, str]:
    """A person's act: write the gate template, TEMPLATE with `params` changed, within LIMITS. Freezing again
    (replace_frozen) makes a new template, so the next round freezes a new record."""
    path = template_path(root)
    if path.exists() and not replace_frozen:
        raise ValueError(f"the gate is already frozen: {path}")
    unknown = set(params or {}) - set(TEMPLATE)
    if unknown:
        raise ValueError(f"unknown gate parameters {sorted(unknown)}; the template has {sorted(TEMPLATE)}")
    template = {**TEMPLATE, **(params or {}), "frozen": now()}
    validate(template, records(root))
    write_json(path, template)
    return load_template(root)


def load_template(root: str | Path) -> tuple[dict, str]:
    """-> (the template, its hash). A template edited past LIMITS is refused."""
    path = template_path(root)
    if not path.exists():
        raise ValueError(f"no frozen gate in {path.parent}: a person runs `ouroloop gate freeze` first")
    raw = path.read_bytes()
    template = json.loads(raw)
    validate(template)
    return template, _hash(raw)


def _frac(params: dict, point: str) -> float:
    fracs = params["cal_frac"]
    return fracs.get(point, fracs.get("*", 0.15))


def validate(params: dict, past: list[GateRecord] = ()) -> None:
    """No template loosens LIMITS. Nor does one give a point a larger share of calibration sessions than an earlier
    record did: the calibration split is by a hash of the session id, so a larger share would take in sessions a
    promoted model (or the parent a candidate starts from) was trained on, and its thresholds would be optimistic."""
    for key, (lo, hi) in LIMITS.items():
        v = params
        for part in key.split("."):
            v = v.get(part) if isinstance(v, dict) else None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo or (hi is not None and v > hi):
            raise ValueError(f"gate template: {key} = {v!r} is looser than any template may be "
                             f"({'at most ' + str(hi) if hi is not None else 'at least ' + str(lo)})")
    for r in past:
        for point in {*params["cal_frac"], *r.params["cal_frac"], "*"}:
            if _frac(params, point) > _frac(r.params, point):
                raise ValueError(f"gate template: cal_frac of {point!r} grows from {_frac(r.params, point)} (record "
                                 f"{r.round}) to {_frac(params, point)}; it may only shrink")


class GateRecord:
    """A round's gate record as frozen in the ledger, whose gate events also say when it was used and what it decided
    (the last verdict counts). The ledger is append-only (PLAN_v0.2 §1 rule 3); gates/<round>.json and
    gates/<round>.used.json are copies, for people and for the runtime, which the gate never reads: deleting or
    editing them changes no record, frees no test session and undoes no use."""

    def __init__(self, root: str | Path, events: dict):
        self.root = Path(root)
        self.hash, self.data = events["frozen"]["record"], events["frozen"]["data"]
        self.round, self.params, self.fresh = self.data["round"], self.data["params"], self.data["fresh"]
        self.test_sessions: list[str] = list(self.data["test_sessions"])
        self.took = False           # this object used the record up (another process may have, instead)
        self._events = events

    @property
    def usage(self) -> dict | None:
        used, closed = self._events.get("used"), self._events.get("closed")
        if not used:
            return None
        revoked = self._events.get("revoked")
        return {"ts": used["ts"], "by": used["by"],
                **{k: v for k, v in (closed or {}).items() if k not in ("type", "event", "round", "record", "ts")},
                **({"revoked": revoked["ts"], "revoked_because": revoked["reason"]} if revoked else {})}

    @property
    def used(self) -> bool:
        return "used" in self._events

    def _append(self, event: str, **fields) -> None:
        ev = Ledger(self.root / "ledger").append({"type": "gate", "event": event, "round": self.round,
                                                  "record": self.hash, **fields})
        self._events = {**self._events, event: ev}
        self.mirror()

    def mirror(self) -> None:
        """Write the copies in gates/: the record, and once used, its use and verdict with the record's hash."""
        write_json(self.root / "gates" / f"{self.round}.json", self.data)
        if self.used:
            write_json(self.root / "gates" / f"{self.round}.used.json", {**self.usage, "record": self.hash})

    def consume(self, by: str) -> bool:
        """Called right before the first inference request on the test sessions: from then on the record and its
        sessions are used up, whatever the outcome. False when it already was (by this or another process). A record
        that is no longer the round's (see check) raises before anything is inferred."""
        with locked(_lock(self.root)):
            log = _log(self.root)
            check(self, log)
            self._events = log[self.round]
            if self.used:
                return False
            self._append("used", by=by)
        self.took = True
        return True

    def close(self, outcome: str, reasons: list, sessions: list[str] = (), calibrations: list[dict] = ()) -> None:
        """The verdict. An accepting one lists the calibrations the promotion stores: the only ones that may act."""
        self._append("closed", outcome=outcome, reasons=reasons, sessions=list(sessions),
                     calibrations=list(calibrations))

    @property
    def ran_own(self) -> bool:
        """The evaluation that used the record ran its own test sessions (a prompt gate): the record's ledger
        sessions were never inferred on, so they stay fresh for the next record."""
        return bool((self.usage or {}).get("sessions"))

    def seen(self) -> set[str]:
        return set(self.usage["sessions"]) if self.ran_own else set(self.test_sessions)

    def cal_frac(self, point: str) -> float:
        return _frac(self.params, point)

    def split(self, rows: list[tuple], sessions: dict, cal_frac: float = 0.0) -> dict[str, list[tuple]]:
        """Rows (row, target, weight) by their part in the gate: "test", this record's test sessions; "held_out",
        the held-out tasks' sessions reported beside them; "train" and "calibration" (by a hash of the session id),
        the other sessions that started before the record was frozen, never a held-out task's. `sessions` is
        Ledger.sessions()."""
        test, held, position = set(self.test_sessions), set(self.data["held_out_sessions"]), self.data["position"]
        out: dict[str, list[tuple]] = {"train": [], "calibration": [], "test": [], "held_out": []}
        for r in rows:
            s = r[0].decision.get("session", "")
            info = sessions.get(s, {})
            if s in test:
                out["test"].append(r)
            elif s in held:
                out["held_out"].append(r)
            elif not info.get("held_out") and info.get("start", position) < position:
                out["calibration" if in_calibration(s, cal_frac) else "train"].append(r)
        return out


def revoke_record(root: str | Path, round: str, reason: str) -> None:
    """Withdraw what a gate record accepted (a rollback of the model it promoted): from then on the promotion it
    made neither answers nor acts, whatever harness/revoked.json says, and cannot be made again."""
    log = _log(root)
    if "used" in log.get(round, {}):
        GateRecord(root, log[round])._append("revoked", reason=reason)


def _lock(root: str | Path) -> Path:
    return Path(root) / "gates" / "records"


def _log(root: str | Path) -> dict[str, dict]:
    """The gate events of the workspace's ledger by round: "frozen", "used", "closed", "revoked" (the last of each)."""
    out: dict[str, dict] = {}
    for ev in Ledger(Path(root) / "ledger").events():
        if ev.get("type") == "gate":
            out.setdefault(ev["round"], {})[ev["event"]] = ev
    return out


def records(root: str | Path, log: dict | None = None) -> list[GateRecord]:
    log = _log(root) if log is None else log
    return [GateRecord(root, e) for _, e in sorted(log.items()) if "frozen" in e]


def seen_sessions(root: str | Path) -> set[str]:
    return set().union(*(r.seen() for r in records(root) if r.used))


def check(record: GateRecord, log: dict | None = None) -> None:
    """Only the round's record can decide: the latest one frozen in the ledger, unchanged, from the gate template
    and the code in force. An older record would score sessions a newer one also holds."""
    log = _log(record.root) if log is None else log
    frozen = log.get(record.round, {}).get("frozen")
    if not frozen or frozen["record"] != record.hash or frozen["data"] != record.data:
        raise ValueError(f"gate record {record.round} is not the one frozen in the ledger")
    latest = max(r for r, e in log.items() if "frozen" in e)
    if record.round != latest:
        raise ValueError(f"gate record {record.round} was superseded by {latest}")
    if record.data["template"] != load_template(record.root)[1] or record.data["code"]["ouroloop"] != code_version():
        raise ValueError(f"gate record {record.round} was frozen from another gate template or code")


def open_record(rt) -> GateRecord:
    """The round's gate record: the latest one while nothing has used it and it was frozen from the current template
    and code (a rerun after a failure before inference uses the same one), else a new one. A latest record without
    fresh sessions, which can only validate, gives way to a new one once there are enough fresh sessions; so does an
    unused one once newer labelled sessions would change its test set: no inference has touched its test sessions,
    so they can train the new round instead of starving it (a first record that tested on most of the sessions
    there were). The copies in gates/ are written again from the ledger."""
    params, template = load_template(rt.root)
    code = code_version()
    with locked(_lock(rt.root)):
        past = records(rt.root)
        for r in past:
            r.mirror()
        last = past[-1] if past else None
        reuse = last and not last.used and last.data["template"] == template and last.data["code"]["ouroloop"] == code
        data = _choose(rt, params, past)
        if reuse and (not data["fresh"] or (last.fresh and data["test_sessions"] == last.test_sessions)):
            return last
        validate(params, past)
        data = {"round": f"r_{len(past) + 1:04d}", "frozen": now(), "template": template, "params": params,
                "code": {"ouroloop": code, "serve": _serve_versions(rt)}, **data}
        raw = json.dumps(data, ensure_ascii=False, indent=2)
        ev = rt.ledger.append({"type": "gate", "event": "frozen", "round": data["round"], "record": _hash(raw.encode()),
                               "data": data})
        record = GateRecord(rt.root, {"frozen": ev})
        record.mirror()
        return record


def _serve_versions(rt) -> dict:
    serve = {}
    for name, backend in rt.backends.items():
        if hasattr(backend, "code_version"):
            try:
                serve[name] = backend.code_version()
            except Exception:  # noqa: BLE001 - recorded as unknown; the gate's own inference fails later if it is down
                serve[name] = None
    return serve


def _choose(rt, params: dict, past: list[GateRecord]) -> dict:
    """Choose the test sessions: the latest of the non-held-out sessions that started after the freezing of the last
    record whose fresh sessions a gate used (a prompt gate uses the runs it makes instead), and never went through a
    gate, at most a generation's (budget.sessions_per_generation, at least `min_sessions`; for the first record, the
    latest `min_sessions`). The older of them train the round: after rounds that trained nothing, the data that made
    the schedule train is not all held for testing. With fewer, the latest older sessions, which can only validate.
    Only sessions with a labelled decision at a registered point count: one still running, or one that broke before
    its probe, would be no evidence. The held-out tasks' sessions from the same time are reported beside them."""
    sessions, position = rt.ledger.sessions()
    used = [r for r in past if r.used]
    seen = set().union(*(r.seen() for r in used))
    window = next((r.data["position"] for r in reversed(used) if r.fresh and not r.ran_own), None)
    live = sorted((s for s, info in sessions.items() if info["labelled"] & set(rt.points)),
                  key=lambda s: sessions[s]["start"])
    pool = [s for s in live if not sessions[s].get("held_out")]
    k = params["min_sessions"]
    if window is None:
        fresh = [s for s in pool if s not in seen][-k:]
    else:
        most = max(k, params.get("budget", {}).get("sessions_per_generation", k))
        fresh = [s for s in pool if sessions[s]["start"] >= window and s not in seen][-most:]
    test = fresh if len(fresh) >= k else [s for s in pool if window is None or sessions[s]["start"] < window][-k:]
    first = min((sessions[s]["start"] for s in test), default=position)
    held = [s for s in live if sessions[s].get("held_out") and sessions[s]["start"] >= first]
    return {"position": position, "fresh": len(fresh) >= k, "test_sessions": test, "held_out_sessions": held}


@dataclass
class GateResult:
    outcome: str      # accepted, rejected, validated, inconclusive, conflict, insufficient_evidence, blocked, ...
    reasons: list[str]
    n: int
    delta: float | None = None            # mean challenger - champion loss (-gain, failure rate); negative is better
    ci: tuple[float, float] | None = None
    used: bool = False                    # this evaluation used the record up: its verdict is the round's


def run(record: GateRecord, ev: Callable[[], EvalResult], *, seed: int = 0) -> tuple[GateResult, EvalResult]:
    """The single entry of every promotion. `ev` evaluates the candidate and calls record.consume() right before its
    first inference request on the test sessions. Only an evaluation that used the record up, on fresh test sessions
    or on new sessions it ran itself (prompt candidates), can be accepted and promoted; on sessions a gate has seen
    the verdict is "validated" at best. An infrastructure failure is "inconclusive"; a promotion that loses the
    compare-and-swap is "conflict". Gate parameters come from the record, never from the candidate. A record that is
    not the round's (see check) raises before anything is evaluated."""
    check(record)
    params, before = record.params, record.took
    try:
        ev = ev()
    except INFRA as e:
        ev = EvalResult("", [], [], [], lambda: None, inconclusive=[f"{type(e).__name__}: {e}"[:500]])
    except Exception as e:  # a broken candidate: the caller decides; a record it used stays used
        if record.took and not before:
            record.close("failed", [f"{type(e).__name__}: {e}"[:500]])
        raise
    used = record.took and not before
    if ev.rejected:
        g = GateResult("rejected", ev.rejected, len(ev.challenger))
    elif ev.inconclusive:
        g = GateResult("inconclusive", ev.inconclusive, 0)
    elif ev.blocked or ev.insufficient:
        g = GateResult("blocked" if ev.blocked else "insufficient_data", ev.blocked or ev.insufficient, 0)
    elif ev.per_point:
        g = decide_points(ev.per_point, protected_ok=ev.protected_ok, min_sessions=params["min_sessions"],
                          n_boot=params["n_boot"], seed=seed, risk=params["risk"])
    else:
        g = decide(ev.champion, ev.challenger, ev.groups, protected_ok=ev.protected_ok,
                   **{**ev.gate, "min_items": params["suite"]["min_items"], "n_boot": params["n_boot"], "seed": seed})
    if g.outcome == "accepted" and not (used and (record.fresh or ev.sessions)):
        g = replace(g, outcome="validated",
                    reasons=[*g.reasons, "on test sessions a gate has used or seen: validated, not promoted"])
    if g.outcome == "accepted":
        record.close("accepted", g.reasons, ev.sessions, ev.calibrations)   # what a promotion is checked against
        try:
            ev.apply()
        except PromotionConflict as e:
            g = replace(g, outcome="conflict", reasons=[str(e)])
        except Exception as e:
            record.close("failed", [f"{type(e).__name__}: {e}"[:500]], ev.sessions)
            raise
    g.used = used
    if used and g.outcome != "accepted":
        record.close(g.outcome, g.reasons, ev.sessions)
    return g, ev


def paired_bootstrap(a: list[float], b: list[float], groups: list[str], n_boot: int = 2000,
                     seed: int = 0) -> tuple[float, float, float]:
    """Mean of b - a with a 95% percentile interval, resampling whole groups (sessions)."""
    sums: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
    for x, y, g in zip(a, b, groups):
        sums[g][0] += y - x
        sums[g][1] += 1
    keys = list(sums)
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        s = n = 0.0
        for _ in keys:
            k = keys[rng.randrange(len(keys))]
            s += sums[k][0]
            n += sums[k][1]
        means.append(s / n)
    means.sort()
    point = sum(y - x for x, y in zip(a, b)) / len(a)
    return point, means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1]


def decide_points(results: dict[str, tuple], *, protected_ok: bool = True, min_sessions: int = 30,
                  n_boot: int = 2000, seed: int = 0, risk: float | None = None, alpha: float = 0.05) -> GateResult:
    """A model serves several decision points; each point's results are (champion loss, challenger loss, session,
    ...) per test row, a loss being -gain. Reject a challenger that harms more than `risk` of the test sessions it
    acts in with confidence (the post-act monitor's rule, before promotion; an act of no gain, a right default
    swapped for another right answer, is not counted, which only makes the rule stricter); else accept if at least
    one point's gain rises significantly and no point's falls significantly. Points with test items from fewer than
    `min_sessions` sessions can neither pass nor block."""
    if not protected_ok:
        return GateResult("rejected", ["a protected sample failed"], sum(len(r[0]) for r in results.values()))
    improved, regressed, thin, harmful, n = [], [], [], [], 0
    summary = {}
    for name, (a, b, g, *_) in results.items():
        n += len(a)
        acts = {s for x, s in zip(b, g) if x}
        bad = {s for x, s in zip(b, g) if x > 0}
        if risk is not None and cp_lower(len(bad), len(acts), alpha) > risk:
            harmful.append(f"{name} harms {len(bad)} of the {len(acts)} test sessions it acts in")
        if len(set(g)) < max(2, min_sessions):
            thin.append(name)
            continue
        delta, lo, hi = paired_bootstrap(a, b, g, n_boot, seed)
        summary[name] = (delta, lo, hi)
        if lo > 0:
            regressed.append(name)
        elif hi < 0:
            improved.append(name)
    pooled = [(d, lo, hi) for d, lo, hi in summary.values()]
    delta = sum(d for d, _, _ in pooled) / len(pooled) if pooled else None
    ci = (min(lo for _, lo, _ in pooled), max(hi for _, _, hi in pooled)) if pooled else None
    if harmful:
        return GateResult("rejected", [f"{'; '.join(harmful)}: above the risk {risk} with confidence"], n, delta, ci)
    if regressed:
        return GateResult("rejected", [f"gain fell significantly on {', '.join(regressed)}"], n, delta, ci)
    if improved:
        return GateResult("accepted", [f"gain rose significantly on {', '.join(improved)}"], n, delta, ci)
    if len(thin) == len(results):
        return GateResult("insufficient_evidence", [f"test items from fewer than {min_sessions} sessions on "
                                                    f"{', '.join(thin)}"], n)
    return GateResult("rejected", ["no point's gain rose significantly"], n, delta, ci)


def decide(champion: list[float], challenger: list[float], groups: list[str], *, protected_ok: bool = True,
           regressions: dict | None = None, min_items: int = 30, n_boot: int = 2000, seed: int = 0,
           metric: str = "the loss") -> GateResult:
    """Per-item losses (lower is better) of the champion and the challenger on the same items."""
    n = len(champion)
    if n != len(challenger) or n != len(groups) or any(v is None for v in champion + challenger):
        return GateResult("insufficient_evidence", ["some planned items have no score"], n)
    if n < min_items or len(set(groups)) < 2:
        return GateResult("insufficient_evidence",
                          [f"{n} items in {len(set(groups))} groups; need {min_items} items in 2+ groups"], n)
    if not protected_ok:
        return GateResult("rejected", ["a protected sample failed"], n)
    for name, (a, b, g) in (regressions or {}).items():
        if paired_bootstrap(a, b, g, n_boot, seed)[1] > 0:
            return GateResult("rejected", [f"significant regression on {name}"], n)
    delta, lo, hi = paired_bootstrap(champion, challenger, groups, n_boot, seed)
    if all(abs(y - x) < 1e-12 for x, y in zip(champion, challenger)):
        return GateResult("rejected", ["no change on any item"], n, delta, (lo, hi))
    if hi < 0:
        return GateResult("accepted", [f"{metric} improved: 95% interval below 0"], n, delta, (lo, hi))
    return GateResult("rejected", [f"no significant improvement in {metric}"], n, delta, (lo, hi))
