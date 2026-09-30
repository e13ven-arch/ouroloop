"""M6 acceptance (docs/modules/06_RESEARCH_CAMPAIGN.md §5): what a campaign compares, what it refuses, and what
it may not conclude."""
import json

import pytest
import rsi

from ouroloop import bench, policies as P
from ouroloop.benchmark import TaskResult
from ouroloop.evolve import contracts as ct
from ouroloop.evolve.execution import BudgetError, Budgets
from ouroloop.evolve.task_gate import GatePlan
from ouroloop.research.campaign import (ArmOutcome, CampaignError, CampaignPair, CampaignPlan, arm_workspace,
                                        judge_research, run_campaign, select_w, validate)

W = {name: f"policyset:sha256:{i:064x}" for i, name in enumerate(("w0", "w_control", "w_candidate"), start=1)}
R = {name: f"policyset:sha256:{i:064x}" for i, name in enumerate(("r0", "r1"), start=10)}


def pair(objects, *, pair_id="pair-1", group="group-1", outer=("f_outer",), inner_dev=("f_dev",),
         inner_train=("f_train",), w0=None, seeds=(0,)):
    return CampaignPair(pair_id=pair_id, independence_group=group, initial_w_ref=w0 or W["w0"],
                        initial_experience_ref=objects.put_json("change", {"experience": []}),
                        inner_train_groups=inner_train, inner_dev_groups=inner_dev, outer_groups=outer,
                        split_ref="split-1", repeats=1, seed_schedule=seeds)


def plan(objects, pairs, **kw):
    gate = kw.pop("gate", None) or GatePlan(min_groups=1, min_gain=0.05, bootstrap_replicates=200)
    control = kw.pop("r_control_ref", None) or objects.put(ct.PolicySet(owner="research", policies=()))
    candidate = kw.pop("r_candidate_ref", None) or objects.put(
        ct.PolicySet(owner="research", policies=("policy:sha256:" + "c" * 64,), parent_ref=control))
    return CampaignPlan(id=kw.pop("id", "campaign-1"), core_ref=objects.put(ct.CoreManifest(domain="rsi")),
                        r_control_ref=control, r_candidate_ref=candidate,
                        campaign_pairs=tuple(objects.put(p) for p in pairs), arm_budget={"model_calls": 100},
                        worker_round_limit=1, research_gate_plan=gate.to_json(), **kw)


def outcome(arm, pair_id="pair-1", *, installed=(), w_star="", stop="rounds_done", costs=None, requests=()):
    return ArmOutcome(arm=arm, pair_id=pair_id, w_star=w_star, installed=tuple(installed), stop_reason=stop,
                      costs=dict(costs or {}), research_requests=tuple(requests))


def results(task_success: dict, split="dev") -> list:
    return [TaskResult(t, f"family_{t}", split, 0, "w", "run", "man", success=bool(ok),
                       partial_credit=1.0 if ok else 0.5) for t, ok in task_success.items()]


# ---------------------------------------------------------------------------- RC-04 / RC-05
def test_rc04_a_starting_version_drawn_from_the_outer_families_is_refused(tmp_path):
    objects = ct.Objects(tmp_path)
    p = pair(objects)
    campaign = plan(objects, [p])
    assert validate(campaign, objects=objects, lineage=lambda ref: set()) == []
    reasons = validate(campaign, objects=objects, lineage=lambda ref: {"f_outer"} if ref == W["w0"] else set())
    assert reasons and "already comes from the outer families" in reasons[0]
    # and renaming the task cannot get round it: the check is on the family ids of the lineage
    with pytest.raises(ct.ContractError, match="also inner groups"):
        pair(objects, outer=("f_dev",))


def test_rc05_ten_seeds_on_one_pool_are_one_independence_group(tmp_path):
    objects = ct.Objects(tmp_path)
    pairs = [pair(objects, pair_id=f"pair-{i}", group="group-1", seeds=(i,)) for i in range(10)]
    campaign = plan(objects, pairs)
    outer = {p.pair_id: {"candidate": results({"t1": True}), "control": results({"t1": False})} for p in pairs}
    arms = {"control": [outcome("control", p.pair_id) for p in pairs],
            "candidate": [outcome("candidate", p.pair_id) for p in pairs]}
    verdict, reasons, metrics, uncertainty = judge_research(campaign, pairs, outer, arms)
    assert metrics["groups"] == 1 and metrics["pairs"] == 10
    assert uncertainty["significant"] is False          # one group is never a significant comparison
    assert verdict == "accepted" and "not supported by this run" in reasons[-1]


