"""M7 cycle and [improvement] (docs/modules/07_BENCH_REPORT.md §1–2; BR6-01, BR6-09, BR6-10, BR6-12, BR6-14,
BR6-15; IT6-21): the automatic loop as calls to the existing phases, from a config with a preset and a budget and
no hand-written refs.

The fixture preset is the Marketing preset moved to the fixture's domain with text components only, so no worker
is needed. The researchers are scripted and say which role they play; every model the loop calls is the fixture's.
"""
import json

import pytest
import rsi

from ouroloop import bench, policies as P
from ouroloop.agent import harness_package as hp
from ouroloop.evolve import contracts as ct
from ouroloop.research.experiment import ProposalResult

SKILL = "check-before-finishing"
R1_PROPOSE = P.PROPOSE_PACKAGE + " Before proposing, name the step a request asks for at its end."


@pytest.fixture
def fixture_preset(monkeypatch):
    preset = json.loads(json.dumps(bench.PRESETS["marketing_harness_v1"]))
    preset["domain"] = rsi.DOMAIN
    preset["harness"].update(components=["memory", "skills"], sandbox=None)
    preset["task_plan"]["gate"].update(min_groups=1, bootstrap_replicates=200)
    preset["schedule"].update(worker_rounds=1)
    preset["campaign"].update(worker_round_limit=1, min_groups=1)
    monkeypatch.setitem(bench.PRESETS, "rsi_harness_test", preset)
    return preset


def cycle_config(fake_upstream, tmp_path, **improvement):
    return {"workspace": {"root": str(tmp_path / "ws")},
            "bench": {"domain": rsi.DOMAIN, "upstream_path": fake_upstream, "families": dict(rsi.FAMILIES),
                      "budget": {"max_steps": 8, "max_tool_calls": 12},
                      "ratios": {"train": 0.5, "dev": 0.25, "test": 0.25}},
            "llm": {"type": "rsi:BusinessWorker"},
            "backends": {"mock": {"type": "mock"}},
            "rsi": {"scope_id": "cyc", "repeats": 1},
            "improvement": {"mode": "automatic", "preset": "rsi_harness_test", **improvement},
            "freeze": {"budget": {"model_calls": 4000, "tool_calls": 4000}}}


def gap_w0(cfg, tmp_path):
    """Bootstrap from the config (K from the preset), then start from the fixture's W0 with the known gap."""
    from ouroloop.benchmark import Results
    from ouroloop.ledger import write_json
    rsi.splits(rsi.suite(cfg["bench"]["upstream_path"])).freeze(Results(tmp_path / "ws", rsi.DOMAIN).manifest_path())
    activity = bench.open_activity(cfg, ".", out=lambda _line: None)
    objects = activity.objects
    booted = ct.SystemRelease.from_json(objects.get(activity.release_ref))
    actor = next(objects.load(r, ct.PolicyDescriptor) for r in objects.load(booted.worker_ref, ct.PolicySet).policies
                 if objects.load(r, ct.PolicyDescriptor).target == "worker.actor")
    w0 = P.default_worker_set(objects, finish_check=rsi.W0_FINISH,
                              actor_identity_ref=actor.implementation["model_identity_ref"],
                              package_ref=hp.empty_package(objects))
    release = activity.releases.bootstrap(w0, booted.research_ref, booted.core_ref, reason="W0 with the gap",
                                          activate=False)
    write_json(activity.releases.active_path, {"release_ref": release, "revision": 1, "last_operation_id": None})
    return activity, booted


class Stopper:
    """A worker researcher with nothing to propose; it records what its propose step was rendered from."""

    def __init__(self):
        self.requests = []

    def note_request(self, summary, prompt, *, allowed=()):
        return prompt, json.dumps({"failure": summary}, sort_keys=True)

    def note(self, system, user):
        self.requests.append({"step": "note", "system": system})
        return {"failure_class": "missed_step", "root_cause": "stopped early", "mechanism": "check on finish",
                "component": "skills", "generality": "multi-step requests"}

    def summarize(self, context, prompt, notes, coverage, experience=(), *, package=None, allowed=()):
        self.requests.append({"step": "propose", "system": prompt})
        return ProposalResult(next_action="stop", reason="nothing to change")

    def revise(self, prompt, patch, check_output):
        return patch


