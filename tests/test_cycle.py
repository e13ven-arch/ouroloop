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
                      # all five hard limits: a new freeze is refused without them (M0 §2, v0.3)
                      "budget": {"max_steps": 8, "max_tool_calls": 12, "max_model_calls": 20, "max_tokens": 4000},
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
                                          target_harness_ref=booted.target_harness_ref,
                                          required_capabilities=booted.required_capabilities,
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
    # the outer comparison's share (8% of the frozen root) was frozen into the plan, held, then handed over
    from ouroloop.evolve.execution import Budgets
    from ouroloop.research.campaign import CampaignPlan
    campaign = activity.objects.load(steps["slot-0"]["campaign"], CampaignPlan)
    assert campaign.outer_budget == {"model_calls": 320.0, "tool_calls": 320.0}
    holds = [r for r in Budgets(tmp_path / "ws")._read()["reservations"].values()
             if r["operation_id"].endswith(":outer:hold")]
    assert [h["status"] for h in holds] == ["released"]
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


def frozen(cfg):
    """The config with its [freeze.*] tables frozen and named under [rsi], as `ouroloop freeze` leaves it."""
    refs = bench.freeze(cfg, ".", out=lambda _line: None)
    return {**cfg, "rsi": {**cfg["rsi"], **refs}}


def test_a_restarted_baseline_pays_only_for_what_is_missing(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    cfg = frozen(cfg)
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
    assert result.proposals == [] and result.unreadable and "malformed" in result.reason
    assert result.next_action == "none"                     # not R's "stop": the reply decided nothing


class _SlotModel:
    """The research model behind the cycle's own slot researcher, metered on the activity's book: `reply` each
    time it is asked, a refusal for None, a reply cut at the output limit for "cut"."""
    name, model, reply, asked, last = "slot-model", "r-1", "", 0, None

    def __init__(self, **_):
        pass

    def chat(self, system, messages, specs=()):
        from ouroloop.chat import ChatTurn
        _SlotModel.asked += 1
        _SlotModel.last = json.loads(messages[0]["content"])
        usage = {"input_tokens": 10, "output_tokens": 5}
        if _SlotModel.reply is None:
            return ChatTurn("", [], "refusal", usage=usage)
        if _SlotModel.reply == "cut":           # as the manual provider cuts one: no usage reported
            return ChatTurn('{"next_action": "none", "proposals": [{"hyp', [], "max_tokens")
        return ChatTurn(_SlotModel.reply, [], "end", usage=usage)


def slot_cycle(cfg, monkeypatch, reply=None, **kw):
    """A cycle whose research slot asks the cycle's own researcher, on the fake model: the production path."""
    if "slot_researcher" not in kw:
        monkeypatch.setattr(_SlotModel, "reply", reply)
        monkeypatch.setattr(_SlotModel, "asked", 0)
        cfg["research"] = {"llm": {"type": "test_cycle:_SlotModel"}}
    return bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), **kw)


R1_ITEM = {"hypothesis": "h", "target": "research.propose", "text": R1_PROPOSE}


class _NoteModel:
    """The research model behind the cycle's own worker researcher: a note for a note request, a stop for the
    summary, keeping what each summary request carried."""
    name, model, summaries = "note-model", "r-2", []

    def __init__(self, **_):
        pass

    def chat(self, system, messages, specs=()):
        from ouroloop.chat import ChatTurn
        sent = json.loads(messages[0]["content"])
        if "failure" in sent:
            reply = {"failure_class": "missed_step", "root_cause": "stopped early", "mechanism": "check on finish",
                     "component": "skills", "generality": "multi-step requests"}
        else:
            _NoteModel.summaries.append(sent)
            reply = {"next_action": "stop", "reason": "nothing to change"}
        return ChatTurn(json.dumps(reply), [], "end", usage={"input_tokens": 10, "output_tokens": 5})


def test_the_summary_request_lists_the_hosts_base_tools(fake_upstream, tmp_path, fixture_preset, monkeypatch):
    """docs/modules/04_RESEARCH_LOOP.md §1: the summary sees no failure transcript, so it is shown the base tools a
    package tool or hook can call, with their parameters — the names and arguments code components need."""
    monkeypatch.setattr(_NoteModel, "summaries", [])
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    cfg["research"] = {"llm": {"type": "test_cycle:_NoteModel"}}
    bench.cycle(cfg, ".", out=lambda _line: None, until="segment-0/round-0")
    tools = _NoteModel.summaries[0]["tools"]
    assert [t["name"] for t in tools] == ["api_search", "api_fetch", "base64_encode"]
    fetch = next(t for t in tools if t["name"] == "api_fetch")
    assert {"method", "url", "body"} <= set(fetch["parameters"]["properties"]) and fetch["description"]


@pytest.mark.parametrize("reply,why", [
    ("", "was empty"),
    (None, "declined the request"),
    ("I would have the propose prompt name the last step.", "not readable JSON"),
    (json.dumps({"reason": "the rounds went well"}), "no proposal and no next_action"),
    (json.dumps({"next_action": "collect", "reason": "too few rounds"}), "is not one of"),
    (json.dumps({"next_action": "none", "proposals": [{"hypothesis": "h", "target": "research.propose"}]}),
     "no new prompt text"),
    (json.dumps({"proposals": [{"hypothesis": "h", "target": "worker.harness", "text": "Add a skill."}]}),
     "is not one of"),
    (json.dumps({"proposals": [{"hypothesis": "h", "target": "research.propose", "text": P.PROPOSE_PACKAGE}]}),
     "is the current research.propose prompt"),
    (json.dumps({"proposals": ["name the last step"]}), "not an object"),
    (json.dumps({"proposals": "name the last step", "next_action": "none"}), "not a list"),
    (json.dumps({"proposals": R1_ITEM, "next_action": "none"}), "not a list"),
    # a malformed proposal after a well-formed one is not dropped with the first read on
    (json.dumps({"proposals": [R1_ITEM, "and a second idea"]}), "not an object")],
    ids=["empty", "refused", "not_json", "no_next_action", "unknown_next_action", "no_text", "worker_target",
         "unchanged_text", "not_an_object", "not_a_list", "an_object_not_a_list", "malformed_after_a_good_one"])
def test_an_unreadable_slot_reply_is_an_invalid_slot(fake_upstream, tmp_path, fixture_preset, monkeypatch, reply,
                                                     why):
    """docs/modules/04_RESEARCH_LOOP.md §1: an empty, refused, non-JSON or malformed research-slot reply decides
    nothing. The slot is invalid, not no_proposal; it is not asked again, the next worker segment runs, and the
    report's C4 says the slot's reply was invalid rather than that R proposed nothing."""
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    result = slot_cycle(cfg, monkeypatch, reply)
    slot = result["steps"]["slot-0"]
    assert result["status"] == "done" and slot["outcome"] == "invalid" and slot["stage"] == "reply", slot
    assert why in slot["reason"] and slot["reason"].count(why) == 1 and "campaign" not in slot
    assert "segment-1" in result["steps"] and _SlotModel.asked == 1
    c4 = result["report"]["C4"]
    assert c4["state"] == "not_run" and "reply was invalid" in c4["detail"] and why in c4["detail"]
    assert "proposed no change" not in c4["detail"]
    again = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper())
    assert again["steps"]["slot-0"] == slot and _SlotModel.asked == 1    # an invalid slot is not asked again


@pytest.mark.parametrize("decision", [{"next_action": "stop", "reason": "the rounds went well"},
                                      {"next_action": "none", "proposals": [], "reason": "nothing to change"}])
def test_a_slot_decision_r_states_is_still_no_proposal(fake_upstream, tmp_path, fixture_preset, monkeypatch,
                                                       decision):
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    result = slot_cycle(cfg, monkeypatch, json.dumps(decision))
    slot = result["steps"]["slot-0"]
    assert slot["outcome"] == "no_proposal" and slot["stage"] == "reply" and slot["reason"] == decision["reason"]
    assert "proposed no change" in result["report"]["C4"]["detail"]
    assert _SlotModel.last["budget"]["remaining"]                  # the slot's R is shown what is left (M4 §5)


class _RefusedBySlotAdmission(RProposer):
    """A slot step whose proposal changes W, which a research slot may not: admission refuses it."""

    def propose_research(self, context, prompt, current, experience=(), *, allowed=()):
        return ProposalResult(proposals=[ct.ExperimentProposal(
            owner="research", hypothesis="h", origin="scripted",
            changes=({"target": "worker.system_prompt", "kind": "prompt", "change": {"text": "x"}},))])


@pytest.mark.parametrize("stage", ["admission", "build"])
def test_a_slot_that_admission_or_the_build_refuses_says_which(fake_upstream, tmp_path, fixture_preset,
                                                               monkeypatch, stage):
    """The slot's record says where it ended, so C4 can say so; a proposed prompt is taken whole, and one longer
    than K allows fails at the build rather than being cut into a text R did not write."""
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    long_text = R1_PROPOSE + " Name it." * 3000
    if stage == "admission":
        result = slot_cycle(cfg, monkeypatch, slot_researcher=_RefusedBySlotAdmission())
    else:
        result = slot_cycle(cfg, monkeypatch, json.dumps({"proposals": [{**R1_ITEM, "text": long_text}]}))
    slot = result["steps"]["slot-0"]
    assert result["status"] == "done" and slot["stage"] == stage and "campaign" not in slot, slot
    detail = result["report"]["C4"]["detail"]
    assert ("admission refused" if stage == "admission" else "did not build") in detail
    if stage == "build":
        assert str(len(long_text)) in slot["reason"]


def test_a_slot_reply_cut_at_the_output_limit_pauses_and_is_asked_again(fake_upstream, tmp_path, fixture_preset,
                                                                        monkeypatch):
    """docs/modules/04_RESEARCH_LOOP.md §1: a cut reply is a configuration problem, not R's answer: the slot pauses,
    is not recorded invalid, and the same step is asked again once the cycle runs again, each call booked once."""
    from ouroloop.evolve.execution import Budgets
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    result = slot_cycle(cfg, monkeypatch, "cut")
    assert result["status"] == "paused" and result["steps"]["slot-0"]["outcome"] == "paused"
    assert "output limit" in result["steps"]["slot-0"]["reason"] and "segment-1" not in result["steps"]
    assert "None" not in result["steps"]["slot-0"]["reason"]        # no count is given when none was reported
    monkeypatch.setattr(_SlotModel, "reply", json.dumps({"next_action": "stop", "reason": "the rounds went well"}))
    again = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper())
    assert _SlotModel.asked == 2 and again["steps"]["slot-0"]["outcome"] == "no_proposal"
    book = Budgets(tmp_path / "ws")._read()
    calls = [r for r in book["reservations"].values()
             if r["scope_id"].endswith(":slot-0") and ":research" in r["operation_id"]]
    assert len(calls) == 2 and {r["status"] for r in calls} == {"settled"}      # the cut call, then the asked again


def test_the_slot_keeps_rs_propose_text_and_its_format_governs():
    """The slot's system prompt is R's own bound propose text, then the slot format, which says what that text is,
    that this call changes it, and that the format governs where they differ (docs/modules/04_RESEARCH_LOOP.md §5)."""
    from ouroloop.research.basic import RESEARCH_FORMAT, PolicyResearcher
    from ouroloop.research.experiment import propose_experiment
    sent = []

    class Replies:
        name = "fake"

        def complete(self, system, prompt):
            sent.append((system, json.loads(prompt)))
            return json.dumps({"next_action": "stop", "reason": "nothing to change"})
    prompts = {"research.diagnose": P.DIAGNOSE_NOTE, "research.propose": R1_PROPOSE}
    bound = P.BoundRuntime(release_ref="", profile="task", authorization="x", services=P.Services(None))
    bound.research["research.diagnose"] = type("D", (), {"output_schema": "failure_note.v1"})()
    bound.prompts.update(prompts)
    context = type("C", (), {"goal": "g", "budget": {}})()
    propose_experiment(context, bound, "research", researcher=PolicyResearcher(Replies()), objects=None)
    (system, user), = sent
    assert system == f"{R1_PROPOSE}\n\n{RESEARCH_FORMAT}" and user["current_prompts"] == prompts
    for declared in ("The text above is research.propose", "proposes a change to those instructions themselves",
                     "this format governs"):
        assert declared in RESEARCH_FORMAT


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


