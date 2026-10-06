"""v0.5: a round whose candidates fixed code declares (docs/design/V0_5_PLAN.md §1.3, §1.4) — the experience library's
snapshot (operator `library`) and a refit method table (operator `refit`).

The candidate is not R's, so there is no session, no note and no revision: the round takes the complete package the
fixed code built, runs the same draft check (shape, kinds, literals, the kit check, the worker's probe and smoke, the
package tests), the same admission (the research policy's preflight list and the core checks), the train-side screen
on the candidate's failing tasks plus the round's passing sample, the same eligibility rule, and — for the eligible
one — the same gate, confirmation and install through M5. Every stage writes the engine ledger, under a candidate id
`<scope>/f<ordinal>/<operator>` and the declaration fixed code wrote, so the record reads like any other round's.
"""
from __future__ import annotations

from ..agent import harness_package as hp
from ..evolve import contracts as ct
from ..evolve import experience as xp
from .session import (THIN_SCREEN_TASKS, _read_screen, diff_of, eligible, gate_ledger_fields, pick_best)

OPERATORS = ("library", "refit")


def next_ordinal(experience) -> int:
    """The next fixed round's ordinal: one past the highest any fixed-code row carries (a candidate id is
    `<scope>/f<ordinal>/<operator>`, unique across the activity)."""
    seen = [int(e.get("ordinal") or 0) for e in experience or () if e.get("fixed_code")]
    return max(seen) + 1 if seen else 0


def fixed_candidate(*, operator: str, files: dict, mechanism: str, site: str, carrier: str, failing,
                    trigger_condition: str, notes: str = "") -> dict:
    """A candidate fixed code declares: its complete package files, its declaration and the train tasks its screen
    reads besides the passing sample."""
    if operator not in OPERATORS:
        raise ValueError(f"operator {operator!r}: one of {list(OPERATORS)}")
    return {"operator": operator, "files": dict(files), "failing": list(dict.fromkeys(failing)),
            "declaration": {"mechanism": mechanism, "mechanism_normalized": mechanism.lower(), "site": site,
                            "carrier": carrier, "predicted_affected": list(dict.fromkeys(failing)),
                            "trigger_condition": trigger_condition, "differs_from": [], "notes": [],
                            "operator": operator, "declared_by": "fixed code", "detail": notes[:500]}}


