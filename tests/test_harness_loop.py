"""Stage A end to end (docs/modules/08_INTEGRATION_CASES.md IT6-22 text part, IT6-27, IT6-28; RL6-17/18/19):
a first-version activity diagnoses train failures through per-task notes, proposes a text package, revises a
failed draft, passes the gate, installs, and the next session actually mounts and reads the package.

The researcher here is scripted and reads only what it is given — failure summaries and check output — and the
worker is the fixture's BusinessWorker, which changes behaviour only when a skill it reads tells it to.
"""
import json

import rsi

from ouroloop import bench, policies as P
from ouroloop.agent import harness_package as hp
from ouroloop.evolve import contracts as ct
from ouroloop.evolve import experience as xp
from ouroloop.evolve.task_gate import GatePlan

SKILL = "check-before-finishing"


class NoteResearcher:
    """Writes a note from each failure summary, proposes a skill, and fixes its own broken first draft."""

    def __init__(self, *, broken_first_draft: bool = True, repairable: bool = True):
        self.broken_first_draft = broken_first_draft
        self.repairable = repairable
        self.requests = []
        self.seen = []

    def note_request(self, summary, prompt, *, allowed=()):
        return prompt, json.dumps({"allowed": list(allowed), "failure": summary}, sort_keys=True)

    def note(self, system, user):
        failure = json.loads(user)["failure"]
        self.requests.append({"step": "note", "task": failure["task_id"]})
        self.seen.append(user)
        missed = any("slack" in json.dumps(c).lower() or "message" in json.dumps(c).lower()
                     for c in failure["checks"] if not c["passed"])
        return {"failure_class": "missed_step" if missed else "other",
                "root_cause": "finished after the update without doing the rest of the request",
                "mechanism": "re-read the request and check every step before finishing",
                "component": "skills", "generality": "any multi-step request can end early"}

    def summarize(self, context, prompt, notes, coverage, experience=(), *, package=None, allowed=()):
        from ouroloop.research.experiment import ProposalResult
        self.requests.append({"step": "propose", "coverage": coverage})
        self.seen.append(json.dumps({"notes": notes, "experience": list(experience)}, default=str))
        if not any(n["failure_class"] == "missed_step" for n in notes):
            return ProposalResult(next_action="stop", reason="nothing general to fix")
        manifest = hp.empty_manifest()
        if not self.broken_first_draft:
            manifest["skills"] = [{"name": SKILL, "path": f"skills/{SKILL}.md", "description": "before finishing"}]
        patch = {"write": {"manifest.json": json.dumps(manifest),
                           f"skills/{SKILL}.md": f"Before you finish, {rsi.FINISH_CHECK_MARK}."}}
        proposal = ct.ExperimentProposal(owner="worker", hypothesis="a skill that checks every step closes it",
                                         changes=({"target": "worker.harness", "kind": "workflow",
                                                   "change": {"patch": patch}},),
                                         expected="the notification task passes", origin="scripted")
        result = ProposalResult(proposals=[proposal])
        result.requirements = ({"id": "r1", "observed_behavior": "stops after the update"},)
        return result

    def revise(self, prompt, patch, check_output):
        self.requests.append({"step": "revise", "check_output": check_output})
        self.seen.append(check_output)
        if not self.repairable:
            return patch
        manifest = json.loads(patch["write"]["manifest.json"])
        manifest["skills"] = [{"name": SKILL, "path": f"skills/{SKILL}.md", "description": "before finishing"}]
        return {"write": {**patch["write"], "manifest.json": json.dumps(manifest)}}


