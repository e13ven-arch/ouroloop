"""Conditional credit and the anti-bloat rule (docs/design/IMPROVER.md §10; DESIGN §6.8).

After an install the new W's train reading (the next round's measure) is read against the parent's: for each component
the candidate changed, a task's delta (success now minus success before) counts only where the component showed in the
new attempt (evidence.footprint). The sum is the component's credit; the estimate of (line, cause, kind) learns from it.
A tool, hook or skill that never shows in N credited readings in a row — or that was installed and never earned a
conditioned fix since — becomes a removal candidate the fixed code writes and the next round screens like any other.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..contracts import Record, register
from .components import present, without


@register
@dataclass(frozen=True)
class Credit(Record):
    TYPE = "credit"
    release_ref: str = ""
    component: str = ""                 # kind:name
    kind: str = ""
    task_id: str = ""
    conditioned: bool = False
    delta: float = 0.0


def key(component: dict) -> str:
    return f"{component['kind']}:{component['name']}"


def credit(release_ref: str, parent_reading: dict, child_reading: dict, *, comps: list[dict], evidence) -> list[Credit]:
    out = []
    for comp in comps:
        if comp["kind"] == "test" or comp.get("change") == "deleted":
            continue
        for t in sorted(set(parent_reading) & set(child_reading)):
            row = child_reading[t]
            shown = evidence.footprint(row, comp)
            out.append(Credit(release_ref=release_ref, component=key(comp), kind=comp["kind"], task_id=t,
                              conditioned=shown, delta=float(int(row.success) - int(parent_reading[t].success))))
    return out


def summary(credits: list[Credit]) -> dict[str, dict]:
    """Per component: the conditioned credit (sum of deltas where it showed) and the tasks it showed on."""
    out: dict[str, dict] = {}
    for c in credits:
        slot = out.setdefault(c.component, {"kind": c.kind, "credit": 0.0, "shown": 0, "fixed": [], "broke": []})
        if c.conditioned:
            slot["shown"] += 1
            slot["credit"] = round(slot["credit"] + c.delta, 6)
            if c.delta > 0:
                slot["fixed"].append(c.task_id)
            elif c.delta < 0:
                slot["broke"].append(c.task_id)
    return out


def footprints(reading: dict, package, evidence) -> dict[str, int]:
    """How many attempts of a reading each present tool, hook and skill showed on."""
    return {key(c): sum(1 for r in reading.values() if evidence.footprint(r, c)) for c in present(package)}


def removal_candidates(history: list[dict], package, *, zero_rounds: int) -> list[tuple[dict, dict[str, str]]]:
    """`history`: the credited rounds' records, oldest first, each {footprints: {component: n}, installed: {component:
    credit}}. A present tool, hook or skill that showed on no attempt in the last `zero_rounds` of them, or that an
    install brought in and that has had zero conditioned credit in every one since, is proposed for removal."""
    if len(history) < zero_rounds:
        return []
    recent = history[-zero_rounds:]
    out = []
    for comp in present(package):
        k = key(comp)
        # a round before the component was in the package is not a round it failed to show in
        dead = all(k in (h.get("footprints") or {}) and int(h["footprints"][k]) == 0 for h in recent)
        installed_at = [i for i, h in enumerate(history) if k in (h.get("installed") or {})]
        idle = bool(installed_at) and len(history) - installed_at[-1] >= zero_rounds and all(
            float((h.get("installed") or {}).get(k, 0.0)) <= 0.0 for h in history[installed_at[-1]:])
        if dead or idle:
            out.append(({**comp, "why": "never showed" if dead else "no conditioned credit since its install"},
                        without(package, comp)))
    return out
