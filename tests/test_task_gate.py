"""M3 acceptance (docs/modules/03_EVALUATION_RELEASE.md §5): the paired verdict, and installing a complete
version with its failure and recovery paths."""
import json

import pytest
import rsi

from ouroloop import policies as P
from ouroloop.benchmark import TaskResult, pair_key
from ouroloop.evolve import contracts as ct
from ouroloop.evolve.task_gate import (GatePlan, TaskEvalPlan, evaluate_task, judge, paired_bootstrap, per_task,
                                       regression_rate)
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
def test_er01_a_candidate_that_loses_on_dev_is_rejected(tmp_path):
    p = plan(tmp_path)
    verdict, reasons, metrics, uncertainty = judge(
        p, rows(p, {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}),
        rows(p, {"t1": True, "t2": True, "t3": True, "t4": False, "t5": False}), FAMILIES)
    assert verdict == "rejected" and metrics["regression_rate"] > 0
    assert any("regression rate" in r or "min_gain" in r for r in reasons)


def test_er04_a_small_gain_can_be_accepted_without_being_significant(tmp_path):
    p = plan(tmp_path, gate=GatePlan(min_gain=0.15, min_groups=3, bootstrap_replicates=400))
    verdict, reasons, metrics, uncertainty = judge(
        p, rows(p, {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}),
        rows(p, {"t1": False, "t2": True, "t3": False, "t4": False, "t5": False}), FAMILIES)
    assert verdict == "accepted" and metrics["gain"] == pytest.approx(0.2)
    assert uncertainty["significant"] is False and "not a significant improvement" in reasons[0]


def test_a_quality_gate_may_not_have_a_min_gain_of_zero():
    with pytest.raises(ct.ContractError, match="above zero"):
        GatePlan(mode="quality", min_gain=0.0)


def test_a_gate_that_sets_a_removed_clause_is_refused_and_the_kept_fields_keep_the_plan_ref():
    """P1 step 6: the economy mode, the runtime cost ratio and protected requirements are judged by nothing, so a
    gate setting one is refused rather than frozen and silently ignored; their fields stay at the values every plan
    was frozen with, so a plan's ref does not move."""
    for kw, match in (({"mode": "economy", "max_runtime_cost_ratio": 0.8}, "economy is at tag v0.4.0"),
                      ({"max_runtime_cost_ratio": 0.8}, "not judged any more"),
                      ({"protected_requirements": ("req-1",)}, "not judged any more")):
        with pytest.raises(ct.ContractError, match=match):
            GatePlan(**kw)
    fields = GatePlan().to_json()
    assert fields["mode"] == "quality" and fields["max_runtime_cost_ratio"] is None
    assert fields["protected_requirements"] == [] and fields["max_quality_loss"] == 0.02
    assert GatePlan.from_json(fields).ref() == GatePlan().ref()


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
    releases, parent = rsi.bootstrap_workspace(tmp_path, objects=objects)
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


def test_a_rollback_with_no_good_ancestor_is_refused_and_changes_nothing(tmp_path):
    """The pointer may only name a release that was evaluated and installed before: with no authorised ancestor that
    stands (here the bootstrap release has none), the rollback is refused before anything is revoked."""
    objects, releases, parent = workspace(tmp_path)
    with pytest.raises(NotAuthorized, match="no authorised ancestor"):
        releases.rollback("nothing to fall back to")
    assert releases.active()["release_ref"] == parent and releases.revoked(parent) is False
    one = releases.install(accepted_receipt(objects, releases, parent, text="one"), parent, "op-a")
    releases.revoke(parent, "the bootstrap is bad too")
    with pytest.raises(NotAuthorized, match="no authorised ancestor"):
        releases.rollback("its only ancestor is revoked")
    assert releases.active()["release_ref"] == one.target_release        # the pointer stays where it was
    assert one.target_release not in releases.revoked()                     # and nothing more was revoked


# ---------------------------------------------------------------------------- v0.3 (M3 §2.1): the gate's statistics
def readings(p: TaskEvalPlan, *outcome_sets, cost=None, run_prefix="ctl") -> list[TaskResult]:
    """Several readings of one version under the plan's conditions, each as its own run: what a pooled control is
    made of. Their pair keys are other gates' (or none); only the task and the validity matter to the pool."""
    out = []
    for n, outcomes in enumerate(outcome_sets):
        for r in rows(p, outcomes, cost=cost, attempt_suffix=f"@{run_prefix}{n}"):
            r.run = f"{run_prefix}-{n}"
            r.pair_key = pair_key(f"plan:sha256:{n:064x}", r.task_id, r.attempt)
            out.append(r)
    return out


