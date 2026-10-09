"""The layering (docs/design/IMPROVER.md §6; DESIGN §6.4): which kind of component each cause gets this round.

An estimate per (business line, cause, kind) is learned from the ledger's readings: each screened candidate of that
cause and kind adds its conditioned net flips on the cause's tasks, as a share of them. Before any reading it is the
prior (Settings.priors), the code kinds with the higher uncertainty, so the upper bound puts the exploration on code
first. Two structural rules hold whatever the estimates say: a mechanizable cause gets a code candidate (and twice as
many after a round that delivered none), and the loop's own kinds (L5) only go to the causes that point at the loop.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from ..contracts import Record, register, write_json
from .settings import CODE_KINDS, L5_KINDS, LOOP_CAUSES, TEXT_KINDS, Settings


@dataclass
class Estimate:
    line: str
    cause: str
    kind: str
    fixes: float = 0.0                  # the sum of the observations (conditioned net flips per cause task)
    cost: float = 0.0
    n: int = 0
    mean: float = 0.0
    unc: float = 0.0

    @property
    def upper(self) -> float:
        return round(self.mean + self.unc, 6)


class Estimates:
    """<workspace>/ledger/estimates.json: {line: {cause: {kind: {fixes, cost, n, observations}}}}. Importable across
    activities. A screened candidate is one observation (its conditioned net flips per cause task); an installed one's
    observation becomes its net fixes on the cause's tasks after the install (DESIGN §6.4)."""

    def __init__(self, root, settings: Settings):
        self.path = Path(root) / "ledger" / "estimates.json"
        self.s = settings
        self.table = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def get(self, line: str, cause: str, kind: str) -> Estimate:
        row = ((self.table.get(line) or {}).get(cause) or {}).get(kind) or {}
        prior, unc = self.s.prior(kind)
        n = int(row.get("n", 0))
        fixes = float(row.get("fixes", 0.0))
        # the prior counts as one observation; the uncertainty shrinks with the readings
        return Estimate(line, cause, kind, fixes, float(row.get("cost", 0.0)), n,
                        round((prior + fixes) / (1 + n), 6), round(unc / math.sqrt(1 + n), 6))

    def update(self, line: str, cause: str, kind: str, *, fixes: float, cost: float = 0.0, key: str | None = None) \
            -> None:
        """One observation. Under a `key` (round:dir) it is that candidate's: writing the key again replaces it (a
        resumed step, or the install's reading taking the place of the screen's)."""
        slot = self.table.setdefault(line, {}).setdefault(cause, {}).setdefault(kind, {"fixes": 0.0, "cost": 0.0,
                                                                                     "n": 0})
        seen = slot.setdefault("observations", {}) if key is not None else {}
        if key is not None and key in seen:
            old_fixes, old_cost = seen[key]
            slot["fixes"], slot["cost"] = float(slot["fixes"]) - old_fixes, float(slot["cost"]) - old_cost
            slot["n"] = int(slot["n"]) - 1
        slot["fixes"] = round(float(slot["fixes"]) + float(fixes), 6)
        slot["cost"] = round(float(slot["cost"]) + float(cost or 0.0), 6)
        slot["n"] = int(slot["n"]) + 1
        if key is not None:
            seen[key] = [round(float(fixes), 6), round(float(cost or 0.0), 6)]
        write_json(self.path, self.table)

    def tried(self, line: str, cause: str) -> set[str]:
        return {k for k, v in ((self.table.get(line) or {}).get(cause) or {}).items() if int(v.get("n", 0)) > 0}


@register
@dataclass(frozen=True)
class Assignment(Record):
    TYPE = "assignment"
    cause_id: str = ""
    cause: str = ""
    kind: str = ""
    slots: int = 1
    exploration: bool = False
    reason: str = ""


def kinds_for(cause: str, *, mechanizable: bool, allowed) -> list[str]:
    """The kinds a cause may be assigned, code first: a mechanizable cause tool, hook, then the text kinds; any other
    the text kinds; a cause of the loop adds agent, config, vendor (L5). Memory is never R's: the library's snapshot is
    a fixed candidate. Only kinds K allows."""
    kinds = list(CODE_KINDS) + list(TEXT_KINDS) if mechanizable else list(TEXT_KINDS)
    if cause in LOOP_CAUSES:
        kinds += list(L5_KINDS)
    return [k for k in kinds if k in set(allowed)]


