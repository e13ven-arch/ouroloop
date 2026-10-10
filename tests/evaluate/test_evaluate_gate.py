"""The paired gate (docs/design/EVALUATE.md §5): the frozen plan, the statistics over tasks and families, the verdict's
clauses in order, the pooled control, the confirmation reading, refills, and the receipt evaluate_task files."""
import json

import pytest

from ouroloop.contracts import ContractError, Objects
from ouroloop.evaluate.gate import (GatePlan, TaskEvalPlan, evaluate_task, judge, paired_bootstrap, per_task,
                                    receipt_differences, refill_attempts, regressed, regression_rate, shortfall)
from ouroloop.evaluate.release import GateReceipt
from ouroloop.evaluate.results import TaskResult, pair_key

FAMILIES = {"t1": "f1", "t2": "f1", "t3": "f2", "t4": "f3", "t5": "f4"}
TASKS = ("t1", "t2", "t3", "t4", "t5")


def gate(**kw):
    return GatePlan(**{"min_gain": 0.05, "min_groups": 3, "bootstrap_replicates": 400, **kw})


def plan(**kw):
    g = kw.pop("gate", None) or gate()
    return TaskEvalPlan(**{"candidate_ref": "package:sha256:" + "a" * 64, "control_ref": "package:sha256:" + "b" * 64,
                           "parent_release": "release:sha256:" + "c" * 64, "core_ref": "core:sha256:" + "d" * 64,
                           "split_ref": "split-1", "task_ids": TASKS, "gate_plan": g.to_json(), **kw})


def rows(p: TaskEvalPlan, outcomes: dict, *, cost=None, repeats=1, validity=None, failure_class="",
         supersedes="", attempt_suffix="") -> list[TaskResult]:
    """One TaskResult per task and repeat, with the plan's pair key: comparisons are keyed on the plan, not on a
    session id."""
    out = []
    for task, success in outcomes.items():
        for i in range(repeats):
            invalid = validity == "invalid"
            out.append(TaskResult(
                task, FAMILIES[task], "dev", i, "candidate", "run-1", "manifest-1",
                success=bool(success), partial_credit=1.0 if success else 0.5,
                cost_usd=cost, validity="invalid" if invalid else "valid",
                invalid_reason="provider unavailable" if invalid else "",
                termination="infrastructure" if invalid else "finished",
                failure_class=failure_class, supersedes=supersedes,
                attempt_id=f"run-1/{task}/r{i}{attempt_suffix}", pair_key=pair_key(p.ref(), task, i)))
    return out


def readings(p: TaskEvalPlan, *outcome_sets, cost=None, run_prefix="ctl") -> list[TaskResult]:
    """Several readings of one version under the plan's conditions, each its own run: what a pooled control is made
    of. Their pair keys are other gates' (or none); only the task and the validity matter to the pool."""
    out = []
    for n, outcomes in enumerate(outcome_sets):
        for r in rows(p, outcomes, cost=cost, attempt_suffix=f"@{run_prefix}{n}"):
            r.run = f"{run_prefix}-{n}"
            r.pair_key = pair_key(f"plan:sha256:{n:064x}", r.task_id, r.attempt)
            out.append(r)
    return out


# ---------------------------------------------------------------------------- the plan
def test_a_gate_plan_refuses_thresholds_that_judge_nothing():
    with pytest.raises(ContractError, match="above zero"):
        GatePlan(min_gain=0.0)
    for drop in (0.0, 1.0, 1.5):
        with pytest.raises(ContractError, match=r"strictly inside \(0, 1\)"):
            GatePlan(regression_drop=drop)
    assert GatePlan(regression_drop=0.99).regression_drop == 0.99
    with pytest.raises(ContractError, match="both cost_beta0 and cost_beta1"):
        GatePlan(cost_beta0=0.1)
    with pytest.raises(ContractError, match="not below zero"):
        GatePlan(cost_beta0=-0.1, cost_beta1=1.0)
    with pytest.raises(ContractError, match="at least 1"):
        GatePlan(min_tasks=0)
    with pytest.raises(ContractError, match="missing_cost_policy"):
        GatePlan(missing_cost_policy="zero")
    with pytest.raises(ContractError, match="confidence_level"):
        GatePlan(confidence_level=1.0)


def test_a_gate_that_sets_a_removed_clause_is_refused_and_unset_options_keep_the_plan_ref():
    """The economy mode, the runtime cost ratio and protected requirements are judged by nothing: a plan naming one
    is refused rather than frozen and silently ignored. An optional clause left unset is absent from the plan."""
    for removed in ({"mode": "economy"}, {"max_runtime_cost_ratio": 0.8}, {"protected_requirements": ["req-1"]}):
        with pytest.raises(ContractError, match="does not take"):
            GatePlan.from_json({**GatePlan().to_json(), **removed})
    fields = GatePlan().to_json()
    assert not {"regression_drop", "cost_beta0", "cost_beta1", "min_tasks"} & set(fields)
    assert GatePlan.from_json(fields).ref() == GatePlan().ref()
    full = GatePlan(regression_drop=0.5, cost_beta0=0.1, cost_beta1=2.0, min_tasks=5)
    assert GatePlan.from_json(json.loads(json.dumps(full.to_json()))) == full and full.ref() != GatePlan().ref()