def test_the_control_arm_pools_every_valid_reading_of_the_installed_version(tmp_path):
    """M3 §2.1(a) (v0.3), ER6-17: with the control pooled, every task's control mean is the mean over all of the
    installed version's valid readings, the receipt says how many readings and which attempts, and a task that
    passed in one of two readings is 0.5 — not the 0 or 1 of whichever reading a fresh gate would have taken."""
    p = plan(tmp_path, control_pooling="pooled")
    pool = readings(p, {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False},
                    {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False})
    assert per_task(pool) == {"t1": 1.0, "t2": 0.5, "t3": 0.0, "t4": 0.0, "t5": 0.0}
    subject = rows(p, {"t1": True, "t2": True, "t3": True, "t4": True, "t5": False})
    verdict, reasons, metrics, _ = judge(p, subject, pool, FAMILIES, control_pooled=True)
    assert verdict == "accepted" and metrics["gain"] == pytest.approx((0 + 0.5 + 1 + 1 + 0) / 5)
    assert metrics["control_readings"] == 2 and len(metrics["control_attempt_refs"]) == 10
    assert metrics["control_pass_rate"] == pytest.approx(0.3) and metrics["readings"] == 1
    # one reading short on a task: that task is missing, and the comparison is inconclusive rather than read on
    # the readings that happen to be there
    short = [r for r in pool if not (r.task_id == "t5")]
    verdict, reasons, _, _ = judge(p, subject, short, FAMILIES, control_pooled=True)
    assert verdict == "inconclusive" and any("fewer than 1 pooled control readings: ['t5']" in r for r in reasons)


def test_the_control_arm_reads_only_the_pairs_the_pool_is_short_of(tmp_path):
    """M3 §2.1(a) (v0.3, after review): the pool is the control's starting point, topped up — a complete pool runs no
    control attempt; a pool short on one task runs the control arm for exactly that task's pairs, the gate is
    conclusive, and the topped-up row joins the control (so the next gate's pool holds it); an empty pool is the
    whole plan short, on the same path, not a separate "fresh" branch. A plan frozen without the field runs the
    control arm whole, whatever pool it is handed, and its results object does not say pooled."""
    from ouroloop.evolve.task_gate import shortfall
    objects = ct.Objects(tmp_path)
    good = {"t1": True, "t2": True, "t3": True, "t4": True, "t5": False}
    base = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    complete = readings(plan(tmp_path, control_pooling="pooled"), base)
    short = [r for r in complete if r.task_id != "t5"]
    cases = (("pooled", complete, [], True), ("pooled", short, [[("t5", 0)]], True),
             ("pooled", [], [[(t, 0) for t in TASKS]], True), (None, complete, [[(t, 0) for t in TASKS]], False))
    for pooling, pool, control_orders, says_pooled in cases:
        p = plan(tmp_path, control_pooling=pooling)
        orders = []

        def run(arm, order, retry=0, supersedes=None):
            if arm == "control":
                orders.append(list(order))
                return rows(p, {t: base[t] for t, _ in order}, attempt_suffix="@topup")
            return rows(p, good)
        receipt = evaluate_task(p, run=run, objects=objects, experiment_ref="experiment:sha256:" + "a" * 64,
                                families=FAMILIES, control_pool=pool)
        assert orders == control_orders, (pooling, len(pool))
        assert receipt.verdict == "accepted" and receipt.kind == "full"
        stored = objects.get(receipt.results_ref)
        assert stored["control_pooled"] is says_pooled
        assert len(stored["attempts"]["control"]) == 5                    # the pool plus the top-up: one per task
        assert receipt.metrics["control_readings"] == (2 if pooling == "pooled" and 0 < len(pool) < 5 else 1)
    # the pairs a pool is short of, per task, up to the plan's repeats
    two = plan(tmp_path, control_pooling="pooled", repeats=2)
    assert shortfall(two, readings(two, base)) == [(t, 1) for t in TASKS]      # one more of each, at the free index
    both = readings(two, base) + [r for r in readings(two, base, run_prefix="other") if r.task_id == "t1"]
    assert shortfall(two, both) == [(t, 1) for t in TASKS if t != "t1"]          # two readings of t1, both index 0
    assert shortfall(two, []) == [(t, i) for t in TASKS for i in range(2)] and shortfall(two, readings(two, base, base)) == []