def test_a_final_campaign_needs_more_than_one_group(tmp_path):
    objects = ct.Objects(tmp_path)
    campaign = plan(objects, [pair(objects)], phase="final")
    reasons = validate(campaign, objects=objects)
    assert reasons and "more seeds on one pool do not make it two" in reasons[0]
    with pytest.raises(CampaignError):
        run_campaign(campaign, run_arm=lambda *a: outcome("x"), evaluate_outer=lambda *a: {},
                     objects=objects, budget=Budgets(tmp_path).account("c", {}))


def test_the_two_arms_may_not_run_the_same_policy_and_the_candidate_must_be_research(tmp_path):
    objects = ct.Objects(tmp_path)
    same = objects.put(ct.PolicySet(owner="research", policies=()))
    with pytest.raises(ct.ContractError, match="same research policy"):
        plan(objects, [pair(objects)], r_control_ref=same, r_candidate_ref=same)
    worker_set = objects.put(ct.PolicySet(owner="worker", policies=()))
    campaign = plan(objects, [pair(objects)], r_candidate_ref=worker_set)
    assert any("worker policy set" in r for r in validate(campaign, objects=objects))


# ---------------------------------------------------------------------------- RC-02 / selection
def test_rc02_an_arm_that_installed_nothing_returns_w0_with_its_costs(tmp_path):
    objects = ct.Objects(tmp_path)
    p = pair(objects)
    assert select_w(outcome("control", stop="no_proposal", costs={"model_calls": 12}), p) == W["w0"]
    assert select_w(outcome("candidate", installed=("release:sha256:" + "a" * 64,)), p) == \
        "release:sha256:" + "a" * 64
    campaign = plan(objects, [p])
    arms = {"control": [outcome("control", stop="budget_exhausted", costs={"model_calls": 12})],
            "candidate": [outcome("candidate", stop="rounds_done", costs={"model_calls": 30})]}
    outer = {"pair-1": {"candidate": results({"t1": True}), "control": results({"t1": False})}}
    verdict, reasons, metrics, _ = judge_research(campaign, [p], outer, arms)
    assert "pair-1/control" in metrics["not_reached"] and verdict == "accepted"
    assert metrics["installed"] == {"control": [0], "candidate": [0]}


def test_an_arm_with_no_outer_result_is_inconclusive_rather_than_a_win(tmp_path):
    objects = ct.Objects(tmp_path)
    p = pair(objects)
    campaign = plan(objects, [p])
    outer = {"pair-1": {"candidate": results({"t1": True}), "control": []}}
    verdict, reasons, metrics, _ = judge_research(campaign, [p], outer,
                                                 {"control": [outcome("control")],
                                                  "candidate": [outcome("candidate")]})
    assert verdict == "inconclusive" and any("no valid outer attempt" in r for r in reasons)


def test_rc09_a_chain_that_ran_without_a_gain_is_reported_as_not_supported(tmp_path):
    objects = ct.Objects(tmp_path)
    p = pair(objects)
    campaign = plan(objects, [p])
    outer = {"pair-1": {"candidate": results({"t1": False, "t2": True}),
                        "control": results({"t1": True, "t2": True})}}
    verdict, reasons, metrics, uncertainty = judge_research(campaign, [p], outer,
                                                           {"control": [outcome("control")],
                                                            "candidate": [outcome("candidate")]})
    assert verdict == "rejected" and metrics["gain"] < 0 and uncertainty["significant"] is False


def test_rc10_a_hand_written_candidate_is_labelled_manual(tmp_path):
    objects = ct.Objects(tmp_path)
    p = pair(objects)
    campaign = plan(objects, [p], discovery="manual_seed")
    outer = {"pair-1": {"candidate": results({"t1": True}), "control": results({"t1": False})}}
    verdict, reasons, metrics, _ = judge_research(campaign, [p], outer,
                                                 {"control": [outcome("control")],
                                                  "candidate": [outcome("candidate")]})
    assert metrics["discovery"] == "manual_seed"
    assert any("written by hand" in r for r in reasons) and verdict == "accepted"


# ---------------------------------------------------------------------------- RC-06 / isolation
def test_rc06_an_arm_cannot_be_given_more_than_its_share(tmp_path):
    budgets = Budgets(tmp_path)
    top = budgets.account("campaign", {"model_calls": 100})
    a = top.child("campaign:pair-1:control", {"model_calls": 50})
    b = top.child("campaign:pair-1:candidate", {"model_calls": 50})
    a.settle(a.reserve("op-a", {"model_calls": 50}).id, {"model_calls": 50})
    with pytest.raises(BudgetError):
        b.reserve("op-b", {"model_calls": 51})
    assert b.reserve("op-b", {"model_calls": 50}).bound == {"model_calls": 50.0}
    assert top.remaining()["remaining"]["model_calls"] == 0.0