def test_a_task_plan_names_its_tasks_and_takes_one_confirmation_reading():
    with pytest.raises(ContractError, match="names the tasks"):
        plan(task_ids=())
    with pytest.raises(ContractError, match="repeats"):
        plan(repeats=0)
    with pytest.raises(ContractError, match="control_pooling"):
        plan(control_pooling="shared")
    with pytest.raises(ContractError, match="0 or 1"):
        plan(confirmation_repeats=2)
    assert plan(confirmation_repeats=0).confirmations == 0 and plan(confirmation_repeats=1).confirmations == 1
    assert plan(control_pooling="pooled").pooled and not plan().pooled
    p = plan(repeats=2)
    assert TaskEvalPlan.from_json(json.loads(json.dumps(p.to_json()))).ref() == p.ref()
    assert p.gate() == gate()


def test_the_execution_order_is_fixed_by_the_seed_and_pairs_both_arms():
    p = plan(repeats=2, order_seed=7)
    order = p.order()
    assert order == plan(repeats=2, order_seed=7).order()
    assert len(order) == 20 and {(t, i) for t, i, _ in order} == {(t, i) for t in TASKS for i in range(2)}
    for k in range(0, len(order), 2):                      # each pair's two arms run next to each other
        (t1, i1, a1), (t2, i2, a2) = order[k], order[k + 1]
        assert (t1, i1) == (t2, i2) and {a1, a2} == {"subject", "control"}
    assert len({tuple(a for _, _, a in plan(order_seed=s).order()) for s in range(6)}) > 1   # the seed decides
    # what two gates of one activity differ by is not part of the conditions a pooled reading is matched on
    assert plan(candidate_ref="package:sha256:" + "e" * 64).conditions() == plan().conditions()
    assert plan(repeats=2).conditions() != plan().conditions()


# ---------------------------------------------------------------------------- statistics
def test_repeats_are_averaged_before_tasks_are_counted():
    p = plan(repeats=2)
    mixed = [*rows(p, {"t1": True}), *rows(p, {"t1": False}), *rows(p, {"t2": True}, repeats=3)]
    assert per_task(mixed) == {"t1": 0.5, "t2": 1.0}
    # an invalid attempt is not a reading
    assert per_task([*mixed, *rows(p, {"t2": False}, validity="invalid")]) == {"t1": 0.5, "t2": 1.0}


def test_the_interval_is_resampled_over_families_not_tasks():
    subject = {"t1": 1.0, "t2": 1.0, "t3": 1.0, "t4": 0.0, "t5": 0.0}
    control = {t: 0.0 for t in subject}
    wide = paired_bootstrap(subject, control, FAMILIES, replicates=600)
    narrow = paired_bootstrap(subject, control, {t: t for t in subject}, replicates=600)
    assert wide["groups"] == 4 and narrow["groups"] == 5 and wide["tasks"] == narrow["tasks"] == 5
    assert wide["point"] == narrow["point"] == 0.6
    assert (wide["high"] - wide["low"]) > (narrow["high"] - narrow["low"])
    # one family that moves together is one draw: tasks t1 and t2 are never drawn apart
    together = paired_bootstrap({"t1": 1.0, "t2": 1.0}, {"t1": 0.0, "t2": 0.0}, FAMILIES, replicates=200)
    assert together["groups"] == 1 and together["low"] == together["high"] == 1.0 and not together["reliable"]
    empty = paired_bootstrap({}, {}, FAMILIES)
    assert empty["point"] is None and empty["low"] is None and not empty["reliable"]
    assert paired_bootstrap(subject, control, FAMILIES, seed=3, replicates=300) == \
        paired_bootstrap(subject, control, FAMILIES, seed=3, replicates=300)


def test_a_regression_is_a_drop_of_more_than_the_plans_magnitude():
    """Under regression_drop 0.5 a task the control passed and the candidate passed in one of two repeats (1.0 -> 0.5)
    is not a regression and one it failed in both (1.0 -> 0.0) is. Without the field any drop counts."""
    assert regression_rate({"t1": 0.0, "t2": 1.0}, {"t1": 1.0, "t2": 1.0}) == 0.5
    assert regression_rate({"t1": 0.5, "t2": 0.0}, {"t1": 1.0, "t2": 1.0}, 0.5) == 0.5
    assert regression_rate({"t1": 0.5, "t2": 0.0}, {"t1": 1.0, "t2": 1.0}, 0.4) == 1.0
    assert regression_rate({"t1": 2 / 3, "t2": 1 / 3}, {"t1": 1.0, "t2": 1.0}, 0.5) == 0.5     # three repeats
    assert regression_rate({"t1": 0.5, "t2": 0.0}, {"t1": 1.0, "t2": 1.0}) == 1.0
    assert regressed({"t9": 0.0}, {"t1": 1.0}) == [] and regression_rate({}, {}) == 0.0
    control = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}
    for drop, listed in ((0.5, ["t2"]), (None, ["t1", "t2"])):
        p = plan(repeats=2, gate=gate(max_regression_rate=0.5, regression_drop=drop))
        both = rows(p, {"t1": True, "t2": False, "t3": True, "t4": True, "t5": True}, repeats=2)
        subject = [both[0], rows(p, {"t1": False}, repeats=2)[1], *both[2:]]    # t1: one of two repeats
        _, _, metrics, _ = judge(p, subject, rows(p, control, repeats=2), FAMILIES)
        assert [r["task"] for r in metrics["regressions"]] == listed, drop
        assert metrics["regression_rate"] == pytest.approx(len(listed) / 5)


