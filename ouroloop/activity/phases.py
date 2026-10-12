"""The phases of an activity (docs/design/ACTIVITY.md §3, §4). The order and the wiring are here; every mechanism is in
another package: the attempt and the gate in evaluate/, the run in runtime/, the package in harness/.

    smoke     the upstream version, the frozen split, the scorer contract, the input audit and the seed's literal scan
              (checks only, no model); without --checks-only, one dev attempt through the whole runner shell (a split
              without dev: one train attempt)
    freeze    the gate's plan template, the budget root and its final share, and — when the config has [final] — the
              sealed final plan; prints the [rsi] lines the config then names
    baseline  W0 on every train task and on dev `control_readings` times (the pooled control's first readings); with
              [rsi] baseline_from, another workspace's readings of the same AttemptIdentity are imported first
    library   the memory's train stream and its snapshot as a candidate (docs/design/MEMORY.md §7); K's memory.enabled
    round     one round of the improver (docs/design/IMPROVER.md §3, v1.2: one package, held-out acceptance on the
              working line); `cycle` runs them with the patience rule
    gate      the release gate at the end of the activity (docs/design/EVALUATE.md §5.2): the working line against W0
              on dev; passed, it becomes the release line's W*; nothing to do in an activity without dev tasks
    release   the release by decision (EVALUATE §7, stage 4c): in an activity without dev tasks, the working line is
              promoted to the release line once its own train reading is complete (the final prices it from that)
    evaluate  the sealed final on the test tasks (W* against W0); it reports, it does not choose
    report    the activity's report from what is on disk; no model is called
    export    the released W* as an app

`cycle --until` runs baseline, then the library, then the rounds (and with `--until gate` the release gate), and stops
after the named step; run again, it resumes, and pays for nothing already on record.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Callable

from ..adapters import automationbench as ab
from ..contracts import short, write_json
from ..evaluate.attempt import make_agent_factory, run_tasks, task_hash
from ..evaluate.final import FinalPlan, estimate_lines, final_estimate, run_final, seal, share_problem
from ..evaluate.gate import GatePlan, TaskEvalPlan, refill_attempts
from ..evaluate.release import NotAuthorized, Release, Releases
from ..evaluate.results import TaskResult, attempt_id, summarise
from ..harness.literals import findings, forbidden, literal_index
from ..harness.package import Limits, PackageError, build
from .checks import require, startup_checks

PHASES = ("smoke", "freeze", "baseline", "library", "round", "gate", "release", "evaluate", "report", "export")


# ---------------------------------------------------------------------------- one arm of tasks
def run_arm(activity, *, release: Release, split: str, candidate: str, role: str, pairs: list, account,
            plan_ref: str = "", arm: str = "subject", retry: int = 0, supersedes: dict | None = None,
            notes: str = "", allocation: str | None = None, package=None, memory=None) -> list[TaskResult]:
    """The attempts of one arm, under one run identity. Attempts already on record for this run and these pairs are
    kept as they were, not paid again; the rest run through the runner shell with K's worker. `package` runs in
    place of the release's own W (the memory stream's W_base with memory on); `memory` is what each attempt gets of
    the memory, by default the package's own snapshot when it reads one."""
    package = package if package is not None else activity.package(release)
    run = activity.run_manifest(candidate=candidate, role=role, split=split, release=release, package=package,
                                notes=notes)
    activity.results.put_run(run)
    recorded = {r.attempt_id: r for r in activity.results.of_run(run.hash())}
    want = [(t, i, attempt_id(run.hash(), t, i, arm, retry)) for t, i in pairs]
    todo = [(t, i) for t, i, a in want if a not in recorded]
    if todo:
        provider = activity.actor_provider(account, allocation=allocation)
        make = make_agent_factory(provider=provider, package=package, core=activity.k, worker=activity.worker(),
                                  root=activity.root, pricing=activity.pricing(), release_ref=release.ref(),
                                  core_ref=activity.core_ref, arm=arm,
                                  memory=memory if memory is not None else activity.memory_for(package))
        try:
            for r in run_tasks(activity.suite, [], make, run=run, splits=activity.splits, results=activity.results,
                               order=todo, arm=arm, plan_ref=plan_ref, release_ref=release.ref(), retry_index=retry,
                               supersedes=supersedes, concurrency=activity.concurrency):
                recorded[r.attempt_id] = r
        finally:
            # the provider's rate-limit refusals the meter absorbed (released, never charged): on record per run
            if getattr(provider, "rate_limited", 0):
                activity.events.append("rate_limited", run=run.hash(), candidate=candidate, split=split,
                                       count=int(provider.rate_limited))
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
    # train and dev only: the test tasks' assertions are not read before the final plan is sealed (PLAN rule 6)
    dups = ab.near_duplicates(activity.suite, threshold, ids=[*splits.ids("train"), *splits.ids("dev")])
    out(f"families   {len(splits.splits)} groups; {len(dups)} train/dev task pairs above overlap {threshold}")
    # the smoke's tasks are dev's; a split without dev (the two-way protocol, ACTIVITY §4) reads train instead —
    # never test (PLAN rule 6)
    split = "dev" if splits.ids("dev") else "train"
    pool = splits.ids(split)
    chosen = [t for t in (tasks or []) if t in pool] if tasks else pool[:int(activity.k["bench"]["smoke_tasks"])]
    chosen = chosen[:limit] if limit else chosen
    outside = sorted(set(tasks or ()) - set(pool))
    if outside:
        out(f"NOTE       {outside} are not {split} tasks: the smoke reads {split} only")
    checks = ab.contract_check(activity.suite, chosen or pool[:1])
    out(f"contract   rubric={checks['rubric'].get('funcs') or 'not importable'}")
    for line in checks["failed"]:
        out(f"FAILED     {line}")
    leaks = []
    for tid in chosen or pool[:1]:
        episode = activity.suite.episode(tid)
        try:
            found = ab.audit_inputs(activity.suite.tasks[tid], activity.suite.grading(tid),
                                    ab.agent_payload(activity.suite.tasks[tid], episode))
        finally:
            episode.close()
        leaks += found["leaks"]
    out(f"audit      {len(chosen or pool[:1])} tasks: {len(leaks)} leaks")
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
        rows = run_arm(activity, release=release, split=split, candidate="smoke", role="base",
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
                     "state": grading.initial_state, "prompt": activity.suite.tasks[tid].trigger})
    banned = forbidden(literal_index(rows))
    from ..harness.seed import seed_files
    found = findings(seed_files(activity.k["harness"]["seed"], core=activity.k), None, banned)
    return {"forbidden": len(banned), "findings": found}


