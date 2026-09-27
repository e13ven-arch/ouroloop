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
    # and a W2 came out of it, through the ordinary task gate
    rounds = [r for r in result["rounds"] if r["outcome"] != "stopped"]
    assert rounds and rounds[0]["outcome"] in ("installed", "rejected", "inconclusive")
    if rounds[0]["outcome"] == "installed":
        w2 = ct.SystemRelease.from_json(objects.get(result["release"])).worker_ref
        assert w2 != w0 and ct.SystemRelease.from_json(objects.get(result["release"])).research_ref == r1


def _prompt_of(objects, policy_set_ref: str, target: str) -> str:
    policies = P.validate_policy_set(objects.load(policy_set_ref, ct.PolicySet), objects).policies
    return objects.get(policies[target].implementation["artifact_ref"])["text"]


def test_it03_the_arms_cannot_read_each_other_or_the_mainline(fake_upstream, tmp_path):
    from ouroloop.research.campaign import arm_workspace
    lines = []
    cfg, activity, w0_release, plan_ref = prepared(fake_upstream, tmp_path, out=lines.append)
    w0 = ct.SystemRelease.from_json(activity.objects.get(w0_release)).worker_ref
    bench.research_slot(cfg, ".", pairs=campaign_pairs(activity, initial_w=w0),
                        researcher=rsi.ResearchPolicyProposer(),
                        arm_researcher=lambda: rsi.ScriptedResearcher(follow_prompt=True), out=lines.append,
                        worker_rounds=1)
    control = arm_workspace(activity.root, "pair-1", "control")
    candidate = arm_workspace(activity.root, "pair-1", "candidate")
    for side in (control, candidate):
        assert (side / "rsi" / "active.json").exists() and (side / "experience.jsonl").exists()
    # each arm has its own pointer and its own experience; neither is the mainline's
    pointers = {json.loads((side / "rsi" / "active.json").read_text())["release_ref"] for side in (control,
                                                                                                  candidate)}
    assert activity.releases.active()["release_ref"] not in pointers
    control_experience = (control / "experience.jsonl").read_text()
    candidate_experience = (candidate / "experience.jsonl").read_text()
    assert control_experience != candidate_experience