@pytest.mark.parametrize("usd,share", [(5.0, "a research-slot arm"), (12.0, "a research slot's outer comparison")])
def test_a_cycle_whose_research_arms_could_not_make_one_call_is_refused_up_front(fake_upstream, tmp_path,
                                                                                  fixture_preset, usd, share):
    """One call reserves about $1.47 here: $5 leaves an arm's 15% short of it, $12 leaves the outer's 8% short."""
    cfg = cycle_config(fake_upstream, tmp_path)
    cfg["freeze"]["budget"] = {"usd": usd}
    cfg["pricing"] = {"input_per_1k": 0.0014, "output_per_1k": 0.0044, "max_input_tokens": 1_000_000,
                      "max_output_tokens": 16384, "source": "test"}
    gap_w0(cfg, tmp_path)
    with pytest.raises(ValueError, match="research slot cannot run: one call reserves up to") as refused:
        bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper())
    assert share in str(refused.value)


# ---------------------------------------------------------------------------- before the first paid activity
# docs/ROADMAP.md §3, the first paid activity's protocol, step 1 ("before the baseline").
def rows_of(tmp_path):
    from ouroloop.benchmark import Results
    return list(Results(tmp_path / "ws", rsi.DOMAIN).all())


def test_the_baseline_train_is_the_first_measurement(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    w0 = activity.releases.active()["release_ref"]
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), until="segment-0")
    assert result["status"] == "stopped" and result["steps"]["segment-0"]["stop_reason"] == "no_proposal"
    train = [r for r in rows_of(tmp_path) if r.split == "train" and r.system_release_ref == w0]
    # W0's train split was measured once: by the baseline, under the measurement's identity; round 0 read it
    assert sorted(r.task_id for r in train) == sorted(rsi.TRAIN)
    assert {bench._arm_of(r.attempt_id) for r in train} == {"measure"}
    plans = [e for e in ct.Events(activity.root).of("measurement_planned") if e["release"] == w0]
    assert plans and plans[0]["first"] and sorted(plans[0]["tasks"]) == sorted(rsi.TRAIN)


def test_baseline_attempt_ids_are_stable(fake_upstream, tmp_path, fixture_preset):
    """A baseline run on its own after `ouroloop freeze`, and the cycle's baseline after it, are one run, under a
    task plan whose conditions differ from the defaults (so a baseline outside the frozen plan would not match)."""
    fixture_preset["task_plan"]["per_attempt_budget"] = {"max_steps": 6}
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    named = frozen(cfg)
    assert {"task_plan_ref", "schedule_ref", "budget_ref"} <= set(named["rsi"])   # the preset's plans too
    by_cycle = bench._cycle_config(cfg, ".", bench.improvement_settings(cfg), lambda _line: None)["rsi"]
    assert {k: named["rsi"][k] for k in ("task_plan_ref", "schedule_ref", "budget_ref")} == \
        {k: by_cycle[k] for k in ("task_plan_ref", "schedule_ref", "budget_ref")}
    bench.baseline(named, ".", out=lambda _line: None)
    before = [(r.attempt_id, r.run) for r in rows_of(tmp_path)]
    assert all(bench._arm_of(a) == "measure" for a, _ in before if "/rsi.dev" not in a)
    planned = [e for e in ct.Events(tmp_path / "ws").of("measurement_planned")]
    assert {e["run"] for e in planned} == {run for a, run in before if "/rsi.dev" not in a}
    assert not (tmp_path / "ws" / "rsi" / "cycle.json").exists()
    lines = []
    result = bench.cycle(cfg, ".", out=lines.append, researcher=Stopper(), until="baseline")
    assert result["status"] == "stopped" and "segment-0" not in result["steps"]
    assert not any("baseline is on record" in line for line in lines)      # the cycle ran its baseline
    assert [(r.attempt_id, r.run) for r in rows_of(tmp_path)] == before      # and paid for none of it


def test_an_interrupted_measurement_is_finished_not_reused(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    named = frozen(cfg)
    work = bench.improvement(activity, template_ref=named["rsi"]["task_plan_ref"])
    w0 = activity.releases.active()["release_ref"]
    # the whole split was booked and a pause stopped it after two tasks
    work.train_measurement(w0, rsi.TRAIN[:2], first=True, planned=rsi.TRAIN)
    last = work.last_measurement(w0)
    assert last["missing"] == rsi.TRAIN[2:]
    measured = work.measure(w0, last)
    assert not measured["reused"] and sorted(r.task_id for r in measured["train"]) == sorted(rsi.TRAIN)
    measures = [r for r in rows_of(tmp_path) if bench._arm_of(r.attempt_id) == "measure"]
    assert len(measures) == len(rsi.TRAIN)            # the two recorded attempts were not paid for again
    assert work.last_measurement(w0)["missing"] == []


def test_an_interrupted_collection_batch_is_finished_at_its_own_index(fake_upstream, tmp_path, fixture_preset,
                                                                       monkeypatch):
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    named = frozen(cfg)
    work = bench.improvement(activity, template_ref=named["rsi"]["task_plan_ref"])
    w0 = activity.releases.active()["release_ref"]
    measured = work.measure(w0, None)

    def crash(*a, **kw):
        raise RuntimeError("the process died after the booking")
    with monkeypatch.context() as m:
        m.setattr(work, "_run_collection", crash)
        with pytest.raises(RuntimeError):
            work.collect_batch(measured, batch=1, root=activity.root)
    # booked, so it counts toward the quota although it never ran
    assert bench._collection_batches(activity.root, work.scope_id, None) == 1
    assert len(work.finish_collections(activity.root, measured)) == 1
    assert work.finish_collections(activity.root, measured) == []           # finished once
    assert bench._collection_batches(activity.root, work.scope_id, None) == 1
    collected = [r for r in rows_of(tmp_path) if bench._arm_of(r.attempt_id) == "collect"]
    assert collected and {r.attempt for r in collected} == {1}              # at the index it was booked at
    # a restart rebuilds the measurement with the batch's attempts in it
    assert {r.attempt_id for r in collected} <= {r.attempt_id for r in work.last_measurement(w0)["train"]}


def test_cycle_stops_at_the_named_step(fake_upstream, tmp_path, fixture_preset):
    fixture_preset["schedule"].update(worker_rounds=2, patience=2)
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    for bad in ("segment-3", "slot-1", "segment-0/round-2", "nope"):
        with pytest.raises(ValueError, match="--until"):
            bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), until=bad)
    lines = []
    first = bench.cycle(cfg, ".", out=lines.append, researcher=SkillAnyway(), until="segment-0/round-0")
    assert first["status"] == "stopped" and first["ok"] is True
    assert first["steps"]["segment-0"]["stop_reason"] == "stopped_at" and "slot-0" not in first["steps"]
    assert any("stopped at segment-0/round-0" in line for line in lines)
    segment = json.loads((tmp_path / "ws" / "rsi" / "segments" / "segment-0.json").read_text())
    assert not segment.get("finished")                      # the stop point does not finish the segment
    # running again continues with round 1 only, and stops before the research slot
    worker = Stopper()
    second = bench.run_phase(cfg, ".", phase="cycle", out=lambda _line: None, researcher=worker,
                             until="segment-0")
    assert second["status"] == "stopped" and "slot-0" not in second["steps"]
    assert sum(1 for r in worker.requests if r["step"] == "propose") == 1
    assert json.loads((tmp_path / "ws" / "rsi" / "segments" / "segment-0.json").read_text())["finished"]


def test_a_resumed_segment_keeps_its_patience_and_failure_counts():
    from ouroloop.research.loop import counters_of
    assert counters_of([{"outcome": "rejected"}, {"outcome": "stopped", "reason": "rounds_done"}]) == (1, 0)
    assert counters_of([{"outcome": "installed"}, {"outcome": "inconclusive"}]) == (1, 1)
    # a paused round, or one lost to infrastructure, is run again after the repair and counts toward neither limit
    assert counters_of([{"outcome": "rejected"},
                        {"outcome": "infrastructure_failed", "next_action": "repair_runtime"}]) == (1, 0)
    assert counters_of([{"outcome": "inconclusive", "reason": INFRA}] * 2
                       + [{"outcome": "stopped", "reason": "infrastructure_failures"}]) == (0, 0)


def test_more_research_slots_than_the_preset_are_refused(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    cfg["freeze"]["schedule"] = {"research_slots": 2}
    gap_w0(cfg, tmp_path)
    with pytest.raises(ValueError, match="research_slots 2 is above the preset's 1"):
        bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper())