class SkillAnyway(Stopper):
    """Proposes the finish-check skill whatever the notes say."""

    def summarize(self, context, prompt, notes, coverage, experience=(), *, package=None, allowed=()):
        self.requests.append({"step": "propose", "system": prompt})
        manifest = {**hp.empty_manifest(),
                    "skills": [{"name": SKILL, "path": f"skills/{SKILL}.md", "description": "before finishing"}]}
        patch = {"write": {"manifest.json": json.dumps(manifest),
                           f"skills/{SKILL}.md": f"Before you finish, {rsi.FINISH_CHECK_MARK}."}}
        return ProposalResult(proposals=[ct.ExperimentProposal(
            owner="worker", hypothesis="a skill that checks every step", origin="scripted",
            changes=({"target": "worker.harness", "kind": "workflow", "change": {"patch": patch}},))])


class RProposer:
    """The research slot's step: proposes a new research.propose prompt."""

    def __init__(self):
        self.requests = []

    def propose_research(self, context, prompt, current, experience=(), *, allowed=()):
        self.requests.append({"step": "propose", "system": prompt, "current": sorted(current)})
        return ProposalResult(proposals=[ct.ExperimentProposal(
            owner="research", hypothesis="a propose step that names the final step closes end-of-request gaps",
            changes=({"target": "research.propose", "kind": "prompt", "change": {"text": R1_PROPOSE}},),
            origin="scripted")])


def arms(roles):
    made = []
    it = iter(roles)

    def factory():
        made.append(next(it)())
        return made[-1]
    return factory, made


def outer_on_the_gap(monkeypatch):
    """The fixture has one failing development family, so the pair is fixed by hand for this test: the arms'
    inner gate reads it and the outer comparison uses the training family with the same gap (as IT-03 does)."""
    from ouroloop.research.campaign import CampaignPair

    def pairs(activity, cfg):
        w = ct.SystemRelease.from_json(activity.objects.get(activity.releases.active()["release_ref"])).worker_ref
        return [CampaignPair(pair_id="pair-1", independence_group="group-1", initial_w_ref=w,
                             initial_experience_ref=activity.objects.put_json("change", {"experience": []}),
                             inner_train_groups=("rsi.update", "rsi.hold", "rsi.notify_once"),
                             inner_dev_groups=("rsi.dev_notify_after_update",),
                             outer_groups=("rsi.notify_after_update",), split_ref=activity.splits.hash(),
                             repeats=1, seed_schedule=(0,))]
    monkeypatch.setattr(bench, "cycle_pairs", pairs)