def v06_prepared(fake_upstream, tmp_path, *, max_draft_repairs=3):
    from ouroloop.benchmark import Results
    from ouroloop.ledger import write_json
    cfg = {"workspace": {"root": str(tmp_path / "ws")},
           "bench": {"domain": rsi.DOMAIN, "upstream_path": fake_upstream, "families": dict(rsi.FAMILIES),
                     "budget": {"max_steps": 8, "max_tool_calls": 12},
                     "ratios": {"train": 0.5, "dev": 0.25, "test": 0.25}},
           "llm": {"type": "rsi:BusinessWorker"},
           "backends": {"mock": {"type": "mock"}},
           "rsi": {"scope_id": "a", "repeats": 1},
           "harness_evolution": {"components": ["memory", "skills"], "max_draft_repairs": max_draft_repairs,
                                 "max_failure_notes": 30}}
    suite = rsi.suite(fake_upstream)
    rsi.splits(suite).freeze(Results(tmp_path / "ws", rsi.DOMAIN).manifest_path())
    activity = bench.open_activity(cfg, ".", out=lambda _line: None)
    objects = activity.objects
    booted = ct.SystemRelease.from_json(objects.get(activity.release_ref))
    worker = objects.load(booted.worker_ref, ct.PolicySet)
    by_target = {objects.load(r, ct.PolicyDescriptor).target: objects.load(r, ct.PolicyDescriptor)
                 for r in worker.policies}
    actor_ref = by_target["worker.actor"].implementation["model_identity_ref"]
    w0 = P.default_worker_set(objects, finish_check=rsi.W0_FINISH, actor_identity_ref=actor_ref,
                              package_ref=hp.empty_package(objects))
    release = activity.releases.bootstrap(w0, booted.research_ref, booted.core_ref, reason="W0 fixture",
                                          activate=False)
    write_json(activity.releases.active_path, {"release_ref": release, "revision": 1, "last_operation_id": None})
    plan_ref = bench.freeze_task_plan(objects, core_ref=booted.core_ref, split_ref=activity.splits.hash(),
                                      task_ids=tuple(rsi.DEV),
                                      gate=GatePlan(min_gain=0.05, min_groups=1, bootstrap_replicates=200))
    cfg["rsi"]["task_plan_ref"] = plan_ref
    cfg["rsi"]["schedule_ref"] = objects.put_json("change", {"worker_rounds": 1, "patience": 1,
                                                            "candidates_per_round": 1})
    return cfg, activity, release


def test_it6_22_text_package_is_diagnosed_drafted_revised_installed_and_mounted(fake_upstream, tmp_path):
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    researcher = NoteResearcher()
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=researcher)
    outcomes = [r["outcome"] for r in result["rounds"]]
    assert "installed" in outcomes, outcomes
    objects = activity.objects
    # the installed W mounts a package that holds the skill, and a new session composes it into the prompt
    bound = P.bind_release(result["release"], profile="task", services=P.Services(objects), authorization="x")
    assert bound.package is not None and bound.package.skill_text(SKILL)
    prompt, parts = bound.system_prompt("BASE")
    assert "worker.harness" in parts["parts"] and SKILL in prompt
    # RL6-18 / IT6-28: the first draft failed its check, R saw only the check output, the revision passed
    checks = list(ct.Events(activity.root).of("draft_check"))
    assert [c["ok"] for c in checks] == [False, True] and checks[0]["errors"]
    revise = [r for r in researcher.requests if r["step"] == "revise"]
    assert len(revise) == 1 and "not listed" in revise[0]["check_output"]
    assert "rsi.dev_" not in revise[0]["check_output"] and "partial" not in revise[0]["check_output"]
    # RL6-19 / IT6-27: notes on train failures only, coverage recorded, and nothing from dev reaches R
    diagnosis = [e for e in xp.read(activity.root) if e.get("kind") == "diagnosis"]
    assert diagnosis and diagnosis[0]["noted"] == diagnosis[0]["failures"] >= 1
    assert all(t.startswith("rsi.") and "dev" not in t for t in diagnosis[0]["included"])
    assert not any("rsi.dev_" in text or "rsi.final_" in text for text in researcher.seen)
    notes = list(ct.Objects(activity.root).dir.joinpath("note").glob("*.json"))
    assert notes


def test_it6_28_revisions_run_out_and_the_round_ends_invalid_without_freezing(fake_upstream, tmp_path):
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path, max_draft_repairs=2)
    researcher = NoteResearcher(repairable=False)
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=researcher)
    outcomes = [r["outcome"] for r in result["rounds"]]
    assert outcomes[0] == "invalid", outcomes
    assert result["release"] == w0                                   # nothing installed
    checks = list(ct.Events(activity.root).of("draft_check"))
    assert len(checks) == 3 and not any(c["ok"] for c in checks)     # the first draft and two revisions
    assert len([r for r in researcher.requests if r["step"] == "revise"]) == 2
    assert not list(ct.Objects(activity.root).dir.joinpath("experiment").glob("*.json"))   # never frozen


