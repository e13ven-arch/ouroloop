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
    proposal_result = propose_experiment(ctx.context, ctx.bound, ctx.owner, researcher=ctx.researcher,
                                        objects=ctx.objects, max_candidates=ctx.schedule.candidates_per_round,
                                        experience=ctx.experience)
    if not proposal_result.proposals:
        _experience.append(root, {"kind": "round", "outcome": "no_proposal",
                                  "next_action": proposal_result.next_action, "reason": proposal_result.reason,
                                  "scope": ctx.scope_id, "release": before})
        return _ct.RoundResult(outcome="no_proposal", release_before=before, release_after=before,
                               reason=proposal_result.reason or proposal_result.next_action,
                               evidence_refs=tuple(proposal_result.evidence_refs))
    proposal = proposal_result.proposals[0]
    admission = admit(proposal, context=ctx.context, core=core, objects=ctx.objects, owner=ctx.owner,
                      checks=tuple(ctx.bound.preflight or ()), history=ctx.history, budget=budget,
                      feedback_builders=ctx.feedback_builders)
    if not admission.ok:
        _experience.append(root, {"kind": "round", "outcome": admission.status, "reasons": admission.reasons,
                                  "checks": admission.checks, "scope": ctx.scope_id, "release": before,
                                  "hypothesis": proposal.hypothesis})
        return _ct.RoundResult(outcome=admission.status, release_before=before, release_after=before,
                               reason="; ".join(admission.reasons)[:300])
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
                      for r in proposal_result.requirements],
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


def run_worker_rounds(make_round, budget, *, schedule: Schedule | None = None, root=None) -> list:
    """Rounds until one of the fixed stop rules fires (execution.Schedule).

    `make_round(index) -> RoundContext` is called per round, so each round sees the release, the experience and
    the results that the previous one left — which is the whole point of a loop whose versions persist.
    """
    schedule = schedule or Schedule()
    results, idle, infra = [], 0, 0

    def stop_now(index: int, proposals: bool = True) -> str:
        return schedule.stop(round_index=index, idle=idle, infra_failures=infra, proposals=proposals,
                             affordable=budget.affordable({}) if budget else True)

    for index in range(schedule.worker_rounds):
        stop = stop_now(index)
        if stop:
            results.append(_ct.RoundResult(outcome="stopped", reason=stop))
            return results
        result = run_worker_round(make_round(index), budget)
        results.append(result)
        if result.outcome == "no_proposal":
            results.append(_ct.RoundResult(outcome="stopped", reason="no_proposal"))
            return results
        idle = 0 if result.outcome == "installed" else idle + 1
        infra = infra + 1 if result.outcome in ("inconclusive", "installation_failed") else 0
        stop = stop_now(index + 1)
        if stop:
            results.append(_ct.RoundResult(outcome="stopped", reason=stop))
            return results
    return results
