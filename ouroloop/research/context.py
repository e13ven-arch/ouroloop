"""M2: what a researcher may read (docs/modules/02_EVIDENCE_TRAINING.md §3).

A whitelist projection, not a filtered object graph: nothing here hands out a runtime, an episode, a file handle,
a provider credential or an oracle. What it does hand out is checked first — every piece of evidence and every
ancestor it was derived from must be inside the scope's partitions, so a summary of a final-split result cannot
enter through the door marked "summary" (CT-04).

The projection is also the reason a researcher's opinion cannot become a fact: text a model produced is recorded
with its producer and its sources, and stays a hypothesis until something outside it says otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..evolve import contracts as ct
from .inspection import ORACLE, Inspection, public


class AccessDenied(RuntimeError):
    """Evidence outside this scope's data boundary, or a resource a researcher may not hold."""


@dataclass
class ResearchContext:
    """Everything one research step may see, and nothing else."""
    scope_id: str
    goal: str
    partitions: tuple[str, ...]
    release_ref: str = ""
    experience_ref: str = ""
    inspection: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)          # aggregate task results of the allowed partitions
    requirements: tuple = ()
    activity: dict = field(default_factory=dict)
    feedback: dict = field(default_factory=dict)         # the catalogue: builders, counts, exclusions
    experience: tuple = ()
    budget: dict = field(default_factory=dict)
    evidence: tuple = ()                                 # the evidence ids this context is built from
    targets: dict = field(default_factory=dict)          # target -> what may be bound there
    notes: tuple = ()
    tools: tuple = ()           # the host's base tools {name, description, parameters}: what package code can call

    def to_json(self) -> dict:
        return {"scope_id": self.scope_id, "goal": self.goal, "partitions": list(self.partitions),
                "release_ref": self.release_ref, "experience_ref": self.experience_ref,
                "inspection": self.inspection, "results": self.results,
                "requirements": [dict(r) for r in self.requirements], "activity": self.activity,
                "feedback": self.feedback, "experience": list(self.experience), "budget": self.budget,
                "evidence": list(self.evidence), "targets": self.targets, "notes": list(self.notes),
                **({"tools": [dict(t) for t in self.tools]} if self.tools else {})}

    def prompt_state(self, max_chars: int = 12000) -> str:
        """The context as the text a research step is given, cut to a bound with the cut recorded."""
        import json
        text = json.dumps(self.to_json(), ensure_ascii=False, indent=1, sort_keys=True, default=str)
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n[... {len(text) - max_chars} characters not shown ...]"


def check_evidence(objects: ct.Objects, refs, allowed: tuple[str, ...]) -> list[str]:
    """Which of these evidence refs are outside the boundary. Ancestors count: an evidence record inherits the
    strictest partition of its inputs, so checking the record itself is enough only because building one already
    combined them."""
    bad = []
    for ref in refs or ():
        try:
            evidence = ct.EvidenceRef.from_json(objects.get(ref))
        except (KeyError, ct.ContractError) as e:
            bad.append(f"{ct.short(ref) if isinstance(ref, str) else ref}: {e}")
            continue
        if not ct.visible(evidence.partition, allowed):
            bad.append(f"{evidence.id}: {evidence.partition} evidence, and this scope reads {list(allowed)}")
    return bad


def make_context(scope_id: str, *, goal: str, release_ref: str, experience_ref: str = "",
                 evidence_refs=(), objects: ct.Objects, partitions: tuple[str, ...] = ("train", "dev"),
                 inspection: Inspection | dict | None = None, results: dict | None = None,
                 requirements=(), feedback: dict | None = None, experience=(), budget: dict | None = None,
                 targets: dict | None = None, notes=(), tools=()) -> ResearchContext:
    """Build the context for one research step, refusing anything outside the boundary.

    The refusal is the useful part: a step that was handed a final-split result and then told not to look at it
    would be relying on the model's restraint, which is not a boundary.
    """
    bad = check_evidence(objects, evidence_refs, partitions)
    if bad:
        raise AccessDenied(f"evidence outside the {list(partitions)} boundary: {bad[:5]}")
    shown = public(inspection) if isinstance(inspection, Inspection) else _scrub(inspection or {})
    # What each target actually did, from the inspection: a target with no observation is left out rather than
    # reported as zero calls (research/inspection.py makes the same distinction).
    activity = {target: entry.get("calls")
                for target, entry in {**shown.get("worker", {}), **shown.get("research", {})}.items()
                if entry.get("observed")}
    return ResearchContext(
        scope_id=scope_id, goal=goal, partitions=tuple(partitions), release_ref=release_ref,
        experience_ref=experience_ref, inspection=shown, results=_scrub(results or {}),
        requirements=tuple(_scrub(dict(r)) for r in requirements), activity=activity,
        feedback=_scrub(feedback or {}), experience=tuple(_scrub(dict(e)) for e in experience),
        budget=dict(budget or {}), evidence=tuple(evidence_refs), targets=dict(targets or {}),
        notes=tuple(notes), tools=tuple(dict(t) for t in tools))


def _scrub(obj):
    """Drop anything whose key names a grader's material or a local path. Belt and braces: the callers already
    build these from projections, and one wrong key here would be a leak that no later check would notice."""
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()
                if not any(word in str(k).lower() for word in (*ORACLE, "path", "secret", "token"))}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    if isinstance(obj, tuple):
        return [_scrub(v) for v in obj]
    return obj


def task_results_summary(results, *, partitions: tuple[str, ...] = ("train", "dev"),
                         splits=None) -> dict:
    """What the researcher sees of the task results: per-task outcomes of the allowed partitions only.

    Success, partial credit and the failure it left behind, with no assertion detail: which requirement the
    grader checked is the grader's, and a researcher who could read it would be reading the answer key
    (docs/DESIGN.md §3.1.5).
    """
    out: dict = {"partitions": list(partitions), "tasks": {}, "excluded": 0}
    for r in results:
        split = r.split if splits is None else splits.splits.get(splits.family_of(r.task_id), r.split)
        if split not in partitions:
            out["excluded"] += 1
            continue
        entry = out["tasks"].setdefault(r.task_id, {"attempts": 0, "success": 0, "partial_credit": [],
                                                   "family": r.family_id, "split": split, "invalid": 0,
                                                   "terminations": {}, "failure_classes": {}})
        entry["attempts"] += 1
        if r.validity != "valid":
            entry["invalid"] += 1
            continue
        entry["success"] += int(bool(r.success))
        entry["partial_credit"].append(round(r.partial_credit, 4))
        entry["terminations"][r.termination] = entry["terminations"].get(r.termination, 0) + 1
        if r.failure_class:
            entry["failure_classes"][r.failure_class] = entry["failure_classes"].get(r.failure_class, 0) + 1
    for entry in out["tasks"].values():
        scored = len(entry["partial_credit"])
        entry["pass_rate"] = round(entry["success"] / scored, 4) if scored else None
        entry["mean_partial_credit"] = round(sum(entry["partial_credit"]) / scored, 4) if scored else None
    return out