# ---------------------------------------------------------------------------- the verdict
def test_a_candidate_that_loses_on_dev_is_rejected():
    p = plan()
    verdict, reasons, metrics, _ = judge(
        p, rows(p, {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}),
        rows(p, {"t1": True, "t2": True, "t3": True, "t4": False, "t5": False}), FAMILIES)
    assert verdict == "rejected" and metrics["gain"] == pytest.approx(-0.4)
    assert reasons == ["gain -0.4 below the plan's min_gain 0.05", "regression rate 0.4 above 0.2"]


def test_a_small_gain_can_be_accepted_without_being_significant():
    p = plan(gate=gate(min_gain=0.15))
    verdict, reasons, metrics, uncertainty = judge(
        p, rows(p, {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}),
        rows(p, {"t1": False, "t2": True, "t3": False, "t4": False, "t5": False}), FAMILIES)
    assert verdict == "accepted" and metrics["gain"] == pytest.approx(0.2)
    assert uncertainty["significant"] is False and "not a significant improvement" in reasons[0]
    assert metrics["subject_pass_rate"] == pytest.approx(0.4) and metrics["control_pass_rate"] == pytest.approx(0.2)


def test_the_gain_is_compared_with_min_gain_inclusively():
    for min_gain, expected in ((0.2, "accepted"), (0.21, "rejected")):
        p = plan(gate=gate(min_gain=min_gain))
        subject = rows(p, {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False})
        assert judge(p, subject, rows(p, dict.fromkeys(TASKS, False)), FAMILIES)[0] == expected, min_gain


def test_a_candidate_defect_is_invalid_and_cannot_be_dropped_as_infrastructure():
    p = plan(task_ids=("t1", "t3", "t4"))
    subject = [*rows(p, {"t1": True, "t3": True}),
               *rows(p, {"t4": False}, validity="invalid", failure_class="candidate_defect")]
    verdict, reasons, _, _ = judge(p, subject, rows(p, {"t1": False, "t3": False, "t4": False}), FAMILIES)
    assert verdict == "invalid" and "candidate defect on t4" in reasons[0]


def test_an_evaluator_failure_or_an_exhausted_budget_is_inconclusive():
    p = plan(task_ids=("t1", "t3", "t4"))
    control = rows(p, {"t1": False, "t3": False, "t4": False})
    for failure, said in (("evaluator", "evaluator failure"), ("budget_exhausted", "budget_exhausted")):
        subject = [*rows(p, {"t1": True, "t3": True}), *rows(p, {"t4": False}, validity="invalid",
                                                             failure_class=failure)]
        verdict, reasons, _, _ = judge(p, subject, control, FAMILIES)
        assert verdict == "inconclusive" and said in reasons[0], failure


def test_too_few_groups_is_inconclusive_not_accepted():
    p = plan(task_ids=("t1", "t2"))
    verdict, reasons, _, _ = judge(p, rows(p, {"t1": True, "t2": True}), rows(p, {"t1": False, "t2": False}),
                                   FAMILIES)
    assert verdict == "inconclusive" and reasons == ["1 independent groups, the plan needs 3"]
    loose = plan(task_ids=("t1", "t2"), gate=gate(min_groups=1))
    assert judge(loose, rows(loose, {"t1": True, "t2": True}), rows(loose, {"t1": False, "t2": False}),
                 FAMILIES)[0] == "accepted"


def test_a_candidate_whose_cost_outgrows_its_gain_is_rejected_with_the_cost_reason():
    """With beta0 0.10 and beta1 2.0 a candidate at +0.2 may cost up to 0.45 x (1 + 0.1 + 0.4) = 0.675 an attempt: at
    0.60 it passes with the allowance on the receipt, at 0.70 it is rejected with the numbers as the reason. Without
    the betas cost is reported and not judged."""
    control = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    subject = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}
    costed = gate(cost_beta0=0.10, cost_beta1=2.0)
    for cost, expected in ((0.60, "accepted"), (0.70, "rejected")):
        p = plan(gate=costed)
        verdict, reasons, metrics, _ = judge(p, rows(p, subject, cost=cost), rows(p, control, cost=0.45), FAMILIES)
        assert verdict == expected, (cost, reasons)
        assert metrics["cost_per_attempt"] == {"subject": pytest.approx(cost), "control": pytest.approx(0.45),
                                               "allowed": pytest.approx(0.675)}
    assert reasons == ["cost per attempt 0.7 above the allowed 0.675 (control 0.45 × (1 + β0 0.1 + β1 2.0 × gain 0.2))"]
    p = plan()
    verdict, _, metrics, _ = judge(p, rows(p, subject, cost=0.70), rows(p, control, cost=0.45), FAMILIES)
    assert verdict == "accepted" and metrics["cost_per_attempt"]["allowed"] is None