def test_a_paid_phase_refuses_an_unfrozen_budget(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    with pytest.raises(ValueError, match="names no budget_ref"):
        bench.baseline(cfg, ".", out=lambda _line: None)
    book = tmp_path / "ws" / "rsi" / "budget" / "book.json"
    assert not book.exists() or "cyc" not in json.loads(book.read_text())["scopes"]   # no empty root was opened
    bench.baseline(frozen(cfg), ".", out=lambda _line: None)


def test_startup_checks_the_pinned_models(fake_upstream, tmp_path, fixture_preset, monkeypatch):
    monkeypatch.delenv("OUROLOOP_TEST_UNSET_KEY", raising=False)
    cfg = cycle_config(fake_upstream, tmp_path)
    cfg["llm"] = {"type": "openai_compat", "model": "m", "api_key_env": "OUROLOOP_TEST_UNSET_KEY",
                  "max_tokens": 20000}
    cfg["pricing"] = {"input_per_1k": 0.001, "output_per_1k": 0.002, "max_input_tokens": 100000,
                      "max_output_tokens": 16384, "source": "test"}
    activity = bench.open_activity(cfg, ".", out=lambda _line: None)     # W0 pins this actor
    # [llm] changed after the bootstrap: what is checked is what the release pins, and the research model
    later = {**cfg, "llm": {"type": "rsi:BusinessWorker"},
             "research": {"llm": {"type": "manual", "dir": str(tmp_path / "m"), "max_tokens": 32768}}}
    problems = bench.startup_checks(activity, later, phase="baseline")
    assert any(p.startswith("the actor the active release pins max_tokens 20000 is above") for p in problems)
    assert any("the actor the active release pins reads its key from OUROLOOP_TEST_UNSET_KEY" in p
               for p in problems)
    assert any(p.startswith("[research.llm] max_tokens 32768 is above") for p in problems)
    assert not any("[research.llm] reads its key" in p for p in problems)    # a manual model has no key
    monkeypatch.setenv("OUROLOOP_TEST_UNSET_KEY", "set")
    assert not any("reads its key" in p for p in bench.startup_checks(activity, later, phase="baseline"))


def test_startup_checks_that_a_claude_code_model_is_logged_in(fake_upstream, tmp_path, fixture_preset):
    """A `claude_code` model has no key to look for: the check asks its CLI whether it is logged in (M1 §1)."""
    import stat
    import sys
    from pathlib import Path
    cli = tmp_path / "claude"
    cli.write_text(f"#!{sys.executable}\nimport sys\nprint(open(sys.argv[0] + '.auth').read())\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    Path(f"{cli}.auth").write_text('{"loggedIn": false}')
    cfg = cycle_config(fake_upstream, tmp_path)
    activity = bench.open_activity(cfg, ".", out=lambda _line: None)
    research = {"type": "claude_code", "model": "claude-opus-5-5", "binary": str(cli), "effort": "medium",
                "workdir": str(tmp_path / "empty"), "max_tokens": 8000}
    later = {**cfg, "research": {"llm": research},
             "pricing": {"input_per_1k": 0.004, "output_per_1k": 0.02, "max_input_tokens": 1000000,
                         "max_output_tokens": 32768, "source": "test"}}
    problems = bench.startup_checks(activity, later, phase="baseline")
    assert any(p.startswith("[research.llm]: the claude CLI") and "not logged in" in p for p in problems)
    # one CLI call can make 5 requests: 5 x 8000 output tokens is more than one reservation bounds
    assert any(p.startswith("[research.llm] max_tokens 8000 times the 5 requests") for p in problems)
    Path(f"{cli}.auth").write_text('{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty"}')
    research["max_tokens"] = 6000
    assert not any("claude" in p for p in bench.startup_checks(activity, later, phase="baseline"))


def test_cycle_cannot_spend_the_final_allocation(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    cfg["freeze"]["budget"] = {"model_calls": 100, "final": {"model_calls": 30}}
    gap_w0(cfg, tmp_path)
    named = frozen(cfg)
    activity = bench.open_activity(named, ".", out=lambda _line: None)
    assert bench.budget_allocations(activity.objects.get(named["rsi"]["budget_ref"])) == \
        {"final": {"model_calls": 30.0}}
    improving = bench.activity_budget(activity, named, "improve")
    final = bench.activity_budget(activity, named, "final:x")
    # a fresh root could hold 31 more calls: only the final's share refuses them
    assert final.affordable({"model_calls": 30}, allocation="final")
    assert not final.affordable({"model_calls": 31}, allocation="final")
    assert improving.affordable({"model_calls": 70}) and not improving.affordable({"model_calls": 71})
    # what the cycle is shown as left is what it may reserve, without the final's share
    assert improving.remaining()["effective_remaining"]["model_calls"] == 70
    assert final.remaining(allocation="final")["effective_remaining"]["model_calls"] == 30
    improving.reserve("everything the cycle may spend", {"model_calls": 70})
    assert improving.remaining()["effective_remaining"]["model_calls"] == 0
    assert final.affordable({"model_calls": 30}, allocation="final")


def test_a_share_that_cannot_work_is_refused_before_a_root_is_opened(fake_upstream, tmp_path, fixture_preset):
    for share, message in (({"model_calls": 150}, "nothing is left"), ({"usd": 5}, "sets nothing aside"),
                           ({"model_calls": 0}, "above zero")):
        cfg = cycle_config(fake_upstream, tmp_path / message[:6].replace(" ", "_"))
        cfg["freeze"]["budget"] = {"model_calls": 100, "final": share}
        gap_w0(cfg, tmp_path / message[:6].replace(" ", "_"))
        with pytest.raises(ValueError, match=message):
            frozen(cfg)


def test_an_open_roots_allocations_do_not_change(fake_upstream, tmp_path, fixture_preset):
    from ouroloop.evolve.execution import BudgetError
    cfg = cycle_config(fake_upstream, tmp_path)
    cfg["freeze"]["budget"] = {"model_calls": 100, "final": {"model_calls": 30}}
    gap_w0(cfg, tmp_path)
    named = frozen(cfg)
    activity = bench.open_activity(named, ".", out=lambda _line: None)
    bench.activity_budget(activity, named, "improve").reserve("a call", {"model_calls": 1})
    without = {**named, "rsi": {**named["rsi"],
                                "budget_ref": activity.objects.put_json("budget", {"model_calls": 100.0})}}
    with pytest.raises(BudgetError, match="do not change"):               # withdrawn from an open root
        bench.activity_budget(activity, without, "final:x")
    plain = cycle_config(fake_upstream, tmp_path / "late")
    gap_w0(plain, tmp_path / "late")
    plain_named = frozen(plain)
    late_activity = bench.open_activity(plain_named, ".", out=lambda _line: None)
    bench.activity_budget(late_activity, plain_named, "improve").reserve("a call", {"model_calls": 1})
    late = {**plain_named, "rsi": {**plain_named["rsi"], "budget_ref": late_activity.objects.put_json(
        "budget", {"model_calls": 4000.0, "tool_calls": 4000.0, "allocations": {"final": {"model_calls": 30.0}}})}}
    with pytest.raises(BudgetError, match="already spent"):                # added after spending
        bench.activity_budget(late_activity, late, "final:x")


# ---------------------------------------------------------------------------- review fixes (stage 1)
INFRA = "3 pairs are incomplete: ['t#0']"


def scripted_rounds(seq):
    it = iter(seq)

    def run(ctx, budget):
        outcome, reason = next(it)
        return ct.RoundResult(outcome=outcome, reason=reason)
    return run


# A round lost to infrastructure is run again in its slot (M4 §2, 2026-10-05), so a segment pauses on infrastructure
# when the consecutive losses reach max_infrastructure_failures — after a completed round or from the start.
@pytest.mark.parametrize("schedule,seq", [
    ({"worker_rounds": 3, "patience": 3, "max_infrastructure_failures": 2}, [("inconclusive", INFRA)] * 2),
    ({"worker_rounds": 3, "patience": 2, "max_infrastructure_failures": 2},
     [("rejected", "no gain"), ("inconclusive", INFRA), ("inconclusive", INFRA)])])
def test_a_segment_paused_on_infrastructure_resumes_after_the_repair(fake_upstream, tmp_path, fixture_preset,
                                                                    monkeypatch, schedule, seq):
    from ouroloop.research import loop as L
    fixture_preset["schedule"].update(schedule)
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    with monkeypatch.context() as m:
        m.setattr(L, "run_worker_round", scripted_rounds(seq))
        first = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper())
    assert first["status"] == "paused"
    worker = Stopper()                               # the runtime is repaired
    again = bench.cycle(cfg, ".", out=lambda _line: None, researcher=worker)
    assert any(r["step"] == "propose" for r in worker.requests)          # a round ran
    assert again["steps"]["segment-0"]["stop_reason"] == "no_proposal"


def test_a_round_lost_to_infrastructure_is_rerun_in_its_slot_and_the_failure_limit_still_stops(monkeypatch):
    """M4 §2 / M6 RC6-17 (2026-10-05): in a live run, a round `infrastructure_round` recognises — a screen whose
    attempts were all lost, a gate's integrity loss — is run again in the same slot: it takes none of
    schedule.worker_rounds and adds nothing to idle, so two rounds of schedule still give two completed rounds after
    a loss; consecutive losses count toward max_infrastructure_failures, which ends the run as before."""
    from ouroloop.evolve.execution import Schedule
    from ouroloop.research import loop as L
    from ouroloop.research.loop import completed_round
    slots = []
    monkeypatch.setattr(L, "run_worker_round", scripted_rounds([
        ("inconclusive", "infrastructure: no candidate had a valid screen reading, the screen attempts were lost"),
        ("installed", ""), ("rejected", "no gain")]))
    results = L.run_worker_rounds(lambda index: slots.append(index), None,
                                  schedule=Schedule(worker_rounds=2, patience=1, max_infrastructure_failures=2))
    assert [r.outcome for r in results] == ["inconclusive", "installed", "rejected", "stopped"]
    assert slots == [0, 0, 1] and results[-1].reason == "rounds_done"
    rows = [{"outcome": r.outcome, "reason": r.reason} for r in results]
    assert sum(1 for r in rows if completed_round(r)) == 2      # what an arm's RoundLog.done() counts
    # patience 1 did not fire on the loss: it added nothing to idle; the rejected round after the install did
    # consecutive losses stop at the limit, whatever the schedule's rounds
    monkeypatch.setattr(L, "run_worker_round", scripted_rounds([("inconclusive", INFRA)] * 9))
    lost = L.run_worker_rounds(lambda index: None, None,
                               schedule=Schedule(worker_rounds=5, patience=5, max_infrastructure_failures=3))
    assert [r.outcome for r in lost] == ["inconclusive"] * 3 + ["stopped"]
    assert lost[-1].reason == "infrastructure_failures"


def test_the_reuse_check_is_read_from_the_segment_after_the_slot(fake_upstream, tmp_path, fixture_preset,
                                                                 monkeypatch):
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    outer_on_the_gap(monkeypatch)
    factory, _ = arms([Stopper, Stopper, SkillAnyway])
    first = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=RProposer(),
                        arm_researcher=factory, until="slot-0")
    assert first["status"] == "stopped" and first["steps"]["slot-0"]["installed"]
    assert first["steps"]["slot-0"].get("reuse") is None                     # pending, not written off
    worker = Stopper()
    second = bench.cycle(cfg, ".", out=lambda _line: None, researcher=worker, slot_researcher=RProposer(),
                         arm_researcher=arms([])[0])
    reuse = second["steps"]["slot-0"]["reuse"]
    assert reuse["state"] == "verified" and reuse["segment"] == "segment-1"
    assert any(r["step"] == "propose" and r["system"] == R1_PROPOSE for r in worker.requests)


def test_a_paused_segment_after_an_install_keeps_the_reuse_check_pending(fake_upstream, tmp_path, fixture_preset,
                                                                         monkeypatch):
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    outer_on_the_gap(monkeypatch)
    factory, _ = arms([Stopper, Stopper, SkillAnyway])
    real_improve = bench.improve

    def paused_after_the_slot(*a, segment=None, **kw):
        out = real_improve(*a, segment=segment, **kw)
        return {**out, "stop_reason": "repair_runtime"} if segment == "segment-1" else out
    with monkeypatch.context() as m:
        m.setattr(bench, "improve", paused_after_the_slot)
        first = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=RProposer(),
                            arm_researcher=factory)
    assert first["status"] == "paused" and first["steps"]["slot-0"].get("reuse") is None
    state = json.loads((tmp_path / "ws" / "rsi" / "cycle.json").read_text())
    assert state["reuse_pending"]["segment"] == "segment-1"
    again = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=RProposer(),
                        arm_researcher=arms([])[0])
    assert again["steps"]["slot-0"]["reuse"]["state"] == "verified"


class Collector(Stopper):
    """Asks for more evidence, once."""

    def summarize(self, context, prompt, notes, coverage, experience=(), *, package=None, allowed=()):
        self.requests.append({"step": "propose", "system": prompt})
        return ProposalResult(next_action="collect", reason="the failures need another attempt each")


def test_a_collection_a_stop_point_left_unanswered_runs_before_the_next_round(fake_upstream, tmp_path,
                                                                              fixture_preset):
    fixture_preset["schedule"].update(worker_rounds=2, patience=2)
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    first = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Collector(), until="segment-0/round-0")
    assert first["status"] == "stopped"
    assert not [r for r in rows_of(tmp_path) if bench._arm_of(r.attempt_id) == "collect"]   # not paid at the stop
    worker = Stopper()
    bench.cycle(cfg, ".", out=lambda _line: None, researcher=worker, until="segment-0")
    assert [r for r in rows_of(tmp_path) if bench._arm_of(r.attempt_id) == "collect"]
    rounds = json.loads((tmp_path / "ws" / "rsi" / "segments" / "segment-0.json").read_text())["rounds"]
    assert [r["outcome"] for r in rounds].count("collected") == 1
    assert sum(1 for r in worker.requests if r["step"] == "propose") == 1        # round 1 read the batch


def test_a_measurement_booked_under_another_run_is_not_finished_under_this_one(fake_upstream, tmp_path,
                                                                               fixture_preset):
    from dataclasses import replace
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    named = frozen(cfg)
    work = bench.improvement(activity, template_ref=named["rsi"]["task_plan_ref"])
    w0 = activity.releases.active()["release_ref"]
    work.train_measurement(w0, rsi.TRAIN[:2], first=True, planned=rsi.TRAIN)
    work.attempt_budget = replace(work.attempt_budget, max_steps=work.attempt_budget.max_steps + 1)  # new code
    with pytest.raises(ValueError, match="booked under run"):
        work.measure(w0, work.last_measurement(w0))
    assert len([r for r in rows_of(tmp_path) if bench._arm_of(r.attempt_id) == "measure"]) == 2


def test_an_activity_does_not_freeze_its_plans_again(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), until="baseline")
    fixture_preset["task_plan"]["gate"]["min_gain"] = 0.3          # the preset changed between two runs
    with pytest.raises(ValueError, match="plans are not frozen again"):
        bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), until="segment-0")