def run_fixed_round(ctx, budget, candidates: list, *, ordinal: int) -> ct.RoundResult:
    """One fixed-code round (see the module). `ctx` is a RoundContext whose `session` holds the round's screen inputs
    (research.session.SessionInputs: build, screen_arm, sample, before, train_tasks, screen) and whose `drafts` is
    the round's draft check. -> the round's result, as a coding-session round returns it."""
    from ..evolve import experience as _experience
    from ..evolve.execution import BudgetExhausted
    from ..ledger import new_id
    from .experiment import normalize
    from .loop import _admit, _execute_admitted
    inputs = ctx.session
    root = ctx.root or ctx.releases.root
    before = ctx.releases.active()["release_ref"]
    core = ctx.objects.load(ctx.core_ref, ct.CoreManifest)
    release = ctx.objects.load(before, ct.SystemRelease)
    events = ct.Events(root)
    objects = ctx.objects
    tag = {"scope": ctx.scope_id, "release": before, "round": ctx.round_index, "ordinal": ordinal, "fixed_code": True}

    def ended(outcome: str, reason: str, next_action: str | None = None) -> ct.RoundResult:
        _experience.append(root, {"kind": "round", "outcome": outcome, "reason": reason[:300], **tag,
                                  **({"next_action": next_action} if next_action else {})})
        return ct.RoundResult(outcome=outcome, release_before=before, release_after=before, reason=reason[:300],
                              next_action=next_action)

    def ledger(stage: str, candidate: str, **fields) -> None:
        _experience.append(root, xp.ledger_row(stage, candidate, **tag, research_ref=release.research_ref, **fields))

    parent_run, parent_tests = hp.files_of(objects, ctx.bound.package.ref)
    parent = {**parent_run, **parent_tests}
    failed_now = {t for t, ok in inputs.before.items() if not ok}
    screened, admitted = [], []
    for order, cand in enumerate(candidates):
        cid = f"{ctx.scope_id}/f{ordinal}/{cand['operator']}"
        decl = cand["declaration"]
        declaration_ref = objects.put_json("declaration", {k: decl[k] for k in (
            "mechanism", "mechanism_normalized", "site", "carrier", "predicted_affected", "trigger_condition",
            "differs_from", "notes", "operator", "declared_by")})
        ledger("declared", cid, status="declared", dir=cand["operator"], declaration=decl,
               declaration_ref=declaration_ref, diff=diff_of(parent, cand["files"]), carrier=decl["carrier"])
        draft_id = new_id("d")
        events.append("draft_started", scope=ctx.scope_id, draft_id=draft_id, round=ctx.round_index, candidate=cid,
                      fixed=True)
        errors, package_ref = ctx.drafts.check(cand["files"])
        events.append("draft_check", scope=ctx.scope_id, draft_id=draft_id, round=ctx.round_index, revision=0,
                      package=package_ref, ok=not errors, infrastructure=False, errors=[e[:300] for e in errors],
                      fixed=True, candidate=cid)
        events.append("draft_finished", scope=ctx.scope_id, draft_id=draft_id,
                      status="ready" if not errors else "invalid", package=package_ref, fixed=True, candidate=cid)
        ledger("drafted", cid, drafts=[{"revision": 0, "package_ref": package_ref, "ok": not errors}],
               package_ref=package_ref, status="ready" if not errors else "invalid",
               reason="; ".join(errors)[:300] if errors else "")
        if errors:
            continue
        proposal = ct.ExperimentProposal(
            owner=ctx.owner, hypothesis=f"{decl['mechanism']} ({decl['carrier']} at {decl['site']})"[:2000],
            changes=({"target": "worker.harness", "kind": "workflow",
                      "change": {"builtin": hp.load_package(objects, package_ref).api_version,
                                 "package_ref": package_ref}},),
            expected=f"fixes {decl['predicted_affected'][:20]}"[:1000], abandon_if=decl["trigger_condition"][:500],
            origin="fixed", declaration_ref=declaration_ref, notes=decl.get("detail", "")[:500])
        proposal = normalize(proposal, objects)
        admission = _admit(ctx, proposal, core, budget)
        ledger("admitted", cid, status=admission.status, reasons=admission.reasons, proposal_ref=admission.proposal_ref)
        if not admission.ok:
            continue
        failing = [t for t in cand["failing"] if t in set(inputs.train_tasks) and t in failed_now]
        tasks = failing + [t for t in inputs.sample if t not in failing]
        entry = {"candidate": cid, "order": order, "tasks": tasks, "failing": failing,
                 "sample": [t for t in tasks if t not in failing], "thin_screen": len(failing) < THIN_SCREEN_TASKS,
                 "net_flips": None, "cost_per_attempt": None, "before": {}, "after": {}, "candidate_ref": None,
                 "experiment_id": ctx.experiment_id or new_id("x"), "proposal_ref": admission.proposal_ref}
        admitted.append({"id": cid, "proposal": proposal, "admission": admission, "entry": entry})
    try:
        for cand in admitted:
            entry = cand["entry"]
            spent = False
            if not entry["failing"]:
                entry["reason"] = ("no failing train task to screen on: none of its tasks failed in the latest "
                                   "measurement")
            else:
                try:
                    entry["candidate_ref"] = inputs.build(cand["proposal"], entry["experiment_id"])
                except Exception as e:  # noqa: BLE001 - a candidate that does not build has no reading: invalid
                    if isinstance(e, BudgetExhausted):
                        raise
                    entry["reason"] = f"build: {type(e).__name__}: {e}"[:300]
                else:
                    spent = _read_screen(entry, inputs.screen_arm(entry["candidate_ref"], entry["tasks"]),
                                         inputs.before)
            events.append("train_screen", scope=ctx.scope_id, round=ctx.round_index, ordinal=ordinal,
                          candidate=cand["id"], tasks=entry["tasks"], net_flips=entry["net_flips"],
                          **{k: entry[k] for k in ("fixes", "sample_regressions") if k in entry},
                          cost_per_attempt=entry["cost_per_attempt"], thin_screen=entry["thin_screen"],
                          attempts=entry.get("attempts") or [], fixed=True,
                          **({"error": entry["reason"]} if entry.get("reason") else {}))
            screened.append(entry)
            if spent:
                raise BudgetExhausted(f"the screen of {cand['id']} ran out of budget: {entry['reason']}")
    except BudgetExhausted as e:
        for s in screened:
            s["outcome"] = "interrupted"
            ledger("screened", s["candidate"], **_screen_fields(s))
        return ended("budget_exhausted", str(e), "stop")
    best = pick_best(screened, inputs.screen)
    for s in screened:
        if s["net_flips"] is None:
            s["outcome"] = "lost" if s.get("lost") else "invalid"
        elif best is not None and s is best:
            s["outcome"] = "selected"
        elif eligible(s, inputs.screen):
            s["outcome"] = "not_selected"
        else:
            s["outcome"] = "screened_out"
        ledger("screened", s["candidate"], **_screen_fields(s))
    if not admitted:
        return ended("invalid", "the fixed candidate did not pass its draft check or admission", "none")
    if best is None:
        return ended("screened_out", "; ".join(f"{s['candidate'].rpartition('/')[2]}: fixes {s.get('fixes')} sample "
                                               f"regressions {s.get('sample_regressions')} ({s.get('reason') or ''})"
                                               for s in screened)[:300] or "no screen reading", "none")
    chosen = next(c for c in admitted if c["id"] == best["candidate"])
    result = _execute_admitted(ctx, chosen["admission"], chosen["proposal"], before, root, budget, requirements=(),
                               experiment_id=best["experiment_id"])
    ledger("gated", chosen["id"], **gate_ledger_fields(result.outcome, result.receipt_ref, objects))
    return result


def _screen_fields(s: dict) -> dict:
    return {**{k: s.get(k) for k in ("tasks", "failing", "sample", "before", "after", "net_flips", "cost_per_attempt",
                                     "thin_screen", "outcome", "reason", "lost_to", "experiment_id", "proposal_ref")},
            **{k: s[k] for k in ("fixes", "sample_regressions") if k in s}}