# ---------------------------------------------------------------------------- the whole cycle
def test_br6_09_it6_21_a_cycle_from_a_preset_runs_every_step_and_shows_r1_is_used(fake_upstream, tmp_path,
                                                                                   fixture_preset, monkeypatch):
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, booted = gap_w0(cfg, tmp_path)
    outer_on_the_gap(monkeypatch)
    worker, slot = Stopper(), RProposer()
    factory, made = arms([Stopper, Stopper, SkillAnyway])       # common inputs, control arm, candidate arm
    lines = []
    result = bench.cycle(cfg, ".", out=lines.append, researcher=worker, slot_researcher=slot,
                         arm_researcher=factory)
    steps = result["steps"]
    assert result["status"] == "done", lines[-10:]
    # BR6-09: nothing was named by hand — the plans were frozen from the preset, and K froze its methods
    assert any(line.startswith("cycle      froze") for line in lines)
    core = activity.objects.load(booted.core_ref, ct.CoreManifest)
    assert core.capabilities["methods"] == ["policy_edit", "harness_edit"]
    # BR6-12 / IT6-21: the first segment ended without a candidate, and the planned research slot still ran
    assert steps["segment-0"]["stop_reason"] == "no_proposal"
    assert steps["slot-0"]["verdict"] == "accepted" and steps["slot-0"]["installed"] is True
    r1 = steps["slot-0"]["r1"]
    active = ct.SystemRelease.from_json(activity.objects.get(activity.releases.active()["release_ref"]))
    assert active.research_ref == r1
    # the segment after the install loaded R1, and its propose step was rendered from R1's text
    assert steps["slot-0"]["reuse"]["state"] == "verified"
    assert any(r["step"] == "propose" and r["system"] == R1_PROPOSE for r in worker.requests)
    # the arms ran in their own scopes and did not open a research slot of their own
    assert len(made) == 3 and not any(e.get("event") == "campaign_arm" and e.get("pair") != "pair-1"
                                      for e in ct.Events(activity.root).all())
    # the report was written, offline, with the first version's own evidence
    assert any(line.startswith("wrote") for line in lines)
    report = json.loads((tmp_path / "ws" / "bench" / rsi.DOMAIN / "report.json").read_text())
    harness = report["harness"]
    assert harness["cycle"]["status"] == "done" and harness["reuse"][0]["state"] == "verified"
    assert harness["not_run"]["C3"] and report["claims"]["C3"]["state"] == "not_run"
    assert harness["measurements"] and harness["notes"] is not None
    assert any(r["research_ref"] == r1 for r in harness["research_steps"])
    assert all("package_ref" in p for p in harness["packages"].values()) and harness["packages"]
    markdown = (tmp_path / "ws" / "bench" / rsi.DOMAIN / "report.md").read_text()
    assert "## Capability package" in markdown and "reuse after slot-0: verified" in markdown

    # BR6-10: running the cycle again pays for nothing already done
    before = sum(1 for _ in open(tmp_path / "ws" / "bench" / rsi.DOMAIN / "results.jsonl"))
    worker2, slot2 = Stopper(), RProposer()
    factory2, made2 = arms([])
    again = bench.cycle(cfg, ".", out=lambda _line: None, researcher=worker2, slot_researcher=slot2,
                        arm_researcher=factory2)
    after = sum(1 for _ in open(tmp_path / "ws" / "bench" / rsi.DOMAIN / "results.jsonl"))
    assert again["status"] == "done" and after == before
    assert worker2.requests == [] and slot2.requests == [] and made2 == []


def test_a_segment_that_pauses_stops_the_cycle_before_any_research_slot(fake_upstream, tmp_path, fixture_preset,
                                                                        monkeypatch):
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    real_improve = bench.improve

    def paused(*a, **kw):
        out = real_improve(*a, **kw)
        return {**out, "stop_reason": "repair_runtime"}
    monkeypatch.setattr(bench, "improve", paused)
    slot = RProposer()
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=slot)
    assert result["status"] == "paused" and "slot-0" not in result["steps"] and slot.requests == []


# ---------------------------------------------------------------------------- the reader
def test_br6_01_the_improvement_reader_refuses_what_is_not_implemented(fake_upstream, tmp_path, fixture_preset):
    base = cycle_config(fake_upstream, tmp_path)
    for table, message in (({"preset": "nope"}, "preset 'nope'"),
                           ({"mode": "manual"}, "automatic loop"),
                           ({"allowed_methods": ["harness_edit", "distill_actor"]}, "second version"),
                           ({"allowed_methods": ["rewrite_everything"]}, "one of"),
                           ({"max_candidates_per_round": 3}, "one candidate per round"),
                           ({"max_collection_batches": 5}, "at most 2"),
                           ({"surprise": 1}, "does not take")):
        cfg = {**base, "improvement": {**base["improvement"], **table}}
        with pytest.raises(ValueError, match=message):
            bench.improvement_settings(cfg)
    with pytest.raises(ValueError, match="for the marketing domain"):
        bench.improvement_settings({**base, "improvement": {"preset": "marketing_harness_v1"}})


def test_br6_15_the_preset_is_a_ceiling_the_config_may_only_lower(fake_upstream, tmp_path, fixture_preset):
    base = cycle_config(fake_upstream, tmp_path)
    lowered = bench.harness_settings({**base, "harness_evolution": {"max_draft_repairs": 1, "max_failure_notes": 10,
                                                                    "components": ["skills"]}})
    assert lowered["max_draft_repairs"] == 1 and lowered["diagnosis"]["max_notes"] == 10
    assert lowered["package"]["components"] == ["skills"]
    default = bench.harness_settings(base)
    assert default["max_draft_repairs"] == 3 and default["diagnosis"]["max_notes"] == 30
    for table, message in (({"max_draft_repairs": 9}, "above the preset"),
                           ({"components": ["skills", "tools"]}, "outside the preset"),
                           ({"sandbox": "docker"}, "preset fixes")):
        with pytest.raises(ValueError, match=message):
            bench.harness_settings({**base, "harness_evolution": table})