# ---------------------------------------------------------------------------- freeze
def gate_template(activity) -> TaskEvalPlan | None:
    """The gate's frozen template: every dev task, K's repeats, order and thresholds; the candidate is filled in at
    each gate, the rest is never touched again. None when the activity has no dev tasks (stage 4c: the release is by
    decision, the final's two arms are the reading)."""
    if not activity.splits.ids("dev"):
        return None
    g = activity.k["gate"]
    gate = GatePlan(min_groups=int(g["min_groups"]), min_gain=float(g["min_gain"]),
                    max_regression_rate=float(g["max_regression_rate"]), missing_cost_policy=g["missing_cost_policy"],
                    regression_drop=g.get("regression_drop"), cost_beta0=g.get("cost_beta0"),
                    cost_beta1=g.get("cost_beta1"), min_tasks=g.get("min_tasks"),
                    partial_min_gain=g.get("partial_min_gain"))
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
    release = activity.released_release()
    seed = activity.seed_release()
    # An arm's name is part of every attempt id, which is a session id (runtime.records.SESSION_ID): W* is "W_star".
    # W* is the release line's (the release gate's promotion), never the working line's (EVALUATE §7, v1.2).
    arms = {"W0": seed.ref()} if release is None else {"W0": seed.ref(), "W_star": release.ref()}
    return FinalPlan(experiment_id=f"{activity.scope_id}-final", tasks=tuple(activity.splits.ids(final["tasks"])),
                     repeats=int(final["repeats"]), arms=arms, claims=tuple(final["claims"]) if len(arms) > 1 else (),
                     split_hash=activity.splits.hash(), core_ref=activity.core_ref,
                     max_infrastructure_retries=int(activity.k["gate"]["max_infrastructure_retries"]))


def freeze(activity, *, out: Callable[[str], None] = print) -> dict:
    """Freeze what the paid phases run under, and print the [rsi] lines that name it. Freezing again gives the same
    refs; a final plan different from the one sealed under the same id is refused."""
    refs = {"scope_id": activity.scope_id, "core_ref": activity.core_ref, "split_ref": activity.splits.hash()}
    plan_template = gate_template(activity)
    template = None
    if plan_template is not None:
        template = activity.objects.put(plan_template)
        activity.events.append("gate_template", plan=template)
    else:
        out("gate       no dev tasks in this split: no gate template; the release is by decision (`--phase release`)")
    root = activity.root_account()
    out(f"budget     root {activity.scope_id}: {root.remaining()['limits'] or 'no dollar limit'}, "
        f"set aside {root.remaining()['allocations'] or 'nothing'}")
    plan = final_plan(activity) if "final" in activity.config else None
    unfinished = sorted(p.parent.name for p in (activity.root / "rounds").glob("r*/round.json")
                        if json.loads(p.read_text(encoding="utf-8")).get("status") not in ("done", "stopped"))
    if plan is not None and len(plan.arms) < 2:
        out("final      not sealed: the final compares W* with W0, and no release passed the release gate yet; run "
            "`--phase gate` when the rounds are over, then freeze again")
    elif plan is not None and unfinished:
        out(f"final      not sealed: round {', '.join(unfinished)} is not finished; freeze again when the rounds and "
            f"the gate are over")
    elif plan is not None:
        out(f"final      sealing W* = the released {short(activity.released_release().ref())}: a gate after this "
            f"cannot change the final, so seal when the rounds and the gate are over")
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
    return {**refs, **({"gate_template": template} if template else {})}


def _execution(activity, release_ref):
    try:
        rel = activity.releases.load(release_ref)
    except Exception:  # noqa: BLE001 - a ref this store does not hold executes as nothing known
        return None
    return rel.package_ref, rel.core_ref


