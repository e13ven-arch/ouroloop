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