def test_a_cycle_without_a_budget_is_refused_before_anything_runs(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    cfg.pop("freeze")
    gap_w0(cfg, tmp_path)
    with pytest.raises(ValueError, match="budget"):
        bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper())


def test_a_frozen_method_list_refuses_a_change_of_another_kind(tmp_path):
    from ouroloop.research.experiment import core_checks
    objects = ct.Objects(tmp_path)
    core = ct.CoreManifest(domain="rsi", capabilities={"methods": ["harness_edit"]})
    proposal = ct.ExperimentProposal(owner="worker", hypothesis="h",
                                     changes=({"target": "worker.system_prompt", "kind": "prompt",
                                               "change": {"text": "x"}},))
    reasons = core_checks(proposal, context=None, core=core, objects=objects, owner="worker")
    assert any("a policy_edit change, and this activity allows only ['harness_edit']" in r for r in reasons)


def test_br6_14_a_first_version_cycle_needs_no_trainer_or_teacher(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    settings = bench.improvement_settings(cfg)
    assert "trainer" not in cfg and "teacher" not in json.dumps(settings)
    core_caps = bench.harness_settings(cfg)
    assert core_caps is not None and core_caps["sandbox"] is None


def test_the_example_config_reads_and_names_no_secret():
    """examples/automationbench/improvement.toml: complete, readable by the strict readers, and without a key."""
    from pathlib import Path
    from ouroloop import config
    path = Path(__file__).resolve().parents[1] / "examples" / "automationbench" / "improvement.toml"
    cfg = config.load(path)
    settings = bench.improvement_settings(cfg)
    assert settings["preset"] == "marketing_harness_v1" and settings["allowed_methods"] == ["policy_edit",
                                                                                           "harness_edit"]
    harness = bench.harness_settings(cfg)
    assert harness["sandbox"] == "docker" and harness["max_draft_repairs"] == 3
    assert harness["diagnosis"]["max_notes"] == 30
    assert cfg["llm"]["model"] == "glm-5.3" and "trainer" not in cfg and "teacher" not in cfg
    text = path.read_text()
    assert "api_key_env" in text and not any(k in cfg["llm"] for k in ("api_key", "token"))
    assert bench.RsiConfig.load(cfg).scope_id == "marketing-improvement"


# ---------------------------------------------------------------------------- review fixes
def test_the_slot_judges_r1_over_the_presets_groups_and_gates_arms_on_their_own(fake_upstream, tmp_path,
                                                                                fixture_preset, monkeypatch):
    from ouroloop.evolve.task_gate import GatePlan
    from ouroloop.research.campaign import CampaignPlan
    fixture_preset["task_plan"]["gate"]["min_groups"] = 2
    fixture_preset["campaign"]["min_groups"] = 1
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    outer_on_the_gap(monkeypatch)
    factory, _ = arms([Stopper, Stopper, SkillAnyway])
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=RProposer(),
                         arm_researcher=factory)
    activity = bench.open_activity(cfg, ".", out=lambda _line: None)
    plan = activity.objects.load(result["steps"]["slot-0"]["campaign"], CampaignPlan)
    assert plan.gate().min_groups == 1                                    # the research verdict: the preset's
    assert GatePlan.from_json(activity.objects.get(plan.worker_gate_plan_ref)).min_groups == 1   # 1 inner family
    assert result["steps"]["slot-0"]["verdict"] == "accepted"


def test_a_paused_research_slot_stops_the_cycle_before_the_next_segment(fake_upstream, tmp_path, fixture_preset,
                                                                         monkeypatch):
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    outer_on_the_gap(monkeypatch)

    def paused_research(cfg, base=".", **kw):
        receipt = ct.EvaluationReceipt(profile="research", verdict="inconclusive",
                                       reasons=("pair-1/candidate paused (repair_runtime)",))
        ref = activity.objects.put(receipt)
        return {"receipt": ref, "verdict": "inconclusive", "installation": None, "release": ""}
    monkeypatch.setattr(bench, "research", paused_research)
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=RProposer())
    assert result["status"] == "paused" and result["ok"] is False
    assert result["steps"]["slot-0"]["outcome"] == "paused" and "segment-1" not in result["steps"]