# ---------------------------------------------------------------------------- baseline
def import_baseline(activity, source_root: Path, *, out: Callable[[str], None] = print) -> dict:
    """Another workspace's W0 readings into this one (EVALUATE §8, ACTIVITY §4, v1.2): when its seed package under its
    K has this activity's AttemptIdentity, its baseline runs (roles base and control, train and dev), their results,
    grading rows and transcripts are copied as recorded, with the source on record; nothing is paid. Run again, it
    copies nothing twice. -> {imported, attempts, identity} or {imported: False, why}."""
    from ..evaluate.results import RunManifest, Results
    from ..runtime.records import session_file
    source_root = Path(source_root)
    core_file = source_root / "core.json"
    if not core_file.exists():
        return {"imported": False, "why": f"{source_root} holds no core.json"}
    src_core = json.loads(core_file.read_text(encoding="utf-8"))
    src_releases = Releases(source_root)
    if src_releases.active_ref() is None:
        return {"imported": False, "why": f"{source_root} has no release"}
    src_seed = src_releases.history()[-1]
    seed = activity.seed_release()
    if src_seed.package_ref != seed.package_ref:
        return {"imported": False, "why": "the source's W0 is another package"}
    from ..evaluate.release import attempt_identity
    theirs = attempt_identity(attempt_version=src_core.get("attempt_version") or "", package_ref=src_seed.package_ref,
                              limits=src_core.get("limits") or {}, model=src_core.get("model") or {},
                              image_id=(src_core.get("worker") or {}).get("image_id"),
                              split_hash=src_core.get("split_hash") or "")
    ours = activity.core.attempt_identity(seed.package_ref)
    if theirs != ours:
        return {"imported": False, "why": f"AttemptIdentity differs ({theirs} there, {ours} here)"}
    src = Results(source_root, activity.domain)
    runs = {h: r for h, r in src.runs().items() if r.package_ref == src_seed.package_ref
            and r.role in ("base", "control") and r.split in ("train", "dev")}
    have = {r.attempt_id for r in activity.results.all()}
    copied, sessions = [], []
    for row in src.all():
        run = runs.get(row.run)
        if run is None or row.attempt_id in have or row.validity != "valid":
            continue
        if row.run not in {h for h in activity.results.runs()}:
            activity.results.put_run(run)
        activity.results.append(row)
        copied.append(row.attempt_id)
        sessions.append(row.evidence or row.attempt_id)
    activity.results.copy_grading(src, copied)
    for session in sessions:
        try:
            source, target = session_file(source_root, session), session_file(activity.root, session)
        except ValueError:
            continue
        if source.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    if copied:
        activity.events.append("baseline_imported", source=str(source_root), attempts=len(copied), identity=ours,
                               runs=sorted(runs))
    out(f"baseline   imported {len(copied)} W0 attempts from {source_root} (AttemptIdentity {ours})")
    return {"imported": True, "attempts": len(copied), "identity": ours}


def _read_rows(activity, package_ref: str, *, split: str, candidate: str) -> list[TaskResult]:
    """The valid readings of this package under runs of this name and split, whoever recorded them (an imported
    baseline's runs carry the source's release and core), one per (task, repeat), the latest."""
    runs = activity.results.runs()
    return latest([r for r in activity.results.all() if r.split == split and r.validity == "valid"
                   and runs.get(r.run) is not None and runs[r.run].package_ref == package_ref
                   and runs[r.run].candidate == candidate and runs[r.run].role == "base"])


def baseline(activity, *, limit: int | None = None, out: Callable[[str], None] = print) -> dict:
    """W0 on train (once) and on dev `control_readings` times, each reading its own run of the same release, so the
    pooled control starts with that many readings. A restarted baseline runs only what is missing; with [rsi]
    baseline_from, what the named workspace read of the same AttemptIdentity is imported first and not paid."""
    require(startup_checks(activity, phase="baseline"), phase="baseline", activity=activity)
    release = activity.active_release()
    account = activity.account("baseline")
    train = activity.splits.ids("train")[:limit] if limit else activity.splits.ids("train")
    dev = activity.splits.ids("dev")[:limit] if limit else activity.splits.ids("dev")
    report = {"release": release.ref(), "train": None, "dev": [], "imported": None}
    source = (activity.config.get("rsi") or {}).get("baseline_from")
    if source:
        base = Path(activity.config.get("_base") or ".")
        path = Path(source).expanduser()
        report["imported"] = import_baseline(activity, path if path.is_absolute() else (base / path).resolve(),
                                             out=out)
        if not report["imported"].get("imported"):
            out(f"baseline   nothing imported from {source}: {report['imported'].get('why')}")
    out(f"baseline   release {short(release.ref())}: {len(train)} train tasks, {len(dev)} dev tasks × "
        f"{activity.k['gate']['control_readings']} readings")
    read = {r.task_id for r in _read_rows(activity, release.package_ref, split="train", candidate="base")}
    todo = [t for t in train if t not in read]
    if todo:
        _reading(activity, release=release, split="train", candidate="base", tasks=todo, account=account, out=out)
    wanted = set(train)
    report["train"] = summarise([r for r in _read_rows(activity, release.package_ref, split="train", candidate="base")
                                 if r.task_id in wanted])
    for i in range(1, int(activity.k["gate"]["control_readings"]) + 1):
        name = "base" if i == 1 else f"base:reading-{i}"
        read = {r.task_id for r in _read_rows(activity, release.package_ref, split="dev", candidate=name)}
        todo = [t for t in dev if t not in read]
        if todo:
            _reading(activity, release=release, split="dev", candidate=name, tasks=todo, account=account, out=out)
        report["dev"].append(summarise([r for r in _read_rows(activity, release.package_ref, split="dev",
                                                              candidate=name) if r.task_id in set(dev)]))
    return report