def test_until_slot_stops_before_the_next_segment(fake_upstream, tmp_path, fixture_preset, monkeypatch):
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    outer_on_the_gap(monkeypatch)

    def rejected(cfg, base=".", **kw):
        ref = activity.objects.put(ct.EvaluationReceipt(profile="research", verdict="rejected", reasons=()))
        return {"receipt": ref, "verdict": "rejected", "installation": None, "release": ""}
    monkeypatch.setattr(bench, "research", rejected)
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=RProposer(),
                         until="slot-0")
    assert result["status"] == "stopped" and result["steps"]["slot-0"]["verdict"] == "rejected"
    assert "segment-1" not in result["steps"]


def test_which_paid_phases_need_which_frozen_plans(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    plain = {k: v for k, v in cfg.items() if k != "improvement"}         # not an automatic activity
    problems = bench.startup_checks(activity, plain, phase="baseline")
    assert any("names no budget_ref" in p for p in problems)            # its own [freeze.budget]
    assert not any("names no task_plan_ref" in p for p in problems)
    bare = {k: v for k, v in cfg.items() if k != "freeze"}               # an automatic activity: all three
    problems = bench.startup_checks(activity, bare, phase="baseline")
    assert all(any(f"names no {t}_ref" in p for p in problems) for t in ("task_plan", "schedule", "budget"))


def test_a_manual_model_is_not_checked_for_a_key(fake_upstream, tmp_path, fixture_preset, monkeypatch):
    monkeypatch.delenv("OUROLOOP_TEST_UNSET_KEY", raising=False)
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    later = {**cfg, "research": {"llm": {"type": "manual", "dir": str(tmp_path / "m"),
                                         "api_key_env": "OUROLOOP_TEST_UNSET_KEY"}}}
    assert not any("reads its key" in p for p in bench.startup_checks(activity, later, phase="baseline"))


def test_a_limited_baseline_is_finished_by_the_first_round(fake_upstream, tmp_path, fixture_preset):
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    named = frozen(cfg)
    w0 = activity.releases.active()["release_ref"]
    bench.baseline(named, ".", out=lambda _line: None, limit=1)
    assert len([r for r in rows_of(tmp_path) if r.split == "train" and r.system_release_ref == w0]) == 1
    bench.improve(named, ".", out=lambda _line: None, researcher=Stopper())
    train = [r.task_id for r in rows_of(tmp_path) if r.split == "train" and r.system_release_ref == w0]
    assert sorted(train) == sorted(rsi.TRAIN)                      # the rest once, the first not again


def test_improve_finishes_an_interrupted_batch(fake_upstream, tmp_path, fixture_preset, monkeypatch):
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    named = frozen(cfg)
    work = bench.improvement(activity, template_ref=named["rsi"]["task_plan_ref"])
    w0 = activity.releases.active()["release_ref"]
    measured = work.measure(w0, None)

    def crash(*a, **kw):
        raise RuntimeError("died after the booking")
    with monkeypatch.context() as m:
        m.setattr(work, "_run_collection", crash)
        with pytest.raises(RuntimeError):
            work.collect_batch(measured, batch=1, root=activity.root)
    bench.improve(named, ".", out=lambda _line: None, researcher=Stopper())
    collected = [r for r in rows_of(tmp_path) if bench._arm_of(r.attempt_id) == "collect"]
    assert collected and {r.attempt for r in collected} == {1}


class Useless(Stopper):
    """Proposes a skill that changes nothing the fixture checks."""

    def summarize(self, context, prompt, notes, coverage, experience=(), *, package=None, allowed=()):
        self.requests.append({"step": "propose", "system": prompt})
        manifest = {**hp.empty_manifest(),
                    "skills": [{"name": "be-polite", "path": "skills/be-polite.md", "description": "tone"}]}
        patch = {"write": {"manifest.json": json.dumps(manifest), "skills/be-polite.md": "Be polite."}}
        return ProposalResult(proposals=[ct.ExperimentProposal(
            owner="worker", hypothesis="politeness", origin="scripted",
            changes=({"target": "worker.harness", "kind": "workflow", "change": {"patch": patch}},))])


def test_a_stop_point_does_not_hide_the_schedules_own_end(fake_upstream, tmp_path, fixture_preset):
    fixture_preset["schedule"].update(worker_rounds=3, patience=1)
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    first = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Useless(), until="segment-0/round-0")
    # patience fires after round 0, at the stop point: the segment ended by its own rule, and says so
    assert first["status"] == "stopped" and first["steps"]["segment-0"]["stop_reason"] == "patience"
    worker = Useless()
    second = bench.cycle(cfg, ".", out=lambda _line: None, researcher=worker, until="segment-0")
    assert [r for r in worker.requests if r["step"] == "propose"] == []
    assert second["steps"]["segment-0"]["stop_reason"] == "patience"


def test_an_account_refusal_while_finishing_a_batch_pauses(fake_upstream, tmp_path, fixture_preset, monkeypatch):
    from ouroloop.providers import ProviderAccountError
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    named = frozen(cfg)
    work = bench.improvement(activity, template_ref=named["rsi"]["task_plan_ref"])
    measured = work.measure(activity.releases.active()["release_ref"], None)

    def crash(*a, **kw):
        raise RuntimeError("died after the booking")
    with monkeypatch.context() as m:
        m.setattr(work, "_run_collection", crash)
        with pytest.raises(RuntimeError):
            work.collect_batch(measured, batch=1, root=activity.root)

    def refused(self, *a, **kw):
        raise ProviderAccountError("insufficient balance")
    monkeypatch.setattr(bench.Improvement, "_run_collection", refused)
    result = bench.improve(named, ".", out=lambda _line: None, researcher=Stopper())
    assert result["stop_reason"] == "repair_runtime"          # paused, not an exception out of the phase


def test_a_frozen_plan_keeps_its_attempt_limits_when_the_config_changes(fake_upstream, tmp_path, fixture_preset):
    """Every per-attempt limit is frozen with the task plan, so a change to [bench.budget] while an experiment is
    open cannot finish its comparison under other limits (docs/modules/05_EXECUTION_BUDGET.md §3)."""
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    named = frozen(cfg)
    activity = bench.open_activity(named, ".", out=lambda _line: None)
    from ouroloop.evolve.task_gate import TaskEvalPlan
    plan = TaskEvalPlan.from_json(activity.objects.get(named["rsi"]["task_plan_ref"]))
    assert plan.per_attempt_budget["max_steps"] == 8 and plan.per_attempt_budget["max_tool_calls"] == 12
    assert "max_seconds" in plan.per_attempt_budget
    changed = {**named, "bench": {**named["bench"], "budget": {"max_steps": 3, "max_tool_calls": 4}}}
    later = bench.open_activity(changed, ".", out=lambda _line: None)
    work = bench.improvement(later, template_ref=named["rsi"]["task_plan_ref"])
    assert (work.attempt_budget.max_steps, work.attempt_budget.max_tool_calls) == (8, 12)
    assert bench._attempt_budget(later).max_steps == 8          # what a campaign's arms and outer runs read


def test_a_finished_segment_is_not_recorded_as_paused_by_a_later_failure(fake_upstream, tmp_path, fixture_preset,
                                                                         monkeypatch):
    """A finished segment is read back before the budget root or a model is touched, so a budget problem that
    belongs to a later step is not recorded against it, and it keeps the end its segment log says."""
    from ouroloop.evolve.execution import BudgetMismatch
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    first = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), until="segment-0")
    assert first["steps"]["segment-0"]["stop_reason"] == "no_proposal"
    real = bench.activity_budget

    def mismatched(activity, cfg, phase):
        if phase == "improve":
            raise BudgetMismatch("scope 'cyc' already exists with other limits")
        return real(activity, cfg, phase)
    monkeypatch.setattr(bench, "activity_budget", mismatched)
    again = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), until="segment-0")
    record = again["steps"]["segment-0"]
    assert again["status"] == "stopped" and record["stop_reason"] == "no_proposal" and "outcome" not in record


def test_a_slot_after_a_worker_install_runs(fake_upstream, tmp_path, fixture_preset):
    """Once segment-0 has installed a package, what the slot copies into the campaign workspaces reaches that
    install's receipt and its frozen plan, which embeds its gate plan whole and never stored it. The common inputs,
    both arms and the receipt still run, and so does the next segment (docs/ROADMAP.md §3, before the slot)."""
    from ouroloop.evolve.task_gate import GatePlan
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    w0 = activity.releases.active()["release_ref"]
    factory, made = arms([Stopper, Stopper, SkillAnyway])       # common inputs, control arm, candidate arm
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=SkillAnyway(), slot_researcher=RProposer(),
                         arm_researcher=factory)
    steps = result["steps"]
    assert result["status"] == "done", steps
    # segment-0 installed W1, authorised by a task receipt whose plan embeds its gate plan, own ref included
    assert steps["segment-0"]["release"] != w0
    installed = ct.SystemRelease.from_json(activity.objects.get(steps["segment-0"]["release"]))
    authorization = activity.objects.load(installed.authorization_ref, ct.Authorization)
    plan = activity.objects.get(activity.objects.load(authorization.ref, ct.EvaluationReceipt).plan_ref)
    # the plan embeds the gate plan whole (the slot's inner gate, which carries the same clauses since v0.3, is the
    # one gate plan object the workspace stores: the mainline receipt's plan never needed one)
    assert authorization.kind == "accepted"
    assert plan["gate_plan"]["ref"] == GatePlan.from_json(plan["gate_plan"]).ref()
    # the slot ran from W1 to a receipt
    slot = steps["slot-0"]
    assert "outcome" not in slot and len(made) == 3
    receipt = activity.objects.load(slot["receipt"], ct.EvaluationReceipt)
    assert receipt.profile == "research" and receipt.verdict == slot["verdict"] and slot["installed"] is False
    # W1 already closes the fixture's one gap, so both arms seal it: no outer run, and nothing about R compared
    assert slot["verdict"] == "inconclusive" and receipt.metrics["identical_arms"] == ["pair-1"]
    assert receipt.metrics["control_outer_pass_rate"] is None
    ran = list(ct.Events(activity.root).of("campaign_arm"))
    assert [e["arm"] for e in ran] == ["control", "candidate"]
    assert all(e["w_star"] == installed.worker_ref for e in ran)
    # the pair's E0 holds the install's experiment, and both arms' logs begin with it
    from ouroloop.evolve import experience as ex
    from ouroloop.research.campaign import CampaignPair, CampaignPlan, arm_workspace
    campaign = activity.objects.load(slot["campaign"], CampaignPlan)
    pair = activity.objects.load(campaign.campaign_pairs[0], CampaignPair)
    e0 = activity.objects.get(pair.initial_experience_ref)["experience"]
    assert any(e["kind"] == "experiment" and e["installed"] for e in e0)
    for side in ("control", "candidate"):
        assert ex.read(arm_workspace(activity.root, pair.pair_id, side, campaign.id))[:len(e0)] == e0
    assert "outcome" not in steps["segment-1"] and steps["segment-1"]["stop_reason"] == "rounds_done"


class Offline:
    """The research model's completion interface, answered offline: a note for a note request, a stop for the
    summary. It keeps every request, so a test reads what the model would have read."""

    def __init__(self):
        self.sent = []

    def complete(self, system, text):
        self.sent.append((system, text))
        if "failure" in json.loads(text):
            return json.dumps({"failure_class": "missed_step", "root_cause": "stopped early",
                               "mechanism": "check on finish", "component": "skills",
                               "generality": "multi-step requests"})
        return json.dumps({"next_action": "stop", "reason": "nothing to change"})