def test_a_pooled_control_counts_only_readings_under_the_same_plan_conditions_and_model(fake_upstream, tmp_path,
                                                                                        monkeypatch):
    """M3 §2.1(a) (v0.3): the pool is the installed W's valid dev readings under the frozen conditions — the
    baseline's dev run and a gate's control arm among them — and not a reading under other per-attempt limits, a
    reading of another W, a train reading, a screen arm's partial reading, or an invalid attempt. A release that
    only changed R is the same W. The verify bound counts the pairs the pool is short of, not a whole control arm."""
    from test_rsi_integration import prepared
    from ouroloop import bench
    from ouroloop.benchmark import Budget
    cfg, activity, release, plan_ref = prepared(fake_upstream, tmp_path)
    plan_ref = bench.freeze_task_plan(activity.objects, core_ref=ct.SystemRelease.from_json(
        activity.objects.get(release)).core_ref, split_ref=activity.splits.hash(),
        task_ids=tuple(__import__("rsi").DEV), gate=GatePlan(min_gain=0.05, min_groups=1, bootstrap_replicates=200),
        per_attempt_budget=activity.bench.budget.limits(), control_pooling="pooled", confirmation_repeats=1)
    work = bench.improvement(activity, template_ref=plan_ref, provider=__import__("rsi").BusinessWorker())
    dev = [(t, 0) for t in work.dev_tasks]
    baseline = work.run_arm("dev_baseline", release, dev, plan_ref="", split="dev", candidate="current", role="base")
    control = work.run_arm("control", release, dev, plan_ref=plan_ref, split="dev", candidate="control", role="base")
    work.run_arm("measure", release, [(t, 0) for t in work.train_tasks[:1]], plan_ref="", split="train",
                 candidate="current", role="base")                                 # train: not a dev reading
    other = bench.improvement(activity, template_ref=plan_ref, provider=__import__("rsi").BusinessWorker())
    other.attempt_budget = Budget(**{**activity.bench.budget.limits(), "max_steps": 3})
    other.run_arm("dev_baseline", release, dev, plan_ref="", split="dev", candidate="current", role="base")
    # another W: W0 with the finish check, as a release of its own; and the same W under another R
    objects = activity.objects
    w1 = P.replace_policies(objects, ct.SystemRelease.from_json(objects.get(release)).worker_ref,
                            [P.descriptor(objects, "worker.finish", "prompt", {"text": P.FINISH_CHECK})])
    before = ct.SystemRelease.from_json(objects.get(release))
    other_w = activity.releases.bootstrap(w1, before.research_ref, before.core_ref, reason="W1", activate=False)
    work.run_arm("dev_baseline", other_w, dev, plan_ref="", split="dev", candidate="current", role="base")
    r1 = P.replace_policies(objects, before.research_ref, [P.descriptor(
        objects, "research.preflight", "workflow", {"builtin": "research_preflight.v1", "checks": ["input_coverage"]})])
    same_w = activity.releases.bootstrap(before.worker_ref, r1, before.core_ref, reason="W0 under R1", activate=False)
    under_r1 = work.run_arm("dev_baseline", same_w, dev, plan_ref="", split="dev", candidate="current", role="base")
    # a screen's control arm: the screen tasks only, attempt 0 — not a reading of the plan
    work.run_arm("screen_control", release, dev[:1], plan_ref=plan_ref, split="dev", candidate="control", role="base")
    pool = work.control_pool(release)
    assert {r.attempt_id for r in pool} == {r.attempt_id for r in (*baseline, *control, *under_r1)}
    assert all(r.split == "dev" and r.validity == "valid" for r in pool)
    # a plan frozen without pooling has no pool, and the headroom rule reads the same readings on its own
    fresh = bench.improvement(activity, template_ref=bench.freeze_task_plan(
        objects, core_ref=before.core_ref, split_ref=activity.splits.hash(), task_ids=tuple(__import__("rsi").DEV),
        gate=GatePlan(min_gain=0.05, min_groups=1, bootstrap_replicates=200),
        per_attempt_budget=activity.bench.budget.limits()), provider=__import__("rsi").BusinessWorker())
    assert fresh.control_pool(release) == []
    # the verify bound counts the confirmation and only the control pairs the pool is short of
    assert work._verify_bound() == {"model_calls": 2 * len(dev), "tool_calls": 2 * len(dev)}
    assert fresh._verify_bound() == {"model_calls": 2 * len(dev), "tool_calls": 2 * len(dev)}
    work.confirmation_repeats = None
    assert work._verify_bound() == {"model_calls": len(dev), "tool_calls": len(dev)}
    monkeypatch.setattr(work, "control_pool", lambda *a, **k: [r for r in pool if r.task_id != dev[0][0]])
    assert work._verify_bound() == {"model_calls": len(dev) + 1, "tool_calls": len(dev) + 1}


