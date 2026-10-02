"""M3 acceptance (docs/modules/03_EVALUATION_RELEASE.md §5): the paired verdict, and installing a complete
version with its failure and recovery paths."""
import json

import pytest

from ouroloop import policies as P
from ouroloop.benchmark import TaskResult, pair_key
from ouroloop.evolve import contracts as ct
from ouroloop.evolve.task_gate import (GatePlan, TaskEvalPlan, evaluate_task, judge, paired_bootstrap,
                                       per_task, regression_rate, screen)
from ouroloop.release import NotAuthorized, Releases, Stale

FAMILIES = {"t1": "f1", "t2": "f1", "t3": "f2", "t4": "f3", "t5": "f4"}
TASKS = ("t1", "t2", "t3", "t4", "t5")


def plan(tmp_path, **kw):
    gate = kw.pop("gate", None) or GatePlan(mode="quality", min_gain=0.05, min_groups=3, bootstrap_replicates=400)
    return TaskEvalPlan(candidate_ref="policyset:sha256:" + "a" * 64,
                        control_ref="policyset:sha256:" + "b" * 64,
                        parent_release="release:sha256:" + "c" * 64, core_ref="core:sha256:" + "d" * 64,
                        split_ref="split-1", task_ids=kw.pop("task_ids", TASKS), repeats=kw.pop("repeats", 1),
                        gate_plan=gate.to_json(), **kw)


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


# ---------------------------------------------------------------------------- statistics
def test_repeats_are_averaged_before_tasks_are_counted(tmp_path):
    p = plan(tmp_path, repeats=2)
    mixed = [*rows(p, {"t1": True}, repeats=1), *rows(p, {"t1": False}, repeats=1)]
    assert per_task(mixed) == {"t1": 0.5}


def test_the_interval_is_resampled_over_families_not_trajectories():
    subject = {"t1": 1.0, "t2": 1.0, "t3": 1.0, "t4": 0.0, "t5": 0.0}
    control = {t: 0.0 for t in subject}
    wide = paired_bootstrap(subject, control, FAMILIES, replicates=600)
    narrow = paired_bootstrap(subject, control, {t: t for t in subject}, replicates=600)
    assert wide["groups"] == 4 and narrow["groups"] == 5
    assert (wide["high"] - wide["low"]) >= (narrow["high"] - narrow["low"])
    assert regression_rate(subject, control) == 0.0


def test_regression_rate_counts_tasks_whose_mean_fell():
    assert regression_rate({"t1": 0.0, "t2": 1.0}, {"t1": 1.0, "t2": 1.0}) == 0.5


# ---------------------------------------------------------------------------- ER-01 / ER-04 / ER-05
def test_er01_a_candidate_that_passes_the_screen_and_loses_on_dev_is_rejected(tmp_path):
    p = plan(tmp_path, screen_ids=("t1", "t3"))
    screened = screen(p, rows(p, {"t1": True, "t3": True}), rows(p, {"t1": False, "t3": False}))
    assert screened[0] == "pass"
    verdict, reasons, metrics, uncertainty = judge(
        p, rows(p, {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}),
        rows(p, {"t1": True, "t2": True, "t3": True, "t4": False, "t5": False}), FAMILIES)
    assert verdict == "rejected" and metrics["regression_rate"] > 0
    assert any("regression rate" in r or "min_gain" in r for r in reasons)


def test_a_screen_never_removes_a_candidate_for_not_having_triggered(tmp_path):
    p = plan(tmp_path, screen_ids=("t1",))
    same = {"t1": False}
    verdict, reasons = screen(p, rows(p, same), rows(p, same))
    assert verdict == "pass" and "no defect" in reasons[0]


def test_er04_a_small_gain_can_be_accepted_without_being_significant(tmp_path):
    p = plan(tmp_path, gate=GatePlan(min_gain=0.15, min_groups=3, bootstrap_replicates=400))
    verdict, reasons, metrics, uncertainty = judge(
        p, rows(p, {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}),
        rows(p, {"t1": False, "t2": True, "t3": False, "t4": False, "t5": False}), FAMILIES)
    assert verdict == "accepted" and metrics["gain"] == pytest.approx(0.2)
    assert uncertainty["significant"] is False and "not a significant improvement" in reasons[0]


