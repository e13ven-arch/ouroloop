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
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..benchmark import TaskResult, pair_key
from . import contracts as ct

MODES = ("quality", "economy")
MISSING_COST = ("refuse", "report_unknown")


@dataclass
class GatePlan(ct.Record):
    """The thresholds, fixed by whoever starts the experiment before the baseline is run."""
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


@dataclass
class TaskEvalPlan(ct.Record):
    """One frozen comparison. `task_ids` are development tasks; a final plan reports and never installs."""
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
        GatePlan.from_json(self.gate_plan) if self.gate_plan else None

    def gate(self) -> GatePlan:
        return GatePlan.from_json(self.gate_plan)

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


def regression_rate(subject: dict[str, float], control: dict[str, float]) -> float:
    """The share of tasks whose mean fell. One definition, used by every verdict of this plan."""
    shared = sorted(set(subject) & set(control))
    if not shared:
        return 0.0
    return round(sum(subject[t] < control[t] - 1e-9 for t in shared) / len(shared), 6)


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
              families: dict[str, str]) -> tuple[str, list[str]]:
    """What makes this comparison unusable, before any number is compared. -> ("", []) when it is usable."""
    reasons: list[str] = []
    defects = [r for r in subject if r.failure_class == "candidate_defect"]
    if defects:
        return "invalid", [f"candidate defect on {r.task_id}: {r.invalid_reason}" for r in defects[:5]]
    evaluator = [r for r in (*subject, *control) if r.failure_class == "evaluator"]
    if evaluator:
        return "inconclusive", [f"evaluator failure on {r.task_id}: {r.invalid_reason}" for r in evaluator[:5]]
    exhausted = [r for r in (*subject, *control) if r.failure_class == "budget_exhausted"]
    if exhausted:
        # The total budget ran out before the comparison finished: whatever the partial numbers say, they are the
        # numbers of a comparison nobody planned (docs/modules/05_EXECUTION_BUDGET.md §1).
        return "inconclusive", [f"budget_exhausted after {len(exhausted)} attempts could not run"]
    expected = {pair_key(plan.ref(), t, i) for t in plan.task_ids for i in range(plan.repeats)}
    have = {arm: {r.pair_key for r in rows if r.validity == "valid"} for arm, rows in
            (("subject", subject), ("control", control))}
    missing = sorted((expected - have["subject"]) | (expected - have["control"]))
    if missing:
        reasons.append(f"{len(missing)} pairs are incomplete: {[m.split('/', 1)[-1] for m in missing[:5]]}")
    groups = len({families.get(r.task_id, r.task_id) for r in subject if r.validity == "valid"})
    gate = plan.gate()
    if groups < gate.min_groups:
        reasons.append(f"{groups} independent groups, the plan needs {gate.min_groups}")
    return ("inconclusive", reasons) if reasons else ("", [])


