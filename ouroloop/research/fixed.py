"""v0.5: a round whose candidates fixed code declares (docs/design/V0_5_PLAN.md §1.3, §1.4) — the experience library's
snapshot (operator `library`) and a refit method table (operator `refit`).

The candidate is not R's, so there is no session, no note and no revision: the round takes the complete package the
fixed code built, runs the same draft check (shape, kinds, literals, the kit check, the worker's probe and smoke, the
package tests), the same admission (the research policy's preflight list and the core checks), the train-side screen
on the candidate's failing tasks plus the round's passing sample, the same eligibility rule, and — for the eligible
one — the same gate, confirmation and install through M5. Every stage writes the engine ledger, under a candidate id
`<scope>/f<ordinal>/<operator>` and the declaration fixed code wrote, so the record reads like any other round's.

Resumable (2026-10-07 review): a round that an interruption stopped keeps its ordinal (next_ordinal), its candidate
ids and its experiment ids (fixed_experiment_id), so running its phase again writes no stage twice and pays for
nothing twice; its experiment is never one of R's rounds (research.loop.resume_unfinished skips a fixed candidate's
experiment when R's segment resumes, and the fixed phase finishes it).
"""
from __future__ import annotations

from ..agent import harness_package as hp
from ..evolve import contracts as ct
from ..evolve import experience as xp
from .session import (THIN_SCREEN_TASKS, _read_screen, diff_of, eligible, gate_ledger_fields, pick_best)

OPERATORS = ("library", "refit")


def round_ended(experience, ordinal: int) -> dict | None:
    """The terminal row of fixed round `ordinal`, or None while it has not ended: a fixed round writes one `round` row
    when it ends (whatever the outcome, budget_exhausted included); an interruption (a crash, a stop by hand) leaves
    none, and the round is resumed, not started again."""
    return next((e for e in experience or () if e.get("kind") == "round" and e.get("fixed_code")
                 and int(e.get("ordinal") if e.get("ordinal") is not None else -1) == int(ordinal)), None)


def next_ordinal(experience, operator: str | None = None) -> int:
    """The ordinal of the next fixed round: the unfinished round of the same operator when there is one (declared,
    never ended — it is resumed under its own candidate ids and experiment ids), else one past the highest any
    fixed-code row carries (a candidate id is `<scope>/f<ordinal>/<operator>`, unique across the activity)."""
    rows = [e for e in experience or () if e.get("fixed_code")]
    if operator is not None:
        open_ = sorted({int(e.get("ordinal") or 0) for e in rows if e.get("kind") == "ledger"
                        and e.get("stage") == "declared" and str(e.get("candidate") or "").endswith(f"/{operator}")
                        and round_ended(experience, int(e.get("ordinal") or 0)) is None})
        if open_:
            return open_[-1]
    seen = [int(e.get("ordinal") or 0) for e in rows]
    return max(seen) + 1 if seen else 0