def test_er05_an_economy_verdict_on_unknown_cost_is_inconclusive(tmp_path):
    gate = GatePlan(mode="economy", max_quality_loss=0.05, max_runtime_cost_ratio=0.8, min_groups=3,
                    bootstrap_replicates=400)
    p = plan(tmp_path, gate=gate)
    same = {"t1": True, "t2": True, "t3": True, "t4": False, "t5": False}
    verdict, reasons, metrics, _ = judge(p, rows(p, same), rows(p, same, cost=0.1), FAMILIES)
    assert verdict == "inconclusive" and "unknown cost is not zero" in reasons[-1]
    assert metrics["cost_missing"]["subject"] == 5
    # measured on both arms but the quality interval is too wide to call non-inferior
    worse = {"t1": False, "t2": False, "t3": True, "t4": False, "t5": False}
    verdict, reasons, _, _ = judge(p, rows(p, worse, cost=0.01), rows(p, same, cost=0.1), FAMILIES)
    assert verdict == "rejected" and any("below control" in r for r in reasons)


def test_er05_economy_accepts_only_a_supported_cost_reduction(tmp_path):
    gate = GatePlan(mode="economy", max_quality_loss=0.05, max_runtime_cost_ratio=0.8, min_groups=3,
                    bootstrap_replicates=400)
    p = plan(tmp_path, gate=gate)
    same = {"t1": True, "t2": True, "t3": True, "t4": True, "t5": True}
    verdict, reasons, metrics, unc = judge(p, rows(p, same, cost=0.05), rows(p, same, cost=0.10), FAMILIES)
    assert verdict == "accepted" and metrics["subject_cost_usd"] < metrics["control_cost_usd"]
    assert unc["cost"]["high"] < 0
    equal = judge(p, rows(p, same, cost=0.10), rows(p, same, cost=0.10), FAMILIES)
    assert equal[0] == "inconclusive" and "does not support a reduction" in equal[1][-1]


def test_a_quality_gate_may_not_have_a_min_gain_of_zero():
    with pytest.raises(ct.ContractError, match="above zero"):
        GatePlan(mode="quality", min_gain=0.0)
    with pytest.raises(ct.ContractError, match="measurable cost"):
        GatePlan(mode="economy")


def test_protected_requirements_block_acceptance(tmp_path):
    p = plan(tmp_path, gate=GatePlan(min_gain=0.05, min_groups=3, bootstrap_replicates=200,
                                     protected_requirements=("req-1",)))
    better = {"t1": True, "t2": True, "t3": True, "t4": True, "t5": True}
    verdict, reasons, _, _ = judge(p, rows(p, better), rows(p, {t: False for t in TASKS}), FAMILIES,
                                   protected={"req-1": False})
    assert verdict == "rejected" and "protected requirements regressed" in reasons[-1]


# ---------------------------------------------------------------------------- ER-02 / integrity
def test_er02_a_refill_completes_the_pair_and_the_failed_attempt_stays_on_record(tmp_path):
    p = plan(tmp_path, task_ids=("t1", "t3", "t4"))
    good = {"t1": True, "t3": True, "t4": True}
    broken = rows(p, {"t1": True}, validity="invalid", failure_class="infrastructure")
    subject = [*broken, *rows(p, good, attempt_suffix="#1", supersedes=broken[0].attempt_id)]
    verdict, reasons, metrics, _ = judge(p, subject, rows(p, {"t1": False, "t3": False, "t4": False}), FAMILIES)
    assert verdict == "accepted" and metrics["invalid"]["subject"] == 1
    assert any(r.supersedes for r in subject) and len([r for r in subject if r.task_id == "t1"]) == 2
    # without the refill the pair is incomplete, and that is inconclusive rather than a win
    verdict, reasons, _, _ = judge(p, broken, rows(p, {"t1": False, "t3": False, "t4": False}), FAMILIES)
    assert verdict == "inconclusive" and any("pairs are incomplete" in r for r in reasons)