def test_each_arm_gets_its_own_workspace_path(tmp_path):
    control = arm_workspace(tmp_path, "pair-1", "control")
    candidate = arm_workspace(tmp_path, "pair-1", "candidate")
    assert control != candidate and control.parent == candidate.parent


def test_run_campaign_files_every_arm_and_its_outer_results(tmp_path):
    objects = ct.Objects(tmp_path)
    p = pair(objects)
    campaign = plan(objects, [p])
    seen = []

    def run_arm(pair_obj, arm, r_ref, budget):
        seen.append((pair_obj.pair_id, arm, r_ref, budget.scope_id))
        return outcome(arm, pair_obj.pair_id, w_star=W["w_candidate"] if arm == "candidate" else W["w_control"],
                       installed=("release:sha256:" + "b" * 64,) if arm == "candidate" else (),
                       requests=({"step": "propose", "prompt_hash": arm},))

    def evaluate_outer(pair_obj, sealed):
        assert sealed == {"control": W["w_control"], "candidate": W["w_candidate"]}
        return {"candidate": results({"t1": True}), "control": results({"t1": False})}

    events = ct.Events(tmp_path)
    receipt = run_campaign(campaign, run_arm=run_arm, evaluate_outer=evaluate_outer, objects=objects,
                           budget=Budgets(tmp_path).account("campaign", {"model_calls": 1000}), events=events)
    assert [arm for _p, arm, _r, _s in seen] == ["control", "candidate"]
    assert {scope for *_x, scope in seen} == {"campaign-1:pair-1:control", "campaign-1:pair-1:candidate"}
    assert receipt.profile == "research" and receipt.verdict == "accepted"
    stored = objects.get(receipt.results_ref)
    assert set(stored["arms"]) == {"control", "candidate"}
    assert stored["arms"]["candidate"][0]["research_requests"][0]["prompt_hash"] == "candidate"
    assert [e["arm"] for e in events.of("campaign_arm")] == ["control", "candidate"]
    assert receipt.costs == {"control": {}, "candidate": {}}


@pytest.mark.parametrize("stop", ["repair_runtime", "infrastructure_failures"])
def test_rc6_11_a_paused_arm_is_not_a_finished_arm_and_nothing_is_compared(tmp_path, stop):
    objects = ct.Objects(tmp_path)
    campaign = plan(objects, [pair(objects, pair_id="pair-1"), pair(objects, pair_id="pair-2", group="group-2",
                                                                    inner_dev=("f_dev2",), outer=("f_outer2",))])
    ran = []

    def run_arm(pair_obj, arm, r_ref, budget):
        ran.append((pair_obj.pair_id, arm))
        return outcome(arm, pair_obj.pair_id, stop=stop if arm == "candidate" else "rounds_done")

    def evaluate_outer(pair_obj, sealed):
        pytest.fail("a pair with a paused arm is not measured on the outer tasks")

    events = ct.Events(tmp_path)
    receipt = run_campaign(campaign, run_arm=run_arm, evaluate_outer=evaluate_outer, objects=objects,
                           budget=Budgets(tmp_path).account("campaign", {"model_calls": 1000}), events=events)
    # the other arm still ran (its rounds are kept for the resumed run), but no later pair and no outer run
    assert ran == [("pair-1", "control"), ("pair-1", "candidate")]
    assert receipt.verdict == "inconclusive"
    assert any(f"pair-1/candidate paused ({stop})" in r for r in receipt.reasons)
    assert any("resumes to the frozen round count" in r for r in receipt.reasons)
    assert [e["paused"] for e in events.of("campaign_arm")] == [False, True]


# ---------------------------------------------------------------------------- IT6-16: a false recursion
def test_it6_16_an_r1_that_only_renames_r0_is_refused_before_anything_runs(tmp_path):
    objects = ct.Objects(tmp_path)
    r0 = P.default_research_set(objects, note_contract=True)
    renamed = objects.put(ct.PolicySet(owner="research", policies=objects.load(r0, ct.PolicySet).policies,
                                       parent_ref=r0))
    assert renamed != r0
    campaign = plan(objects, [pair(objects)], r_control_ref=r0, r_candidate_ref=renamed)
    reasons = validate(campaign, objects=objects)
    assert reasons and "only its manifest differs" in reasons[0]