def fixed_experiment_id(candidate_id: str) -> str:
    """A fixed candidate's experiment id, derived from its candidate id: the candidate W its screen builds, the run
    identity its screen attempts are recorded under and the Execution its gate keeps are therefore the same on a
    resume, and nothing that was paid for is paid again."""
    from ..benchmark import digest
    return "x_" + digest({"fixed_candidate": candidate_id})[:17]


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
    the round's draft check. -> the round's result, as a coding-session round returns it.

    Called again with the ordinal of a round that has not ended, it writes no stage twice and pays for nothing twice:
    the declaration, the draft check, the admission and a finished screen are read back from the ledger; an
    interrupted screen reruns under the same candidate W (the experiment id is the candidate id's), so the attempts
    on record are found, not paid again; a gate with an Execution on record continues from its recorded state (the
    first reading, the confirmation, the install). Before the first screen attempt, the screens and the gate's
    verification bound must fit the budget. The round ends with one `round` row; a round that has ended answers
    with that row."""
    from ..evolve import experience as _experience
    from ..evolve.execution import BudgetExhausted, Execution
    from ..ledger import new_id
    from .experiment import Admission, normalize
    from .loop import _admit, _execute_admitted
    inputs = ctx.session
    root = ctx.root or ctx.releases.root
    before = ctx.releases.active()["release_ref"]
    core = ctx.objects.load(ctx.core_ref, ct.CoreManifest)
    release = ctx.objects.load(before, ct.SystemRelease)
    events = ct.Events(root)
    objects = ctx.objects
    tag = {"scope": ctx.scope_id, "release": before, "round": ctx.round_index, "ordinal": ordinal, "fixed_code": True}
    experience = _experience.read(root)
    done = round_ended(experience, ordinal)
    if done is not None:
        return ct.RoundResult(outcome=str(done.get("outcome")), release_before=before, release_after=before,
                              experiment_id=done.get("experiment_id"), reason=str(done.get("reason") or "")[:300])
    folded = {e["candidate"]: e for e in xp.ledger_rows(experience)}

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
    screened, admitted, to_screen = [], [], []
    for order, cand in enumerate(candidates):
        cid = f"{ctx.scope_id}/f{ordinal}/{cand['operator']}"
        prior = folded.get(cid) or {}
        decl = cand["declaration"]
        declaration_ref = prior.get("declaration_ref")
        if not declaration_ref:
            declaration_ref = objects.put_json("declaration", {k: decl[k] for k in (
                "mechanism", "mechanism_normalized", "site", "carrier", "predicted_affected", "trigger_condition",
                "differs_from", "notes", "operator", "declared_by")})
            ledger("declared", cid, status="declared", dir=cand["operator"], declaration=decl,
                   declaration_ref=declaration_ref, diff=diff_of(parent, cand["files"]), carrier=decl["carrier"])
        if prior.get("drafts"):
            package_ref = prior.get("package_ref") if prior["drafts"][-1].get("ok") else None
            if package_ref is None:
                continue
        else:
            draft_id = new_id("d")
            events.append("draft_started", scope=ctx.scope_id, draft_id=draft_id, round=ctx.round_index,
                          candidate=cid, fixed=True)
            errors, package_ref = ctx.drafts.check(cand["files"])
            events.append("draft_check", scope=ctx.scope_id, draft_id=draft_id, round=ctx.round_index, revision=0,
                          package=package_ref, ok=not errors, infrastructure=False,
                          errors=[e[:300] for e in errors], fixed=True, candidate=cid)
            events.append("draft_finished", scope=ctx.scope_id, draft_id=draft_id,
                          status="ready" if not errors else "invalid", package=package_ref, fixed=True,
                          candidate=cid)
            ledger("drafted", cid, drafts=[{"revision": 0, "package_ref": package_ref, "ok": not errors}],
                   package_ref=package_ref, status="ready" if not errors else "invalid",
                   reason="; ".join(errors)[:300] if errors else "")
            if errors:
                continue
        experiment_id = fixed_experiment_id(cid)
        if prior.get("admission") is not None:
            if prior["admission"].get("status") != "admitted" or not prior.get("proposal_ref"):
                continue
            proposal = objects.load(prior["proposal_ref"], ct.ExperimentProposal)
            admission = Admission(status="admitted", proposal_ref=prior["proposal_ref"])
        else:
            proposal = ct.ExperimentProposal(
                owner=ctx.owner, hypothesis=f"{decl['mechanism']} ({decl['carrier']} at {decl['site']})"[:2000],
                changes=({"target": "worker.harness", "kind": "workflow",
                          "change": {"builtin": hp.load_package(objects, package_ref).api_version,
                                     "package_ref": package_ref}},),
                expected=f"fixes {decl['predicted_affected'][:20]}"[:1000],
                abandon_if=decl["trigger_condition"][:500], origin="fixed", declaration_ref=declaration_ref,
                notes=decl.get("detail", "")[:500])
            proposal = normalize(proposal, objects)
            admission = _admit(ctx, proposal, core, budget)
            ledger("admitted", cid, status=admission.status, reasons=admission.reasons,
                   proposal_ref=admission.proposal_ref)
            if not admission.ok:
                continue
        cand_row = {"id": cid, "proposal": proposal, "admission": admission}
        admitted.append(cand_row)
        screen = prior.get("screen")
        if screen is not None and screen.get("outcome") not in (None, "interrupted"):
            screened.append({**screen, "candidate": cid, "order": order, "recorded": True,
                             "experiment_id": prior.get("experiment_id") or experiment_id,
                             "proposal_ref": admission.proposal_ref})
        else:
            failing = [t for t in cand["failing"] if t in set(inputs.train_tasks) and t in failed_now]
            tasks = failing + [t for t in inputs.sample if t not in failing]
            to_screen.append((cand_row, {
                "candidate": cid, "order": order, "tasks": tasks, "failing": failing,
                "sample": [t for t in tasks if t not in failing], "thin_screen": len(failing) < THIN_SCREEN_TASKS,
                "net_flips": None, "cost_per_attempt": None, "before": {}, "after": {}, "candidate_ref": None,
                "experiment_id": experiment_id, "proposal_ref": admission.proposal_ref}))
    attempts = sum(len(e["tasks"]) for _c, e in to_screen if e["failing"])
    if attempts and budget is not None:
        # before the first screen attempt: the screens and the gate's own verification bound must fit, or the round
        # ends here, budget_exhausted, with nothing paid (as a coding-session round checks)
        verify = dict((ctx.handlers or {}).get("verify_bound") or {})
        need = {"model_calls": attempts + int(verify.get("model_calls") or 0),
                "tool_calls": attempts + int(verify.get("tool_calls") or 0)}
        if verify.get("usd") is not None:
            need["usd"] = round(float(verify["usd"]) + attempts * float(verify.get("usd_per_attempt") or 0.0), 6)
        if not budget.affordable(need):
            return ended("budget_exhausted", f"the fixed round's screens ({attempts} attempts) and the gate's "
                                             f"verification bound {verify} do not fit the budget", "stop")
    try:
        for cand_row, entry in to_screen:
            spent = False
            if not entry["failing"]:
                entry["reason"] = ("no failing train task to screen on: none of its tasks failed in the latest "
                                   "measurement")
            else:
                try:
                    entry["candidate_ref"] = inputs.build(cand_row["proposal"], entry["experiment_id"])
                except Exception as e:  # noqa: BLE001 - a candidate that does not build has no reading: invalid
                    if isinstance(e, BudgetExhausted):
                        raise
                    entry["reason"] = f"build: {type(e).__name__}: {e}"[:300]
                else:
                    spent = _read_screen(entry, inputs.screen_arm(entry["candidate_ref"], entry["tasks"]),
                                         inputs.before)
            events.append("train_screen", scope=ctx.scope_id, round=ctx.round_index, ordinal=ordinal,
                          candidate=cand_row["id"], tasks=entry["tasks"], net_flips=entry["net_flips"],
                          **{k: entry[k] for k in ("fixes", "sample_regressions") if k in entry},
                          cost_per_attempt=entry["cost_per_attempt"], thin_screen=entry["thin_screen"],
                          attempts=entry.get("attempts") or [], fixed=True,
                          **({"error": entry["reason"]} if entry.get("reason") else {}))
            screened.append(entry)
            if spent:
                raise BudgetExhausted(f"the screen of {cand_row['id']} ran out of budget: {entry['reason']}")
    except BudgetExhausted as e:
        for s in screened:
            if not s.get("recorded"):
                s["outcome"] = "interrupted"
                ledger("screened", s["candidate"], **_screen_fields(s))
        return ended("budget_exhausted", str(e), "stop")
    screened.sort(key=lambda s: s["order"])
    best = next((s for s in screened if s.get("recorded") and s.get("outcome") == "selected"), None)
    if best is None:
        best = pick_best([s for s in screened if not s.get("recorded")], inputs.screen)
    for s in screened:
        if s.get("recorded"):
            continue
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
    experiment_id = best["experiment_id"]
    execution = Execution(root, experiment_id)
    recorded_ref = next((h.get("experiment") for h in execution.state()["history"] if h.get("experiment")), None)
    if recorded_ref and objects.has(recorded_ref):
        result = _resume_execution(ctx, root, budget, execution, objects.load(recorded_ref, ct.Experiment))
    else:
        result = _execute_admitted(ctx, chosen["admission"], chosen["proposal"], before, root, budget, requirements=(),
                                   experiment_id=experiment_id)
    if not (folded.get(chosen["id"]) or {}).get("gate"):
        ledger("gated", chosen["id"], **gate_ledger_fields(result.outcome, result.receipt_ref, objects))
    _experience.append(root, {"kind": "round", "outcome": result.outcome, "reason": (result.reason or "")[:300],
                              **tag, "experiment_id": experiment_id})
    return result


def _resume_execution(ctx, root, budget, execution, experiment) -> ct.RoundResult:
    """A fixed round's gate that an interruption left on record: continued from its recorded state by the one loop
    that runs every experiment (execution.execute_experiment), its experiment record written once."""
    from ..evolve import experience as _experience
    from ..evolve.execution import execute_experiment
    outcome = execute_experiment(experiment, ctx.handlers, budget=budget, execution=execution,
                                 expected_parent=experiment.parent_release)
    receipt, installation = outcome.get("receipt"), outcome.get("installation")
    if receipt is None:
        recorded = next((h.get("receipt") for h in reversed(execution.state()["history"]) if h.get("receipt")), None)
        if recorded:
            receipt = ctx.handlers["load_receipt"](recorded)
    if not any(e.get("kind") == "experiment" and e.get("experiment") == experiment.id
               for e in _experience.read(root)):
        proposal = ctx.objects.load(experiment.proposal_ref, ct.ExperimentProposal)
        _experience.append(root, _experience.experiment_record(
            experiment_id=experiment.id, proposal_ref=experiment.proposal_ref, owner=proposal.owner,
            hypothesis=proposal.hypothesis, changes=proposal.changes, build=outcome.get("build"), receipt=receipt,
            installation=installation, release_before=experiment.parent_release,
            release_after=ctx.releases.active()["release_ref"], trained=bool(proposal.training),
            evidence_refs=proposal.evidence_refs, notes=f"resumed: {'->'.join(outcome['states'])}"))
    final = outcome["states"][-1] if outcome["states"] else execution.state()["status"]
    return ct.RoundResult(outcome=final, experiment_id=experiment.id, release_before=experiment.parent_release,
                          release_after=ctx.releases.active()["release_ref"],
                          receipt_ref=receipt.ref() if receipt else None, reason=f"resumed: {final}")


def _screen_fields(s: dict) -> dict:
    return {**{k: s.get(k) for k in ("tasks", "failing", "sample", "before", "after", "net_flips", "cost_per_attempt",
                                     "thin_screen", "outcome", "reason", "lost_to", "experiment_id", "proposal_ref")},
            **{k: s[k] for k in ("fixes", "sample_regressions") if k in s}}
