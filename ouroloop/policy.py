"""From an answer to an action: the execution rule, exploration and propensities (PLAN_v0.2 §0), and thresholds."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .types import Answer

ROUTES = ("act", "default", "shadow", "rule", "error")
ACTING = {"stop"}       # v0.2: the only point that may be switched on, wherever it is switched on


@dataclass
class Policy:
    act: bool = False       # may the model's answer be acted on; off, the point is shadow (v0.2: only stop may act)
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


def fit_threshold(confidences: list[float], correct: list[bool], sessions: list[str] | None = None,
                  risk: float = 0.05, alpha: float = 0.05) -> float | None:
    """Lowest confidence cut-off whose acting decisions keep a Clopper-Pearson error bound <= risk (None if none
    does). Counted per session: at each cut-off a session counts only its first decision at or above it, the one
    that would act and change what follows, so a session's many rows are not taken as independent evidence. Pass
    only decisions that would act (a label other than the default), in time order; without `sessions` every
    decision is its own session."""
    sessions = sessions if sessions is not None else list(range(len(confidences)))
    best = None
    for cut in sorted(set(confidences), reverse=True):
        first: dict = {}
        for conf, ok, s in zip(confidences, correct, sessions):
            if conf >= cut and s not in first:
                first[s] = ok
        if cp_upper(sum(not ok for ok in first.values()), len(first), alpha) <= risk:
            best = cut
    return best