def test_a_restarted_baseline_pays_only_for_what_is_missing(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    bench.baseline(cfg, ".", out=lambda _line: None)
    path = tmp_path / "ws" / "bench" / rsi.DOMAIN / "results.jsonl"
    rows = sum(1 for _ in open(path))
    bench.baseline(cfg, ".", out=lambda _line: None)
    assert sum(1 for _ in open(path)) == rows


def test_an_r_only_install_reuses_the_measurement(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    work = bench.improvement(activity)
    w0 = activity.releases.active()["release_ref"]
    rel = ct.SystemRelease.from_json(activity.objects.get(w0))
    r1 = P.default_research_set(activity.objects, note_contract=True, propose=R1_PROPOSE)
    other = activity.releases.bootstrap(rel.worker_ref, r1, rel.core_ref, reason="only R", activate=False)
    measured = work.measure(w0, None)
    again = work.measure(other, measured)
    assert again["reused"] and again["release"] == other and again["measured_release"] == w0


def test_frozen_plans_may_not_loosen_the_preset(fake_upstream, tmp_path, fixture_preset):
    for extra, message in (({"schedule": {"collection_batches": 9}}, "above max_collection_batches"),
                           ({"schedule": {"candidates_per_round": 4}}, "candidates_per_round"),
                           ({"task_plan": {"tasks": "dev", "gate": {"min_gain": 0.05, "min_groups": 1,
                                                                    "max_regression_rate": 1.0}}},
                            "max_regression_rate 1.0 is above")):
        cfg = cycle_config(fake_upstream, tmp_path / message[:8].replace(" ", "_"))
        cfg["freeze"].update(extra)
        gap_w0(cfg, tmp_path / message[:8].replace(" ", "_"))
        with pytest.raises(ValueError, match=message):
            bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper())


def test_the_preset_fixes_the_worker_image_and_needs_a_method(fake_upstream, tmp_path):
    cfg = {"bench": {"domain": "marketing"}, "improvement": {"preset": "marketing_harness_v1"},
           "harness_evolution": {"image": "someone-elses-image:latest"}}
    with pytest.raises(ValueError, match="the preset fixes"):
        bench.harness_settings(cfg)
    with pytest.raises(ValueError, match="allowed_methods is empty"):
        bench.improvement_settings({**cfg, "improvement": {**cfg["improvement"], "allowed_methods": []}})


def test_a_malformed_research_slot_reply_proposes_nothing_instead_of_crashing():
    from ouroloop.research.basic import PolicyResearcher

    class Replies:
        name = "fake"

        def complete(self, system, prompt):
            return json.dumps({"proposals": ["make it better", {"target": "research.propose", "text": ""}]})
    context = type("C", (), {"goal": "g", "budget": {}})()
    result = PolicyResearcher(Replies()).propose_research(context, "p", {"research.propose": "old"})
    assert result.proposals == [] and result.next_action == "stop"


def test_reuse_is_verified_only_by_a_request_from_r1s_changed_prompt(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, booted = gap_w0(cfg, tmp_path)
    r0 = booted.research_ref
    r1 = P.default_research_set(activity.objects, note_contract=True, propose=R1_PROPOSE)
    events = ct.Events(activity.root)
    events.append("research_steps", scope="cyc", research_ref=r1, steps=[], context_ref="",
                  prompts={"research.propose": ct.digest({"text": R1_PROPOSE})[:16]})
    assert bench._reuse_fact(activity, r1, r0, "segment-1")["state"] == "not_completed"   # loaded, sent nothing
    events.append("research_steps", scope="cyc", research_ref=r1, steps=[{"step": "propose", "prompt_hash": "x"}],
                  context_ref="", prompts={"research.propose": ct.digest({"text": "R0's text"})[:16]})
    assert bench._reuse_fact(activity, r1, r0, "segment-1")["state"] == "not_completed"   # R0's text was bound
    events.append("research_steps", scope="cyc", research_ref=r1, steps=[{"step": "propose", "prompt_hash": "x"}],
                  context_ref="", prompts={"research.propose": ct.digest({"text": R1_PROPOSE})[:16]})
    assert bench._reuse_fact(activity, r1, r0, "segment-1")["state"] == "verified"