def test_rc6_01_both_arms_start_from_the_same_e0(fake_upstream, tmp_path, fixture_preset):
    """docs/modules/06_RESEARCH_CAMPAIGN.md §2: both arms' experience logs begin with the pair's E0 as frozen, and
    their R reads it under one snapshot ref. E0 is the mainline log without each experiment's raw gate reasons,
    which can name development tasks; what R is shown of a mainline experiment — its outcome and aggregate
    figures — stays."""
    from ouroloop.evolve import experience as ex
    from ouroloop.research.basic import PolicyResearcher
    from ouroloop.research.campaign import CampaignPair, CampaignPlan, arm_workspace
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    models = []

    def arm():
        models.append(Offline())               # the common inputs', then the control arm's, then the candidate's
        return PolicyResearcher(models[-1])
    # segment-0 runs one rejected experiment, gated on the whole development plan; the slot pairs by the preset
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Useless(), slot_researcher=RProposer(),
                         arm_researcher=arm, until="slot-0")
    plan = activity.objects.load(result["steps"]["slot-0"]["campaign"], CampaignPlan)
    pair = activity.objects.load(plan.campaign_pairs[0], CampaignPair)
    e0 = activity.objects.get(pair.initial_experience_ref)["experience"]
    gated = next(e for e in ex.read(activity.root) if e.get("kind") == "experiment")
    assert gated["metrics"] and gated["verdict"] == "rejected" and gated["reasons"]
    assert [e["kind"] for e in e0] == ["diagnosis", "experiment"]
    assert e0[1] == {k: v for k, v in gated.items() if k != "reasons"}
    sides = {side: arm_workspace(activity.root, pair.pair_id, side, plan.id) for side in ("control", "candidate")}
    refs = set()
    for (side, root), model in zip(sides.items(), models[1:]):
        assert ex.read(root)[:len(e0)] == e0
        first = next(ct.Events(root).of("research_steps"))
        refs.add(ct.Objects(root).get(first["context_ref"])["experience_ref"])
        system, text = next((s, t) for s, t in model.sent if "notes" in json.loads(t))
        assert system.startswith(R1_PROPOSE) == (side == "candidate")
        row = next(r for r in json.loads(text)["experience"] if r["kind"] == "experiment")
        assert row["outcome"] == "rejected" and row["installed"] is False and "metrics" in row, side
    assert refs == {f"experience:{ct.digest(e0)[:16]}"}
    # nothing any arm's R was sent names an outer task
    outer = {e.task_id for e in activity.splits.entries if e.family_id in pair.outer_groups}
    assert outer and not [t for m in models for _, text in m.sent for t in outer if t in text]
    # a restarted arm finds E0 already there; a log begun from another E0 is refused
    log = sides["control"] / "experience.jsonl"
    before = log.read_text()
    bench._seed_experience(sides["control"], e0)
    assert log.read_text() == before
    with pytest.raises(ValueError, match="another E0"):
        bench._seed_experience(sides["control"], e0[1:])


def _leave(tmp_path, model_calls):
    """Spend the activity's root elsewhere, down to `model_calls` left."""
    from ouroloop.evolve.execution import Budgets
    budgets = Budgets(tmp_path / "ws")
    left = budgets.headroom("cyc", budgets._read())["model_calls"]
    budgets.settle(budgets.reserve("cyc", "spent elsewhere", {"model_calls": left - model_calls}).id,
                   {"model_calls": left - model_calls})


# The fixture's root is 4000 model calls: each arm's share is 600, the outer comparison's 320, so a slot needs
# 1520, and one more for R's own call when the cycle's metered researcher asks it.
@pytest.mark.parametrize("left,metered,opens", [(1519, False, False), (1520, True, False), (1520, False, True),
                                                (1521, True, True)])
def test_a_slot_whose_outer_share_the_root_cannot_hold_asks_r_nothing(fake_upstream, tmp_path, fixture_preset,
                                                                      monkeypatch, left, metered, opens):
    """docs/modules/05_EXECUTION_BUDGET.md §3: a slot opens only when the root can cover its outer comparison's share
    and both arms' shares, and R's call when it is the cycle's metered researcher that asks. When it cannot, R is
    not asked, no campaign is frozen, and the activity ends as budget_exhausted."""
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), until="segment-0")
    _leave(tmp_path, left)
    slot = RProposer()
    if metered:
        result = slot_cycle(cfg, monkeypatch, json.dumps({"proposals": [R1_ITEM]}), until="slot-0")
        asked = _SlotModel.asked
    else:
        result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=slot,
                             until="slot-0")
        asked = len(slot.requests)
    step = result["steps"]["slot-0"]
    if opens:
        # R was asked (on the metered model, the campaign's own research calls follow it)
        assert asked >= 1 and step.get("outcome") != "budget_exhausted" and step.get("campaign"), step
        return
    assert result["status"] == "budget_exhausted" and step["outcome"] == "budget_exhausted", step
    assert "outer comparison's share" in step["reason"] and "campaign" not in step and asked == 0
    assert "before a campaign ran" in result["report"]["C4"]["detail"]


def test_common_inputs_that_leave_the_arms_short_end_the_slot_inconclusive(fake_upstream, tmp_path, fixture_preset,
                                                                           monkeypatch):
    """The slot opened with exactly enough for both arms and the outer comparison; the common measurement then
    spends some of it, so the root can no longer cover both arms. Neither arm runs, the outer share goes back, and
    the comparison is inconclusive for the budget rather than judged with R1's arm starved. (2026-10-05: the open
    check now counts the common inputs' estimate and the common inputs reuse the mainline's readings, so this is
    reached only when the estimate falls short of what they spend — here nothing is reusable and the estimate says 0.)"""
    from ouroloop.evolve.execution import Budgets
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), until="segment-0")
    _leave(tmp_path, 1520)
    monkeypatch.setattr(bench, "_mainline_readings", lambda *a, **k: ([], []))
    monkeypatch.setattr(bench, "common_inputs_estimate", lambda *a, **k: {})
    factory, made = arms([Stopper, Stopper, SkillAnyway])
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=RProposer(),
                         arm_researcher=factory, until="slot-0")
    slot = result["steps"]["slot-0"]
    assert slot["verdict"] == "inconclusive" and len(made) == 1, slot              # the common inputs only
    activity = bench.open_activity(cfg, ".", out=lambda _line: None)
    receipt = activity.objects.load(slot["receipt"], ct.EvaluationReceipt)
    assert "cannot set aside both arms' shares" in receipt.reasons[0] and "outer" not in receipt.costs
    assert Budgets(tmp_path / "ws").open_reservations() == []
    assert not list(ct.Events(activity.root).of("campaign_arm"))