# ---------------------------------------------------------------------------- the memory stream
def retry_note(checks) -> str:
    """The retry's note: the failed checks of the first encounter by type and params (train grading only)."""
    failed = [c for c in checks or () if not c.get("passed") and not c.get("excluded")]
    if not failed:
        return ""
    lines = [f"- {c.get('type')}: {json.dumps(c.get('params'), ensure_ascii=False, default=str)[:300]}"
             for c in failed[:12]]
    return ("A previous attempt at this very task was graded and failed these checks. Work the task again from the "
            "start and make sure the result satisfies them:\n" + "\n".join(lines))


def stream_order(activity) -> list[str]:
    """The train tasks in the stream's order: shuffled once by K's gate.order_seed and the domain, then frozen in the
    stream's ledger."""
    order = list(activity.splits.ids("train"))
    random.Random(f"library:{activity.k['gate']['order_seed']}:{activity.domain}").shuffle(order)
    return order


def base_readings(activity, release: Release) -> dict:
    """W_base's valid train readings per task: its own package's runs, never the stream's (role "library": under a K
    with the memory on the stream's package is W_base's own). -> {task_id: [rows]}."""
    runs = activity.results.runs()
    out: dict = {}
    for r in activity.results.all():
        run = runs.get(r.run)
        if r.split != "train" or r.validity != "valid" or run is None or run.package_ref != release.package_ref \
                or run.role == "library":
            continue
        out.setdefault(r.task_id, []).append(r)
    return out