def test_notes_are_reused_only_on_their_full_identity(fake_upstream, tmp_path):
    """RL6-24 in miniature: the same summary under the same prompt is not asked twice; a changed prompt is."""
    from pathlib import Path
    from ouroloop.research import diagnosis as dg
    objects = ct.Objects(tmp_path)
    cache = dg.NoteCache(Path(tmp_path), objects)
    summary = {"task_id": "rsi.update_stage", "checks": [], "turns": []}
    researcher = NoteResearcher()
    run = dg.Diagnosis(summaries=[summary], order=["rsi.update_stage"], cache=cache, model={"model": "m"})
    run.run(researcher, "diagnose v1")
    run.run(researcher, "diagnose v1")
    assert len([r for r in researcher.requests if r["step"] == "note"]) == 1
    run.run(researcher, "diagnose v2")                                # R1 changed diagnose: a new note
    assert len([r for r in researcher.requests if r["step"] == "note"]) == 2
    dg.Diagnosis(summaries=[summary], order=["rsi.update_stage"], cache=cache,
                 model={"model": "other"}).run(researcher, "diagnose v1")
    assert len([r for r in researcher.requests if r["step"] == "note"]) == 3


def test_note_selection_rotates_to_failures_not_yet_read():
    from ouroloop.research import diagnosis as dg
    summaries = [{"task_id": t} for t in ("a", "b", "c", "d")]
    first, rest = dg.select_failures(summaries, order=["a", "b", "c", "d"], last_included={}, cap=2)
    assert [s["task_id"] for s in first] == ["a", "b"]
    second, _ = dg.select_failures(summaries, order=["a", "b", "c", "d"], last_included={"a": 0, "b": 0}, cap=2)
    assert [s["task_id"] for s in second] == ["c", "d"]


def test_the_entry_point_origin_survives_normalisation(tmp_path):
    from ouroloop.research.experiment import normalize
    objects = ct.Objects(tmp_path)
    p = ct.ExperimentProposal(owner="worker", changes=({"target": "worker.system_prompt", "kind": "prompt",
                                                        "change": {"text": "x"}},), origin="scripted")
    assert normalize(p, objects).origin == "scripted"


def test_br6_17_a_core_that_allows_code_cannot_start_without_a_worker(fake_upstream, tmp_path):
    import pytest
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    # the same activity, frozen with a core that allows a code component
    core = activity.objects.load(ct.SystemRelease.from_json(activity.objects.get(w0)).core_ref, ct.CoreManifest)
    harness = dict(core.capabilities["harness"])
    harness["package"] = {**harness["package"], "components": ["memory", "skills", "on_finish"]}
    core2 = activity.objects.put(ct.CoreManifest(**{**{f: getattr(core, f) for f in core.__dataclass_fields__},
                                                     "capabilities": {**core.capabilities, "harness": harness}}))
    rel = ct.SystemRelease.from_json(activity.objects.get(w0))
    release = activity.releases.bootstrap(rel.worker_ref, rel.research_ref, core2, reason="code allowed",
                                          activate=False)
    from ouroloop.ledger import write_json
    write_json(activity.releases.active_path, {"release_ref": release, "revision": 2, "last_operation_id": None})
    with pytest.raises(ValueError, match="no qualified isolated worker"):
        bench.improve(cfg, ".", out=lambda _line: None, researcher=NoteResearcher())


def test_rl6_20_after_an_install_only_the_failed_train_tasks_are_rerun(fake_upstream, tmp_path):
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    cfg["rsi"]["schedule_ref"] = activity.objects.put_json("change", {"worker_rounds": 3, "patience": 2,
                                                                     "candidates_per_round": 1})
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=NoteResearcher())
    rows = [r for r in activity.results.all() if "/measure" in (r.attempt_id or "")]
    first = [r for r in rows if r.system_release_ref == w0]
    second = [r for r in rows if r.system_release_ref == result["release"]]
    assert sorted(r.task_id for r in first) == sorted(rsi.TRAIN)                    # round 0: every train task
    failed_first = sorted(r.task_id for r in first if not r.success)
    assert failed_first and sorted(r.task_id for r in second) == failed_first      # round 1: the failures only
    assert all(r.success for r in second)                                          # the skill closed them
    # the passing results of round 0 are history of W0, not rerun and not relabelled as the new version's
    assert all(r.system_release_ref == w0 for r in first)


def test_rl6_22_a_round_that_installs_nothing_reuses_the_measurement(fake_upstream, tmp_path):
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path, max_draft_repairs=0)
    cfg["rsi"]["schedule_ref"] = activity.objects.put_json("change", {"worker_rounds": 2, "patience": 3,
                                                                     "candidates_per_round": 1})
    bench.improve(cfg, ".", out=lambda _line: None, researcher=NoteResearcher(repairable=False))
    rows = [r for r in activity.results.all() if "/measure" in (r.attempt_id or "")]
    assert sorted(r.task_id for r in rows) == sorted(rsi.TRAIN)                     # measured once, reused once