def confirmed(tmp_path, first: dict, second: dict, control: dict, *, confirming=None, pool=True):
    """One gate under a plan that pools its control and confirms once: the candidate reads `first`, then `second`."""
    objects = ct.Objects(tmp_path)
    p = plan(tmp_path, control_pooling="pooled" if pool else None, confirmation_repeats=1,
             gate=GatePlan(min_gain=0.05, min_groups=3, max_regression_rate=0.5, bootstrap_replicates=200))
    arms = []

    def run(arm, order, retry=0, supersedes=None):
        arms.append(arm)
        return rows(p, {"subject": first, "confirm": second, "control": control}[arm],
                    attempt_suffix="" if arm == "subject" else f"@{arm}")
    receipt = evaluate_task(p, run=run, objects=objects, experiment_ref="experiment:sha256:" + "b" * 64,
                            families=FAMILIES, control_pool=readings(p, control) if pool else None,
                            confirming=confirming)
    return receipt, arms, objects


FIVE = ("t1", "t2", "t3", "t4", "t5")


def test_an_accepted_first_reading_is_confirmed_before_it_installs(tmp_path):
    """M3 §2.1(b) (v0.3), IT6-33: a first reading that clears min_gain is read again under the same plan — the
    `confirming` hook is told first — and the receipt is kind `confirmed`: the pooled candidate readings against the
    pooled control, with the first reading's numbers and the confirmation's own gain kept."""
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
    assert "on the confirmation reading alone" in receipt.reasons[0]
    stored = objects.get(receipt.results_ref)["attempts"]
    assert set(stored) == {"subject", "control", "confirm"} and len(stored["confirm"]) == 5
    assert objects.load(objects.put(receipt), ct.EvaluationReceipt).kind == "confirmed"


def test_a_confirmation_whose_pooled_gain_falls_below_min_gain_rejects(tmp_path):
    """M3 §2.1(b) (v0.3), ER6-18: first +0.2, confirmation −0.2, pooled 0.0 — below min_gain — so the candidate is
    rejected, on a receipt of kind `confirmed` that keeps the first reading's numbers."""
    control = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    first = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}
    second = {"t1": False, "t2": False, "t3": False, "t4": False, "t5": False}
    receipt, arms, _ = confirmed(tmp_path, first, second, control)
    assert arms == ["subject", "confirm"] and receipt.verdict == "rejected" and receipt.kind == "confirmed"
    assert receipt.metrics["first_reading"] == {"gain": 0.2, "verdict": "accepted"}
    assert receipt.metrics["gain"] == pytest.approx(0.0) and receipt.metrics["confirmation_gain"] == pytest.approx(-0.2)
    assert any("gain 0.0 below the plan's min_gain" in r for r in receipt.reasons)
    assert any("confirmation reading alone has gain -0.2, below the plan's min_gain 0.05" in r
               for r in receipt.reasons)


def test_a_confirmation_reading_under_min_gain_rejects_even_when_the_pool_clears_it(tmp_path):
    """M3 §2.1(b) second version (v0.3, 2026-10-04), ER6-18: a confirmation reading that comes back exactly where
    the control is — the pattern of every same-version pair the first replay accepted: first +0.2 (there +0.12),
    confirmation 0, pooled +0.1 still over min_gain — is rejected, on a `confirmed` receipt whose reason names the
    confirmation's 0.0 and the plan's 0.05; and so is a confirmation that is a loss (−0.2) while the pool still
    clears (+0.2)."""
    control = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    first = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}       # +0.2
    receipt, arms, _ = confirmed(tmp_path, first, control, control)                # confirmation reads as the control
    assert arms == ["subject", "confirm"] and receipt.verdict == "rejected" and receipt.kind == "confirmed"
    assert receipt.metrics["gain"] == pytest.approx(0.1) and receipt.metrics["confirmation_gain"] == 0.0
    assert receipt.metrics["first_reading"] == {"gain": 0.2, "verdict": "accepted"}
    assert receipt.reasons == ("the confirmation reading alone has gain 0.0, below the plan's min_gain 0.05",)
    strong = {"t1": True, "t2": True, "t3": True, "t4": True, "t5": False}        # +0.6, then a loss
    receipt, _, _ = confirmed(tmp_path, strong, dict.fromkeys(FIVE, False), control)
    assert receipt.verdict == "rejected" and receipt.kind == "confirmed"
    assert receipt.metrics["gain"] == pytest.approx(0.2) and receipt.metrics["confirmation_gain"] == pytest.approx(-0.2)
    assert receipt.reasons == ("the confirmation reading alone has gain -0.2, below the plan's min_gain 0.05",)


