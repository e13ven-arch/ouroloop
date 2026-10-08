"""The phases of an activity (docs/design/ACTIVITY.md §3, §4). The order and the wiring are here; every mechanism is in
another package: the attempt and the gate in evaluate/, the run in runtime/, the package in harness/.

    smoke     the upstream version, the frozen split, the scorer contract, the input audit and the seed's literal scan
              (checks only, no model); without --checks-only, one dev attempt through the whole runner shell
    freeze    the gate's plan template, the budget root and its final share, and — when the config has [final] — the
              sealed final plan; prints the [rsi] lines the config then names
    baseline  W0 on every train task and on dev `control_readings` times (the pooled control's first readings)
    library   the memory's stream and snapshot (stage 2, docs/design/MEMORY.md §7)
    round     one round of the improver (stage 3, docs/design/IMPROVER.md §3)
    evaluate  the sealed final on the test tasks; it reports, it does not choose
    report    the activity's report from what is on disk; no model is called
    export    an installed release as an app

`cycle --until` runs baseline, then the library, then the rounds, and stops after the named step; run again, it
resumes, and pays for nothing already on record.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from ..adapters import automationbench as ab
from ..contracts import short, write_json
from ..evaluate.attempt import make_agent_factory, run_tasks
from ..evaluate.final import FinalPlan, estimate_lines, final_estimate, run_final, seal, share_problem
from ..evaluate.gate import GatePlan, TaskEvalPlan, refill_attempts
from ..evaluate.release import NotAuthorized, Release
from ..evaluate.results import TaskResult, attempt_id, summarise
from ..harness.literals import findings, forbidden, literal_index
from .checks import require, startup_checks

PHASES = ("smoke", "freeze", "baseline", "library", "round", "evaluate", "report", "export")
STAGE2 = "the library phase needs the memory package (stage 2, docs/design/MEMORY.md §7)"
STAGE3 = "the round phase needs the improver package (stage 3, docs/design/IMPROVER.md §3)"


# ---------------------------------------------------------------------------- one arm of tasks
def run_arm(activity, *, release: Release, split: str, candidate: str, role: str, pairs: list, account,
            plan_ref: str = "", arm: str = "subject", retry: int = 0, supersedes: dict | None = None,
            notes: str = "", allocation: str | None = None) -> list[TaskResult]:
    """The attempts of one arm, under one run identity. Attempts already on record for this run and these pairs are
    kept as they were, not paid again; the rest run through the runner shell with K's worker."""
    package = activity.package(release)
    run = activity.run_manifest(candidate=candidate, role=role, split=split, release=release, notes=notes)
    activity.results.put_run(run)
    recorded = {r.attempt_id: r for r in activity.results.of_run(run.hash())}
    want = [(t, i, attempt_id(run.hash(), t, i, arm, retry)) for t, i in pairs]
    todo = [(t, i) for t, i, a in want if a not in recorded]
    if todo:
        make = make_agent_factory(provider=activity.actor_provider(account, allocation=allocation), package=package,
                                  core=activity.k, worker=activity.worker(), root=activity.root,
                                  pricing=activity.pricing(), release_ref=release.ref(), core_ref=activity.core_ref,
                                  arm=arm)
        for r in run_tasks(activity.suite, [], make, run=run, splits=activity.splits, results=activity.results,
                           order=todo, arm=arm, plan_ref=plan_ref, release_ref=release.ref(), retry_index=retry,
                           supersedes=supersedes, concurrency=activity.concurrency):
            recorded[r.attempt_id] = r
    return [recorded[a] for _t, _i, a in want if a in recorded]


def latest(rows: list[TaskResult]) -> list[TaskResult]:
    """One row per (task, repeat): the last one recorded (a refill supersedes what it refilled)."""
    out: dict = {}
    for r in rows:
        out[(r.task_id, r.attempt)] = r
    return list(out.values())


def _reading(activity, *, release: Release, split: str, candidate: str, tasks: list, account, out) -> list:
    pairs = [(t, i) for t in tasks for i in range(int(activity.k["gate"]["repeats"]))]

    def run(arm, chosen, retry=0, supersedes=None):
        return run_arm(activity, release=release, split=split, candidate=candidate, role="base", pairs=chosen,
                       account=account, arm=arm, retry=retry, supersedes=supersedes)
    rows = latest(refill_attempts(run("subject", pairs), run, "subject",
                                  int(activity.k["gate"]["max_infrastructure_retries"])))
    s = summarise(rows)
    out(f"arm        {candidate} {split}: {s['passed']}/{s['tasks']} passed, partial={s['partial_credit']}, "
        f"invalid={s['invalid']}, cost={'unmeasured' if s['cost_usd'] is None else s['cost_usd']}")
    return rows