def test_bench_concurrency_is_a_whole_number_of_attempts(fake_upstream, tmp_path, fixture_preset):
    """[bench] concurrency is a setting of the machine (M5 §2): it is accepted, it reaches the arm runner, and a
    value that is not a count is refused before anything is paid for."""
    cfg = cycle_config(fake_upstream, tmp_path)
    cfg["bench"]["concurrency"] = 4
    activity = bench.open_activity(cfg, ".", out=lambda _line: None)
    assert activity.bench.concurrency == 4
    assert bench.improvement(activity).concurrency == 4
    assert not any("concurrency" in p for p in bench.startup_checks(activity, cfg, phase="baseline"))
    # every Improvement the bench builds carries it: the activity's, a campaign arm's, the common inputs'
    import ast
    import inspect
    calls = [node for node in ast.walk(ast.parse(inspect.getsource(bench)))
             if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Improvement"]
    assert len(calls) >= 3 and all(any(k.arg == "concurrency" for k in c.keywords) for c in calls)
    cfg["bench"]["concurrency"] = 0
    bad = bench.open_activity(cfg, ".", out=lambda _line: None)
    assert any(p.startswith("[bench] concurrency must be a whole number") for p in
               bench.startup_checks(bad, cfg, phase="baseline"))


# ---------------------------------------------------------------------------- v0.3: the five hard limits, max_effort
def test_the_frozen_plan_and_the_host_contract_carry_the_five_hard_limits(fake_upstream, tmp_path, fixture_preset):
    """docs/modules/00_CONTRACTS.md §2 (单次尝试硬上限): the task plan freezes every dimension the config sets, the
    TargetHarness lists all five, an arm's plan carries the same; limit_problems asks for all five by default and
    for the three a plan or K from before v0.3 had when told so."""
    from ouroloop.benchmark import Budget
    from ouroloop.evolve.task_gate import TaskEvalPlan
    cfg = cycle_config(fake_upstream, tmp_path)
    cfg["bench"]["budget"].update(max_model_calls=20, max_tokens=4000)
    gap_w0(cfg, tmp_path)
    named = frozen(cfg)
    activity = bench.open_activity(named, ".", out=lambda _line: None)
    plan = TaskEvalPlan.from_json(activity.objects.get(named["rsi"]["task_plan_ref"]))
    assert {k: plan.per_attempt_budget[k] for k in bench.HARD_LIMITS} == {
        "max_steps": 8, "max_model_calls": 20, "max_tool_calls": 12, "max_seconds": 900.0, "max_tokens": 4000}
    work = bench.improvement(activity, template_ref=named["rsi"]["task_plan_ref"])
    assert (work.attempt_budget.max_model_calls, work.attempt_budget.max_tokens) == (20, 4000)
    assert set(work.plan("a", "b", "c").per_attempt_budget) == set(bench.HARD_LIMITS)
    release = ct.SystemRelease.from_json(activity.objects.get(activity.release_ref))
    limits = activity.objects.get(activity.objects.load(release.target_harness_ref, ct.TargetHarness).limits_ref)
    assert limits == {"max_steps": 8, "max_model_calls": 20, "max_tool_calls": 12, "max_seconds": 900.0,
                      "max_tokens": 4000}
    assert bench.startup_checks(activity, named, phase="baseline", needs_model=False) == []
    short = Budget(max_steps=8, max_tool_calls=12)
    assert bench.limit_problems(short) == ["[bench.budget] needs max_model_calls, max_tokens per attempt (every new "
                                           "plan freezes all five hard limits)"]
    assert bench.limit_problems(short, five=False) == []
    assert bench.limit_problems(Budget(max_steps=8, max_tool_calls=12, max_model_calls=1, max_tokens=1)) == []
    assert bench.limit_problems(Budget(max_steps=0, max_tool_calls=12), five=False) == ["[bench.budget] needs "
                                                                                       "max_steps per attempt"]


def test_a_new_freeze_needs_all_five_limits_and_an_old_plan_is_read_with_its_three(fake_upstream, tmp_path,
                                                                                   fixture_preset):
    """docs/modules/00_CONTRACTS.md §2 (v0.3) "新冻结的计划五项齐全，缺一项拒绝冻结", CT7-06: a config without the two v0.3
    dimensions is refused a freeze whatever components K allows — the rule is K's, so the comparison identity does
    not change when agents are allowed later — and a v0.3 K without a plan asks for them at startup; a plan frozen
    before the two existed is read as it was frozen, the host runs it without them, and startup says nothing."""
    from ouroloop.evolve.task_gate import GatePlan
    cfg = cycle_config(fake_upstream, tmp_path)
    for key in bench.V03_BUDGET_FIELDS:
        del cfg["bench"]["budget"][key]
    activity, booted = gap_w0(cfg, tmp_path)
    assert "agent" not in bench._harness_of(activity)["package"]["components"]
    with pytest.raises(ValueError, match="freeze refused: .*needs max_model_calls, max_tokens per attempt"):
        bench.freeze(cfg, ".", out=lambda _line: None)
    missing = "[bench.budget] needs max_model_calls, max_tokens per attempt (every new plan freezes all five hard limits)"
    assert [p for p in bench.startup_checks(activity, cfg, phase="baseline", needs_model=False)
            if p.startswith("[bench.budget]")] == [missing]
    # the plan as `ouroloop freeze` wrote it before v0.3: the three limits, and nothing more
    old_ref = bench.freeze_task_plan(activity.objects, core_ref=booted.core_ref, split_ref=activity.splits.hash(),
                                     task_ids=tuple(rsi.DEV),
                                     gate=GatePlan(min_gain=0.05, min_groups=1, bootstrap_replicates=200),
                                     per_attempt_budget={"max_steps": 8, "max_tool_calls": 12, "max_seconds": 900.0})
    named = {**cfg, "rsi": {**cfg["rsi"], "task_plan_ref": old_ref}}
    later = bench.open_activity(named, ".", out=lambda _line: None)
    assert bench._attempt_budget(later).max_tokens is None and bench._attempt_budget(later).max_steps == 8
    assert [p for p in bench.startup_checks(later, named, phase="baseline", needs_model=False)
            if p.startswith("[bench.budget]")] == []


def test_max_effort_is_frozen_into_k_with_a_default_of_high_and_narrows_only(fake_upstream, tmp_path,
                                                                             fixture_preset):
    """docs/modules/00_CONTRACTS.md §2 (v0.3): K's `max_effort` is the ceiling the gateway caps a package's requested
    effort to; [harness_evolution] may lower it and never raise it, and names package API v2 to allow agents."""
    base = cycle_config(fake_upstream, tmp_path)
    assert bench.harness_settings(base)["max_effort"] == "high"
    assert bench.harness_settings({**base, "harness_evolution": {"max_effort": "medium"}})["max_effort"] == "medium"
    for table, message in (({"max_effort": "max"}, "above the preset"), ({"max_effort": "turbo"}, "one of"),
                           ({"package_api": "harness_package.v2"}, "preset fixes")):
        with pytest.raises(ValueError, match=message):
            bench.harness_settings({**base, "harness_evolution": table})
    activity, _booted = gap_w0(base, tmp_path)
    frozen_k = bench._harness_of(activity)
    assert frozen_k["max_effort"] == "high" and frozen_k["package_api"] == "harness_package.v1"
    assert hp.Limits.from_json(frozen_k["package"]).package_api == "harness_package.v1"
    # the table alone, without a preset: v1 and high unless named, v2 by name, unknown versions refused
    plain = bench.harness_settings({"harness_evolution": {"components": ["memory"]}})
    assert plain["max_effort"] == "high" and plain["package_api"] == "harness_package.v1"
    v2 = bench.harness_settings({"harness_evolution": {"components": ["memory", "agent"],
                                                       "package_api": "harness_package.v2", "max_effort": "low"}})
    assert v2["package"]["package_api"] == "harness_package.v2" and v2["max_effort"] == "low"
    with pytest.raises(ValueError, match="this host serves"):
        bench.harness_settings({"harness_evolution": {"package_api": "harness_package.v9"}})
    with pytest.raises(ValueError, match="one of"):
        bench.harness_settings({"harness_evolution": {"max_effort": "ultra"}})


def test_the_v3_preset_freezes_a_k_with_the_agent_kind_the_effort_ceiling_and_the_five_limits(fake_upstream, tmp_path,
                                                                                               monkeypatch):
    """docs/modules/07_BENCH_REPORT.md §2 and M0 §2 (v0.3): `automationbench_harness_v3` is its own preset — the v1
    preset's numbers under package API v2 with the `agent` kind, `max_effort` high, and the five per-attempt hard
    limits at the design's values as [bench.budget]'s defaults — so a config that writes no budget bootstraps a K
    whose harness allows agents and whose TargetHarness and frozen task plan carry all five; [bench.budget] may lower
    one under it and is refused when it raises one; the v1 preset is untouched and supplies no budget."""
    from ouroloop.agent.worker import ProcessWorker
    from ouroloop.evolve.task_gate import TaskEvalPlan
    five = {"max_steps": 50, "max_model_calls": 200, "max_tool_calls": 200, "max_seconds": 900.0, "max_tokens": 400000}
    v1, v3 = bench.PRESETS["marketing_harness_v1"], bench.PRESETS["automationbench_harness_v3"]
    assert v1["harness"]["package_api"] == "harness_package.v1" and "agent" not in v1["harness"]["components"]
    assert "budget" not in v1 and bench.preset_budget({"improvement": {"preset": "marketing_harness_v1"}}) == {}
    assert v3["budget"] == five and v3["harness"]["package_api"] == "harness_package.v2"
    assert v3["harness"]["components"][0] == "agent" and set(v3["harness"]["components"]) == set(hp.KINDS)
    # v0.3 step 5 (M7 §2): the v3 preset has no domain lock — every listed domain must load — the v1 one keeps it
    assert v3["harness"]["max_effort"] == "high" and v3["domain"] is None and v1["domain"] == "marketing"
    same = lambda h: {k: v for k, v in h.items()  # noqa: E731
                      if k not in ("package_api", "components", "max_effort", "research_session", "screen")}
    assert same(v3["harness"]) == same(v1["harness"])
    # v0.3 step 3 (M4 §1, §2; M7 §2): the coding session's caps, the screen's numbers and k live on the v3 preset
    # only; the v1 preset has no session and runs one candidate a round as its cores froze it
    # v0.4 (V0_4_PLAN §1.3, §1.6): admission's read floors and the v0.4 screen (6 passing tasks redrawn per round,
    # ≥1 fix and ≤1 sample regression, net flips ≥ 0) join them; a K frozen before has neither
    assert v3["harness"]["research_session"] == {"max_turns": 40, "max_edits": 24, "max_trajectory_bytes": 65536,
                                                 "max_trajectory_bytes_total": 524288, "min_failing_reads": 1,
                                                 "min_passing_reads": 1}
    assert v3["harness"]["screen"] == {"passing_sample": 6, "min_net_flips": 0, "min_fixes": 1,
                                       "max_sample_regressions": 1} and v3["max_candidates_per_round"] == 3
    assert "research_session" not in v1["harness"] and "max_candidates_per_round" not in v1
    assert v3["schedule"] == v1["schedule"]
    # v0.3 step 2 (M3 §2.1, M6 §2, M5 §2): the v3 preset's gate statistics, slot rules and concurrency; the v1
    # preset's tables are the ones its cores froze and are not revisited
    assert v3["task_plan"] == {**v1["task_plan"], "control_pooling": "pooled", "confirmation_repeats": 1,
                               "gate": {**v1["task_plan"]["gate"], "regression_drop": 0.5, "cost_beta0": 0.10,
                                        "cost_beta1": 2.0, "min_tasks": 25}}
    # the arm and outer shares re-estimated for step 5's $2,800 root (M6 §2, 2026-10-05), v1's 0.15 / 0.08 kept
    assert v3["campaign"] == {**v1["campaign"], "worker_round_limit": 3, "min_headroom_tasks": 6,
                              "arm_share": 0.16, "outer_share": 0.06}
    assert v3["concurrency"] == 8 and "concurrency" not in v1
    assert "control_pooling" not in v1["task_plan"] and "regression_drop" not in v1["task_plan"]["gate"]
    preset = json.loads(json.dumps(v3))
    preset["domain"] = rsi.DOMAIN
    preset["harness"]["sandbox"] = None                 # no image to freeze here; the worker is the test's
    # the fixture's dev split has three tasks: the least count is the fixture's, the other clauses the preset's
    preset["task_plan"]["gate"].update(min_groups=1, bootstrap_replicates=200, min_tasks=len(rsi.DEV))
    monkeypatch.setitem(bench.PRESETS, "v3_test", preset)
    monkeypatch.setattr(bench, "isolated_worker", lambda settings: ProcessWorker(for_tests=True) if settings else None)
    cfg = cycle_config(fake_upstream, tmp_path)
    del cfg["bench"]["budget"]                          # the preset's five, not the config's
    cfg["improvement"]["preset"] = "v3_test"
    assert bench.BenchConfig.load(cfg).budget.limits() == five
    activity, _ = gap_w0(cfg, tmp_path)
    harness = bench._harness_of(activity)
    assert harness["package_api"] == "harness_package.v2" and harness["max_effort"] == "high"
    assert harness["package"]["components"][0] == "agent" and hp.Limits.from_json(harness["package"]).allows_code
    release = ct.SystemRelease.from_json(activity.objects.get(activity.release_ref))
    target = activity.objects.load(release.target_harness_ref, ct.TargetHarness)
    assert activity.objects.get(target.limits_ref) == five
    assert "harness_package.v2" in release.required_capabilities and "isolated_worker" in release.required_capabilities
    named = frozen(cfg)
    plan = TaskEvalPlan.from_json(activity.objects.get(named["rsi"]["task_plan_ref"]))
    assert {k: plan.per_attempt_budget[k] for k in bench.HARD_LIMITS} == five
    assert bench.startup_checks(bench.open_activity(named, ".", out=lambda _line: None), named, phase="baseline",
                                needs_model=False) == []
    lowered = bench.BenchConfig.load({**cfg, "bench": {**cfg["bench"], "budget": {"max_steps": 8, "max_tokens": 4000}}})
    assert (lowered.budget.max_steps, lowered.budget.max_tokens, lowered.budget.max_model_calls) == (8, 4000, 200)
    with pytest.raises(ValueError, match=r"\['max_tokens'\] above the preset's \[400000\].*not raised"):
        bench.BenchConfig.load({**cfg, "bench": {**cfg["bench"], "budget": {"max_tokens": 500000}}})


# ---------------------------------------------------------------------------- v0.3 (M6 §2): the slot's pools, headroom, pooled arms
def test_a_dev_split_under_twice_min_tasks_opens_no_slot(fake_upstream, tmp_path, fixture_preset):
    """M6 §2 (v0.3), RC6-15, ER6-22: the inner gate is the first half of dev and the outer pool the second, each no
    smaller than the gate's min_tasks; a dev split too small for that — the fixture's three families under
    min_tasks 2, as Marketing's 25 tasks under 25 — opens no slot: R is not asked, nothing is held or frozen, the
    slot records `no_slot_pool`, the next segment runs and the report's C4 is not_run with the reason."""
    fixture_preset["task_plan"]["gate"]["min_tasks"] = 2
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    slot = RProposer()
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=slot)
    step = result["steps"]["slot-0"]
    assert step["outcome"] == "no_slot_pool"
    assert "the inner half has 1 task, fewer than min_tasks 2" in step["reason"] and "dev 3 < 2 × 2" in step["reason"]
    assert "outer half" not in step["reason"]                      # only the inequality that failed is stated
    assert slot.requests == [] and "campaign" not in step and "headroom" not in step
    assert result["status"] == "done" and result["steps"]["segment-1"]["stop_reason"] == "no_proposal"
    assert result["report"]["C4"]["state"] == "not_run" and "too small for two gates" in result["report"]["C4"]["detail"]
    named = frozen(cfg)                       # the cycle's own plans, frozen again to the same refs
    activity = bench.open_activity(named, ".", out=lambda _line: None)
    pair = bench.cycle_pairs(activity, named)[0]
    families = list(dict.fromkeys(activity.splits.family_of(t) for t in rsi.DEV))
    assert pair.inner_dev_groups == (families[0],) and pair.outer_groups == tuple(families[1:])
    # on record: a second run of the cycle does not ask R either
    again = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=slot)
    assert again["steps"]["slot-0"]["outcome"] == "no_slot_pool" and slot.requests == []


@pytest.mark.parametrize("least,opens", [(2, False), (1, True)])
def test_a_slot_without_headroom_asks_r_nothing(fake_upstream, tmp_path, fixture_preset, monkeypatch, least, opens):
    """M6 §2 (v0.3), RC6-12, IT6-40: before R is asked the slot counts the inner dev tasks the start W never passed
    in its pooled readings; fewer than `campaign.min_headroom_tasks` and the slot records `no_headroom` — not a
    budget end, not an invalid round — and the next segment runs. The fixture's W0 never passes one inner task."""
    fixture_preset["campaign"]["min_headroom_tasks"] = least
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    outer_on_the_gap(monkeypatch)
    slot = RProposer()
    factory, made = arms([Stopper, Stopper, SkillAnyway])
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=slot,
                         arm_researcher=factory)
    step = result["steps"]["slot-0"]
    assert step["headroom"] == 1 and result["status"] == "done" and "segment-1" in result["steps"]
    if opens:
        assert len(slot.requests) == 1 and step.get("campaign") and step["verdict"] == "accepted"
        return
    assert step["outcome"] == "no_headroom" and "never passed 1 of the 1 inner dev tasks" in step["reason"]
    assert slot.requests == [] and made == [] and "campaign" not in step
    assert result["report"]["C4"]["state"] == "not_run" and "headroom" in result["report"]["C4"]["detail"]