def test_a_candidate_that_clears_the_gate_on_both_readings_is_accepted(tmp_path):
    """M3 §2.1(b) second version (v0.3, 2026-10-04): a candidate installs when both independent readings clear
    min_gain against the same pooled control — first +0.4, confirmation +0.2 — and the receipt says so on all three
    numbers; a confirmation at exactly min_gain clears (the clause is "at least")."""
    control = dict.fromkeys(FIVE, False)
    first = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}
    second = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    receipt, arms, _ = confirmed(tmp_path, first, second, control)
    assert arms == ["subject", "confirm"] and receipt.verdict == "accepted" and receipt.kind == "confirmed"
    assert (receipt.metrics["first_reading"]["gain"], receipt.metrics["confirmation_gain"], receipt.metrics["gain"]) \
        == (0.4, pytest.approx(0.2), pytest.approx(0.3))
    assert receipt.reasons[0].startswith("development acceptance: the gain clears the plan's threshold on the first "
                                         "reading, on the confirmation reading alone and on the pool of both")
    # at the threshold itself: a 20-task plan where the confirmation gains exactly one task of twenty
    tasks = tuple(f"t{i}" for i in range(1, 21))
    many = {t: f"f{(i % 5) + 1}" for i, t in enumerate(tasks)}
    p = TaskEvalPlan(candidate_ref="policyset:sha256:" + "a" * 64, control_ref="policyset:sha256:" + "b" * 64,
                     parent_release="release:sha256:" + "c" * 64, core_ref="core:sha256:" + "d" * 64,
                     split_ref="split-1", task_ids=tasks, control_pooling="pooled", confirmation_repeats=1,
                     gate_plan=GatePlan(min_gain=0.05, min_groups=3, bootstrap_replicates=200).to_json())

    def reading(passing, suffix):
        return [TaskResult(t, many[t], "dev", 0, "candidate", f"run{suffix}", "m", success=t in passing,
                           partial_credit=1.0 if t in passing else 0.5, attempt_id=f"run{suffix}/{t}/r0/x#0",
                           pair_key=pair_key(p.ref(), t, 0)) for t in tasks]
    verdict, reasons, metrics, _ = judge(p, reading({"t1", "t2", "t3"}, "-first"), reading(set(), "-control"), many,
                                         control_pooled=True, confirmation=reading({"t1"}, "-confirm"),
                                         first={"gain": 0.15, "verdict": "accepted"})
    assert verdict == "accepted" and metrics["confirmation_gain"] == pytest.approx(0.05)


def test_a_rejected_first_reading_is_not_confirmed(tmp_path):
    """M3 §2.1(b) (v0.3): a first reading under min_gain is rejected as read, on a `full` receipt; the confirmation
    arm never runs and the state hook is not told."""
    control = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    first = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    told = []
    receipt, arms, _ = confirmed(tmp_path, first, first, control, confirming=told.append)
    assert arms == ["subject"] and told == []
    assert receipt.verdict == "rejected" and receipt.kind == "full" and receipt.metrics["readings"] == 1
    assert "first_reading" not in receipt.metrics