def test_unknown_cost_follows_the_missing_cost_policy_in_the_cost_term():
    """An attempt without a price is unknown, never zero: under `refuse` the gate is inconclusive and says so, under
    `report_unknown` the cost term is skipped and the reason records that."""
    control = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    subject = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}
    for policy, expected in (("refuse", "inconclusive"), ("report_unknown", "accepted")):
        p = plan(gate=gate(cost_beta0=0.10, cost_beta1=2.0, missing_cost_policy=policy))
        verdict, reasons, metrics, _ = judge(p, rows(p, subject, cost=0.6), rows(p, control), FAMILIES)
        assert verdict == expected and metrics["cost_per_attempt"]["control"] is None
        assert metrics["cost_missing"] == {"subject": 0, "control": 5}
        assert any("not measured on every valid attempt" in r for r in reasons)
    assert "cost term was not judged" in reasons[-1]


# ---------------------------------------------------------------------------- refills
def test_a_refill_completes_the_pair_and_the_failed_attempt_stays_on_record():
    p = plan(task_ids=("t1", "t3", "t4"))
    good = {"t1": True, "t3": True, "t4": True}
    broken = rows(p, {"t1": True}, validity="invalid", failure_class="infrastructure")
    subject = [*broken, *rows(p, good, attempt_suffix="#1", supersedes=broken[0].attempt_id)]
    control = rows(p, {"t1": False, "t3": False, "t4": False})
    verdict, _, metrics, _ = judge(p, subject, control, FAMILIES)
    assert verdict == "accepted" and metrics["invalid"]["subject"] == 1
    # without the refill the pair is incomplete, and that is inconclusive rather than a win
    verdict, reasons, _, _ = judge(p, broken, control, FAMILIES)
    assert verdict == "inconclusive" and any("pairs are incomplete" in r for r in reasons)


def test_refill_attempts_supersede_the_latest_failure_and_stop_at_the_limit():
    """Only infrastructure failures are re-run, at most `limit` times per pair; each refill names the attempt it
    replaces (the latest one), and every original stays on record."""
    p = plan(task_ids=("t1", "t3", "t4"))
    first = [*rows(p, {"t1": True}, validity="invalid", failure_class="infrastructure"),
             *rows(p, {"t3": False}, validity="invalid", failure_class="candidate_defect"),
             *rows(p, {"t4": False})]
    calls = []

    def run(arm, order, retry, supersedes):
        calls.append((arm, order, retry, dict(supersedes)))
        if retry == 1:          # the refill breaks again
            return rows(p, {"t1": True}, validity="invalid", failure_class="infrastructure", attempt_suffix="#1",
                        supersedes=supersedes[("t1", 0)])
        return rows(p, {"t1": True}, attempt_suffix=f"#{retry}", supersedes=supersedes[("t1", 0)])

    out = refill_attempts(first, run, "subject", 3)
    assert [(arm, order, retry) for arm, order, retry, _ in calls] == [("subject", [("t1", 0)], 1),
                                                                      ("subject", [("t1", 0)], 2)]
    assert calls[0][3] == {("t1", 0): "run-1/t1/r0"} and calls[1][3] == {("t1", 0): "run-1/t1/r0#1"}
    assert len(out) == 5 and out[:3] == first and out[-1].validity == "valid" and out[-1].supersedes == "run-1/t1/r0#1"
    # the limit holds even when every refill breaks, and a limit of 0 re-runs nothing
    always = []
    refill_attempts(first, lambda arm, order, retry, sup: always.append(retry) or rows(
        p, {"t1": True}, validity="invalid", failure_class="infrastructure"), "control", 2)
    assert always == [1, 2]
    assert refill_attempts(first, lambda *a: pytest.fail("ran"), "subject", 0) == first