def test_the_headroom_check_runs_before_the_budget_check(fake_upstream, tmp_path, fixture_preset, monkeypatch):
    """M6 §2 (v0.3, after review): the slot's checks run pools → headroom → budget, the budget last — a root with
    nothing left and a W without headroom give `no_headroom`, not `budget_exhausted`, and the slot is on record
    as such; a dev split too small is `no_slot_pool` before either, with the same empty root."""
    fixture_preset["campaign"]["min_headroom_tasks"] = 2
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    outer_on_the_gap(monkeypatch)
    bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), until="segment-0")
    _leave(tmp_path, 0)                                    # nothing left: the budget check alone would end the slot
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=RProposer())
    assert result["steps"]["slot-0"]["outcome"] == "no_headroom" and result["steps"]["slot-0"]["headroom"] == 1
    assert "segment-1" in result["steps"]                   # the activity went on; the empty root ends it there
    fixture_preset["task_plan"]["gate"]["min_tasks"] = 2
    other = cycle_config(fake_upstream, tmp_path / "other")
    gap_w0(other, tmp_path / "other")
    bench.cycle(other, ".", out=lambda _line: None, researcher=Stopper(), until="segment-0")
    _leave(tmp_path / "other", 0)
    result = bench.cycle(other, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=RProposer())
    assert result["steps"]["slot-0"]["outcome"] == "no_slot_pool" and "headroom" not in result["steps"]["slot-0"]


def test_a_frozen_plan_may_not_drop_the_presets_pooling_or_confirmation(fake_upstream, tmp_path, fixture_preset):
    """M3 §2.1(a)(b) (v0.3, after review): under a preset whose task plan pools the control and confirms once, a
    [freeze.task_plan] table that leaves the two fields out, or sets fresh / 0, loosens the gate and the cycle
    refuses it before anything runs; the preset's own values pass, and tightening is not possible here (there is
    nothing stricter than pooled and one confirmation)."""
    fixture_preset["task_plan"].update(control_pooling="pooled", confirmation_repeats=1)
    settings = bench.improvement_settings(cycle_config(fake_upstream, tmp_path))
    gate = dict(fixture_preset["task_plan"]["gate"])
    assert bench._beyond_preset(settings, None, gate, {"control_pooling": "pooled", "confirmation_repeats": 1}) == []
    for plan in ({}, {"control_pooling": "fresh", "confirmation_repeats": 0}):
        problems = bench._beyond_preset(settings, None, gate, plan)
        assert any(p.startswith("control_pooling") and "looser than the preset's pooled" in p for p in problems)
        assert any(p.startswith("confirmation_repeats") and "below the preset's 1" in p for p in problems)
    cfg = cycle_config(fake_upstream, tmp_path)
    cfg["freeze"]["task_plan"] = {"tasks": "dev", "gate": gate}            # the fields left out
    gap_w0(cfg, tmp_path)
    with pytest.raises(ValueError, match="beyond the preset.*control_pooling None is looser"):
        bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper())


def pooled_slot(fake_upstream, tmp_path, fixture_preset, monkeypatch):
    """A cycle whose template pools its control and confirms once, through the research slot with the gap as the
    arms' inner task: common inputs, the control arm (nothing proposed), the candidate arm (the skill)."""
    from ouroloop.research.campaign import CampaignPair, CampaignPlan, arm_workspace
    fixture_preset["task_plan"].update(control_pooling="pooled", confirmation_repeats=1)
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    outer_on_the_gap(monkeypatch)
    factory, made = arms([Stopper, Stopper, SkillAnyway])
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=RProposer(),
                         arm_researcher=factory)
    steps = result["steps"]
    assert result["status"] == "done" and steps["slot-0"]["verdict"] == "accepted", steps
    plan = activity.objects.load(steps["slot-0"]["campaign"], CampaignPlan)
    pair = activity.objects.load(plan.campaign_pairs[0], CampaignPair)
    roots = {side: arm_workspace(activity.root, pair.pair_id, side, plan.id) for side in ("common", "control",
                                                                                           "candidate")}
    return activity, plan, roots


def test_an_arm_pools_only_the_common_readings_and_its_own(fake_upstream, tmp_path, fixture_preset, monkeypatch):
    """M6 §2 (v0.3), RC6-13: under a pooled template the start W's one reading of the inner dev tasks is a common
    input and both arms get that reading as recorded; neither arm runs a control arm of its own, the candidate arm's
    gate pools the one common reading, and the control arm's record never holds the candidate arm's readings.
    (2026-10-05 review: the reading is the mainline baseline's own dev reading of the start W, reused as recorded —
    arm `base` — rather than a second `dev_baseline` reading paid for in the common scope.)"""
    from ouroloop.benchmark import Results
    activity, plan, roots = pooled_slot(fake_upstream, tmp_path, fixture_preset, monkeypatch)
    common = [r for r in Results(roots["common"], rsi.DOMAIN).all() if r.split == "dev"]
    assert len(common) == 1 and bench._arm_of(common[0].attempt_id) == "base" and not common[0].success
    assert common[0].attempt_id in {r.attempt_id for r in Results(activity.root, rsi.DOMAIN).all()}   # the mainline's
    rows = {side: list(Results(roots[side], rsi.DOMAIN).all()) for side in ("control", "candidate")}
    for side, arm_rows in rows.items():
        assert {r.attempt_id for r in arm_rows if r.split == "dev" and bench._arm_of(r.attempt_id) in ("base", "dev_baseline")} \
            == {common[0].attempt_id}, side
        assert not [r for r in arm_rows if bench._arm_of(r.attempt_id) == "control"], side
        assert (roots[side] / "bench" / rsi.DOMAIN / "runs" / f"{common[0].run}.json").exists()
    assert not [r for r in rows["control"] if bench._arm_of(r.attempt_id) in ("subject", "confirm")]
    assert {bench._arm_of(r.attempt_id) for r in rows["candidate"]} >= {"subject", "confirm"}
    objects = ct.Objects(roots["candidate"])
    receipt = objects.load(next(ct.Events(roots["candidate"]).of("evaluation_completed"))["receipt"],
                           ct.EvaluationReceipt)
    assert receipt.metrics["control_readings"] == 1 and receipt.metrics["control_attempt_refs"] == [common[0].attempt_id]
    assert objects.get(receipt.results_ref)["control_pooled"] is True
    from ouroloop.evolve.task_gate import TaskEvalPlan
    template = TaskEvalPlan.from_json(objects.get(receipt.plan_ref))
    assert template.pooled and template.confirmations == 1 and template.gate().min_groups == 1


def test_an_inner_gate_confirms_before_it_installs(fake_upstream, tmp_path, fixture_preset, monkeypatch):
    """M6 §2 (v0.3), RC6-13: the candidate arm's inner gate reads the skill once, then again, and installs on a
    `confirmed` receipt whose first reading and confirmation gain are both on it; the arm's state record went through
    `confirming`; the outer comparison then finds two different W* and R1 is installed."""
    from ouroloop.evolve.execution import Execution
    activity, plan, roots = pooled_slot(fake_upstream, tmp_path, fixture_preset, monkeypatch)
    objects = ct.Objects(roots["candidate"])
    events = list(ct.Events(roots["candidate"]).of("evaluation_completed"))
    receipt = objects.load(events[-1]["receipt"], ct.EvaluationReceipt)
    assert receipt.verdict == "accepted" and receipt.kind == "confirmed" and events[-1]["reading_kind"] == "confirmed"
    assert receipt.metrics["readings"] == 2 and receipt.metrics["first_reading"]["verdict"] == "accepted"
    assert receipt.metrics["confirmation_gain"] == receipt.metrics["gain"] == receipt.metrics["first_reading"]["gain"]
    states = [e["status"] for e in Execution(roots["candidate"], json.loads(
        (roots["candidate"] / "rsi" / "arm.json").read_text())["rounds"][0]["experiment"]).state()["history"]]
    assert states == ["admitted", "building", "screening", "evaluating", "evaluated", "confirming", "evaluated",
                      "accepted", "installing", "installed"]
    arm_events = [e for e in ct.Events(activity.root).of("campaign_arm")]
    assert len({e["w_star"] for e in arm_events}) == 2
    assert ct.SystemRelease.from_json(activity.objects.get(activity.releases.active()["release_ref"])).research_ref \
        == plan.r_candidate_ref


class Died(BaseException):
    """The process dying mid-arm: not an exception any handler is allowed to swallow."""


def test_a_control_arm_cut_short_is_topped_up_on_restart_and_nothing_is_paid_twice(fake_upstream, tmp_path,
                                                                                   fixture_preset, monkeypatch):
    """M3 §2.1(a) (v0.3, after review): under a pooled plan with nothing pooled yet (a first-version activity reads
    train only before its first gate), the process dies after the control arm recorded one task. The restart finds
    that reading in the pool, reads only the two pairs still short, and the gate is conclusive — one control attempt
    per task, the one recorded before the crash kept, none paid twice, the candidate installed."""
    from ouroloop.evolve import task_eval
    from ouroloop.evolve.execution import unfinished
    from ouroloop.benchmark import Results
    fixture_preset["task_plan"].update(control_pooling="pooled", confirmation_repeats=0)
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    named = frozen(cfg)
    original = task_eval.run_attempt
    state = {"control": 0}

    def dying(*args, **kw):
        if kw.get("arm") == "control":
            state["control"] += 1
            if state["control"] == 2:
                raise Died("the process died in the control arm")
        return original(*args, **kw)
    monkeypatch.setattr(task_eval, "run_attempt", dying)
    with pytest.raises(Died):
        bench.improve(named, ".", out=lambda _line: None, researcher=SkillAnyway())
    results = Results(activity.root, rsi.DOMAIN)
    recorded = [r for r in results.all() if bench._arm_of(r.attempt_id) == "control"]
    assert len(recorded) == 1 and unfinished(activity.root)[0]["status"] == "evaluating"
    monkeypatch.setattr(task_eval, "run_attempt", original)
    result = bench.improve(named, ".", out=lambda _line: None, researcher=SkillAnyway())
    assert result["rounds"][0]["outcome"] == "installed" and "resumed from evaluating" in result["rounds"][0]["reason"]
    control = [r for r in results.all() if bench._arm_of(r.attempt_id) == "control"]
    assert sorted(r.task_id for r in control) == sorted(rsi.DEV)            # one per task, the first one kept
    assert len({r.attempt_id for r in control}) == 3 and recorded[0].attempt_id in {r.attempt_id for r in control}
    receipt = activity.objects.load(list(ct.Events(activity.root).of("evaluation_completed"))[-1]["receipt"],
                                    ct.EvaluationReceipt)
    assert receipt.verdict == "accepted" and receipt.metrics["control_readings"] == 1
    assert sorted(receipt.metrics["control_attempt_refs"]) == sorted(r.attempt_id for r in control)
    assert activity.objects.get(receipt.results_ref)["control_pooled"] is True and not unfinished(activity.root)


