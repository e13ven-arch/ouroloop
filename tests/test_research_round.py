"""M4 acceptance (docs/modules/04_RESEARCH_LOOP.md §5): diagnosis, admission and what a round writes down.

A scripted research step is used on purpose: what is checked here is the control flow and the permissions, not
whether a real model can find a good change. That is a business experiment, and a test with a hand-written correct
candidate would not be evidence for it.
"""
import json

import pytest
import rsi
from train_fixtures import PhraseTrainer

from ouroloop import bench, policies as P
from ouroloop.evolve import contracts as ct
from ouroloop.evolve import experience as experience_mod
from ouroloop.evolve.execution import Budgets
from ouroloop.evolve.task_gate import GatePlan
from ouroloop.research.context import make_context
from ouroloop.research.experiment import (ProposalResult, admit, check_duplicate_experiment,
                                          check_input_coverage, check_label_availability, freeze, normalize,
                                          propose_experiment, retrieve)


def setup(fake_upstream, tmp_path, **kw):
    from test_rsi_integration import prepared
    return prepared(fake_upstream, tmp_path, out=lambda _l: None, **kw)


def context_of(activity, work, **kw):
    evidence = work.collect()
    experience, ref = experience_mod.snapshot(activity.root)
    return work.context(goal="close the gap", release_ref=activity.releases.active()["release_ref"],
                        evidence=evidence, experience=experience, experience_ref=ref), evidence


def core_of(activity, work):
    return activity.objects.load(work.core_ref, ct.CoreManifest)


# ---------------------------------------------------------------------------- RL-01
def test_rl01_a_training_proposal_without_the_input_or_the_labels_does_not_spend_the_budget(fake_upstream,
                                                                                            tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path, train=True)
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker(),
                             trainer=PhraseTrainer())
    context, evidence = context_of(activity, work)
    trained = rsi.ScriptedResearcher(carrier="train", min_labels=0)
    result = propose_experiment(context, P.bind_release(activity.releases.active()["release_ref"], profile="task",
                                                       services=work.services(), authorization="installed"),
                               "worker", researcher=trained, objects=activity.objects)
    proposal = result.proposals[0]
    # no coverage at all yet: the check warns rather than letting a training job start on nothing
    empty = make_context("it", goal="g", release_ref=release, objects=activity.objects,
                         evidence_refs=proposal.evidence_refs, inspection={"coverage": {}},
                         feedback={"valid": 0, "labels": {}})
    assert check_input_coverage(proposal, context=empty)[0] == "warn"
    assert check_label_availability(proposal, context=empty)[0] == "block"
    admission = admit(proposal, context=empty, core=core_of(activity, work), objects=activity.objects,
                      checks=("input_coverage", "label_availability"))
    assert admission.status == "collect" and any("no valid label" in r for r in admission.reasons)
    budget = Budgets(activity.root).account("rl01", {"train_jobs": 1})
    assert budget.remaining()["used"] == {}          # nothing was spent to find that out


def test_a_training_proposal_with_one_label_class_is_not_a_training_experiment(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path, train=True)
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker(),
                             trainer=PhraseTrainer())
    context, _ = context_of(activity, work)
    proposal = ct.ExperimentProposal(owner="worker", hypothesis="train it",
                                     training=({"point": "worker.stop", "feedback_ref": "completion_snapshot@v1",
                                                "data_selection_ref": ct.ref_for("change", {"a": 1}),
                                                "recipe_ref": ct.ref_for("change", {})},))
    one_class = make_context("it", goal="g", release_ref=release, objects=activity.objects,
                             feedback={"valid": 30, "labels": {"yes": 30}})
    assert check_label_availability(proposal, context=one_class)[0] == "block"


# ---------------------------------------------------------------------------- RL-04
def test_rl04_a_proposal_that_cites_evidence_it_was_never_given_is_invalid(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker())
    context, _ = context_of(activity, work)
    bound = P.bind_release(activity.releases.active()["release_ref"], profile="task", services=work.services(),
                           authorization="installed")
    result = propose_experiment(context, bound, "worker",
                               researcher=rsi.ScriptedResearcher(cite_missing=True), objects=activity.objects)
    admission = admit(result.proposals[0], context=context, core=core_of(activity, work),
                      objects=activity.objects, checks=tuple(bound.preflight))
    assert admission.status == "invalid" and any("not in its context" in r for r in admission.reasons)
    # and nothing wrote a feedback record out of it
    assert not [e for e in activity.rt.ledger.events() if e.get("type") == "outcome"
                and e.get("source") == "human"]


def test_a_proposal_for_another_owners_target_is_invalid(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker())
    context, _ = context_of(activity, work)
    proposal = ct.ExperimentProposal(owner="worker", hypothesis="change how research retrieves",
                                     changes=({"target": "research.retrieve", "kind": "rule",
                                               "change": {"builtin": "research_retrieve.v1", "max_items": 1}},))
    admission = admit(normalize(proposal, activity.objects), context=context, core=core_of(activity, work),
                      objects=activity.objects)
    assert admission.status == "invalid" and any("research target" in r for r in admission.reasons)


def test_an_unknown_feedback_builder_is_refused(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker())
    context, _ = context_of(activity, work)
    selection = activity.objects.put_json("change", {"seed": 0})
    recipe = activity.objects.put_json("change", {})
    proposal = ct.ExperimentProposal(owner="worker", hypothesis="train on the final score",
                                     training=({"point": "worker.stop", "feedback_ref": "task_reward@v1",
                                                "data_selection_ref": selection, "recipe_ref": recipe},))
    admission = admit(proposal, context=context, core=core_of(activity, work), objects=activity.objects,
                      feedback_builders=("completion_snapshot@v1",))
    assert admission.status == "invalid" and any("unknown feedback builder" in r for r in admission.reasons)


# ---------------------------------------------------------------------------- RL-06
def test_rl06_a_research_policy_may_reorder_the_checks_but_not_remove_the_core_ones(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    objects = activity.objects
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker())
    context, _ = context_of(activity, work)
    # a legal change: fewer auxiliary checks, in another order
    reordered = P.replace_policies(objects, ct.SystemRelease.from_json(objects.get(release)).research_ref,
                                   [P.descriptor(objects, "research.preflight", "workflow",
                                                 {"builtin": "research_preflight.v1",
                                                  "checks": ["duplicate_experiment", "input_coverage"]})])
    checks = P.validate_policy_set(objects.load(reordered, ct.PolicySet), objects).policies
    config = objects.get(checks["research.preflight"].implementation["config_ref"])
    assert config["checks"] == ["duplicate_experiment", "input_coverage"]
    # an illegal one: a check nobody registered
    with pytest.raises(ct.ContractError, match="unknown preflight checks"):
        P.descriptor(objects, "research.preflight", "workflow",
                     {"builtin": "research_preflight.v1", "checks": ["skip_core"]})
    # and the core check runs whatever the auxiliary list says, including when it is empty
    bad = ct.ExperimentProposal(owner="worker", hypothesis="x",
                                changes=({"target": "worker.stop", "kind": "prompt",
                                          "change": {"text": "always yes"}},))
    admission = admit(bad, context=context, core=core_of(activity, work), objects=objects, checks=())
    assert admission.status == "invalid" and any("prompt change" in r or "does not take" in r
                                                for r in admission.reasons)
    assert admission.checks[-1]["check"] == "core"


# ---------------------------------------------------------------------------- RL-08
def test_rl08_an_empty_proposal_list_is_a_structured_outcome_not_a_crash(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker())
    context, _ = context_of(activity, work)
    bound = P.bind_release(activity.releases.active()["release_ref"], profile="task", services=work.services(),
                           authorization="installed")

    class Silent:
        def diagnose(self, context, prompt):
            return []

        def propose(self, context, prompt, requirements, experience=()):
            return ProposalResult([], next_action="none", reason="")

    result = propose_experiment(context, bound, "worker", researcher=Silent(), objects=activity.objects)
    assert result.proposals == [] and result.next_action == "collect" and result.reason
    round_result = bench.improve(cfg, ".", out=lambda _l: None, researcher=Silent())
    outcomes = [r["outcome"] for r in round_result["rounds"]]
    assert outcomes[0] == "no_proposal" and "stopped" in outcomes
    entries = [e for e in experience_mod.read(activity.root) if e.get("kind") == "round"]
    assert entries and entries[-1]["outcome"] == "no_proposal" and entries[-1]["next_action"] == "collect"
    assert not [e for e in experience_mod.read(activity.root) if e.get("kind") == "experiment"]


def test_more_proposals_than_the_round_allows_are_truncated_and_recorded(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker())
    context, _ = context_of(activity, work)
    bound = P.bind_release(activity.releases.active()["release_ref"], profile="task", services=work.services(),
                           authorization="installed")

    class Eager(rsi.ScriptedResearcher):
        def propose(self, context, prompt, requirements, experience=()):
            one = super().propose(context, prompt, requirements, experience)
            second = ct.ExperimentProposal(owner="worker", hypothesis="another idea",
                                           changes=({"target": "worker.system_prompt", "kind": "prompt",
                                                     "change": {"text": "be careful"}},))
            return ProposalResult([*one.proposals, second], next_action="none")

    result = propose_experiment(context, bound, "worker", researcher=Eager(), objects=activity.objects,
                               max_candidates=1)
    assert len(result.proposals) == 1 and len(result.dropped) == 1


# ---------------------------------------------------------------------------- RL-02 / RL-03
def test_rl02_experience_holds_the_improvement_and_the_regression_together(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    record = experience_mod.experiment_record(
        experiment_id="x1", proposal_ref="proposal:sha256:" + "a" * 64, owner="worker",
        hypothesis="the finish check closes the omission",
        changes=({"target": "worker.finish", "kind": "prompt", "change_ref": "change:sha256:" + "b" * 64},),
        requirements=["req:rsi.dev_notify_after_update"],
        receipt=type("R", (), {"verdict": "rejected", "metrics": {"regression_rate": 0.25, "gain": 0.2},
                               "reasons": ("regression rate 0.25 above 0.2",), "uncertainty": {},
                               "ref": lambda self=None: "receipt:sha256:" + "c" * 64})(),
        release_before=release, release_after=release, tags=("worker.finish",),
        targets=("worker.finish",), open_questions=["the notify_once task regressed"])
    experience_mod.append(activity.root, record)
    entries = experience_mod.read(activity.root)
    # the gain and the regression are one record, retrievable by the next round, and the retrieval can only narrow
    found = retrieve(entries, {"targets": ("worker.finish",), "max_items": 10, "sort": "recency"})
    assert found and found[0]["targets"] == ["worker.finish"]
    assert found[0]["metrics"]["gain"] == 0.2 and found[0]["regression_rate"] == 0.25
    assert found[0]["requirements"] == ["req:rsi.dev_notify_after_update"] and not found[0]["installed"]
    assert retrieve(entries, {"statuses": ("regression",), "max_items": 10}) == []    # no entry carries a status
    assert len(retrieve(entries, {"max_items": 1})) == 1


def test_rl03_an_infrastructure_failure_is_not_a_refutation(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker())
    context, _ = context_of(activity, work)
    proposal = normalize(ct.ExperimentProposal(
        owner="worker", hypothesis="the finish check closes the omission",
        changes=({"target": "worker.finish", "kind": "prompt", "change": {"text": P.FINISH_CHECK}},)),
        activity.objects)
    first = check_duplicate_experiment(proposal, history=())
    assert first[0] == "pass"
    from ouroloop.research.experiment import _signature
    again = check_duplicate_experiment(proposal, history=(_signature(proposal),))
    assert again[0] == "block" and "already frozen" in again[1]
    # an experiment recorded as inconclusive does not read as a refuted hypothesis
    experience_mod.append(activity.root, experience_mod.experiment_record(
        experiment_id="x2", proposal_ref=activity.objects.put(proposal), owner="worker",
        hypothesis=proposal.hypothesis, changes=proposal.changes,
        receipt=type("R", (), {"verdict": "inconclusive", "metrics": {}, "reasons": ("provider unavailable",),
                               "uncertainty": {}, "ref": lambda self=None: None})(),
        costs={"model_calls": 12}))
    entry = experience_mod.read(activity.root)[-1]
    assert entry["verdict"] == "inconclusive" and entry["installed"] is False and entry["costs"]["model_calls"]


def test_the_experiment_is_frozen_by_the_framework_not_the_proposal(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    objects = activity.objects
    proposal_ref = objects.put(normalize(ct.ExperimentProposal(
        owner="worker", hypothesis="h",
        changes=({"target": "worker.finish", "kind": "prompt", "change": {"text": "check"}},)), objects))
    experiment = freeze(proposal_ref, objects=objects, parent_release=release, experience_ref="exp-1",
                        core_ref=ct.SystemRelease.from_json(objects.get(release)).core_ref,
                        eval_template_ref=plan_ref, budget_scope="it")
    assert experiment.parent_release == release and experiment.eval_template_ref == plan_ref
    fields = set(ct.ExperimentProposal.__dataclass_fields__)
    assert not fields & {"verdict", "accepted", "results", "gate_plan", "threshold"}


# ---------------------------------------------------------------------------- unreadable replies (M4 §1)
NOTE = {"failure_class": "missed_step", "root_cause": "stopped before the last step", "mechanism": "check every "
        "step before finishing", "component": "skills", "generality": "multi-step requests"}


class SummaryReplies:
    """A research model that answers each note with a readable note and each summary with `reply` — a refusal
    when it is None — through the production completion interface."""
    name = "fake"
    model = "fake"
    supports_history_edits = True

    def __init__(self, reply):
        self.reply, self.asked = reply, []

    def chat(self, system, messages, tools):
        from ouroloop.chat import ChatTurn
        from ouroloop.research.basic import NOTE_FORMAT
        if NOTE_FORMAT in system:
            self.asked.append("note")
            return ChatTurn(json.dumps(NOTE), [], "end")
        self.asked.append("summary")
        return ChatTurn("", [], "refusal") if self.reply is None else ChatTurn(self.reply, [], "end")


def improve_with(fake_upstream, tmp_path, reply, schedule):
    from test_harness_loop import v06_prepared
    from ouroloop.bench import _Completions
    from ouroloop.research.basic import PolicyResearcher
    cfg, activity, _ = v06_prepared(fake_upstream, tmp_path)
    cfg["rsi"]["schedule_ref"] = activity.objects.put_json("change", {"candidates_per_round": 1, **schedule})
    model = SummaryReplies(reply)
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=PolicyResearcher(_Completions(model)))
    return result, model, activity


@pytest.mark.parametrize("reply,why", [
    ("", "was empty"),
    (None, "declined the request"),
    ("I would add a skill that checks the last step.", "not readable JSON"),
    (json.dumps({"reason": "the notes say enough", "requirements": []}), "no proposal and no next_action"),
    (json.dumps({"next_action": "collect", "proposals": [{"hypothesis": "h",
                                                          "changes": [{"target": "worker.harness"}]}]}),
     "malformed"),
    (json.dumps({"proposals": ["add a skill that checks the last step"]}), "malformed"),
    (json.dumps({"proposals": "add a skill that checks the last step", "next_action": "collect"}), "not a list"),
    # a malformed proposal after a well-formed one is not dropped and the first read on
    (json.dumps({"proposals": [{"hypothesis": "h", "changes": [{"target": "worker.system_prompt", "kind": "prompt",
                                                                 "change": {"text": "Do every step."}}]},
                               "and a second idea"]}), "malformed")])
def test_an_unreadable_reply_is_an_invalid_round(fake_upstream, tmp_path, reply, why):
    """docs/modules/04_RESEARCH_LOOP.md §1: an empty, refused, non-JSON or malformed summary reply decides
    nothing. The round is invalid and counts toward patience; nothing R might have meant — a collection here —
    is acted on, and the next round asks again."""
    from ouroloop.benchmark import Results
    result, model, activity = improve_with(fake_upstream, tmp_path, reply, {"worker_rounds": 3, "patience": 2})
    rounds = [r for r in result["rounds"] if r["outcome"] != "stopped"]
    assert [r["outcome"] for r in rounds] == ["invalid", "invalid"], result["rounds"]
    assert all(why in r["reason"] and r["next_action"] == "none" for r in rounds)
    assert model.asked.count("summary") == 2 and result["stop_reason"] == "patience"
    assert not [r for r in Results(activity.root, rsi.DOMAIN).all() if "/collect" in (r.attempt_id or "")]
    assert not [e for e in experience_mod.read(activity.root) if e.get("kind") == "experiment"]


@pytest.mark.parametrize("decision,outcome", [({"next_action": "stop", "reason": "nothing general to fix"},
                                               "no_proposal"),
                                              ({"next_action": "collect", "reason": "one attempt is too few"},
                                               "needs_collection")])
def test_a_decision_r_states_is_still_read_as_one(fake_upstream, tmp_path, decision, outcome):
    result, _, _ = improve_with(fake_upstream, tmp_path, json.dumps(decision), {"worker_rounds": 1, "patience": 1})
    assert result["rounds"][0]["outcome"] == outcome


# ---------------------------------------------------------------------------- what a proposal may carry
class Named:
    """A backend a proposal names: constructing it is what an import in the control plane would do."""
    built: list = []

    def __init__(self, **spec):
        Named.built.append(spec)


def test_a_proposal_cannot_name_a_backend(fake_upstream, tmp_path):
    """A model is what a training entry builds. A proposal that names a backend — imported by name in the control
    plane at the build's preflight — or changes a model with no training entry for that target is refused at
    admission, whether or not K froze a method list; an experiment frozen before that is refused at the build,
    before anything is imported."""
    from dataclasses import replace
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker())
    context, _ = context_of(activity, work)
    core = core_of(activity, work)
    objects = activity.objects
    assert not (core.capabilities or {}).get("methods")          # the shape the review found reachable
    named = ct.ExperimentProposal(owner="worker", hypothesis="a decider of my own", changes=(
        {"target": "worker.stop", "kind": "model",
         "change": {"backend": {"type": "test_research_round:Named", "x": 1}}},))
    bare = ct.ExperimentProposal(owner="worker", hypothesis="a model nobody trained", changes=(
        {"target": "worker.stop", "kind": "model", "change": {"instructions": "stop when done"}},))
    training = {"point": "worker.stop", "feedback_ref": "completion_snapshot@v1",
                "data_selection_ref": ct.ref_for("change", {"a": 1}), "recipe_ref": ct.ref_for("change", {})}
    actor = ct.ExperimentProposal(owner="worker", hypothesis="another actor", training=(training,), changes=(
        {"target": "worker.actor", "kind": "model", "change": {"model_identity_ref": "model:x"}},))
    with_methods = replace(core, capabilities={**(core.capabilities or {}),
                                               "methods": ["harness_edit", "decision_train"]})
    for k in (core, with_methods):
        refused = admit(normalize(named, objects), context=context, core=k, objects=objects)
        assert refused.status == "invalid" and any("does not name a backend" in r for r in refused.reasons)
        untrained = admit(normalize(bare, objects), context=context, core=k, objects=objects)
        assert untrained.status == "invalid" and any("from a training entry" in r for r in untrained.reasons)
    other = admit(normalize(actor, objects), context=context, core=with_methods, objects=objects)
    assert any("worker.actor: a model change comes from a training entry" in r for r in other.reasons)
    experiment = freeze(objects.put(normalize(named, objects)), objects=objects, parent_release=release,
                        experience_ref="", core_ref=work.core_ref, eval_template_ref=plan_ref, budget_scope="s")
    built = P.build_candidate(experiment, objects=objects, parent_release=release, services=work.services())
    assert built.status == "invalid" and any("does not name a backend" in e for e in built.errors)
    assert Named.built == []                                      # nothing was imported
    # the same rule for a model change with no training, frozen before admission refused it
    swap = ct.ExperimentProposal(owner="worker", hypothesis="a model nobody trained", changes=(
        {"target": "worker.stop", "kind": "model", "change": {"instructions": "stop when done"}},))
    frozen_swap = freeze(objects.put(normalize(swap, objects)), objects=objects, parent_release=release,
                         experience_ref="", core_ref=work.core_ref, eval_template_ref=plan_ref, budget_scope="s")
    swapped = P.build_candidate(frozen_swap, objects=objects, parent_release=release, services=work.services())
    assert swapped.status == "invalid" and any("from a training entry" in e for e in swapped.errors)
    # a training entry for a target that is not a trained decision is refused at admission
    odd = ct.ExperimentProposal(owner="worker", hypothesis="train the prompt", training=(
        {**training, "point": "worker.system_prompt"},))
    assert any("not a decision target" in r for r in admit(odd, context=context, core=with_methods,
                                                          objects=objects).reasons)