def test_the_retry_policy_refills_only_the_infrastructure_failures(tmp_path):
    """As evaluate_task runs it: the plan's retry policy refills the broken attempt, the original stays on record, and
    a task the agent simply failed is never re-rolled."""
    objects = Objects(tmp_path)
    p = plan(task_ids=("t1", "t3", "t4"), retry_policy={"max_infrastructure_retries": 1})
    calls = []

    def run(arm, order, retry=0, supersedes=None):
        calls.append((arm, tuple(t for t, _ in order), retry))
        if arm == "subject" and retry == 0:
            return [*rows(p, {"t1": True}, validity="invalid", failure_class="infrastructure"),
                    *rows(p, {"t3": True, "t4": False})]
        if arm == "subject":
            return rows(p, {"t1": True}, attempt_suffix="#1", supersedes=supersedes[("t1", 0)])
        return rows(p, {"t1": False, "t3": False, "t4": False})

    receipt = evaluate_task(p, run=run, objects=objects, families=FAMILIES)
    assert calls[1] == ("subject", ("t1",), 1) and len(calls) == 3      # only the broken pair, once
    stored = objects.get(receipt.results_ref)["attempts"]["subject"]
    assert len(stored) == 4 and [a["validity"] for a in stored].count("invalid") == 1
    assert [a for a in stored if a["supersedes"]][0]["supersedes"] == "run-1/t1/r0"
    assert receipt.verdict == "accepted" and receipt.metrics["invalid"]["subject"] == 1
    assert all("t4" not in tasks or retry == 0 for _arm, tasks, retry in calls)


# ---------------------------------------------------------------------------- the receipt
def test_evaluate_task_files_every_attempt_and_the_order(tmp_path):
    objects = Objects(tmp_path)
    p = plan(task_ids=("t1", "t3", "t4"))
    outcomes = {"subject": {"t1": True, "t3": True, "t4": True}, "control": {"t1": False, "t3": True, "t4": False}}
    seen, sunk = [], []

    def run(arm, order, retry=0, supersedes=None):
        seen.append((arm, tuple(order), retry))
        return rows(p, {t: outcomes[arm][t] for t, _ in order}, cost=0.1)

    receipt = evaluate_task(p, run=run, objects=objects, families=FAMILIES,
                            results_sink=lambda arm, r: sunk.append((arm, r.task_id)))
    assert isinstance(receipt, GateReceipt) and receipt.verdict == "accepted" and receipt.kind == "full"
    assert (receipt.subject_ref, receipt.control_ref, receipt.parent_release) == (
        p.candidate_ref, p.control_ref, p.parent_release)
    assert receipt.plan_ref == p.ref() and objects.get(receipt.plan_ref)["task_ids"] == ["t1", "t3", "t4"]
    stored = objects.get(receipt.results_ref)
    assert stored["plan"] == p.ref() and len(stored["attempts"]["subject"]) == 3 and len(stored["order"]) == 6
    assert [tuple(o) for o in stored["order"]] == p.order() and stored["control_pooled"] is False
    assert [(arm, set(order)) for arm, order, _ in seen] == [("subject", {("t1", 0), ("t3", 0), ("t4", 0)}),
                                                            ("control", {("t1", 0), ("t3", 0), ("t4", 0)})]
    assert sorted(sunk) == sorted([(a, t) for a in ("subject", "control") for t in ("t1", "t3", "t4")])
    assert receipt.costs == {"subject_usd": pytest.approx(0.3), "control_usd": pytest.approx(0.3),
                             "missing": {"subject": 0, "control": 0}}
    assert objects.load(objects.put(receipt), GateReceipt) == receipt


def test_the_receipt_lists_each_regressed_task_and_its_three_classes(tmp_path):
    """A regression within the frozen limit is accepted and kept on the receipt task by task; the same attempts past
    the limit reject. The receipt's results object gives fixed / regressed / still_failing, nothing recomputed."""
    objects = Objects(tmp_path)
    subject = {"t1": True, "t2": False, "t3": True, "t4": True, "t5": False}
    control = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}

    def attempts(p, arm):
        if arm == "control":
            return rows(p, control, repeats=2)
        both = rows(p, subject, repeats=2)          # t1 fails one of two repeats: its mean falls to 0.5
        return [both[0], rows(p, {"t1": False}, repeats=2)[1], *both[2:]]

    p = plan(repeats=2, gate=gate(max_regression_rate=0.4))
    receipt = evaluate_task(p, run=lambda arm, order, retry=0, supersedes=None: attempts(p, arm), objects=objects,
                            families=FAMILIES)
    assert receipt.verdict == "accepted" and receipt.metrics["regression_rate"] == 0.4
    assert receipt.metrics["regressions"] == [
        {"task": "t1", "family": "f1", "control": 1.0, "subject": 0.5},
        {"task": "t2", "family": "f1", "control": 1.0, "subject": 0.0}]
    assert receipt_differences(receipt, objects) == {"fixed": ["t3", "t4"], "regressed": ["t1", "t2"],
                                                     "still_failing": ["t5"]}
    strict = plan(repeats=2)
    verdict, reasons, metrics, _ = judge(strict, attempts(strict, "subject"), attempts(strict, "control"), FAMILIES)
    assert verdict == "rejected" and reasons == ["regression rate 0.4 above 0.2"]
    assert metrics["regressions"] == receipt.metrics["regressions"]


def test_receipt_differences_read_the_plans_regression_drop(tmp_path):
    objects = Objects(tmp_path)
    p = plan(repeats=2, gate=gate(max_regression_rate=0.5, regression_drop=0.5))
    both = rows(p, {"t1": True, "t2": False, "t3": True, "t4": True, "t5": True}, repeats=2)
    subject = [both[0], rows(p, {"t1": False}, repeats=2)[1], *both[2:]]
    control = rows(p, {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}, repeats=2)
    receipt = evaluate_task(p, run=lambda arm, *a, **k: subject if arm == "subject" else control, objects=objects,
                            families=FAMILIES)
    assert receipt_differences(receipt, objects)["regressed"] == ["t2"]      # t1 fell by 0.5, not more