def test_it6_16_a_candidate_arm_that_sent_r0s_prompt_makes_the_comparison_invalid(tmp_path):
    objects = ct.Objects(tmp_path)
    r0 = P.default_research_set(objects, note_contract=True)
    r1 = P.default_research_set(objects, note_contract=True, propose="R1: propose the smallest change.")
    campaign = plan(objects, [pair(objects)], r_control_ref=r0, r_candidate_ref=r1)
    prompts = {"control": P.prompts_of(objects, r0), "candidate": P.prompts_of(objects, r1)}
    assert prompts["control"]["research.diagnose"] == prompts["candidate"]["research.diagnose"]

    def arm_with(requests):
        def run_arm(pair_obj, arm, r_ref, budget):
            return outcome(arm, pair_obj.pair_id, requests=requests if arm == "candidate" else ())
        return run_arm

    def evaluate_outer(pair_obj, sealed):
        return {"candidate": results({"t1": True}), "control": results({"t1": False})}

    def run(requests, name):
        # each run on its own book: a campaign's arm scopes are fixed once per book
        return run_campaign(campaign, run_arm=arm_with(requests), evaluate_outer=evaluate_outer, objects=objects,
                            budget=Budgets(tmp_path / name).account("campaign", {"model_calls": 1000}),
                            prompts=prompts)

    r0_propose = prompts["control"]["research.propose"] + "\n\nFORMAT"
    r1_propose = prompts["candidate"]["research.propose"] + "\n\nFORMAT"
    # the manifest said R1, the propose request carried R0's text: nothing about R1 was compared
    lying = run(({"step": "propose", "system": r0_propose},), "a")
    assert lying.verdict == "invalid"
    assert "no propose/revise request carried R1's research.propose" in lying.reasons[0]
    # the request carried R1's text: the comparison stands on its outer results
    honest = run(({"step": "note", "system": prompts["candidate"]["research.diagnose"] + "\n\nNOTE"},
                  {"step": "propose", "system": r1_propose}), "b")
    assert honest.verdict == "accepted"
    # a researcher that records no prompt cannot show it either way, and the receipt says so
    silent = run(({"step": "propose"},), "c")
    assert silent.verdict == "accepted" and any("not verified" in r for r in silent.reasons)


# ---------------------------------------------------------------------------- RC-03 / RC-07 / RC-08
def test_rc03_a_branch_cannot_read_the_other_branch_or_the_mainline(fake_upstream, tmp_path):
    from test_rsi_integration import campaign_pairs, prepared
    lines = []
    cfg, activity, w0_release, plan_ref = prepared(fake_upstream, tmp_path, out=lines.append)
    w0 = ct.SystemRelease.from_json(activity.objects.get(w0_release)).worker_ref
    slot = bench.research_slot(cfg, ".", pairs=campaign_pairs(activity, initial_w=w0),
                               researcher=rsi.ResearchPolicyProposer(),
                               arm_researcher=lambda: rsi.ScriptedResearcher(follow_prompt=True), out=lines.append,
                               worker_rounds=1)
    from ouroloop.research.campaign import CampaignPlan
    plan_id = activity.objects.load(slot["campaign"], CampaignPlan).id
    control = arm_workspace(activity.root, "pair-1", "control", plan_id)
    candidate = arm_workspace(activity.root, "pair-1", "candidate", plan_id)
    # the arms' evidence is separate, and neither holds the outer tasks' results
    for side in (control, candidate):
        attempts = [json.loads(line) for line in
                    (side / "bench" / rsi.DOMAIN / "results.jsonl").read_text().splitlines()]
        assert attempts and all(a["task_id"] != "rsi.notify_after_update" for a in attempts), side
        assert {a["split"] for a in attempts} <= {"train", "dev"}
    # the outer results live only in the evaluator's own workspace
    outer = [json.loads(line) for line in
             (activity.root / "bench" / rsi.DOMAIN / "results.jsonl").read_text().splitlines()]
    assert any(a["task_id"] == "rsi.notify_after_update" and a["candidate"].endswith("w_star") for a in outer)


def test_rc07_and_rc08_a_branch_w_never_updates_the_mainline(fake_upstream, tmp_path):
    from test_rsi_integration import campaign_pairs, prepared
    lines = []
    cfg, activity, w0_release, plan_ref = prepared(fake_upstream, tmp_path, out=lines.append)
    objects = activity.objects
    w0 = ct.SystemRelease.from_json(objects.get(w0_release)).worker_ref
    outcome_ = bench.research_slot(cfg, ".", pairs=campaign_pairs(activity, initial_w=w0),
                                   researcher=rsi.ResearchPolicyProposer(),
                                   arm_researcher=lambda: rsi.ScriptedResearcher(follow_prompt=True),
                                   out=lines.append, worker_rounds=1)
    assert outcome_["verdict"] == "accepted"
    after = ct.SystemRelease.from_json(objects.get(activity.releases.active()["release_ref"]))
    # only R changed: the W the winning arm produced is evidence about a research policy, not a promotion
    assert after.worker_ref == w0 and after.research_ref == outcome_["r1"]
    arms = [e for e in ct.Events(activity.root).of("campaign_arm")]
    branch_w = {e["w_star"] for e in arms} - {w0}
    assert branch_w and branch_w.isdisjoint({after.worker_ref})