def test_a_resumed_experiment_that_names_a_backend_is_not_evaluated(fake_upstream, tmp_path):
    """Built before the rule, stopped at the screen: a resume does not build again, so the rule is checked on the
    resume itself and the experiment ends invalid before anything binds the backend it names."""
    from ouroloop.evolve.execution import Execution
    from ouroloop.research.loop import resume_unfinished
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker())
    objects = activity.objects
    named = ct.ExperimentProposal(owner="worker", hypothesis="a decider of my own", changes=(
        {"target": "worker.stop", "kind": "model",
         "change": {"backend": {"type": "test_research_round:Named", "x": 2}}},))
    experiment = freeze(objects.put(normalize(named, objects)), objects=objects, parent_release=release,
                        experience_ref="", core_ref=work.core_ref, eval_template_ref=plan_ref, budget_scope="s")
    execution = Execution(activity.root, experiment.id)
    execution.advance("admitted", experiment=objects.put(experiment))
    execution.advance("building")
    execution.advance("screening")
    Named.built.clear()
    [result] = resume_unfinished(activity.root, objects, work.handlers(), None)
    assert result.outcome == "invalid" and "does not name a backend" in result.reason
    assert execution.state()["status"] == "invalid" and Named.built == []


class LeakyPrompt:
    """Writes notes like NoteResearcher, and proposes the given changes whatever they say."""

    def __init__(self, changes):
        from test_harness_loop import NoteResearcher
        self.inner, self.changes = NoteResearcher(), changes
        self.requests = self.inner.requests

    def note_request(self, *a, **kw):
        return self.inner.note_request(*a, **kw)

    def note(self, system, user):
        return self.inner.note(system, user)

    def summarize(self, context, prompt, notes, coverage, experience=(), *, package=None, allowed=()):
        self.requests.append({"step": "propose"})
        return ProposalResult(proposals=[ct.ExperimentProposal(owner="worker", hypothesis="say who to tell",
                                                               changes=tuple(self.changes), origin="scripted")])

    def revise(self, prompt, patch, check_output):
        self.requests.append({"step": "revise"})
        return patch


LEAKED = "Sam Okafor"            # a record name from the train tasks' initial state: a literal the check knows


def leaked_in_one_task(monkeypatch):
    """V0_4_PLAN §1.5: only a value of one train task is forbidden, and the fixture's contacts are in every train
    task's state; here LEAKED is made one task's value, the shape a real answer has."""
    monkeypatch.setattr(bench.Improvement, "train_state_index", lambda self: ({LEAKED: {"rsi.notify_once"}}, set()))


@pytest.mark.parametrize("changes", [
    [{"target": "worker.system_prompt", "kind": "prompt",
      "change": {"text": f"Rules:\n{LEAKED} must hear about every update."}}],
    [{"target": "worker.finish", "kind": "workflow",
      "change": {"builtin": "finish_check.v1", "instruction": f"Before finishing, message {LEAKED}."}}],
    [{"target": "worker.harness", "kind": "workflow", "change": {"patch": {"write": {
        "manifest.json": json.dumps({"api_version": "harness_package.v1", "tools": [], "hooks": [], "skills": [],
                                     "memory": {"enabled": True}}),
        "memory.md": "Do every step the request asks for."}}}},
     {"target": "worker.system_prompt", "kind": "prompt", "change": {"text": f"Always copy {LEAKED}."}}]],
    ids=["system_prompt", "finish_instruction", "mixed_with_a_clean_package"])
def test_a_prompt_change_gets_the_literal_check(fake_upstream, tmp_path, changes, monkeypatch):
    """A prompt reaches the actor as directly as a package file: a train literal of one task in its text makes the
    round invalid — no revision (a prompt has no draft), nothing frozen, no gate paid — and (v0.4, V0_4_PLAN §1.5)
    the record names the target, the line and the literal, as the check output to R does."""
    from test_harness_loop import v06_prepared
    leaked_in_one_task(monkeypatch)
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    researcher = LeakyPrompt(changes)
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=researcher)
    assert result["rounds"][0]["outcome"] == "invalid", result["rounds"]
    assert result["release"] == w0
    assert not list((activity.root / "rsi" / "objects" / "experiment").glob("*.json"))
    assert not [r for r in researcher.requests if r["step"] == "revise"]
    assert not list(ct.Events(activity.root).of("draft_check"))       # refused before any package draft
    row = [e for e in experience_mod.read(activity.root) if e.get("kind") == "round"][0]
    assert row["outcome"] == "invalid" and "task-specific values" in row["checks"][0]["detail"]
    target = next(c["target"] for c in changes if c["target"] != "worker.harness")
    assert f"{target}:" in row["checks"][0]["detail"] and repr(LEAKED) in row["checks"][0]["detail"]


def test_a_finish_instruction_stored_by_ref_gets_the_literal_check(fake_upstream, tmp_path, monkeypatch):
    from test_harness_loop import v06_prepared
    leaked_in_one_task(monkeypatch)
    cfg, activity, _ = v06_prepared(fake_upstream, tmp_path)
    ref = activity.objects.put_json("change", {"text": f"Before finishing, message {LEAKED}."})
    researcher = LeakyPrompt([{"target": "worker.finish", "kind": "workflow",
                               "change": {"builtin": "finish_check.v1", "instruction_ref": ref}}])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=researcher)
    assert result["rounds"][0]["outcome"] == "invalid", result["rounds"]


def test_a_prompt_change_without_a_literal_is_not_refused(fake_upstream, tmp_path):
    from test_harness_loop import v06_prepared
    cfg, activity, _ = v06_prepared(fake_upstream, tmp_path)
    researcher = LeakyPrompt([{"target": "worker.system_prompt", "kind": "prompt",
                               "change": {"text": "Rules:\nTell the requester about every update."}}])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=researcher)
    assert result["rounds"][0]["outcome"] != "invalid"
    assert list((activity.root / "rsi" / "objects" / "experiment").glob("*.json"))


# ---------------------------------------------------------------------------- v0.3: the coding-session round (M4 §1, §2, §5)
from test_research_session import MEMORY, SKILL, memory_candidate, prepared_v3  # noqa: E402

from ouroloop.evolve import experience as xp  # noqa: E402
from ouroloop.research.experiment import declaration_reasons  # noqa: E402


# the v0.3 session table: a K frozen before v0.4's read floors (V0_4_PLAN §2: such a K keeps v0.3's admission)
V03_SESSION = {"max_turns": 40, "max_edits": 24, "max_trajectory_bytes": 65536, "max_trajectory_bytes_total": 524288}


def skill_candidate(inputs, *, name="s1", text=f"Before you finish, {rsi.FINISH_CHECK_MARK}.\n",
                    mechanism="a skill read before finishing"):
    from ouroloop.agent import harness_package as hp
    manifest = {**hp.empty_manifest(), "skills": [{"name": SKILL, "path": f"skills/{SKILL}.md",
                                                  "description": "before finishing"}]}
    return {"dir": name, "files": {"manifest.json": json.dumps(manifest), f"skills/{SKILL}.md": text},
            "declaration": {"mechanism": mechanism, "site": f"skills/{SKILL}.md", "carrier": "skill"}}


def ledger_of(activity):
    return {r["candidate"].rpartition("/")[2]: r for r in xp.ledger_rows(xp.read(activity.root))}


def test_a_round_declares_up_to_k_candidates_and_drops_a_repeated_mechanism_site(fake_upstream, tmp_path,
                                                                                 monkeypatch):
    """RL6-28: of three declared candidates, the one whose declared carrier (hook) does not match its diff (memory
    only) is invalid as `declaration_mismatch`, the one repeating another's normalised (mechanism, site) is dropped,
    the remaining one goes through the draft check, the screen and the gate; the round is not invalid."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    step = rsi.SessionScript(lambda inputs: [
        memory_candidate(inputs, name="m1"),
        memory_candidate(inputs, name="m2", text=MEMORY + "Twice.\n", mechanism="Check  every step BEFORE finishing"),
        {**memory_candidate(inputs, name="h1"), "declaration": {"mechanism": "a check before finishing",
                                                                 "site": "on_finish", "carrier": "hook"}}])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert [r["outcome"] for r in result["rounds"]][0] == "installed"
    rows = ledger_of(activity)
    assert rows["m1"]["outcome"] == "installed"
    assert rows["m2"]["outcome"] == "dropped" and "same (mechanism, site)" in rows["m2"]["reason"]
    assert rows["h1"]["outcome"] == "declaration_mismatch" and "carrier hook" in rows["h1"]["reason"]
    assert "drafts" not in rows["m2"] and "drafts" not in rows["h1"]          # never checked, never screened
    assert len(list(ct.Events(activity.root).of("train_screen"))) == 1
    assert len(list(ct.Events(activity.root).of("research_session"))) == 1     # one session for the round


def test_a_text_only_candidate_set_is_checked_like_any_other(fake_upstream, tmp_path, monkeypatch):
    """RL6-30: a round whose candidates are all text (memory, a skill) goes through the draft check, the screen and
    the gate as any other; the carriers are in the ledger and no structure error asks for code."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    step = rsi.SessionScript(lambda inputs: [memory_candidate(inputs), skill_candidate(inputs)])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert [r["outcome"] for r in result["rounds"]][0] == "installed"
    rows = ledger_of(activity)
    assert {rows["m1"]["carrier"], rows["s1"]["carrier"]} == {"memory", "skill"}
    assert all(r["drafts"][0]["ok"] and r["admission"]["status"] == "admitted" for r in rows.values())
    # both cleared the floor (+2 each); the one that lost the pick is `not_selected`, not screened out (M4 §2 taxonomy)
    assert sorted(r["screen"]["outcome"] for r in rows.values()) == ["not_selected", "selected"]
    assert sorted(r["outcome"] for r in rows.values()) == ["installed", "not_selected"]
    checks = list(ct.Events(activity.root).of("draft_check"))
    assert len(checks) == 2 and all(c["ok"] and c["session"] for c in checks)


def test_the_screen_reruns_the_notes_tasks_and_a_fixed_passing_sample_from_train_only(fake_upstream, tmp_path,
                                                                                      monkeypatch):
    """RL6-31 / M4 §2: a candidate's screen runs the failed train tasks its notes came from plus the round's fixed
    passing sample — the same sample for every candidate — one attempt each, under the `train_screen` arm on the
    train split; no dev task is touched before the gate, and the screen's attempts never enter a dev pool."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    step = rsi.SessionScript(lambda inputs: [memory_candidate(inputs), skill_candidate(inputs)])
    bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    screens = list(ct.Events(activity.root).of("train_screen"))
    assert len(screens) == 2
    failing = {"rsi.notify_after_update", "rsi.notify_after_update_variant"}
    for s in screens:
        assert set(s["tasks"]) <= set(rsi.TRAIN) and failing <= set(s["tasks"])
        assert set(s["tasks"]) - failing == {"rsi.update_stage", "rsi.policy_hold", "rsi.notify_once"}   # the sample
    assert screens[0]["tasks"] == screens[1]["tasks"]
    rows = [r for r in activity.results.all() if "/train_screen" in r.attempt_id]
    assert rows and all(r.split == "train" for r in rows) and len(rows) == 2 * len(screens[0]["tasks"])
    dev_before_gate = [r for r in activity.results.all() if r.split == "dev"]
    assert all("/subject" in r.attempt_id or "/control" in r.attempt_id or "/confirm" in r.attempt_id
               for r in dev_before_gate)
    # the screen arm is never a reading of the dev plan: the control pool does not know it
    work = bench.improvement(activity, template_ref=cfg["rsi"]["task_plan_ref"], provider=rsi.BusinessWorker())
    assert not [r for r in work.control_pool(w0) if "/train_screen" in r.attempt_id]


def test_only_the_best_screened_candidate_pays_the_gate(fake_upstream, tmp_path, monkeypatch):
    """RL6-31: two admitted candidates, one whose screen flips its failing tasks to pass (net +2) and one that
    changes nothing on them (0): only the first is frozen into an experiment and read on dev; the second ends
    `screened_out` with its screen result in the ledger, and there is one receipt."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    step = rsi.SessionScript(lambda inputs: [
        memory_candidate(inputs, name="useless", text="Be concise.\n", mechanism="a shorter final message"),
        memory_candidate(inputs, name="good")])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert [r["outcome"] for r in result["rounds"]][0] == "installed"
    rows = ledger_of(activity)
    assert rows["useless"]["screen"]["net_flips"] == 0 and rows["useless"]["outcome"] == "screened_out"
    assert rows["good"]["screen"]["net_flips"] == 2 and rows["good"]["outcome"] == "installed"
    assert "gate" not in rows["useless"] and rows["good"]["gate"]["verdict"] == "accepted"
    assert len([e for e in xp.read(activity.root) if e.get("kind") == "experiment"]) == 1
    assert len(list(ct.Objects(activity.root).dir.joinpath("receipt").glob("*.json"))) == 1