def test_er02_the_retry_policy_refills_only_the_infrastructure_failures(tmp_path):
    """ER-02 as the production path runs it: the plan's retry policy refills the broken attempt, the original
    stays on record, and a task the agent simply failed is never re-rolled."""
    objects = ct.Objects(tmp_path)
    p = plan(tmp_path, task_ids=("t1", "t3", "t4"), retry_policy={"max_infrastructure_retries": 1})
    calls = []
    first = {"subject": True}

    def run(arm, order, retry=0, supersedes=None):
        calls.append((arm, tuple(t for t, _ in order), retry))
        if arm == "subject" and first["subject"]:
            first["subject"] = False
            return [*rows(p, {"t1": True}, validity="invalid", failure_class="infrastructure"),
                    *rows(p, {"t3": True, "t4": False})]
        if arm == "subject":
            # the runner writes the refill with the id of the attempt it replaces, as it is recorded
            return rows(p, {"t1": True}, attempt_suffix="#1", supersedes=supersedes[("t1", 0)])
        return rows(p, {"t1": False, "t3": False, "t4": False})

    receipt = evaluate_task(p, run=run, objects=objects, experiment_ref="experiment:sha256:" + "a" * 64,
                           families=FAMILIES)
    assert calls[1] == ("subject", ("t1",), 1) and len(calls) == 3      # only the broken pair, once
    stored = objects.get(receipt.results_ref)["attempts"]["subject"]
    assert len(stored) == 4 and [a["validity"] for a in stored].count("invalid") == 1
    assert [a for a in stored if a["supersedes"]][0]["supersedes"].startswith("run-1/t1")
    assert receipt.verdict == "accepted" and receipt.metrics["invalid"]["subject"] == 1
    # the task the agent failed (t4) is not re-run: it is evidence
    assert all("t4" not in tasks or retry == 0 for _arm, tasks, retry in calls)


def test_a_candidate_defect_is_invalid_and_cannot_be_dropped_as_infrastructure(tmp_path):
    p = plan(tmp_path, task_ids=("t1", "t3", "t4"))
    subject = [*rows(p, {"t1": True, "t3": True}), *rows(p, {"t4": False}, validity="invalid",
                                                         failure_class="candidate_defect")]
    verdict, reasons, _, _ = judge(p, subject, rows(p, {"t1": False, "t3": False, "t4": False}), FAMILIES)
    assert verdict == "invalid" and "candidate defect" in reasons[0]


def test_an_evaluator_failure_stops_the_evaluation(tmp_path):
    p = plan(tmp_path, task_ids=("t1", "t3", "t4"))
    subject = [*rows(p, {"t1": True, "t3": True}), *rows(p, {"t4": False}, validity="invalid",
                                                         failure_class="evaluator")]
    verdict, reasons, _, _ = judge(p, subject, rows(p, {"t1": False, "t3": False, "t4": False}), FAMILIES)
    assert verdict == "inconclusive" and "evaluator failure" in reasons[0]


def test_too_few_groups_is_inconclusive_not_accepted(tmp_path):
    p = plan(tmp_path, task_ids=("t1", "t2"), gate=GatePlan(min_gain=0.05, min_groups=3,
                                                            bootstrap_replicates=200))
    verdict, reasons, _, _ = judge(p, rows(p, {"t1": True, "t2": True}), rows(p, {"t1": False, "t2": False}),
                                   FAMILIES)
    assert verdict == "inconclusive" and any("independent groups" in r for r in reasons)


def test_evaluate_task_files_every_attempt_and_the_order(tmp_path):
    objects = ct.Objects(tmp_path)
    p = plan(tmp_path, task_ids=("t1", "t3", "t4"))
    outcomes = {"subject": {"t1": True, "t3": True, "t4": True}, "control": {"t1": False, "t3": True, "t4": False}}
    seen = []

    def run(arm, order, retry=0, supersedes=None):
        seen.append((arm, tuple(order), retry))
        return rows(p, outcomes[arm])

    receipt = evaluate_task(p, run=run, objects=objects, experiment_ref="experiment:sha256:" + "f" * 64,
                            families=FAMILIES)
    assert receipt.verdict == "accepted" and receipt.profile == "task"
    stored = objects.get(receipt.results_ref)
    assert len(stored["attempts"]["subject"]) == 3 and len(stored["order"]) == 6
    assert {arm for arm, *_rest in seen} == {"subject", "control"}
    assert objects.get(receipt.plan_ref)["task_ids"] == ["t1", "t3", "t4"]


