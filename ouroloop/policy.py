"""From an answer to an action: the execution rule, exploration and propensities (PLAN_v0.2 §0), and thresholds."""
from __future__ import annotations

import bisect
import hashlib
import math
import random
from dataclasses import dataclass
from functools import lru_cache

from .types import Answer

ROUTES = ("act", "default", "shadow", "rule", "error")
# The harness's built-in points (ouroloop.points). In v0.2 stop is the only one of them that may be switched on
# (PLAN_v0.2 §3: the others wait for their triggers); a point that a scenario pack or a contract user defines may be.
HARNESS = frozenset({"retry", "compact", "tool_gate", "stop", "route"})
ACTING = {"stop"}
# How thresholds and the post-act monitor count a session's acting decisions, one per session either way: "first",
# its first one (a terminal decision such as stop acts once and changes what follows); "sampled", one chosen by a
# hash of the session id (a decision repeated within a session, such as a move in a game).
COUNTS = ("first", "sampled")


def may_act(point: str) -> bool:
    return point in ACTING or point not in HARNESS


@dataclass
class Policy:
    act: bool = False       # may the model's answer be acted on; off, the point is shadow (see may_act)
    explore: float = 0.05   # share of decisions the model could act on that still take the default, for outcomes


@dataclass
class Route:
    route: str          # "act", "default" or "shadow" here; the runtime adds "rule" and "error"
    explored: bool
    propensity: float   # probability of taking this route under the policy, for later off-policy correction


def route(policy: Policy, ans: Answer, default: str | None, threshold: float | None, rng: random.Random) -> Route:
    """The execution rule: act only when the point's act is on, the answer is decisive (no abstention, no tied top
    label), its label differs from the phase's default and its tempered confidence reaches a gated threshold (which
    a NaN threshold never is). Otherwise the default is taken: on route "shadow" when act is off, "default" when on."""
    if not policy.act:
        return Route("shadow", False, 1.0)
    if threshold is None or not ans.decisive or ans.label == default or not ans.confidence >= threshold:
        return Route("default", False, 1.0)
    if rng.random() < policy.explore:
        return Route("default", True, policy.explore)
    return Route("act", False, 1.0 - policy.explore)


def _binom_cdf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    lp, lq, lnf = math.log(p), math.log1p(-p), math.lgamma(n + 1)
    return min(1.0, sum(math.exp(lnf - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
                        for i in range(k + 1)))


@lru_cache(maxsize=65536)
def cp_upper(errors: int, n: int, alpha: float = 0.05) -> float:
    """One-sided Clopper-Pearson upper bound on an error rate after `errors` mistakes in `n` trials."""
    if n == 0 or errors >= n:
        return 1.0
    lo, hi = errors / n, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _binom_cdf(errors, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def cp_lower(errors: int, n: int, alpha: float = 0.05) -> float:
    """One-sided Clopper-Pearson lower bound on an error rate after `errors` mistakes in `n` trials."""
    if n == 0 or errors <= 0:
        return 0.0
    lo, hi = 0.0, errors / n
    for _ in range(60):
        mid = (lo + hi) / 2
        if 1.0 - _binom_cdf(errors - 1, n, mid) < alpha:
            lo = mid
        else:
            hi = mid
    return lo


def pick(session, salt: str, n: int) -> int:
    """The row a "sampled" count takes among a session's n rows: by a hash of the session id and `salt`."""
    return int(hashlib.sha1(f"{session}\x00{salt}".encode()).hexdigest()[:15], 16) % n


def per_session(items, count: str = "first", salt: str = "") -> dict:
    """(session, value) pairs in time order -> one value per session, by the counting rule (see COUNTS)."""
    by: dict = {}
    for s, v in items:
        by.setdefault(s, []).append(v)
    return {s: vs[0] if count == "first" else vs[pick(s, salt, len(vs))] for s, vs in by.items()}


def fit_threshold(confidences: list[float], correct: list[bool], sessions: list[str] | None = None,
                  risk: float = 0.05, alpha: float = 0.05, count: str = "first") -> float | None:
    """Lowest confidence cut-off whose acting decisions keep a Clopper-Pearson error bound <= risk (None if none
    does). Counted per session, so a session's many rows are not taken as independent evidence: at each cut-off a
    session counts one of its decisions at or above it, by `count` ("first": its first, the one that would act and
    change what follows; "sampled": one chosen by a hash of the session id and the cut-off). Pass only decisions that
    would act (a label other than the default), in time order; without `sessions` every decision is its own session."""
    if count not in COUNTS:
        raise ValueError(f"count {count!r}: one of {COUNTS}")
    sessions = sessions if sessions is not None else list(range(len(confidences)))
    order = sorted(range(len(confidences)), key=lambda i: -confidences[i])
    rows: dict = {}             # session -> its rows at or above the cut-off (time indices, in time order)
    errors, best, k = 0, None, 0
    while k < len(order):
        cut = confidences[order[k]]
        while k < len(order) and confidences[order[k]] == cut:     # the cut-off takes in every row at it
            i = order[k]
            k += 1
            mine = rows.setdefault(sessions[i], [])
            if count == "first" and (not mine or i < mine[0]):     # a new first row for its session
                errors += (not correct[i]) - (bool(mine) and not correct[mine[0]])
            bisect.insort(mine, i)
        if count == "sampled":
            errors = sum(not correct[m[pick(s, repr(cut), len(m))]] for s, m in rows.items())
        if errors <= risk * len(rows) and cp_upper(errors, len(rows), alpha) <= risk:
            best = cut
    return best