def test_a_round_with_nothing_worth_the_gate_is_screened_out(fake_upstream, tmp_path, monkeypatch):
    """RL6-32: every candidate's net flips under `screen.min_net_flips`: the round ends `screened_out` — no gate,
    no experiment, no install — it counts toward patience, and each candidate's screen is in the ledger."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch, rounds=2)
    mechanisms = iter(["a shorter final message", "a polite greeting first"])   # two mechanisms: no repeat refused
    step = rsi.SessionScript(lambda inputs: [
        memory_candidate(inputs, name="useless", text="Be concise.\n", mechanism=next(mechanisms))])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    outcomes = [r["outcome"] for r in result["rounds"]]
    assert outcomes == ["screened_out", "screened_out", "stopped"] and result["stop_reason"] in ("patience", "rounds_done")
    assert result["release"] == w0
    # a screened_out round counts toward patience as any round without an install does
    from ouroloop.research.loop import counters_of
    assert counters_of([r for r in result["rounds"] if r["outcome"] != "stopped"]) == (2, 0)
    assert not [e for e in xp.read(activity.root) if e.get("kind") == "experiment"]
    rows = xp.ledger_rows(xp.read(activity.root))
    assert len(rows) == 2 and all(r["outcome"] == "screened_out" and r["screen"]["net_flips"] == 0 for r in rows)
    assert all(r["screen"]["thin_screen"] for r in rows)                   # two note-derived tasks: fewer than 3


def test_screen_readings_of_the_installed_candidate_are_reused_as_its_train_diagnosis(fake_upstream, tmp_path,
                                                                                       monkeypatch):
    """RL6-33: after the screened candidate is installed, the next round's rerun of the previous failures finds its
    screen readings under the installed version's execution identity and runs none of them again; a candidate that
    was not installed leaves readings no version's measurement reads."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch, rounds=2)
    step = rsi.SessionScript(lambda inputs: [
        memory_candidate(inputs, name="useless", text="Be concise.\n", mechanism="a shorter final message"),
        memory_candidate(inputs, name="good")])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    outcomes = [r["outcome"] for r in result["rounds"]]
    assert outcomes[0] == "installed", outcomes
    failing = {"rsi.notify_after_update", "rsi.notify_after_update_variant"}
    measures = [r for r in activity.results.all() if "/measure" in r.attempt_id and r.task_id in failing]
    assert len(measures) == 2 and all(r.system_release_ref == w0 for r in measures)   # W0's measurement only
    diagnosis = [e for e in xp.read(activity.root) if e.get("kind") == "diagnosis"]
    assert diagnosis[1]["measured"] == 2 and diagnosis[1]["passed"] == 2 and diagnosis[1]["failures"] == 0
    installed = result["release"]
    work = bench.improvement(activity, template_ref=cfg["rsi"]["task_plan_ref"], provider=rsi.BusinessWorker())
    reused = work.screen_readings(installed, failing)
    assert {r.task_id for r in reused} == failing and all("/train_screen" in r.attempt_id for r in reused)
    assert work.screen_readings(w0, failing) == []            # the uninstalled candidate's readings read as no one's


def test_the_ledger_reads_fixed_regressed_and_still_failing_from_the_receipt(tmp_path):
    """RL6-35: the three per-task classes come off the receipt's own results object — the candidate (subject and
    confirmation readings together) against the pooled control, with the plan's regression_drop — and nothing is
    recomputed or labelled: one task fixed, one regressed, one still failing, with the receipt's refs."""
    from test_task_gate import confirmed
    from ouroloop.evolve.task_gate import receipt_differences
    control = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": True}
    first = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": False}
    receipt, _arms, objects = confirmed(tmp_path, first, first, control)
    assert receipt_differences(receipt, objects) == {"fixed": ["t2"], "regressed": ["t5"], "still_failing": ["t3", "t4"]}
    row = xp.ledger_row("gated", "c", verdict=receipt.verdict, receipt=receipt.ref(), reading_kind=receipt.reading_kind,
                        gain=receipt.metrics["gain"], control_readings=receipt.metrics["control_readings"],
                        **receipt_differences(receipt, objects))
    folded = xp.ledger_rows([xp.ledger_row("declared", "c", declaration={"mechanism": "m", "site": "s",
                                                                          "carrier": "memory"}, status="declared"),
                             row])[0]
    assert folded["gate"]["fixed"] == ["t2"] and folded["gate"]["regressed"] == ["t5"]
    assert receipt.verdict == "rejected" and receipt.reading_kind == "full"        # +1 −1: gain 0, no confirmation
    assert xp.ledger_projection([folded])[0]["gate"] == {"verdict": receipt.verdict, "reading_kind": "full",
                                                        "gain": receipt.metrics["gain"],
                                                        "control_readings": receipt.metrics["control_readings"],
                                                        "cost_per_attempt": None,
                                                        "fixed": 1, "regressed": 1, "still_failing": 2}


def test_a_failed_confirmation_is_a_confirmed_receipt_rejected_in_the_ledger(tmp_path):
    """RL6-38: a candidate whose confirmation reading did not clear min_gain leaves a `confirmed` receipt with
    verdict rejected and its first reading kept; the ledger's gate stage records that verdict and that kind, with
    the receipt ref, and the candidate's outcome is rejected — nothing is installed."""
    from test_task_gate import FIVE, confirmed
    control = {"t1": True, "t2": False, "t3": False, "t4": False, "t5": False}
    first = {"t1": True, "t2": True, "t3": True, "t4": False, "t5": False}
    receipt, arms, objects = confirmed(tmp_path, first, dict.fromkeys(FIVE, False), control)
    assert receipt.verdict == "rejected" and receipt.kind == "confirmed" and receipt.metrics["first_reading"]["gain"] == 0.4
    from ouroloop.evolve.task_gate import receipt_differences
    row = xp.ledger_row("gated", "c", verdict=receipt.verdict, receipt=receipt.ref(), reading_kind=receipt.reading_kind,
                        installed=False, **receipt_differences(receipt, objects))
    folded = xp.ledger_rows([xp.ledger_row("declared", "c", declaration={}, status="declared"), row])[0]
    assert folded["outcome"] == "rejected" and folded["gate"]["reading_kind"] == "confirmed"
    assert folded["gate"]["receipt"] == receipt.ref()
    # and a rejected row is what makes the same (mechanism, site) a repeat for admission
    assert folded["outcome"] in xp.REPEAT_OUTCOMES


def test_the_package_and_the_session_cannot_write_the_ledger(fake_upstream, tmp_path, monkeypatch):
    """RL6-40: every ledger row of a round carries the fixed code's scope, round, release and R ref; a session that
    writes `ledger.json`, `inputs/ledger.json` or `package/...` is refused by the worker's tree and the ledger has
    only the engine's rows for the candidates the round declared."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)

    from ouroloop.chat import ToolCall

    class Writes(rsi.SessionScript):
        def _enqueue(self, candidates):
            super()._enqueue(candidates)
            self._queue.insert(0, [ToolCall("x1", "write_file", {"path": "ledger.json", "text": "{}"}),
                                   ToolCall("x2", "write_file", {"path": "inputs/ledger.json", "text": "[]"}),
                                   ToolCall("x3", "write_file", {"path": "package/memory.md", "text": "mine"}),
                                   ToolCall("x4", "delete_file", {"path": "inputs/notes.json"})])
    step = Writes(lambda inputs: [memory_candidate(inputs)])
    bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    session = list(ct.Events(activity.root).of("research_session"))[0]
    refused = [c for c in session["calls"] if c.get("path") in ("ledger.json", "inputs/ledger.json", "package/memory.md",
                                                               "inputs/notes.json")]
    assert len(refused) == 4 and not any(c["ok"] for c in refused)
    rows = [e for e in xp.read(activity.root) if e.get("kind") == "ledger"]
    assert rows and all(e["scope"] == "cyc" and e["round"] == 0 and e["release"] == w0 and e["research_ref"]
                        for e in rows)
    assert {e["candidate"] for e in rows} == {"cyc/d0/m1"}          # <scope>/d<diagnosis ordinal>/<dir>


def test_admission_refuses_a_rejected_mechanism_at_the_same_site_without_differs_from(fake_upstream, tmp_path,
                                                                                      monkeypatch):
    """RL6-34 / M4 §5: a candidate on the same normalised (mechanism, site) as a candidate the ledger shows
    rejected or screened out is refused at admission (`repeated_mechanism`) unless its declaration's differs_from
    cites that row with a sentence that is not the old mechanism; the difference is written into the ledger and
    judged by the screen and the gate, not by the admission."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch, rounds=3)
    seen = []

    def plan(inputs):
        seen.append(inputs)
        earlier = [row for row in inputs["ledger"] if row["outcome"] == "screened_out"]
        base = memory_candidate(inputs, name="again", text=MEMORY, mechanism="a shorter final message")
        if len(seen) == 1:
            return [memory_candidate(inputs, name="first", text="Be concise.\n", mechanism="a shorter final message")]
        if len(seen) == 2:
            return [base]                                              # the same (mechanism, site), no differs_from
        return [{**base, "declaration": {**base["declaration"], "differs_from": [
            {"ref": earlier[0]["declaration_ref"], "how": "this one names the tool result to check first"}]}}]
    step = rsi.SessionScript(plan)
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    outcomes = [r["outcome"] for r in result["rounds"] if r["outcome"] != "stopped"]
    assert outcomes == ["screened_out", "invalid", "installed"], result["rounds"]
    rows = xp.ledger_rows(xp.read(activity.root))
    assert [r["outcome"] for r in rows] == ["screened_out", "invalid", "installed"]
    refused = rows[1]["admission"]
    assert refused["status"] == "invalid" and any("repeated_mechanism" in x and "differs_from" in x for x in refused["reasons"])
    assert rows[2]["declaration"]["differs_from"][0]["ref"] == rows[0]["declaration_ref"]
    assert rows[2]["admission"]["status"] == "admitted"
    # R read the ledger's projection in its inputs: the screened-out row with its declaration ref, no dev task id
    assert seen[1]["ledger"][0]["outcome"] == "screened_out" and seen[1]["ledger"][0]["declaration_ref"]
    assert "rsi.dev" not in json.dumps(seen[2]["ledger"])


def test_a_declaration_naming_a_dev_task_is_refused_at_admission(fake_upstream, tmp_path):
    """CT7-08 / M0 §3: `predicted_affected` is train lineage — a dev or test task id in it is refused at admission;
    train ids pass; and two proposals with the same change under different declarations are one candidate (the
    declaration ref is not in the signature the duplicate check compares)."""
    from ouroloop.research.experiment import _signature
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker())
    context, _ = context_of(activity, work)
    objects = activity.objects
    declare = lambda tasks: {"mechanism": "m", "mechanism_normalized": "m", "site": "memory.md", "carrier": "memory",  # noqa: E731
                             "predicted_affected": tasks, "trigger_condition": "always", "notes": [], "differs_from": []}
    proposal = normalize(ct.ExperimentProposal(
        owner="worker", hypothesis="h", changes=({"target": "worker.finish", "kind": "prompt", "change": {"text": "c"}},),
        declaration_ref=objects.put_json("declaration", declare([rsi.DEV[0]]))), objects)
    refused = admit(proposal, context=context, core=core_of(activity, work), objects=objects,
                    declaration=declare([rsi.DEV[0], rsi.TRAIN[0]]), train_tasks=rsi.TRAIN)
    assert refused.status == "invalid" and any("outside the train split" in r and rsi.DEV[0] in r for r in refused.reasons)
    passed = admit(proposal, context=context, core=core_of(activity, work), objects=objects,
                   declaration=declare([rsi.TRAIN[0]]), train_tasks=rsi.TRAIN)
    assert passed.ok
    other = normalize(ct.ExperimentProposal(
        owner="worker", hypothesis="h", changes=({"target": "worker.finish", "kind": "prompt", "change": {"text": "c"}},),
        declaration_ref=objects.put_json("declaration", declare([rsi.TRAIN[1]]))), objects)
    assert _signature(proposal) == _signature(other) and proposal.declaration_ref != other.declaration_ref
    assert declaration_reasons(None) == [] and declaration_reasons(declare([rsi.TRAIN[0]]), ledger=()) == []


def test_a_v1_package_round_still_runs_with_the_default_agent(fake_upstream, tmp_path, monkeypatch):
    """M0 §2 (v0.3): the v3 core's W0 mounts a v1 package, and the session round runs its attempts under the default
    agent in the host (no agent entry, no code); the candidate the session writes is a v1 manifest too, and the
    installed release still binds under the v1 built-in. The v1 preset keeps the summary-call path untouched."""
    from ouroloop.agent import harness_package as hp
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    step = rsi.SessionScript(lambda inputs: [memory_candidate(inputs)])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert [r["outcome"] for r in result["rounds"]][0] == "installed"
    bound = P.bind_release(result["release"], profile="task", services=P.Services(activity.objects), authorization="x")
    assert bound.package.api_version == hp.API_VERSIONS[0] and not bound.package.runs_code
    agents = [json.loads(line) for p in (tmp_path / "ws" / "transcripts").glob("*.jsonl")
              for line in p.read_text().splitlines() if '"kind": "agent"' in line]
    from ouroloop.agent.default_agent import DEFAULT_ENTRY
    assert agents and {a["placement"] for a in agents} == {"host"} and {a["entry"] for a in agents} == {DEFAULT_ENTRY}
    # the v1 preset: no session inputs, the summary call as before
    from test_harness_loop import NoteResearcher, v06_prepared
    cfg1, activity1, _ = v06_prepared(fake_upstream, tmp_path / "v1")
    researcher = NoteResearcher(broken_first_draft=False)
    bench.improve(cfg1, ".", out=lambda _line: None, researcher=researcher)
    assert any(r["step"] == "propose" for r in researcher.requests)
    assert not list(ct.Events(activity1.root).of("research_session"))


