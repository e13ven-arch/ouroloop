"""From probabilities to an action: modes, thresholds, exploration and propensities (PLAN §2.3)."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

MODES = ("shadow", "gate", "auto")


@dataclass
class Policy:
    mode: str = "shadow"               # shadow: only record; gate: may only apply `conservative`; auto: act on the threshold
    risk: float = 0.05                 # target error rate among automatic actions
    threshold: float | None = None     # fitted on the calibration split; None means never act automatically
    fallback: str = "llm"              # who decides when the model does not: "llm", "human" or "default"
    default: str | None = None         # the label used when fallback == "default"
    explore: float = 0.05              # share of eligible decisions still sent to the fallback, so both sides get outcomes
    conservative: str | None = None    # the one label gate mode may apply by itself (e.g. "no" for tool_gate)


@dataclass
class Route:
    route: str          # "shadow", "auto" or "fallback"
    explored: bool
    propensity: float   # probability of taking this route under the policy, for later off-policy correction


def route(policy: Policy, label: str, confidence: float, rng: random.Random) -> Route:
    if policy.mode == "shadow":
        return Route("shadow", False, 1.0)
    eligible = policy.threshold is not None and confidence >= policy.threshold
    if policy.mode == "gate" and label != policy.conservative:
        eligible = False
    if not eligible:
        return Route("fallback", False, 1.0)
    if rng.random() < policy.explore:
        return Route("fallback", True, policy.explore)
    return Route("auto", False, 1.0 - policy.explore)


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


def fit_threshold(confidences: list[float], correct: list[bool], risk: float = 0.05,
                  alpha: float = 0.05) -> float | None:
    """Lowest confidence cut-off whose accepted decisions have an error upper bound ≤ risk (None if none does)."""
    pairs = sorted(zip(confidences, correct), key=lambda t: -t[0])
    best, errors = None, 0
    for i, (conf, ok) in enumerate(pairs, 1):
        errors += not ok
        if i < len(pairs) and pairs[i][0] == conf:
            continue  # a cut-off accepts every decision with the same confidence
        if cp_upper(errors, i, alpha) <= risk:
            best = conf
    return best