def library(activity, *, out: Callable[[str], None] = print) -> dict:
    """The memory's train stream and its snapshot (docs/design/MEMORY.md §7). In the stream's frozen order, a batch at
    a time: each task's first encounter under W_base with the live library (leave-one-out), a failed one tried once
    more with its failed checks as a note, then the batch's trajectories (the baseline's too) summarised by ReMe
    through the metered bridge, checked and written, the first encounters credited. At the end the readout, and —
    unless D <= abort_d, the library is empty or none of it fits — the snapshot as a candidate package for the
    improver's ladder (stage 3): its rows as a package carries them, ranked by credit and kept within K's
    max_memory_bytes, what it left out recorded in the `library_snapshot` event (MEMORY §7.1). Run again, it
    continues where it stopped and pays for nothing on record. A failure of the memory's sidecar pauses the phase
    (status `paused`): nothing is recorded as lost for it."""
    require(startup_checks(activity, phase="library"), phase="library", activity=activity)
    from ..contracts import MemorySidecarError
    from ..memory.track import MemoryStepError
    from ..memory import snapshot_digest
    from ..memory.settings import Settings
    from ..memory.snapshot import snapshot_candidate, stream_files
    from ..runtime.trace import TraceStore
    settings = Settings.from_core(activity.k)
    base = activity.active_release()
    base_package = activity.package(base)
    limits = Limits.from_core(activity.k)
    seed_package = activity.package(activity.seed_release())
    stream_package = build(activity.objects, stream_files(base_package), limits=limits, seed=seed_package)
    train = activity.splits.ids("train")
    tasks = {t: activity.suite.tasks[t] for t in train}
    hashes = {t: task_hash(tasks[t]) for t in train}
    by_hash = {h: t for t, h in hashes.items()}
    readings = base_readings(activity, base)
    if not readings:
        raise ValueError(f"the library compares each first encounter with W_base's own train readings, and "
                         f"{short(base.ref())} has none: run the baseline first")
    b = {t: sum(int(r.success) for r in rows) / len(rows) for t, rows in readings.items()}
    trace = TraceStore(activity.root, activity.splits.split_of)
    rows = [{"task_id": t, "assertions": [dict(a) for a in activity.suite.grading(t).assertions],
             "state": activity.suite.grading(t).initial_state, "prompt": activity.suite.tasks[t].trigger}
            for t in train]
    banned = forbidden(literal_index(rows))
    account = activity.account("library")
    retries = int(activity.k["gate"]["max_infrastructure_retries"])
    earlier = {r.attempt_id for r in activity.results.all()}       # on record before this run of the phase

    def attempt(row) -> dict | None:
        if row is None:
            return None
        said = trace.finish(row.attempt_id)
        return {"session": row.attempt_id, "reward": float(row.partial_credit), "passed": bool(row.success),
                "release_ref": row.system_release_ref, "shown": said["shown"], "final_reason": said["reason"],
                "final_text": said["text"], "trace_refs": [trace.ref(row.attempt_id, 1)]}

    def arm_of(name: str, memory, pairs) -> list[TaskResult]:
        def run_from(offset: int):
            def run(arm, chosen, retry=0, supersedes=None):
                return run_arm(activity, release=base, split="train", candidate="library", role="library",
                               pairs=chosen, account=account, arm=arm, retry=offset + retry, supersedes=supersedes,
                               package=stream_package, memory=memory, notes=f"memory stream: {name}")
            return run
        rows = latest(refill_attempts(run_from(0)(name, pairs), run_from(0), name, retries))
        # An attempt the budget refused on an earlier run of the phase is no evidence, and the stream has no
        # comparison for the refusal to end (EVALUATE §5): it runs again, superseding it, at the next block of retry
        # indexes. A block on record is reused, so a later run pays for nothing twice.
        offset = 0
        while True:
            refused = {(r.task_id, r.attempt): r.attempt_id for r in rows
                       if r.failure_class == "budget_exhausted" and r.attempt_id in earlier}
            if not refused:
                return rows
            offset += retries + 1
            run = run_from(offset)
            rows = latest(rows + refill_attempts(run(name, sorted(refused), 0, refused), run, name, retries))

    def paused(e: Exception) -> dict:
        out(f"library    paused: the memory's sidecar failed ({type(e).__name__}: {str(e)[:200]}); run the phase "
            f"again to continue")
        return {"status": "paused", "why": f"{type(e).__name__}: {str(e)[:300]}"}

    service = activity.memory()
    try:
        service.start(research=activity.research_provider("memory", retries=False), concurrency=activity.concurrency)
    except MemorySidecarError as e:
        service.stop()
        return paused(e)
    try:
        step = service.stream(train_texts={hashes[t]: tasks[t].trigger for t in train}, forbidden=banned,
                              trajectory_of=lambda s: trace.trajectory(s, annotations=False),
                              activity=activity.scope_id, concurrency=activity.concurrency)
        step.resume()
        for spec in settings.imports:
            imported = _import(activity, step, spec)
            out(f"library    imported {spec['release_ref'][:24]}: {len(imported['added'])} added, "
                f"{len(imported['dropped'])} dropped{' (already imported)' if imported['again'] else ''}")
        todo = step.plan([hashes[t] for t in stream_order(activity)])
        out(f"library    W_base {short(base.ref())}: {len(train)} train tasks, {len(todo)} to stream, "
            f"{len(step.active())} entries live")
        for i in range(0, len(todo), settings.stream_batch):
            batch = [by_hash[h] for h in todo[i:i + settings.stream_batch]]
            live = step.live_view()
            firsts = {r.task_id: r for r in arm_of("first", activity.memory_for(stream_package, live=live),
                                                   [(t, 0) for t in batch])}
            if any(r.failure_class == "budget_exhausted" for r in firsts.values()):
                out("library    stopped: the budget refused a first encounter; run the phase again to continue")
                return {"status": "budget_exhausted", "processed": len(step.ledger["processed"])}
            notes = {t: retry_note(activity.results.grading(r.attempt_id)) for t, r in firsts.items()
                     if settings.retry and r.validity == "valid" and not r.success}
            notes = {t: n for t, n in notes.items() if n}
            seconds = {}
            if notes:
                memory = activity.memory_for(stream_package, live=live, note_of=lambda task: notes[task.task_id])
                seconds = {r.task_id: r for r in arm_of("retry", memory, [(t, 0) for t in sorted(notes)])}
            if any(r.failure_class == "budget_exhausted" for r in seconds.values()):
                out("library    stopped: the budget refused a retry; run the phase again to continue")
                return {"status": "budget_exhausted", "processed": len(step.ledger["processed"])}
            items, lost = [], []
            for t in batch:
                first = firsts.get(t)
                if first is None or first.validity != "valid":
                    lost.append((hashes[t], f"the first encounter is {first.failure_class if first else 'missing'}"))
                    continue
                second = seconds.get(t)
                base_row = readings.get(t, [None])[-1]
                items.append(({"task_hash": hashes[t], "text": tasks[t].trigger, "b": b.get(t),
                               "stable_pass": b.get(t) == 1.0, "split": "train"},
                              attempt(first), attempt(second if second is not None and second.validity == "valid"
                                                      else None), attempt(base_row)))
            if lost and not service.healthy():
                raise MemorySidecarError("the sidecar does not answer: the lost first encounters are its, not the "
                                         "tasks'")
            if items:
                step.process_batch(items)
            for h, why in lost:              # after the batch is written, so a failed batch is cut the same next run
                step.lose(h, why)
            done = step.ledger["processed"]
            out(f"library    {len(done)}/{len(train)}: " + ", ".join(
                f"{t} first={int(firsts[t].success) if t in firsts else '-'}"
                f"{' retry=' + str(int(seconds[t].success)) if t in seconds else ''}"
                f" +{len(done[hashes[t]].get('added') or ())}" for t in batch) + f"; {len(step.active())} live")
        result = step.finish(max_bytes=limits.max_memory_bytes)
    except (MemorySidecarError, MemoryStepError) as e:
        # a step that could not be trusted stores nothing; the summaries that passed are on the ledger
        return paused(e)
    finally:
        service.stop()
    ro, snapshot, fit = result["readout"], result["snapshot"], result["fit"]
    out(f"library    D={ro['D']} B={ro['B']} halves {ro['first_half']} -> {ro['second_half']} "
        f"(P {ro['p_second_half_better']}); entries {ro['entries']}")
    best = fit["omitted"][0] if fit["omitted"] else None
    out(f"library    snapshot: {fit['kept']}/{fit['entries']} entries, {fit['bytes']} bytes of K's {fit['max_bytes']}"
        + (f"; {len(fit['omitted'])} left out by rank ({fit['rank']}), the best of them with {best['fixes']} fixes "
           f"and {best['n_cited']} citations" if best else ""))
    outcome = {"outcome": "not_gated", "why": result["abort"]} if result["abort"] else None
    if outcome is None:
        try:
            candidate = build(activity.objects, snapshot_candidate(base_package, snapshot), limits=limits,
                              seed=seed_package)
            outcome = {"outcome": "candidate", "package_ref": candidate.ref(), "parent": base.ref()}
        except PackageError as e:
            outcome = {"outcome": "not_gated", "why": f"the snapshot does not fit K's package limits: {e}"}
    outcome["snapshot_digest"] = snapshot_digest(snapshot)
    if not any(e.get("snapshot_digest") == outcome["snapshot_digest"] and e.get("base") == base.ref()
               for e in activity.events.read("library_snapshot")):
        activity.events.append("library_snapshot", base=base.ref(), readout=ro, fit=fit, **outcome)
    if outcome["outcome"] == "candidate":
        out(f"library    the snapshot is a candidate: {short(outcome['package_ref'])} (the improver's ladder takes "
            f"it from V3; stage 3)")
    else:
        out(f"library    the snapshot is not gated: {outcome['why']}")
    return {"status": "done", "readout": ro, "snapshot": outcome}