def test_it6_31_the_engine_writes_the_ledger_from_the_declaration_the_screen_and_the_receipt_and_refuses_a_repeated_mechanism_and_site(
        fake_upstream, tmp_path, monkeypatch):
    """IT6-31: one activity, two rounds: round 0's candidate is screened out and recorded with its declaration and
    screen; round 1 repeats its (mechanism, site) without differs_from and is refused at admission — both from the
    engine's own rows; R read the ledger projection first, and no row of the ledger was written by R or the package."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch, rounds=2)
    inputs_seen = []

    def plan(inputs):
        inputs_seen.append(inputs)
        return [memory_candidate(inputs, name="c", text="Be concise.\n", mechanism="a shorter final message")]
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(plan))
    assert [r["outcome"] for r in result["rounds"] if r["outcome"] != "stopped"] == ["screened_out", "invalid"]
    rows = xp.ledger_rows(xp.read(activity.root))
    assert [r["outcome"] for r in rows] == ["screened_out", "invalid"]
    assert rows[0]["declaration"]["mechanism_normalized"] == "a shorter final message"
    assert rows[0]["screen"]["net_flips"] == 0 and rows[0]["screen"]["tasks"]
    assert any("repeated_mechanism" in x for x in rows[1]["admission"]["reasons"])
    assert inputs_seen[0]["ledger"] == [] and inputs_seen[1]["ledger"][0]["outcome"] == "screened_out"
    assert all(e.get("kind") != "ledger" or e.get("research_ref") for e in xp.read(activity.root))


def test_it6_39_r_edits_the_package_in_one_bounded_worker_session_per_round_and_the_draft_check_is_unchanged(
        fake_upstream, tmp_path, monkeypatch):
    """IT6-39: one worker session a round; a candidate hook whose first draft fails the isolated check comes back
    into the same session with the check output and is revised there (turns and edits still counted against the
    round's caps); the revised draft passes the unchanged chain and the hook runs under the gate's candidate arm."""
    from ouroloop.agent import harness_package as hp
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    hook = ("def run(ctx, event):\n    final = str(event.get('final_message', ''))\n"
            "    if '#ops' in str(ctx.view.get('task', '')) and 'Message posted' not in final:\n"
            "        return {'action': 'block', 'message': 'Before finishing, " + rsi.FINISH_CHECK_MARK + ".'}\n"
            "    return {'action': 'continue'}\n")
    manifest = {**hp.empty_manifest(),
                "hooks": [{"name": "closing", "site": "on_finish", "entrypoint": "middlewares/closing.py:run"}]}

    def plan(inputs):
        return [{"dir": "h", "files": {"manifest.json": json.dumps(manifest),
                                       "middlewares/closing.py": "import no_such_module_for_this_test\n" + hook},
                 "declaration": {"mechanism": "a check before finishing", "site": "on_finish", "carrier": "hook"}}]

    def repair(message, inputs):
        assert "candidates/h failed its draft check" in message and "no_such_module_for_this_test" in message
        return [{"dir": "h", "files": {"middlewares/closing.py": hook},
                 "declaration": {"mechanism": "a check before finishing", "site": "on_finish", "carrier": "hook"}}]
    step = rsi.SessionScript(plan, repair=repair)
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert [r["outcome"] for r in result["rounds"]][0] == "installed", result["rounds"]
    checks = list(ct.Events(activity.root).of("draft_check"))
    assert [c["ok"] for c in checks] == [False, True] and [c["revision"] for c in checks] == [0, 1]
    sessions = list(ct.Events(activity.root).of("research_session"))
    assert len(sessions) == 1 and sessions[0]["finishes"] == 2 and sessions[0]["edits"] == 3
    rows = ledger_of(activity)
    assert rows["h"]["drafts"] == [{"revision": 0, "package_ref": None, "ok": False},
                                   {"revision": 1, "package_ref": rows["h"]["package_ref"], "ok": True}]
    hooks = [line for p in (tmp_path / "ws" / "transcripts").glob("*.jsonl") for line in p.read_text().splitlines()
             if '"kind": "hook"' in line]
    assert hooks


def test_it6_39_r_reads_raw_train_trajectories_through_a_byte_capped_tool_and_a_dev_type_histogram_without_task_facts(
        fake_upstream, tmp_path, monkeypatch):
    """IT6-39 / RL6-36 / RL6-37: the session's inputs list the failed train attempts it may read; `read_trajectory`
    hands back the host-written record (gateway calls, broker events) within the per-call cap, refuses a dev attempt
    and the cumulative cap; the dev histogram carries assertion types and counts only — no params, no task ids — and
    comes from the installed W's pooled dev readings; the ledger projection shows dev results as counts."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    bench.baseline(cfg, ".", out=lambda _line: None)                       # W0's dev readings: the histogram's pool
    seen = {}

    class Reader(rsi.SessionScript):
        def session_turn(self, system, messages, tools):
            if len(messages) == 1:
                inputs = json.loads(messages[0]["content"])
                seen["inputs"] = inputs
                ref = inputs["trajectories"][0]["attempt_ref"]
                dev = [r.attempt_id for r in activity.results.all() if r.split == "dev"][0]
                self._queue = [[ToolCall("r1", "read_trajectory", {"attempt_ref": ref, "limit": 100000}),
                                ToolCall("r2", "read_trajectory", {"attempt_ref": dev}),
                                ToolCall("r3", "read_trajectory", {"attempt_ref": ref, "offset": 0, "limit": 600000})]]
                self.inputs = inputs
                self._enqueue(self.plan(inputs))
                return ChatTurn("", self._queue.pop(0), "tool_use")
            if messages[-1].get("role") == "tool" and messages[-1]["results"][0]["name"] == "read_trajectory":
                seen["results"] = messages[-1]["results"]
            return super().session_turn(system, messages, tools)
    from ouroloop.chat import ChatTurn, ToolCall
    step = Reader(lambda inputs: [memory_candidate(inputs)])
    bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    inputs = seen["inputs"]
    failing = [t for t in inputs["trajectories"] if t["outcome"] == "failing"]
    assert {t["task_id"] for t in failing} == {"rsi.notify_after_update", "rsi.notify_after_update_variant"}
    # v0.4 (V0_4_PLAN §1.3): the passing train attempts are listed too, each marked, after the failures
    passing = [t for t in inputs["trajectories"] if t["outcome"] == "passing"]
    assert passing and {t["task_id"] for t in passing} <= set(rsi.TRAIN) - {t["task_id"] for t in failing}
    assert inputs["trajectories"] == failing + passing
    histogram = inputs["dev_failure_histogram"]
    assert set(histogram) == {"split", "types", "readings", "tasks", "attempts"} and histogram["split"] == "dev"
    assert histogram["readings"] == 1 and histogram["tasks"] == len(rsi.DEV) and sum(histogram["types"].values()) >= 1
    assert "rsi.dev" not in json.dumps(histogram) and "params" not in json.dumps(histogram)
    r1, r2, r3 = seen["results"]
    assert not r1["is_error"] and "gateway_call" in r1["output"] and "broker_event" in r1["output"]
    assert len(r1["output"].encode()) <= 65536 + 60
    assert r2["is_error"] and r2["output"].startswith("AccessDenied")
    assert not r3["is_error"] and len(r3["output"].encode()) <= 65536 + 60      # the per-call cap, not the asked 600000
    session = list(ct.Events(activity.root).of("research_session"))[0]
    assert session["trajectory_reads"] == 3 and 0 < session["trajectory_bytes"] <= 2 * 65536
    assert [c.get("ok") for c in session["calls"] if c["tool"] == "read_trajectory"] == [True, False, True]
    assert "rsi.dev" not in json.dumps(inputs["ledger"]) and "rsi.final" not in json.dumps(inputs)


# ---------------------------------------------------------------------------- review of 2026-10-05: round, screen, ledger
def test_a_write_the_tree_refuses_is_a_tool_error_the_round_survives(fake_upstream, tmp_path, monkeypatch):
    """M4 §1 (2026-10-05): a session write under a file (`manifest.json/x.md`) is the tool's error handed to R; the
    round goes on to its recorded outcome instead of raising out of bench.improve."""
    from ouroloop.chat import ToolCall
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)

    class UnderAFile(rsi.SessionScript):
        def _enqueue(self, candidates):
            super()._enqueue(candidates)
            self._queue.insert(0, [ToolCall("u", "write_file", {"path": "candidates/m1/manifest.json/x.md", "text": "x"})])
    step = UnderAFile(lambda inputs: [memory_candidate(inputs)])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert [r["outcome"] for r in result["rounds"]][0] == "installed", result["rounds"]
    session = list(ct.Events(activity.root).of("research_session"))[0]
    refused = [c for c in session["calls"] if c.get("path") == "candidates/m1/manifest.json/x.md"]
    assert len(refused) == 1 and not refused[0]["ok"]


def test_a_revision_may_change_files_but_not_the_declaration(fake_upstream, tmp_path, monkeypatch):
    """M4 §1 (2026-10-05): the declaration a candidate carries is the one its first finish gave; a revision whose
    finish redeclares the same dir under another (mechanism, site, carrier) makes the candidate invalid with the
    reason, and the ledger's declared row, declaration ref and admission all read the original declaration."""
    from ouroloop.agent import harness_package as hp
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    manifest = {**hp.empty_manifest(),
                "hooks": [{"name": "closing", "site": "on_finish", "entrypoint": "middlewares/closing.py:run"}]}
    hook = "def run(ctx, event):\n    return {'action': 'continue'}\n"
    first = {"mechanism": "a check before finishing", "site": "on_finish", "carrier": "hook"}

    def plan(inputs):
        return [{"dir": "h", "files": {"manifest.json": json.dumps(manifest),
                                       "middlewares/closing.py": "import no_such_module_for_this_test\n" + hook},
                 "declaration": first}]

    def repair(message, inputs):
        return [{"dir": "h", "files": {"middlewares/closing.py": hook},
                 "declaration": {"mechanism": "a different check on finish", "site": "on_finish", "carrier": "hook"}}]
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(plan, repair=repair))
    assert [r["outcome"] for r in result["rounds"]][0] == "invalid"
    rows = ledger_of(activity)
    assert rows["h"]["outcome"] == "invalid" and "the revision redeclared" in rows["h"]["reason"]
    assert rows["h"]["declaration"]["mechanism"] == first["mechanism"]
    assert activity.objects.get(rows["h"]["declaration_ref"])["mechanism"] == first["mechanism"]
    assert "admission" not in rows["h"] and "screen" not in rows["h"]
    checks = list(ct.Events(activity.root).of("draft_check"))
    assert [c["ok"] for c in checks] == [False]                        # the redeclared revision was never checked


def test_a_gate_finished_on_resume_writes_its_gated_ledger_row(fake_upstream, tmp_path, monkeypatch):
    """M4 §5 (2026-10-05): a round whose gate is interrupted leaves the experiment open; the restart finishes it
    through resume_unfinished, which writes the candidate's `gated` row through the same helper the round uses,
    so the ledger carries the verdict of a gate that was paid for across the interruption."""
    from ouroloop.providers import ProviderError
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    step = rsi.SessionScript(lambda inputs: [memory_candidate(inputs)])
    real = bench.Improvement._evaluate
    tripped = []

    def once(self, candidate_ref, budget, confirming=None):
        if not tripped:
            tripped.append(candidate_ref)
            raise ProviderError("the gate's model went away", retryable=False)
        return real(self, candidate_ref, budget, confirming=confirming)
    with monkeypatch.context() as m:
        m.setattr(bench.Improvement, "_evaluate", once)
        first = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert first["stop_reason"] == "repair_runtime" and first["rounds"][0]["outcome"] == "infrastructure_failed"
    rows = ledger_of(activity)
    assert rows["m1"]["outcome"] == "pending" and "gate" not in rows["m1"]
    assert rows["m1"]["screen"]["outcome"] == "selected" and rows["m1"]["experiment_id"] and rows["m1"]["proposal_ref"]
    second = bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(lambda inputs: []))
    assert second["rounds"][0]["outcome"] == "installed" and second["rounds"][0]["reason"].startswith("resumed")
    rows = ledger_of(activity)
    assert rows["m1"]["outcome"] == "installed" and rows["m1"]["gate"]["verdict"] == "accepted"
    assert rows["m1"]["gate"]["fixed"] == [rsi.DEV[0]] and rows["m1"]["gate"]["receipt"]
    # the resumed experiment was not proposed again: one candidate on the ledger, and the round the schedule still
    # owed after the resume found no train task failing under the installed W, so it asked for evidence without
    # opening a session (M4 §2, 2026-10-05; the stop that followed is that round's)
    assert len(rows) == 1 and [r["outcome"] for r in second["rounds"][1:]] == ["needs_collection", "stopped"]


def test_paid_screens_are_on_the_ledger_when_a_later_screen_ends_the_round(fake_upstream, tmp_path, monkeypatch):
    """M4 §5 (2026-10-05): the first candidate's screen was paid before the second's raised BudgetExhausted; the
    round ends budget_exhausted and the first screen is still on the ledger (outcome `interrupted`, folded pending)."""
    from ouroloop.evolve.execution import BudgetExhausted
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    real = bench.Improvement.train_screen
    calls = []

    def second_dies(self, candidate_ref, tasks):
        calls.append(candidate_ref)
        if len(calls) == 2:
            raise BudgetExhausted("the second screen's reservation was refused")
        return real(self, candidate_ref, tasks)
    monkeypatch.setattr(bench.Improvement, "train_screen", second_dies)
    step = rsi.SessionScript(lambda inputs: [memory_candidate(inputs, name="a"),
                                             memory_candidate(inputs, name="b", text=MEMORY + "Twice.\n",
                                                              mechanism="check every step twice")])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert result["rounds"][0]["outcome"] == "budget_exhausted" and result["stop_reason"] == "budget_exhausted"
    rows = ledger_of(activity)
    assert rows["a"]["screen"]["outcome"] == "interrupted" and rows["a"]["screen"]["net_flips"] == 2
    assert rows["a"]["outcome"] == "pending" and "screen" not in rows["b"]
    assert rows["b"]["proposal_ref"] and rows["b"]["proposal_ref"] != rows["a"]["proposal_ref"]   # from its admitted row


def test_the_screen_builds_on_the_pointer_not_on_the_previous_gates_parent(fake_upstream, tmp_path, monkeypatch):
    """M4 §2 (2026-10-05, HIGH): round 0 installs W1; round 1's screen permit and build name W1 as parent (the
    pointer), not the W0 the previous experiment was frozen on; and the screened W of an installed round is the W
    the gate read. The actor never sends a notify-only request's message, so a train task still fails under W1 and
    round 1 has a failing set to screen (M4 §2, 2026-10-05: no failing task, no screen)."""
    import sys
    from ouroloop.chat import ChatTurn

    class Stubborn(rsi.BusinessWorker):
        def chat(self, system, messages, specs):
            want = rsi._needs(next((m["content"] for m in messages if m.get("role") == "user"), ""))
            if want["notify"] and not want["update"] and not want["hold"]:
                return ChatTurn("Done.", [], "end", usage={"input_tokens": 100, "output_tokens": 20})
            return super().chat(system, messages, specs)
    monkeypatch.setitem(sys.modules, "stubborn_actor", type("M", (), {"Stubborn": Stubborn}))
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch, rounds=2,
                                    llm={"type": "stubborn_actor:Stubborn"})
    texts = iter([MEMORY, MEMORY + "Twice.\n"])
    mechanisms = iter(["check every step before finishing", "a second pass over the request"])
    step = rsi.SessionScript(lambda inputs: [memory_candidate(inputs, text=next(texts), mechanism=next(mechanisms))])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    outcomes = [r["outcome"] for r in result["rounds"] if r["outcome"] != "stopped"]
    assert outcomes[0] == "installed" and outcomes[1] in ("screened_out", "rejected", "installed"), outcomes
    w1 = result["rounds"][0]["release_after"]
    objects = activity.objects
    screen_releases = {}
    for r in activity.results.all():
        if "/train_screen" in r.attempt_id:
            release = objects.load(r.system_release_ref, ct.SystemRelease)
            screen_releases.setdefault(release.parent_ref, set()).add(release.worker_ref)
    assert set(screen_releases) == {w0, w1}                     # round 0 built on W0, round 1 on W1
    receipts = [objects.load(e["receipt"], ct.EvaluationReceipt) for e in ct.Events(activity.root).of("receipt_registered")
                if e.get("scope") == "cyc"] if any(True for _ in ct.Events(activity.root).of("receipt_registered")) else []
    experiment_rows = [e for e in xp.read(activity.root) if e.get("kind") == "experiment"]
    gated_w = objects.load(experiment_rows[0]["receipt"], ct.EvaluationReceipt).subject_ref
    assert gated_w in screen_releases[w0]                        # the screened W and the gated W are one
    # the carried parent is cleared at each round's start
    work = bench.improvement(activity, template_ref=cfg["rsi"]["task_plan_ref"], provider=rsi.BusinessWorker())
    work._parent_release = w0
    work.handlers()
    assert work._parent_release == "" and work._parent() == activity.releases.active()["release_ref"]


