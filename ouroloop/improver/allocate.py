"""The focus list (docs/design/IMPROVER.md §6; DESIGN §6.4, v1.2): what the round suggests R works on.

An estimate per (business line, cause, kind) is learned from the ledger's readings: each screened candidate's
conditioned net flips on its causes' held-out tasks, as a share of them, and after an acceptance the credit's reading.
Before any reading it is the prior (Settings.priors), the code kinds with the higher uncertainty, so the score puts code
first. The list is advice: R decides what the package holds, over several causes and with several carriers; the hard
rules (a tool or hook when a mechanizable cause exists, at most l5_per_round changes of the loop's own kinds) are V0's
and the ledger's (verify/static.py, round.py), not slots.
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
class Focus(Record):
    """The round's focus list as R reads it (inputs/focus.json)."""
    TYPE = "focus"
    ranked: tuple = ()                  # ({cause_id, name, tasks, mechanizable, kinds: [{kind, score, n, tried}]})
    require_code: bool = False          # a mechanizable cause is diagnosed: the package holds a tool or a hook
    l5_suggested: bool = False          # a cause of the loop is diagnosed: agent, config or vendor may be the carrier
    l6_suggested: bool = False          # stalled, and the training chain is bound (stage 5; False until then)
    audit_suggested: bool = False       # text-only causes hold audit_hook_share of the cases: an aux audit hook fits
    text_only_share: float = 0.0        # the share of cases in causes with text carriers only (v1.3)
    notes: tuple = ()
    merge: tuple = ()                   # v1.8: the frontier entries this round asks R to merge ({entry, round, dir,
                                        # mechanism, kind, fixes, regressions, attempts, cause_ids, heuristic_terms})

    def __post_init__(self):
        object.__setattr__(self, "ranked", tuple(dict(r) for r in self.ranked or ()))
        object.__setattr__(self, "notes", tuple(self.notes or ()))
        object.__setattr__(self, "merge", tuple(dict(m) for m in self.merge or ()))


def kinds_for(cause: str, *, mechanizable: bool, allowed) -> list[str]:
    """The kinds a cause may take, code first: a mechanizable cause tool, hook, then the text kinds; any other the
    text kinds; a cause of the loop adds agent, config, vendor (L5). Memory is never R's: the library's snapshot is a
    fixed candidate. Only kinds K allows."""
    # v1.11 (DESIGN §6.21): a hook (a hold before a write, with the evidence of this run) carries a document's rule
    # or an excluded item as well as a mechanizable check — every cause may take one, before the text kinds
    kinds = list(CODE_KINDS) + list(TEXT_KINDS) if mechanizable else ["hook"] + list(TEXT_KINDS)
    if cause in LOOP_CAUSES:
        kinds += list(L5_KINDS)
    return [k for k in kinds if k in set(allowed)]


def focus(causes, estimates: Estimates, archive, *, settings: Settings, line: str, allowed, hide=frozenset(),
          stalled: bool = False, train_available: bool = False) -> Focus:
    """The focus list: the causes by size, each with its kinds by score (mean + uncertainty, ties by the kind order,
    code first) and what the archive holds of earlier candidates of that cause and kind (held-out task ids of `hide`
    counted, never named)."""
    ranked = []
    for c in sorted(causes, key=lambda c: (-len(c.tasks), c.name)):
        kinds = kinds_for(c.name, mechanizable=c.mechanizable, allowed=allowed)
        if not kinds:
            continue
        earlier = archive.for_prompt(line, c.name, hide=hide) if archive is not None else []
        rows = []
        for k in sorted(kinds, key=lambda k: (-estimates.get(line, c.name, k).upper, kinds.index(k))):
            e = estimates.get(line, c.name, k)
            rows.append({"kind": k, "score": e.upper, "mean": e.mean, "n": e.n,
                         "tried": [x for x in earlier if k in str(x.get("kind") or "").split("+")]})
        ranked.append({"cause_id": c.cause_id, "name": c.name, "tasks": list(c.tasks), "mechanizable": c.mechanizable,
                       "kinds": rows})
    notes = []
    mech = any(c.mechanizable for c in causes) and bool(set(CODE_KINDS) & set(allowed))
    if settings.require_code_component and mech:
        notes.append("a mechanizable cause is diagnosed: the package holds at least one tool or hook, or the ledger "
                     "records no_code_candidate")
    l5 = any(c.name in LOOP_CAUSES for c in causes) and bool(set(L5_KINDS) & set(allowed))
    if l5:
        notes.append(f"a cause of the loop is diagnosed: the agent, a config knob or a vendored file may change "
                     f"(at most {settings.l5_per_round} such change a round)")
    l6 = bool(stalled and train_available)
    if stalled and not train_available:
        notes.append(f"l6_unavailable: no acceptance in {settings.stall_rounds} rounds and the training chain is not "
                     f"bound (stage 5)")
    # v1.3: causes with text carriers only (not mechanizable, not of the loop) — a hook that audits the agent's work
    # against the request with the aux model covers several of them at once
    total = sum(len(c.cases) for c in causes)
    text_only = [c for c in causes if not c.mechanizable and c.name not in LOOP_CAUSES]
    share = round(sum(len(c.cases) for c in text_only) / total, 4) if total else 0.0
    audit = bool(text_only) and share >= float(settings.audit_hook_share) and "hook" in set(allowed)
    if audit:
        notes.insert(0, f"audit hook suggested: {share:.0%} of the cases are in causes with text carriers only "
                        f"({', '.join(c.name for c in text_only)}); one on_finish or after_turn hook that calls the "
                        f"aux model (uses_model) to check the work against the request covers them together")
    return Focus(ranked=tuple(ranked), require_code=bool(settings.require_code_component and mech), l5_suggested=l5,
                 l6_suggested=l6, audit_suggested=audit, text_only_share=share, notes=tuple(notes))


def stalled(ledger, rounds: int) -> bool:
    """No acceptance in the last `rounds` rounds that focused (DESIGN §6.4's switch rule, read off the ledger)."""
    ran = [r.get("round_id") for r in ledger.rows(stage="focused")]
    if len(ran) < rounds:
        return False
    recent = set(ran[-rounds:])
    return not any(r.get("round_id") in recent for r in ledger.rows(stage="accepted"))