def test_a_failed_confirmation_keeps_kind_confirmed_and_the_first_reading(tmp_path):
    """M3 §2.1(b) (v0.3), EX6-15: the two receipts a confirmation can leave are both `confirmed`; the install
    authorization reads the kind off the plan — under a plan that confirms, an accepted `full` receipt does not
    install, and a confirmed one does."""
    control = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    first = {"t1": True, "t2": True, "t3": True, "t4": False, "t5": False}        # +0.4
    passed, _, objects = confirmed(tmp_path, first, first, control)
    failed, _, _ = confirmed(tmp_path, first, dict.fromkeys(FIVE, False), control)   # confirmation −0.2
    assert (passed.verdict, passed.kind, failed.verdict, failed.kind) == ("accepted", "confirmed", "rejected",
                                                                           "confirmed")
    assert failed.metrics["first_reading"] == passed.metrics["first_reading"] == {"gain": 0.4, "verdict": "accepted"}
    # the authorization: a confirming plan installs only a confirmed receipt
    objects, releases, parent = workspace(tmp_path / "ws")
    candidate = P.replace_policies(objects, objects.load(parent, ct.SystemRelease).worker_ref,
                                   [P.descriptor(objects, "worker.system_prompt", "prompt", {"text": "x"})])
    confirming_plan = plan(tmp_path, confirmation_repeats=1, control_pooling="pooled")
    for kind, installs in ((None, False), ("full", False), ("confirmed", True)):
        receipt = objects.put(ct.EvaluationReceipt(
            experiment_ref="experiment:sha256:" + "c" * 64, profile="task", subject_ref=candidate,
            control_ref=objects.load(parent, ct.SystemRelease).worker_ref, plan_ref=objects.put(confirming_plan),
            results_ref=objects.put_json("change", {"kind": kind}), verdict="accepted", kind=kind))
        releases.register_receipt(receipt, plan=confirming_plan.ref())
        if installs:
            assert releases.install(receipt, parent, f"op-{kind}").status == "installed"
        else:
            with pytest.raises(NotAuthorized, match="confirms a candidate before it installs"):
                releases.install(receipt, parent, f"op-{kind}")
    assert ct.EvaluationReceipt(verdict="accepted").reading_kind == "full"
    with pytest.raises(ct.ContractError, match="receipt kind"):
        ct.EvaluationReceipt(kind="provisional")


def test_a_regression_is_a_drop_of_at_least_the_plans_magnitude(tmp_path):
    """M3 §2.1(d) (v0.3), ER6-19, IT6-34: under regression_drop 0.5 a task the control passed that the candidate
    passed in one of two repeats (1.0 → 0.5) is not a regression and one it failed in both (1.0 → 0.0) is; the
    receipt lists only the latter. Without the field any drop still counts, as before."""
    assert regression_rate({"t1": 0.5, "t2": 0.0}, {"t1": 1.0, "t2": 1.0}, 0.5) == 0.5
    assert regression_rate({"t1": 0.5, "t2": 0.0}, {"t1": 1.0, "t2": 1.0}, 0.4) == 1.0
    assert regression_rate({"t1": 2 / 3, "t2": 1 / 3}, {"t1": 1.0, "t2": 1.0}, 0.5) == 0.5     # three repeats
    assert regression_rate({"t1": 0.5, "t2": 0.0}, {"t1": 1.0, "t2": 1.0}) == 1.0
    control = rows(plan(tmp_path, repeats=2), {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}, repeats=2)
    for drop, listed in ((0.5, ["t2"]), (None, ["t1", "t2"])):
        p = plan(tmp_path, repeats=2, gate=GatePlan(min_gain=0.05, min_groups=3, max_regression_rate=0.5,
                                                    regression_drop=drop, bootstrap_replicates=200))
        both = rows(p, {"t1": True, "t2": False, "t3": True, "t4": True, "t5": True}, repeats=2)
        subject = [both[0], rows(p, {"t1": False}, repeats=2)[1], *both[2:]]    # t1: one of two repeats
        verdict, reasons, metrics, _ = judge(p, subject, rows(p, {"t1": True, "t2": True, "t3": False, "t4": False,
                                                                 "t5": False}, repeats=2), FAMILIES)
        assert [r["task"] for r in metrics["regressions"]] == listed, drop
        assert metrics["regression_rate"] == pytest.approx(len(listed) / 5)
    del control
    with pytest.raises(ct.ContractError, match="regression_drop"):
        GatePlan(regression_drop=1.5)