def test_a_screen_attempt_lost_to_infrastructure_is_refilled_once_and_no_reading_is_invalid(fake_upstream, tmp_path,
                                                                                            monkeypatch):
    """M4 §2 (2026-10-05): the screen's attempts go through the plan's refill like a gate arm's — an infrastructure
    failure is retried once and the refill supersedes the lost attempt — and a candidate with no valid screen
    reading at all ends `invalid` with the reason, never `screened_out`."""
    import sys
    from ouroloop.providers import ProviderError

    class FlakyOnce(rsi.BusinessWorker):
        tripped = False

        def chat(self, system, messages, specs):
            if rsi.FINISH_CHECK_MARK in (system or "") and not type(self).tripped:
                type(self).tripped = True
                raise ProviderError("gone for one call", retryable=True)
            return super().chat(system, messages, specs)
    monkeypatch.setitem(sys.modules, "flaky_screen", type("M", (), {"FlakyOnce": FlakyOnce}))
    # the release pins the actor at bootstrap: the flaky one is pinned by the fixture's config before the bootstrap
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch, llm={"type": "flaky_screen:FlakyOnce"})
    step = rsi.SessionScript(lambda inputs: [memory_candidate(inputs)])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert [r["outcome"] for r in result["rounds"]][0] == "installed", result["rounds"]
    screen = list(ct.Events(activity.root).of("train_screen"))[0]
    attempts = screen["attempts"]
    assert any(a.endswith("#1") for a in attempts) and screen["net_flips"] == 2
    rows = {r.attempt_id: r for r in activity.results.all() if "/train_screen" in r.attempt_id}
    refill = next(r for a, r in rows.items() if a.endswith("#1"))
    assert refill.supersedes in rows and rows[refill.supersedes].validity == "invalid"
    # the superseded attempt is no loss: only a `lost` screen names what it was lost to (2026-10-05, second recheck)
    ledger = ledger_of(activity)
    assert ledger["m1"]["screen"]["lost_to"] is None and "lost_to" not in screen
    assert "lost_to" not in xp.ledger_projection(list(ledger.values()))[0]["screen"]
    # no reading at all: invalid, with the reason, never screened_out
    cfg2, activity2, _ = prepared_v3(fake_upstream, tmp_path / "none", monkeypatch)
    monkeypatch.setattr(bench.Improvement, "train_screen", lambda self, candidate_ref, tasks: [])
    result2 = bench.improve(cfg2, ".", out=lambda _line: None, researcher=rsi.SessionScript(lambda i: [memory_candidate(i)]))
    assert result2["rounds"][0]["outcome"] == "invalid" and "no valid screen reading" in result2["rounds"][0]["reason"]
    rows2 = ledger_of(activity2)
    assert rows2["m1"]["outcome"] == "invalid" and rows2["m1"]["screen"]["outcome"] == "invalid"
    assert "no valid screen reading" in rows2["m1"]["screen"]["reason"]


def test_the_round_checks_every_screen_and_the_gate_bound_before_paying_a_screen(fake_upstream, tmp_path,
                                                                                 monkeypatch):
    """M4 §2 (2026-10-05): before the first screen the round checks once that all admitted candidates' screen
    attempts plus the gate's verification bound fit the budget; when they do not it ends budget_exhausted with no
    screen paid."""
    from ouroloop.evolve.execution import BudgetAccount
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    real = BudgetAccount.affordable
    asked = []

    def tight(self, bound, *, allocation=None):
        asked.append(dict(bound))
        if int(bound.get("model_calls") or 0) >= 14:          # 5 screen attempts + the gate's bound of 9
            return False
        return real(self, bound, allocation=allocation)
    monkeypatch.setattr(BudgetAccount, "affordable", tight)
    step = rsi.SessionScript(lambda inputs: [memory_candidate(inputs)])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert result["rounds"][0]["outcome"] == "budget_exhausted"
    assert "screens (5 attempts) and the gate's verification bound" in result["rounds"][0]["reason"]
    assert not list(ct.Events(activity.root).of("train_screen"))
    assert "screen" not in ledger_of(activity)["m1"] and any(b.get("model_calls") == 14 for b in asked)


def test_ledger_candidate_ids_are_unique_across_segments(fake_upstream, tmp_path, monkeypatch):
    """M4 §5 (2026-10-05, HIGH): two segments whose R reuses `candidates/c1` fold to two ledger entries — the id is
    the scope's diagnosis ordinal, not the per-segment round index — and the mechanism screened out in segment 0
    still triggers repeated_mechanism in segment 1."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    useless = lambda inputs: [memory_candidate(inputs, name="c1", text="Be concise.\n", mechanism="a shorter final message")]  # noqa: E731
    first = bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(useless), segment="segment-0")
    second = bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(useless), segment="segment-1")
    assert first["rounds"][0]["outcome"] == "screened_out" and second["rounds"][0]["outcome"] == "invalid"
    rows = xp.ledger_rows(xp.read(activity.root))
    assert [r["candidate"] for r in rows] == ["cyc/d0/c1", "cyc/d1/c1"]
    assert [r["outcome"] for r in rows] == ["screened_out", "invalid"]
    assert any("repeated_mechanism" in x for x in rows[1]["admission"]["reasons"])
    assert [r["ordinal"] for r in rows] == [0, 1] and [r["round"] for r in rows] == [0, 0]


def test_a_finish_with_more_than_k_candidates_drops_the_extra_before_any_check(fake_upstream, tmp_path, monkeypatch):
    """M4 §1 (2026-10-05): the (k+1)th declared candidate is `dropped` with the reason and is never drafted,
    admitted or screened; the k others go through."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch, k=3)
    step = rsi.SessionScript(lambda inputs: [
        memory_candidate(inputs, name="a"),
        memory_candidate(inputs, name="b", text=MEMORY + "B\n", mechanism="a second look at the request"),
        memory_candidate(inputs, name="c", text=MEMORY + "C\n", mechanism="a third look at the request"),
        memory_candidate(inputs, name="d", text=MEMORY + "D\n", mechanism="a fourth look at the request")])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert [r["outcome"] for r in result["rounds"]][0] == "installed"
    rows = ledger_of(activity)
    assert rows["d"]["outcome"] == "dropped" and "beyond the round's 3 candidates" in rows["d"]["reason"]
    assert "drafts" not in rows["d"] and "admission" not in rows["d"] and "screen" not in rows["d"]
    assert all("screen" in rows[n] for n in ("a", "b", "c")) and len(list(ct.Events(activity.root).of("train_screen"))) == 3


def test_a_session_declaration_naming_a_dev_task_is_refused_inside_the_round(fake_upstream, tmp_path, monkeypatch):
    """CT7-08 inside the real round (2026-10-05): a declaration whose predicted_affected names a dev task is refused
    at admission with the train-split reason — `train_tasks` reaches `_admit` — and the round records it."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)

    def plan(inputs):
        c = memory_candidate(inputs)
        c["declaration"]["predicted_affected"] = [rsi.DEV[0], rsi.TRAIN[1]]
        return [c]
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(plan))
    assert result["rounds"][0]["outcome"] == "invalid"
    rows = ledger_of(activity)
    assert rows["m1"]["admission"]["status"] == "invalid"
    assert any("outside the train split" in x and rsi.DEV[0] in x for x in rows["m1"]["admission"]["reasons"])
    assert "screen" not in rows["m1"]


def test_pick_best_pins_every_key():
    """M4 §2 (2026-10-05): the pick is the highest net flips at or above the floor whatever the list order; a tie
    goes to the lower mean cost, an unknown cost after a known one, and equal cost to declaration order; below the
    floor nothing is eligible."""
    from ouroloop.research.session import ScreenSettings, pick_best
    one, none = ScreenSettings(min_net_flips=1), ScreenSettings(min_net_flips=0)
    rows = [{"candidate": "a", "order": 0, "net_flips": 1, "cost_per_attempt": 0.1},
            {"candidate": "b", "order": 1, "net_flips": 3, "cost_per_attempt": 0.9},
            {"candidate": "c", "order": 2, "net_flips": 3, "cost_per_attempt": 0.2}]
    assert pick_best(rows, one)["candidate"] == "c" and pick_best(list(reversed(rows)), one)["candidate"] == "c"
    tie = [{"candidate": "x", "order": 0, "net_flips": 2, "cost_per_attempt": None},
           {"candidate": "y", "order": 1, "net_flips": 2, "cost_per_attempt": 0.5}]
    assert pick_best(tie, one)["candidate"] == "y"                    # known cost before unknown
    equal = [{"candidate": "p", "order": 1, "net_flips": 2, "cost_per_attempt": 0.5},
             {"candidate": "q", "order": 0, "net_flips": 2, "cost_per_attempt": 0.5}]
    assert pick_best(equal, one)["candidate"] == "q"                  # equal: declaration order
    assert pick_best([{"candidate": "z", "order": 0, "net_flips": 0, "cost_per_attempt": 0.1}], one) is None
    assert pick_best([{"candidate": "n", "order": 0, "net_flips": None, "cost_per_attempt": None}], none) is None
    zero = [{"candidate": "z", "order": 0, "net_flips": 0, "cost_per_attempt": 0.1}]
    assert pick_best(zero, none)["candidate"] == "z"


def test_declaration_reasons_require_a_real_difference_against_the_cited_row():
    """M4 §5 (2026-10-05): `how` that is the cited mechanism's own words (normalised) counts as not written, and
    citing an unrelated declaration ref does not satisfy differs_from for the row that matches."""
    ledger = [{"candidate": "cyc/d0/c1", "outcome": "screened_out", "declaration_ref": "declaration:sha256:" + "a" * 64,
               "declaration": {"mechanism": "A shorter final message", "mechanism_normalized": "a shorter final message",
                               "site": "memory.md", "carrier": "memory"}},
              {"candidate": "cyc/d0/c2", "outcome": "installed", "declaration_ref": "declaration:sha256:" + "b" * 64,
               "declaration": {"mechanism": "check every step", "mechanism_normalized": "check every step",
                               "site": "memory.md", "carrier": "memory"}}]
    same = {"mechanism": "a shorter final  message", "mechanism_normalized": "a shorter final message",
            "site": "memory.md", "carrier": "memory", "predicted_affected": [], "differs_from": []}
    assert any("repeated_mechanism" in r for r in declaration_reasons(same, ledger=ledger))
    echoed = {**same, "differs_from": [{"ref": "declaration:sha256:" + "a" * 64, "how": "A shorter final message"}]}
    assert any("repeated_mechanism" in r for r in declaration_reasons(echoed, ledger=ledger))
    unrelated = {**same, "differs_from": [{"ref": "declaration:sha256:" + "b" * 64, "how": "names the tool result"}]}
    assert any("repeated_mechanism" in r for r in declaration_reasons(unrelated, ledger=ledger))
    stated = {**same, "differs_from": [{"ref": "declaration:sha256:" + "a" * 64, "how": "names the tool result"}]}
    assert declaration_reasons(stated, ledger=ledger) == []
    # an installed row on the same key is no repeat: only rejected and screened_out are
    installed_key = {**same, "mechanism": "check every step", "mechanism_normalized": "check every step"}
    assert declaration_reasons(installed_key, ledger=ledger) == []


# ---------------------------------------------------------------------------- recheck of 2026-10-05: round, screen, ledger
class FirstCalls(rsi.SessionScript):
    """The scripted session with some calls sent first, and every tool result it is handed kept."""

    def __init__(self, plan, first, **kw):
        super().__init__(plan, **kw)
        self.first, self.results = list(first), []

    def _enqueue(self, candidates):
        super()._enqueue(candidates)
        if self.first:
            self._queue.insert(0, list(self.first))
            self.first = []

    def session_turn(self, system, messages, tools):
        if messages[-1].get("role") == "tool":
            self.results.extend(messages[-1]["results"])
        return super().session_turn(system, messages, tools)


def test_a_call_that_is_not_strict_utf8_leaves_the_round_and_its_session_record_intact(fake_upstream, tmp_path,
                                                                                         monkeypatch):
    """M4 §1 (2026-10-05 recheck): a write whose path holds a lone surrogate is the tool's error; the round goes on
    to install, and its research_session event — which used to fail to encode after the worker had answered — is on
    record with the call's safe row."""
    from ouroloop.chat import ToolCall
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    step = FirstCalls(lambda inputs: [memory_candidate(inputs)],
                      [ToolCall("u", "write_file", {"path": "candidates/m1/\udc80.md", "text": "x"})])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert [r["outcome"] for r in result["rounds"]][0] == "installed", result["rounds"]
    session = list(ct.Events(activity.root).of("research_session"))[0]
    assert {"turn": 1, "tool": "write_file", "ok": False, "error": "not strict UTF-8"} in session["calls"]
    assert step.results[0]["is_error"] and "not strict UTF-8" in step.results[0]["output"]


def test_the_round_hands_ks_package_cap_to_the_work_tree(fake_upstream, tmp_path, monkeypatch):
    """M4 §1 (2026-10-05 recheck): the tree's byte cap is K's max_package_bytes, carried from the frozen harness
    through the round's inputs to `work init`: under a K of 1000 bytes, a 2000-byte write comes back as the tool's
    error naming 1000, and the round goes on with the candidate that fits."""
    from ouroloop.chat import ToolCall
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch, harness={"max_package_bytes": 1000})
    core = activity.objects.load(activity.objects.load(w0, ct.SystemRelease).core_ref, ct.CoreManifest)
    assert core.capabilities["harness"]["package"]["max_package_bytes"] == 1000
    step = FirstCalls(lambda inputs: [memory_candidate(inputs)],
                      [ToolCall("big", "write_file", {"path": "candidates/big/a.md", "text": "x" * 2000})])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    refused = step.results[0]
    assert refused["is_error"] and "at most 1000 bytes" in refused["output"], refused
    assert [r["outcome"] for r in result["rounds"]][0] == "installed", result["rounds"]


def test_a_resumed_gate_finds_its_candidate_by_experiment_id_not_by_a_repeated_proposal_ref(fake_upstream, tmp_path,
                                                                                          monkeypatch):
    """M4 §5 (2026-10-05 recheck): proposal refs are content-addressed and repeat across rounds, so an earlier
    candidate admitted with the identical proposal ref but never screened sits before the interrupted one in the log;
    the resumed gate's `gated` row still lands on the candidate whose screen named the experiment."""
    from ouroloop.providers import ProviderError
    from ouroloop.research.session import ledger_candidate_of
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    real = bench.Improvement._evaluate
    tripped = []

    def once(self, candidate_ref, budget, confirming=None):
        if not tripped:
            tripped.append(candidate_ref)
            raise ProviderError("the gate's model went away", retryable=False)
        return real(self, candidate_ref, budget, confirming=confirming)
    with monkeypatch.context() as m:
        m.setattr(bench.Improvement, "_evaluate", once)
        bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(lambda i: [memory_candidate(i)]))
    path = tmp_path / "ws" / "experience.jsonl"
    lines = path.read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    admitted = next(r for r in rows if r.get("kind") == "ledger" and r.get("stage") == "admitted")
    first = next(i for i, r in enumerate(rows) if r.get("kind") == "ledger")
    earlier = [{**next(r for r in rows if r.get("kind") == "ledger" and r.get("stage") == "declared"),
                "candidate": "cyc/d0/earlier", "dir": "earlier"},
               {**admitted, "candidate": "cyc/d0/earlier"}]
    path.write_text("\n".join([*lines[:first], *map(json.dumps, earlier), *lines[first:]]) + "\n")
    second = bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(lambda inputs: []))
    assert second["rounds"][0]["outcome"] == "installed" and second["rounds"][0]["reason"].startswith("resumed")
    folded = ledger_of(activity)
    assert folded["earlier"]["proposal_ref"] == folded["m1"]["proposal_ref"]
    assert "gate" not in folded["earlier"] and folded["earlier"]["outcome"] == "pending"
    assert folded["m1"]["outcome"] == "installed" and folded["m1"]["gate"]["verdict"] == "accepted"
    # an older log, whose entries carry no experiment id, falls back to the latest ungated entry on the proposal ref
    old = [r for r in xp.read(activity.root) if r.get("kind") == "ledger" and r.get("stage") in ("declared", "admitted")]
    assert ledger_candidate_of(old, admitted["proposal_ref"])["candidate"] == "cyc/d0/m1"
    assert ledger_candidate_of(old, admitted["proposal_ref"], "x-not-on-record")["candidate"] == "cyc/d0/m1"


