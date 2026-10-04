"""M3: the paired full-task evaluation, its statistics and its verdict
(docs/modules/03_EVALUATION_RELEASE.md).

The plan is frozen before the candidate's first attempt, and the verdict is read off that plan afterwards. That
order is the whole point: thresholds chosen after the numbers are in are not thresholds.

What the module is careful about:

    A task is one unit, not one attempt. Repeats of a task are averaged first, so a task that happened to be
    retried more often does not weigh more, and a trajectory is never treated as an independent sample.
    A confidence interval is resampled over families. Variants of one workflow move together, so resampling
    trajectories would report an interval far narrower than the evidence supports.
    Development acceptance is not significance. A small positive gain that clears the plan's threshold may be
    accepted as the next version while the interval still contains zero; the receipt says both, and a report may
    not upgrade the first into the second (ER-04).
    Missing cost is missing. An economy verdict on unknown cost is inconclusive, never "cheaper" (ER-05).

v0.3 (docs/modules/03_EVALUATION_RELEASE.md §2.1, 2026-10-04) adds to the same `judge`, read off the same frozen
plan: a pooled control (a), a confirmation reading before an install that has to clear the gate on its own (b,
second version of the rule), a regression that is a drop of a magnitude rather than any drop (d), a cost term
beside the gain (e) and a least task count at freeze (f). Every
new field is None when a plan was frozen without it, and `Record.payload` leaves None out, so a plan frozen before
these fields existed keeps its ref and is judged as it was; the v3 preset carries the design's values.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..benchmark import TaskResult, pair_key
from . import contracts as ct

MODES = ("quality", "economy")
MISSING_COST = ("refuse", "report_unknown")
CONTROL_POOLING = ("fresh", "pooled")
# The two kinds of a task receipt (M3 §2.1(b), v0.3): one complete paired reading, or one the confirmation reading
# followed — whether it then passed or not. A receipt written before the field existed reads as "full".
RECEIPT_KINDS = ("full", "confirmed")


@dataclass
class GatePlan(ct.Record):
    """The thresholds, fixed by whoever starts the experiment before the baseline is run.

    v0.3 fields, each None in a plan frozen without it (today's rule), set by the v3 preset (M3 §2.3):
        regression_drop   (d) a task regressed when its mean fell by at least this much; None: any drop counts
        cost_beta0/1      (e) the candidate's cost per attempt may exceed the control's by beta0 + beta1 × gain;
                              None: no cost term
        min_tasks         (f) a gate plan with fewer tasks is refused at freeze; None: no least count
    """
    TYPE = "gateplan"
    mode: str = "quality"
    min_groups: int = 3
    min_gain: float = 0.05
    max_quality_loss: float = 0.02
    max_regression_rate: float = 0.2
    max_runtime_cost_ratio: float | None = None
    confidence_level: float = 0.95
    bootstrap_seed: int = 0
    bootstrap_replicates: int = 2000
    protected_requirements: tuple[str, ...] = ()
    missing_cost_policy: str = "refuse"
    regression_drop: float | None = None
    cost_beta0: float | None = None
    cost_beta1: float | None = None
    min_tasks: int | None = None
    schema_version: int = ct.SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ct.ContractError(f"gate mode {self.mode!r}: one of {list(MODES)}")
        if self.missing_cost_policy not in MISSING_COST:
            raise ct.ContractError(f"missing_cost_policy {self.missing_cost_policy!r}: {list(MISSING_COST)}")
        if self.mode == "quality" and not self.min_gain > 0:
            raise ct.ContractError("min_gain must be above zero: a threshold at zero accepts a tie")
        if self.mode == "economy" and self.max_runtime_cost_ratio is None:
            raise ct.ContractError("an economy gate needs a measurable cost ratio to judge")
        if not 0.5 <= self.confidence_level < 1:
            raise ct.ContractError(f"confidence_level {self.confidence_level!r}")
        if self.regression_drop is not None and not 0 < float(self.regression_drop) < 1:
            # A drop has to exceed it (ER6-19), and no mean can drop by more than 1: at 1.0 no task could ever regress.
            raise ct.ContractError(f"regression_drop {self.regression_drop!r}: a drop of a task's mean success, "
                                   f"strictly inside (0, 1); leave it out for the strict rule")
        if (self.cost_beta0 is None) != (self.cost_beta1 is None):
            raise ct.ContractError("the cost term takes both cost_beta0 and cost_beta1, or neither")
        if self.cost_beta0 is not None and (float(self.cost_beta0) < 0 or float(self.cost_beta1) < 0):
            raise ct.ContractError("cost_beta0 and cost_beta1 are allowances, not below zero")
        if self.min_tasks is not None and int(self.min_tasks) < 1:
            raise ct.ContractError("min_tasks is a count of tasks, at least 1; leave it out for no least count")

    @property
    def cost_term(self) -> bool:
        """Whether this gate judges cost per attempt beside the gain (M3 §2.1(e), v0.3)."""
        return self.cost_beta0 is not None and self.cost_beta1 is not None

    def to_json(self) -> dict:
        """A v0.3 field that is unset is left out, as `payload` leaves it out of the ref: a task plan embeds this
        JSON whole (`gate_plan`), so a gate frozen without the fields keeps the plan ref it had before they existed."""
        return {k: v for k, v in super().to_json().items() if not (k in V03_GATE_FIELDS and v is None)}


V03_GATE_FIELDS = ("regression_drop", "cost_beta0", "cost_beta1", "min_tasks")


@dataclass
class TaskEvalPlan(ct.Record):
    """One frozen comparison. `task_ids` are development tasks; a final plan reports and never installs.

    v0.3 (M3 §2.1(a)(b)): `control_pooling` "pooled" reads the control arm as every valid reading of the installed
    W under this plan's conditions and model identity (None or "fresh": a fresh control reading every gate, as
    before); `confirmation_repeats` is how many more candidate readings an accepted first reading is confirmed by
    before it may install (None or 0: none). Both are None in a plan frozen without them, so its ref is unchanged.
    """
    TYPE = "plan"
    profile: str = "task"
    candidate_ref: str = ""
    control_ref: str = ""
    parent_release: str = ""
    core_ref: str = ""
    split_ref: str = ""
    task_ids: tuple[str, ...] = ()
    repeats: int = 1
    order_seed: int = 0
    per_attempt_budget: dict = field(default_factory=dict)
    retry_policy: dict = field(default_factory=dict)
    screen_ids: tuple[str, ...] = ()
    gate_plan: dict = field(default_factory=dict)
    price_ref: str | None = None
    cache_policy: str = "identity_only"
    installs: bool = True                     # a final plan sets this false: it reports, it does not promote
    control_pooling: str | None = None
    confirmation_repeats: int | None = None
    schema_version: int = ct.SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.profile not in ct.PROFILES:
            raise ct.ContractError(f"profile {self.profile!r}")
        if not self.task_ids:
            raise ct.ContractError("a plan names the tasks it runs")
        if self.repeats < 1:
            raise ct.ContractError("repeats is at least 1")
        unknown = sorted(set(self.screen_ids) - set(self.task_ids))
        if unknown:
            raise ct.ContractError(f"screen tasks outside the plan: {unknown}")
        if self.control_pooling is not None and self.control_pooling not in CONTROL_POOLING:
            raise ct.ContractError(f"control_pooling {self.control_pooling!r}: one of {list(CONTROL_POOLING)}")
        if self.confirmation_repeats is not None and int(self.confirmation_repeats) not in (0, 1):
            # M3 §2.1(b): one confirmation reading. The rule is two independent readings each clearing the gate; a
            # third reading is not part of it, and would only be paid for.
            raise ct.ContractError(f"confirmation_repeats {self.confirmation_repeats!r}: 0 or 1 — one confirmation "
                                   f"reading (M3 §2.1(b))")
        gate = GatePlan.from_json(self.gate_plan) if self.gate_plan else None
        if gate is not None and gate.mode == "economy" and self.confirmations > 0:
            # The confirmation clause lives in the quality verdict: an economy plan would pay for a reading that
            # constrains nothing and still write a receipt of kind "confirmed".
            raise ct.ContractError("confirmation_repeats with an economy gate: the confirmation reading is a clause "
                                   "of the quality verdict (M3 §2.1(b))")

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
            # development search makes even that weaker than it looks (docs/modules/03_EVALUATION_RELEASE.md §1).
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
        # numbers of a comparison nobody planned (docs/modules/05_EXECUTION_BUDGET.md §1).
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
          *, protected: dict | None = None, control_pooled: bool = False,
          confirmation: list[TaskResult] | None = None, first: dict | None = None) -> tuple[str, list[str], dict, dict]:
    """-> (verdict, reasons, metrics, uncertainty), read off the frozen plan.

    `protected` maps a protected requirement id to whether it still holds; a requirement that regressed blocks
    acceptance whatever the averages say.

    `control_pooled` says the control rows are the installed version's pooled readings (M3 §2.1(a), v0.3): the
    receipt then lists how many readings and which attempts, so a reviewer can recompute it. `confirmation` is the
    candidate's confirmation reading (b, second version of the rule, 2026-10-04): the candidate's per-task means —
    the gain, the regressions, the cost — are taken over both readings, and the verdict is accepted only if the
    confirmation reading alone also clears `min_gain` against the same control, so a candidate installs only when it
    clears the gate twice on two independent readings. (The first version asked the confirmation to be ≥ 0, which a
    second reading of an unchanged version meets by expectation: the A/A replay accepted 4 of 20 same-version
    pairs.) `first` ({gain, verdict}) is kept on the receipt as `first_reading`.

    The clauses are judged in the order M3 §2.1(h) fixes — integrity, protected requirements, gain, regression,
    cost, the plan's other conditions, the confirmation — and every clause that fails is written down, not only
    the first.
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
    regressions = regression_rate(s_tasks, c_tasks, gate.regression_drop)
    s_attempt, s_unpriced = cost_per_attempt(candidate)
    c_attempt, c_unpriced = cost_per_attempt(control)
    metrics = {"subject_pass_rate": pass_rate(candidate), "control_pass_rate": pass_rate(control),
               "subject_partial_credit": (round(sum(per_task(candidate, metric="partial_credit").values())
                                                / max(len(s_tasks), 1), 6) if s_tasks else None),
               "control_partial_credit": (round(sum(per_task(control, metric="partial_credit").values())
                                                / max(len(c_tasks), 1), 6) if c_tasks else None),
               "gain": quality["point"], "regression_rate": regressions,
               # Each task the rate counted, with both arms' means: a regression within the frozen limit stays on
               # record, not only in a count (ER6-13). These are dev task ids, so the experience log leaves the
               # key out and no step of R reads it (docs/modules/00_CONTRACTS.md §4).
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
    confirmation_gain = None
    if confirmation is not None:
        confirmation_gain = paired_point(per_task(confirmation), c_tasks)
        metrics["confirmation_gain"] = confirmation_gain
        metrics["first_reading"] = dict(first or {})
    uncertainty = {"quality": quality, "cost": cost,
                   # Said plainly so nothing downstream has to infer it (ER-04).
                   "significant": bool(quality.get("excludes_zero") and quality.get("reliable")),
                   "note": "a development interval describes this comparison; a formal claim needs the locked "
                           "final plan"}
    broken = sorted(k for k, ok in (protected or {}).items() if not ok)
    if broken:
        return "rejected", [*reasons, f"protected requirements regressed: {broken}"], metrics, uncertainty
    if verdict:
        return verdict, reasons, metrics, uncertainty
    if gate.mode == "quality":
        notes: list[str] = []
        if quality["point"] < gate.min_gain:
            reasons.append(f"gain {quality['point']} below the plan's min_gain {gate.min_gain}")
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
                    return "inconclusive", [*reasons, f"{why} and this plan refuses unknown cost"], metrics, \
                        uncertainty
                notes.append(f"{why}: the cost term was not judged (missing_cost_policy report_unknown)")
            else:
                allowed = round(c_attempt * (1 + float(gate.cost_beta0) + float(gate.cost_beta1) * quality["point"]), 6)
                metrics["cost_per_attempt"]["allowed"] = allowed
                if s_attempt > allowed + 1e-9:
                    reasons.append(f"cost per attempt {s_attempt} above the allowed {allowed} (control {c_attempt} × "
                                   f"(1 + β0 {gate.cost_beta0} + β1 {gate.cost_beta1} × gain {quality['point']}))")
        if gate.max_runtime_cost_ratio is not None:
            ratio = _ratio(metrics["subject_cost_usd"], metrics["control_cost_usd"])
            if ratio is None and gate.missing_cost_policy == "refuse":
                return "inconclusive", [*reasons, "cost was not measured and this plan refuses unknown cost"], \
                    metrics, uncertainty
            if ratio is not None and ratio > gate.max_runtime_cost_ratio:
                reasons.append(f"cost ratio {ratio} above {gate.max_runtime_cost_ratio}")
        if confirmation is not None and (confirmation_gain is None or confirmation_gain < gate.min_gain):
            # M3 §2.1(b), second version: the confirmation reading on its own clears min_gain too, whatever the
            # pooled estimate says. A second reading of an unchanged version sits at 0 against the pooled control
            # by expectation, so "≥ 0" constrained nothing (the replay accepted 4/20 same-version pairs); two
            # independent readings each at ≥ +0.05 happen by chance about once in twenty.
            reasons.append(f"the confirmation reading alone has gain {confirmation_gain}, below the plan's "
                           f"min_gain {gate.min_gain}")
        if reasons:
            return "rejected", [*reasons, *notes], metrics, uncertainty
        return "accepted", ["development acceptance: the gain clears the plan's threshold"
                            + (" on the first reading, on the confirmation reading alone and on the pool of both"
                               if confirmation is not None else "")
                            + ("" if uncertainty["significant"] else "; the interval still contains zero, so "
                                                                    "this is not a significant improvement"),
                            *notes], metrics, uncertainty
    # economy: quality must be non-inferior and cost must be lower
    ratio = _ratio(metrics["subject_cost_usd"], metrics["control_cost_usd"])
    if ratio is None or s_missing or c_missing:
        return "inconclusive", [*reasons, "an economy verdict needs measured cost on both arms; unknown cost is "
                                          "not zero"], metrics, uncertainty
    if quality["low"] is None or not quality["reliable"]:
        return "inconclusive", [*reasons, "the non-inferiority interval is not reliable at this group count"], \
            metrics, uncertainty
    if quality["low"] < -gate.max_quality_loss:
        reasons.append(f"quality could be {quality['low']} below control, past -{gate.max_quality_loss}")
    if not (cost["high"] is not None and cost["high"] < 0):
        return "inconclusive", [*reasons, "the cost interval does not support a reduction"], metrics, uncertainty
    if ratio > gate.max_runtime_cost_ratio:
        reasons.append(f"cost ratio {ratio} above {gate.max_runtime_cost_ratio}")
    if regressions > gate.max_regression_rate:
        reasons.append(f"regression rate {regressions} above {gate.max_regression_rate}")
    if reasons:
        return "rejected", reasons, metrics, uncertainty
    return "accepted", ["development acceptance: non-inferior quality at lower measured cost"], metrics, \
        uncertainty


def _ratio(subject: float | None, control: float | None) -> float | None:
    if subject is None or control is None or control == 0:
        return None
    return round(subject / control, 6)


# ---------------------------------------------------------------------------- the evaluation
def screen(plan: TaskEvalPlan, subject: list[TaskResult], control: list[TaskResult]) -> tuple[str, list[str]]:
    """The cheap screen: it removes a candidate with a definite defect or an obvious predeclared regression.

    It does not promote and it does not remove a candidate merely for not having triggered: a policy that was
    never in a position to act on these tasks has produced no evidence either way, and that is recorded rather
    than counted against it (docs/modules/03_EVALUATION_RELEASE.md §2.3).
    """
    defects = [r for r in subject if r.failure_class == "candidate_defect"]
    if defects:
        return "invalid", [f"candidate defect on {r.task_id}: {r.invalid_reason}" for r in defects[:5]]
    s_tasks, c_tasks = per_task(subject), per_task(control)
    lost = sorted(t for t in set(s_tasks) & set(c_tasks) if c_tasks[t] == 1.0 and s_tasks[t] == 0.0)
    if lost:
        return "screened_out", [f"tasks the control passed and the candidate failed: {lost}"]
    return "pass", ["screen passed: no defect and no predeclared regression"
                    + ("" if s_tasks else "; no task result yet")]


def refill_attempts(rows: list, run, arm: str, limit: int) -> list:
    """Re-run the attempts that failed for infrastructure reasons, at most `limit` times per pair.

    Only those: a task the agent legitimately failed is evidence and is never re-rolled, a candidate defect is the
    candidate's, and an exhausted budget ends the comparison (docs/modules/03_EVALUATION_RELEASE.md §3). The
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


def evaluate_task(plan: TaskEvalPlan, *, run, objects: ct.Objects, experiment_ref: str,
                  families: dict[str, str], protected: dict | None = None,
                  results_sink=None, control_pool: list | None = None, confirming=None) -> ct.EvaluationReceipt:
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
                                                   protected=protected, control_pooled=pooled)
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
                                                       protected=protected, control_pooled=pooled,
                                                       confirmation=confirmation, first=first)
        kind = "confirmed"
    results_ref = objects.put_json("change", {
        "plan": plan_ref, "attempts": {arm: [r.to_json() for r in rows[arm]] for arm in rows},
        "order": [[t, i, a] for t, i, a in order], "control_pooled": pooled})
    return ct.EvaluationReceipt(experiment_ref=experiment_ref, profile="task", subject_ref=plan.candidate_ref,
                                control_ref=plan.control_ref, plan_ref=plan_ref, results_ref=results_ref,
                                verdict=verdict, reasons=tuple(reasons), metrics=metrics,
                                uncertainty=uncertainty, kind=kind,
                                costs={"subject_usd": metrics["subject_cost_usd"],
                                       "control_usd": metrics["control_cost_usd"],
                                       "missing": metrics["cost_missing"]})