def test_a_candidate_whose_cost_outgrows_its_gain_is_rejected_with_the_cost_reason(tmp_path):
    """M3 §2.1(e) (v0.3), ER6-20, IT6-34: with β0 0.10 and β1 2.0 a candidate at +0.2 may cost up to
    0.45 × (1 + 0.1 + 0.4) = 0.675 an attempt; at 0.60 it passes with the allowance on the receipt, at 0.70 it is
    rejected with the numbers as the reason. Without the betas (an older plan) cost is reported and not judged."""
    control = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    subject = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}
    costed = GatePlan(min_gain=0.05, min_groups=3, cost_beta0=0.10, cost_beta1=2.0, bootstrap_replicates=200)
    for cost, verdict in ((0.60, "accepted"), (0.70, "rejected")):
        p = plan(tmp_path, gate=costed)
        got, reasons, metrics, _ = judge(p, rows(p, subject, cost=cost), rows(p, control, cost=0.45), FAMILIES)
        assert got == verdict, (cost, reasons)
        assert metrics["cost_per_attempt"] == {"subject": pytest.approx(cost), "control": pytest.approx(0.45),
                                               "allowed": pytest.approx(0.675)}
    assert reasons == ["cost per attempt 0.7 above the allowed 0.675 (control 0.45 × (1 + β0 0.1 + β1 2.0 × gain 0.2))"]
    p = plan(tmp_path)
    got, reasons, metrics, _ = judge(p, rows(p, subject, cost=0.70), rows(p, control, cost=0.45), FAMILIES)
    assert got == "accepted" and metrics["cost_per_attempt"]["allowed"] is None
    with pytest.raises(ct.ContractError, match="both cost_beta0 and cost_beta1"):
        GatePlan(cost_beta0=0.1)


def test_unknown_cost_follows_the_missing_cost_policy_in_the_cost_term(tmp_path):
    """M3 §2.1(e) (v0.3): an attempt without a price is unknown, never zero — under `refuse` the gate is
    inconclusive and says so, under `report_unknown` the cost term is skipped and the reason records that."""
    control = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    subject = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}
    for policy, verdict in (("refuse", "inconclusive"), ("report_unknown", "accepted")):
        p = plan(tmp_path, gate=GatePlan(min_gain=0.05, min_groups=3, cost_beta0=0.10, cost_beta1=2.0,
                                         missing_cost_policy=policy, bootstrap_replicates=200))
        got, reasons, metrics, _ = judge(p, rows(p, subject, cost=0.6), rows(p, control), FAMILIES)
        assert got == verdict and metrics["cost_per_attempt"]["control"] is None
        assert any("not measured on every valid attempt" in r for r in reasons)
    assert "cost term was not judged" in reasons[-1]


def test_a_confirming_experiment_passes_through_confirming_and_a_restart_finishes_it(tmp_path):
    """M5 §1 (v0.3), EX6-14: the evaluate handler's `confirming` hook writes a provisional `evaluated` (verdict and
    gain, no receipt) and `confirming`; the receipt then closes `evaluated` → `accepted`. Interrupted in
    `confirming`, a restart runs the handler again, writes neither provisional event twice, and ends the same way."""
    from ouroloop.evolve.execution import Execution, execute_experiment
    from test_execution import _experiment
    from ouroloop.evolve.execution import Budgets
    objects = ct.Objects(tmp_path)
    account = Budgets(tmp_path).account("experiment", {"usd": 10.0})
    candidate = "policyset:sha256:" + "e" * 64
    receipt = ct.EvaluationReceipt(experiment_ref="experiment:sha256:" + "f" * 64, profile="task",
                                   subject_ref=candidate, control_ref=candidate,
                                   plan_ref=objects.put_json("change", {"plan": 1}),
                                   results_ref=objects.put_json("change", {"results": []}), verdict="accepted",
                                   kind="confirmed", metrics={"gain": 0.3, "first_reading": {"gain": 0.4}})
    calls = []

    def evaluate(ref, budget, confirming=None):
        calls.append(ref)
        confirming({"gain": 0.4, "verdict": "accepted"})
        if len(calls) == 1:
            raise KeyboardInterrupt("the process died during the confirmation reading")
        return receipt
    handlers = {"build": lambda experiment, budget: ct.BuildResult(status="ready", candidate_ref=candidate),
                "evaluate": evaluate, "load_receipt": lambda ref: objects.load(ref, ct.EvaluationReceipt)}
    ex = Execution(tmp_path, "exp-6")
    with pytest.raises(KeyboardInterrupt):
        execute_experiment(_experiment(tmp_path), handlers, budget=account, execution=ex)
    history = ex.state()["history"]
    assert [e["status"] for e in history] == ["admitted", "building", "evaluating", "evaluated", "confirming"]
    assert history[3]["provisional"] is True and history[3]["gain"] == 0.4 and "receipt" not in history[3]
    out = execute_experiment(_experiment(tmp_path), handlers, budget=account, execution=ex)
    assert out["resumed_from"] == "confirming" and out["states"] == ["evaluated", "accepted"]
    assert [e["status"] for e in ex.state()["history"]][5:] == ["evaluated", "accepted"]
    assert ex.state()["history"][5]["receipt"] == receipt.ref() and len(calls) == 2