def test_a_same_key_revision_keeps_the_first_declaration_everywhere(fake_upstream, tmp_path, monkeypatch):
    """M4 §1 (2026-10-05 recheck): a revision's finish that keeps (mechanism, site, carrier) but changes the
    trigger_condition and predicted_affected changes nothing of the declaration: the ledger, the declaration object
    and the proposal (its `expected` and `abandon_if`) all read the first one."""
    from ouroloop.agent import harness_package as hp
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    manifest = {**hp.empty_manifest(),
                "hooks": [{"name": "closing", "site": "on_finish", "entrypoint": "middlewares/closing.py:run"}]}
    hook = "def run(ctx, event):\n    return {'action': 'continue'}\n"
    key = {"mechanism": "a check before finishing", "site": "on_finish", "carrier": "hook"}

    def plan(inputs):
        return [{"dir": "h", "files": {"manifest.json": json.dumps(manifest),
                                       "middlewares/closing.py": "import no_such_module_for_this_test\n" + hook},
                 "declaration": dict(key)}]

    def repair(message, inputs):
        return [{"dir": "h", "files": {"middlewares/closing.py": hook},
                 "declaration": {**key, "trigger_condition": "only when the request names a channel",
                                 "predicted_affected": [rsi.TRAIN[0]]}}]
    step = rsi.SessionScript(plan, repair=repair)
    bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    rows = ledger_of(activity)
    first = rows["h"]["declaration"]
    assert first["trigger_condition"] == "always" and first["predicted_affected"] != [rsi.TRAIN[0]]
    assert activity.objects.get(rows["h"]["declaration_ref"])["trigger_condition"] == "always"
    assert [d["ok"] for d in rows["h"]["drafts"]] == [False, True] and rows["h"]["admission"]["status"] == "admitted"
    proposal = activity.objects.load(rows["h"]["proposal_ref"], ct.ExperimentProposal)
    assert proposal.abandon_if == "always" and proposal.expected == f"fixes {first['predicted_affected']}"
    assert proposal.declaration_ref == rows["h"]["declaration_ref"]


def _invalid(rows, failure_class, keep=lambda r: True):
    import dataclasses
    return [dataclasses.replace(r, validity="invalid", success=False, failure_class=failure_class,
                                invalid_reason=f"{failure_class} in the test") if keep(r) else r for r in rows]


@pytest.mark.parametrize("lost_to", ["infrastructure", "evaluator"])
def test_a_round_whose_screens_were_lost_to_infrastructure_is_rerun_not_invalid(fake_upstream, tmp_path, monkeypatch,
                                                                                lost_to):
    """M4 §2 (2026-10-05 recheck): when no candidate has a valid screen reading and the attempts (refill included)
    were lost to infrastructure or the evaluator, the candidate is `lost` with the failure classes in `lost_to` — not
    `invalid`, no repeat trigger — and the round ends `inconclusive` for infrastructure, as a gate's integrity loss
    does: run again, never a completed round. Lost to the budget instead, the round ends budget_exhausted."""
    from ouroloop.research.loop import completed_round, infrastructure_round
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    real = bench.Improvement.train_screen
    monkeypatch.setattr(bench.Improvement, "train_screen",
                        lambda self, ref, tasks: _invalid(real(self, ref, tasks), lost_to))
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(lambda i: [memory_candidate(i)]))
    round0 = result["rounds"][0]
    assert round0["outcome"] == "inconclusive" and round0["reason"].startswith("infrastructure:"), round0
    row = next(r for r in xp.read(activity.root) if r.get("kind") == "round")
    assert infrastructure_round(row) and not completed_round(row)
    rows = ledger_of(activity)
    assert rows["m1"]["outcome"] == "lost" and rows["m1"]["screen"]["lost_to"] == [lost_to]
    assert rows["m1"]["screen"]["net_flips"] is None and "no valid screen reading on the failing set" in rows["m1"]["reason"]
    assert xp.ledger_projection(list(rows.values()))[0]["screen"]["lost_to"] == [lost_to]
    assert declaration_reasons(rows["m1"]["declaration"], ledger=list(rows.values())) == []   # no repeat trigger
    # the same screens lost to a spent budget: the round ends budget_exhausted, the paid screen kept as interrupted
    cfg2, activity2, _ = prepared_v3(fake_upstream, tmp_path / "budget", monkeypatch)
    monkeypatch.setattr(bench.Improvement, "train_screen",
                        lambda self, ref, tasks: _invalid(real(self, ref, tasks), "budget_exhausted"))
    result2 = bench.improve(cfg2, ".", out=lambda _line: None, researcher=rsi.SessionScript(lambda i: [memory_candidate(i)]))
    assert result2["rounds"][0]["outcome"] == "budget_exhausted" and result2["stop_reason"] == "budget_exhausted"
    assert ledger_of(activity2)["m1"]["screen"]["outcome"] == "interrupted"


def test_a_screen_with_no_reading_on_its_failing_set_is_invalid_never_screened_out(fake_upstream, tmp_path,
                                                                                   monkeypatch):
    """M4 §2 (2026-10-05 recheck): net flips need a valid after-reading on the failing set; read on the passing
    sample alone they could only be <= 0, and screened_out would make the mechanism a repeat. Such a screen is
    `invalid` with the reason; so is one with a candidate defect on any task (the gate would judge it invalid)."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    real = bench.Improvement.train_screen

    def passed():                 # the round's own train measurement: on record once the round has run it
        return {r.task_id for r in activity.results.all() if "/measure" in r.attempt_id and r.success}
    monkeypatch.setattr(bench.Improvement, "train_screen",
                        lambda self, ref, tasks: real(self, ref, [t for t in tasks if t in passed()]))
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(lambda i: [memory_candidate(i)]))
    assert result["rounds"][0]["outcome"] == "invalid", result["rounds"]
    rows = ledger_of(activity)
    screen = rows["m1"]["screen"]
    assert rows["m1"]["outcome"] == "invalid" and screen["outcome"] == "invalid" and screen["net_flips"] is None
    assert "no valid screen reading on the failing set" in screen["reason"] and screen["failing"]
    event = list(ct.Events(activity.root).of("train_screen"))[0]
    assert event["attempts"] and all(a.split("/")[1] in passed() for a in event["attempts"])
    # a candidate defect on a sample task: invalid with the defect, though the failing set read
    cfg2, activity2, _ = prepared_v3(fake_upstream, tmp_path / "defect", monkeypatch)
    sample = {r.task_id for r in activity2.results.all() if r.success} | passed()
    monkeypatch.setattr(bench.Improvement, "train_screen", lambda self, ref, tasks: _invalid(
        real(self, ref, tasks), "candidate_defect", keep=lambda r: r.task_id in sample))
    bench.improve(cfg2, ".", out=lambda _line: None, researcher=rsi.SessionScript(lambda i: [memory_candidate(i)]))
    defect = ledger_of(activity2)["m1"]
    assert defect["outcome"] == "invalid" and defect["screen"]["reason"].startswith("candidate defect on")


def test_a_predicted_task_that_already_passes_stays_out_of_the_failing_set(fake_upstream, tmp_path, monkeypatch):
    """M4 §2 (2026-10-05, C11): the failing set is (the notes' tasks ∪ predicted_affected) ∩ the tasks that failed in
    the latest train measurement; a predicted task that passes stays in the declaration and reaches the screen only
    through the passing sample; `thin_screen` counts the failing set alone."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    measured, chosen = {}, []

    def plan(inputs):             # the round's train measurement is on record by the time the session opens
        measured.update({r.task_id: r.success for r in activity.results.all() if "/measure" in r.attempt_id})
        chosen.append(sorted(t for t, ok in measured.items() if ok)[0])
        c = memory_candidate(inputs)
        c["declaration"]["predicted_affected"] = sorted({n["task_id"] for n in inputs["notes"]}) + chosen[:1]
        return [c]
    bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(plan))
    passing = chosen[0]
    rows = ledger_of(activity)
    screen = rows["m1"]["screen"]
    assert passing in rows["m1"]["declaration"]["predicted_affected"] and passing not in screen["failing"]
    assert set(screen["failing"]) == {t for t, ok in measured.items() if not ok}
    assert screen["thin_screen"] is (len(screen["failing"]) < 3)
    assert set(screen["sample"]) <= {t for t, ok in measured.items() if ok}


@pytest.mark.parametrize("stage,status,outcome", [
    ("admitted", "collect", "blocked"), ("admitted", "repair_runtime", "blocked"), ("admitted", "invalid", "invalid"),
    ("screened", "selected", "pending"), ("screened", "not_selected", "not_selected"),
    ("screened", "screened_out", "screened_out"), ("screened", "invalid", "invalid"), ("screened", "lost", "lost"),
    ("screened", "interrupted", "pending")])
def test_each_admission_status_and_screen_outcome_folds_to_its_own_ledger_outcome(stage, status, outcome):
    """M4 §5 (2026-10-05 recheck): an admission that answered collect or repair_runtime is `blocked` (not the
    candidate's fault), a refused one `invalid`; every screen outcome of SCREEN_OUTCOMES folds as M4 §2 says — the
    selected one stays pending until its gate, an interrupted one is pending (the round runs again) — and the
    projection R reads carries the same outcome."""
    assert status in xp.SCREEN_OUTCOMES or stage == "admitted"
    declared = xp.ledger_row("declared", "s/d0/c1", dir="c1", status="declared",
                             declaration={"mechanism": "m", "site": "memory.md", "carrier": "memory"})
    if stage == "admitted":
        rows = [declared, xp.ledger_row("admitted", "s/d0/c1", status=status, reasons=["why"])]
    else:
        rows = [declared, xp.ledger_row("admitted", "s/d0/c1", status="admitted", reasons=[], proposal_ref="proposal:x"),
                xp.ledger_row("screened", "s/d0/c1", outcome=status, net_flips=None, reason="r")]
    folded = xp.ledger_rows(rows)
    assert folded[0]["outcome"] == outcome and xp.ledger_projection(folded)[0]["outcome"] == outcome
    if stage == "admitted":
        assert xp.ledger_projection(folded)[0]["admission"] == status


def test_the_proposal_ref_is_read_from_the_admitted_row():
    """M4 §5 (2026-10-05, D14): the proposal ref is written on the admitted row and read from there, so a candidate
    whose screen never ran still carries it."""
    rows = [xp.ledger_row("declared", "s/d0/c1", dir="c1", status="declared", declaration={}),
            xp.ledger_row("drafted", "s/d0/c1", drafts=[], package_ref="package:p", status="ready"),
            xp.ledger_row("admitted", "s/d0/c1", status="admitted", reasons=[], proposal_ref="proposal:sha256:P")]
    assert xp.ledger_rows(rows)[0]["proposal_ref"] == "proposal:sha256:P"


# ---------------------------------------------------------------------------- second recheck of 2026-10-05
def test_the_round_keys_native_content_by_the_research_providers_name(fake_upstream, tmp_path, monkeypatch):
    """M4 §1 (2026-10-05, second recheck): the round names the session's provider by the research completion
    interface's `name` (as the v1 agent/loop.py keyed native content), so a session run through the Anthropic provider
    replays the first reply's blocks on its second call and the round installs; with the bare list it raised."""
    from types import SimpleNamespace
    from test_research_session import FakeAnthropic, anthropic_response
    from ouroloop.providers.anthropic import AnthropicProvider
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)

    class ViaAnthropic(rsi.SessionScript):
        """The scripted session's turns, each sent through AnthropicProvider.chat over a fake Messages API."""

        def __init__(self, plan):
            super().__init__(plan)
            self.client = FakeAnthropic()
            self.provider = AnthropicProvider(client=self.client, fallbacks=False)
            self.llm = SimpleNamespace(name=self.provider.name)

        def session_turn(self, system, messages, tools):
            turn = super().session_turn(system, messages, tools)
            self.client.queue.append(anthropic_response(turn.text if turn.tool_calls else "", turn.tool_calls))
            return self.provider.chat(system, messages, tools)
    step = ViaAnthropic(lambda inputs: [memory_candidate(inputs)])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert [r["outcome"] for r in result["rounds"]][0] == "installed", result["rounds"]
    second = step.client.sent[1]["messages"]
    assert [m for m in second if m["role"] == "assistant"][0]["content"][0].type == "tool_use"


def test_a_candidate_the_worker_cannot_hand_back_is_invalid_with_the_workers_error(fake_upstream, tmp_path,
                                                                                    monkeypatch):
    """M4 §1 (2026-10-05, second recheck): a refused export of a candidate directory — when it is first read back,
    or after a revision — ends that candidate `invalid` with the worker's error; the round goes on with the others
    and, when none is left, ends invalid with the reasons, never with a KeyError."""
    from ouroloop.research import session as S
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    real = S.WorkTree.fetch
    refusal = "the answer would exceed the protocol line; read it in pieces"
    monkeypatch.setattr(S.WorkTree, "fetch", lambda self, name: (None, refusal) if name == "a" else real(self, name))
    step = rsi.SessionScript(lambda inputs: [memory_candidate(inputs, name="a"),
                                             memory_candidate(inputs, name="b", text=MEMORY + "B\n",
                                                              mechanism="a second look at the request")])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert [r["outcome"] for r in result["rounds"]][0] == "installed", result["rounds"]
    rows = ledger_of(activity)
    assert rows["a"]["outcome"] == "invalid" and refusal in rows["a"]["reason"] and rows["b"]["outcome"] == "installed"
    # every candidate refused: the round is invalid with the reasons
    cfg2, activity2, _ = prepared_v3(fake_upstream, tmp_path / "all", monkeypatch)
    monkeypatch.setattr(S.WorkTree, "fetch", lambda self, name: (None, refusal))
    result2 = bench.improve(cfg2, ".", out=lambda _line: None, researcher=rsi.SessionScript(lambda i: [memory_candidate(i)]))
    assert result2["rounds"][0]["outcome"] == "invalid" and refusal[:40] in result2["rounds"][0]["reason"]
    # refused after a revision: the candidate is invalid with the worker's error
    from ouroloop.agent import harness_package as hp
    cfg3, activity3, _ = prepared_v3(fake_upstream, tmp_path / "revised", monkeypatch)
    fetched = []
    monkeypatch.setattr(S.WorkTree, "fetch", lambda self, name: (fetched.append(name), (None, refusal))[1]
                        if len(fetched) >= 1 else (fetched.append(name), real(self, name))[1])
    manifest = {**hp.empty_manifest(),
                "hooks": [{"name": "closing", "site": "on_finish", "entrypoint": "middlewares/closing.py:run"}]}
    hook = "def run(ctx, event):\n    return {'action': 'continue'}\n"
    key = {"mechanism": "a check before finishing", "site": "on_finish", "carrier": "hook"}
    step3 = rsi.SessionScript(
        lambda inputs: [{"dir": "h", "files": {"manifest.json": json.dumps(manifest),
                                               "middlewares/closing.py": "import no_such_module_for_this_test\n" + hook},
                         "declaration": dict(key)}],
        repair=lambda message, inputs: [{"dir": "h", "files": {"middlewares/closing.py": hook}, "declaration": dict(key)}])
    bench.improve(cfg3, ".", out=lambda _line: None, researcher=step3)
    h = ledger_of(activity3)["h"]
    assert h["outcome"] == "invalid" and "the revised candidate could not be read back" in h["reason"]


