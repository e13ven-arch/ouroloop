"""Research rounds (PLAN §5.1): report -> agent proposes -> framework evaluates, gates, records."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..evolve import experience
from ..evolve.gate import GateResult, decide, decide_points
from ..evolve.replay import EVALUATORS, gold, labelled, predict, signature, split
from ..types import brier
from .agent import Report, Workspace


def build_report(rt, test_frac: float = 0.15, max_examples: int = 3) -> Report:
    """Dev-split metrics and error groups for every registered point, from the current champion specs."""
    metrics, clusters = {}, []
    for name, p in rt.points.items():
        dev, _ = split(labelled(rt, name), test_frac)
        if not dev:
            continue
        views, preds = predict(rt, p, p.spec, dev)
        correct = [gold(pr) == gold(t) for pr, (_, t, _) in zip(preds, dev)]
        metrics[name] = {"n": len(dev), "brier": sum(brier(pr, t) for pr, (_, t, _) in zip(preds, dev)) / len(dev),
                         "accuracy": sum(correct) / len(dev)}
        groups: dict[tuple, list[str]] = defaultdict(list)
        for view, pr, (_, t, _), ok in zip(views, preds, dev, correct):
            if not ok:
                groups[(gold(t), gold(pr), signature(view))].append(view)
        for (g, pred, sig), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            clusters.append({"id": f"c{len(clusters) + 1}", "point": name, "gold": g, "predicted": pred,
                             "signature": sig, "n": len(items), "examples": items[:max_examples]})
    return Report(metrics, clusters)


@dataclass
class RoundResult:
    report: Report
    evaluated: list[dict] = field(default_factory=list)

    @property
    def promoted(self) -> list[dict]:
        return [e for e in self.evaluated if e["gate"] == "accepted"]


def _gate(ev, min_items: int, seed: int) -> GateResult:
    if ev.rejected:
        return GateResult("rejected", ev.rejected, len(ev.challenger))
    if ev.blocked or ev.insufficient:
        return GateResult("blocked" if ev.blocked else "insufficient_data", ev.blocked or ev.insufficient, 0)
    if ev.per_point:
        return decide_points(ev.per_point, protected_ok=ev.protected_ok, min_items=min_items, seed=seed)
    return decide(ev.champion, ev.challenger, ev.groups, protected_ok=ev.protected_ok,
                  **{"min_items": min_items, "seed": seed, **ev.gate})


def run_round(rt, agent, llm, judge, *, evaluators: dict | None = None, context=None,
              min_items: int = 30, test_frac: float = 0.15, seed: int = 0) -> RoundResult:
    """`context` is a dict, or a function returning one at the start of each round (e.g. the current prompt and
    its task results)."""
    evaluators = {**EVALUATORS, **(evaluators or {})}
    context = context() if callable(context) else context
    report = build_report(rt, test_frac)
    ws = Workspace(rt, report, experience.read(rt.root), llm, judge, kinds=sorted(evaluators), context=context)
    result = RoundResult(report)
    target_promoted: dict[str, tuple[str, bool]] = {}
    for cand in agent.step(ws):
        record = {"candidate": cand.id, "kind": cand.kind, "point": cand.point, "change": cand.change,
                  "hypothesis": cand.hypothesis, "cost": cand.cost, "p_pass": cand.meta.get("p_pass"),
                  "explored": bool(cand.meta.get("explored"))}
        fn = evaluators.get(cand.kind)
        if fn is None:
            experience.append(rt.root, {**record, "gate": "not_evaluated", "verdict": "no evaluator for this kind"})
            continue
        try:
            ev = fn(rt, cand, test_frac=test_frac)
        except Exception as e:  # noqa: BLE001 - a broken candidate must not end the round
            entry = {**record, "gate": "failed", "reasons": [f"{type(e).__name__}: {e}"], "verdict": "failed"}
            experience.append(rt.root, entry)
            result.evaluated.append(entry)
            continue
        g = _gate(ev, min_items, seed)
        if ev.record:
            ev.record(g.outcome, g.reasons)
        if g.outcome == "accepted":
            ev.apply()
        judged = g.outcome in ("accepted", "rejected")
        entry = {**record, "gate": g.outcome, "reasons": g.reasons, "n": g.n, "delta": g.delta,
                 "ci": list(g.ci) if g.ci else None,
                 "verdict": (experience.verdict([b - a for a, b in zip(ev.champion, ev.challenger)])
                             if judged else "insufficient"), **({"details": ev.details} if ev.details else {})}
        experience.append(rt.root, entry)
        result.evaluated.append(entry)
        # The judge's calls become labelled data: did the candidate it rated actually pass the gate?
        if cand.meta.get("worth_trying") and judged:
            rt.outcome(cand.meta["worth_trying"], "yes" if g.outcome == "accepted" else "no", note="promotion gate")
        if cand.meta.get("pick_target"):
            label, promoted = target_promoted.get(cand.meta["pick_target"], (cand.meta["target"], False))
            target_promoted[cand.meta["pick_target"]] = (label, promoted or g.outcome == "accepted")
    for did, (label, promoted) in target_promoted.items():
        # Only a success says which choice was right; a failure only says this one was not.
        rt.outcome(did, label if promoted else None, reward=1.0 if promoted else 0.0, note="promotion gate")
    return result


def run_rounds(rt, agent, llm, judge, *, rounds: int = 5, patience: int = 2, **kw) -> list[RoundResult]:
    """Repeat rounds until `rounds` is reached, the agent has nothing to propose, or `patience` rounds in a row
    promote nothing."""
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