def _import(activity, step, spec: dict) -> dict:
    """One of K's memory.imports: the release's package must be in this activity's object store."""
    ref = spec["release_ref"]
    if not activity.objects.has(ref):
        raise ValueError(f"[memory] imports names {ref}, which this workspace's object store does not hold: copy the "
                         f"release's closure in first (contracts.copy_closure)")
    package = activity.package(activity.releases.load(ref))
    from ..memory.client import parse_jsonl
    rows = parse_jsonl(package.memory_text)
    lines = sorted({(r.get("metadata") or {}).get("ol_line") for r in rows})
    if len(lines) != 1:
        raise ValueError(f"{ref}: a snapshot of one business line is imported, not {lines}")
    return step.import_snapshot(package.memory_text, source_release_ref=ref, source_line=lines[0])


def round_(activity, index: int, *, out: Callable[[str], None] = print):
    """One round of the improver (docs/design/IMPROVER.md §3); run again, it continues and pays for nothing done."""
    require(startup_checks(activity, phase="round"), phase="round", activity=activity)
    from ..improver.round import run_round
    return run_round(activity, index, run_arm=run_arm, out=out)


# ---------------------------------------------------------------------------- the release gate
def gate(activity, *, out: Callable[[str], None] = print) -> dict:
    """The release gate at the end of the activity (EVALUATE §5.2; IMPROVER §9.2): the working line's W against W0 on
    every dev task of the frozen template — W read `repeats` times plus the plan's confirmation, W0 the pooled control
    of the baseline's readings, topped up. Accepted (confirmed when the plan confirms), the working line's release is
    promoted to the release line (`rsi/released.json`, reason `gate`); otherwise there is no release and the report
    says so. Run again, it reads what is on record and pays for nothing done; a gate of a working line that moved
    since is a new gate."""
    require(startup_checks(activity, phase="gate"), phase="gate", activity=activity)
    from ..evaluate.gate import evaluate_task, receipt_differences
    from ..improver.ledger import Ledger
    from ..improver.screen import control_pool, cost_per_attempt, gate_attempts, gate_plan
    from .checks import cli_change_problem
    work, seed = activity.active_release(), activity.seed_release()
    path = activity.root / "rsi" / "gate.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if state.get("status") == "done" and state.get("working") == work.ref():
        out(f"gate       on record: {state.get('verdict')} for {short(work.ref())}"
            + (f"; released {short(state['released'])}" if state.get("released") else ""))
        return state
    if not activity.splits.ids("dev"):
        out("gate       no dev tasks in this split: nothing to gate; the release is by decision (`--phase release`)")
        return {"status": "nothing_to_gate", "why": "no dev tasks", "working": work.ref()}
    if work.ref() == seed.ref():
        out("gate       the working line is W0: nothing was accepted, nothing to gate")
        return {"status": "nothing_to_gate", "why": "the working line is W0", "working": work.ref()}
    unfinished = sorted(p.parent.name for p in (activity.root / "rounds").glob("r*/round.json")
                        if json.loads(p.read_text(encoding="utf-8")).get("status") not in ("done", "stopped"))
    if unfinished:
        raise ValueError(f"gate refused: round {', '.join(unfinished)} is not finished and can still move the working "
                         f"line; finish or stop it first")
    problem = cli_change_problem(activity)
    if problem:
        raise ValueError(f"gate refused: {problem}")
    package = activity.package(work)
    plan = gate_plan(activity, package=package, base_release=seed)
    plan_ref = activity.objects.put(plan)
    account = activity.account("gate")
    name = f"gate:{short(work.ref())}"
    recorded = {r.task_id for r in activity.results.all() if r.validity == "valid"
                and activity.results.runs().get(r.run) is not None and activity.results.runs()[r.run].candidate == name}
    if not recorded:
        dev = cost_per_attempt(activity, seed.package_ref, "dev")
        train0, train = cost_per_attempt(activity, seed.package_ref, "train"), \
            cost_per_attempt(activity, work.package_ref, "train")
        scale = max(1.0, train / train0) if train and train0 else 1.0
        usd = None if dev is None else round(gate_attempts(activity, seed.package_ref) * dev * scale * 1.15, 6)
        left = (account.remaining().get("effective_remaining") or {}).get("usd")
        out(f"gate       {short(work.ref())} against W0 on {len(plan.task_ids)} dev tasks, up to "
            f"{gate_attempts(activity, seed.package_ref)} attempts"
            + (f", about ${usd:.2f} at the costs on record" if usd is not None else ", unpriced")
            + (f"; ${float(left):.2f} left outside the final's share" if left is not None else ""))
        if usd is not None and left is not None and usd > float(left) + 1e-9:
            raise ValueError(f"gate refused: about ${usd:.2f} does not fit the ${float(left):.2f} left outside the "
                             f"final's share; nothing was run")
    families = {t: activity.splits.family_of(t) for t in plan.task_ids}

    def run(arm, order, retry=0, supersedes=None):
        if arm == "control":
            return run_arm(activity, release=seed, split="dev", candidate=f"control:{short(seed.ref())}:gate",
                           role="control", pairs=order, account=account, arm="control", retry=retry,
                           supersedes=supersedes, notes="the release gate's control top-up")
        # the working line's own release runs its package: the reading is W_work's (the final's estimate reads it)
        return run_arm(activity, release=work, split="dev", candidate=name, role="subject", pairs=order,
                       account=account, plan_ref=plan_ref, arm=arm, retry=retry, supersedes=supersedes,
                       notes=f"the release gate of {short(work.ref())}")
    receipt = evaluate_task(plan, run=run, objects=activity.objects, families=families,
                            control_pool=control_pool(activity, seed.package_ref, plan.task_ids))
    receipt_ref = activity.objects.put(receipt)
    activity.releases.register_receipt(receipt_ref, phase="gate", working=work.ref())
    diff = receipt_differences(receipt, activity.objects)
    metrics = receipt.metrics or {}
    result = {"status": "done", "working": work.ref(), "receipt": receipt_ref, "verdict": receipt.verdict,
              "kind": receipt.kind, "gain": metrics.get("gain"), "partial_gain": metrics.get("partial_gain"),
              "fixed_count": len(diff["fixed"]), "regressed_count": len(diff["regressed"]), "released": None,
              "reasons": list(receipt.reasons)}
    if receipt.verdict == "accepted":
        released = activity.releases.promote(work, receipt_ref, operation_id=f"promote:{short(work.ref())}")
        result["released"] = released.ref()
    Ledger(activity.root).row("gated", round_id=None, line=activity.domain, dir="working-line",
                              verdict=receipt.verdict, gain=result["gain"], fixed_count=result["fixed_count"],
                              regressed_count=result["regressed_count"], release=result["released"])
    activity.events.append("release_gate", **{("receipt_kind" if k == "kind" else k): v for k, v in result.items()
                                             if k != "reasons"})
    write_json(path, result)
    out(f"gate       {receipt.verdict} ({receipt.kind}): gain {result['gain']}, partial {result['partial_gain']}, "
        f"dev fixed {result['fixed_count']}, regressed {result['regressed_count']}"
        + (f"; released {short(result['released'])}" if result["released"] else "; no release")
        + (f"; {'; '.join(receipt.reasons)[:300]}" if receipt.reasons and receipt.verdict != "accepted" else ""))
    return result


