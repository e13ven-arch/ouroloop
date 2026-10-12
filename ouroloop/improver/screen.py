"""V3, the held-out screen, which is the activity's acceptance (docs/design/IMPROVER.md §9; DESIGN §6.6, §6.7, v1.2).

V3 is the first paid rung and the only one inside an activity. A candidate package runs once on every held-out train
task — the part of train R never sees, fixed for the activity. The failing set is the held-out tasks the working line
fails, the passing set the rest. A fix counts only where one of the package's changed components showed in the attempt
(the footprint: a tool the model called, a hook that acted, a skill it read, memory it was given; a prompt, the agent,
a config knob or a vendored file on every attempt): an outcome that changed where nothing of the package acted is
noise. A regression counts on the passing set whatever the footprint.

The flips are then confirmed (v1.7, DESIGN §6.17: finance-v7 measured 10–17% of the tasks flipping between two runs
of one package, so a single reading's ±2 is noise): every conditioned fix and every regression is read again under the
candidate (role `confirm`) until it has `confirm_readings` readings, and the working line's own readings of that task
(first encounter, measure, earlier confirmations) are topped up to the same count. A fix is confirmed when every
candidate reading passes with the footprint and every W reading fails; a regression when every candidate reading fails
and every W reading passes; a task W itself passes and fails is `noisy` and counts neither way; the rest is
`unconfirmed`. `fixed` and `regressed` are the confirmed sets, `flipped_fixed` / `flipped_regressed` the first
reading's.

The accept rule (all clauses needed): net flips = confirmed fixes − confirmed regressions ≥ accept_min_net_flips;
confirmed regressions ≤ accept_max_regressions; a package whose footprint is every attempt (only prompt / config /
agent / vendor components) has ≥ accept_min_whole_fixes confirmed fixes — the second fix stands in for the footprint a
chance pass would have had to land in; the held-out score ≥ the activity's best so far − accept_floor_tolerance. An
accepted package is installed on the working line (`rsi/active.json`, reason `screen`, CAS under `install:<round_id>`);
a rejected one leaves the line where it was. Dev is not read here: the release gate at the end of the activity is
evaluate's (activity/phases.py `gate`), and `template`, `control_pool`, `gate_attempts` and `price` here serve it.

The runs go through the activity's `run_arm` (handed in: the improver imports nothing of the activity).
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from ..contracts import Record, register, short
from ..evaluate.gate import TaskEvalPlan, refill_attempts, shortfall
from ..evaluate.results import TaskResult
from .evidence import NOT_READINGS, TRAIN

WHOLE_KINDS = ("prompt", "agent", "config", "vendor", "removal", "model")


@register
@dataclass(frozen=True)
class ScreenResult(Record):
    TYPE = "screen"
    candidate: str = ""                 # r<i>:<dir>
    package_ref: str = ""
    holdout: tuple = ()                 # every held-out train task
    failing: tuple = ()                 # held-out tasks the working line fails
    passing: tuple = ()                 # held-out tasks it passes
    fixed: tuple = ()                   # failing tasks that pass now where a changed component showed, confirmed
    unconditioned: tuple = ()           # failing tasks that pass now where none did (not counted)
    regressed: tuple = ()               # passing tasks that fail now, confirmed
    flipped_fixed: tuple = ()           # v1.7: the first reading's conditioned fixes, before the confirmation
    flipped_regressed: tuple = ()       # v1.7: the first reading's regressions
    noisy: tuple = ()                   # v1.7: flipped tasks the working line itself passes and fails
    unconfirmed: tuple = ()             # v1.7: flipped tasks the candidate's second reading did not repeat
    confirm_attempts: int = 0           # v1.7: readings the confirmation used (the candidate's and W's)
    footprints: dict = None             # component key -> the held-out tasks it showed on
    net_flips: int = 0
    holdout_score: int = 0              # held-out tasks the candidate passes (valid attempts)
    best_so_far: int = 0                # the working line's best held-out score in this activity
    cost: float | None = None           # per valid attempt
    outcome: str = "screened"           # screened | invalid | budget_exhausted | defect | not_run | stacked
    accepted: bool = False
    stacked_on: str | None = None       # the working-line release this accepted package was stacked on (§9.4)
    reasons: tuple = ()                 # why it is not accepted (empty when accepted or not screened)
    reason: str = ""                    # why it was not screened

    def __post_init__(self):
        for name in ("holdout", "failing", "passing", "fixed", "unconditioned", "regressed", "reasons", "flipped_fixed",
                     "flipped_regressed", "noisy", "unconfirmed"):
            object.__setattr__(self, name, tuple(getattr(self, name) or ()))
        object.__setattr__(self, "footprints", {k: list(v) for k, v in dict(self.footprints or {}).items()})


def condition(comps: list[dict]) -> list[dict]:
    """The components whose footprint conditions a fix: every changed component but the tests and the deleted ones;
    one of the whole kinds (a prompt, the agent, a config knob, a vendored file, a removal) makes every attempt
    count."""
    out = []
    for c in comps:
        if c["kind"] == "test" or c.get("change") == "deleted":
            continue
        out.append({"kind": "prompt", "name": "*"} if c["kind"] in WHOLE_KINDS else c)
    if any(c["change"] == "deleted" and c["kind"] in ("tool", "hook", "skill") for c in comps) and not out:
        out.append({"kind": "prompt", "name": "*"})          # a removal: the package acts on every attempt
    return out


def key(component: dict) -> str:
    return f"{component['kind']}:{component.get('name')}"


def latest(rows: list[TaskResult]) -> dict[str, TaskResult]:
    out: dict = {}
    for r in rows:
        out[r.task_id] = r
    return out


def accept_problems(*, net_flips: int, regressed: int, holdout_score: int, best_so_far: int, settings,
                    fixed: int | None = None, whole: bool = False) -> list[str]:
    """The accept rule's failed clauses, in words. `whole`: the package's footprint is every attempt, so it needs
    `accept_min_whole_fixes` confirmed fixes (`fixed`)."""
    out = []
    if net_flips < int(settings.accept_min_net_flips):
        out.append(f"net flips {net_flips} below accept_min_net_flips {settings.accept_min_net_flips}")
    if regressed > int(settings.accept_max_regressions):
        out.append(f"{regressed} regressions above accept_max_regressions {settings.accept_max_regressions}")
    if whole and fixed is not None and fixed < int(getattr(settings, "accept_min_whole_fixes", 1)):
        out.append(f"{fixed} confirmed fixes below accept_min_whole_fixes {settings.accept_min_whole_fixes} for a "
                   f"package whose footprint is every attempt")
    if holdout_score < best_so_far - int(settings.accept_floor_tolerance):
        out.append(f"held-out score {holdout_score} below the best so far {best_so_far} minus "
                   f"accept_floor_tolerance {settings.accept_floor_tolerance}")
    return out


def w_readings(activity, package_ref: str, tasks) -> dict[str, list[TaskResult]]:
    """{task: the working line's valid train readings of it} — every role but a screen's and the library's (the
    stream's first encounter, a measure, an earlier confirmation)."""
    runs = activity.results.runs()
    out: dict[str, list[TaskResult]] = {t: [] for t in tasks}
    for r in activity.results.all():
        run = runs.get(r.run)
        if r.task_id in out and r.split == TRAIN and r.validity == "valid" and run is not None \
                and run.package_ref == package_ref and run.role not in NOT_READINGS + ("screen",):
            out[r.task_id].append(r)
    return out


def screen(activity, *, run_arm, name: str, package, base_release, holdout: list[str], reading: dict, evidence,
           conditions: list[dict], account, retries: int, best_so_far: int, settings) -> ScreenResult:
    """Run one candidate package on every held-out task, read it against the working line's reading (`reading`:
    {task: TaskResult} of the current W on train), then confirm the flips (v1.7)."""
    failing = [t for t in holdout if t in reading and not reading[t].success]
    passing = [t for t in holdout if t in reading and reading[t].success]
    base = dict(candidate=name, package_ref=package.ref(), holdout=tuple(holdout), failing=tuple(failing),
                passing=tuple(passing), best_so_far=int(best_so_far))
    if not holdout:
        return ScreenResult(**base, outcome="not_run", reason="no held-out train task")

    def run(arm, chosen, retry=0, supersedes=None):
        return run_arm(activity, release=base_release, split="train", candidate=name, role="screen", pairs=chosen,
                       account=account, arm=arm, retry=retry, supersedes=supersedes, package=package,
                       notes=f"improver screen {name}")
    rows = refill_attempts(run("subject", [(t, 0) for t in holdout]), run, "subject", int(retries))
    after = latest(rows)
    if any(r.failure_class == "budget_exhausted" for r in after.values()):
        return ScreenResult(**base, outcome="budget_exhausted", reason="the budget refused a screen attempt")
    defects = [r for r in after.values() if r.failure_class == "candidate_defect"]
    if defects:
        return ScreenResult(**base, outcome="defect", reason=f"candidate defect on {defects[0].task_id}: "
                                                             f"{defects[0].invalid_reason or defects[0].stop}"[:300])
    valid = {t: r for t, r in after.items() if r.validity == "valid"}
    if not valid or not any(t in valid for t in failing or holdout):
        return ScreenResult(**base, outcome="invalid", reason="no valid reading on the held-out tasks")
    footprints: dict[str, list] = {}

    def shown(r: TaskResult) -> bool:
        return any(c.get("name") == "*" or evidence.footprint(r, c) for c in conditions)

    for t in holdout:
        r = valid.get(t)
        if r is None:
            continue
        for c in conditions:
            if c.get("name") == "*" or evidence.footprint(r, c):
                footprints.setdefault(key(c), []).append(t)
    flipped_fixed, unconditioned = [], []
    for t in failing:
        r = valid.get(t)
        if r is None or not r.success:
            continue
        (flipped_fixed if shown(r) else unconditioned).append(t)
    flipped_regressed = [t for t in passing if t in valid and not valid[t].success]
    score = sum(1 for r in valid.values() if r.success)
    costs = [r.cost_usd for r in valid.values()]
    cost = None if not costs or any(c is None for c in costs) else round(sum(costs) / len(costs), 6)
    whole = bool(conditions) and all(c.get("name") == "*" for c in conditions)
    base.update(flipped_fixed=tuple(flipped_fixed), flipped_regressed=tuple(flipped_regressed),
                unconditioned=tuple(unconditioned), footprints=footprints, holdout_score=score, cost=cost)
    # the confirmation (DESIGN §6.17): the flipped tasks again under the candidate, W's readings topped up
    n = int(getattr(settings, "confirm_readings", 1))
    flipped = flipped_fixed + flipped_regressed
    fixed, regressed, noisy, unconfirmed, used = [], [], [], [], 0
    cand: dict[str, list[TaskResult]] = {t: [valid[t]] for t in flipped}
    w_rows = w_readings(activity, base_release.package_ref, flipped)
    if flipped and n >= 2:
        def confirm(arm, chosen, retry=0, supersedes=None):
            return run_arm(activity, release=base_release, split="train", candidate=name, role="confirm",
                           pairs=chosen, account=account, arm=arm, retry=retry, supersedes=supersedes,
                           package=package, notes=f"improver confirm {name}: the flipped tasks again")
        again = refill_attempts(confirm("subject", [(t, i) for t in flipped for i in range(n - 1)]), confirm,
                                "subject", int(retries))
        w_name = f"confirm:{short(base_release.ref())}"

        def w_run(arm, chosen, retry=0, supersedes=None):
            return run_arm(activity, release=base_release, split="train", candidate=w_name, role="confirm",
                           pairs=chosen, account=account, arm=arm, retry=retry, supersedes=supersedes,
                           notes="the working line's reading of a flipped task, for the confirmation")
        # W's own run of confirmations: the indices continue what is on record, so a screen read again pays nothing
        runs = activity.results.runs()
        on_record = {t: sum(1 for r in activity.results.all() if r.task_id == t and runs.get(r.run) is not None
                            and runs[r.run].candidate == w_name and runs[r.run].role == "confirm") for t in flipped}
        pairs = [(t, on_record[t] + i) for t in flipped for i in range(max(0, n - len(w_rows[t])))]
        more = refill_attempts(w_run("subject", pairs), w_run, "subject", int(retries)) if pairs else []
        rows_all = list(again) + list(more)
        if any(r.failure_class == "budget_exhausted" for r in rows_all):
            return ScreenResult(**base, outcome="budget_exhausted", reason="the budget refused a confirmation attempt")
        defects = [r for r in again if r.failure_class == "candidate_defect"]
        if defects:
            return ScreenResult(**base, outcome="defect", reason=f"candidate defect on {defects[0].task_id}: "
                                                                 f"{defects[0].invalid_reason or defects[0].stop}"[:300])
        used = len(rows_all)
        for r in again:
            if r.validity == "valid":
                cand[r.task_id].append(r)
        for r in more:
            if r.validity == "valid":
                w_rows[r.task_id].append(r)
        for t in flipped:
            c, w = cand[t], w_rows[t]
            if len(c) < n or not w:
                unconfirmed.append(t)
                continue
            w_pass, w_fail = any(x.success for x in w), any(not x.success for x in w)
            if w_pass and w_fail:
                noisy.append(t)
            elif t in flipped_fixed and all(x.success and shown(x) for x in c) and w_fail:
                fixed.append(t)
            elif t in flipped_regressed and all(not x.success for x in c) and w_pass:
                regressed.append(t)
            else:
                unconfirmed.append(t)
    else:
        fixed, regressed = list(flipped_fixed), list(flipped_regressed)
    net = len(fixed) - len(regressed)
    problems = accept_problems(net_flips=net, regressed=len(regressed), holdout_score=score, best_so_far=best_so_far,
                               settings=settings, fixed=len(fixed), whole=whole)
    return ScreenResult(**base, fixed=tuple(fixed), regressed=tuple(regressed), noisy=tuple(noisy),
                        unconfirmed=tuple(unconfirmed), confirm_attempts=used, net_flips=net,
                        accepted=not problems, reasons=tuple(problems))

def pick_order(results) -> list[ScreenResult]:
    """The accepted results in install order: the most net flips, then the lower cost (unknown last), then the order
    given (IMPROVER §9, §9.4)."""
    ok = [(i, r) for i, r in enumerate(results) if r.accepted]
    return [r for _, r in sorted(ok, key=lambda x: (-x[1].net_flips, x[1].cost is None, x[1].cost or 0.0, x[0]))]


def pick_best(results) -> ScreenResult | None:
    """The first of pick_order, or None when nothing is accepted."""
    order = pick_order(results)
    return order[0] if order else None


def accept(activity, result: ScreenResult, *, round_id: str, base_release):
    """Install an accepted package on the working line (reason `screen`), CAS under `install:<round_id>`."""
    if not result.accepted:
        raise ValueError(f"{result.candidate} is not accepted: {list(result.reasons)}")
    screen_ref = activity.objects.put(result)
    release = activity.releases.screened(result.package_ref, screen_ref, parent=base_release)
    return activity.releases.install_screened(release, screen_ref, expected_parent=base_release.ref(),
                                              operation_id=f"install:{round_id}".replace("/", "_"))


# ---------------------------------------------------------------------------- pricing before anything is paid
def cost_per_attempt(activity, package_ref: str, split: str) -> float | None:
    """The package's mean cost per valid attempt on a split, from the record (None: no priced reading)."""
    runs = activity.results.runs()
    costs = [r.cost_usd for r in activity.results.all() if r.split == split and r.validity == "valid"
             and runs.get(r.run) is not None and runs[r.run].package_ref == package_ref]
    if not costs or any(c is None for c in costs):
        return None
    return sum(costs) / len(costs)


def gate_attempts(activity, control_ref: str | None = None) -> int:
    """The release gate's attempts at most: every dev task of the template, the subject arm and its confirmations,
    and the pairs the pooled control of `control_ref` (W0's package) is short of."""
    plan = template(activity)
    if plan is None:
        return 0
    short = shortfall(plan, control_pool(activity, control_ref, plan.task_ids)) if control_ref else []
    return len(plan.task_ids) * plan.repeats * (1 + plan.confirmations) + len(short)


def price(activity, base_package_ref: str, *, screen_attempts: int) -> tuple[float | None, str]:
    """What screens will cost at most, from the working line's train costs on record. -> (usd, "") or (None, why it
    cannot be priced)."""
    train = cost_per_attempt(activity, base_package_ref, "train")
    if train is None:
        return None, "the working line has no priced train reading on record: the screen cannot be priced"
    return round(screen_attempts * train, 6), ""


# ---------------------------------------------------------------------------- the release gate's readings
def template(activity) -> TaskEvalPlan | None:
    rows = list(activity.events.read("gate_template"))
    if not rows:
        return None
    return TaskEvalPlan.from_json(activity.objects.get(rows[-1]["plan"]))


def control_pool(activity, package_ref: str, task_ids) -> list[TaskResult]:
    runs = activity.results.runs()
    wanted = set(task_ids)
    return [r for r in activity.results.all() if r.split == "dev" and r.validity == "valid" and r.task_id in wanted
            and runs.get(r.run) is not None and runs[r.run].package_ref == package_ref
            and runs[r.run].role in ("base", "control")]


def gate_plan(activity, *, package, base_release) -> TaskEvalPlan:
    """The frozen template with the arms filled in: the candidate package against W0's."""
    plan = template(activity)
    if plan is None:
        raise ValueError("no frozen gate template: run `ouroloop freeze` first")
    return replace(plan, candidate_ref=package.ref(), control_ref=base_release.package_ref,
                   parent_release=base_release.ref())