def test_a_round_with_no_failing_train_task_asks_for_evidence_without_opening_the_session(fake_upstream, tmp_path,
                                                                                         monkeypatch):
    """M4 §2 (2026-10-05, second recheck): when no train task failed in the latest measurement no candidate could
    have a failing set, so the round ends needs_collection with next_action collect before the paid session opens:
    no session request, no research_session event; the session format says what a candidate is screened on."""
    import sys
    from ouroloop.research.session import SESSION_FORMAT

    class Reminded(rsi.BusinessWorker):
        def chat(self, system, messages, specs):
            return super().chat((system or "") + "\n" + rsi.FINISH_CHECK_MARK, messages, specs)
    monkeypatch.setitem(sys.modules, "reminded_actor", type("M", (), {"Reminded": Reminded}))
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch, llm={"type": "reminded_actor:Reminded"})
    step = rsi.SessionScript(lambda inputs: [memory_candidate(inputs)])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    first = result["rounds"][0]
    assert first["outcome"] == "needs_collection" and first["next_action"] == "collect", first
    assert first["reason"].startswith("no train task failed in the latest measurement")
    assert not [r for r in step.requests if r["step"] == "session"]
    assert not list(ct.Events(activity.root).of("research_session")) and not ledger_of(activity)
    assert "screened on the train tasks that currently fail" in SESSION_FORMAT


def test_a_crash_between_a_terminal_gate_and_its_ledger_row_is_filled_on_resume(fake_upstream, tmp_path,
                                                                              monkeypatch):
    """M4 §5 (2026-10-05, second recheck): the experiment reached a terminal state and the round died before writing
    the gated row; the restart's resume_unfinished finds the selected candidate whose Execution is terminal and
    writes the row from that state's receipt, once."""
    from ouroloop.research import session as S
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    real = S.gate_ledger_fields

    class Died(BaseException):
        pass
    calls = []

    def dies_once(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise Died("the round died after the gate")
        return real(*a, **kw)
    monkeypatch.setattr(S, "gate_ledger_fields", dies_once)
    with pytest.raises(Died):
        bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(lambda i: [memory_candidate(i)]))
    rows = ledger_of(activity)
    assert rows["m1"]["screen"]["outcome"] == "selected" and "gate" not in rows["m1"]
    bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(lambda inputs: []))
    rows = ledger_of(activity)
    assert rows["m1"]["outcome"] == "installed" and rows["m1"]["gate"]["verdict"] == "accepted"
    assert rows["m1"]["gate"]["receipt"] and rows["m1"]["gate"]["fixed"] == [rsi.DEV[0]]
    gated = [r for r in xp.read(activity.root) if r.get("kind") == "ledger" and r.get("stage") == "gated"]
    assert len(gated) == 1


def test_ledger_candidate_of_falls_back_only_to_ungated_entries_without_an_experiment_id():
    """M4 §5 (2026-10-05, second recheck): the fallback on the proposal ref skips an entry already gated and an
    entry whose screen named another experiment, and takes the latest of what is left."""
    from ouroloop.research.session import ledger_candidate_of
    P = "proposal:sha256:" + "p" * 64

    def candidate(name, *, experiment=None, gated=False):
        rows = [xp.ledger_row("declared", f"s/d0/{name}", dir=name, status="declared", declaration={}),
                xp.ledger_row("admitted", f"s/d0/{name}", status="admitted", reasons=[], proposal_ref=P)]
        if experiment:
            rows.append(xp.ledger_row("screened", f"s/d0/{name}", outcome="selected", experiment_id=experiment,
                                      proposal_ref=P))
        if gated:
            rows.append(xp.ledger_row("gated", f"s/d0/{name}", verdict="rejected", installed=False))
        return rows
    old_a, old_b, gated, named = candidate("a"), candidate("b"), candidate("g", gated=True), candidate("x", experiment="x1")
    assert ledger_candidate_of([*old_a, *old_b], P)["candidate"] == "s/d0/b"               # the latest
    assert ledger_candidate_of([*old_a, *gated], P)["candidate"] == "s/d0/a"               # not the gated one
    assert ledger_candidate_of([*old_a, *named], P, "x9")["candidate"] == "s/d0/a"         # not another experiment's
    assert ledger_candidate_of([*old_a, *named], P, "x1")["candidate"] == "s/d0/x"         # its own, by id
    assert ledger_candidate_of([*gated, *named], P, "x9") is None


def test_a_screen_row_outcome_outside_the_vocabulary_is_refused_where_the_row_is_made():
    """M4 §2 (2026-10-05, second recheck): xp.ledger_row refuses a screened row whose outcome is not one of
    SCREEN_OUTCOMES, so no row the fold would misread is ever written."""
    with pytest.raises(ValueError, match="screen outcome 'passed'"):
        xp.ledger_row("screened", "s/d0/c1", outcome="passed")
    assert all(xp.ledger_row("screened", "s/d0/c1", outcome=o)["outcome"] == o for o in xp.SCREEN_OUTCOMES)


def test_a_candidate_with_no_failing_task_is_not_screened(fake_upstream, tmp_path, monkeypatch):
    """M4 §2 (2026-10-05, second recheck): a candidate whose notes and predicted_affected name no task that fails
    now has an empty failing set: it is `invalid` and no screen attempt runs for it. Under a K without the v0.4 read
    floors: with them, admission refuses it first (test_a_candidate_whose_session_read_none_of_its_failures_is_refused)."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch, harness={"research_session": V03_SESSION})

    def plan(inputs):
        passing = sorted({r.task_id for r in activity.results.all() if "/measure" in r.attempt_id and r.success})
        c = memory_candidate(inputs)
        c["declaration"].update(notes=[], predicted_affected=passing[:1])
        return [c]
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(plan))
    assert result["rounds"][0]["outcome"] == "invalid"
    m1 = ledger_of(activity)["m1"]
    assert m1["outcome"] == "invalid" and m1["screen"]["failing"] == [] and "no failing train task" in m1["reason"]
    assert not [r for r in activity.results.all() if "/train_screen" in r.attempt_id]


# ---------------------------------------------------------------------------- v0.3 step 5 (2026-10-06): R sees each domain
def test_the_ledger_projection_shows_each_domains_gate_outcome_without_dev_task_ids(tmp_path):
    """M4 §1 台账投影 (2026-10-06): over several domains the gate R reads in its ledger shows, per domain, the fixed /
    regressed / still_failing counts and the two dev means with their difference (the receipt's metrics.per_domain)
    — "helped marketing, hurt sales" — and no dev task id; an arm's E0, whose lists became counts, shows the same
    counts; a gate over one domain shows none."""
    objects = ct.Objects(tmp_path)
    per_domain = {"marketing": {"tasks": 2, "subject": 1.0, "control": 0.0, "gain": 1.0},
                  "sales": {"tasks": 2, "subject": 0.0, "control": 1.0, "gain": -1.0}}
    receipt = objects.put_json("receipt", {"metrics": {"gain": 0.0, "per_domain": per_domain}})
    dev = {"fixed": ["marketing.dev_a", "marketing.dev_b"], "regressed": ["sales.dev_c", "sales.dev_d"],
           "still_failing": ["sales.dev_e"]}
    rows = [xp.ledger_row("declared", "s/d2/m1", scope="s", round=2, dir="m1", status="declared",
                          declaration={"mechanism": "check", "site": "memory.md", "carrier": "memory"}),
            xp.ledger_row("gated", "s/d2/m1", scope="s", round=2, verdict="rejected", receipt=receipt, gain=0.0, **dev)]
    shown = xp.ledger_projection(xp.ledger_rows(rows), objects)[0]["gate"]
    assert shown["by_domain"] == {
        "marketing": {"fixed": 2, "regressed": 0, "still_failing": 0, "subject": 1.0, "control": 0.0, "gain": 1.0},
        "sales": {"fixed": 0, "regressed": 2, "still_failing": 1, "subject": 0.0, "control": 1.0, "gain": -1.0}}
    assert (shown["fixed"], shown["regressed"], shown["still_failing"]) == (2, 2, 1)
    text = json.dumps(xp.ledger_projection(xp.ledger_rows(rows), objects))
    assert not any(t in text for ids in dev.values() for t in ids)
    # an arm's E0: the per-task lists became counts, and the per-domain counts survive the stripping
    stripped = [rows[0], xp.strip_dev_task_ids(rows[1])]
    assert not any(t in json.dumps(stripped) for ids in dev.values() for t in ids)
    e0 = xp.ledger_projection(xp.ledger_rows(stripped))[0]["gate"]["by_domain"]
    assert e0["marketing"]["fixed"] == 2 and e0["sales"]["regressed"] == 2 and "gain" not in e0["sales"]
    one = [rows[0], xp.ledger_row("gated", "s/d2/m1", scope="s", round=2, verdict="rejected",
                                  fixed=["rsi.dev_update_stage"], regressed=[], still_failing=[])]
    assert "by_domain" not in xp.ledger_projection(xp.ledger_rows(one))[0]["gate"]


# ---------------------------------------------------------------------------- v0.4 (docs/design/V0_4_PLAN.md)
from ouroloop.benchmark import TaskResult  # noqa: E402
from ouroloop.research.experiment import grounding_reasons  # noqa: E402
from ouroloop.research.session import ScreenSettings, eligible, passing_sample, pick_best  # noqa: E402


def _passed(tasks):
    return [TaskResult(t, t, "train", 0, "W0", "r", "m", success=True, partial_credit=1.0) for t in tasks]


def test_the_passing_sample_is_redrawn_each_round_with_must_not_tasks_first():
    """V0_4_PLAN §1.6 (a): six of the passing train tasks, drawn afresh each round from (order_seed, the round's
    ordinal) — the same round draws the same tasks, another round others — tasks with must-not checks taken first,
    listed in the frozen order."""
    order = [f"t{i:02d}" for i in range(20)]
    rows = _passed(order[:16])
    must_not = ("t03", "t11", "t15")
    draws = [passing_sample(rows, order=order, size=6, seed=1, round_ordinal=r, must_not=must_not) for r in range(4)]
    assert all(len(d) == 6 and set(must_not) <= set(d) and d == sorted(d) for d in draws)
    assert len({tuple(d) for d in draws}) > 1                                   # redrawn per round
    assert passing_sample(rows, order=order, size=6, seed=1, round_ordinal=2, must_not=must_not) == draws[2]
    assert set(passing_sample(rows, order=order, size=2, seed=1, round_ordinal=0, must_not=must_not)) <= set(must_not)
    assert not set(passing_sample(rows, order=order, size=6, seed=1, round_ordinal=0)) - set(order[:16])


def test_the_screen_needs_a_fix_and_at_most_one_sample_regression_then_ranks_by_net_flips():
    """V0_4_PLAN §1.6 (b): eligible with ≥ min_fixes fixes and ≤ max_sample_regressions regressions on the passing
    sample (net flips = fixes − regressions, floor 0), then the highest net flips — so a broad change that fixes
    three and breaks two is not eligible; without the cap, sample regressions count only through net flips."""
    def screened(name, order, fixes, regressions, cost=0.5):
        failing, sample = [f"f{i}" for i in range(3)], [f"s{i}" for i in range(6)]
        before = {**{t: False for t in failing}, **{t: True for t in sample}}
        after = {**{t: i < fixes for i, t in enumerate(failing)}, **{t: i >= regressions for i, t in enumerate(sample)}}
        return {"candidate": name, "order": order, "failing": failing, "sample": sample, "before": before,
                "after": after, "net_flips": fixes - regressions, "cost_per_attempt": cost}
    broad, narrow, quiet = screened("broad", 0, 3, 2), screened("narrow", 1, 1, 0), screened("quiet", 2, 0, 0)
    v04 = ScreenSettings(passing_sample=6, min_net_flips=0, min_fixes=1, max_sample_regressions=1)
    assert [eligible(s, v04) for s in (broad, narrow, quiet)] == [False, True, False]
    assert pick_best([broad, narrow, quiet], v04)["candidate"] == "narrow"
    one_each = screened("even", 3, 1, 1)
    assert eligible(one_each, v04) and one_each["net_flips"] == 0
    assert pick_best([one_each, narrow], v04)["candidate"] == "narrow"           # ranked by net flips
    uncapped = ScreenSettings(passing_sample=6, min_net_flips=0, min_fixes=1)
    assert [eligible(s, uncapped) for s in (broad, narrow, quiet)] == [True, True, False]
    assert pick_best([broad, narrow, quiet], uncapped)["candidate"] == "broad"   # a tie on net flips: declaration order


def test_grounding_needs_a_failed_read_among_the_predicted_tasks_and_one_passing_read():
    """V0_4_PLAN §1.3: the host's read record grounds a candidate — a failed trajectory of a task in its
    predicted_affected, and a passing one in the session when one was listed; each reason names what is missing."""
    decl = {"predicted_affected": ["a", "b"]}
    floors = {"min_failing": 1, "min_passing": 1, "passing_listed": True}
    assert grounding_reasons(decl, {"failing": ["b"], "passing": ["z"], **floors}) == []
    missing = grounding_reasons(decl, {"failing": ["c"], "passing": [], **floors})
    assert len(missing) == 2 and "failed trajectory" in missing[0] and "['a', 'b']" in missing[0]
    assert "passing trajectory" in missing[1]
    assert grounding_reasons(decl, {"failing": ["a"], "passing": [], **{**floors, "passing_listed": False}}) == []


def test_admission_reads_the_floors_against_the_hosts_read_record():
    """V0_4_PLAN §1.3 (review 2026-10-06): what admission's backstop checks (grounding_inputs) is the host's record
    of the session's reads with K's floors, and whether a passing attempt was listed; a floor of 0 is met by no read
    (P1 step 6: no separate path for it), and without a reader there is nothing to check."""
    from types import SimpleNamespace
    from ouroloop.research.session import SessionLimits, grounding_inputs
    reader = SimpleNamespace(read_tasks=lambda: {"failing": ["a"], "passing": []})
    floors = SessionLimits(min_failing_reads=1, min_passing_reads=1)
    reads = grounding_inputs(SimpleNamespace(limits=floors, trajectories=reader), [{"outcome": "passing"}])
    assert reads == {"failing": ["a"], "passing": [], "min_failing": 1, "min_passing": 1, "passing_listed": True}
    assert declaration_reasons({"predicted_affected": ["a"]}, reads=reads)[0].startswith("ungrounded: the session "
                                                                                        "read 0 passing")
    assert len(declaration_reasons({"predicted_affected": ["b"]}, reads=reads)) == 2
    none = grounding_inputs(SimpleNamespace(limits=SessionLimits(), trajectories=reader), [{"outcome": "passing"}])
    assert none["min_failing"] == none["min_passing"] == 0
    assert declaration_reasons({"predicted_affected": ["b"]}, reads=none) == []
    assert grounding_inputs(SimpleNamespace(limits=floors, trajectories=None), []) is None


def test_an_ungrounded_finish_is_refused_in_the_session_and_admission_is_the_backstop(fake_upstream, tmp_path,
                                                                                      monkeypatch):
    """V0_4_PLAN §1.3 (M4 §1 v0.4; review 2026-10-06): under the v3 preset's read floors, the first `finish` of a
    session that read no trajectory is the finish call's error, naming the failed and the passing read each candidate
    lacks — R may read and finish again within its caps, and then its candidate is admitted and screened; a session
    that never reads decides nothing and no screen is paid. The reads are the host's record, not R's word; admission
    checks the same record as the backstop (declaration_reasons with `reads`)."""
    from ouroloop.chat import ChatTurn
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    result = bench.improve(cfg, ".", out=lambda _line: None,
                           researcher=rsi.SessionScript(lambda inputs: [memory_candidate(inputs)], read=False))
    assert result["rounds"][0]["outcome"] == "invalid"
    session = list(ct.Events(activity.root).of("research_session"))[0]
    refused = [c for c in session["calls"] if c["tool"] == "finish" and not c.get("ok")]
    assert refused and "not grounded" in refused[0]["error"] and "failed trajectory" in refused[0]["error"]
    assert "passing trajectory" in refused[0]["error"]
    assert not [r for r in activity.results.all() if "/train_screen" in r.attempt_id]

    class ReadsWhenTold(rsi.SessionScript):
        def session_turn(self, system, messages, tools):
            last = messages[-1]
            if last.get("role") == "tool" and any("not grounded" in str(r.get("output")) for r in last["results"]):
                self.requests.append({"step": "session", "system": system})
                self._queue = [self._reads(self.plan(self.inputs)), [self._declare(self.plan(self.inputs))]]
                return ChatTurn("", self._queue.pop(0), "tool_use")
            return super().session_turn(system, messages, tools)
    cfg2, activity2, _ = prepared_v3(fake_upstream, tmp_path / "told", monkeypatch)
    bench.improve(cfg2, ".", out=lambda _line: None,
                  researcher=ReadsWhenTold(lambda inputs: [memory_candidate(inputs)], read=False))
    grounded = ledger_of(activity2)["m1"]
    assert grounded["admission"]["status"] == "admitted" and grounded["screen"]["net_flips"] is not None
    session = list(ct.Events(activity2.root).of("research_session"))[0]
    assert [c.get("ok") for c in session["calls"] if c["tool"] == "finish"] == [False, True]
    assert session["trajectory_reads"] == 2
    decl = {"predicted_affected": ["rsi.notify_after_update"]}
    backstop = declaration_reasons(decl, reads={"failing": [], "passing": [], "min_failing": 1, "min_passing": 1,
                                                "passing_listed": True})
    assert len(backstop) == 2 and all(r.startswith("ungrounded") for r in backstop)


def test_the_session_lists_passing_attempts_must_not_first_and_reads_the_interface_table(fake_upstream, tmp_path,
                                                                                         monkeypatch):
    """V0_4_PLAN §1.2, §1.3: the session's inputs hold api_errors.json — counted over the latest train measurement,
    no task id in it — and trajectories.json lists the failures, then the passing train attempts with the tasks that
    score a must-not check first and marked; the v0.4 screen's sample comes from the same passing tasks, must-not
    first, and the screen's ledger row carries its fixes and sample regressions."""
    from ouroloop.adapters import automationbench as ab
    marked = "rsi.policy_hold"
    monkeypatch.setattr(ab, "has_must_not", lambda suite, task: task == marked)
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch, screen={"passing_sample": 1})

    class Lister(rsi.SessionScript):
        def _reads(self, candidates):
            from ouroloop.chat import ToolCall
            return [ToolCall("l1", "list_files", {"path": "inputs"}), *super()._reads(candidates)]
    step = Lister(lambda inputs: [memory_candidate(inputs)])
    bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    assert any("api_errors.json" in s and "trajectories.json" in s for s in step.seen)   # in the inputs/ tree
    inputs = step.inputs
    passing = [t for t in inputs["trajectories"] if t["outcome"] == "passing"]
    assert passing[0]["task_id"] == marked and passing[0]["must_not"] is True
    assert all("must_not" not in t for t in passing[1:])
    table = inputs["api_errors"]
    assert table["split"] == "train" and table["attempts"] == len(rsi.TRAIN) and table["endpoints"]
    assert any(e["endpoint"].startswith("PATCH api.hubapi.com/crm/v3/objects/contacts") for e in table["endpoints"])
    assert not any(t in json.dumps(table) for t in rsi.TRAIN)
    m1 = ledger_of(activity)["m1"]
    assert m1["screen"]["sample"] == [marked]                     # one task, the must-not one first
    assert m1["screen"]["fixes"] >= 1 and m1["screen"]["sample_regressions"] == 0
    shown = xp.ledger_projection(xp.ledger_rows(xp.read(activity.root)))[0]["screen"]
    assert (shown["fixes"], shown["sample_regressions"]) == (m1["screen"]["fixes"], 0)     # R reads them too