# ---------------------------------------------------------------------------- the release by decision
def release(activity, *, out: Callable[[str], None] = print) -> dict:
    """The release by decision (EVALUATE §7, stage 4c): in an activity without dev tasks, the working line's release is
    promoted to the release line (reason `decision`, no receipt) once its package's train reading is complete — the
    tasks it lacks (the studied part; the held-out part was read at the screen) are read under its own release first
    (role `measure`), so the final's estimate prices W* from them. An
    activity with dev tasks releases by its gate; a working line that is still W0 has nothing to release; a running
    round refuses. Run again, it promotes nothing twice and pays nothing."""
    require(startup_checks(activity, phase="release"), phase="release", activity=activity)
    if activity.splits.ids("dev"):
        raise ValueError("release refused: this activity has dev tasks, so its release is the gate's (`--phase gate`)")
    work, seed = activity.active_release(), activity.seed_release()
    released = activity.releases.released()
    if released is not None and released.parent_ref == work.ref():
        out(f"release    on record: {short(released.ref())} released from {short(work.ref())}")
        return {"status": "done", "working": work.ref(), "released": released.ref(), "measured": 0}
    if work.ref() == seed.ref():
        raise ValueError("release refused: the working line is W0: nothing was accepted, nothing to release")
    unfinished = sorted(p.parent.name for p in (activity.root / "rounds").glob("r*/round.json")
                        if json.loads(p.read_text(encoding="utf-8")).get("status") not in ("done", "stopped"))
    if unfinished:
        raise ValueError(f"release refused: round {', '.join(unfinished)} is not finished and can still move the "
                         f"working line; finish or stop it first")
    # what W*'s package has no valid train reading of: the held-out part was read at its screen (under the parent's
    # release, with this package), the studied part is read now under W*'s own release — enough for the final's
    # estimate, which prices an arm from readings of a release that executes as it does
    runs = activity.results.runs()
    read = {r.task_id for r in activity.results.all() if r.split == "train" and r.validity == "valid"
            and runs.get(r.run) is not None and runs[r.run].package_ref == work.package_ref
            and runs[r.run].role != "library"}
    train = activity.splits.ids("train")
    todo = [t for t in train if t not in read]
    out(f"release    {short(work.ref())}: {len(todo)} of {len(train)} train tasks still to read for its package")
    if todo:
        account = activity.account("release")

        def run(arm, chosen, retry=0, supersedes=None):
            return run_arm(activity, release=work, split="train", candidate=f"release:{short(work.ref())}",
                           role="measure", pairs=chosen, account=account, arm=arm, retry=retry, supersedes=supersedes,
                           notes="the released W's own train reading")
        rows = refill_attempts(run("subject", [(t, 0) for t in todo]), run, "subject",
                               int(activity.k["gate"]["max_infrastructure_retries"]))
        if any(r.failure_class == "budget_exhausted" for r in rows):
            raise ValueError("release refused: the budget refused the released W's train reading; nothing was promoted")
    promoted = activity.releases.promote(work, None, reason="decision", operation_id=f"release:{short(work.ref())}")
    activity.events.append("release_decision", working=work.ref(), released=promoted.ref(), measured=len(todo))
    result = {"status": "done", "working": work.ref(), "released": promoted.ref(), "measured": len(todo)}
    write_json(activity.root / "rsi" / "release.json", result)
    out(f"release    {short(promoted.ref())} released by decision from the working line {short(work.ref())}; "
        f"freeze seals the final next")
    return result