# ---------------------------------------------------------------------------- the pooled control
def test_the_control_arm_pools_every_valid_reading_of_the_installed_version():
    """With the control pooled, every task's control mean is the mean over all the installed version's valid readings,
    the receipt says how many readings and which attempts, and a task that passed in one of two readings is 0.5."""
    p = plan(control_pooling="pooled")
    pool = readings(p, {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False},
                    {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False})
    assert per_task(pool) == {"t1": 1.0, "t2": 0.5, "t3": 0.0, "t4": 0.0, "t5": 0.0}
    subject = rows(p, {"t1": True, "t2": True, "t3": True, "t4": True, "t5": False})
    verdict, _, metrics, _ = judge(p, subject, pool, FAMILIES, control_pooled=True)
    assert verdict == "accepted" and metrics["gain"] == pytest.approx((0 + 0.5 + 1 + 1 + 0) / 5)
    assert metrics["control_readings"] == 2 and len(metrics["control_attempt_refs"]) == 10
    assert metrics["control_pass_rate"] == pytest.approx(0.3) and metrics["readings"] == 1
    # a fresh judge of the same rows needs the plan's own pair keys on the control
    assert judge(p, subject, pool, FAMILIES)[0] == "inconclusive"
    # one reading short on a task: that task is missing, and the comparison is inconclusive
    short = [r for r in pool if r.task_id != "t5"]
    verdict, reasons, _, _ = judge(p, subject, short, FAMILIES, control_pooled=True)
    assert verdict == "inconclusive" and any("fewer than 1 pooled control readings: ['t5']" in r for r in reasons)


def test_the_control_arm_reads_only_the_pairs_the_pool_is_short_of(tmp_path):
    """The pool is the control's starting point, topped up: a complete pool runs no control attempt; a pool short on
    one task runs exactly that task's pairs; an empty pool is the whole plan. A plan frozen without pooling runs the
    control arm whole, whatever pool it is handed, and its results object does not say pooled."""
    objects = Objects(tmp_path)
    good = {"t1": True, "t2": True, "t3": True, "t4": True, "t5": False}
    base = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    complete = readings(plan(control_pooling="pooled"), base)
    short = [r for r in complete if r.task_id != "t5"]
    cases = (("pooled", complete, [], True), ("pooled", short, [[("t5", 0)]], True),
             ("pooled", [], [[(t, 0) for t in TASKS]], True), (None, complete, [[(t, 0) for t in TASKS]], False))
    for pooling, pool, control_orders, says_pooled in cases:
        p = plan(control_pooling=pooling)
        orders = []

        def run(arm, order, retry=0, supersedes=None):
            if arm == "control":
                orders.append(list(order))
                return rows(p, {t: base[t] for t, _ in order}, attempt_suffix="@topup")
            return rows(p, good)
        receipt = evaluate_task(p, run=run, objects=objects, families=FAMILIES, control_pool=pool)
        assert orders == control_orders, (pooling, len(pool))
        assert receipt.verdict == "accepted" and receipt.kind == "full"
        stored = objects.get(receipt.results_ref)
        assert stored["control_pooled"] is says_pooled
        assert len(stored["attempts"]["control"]) == 5                    # the pool plus the top-up: one per task
    two = plan(control_pooling="pooled", repeats=2)
    assert shortfall(two, readings(two, base)) == [(t, 1) for t in TASKS]      # one more of each, at the free index
    both = readings(two, base) + [r for r in readings(two, base, run_prefix="other") if r.task_id == "t1"]
    assert shortfall(two, both) == [(t, 1) for t in TASKS if t != "t1"]
    assert shortfall(two, []) == [(t, i) for t in TASKS for i in range(2)]
    assert shortfall(two, readings(two, base, base)) == []
    invalid = rows(two, base, validity="invalid", failure_class="infrastructure")
    assert shortfall(two, invalid) == [(t, i) for t in TASKS for i in range(2)]   # an invalid reading fills nothing


# ---------------------------------------------------------------------------- the confirmation reading
FIVE = TASKS


def confirmed(tmp_path, first: dict, second: dict, control: dict, *, confirming=None):
    """One gate under a plan that pools its control and confirms once: the candidate reads `first`, then `second`."""
    objects = Objects(tmp_path)
    p = plan(control_pooling="pooled", confirmation_repeats=1, gate=gate(max_regression_rate=0.5,
                                                                         bootstrap_replicates=200))
    arms = []

    def run(arm, order, retry=0, supersedes=None):
        arms.append(arm)
        return rows(p, {"subject": first, "confirm": second, "control": control}[arm],
                    attempt_suffix="" if arm == "subject" else f"@{arm}")
    receipt = evaluate_task(p, run=run, objects=objects, families=FAMILIES, control_pool=readings(p, control),
                            confirming=confirming)
    return receipt, arms, objects


