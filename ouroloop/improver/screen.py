"""V3, the held-out screen, which is the activity's acceptance (docs/design/IMPROVER.md §9; DESIGN §6.6, §6.7, v1.2).

V3 is the first paid rung and the only one inside an activity. A candidate package runs once on every held-out train
task — the part of train R never sees, fixed for the activity. The failing set is the held-out tasks the working line
fails, the passing set the rest. A fix counts only where one of the package's changed components showed in the attempt
(the footprint: a tool the model called, a hook that acted, a skill it read, memory it was given; a prompt, the agent,
a config knob or a vendored file on every attempt): an outcome that changed where nothing of the package acted is
noise. A regression counts on the passing set whatever the footprint.

The accept rule (three clauses, all needed): net flips = conditioned fixes − regressions ≥ accept_min_net_flips;
regressions ≤ accept_max_regressions; the held-out score ≥ the activity's best so far − accept_floor_tolerance. An
accepted package is installed on the working line (`rsi/active.json`, reason `screen`, CAS under `install:<round_id>`);
a rejected one leaves the line where it was. Dev is not read here: the release gate at the end of the activity is
evaluate's (activity/phases.py `gate`), and `template`, `control_pool`, `gate_attempts` and `price` here serve it.

The runs go through the activity's `run_arm` (handed in: the improver imports nothing of the activity).
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from ..contracts import Record, register
from ..evaluate.gate import TaskEvalPlan, refill_attempts, shortfall
from ..evaluate.results import TaskResult

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
    fixed: tuple = ()                   # failing tasks that pass now where a changed component showed
    unconditioned: tuple = ()           # failing tasks that pass now where none did (not counted)
    regressed: tuple = ()               # passing tasks that fail now
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
        for name in ("holdout", "failing", "passing", "fixed", "unconditioned", "regressed", "reasons"):
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


def accept_problems(*, net_flips: int, regressed: int, holdout_score: int, best_so_far: int, settings) -> list[str]:
    """The accept rule's failed clauses, in words."""
    out = []
    if net_flips < int(settings.accept_min_net_flips):
        out.append(f"net flips {net_flips} below accept_min_net_flips {settings.accept_min_net_flips}")
    if regressed > int(settings.accept_max_regressions):
        out.append(f"{regressed} regressions above accept_max_regressions {settings.accept_max_regressions}")
    if holdout_score < best_so_far - int(settings.accept_floor_tolerance):
        out.append(f"held-out score {holdout_score} below the best so far {best_so_far} minus "
                   f"accept_floor_tolerance {settings.accept_floor_tolerance}")
    return out


def screen(activity, *, run_arm, name: str, package, base_release, holdout: list[str], reading: dict, evidence,
           conditions: list[dict], account, retries: int, best_so_far: int, settings) -> ScreenResult:
    """Run one candidate package on every held-out task and read it against the working line's reading
    (`reading`: {task: TaskResult} of the current W on train)."""
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
    for t in holdout:
        r = valid.get(t)
        if r is None:
            continue
        for c in conditions:
            if c.get("name") == "*" or evidence.footprint(r, c):
                footprints.setdefault(key(c), []).append(t)
    fixed, unconditioned = [], []
    for t in failing:
        r = valid.get(t)
        if r is None or not r.success:
            continue
        shown = any(t in footprints.get(key(c), ()) for c in conditions)
        (fixed if shown else unconditioned).append(t)
    regressed = [t for t in passing if t in valid and not valid[t].success]
    score = sum(1 for r in valid.values() if r.success)
    costs = [r.cost_usd for r in valid.values()]
    cost = None if not costs or any(c is None for c in costs) else round(sum(costs) / len(costs), 6)
    net = len(fixed) - len(regressed)
    problems = accept_problems(net_flips=net, regressed=len(regressed), holdout_score=score, best_so_far=best_so_far,
                               settings=settings)
    return ScreenResult(**base, fixed=tuple(fixed), unconditioned=tuple(unconditioned), regressed=tuple(regressed),
                        footprints=footprints, net_flips=net, holdout_score=score, cost=cost,
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