class CollectFirst(NoteResearcher):
    """Asks for more evidence before proposing, `times` times."""

    def __init__(self, times=1, **kw):
        super().__init__(**kw)
        self.times = times

    def summarize(self, context, prompt, notes, coverage, experience=(), *, package=None, allowed=()):
        from ouroloop.research.experiment import ProposalResult
        if self.times > 0:
            self.times -= 1
            self.requests.append({"step": "propose", "coverage": coverage})
            return ProposalResult(next_action="collect", reason="one attempt per task is too few to see a pattern")
        return super().summarize(context, prompt, notes, coverage, experience, package=package, allowed=allowed)


def test_rl6_11_a_collect_request_is_dispatched_and_the_next_round_proposes(fake_upstream, tmp_path):
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    cfg["rsi"]["schedule_ref"] = activity.objects.put_json("change", {"worker_rounds": 3, "patience": 2,
                                                                     "candidates_per_round": 1})
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=CollectFirst(times=1))
    outcomes = [r["outcome"] for r in result["rounds"]]
    assert outcomes[:3] == ["needs_collection", "collected", "installed"], outcomes
    assert result["rounds"][0]["action_ref"] == result["rounds"][1]["action_ref"]      # the round names its request
    collected = [r for r in result["rounds"] if r["outcome"] == "collected"][0]
    request = activity.objects.load(collected["action_ref"], ct.CollectionRequest)
    assert request.purpose == "task_feedback" and request.mode == "plain"
    rows = [r for r in activity.results.all() if "/collect" in (r.attempt_id or "")]
    assert rows and all(r.split == "train" for r in rows)


def test_it6_19_collection_is_bounded_per_segment(fake_upstream, tmp_path):
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    cfg["rsi"]["schedule_ref"] = activity.objects.put_json("change", {"worker_rounds": 6, "patience": 5,
                                                                     "candidates_per_round": 1,
                                                                     "collection_batches": 1})
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=CollectFirst(times=5))
    outcomes = [r["outcome"] for r in result["rounds"]]
    assert outcomes.count("collected") == 1 and result["rounds"][-1]["reason"] == "collection_limit", outcomes


class ExplodingWorker:
    """A provider that must never be called: it stands in for a local [llm] edit made after bootstrap."""
    name = "exploding"
    model = "exploding-1"

    def chat(self, system, messages, specs):
        raise AssertionError("the configured [llm] was used instead of the actor the release pins")


def test_pr6_01_the_release_pins_the_actor_and_the_config_cannot_override_it(fake_upstream, tmp_path):
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    cfg["llm"] = {"type": "test_harness_loop:ExplodingWorker"}
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=NoteResearcher())
    assert "installed" in [r["outcome"] for r in result["rounds"]]
    rel = ct.SystemRelease.from_json(activity.objects.get(w0))
    actor = next(activity.objects.load(r, ct.PolicyDescriptor) for r in
                 activity.objects.load(rel.worker_ref, ct.PolicySet).policies
                 if activity.objects.load(r, ct.PolicyDescriptor).target == "worker.actor")
    identity = activity.objects.load(actor.implementation["model_identity_ref"], ct.ModelIdentity)
    assert identity.role == "actor" and identity.provider == "rsi:BusinessWorker" and identity.mutable
    runs = list((activity.results.root / "runs").glob("*.json"))
    manifests = [json.loads(p.read_text()) for p in runs]
    assert any(m.get("actor_identity_ref") == identity.ref() for m in manifests)
    assert all("exploding" not in json.dumps(m.get("model")) for m in manifests)


# ---------------------------------------------------------------------------- review fixes
def test_collection_quota_and_progress_survive_a_restart(fake_upstream, tmp_path):
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    cfg["rsi"]["schedule_ref"] = activity.objects.put_json("change", {"worker_rounds": 4, "patience": 4,
                                                                     "candidates_per_round": 1,
                                                                     "collection_batches": 1})
    first = bench.improve(cfg, ".", out=lambda _line: None, researcher=CollectFirst(times=5))
    second = bench.improve(cfg, ".", out=lambda _line: None, researcher=CollectFirst(times=5))
    assert [r["outcome"] for r in first["rounds"]].count("collected") == 1
    assert "collected" not in [r["outcome"] for r in second["rounds"]]             # the quota was not reset
    assert second["rounds"][-1]["reason"] == "collection_limit"


