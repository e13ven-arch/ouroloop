"""V3, the train screen, and V4, the dev gate (docs/design/IMPROVER.md §9; DESIGN §6.6).

V3 is the first paid rung. A candidate runs on its failing set — the train tasks of its cause and its claims that the
current W still fails — and on a passing sample the round draws from the train tasks the current W passes. A fix
counts only where the candidate's component showed (the footprint: a tool the model called, a hook that acted, a skill
it read, memory it was given; a prompt, the agent or a config on every attempt): an outcome that changed where the
component never acted is noise, not its effect. A regression counts on the sample whatever the footprint. The best
eligible candidate pays V4.

V4 is evaluate's gate over the frozen template (the `gate_template` event), the candidate against the current W on dev,
the control pooled from the current W's dev readings under the same conditions and topped up. An accepted receipt —
confirmed when the plan confirms — is registered and installed by CAS under `install:<round_id>`.

The runs go through the activity's `run_arm` (handed in: the improver imports nothing of the activity).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, replace

from ..contracts import Record, register
from ..evaluate.gate import TaskEvalPlan, evaluate_task, receipt_differences, refill_attempts, shortfall
from ..evaluate.results import TaskResult

WHOLE_KINDS = ("prompt", "agent", "config", "vendor", "removal", "model")


@register
@dataclass(frozen=True)
class ScreenResult(Record):
    TYPE = "screen"
    candidate: str = ""
    package_ref: str = ""
    tasks: tuple = ()
    failing: tuple = ()
    sample: tuple = ()
    fixed: tuple = ()                   # failing-set tasks that now pass where the component showed
    unconditioned: tuple = ()           # failing-set tasks that now pass where it did not (not counted)
    regressed: tuple = ()               # sample tasks that now fail
    net_flips: int = 0
    cost: float | None = None           # per valid attempt
    thin: bool = False
    outcome: str = "screened"           # screened | invalid | budget_exhausted | defect | not_run
    reason: str = ""


def passing_sample(reading: dict, *, order, size: int, seed: int, round_index: int, exclude=()) -> list[str]:
    """`size` train tasks the current W passes, drawn per round (the same round draws the same), in the frozen order."""
    passed = [t for t in order if t in reading and reading[t].success and t not in set(exclude)]
    rng = random.Random(f"passing-sample:{int(seed)}:{int(round_index)}")
    rng.shuffle(passed)
    chosen = set(passed[:max(int(size), 0)])
    return [t for t in order if t in chosen]


def condition(decl, comps: list[dict]) -> list[dict]:
    """The components whose footprint conditions a fix: those of the declared kind the candidate changed; for a prompt,
    the agent, a config, a vendored package, a removal or the model, every attempt."""
    if decl.kind in WHOLE_KINDS:
        return [{"kind": "prompt", "name": "*"}]
    if decl.kind == "memory":
        return [{"kind": "memory", "name": "snapshot"}]
    return [c for c in comps if c["kind"] == decl.kind and c["change"] != "deleted"]


def latest(rows: list[TaskResult]) -> dict[str, TaskResult]:
    out: dict = {}
    for r in rows:
        out[r.task_id] = r
    return out


def screen(activity, *, run_arm, name: str, package, base_release, failing: list[str], sample: list[str],
           reading: dict, evidence, conditions: list[dict], account, retries: int, thin_tasks: int) -> ScreenResult:
    """Run one candidate's screen and read it. `reading` is the current W's train reading ({task: TaskResult})."""
    tasks = list(dict.fromkeys([*failing, *sample]))
    base = dict(candidate=name, package_ref=package.ref(), tasks=tuple(tasks), failing=tuple(failing),
                sample=tuple(sample), thin=len(failing) < int(thin_tasks))
    if not failing:
        return ScreenResult(**base, outcome="not_run", reason="no train task it claims fails now")

    def run(arm, chosen, retry=0, supersedes=None):
        return run_arm(activity, release=base_release, split="train", candidate=name, role="screen", pairs=chosen,
                       account=account, arm=arm, retry=retry, supersedes=supersedes, package=package,
                       notes=f"improver screen {name}")
    rows = refill_attempts(run("subject", [(t, 0) for t in tasks]), run, "subject", int(retries))
    after = {t: r for t, r in latest(rows).items()}
    if any(r.failure_class == "budget_exhausted" for r in after.values()):
        return ScreenResult(**base, outcome="budget_exhausted", reason="the budget refused a screen attempt")
    defects = [r for r in after.values() if r.failure_class == "candidate_defect"]
    valid = {t: r for t, r in after.items() if r.validity == "valid"}
    if defects:
        return ScreenResult(**base, outcome="defect", reason=f"candidate defect on {defects[0].task_id}: "
                                                             f"{defects[0].invalid_reason or defects[0].stop}"[:300])
    if not any(t in valid for t in failing):
        return ScreenResult(**base, outcome="invalid", reason="no valid reading on the failing set")
    fixed, unconditioned = [], []
    for t in failing:
        r = valid.get(t)
        if r is None or not r.success:
            continue
        shown = any(c.get("name") == "*" or evidence.footprint(r, c) for c in conditions)
        (fixed if shown else unconditioned).append(t)
    regressed = [t for t in sample if t in valid and t in reading and reading[t].success and not valid[t].success]
    costs = [r.cost_usd for r in valid.values()]
    cost = None if not costs or any(c is None for c in costs) else round(sum(costs) / len(costs), 6)
    return ScreenResult(**base, fixed=tuple(fixed), unconditioned=tuple(unconditioned), regressed=tuple(regressed),
                        net_flips=len(fixed) - len(regressed), cost=cost)


def eligible(result: ScreenResult, settings) -> bool:
    return (result.outcome == "screened" and result.net_flips >= settings.screen_min_net_flips
            and len(result.fixed) >= settings.screen_min_fixes
            and len(result.regressed) <= settings.screen_max_sample_regressions)


def pick_best(results: list[ScreenResult], settings) -> ScreenResult | None:
    """The one that pays the gate: among the eligible, the most net flips, then the lower cost (unknown last), then
    the order given."""
    ok = [(i, r) for i, r in enumerate(results) if eligible(r, settings)]
    if not ok:
        return None
    return sorted(ok, key=lambda x: (-x[1].net_flips, x[1].cost is None, x[1].cost or 0.0, x[0]))[0][1]


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
    """The gate's attempts at most: every dev task of the template, the subject arm and its confirmations, and the
    pairs the pooled control of `control_ref` (the current W's package) is short of — after an install the new W's
    dev readings are its gate's subject arm, not a control, so its first gate reads the whole control."""
    plan = template(activity)
    if plan is None:
        return 0
    short = shortfall(plan, control_pool(activity, control_ref, plan.task_ids)) if control_ref else []
    return len(plan.task_ids) * plan.repeats * (1 + plan.confirmations) + len(short)


def price(activity, base_package_ref: str, *, screen_attempts: int, gate: bool = True) -> tuple[float | None, str]:
    """What the screens and the gate will cost at most, from the current W's costs on record. -> (usd, "") or (None,
    why it cannot be priced)."""
    train = cost_per_attempt(activity, base_package_ref, "train")
    dev = cost_per_attempt(activity, base_package_ref, "dev") if gate else 0.0
    if train is None or dev is None:
        return None, "the current W has no priced train or dev reading on record: the screens cannot be priced"
    return round(screen_attempts * train + (gate_attempts(activity, base_package_ref) * dev if gate else 0.0), 6), ""


# ---------------------------------------------------------------------------- V4
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


def gate(activity, *, run_arm, round_id: str, package, base_release, account) -> dict:
    """V4: the paired dev gate of one candidate, and its install when the receipt accepts."""
    from ..contracts import short
    plan = template(activity)
    if plan is None:
        raise ValueError("no frozen gate template: run `ouroloop freeze` first")
    plan = replace(plan, candidate_ref=package.ref(), control_ref=base_release.package_ref,
                   parent_release=base_release.ref())
    plan_ref = activity.objects.put(plan)
    families = {t: activity.splits.family_of(t) for t in plan.task_ids}

    def run(arm, order, retry=0, supersedes=None):
        if arm == "control":
            # this gate's run: a top-up an earlier gate's budget refused is read again, not handed back
            return run_arm(activity, release=base_release, split="dev",
                           candidate=f"control:{short(base_release.ref())}:{round_id}", role="control", pairs=order,
                           account=account, arm="control", retry=retry, supersedes=supersedes,
                           notes="the gate's control top-up")
        return run_arm(activity, release=base_release, split="dev", candidate=f"{round_id}:gate", role="subject",
                       pairs=order, account=account, plan_ref=plan_ref, arm=arm, retry=retry, supersedes=supersedes,
                       package=package, notes=f"the gate of {round_id}")
    receipt = evaluate_task(plan, run=run, objects=activity.objects, families=families,
                            control_pool=control_pool(activity, base_release.package_ref, plan.task_ids))
    receipt_ref = activity.objects.put(receipt)
    activity.releases.register_receipt(receipt_ref, round=round_id)
    diff = receipt_differences(receipt, activity.objects)
    out = {"receipt": receipt_ref, "verdict": receipt.verdict, "kind": receipt.kind,
           "gain": (receipt.metrics or {}).get("gain"), "fixed_count": len(diff["fixed"]),
           "regressed_count": len(diff["regressed"]), "installed": None}
    if receipt.verdict == "accepted":
        release = activity.releases.candidate(package.ref(), receipt_ref, parent=base_release)
        done = activity.releases.install(release, receipt_ref, expected_parent=base_release.ref(),
                                         operation_id=f"install:{round_id}".replace("/", "_"))
        out["install"] = done.status
        out["installed"] = release.ref() if done.status == "installed" else None
    return out
