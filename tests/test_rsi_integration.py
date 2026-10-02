"""The integration acceptance of docs/modules/08_INTEGRATION_CASES.md: the whole loop over the fixture domain.

Every report built here is a `synthetic_contract_test`. The fixture's numbers are the fixture's; what these tests
show is that the path is real — the policy that runs is the one that was installed, the labels come from observed
facts, the pointer moves only through a receipt, and a crash leaves something recoverable.
"""
import json

import pytest
import rsi

from ouroloop import bench, policies as P
from ouroloop.evolve import contracts as ct
from ouroloop.evolve.execution import Budgets, Schedule
from ouroloop.evolve.task_gate import GatePlan
from ouroloop.report import build_report, render_markdown


def config(fake_upstream, tmp_path, **extra):
    cfg = {"workspace": {"root": str(tmp_path / "ws")},
           "bench": {"domain": rsi.DOMAIN, "upstream_path": fake_upstream, "families": dict(rsi.FAMILIES),
                     "budget": {"max_steps": 8, "max_tool_calls": 12},
                     "ratios": {"train": 0.5, "dev": 0.25, "test": 0.25}},
           "llm": {"type": "rsi:BusinessWorker"},
           "backends": {"mock": {"type": "mock"}},
           "rsi": {"scope_id": "it", "repeats": 1}}
    for key, value in extra.items():
        cfg[key] = {**cfg.get(key, {}), **value} if isinstance(value, dict) else value
    return cfg


def prepared(fake_upstream, tmp_path, *, cfg=None, gate=None, screen=(), dev=None, train=False, out=None):
    """A workspace with W0 installed and a frozen development plan, as `bench improve` needs it."""
    cfg = cfg or config(fake_upstream, tmp_path)
    if train:
        cfg["trainer"] = {"type": "rsi:PhraseTrainer"}
    # The fixture's splits are declared, not hashed: these tasks were written to be a training pool, a
    # development pool and a final pool, and the test says which is which (docs/DESIGN.md §3.1.2).
    from ouroloop.benchmark import Results
    suite = rsi.suite(fake_upstream)
    rsi.splits(suite).freeze(Results(tmp_path / "ws", rsi.DOMAIN).manifest_path())
    activity = bench.open_activity(cfg, ".", out=out or (lambda _line: None))
    # W0: no finish-check instruction, which is the gap the loop starts from
    w0 = rsi.w0(activity.objects)
    release = activity.releases.bootstrap(w0, P.default_research_set(activity.objects),
                                          ct.SystemRelease.from_json(
                                              activity.objects.get(activity.release_ref)).core_ref,
                                          reason="W0 for the integration fixture", activate=False)
    from ouroloop.ledger import write_json
    write_json(activity.releases.active_path, {"release_ref": release, "revision": 1, "last_operation_id": None})
    plan_ref = bench.freeze_task_plan(
        activity.objects, core_ref=ct.SystemRelease.from_json(activity.objects.get(release)).core_ref,
        split_ref=activity.splits.hash(), task_ids=tuple(dev or rsi.DEV),
        gate=gate or GatePlan(min_gain=0.05, min_groups=1, bootstrap_replicates=200), screen_ids=tuple(screen))
    cfg["rsi"]["task_plan_ref"] = plan_ref
    cfg["rsi"]["schedule_ref"] = activity.objects.put_json("change", {"worker_rounds": 1, "patience": 1,
                                                                     "candidates_per_round": 1})
    cfg["rsi"]["budget_ref"] = activity.objects.put_json("change", {"model_calls": 4000, "tool_calls": 4000,
                                                                   "candidates": 4, "train_jobs": 2,
                                                                   "training_seconds": 600})
    return cfg, activity, release, plan_ref


# ---------------------------------------------------------------------------- IT-01
def test_it01_a_prompt_candidate_closes_the_gap_and_the_next_session_uses_it(fake_upstream, tmp_path):
    lines = []
    cfg, activity, w0_release, plan_ref = prepared(fake_upstream, tmp_path, out=lines.append)
    result = bench.improve(cfg, ".", out=lines.append, researcher=rsi.ScriptedResearcher())
    rounds = [r for r in result["rounds"] if r["outcome"] not in ("stopped",)]
    assert rounds and rounds[0]["outcome"] == "installed", [r["outcome"] for r in result["rounds"]]
    assert result["release"] != w0_release
    objects = activity.objects
    after = ct.SystemRelease.from_json(objects.get(result["release"]))
    assert after.research_ref == ct.SystemRelease.from_json(objects.get(w0_release)).research_ref
    # the installed W really carries the instruction, and a new session composes it into the prompt
    bound = P.bind_release(result["release"], profile="task",
                           services=P.Services(objects, serving=activity.rt.serving), authorization="installed")
    assert P.FINISH_CHECK in bound.system_prompt("BASE")[0]
    # and it was reached through a receipt, not by writing the pointer
    receipts = [e for e in ct.Events(activity.root).of("evaluation_completed")]
    installs = [e for e in ct.Events(activity.root).of("installed")]
    assert receipts and installs and installs[-1]["release"] == result["release"]
    receipt = objects.load(receipts[-1]["receipt"], ct.EvaluationReceipt)
    assert receipt.verdict == "accepted" and receipt.metrics["gain"] > 0
    assert receipt.uncertainty["significant"] in (True, False)      # stated either way, never implied
    # no training was needed
    experiments = [e for e in __import__("ouroloop.evolve.experience", fromlist=["x"]).read(activity.root)
                   if e.get("kind") == "experiment"]
    assert experiments and experiments[-1]["trained"] is False and experiments[-1]["installed"] is True
    assert activity.budgets.account("it", {"model_calls": 4000, "tool_calls": 4000, "candidates": 4,
                                           "train_jobs": 2, "training_seconds": 600}).remaining()["used"].get(
        "train_jobs") is None


def test_the_installed_version_makes_the_failing_dev_task_pass(fake_upstream, tmp_path):
    cfg, activity, w0_release, plan_ref = prepared(fake_upstream, tmp_path)
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker())
    order = [(t, 0) for t in rsi.DEV]
    before = work.run_arm("before", w0_release, order, plan_ref="", split="dev")
    w1 = P.replace_policies(activity.objects, rsi.w0(activity.objects),
                            [P.descriptor(activity.objects, "worker.finish", "prompt",
                                          {"text": P.FINISH_CHECK})])
    release = activity.releases.bootstrap(w1, ct.SystemRelease.from_json(
        activity.objects.get(w0_release)).research_ref,
        ct.SystemRelease.from_json(activity.objects.get(w0_release)).core_ref, reason="W1", activate=False)
    after = work.run_arm("after", release, order, plan_ref="", split="dev")
    passed = {r.task_id: r.success for r in before}, {r.task_id: r.success for r in after}
    assert passed[0]["rsi.dev_notify_after_update"] is False
    assert passed[1]["rsi.dev_notify_after_update"] is True
    assert all(passed[1][t] for t in passed[1])           # nothing else regressed


# ---------------------------------------------------------------------------- IT-05 (permissions)
def test_it05_each_boundary_is_refused_by_the_module_that_owns_it(fake_upstream, tmp_path):
    from ouroloop.evolve.build import build_task_dataset, check_lineage
    from ouroloop.evolve.feedback import build_feedback
    from ouroloop.points import business_stop_point
    from ouroloop.research.context import AccessDenied, make_context
    cfg, activity, release, plan_ref = prepared(fake_upstream, tmp_path)
    objects = activity.objects
    # a research proposal that changes the scorer, or executes Python, is refused by the registry
    with pytest.raises(ct.ContractError):
        P.descriptor(objects, "worker.stop", "rule", {"builtin": "automationbench.rubric:partial_credit"})
    with pytest.raises(ct.ContractError):
        P.descriptor(objects, "research.retrieve", "rule",
                     {"builtin": "research_retrieve.v1", "sort": "__import__('os').system"})
    # final evidence, however it is wrapped, does not reach a context or a dataset
    final = objects.put(ct.EvidenceRef(id="f1", kind="summary", scope_id="it", partition="final",
                                       producer_ref="llm", content_ref=objects.put_json("change", {"x": 1})))
    with pytest.raises(AccessDenied):
        make_context("it", goal="g", release_ref=release, evidence_refs=(final,), objects=objects)
    # a candidate's exception is a defect, not an infinitely retryable infrastructure failure
    from ouroloop.evolve.task_gate import TaskEvalPlan, judge
    from ouroloop.benchmark import TaskResult, pair_key
    plan = TaskEvalPlan.from_json(objects.get(plan_ref))
    defect = TaskResult("rsi.dev_update_stage", "rsi.dev_update", "dev", 0, "c", "run", "man",
                        validity="invalid", invalid_reason="candidate: illegal action",
                        termination="harness_error", failure_class="candidate_defect",
                        pair_key=pair_key(plan.ref(), "rsi.dev_update_stage", 0))
    verdict, reasons, _, _ = judge(plan, [defect], [defect], {e.task_id: e.family_id
                                                             for e in activity.splits.entries})
    assert verdict == "invalid" and "candidate defect" in reasons[0]


