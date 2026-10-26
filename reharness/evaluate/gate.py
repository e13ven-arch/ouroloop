"""The paired dev gate (docs/design/EVALUATE.md §5; DESIGN §6.6 V4): its frozen plan, its statistics and its verdict.

The plan is frozen before the candidate's first attempt, and the verdict is read off that plan afterwards: thresholds
chosen after the numbers are in are not thresholds.

    A task is one unit, not one attempt. Repeats are averaged first, so a task retried more often does not weigh more.
    A confidence interval is resampled over families: variants of one workflow move together.
    Development acceptance is not significance: a gain that clears min_gain may install while its interval still
    contains zero; the receipt says both, and a report may not upgrade the first into the second.
    Missing cost is missing: under the cost term, unknown cost follows the plan's missing-cost policy, never zero.

The rule, in the order the clauses are judged: integrity (no candidate defect, no evaluator failure, complete pairs,
enough groups), gain ≥ min_gain, regression rate ≤ max_regression_rate (a regression is a drop of more than
regression_drop), cost per attempt ≤ control × (1 + β0 + β1 × gain), and — under a plan with a confirmation reading —
the confirmation reading alone clearing min_gain too. A pooled control reads every valid reading of the installed W
under the plan's conditions and is topped up to the plan's repeats.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..contracts import ContractError, Objects, Record, register
from .release import GateReceipt
from .results import TaskResult, pair_key

MISSING_COST = ("refuse", "report_unknown")
CONTROL_POOLING = ("fresh", "pooled")


@register
@dataclass(frozen=True)
class GatePlan(Record):
    """The thresholds, frozen with the activity (K's `gate`) before the baseline is run.

        min_gain             the paired gain a candidate must reach (> 0: a threshold at zero accepts a tie)
        max_regression_rate  the share of tasks allowed to regress
        regression_drop      a task regressed when its mean fell by more than this; None: any drop counts
        cost_beta0/1         the candidate's cost per attempt may exceed the control's by beta0 + beta1 × gain;
                             None: no cost term
        min_tasks            a plan with fewer tasks is refused at freeze; None: no least count
        partial_min_gain     EVALUATE §5.1: a candidate whose strict gain is below min_gain is still accepted when its
                             partial-credit gain reaches this, with an interval above zero, and its strict gain is
                             not below zero; None: the strict rule alone
    """
    TYPE = "gateplan"
    min_groups: int = 3
    min_gain: float = 0.05
    max_regression_rate: float = 0.2
    confidence_level: float = 0.95
    bootstrap_seed: int = 0
    bootstrap_replicates: int = 2000
    missing_cost_policy: str = "refuse"
    regression_drop: float | None = None
    cost_beta0: float | None = None
    cost_beta1: float | None = None
    min_tasks: int | None = None
    partial_min_gain: float | None = None

    def __post_init__(self) -> None:
        if self.missing_cost_policy not in MISSING_COST:
            raise ContractError(f"missing_cost_policy {self.missing_cost_policy!r}: {list(MISSING_COST)}")
        if not self.min_gain > 0:
            raise ContractError("min_gain must be above zero: a threshold at zero accepts a tie")
        if not 0.5 <= self.confidence_level < 1:
            raise ContractError(f"confidence_level {self.confidence_level!r}")
        if self.regression_drop is not None and not 0 < float(self.regression_drop) < 1:
            raise ContractError(f"regression_drop {self.regression_drop!r}: a drop of a task's mean success, "
                                f"strictly inside (0, 1); leave it out for the strict rule")
        if (self.cost_beta0 is None) != (self.cost_beta1 is None):
            raise ContractError("the cost term takes both cost_beta0 and cost_beta1, or neither")
        if self.cost_beta0 is not None and (float(self.cost_beta0) < 0 or float(self.cost_beta1) < 0):
            raise ContractError("cost_beta0 and cost_beta1 are allowances, not below zero")
        if self.min_tasks is not None and int(self.min_tasks) < 1:
            raise ContractError("min_tasks is a count of tasks, at least 1; leave it out for no least count")
        if self.partial_min_gain is not None and not 0 < float(self.partial_min_gain) < 1:
            raise ContractError("partial_min_gain is a partial-credit gain strictly inside (0, 1); leave it out for "
                                "the strict rule alone")

    @property
    def cost_term(self) -> bool:
        return self.cost_beta0 is not None and self.cost_beta1 is not None

    def to_json(self) -> dict:
        return {k: v for k, v in super().to_json().items() if not (k in OPTIONAL_GATE_FIELDS and v is None)}


OPTIONAL_GATE_FIELDS = ("regression_drop", "cost_beta0", "cost_beta1", "min_tasks", "partial_min_gain")


@register
@dataclass(frozen=True)
class TaskEvalPlan(Record):
    """One frozen comparison: the candidate package against the installed one on these dev tasks, each `repeats`
    times, in an order fixed by `order_seed`. `control_pooling` "pooled" reads the control arm as every valid reading of
    the installed W under this plan's conditions, topped up; `confirmation_repeats` (0 or 1) reads the candidate once
    more before it may install. A final plan (`installs` False) reports and never installs."""
    TYPE = "plan"
    candidate_ref: str = ""
    control_ref: str = ""
    parent_release: str = ""
    core_ref: str = ""
    split_ref: str = ""
    task_ids: tuple = ()
    repeats: int = 1
    order_seed: int = 0
    per_attempt_budget: dict = field(default_factory=dict)
    retry_policy: dict = field(default_factory=dict)
    gate_plan: dict = field(default_factory=dict)
    price_ref: str | None = None
    installs: bool = True
    control_pooling: str | None = None
    confirmation_repeats: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_ids", tuple(self.task_ids))
        if not self.task_ids:
            raise ContractError("a plan names the tasks it runs")
        if self.repeats < 1:
            raise ContractError("repeats is at least 1")
        if self.control_pooling is not None and self.control_pooling not in CONTROL_POOLING:
            raise ContractError(f"control_pooling {self.control_pooling!r}: one of {list(CONTROL_POOLING)}")
        if self.confirmation_repeats is not None and int(self.confirmation_repeats) not in (0, 1):
            raise ContractError(f"confirmation_repeats {self.confirmation_repeats!r}: 0 or 1 — one confirmation "
                                f"reading")

    def gate(self) -> GatePlan:
        return GatePlan.from_json(self.gate_plan)

    @property
    def pooled(self) -> bool:
        return self.control_pooling == "pooled"

    @property
    def confirmations(self) -> int:
        return int(self.confirmation_repeats or 0)

    def conditions(self) -> dict:
        """What makes two readings of one W readings under the same comparison (M3 §2.1(a), v0.3): the task list,
        the repeats, the order seed, the per-attempt limits, the split and the core. The candidate, the control and
        the parent are left out — they are what the plans of two gates of one activity differ by."""
        return {"task_ids": list(self.task_ids), "repeats": self.repeats, "order_seed": self.order_seed,
                "per_attempt_budget": dict(self.per_attempt_budget), "split_ref": self.split_ref,
                "core_ref": self.core_ref}

    def order(self, arms: tuple[str, str] = ("subject", "control")) -> list[tuple[str, int, str]]:
        """The execution order: every (task, repeat) for both arms, with the arm order alternating on the plan's
        seed. Fixed here so both sides meet the same conditions in a comparable sequence, and so a rerun of the
        plan runs the same sequence."""
        rng = random.Random(self.order_seed)
        out = []
        for task_id in self.task_ids:
            for repeat in range(self.repeats):
                first, second = (arms if rng.random() < 0.5 else arms[::-1])
                out.append((task_id, repeat, first))
                out.append((task_id, repeat, second))
        return out


# ---------------------------------------------------------------------------- aggregation
def per_task(results: list[TaskResult], *, metric: str = "success") -> dict[str, float]:
    """Task -> the mean of its valid attempts. Repeats are averaged before anything else."""
    by_task: dict[str, list[float]] = {}
    for r in results:
        if r.validity != "valid":
            continue
        value = float(getattr(r, metric)) if metric != "success" else float(bool(r.success))
        by_task.setdefault(r.task_id, []).append(value)
    return {t: sum(v) / len(v) for t, v in by_task.items()}


def pass_rate(results: list[TaskResult]) -> float | None:
    tasks = per_task(results)
    return round(sum(tasks.values()) / len(tasks), 6) if tasks else None


def per_task_cost(results: list[TaskResult]) -> tuple[dict[str, float], int]:
    """Task -> mean measured cost, and how many valid attempts had none. Unknown cost is never averaged in as
    zero (benchmark.summarise makes the same choice)."""
    by_task: dict[str, list[float]] = {}
    missing = 0
    for r in results:
        if r.validity != "valid":
            continue
        if r.cost_usd is None:
            missing += 1
            continue
        by_task.setdefault(r.task_id, []).append(float(r.cost_usd))
    return {t: sum(v) / len(v) for t, v in by_task.items()}, missing


def cost_per_attempt(results: list[TaskResult]) -> tuple[float | None, int]:
    """The mean measured cost of one valid attempt, and how many valid attempts had none (M5 §2 每次尝试成本进收据,
    v0.3). None when no valid attempt was priced: unknown, not free. The receipt and the report both read this
    one number under this one name."""
    valid = [r for r in results if r.validity == "valid"]
    costed = [float(r.cost_usd) for r in valid if r.cost_usd is not None]
    return (round(sum(costed) / len(costed), 6) if costed else None), len(valid) - len(costed)


def regressed(subject: dict[str, float], control: dict[str, float], drop: float | None = None) -> list[str]:
    """The tasks that regressed, sorted by id: the numerator of regression_rate, so the list a receipt shows and
    the rate its verdict used cannot disagree.

    With `drop` (M3 §2.1(d), v0.3) a task regressed when its mean fell by more than that — at one repeat, control
    1.0 → candidate 0.0; at two, 1.0 → 0.5 is not a regression and 1.0 → 0.0 is (ER6-19) — because "any drop" fired
    in a fifth of same-version comparisons and fired more often the more repeats were added. Without it (a plan
    frozen without the field) any drop counts, as before."""
    if drop is None:
        return sorted(t for t in set(subject) & set(control) if subject[t] < control[t] - 1e-9)
    return sorted(t for t in set(subject) & set(control) if control[t] - subject[t] > float(drop) + 1e-9)


def regression_rate(subject: dict[str, float], control: dict[str, float], drop: float | None = None) -> float:
    """The share of tasks that regressed (`regressed`). One definition, used by every verdict of this plan."""
    shared = set(subject) & set(control)
    if not shared:
        return 0.0
    return round(len(regressed(subject, control, drop)) / len(shared), 6)


def paired_point(subject: dict[str, float], control: dict[str, float]) -> float | None:
    """The paired difference over the tasks both sides have, with equal weight per task; None with none shared."""
    shared = sorted(set(subject) & set(control))
    return round(sum(subject[t] - control[t] for t in shared) / len(shared), 6) if shared else None


def paired_bootstrap(subject: dict[str, float], control: dict[str, float], families: dict[str, str], *,
                     seed: int = 0, replicates: int = 2000, confidence: float = 0.95) -> dict:
    """The paired difference and its interval, resampled over families.

    A drawn family brings all of its tasks, and the difference is recomputed with equal weight per task. Families
    are the unit because variants of one workflow are not independent evidence: resampling tasks or trajectories
    would report an interval the data does not support.
    """
    shared = sorted(set(subject) & set(control))
    by_family: dict[str, list[str]] = {}
    for t in shared:
        by_family.setdefault(families.get(t, t), []).append(t)
    groups = sorted(by_family)
    point = (sum(subject[t] - control[t] for t in shared) / len(shared)) if shared else None
    if not groups or point is None:
        return {"point": point, "low": None, "high": None, "groups": len(groups), "tasks": len(shared),
                "replicates": 0, "confidence": confidence, "reliable": False}
    rng = random.Random(seed)
    diffs = []
    for _ in range(replicates):
        drawn = [by_family[groups[rng.randrange(len(groups))]] for _ in groups]
        tasks = [t for group in drawn for t in group]
        diffs.append(sum(subject[t] - control[t] for t in tasks) / len(tasks))
    diffs.sort()
    alpha = (1 - confidence) / 2
    low = diffs[max(int(alpha * len(diffs)) - 1, 0)]
    high = diffs[min(int((1 - alpha) * len(diffs)), len(diffs) - 1)]
    return {"point": round(point, 6), "low": round(low, 6), "high": round(high, 6), "groups": len(groups),
            "tasks": len(shared), "replicates": replicates, "confidence": confidence,
            "reliable": len(groups) >= 3,
            # "the interval excludes zero" is a statement about this comparison only, and an adaptive
            # development search makes even that weaker than it looks (EVALUATE §5).
            "excludes_zero": bool(low > 0 or high < 0)}


# ---------------------------------------------------------------------------- integrity and verdict
def integrity(plan: TaskEvalPlan, subject: list[TaskResult], control: list[TaskResult],
              families: dict[str, str], *, control_pooled: bool = False,
              confirmation: list[TaskResult] | None = None) -> tuple[str, list[str]]:
    """What makes this comparison unusable, before any number is compared. -> ("", []) when it is usable.

    A pooled control (M3 §2.1(a), v0.3) was not read under this plan's pair keys — its readings come from earlier
    gates and the baseline — so its completeness is counted per task: at least `repeats` valid readings of every
    task, else that task is missing and the comparison inconclusive. The candidate's readings, the confirmation
    one included (b), still have to be complete under this plan's own keys.
    """
    reasons: list[str] = []
    candidate = [*subject, *(confirmation or ())]
    defects = [r for r in candidate if r.failure_class == "candidate_defect"]
    if defects:
        return "invalid", [f"candidate defect on {r.task_id}: {r.invalid_reason}" for r in defects[:5]]
    evaluator = [r for r in (*candidate, *control) if r.failure_class == "evaluator"]
    if evaluator:
        return "inconclusive", [f"evaluator failure on {r.task_id}: {r.invalid_reason}" for r in evaluator[:5]]
    exhausted = [r for r in (*candidate, *control) if r.failure_class == "budget_exhausted"]
    if exhausted:
        # The total budget ran out before the comparison finished: whatever the partial numbers say, they are the
        # numbers of a comparison nobody planned (EVALUATE §9).
        return "inconclusive", [f"budget_exhausted after {len(exhausted)} attempts could not run"]
    expected = {pair_key(plan.ref(), t, i) for t in plan.task_ids for i in range(plan.repeats)}
    have_subject = {r.pair_key for r in subject if r.validity == "valid"}
    missing = expected - have_subject
    if control_pooled:
        counts: dict[str, int] = {}
        for r in control:
            if r.validity == "valid":
                counts[r.task_id] = counts.get(r.task_id, 0) + 1
        short = sorted(t for t in plan.task_ids if counts.get(t, 0) < plan.repeats)
        if short:
            reasons.append(f"{len(short)} tasks have fewer than {plan.repeats} pooled control readings: {short[:5]}")
    else:
        missing |= expected - {r.pair_key for r in control if r.validity == "valid"}
    if missing:
        reasons.append(f"{len(missing)} pairs are incomplete: {[m.split('/', 1)[-1] for m in sorted(missing)[:5]]}")
    if confirmation is not None:
        unconfirmed = sorted(expected - {r.pair_key for r in confirmation if r.validity == "valid"})
        if unconfirmed:
            reasons.append(f"the confirmation reading is incomplete on {len(unconfirmed)} pairs: "
                           f"{[m.split('/', 1)[-1] for m in unconfirmed[:5]]}")
    groups = len({families.get(r.task_id, r.task_id) for r in subject if r.validity == "valid"})
    gate = plan.gate()
    if groups < gate.min_groups:
        reasons.append(f"{groups} independent groups, the plan needs {gate.min_groups}")
    return ("inconclusive", reasons) if reasons else ("", [])


def judge(plan: TaskEvalPlan, subject: list[TaskResult], control: list[TaskResult], families: dict[str, str],
          *, control_pooled: bool = False, confirmation: list[TaskResult] | None = None,
          first: dict | None = None) -> tuple[str, list[str], dict, dict]:
    """-> (verdict, reasons, metrics, uncertainty), read off the frozen plan.

    `control_pooled` says the control rows are the installed version's pooled readings (M3 §2.1(a), v0.3): the
    receipt then lists how many readings and which attempts, so a reviewer can recompute it. `confirmation` is the
    candidate's confirmation reading (b, second version of the rule, 2026-10-04): the candidate's per-task means —
    the gain, the regressions, the cost — are taken over both readings, and the verdict is accepted only if the
    confirmation reading alone also clears `min_gain` against the same control, so a candidate installs only when it
    clears the gate twice on two independent readings. (The first version asked the confirmation to be ≥ 0, which a
    second reading of an unchanged version meets by expectation: the A/A replay accepted 4 of 20 same-version
    pairs.) `first` ({gain, verdict}) is kept on the receipt as `first_reading`.

    The clauses are judged in the order M3 §2.1(h) fixes — integrity, gain, regression, cost, the confirmation —
    and every clause that fails is written down, not only the first.
    """
    gate = plan.gate()
    verdict, reasons = integrity(plan, subject, control, families, control_pooled=control_pooled,
                                 confirmation=confirmation)
    candidate = [*subject, *(confirmation or ())]
    s_tasks, c_tasks = per_task(candidate), per_task(control)
    s_cost, s_missing = per_task_cost(candidate)
    c_cost, c_missing = per_task_cost(control)
    quality = paired_bootstrap(s_tasks, c_tasks, families, seed=gate.bootstrap_seed,
                               replicates=gate.bootstrap_replicates, confidence=gate.confidence_level)
    cost = paired_bootstrap(s_cost, c_cost, families, seed=gate.bootstrap_seed,
                            replicates=gate.bootstrap_replicates, confidence=gate.confidence_level)
    s_partial, c_partial = per_task(candidate, metric="partial_credit"), per_task(control, metric="partial_credit")
    partial = paired_bootstrap(s_partial, c_partial, families, seed=gate.bootstrap_seed,
                               replicates=gate.bootstrap_replicates, confidence=gate.confidence_level)
    regressions = regression_rate(s_tasks, c_tasks, gate.regression_drop)
    s_attempt, s_unpriced = cost_per_attempt(candidate)
    c_attempt, c_unpriced = cost_per_attempt(control)
    metrics = {"subject_pass_rate": pass_rate(candidate), "control_pass_rate": pass_rate(control),
               "subject_partial_credit": (round(sum(per_task(candidate, metric="partial_credit").values())
                                                / max(len(s_tasks), 1), 6) if s_tasks else None),
               "control_partial_credit": (round(sum(per_task(control, metric="partial_credit").values())
                                                / max(len(c_tasks), 1), 6) if c_tasks else None),
               "gain": quality["point"], "regression_rate": regressions,
               # EVALUATE §5.1: the partial-credit gain beside the strict one, whichever rule the plan judges by
               "partial_gain": partial["point"], "partial_interval": [partial.get("low"), partial.get("high")],
               "accepted_by": None,
               # Each task the rate counted, with both arms' means: a regression within the frozen limit stays on
               # record, not only in a count (ER6-13). These are dev task ids, so the experience log leaves the
               # key out and no step of R reads it (IMPROVER §11).
               "regressions": [{"task": t, "family": families.get(t, t), "control": round(c_tasks[t], 6),
                                "subject": round(s_tasks[t], 6)}
                               for t in regressed(s_tasks, c_tasks, gate.regression_drop)],
               "tasks": len(s_tasks),
               "groups": quality["groups"], "subject_cost_usd": round(sum(s_cost.values()), 6) or None,
               "control_cost_usd": round(sum(c_cost.values()), 6) or None,
               "cost_missing": {"subject": s_missing, "control": c_missing},
               # M5 §2 (v0.3): the one number the cost term judges and the report lists; `allowed` once computed.
               "cost_per_attempt": {"subject": s_attempt, "control": c_attempt, "allowed": None},
               "invalid": {"subject": sum(r.validity == "invalid" for r in candidate),
                           "control": sum(r.validity == "invalid" for r in control)},
               # M3 §2.1(a): how many readings the control arm pools (one per run) and which attempts they are.
               "control_readings": len({r.run for r in control if r.validity == "valid"}),
               "control_attempt_refs": sorted(r.attempt_id for r in control if r.validity == "valid" and r.attempt_id),
               "readings": 1 + plan.confirmations if confirmation is not None else 1}
    confirmation_gain = confirmation_partial = None
    if confirmation is not None:
        confirmation_gain = paired_point(per_task(confirmation), c_tasks)
        confirmation_partial = paired_point(per_task(confirmation, metric="partial_credit"), c_partial)
        metrics["confirmation_gain"] = confirmation_gain
        metrics["confirmation_partial_gain"] = confirmation_partial
        metrics["first_reading"] = dict(first or {})
    uncertainty = {"quality": quality, "cost": cost, "partial": partial,
                   # Said plainly so nothing downstream has to infer it (ER-04).
                   "significant": bool(quality.get("excludes_zero") and quality.get("reliable")),
                   "note": "a development interval describes this comparison; a formal claim needs the locked "
                           "final plan"}
    if verdict:
        return verdict, reasons, metrics, uncertainty
    notes: list[str] = []
    # EVALUATE §5.1: the strict rule, or — when the plan sets partial_min_gain — the partial-credit rule, each with its
    # own confirmation clause; whichever the candidate clears names the acceptance
    strict_ok = quality["point"] >= gate.min_gain and (
        confirmation is None or (confirmation_gain is not None and confirmation_gain >= gate.min_gain))
    partial_ok = gate.partial_min_gain is not None and partial["point"] is not None \
        and partial["point"] >= float(gate.partial_min_gain) and bool(partial.get("excludes_zero")) \
        and bool(partial.get("reliable")) and quality["point"] >= 0 and (
            confirmation is None or (confirmation_partial is not None
                                     and confirmation_partial >= float(gate.partial_min_gain)))
    if quality["point"] < gate.min_gain and not partial_ok:
        reasons.append(f"gain {quality['point']} below the plan's min_gain {gate.min_gain}"
                       + (f"; partial-credit gain {partial['point']} {metrics['partial_interval']} does not clear "
                          f"partial_min_gain {gate.partial_min_gain} with an interval above zero"
                          + (" and a strict gain of at least zero" if quality["point"] < 0 else "")
                          if gate.partial_min_gain is not None else ""))
    if regressions > gate.max_regression_rate:
        reasons.append(f"regression rate {regressions} above {gate.max_regression_rate}")
    if gate.cost_term:
        # M3 §2.1(e): cost_subject ≤ cost_control × (1 + β0 + β1 × gain), gain as a fraction. A dearer candidate
        # that is not better is refused; one that is dearer and better passes in proportion. Unknown cost on
        # any valid attempt follows the plan's missing-cost policy: never read as zero.
        if s_attempt is None or c_attempt is None or s_unpriced or c_unpriced:
            why = (f"cost per attempt is not measured on every valid attempt (unpriced: subject {s_unpriced}, "
                   f"control {c_unpriced})")
            if gate.missing_cost_policy == "refuse":
                return "inconclusive", [*reasons, f"{why} and this plan refuses unknown cost"], metrics, uncertainty
            notes.append(f"{why}: the cost term was not judged (missing_cost_policy report_unknown)")
        else:
            allowed = round(c_attempt * (1 + float(gate.cost_beta0) + float(gate.cost_beta1) * quality["point"]), 6)
            metrics["cost_per_attempt"]["allowed"] = allowed
            if s_attempt > allowed + 1e-9:
                reasons.append(f"cost per attempt {s_attempt} above the allowed {allowed} (control {c_attempt} × "
                               f"(1 + β0 {gate.cost_beta0} + β1 {gate.cost_beta1} × gain {quality['point']}))")
    if confirmation is not None and not strict_ok and not partial_ok \
            and (confirmation_gain is None or confirmation_gain < gate.min_gain):
        # M3 §2.1(b), second version: the confirmation reading on its own clears min_gain too, whatever the
        # pooled estimate says. A second reading of an unchanged version sits at 0 against the pooled control
        # by expectation, so "≥ 0" constrained nothing (the replay accepted 4/20 same-version pairs); two
        # independent readings each at ≥ +0.05 happen by chance about once in twenty.
        reasons.append(f"the confirmation reading alone has gain {confirmation_gain}, below the plan's "
                       f"min_gain {gate.min_gain}"
                       + (f" (and its partial-credit gain {confirmation_partial} is below partial_min_gain "
                          f"{gate.partial_min_gain})" if gate.partial_min_gain is not None else ""))
    if reasons:
        return "rejected", [*reasons, *notes], metrics, uncertainty
    metrics["accepted_by"] = "strict" if strict_ok else "partial"
    if not strict_ok:
        notes.append(f"accepted by partial credit: gain {partial['point']} {metrics['partial_interval']} clears "
                     f"partial_min_gain {gate.partial_min_gain} with an interval above zero, the strict gain "
                     f"{quality['point']} is not below zero")
    return "accepted", ["development acceptance: the gain clears the plan's threshold"
                        + (" on the first reading, on the confirmation reading alone and on the pool of both"
                           if confirmation is not None else "")
                        + ("" if uncertainty["significant"] else "; the interval still contains zero, so "
                                                                "this is not a significant improvement"),
                        *notes], metrics, uncertainty


# ---------------------------------------------------------------------------- the evaluation
def refill_attempts(rows: list, run, arm: str, limit: int) -> list:
    """Re-run the attempts that failed for infrastructure reasons, at most `limit` times per pair.

    Only those: a task the agent legitimately failed is evidence and is never re-rolled, a candidate defect is the
    candidate's, and an exhausted budget ends the comparison (EVALUATE §5). The
    original attempt and its cost stay on record, and each refill is written with the id of the attempt it
    supersedes — a comparison that deleted its failures would be describing a different run.

    `run(arm, order, retry, supersedes)` runs `order` at retry index `retry`, where `supersedes` maps each
    (task, repeat) to the attempt it replaces.
    """
    out = list(rows)
    for attempt_index in range(1, limit + 1):
        done = {(r.task_id, r.attempt) for r in out if r.validity == "valid"}
        latest: dict = {}
        for r in out:
            if r.failure_class == "infrastructure" and (r.task_id, r.attempt) not in done:
                latest[(r.task_id, r.attempt)] = r
        if not latest:
            break
        order = sorted(latest)
        out.extend(run(arm, order, attempt_index, {key: latest[key].attempt_id for key in order}))
    return out


def refill(plan: TaskEvalPlan, arm: str, rows: list, run) -> list:
    """refill_attempts under the plan's frozen retry policy."""
    return refill_attempts(rows, run, arm, int((plan.retry_policy or {}).get("max_infrastructure_retries", 0)))


def receipt_differences(receipt: GateReceipt, objects: Objects) -> dict[str, list[str]]:
    """The gate's three per-task classes read off a receipt's own results object (M4 §5 引擎自有的实验台账, v0.3),
    nothing recomputed from fresh data: `fixed` — the candidate's mean success on the task is above the (pooled)
    control's; `regressed` — below it by more than the plan's `regression_drop` (the tasks `regression_rate`
    counted, M3 §2.1(d)); `still_failing` — both arms' means are 0. The candidate's rows are the subject reading and
    its confirmation readings together, as `judge` read them. Dev task ids: the ledger keeps them; R's projection
    and an arm's E0 see counts."""
    results = objects.get(receipt.results_ref)
    attempts = results.get("attempts") or {}
    candidate = [TaskResult.from_json(r) for arm, rows in attempts.items() for r in rows
                 if arm == "subject" or arm.startswith("confirm")]
    control = [TaskResult.from_json(r) for r in attempts.get("control") or ()]
    s_tasks, c_tasks = per_task(candidate), per_task(control)
    plan = TaskEvalPlan.from_json(objects.get(receipt.plan_ref)) if receipt.plan_ref and objects.has(receipt.plan_ref) \
        else None
    drop = plan.gate().regression_drop if plan is not None and plan.gate_plan else None
    shared = sorted(set(s_tasks) & set(c_tasks))
    return {"fixed": [t for t in shared if s_tasks[t] > c_tasks[t] + 1e-9],
            "regressed": regressed(s_tasks, c_tasks, drop),
            "still_failing": [t for t in shared if s_tasks[t] < 1e-9 and c_tasks[t] < 1e-9]}


def confirmation_arm(index: int) -> str:
    """The arm name of the index-th confirmation reading (M3 §2.1(b), v0.3): `confirm`, `confirm1`, ... — its own
    arm, so its attempt ids are never the first reading's and a resume finds exactly the reading it had started."""
    return "confirm" if index == 0 else f"confirm{index}"


def shortfall(plan: TaskEvalPlan, control_pool: list | None) -> list[tuple[str, int]]:
    """The (task, repeat) pairs the pooled control is short of (M3 §2.1(a), v0.3): for each task of the plan, as
    many repeats as the pool has fewer valid readings than `plan.repeats`, at repeat indices no pooled reading of
    that task carries — so a resume of this same run never asks for a pair it already recorded (run_arm would hand
    the recorded row back and the pool would count it twice) and reads the missing index instead. The control arm
    reads exactly these — an empty pool is the whole plan, a complete pool nothing — so a pool one broken baseline
    attempt or an interrupted control arm left short is topped up rather than read as missing at every later gate."""
    taken: dict[str, set[int]] = {t: set() for t in plan.task_ids}
    counts: dict[str, int] = {t: 0 for t in plan.task_ids}
    for r in control_pool or ():
        if r.validity == "valid" and r.task_id in counts:
            counts[r.task_id] += 1
            taken[r.task_id].add(int(getattr(r, "attempt", 0) or 0))
    out: list[tuple[str, int]] = []
    for t in plan.task_ids:
        free = [i for i in range(plan.repeats) if i not in taken[t]]
        # fewer rows than repeats means at least as many free indices as readings missing
        out.extend((t, i) for i in free[:max(0, plan.repeats - counts[t])])
    return out


def evaluate_task(plan: TaskEvalPlan, *, run, objects: Objects, families: dict[str, str], results_sink=None,
                  control_pool: list | None = None, confirming=None) -> GateReceipt:
    """Run both arms of a frozen plan and file the receipt.

    `run(arm, order, retry=0, supersedes=None) -> list[TaskResult]` belongs to the caller: this module fixes the
    order, the statistics and the verdict, and the runner belongs to whoever owns the environment. Every attempt
    and its cost go into the receipt's results object, including the failures — a receipt that kept only the
    attempts that worked would be describing a different run.

    v0.3 (M3 §2.1(a)(b)): `control_pool` is every valid reading of the installed version the caller found under
    this plan's conditions. Under a pooled plan the pool is the control's starting point, topped up, never
    either/or: the control arm reads exactly the pairs the pool is short of (`shortfall` — an empty pool is the
    whole plan, a complete pool nothing), through the same refill and the same run identity as any arm, so a
    resume finds what an interrupted top-up had recorded; the receipt says how many readings the control pools
    (`control_readings`, `control_attempt_refs`) and the results object that the control was pooled. A first
    reading that is accepted under a plan with `confirmation_repeats` is not the verdict: `confirming(first)` is
    told (the state machine's provisional `evaluated` → `confirming`), the candidate arm is read once more under
    the same plan, and the receipt — kind `confirmed` whether it then passed or not — accepts only when the
    confirmation reading alone clears `min_gain` too (the pooled readings are what the regressions, the cost and
    the recorded gain are read on; M3 §2.1(b), second version) and keeps the first reading's numbers. A first
    reading that was not accepted is not confirmed: kind `full`, the verdict as read.
    """
    plan_ref = objects.put(plan)
    order = plan.order()
    rows: dict[str, list] = {"subject": [], "control": []}
    arm_orders = {arm: [(task, repeat) for task, repeat, side in order if side == arm] for arm in rows}
    pooled = plan.pooled
    rows["subject"] = refill(plan, "subject", list(run("subject", arm_orders["subject"], 0, None)), run)
    pool = list(control_pool or ()) if pooled else []
    # the pairs the control arm reads: what the pool lacks under a pooled plan, the whole plan otherwise
    short = shortfall(plan, pool) if pooled else arm_orders["control"]
    topped_up = refill(plan, "control", list(run("control", short, 0, None)), run) if short else []
    rows["control"] = pool + topped_up
    if results_sink is not None:
        for r in rows["subject"]:
            results_sink("subject", r)
        for r in topped_up:
            results_sink("control", r)
    verdict, reasons, metrics, uncertainty = judge(plan, rows["subject"], rows["control"], families,
                                                   control_pooled=pooled)
    kind = "full"
    if plan.confirmations > 0 and verdict == "accepted":
        first = {"gain": metrics["gain"], "verdict": verdict}
        if confirming is not None:
            confirming(first)
        confirmation: list = []
        for index in range(plan.confirmations):
            arm = confirmation_arm(index)
            rows[arm] = refill(plan, arm, list(run(arm, arm_orders["subject"], 0, None)), run)
            if results_sink is not None:
                for r in rows[arm]:
                    results_sink(arm, r)
            confirmation.extend(rows[arm])
        verdict, reasons, metrics, uncertainty = judge(plan, rows["subject"], rows["control"], families,
                                                       control_pooled=pooled, confirmation=confirmation,
                                                       first=first)
        kind = "confirmed"
    results_ref = objects.put_json("gate_results", {
        "plan": plan_ref, "attempts": {arm: [r.to_json() for r in rows[arm]] for arm in rows},
        "order": [[t, i, a] for t, i, a in order], "control_pooled": pooled})
    return GateReceipt(plan_ref=plan_ref, subject_ref=plan.candidate_ref, control_ref=plan.control_ref,
                       parent_release=plan.parent_release, results_ref=results_ref, verdict=verdict,
                       reasons=tuple(reasons), metrics=metrics, uncertainty=uncertainty, kind=kind,
                       costs={"subject_usd": metrics["subject_cost_usd"], "control_usd": metrics["control_cost_usd"],
                              "missing": metrics["cost_missing"]})


def transport_problem(activity) -> str | None:
    """A comparison across readings (a held-out screen, the release gate) holds only on one transport: the claude CLI
    on PATH must be the one the activity recorded at its first paid phase (the `cli_version` event). None when it is,
    or when the activity runs a test provider."""
    rows = list(activity.events.read("cli_version"))
    if not rows or getattr(activity, "provider_factory", None) is not None:
        return None
    from ..providers.claude_code import cli_version
    now = cli_version()
    if now != rows[0].get("version"):
        return (f"cli: the claude CLI is {now or 'unreadable'} and this activity ran on {rows[0].get('version')}: no "
                f"comparison on mixed transports (reinstall it, with DISABLE_AUTOUPDATER=1)")
    return None