def test_the_receipt_lists_each_regressed_task(tmp_path):
    """ER6-13 / IT6-26: a regression within the frozen limit is accepted and kept on the receipt task by task, and
    the same regressions past the limit reject. The list names dev tasks, so it stays with the receipt: neither the
    experience log nor what R reads of it carries a task id (docs/modules/00_CONTRACTS.md §4)."""
    from ouroloop.evolve.experience import experiment_record
    from ouroloop.research.context import make_context
    from ouroloop.research.diagnosis import experience_projection
    objects = ct.Objects(tmp_path)
    subject = {"t1": True, "t2": False, "t3": True, "t4": True, "t5": False}
    control = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}

    def attempts(p, arm):
        if arm == "control":
            return rows(p, control, repeats=2)
        both = rows(p, subject, repeats=2)          # t1 fails one of two repeats: its mean falls to 0.5
        return [both[0], rows(p, {"t1": False}, repeats=2)[1], *both[2:]]

    p = plan(tmp_path, repeats=2, gate=GatePlan(min_gain=0.05, min_groups=3, max_regression_rate=0.4,
                                                bootstrap_replicates=400))
    receipt = evaluate_task(p, run=lambda arm, order, retry=0, supersedes=None: attempts(p, arm), objects=objects,
                            experiment_ref="experiment:sha256:" + "e" * 64, families=FAMILIES)
    assert receipt.verdict == "accepted" and receipt.metrics["regression_rate"] == 0.4
    assert receipt.metrics["regressions"] == [
        {"task": "t1", "family": "f1", "control": 1.0, "subject": 0.5},
        {"task": "t2", "family": "f1", "control": 1.0, "subject": 0.0}]
    assert objects.load(objects.put(receipt), ct.EvaluationReceipt).metrics["regressions"][1]["task"] == "t2"
    # the same attempts under a cap of 0.2: the gain still clears min_gain, and the regressions alone reject
    strict = plan(tmp_path, repeats=2, gate=GatePlan(min_gain=0.05, min_groups=3, bootstrap_replicates=400))
    verdict, reasons, metrics, _ = judge(strict, attempts(strict, "subject"), attempts(strict, "control"),
                                         FAMILIES)
    assert verdict == "rejected" and reasons == ["regression rate 0.4 above 0.2"]
    assert metrics["regressions"] == receipt.metrics["regressions"]
    # the experience log keeps the rate; R's projection and the v0.5 steps' raw rows name no task
    record = experiment_record(experiment_id="e", proposal_ref="p", owner="worker", hypothesis="h",
                               changes=[{"target": "worker.harness"}], receipt=receipt)
    assert "regressions" not in record["metrics"] and record["metrics"]["regression_rate"] == 0.4
    legacy = make_context("s", goal="g", release_ref="", objects=objects, experience=[record])
    for text in (json.dumps(experience_projection([record])), legacy.prompt_state()):
        assert '"t1"' not in text and '"t2"' not in text


# ---------------------------------------------------------------------------- installing (ER-06 to ER-10)
def workspace(tmp_path):
    objects = ct.Objects(tmp_path)
    releases, parent = P.bootstrap_workspace(tmp_path, objects=objects)
    return objects, releases, parent


def accepted_receipt(objects, releases, parent, *, text="check every requirement"):
    candidate = P.replace_policies(objects, objects.load(parent, ct.SystemRelease).worker_ref,
                                   [P.descriptor(objects, "worker.system_prompt", "prompt", {"text": text})])
    receipt = ct.EvaluationReceipt(experiment_ref="experiment:sha256:" + "a" * 64, profile="task",
                                   subject_ref=candidate,
                                   control_ref=objects.load(parent, ct.SystemRelease).worker_ref,
                                   plan_ref=objects.put_json("change", {"plan": text}),
                                   results_ref=objects.put_json("change", {"results": []}), verdict="accepted")
    ref = objects.put(receipt)
    releases.register_receipt(ref, plan=receipt.plan_ref)
    return ref


def test_er06_a_preflight_failure_leaves_the_pointer_alone_and_may_be_retried(tmp_path):
    objects, releases, parent = workspace(tmp_path)
    ref = accepted_receipt(objects, releases, parent)
    broken = {"fail": True}

    def preflight(target):
        if broken["fail"]:
            raise RuntimeError("the model server could not load the artifact")
        return {"assembled": target}

    first = releases.install(ref, parent, "op-1", preflight=preflight)
    assert first.status == "installation_failed" and "model server" in first.reason
    assert releases.active()["release_ref"] == parent
    broken["fail"] = False
    again = releases.install(ref, parent, "op-1", preflight=preflight)     # same artifacts, no re-evaluation
    assert again.status == "installed" and releases.active()["release_ref"] == again.target_release
    assert objects.get(again.preflight_ref)["preflight"]["assembled"] == again.target_release


def test_er07_two_candidates_on_one_parent_do_not_both_install(tmp_path):
    objects, releases, parent = workspace(tmp_path)
    first = releases.install(accepted_receipt(objects, releases, parent, text="one"), parent, "op-a")
    second = releases.install(accepted_receipt(objects, releases, parent, text="two"), parent, "op-b")
    assert first.status == "installed" and second.status == "stale_parent"
    assert second.observed_pointer == first.target_release
    assert releases.active()["release_ref"] == first.target_release