# ---------------------------------------------------------------------------- the final
def evaluate(activity, *, out: Callable[[str], None] = print) -> dict:
    """The sealed final ([rsi] final_plan_ref). It prints the expected spend first and refuses when what is left of the
    final's share cannot hold it; run again, it runs only what is missing."""
    ref = (activity.config.get("rsi") or {}).get("final_plan_ref")
    if not ref or not activity.objects.has(ref):
        raise ValueError("[rsi] final_plan_ref must name the final plan `ouroloop freeze` sealed")
    plan = FinalPlan.from_json(activity.objects.get(ref))
    require(startup_checks(activity, phase="evaluate"), phase="evaluate", activity=activity)
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
    """The release line's W* as an app (the seed too, as the first released thing: W0 is what runs before any gate
    passed); a working-line release that no gate promoted is not exported."""
    from ..runtime.app import export as export_app
    ref = release_ref or activity.releases.released_ref() or activity.seed_release().ref()
    released = {e.get("release") for e in activity.events.read("released")} | {activity.seed_release().ref()}
    if ref not in released or ref not in activity.releases.installed() or activity.releases.revoked(ref):
        raise NotAuthorized(f"{short(ref)} is not a released, unrevoked release (the release line's W* or W0)")
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
    """baseline -> library -> rounds (-> gate), stopping after `until` (baseline, library, round-K or gate); each step
    is on record, so running the cycle again resumes and pays for nothing already done. The rounds stop after
    schedule.patience rounds in a row without a held-out acceptance, or at schedule.rounds; the release gate runs only
    under `--until gate` (ACTIVITY §4: the user starts it when the rounds are over)."""
    path = activity.root / "rsi" / "cycle.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"steps": {}}
    rounds = int(activity.k["schedule"]["rounds"])
    stops = ("baseline", "library") + tuple(f"round-{i}" for i in range(rounds)) + ("gate",)
    if until is not None and until not in stops:
        raise ValueError(f"--until {until!r}: one of {list(stops)}")
    if "baseline" not in state["steps"]:
        state["steps"]["baseline"] = baseline(activity, out=out)
        write_json(path, state)
    if until == "baseline":
        return {**state, "status": "stopped", "at": "baseline"}
    if (state["steps"].get("library") or {}).get("status") != "done":
        if (activity.k.get("memory") or {}).get("enabled"):
            state["steps"]["library"] = library(activity, out=out)
        else:
            state["steps"]["library"] = {"status": "done", "skipped": "[memory] enabled = false"}
        write_json(path, state)
        if state["steps"]["library"].get("status") != "done":
            return {**state, "status": "stopped", "at": "library"}
    if until == "library":
        return {**state, "status": "stopped", "at": "library"}
    idle, reason = 0, None
    for i in range(rounds):
        key = f"round-{i}"
        if (state["steps"].get(key) or {}).get("status") not in ("done", "stopped"):
            result = round_(activity, i, out=out)
            state["steps"][key] = {"status": result.status, "installed": result.installed, "stop": result.stop,
                                   "accepted": result.accepted}
            write_json(path, state)
            if result.status != "done" and result.stop and result.stop.startswith(("budget", "unpriced", "cli")):
                return {**state, "status": "stopped", "at": key}
        # the patience counts rounds in a row without a held-out acceptance (IMPROVER §3)
        idle = 0 if state["steps"][key].get("accepted") else idle + 1
        if until == key:
            return {**state, "status": "stopped", "at": key}
        if idle >= int(activity.k["schedule"]["patience"]):
            reason = f"{idle} rounds without an acceptance"
            state["rounds_ended"] = {"at": key, "reason": reason}
            write_json(path, state)
            break
    if until != "gate":
        return {**state, "status": "done" if reason is None else "stopped", **({"reason": reason} if reason else {}),
                "at": "rounds"}
    state["steps"]["gate"] = gate(activity, out=out)
    write_json(path, state)
    return {**state, "status": "done", "at": "gate"}
