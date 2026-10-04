"""The ceiling experiment (docs/ROADMAP.md §3 v0.3 step 4; examples/automationbench/ceiling/README.md).

Two arms on one frozen task list, under the v3 runner shell, recorded like any arm: `wstar` (v0.2's W* package, its
v1 manifest as it was installed) and `ceiling` (the hand-written agent package: W*'s components plus full-text
reading, an evidence table before the first write, a check before finishing, effort at K's cap). Both run on the same
tasks, the same number of attempts each, the same pinned actor and effort, in one workspace of their own.

    .venv/bin/python examples/automationbench/ceiling_run.py --config examples/automationbench/claude-ceiling.toml \
        --checks-only        # every startup check, the draft checks of both packages; no model call
    .venv/bin/python examples/automationbench/ceiling_run.py --config examples/automationbench/claude-ceiling.toml

What it reuses, in order: `bench._cycle_config` freezes the preset's plans and the [freeze.budget] root (as the cycle
would); `bench.open_activity` bootstraps W0/R0/K from the config; `bench.startup_checks` is the paid phases' own list;
each package goes through `Drafts.check` exactly as an R candidate's draft does (shape and K's component kinds, the
train-literal check, the isolated probe, the agent smoke, the package's tests, in the activity's qualified worker);
`policies.build_candidate` turns it into a candidate W (W0 with worker.harness replaced, nothing else); each arm and
split runs under a TaskEvalPlan that installs nothing, a permit and a candidate release, through
`Improvement.run_arm` with up to three infrastructure refills (`task_gate.refill`) — the train-side screen's path.
Attempts, transcripts, gateway logs, broker events and grading land in the workspace as any arm's do.

The task list is read, never chosen: `[ceiling] tasks` names the frozen file (ceiling/tasks.json), the runner checks
it against the workspace's split (same split hash, dev tasks in dev, train tasks in train, none in test) and runs
exactly those tasks. Repeats alternate the arm order (repeat 0: wstar first; repeat 1: ceiling first), so neither arm
always runs last. A stop of the budget root part way through leaves pairs without both arms' attempts, and decide()
then says `incomplete`. An attempt ends one of three ways for the table: a valid result; invalid because the arm's
own package crashed (its agent program raised at run time, AgentRunError, or the candidate could not be assembled),
which counts as a failed attempt of that arm — it is the package's behaviour; or invalid for the environment (worker,
provider, budget), which leaves the pair unread. The result — per task and arm, the failed assertion types, the
attempts a hard limit ended and the package's crashes, cost per attempt, and the decision rule's numbers (README,
ROADMAP step 4) — is written to <workspace>/ceiling/result.json and result.md and printed. A rerun resumes: recorded
attempts are not paid again.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from ouroloop import bench, config as config_mod, policies as P  # noqa: E402
from ouroloop.agent import harness_package as hp  # noqa: E402
from ouroloop.benchmark import summarise  # noqa: E402
from ouroloop.evolve import contracts as ct  # noqa: E402
from ouroloop.evolve.task_gate import TaskEvalPlan, refill  # noqa: E402
from ouroloop.ledger import write_json  # noqa: E402
from ouroloop.research import diagnosis as dg  # noqa: E402
from ouroloop.research.drafts import Drafts  # noqa: E402

SCHEMA = "ouroloop.ceiling_tasks.v1"
ARMS = ("wstar", "ceiling")             # the comparison arm and the hand package; repeat 0 runs them in this order
SPLITS = ("dev", "train")
CEILING_KEYS = {"tasks", "attempts", "packages", "estimate"}
MIN_STILL_FAILING = 4                   # the decision needs at least this many tasks W* still fails in the run
INFRASTRUCTURE_RETRIES = 3              # refills of an attempt lost to infrastructure (the plan's retry policy)
# How run_attempt records an agent program that raised at run time under the runner shell (task_eval: AgentRunError,
# failure_class "infrastructure", termination harness_error): for this experiment that is the arm's own behaviour.
PACKAGE_CRASH = "harness: AgentRunError"


# ---------------------------------------------------------------------------- inputs
def load_tasks(path) -> dict:
    """The frozen task file, read-only: -> {"dev", "train", "sha256", "spec"}. Refuses a file that is not the frozen
    schema, names a task twice, or names none."""
    raw = Path(path).read_bytes()
    spec = json.loads(raw.decode("utf-8"))
    if not isinstance(spec, dict) or spec.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not a {SCHEMA} file")
    lists = {}
    for split in SPLITS:
        entries = spec.get(split) or []
        if not isinstance(entries, list) or not all(isinstance(e, dict) and isinstance(e.get("task_id"), str)
                                                    for e in entries):
            raise ValueError(f"{path}: {split} is a list of {{task_id, ...}}")
        lists[split] = [e["task_id"] for e in entries]
    every = lists["dev"] + lists["train"]
    if not every:
        raise ValueError(f"{path}: names no task")
    if len(set(every)) != len(every):
        raise ValueError(f"{path}: names a task twice")
    return {**lists, "sha256": hashlib.sha256(raw).hexdigest(), "spec": spec}


def ceiling_settings(cfg: dict, base) -> dict:
    """[ceiling]: the frozen task file, attempts per task and arm, the two arms' package directories, and the
    per-attempt cost estimate used only to print what the run should cost. Paths are relative to the config."""
    table = dict(cfg.get("ceiling") or {})
    unknown = sorted(set(table) - CEILING_KEYS)
    if unknown:
        raise ValueError(f"[ceiling] does not take {unknown}; it takes {sorted(CEILING_KEYS)}")
    packages = dict(table.get("packages") or {})
    if sorted(packages) != sorted(ARMS):
        raise ValueError(f"[ceiling] packages names exactly the arms {list(ARMS)}, not {sorted(packages)}")
    attempts = table.get("attempts", 2)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError(f"[ceiling] attempts is a whole number of at least 1, not {attempts!r}")
    estimate = {arm: float(v) for arm, v in dict(table.get("estimate") or {}).items() if arm in ARMS}
    return {"tasks": Path(base) / str(table.get("tasks") or "ceiling/tasks.json"), "attempts": attempts,
            "packages": {arm: Path(base) / str(packages[arm]) for arm in ARMS}, "estimate": estimate}


def read_package(path) -> dict:
    root = Path(path)
    if not root.is_dir():
        raise ValueError(f"{root}: no package directory")
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


# ---------------------------------------------------------------------------- the checks
def prepare(cfg: dict, base, *, out=print) -> dict:
    """Every check before a model is called: the plans and the root frozen, the activity opened (W0 bootstrapped on
    the first run), the paid phases' startup checks, the task file against the split, both packages through the
    draft check, both candidate W built. -> {"problems", ...}; nothing here calls a model."""
    settings = ceiling_settings(cfg, base)
    tasks = load_tasks(settings["tasks"])
    improvement = bench.improvement_settings(cfg)
    if improvement is None:
        raise ValueError("the ceiling runs under an [improvement] preset (the v3 runner shell); the config has none")
    cfg = bench._cycle_config(cfg, base, improvement, out)
    activity = bench.open_activity(cfg, base, out=out)
    problems = list(bench.startup_checks(activity, cfg, phase="ceiling", needs_model=True))
    harness = bench._harness_of(activity) or {}
    if not harness.get("max_effort"):
        problems.append("the activity's K fixes no max_effort: it is not a v3 core, and the runner shell would not run")
    source = tasks["spec"].get("source") or {}
    if source.get("split_hash") and source["split_hash"] != activity.splits.hash():
        problems.append(f"the task file was chosen on split {source['split_hash']} and this workspace's split is "
                        f"{activity.splits.hash()}: the same task ids would not be the same tasks' split")
    known = {e.task_id: activity.splits.split_of(e.task_id) for e in activity.splits.entries}
    for split in SPLITS:
        for task_id in tasks[split]:
            if task_id not in known:
                problems.append(f"{task_id} is not a task of this workspace's split")
            elif known[task_id] != split:
                problems.append(f"{task_id} is listed under {split} and is a {known[task_id]} task"
                                + ("; test is never run here" if known[task_id] == "test" else ""))
    # The frozen template's comparison settings (seed, per-attempt limits), as every phase of the activity reads them.
    work = bench.improvement(activity, template_ref=cfg["rsi"].get("task_plan_ref"), pricing=bench.pricing_of(cfg))
    parent = activity.releases.active()["release_ref"]
    bound = P.bind_release(parent, profile="preflight", services=work.services())
    if bound.package is None:
        problems.append("the active release mounts no package")
        return {"problems": problems, "cfg": cfg, "settings": settings, "tasks": tasks}
    limits = hp.Limits.from_json(harness["package"])
    drafts = Drafts(activity.objects, parent_ref=bound.package.ref, limits=limits,
                    literals=dg.train_literals(activity.results) | work.train_state_literals(),
                    worker=work.worker, scope=work.scope_id, allowed_literals=work.tool_vocabulary(),
                    fixture=work.fixture_world, reserved_names=work.tool_vocabulary(names_only=True),
                    image=harness.get("image_id") or harness.get("image"))
    arms: dict = {}
    for arm in ARMS:
        try:
            files = read_package(settings["packages"][arm])
            errors, package_ref = drafts.check(files)
        except Exception as e:  # noqa: BLE001 - a package that cannot be checked is a refusal, named
            errors, package_ref = [f"{type(e).__name__}: {e}"[:300]], None
        if errors:
            problems += [f"the {arm} package fails its draft check: {e}" for e in errors]
            continue
        package = activity.objects.load(package_ref, ct.HarnessPackage)
        w_star = source.get("w_star") or {}
        if arm == "wstar" and w_star.get("package_bundle_ref") and \
                (package.bundle_ref, package.test_bundle_ref) != (w_star["package_bundle_ref"],
                                                                  w_star.get("package_test_bundle_ref")):
            problems.append(f"the wstar package's files are not W*'s: bundle {package.bundle_ref} against the "
                            f"recorded {w_star['package_bundle_ref']}")
        view = hp.load_package(activity.objects, package_ref)
        proposal = ct.ExperimentProposal(
            owner="worker", origin="manual",
            hypothesis=f"ceiling experiment arm {arm}: the package at {settings['packages'][arm].name}",
            changes=({"target": "worker.harness", "kind": "workflow",
                      "change": {"builtin": view.api_version, "package_ref": package_ref}},))
        stand_in = ct.Experiment(id=f"ceiling-{arm}", proposal_ref=activity.objects.put(proposal),
                                 parent_release=parent, experience_ref="", core_ref=work.core_ref,
                                 eval_template_ref=work.template_ref, budget_scope=work.scope_id)
        build = P.build_candidate(stand_in, objects=activity.objects, parent_release=parent, owner="worker",
                                  services=work.services())
        if build.status != "ready":
            problems.append(f"the {arm} candidate does not build: {'; '.join(build.errors)[:300]}")
            continue
        arms[arm] = {"package_ref": package_ref, "candidate_ref": build.candidate_ref, "api": view.api_version,
                     "files": len(files), "bytes": sum(len(t.encode("utf-8")) for t in files.values()),
                     "agent": (view.agent or {}).get("entrypoint") or "default agent",
                     "roles": list(view.roles)}
    return {"problems": problems, "cfg": cfg, "settings": settings, "tasks": tasks, "activity": activity,
            "work": work, "parent": parent, "arms": arms, "harness": harness}


def estimate(settings: dict, tasks: dict) -> dict:
    n = len(tasks["dev"]) + len(tasks["train"])
    per_arm = n * settings["attempts"]
    usd = {arm: round(per_arm * settings["estimate"][arm], 2) for arm in ARMS if arm in settings["estimate"]}
    return {"tasks": n, "attempts": per_arm * len(ARMS), "attempts_per_arm": per_arm,
            "nominal_usd": usd, "nominal_usd_total": round(sum(usd.values()), 2) if usd else None}


# ---------------------------------------------------------------------------- the decision (README, ROADMAP step 4)
def decide(per_task: dict, attempts: int) -> dict:
    """The rule written before any run. `per_task`: task -> {"split", "persistent", "arms": {arm: [success, ...]}}
    over each repeat's final row (tabulate): a valid attempt, or a failure where the arm's own package crashed; an
    attempt lost to the environment has no entry. A task is read when both arms have `attempts` entries. R: read tasks the W* arm fails
    on every attempt (still failing in this run); X: tasks of R the ceiling passes at least once (fixed); B: read tasks
    the W* arm passes at least once and the ceiling never (broken); E: read tasks the W* arm passes at least once.

    search   — every task read, |R| >= MIN_STILL_FAILING, |X| > |R| / 2 (more than half) and |X| > |B|
    training — every task read, |R| >= MIN_STILL_FAILING, and not search
    inconclusive — every task read and |R| < MIN_STILL_FAILING (W* itself passes most of the set in this run)
    incomplete — some task not read (a budget stop or infrastructure): the numbers are recorded, no verdict"""
    read, unread = [], []
    for task_id, t in per_task.items():
        counts = [len(t["arms"].get(arm) or ()) for arm in ARMS]
        (read if all(c >= attempts for c in counts) else unread).append(task_id)
    w = {t: per_task[t]["arms"]["wstar"][:attempts] for t in read}
    c = {t: per_task[t]["arms"]["ceiling"][:attempts] for t in read}
    R = [t for t in read if not any(w[t])]
    X = [t for t in R if any(c[t])]
    E = [t for t in read if any(w[t])]
    B = [t for t in E if not any(c[t])]
    needed = len(R) // 2 + 1                     # more than half
    if unread:
        verdict, reason = "incomplete", f"{len(unread)} of {len(per_task)} tasks lack {attempts} valid attempts in " \
                                        f"both arms"
    elif len(R) < MIN_STILL_FAILING:
        verdict, reason = "inconclusive", f"the W* arm fails only {len(R)} of the {len(read)} tasks on every attempt " \
                                          f"(fewer than {MIN_STILL_FAILING})"
    elif len(X) >= needed and len(X) > len(B):
        verdict, reason = "search", f"the ceiling fixes {len(X)} of the {len(R)} tasks W* still fails (needed " \
                                    f"{needed}) and breaks {len(B)}"
    else:
        verdict, reason = "training", f"the ceiling fixes {len(X)} of the {len(R)} tasks W* still fails; search " \
                                      f"needs {needed} (more than half) and more than the {len(B)} it breaks"

    def part(pred) -> dict:
        r = [t for t in R if pred(t)]
        return {"still_failing": len(r), "fixed": len([t for t in X if pred(t)]),
                "broken": len([t for t in B if pred(t)]), "wstar_passes": len([t for t in E if pred(t)])}
    return {"verdict": verdict, "reason": reason, "tasks": len(per_task), "read": len(read), "unread": unread,
            "still_failing": R, "fixed": X, "broken": B, "wstar_passes": E,
            "numbers": {"R": len(R), "X": len(X), "B": len(B), "E": len(E), "needed": needed},
            "by_split": {s: part(lambda t, s=s: per_task[t]["split"] == s) for s in SPLITS},
            "persistent": part(lambda t: bool(per_task[t].get("persistent")))}


def package_crash(row) -> bool:
    """An invalid attempt the arm's own package caused: its agent program raised at run time, or the candidate could
    not be assembled. Counted as a failed attempt of that arm, not as an unread pair."""
    return row.validity == "invalid" and (row.failure_class == "candidate_defect"
                                          or str(row.invalid_reason).startswith(PACKAGE_CRASH))


def final_rows(rows) -> list:
    """The one row that stands for each (task, repeat): its latest valid attempt, else its last refill."""
    out: dict = {}
    for r in rows:
        key = (r.task_id, r.attempt)
        rank = (r.validity == "valid", bench._retry_of(r.attempt_id))
        if key not in out or rank >= out[key][0]:
            out[key] = (rank, r)
    return [r for _, r in sorted(out.values(), key=lambda v: (v[1].task_id, v[1].attempt))]


def tabulate(rows_by_arm: dict, tasks: dict, results, attempts: int) -> dict:
    """The per-task, per-arm table from the attempts on record. Each (task, repeat) is read from its final row: a valid
    attempt as it scored; an attempt the arm's own package crashed as a failure of that arm; an attempt lost to the
    environment (worker, provider, budget) is not read, and leaves its pair unread."""
    entries = {e["task_id"]: e for split in SPLITS for e in tasks["spec"].get(split) or ()}
    per_task: dict = {}
    for split in SPLITS:
        for task_id in tasks[split]:
            e = entries.get(task_id, {})
            per_task[task_id] = {"split": split, "persistent": e.get("persistent"),
                                 "failure_kinds": e.get("failure_kinds") or [], "arms": {}}
    arms_out: dict = {}
    for arm in ARMS:
        rows = rows_by_arm.get(arm) or []
        failed_types: Counter = Counter()
        detail: dict = {}
        crashes, unread, limits = [], [], {}
        for r in final_rows(rows):
            if r.validity != "valid" and not package_crash(r):
                unread.append({"attempt_id": r.attempt_id, "failure_class": r.failure_class,
                               "reason": str(r.invalid_reason)[:200]})
                continue
            slot = detail.setdefault(r.task_id, {"success": [], "partial": [], "cost": [], "failed_types": Counter(),
                                                 "package_crash": []})
            slot["cost"].append(r.cost_usd)
            if r.validity != "valid":
                crashes.append({"attempt_id": r.attempt_id, "reason": str(r.invalid_reason)[:200]})
                slot["success"].append(False)
                slot["partial"].append(None)
                slot["package_crash"].append(True)
                continue
            if r.termination == "budget":
                limits.setdefault(r.limit or "unnamed", []).append(r.attempt_id)
            graded = results.grading(r.attempt_id, splits=SPLITS) if results is not None else []
            types = Counter(a.get("type") for a in graded if not a.get("passed") and not a.get("excluded"))
            failed_types.update(types)
            slot["success"].append(bool(r.success))
            slot["partial"].append(round(r.partial_credit, 4))
            slot["package_crash"].append(False)
            slot["failed_types"].update(types)
        for task_id, slot in detail.items():
            if task_id in per_task:
                per_task[task_id]["arms"][arm] = slot["success"]
                per_task[task_id].setdefault("detail", {})[arm] = {
                    "success": slot["success"], "partial": slot["partial"], "cost_usd": slot["cost"],
                    "package_crash": slot["package_crash"],
                    "failed_assertion_types": dict(sorted(slot["failed_types"].items()))}
        summary = summarise(rows)
        arms_out[arm] = {"attempts": summary["attempts"], "invalid": summary["invalid"],
                         "invalid_reasons": summary["invalid_reasons"], "passed_tasks": summary["passed"],
                         "pass_rate": summary["pass_rate"], "partial_credit": summary["partial_credit"],
                         "cost_usd": summary["cost_usd"], "cost_per_attempt": summary["cost_per_attempt"],
                         "terminations": summary["terminations"],
                         # the attempts a hard limit ended, by dimension: a `seconds` cut in one arm only is a
                         # condition of the run, not of the package, and is read as such
                         "limits": {k: v for k, v in sorted(limits.items())},
                         "package_crashes": crashes, "unread_attempts": unread,
                         "failed_assertion_types": dict(sorted(failed_types.items()))}
    decision = decide(per_task, attempts)
    kinds: dict = {}
    for task_id, t in per_task.items():
        for kind in t["failure_kinds"]:
            k = kinds.setdefault(kind, {"tasks": 0, "still_failing": 0, "fixed": 0, "broken": 0, "wstar_passes": 0})
            k["tasks"] += 1
            k["still_failing"] += task_id in decision["still_failing"]
            k["fixed"] += task_id in decision["fixed"]
            k["broken"] += task_id in decision["broken"]
            k["wstar_passes"] += task_id in decision["wstar_passes"]
    return {"per_task": per_task, "arms": arms_out, "by_failure_kind": kinds, "decision": decision}


def render(result: dict) -> str:
    """The table and the decision as text, for the terminal and result.md."""
    lines = ["# Ceiling experiment", ""]
    d = result["decision"]
    lines.append(f"verdict: {d['verdict']} — {d['reason']}")
    n = d["numbers"]
    lines.append(f"R (W* fails every attempt) {n['R']}  X (ceiling fixes) {n['X']}  needed {n['needed']}  "
                 f"B (ceiling breaks) {n['B']}  E (W* passes here) {n['E']}  read {d['read']}/{d['tasks']}")
    for split, part in d["by_split"].items():
        lines.append(f"  {split:6s} still failing {part['still_failing']}, fixed {part['fixed']}, broken "
                     f"{part['broken']}, W* passes {part['wstar_passes']}")
    p = d["persistent"]
    lines.append(f"  the dev tasks W* failed on every v0.2 reading: still failing {p['still_failing']}, fixed "
                 f"{p['fixed']}")
    lines += ["", "| task | split | failure kinds | wstar | ceiling | class |", "|---|---|---|---|---|---|"]
    for task_id, t in result["per_task"].items():
        w, c = t["arms"].get("wstar") or [], t["arms"].get("ceiling") or []
        cls = ("fixed" if task_id in d["fixed"] else "broken" if task_id in d["broken"] else
               "still failing" if task_id in d["still_failing"] else "W* passes" if task_id in d["wstar_passes"]
               else "unread")
        lines.append(f"| {task_id} | {t['split']} | {', '.join(t['failure_kinds'])} | {sum(w)}/{len(w)} | "
                     f"{sum(c)}/{len(c)} | {cls} |")
    lines += ["", "| arm | attempts | invalid | pass rate | cost/attempt | cost |", "|---|---|---|---|---|---|"]
    for arm, a in result["arms"].items():
        lines.append(f"| {arm} | {a['attempts']} | {a['invalid']} | {a['pass_rate']} | {a['cost_per_attempt']} | "
                     f"{a['cost_usd']} |")
    lines += ["", "failed assertion types by arm:"]
    for arm, a in result["arms"].items():
        lines.append(f"  {arm}: {json.dumps(a['failed_assertion_types'])}")
    lines += ["", "attempts a hard limit ended, the package's own crashes (failed attempts), and attempts lost to the "
                  "environment (unread):"]
    for arm, a in result["arms"].items():
        cut = ", ".join(f"{dim} {len(ids)} ({', '.join(ids)})" for dim, ids in a["limits"].items()) or "none"
        lines.append(f"  {arm}: limits {cut}; package crashes {len(a['package_crashes'])}"
                     + "".join(f"\n    {c['attempt_id']}: {c['reason']}" for c in a["package_crashes"])
                     + f"; unread {len(a['unread_attempts'])}"
                     + "".join(f"\n    {u['attempt_id']}: {u['failure_class']} {u['reason']}"
                               for u in a["unread_attempts"]))
    lines += ["", "by failure kind (tasks of the frozen set):"]
    for kind, k in sorted(result["by_failure_kind"].items()):
        lines.append(f"  {kind}: {json.dumps(k)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------- the run
def run(cfg: dict, base, *, checks_only: bool = False, out=print) -> dict:
    prepared = prepare(cfg, base, out=out)
    settings, tasks = prepared["settings"], prepared["tasks"]
    est = estimate(settings, tasks)
    out(f"tasks      {tasks['sha256'][:16]} {settings['tasks']}: dev {len(tasks['dev'])}, train {len(tasks['train'])}")
    for arm, a in (prepared.get("arms") or {}).items():
        out(f"arm        {arm}: {a['api']} agent={a['agent']} roles={a['roles']} {a['files']} files {a['bytes']} bytes "
            f"package {ct.short(a['package_ref'])} W {ct.short(a['candidate_ref'])}")
    out(f"plan       {est['attempts']} attempts ({est['attempts_per_arm']} per arm: {est['tasks']} tasks x "
        f"{settings['attempts']}); nominal estimate {est['nominal_usd'] or 'not set'}"
        + (f" = ${est['nominal_usd_total']}" if est["nominal_usd_total"] else ""))
    activity = prepared.get("activity")
    frozen = activity.objects.get(prepared["cfg"]["rsi"]["budget_ref"]) if activity is not None else {}
    root_usd = bench.budget_limits(frozen).get("usd")
    if root_usd is not None:
        bound = bench.pricing_of(prepared["cfg"]).bound().get("usd") or 0.0
        slots = activity.bench.concurrency
        # Every call reserves `bound` before it is made and an unknown settlement keeps it, so the calls that can be
        # in flight at once are floor((root - spent - unknown x bound) / bound); a call waiting for a slot waits
        # inside its attempt's max_seconds. The run keeps all its slots to the end only while the estimate stays
        # under the root less (1 + concurrency) bounds.
        room = root_usd - bound * (1 + slots)
        out(f"budget     root ${root_usd:g} with a hard stop; one call reserves up to ${bound:.2f} before it is made "
            f"(slots in flight = floor((root - spent - unknown x ${bound:.2f}) / ${bound:.2f})); with {slots} in "
            f"flight the run keeps every slot while its spend stays under ${max(room, 0):.2f}")
        if est["nominal_usd_total"] and est["nominal_usd_total"] > room:
            out(f"NOTE       the nominal estimate ${est['nominal_usd_total']} is above the root less {1 + slots} "
                f"reservations (${max(room, 0):.2f}): the last lanes would run with fewer slots, their calls waiting "
                f"inside the attempts' 900 s, and the hard stop may end the run `incomplete`. A root's limits are "
                f"fixed once it opens: raise [freeze.budget] usd before the first real run.")
    for line in prepared["problems"]:
        out(f"REFUSED    {line}")
    report = {"ok": not prepared["problems"], "problems": prepared["problems"], "tasks_sha256": tasks["sha256"],
              "arms": prepared.get("arms") or {}, "estimate": est, "checks_only": checks_only}
    if prepared["problems"] or checks_only:
        out("result     " + ("checks passed; no model was called" if report["ok"] else "CHECKS FAILED; nothing ran"))
        return report
    work, parent, objects = prepared["work"], prepared["parent"], activity.objects
    work.budget = bench.activity_budget(activity, prepared["cfg"], "ceiling")
    core = work.core_ref
    w0 = ct.SystemRelease.from_json(objects.get(parent)).worker_ref
    lanes: dict = {}
    for arm in ARMS:
        for split in SPLITS:
            if not tasks[split]:
                continue
            plan = TaskEvalPlan(candidate_ref=prepared["arms"][arm]["candidate_ref"], control_ref=w0,
                                parent_release=parent, core_ref=core, split_ref=activity.splits.hash(),
                                task_ids=tuple(tasks[split]), repeats=settings["attempts"],
                                order_seed=work.order_seed, per_attempt_budget=work.attempt_budget.limits(),
                                retry_policy={"max_infrastructure_retries": INFRASTRUCTURE_RETRIES},
                                gate_plan=work.gate.to_json(),
                                installs=False)
            plan_ref = objects.put(plan)
            permit = activity.releases.permit(prepared["arms"][arm]["candidate_ref"], parent, plan_ref,
                                              work.scope_id, core)
            release = activity.releases.candidate_release(parent, prepared["arms"][arm]["candidate_ref"], permit)
            lanes[(arm, split)] = {"plan": plan, "plan_ref": plan_ref, "permit": permit, "release": release}
    activity.releases.events.append("ceiling_run", scope=work.scope_id, tasks_sha256=tasks["sha256"],
                                    attempts=settings["attempts"],
                                    arms={arm: {k: a[k] for k in ("package_ref", "candidate_ref")}
                                          for arm, a in prepared["arms"].items()},
                                    plans={f"{arm}/{split}": lane["plan_ref"] for (arm, split), lane in lanes.items()})
    rows_by_arm: dict = {arm: [] for arm in ARMS}
    stopped = ""
    for repeat in range(settings["attempts"]):
        for arm in (ARMS if repeat % 2 == 0 else ARMS[::-1]):
            for split in SPLITS:
                lane = lanes.get((arm, split))
                if lane is None or stopped:
                    continue

                def go(arm_name, order, retry=0, supersedes=None, lane=lane, split=split):
                    return work.run_arm(arm_name, lane["release"], order, plan_ref=lane["plan_ref"],
                                        permit=lane["permit"], split=split, candidate=arm_name, role="evolved",
                                        retry_index=retry, resume=True, supersedes=supersedes)
                rows = refill(lane["plan"], arm, list(go(arm, [(t, repeat) for t in tasks[split]])), go)
                rows_by_arm[arm] += rows
                done = summarise(rows)
                out(f"ran        repeat {repeat} {arm} {split}: {done['passed']}/{done['tasks']} passed, "
                    f"{done['invalid']} invalid, cost {done['cost_usd']}")
                if any(r.failure_class == "budget_exhausted" for r in rows):
                    stopped = "the budget root's hard stop"
    if stopped:
        out(f"STOPPED    {stopped}: no further arm was started")
    result = tabulate(rows_by_arm, tasks, activity.results, settings["attempts"])
    result.update({"tasks_file": str(settings["tasks"]), "tasks_sha256": tasks["sha256"],
                   "arms_packages": {arm: {k: a[k] for k in ("package_ref", "candidate_ref", "agent", "roles")}
                                     for arm, a in prepared["arms"].items()},
                   "attempts_per_task": settings["attempts"], "stopped": stopped or None,
                   "budget": work.budget.remaining() if work.budget is not None else None})
    target = Path(activity.root) / "ceiling"
    write_json(target / "result.json", result)
    text = render(result)
    (target / "result.md").write_text(text, encoding="utf-8")
    out(text)
    return {**report, "result": result}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", required=True, help="the experiment's config (claude-ceiling.toml)")
    parser.add_argument("--checks-only", action="store_true",
                        help="run every startup check and both packages' draft checks, call no model, run nothing")
    parser.add_argument("--concurrency", type=int, help="attempts in flight for this run, over [bench] concurrency")
    args = parser.parse_args(argv)
    cfg = config_mod.load(args.config)
    if args.concurrency is not None:
        cfg = {**cfg, "bench": {**dict(cfg.get("bench") or {}), "concurrency": int(args.concurrency)}}
    try:
        report = run(cfg, Path(args.config).resolve().parent, checks_only=args.checks_only)
    except ValueError as e:
        print(f"REFUSED    {e}")
        return 1
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