# ---------------------------------------------------------------------------- smoke
def smoke(activity, *, tasks=None, limit: int | None = None, checks_only: bool = False,
          out: Callable[[str], None] = print) -> dict:
    """The checks before any model is called, then (without checks_only) one dev attempt end to end. `ok` is whether
    the checks passed and nothing leaked — never whether the agent did well."""
    v = activity.versions
    out(f"upstream   {v.get('path') or 'installed package'} @ {(v.get('commit') or '')[:12] or '(no git)'} "
        f"package={v.get('package') or '?'} python={v.get('python')}")
    splits = activity.splits
    counts = splits.counts()
    out(f"splits     {activity.results.manifest_path()} hash={splits.hash()} rule={splits.split_rule or 'hash'} "
        f"seed={splits.seed}")
    out("           " + "  ".join(f"{s}: {c['tasks']} tasks / {c['families']} families" for s, c in counts.items()))
    for line in activity.drift:
        out(f"DRIFT      {line}")
    threshold = float(activity.k["bench"]["near_duplicate_threshold"])
    dups = ab.near_duplicates(activity.suite, threshold)
    out(f"families   {len(splits.splits)} groups; {len(dups)} task pairs above overlap {threshold}")
    dev = splits.ids("dev")
    chosen = [t for t in (tasks or []) if t in dev] if tasks else dev[:int(activity.k["bench"]["smoke_tasks"])]
    chosen = chosen[:limit] if limit else chosen
    outside = sorted(set(tasks or ()) - set(dev))
    if outside:
        out(f"NOTE       {outside} are not dev tasks: the smoke reads dev only")
    checks = ab.contract_check(activity.suite, chosen or dev[:1])
    out(f"contract   rubric={checks['rubric'].get('funcs') or 'not importable'}")
    for line in checks["failed"]:
        out(f"FAILED     {line}")
    leaks = []
    for tid in chosen or dev[:1]:
        episode = activity.suite.episode(tid)
        try:
            found = ab.audit_inputs(activity.suite.tasks[tid], activity.suite.grading(tid),
                                    ab.agent_payload(activity.suite.tasks[tid], episode))
        finally:
            episode.close()
        leaks += found["leaks"]
    out(f"audit      {len(chosen or dev[:1])} tasks: {len(leaks)} leaks")
    for line in leaks:
        out(f"LEAK       {line}")
    literal = seed_literal_scan(activity)
    out(f"literals   the seed searched whole against {literal['forbidden']} single-task train values: "
        f"{len(literal['findings'])} found")
    for f in literal["findings"][:10]:
        out(f"LITERAL    {f['path']}:{f['line']}: {f['literal']!r}")
    problems = startup_checks(activity, phase="smoke", needs_model=not checks_only, needs_worker=not checks_only)
    for line in problems:
        out(f"REFUSED    {line}")
    report = {"upstream": v, "splits": {"hash": splits.hash(), "counts": counts, "drift": activity.drift},
              "near_duplicates": dups[:20], "contract": checks, "leaks": leaks, "literals": literal,
              "problems": problems, "attempts": [], "summary": {},
              "ok": bool(chosen) and not checks["failed"] and not leaks and not activity.drift
              and not literal["findings"] and not problems}
    if not checks_only and report["ok"]:
        release = activity.active_release()
        rows = run_arm(activity, release=release, split="dev", candidate="smoke", role="base",
                       pairs=[(t, 0) for t in chosen], account=activity.account("smoke"), notes="ouroloop smoke")
        for r in rows:
            out(f"attempt    {r.task_id} success={r.success} partial={r.partial_credit:.3f} steps={r.steps} "
                f"tools={r.tool_calls} {r.termination} {r.validity} session={r.evidence}")
        report["attempts"], report["summary"] = [r.to_json() for r in rows], summarise(rows)
    out(f"result     {'checks passed' if report['ok'] else 'CHECKS FAILED'}; this phase makes no claim about "
        f"capability")
    return report


def seed_literal_scan(activity) -> dict:
    """The seed searched whole for single-task train values (the train tasks' assertion params and initial states):
    the full scan the seed gets once; candidates are searched only on the lines they change."""
    rows = []
    for tid in activity.splits.ids("train"):
        grading = activity.suite.grading(tid)
        rows.append({"task_id": tid, "assertions": [dict(a) for a in grading.assertions],
                     "state": grading.initial_state})
    banned = forbidden(literal_index(rows))
    from ..harness.seed import seed_files
    found = findings(seed_files(activity.k["harness"]["seed"], core=activity.k), None, banned)
    return {"forbidden": len(banned), "findings": found}