def test_an_accepted_first_reading_is_confirmed_before_it_installs(tmp_path):
    """A first reading that clears min_gain is read again under the same plan (the `confirming` hook told first), and
    the receipt is kind `confirmed`: both candidate readings against the pooled control, with the first reading's
    numbers and the confirmation's own gain kept."""
    control = dict.fromkeys(FIVE, False)
    first = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}        # +0.4
    second = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}       # +0.2 on its own
    told = []
    receipt, arms, objects = confirmed(tmp_path, first, second, control, confirming=told.append)
    assert arms == ["subject", "confirm"] and told == [{"gain": 0.4, "verdict": "accepted"}]
    assert receipt.verdict == "accepted" and receipt.kind == "confirmed"
    assert receipt.metrics["readings"] == 2 and receipt.metrics["gain"] == pytest.approx(0.3)
    assert receipt.metrics["first_reading"] == {"gain": 0.4, "verdict": "accepted"}
    assert receipt.metrics["confirmation_gain"] == pytest.approx(0.2)
    assert receipt.reasons[0].startswith("development acceptance: the gain clears the plan's threshold on the first "
                                         "reading, on the confirmation reading alone and on the pool of both")
    stored = objects.get(receipt.results_ref)["attempts"]
    assert set(stored) == {"subject", "control", "confirm"} and len(stored["confirm"]) == 5
    assert objects.load(objects.put(receipt), GateReceipt).kind == "confirmed"


def test_a_confirmation_whose_pooled_gain_falls_below_min_gain_rejects(tmp_path):
    control = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    first = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}
    second = dict.fromkeys(FIVE, False)
    receipt, arms, _ = confirmed(tmp_path, first, second, control)
    assert arms == ["subject", "confirm"] and receipt.verdict == "rejected" and receipt.kind == "confirmed"
    assert receipt.metrics["first_reading"] == {"gain": 0.2, "verdict": "accepted"}
    assert receipt.metrics["gain"] == pytest.approx(0.0)
    assert receipt.metrics["confirmation_gain"] == pytest.approx(-0.2)
    assert any("gain 0.0 below the plan's min_gain" in r for r in receipt.reasons)
    assert any("confirmation reading alone has gain -0.2, below the plan's min_gain 0.05" in r
               for r in receipt.reasons)


def test_a_confirmation_reading_under_min_gain_rejects_even_when_the_pool_clears_it(tmp_path):
    """A confirmation reading back where the control is (first +0.2, confirmation 0, pooled +0.1 still over min_gain)
    is rejected; so is a confirmation that is a loss while the pool still clears."""
    control = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    first = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}
    receipt, arms, _ = confirmed(tmp_path, first, control, control)
    assert arms == ["subject", "confirm"] and receipt.verdict == "rejected" and receipt.kind == "confirmed"
    assert receipt.metrics["gain"] == pytest.approx(0.1) and receipt.metrics["confirmation_gain"] == 0.0
    assert receipt.reasons == ("the confirmation reading alone has gain 0.0, below the plan's min_gain 0.05",)
    strong = {"t1": True, "t2": True, "t3": True, "t4": True, "t5": False}        # +0.6, then a loss
    receipt, _, _ = confirmed(tmp_path, strong, dict.fromkeys(FIVE, False), control)
    assert receipt.verdict == "rejected" and receipt.metrics["gain"] == pytest.approx(0.2)
    assert receipt.reasons == ("the confirmation reading alone has gain -0.2, below the plan's min_gain 0.05",)


def test_a_confirmation_at_exactly_min_gain_clears():
    tasks = tuple(f"t{i}" for i in range(1, 21))
    many = {t: f"f{(i % 5) + 1}" for i, t in enumerate(tasks)}
    p = TaskEvalPlan(candidate_ref="package:sha256:" + "a" * 64, control_ref="package:sha256:" + "b" * 64,
                     task_ids=tasks, control_pooling="pooled", confirmation_repeats=1,
                     gate_plan=gate(bootstrap_replicates=200).to_json())

    def reading(passing, suffix):
        return [TaskResult(t, many[t], "dev", 0, "candidate", f"run{suffix}", "m", success=t in passing,
                           partial_credit=1.0 if t in passing else 0.5, attempt_id=f"run{suffix}/{t}/r0/x#0",
                           pair_key=pair_key(p.ref(), t, 0)) for t in tasks]
    verdict, _, metrics, _ = judge(p, reading({"t1", "t2", "t3"}, "-first"), reading(set(), "-control"), many,
                                   control_pooled=True, confirmation=reading({"t1"}, "-confirm"),
                                   first={"gain": 0.15, "verdict": "accepted"})
    assert verdict == "accepted" and metrics["confirmation_gain"] == pytest.approx(0.05)
    verdict, reasons, _, _ = judge(p, reading({"t1", "t2", "t3"}, "-first"), reading(set(), "-control"), many,
                                   control_pooled=True, confirmation=reading(set(), "-confirm"))
    assert verdict == "rejected" and any("confirmation reading alone" in r for r in reasons)


