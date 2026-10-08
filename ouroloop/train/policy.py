"""From an answer to an action: the execution rule, exploration and propensities (docs/DESIGN.md invariant 1), and
thresholds.

The risk a threshold and the post-act monitor bound is per session: a session is harmed when any decision the model
acted on in it is wrong, and the budget is the share of the sessions the model acts in that are harmed. A decision
asked many times in a session compounds its per-decision error over them, so a per-decision budget would say nothing
about what a session risks."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from functools import lru_cache

from .types import Answer

# "policy" is the task/research profile's equivalent of "act": a bound policy's own answer was executed
# (runtime.propose). It is a separate name because the permission behind it is different — a release's
# authorisation, not a gate record's calibration.
ROUTES = ("act", "default", "shadow", "rule", "error", "policy")
@dataclass
class Policy:
    act: bool = False       # may the model's answer be acted on; off, the point is shadow
    explore: float = 0.05   # share of decisions the model could act on that still take the default, for outcomes


@dataclass
class Route:
    route: str          # "act", "default" or "shadow" here; the runtime adds "rule" and "error"
    explored: bool
    propensity: float   # probability of taking this route under the policy, for later off-policy correction


def route(policy: Policy, ans: Answer, default: str | None, threshold: float | None, rng: random.Random) -> Route:
    """The execution rule: act only when the point's act is on, the answer is decisive (no abstention, no tied top
    label), its label differs from `default` (the routing default: the phase's rule answer, or its fixed label) and
    its tempered confidence reaches a gated threshold (which a NaN threshold never is). Otherwise the default is
    taken: on route "shadow" when act is off, "default" when on."""
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


def harmed(items) -> dict:
    """(session, correct) pairs of the decisions a model acted on -> session -> whether it is harmed (any of them
    wrong)."""
    out: dict = {}
    for s, ok in items:
        out[s] = out.get(s, False) or not ok
    return out


def fit_threshold(confidences: list[float], correct: list[bool], sessions: list | None = None,
                  risk: float = 0.05, alpha: float = 0.05) -> float | None:
    """The confidence cut-off to act at, None if none: each cut-off is a test that the sessions it would act in keep
    a Clopper-Pearson bound on the harmed share <= risk, taken in a fixed sequence (Learn-then-Test): down from the
    most confident (ties taken together), starting at the first cut-off with enough sessions to pass at all (a count
    of sessions, never of errors), the lowest before the first that fails. Keeping any lower cut-off that passes
    after one failed would let luck pick among dozens of nested tests, and a model above its risk would get a
    threshold far more often than alpha. At a cut-off, the sessions with a row at or above it are counted, and one is
    harmed when any of those rows is wrong: a session's many rows are neither independent evidence nor independent
    risks. Pass only decisions that would act (a label other than the routing default); without `sessions` every
    decision is its own session."""
    sessions = sessions if sessions is not None else list(range(len(confidences)))
    order = sorted(range(len(confidences)), key=lambda i: -confidences[i])
    bad: dict = {}              # session -> harmed by its rows at or above the cut-off
    n_harmed, best, k = 0, None, 0
    while k < len(order):
        cut = confidences[order[k]]
        while k < len(order) and confidences[order[k]] == cut:     # the cut-off takes in every row at it
            i = order[k]
            k += 1
            was = bad.get(sessions[i], False)
            bad[sessions[i]] = was or not correct[i]
            n_harmed += bad[sessions[i]] and not was
        if cp_upper(0, len(bad), alpha) > risk:                     # too few sessions to pass: not a test yet
            continue
        if n_harmed > risk * len(bad) or cp_upper(n_harmed, len(bad), alpha) > risk:
            break
        best = cut
    return best