# ---------------------------------------------------------------------------- freeze
def gate_template(activity) -> TaskEvalPlan:
    """The gate's frozen template: every dev task, K's repeats, order and thresholds; the candidate is filled in at
    each gate, the rest is never touched again."""
    g = activity.k["gate"]
    gate = GatePlan(min_groups=int(g["min_groups"]), min_gain=float(g["min_gain"]),
                    max_regression_rate=float(g["max_regression_rate"]), missing_cost_policy=g["missing_cost_policy"],
                    regression_drop=g.get("regression_drop"), cost_beta0=g.get("cost_beta0"),
                    cost_beta1=g.get("cost_beta1"), min_tasks=g.get("min_tasks"))
    tasks = tuple(activity.splits.ids("dev"))
    if gate.min_tasks is not None and len(tasks) < int(gate.min_tasks):
        raise ValueError(f"freeze refused: the gate runs {len(tasks)} dev tasks, fewer than its min_tasks "
                         f"{gate.min_tasks}")
    return TaskEvalPlan(core_ref=activity.core_ref, split_ref=activity.splits.hash(), task_ids=tasks,
                        repeats=int(g["repeats"]), order_seed=int(g["order_seed"]), gate_plan=gate.to_json(),
                        per_attempt_budget={k: v for k, v in activity.attempt_limits().limits().items()},
                        retry_policy={"max_infrastructure_retries": int(g["max_infrastructure_retries"])},
                        control_pooling=g.get("control_pooling"), confirmation_repeats=int(g["confirmations"]))


def final_plan(activity) -> FinalPlan:
    final = activity.k["final"]
    release = activity.active_release()
    seed = activity.seed_release()
    arms = {"W0": seed.ref()} if release.ref() == seed.ref() else {"W0": seed.ref(), "W*": release.ref()}
    return FinalPlan(experiment_id=f"{activity.scope_id}-final", tasks=tuple(activity.splits.ids(final["tasks"])),
                     repeats=int(final["repeats"]), arms=arms, claims=tuple(final["claims"]) if len(arms) > 1 else (),
                     split_hash=activity.splits.hash(), core_ref=activity.core_ref,
                     max_infrastructure_retries=int(activity.k["gate"]["max_infrastructure_retries"]))


def freeze(activity, *, out: Callable[[str], None] = print) -> dict:
    """Freeze what the paid phases run under, and print the [rsi] lines that name it. Freezing again gives the same
    refs; a final plan different from the one sealed under the same id is refused."""
    refs = {"scope_id": activity.scope_id, "core_ref": activity.core_ref, "split_ref": activity.splits.hash()}
    template = activity.objects.put(gate_template(activity))
    activity.events.append("gate_template", plan=template)
    root = activity.root_account()
    out(f"budget     root {activity.scope_id}: {root.remaining()['limits'] or 'no dollar limit'}, "
        f"set aside {root.remaining()['allocations'] or 'nothing'}")
    if "final" in activity.config:
        plan = final_plan(activity)
        if len(plan.arms) < 2:
            raise ValueError("freeze refused: the final compares W* with W0, and nothing was installed yet")
        estimate = final_estimate(list(activity.results.all()), plan, plan_ref=None,
                                  execution_of=lambda ref: _execution(activity, ref), pricing=activity.pricing(),
                                  concurrency=activity.concurrency)
        for line in estimate_lines(estimate, (activity.k["budget"] or {}).get("final_usd")):
            out(line)
        problem = share_problem(estimate, (activity.k["budget"] or {}).get("final_usd"))
        if problem:
            raise ValueError(f"freeze refused: {problem}; the plan was not sealed")
        refs["final_plan_ref"] = seal(plan, activity.root, activity.objects)["plan"]
    out("frozen     name these under [rsi]:")
    out("[rsi]")
    for key, value in refs.items():
        out(f'{key} = "{value}"')
    return {**refs, "gate_template": template}


def _execution(activity, release_ref):
    try:
        rel = activity.releases.load(release_ref)
    except Exception:  # noqa: BLE001 - a ref this store does not hold executes as nothing known
        return None
    return rel.package_ref, rel.core_ref


# ---------------------------------------------------------------------------- baseline
def baseline(activity, *, limit: int | None = None, out: Callable[[str], None] = print) -> dict:
    """W0 on train (once) and on dev `control_readings` times, each reading its own run of the same release, so the
    pooled control starts with that many readings. A restarted baseline runs only what is missing."""
    require(startup_checks(activity, phase="baseline"), phase="baseline")
    release = activity.active_release()
    account = activity.account("baseline")
    train = activity.splits.ids("train")[:limit] if limit else activity.splits.ids("train")
    dev = activity.splits.ids("dev")[:limit] if limit else activity.splits.ids("dev")
    out(f"baseline   release {short(release.ref())}: {len(train)} train tasks, {len(dev)} dev tasks × "
        f"{activity.k['gate']['control_readings']} readings")
    report = {"release": release.ref(), "train": None, "dev": []}
    rows = _reading(activity, release=release, split="train", candidate="base", tasks=train, account=account, out=out)
    report["train"] = summarise(rows)
    for i in range(1, int(activity.k["gate"]["control_readings"]) + 1):
        name = "base" if i == 1 else f"base:reading-{i}"
        rows = _reading(activity, release=release, split="dev", candidate=name, tasks=dev, account=account, out=out)
        report["dev"].append(summarise(rows))
    return report


