"""The promotion gate shared by model, spec and prompt candidates (PLAN §4.6), in order:
evidence complete -> protected samples pass -> no significant regression -> significant improvement."""
from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class GateResult:
    outcome: str                          # "accepted", "rejected" or "insufficient_evidence"
    reasons: list[str]
    n: int
    delta: float | None = None            # mean challenger - champion loss (Brier, failure rate); negative is better
    ci: tuple[float, float] | None = None


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


def decide_points(results: dict[str, tuple[list[float], list[float], list[str]]], *, protected_ok: bool = True,
                  min_items: int = 30, n_boot: int = 2000, seed: int = 0) -> GateResult:
    """A model serves several decision points: accept if at least one point improves significantly and no point
    gets significantly worse. Points with too little test data can neither pass nor block."""
    if not protected_ok:
        return GateResult("rejected", ["a protected sample failed"], sum(len(a) for a, _, _ in results.values()))
    improved, regressed, thin, n = [], [], [], 0
    summary = {}
    for name, (a, b, g) in results.items():
        n += len(a)
        if len(a) < min_items or len(set(g)) < 2:
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
    if regressed:
        return GateResult("rejected", [f"significant regression on {', '.join(regressed)}"], n, delta, ci)
    if improved:
        return GateResult("accepted", [f"Brier improved on {', '.join(improved)}"], n, delta, ci)
    if len(thin) == len(results):
        return GateResult("insufficient_evidence", [f"too few test items on {', '.join(thin)}"], n)
    return GateResult("rejected", ["no point improved significantly"], n, delta, ci)


def decide(champion: list[float], challenger: list[float], groups: list[str], *, protected_ok: bool = True,
           regressions: dict | None = None, min_items: int = 30, n_boot: int = 2000, seed: int = 0,
           metric: str = "Brier") -> GateResult:
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