def test_a_restart_after_an_install_reruns_only_the_failures(fake_upstream, tmp_path):
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    first = bench.improve(cfg, ".", out=lambda _line: None, researcher=NoteResearcher())
    assert first["release"] != w0
    bench.improve(cfg, ".", out=lambda _line: None, researcher=NoteResearcher())
    under_w1 = [r for r in activity.results.all() if "/measure" in (r.attempt_id or "")
                and r.system_release_ref == first["release"]]
    failed_w0 = sorted({r.task_id for r in activity.results.all() if "/measure" in (r.attempt_id or "")
                        and r.system_release_ref == w0 and not r.success})
    assert sorted({r.task_id for r in under_w1}) == failed_w0


class DiesOnRevise(NoteResearcher):
    def revise(self, prompt, patch, check_output):
        raise KeyboardInterrupt("the process dies while R revises the draft")


def test_ex6_12_an_interrupted_draft_resumes_its_revision_count(fake_upstream, tmp_path):
    import pytest
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    with pytest.raises(KeyboardInterrupt):
        bench.improve(cfg, ".", out=lambda _line: None, researcher=DiesOnRevise())
    resumed = NoteResearcher()
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=resumed)
    assert "installed" in [r["outcome"] for r in result["rounds"]]
    assert not [r for r in resumed.requests if r["step"] == "propose"]      # no new proposal was asked for
    checks = list(ct.Events(activity.root).of("draft_check"))
    assert [c["revision"] for c in checks] == [0, 1] and len({c["draft_id"] for c in checks}) == 1


def test_budget_exhaustion_in_the_note_map_stops_the_round_cleanly(fake_upstream, tmp_path):
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)

    class Broke(NoteResearcher):
        def note(self, system, user):
            from ouroloop.evolve.execution import BudgetError
            raise BudgetError("model_calls exhausted")
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=Broke())
    assert [r["outcome"] for r in result["rounds"]][:2] == ["budget_exhausted", "stopped"]
    assert result["rounds"][1]["reason"] == "budget_exhausted"


def test_the_summary_call_reads_no_dev_task_ids_or_scorer_text_from_the_experience():
    from ouroloop.research.diagnosis import experience_projection
    row = {"kind": "experiment", "outcome": "inconclusive", "hypothesis": "h", "changes": [{"target": "worker.harness"}],
           "reasons": ["evaluator failure on rsi.dev_notify_after_update: scorer: ValueError: Unknown assertion",
                       "2 pairs are incomplete: ['rsi.dev_update_stage/r0']"],
           "open_questions": ["rsi.dev_policy_hold looked odd"], "metrics": {"gain": 0.0, "regression_rate": 0.1}}
    text = json.dumps(experience_projection([row]))
    assert "rsi.dev" not in text and "ValueError" not in text and "Unknown assertion" not in text
    assert "evaluator_failure" in text and "incomplete_pairs" in text


def test_harness_config_on_an_existing_v2_activity_is_refused(fake_upstream, tmp_path):
    import pytest
    from test_rsi_integration import prepared
    cfg, activity, release, plan_ref = prepared(fake_upstream, tmp_path)
    cfg["harness_evolution"] = {"components": ["memory", "skills"]}
    with pytest.raises(ValueError, match="bootstrapped without it"):
        bench.improve(cfg, ".", out=lambda _line: None, researcher=NoteResearcher())


def test_v2_prompt_hash_is_unchanged_and_r_candidates_keep_the_note_contract(tmp_path):
    releases, rel, objects = rsi.workspace(tmp_path)
    bound = P.bind_release(rel, profile="task", services=P.Services(objects))
    assert "package_ref" not in bound.system_prompt("BASE")[1]
    r0 = P.default_research_set(objects, note_contract=True)
    parent = objects.load(rel, ct.SystemRelease)
    release = releases.bootstrap(parent.worker_ref, r0, parent.core_ref, reason="r0", activate=False)
    proposal = ct.ExperimentProposal(owner="research", changes=({"target": "research.diagnose", "kind": "prompt",
                                                                 "change": {"text": "R1 diagnose"}},))
    exp = ct.Experiment(id="x", proposal_ref=objects.put(proposal), parent_release=release, experience_ref="",
                        core_ref=parent.core_ref, eval_template_ref="", budget_scope="s")
    built = P.build_candidate(exp, objects=objects, parent_release=release, owner="research")
    candidate = objects.load(built.candidate_ref, ct.PolicySet)
    diagnose = next(objects.load(r, ct.PolicyDescriptor) for r in candidate.policies
                    if objects.load(r, ct.PolicyDescriptor).target == "research.diagnose")
    assert diagnose.output_schema == "failure_note.v1"