# ---------------------------------------------------------------------------- v0.3 (M6 §2, RC6-16): the ledger in E0
def test_e0_strips_dev_task_ids_from_ledger_rows(fake_upstream, tmp_path, fixture_preset):
    """RC6-16 / M6 §2 (v0.3): the ledger rows enter both arms' E0 with the gate stage's per-task lists reduced to
    counts — the first half of dev is what the arms optimise on and the second half the outer pool — while the
    train task ids of the screen stage stay, as the experiment rows' raw gate reasons are left out."""
    from ouroloop.evolve import experience as xp
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    xp.append(activity.root, xp.ledger_row("screened", "cyc/r0/m1", scope="cyc", round=0,
                                           tasks=["rsi.notify_after_update"], net_flips=1, outcome="selected"))
    xp.append(activity.root, xp.ledger_row("gated", "cyc/r0/m1", scope="cyc", round=0, verdict="accepted",
                                           receipt="receipt:sha256:" + "b" * 64, fixed=["rsi.dev_notify_after_update"],
                                           regressed=[], still_failing=["rsi.dev_policy_hold"], installed=True))
    xp.append(activity.root, {"kind": "experiment", "experiment": "x1", "verdict": "rejected",
                              "reasons": ["rsi.dev_policy_hold regressed"], "metrics": {"gain": 0.0}})
    named = frozen(cfg)
    pair = bench.cycle_pairs(bench.open_activity(named, ".", out=lambda _line: None), named)[0]
    e0 = activity.objects.get(pair.initial_experience_ref)["experience"]
    gated = next(e for e in e0 if e.get("kind") == "ledger" and e.get("stage") == "gated")
    assert (gated["fixed"], gated["regressed"], gated["still_failing"]) == (1, 0, 1)
    screened = next(e for e in e0 if e.get("kind") == "ledger" and e.get("stage") == "screened")
    assert screened["tasks"] == ["rsi.notify_after_update"]
    assert "reasons" not in next(e for e in e0 if e.get("kind") == "experiment")
    assert "rsi.dev" not in json.dumps(e0)
    # and the folded rows an arm reads say the same with counts
    assert xp.ledger_rows(e0)[0]["gate"]["fixed"] == 1


def test_a_resumed_run_counts_rounds_lost_to_infrastructure_as_the_live_loop_does():
    """M4 §2 (2026-10-05, third recheck): live, a round lost to infrastructure is run again in its slot and counts
    toward the failure limit; a run resumed from its rows after a crash or a stop point starts from the same count,
    so it cannot pay for one more lost round than an uninterrupted run. A pause starts the count again (the restart
    follows the repair); a stop point does not."""
    from ouroloop.research.loop import counters_of
    lost = {"outcome": "inconclusive", "reason": "infrastructure: no candidate had a valid screen reading"}
    assert counters_of([{"outcome": "rejected"}, lost, lost]) == (1, 2)
    assert counters_of([lost, {"outcome": "stopped", "reason": "stopped_at"}, lost]) == (0, 2)
    assert counters_of([lost, {"outcome": "stopped", "reason": "repair_runtime"}, lost]) == (0, 1)
    assert counters_of([lost, {"outcome": "installed"}]) == (0, 0)
    halt = {"outcome": "infrastructure_failed", "reason": "worker gone", "next_action": "repair_runtime"}
    assert counters_of([halt, {"outcome": "stopped", "reason": "repair_runtime"}]) == (0, 0)


# ---------------------------------------------------------------------------- v0.3 step 5 (M6 §2): halving a pooled dev
def test_the_slot_halves_a_pooled_dev_inside_each_domain():
    """M6 §2 (v0.3 step 5): over one domain the slot cuts dev's frozen order at len // 2, as v0.1/v0.2 did; over
    several it cuts each domain's dev in its frozen order, the odd family going to the outer half on even list
    positions and to the inner on odd ones, so both halves span every domain and stay within a family of each other
    — five domains of 25 give 62 and 63, each domain 12 or 13 in each half, where cutting the pooled order would put
    the first two domains wholly inside and the last two wholly outside."""
    one = [f"rsi.t{i}" for i in range(7)]
    assert bench.halve_dev(one, {f: "rsi" for f in one}) == (one[:3], one[3:])
    names = ["marketing", "sales", "support", "hr", "finance"]
    fams = [f"{d}.t{i}" for d in names for i in range(25)]
    inner, outer = bench.halve_dev(fams, {f: f.split(".")[0] for f in fams})
    assert (len(inner), len(outer)) == (62, 63) and sorted(inner + outer) == sorted(fams)
    count = lambda half, d: sum(f.startswith(d + ".") for f in half)  # noqa: E731
    assert [count(inner, d) for d in names] == [12, 13, 12, 13, 12]
    assert [count(outer, d) for d in names] == [13, 12, 13, 12, 13]
    assert inner[:12] == fams[:12] and outer[:13] == fams[12:25]            # each domain in its frozen order
    pooled_cut = fams[:62]
    assert {f.split(".")[0] for f in pooled_cut} == {"marketing", "sales", "support"}   # what the halving avoids


# ---------------------------------------------------------------------------- 2026-10-05 review (M6 §2): the slot's money
def test_a_slot_whose_common_inputs_the_root_cannot_hold_asks_r_nothing(fake_upstream, tmp_path, fixture_preset,
                                                                       monkeypatch):
    """Review finding 1 (M6 §2): the open check counts the common inputs. With exactly enough left for both arms'
    shares and the outer comparison's (1520 model calls) and a start W the mainline holds no reusable reading of,
    the common inputs' estimate does not fit beside them: R is not asked, nothing is frozen, and the reason names the
    estimate — before the fix the slot opened, paid the common inputs and then could not set aside both arms."""
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), until="segment-0")
    _leave(tmp_path, 1520)
    monkeypatch.setattr(bench, "_mainline_readings", lambda *a, **k: ([], []))
    slot = RProposer()
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=slot, until="slot-0")
    step = result["steps"]["slot-0"]
    assert result["status"] == "budget_exhausted" and step["outcome"] == "budget_exhausted", step
    assert "the common inputs' estimate" in step["reason"] and "campaign" not in step and slot.requests == []
    # all five train tasks and the one inner dev task, at W0's measured calls per attempt
    cfg = bench._cycle_config(cfg, ".", bench.improvement_settings(cfg), lambda _line: None)
    activity = bench.open_activity(cfg, ".", out=lambda _line: None)
    pair = bench.cycle_pairs(activity, cfg)[0]
    estimate = bench.common_inputs_estimate(activity, cfg, pair, {"model_calls": 1})
    assert estimate["model_calls"] > 0 and str(estimate["model_calls"]) in step["reason"]


def test_the_slot_reuses_the_mainline_readings_of_the_start_w(fake_upstream, tmp_path, fixture_preset, monkeypatch):
    """Review findings 1 and 5 (M6 §2): the common inputs are the mainline's readings of the start W where they
    exist — W0's train measurement and its plan-less dev reading here — copied in as recorded, so the common scope
    pays for nothing; both arms still start from a measurement of every train task. Before the fix the common
    workspace measured W0 again on every train task and the inner dev task."""
    from ouroloop.benchmark import Results
    from ouroloop.research.campaign import CampaignPair
    activity, plan, roots = pooled_slot(fake_upstream, tmp_path, fixture_preset, monkeypatch)
    train = bench._tasks_of(activity, activity.objects.load(plan.campaign_pairs[0], CampaignPair).inner_train_groups)
    common = [e for e in ct.Events(activity.root).of("campaign_common")][-1]
    assert common["reused_train"] == common["measured"] == len(train) and common["reused_dev"] == 1
    book = json.loads((activity.root / "rsi" / "budget" / "book.json").read_text())
    assert not [r for r in book["reservations"].values() if r["scope_id"].endswith(":common")]
    mainline = {r.attempt_id for r in Results(activity.root, rsi.DOMAIN).all()}
    for side in ("common", "control", "candidate"):
        reused = [r for r in Results(roots[side], rsi.DOMAIN).all() if r.split == "train"
                  and r.attempt_id in mainline]
        assert {r.task_id for r in reused} == set(train), side            # the mainline's rows, as recorded


def test_an_installed_start_w_reuses_its_own_readings_and_measures_the_rest(fake_upstream, tmp_path, fixture_preset,
                                                                            monkeypatch):
    """Review finding 1 (M6 §2), the installed-W case: after segment-0 installs a W and remeasures the train tasks
    W0 failed under it, the slot's common inputs reuse those readings of W* and measure only the train tasks W* has
    none of; W0's readings are not W*'s and are not reused."""
    fixture_preset["schedule"].update(worker_rounds=2)
    cfg = cycle_config(fake_upstream, tmp_path)
    activity, _ = gap_w0(cfg, tmp_path)
    w0 = activity.releases.active()["release_ref"]
    bench.cycle(cfg, ".", out=lambda _line: None, researcher=SkillAnyway(), until="segment-0")
    cfg = bench._cycle_config(cfg, ".", bench.improvement_settings(cfg), lambda _line: None)
    activity = bench.open_activity(cfg, ".", out=lambda _line: None)
    w_star = activity.releases.active()["release_ref"]
    assert w_star != w0
    pair = bench.cycle_pairs(activity, cfg)[0]
    train = bench._tasks_of(activity, pair.inner_train_groups)
    reused, _dev = bench._mainline_readings(activity, None, train, ())
    assert reused and len(reused) < len(train)
    assert all(not bench.improvement(activity)._runs_like(r.system_release_ref, w0) for r in reused)
    estimate = bench.common_inputs_estimate(activity, cfg, pair, {"model_calls": 1})
    assert estimate["model_calls"] > 0                          # the train tasks W* has no reading of


# ---------------------------------------------------------------------------- stage 0/1 review (2026-10-05): casualties refilled
def test_an_infrastructure_casualty_in_the_baseline_is_refilled_once_and_the_pooled_reading_is_complete(
        fake_upstream, tmp_path, fixture_preset, monkeypatch):
    """Stage 0/1 review, item 3 (M5 §3): an attempt of W0's baseline lost to infrastructure — a 429 the provider's
    retries did not absorb in the 16-way trial, say — and recorded before any refill is not a permanent hole: the
    cycle's baseline finds it, runs it again once (retry 1, written with the attempt it supersedes), and W0's pooled
    dev reading then covers every dev task. Before the fix `resume` handed the casualty back as the reading."""
    from ouroloop.benchmark import Results
    from ouroloop.evolve import task_eval
    from ouroloop.evolve.task_gate import shortfall
    fixture_preset["task_plan"].update(control_pooling="pooled", confirmation_repeats=1)
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    cfg = bench._cycle_config(cfg, ".", bench.improvement_settings(cfg), lambda _line: None)
    original, broke = task_eval.run_attempt, []

    def flaky(*args, **kw):
        if not broke and kw["run"].split == "dev" and kw.get("retry_index", 0) == 0:
            broke.append(args[1])
            row = task_eval.TaskResult.broken(
                args[1], args[1], "dev", kw.get("attempt", 0), kw["run"], kw["splits"].hash(),
                "rate limited: the provider's retries were used up", failure_class="infrastructure",
                attempt_id=task_eval.bench_attempt_id(kw["run"].hash(), args[1], kw.get("attempt", 0),
                                                      kw.get("arm", "subject"), 0),
                pair_key=task_eval.bench_pair_key(kw.get("plan_ref", ""), args[1], kw.get("attempt", 0)))
            return task_eval.Attempt(row, None)
        return original(*args, **kw)
    # the trial: the casualty is recorded and the run stops before any refill
    monkeypatch.setattr(task_eval, "run_attempt", flaky)
    monkeypatch.setattr(bench, "_refill_attempts", lambda rows, run, arm, limit: rows)
    bench.baseline(cfg, ".", out=lambda _line: None)
    monkeypatch.undo()
    fixture_preset["task_plan"].update(control_pooling="pooled", confirmation_repeats=1)
    monkeypatch.setitem(bench.PRESETS, "rsi_harness_test", fixture_preset)
    casualty = [r for r in Results(tmp_path / "ws", rsi.DOMAIN).all() if r.failure_class == "infrastructure"]
    assert len(casualty) == 1 and casualty[0].task_id == broke[0]
    # the cycle's baseline: the casualty run again once, nothing else paid twice
    bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), until="baseline")
    rows = list(Results(tmp_path / "ws", rsi.DOMAIN).all())
    refills = [r for r in rows if r.supersedes]
    assert len(refills) == 1 and refills[0].supersedes == casualty[0].attempt_id and refills[0].validity == "valid"
    assert refills[0].attempt_id.endswith("#1")
    activity = bench.open_activity(cfg, ".", out=lambda _line: None)
    work = bench.improvement(activity, template_ref=activity.rsi.task_plan_ref)
    release = activity.releases.active()["release_ref"]
    plan = work.plan("", "", release)
    pool = work.control_pool(release, plan=plan)
    assert {r.task_id for r in pool} == set(rsi.DEV) and shortfall(plan, pool) == []
