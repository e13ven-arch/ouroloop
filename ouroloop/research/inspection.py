"""M1: what is actually assembled and what it actually did (docs/modules/01_POLICY_RUNTIME.md §4).

A researcher's first question is not "what did I propose" but "what ran". So an Inspection is built from the
bound runtime and from traces that exist, and a target with no trace is reported as `observed: false` rather
than as a row of zeros: "asked and never intervened" and "never asked" are different findings, and filling one
in as the other is how a policy that was never reached gets credited with a result.

Everything here is a projection for the researcher: refs and counts, no oracle, no final-split evidence, no
local paths.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..evolve import contracts as ct
from ..policies import REGISTRY, resolve

# Words that name a grader's own material. A field like this must never reach a researcher's context, however it
# got into a record (docs/modules/02_EVIDENCE_TRAINING.md §3).
ORACLE = ("rubric", "assertion", "assertions", "reference_answer", "gold", "world", "snapshot", "scorer")


@dataclass
class Inspection:
    release_ref: str
    profile: str
    authorization: str
    worker: dict = field(default_factory=dict)
    research: dict = field(default_factory=dict)
    prompt: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def targets(self) -> dict:
        return {**self.worker, **self.research}

    def to_json(self) -> dict:
        return {"release_ref": self.release_ref, "profile": self.profile, "authorization": self.authorization,
                "worker": self.worker, "research": self.research, "prompt": self.prompt,
                "coverage": self.coverage, "notes": list(self.notes)}


def inspect(bound, trace_refs: tuple[str, ...] = (), *, objects: ct.Objects | None = None,
            base_prompt: str = "") -> Inspection:
    """The loaded policies, where each is bound, and what the traces show they did."""
    objects = objects or (bound.services.objects if bound.services else None)
    activity = bound.activity
    traces = _traces(objects, trace_refs)
    per_target: dict[str, dict] = {}
    for target, p in bound.policies().items():
        reg = resolve(target)
        mine = [t for t in traces if t.policy_ref == p.ref()]
        entry = {"policy_ref": p.ref(), "kind": p.kind, "site": reg.site, "input_schema": p.input_schema,
                 "output_schema": p.output_schema, "fields": list(ct.SCHEMAS[p.input_schema].get("fields") or ()),
                 "actions": list(ct.actions_of(p.output_schema)), "fallback": p.fallback, "limits": dict(p.limits),
                 "observed": bool(mine) or activity.observed(target)}
        if entry["observed"]:
            entry.update({"calls": activity.calls.get(target, len(mine)),
                          "proposed": dict(activity.proposed.get(target, _count(mine, "proposed_action"))),
                          "executed": dict(activity.executed.get(target, _count(mine, "executed_action"))),
                          "fallbacks": dict(activity.fallbacks.get(target, {})),
                          "overrides": dict(activity.overrides.get(target, {}))})
        per_target[target] = entry
    inspection = Inspection(
        release_ref=bound.release_ref, profile=bound.profile, authorization=bound.authorization,
        worker={t: e for t, e in per_target.items() if REGISTRY[t].owner == "worker"},
        research={t: e for t, e in per_target.items() if REGISTRY[t].owner == "research"},
        prompt=bound.system_prompt(base_prompt)[1],
        coverage=coverage(objects, traces),
        notes=() if traces or activity.calls else ("no trace for this release yet: nothing here says a policy "
                                                  "ran",))
    return inspection


def _traces(objects, refs) -> list[ct.DecisionTrace]:
    out = []
    for ref in refs or ():
        try:
            out.append(objects.load(ref, ct.DecisionTrace))
        except (KeyError, ct.ContractError):
            continue                       # a trace that cannot be read is not evidence of anything
    return out


def _count(traces, attr) -> dict:
    out: dict = {}
    for t in traces:
        key = str(getattr(t, attr))
        out[key] = out.get(key, 0) + 1
    return out


def coverage(objects, traces) -> dict:
    """How often each field of the input schema was actually there, from the observations the traces point at.

    This is the measurement behind the preflight input-coverage check: a decision cannot learn a requirement it
    never saw, and "the field exists in the schema" is not the same as "the field had content at the decision".
    """
    per_schema: dict[str, dict] = {}
    for t in traces:
        fields_of = ct.SCHEMAS.get(t.input_schema, {}).get("fields") or ()
        entry = per_schema.setdefault(t.input_schema, {"n": 0, "present": {f: 0 for f in fields_of},
                                                      "truncated": 0})
        try:
            obs = objects.get(t.observation_ref)
        except (KeyError, ct.ContractError):
            continue
        entry["n"] += 1
        for f in fields_of:
            if obs.get(f) not in (None, "", [], {}, ()):
                entry["present"][f] = entry["present"].get(f, 0) + 1
        if obs.get("truncation"):
            entry["truncated"] += 1
    return {schema: {"n": e["n"], "truncated": e["truncated"],
                     "present": {f: round(c / e["n"], 4) if e["n"] else None for f, c in e["present"].items()}}
            for schema, e in per_schema.items()}


def public(inspection: Inspection) -> dict:
    """The projection a researcher receives: refs, sites, schemas and counts, with anything that names a grader's
    material removed. It is a filter over a record we built, not a promise about a sandbox."""
    return _clean(inspection.to_json())


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()
                if not any(word in str(k).lower() for word in ORACLE) and "path" not in str(k).lower()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj
