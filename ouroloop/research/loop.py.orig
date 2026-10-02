"""Research rounds: report -> agent proposes -> framework evaluates, and gates through the round's frozen gate record ->
records."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..evolve import experience, gate
from ..evolve.gate import GateRecord
from ..evolve.replay import EVALUATORS, gold, labelled, predict, right, signature
from ..types import brier
from .agent import Report, Workspace


def build_report(rt, record: GateRecord, max_examples: int = 3) -> Report:
    """Metrics and error groups for every registered point, from the current champion specs, on the round's training
    sessions only: never the record's test sessions, never the calibration sessions, never a held-out task's.

    The calibration split is left out although the agent could learn from it, because a candidate chosen by looking
    at it is chosen on the rows its own thresholds are then fitted on: the Clopper-Pearson bound those thresholds
    carry would no longer hold (Learn-then-Test needs the candidate fixed before the calibration data is seen), and
    the risk the gate reports would be optimistic. What the agent may see is what the round may train on."""
    metrics, clusters = {}, []
    sessions, _ = rt.ledger.sessions()
    for name, p in rt.points.items():
        dev = record.split(labelled(rt, name), sessions, record.cal_frac(name))["train"]
        if not dev:
            continue
        views, preds = predict(rt, p, p.spec, dev)
        correct = [gold(pr) in right(t) for pr, (_, t, _) in zip(preds, dev)]
        metrics[name] = {"n": len(dev), "brier": sum(brier(pr, t) for pr, (_, t, _) in zip(preds, dev)) / len(dev),
                         "accuracy": sum(correct) / len(dev)}
        groups: dict[tuple, list[str]] = defaultdict(list)
        for view, pr, (_, t, _), ok in zip(views, preds, dev, correct):
            if not ok:
                groups[(" or ".join(sorted(right(t))), gold(pr), signature(view))].append(view)
        for (g, pred, sig), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            clusters.append({"id": f"c{len(clusters) + 1}", "point": name, "gold": g, "predicted": pred,
                             "signature": sig, "n": len(items), "examples": items[:max_examples]})
    return Report(metrics, clusters)


@dataclass
class RoundResult:
    report: Report
    record: str = ""                      # the gate record the round went through
    evaluated: list[dict] = field(default_factory=list)

    @property
    def promoted(self) -> list[dict]:
        return [e for e in self.evaluated if e["gate"] == "accepted"]


def run_round(rt, agent, llm, judge, *, evaluators: dict | None = None, context=None,
              record: GateRecord | None = None, seed: int = 0) -> RoundResult:
    """One round through one gate record: `record`, or the open one (gate.open_record). Every candidate goes through
    gate.run in the order proposed. The first whose evaluation uses the record up gives the round's only verdict and
    ends the round; a candidate stopped before that (a no-op, screening, the data checks, a failure before inference)
    leaves the record to the next. With a record used before the round, nothing can be promoted. `context` is a
    dict, or a function returning one at the start of each round (e.g. the current prompt and its task results)."""
    record = record or gate.open_record(rt)
    evaluators = {**EVALUATORS, **(evaluators or {})}
    context = context() if callable(context) else context
    report = build_report(rt, record)
    ws = Workspace(rt, report, experience.read(rt.root), llm, judge, kinds=sorted(evaluators), context=context)
    result = RoundResult(report, record.round)
    target_promoted: dict[str, tuple[str, bool]] = {}
    for cand in agent.step(ws):
        entry = {"candidate": cand.id, "kind": cand.kind, "point": cand.point, "change": cand.change,
                 "hypothesis": cand.hypothesis, "cost": cand.cost, "p_pass": cand.meta.get("p_pass"),
                 "record": record.round}
        fn = evaluators.get(cand.kind)
        if fn is None:
            experience.append(rt.root, {**entry, "gate": "not_evaluated", "verdict": "no evaluator for this kind"})
            continue
        took = record.took
        try:
            g, ev = gate.run(record, lambda: fn(rt, cand, record), seed=seed)
        except Exception as e:  # noqa: BLE001 - a broken candidate must not end the round, unless it used the record
            entry = {**entry, "gate": "failed", "reasons": [f"{type(e).__name__}: {e}"], "verdict": "failed",
                     "used": record.took and not took}
            experience.append(rt.root, entry)
            result.evaluated.append(entry)
            if entry["used"]:
                break
            continue
        if ev.record:
            ev.record(g.outcome, g.reasons)
        judged = g.used and g.outcome in ("accepted", "rejected")     # a verdict of the gate, on fresh evidence
        compared = g.outcome in ("accepted", "rejected", "validated", "conflict")
        entry = {**entry, "gate": g.outcome, "reasons": g.reasons, "n": g.n, "delta": g.delta,
                 "ci": list(g.ci) if g.ci else None, "used": g.used,
                 "verdict": (experience.verdict([b - a for a, b in zip(ev.champion, ev.challenger)]) if compared
                             else "inconclusive" if g.outcome == "inconclusive" else "insufficient"),
                 **({"details": ev.details} if ev.details else {})}
        experience.append(rt.root, entry)
        result.evaluated.append(entry)
        # The judge's calls are scored against the gate: did the candidate it rated actually pass? The gate's verdict
        # is derived from models' answers, so it is for evaluation only (source "gate"), never a training target.
        if cand.meta.get("worth_trying") and judged:
            rt.outcome(cand.meta["worth_trying"], "yes" if g.outcome == "accepted" else "no", source="gate",
                       note="promotion gate")
        if cand.meta.get("pick_target"):
            label, promoted = target_promoted.get(cand.meta["pick_target"], (cand.meta["target"], False))
            target_promoted[cand.meta["pick_target"]] = (label, promoted or g.outcome == "accepted")
        if g.used:
            break               # one verdict per round: "rejected" ends it, and so does any use of the record
    for did, (label, promoted) in target_promoted.items():
        # Only a success says which choice was right; a failure only says this one was not.
        rt.outcome(did, label if promoted else None, source="gate", reward=1.0 if promoted else 0.0,
                   note="promotion gate")
    return result


def run_rounds(rt, agent, llm, judge, *, rounds: int = 5, patience: int = 2, **kw) -> list[RoundResult]:
    """Repeat rounds until `rounds` is reached, the agent has nothing to propose, or `patience` rounds in a row
    promote nothing. Each round opens its own gate record."""
    results, idle = [], 0
    for _ in range(rounds):
        res = run_round(rt, agent, llm, judge, **kw)
        results.append(res)
        if not res.evaluated:
            break
        idle = 0 if res.promoted else idle + 1
        if idle >= patience:
            break
    return results


# ---------------------------------------------------------------------------- the task/research profile's round
# docs/modules/04_RESEARCH_LOOP.md §1. The legacy round above stays exactly as it was: it goes through a gate
# record, a replay evaluator and the champion pointer. This round goes through a full-task receipt and a complete
# release, and the two never borrow each other's authorisation.
from dataclasses import dataclass as _dataclass
from dataclasses import field as _field

from ..evolve import contracts as _ct
from ..evolve import experience as _experience
from ..evolve.execution import Execution, Schedule, execute_experiment
from .experiment import admit, freeze, propose_experiment


@_dataclass
class RoundContext:
    """Everything one worker round is fixed to before it starts."""
    releases: object                      # release.Releases
    objects: _ct.Objects
    scope_id: str
    context: object                       # research.context.ResearchContext
    researcher: object                    # .diagnose(context, prompt) / .propose(context, prompt, reqs, exp)
    bound: object                         # the loaded release (policies.BoundRuntime), whose R runs the steps
    core_ref: str
    eval_template_ref: str
    handlers: dict                        # build / evaluate / install / screen / verify_bound
    experience_ref: str = ""
    experience: tuple = ()
    owner: str = "worker"
    schedule: Schedule = _field(default_factory=Schedule)
    history: tuple = ()                   # proposal signatures this activity has already frozen
    feedback_builders: tuple = ("completion_snapshot@v1",)
    experiment_id: str | None = None
    root: object = None
    # v0.6 (docs/modules/04_RESEARCH_LOOP.md §1–2): the per-task diagnosis input, the pre-freeze draft check, and
    # this round's index in the segment, which the note selection uses to rotate through the failures.
    diagnosis: object = None
    drafts: object = None
    round_index: int = 0


def run_worker_round(round_context: RoundContext, budget) -> _ct.RoundResult:
    """One improvement round: propose, admit, build, evaluate, install, then write down what happened.

    The outcome is the execution's own terminal state, never a gate verdict standing in for it: `accepted` and
    `installed` are different facts, and a round that accepted a candidate it then failed to install has to say
    so (docs/modules/00_CONTRACTS.md §3).
    """
    ctx = round_context
    root = ctx.root or ctx.releases.root
    before = ctx.releases.active()["release_ref"]
    core = ctx.objects.load(ctx.core_ref, _ct.CoreManifest)
    from ..evolve.execution import BudgetError
    from ..providers import ReplyTruncated
    resume = ctx.drafts.pending() if ctx.drafts is not None else None
    try:
        if resume is not None:
            # A draft a restart interrupted: continue it rather than asking R for a new proposal (EX6-12).
            proposal = ctx.objects.load(resume["proposal_ref"], _ct.ExperimentProposal)
            sent_before = len(getattr(ctx.researcher, "requests", ()) or ())
            try:
                drafted = ctx.drafts.resolve(proposal, revise=ctx.researcher.revise,
                                             prompt=ctx.bound.prompts.get("research.propose", ""), resume=resume)
            finally:
                _record_research_steps(ctx, root, before, sent_before, None, stage="revise")
            return _after_drafts(ctx, drafted, proposal, before, core, root, budget, requirements=())
        sent_before = len(getattr(ctx.researcher, "requests", ()) or ())
        proposal_result = propose_experiment(ctx.context, ctx.bound, ctx.owner, researcher=ctx.researcher,
                                            objects=ctx.objects, max_candidates=ctx.schedule.candidates_per_round,
                                            experience=ctx.experience, diagnosis=ctx.diagnosis,
                                            live_budget=(lambda: budget.for_research()) if budget is not None
                                            else None)
        _record_research_steps(ctx, root, before, sent_before, proposal_result)
    except ReplyTruncated as e:
        # The research model's output limit is too small for its replies: every round would be cut the same way,
        # so the activity pauses for the configuration to be fixed (docs/modules/00_CONTRACTS.md §3).
        _experience.append(root, {"kind": "round", "outcome": "infrastructure_failed", "reason": str(e)[:300],
                                  "scope": ctx.scope_id, "release": before})
        return _ct.RoundResult(outcome="infrastructure_failed", release_before=before, release_after=before,
                               reason=str(e)[:300], next_action="repair_runtime")
    except BudgetError as e:
        _experience.append(root, {"kind": "round", "outcome": "budget_exhausted", "reason": str(e)[:300],
                                  "scope": ctx.scope_id, "release": before})
        return _ct.RoundResult(outcome="budget_exhausted", release_before=before, release_after=before,
                               reason=str(e)[:300], next_action="stop")
    if proposal_result.diagnosis is not None:
        # What the notes covered, so the next round rotates to failures not yet read and a report cannot present
        # a partial diagnosis as a complete one (docs/modules/04_RESEARCH_LOOP.md §1).
        _experience.append(root, {**proposal_result.diagnosis, "kind": "diagnosis", "scope": ctx.scope_id,
                                  "release": before})
    if not proposal_result.proposals:
        # The v0.6 driver records a collect request as needs_collection; a v2 round keeps its old outcome. A reply
        # that could not be read (the summary's, or every note's) is an invalid round: R decided nothing, so
        # nothing it might have meant — a collection, a stop — is acted on, and the next round asks again.
        unreadable = getattr(proposal_result, "unreadable", False)
        next_action = "none" if unreadable else proposal_result.next_action
        collect = next_action == "collect" and ctx.diagnosis is not None
        outcome = "invalid" if unreadable else "needs_collection" if collect else "no_proposal"
        _experience.append(root, {"kind": "round", "outcome": outcome, "next_action": next_action,
                                  "reason": proposal_result.reason, "scope": ctx.scope_id, "release": before})
        return _ct.RoundResult(outcome=outcome, release_before=before, release_after=before,
                               reason=proposal_result.reason or next_action,
                               evidence_refs=tuple(proposal_result.evidence_refs), next_action=next_action)
    proposal = proposal_result.proposals[0]
    refused = ctx.drafts.check_texts(proposal) if ctx.drafts is not None else []
    if refused:
        # A prompt change has no draft to revise: a train literal in its text makes the proposal invalid, before a
        # package draft is checked or revised and before anything is frozen (docs/modules/04_RESEARCH_LOOP.md §1,
        # docs/modules/00_CONTRACTS.md §3). A resumed draft needs no check: its proposal passed this one before the
        # draft was opened, and revisions replace only the package patch.
        check = {"check": "literal", "verdict": "block", "detail": "; ".join(refused), "evidence_refs": []}
        _experience.append(root, {"kind": "round", "outcome": "invalid", "reasons": refused, "checks": [check],
                                  "scope": ctx.scope_id, "release": before, "hypothesis": proposal.hypothesis})
        return _ct.RoundResult(outcome="invalid", release_before=before, release_after=before,
                               reason="; ".join(refused)[:300], evidence_refs=tuple(proposal_result.evidence_refs))
    if ctx.drafts is not None and _has_patch(proposal, ctx.objects):
        sent_before = len(getattr(ctx.researcher, "requests", ()) or ())
        try:
            drafted = ctx.drafts.resolve(proposal, revise=ctx.researcher.revise,
                                         prompt=ctx.bound.prompts.get("research.propose", ""))
        except BudgetError as e:
            return _ct.RoundResult(outcome="budget_exhausted", release_before=before, release_after=before,
                                   reason=str(e)[:300], next_action="stop")
        except ReplyTruncated as e:
            # A revision cut at the output limit: the open draft stays open and resumes once the limit is raised.
            _experience.append(root, {"kind": "round", "outcome": "infrastructure_failed", "reason": str(e)[:300],
                                      "scope": ctx.scope_id, "release": before})
            return _ct.RoundResult(outcome="infrastructure_failed", release_before=before, release_after=before,
                                   reason=str(e)[:300], next_action="repair_runtime")
        finally:
            _record_research_steps(ctx, root, before, sent_before, None, stage="revise")
        return _after_drafts(ctx, drafted, proposal, before, core, root, budget,
                             requirements=proposal_result.requirements)
    return _admit_and_run(ctx, proposal, before, core, root, budget, requirements=proposal_result.requirements)


def _record_research_steps(ctx, root, before: str, sent_before: int, proposal_result, *,
                           stage: str = "propose") -> None:
    """Which R this round loaded and what its steps actually sent (docs/modules/06_RESEARCH_CAMPAIGN.md §4): the
    loaded R, the context, each request's step and prompt digest, the bound prompts' digests and the proposals.
    It is how a later report shows that an installed R1 was used, rather than copying its name. An event, not an
    experience row: R never reads its own request log back."""
    sent = list(getattr(ctx.researcher, "requests", ()) or ())[sent_before:]
    if stage != "propose" and not sent:
        return                                   # no revision was asked for
    _ct.Events(root).append(
        "research_steps", scope=ctx.scope_id, release=before, stage=stage,
        research_ref=ctx.objects.load(before, _ct.SystemRelease).research_ref,
        context_ref=ctx.objects.put_json("change", ctx.context.to_json()),
        steps=[{"step": r.get("step"), "prompt_hash": r.get("prompt_hash"), "input_hash": r.get("input_hash")}
               for r in sent if isinstance(r, dict)],
        prompts={t: _ct.digest({"text": text})[:16] for t, text in sorted(ctx.bound.prompts.items())
                 if t.startswith("research.")},
        proposals=[ctx.objects.put(p) for p in proposal_result.proposals] if proposal_result else [],
        budget_seen=getattr(proposal_result, "budget_seen", None),
        # What R returned as Requirements, whole: R's own output from train evidence, auditable afterwards.
        requirements=[dict(r) if isinstance(r, dict) else {"id": str(r)}
                      for r in (getattr(proposal_result, "requirements", ()) or ())] if proposal_result else [])


def _after_drafts(ctx, drafted, proposal, before, core, root, budget, *, requirements) -> _ct.RoundResult:
    if drafted.status != "ready":
        infrastructure = drafted.status == "infrastructure_failed"
        _experience.append(root, {"kind": "round", "outcome": drafted.status, "reason": drafted.reason,
                                  "drafts": drafted.drafts, "scope": ctx.scope_id, "release": before,
                                  "hypothesis": proposal.hypothesis})
        return _ct.RoundResult(outcome=drafted.status, release_before=before, release_after=before,
                               reason=drafted.reason[:300],
                               next_action="repair_runtime" if infrastructure else None)
    # Stored again, so what is evaluated and what may be installed are the same checked package bytes.
    from .experiment import normalize as _normalize
    return _admit_and_run(ctx, _normalize(drafted.proposal, ctx.objects), before, core, root, budget,
                          requirements=requirements)


def _admit_and_run(ctx, proposal, before, core, root, budget, *, requirements) -> _ct.RoundResult:
    admission = admit(proposal, context=ctx.context, core=core, objects=ctx.objects, owner=ctx.owner,
                      checks=tuple(ctx.bound.preflight or ()), history=ctx.history, budget=budget,
                      feedback_builders=ctx.feedback_builders)
    if not admission.ok:
        _experience.append(root, {"kind": "round", "outcome": admission.status, "reasons": admission.reasons,
                                  "checks": admission.checks, "scope": ctx.scope_id, "release": before,
                                  "hypothesis": proposal.hypothesis})
        return _ct.RoundResult(outcome=admission.status, release_before=before, release_after=before,
                               reason="; ".join(admission.reasons)[:300],
                               next_action="collect" if admission.status == "collect" else None)
    experiment = freeze(admission.proposal_ref, objects=ctx.objects, parent_release=before,
                        experience_ref=ctx.experience_ref, core_ref=ctx.core_ref,
                        eval_template_ref=ctx.eval_template_ref, budget_scope=ctx.scope_id,
                        experiment_id=ctx.experiment_id)
    ctx.objects.put(experiment)
    execution = Execution(root, experiment.id)
    outcome = execute_experiment(experiment, ctx.handlers, budget=budget, execution=execution,
                                expected_parent=before)
    after = ctx.releases.active()["release_ref"]
    receipt, installation = outcome.get("receipt"), outcome.get("installation")
    record = _experience.experiment_record(
        experiment_id=experiment.id, proposal_ref=admission.proposal_ref, owner=ctx.owner,
        hypothesis=proposal.hypothesis, changes=proposal.changes,
        requirements=[r.get("id") if isinstance(r, dict) else getattr(r, "id", r)
                      for r in requirements],
        build=outcome.get("build"), trained=bool(proposal.training),
        receipt=receipt, installation=installation, activity=ctx.bound.activity.to_json(),
        release_before=before, release_after=after, costs=budget.remaining() if budget else {},
        evidence_refs=proposal.evidence_refs,
        open_questions=[c["detail"] for c in admission.checks if c["verdict"] == "warn"],
        notes=f"states: {'->'.join(outcome['states'])}")
    _experience.append(root, record)
    return _ct.RoundResult(outcome=outcome["states"][-1], experiment_id=experiment.id, release_before=before,
                           release_after=after, evidence_refs=proposal.evidence_refs,
                           receipt_ref=receipt.ref() if receipt else None,
                           reason="; ".join(getattr(receipt, "reasons", ()) or ())[:300])


def _has_patch(proposal, objects) -> bool:
    from .drafts import patch_of
    return any(patch_of(c, objects) is not None for c in proposal.changes)


def resume_unfinished(root, objects: _ct.Objects, handlers: dict, budget, *, activity=None) -> list:
    """Finish the experiments an interrupted run left mid-way, before any new round starts.

    Each continues from the state it recorded (execution.execute_experiment): nothing is re-proposed, rebuilt that
    was built, or re-run that was run. An experiment with no recorded experiment ref cannot be continued faithfully
    and is closed as cancelled, with the reason, rather than guessed at.
    """
    from ..evolve.execution import unfinished
    results = []
    for entry in unfinished(root):
        execution = Execution(root, entry["experiment_id"])
        if not entry["experiment_ref"] or not objects.has(entry["experiment_ref"]):
            execution.advance("cancelled", reason="cannot resume: the experiment it was admitted with is not on "
                                                  "record")
            results.append(_ct.RoundResult(outcome="cancelled", experiment_id=entry["experiment_id"],
                                           reason="not resumable"))
            continue
        experiment = objects.load(entry["experiment_ref"], _ct.Experiment)
        outcome = execute_experiment(experiment, handlers, budget=budget, execution=execution,
                                     expected_parent=experiment.parent_release)
        proposal = objects.load(experiment.proposal_ref, _ct.ExperimentProposal)
        receipt, installation = outcome.get("receipt"), outcome.get("installation")
        _experience.append(root, _experience.experiment_record(
            experiment_id=experiment.id, proposal_ref=experiment.proposal_ref, owner=proposal.owner,
            hypothesis=proposal.hypothesis, changes=proposal.changes, build=outcome.get("build"),
            receipt=receipt, installation=installation, release_before=experiment.parent_release,
            release_after=(installation.target_release if installation is not None
                           and installation.status == "installed" else experiment.parent_release),
            trained=bool(proposal.training), evidence_refs=proposal.evidence_refs,
            notes=f"resumed from {entry['status']}: {'->'.join(outcome['states'])}"))
        results.append(_ct.RoundResult(
            outcome=outcome["states"][-1] if outcome["states"] else entry["status"], experiment_id=experiment.id,
            release_before=experiment.parent_release,
            release_after=installation.target_release if installation is not None else experiment.parent_release,
            receipt_ref=receipt.ref() if receipt else None, reason=f"resumed from {entry['status']}"))
    return results


# Round results that are markers rather than completed rounds, and the reasons that make an inconclusive round an
# infrastructure one: the loop's stop marker and collection batches are not rounds, and a round that ended on
# infrastructure produced no valid comparison, so a resumed arm or segment runs it again rather than counting it.
ROUND_MARKERS = ("stopped", "collected", "collection_no_progress")
INFRASTRUCTURE_REASONS = ("pairs are incomplete", "evaluator failure", "infrastructure")


def infrastructure_round(row: dict) -> bool:
    outcome, reason = row.get("outcome"), str(row.get("reason") or "")
    return outcome in ("infrastructure_failed", "installation_failed") or (
        outcome == "inconclusive" and any(k in reason for k in INFRASTRUCTURE_REASONS))


def completed_round(row: dict) -> bool:
    return row.get("outcome") not in ROUND_MARKERS and not infrastructure_round(row)


def pause_of(rows: list, stop_reason: str) -> str:
    """Why a run of rounds is paused rather than finished (docs/modules/06_RESEARCH_CAMPAIGN.md §3,
    docs/modules/00_CONTRACTS.md §3), or "": a runtime repair, repeated infrastructure failures, or rounds that
    ran out with the last one lost to infrastructure. A spent budget ends; it does not pause."""
    from .campaign import PAUSES
    if stop_reason in PAUSES:
        return stop_reason
    real = [r for r in rows if r.get("outcome") not in ROUND_MARKERS]
    if real and infrastructure_round(real[-1]) and stop_reason in ("rounds_done", "patience"):
        return "infrastructure"
    return ""


def counters_of(rows) -> tuple[int, int]:
    """The idle and infrastructure-failure counts a run of rounds had reached, from its recorded rows, so a run
    resumed after a stop point or a crash applies patience and the failure limit as an uninterrupted run would.
    A round that is run again — a paused one, or one lost to infrastructure (`completed_round`) — counts toward
    neither: after the repair it has not happened yet."""
    idle = infra = 0
    for row in rows or ():
        if not completed_round(row) or row.get("next_action") in ("collect", "repair_runtime") \
                or row.get("outcome") in ("no_proposal", "needs_collection", "budget_exhausted"):
            continue
        idle = 0 if row.get("outcome") == "installed" else idle + 1
        infra = infra + 1 if row.get("outcome") in ("inconclusive", "installation_failed", "infrastructure_failed") \
            else 0
    return idle, infra


def unanswered_collections(rows, booked: int) -> int:
    """Collection requests in a resumed run's record that no batch answered: a round that asked for more evidence
    just before a stop point, whose batch was not paid for because no round would have read it then."""
    asked = sum(1 for r in rows or () if r.get("next_action") == "collect" and r.get("outcome") not in ROUND_MARKERS)
    return max(asked - booked, 0)


def run_worker_rounds(make_round, budget, *, schedule: Schedule | None = None, root=None, collect=None,
                      collected_before: int = 0, on_result=None, history=(), stop_after: int | None = None,
                      resume_collections=None) -> list:
    """Rounds until one of the fixed stop rules fires (execution.Schedule).

    `make_round(index) -> RoundContext` is called per round, so each round sees the release, the experience and
    the results that the previous one left — which is the whole point of a loop whose versions persist.

    Resuming a run: `history` is its recorded rounds, so patience and the failure limit continue from where it
    stopped; `resume_collections()` finishes the batches it booked and never finished, and a collection request a
    stop point left unanswered is answered before the next round. `stop_after` is a stop point (docs/ROADMAP.md §3):
    after that many rounds the run stops with "stopped_at", the lowest of the stop reasons, so a schedule rule that
    fires at the same moment is still the reason given.
    """
    schedule = schedule or Schedule()
    idle, infra = counters_of(history)
    batches = collected_before

    class _Results(list):
        # Every result is handed to `on_result` as it happens, so a caller that keeps its own record of completed
        # rounds (a campaign arm, a cycle segment) survives a crash in the next round.
        def append(self, result):
            super().append(result)
            if on_result is not None:
                on_result(result)
    results = _Results()

    def stop_now(index: int, proposals: bool = True) -> str:
        reason = schedule.stop(round_index=index, idle=idle, infra_failures=infra, proposals=proposals,
                               affordable=budget.affordable({}) if budget else True)
        if not reason and stop_after is not None and index >= stop_after:
            reason = "stopped_at"
        return reason

    def refused(e: Exception) -> bool:
        """An account refusal (no balance, no quota, a refused key) pauses the run; anything else is not handled
        here. Nothing more can run until a person acts, and an experiment it interrupted stays open, so the
        restart after the repair continues it from its recorded state (resume_unfinished)."""
        from ..providers import is_account_error
        if not is_account_error(e):
            return False
        reason = f"the model provider refuses the account: {str(e)[:240]}"
        results.append(_ct.RoundResult(outcome="infrastructure_failed", reason=reason, next_action="repair_runtime"))
        results.append(_ct.RoundResult(outcome="stopped", reason="repair_runtime", next_action="repair_runtime"))
        return True

    def collected(got: dict) -> bool:
        """Record a batch's outcome; False ends the run (no new valid evidence)."""
        results.append(_ct.RoundResult(outcome="collected" if got["progress"] else "collection_no_progress",
                                       reason=got.get("reason", ""), next_action="collect",
                                       action_ref=got.get("request_ref")))
        if not got["progress"]:
            results.append(_ct.RoundResult(outcome="stopped", reason="collection_no_progress"))
        return bool(got["progress"])

    try:
        for got in (resume_collections() if resume_collections is not None else ()):
            if not collected(got):
                return results
        if collect is not None and unanswered_collections(history, collected_before):
            if batches >= schedule.collection_batches:
                results.append(_ct.RoundResult(outcome="stopped", reason="collection_limit"))
                return results
            after = stop_now(0)
            if after:
                results.append(_ct.RoundResult(outcome="stopped", reason=after))
                return results
            batches += 1
            if not collected(collect(None, batch=batches)):
                return results
    except Exception as e:  # noqa: BLE001 - only an account refusal is handled here; anything else re-raises
        if refused(e):
            return results
        raise

    for index in range(schedule.worker_rounds):
        stop = stop_now(index)
        if stop:
            results.append(_ct.RoundResult(outcome="stopped", reason=stop))
            return results
        try:
            result = run_worker_round(make_round(index), budget)
        except Exception as e:  # noqa: BLE001 - only an account refusal is handled here; anything else re-raises
            if refused(e):
                return results
            raise
        results.append(result)
        if result.outcome == "budget_exhausted":
            results.append(_ct.RoundResult(outcome="stopped", reason="budget_exhausted"))
            return results
        if result.next_action == "repair_runtime":
            # A missing capability (the isolated worker) suspends the activity rather than ending a segment that
            # the schedule would then move past (docs/modules/00_CONTRACTS.md §3).
            results.append(_ct.RoundResult(outcome="stopped", reason="repair_runtime", next_action="repair_runtime"))
            return results
        if result.next_action == "collect" and collect is not None:
            # docs/modules/04_RESEARCH_LOOP.md §4: a request for more evidence is dispatched through the same
            # driver within the frozen batch limit, and the next round rebuilds its context from what came back.
            # A batch is not a candidate: it leaves the idle and failure counts as they were.
            if batches >= schedule.collection_batches:
                results.append(_ct.RoundResult(outcome="stopped", reason="collection_limit"))
                return results
            after = stop_now(index + 1)
            if after:                  # no round would read the batch: do not pay for it
                results.append(_ct.RoundResult(outcome="stopped", reason=after))
                return results
            batches += 1
            try:
                got = collect(result, batch=batches)
            except Exception as e:  # noqa: BLE001 - the booked batch is finished at the restart
                if refused(e):
                    return results
                raise
            result.action_ref = got.get("request_ref")
            if not collected(got):
                return results
            continue
        if result.outcome in ("no_proposal", "needs_collection"):
            results.append(_ct.RoundResult(outcome="stopped", reason=result.outcome))
            return results
        idle = 0 if result.outcome == "installed" else idle + 1
        infra = infra + 1 if result.outcome in ("inconclusive", "installation_failed", "infrastructure_failed") \
            else 0
        stop = stop_now(index + 1)
        if stop:
            results.append(_ct.RoundResult(outcome="stopped", reason=stop))
            return results
    return results