def library(activity, *, out: Callable[[str], None] = print) -> dict:
    raise NotImplementedError(STAGE2)


def round_(activity, index: int, *, out: Callable[[str], None] = print) -> dict:
    raise NotImplementedError(STAGE3)


# ---------------------------------------------------------------------------- the final
def evaluate(activity, *, out: Callable[[str], None] = print) -> dict:
    """The sealed final ([rsi] final_plan_ref). It prints the expected spend first and refuses when what is left of the
    final's share cannot hold it; run again, it runs only what is missing."""
    ref = (activity.config.get("rsi") or {}).get("final_plan_ref")
    if not ref or not activity.objects.has(ref):
        raise ValueError("[rsi] final_plan_ref must name the final plan `ouroloop freeze` sealed")
    plan = FinalPlan.from_json(activity.objects.get(ref))
    require(startup_checks(activity, phase="evaluate"), phase="evaluate")
    account = activity.account("final")
    rows = list(activity.results.all())
    estimate = final_estimate(rows, plan, plan_ref=ref, execution_of=lambda r: _execution(activity, r),
                              pricing=activity.pricing(), concurrency=activity.concurrency)
    left = account.remaining(allocation="final").get("effective_remaining", {}).get("usd")
    for line in estimate_lines(estimate, left):
        out(line)
    if left is not None and estimate["expected"] is not None and estimate["reservations"] is not None \
            and estimate["expected"] + estimate["reservations"] > float(left) + 1e-9:
        raise ValueError(f"{plan.experiment_id}: the expected spend and the in-flight reservations do not fit the "
                         f"${float(left):.2f} left to the final; nothing was run")

    def arm(name, release_ref, pairs, retry, supersedes):
        return run_arm(activity, release=activity.releases.load(release_ref), split="test", candidate=name, role=name,
                       pairs=pairs, account=account, plan_ref=ref, arm=name, retry=retry, supersedes=supersedes,
                       allocation="final")
    return {**run_final(plan, root=activity.root, objects=activity.objects, run_arm=arm,
                        recorded={r.attempt_id for r in rows if r.attempt_id}, out=out), "estimate": estimate}


# ---------------------------------------------------------------------------- report, export, cycle
def report(activity, *, write: bool = True, out: Callable[[str], None] = print) -> dict:
    from .report import build_report, render_markdown, write_report
    data = build_report(activity)
    out(render_markdown(data))
    if write:
        paths = write_report(activity, data)
        out(f"wrote      {paths['json']} and {paths['markdown']}")
    return data


def export(activity, release_ref: str | None, to: Path, *, out: Callable[[str], None] = print):
    from ..runtime.app import export as export_app
    ref = release_ref or activity.releases.active_ref()
    if ref not in activity.releases.installed() or activity.releases.revoked(ref):
        raise NotAuthorized(f"{short(ref)} is not an installed, unrevoked release")
    release = activity.releases.load(ref)
    validation = {"release": short(ref), "reason": release.reason}
    if release.receipt_ref:
        receipt = activity.objects.get(release.receipt_ref)
        validation.update({"verdict": receipt.get("verdict"), "gain": (receipt.get("metrics") or {}).get("gain"),
                           "significant": (receipt.get("uncertainty") or {}).get("significant")})
    manifest = export_app(activity.objects, release, activity.k, Path(to), validation=validation)
    out(f"exported   {short(ref)} -> {to}")
    return manifest


def cycle(activity, *, until: str | None = None, out: Callable[[str], None] = print) -> dict:
    """baseline -> library -> rounds, stopping after `until` (baseline, library or round-K); each step is on record,
    so running the cycle again resumes and pays for nothing already done."""
    path = activity.root / "rsi" / "cycle.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"steps": {}}
    stops = ("baseline", "library") + tuple(f"round-{i}" for i in range(int(activity.k["schedule"]["rounds"])))
    if until is not None and until not in stops:
        raise ValueError(f"--until {until!r}: one of {list(stops)}")
    if "baseline" not in state["steps"]:
        state["steps"]["baseline"] = baseline(activity, out=out)
        write_json(path, state)
    if until == "baseline":
        return {**state, "status": "stopped", "at": "baseline"}
    raise NotImplementedError(STAGE2 if until in (None, "library") else STAGE3)