def allocate(causes, estimates: Estimates, *, settings: Settings, line: str, allowed, no_code: set = frozenset(),
             stalled: bool = False, train_available: bool = False) -> tuple[list[Assignment], list[str]]:
    """The round's assignments, and notes for the ledger. `causes` are diagnose.Cause records; `no_code` the causes a
    previous round left without a code candidate at V2 (their code slots double); `stalled`: the code and text kinds
    installed nothing for stall_rounds rounds, when L6 (the model) would be next if the training chain were bound."""
    notes: list[str] = []
    ranked = sorted(causes, key=lambda c: (-len(c.tasks), c.name))
    chosen = [c for c in ranked if kinds_for(c.name, mechanizable=c.mechanizable, allowed=allowed)][:settings.k]
    out: list[Assignment] = []
    l5 = 0
    for c in chosen:
        kinds = kinds_for(c.name, mechanizable=c.mechanizable, allowed=allowed)
        if l5 >= settings.l5_slots:
            kinds = [k for k in kinds if k not in L5_KINDS]
        if not kinds:                   # only L5 kinds, and the round's L5 slots are taken
            continue
        scored = sorted(kinds, key=lambda k: (-estimates.get(line, c.name, k).upper, kinds.index(k)))
        kind = scored[0]
        l5 += kind in L5_KINDS
        out.append(Assignment(cause_id=c.cause_id, cause=c.name, kind=kind,
                              reason=f"upper bound {estimates.get(line, c.name, kind).upper}"))
    # exploration: up to explore_slots assignments take a kind never tried on their cause, code first (the chosen
    # kind itself when it was never tried)
    explored = 0
    for i, a in enumerate(out):
        if explored >= settings.explore_slots:
            break
        c = next(x for x in chosen if x.cause_id == a.cause_id)
        kinds = kinds_for(c.name, mechanizable=c.mechanizable, allowed=allowed)
        untried = [k for k in kinds if k not in estimates.tried(line, c.name)
                   and not (k in L5_KINDS and a.kind not in L5_KINDS and l5 >= settings.l5_slots)]
        if not untried:
            continue
        untried.sort(key=lambda k: (k not in CODE_KINDS, kinds.index(k)))
        kind = a.kind if a.kind in untried else untried[0]
        l5 += (kind in L5_KINDS) - (a.kind in L5_KINDS)
        out[i] = Assignment(cause_id=a.cause_id, cause=a.cause, kind=kind, exploration=True,
                            reason=f"exploration: {kind} never tried on {a.cause}")
        explored += 1
    # the code guarantee
    mech = [c for c in ranked if c.mechanizable and set(CODE_KINDS) & set(allowed)]
    if settings.code_guarantee and mech and out and not any(a.kind in CODE_KINDS for a in out):
        target = mech[0]
        code = next(k for k in CODE_KINDS if k in set(allowed))
        replaced = next((i for i, a in enumerate(out) if a.cause_id == target.cause_id), len(out) - 1)
        out[replaced] = Assignment(cause_id=target.cause_id, cause=target.name, kind=code,
                                   reason=f"code guarantee: {target.name} is mechanizable")
        notes.append(f"code guarantee: {target.name} -> {code}")
    out = [Assignment(cause_id=a.cause_id, cause=a.cause, kind=a.kind, exploration=a.exploration,
                      slots=2 if a.kind in CODE_KINDS and a.cause in set(no_code) else 1,
                      reason=a.reason + ("; two code slots after a round without one" if a.kind in CODE_KINDS
                                         and a.cause in set(no_code) else "")) for a in out]
    if stalled and not train_available:
        notes.append(f"l6_unavailable: no install in {settings.stall_rounds} rounds and the training chain is not "
                     f"bound (stage 5)")
    return out, notes


def stalled(ledger, rounds: int) -> bool:
    """No install in the last `rounds` rounds that ran (DESIGN §6.4's switch rule, read off the ledger)."""
    ran = [r.get("round_id") for r in ledger.rows(stage="allocated")]
    if len(ran) < rounds:
        return False
    recent = set(ran[-rounds:])
    return not any(r.get("round_id") in recent for r in ledger.rows(stage="installed"))