def test_an_incomplete_confirmation_reading_is_inconclusive():
    p = plan(control_pooling="pooled", confirmation_repeats=1)
    first = rows(p, {"t1": True, "t2": True, "t3": True, "t4": False, "t5": False})
    partial = rows(p, {"t1": True, "t2": True}, attempt_suffix="@confirm")
    verdict, reasons, _, _ = judge(p, first, readings(p, dict.fromkeys(FIVE, False)), FAMILIES, control_pooled=True,
                                   confirmation=partial)
    assert verdict == "inconclusive" and any("confirmation reading is incomplete on 3 pairs" in r for r in reasons)


def test_a_rejected_first_reading_is_not_confirmed(tmp_path):
    control = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    told = []
    receipt, arms, _ = confirmed(tmp_path, control, control, control, confirming=told.append)
    assert arms == ["subject"] and told == []
    assert receipt.verdict == "rejected" and receipt.kind == "full" and receipt.metrics["readings"] == 1
    assert "first_reading" not in receipt.metrics


# ---------------------------------------------------------------------------- partial credit (EVALUATE §5.1)
def with_partial(result_rows: list[TaskResult], value: float) -> list[TaskResult]:
    for r in result_rows:
        r.partial_credit = value
    return result_rows


def test_a_strict_tie_with_a_clear_partial_credit_gain_is_accepted_by_partial_credit():
    p = plan(gate=gate(partial_min_gain=0.1))
    none = dict.fromkeys(TASKS, False)
    verdict, reasons, metrics, uncertainty = judge(p, with_partial(rows(p, none), 0.9),
                                                   with_partial(rows(p, none), 0.5), FAMILIES)
    assert verdict == "accepted" and metrics["gain"] == 0.0 and metrics["accepted_by"] == "partial"
    assert metrics["partial_gain"] == pytest.approx(0.4) and uncertainty["partial"]["excludes_zero"]
    assert any("accepted by partial credit" in r for r in reasons)


def test_partial_credit_never_accepts_a_strict_loss_and_needs_an_interval_above_zero():
    p = plan(gate=gate(partial_min_gain=0.1))
    loss = with_partial(rows(p, {"t1": False, "t2": False, "t3": False, "t4": False, "t5": False}), 0.9)
    control = with_partial(rows(p, {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}), 0.5)
    verdict, reasons, metrics, _ = judge(p, loss, control, FAMILIES)
    assert verdict == "rejected" and metrics["accepted_by"] is None
    assert "and a strict gain of at least zero" in reasons[0]
    # the same partial gain on one family only: the interval reaches zero
    mixed = with_partial(rows(p, dict.fromkeys(TASKS, False)), 0.5)
    mixed[0].partial_credit = mixed[1].partial_credit = 0.9             # t1, t2: family f1
    verdict, reasons, metrics, uncertainty = judge(p, mixed, with_partial(rows(p, dict.fromkeys(TASKS, False)), 0.5),
                                                   FAMILIES)
    assert verdict == "rejected" and not uncertainty["partial"]["excludes_zero"]
    assert "does not clear partial_min_gain 0.1 with an interval above zero" in reasons[0]


def test_without_partial_min_gain_the_strict_rule_alone_judges():
    p = plan()
    none = dict.fromkeys(TASKS, False)
    verdict, reasons, metrics, _ = judge(p, with_partial(rows(p, none), 0.9), with_partial(rows(p, none), 0.5),
                                         FAMILIES)
    assert verdict == "rejected" and reasons == ["gain 0.0 below the plan's min_gain 0.05"]
    assert metrics["partial_gain"] == pytest.approx(0.4) and metrics["accepted_by"] is None


def test_the_confirmation_reading_must_clear_partial_min_gain_on_its_own_too():
    p = plan(gate=gate(partial_min_gain=0.1))
    none = dict.fromkeys(TASKS, False)
    control = with_partial(rows(p, none), 0.5)
    good = with_partial(rows(p, none), 0.9)
    flat = with_partial(rows(p, none, attempt_suffix="#c"), 0.5)
    verdict, reasons, metrics, _ = judge(p, good, control, FAMILIES, confirmation=flat)
    assert verdict == "rejected" and metrics["confirmation_partial_gain"] == pytest.approx(0.0)
    assert "its partial-credit gain 0.0 is below partial_min_gain 0.1" in reasons[-1]
    again = with_partial(rows(p, none, attempt_suffix="#c"), 0.9)
    verdict, _, metrics, _ = judge(p, good, control, FAMILIES, confirmation=again)
    assert verdict == "accepted" and metrics["accepted_by"] == "partial"


def test_partial_min_gain_is_a_share_inside_zero_and_one():
    with pytest.raises(ContractError):
        gate(partial_min_gain=1.5)
    assert gate(partial_min_gain=0.03).to_json()["partial_min_gain"] == 0.03
    assert "partial_min_gain" not in gate().to_json()
