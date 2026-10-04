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


@pytest.mark.parametrize("schedule,seq", [
    ({"worker_rounds": 3, "patience": 3, "max_infrastructure_failures": 2}, [("inconclusive", INFRA)] * 2),
    ({"worker_rounds": 3, "patience": 2, "max_infrastructure_failures": 2},
     [("rejected", "no gain"), ("inconclusive", INFRA)])])
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
    assert authorization.kind == "accepted" and not activity.objects.has(plan["gate_plan"]["ref"])
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
    the comparison is inconclusive for the budget rather than judged with R1's arm starved."""
    from ouroloop.evolve.execution import Budgets
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), until="segment-0")
    _leave(tmp_path, 1520)
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