# ---------------------------------------------------------------------------- IT-04 (recovery)
def test_it04_a_crash_between_preflight_and_the_pointer_recovers_to_one_commit(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = prepared(fake_upstream, tmp_path)
    objects, releases = activity.objects, activity.releases
    candidate = P.replace_policies(objects, ct.SystemRelease.from_json(objects.get(release)).worker_ref,
                                   [P.descriptor(objects, "worker.finish", "prompt", {"text": P.FINISH_CHECK})])
    receipt = ct.EvaluationReceipt(experiment_ref="experiment:sha256:" + "a" * 64, profile="task",
                                   subject_ref=candidate,
                                   control_ref=ct.SystemRelease.from_json(objects.get(release)).worker_ref,
                                   plan_ref=plan_ref, results_ref=objects.put_json("change", {"r": []}),
                                   verdict="accepted")
    ref = objects.put(receipt)
    releases.register_receipt(ref, plan=plan_ref)
    crashed = {"count": 0}

    def preflight(target):
        crashed["count"] += 1
        if crashed["count"] == 1:
            raise RuntimeError("the process died after preflight")
        return {"release": target}

    first = releases.install(ref, release, "op-recover", preflight=preflight)
    assert first.status == "installation_failed" and releases.active()["release_ref"] == release
    again = releases.install(ref, release, "op-recover", preflight=preflight)
    assert again.status == "installed"
    revision = releases.active()["revision"]
    third = releases.install(ref, release, "op-recover", preflight=preflight)
    assert third.status == "installed" and releases.active()["revision"] == revision
    assert releases.recover("op-recover").status == "installed"


# ---------------------------------------------------------------------------- IT-07 (the report)
def test_it07_a_report_is_recomputed_from_the_records_and_never_picks_a_winner(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = prepared(fake_upstream, tmp_path)
    bench.improve(cfg, ".", out=lambda _l: None, researcher=rsi.ScriptedResearcher())
    first = build_report(activity.root, rsi.DOMAIN)
    again = build_report(activity.root, rsi.DOMAIN)
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(again, sort_keys=True, default=str)
    text = render_markdown(first)
    assert "ouroloop report" in text and "## Claims" in text
    # C2 has no frontier run here, and C4 no campaign: both say so instead of being omitted
    assert first["claims"]["C2"]["state"] == "not_run" and first["claims"]["C4"]["state"] == "not_run"
    assert any("frontier" in line for line in first["identity"]["missing"])
    # the improvement evidence separates proposed, accepted and installed
    evidence = first["improvement_evidence"]
    assert evidence["experiments"] and evidence["receipts"]
    assert [e["verdict"] for e in evidence["receipts"]] == ["accepted"]
    assert any(i["event"] == "installed" for i in evidence["installations"])
    assert first["run_cost"] and all(c["cost_usd"] is None for c in first["run_cost"].values())


# ---------------------------------------------------------------------------- IT-02
def test_it02_a_trained_decider_changes_what_the_agent_does(fake_upstream, tmp_path):
    lines = []
    cfg, activity, w0_release, plan_ref = prepared(fake_upstream, tmp_path, train=True, out=lines.append)
    cfg["serving"] = {}
    result = bench.improve(cfg, ".", out=lines.append,
                           researcher=rsi.ScriptedResearcher(carrier="train", min_labels=2))
    rounds = [r for r in result["rounds"] if r["outcome"] != "stopped"]
    assert rounds and rounds[0]["outcome"] == "installed", [r["outcome"] for r in result["rounds"]] + lines[-6:]
    objects = activity.objects
    installed = ct.SystemRelease.from_json(objects.get(result["release"]))
    policies = P.validate_policy_set(objects.load(installed.worker_ref, ct.PolicySet), objects).policies
    stop = policies["worker.stop"]
    assert stop.kind == "model" and stop.implementation["model_ref"] == "trained-1"
    inference = objects.get(stop.implementation["inference_ref"])
    assert inference["model_id"] == "trained-1" and inference["threshold_source"] == "argmax"
    # the dataset kept the families apart and carried no oracle
    manifest = ct.DataManifest.from_json(objects.get(
        [e for e in ct.Events(activity.root).all() if e.get("event") == "state"] and
        _dataset_ref(objects)))
    assert set(manifest.fit_groups) & set(rsi.SPLITS) and not (set(manifest.fit_groups)
                                                              & set(manifest.validation_groups))
    assert not (set(manifest.fit_groups) & {f for f, s in rsi.SPLITS.items() if s != "train"})
    rows = [json.loads(line) for line in
            (activity.root / "rsi" / "datasets" / "worker.stop" / "train.jsonl").read_text().splitlines()]
    assert rows and all("rubric" not in r["state"] and "assertion" not in r["state"] for r in rows)
    assert {c["name"] for r in rows for c in r["candidates"]} == {"yes", "no"}
    # and the installed model really intervenes: a "no" sends the agent back, and the task then completes
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker())
    work.serving = activity.rt.serving
    after = work.run_arm("after_install", result["release"], [("rsi.dev_notify_after_update", 0)], plan_ref="",
                         split="dev")
    assert after[0].success is True
    stops = [r.decision for r in activity.rt.ledger.rows("stop")
             if r.decision.get("session") == after[0].evidence and r.decision["phase"] == "on_finish"]
    assert [(d["proposed"], d["action"]) for d in stops][:1] == [("no", "no")]
    assert stops[-1]["proposed"] == "yes"
    # training, inference and failed jobs are all on the budget book
    used = activity.budgets.account("it", json.loads(
        (activity.root / "rsi" / "budget" / "book.json").read_text())["scopes"]["it"]["limits"]).remaining()["used"]
    assert used.get("train_jobs") == 1.0 and used.get("training_seconds") is not None


def _dataset_ref(objects) -> str:
    directory = objects.dir / "dataset"
    refs = sorted(directory.glob("*.json"))
    assert refs, "a data manifest was written"
    return f"dataset:sha256:{refs[0].stem}"


# ---------------------------------------------------------------------------- IT-03
def campaign_pairs(activity, *, initial_w: str):
    """One pair: the inner loop works on the training families and one development family, and the outer family
    is one the inner loop never touches."""
    from ouroloop.research.campaign import CampaignPair
    return [CampaignPair(pair_id="pair-1", independence_group="group-1", initial_w_ref=initial_w,
                         initial_experience_ref=activity.objects.put_json("change", {"experience": []}),
                         inner_train_groups=("rsi.update", "rsi.hold", "rsi.notify_once"),
                         inner_dev_groups=("rsi.dev_notify_after_update",),
                         outer_groups=("rsi.notify_after_update",), split_ref=activity.splits.hash(),
                         repeats=1, seed_schedule=(0,))]


def test_it03_a_research_policy_is_proposed_compared_installed_and_then_used(fake_upstream, tmp_path):
    lines = []
    cfg, activity, w0_release, plan_ref = prepared(fake_upstream, tmp_path, out=lines.append)
    objects = activity.objects
    r0 = ct.SystemRelease.from_json(objects.get(w0_release)).research_ref
    w0 = ct.SystemRelease.from_json(objects.get(w0_release)).worker_ref
    outcome = bench.research_slot(cfg, ".", pairs=campaign_pairs(activity, initial_w=w0),
                                  researcher=rsi.ResearchPolicyProposer(),
                                  arm_researcher=lambda: rsi.ScriptedResearcher(follow_prompt=True),
                                  out=lines.append, worker_rounds=1)
    assert outcome["outcome"] == "accepted", (outcome, lines[-8:])
    r1 = outcome["r1"]
    assert r1 != r0
    # the campaign compared what the two arms produced, on tasks neither inner loop optimised on
    receipt = objects.load(outcome["receipt"], ct.EvaluationReceipt)
    assert receipt.profile == "research" and receipt.subject_ref == r1 and receipt.control_ref == r0
    assert receipt.metrics["candidate_outer_pass_rate"] > receipt.metrics["control_outer_pass_rate"]
    assert receipt.metrics["discovery"] == "scripted" and receipt.uncertainty["significant"] is False
    arms = [e for e in ct.Events(activity.root).of("campaign_arm")]
    assert {e["arm"] for e in arms} == {"control", "candidate"}
    assert len({e["w_star"] for e in arms}) == 2                # the two arms produced different versions
    # R1 is installed, and only R changed
    after = ct.SystemRelease.from_json(objects.get(activity.releases.active()["release_ref"]))
    assert after.research_ref == r1 and after.worker_ref == w0
    # the next worker round actually loads R1: the research step's request carries R1's prompt
    researcher = rsi.ScriptedResearcher()
    result = bench.improve(cfg, ".", out=lines.append, researcher=researcher)
    r1_prompt = _prompt_of(objects, r1, "research.propose")
    assert researcher.requests and any(r["prompt_hash"] == ct.digest({"system": r1_prompt})[:16]
                                       for r in researcher.requests if r["step"] == "propose")
    assert r1_prompt != _prompt_of(objects, r0, "research.propose")
    # and that use is on record, not only in the researcher's memory: the round names the R it loaded and the
    # digest of what its propose step sent (docs/modules/06_RESEARCH_CAMPAIGN.md §4)
    steps = [e for e in ct.Events(activity.root).of("research_steps") if e["research_ref"] == r1]
    assert steps and any(s["step"] == "propose" and s["prompt_hash"] == ct.digest({"system": r1_prompt})[:16]
                         for s in steps[-1]["steps"])
    assert objects.has(steps[-1]["context_ref"])
    # and a W2 came out of it, through the ordinary task gate
    rounds = [r for r in result["rounds"] if r["outcome"] != "stopped"]
    assert rounds and rounds[0]["outcome"] in ("installed", "rejected", "inconclusive")
    if rounds[0]["outcome"] == "installed":
        w2 = ct.SystemRelease.from_json(objects.get(result["release"])).worker_ref
        assert w2 != w0 and ct.SystemRelease.from_json(objects.get(result["release"])).research_ref == r1


def test_an_outer_attempt_lost_to_infrastructure_is_refilled_once(fake_upstream, tmp_path, monkeypatch):
    """docs/modules/06_RESEARCH_CAMPAIGN.md §3: an outer attempt the infrastructure lost is run once more under the
    outer plan's frozen retry policy, as the worker gate does: the original stays on record, the refill names it,
    and a restart pays for neither again."""
    from ouroloop.evolve import task_eval
    from ouroloop.research.campaign import CampaignPlan
    lines = []
    cfg, activity, w0_release, plan_ref = prepared(fake_upstream, tmp_path, out=lines.append)
    w0 = ct.SystemRelease.from_json(activity.objects.get(w0_release)).worker_ref
    state = {"armed": False, "lost": 0}
    real_attempt, real_chat = task_eval.run_attempt, rsi.BusinessWorker.chat

    def run_attempt(*a, **kw):
        state["armed"] = kw.get("arm") == "outer_candidate" and kw.get("retry_index") == 0 and not state["lost"]
        return real_attempt(*a, **kw)

    def chat(self, system, messages, specs):
        if state["armed"]:
            state["armed"], state["lost"] = False, state["lost"] + 1
            raise ConnectionResetError("connection reset by peer")
        return real_chat(self, system, messages, specs)
    monkeypatch.setattr(task_eval, "run_attempt", run_attempt)
    monkeypatch.setattr(rsi.BusinessWorker, "chat", chat)
    as_the_campaign_saw_it = bench.open_activity(cfg, ".", out=lambda _line: None)    # before R1 is installed
    slot = bench.research_slot(cfg, ".", pairs=campaign_pairs(activity, initial_w=w0),
                               researcher=rsi.ResearchPolicyProposer(),
                               arm_researcher=lambda: rsi.ScriptedResearcher(follow_prompt=True), out=lines.append,
                               worker_rounds=1)
    assert state["lost"] == 1
    outer = [r for r in bench.Results(activity.root, rsi.DOMAIN).all() if r.candidate == "candidate_w_star"]
    [lost] = [r for r in outer if r.validity == "invalid"]
    assert lost.failure_class == "infrastructure"
    [refilled] = [r for r in outer if r.supersedes]
    assert refilled.supersedes == lost.attempt_id and refilled.attempt_id.endswith("#1")
    assert refilled.validity == "valid"
    # the refill completed the pair, so the comparison stands on every outer task
    receipt = activity.objects.load(slot["receipt"], ct.EvaluationReceipt)
    assert slot["verdict"] == "accepted" and receipt.metrics["incomplete"] == {}, receipt.reasons
    # run again (a restart after the outer comparison): every attempt is on record, refill included
    plan = activity.objects.load(slot["campaign"], CampaignPlan)
    sealed = {e["arm"]: e["w_star"] for e in ct.Events(activity.root).of("campaign_arm")}
    before = len(list(bench.Results(activity.root, rsi.DOMAIN).all()))
    again = bench._evaluate_outer(as_the_campaign_saw_it, plan,
                                  campaign_pairs(activity, initial_w=w0)[0], sealed, provider=rsi.BusinessWorker())
    assert len(list(bench.Results(activity.root, rsi.DOMAIN).all())) == before
    assert {r.attempt_id for r in again["candidate"]} == {r.attempt_id for r in outer}


def test_an_outer_attempt_lost_again_on_its_refill_leaves_the_comparison_inconclusive(fake_upstream, tmp_path,
                                                                                       monkeypatch):
    """The outer plan's frozen retry policy allows one refill: an attempt lost again on it is not run a third time,
    and the pair is incomplete, so the comparison is inconclusive (docs/modules/06_RESEARCH_CAMPAIGN.md §3)."""
    from ouroloop.evolve import task_eval
    lines = []
    cfg, activity, w0_release, plan_ref = prepared(fake_upstream, tmp_path, out=lines.append)
    w0 = ct.SystemRelease.from_json(activity.objects.get(w0_release)).worker_ref
    state = {"armed": False, "lost": 0, "task": None}
    real_attempt, real_chat = task_eval.run_attempt, rsi.BusinessWorker.chat

    def run_attempt(*a, **kw):
        task = kw.get("task_id", a[1] if len(a) > 1 else None)
        state["armed"] = kw.get("arm") == "outer_candidate" and state["lost"] < 2 and (
            state["task"] in (None, task)) and kw.get("retry_index") == state["lost"]
        if state["armed"]:
            state["task"] = task
        return real_attempt(*a, **kw)

    def chat(self, system, messages, specs):
        if state["armed"]:
            state["armed"], state["lost"] = False, state["lost"] + 1
            raise ConnectionResetError("connection reset by peer")
        return real_chat(self, system, messages, specs)
    monkeypatch.setattr(task_eval, "run_attempt", run_attempt)
    monkeypatch.setattr(rsi.BusinessWorker, "chat", chat)
    slot = bench.research_slot(cfg, ".", pairs=campaign_pairs(activity, initial_w=w0),
                               researcher=rsi.ResearchPolicyProposer(),
                               arm_researcher=lambda: rsi.ScriptedResearcher(follow_prompt=True), out=lines.append,
                               worker_rounds=1)
    assert state["lost"] == 2
    outer = [r for r in bench.Results(activity.root, rsi.DOMAIN).all() if r.candidate == "candidate_w_star"]
    assert not any(r.attempt_id.endswith("#2") for r in outer)
    receipt = activity.objects.load(slot["receipt"], ct.EvaluationReceipt)
    assert slot["verdict"] == "inconclusive" and receipt.metrics["incomplete"] == {"pair-1": {"infrastructure": 1}}


def _prompt_of(objects, policy_set_ref: str, target: str) -> str:
    policies = P.validate_policy_set(objects.load(policy_set_ref, ct.PolicySet), objects).policies
    return objects.get(policies[target].implementation["artifact_ref"])["text"]


def test_it03_the_arms_cannot_read_each_other_or_the_mainline(fake_upstream, tmp_path):
    from ouroloop.research.campaign import arm_workspace
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
    for side in (control, candidate):
        assert (side / "rsi" / "active.json").exists() and (side / "experience.jsonl").exists()
    # each arm has its own pointer and its own experience; neither is the mainline's
    pointers = {json.loads((side / "rsi" / "active.json").read_text())["release_ref"] for side in (control,
                                                                                                  candidate)}
    assert activity.releases.active()["release_ref"] not in pointers
    control_experience = (control / "experience.jsonl").read_text()
    candidate_experience = (candidate / "experience.jsonl").read_text()
    assert control_experience != candidate_experience


# ---------------------------------------------------------------------------- IT-06
def test_it06_a_total_budget_that_cannot_verify_a_candidate_does_not_start_one(fake_upstream, tmp_path):
    lines = []
    cfg, activity, release, plan_ref = prepared(fake_upstream, tmp_path, out=lines.append)
    # the earmark set aside for verification no longer covers what a full comparison and the install probe cost
    budget = activity.budgets.account("tight", {"model_calls": 200, "tool_calls": 200})
    budget.allocate("verification", {"model_calls": 10})
    assert not budget.affordable({"model_calls": 20}, allocation="verification")
    assert not budget.affordable({"model_calls": 195})       # nor may training eat what is left for verifying
    from ouroloop.evolve.execution import Execution, execute_experiment
    from ouroloop.research.experiment import freeze
    experiment = freeze(activity.objects.put_json("proposal", {"owner": "worker"}), objects=activity.objects,
                        parent_release=release, experience_ref="e",
                        core_ref=ct.SystemRelease.from_json(activity.objects.get(release)).core_ref,
                        eval_template_ref=plan_ref, budget_scope="tight")
    started = []
    out = execute_experiment(experiment, {"verify_bound": {"model_calls": 20},
                                          "build": lambda *a: started.append("build"),
                                          "evaluate": lambda *a: started.append("evaluate")},
                             budget=budget, execution=Execution(activity.root, "tight-1"))
    assert out["states"] == ["admitted", "inconclusive"] and not started
    assert Execution(activity.root, "tight-1").state()["history"][-1]["reason"] == "budget_exhausted"


def test_it06_the_arms_of_a_comparison_share_one_book_and_unknown_calls_keep_their_bound(fake_upstream, tmp_path):
    from ouroloop.evolve.execution import BudgetError
    cfg, activity, release, plan_ref = prepared(fake_upstream, tmp_path)
    top = activity.budgets.account("campaign", {"model_calls": 100, "usd": 10.0})
    a = top.child("campaign:arm-a", {"model_calls": 50, "usd": 5.0})
    b = top.child("campaign:arm-b", {"model_calls": 50, "usd": 5.0})
    lost = a.reserve("call-1", {"model_calls": 1, "usd": 3.0})
    a.settle(lost.id, None, "unknown")                 # the response never came back
    assert a.remaining()["remaining"]["usd"] == 2.0 and a.remaining()["unknown"] == 1
    failed = b.reserve("train-1", {"model_calls": 1, "usd": 4.0})
    b.settle(failed.id, {"model_calls": 1, "usd": 4.0})      # a failed training job still costs
    with pytest.raises(BudgetError):
        b.reserve("train-2", {"usd": 2.0})
    assert top.remaining()["remaining"]["usd"] == 3.0        # 3 unknown + 4 spent, counted once each
    report = build_report(activity.root, rsi.DOMAIN)
    scopes = report["improvement_cost"]["scopes"]
    assert scopes["campaign:arm-b"]["settled"]["usd"] == 4.0 and scopes["campaign:arm-a"]["unknown"] == 1


# ---------------------------------------------------------------------------- RC6-11 / IT6-10: first-version arms
def v06_campaign(fake_upstream, tmp_path, *, r1_diagnose=None, r1_propose="Propose one package change.",
                 rounds=1, code=False):
    """A first-version activity and a frozen campaign whose R1 differs from R0 only where the test says."""
    from test_harness_loop import v06_prepared
    from ouroloop.research.campaign import CampaignPlan
    cfg, activity, w0_release = v06_prepared(fake_upstream, tmp_path)
    objects = activity.objects
    rel = ct.SystemRelease.from_json(objects.get(w0_release))
    if code:
        # the same activity under a core that allows an on_finish hook, which only a worker can check
        core = objects.load(rel.core_ref, ct.CoreManifest)
        harness = {**core.capabilities["harness"], "sandbox": "docker",
                   "package": {**core.capabilities["harness"]["package"],
                               "components": ["memory", "skills", "on_finish"]}}
        core2 = objects.put(ct.CoreManifest(**{**{f: getattr(core, f) for f in core.__dataclass_fields__},
                                               "capabilities": {**core.capabilities, "harness": harness}}))
        release = activity.releases.bootstrap(rel.worker_ref, rel.research_ref, core2, reason="code allowed",
                                              activate=False, target_harness_ref=rel.target_harness_ref,
                                              required_capabilities=rel.required_capabilities)
        from ouroloop.ledger import write_json
        write_json(activity.releases.active_path, {"release_ref": release, "revision": 2, "last_operation_id": None})
        cfg["rsi"]["task_plan_ref"] = bench.freeze_task_plan(objects, core_ref=core2, split_ref=activity.splits.hash(),
                                                             task_ids=tuple(rsi.DEV),
                                                             gate=GatePlan(min_gain=0.05, min_groups=1,
                                                                           bootstrap_replicates=200))
        rel = ct.SystemRelease.from_json(objects.get(release))
    r1 = P.default_research_set(objects, note_contract=True, diagnose=r1_diagnose, propose=r1_propose)
    assert r1 != rel.research_ref
    gate = GatePlan(min_groups=1, min_gain=0.01, bootstrap_replicates=200)
    campaign = CampaignPlan(id="c6", core_ref=rel.core_ref, r_control_ref=rel.research_ref, r_candidate_ref=r1,
                            campaign_pairs=(objects.put(v06_pair(activity, initial_w=rel.worker_ref)),),
                            arm_budget={"model_calls": 2000, "tool_calls": 2000}, worker_round_limit=rounds,
                            worker_gate_plan_ref=objects.put(gate), research_gate_plan=gate.to_json(),
                            discovery="scripted")
    cfg["rsi"]["research_plan_ref"] = objects.put(campaign)
    return cfg, activity, campaign


def v06_pair(activity, *, initial_w: str):
    """The inner train pool holds the family W0 fails (it stops after the update), so round one has failures
    to diagnose; the outer families are ones the inner loop never touches."""
    from ouroloop.research.campaign import CampaignPair
    return CampaignPair(pair_id="pair-1", independence_group="group-1", initial_w_ref=initial_w,
                        initial_experience_ref=activity.objects.put_json("change", {"experience": []}),
                        inner_train_groups=("rsi.update", "rsi.notify_after_update"),
                        inner_dev_groups=("rsi.dev_notify_after_update",),
                        outer_groups=("rsi.notify_once", "rsi.hold"), split_ref=activity.splits.hash(),
                        repeats=1, seed_schedule=(0,))


def researchers(make):
    """A researcher factory that keeps each researcher, in the order the campaign asks: common, control,
    candidate."""
    made = []

    def factory():
        made.append(make())
        return made[-1]
    return factory, made


def notes_of(researcher) -> list:
    return [r["task"] for r in researcher.requests if r["step"] == "note"]


def test_rc6_11_an_r1_that_changes_only_propose_shares_the_first_rounds_notes(fake_upstream, tmp_path):
    from test_harness_loop import NoteResearcher
    from ouroloop.evolve import experience as xp
    from ouroloop.research.campaign import arm_workspace
    cfg, activity, campaign = v06_campaign(fake_upstream, tmp_path)
    factory, made = researchers(lambda: NoteResearcher(broken_first_draft=False))
    receipt = bench.research(cfg, ".", out=lambda _line: None, researcher=factory)
    common, control, candidate = made
    # W0 measured once, and the first round's notes written once, at campaign scope
    event = list(ct.Events(activity.root).of("campaign_common"))[-1]
    assert event["notes_shared"] is True and event["selected"] and event["measured"] >= event["failed"] >= 1
    assert sorted(notes_of(common)) == sorted(event["selected"])
    # neither arm paid for a first-round note: each read the common ones as a shared input
    assert notes_of(control) == [] and notes_of(candidate) == []
    for arm in ("control", "candidate"):
        root = arm_workspace(activity.root, "pair-1", arm, "c6")
        diagnosis = [e for e in xp.read(root) if e.get("kind") == "diagnosis"]
        assert diagnosis[0]["reused"] == diagnosis[0]["noted"] == len(event["selected"])
        assert diagnosis[0]["included"] == event["selected"]
        shared = [json.loads(line) for line in (root / "rsi" / "notes.jsonl").read_text().splitlines()]
        assert shared and all(row.get("shared") for row in shared)
        # the arm measured nothing of its own for round one: the common measurement stood for W0
        assert not any("/measure" in (r.attempt_id or "") for r in bench.Results(root, rsi.DOMAIN).all())
    assert receipt["verdict"] in ("accepted", "rejected", "inconclusive")


def test_rc6_11_an_r1_that_changes_diagnose_writes_its_own_notes_on_the_same_selection(fake_upstream, tmp_path):
    from test_harness_loop import NoteResearcher
    from ouroloop.evolve import experience as xp
    from ouroloop.research.campaign import arm_workspace
    cfg, activity, campaign = v06_campaign(fake_upstream, tmp_path,
                                           r1_diagnose="Diagnose the failure from the checks first.")
    factory, made = researchers(lambda: NoteResearcher(broken_first_draft=False))
    bench.research(cfg, ".", out=lambda _line: None, researcher=factory)
    common, control, candidate = made
    event = list(ct.Events(activity.root).of("campaign_common"))[-1]
    assert event["notes_shared"] is False
    # nothing was written at campaign scope that only one arm could use
    assert notes_of(common) == []
    assert sorted(notes_of(control)) == sorted(notes_of(candidate)) == sorted(event["selected"])
    included = [[e for e in xp.read(arm_workspace(activity.root, "pair-1", arm, "c6"))
                 if e.get("kind") == "diagnosis"]
                [0]["included"] for arm in ("control", "candidate")]
    assert included[0] == included[1] == event["selected"]


class FlakyWorker:
    """The protocol-test worker, with a switch that takes it off line the way a stopped Docker daemon would."""
    down = False

    def __new__(cls):
        from ouroloop.agent.worker import ProcessWorker, WorkerUnavailable

        class _Flaky(ProcessWorker):
            def start(self):
                if FlakyWorker.down:
                    raise WorkerUnavailable("the worker could not start: the daemon is not running")
                return super().start()
        return _Flaky(for_tests=True)


def test_rc6_11_an_arm_whose_worker_goes_away_pauses_and_resumes_to_the_frozen_rounds(fake_upstream, tmp_path,
                                                                                       monkeypatch):
    """R1's arm loses its worker in round 2: the arm is paused, the campaign is inconclusive and measures nothing
    outside; once the worker is back, running the campaign again resumes that arm at round 2 — its open draft,
    not a new proposal — and does not run the finished control arm again."""
    from test_harness_loop import NoteResearcher
    from test_package_worker import FINISH_GUARD
    from ouroloop.agent import harness_package as hp
    from ouroloop.research.campaign import arm_workspace
    from ouroloop.research.experiment import ProposalResult
    monkeypatch.setattr(FlakyWorker, "down", False)
    monkeypatch.setattr(bench, "isolated_worker", lambda settings: FlakyWorker() if settings else None)
    cfg, activity, campaign = v06_campaign(fake_upstream, tmp_path, rounds=2, code=True)

    class Control(NoteResearcher):
        def summarize(self, *a, **kw):
            self.requests.append({"step": "propose"})
            return ProposalResult(next_action="stop", reason="nothing to change")

    class Candidate(NoteResearcher):
        """Round 1: a draft it cannot repair (a completed, invalid round). Round 2: an on_finish guard, whose
        check needs the worker — which is gone by then."""
        def summarize(self, context, prompt, notes, coverage, experience=(), *, package=None, allowed=()):
            self.requests.append({"step": "propose"})
            if sum(r["step"] == "propose" for r in self.requests) == 1:
                self.repairable = False
                return super().summarize(context, prompt, notes, coverage, experience, package=package,
                                         allowed=allowed)
            FlakyWorker.down = True
            manifest = {**hp.empty_manifest(),
                        "hooks": [{"name": "guard", "site": "on_finish", "entrypoint": "middlewares/guard.py:run"}]}
            patch = {"write": {"manifest.json": json.dumps(manifest), "middlewares/guard.py": FINISH_GUARD}}
            return ProposalResult(proposals=[ct.ExperimentProposal(
                owner="worker", hypothesis="a finish guard catches the missing message", origin="scripted",
                changes=({"target": "worker.harness", "kind": "workflow", "change": {"patch": patch}},))])

    roles = iter([NoteResearcher, Control, Candidate, NoteResearcher, Control, Candidate])
    made = []

    def factory():
        made.append(next(roles)(broken_first_draft=True))
        return made[-1]

    lines = []
    first = bench.research(cfg, ".", out=lines.append, researcher=factory)
    assert first["verdict"] == "inconclusive", lines
    receipt = activity.objects.load(first["receipt"], ct.EvaluationReceipt)
    assert any("pair-1/candidate paused (repair_runtime)" in r for r in receipt.reasons)
    assert not any(line.startswith("outer") for line in lines)          # nothing measured on the outer tasks
    candidate_root = arm_workspace(activity.root, "pair-1", "candidate", "c6")
    state = json.loads((candidate_root / "rsi" / "arm.json").read_text())
    assert [r["outcome"] for r in state["rounds"]][:2] == ["invalid", "infrastructure_failed"]
    assert state["finished"] is False

    # the repair: the worker is back, and the same frozen campaign runs again
    FlakyWorker.down = False
    lines.clear()
    second = bench.research(cfg, ".", out=lines.append, researcher=factory)
    control_again, candidate_again = made[4], made[5]
    assert control_again.requests == []                                  # the finished arm is not run again
    assert not [r for r in candidate_again.requests if r["step"] == "propose"]   # the open draft, not a new one
    state = json.loads((candidate_root / "rsi" / "arm.json").read_text())
    from ouroloop.research.loop import completed_round
    completed = [r["outcome"] for r in state["rounds"] if completed_round(r)]
    assert len(completed) == campaign.worker_round_limit and state["finished"] is True
    checks = [e for e in ct.Events(candidate_root).of("draft_check") if e.get("ok")]
    assert checks, "the resumed draft was checked by the worker that came back"
    assert any(line.startswith("outer") for line in lines)
    assert second["verdict"] != "inconclusive" or not any("paused" in r for r in
                                                         activity.objects.load(second["receipt"],
                                                                               ct.EvaluationReceipt).reasons)


def test_rc6_10_arms_write_different_code_under_one_frozen_api_and_share_none_of_it(fake_upstream, tmp_path,
                                                                                   monkeypatch):
    """Both arms run under the one K: the same package API, the same component kinds and the same worker
    settings. R0's arm writes an on_finish guard; R1's arm asks for a tool, a kind K does not allow, and for a
    newer package API — both refused at the draft check, before anything is frozen or run. Neither arm's
    package reaches the other's store."""
    from test_harness_loop import NoteResearcher
    from test_package_worker import FINISH_GUARD
    from ouroloop.agent import harness_package as hp
    from ouroloop.research.campaign import arm_workspace
    from ouroloop.research.experiment import ProposalResult
    settings_seen = []

    def worker_for(settings):
        settings_seen.append(settings)
        return FlakyWorker() if settings else None
    monkeypatch.setattr(FlakyWorker, "down", False)
    monkeypatch.setattr(bench, "isolated_worker", worker_for)
    cfg, activity, campaign = v06_campaign(fake_upstream, tmp_path, rounds=1, code=True)

    def proposal(manifest_extra, files):
        manifest = {**hp.empty_manifest(), **manifest_extra}
        patch = {"write": {"manifest.json": json.dumps(manifest), **files}}
        return ProposalResult(proposals=[ct.ExperimentProposal(
            owner="worker", hypothesis="a component that closes the missed step", origin="scripted",
            changes=({"target": "worker.harness", "kind": "workflow", "change": {"patch": patch}},))])

    class Guard(NoteResearcher):
        def summarize(self, *a, **kw):
            return proposal({"hooks": [{"name": "guard", "site": "on_finish",
                                        "entrypoint": "middlewares/guard.py:run"}]},
                            {"middlewares/guard.py": FINISH_GUARD})

    class Overreach(NoteResearcher):
        def summarize(self, *a, **kw):
            self.repairable = False
            return proposal({"api_version": "harness_package.v2",
                             "tools": [{"name": "send_anything", "entrypoint": "tools/send.py:run"}]},
                            {"tools/send.py": "def run(ctx, args):\n    return 'sent'\n"})

        def revise(self, prompt, patch, check_output):
            self.requests.append({"step": "revise", "check_output": check_output})
            return patch

    roles = iter([NoteResearcher, Guard, Overreach])
    made = []

    def factory():
        made.append(next(roles)(broken_first_draft=False))
        return made[-1]

    bench.research(cfg, ".", out=lambda _line: None, researcher=factory)
    control_root, candidate_root = (arm_workspace(activity.root, "pair-1", a, "c6") for a in ("control", "candidate"))
    # one frozen worker setting for the campaign, whichever workspace asked for it
    assert settings_seen and all(s == settings_seen[0] for s in settings_seen)
    # R1's arm: refused at the check, never frozen, never run
    refused = list(ct.Events(candidate_root).of("draft_check"))
    assert refused and not any(e["ok"] for e in refused)
    assert "api_version" in " ".join(refused[0]["errors"])
    candidate_state = json.loads((candidate_root / "rsi" / "arm.json").read_text())
    assert candidate_state["rounds"][0]["outcome"] == "invalid"
    # R0's arm: its guard was checked, gated and installed in its own workspace only
    control_state = json.loads((control_root / "rsi" / "arm.json").read_text())
    assert control_state["rounds"][0]["outcome"] == "installed"
    control_package = P.bind_release(control_state["rounds"][0]["release_after"], profile="task",
                                     services=P.Services(ct.Objects(control_root)), authorization="x").package
    assert control_package.runs_code
    assert not ct.Objects(candidate_root).has(control_package.ref)


# ---------------------------------------------------------------------------- review fixes: arms that survive
def _plan_like(activity, campaign, *, id, r1_propose):
    """The same pair and W0 as `campaign`, a new id and another R1."""
    from ouroloop.research.campaign import CampaignPlan
    r1 = P.default_research_set(activity.objects, note_contract=True, propose=r1_propose)
    plan = CampaignPlan(**{**{f: getattr(campaign, f) for f in campaign.__dataclass_fields__}, "id": id,
                           "r_candidate_ref": r1})
    return activity.objects.put(plan)


def test_a_second_campaign_over_the_same_pair_starts_from_nothing_of_the_first(fake_upstream, tmp_path):
    from test_harness_loop import NoteResearcher
    cfg, activity, campaign = v06_campaign(fake_upstream, tmp_path)
    bench.research(cfg, ".", out=lambda _line: None, researcher=lambda: NoteResearcher(broken_first_draft=False))
    second = _plan_like(activity, campaign, id="c7", r1_propose="Propose the smallest package change.")
    factory, made = researchers(lambda: NoteResearcher(broken_first_draft=False))
    bench.research({**cfg, "rsi": {**cfg["rsi"], "research_plan_ref": second}}, ".", out=lambda _line: None,
                   researcher=factory, campaign_ref=second)
    # both of c7's arms ran their own rounds: nothing came from c6's finished arms
    assert all(any(r["step"] == "propose" for r in m.requests) for m in made[1:])


def test_a_crashed_arm_resumes_to_exactly_the_frozen_rounds_and_keeps_what_it_installed(fake_upstream, tmp_path):
    from test_harness_loop import NoteResearcher
    from ouroloop.research.campaign import arm_workspace
    from ouroloop.research.loop import completed_round
    cfg, activity, campaign = v06_campaign(fake_upstream, tmp_path, rounds=2)

    class CrashesInRoundTwo(NoteResearcher):
        def summarize(self, *a, **kw):
            if sum(r["step"] == "propose" for r in self.requests) >= 1:
                raise ConnectionResetError("the research provider dropped the connection")
            return super().summarize(*a, **kw)

    roles = iter([NoteResearcher, NoteResearcher, CrashesInRoundTwo])
    with pytest.raises(ConnectionResetError):
        bench.research(cfg, ".", out=lambda _line: None,
                       researcher=lambda: next(roles)(broken_first_draft=False))
    root = arm_workspace(activity.root, "pair-1", "candidate", "c6")
    state = json.loads((root / "rsi" / "arm.json").read_text())
    assert [r["outcome"] for r in state["rounds"]] == ["installed"] and not state["finished"]
    bench.research(cfg, ".", out=lambda _line: None, researcher=lambda: NoteResearcher(broken_first_draft=False))
    state = json.loads((root / "rsi" / "arm.json").read_text())
    assert sum(1 for r in state["rounds"] if completed_round(r)) == campaign.worker_round_limit
    from ouroloop.research.campaign import CampaignPair
    w0 = activity.objects.load(campaign.campaign_pairs[0], CampaignPair).initial_w_ref
    arm = [e for e in ct.Events(activity.root).of("campaign_arm") if e["arm"] == "candidate"][-1]
    assert arm["installed"] and arm["w_star"] != w0                    # the install before the crash is its W*
    # the arm's experiments name the plan they were compared under: the arm's own, not the activity's dev plan
    pair = activity.objects.load(campaign.campaign_pairs[0], CampaignPair)
    inner = [e.task_id for e in activity.splits.entries if e.family_id in set(pair.inner_dev_groups)]
    arm_objects = ct.Objects(root)
    frozen = [ct.Experiment.from_json(json.loads(f.read_text())) for f in (arm_objects.dir / "experiment").glob("*.json")]
    assert frozen and all(x.eval_template_ref != cfg["rsi"].get("task_plan_ref") for x in frozen)
    assert all(list(arm_objects.get(x.eval_template_ref)["task_ids"]) == inner for x in frozen)


def test_a_finished_campaign_is_not_run_or_paid_for_again(fake_upstream, tmp_path):
    from test_harness_loop import NoteResearcher
    cfg, activity, campaign = v06_campaign(fake_upstream, tmp_path)
    first = bench.research(cfg, ".", out=lambda _line: None,
                           researcher=lambda: NoteResearcher(broken_first_draft=False))
    rows = sum(1 for _ in open(activity.root / "bench" / rsi.DOMAIN / "results.jsonl"))
    factory, made = researchers(lambda: NoteResearcher(broken_first_draft=False))
    again = bench.research(cfg, ".", out=lambda _line: None, researcher=factory)
    assert again["receipt"] == first["receipt"] and made == []
    assert sum(1 for _ in open(activity.root / "bench" / rsi.DOMAIN / "results.jsonl")) == rows
    receipt = activity.objects.load(first["receipt"], ct.EvaluationReceipt)
    assert receipt.costs["common"] and receipt.costs["outer"]           # the whole comparison's cost is on it


def test_rounds_lost_to_infrastructure_are_rerun_and_pause_the_run():
    from ouroloop.research.loop import completed_round, pause_of
    lost = {"outcome": "inconclusive", "reason": "4 pairs are incomplete: ['t1', 't2']"}
    judged = {"outcome": "inconclusive", "reason": "the interval contains zero"}
    assert not completed_round(lost) and completed_round(judged)
    assert pause_of([{"outcome": "installed"}, lost, {"outcome": "stopped", "reason": "rounds_done"}],
                    "rounds_done") == "infrastructure"
    assert pause_of([judged, {"outcome": "stopped"}], "rounds_done") == ""
    assert pause_of([lost], "budget_exhausted") == ""                   # a spent budget ends; it does not pause


def test_a_campaign_takes_a_factory_so_arms_never_share_a_research_step(fake_upstream, tmp_path):
    from test_harness_loop import NoteResearcher
    cfg, activity, campaign = v06_campaign(fake_upstream, tmp_path)
    with pytest.raises(ValueError, match="factory"):
        bench.research(cfg, ".", out=lambda _line: None, researcher=NoteResearcher())


def test_a_campaign_arm_resumes_its_open_experiment_under_its_frozen_inputs_or_pauses(fake_upstream, tmp_path,
                                                                                      monkeypatch):
    """docs/modules/05_EXECUTION_BUDGET.md §3 for a campaign arm: an experiment its gate left open is resumed before
    any new round; a resume that meets the refused account again pauses the arm and runs nothing behind it, and a
    resume under another template (the per-attempt limits changed) is refused, with the reason on record."""
    import json as _json
    from test_harness_loop import NoteResearcher
    from ouroloop.evolve.execution import unfinished
    from ouroloop.providers import ProviderAccountError
    from ouroloop.research.campaign import arm_workspace
    from ouroloop.research.experiment import ProposalResult
    cfg, activity, campaign = v06_campaign(fake_upstream, tmp_path, rounds=2)
    state = {"broke": False}
    real_chat = rsi.BusinessWorker.chat

    def chat(self, system, messages, specs):
        if state["broke"]:
            raise ProviderAccountError('HTTP 429: {"error":{"code":"1113","message":"balance"}}')
        return real_chat(self, system, messages, specs)
    monkeypatch.setattr(rsi.BusinessWorker, "chat", chat)

    class Control(NoteResearcher):
        def summarize(self, *a, **kw):
            self.requests.append({"step": "propose"})
            return ProposalResult(next_action="stop", reason="nothing to change")

    class BreaksTheAccount(NoteResearcher):
        def summarize(self, *a, **kw):
            out = super().summarize(*a, **kw)
            state["broke"] = True                    # the gate's attempts are the next calls
            return out
    made = []

    def factory_of(*roles):
        roles = iter(roles)

        def factory():
            made.append(next(roles)(broken_first_draft=False))
            return made[-1]
        return factory
    bench.research(cfg, ".", out=lambda _line: None, researcher=factory_of(NoteResearcher, Control, BreaksTheAccount))
    root = arm_workspace(activity.root, "pair-1", "candidate", "c6")
    [opened] = unfinished(root)

    def arm():
        return _json.loads((root / "rsi" / "arm.json").read_text())
    assert not arm()["finished"]
    # the account still refused: the open experiment is resumed and pauses the arm again; no new round behind it
    bench.research(cfg, ".", out=lambda _line: None,
                   researcher=factory_of(NoteResearcher, Control, NoteResearcher, NoteResearcher))
    candidate = made[-1]
    assert not [r for r in candidate.requests if r["step"] in ("note", "propose")]
    assert unfinished(root) == [opened] and not arm()["finished"]
    assert arm()["rounds"][-1]["outcome"] == "stopped" and arm()["rounds"][-1]["reason"] == "repair_runtime"
    # the account topped up, the per-attempt limits changed meanwhile: the arm's template is another one now
    state["broke"] = False
    cfg["bench"]["budget"] = {**cfg["bench"]["budget"], "max_steps": cfg["bench"]["budget"]["max_steps"] + 1}

    def attempts():                          # what the candidate arm has booked, on the activity's one book
        book = _json.loads((activity.root / "rsi" / "budget" / "book.json").read_text())
        return sum(1 for r in book["reservations"].values() if r["scope_id"].endswith(":candidate"))
    paid = attempts()
    assert paid > 0                          # the arm's gate did spend before the account was refused
    bench.research(cfg, ".", out=lambda _line: None,
                   researcher=factory_of(NoteResearcher, Control, NoteResearcher, NoteResearcher))
    refused = list(ct.Events(root).of("resume_refused"))
    assert refused and "eval_template_ref" in refused[-1]["reason"]
    assert unfinished(root) == [opened] and arm()["rounds"][-1]["reason"] == "repair_runtime"
    assert not [r for r in made[-1].requests if r["step"] == "propose"]
    assert attempts() == paid                                                  # nothing paid
