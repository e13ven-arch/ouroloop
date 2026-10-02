"""M4 acceptance (docs/modules/04_RESEARCH_LOOP.md §5): diagnosis, admission and what a round writes down.

A scripted research step is used on purpose: what is checked here is the control flow and the permissions, not
whether a real model can find a good change. That is a business experiment, and a test with a hand-written correct
candidate would not be evidence for it.
"""
import json

import pytest
import rsi

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
                             trainer=rsi.PhraseTrainer())
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
                             trainer=rsi.PhraseTrainer())
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
    experience_mod.append(activity.root, experience_mod.summary_record(
        summary_ref=activity.objects.put_json("change", {"text": "fixes the omission, breaks repeat sending"}),
        source_refs=["experience:x1"], producer_policy_ref="policy:sha256:" + "d" * 64,
        applicable_to={"targets": ["worker.finish"]}, status="regression",
        counterexamples=["rsi.notify_once"]))
    entries = experience_mod.read(activity.root)
    assert experience_mod.requirement_status(entries, "req:rsi.dev_notify_after_update") == "regressed"
    # both facts are retrievable by the next round, and the retrieval can only narrow
    found = retrieve(entries, {"targets": ("worker.finish",), "max_items": 10, "sort": "recency"})
    assert found and found[0]["targets"] == ["worker.finish"]
    assert retrieve(entries, {"statuses": ("regression",), "max_items": 10}) != []
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
    (json.dumps({"proposals": ["add a skill that checks the last step"]}), "malformed")])
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