def judge(plan: TaskEvalPlan, subject: list[TaskResult], control: list[TaskResult], families: dict[str, str],
          *, protected: dict | None = None) -> tuple[str, list[str], dict, dict]:
    """-> (verdict, reasons, metrics, uncertainty), read off the frozen plan.

    `protected` maps a protected requirement id to whether it still holds; a requirement that regressed blocks
    acceptance whatever the averages say.
    """
    gate = plan.gate()
    verdict, reasons = integrity(plan, subject, control, families)
    s_tasks, c_tasks = per_task(subject), per_task(control)
    s_cost, s_missing = per_task_cost(subject)
    c_cost, c_missing = per_task_cost(control)
    quality = paired_bootstrap(s_tasks, c_tasks, families, seed=gate.bootstrap_seed,
                               replicates=gate.bootstrap_replicates, confidence=gate.confidence_level)
    cost = paired_bootstrap(s_cost, c_cost, families, seed=gate.bootstrap_seed,
                            replicates=gate.bootstrap_replicates, confidence=gate.confidence_level)
    regressions = regression_rate(s_tasks, c_tasks)
    metrics = {"subject_pass_rate": pass_rate(subject), "control_pass_rate": pass_rate(control),
               "subject_partial_credit": (round(sum(per_task(subject, metric="partial_credit").values())
                                                / max(len(s_tasks), 1), 6) if s_tasks else None),
               "control_partial_credit": (round(sum(per_task(control, metric="partial_credit").values())
                                                / max(len(c_tasks), 1), 6) if c_tasks else None),
               "gain": quality["point"], "regression_rate": regressions, "tasks": len(s_tasks),
               "groups": quality["groups"], "subject_cost_usd": round(sum(s_cost.values()), 6) or None,
               "control_cost_usd": round(sum(c_cost.values()), 6) or None,
               "cost_missing": {"subject": s_missing, "control": c_missing},
               "invalid": {"subject": sum(r.validity == "invalid" for r in subject),
                           "control": sum(r.validity == "invalid" for r in control)}}
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
        if quality["point"] < gate.min_gain:
            reasons.append(f"gain {quality['point']} below the plan's min_gain {gate.min_gain}")
        if regressions > gate.max_regression_rate:
            reasons.append(f"regression rate {regressions} above {gate.max_regression_rate}")
        if gate.max_runtime_cost_ratio is not None:
            ratio = _ratio(metrics["subject_cost_usd"], metrics["control_cost_usd"])
            if ratio is None and gate.missing_cost_policy == "refuse":
                return "inconclusive", [*reasons, "cost was not measured and this plan refuses unknown cost"], \
                    metrics, uncertainty
            if ratio is not None and ratio > gate.max_runtime_cost_ratio:
                reasons.append(f"cost ratio {ratio} above {gate.max_runtime_cost_ratio}")
        if reasons:
            return "rejected", reasons, metrics, uncertainty
        return "accepted", ["development acceptance: the gain clears the plan's threshold"
                            + ("" if uncertainty["significant"] else "; the interval still contains zero, so "
                                                                    "this is not a significant improvement")], \
            metrics, uncertainty
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


def evaluate_task(plan: TaskEvalPlan, *, run, objects: ct.Objects, experiment_ref: str,
                  families: dict[str, str], protected: dict | None = None,
                  results_sink=None) -> ct.EvaluationReceipt:
    """Run both arms of a frozen plan and file the receipt.

    `run(arm, order, retry=0, supersedes=None) -> list[TaskResult]` belongs to the caller: this module fixes the
    order, the statistics and the verdict, and the runner belongs to whoever owns the environment. Every attempt
    and its cost go into the receipt's results object, including the failures — a receipt that kept only the
    attempts that worked would be describing a different run.
    """
    plan_ref = objects.put(plan)
    order = plan.order()
    rows = {"subject": [], "control": []}
    for arm in ("subject", "control"):
        arm_order = [(task, repeat) for task, repeat, side in order if side == arm]
        rows[arm] = refill(plan, arm, list(run(arm, arm_order, 0, None)), run)
        if results_sink is not None:
            for r in rows[arm]:
                results_sink(arm, r)
    verdict, reasons, metrics, uncertainty = judge(plan, rows["subject"], rows["control"], families,
                                                   protected=protected)
    results_ref = objects.put_json("change", {
        "plan": plan_ref, "attempts": {arm: [r.to_json() for r in rows[arm]] for arm in rows},
        "order": [[t, i, a] for t, i, a in order]})
    return ct.EvaluationReceipt(experiment_ref=experiment_ref, profile="task", subject_ref=plan.candidate_ref,
                                control_ref=plan.control_ref, plan_ref=plan_ref, results_ref=results_ref,
                                verdict=verdict, reasons=tuple(reasons), metrics=metrics,
                                uncertainty=uncertainty,
                                costs={"subject_usd": metrics["subject_cost_usd"],
                                       "control_usd": metrics["control_cost_usd"],
                                       "missing": metrics["cost_missing"]})