def test_a_plan_takes_one_confirmation_reading(tmp_path):
    """M3 §2.1(b) (v0.3, after review): `confirmation_repeats` is 0 or 1 — the rule is two readings each clearing
    the gate, a third reading would only be paid for; `regression_drop` is strictly inside (0, 1), since at 1.0
    "more than" never fires. The receipt's `readings` is 1 + confirmation_repeats."""
    with pytest.raises(ct.ContractError, match=r"0 or 1 — one confirmation reading \(M3 §2.1\(b\)\)"):
        plan(tmp_path, confirmation_repeats=2)
    assert plan(tmp_path, confirmation_repeats=0).confirmations == 0
    with pytest.raises(ct.ContractError, match=r"strictly inside \(0, 1\)"):
        GatePlan(regression_drop=1.0)
    assert GatePlan(regression_drop=0.99).regression_drop == 0.99
    control = dict.fromkeys(FIVE, False)
    first = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}
    receipt, _, _ = confirmed(tmp_path, first, first, control)
    assert receipt.metrics["readings"] == 2 == 1 + TaskEvalPlan.from_json(
        ct.Objects(tmp_path).get(receipt.plan_ref)).confirmations


# ---------------------------------------------------------------------------- v0.3 step 5 (M7 §4): a gate over several domains
def test_a_receipt_over_several_domains_carries_per_domain_means(tmp_path):
    """Review finding 7 (M7 §4 按领域分列): a gate whose tasks span two upstream domains files a receipt whose
    `metrics.per_domain` gives, per domain, the task count, each arm's mean and the difference — means only, no task
    id — from the same per-task means the gain is computed from; the experience log keeps it out (R reads the overall
    gain and the failure-type histogram). Task ids that name no domain (`domain.task`) give no breakdown."""
    from ouroloop.evolve.experience import experiment_record
    tasks = ("sales.a", "sales.b", "support.a", "support.b", "support.c")
    families = {t: t for t in tasks}
    p = TaskEvalPlan(candidate_ref="policyset:sha256:" + "a" * 64, control_ref="policyset:sha256:" + "b" * 64,
                     parent_release="release:sha256:" + "c" * 64, core_ref="core:sha256:" + "d" * 64,
                     split_ref="split-1", task_ids=tasks, repeats=1,
                     gate_plan=GatePlan(mode="quality", min_gain=0.05, min_groups=3, bootstrap_replicates=200).to_json())
    outcomes = {"subject": {"sales.a": True, "sales.b": True, "support.a": True, "support.b": False, "support.c": False},
                "control": {"sales.a": False, "sales.b": False, "support.a": True, "support.b": False, "support.c": False}}

    def run(arm, order, retry=0, supersedes=None):
        return [TaskResult(t, t, "dev", i, arm, f"run-{arm}", "manifest-1", success=outcomes[arm][t],
                           partial_credit=1.0 if outcomes[arm][t] else 0.5, cost_usd=0.5,
                           attempt_id=f"run-{arm}/{t}/r{i}/{arm}#0", pair_key=pair_key(p.ref(), t, i))
                for t, i in order]
    objects = ct.Objects(tmp_path)
    receipt = evaluate_task(p, run=run, objects=objects, experiment_ref="experiment:sha256:" + "e" * 64,
                            families=families)
    split = receipt.metrics["per_domain"]
    assert split == {"sales": {"tasks": 2, "subject": 1.0, "control": 0.0, "gain": 1.0},
                     "support": {"tasks": 3, "subject": round(1 / 3, 6), "control": round(1 / 3, 6), "gain": 0.0}}
    assert receipt.metrics["gain"] == pytest.approx(0.4)
    assert not any(t in json.dumps(split) for t in tasks)
    assert "per_domain" not in experiment_record(experiment_id="x", proposal_ref="p", owner="worker", hypothesis="h",
                                                 changes=(), receipt=receipt)["metrics"]
    single = judge(plan(tmp_path), rows(plan(tmp_path), {"t1": True, "t3": True, "t4": False}),
                   rows(plan(tmp_path), {"t1": False, "t3": False, "t4": False}), FAMILIES)[2]
    assert "per_domain" not in single