def test_a_package_tool_candidate_passes_the_draft_chain_and_reaches_the_screen(fake_upstream, tmp_path, monkeypatch):
    """V0_4_PLAN §1.2 (c): a `tools` candidate written in the session — the tool's code and its manifest entry —
    matches its declaration (carrier tool, site its name), passes the whole draft chain in the worker (shape, kinds,
    literals, isolated probe and the interface smoke that calls the tool), is admitted and is screened: a reading on
    its failing set. The fixture's agent never calls the tool, so it fixes nothing and the v0.4 screen keeps it
    from the gate; what is shown is that an interface fix travels the path a text change does."""
    from ouroloop.agent import harness_package as hp
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch,
                                    components=("memory", "skills", "on_finish", "tools"))
    tool = ("def run(ctx, args):\n"
            "    r = ctx.call_tool('api_search', {'query': str(args.get('query') or 'contacts'), 'top_k': 3})\n"
            "    return 'every page read: ' + str(r.output)[:2000]\n")

    def plan(inputs):
        manifest = {**hp.empty_manifest(), "api_version": "harness_package.v2", "tools": [{
            "name": "search_all_pages", "description": "Search the API catalogue and read every page of the answer.",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            "entrypoint": "tools/search_all_pages.py:run"}]}
        return [{"dir": "t1", "files": {"manifest.json": json.dumps(manifest), "tools/search_all_pages.py": tool},
                 "declaration": {"mechanism": "a tool that reads every page of a search", "site": "search_all_pages",
                                 "carrier": "tool", "trigger_condition": "when the agent searches the catalogue"}}]
    bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(plan))
    t1 = ledger_of(activity)["t1"]
    assert t1["declaration"]["carrier"] == "tool" and t1["declaration"]["site"] == "search_all_pages"
    assert t1["drafts"][-1]["ok"] is True and t1["package_ref"]
    assert t1["admission"]["status"] == "admitted"
    assert t1["screen"]["net_flips"] is not None and t1["screen"]["failing"]
    checks = [e for e in ct.Events(activity.root).of("draft_check") if e.get("candidate", "").endswith("/t1")]
    assert checks and checks[-1]["ok"]
    view = hp.load_package(activity.objects, t1["package_ref"])
    assert [t["name"] for t in view.manifest["tools"]] == ["search_all_pages"]


def test_the_baseline_reads_w0s_dev_three_times_and_the_gate_pools_all_three(fake_upstream, tmp_path, monkeypatch):
    """V0_4_PLAN §1.6 (c), M3 §2.1(a) (v0.4): `[bench] control_readings = 3` makes the baseline read W0's dev three
    times, unconditionally and before any gate — three runs of one execution identity, the first the very run a
    one-reading baseline records — and the gate's pooled control counts all three readings."""
    cfg1, activity1, _ = prepared_v3(fake_upstream, tmp_path / "one", monkeypatch)
    bench.baseline(cfg1, ".", out=lambda _line: None)
    one = {r.run for r in activity1.results.all() if r.split == "dev"}
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path / "three", monkeypatch, bench_extra={"control_readings": 3})
    report = bench.baseline(cfg, ".", out=lambda _line: None)
    dev_runs = {r.run for r in activity.results.all() if r.split == "dev"}
    assert len(dev_runs) == 3 and one <= dev_runs                        # the first reading keeps its identity
    assert len(report["arms"]["base"]["control_readings"]) == 3
    work = bench.improvement(bench.open_activity(cfg, ".", out=lambda _line: None),
                             template_ref=cfg["rsi"]["task_plan_ref"])
    pool = work.control_pool(w0)
    assert {r.run for r in pool} == dev_runs and len(pool) == 3 * len(rsi.DEV)
    bench.improve(cfg, ".", out=lambda _line: None,
                  researcher=rsi.SessionScript(lambda inputs: [memory_candidate(inputs)]))
    gate = ledger_of(activity)["m1"]["gate"]
    assert gate["control_readings"] == 3
    with pytest.raises(ValueError, match="control_readings"):
        bench.BenchConfig.load({"bench": {"control_readings": 0}})


def test_a_session_core_bootstraps_the_v04_r0_and_its_sessions_run_under_the_method_text(fake_upstream, tmp_path,
                                                                                         monkeypatch):
    """V0_4_PLAN §1.7 (M4 §1 v0.4): a core whose K carries the coding session bootstraps R0 with the session's method
    text for research.propose (k candidates, interface first, failing and passing trajectories, restraint for an
    over-action, conditions) and the direction-asking note prompt — not the summary call's "Propose at most one
    change"; the session's system text is that method followed by SESSION_FORMAT, which holds the protocol only (the
    inputs, the tools, the caps, the admission's read rule) and no method sentence; a v1 core keeps its R0."""
    from ouroloop.research.session import SESSION_FORMAT, SESSION_READS
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    prompts = P.prompts_of(activity.objects, activity.objects.load(w0, ct.SystemRelease).research_ref)
    assert prompts["research.propose"] == P.PROPOSE_SESSION and prompts["research.diagnose"] == P.DIAGNOSE_SESSION
    assert "at most one change" not in P.PROPOSE_SESSION and "api_errors.json" in P.PROPOSE_SESSION
    assert "restraint" in P.PROPOSE_SESSION and "passing attempt" in P.PROPOSE_SESSION
    assert "Use the round" not in SESSION_FORMAT and "api_errors.json" in SESSION_FORMAT
    step = rsi.SessionScript(lambda inputs: [memory_candidate(inputs)])
    bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    system = next(r["system"] for r in step.requests if r["step"] == "session")
    assert system.startswith(P.PROPOSE_SESSION) and SESSION_FORMAT[:60] in system
    assert SESSION_READS[:40] in system                                  # the preset's read floors are protocol
    from test_harness_loop import v06_prepared
    _, v1, v1_release = v06_prepared(fake_upstream, tmp_path / "v1")
    v1_prompts = P.prompts_of(v1.objects, v1.objects.load(v1_release, ct.SystemRelease).research_ref)
    assert v1_prompts["research.propose"] == P.PROPOSE_PACKAGE and v1_prompts["research.diagnose"] == P.DIAGNOSE_NOTE


# ---------------------------------------------------------------------------- v0.4 review (2026-10-06)
def test_after_an_install_the_session_inputs_see_the_whole_train_split(fake_upstream, tmp_path, monkeypatch):
    """V0_4_PLAN §1.3, §1.6 (review 2026-10-06): after an install the measurement reruns only W0's former failures,
    so the next round's session inputs fill in the train tasks it did not rerun from the activity's earlier
    measurements: the v0.4 passing sample is drawn from every passing train task (the fixture has five train tasks, so
    all five, under the sample's six), R's passing list includes the tasks W0 passed and nobody reran, the screen's
    "before" covers them, and api_errors.json counts over the whole train split. The measurement itself is not
    touched: it still holds only the rerun tasks."""
    captured = []
    original = bench.Improvement.session_inputs

    def recording(self, evidence, bound, **kw):
        out = original(self, evidence, bound, **kw)
        captured.append((list(evidence.get("train") or ()), out))
        return out
    monkeypatch.setattr(bench.Improvement, "session_inputs", recording)
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch, rounds=2)
    result = bench.improve(cfg, ".", out=lambda _line: None,
                           researcher=rsi.SessionScript(lambda inputs: [memory_candidate(inputs)]))
    assert [r["outcome"] for r in result["rounds"]][0] == "installed", result["rounds"]
    assert len(captured) >= 2
    w0_rows, first = captured[0]
    measured, second = captured[1]
    w0_passed = {r.task_id for r in w0_rows if r.success}
    rerun = {r.task_id for r in measured}
    assert rerun == {r.task_id for r in w0_rows if not r.success} and w0_passed and not w0_passed & rerun
    assert set(second.sample) == set(rsi.TRAIN) and len(second.sample) == len(rsi.TRAIN) <= 6
    by_id = {r.attempt_id: r.task_id for r in activity.results.all()}
    assert w0_passed <= {by_id[a] for a in second.passing_attempts}
    assert set(second.before) == set(rsi.TRAIN) and all(second.before[t] for t in w0_passed)
    assert second.api_errors["attempts"] == len(rsi.TRAIN) and first.api_errors["attempts"] == len(rsi.TRAIN)


def test_the_round_hands_the_draft_check_the_literal_index_its_answers_and_the_header_exemptions(
        fake_upstream, tmp_path, monkeypatch):
    """V0_4_PLAN §1.5 (review 2026-10-06), through Improvement.round_inputs: Drafts gets the literal index (literal ->
    its train tasks, not a flat set), the assertion params' literals as `answers`, and the header names among its
    allowed literals — so a candidate naming a value two train tasks share and a column header of one task passes its
    draft check, and one naming a single task's value is refused. Fails if the index is flattened or the headers or
    the answers are dropped."""
    from ouroloop.agent import harness_package as hp
    from ouroloop.research import drafts as drafts_mod
    made = []

    class Recording(drafts_mod.Drafts):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            made.append(self)
    monkeypatch.setattr(drafts_mod, "Drafts", Recording)
    state = {"Legal Hold": {"rsi.notify_once", "rsi.policy_hold"}, "Exclusion Rule": {"rsi.notify_once"},
             "Ada Quist": {"rsi.policy_hold"}}
    monkeypatch.setattr(bench.Improvement, "train_state_index", lambda self: (dict(state), {"Exclusion Rule"}))
    # the grading params' literals: one task's checked amount (the fixture's own params hold no literal)
    from ouroloop.research import diagnosis as dg
    monkeypatch.setattr(dg, "train_literal_index", lambda store: ({"4,250": {"rsi.notify_once"}}, {"Stage"}))
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    text = f"Before you finish, {rsi.FINISH_CHECK_MARK}. Keep Legal Hold rows; read the Exclusion Rule column.\n"
    bench.improve(cfg, ".", out=lambda _line: None,
                  researcher=rsi.SessionScript(lambda inputs: [memory_candidate(inputs, text=text)]))
    drafts = made[0]
    assert isinstance(drafts.literals, dict) and drafts.literals["Legal Hold"] == state["Legal Hold"]
    assert "Exclusion Rule" in drafts.allowed_literals and "Exclusion Rule" not in drafts.answers
    assert drafts.answers == {"4,250"} and "4,250" in drafts.forbidden()      # a number, but one task's answer
    assert "Stage" in drafts.allowed_literals                                   # the params' headers too
    assert "Ada Quist" not in drafts.answers                                   # a state value, not an answer
    assert {"Legal Hold", "Exclusion Rule"}.isdisjoint(drafts.forbidden() - drafts.allowed_literals)
    assert "Ada Quist" in drafts.forbidden()
    m1 = ledger_of(activity)["m1"]
    assert m1["drafts"][0]["ok"] is True and m1["admission"]["status"] == "admitted"
    errors, _ = drafts.check({"manifest.json": json.dumps({**hp.empty_manifest(), "memory": {"enabled": True}}),
                              "memory.md": "Ask Ada Quist first.\n"})
    assert errors and "memory.md:1: 'Ada Quist'" in errors[0]


def test_read_tasks_counts_only_the_reads_the_host_served(fake_upstream, tmp_path):
    """V0_4_PLAN §1.3 (review 2026-10-06): the reads a floor is checked against are the ones the host served — bytes
    returned, no refusal; a dev attempt (AccessDenied), a read past the session's cap, a read past the end of the
    record (0 bytes) and a malformed read meet no floor, and a second read of one attempt counts once."""
    from test_diagnosis import run
    from ouroloop.research.session import SessionLimits, TrajectoryReader
    results, rows, _ = run(fake_upstream, tmp_path, [rsi.TRAIN[1]], "train",
                           worker=rsi.BusinessWorker(ignore_finish_check=True))
    run(fake_upstream, tmp_path, [rsi.DEV[0]], "dev")
    train = rows[0]
    dev = next(r for r in results.all() if r.split == "dev")
    reader = TrajectoryReader(results, tmp_path / "transcripts", SessionLimits(max_trajectory_bytes=100,
                                                                               max_trajectory_bytes_total=150))
    assert reader.read(dev.attempt_id)[1] and reader.read(train.attempt_id, 10 ** 9, 50)[1] is False
    assert reader.read(train.attempt_id, "x", 1)[1]
    assert reader.read_tasks() == {"failing": [], "passing": []}            # denied, 0 bytes, malformed: none served
    assert reader.read(train.attempt_id, 0, 100)[1] is False and reader.read(train.attempt_id, 100, 100)[1]
    assert reader.read_tasks() == {"failing": [train.task_id], "passing": []}
    assert reader.read(train.attempt_id, 100, 40)[1] is False
    assert reader.read_tasks() == {"failing": [train.task_id], "passing": []}   # one attempt, counted once