def test_a_research_candidate_replaces_only_r_and_carries_w_over(tmp_path):
    objects, releases, parent = workspace(tmp_path)
    before = objects.load(parent, ct.SystemRelease)
    r1 = P.replace_policies(objects, before.research_ref,
                            [P.descriptor(objects, "research.preflight", "workflow",
                                          {"builtin": "research_preflight.v1",
                                           "checks": ["input_coverage", "label_availability"]})])
    receipt = ct.EvaluationReceipt(experiment_ref="experiment:sha256:" + "b" * 64, profile="research",
                                   subject_ref=r1, control_ref=before.research_ref,
                                   plan_ref=objects.put_json("change", {"campaign": 1}),
                                   results_ref=objects.put_json("change", {"results": []}), verdict="accepted")
    ref = objects.put(receipt)
    releases.register_receipt(ref, plan=receipt.plan_ref)
    installed = releases.install(ref, parent, "op-r1")
    after = objects.load(installed.target_release, ct.SystemRelease)
    assert installed.status == "installed" and after.research_ref == r1 and after.worker_ref == before.worker_ref


def test_er08_replaying_the_same_install_operation_commits_once(tmp_path):
    objects, releases, parent = workspace(tmp_path)
    ref = accepted_receipt(objects, releases, parent)
    first = releases.install(ref, parent, "op-1")
    revision = releases.active()["revision"]
    again = releases.install(ref, parent, "op-1")       # a recovered controller replays the same operation
    assert again.status == "installed" and "already committed" in (again.reason or "")
    assert releases.active()["revision"] == revision
    recovered = releases.recover("op-1")
    assert recovered.status == "installed" and recovered.target_release == first.target_release


def test_an_unregistered_or_rejected_receipt_never_installs(tmp_path):
    objects, releases, parent = workspace(tmp_path)
    candidate = P.replace_policies(objects, objects.load(parent, ct.SystemRelease).worker_ref,
                                   [P.descriptor(objects, "worker.system_prompt", "prompt", {"text": "x"})])
    rejected = objects.put(ct.EvaluationReceipt(experiment_ref="experiment:sha256:" + "c" * 64, profile="task",
                                                subject_ref=candidate, control_ref=candidate,
                                                plan_ref=objects.put_json("change", {"p": 1}),
                                                results_ref=objects.put_json("change", {"r": []}),
                                                verdict="rejected"))
    releases.register_receipt(rejected)
    with pytest.raises(NotAuthorized, match="not accepted"):
        releases.install(rejected, parent, "op-x")
    handwritten = objects.put(ct.EvaluationReceipt(experiment_ref="experiment:sha256:" + "d" * 64, profile="task",
                                                  subject_ref=candidate, control_ref=candidate,
                                                  plan_ref=objects.put_json("change", {"p": 2}),
                                                  results_ref=objects.put_json("change", {"r": []}),
                                                  verdict="accepted"))
    with pytest.raises(NotAuthorized, match="not registered"):
        releases.install(handwritten, parent, "op-y")
    assert releases.active()["release_ref"] == parent


def test_a_candidate_built_on_another_parent_is_stale_rather_than_rebased(tmp_path):
    objects, releases, parent = workspace(tmp_path)
    first = releases.install(accepted_receipt(objects, releases, parent, text="one"), parent, "op-a")
    stale = accepted_receipt(objects, releases, parent, text="three")
    with pytest.raises(Stale, match="not part of"):
        releases.install(stale, first.target_release, "op-c")


def test_er10_a_rollback_returns_to_an_authorised_ancestor(tmp_path):
    objects, releases, parent = workspace(tmp_path)
    installed = releases.install(accepted_receipt(objects, releases, parent, text="one"), parent, "op-a")
    target = releases.rollback("the new prompt broke a task")
    assert target == parent and releases.active()["release_ref"] == parent
    assert releases.revoked(installed.target_release) is True
    events = [e["event"] for e in ct.Events(tmp_path).all()]
    assert "revoked" in events and "rolled_back" in events


def test_a_revoked_ancestor_is_skipped_by_the_rollback(tmp_path):
    objects, releases, parent = workspace(tmp_path)
    one = releases.install(accepted_receipt(objects, releases, parent, text="one"), parent, "op-a")
    two = releases.install(accepted_receipt(objects, releases, one.target_release, text="two"),
                           one.target_release, "op-b")
    releases.revoke(one.target_release, "also bad")
    assert releases.rollback("rolling back two") == parent
    assert json.loads((tmp_path / "rsi" / "active.json").read_text())["release_ref"] == parent
    assert two.status == "installed"
