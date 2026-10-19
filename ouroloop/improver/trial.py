"""The in-sample trial (docs/design/IMPROVER.md §9.6; DESIGN §6.19, v1.9): R's package run on the studied failing tasks
it claims, before the paid held-out screen, with the results handed back to R.

finance-v8 screened nine packages for $138 and one of them moved a held-out task: R had never run its package on the
failures it studied. The trial is the cheap step in between — each package that passed its free rungs runs once on up
to `trial_max_tasks` of the studied tasks it predicts and the working line fails (run role `trial`, the round's `trial`
account); R reads each task's outcome, the checks that failed, which of its components acted and the session, revises
and finishes again, at most `trial_max_rounds` times and `trial_max_attempts` attempts a round. A package that fixed
fewer than `trial_min_fixes` of them in-sample is not screened (ledger `verified` outcome `no_trial_fix`).

It is feedback and an entry ticket, not acceptance (DESIGN §6.6, §6.7 stand): the trial's fixes credit nothing, move no
estimate and count on no scoreboard; the working line moves only on the held-out screen's confirmed flips. Every task
here is a studied one — R could read it already — and no held-out task is ever tried.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..contracts import Record, register
from ..evaluate.gate import refill_attempts
from .screen import key, latest

SAID_CHARS = 240


@register
@dataclass(frozen=True)
class TrialResult(Record):
    TYPE = "trial"
    candidate: str = ""                 # <tag>:<dir>
    package_ref: str = ""
    iteration: int = 0                  # 0: the package as finished; 1..: after a revision
    tasks: tuple = ()                   # the studied failing tasks tried
    fixed: tuple = ()                   # tasks that pass now where a changed component acted
    failed: tuple = ()                  # tasks that still fail
    unconditioned: tuple = ()           # tasks that pass where no component acted (not a fix)
    attempts: int = 0                   # valid attempts the trial paid for (0 when all were on record)
    outcome: str = "ok"                 # ok | budget | invalid | defect | not_run
    reason: str = ""
    footprints: dict = None             # task -> [component keys that acted]
    sessions: dict = None               # task -> the candidate's session (studied tasks: R may read them)

    def __post_init__(self):
        for name in ("tasks", "fixed", "failed", "unconditioned"):
            object.__setattr__(self, name, tuple(getattr(self, name) or ()))
        object.__setattr__(self, "footprints", {k: list(v) for k, v in dict(self.footprints or {}).items()})
        object.__setattr__(self, "sessions", dict(self.sessions or {}))


def trial_tasks(decl, *, causes, study_failing, n: int) -> list[str]:
    """The studied tasks the working line fails that the package claims: its components' predicted_affected, or, for a
    component that predicts none (a prompt, a skill), the tasks of the causes it names; in the frozen order, `n` at
    most."""
    claimed: set = set()
    by_id = {c.cause_id: c for c in causes}
    for comp in decl.components:
        if comp.predicted_affected:
            claimed |= set(comp.predicted_affected)
        else:
            for cid in comp.cause_ids:
                cause = by_id.get(cid)
                if cause is not None:
                    claimed |= set(cause.tasks)
    return [t for t in study_failing if t in claimed][:max(int(n), 0)]


def trial(activity, *, run_arm, name: str, package, base_release, tasks: list[str], evidence, conditions: list[dict],
          account, retries: int, iteration: int) -> TrialResult:
    """Run the package once on `tasks` (role `trial`); a fix is a pass where one of `conditions` shows (screen.py's
    footprint). Attempts on record under this run (the same package) are read, not paid again."""
    base = dict(candidate=name, package_ref=package.ref(), iteration=int(iteration), tasks=tuple(tasks))
    if not tasks:
        return TrialResult(**base, outcome="not_run", reason="no studied failing task to try")
    before = {r.attempt_id for r in activity.results.all()}

    def run(arm, chosen, retry=0, supersedes=None):
        return run_arm(activity, release=base_release, split="train", candidate=name, role="trial", pairs=chosen,
                       account=account, arm=arm, retry=retry, supersedes=supersedes, package=package,
                       notes=f"improver trial {name}: the studied failing tasks it predicts, iteration {iteration}")
    rows = refill_attempts(run("subject", [(t, 0) for t in tasks]), run, "subject", int(retries))
    after = latest(rows)
    paid = sum(1 for r in after.values() if r.attempt_id not in before)
    if any(r.failure_class == "budget_exhausted" for r in after.values()):
        return TrialResult(**base, attempts=paid, outcome="budget", reason="the budget refused a trial attempt")
    defects = [r for r in after.values() if r.failure_class == "candidate_defect"]
    if defects:
        return TrialResult(**base, attempts=paid, outcome="defect",
                           reason=f"candidate defect on {defects[0].task_id}: "
                                  f"{defects[0].invalid_reason or defects[0].stop}"[:300])
    valid = {t: r for t, r in after.items() if r.validity == "valid"}
    if not valid:
        return TrialResult(**base, attempts=paid, outcome="invalid", reason="no valid reading on the tried tasks")
    fixed, failed, unconditioned, footprints, sessions = [], [], [], {}, {}
    for t in tasks:
        r = valid.get(t)
        if r is None:
            continue
        shown = [key(c) for c in conditions if c.get("name") == "*" or evidence.footprint(r, c)]
        footprints[t] = shown
        sessions[t] = r.evidence or r.attempt_id
        if not r.success:
            failed.append(t)
        elif shown:
            fixed.append(t)
        else:
            unconditioned.append(t)
    return TrialResult(**base, fixed=tuple(fixed), failed=tuple(failed), unconditioned=tuple(unconditioned),
                       attempts=paid, footprints=footprints, sessions=sessions)


def feedback(result: TrialResult, *, evidence, activity, revisions_left: int, attempts_left: int,
             min_fixes: int) -> str:
    """What R reads of a trial: each task's outcome with the checks that failed (train grading: type and params), the
    components that acted, the session to read_trajectory; how many revisions and attempts are left."""
    lines = [f"Trial of candidates/{result.candidate.rpartition(':')[2]} on the studied failing tasks it claims "
             f"(iteration {result.iteration}): {len(result.fixed)} fixed of {len(result.tasks)} tried"
             + (f" ({len(result.unconditioned)} passed where none of your components acted: not a fix)"
                if result.unconditioned else "") + "."]
    rows = {r.attempt_id: r for r in activity.results.all()}
    for t in result.tasks:
        session = result.sessions.get(t)
        if session is None:
            lines.append(f"- {t}: no valid reading")
            continue
        shown = result.footprints.get(t) or []
        if t in result.fixed:
            lines.append(f"- {t}: PASSED; acted: {', '.join(shown)}; session {session}")
            continue
        row = next((r for r in rows.values() if (r.evidence or r.attempt_id) == session), None)
        checks = []
        if row is not None:
            try:
                checks = [f"{c['type']} {c['params']}" for c in evidence.of(row).failed_checks][:6]
            except Exception:  # noqa: BLE001 - a reading without a transcript still reports its outcome
                checks = []
        if t in result.unconditioned:
            lines.append(f"- {t}: passed, but none of your components acted (no footprint): not counted; session "
                         f"{session}")
        else:
            lines.append(f"- {t}: FAILED; acted: {', '.join(shown) or 'nothing of yours'}; checks failed: "
                         f"{'; '.join(checks) or 'see the trajectory'}; session {session}")
    if len(result.fixed) >= int(min_fixes) and int(min_fixes) > 0:
        lines.append("It fixed enough to be screened on the held-out tasks; you may still improve it: edit the files "
                     "and call finish again with the same declaration, or finish as it is.")
    elif revisions_left > 0 and attempts_left > 0:
        lines.append(f"A package that fixes fewer than {min_fixes} of these in the trial is not screened. Read the "
                     f"sessions (read_trajectory), change the files so the mechanism acts and fixes them, and call "
                     f"finish again with the same declaration ({revisions_left} revision(s) left, {attempts_left} "
                     f"trial attempt(s) left this round).")
    else:
        lines.append(f"No revision is left this round; a package that fixed fewer than {min_fixes} of these is not "
                     f"screened.")
    return "\n".join(lines)