def test_the_first_version_baseline_runs_train_and_dev_with_the_pinned_actor(fake_upstream, tmp_path):
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    cfg["llm"] = {"type": "test_harness_loop:ExplodingWorker"}
    report = bench.baseline(cfg, ".", out=lambda _line: None)
    base = report["arms"]["base"]
    assert base["model"]["provider"] == "rsi:BusinessWorker" and base["model"]["actor_identity_ref"]
    assert base["summary"]["tasks"] == len(rsi.DEV) and base["train"]["tasks"] == len(rsi.TRAIN)


class FrontierProbe:
    """A frontier arm's own model: it counts its calls and answers like the fixture worker."""
    name = "frontier-probe"
    model = "frontier-x"
    calls = 0

    def chat(self, system, messages, specs):
        FrontierProbe.calls += 1
        return rsi.BusinessWorker().chat(system, messages, specs)


def test_a_final_comparison_with_its_own_model_runs_that_model_not_the_pinned_actor(fake_upstream, tmp_path):
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    FrontierProbe.calls = 0
    plan = bench.FinalizationPlan(
        experiment_id="final-frontier", core_ref=activity.objects.load(w0, ct.SystemRelease).core_ref,
        split_ref=activity.splits.hash(), claims=("C1", "C2"),
        comparisons=({"claim": "C1", "subject": "base", "control": "base", "role": "base"},
                     {"claim": "C2", "subject": "frontier_a", "control": "base", "role": "frontier_a",
                      "llm": {"type": "test_harness_loop:FrontierProbe"}}),
        release_refs={"base": w0, "frontier_a": w0}, task_ids=tuple(rsi.FINAL), repeats=1,
        statistical_plan={"confidence": 0.95, "bootstrap_replicates": 100, "bootstrap_seed": 1})
    sealed = bench.seal(plan, activity.root, activity.objects)
    cfg["rsi"]["final_plan_ref"] = sealed["plan"]
    bench.evaluate(cfg, ".", out=lambda _line: None, plan_ref=sealed["plan"])
    assert FrontierProbe.calls > 0
    runs = [json.loads(p.read_text()) for p in (activity.results.root / "runs").glob("*.json")]
    frontier = [m for m in runs if m.get("candidate") == "frontier_a"]
    assert frontier and all(m["model"]["provider"] == "frontier-probe" and not m.get("actor_identity_ref")
                            for m in frontier)


def test_the_report_shows_the_package_what_the_actor_read_and_what_the_drafts_cost(fake_upstream, tmp_path):
    """docs/modules/07_BENCH_REPORT.md §4: the package of each release, the skill reads the transcripts show, the
    draft checks, the note coverage, the gate's frozen thresholds; C3 is not_run in the first version."""
    from ouroloop.report import build_report, render_markdown
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    bench.improve(cfg, ".", out=lambda _line: None, researcher=NoteResearcher())
    data = build_report(activity.root, rsi.DOMAIN)
    harness = data["harness"]
    with_skill = [p for p in harness["packages"].values() if SKILL in p["skills"]]
    assert with_skill and with_skill[0]["kinds"] == ["skills"] and not with_skill[0]["hooks"]
    assert harness["behaviour"]["skill_reads"].get(SKILL, 0) >= 1          # the actor actually opened it
    assert harness["drafts"]["checks"] == 2 and harness["drafts"]["refused"] == 1
    assert harness["notes"][0]["noted"] >= 1
    gate = harness["gates"][0]
    assert gate["min_gain"] == 0.05 and gate["verdict"] in ("accepted", "rejected", "inconclusive")
    assert data["claims"]["C3"]["state"] == "not_run"
    assert "## Capability package" in render_markdown(data)


def test_published_numbers_are_background_and_c2_says_so(fake_upstream, tmp_path):
    from ouroloop.report import build_report
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    path = activity.root / "bench" / rsi.DOMAIN / "external_references.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"source_url": "https://example.org/leaderboard", "retrieved_at": "2026-09-28",
                                 "model": "frontier-x", "effort": "high", "score": 0.41, "metric": "pass rate",
                                 "task_set": "private", "domain": "marketing"}]))
    data = build_report(activity.root, rsi.DOMAIN)
    assert data["external_references"][0]["incomplete"] == ["protocol_notes"]
    assert data["claims"]["C2"]["state"] == "inconclusive"
    assert data["claims"]["C2"]["evidence_kind"] == "external_reference"
