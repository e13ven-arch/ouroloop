"""The phases of `ouroloop bench`: what has to hold before a benchmark number means anything, and the run that
produces one (docs/ROADMAP.md M1).

`benchmark.py` holds the records a phase writes (splits, run identity, task results); this module is the
sequence that writes them, and `adapters/automationbench.py` is the binding to the upstream environment.

    smoke       what upstream this is, the frozen task groups, the adapter contract checks, the input audit, and
                a few real attempts on development tasks. It makes no claim about capability: it says the runner
                measures what upstream measures, and shows what one attempt looks like end to end.
    baseline    a reasonable base version and the declared frontier models, on development tasks, through this
                same driver. Fewer than two vendors is published as an incomplete C2, never filled in with a
                weaker model.
    collect     the installed version on training tasks, with the completion probe labelling offline, and an
                audit of how many labels came from how many independent groups.
    improve     the worker loop with a fixed research policy: diagnose, propose, build, evaluate, install.
    research    two research policies compared by the task policies they produce, in isolated arms (M6).
    evaluate    one sealed finalization plan on the final tasks. It reports; it does not choose.
    report      offline aggregation of what is already on disk. No model is called.

The phases run the loop the other modules define; this module owns the order and the wiring
(docs/modules/07_BENCH_REPORT.md).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import config as config_mod
from .adapters import automationbench as ab
from .benchmark import (HARD_LIMITS, V03_BUDGET_FIELDS, Budget, Results, RunManifest, SplitManifest, assign_splits,
                        digest, summarise)
from .ledger import now, redact, write_json

# The seven phases, and `cycle`: the thin automatic entry that calls them in the frozen order (M7 §1).
PHASES = ("smoke", "baseline", "collect", "improve", "research", "evaluate", "report", "cycle")
IMPLEMENTED = PHASES
# Every phase is wired; what a phase needs is checked before it starts (a frozen plan, a model table, two
# frontier configurations), and a missing piece is named rather than quietly skipped (BR-01).
NOT_YET: dict[str, str] = {}
BENCH_KEYS = {"domain", "upstream_path", "toolset", "seed", "ratios", "budget", "families", "smoke_tasks",
              "near_duplicate_threshold", "repeats", "concurrency"}
# A key whose value is the secret itself, rather than the name of the variable that holds it (api_key_env is a
# name and belongs in the manifest). redact() reads a value's shape and a bare key has none, so the key decides.
SECRET_KEY = re.compile(r"(?i)(api[_-]?key|token|secret|passw(or)?d|credential)$")


def model_identity(provider, table: dict) -> dict:
    """What a run's model was, for the manifest: the whole [llm] table less any secret, because a setting that
    changes results must not be the one that went unrecorded (docs/DESIGN.md §3.2)."""
    config = {k: v for k, v in table.items() if k != "type" and not SECRET_KEY.match(k)}
    return {"provider": provider.name, "model": getattr(provider, "model", ""), "config": redact(config)}


@dataclass
class BenchConfig:
    """The [bench] table. Everything that decides what a run measured is here and goes into a manifest."""
    domain: str = "marketing"
    upstream_path: str | None = None
    toolset: str = ab.TOOLSET
    seed: int = 0
    ratios: dict[str, float] = field(default_factory=lambda: {"train": 0.5, "dev": 0.25, "test": 0.25})
    families: dict[str, str] = field(default_factory=dict)
    budget: Budget = field(default_factory=Budget)
    smoke_tasks: int = 2
    near_duplicate_threshold: float = 0.6
    repeats: int = 1
    concurrency: int = 1            # attempts run at a time; a setting of the machine, not of any frozen plan

    @classmethod
    def load(cls, cfg: dict, base: str | Path = ".") -> "BenchConfig":
        table = dict(cfg.get("bench") or {})
        unknown = sorted(set(table) - BENCH_KEYS)
        if unknown:
            raise ValueError(f"[bench] does not take {unknown}; it takes {sorted(BENCH_KEYS)}")
        budget = Budget(**dict(table.pop("budget", None) or {}))
        toolset = table.get("toolset", ab.TOOLSET)
        if toolset != ab.TOOLSET:
            raise ValueError(f"[bench] toolset = {toolset!r}: only {ab.TOOLSET!r} is wired up (upstream's own "
                             f"default); the zapier toolsets build their schemas through another package")
        path = table.pop("upstream_path", None)
        # A checkout named in the config is relative to the config, so a config can be moved with its checkout.
        if path and not Path(path).expanduser().is_absolute():
            path = str((Path(base) / path).resolve())
        return cls(budget=budget, upstream_path=path, **table)

    def environment(self, versions: dict) -> dict:
        return {"upstream": versions.get("commit", ""), "package": versions.get("package", ""),
                "python": versions.get("python", ""), "toolset": self.toolset,
                "runner": versions.get("runner", ""), "tool_schemas": "ouroloop"}


# ---------------------------------------------------------------------------- splits
def freeze_splits(suite: ab.Suite, cfg: BenchConfig, path: str | Path, *,
                  replace_existing: bool = False) -> tuple[SplitManifest, list[str]]:
    """The frozen task groups of this domain, and what upstream now says that they do not.

    A manifest already on disk is the one that is used, whatever the config now asks for: the groups are fixed
    before the first result is looked at, so a changed ratio or seed is a new experiment (docs/DESIGN.md §3.1).
    """
    path = Path(path)
    fams, rule = ab.families(suite, cfg.families)
    entries = suite.entries(fams)
    if path.exists() and not replace_existing:
        frozen = SplitManifest.load(path)
        return frozen, frozen.drift(entries)
    splits = assign_splits(fams.values(), cfg.ratios, cfg.seed)
    manifest = SplitManifest(cfg.domain, cfg.environment(suite.versions), rule, entries, splits, cfg.ratios,
                             cfg.seed)
    manifest.freeze(path, replace_existing=replace_existing)
    return manifest, []


# ---------------------------------------------------------------------------- the smoke phase
def smoke(cfg: dict, base: str | Path = ".", *, tasks: list[str] | None = None, limit: int | None = None,
          run_agent: bool = True, replace_splits: bool = False,
          out: Callable[[str], None] = print) -> dict:
    """Everything M1 has to show, in order, and a report of it.

    The checks run before any model is called: a run whose scorer or input boundary is wrong is not worth paying
    for. `report["ok"]` is whether every contract check passed and nothing leaked — never whether the agent did
    well.
    """
    bench = BenchConfig.load(cfg, base)
    versions = ab.require(bench.upstream_path)
    out(f"upstream   {versions['path'] or 'installed package'} @ {versions['commit'][:12] or '(no git)'} "
        f"package={versions['package'] or '?'} python={versions['python']} toolset={bench.toolset}")
    for note in versions.get("notes", []):
        out(f"note       {note}")

    suite = ab.load_suite(bench.domain, bench.upstream_path)
    out(f"tasks      {len(suite)} in domain {bench.domain}")

    root = config_mod.workspace_root(cfg, base)
    results = Results(root, bench.domain)
    if replace_splits and results.path.exists():
        raise ValueError(f"{results.path} already holds results of the frozen split: reassigning the groups now "
                         f"would rescore attempts that were made under the old ones. Start a new workspace.")
    manifest, drift = freeze_splits(suite, bench, results.manifest_path(), replace_existing=replace_splits)
    counts = manifest.counts()
    out(f"splits     {results.manifest_path()} hash={manifest.hash()} rule={manifest.family_rule} "
        f"seed={manifest.seed}")
    out("           " + "  ".join(f"{s}: {c['tasks']} tasks / {c['families']} families"
                                  for s, c in counts.items()))
    for line in drift:
        out(f"DRIFT      {line}")

    dups = ab.near_duplicates(suite, bench.near_duplicate_threshold)
    top = max((j for _a, _b, j in ab.near_duplicates(suite, 0.0)), default=0.0)
    out(f"families   {len(manifest.splits)} groups, rule {manifest.family_rule}; highest signature overlap "
        f"between any two tasks {top:.2f}, {len(dups)} pairs above {bench.near_duplicate_threshold}")
    for a, b, j in dups[:10]:
        out(f"           overlap {j}: {a} / {b} — read these before declaring [bench.families]")
    if not dups:
        out(f"           no pair is close enough to be a variant of another, so one task is one family")

    dev = manifest.ids("dev")
    # Named tasks are taken as named; only the default selection is capped by smoke_tasks.
    chosen = [t for t in tasks if t in suite.tasks] if tasks else dev[:limit or bench.smoke_tasks]
    chosen = chosen[:limit] if limit else chosen
    if tasks:
        outside = sorted(set(tasks) - set(dev))
        if outside:
            out(f"NOTE       {outside} are not development tasks; the smoke phase reads dev, and test must not be "
                f"looked at before the final evaluation")
            chosen = [t for t in chosen if t not in outside]
    if not chosen:
        out(f"NOTE       no development task to check: {len(dev)} of {len(suite)} tasks are in dev. The per-task "
            f"checks and the input audit need one.")

    checks = ab.contract_check(suite, chosen)
    out(f"contract   rubric={checks['rubric'].get('funcs') or 'not importable'} "
        f"weights={checks['rubric'].get('weights')}")
    for note in checks["notes"]:
        out(f"note       {note}")
    for tid, b in (checks.get("baseline") or {}).items():
        out(f"baseline   {tid}: doing nothing scores {b['partial_credit']} over {b['scored']} scored assertions "
            f"({b['excluded']} already satisfied and excluded)")
    if checks["inverse_tasks"]:
        out(f"NOTE       {checks['inverse_tasks']}: doing nothing already passes; a pass there is not evidence "
            f"that the agent acted")
    for line in checks["failed"]:
        out(f"FAILED     {line}")

    audit = {"leaks": [], "overlaps": []}
    for tid in chosen:
        ep = suite.episode(tid)
        try:
            found = ab.audit_inputs(suite.tasks[tid], suite.grading(tid), ab.agent_payload(suite.tasks[tid], ep))
        finally:
            ep.close()
        audit["leaks"] += found["leaks"]
        audit["overlaps"] += found["overlaps"]
    out(f"audit      {len(chosen)} tasks: {len(audit['leaks'])} leaks, {len(audit['overlaps'])} prompt/assertion "
        f"overlaps for review")
    for line in audit["leaks"]:
        out(f"LEAK       {line}")
    for line in audit["overlaps"][:5]:
        out(f"           {line}")

    report = {"upstream": versions, "tasks": len(suite), "splits": {"hash": manifest.hash(), "counts": counts,
                                                                   "drift": drift, "rule": manifest.family_rule},
              "near_duplicates": dups[:20], "contract": checks, "audit": audit, "attempts": [], "summary": {},
              # Nothing checked is not the same as nothing wrong.
              "ok": bool(chosen) and not checks["failed"] and not audit["leaks"] and not drift}

    code_problems = config_harness_problems(cfg)
    for line in code_problems:
        out(f"REFUSED    {line}")
    if code_problems:
        report["ok"] = False
        report["harness"] = code_problems
    if run_agent and chosen and not code_problems:
        report.update(_attempt(cfg, base, bench, suite, manifest, results, chosen, versions, out))
    elif run_agent:
        out("run        no development task to attempt")
    out(f"result     {'checks passed' if report['ok'] else 'CHECKS FAILED'}; this phase makes no claim about "
        f"capability")
    return report


def _attempt(cfg: dict, base: str | Path, bench: BenchConfig, suite: ab.Suite, manifest: SplitManifest,
             results: Results, chosen: list[str], versions: dict, out: Callable[[str], None]) -> dict:
    """The few real attempts M1 asks for: a full task result, a tool trace and a termination reason."""
    from .evolve.task_eval import make_agent_factory, run_tasks
    if "llm" not in cfg:
        out("run        no [llm] table: the checks above need no model, an attempt does")
        return {}
    rt = config_mod.build_runtime(cfg, base)
    config_mod.register_builtin_points(rt)
    provider = config_mod.build_provider(cfg["llm"])
    model = model_identity(provider, cfg["llm"])
    # The prompt of this run is whatever system prompt upstream ships with the tasks being attempted. They are
    # normally one per domain; hashing all of the distinct ones keeps the manifest honest if they are not.
    prompts = sorted({suite.tasks[t].system for t in chosen})
    run = RunManifest("base", "base", "dev", model, prompt_hash=digest({"system": prompts})[:16],
                      environment=bench.environment(versions), budget=bench.budget, data=manifest.hash(),
                      repeats=bench.repeats, notes="ouroloop bench --phase smoke")
    results.put_run(run)
    out(f"run        {run.hash()} candidate=base split=dev model={model.get('model') or provider.name} "
        f"budget={bench.budget.max_steps} steps / {bench.budget.max_tool_calls} tool calls")

    def show(r) -> None:
        out(f"attempt    {r.task_id} success={r.success} partial={r.partial_credit:.3f} "
            f"({r.assertions_passed}/{r.assertions_scored}) steps={r.steps} tools={r.tool_calls} "
            f"stop={r.stop}->{r.termination} {r.validity}{': ' + r.invalid_reason if r.invalid_reason else ''} "
            f"session={r.evidence}")

    rows = run_tasks(suite, chosen, make_agent_factory(rt, provider, budget=bench.budget,
                                                       meta={"agent": model.get("model") or provider.name,
                                                             "bench": bench.domain}),
                     run=run, splits=manifest, results=results, repeats=bench.repeats, on_result=show,
                     concurrency=bench.concurrency)
    total = summarise(rows)
    out(f"summary    {total['passed']}/{total['tasks']} tasks passed, partial={total['partial_credit']}, "
        f"invalid={total['invalid']}, cost={'unmeasured' if total['cost_usd'] is None else total['cost_usd']} "
        f"(missing on {total['cost_missing']} attempts), usage={total['usage']}")
    return {"attempts": [r.to_json() for r in rows], "summary": total, "run": run.to_json()}


def run_phase(cfg: dict, base: str | Path = ".", *, phase: str = "smoke", **kw) -> dict:
    """Dispatch one phase. An unknown phase is refused, and a phase whose frozen references are missing refuses
    before it calls a model."""
    if phase in NOT_YET:
        raise NotImplementedError(f"the {phase!r} phase is {NOT_YET[phase]} in docs/ROADMAP.md and is not "
                                  f"implemented; implemented: {list(IMPLEMENTED)}")
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}: one of {list(PHASES)}")
    if phase == "smoke":
        return smoke(cfg, base, **kw)
    runner = {"baseline": baseline, "collect": collect, "improve": improve, "research": research,
              "evaluate": evaluate, "report": report, "cycle": cycle}[phase]
    allowed = {"baseline": {"out", "limit", "tasks"}, "collect": {"out", "repeats", "tasks"},
               "improve": {"out", "rounds", "researcher", "segment"},
               "research": {"out", "researcher", "campaign_ref", "r_candidate_ref"},
               "evaluate": {"out", "plan_ref", "resume"}, "report": {"out", "write"},
               "cycle": {"out", "researcher", "slot_researcher", "arm_researcher", "until"}}[phase]
    unknown = sorted(set(kw) - allowed)
    if unknown:
        raise TypeError(f"the {phase} phase does not take {unknown}; it takes {sorted(allowed)}")
    return runner(cfg, base, **kw)


# ---------------------------------------------------------------------------- the improvement activity (M7)
# docs/modules/07_BENCH_REPORT.md. The phases below run the loop the other modules define; the wiring is here
# because someone has to own it, and it is the same wiring in a test and in a paid run — a test that assembled its
# own shortcut would be testing the shortcut.
from dataclasses import dataclass as _dataclass
from dataclasses import field as _field

from . import policies as _P
from .evolve import contracts as _ct
from .evolve import experience as _experience
from .evolve import feedback as _feedback
from .evolve.build import build_task_dataset as _build_task_dataset
from .evolve.execution import BudgetError, BudgetMismatch, Budgets, Schedule
from .evolve.task_gate import GatePlan, TaskEvalPlan, evaluate_task
from .evolve.task_gate import screen as _screen
from .points import business_stop_point as _business_stop_point
from .release import Releases
from .research.context import make_context as _make_context
from .research.context import task_results_summary as _task_results_summary
from .research.inspection import inspect as _inspect

RSI_KEYS = {"profile", "initial_release_ref", "core_ref", "schedule_ref", "budget_ref", "task_plan_ref",
            "research_plan_ref", "final_plan_ref", "scope_id", "stop_view", "train", "repeats", "order_seed"}
PLACEHOLDER = ("<", "TODO", "changeme")


@_dataclass
class RsiConfig:
    """The `[rsi]` table. Every reference must already exist as a frozen object: a plan is not a config file a
    researcher can edit between rounds (docs/modules/07_BENCH_REPORT.md §2)."""
    profile: str = "task"
    initial_release_ref: str | None = None
    core_ref: str | None = None
    schedule_ref: str | None = None
    budget_ref: str | None = None
    task_plan_ref: str | None = None
    research_plan_ref: str | None = None
    final_plan_ref: str | None = None
    scope_id: str = "activity"
    stop_view: str = "stop.business.v1"
    train: bool = False
    repeats: int = 1
    order_seed: int = 0

    @classmethod
    def load(cls, cfg: dict) -> "RsiConfig":
        table = dict(cfg.get("rsi") or {})
        unknown = sorted(set(table) - RSI_KEYS)
        if unknown:
            raise ValueError(f"[rsi] does not take {unknown}; it takes {sorted(RSI_KEYS)}")
        for key, value in table.items():
            if isinstance(value, str) and value.startswith(PLACEHOLDER):
                raise ValueError(f"[rsi] {key} = {value!r} is a placeholder: freeze the object and name its ref")
        return cls(**table)

    def check(self, objects: _ct.Objects, *, needs=()) -> list[str]:
        missing = []
        for key in needs:
            ref = getattr(self, key)
            if not ref:
                missing.append(f"[rsi] {key} is not set")
            elif not objects.has(ref):
                missing.append(f"[rsi] {key} = {ref}: no such object in this workspace")
        return missing


@_dataclass
class Improvement:
    """One improvement activity: the environment, the workspace, the budget and the frozen comparison policy.

    It owns the four handlers an experiment runs through — build, screen, evaluate, install — so that every round
    of every phase uses the same path from a proposal to a pointer.
    """
    rt: object
    suite: object
    splits: object
    objects: _ct.Objects
    releases: Releases
    provider: object
    core_ref: str
    gate: GatePlan
    dev_tasks: tuple
    scope_id: str = "activity"
    repeats: int = 1
    order_seed: int = 0
    screen_ids: tuple = ()
    budget: object = None
    serving: object = None
    trainer: object = None
    results: object = None                      # benchmark.Results, appended as attempts finish
    stop_view: str = "stop.business.v1"
    attempt_budget: Budget = _field(default_factory=Budget)
    probe_train: bool = False
    train_tasks: tuple = ()
    owner: str = "worker"
    plan_installs: bool = True
    pricing: object = None                      # execution.Pricing: what the agent's calls cost
    harness: dict | None = None                 # v0.6: the package/diagnosis limits K froze, or None (v2 loop)
    worker: object = None                       # the qualified isolated worker for package code, or None
    actor_override: bool = False                # a frontier arm runs its own provider, as a separate comparison
    segment: str | None = None                  # the cycle's worker segment, whose collection quota is its own
    allocation: str | None = None               # the root allocation the arm's calls spend ("final"), if any
    template_ref: str = ""                      # the frozen evaluation template its experiments are held to
    concurrency: int = 1                        # attempts of one arm run at a time ([bench] concurrency)
    _plans: dict = _field(default_factory=dict)
    _experiment_ref: str = ""
    _parent_release: str = ""

    # -------------------------------------------------------------- pieces
    def services(self) -> _P.Services:
        return _P.Services(self.objects, runtime=self.rt, serving=self.serving,
                           provider_factory=lambda identity, config: config_mod.build_provider(config))

    def families(self) -> dict:
        return {e.task_id: e.family_id for e in self.splits.entries}

    def plan(self, candidate_ref: str, control_ref: str, parent_release: str) -> TaskEvalPlan:
        return TaskEvalPlan(candidate_ref=candidate_ref, control_ref=control_ref, parent_release=parent_release,
                            core_ref=self.core_ref, split_ref=self.splits.hash(), task_ids=tuple(self.dev_tasks),
                            repeats=self.repeats, order_seed=self.order_seed,
                            per_attempt_budget=self.attempt_budget.limits(),
                            retry_policy={"max_infrastructure_retries": 1}, screen_ids=tuple(self.screen_ids),
                            gate_plan=self.gate.to_json(), installs=self.plan_installs)

    def run_identity(self, release_ref: str, *, plan_ref: str, split: str, candidate: str, role: str,
                     permit: str | None = None, scope: str | None = None) -> tuple:
        """The run an arm's attempts are recorded under, and what it binds: (RunManifest, bound release, actor).
        Its hash is the attempts' identity, so a run already on record can be found before anything is paid."""
        if permit is not None:
            self.releases.authorize(release_ref, profile="evaluation", plan_ref=plan_ref,
                                    scope_id=scope or self.scope_id)
        else:
            self.releases.authorize(release_ref, profile="task")
        bound = _P.bind_release(release_ref, profile="evaluation" if permit else "task",
                                services=self.services(), authorization=permit or "installed")
        # The release pins the actor it runs; the configured [llm] only bootstrapped it and cannot override it.
        # A frontier arm is the one exception, and it is a different comparison (docs/modules/01_POLICY_RUNTIME.md).
        actor = self.provider
        if bound.actor_identity is not None and not self.actor_override:
            if bound.actor is None:
                raise _P.BindError(f"{_ct.short(release_ref)} pins an actor and no provider could be built for it")
            actor = bound.actor
        release = self.objects.load(release_ref, _ct.SystemRelease)
        pinned = bound.actor_identity is not None and not self.actor_override
        # The first version's run fields (docs/modules/07_BENCH_REPORT.md §2): what kind of run this is, the host
        # contract it ran on, and — for the pinned actor — its recorded configuration rather than an empty one.
        # A v2 activity leaves all three unset, so its run identities do not move.
        mode = ("collection" if split == "train" else "task_evaluation") if self.harness else None
        config = self.objects.get(bound.actor_identity.inference_ref) if pinned else {}
        run = RunManifest(candidate, role, split, model_identity(actor, config),
                          prompt_hash=digest(bound.system_prompt("")[1])[:16],
                          environment=self.rt_environment(), budget=self.attempt_budget,
                          data=self.splits.hash(), repeats=self.repeats,
                          system_release_ref=release_ref,
                          worker_ref=release.worker_ref, research_ref=release.research_ref,
                          core_ref=self.core_ref, eval_plan_ref=plan_ref, budget_scope=self.scope_id,
                          input_schema_refs=(self.stop_view,),
                          actor_identity_ref=bound.actor_identity.ref() if pinned else None,
                          package_ref=bound.package.ref if bound.package is not None else None,
                          mode=mode, target_harness_ref=release.target_harness_ref)
        return run, bound, actor

    def run_arm(self, arm: str, release_ref: str, order, *, plan_ref: str, split: str = "dev",
                candidate: str = "candidate", role: str = "evolved", probe: bool = False,
                permit: str | None = None, scope: str | None = None, retry_index: int = 0,
                resume: bool = False, supersedes: dict | None = None) -> list:
        """One arm's attempts. Each arm is assembled from its own release, with its own session, tool environment
        and episode; nothing mutable is shared between the two sides (docs/modules/03_EVALUATION_RELEASE.md §2).

        Every model call of the arm's agent goes through the activity's budget book when there is one
        (MeteredProvider): reserved before the call, settled on its measured usage, and refused once a limit is
        reached. With `resume`, attempts this same run already recorded are returned as they were rather than
        paid for again: the attempt id is determined by the run, the task, the repeat, the arm and the retry, so
        a restarted plan finds exactly the attempts it had finished (docs/modules/05_EXECUTION_BUDGET.md §4).
        """
        from .evolve.execution import MeteredProvider, Pricing
        from .evolve.task_eval import make_agent_factory, run_tasks
        run, bound, actor = self.run_identity(release_ref, plan_ref=plan_ref, split=split, candidate=candidate,
                                              role=role, permit=permit, scope=scope)
        if self.results is not None:
            self.results.put_run(run)
        order = list(order)
        reused: list = []
        if resume and self.results is not None:
            recorded = {(r.task_id, r.attempt, _retry_of(r.attempt_id)): r for r in self.results.of_run(run.hash())
                        if r.attempt_id}
            reused = [recorded[(t, i, retry_index)] for t, i in order if (t, i, retry_index) in recorded]
            order = [(t, i) for t, i in order if (t, i, retry_index) not in recorded]
            if not order:
                return reused
        provider = actor
        if self.budget is not None:
            provider = MeteredProvider(actor, self.budget, self.pricing or Pricing(),
                                       operation=f"{self.scope_id}:{arm}:{run.hash()}", max_retries=0,
                                       allocation=self.allocation)
        factory = make_agent_factory(self.rt, provider, budget=self.attempt_budget, bound=bound,
                                     objects=self.objects, stop_view=self.stop_view,
                                     meta={"arm": arm, "scope": self.scope_id}, worker=self.worker)
        return reused + run_tasks(self.suite, [t for t, _ in order], factory, run=run, splits=self.splits,
                                  results=self.results, probe=probe, arm=arm, plan_ref=plan_ref,
                                  release_ref=release_ref, order=order, retry_index=retry_index,
                                  pricing=self.pricing, supersedes=supersedes, concurrency=self.concurrency)

    def rt_environment(self) -> dict:
        return {"upstream": self.suite.versions.get("commit", ""), "package": self.suite.versions.get("package", ""),
                "toolset": ab.TOOLSET, "runner": self.suite.versions.get("runner", "")}

    # -------------------------------------------------------------- evidence
    def collect(self, *, tasks=None, release_ref: str | None = None, repeats: int = 1, dev: bool = True) -> dict:
        """Run the current version and gather what a round may learn from.

        Training tasks are run with the completion probe, which labels each stop decision from the state it was
        made on. The development tasks are run too, without a probe and without any online score: a behaviour gap
        is something the current version does on tasks it will be judged on, and a round that only saw the
        training pool could not name one (docs/DESIGN.md §4.3). The version changes between rounds, so this is
        measured per round rather than once.
        """
        release_ref = release_ref or self.releases.active()["release_ref"]
        tasks = tuple(tasks or self.train_tasks or self.splits.ids("train"))
        order = [(t, i) for t in tasks for i in range(repeats)]
        train_results = self.run_arm("collect", release_ref, order, plan_ref="", split="train",
                                     candidate="current", role="base", probe=self.probe_train)
        dev_results = []
        if dev and self.dev_tasks:
            dev_results = self.run_arm("dev_baseline", release_ref,
                                       [(t, i) for t in self.dev_tasks for i in range(self.repeats)],
                                       plan_ref="", split=_split_of_tasks(self.splits, self.dev_tasks),
                                       candidate="current", role="base")
        feedback = _feedback.build_feedback(self.rt, objects=self.objects, scope_id=self.scope_id)
        return {"results": [*train_results, *dev_results], "train": train_results, "dev": dev_results,
                "feedback": feedback, "release": release_ref}

    def context(self, *, goal: str, release_ref: str, evidence: dict, experience=(),
                experience_ref: str = "") -> object:
        bound = _P.bind_release(release_ref, profile="task", services=self.services(),
                                authorization="installed")
        traces = _trace_refs(self.objects)
        inspection = _inspect(bound, traces, objects=self.objects, base_prompt="")
        refs = tuple(r for record in evidence["feedback"].records for r in record.evidence_refs)
        return _make_context(self.scope_id, goal=goal, release_ref=release_ref, experience_ref=experience_ref,
                             evidence_refs=refs, objects=self.objects,
                             partitions=("train", "dev"), inspection=inspection,
                             results=_task_results_summary(evidence["results"], splits=self.splits),
                             feedback=evidence["feedback"].summary(), experience=experience,
                             budget=_effective_budget(self.budget.remaining()) if self.budget else {},
                             tools=self.base_tools() if self.harness else (),
                             targets={**{t: {"binding": r.binding, "kinds": list(r.allowed_kinds),
                                            "site": r.site,
                                            # the instruction text W runs with at this target now, so a
                                            # proposal changes or merges it rather than repeating it
                                            **({"current": bound.prompts[t]} if bound.prompts.get(t) else {})}
                                         for t, r in _P.REGISTRY.items() if r.owner == self.owner
                                         and t in _P.core_targets(self.objects.load(self.core_ref,
                                                                                    _ct.CoreManifest))
                                         and t != "worker.actor"},
                                      **self.training_options()})

    def measure(self, release_ref: str, previous: dict | None = None) -> dict:
        """The first version's per-round measurement (docs/modules/04_RESEARCH_LOOP.md §4), train only.

        Dev reaches R only as aggregates in the experience log, and every gate reruns its own control arm, so a
        round has no use for a fresh dev measurement. The first round measures every train task. After an
        install, only the train tasks that failed in the latest measurement are rerun: that set is the diagnosis
        working set, and the passing results it does not rerun stay history of the version that produced them,
        never part of a new version's train score. With no install, the version is unchanged and the latest
        measurement is reused as it is. Attempts are recorded under their run identity, so a restart reuses what
        was paid for. A new release that runs tasks exactly as the measured one did — only R changed — is the same
        version for this purpose, and the measurement is reused (docs/modules/04_RESEARCH_LOOP.md §4).
        """
        complete = previous is None or not previous.get("missing")
        if previous is not None and complete and (previous["release"] == release_ref
                                                  or self._runs_like(previous["release"], release_ref)):
            return {**previous, "release": release_ref, "measured_release": previous.get("measured_release")
                    or previous["release"], "reused": True}
        if previous is not None and not complete and previous["release"] == release_ref:
            # A measurement a pause or a crash cut short is finished, not taken for the whole: the tasks it planned
            # are run again under resume, which keeps every recorded attempt and pays only for the missing ones.
            self._same_run(previous, release_ref, "an interrupted measurement")
            tasks, first = list(previous["planned"]), previous.get("first", False)
        elif previous is not None:
            tasks, first = list(previous["failed"]), False
        else:
            tasks, first = list(self.train_tasks or self.splits.ids("train")), True
        rows = self.train_measurement(release_ref, tasks, first=first)
        failed = [r.task_id for r in rows if not r.success]
        feedback = _feedback.build_feedback(self.rt, objects=self.objects, scope_id=self.scope_id)
        pool = "this activity's whole train split" if set(tasks) >= set(self.splits.ids("train")) \
            else f"a train pool of {len(tasks)} tasks"
        return {"release": release_ref, "results": list(rows), "train": list(rows), "dev": [],
                "feedback": feedback, "failed": failed, "scope": f"every task of {pool}" if first else
                "the train tasks that failed in the latest measurement", "reused": False,
                "planned": tasks, "missing": [], "first": first}

    def train_measurement(self, release_ref: str, tasks, *, first: bool = False, planned=None) -> list:
        """A version's train attempts under the one run identity a measurement has (arm "measure", candidate
        "current", no plan). The baseline's train run uses it too, so the first round finds the baseline's attempts
        and pays for none of them again: W0's train split is measured once per activity.

        The tasks are booked before anything runs (a `measurement_planned` event), so a measurement that a pause
        or a crash cut short is known to be incomplete when the loop restarts (`last_measurement`). `planned` books
        more than this call runs: a baseline limited to a few tasks is the start of the first measurement, not all
        of it, and the first round runs the rest."""
        tasks = list(tasks)
        if not tasks:
            return []
        self.releases.events.append("measurement_planned", scope=self.scope_id, release=release_ref,
                                    tasks=list(planned if planned is not None else tasks), first=first,
                                    run=self._measure_run(release_ref))
        return self.run_arm("measure", release_ref, [(t, 0) for t in tasks], plan_ref="", split="train",
                            candidate="current", role="base", resume=True)

    def _measure_run(self, release_ref: str) -> str:
        """The run hash a train measurement (or a collection batch) of this release is recorded under now."""
        return self.run_identity(release_ref, plan_ref="", split="train", candidate="current", role="base")[0].hash()

    def _same_run(self, booked: dict, release_ref: str, what: str) -> None:
        """Refuse to finish work booked under another run identity: its recorded attempts would not be found, and
        every one of them would be paid for again under the new identity (docs/ROADMAP.md §3)."""
        run = booked.get("run")
        if run and run != self._measure_run(release_ref):
            raise ValueError(f"{what} was booked under run {run} and this code records it under run "
                             f"{self._measure_run(release_ref)}: its attempts would be paid for again. Finish it "
                             f"with the code it started with, or start a new activity")

    def _runs_like(self, a: str, b: str) -> bool:
        """Whether two releases execute a task identically: the same W and the same K. R takes no part in a run."""
        try:
            ra = self.objects.load(a, _ct.SystemRelease)
            rb = self.objects.load(b, _ct.SystemRelease)
        except (KeyError, _ct.ContractError):
            return False
        return (ra.worker_ref, ra.core_ref) == (rb.worker_ref, rb.core_ref)

    def collect_batch(self, measured: dict, *, batch: int, root, max_tasks: int = 10) -> dict:
        """One bounded task_feedback batch (docs/modules/04_RESEARCH_LOOP.md §4): plain train attempts of the
        current version on the latest failures (or, with none, the first train tasks), frozen as a
        CollectionRequest and booked before anything runs. New valid results join the measurement the next round
        reads; none is no progress, which ends the segment rather than collecting in a loop.

        The booking (a `collection_booked` event: the request, its attempt index and tasks) is what a restart reads:
        a batch a crash cut short is finished at its own index (`finish_collections`) rather than paid for again
        under a new one, and it counts toward the segment's quota once, whether or not it finished."""
        release_ref = measured["release"]
        tasks = (list(measured.get("failed") or ()) or list(self.train_tasks or self.splits.ids("train")))[:max_tasks]
        request = _ct.CollectionRequest(
            purpose="task_feedback", release_ref=release_ref,
            task_selection_ref=self.objects.put_json("change", {"rule": "latest train failures, else train order",
                                                                "tasks": tasks}),
            limits_ref=self.objects.put_json("change", {"max_tasks": max_tasks, "attempt": batch}))
        request_ref = self.objects.put(request)
        used = [r.attempt for r in self.results.all() if _arm_of(r.attempt_id) == "collect"
                and r.system_release_ref == release_ref] if self.results is not None else []
        booked = [int(e.get("attempt") or 0) for e in self.releases.events.of("collection_booked")
                  if e.get("release") == release_ref]
        attempt = max(used + booked, default=0) + 1     # a fresh attempt index: a re-issued batch is not free data
        self.releases.events.append("collection_booked", scope=self.scope_id, segment=self.segment, batch=batch,
                                    release=release_ref, request=request_ref, tasks=tasks, attempt=attempt,
                                    run=self._measure_run(release_ref))
        return self._run_collection(measured, root=root, batch=batch, request_ref=request_ref, tasks=tasks,
                                    attempt=attempt)

    def _run_collection(self, measured: dict, *, root, batch: int, request_ref: str, tasks, attempt: int) -> dict:
        release_ref = measured["release"]
        rows = self.run_arm("collect", release_ref, [(t, attempt) for t in tasks], plan_ref="", split="train",
                            candidate="current", role="base", resume=True)
        # Progress is what this batch ran at its own attempt index, which no earlier batch used: attempts it
        # recorded before a crash are this batch's evidence as much as the ones finished after the restart.
        valid = [r for r in rows if r.validity == "valid" and r.attempt == attempt]
        # A measurement rebuilt after a restart already holds the attempts recorded before the crash.
        held = {r.attempt_id for r in measured.get("train", ())}
        fresh = [r for r in rows if r.attempt_id not in held]
        measured["results"] = [*measured.get("results", ()), *fresh]
        measured["train"] = [*measured.get("train", ()), *fresh]
        _experience.append(root, {"kind": "collection", "request": request_ref, "batch": batch,
                                  "scope": self.scope_id, "tasks": list(tasks), "valid": len(valid),
                                  "attempts": len(rows), **({"segment": self.segment} if self.segment else {})})
        return {"progress": bool(valid), "request_ref": request_ref,
                "reason": f"{len(valid)} new valid train attempts" if valid else "no new valid evidence"}

    def finish_collections(self, root, measured: dict | None = None) -> list[dict]:
        """The batches this segment booked and never finished — a crash between the booking and the experience
        record — each run again at its own attempt index under resume: what was recorded is kept, only the missing
        attempts are paid for, and the batch is not counted a second time."""
        finished = {e.get("request") for e in _experience.read(root) if e.get("kind") == "collection"
                    and e.get("scope") == self.scope_id and e.get("segment") == self.segment}
        out = []
        for e in self.releases.events.of("collection_booked"):
            if e.get("scope") != self.scope_id or e.get("segment") != self.segment or e.get("request") in finished:
                continue
            self._same_run(e, e["release"], f"collection batch {e.get('batch')}")
            target = measured if measured is not None and measured.get("release") == e["release"] else \
                {"release": e["release"], "results": [], "train": []}
            out.append(self._run_collection(target, root=root, batch=int(e["batch"]), request_ref=e["request"],
                                            tasks=e["tasks"], attempt=int(e["attempt"])))
            finished.add(e.get("request"))
        return out

    def last_measurement(self, release_ref: str) -> dict | None:
        """The measurement a restarted loop continues from, rebuilt from recorded results: the latest measured
        release, its attempts — the measurement's and the collection batches' of that release — and its failed
        train tasks. A restart after an install then reruns only those failures, as an uninterrupted loop would
        (RL6-20). A measurement whose booked tasks are not all on record comes back with what is `missing`, and
        `measure` finishes it rather than reading part of the split as the whole."""
        if self.results is None:
            return None
        by_release: dict = {}
        collected: dict = {}
        for r in self.results.all():
            if not r.system_release_ref:
                continue
            arm = _arm_of(r.attempt_id)
            if arm == "measure":
                by_release.setdefault(r.system_release_ref, []).append(r)
            elif arm == "collect":
                collected.setdefault(r.system_release_ref, []).append(r)
        if not by_release:
            return None
        latest = release_ref if release_ref in by_release else list(by_release)[-1]
        plan = self._measurement_plan(latest)
        # Only the booked run's attempts are this measurement: a row of another run (older code, another plan)
        # would make an incomplete measurement look complete.
        rows = [r for r in by_release[latest] if not (plan and plan.get("run")) or r.run == plan["run"]]
        seen = {r.task_id for r in rows}
        planned = list(plan["tasks"]) if plan else sorted(seen)
        missing = [t for t in planned if t not in seen]
        extra = collected.get(latest, [])
        feedback = _feedback.build_feedback(self.rt, objects=self.objects, scope_id=self.scope_id)
        return {"release": latest, "results": [*rows, *extra], "train": [*rows, *extra], "dev": [],
                "feedback": feedback, "failed": [r.task_id for r in rows if not r.success] + missing,
                "reused": True, "planned": planned, "missing": missing, "first": bool(plan and plan.get("first")),
                "run": (plan or {}).get("run"), "scope": "train tasks of the latest recorded measurement"}

    def _measurement_plan(self, release_ref: str) -> dict | None:
        """The latest booked measurement of a release in this scope, or None for one recorded before measurements
        were booked."""
        plan = None
        for e in self.releases.events.of("measurement_planned"):
            if e.get("scope") == self.scope_id and e.get("release") == release_ref:
                plan = e
        return plan

    def round_inputs(self, index: int, evidence: dict, experience, bound, *, root, research_model: dict,
                     shared_notes=None):
        """The first version's per-round inputs: the diagnosis of the train failures and the draft check. Both
        read their limits from K, and both are None for a v2 activity (docs/modules/04_RESEARCH_LOOP.md §1–2)."""
        if not self.harness:
            return None, None
        from .agent import harness_package as hp
        from .research import diagnosis as dg
        from .research.drafts import Drafts
        limits = dg.DiagnosisLimits(**self.harness["diagnosis"])
        package = hp.Limits.from_json(self.harness["package"])
        latest = {r.task_id: r for r in evidence.get("train") or () if r.validity == "valid"}
        if bound.package is None:
            raise _P.BindError("a first-version activity mounts a capability package, and this release has none")
        run_files, test_files = hp.files_of(self.objects, bound.package.ref)
        diagnosis = dg.Diagnosis(
            summaries=dg.failure_summaries(evidence.get("train") or (), store=self.results,
                                           transcripts=Path(self.rt.root) / "transcripts", limits=limits),
            order=list(self.splits.ids("train")), history=dg.last_included(experience, self.scope_id),
            limits=limits, cache=dg.NoteCache(Path(root), self.objects, shared=shared_notes), model=research_model,
            round_index=dg.diagnosis_count(experience, self.scope_id),
            components=package.components, package={**run_files, **test_files},
            scope=evidence.get("scope") or "train failures of the latest measurement",
            measured=len(latest), passed=sum(1 for r in latest.values() if r.success))
        drafts = Drafts(self.objects, parent_ref=bound.package.ref, limits=package,
                        literals=dg.train_literals(self.results) | self.train_state_literals(),
                        max_repairs=int(self.harness["max_draft_repairs"]), events=_ct.Events(root),
                        worker=self.worker, round_index=index, scope=self.scope_id,
                        allowed_literals=self.tool_vocabulary(), fixture=self.fixture_world,
                        reserved_names=self.tool_vocabulary(names_only=True),
                        image=self.harness.get("image_id") or self.harness.get("image"), tools=self.base_tools())
        return diagnosis, drafts

    def fixture_world(self):
        """The world package tests run against: a fresh episode of the first train task, never dev or final
        (docs/modules/05_EXECUTION_BUDGET.md §1)."""
        return self.suite.episode(next(iter(self.train_tasks or self.splits.ids("train"))))

    def train_state_literals(self) -> set:
        """Task-specific identifiers in the initial state of train tasks (record ids, e-mails, record names): R
        sees them in tool outputs, and none may be written into a shared component (docs/modules/00_CONTRACTS.md
        §3). Read from the grading side of the suite, train tasks only."""
        from .agent.harness_package import literals_of
        out: set = set()
        for task_id in self.train_tasks or self.splits.ids("train"):
            try:
                out |= literals_of(self.suite.grading(task_id).initial_state)
            except Exception:  # noqa: BLE001 - a task without a readable initial state adds nothing
                continue
        return out

    def base_tools(self) -> list[dict]:
        """The host's base tools as R is shown them when it writes or revises a package: name, description and
        parameter schema. They are what a package tool or hook can call through ctx.call_tool, and the summary
        sees no failure transcript to learn them from (docs/modules/04_RESEARCH_LOOP.md §1)."""
        try:
            episode = self.suite.episode(next(iter(self.train_tasks or self.splits.ids("train"))))
            tools = [{"name": s.name, "description": s.description, "parameters": dict(s.schema or {})}
                     for s in episode.specs()]
            episode.close()
            return tools
        except Exception:  # noqa: BLE001 - without an episode there is no list to show
            return []

    def tool_vocabulary(self, *, names_only: bool = False) -> set:
        """Names of the business tools and their parameters: the host's vocabulary, never a task's answer. With
        `names_only`, the tool names alone — the names a package tool may not take."""
        names: set = set()
        try:
            episode = self.suite.episode(next(iter(self.train_tasks or self.splits.ids("train"))))
            for spec in episode.specs():
                names.add(spec.name)
                if not names_only:
                    names |= set((spec.schema or {}).get("properties", {}))
            episode.close()
        except Exception:  # noqa: BLE001 - without an episode the vocabulary is simply empty
            pass
        return names

    def training_options(self) -> dict:
        """What a training proposal may name, when this activity has a trainer at all.

        The data selection and the recipe are frozen objects the researcher refers to, not text it writes: the
        selection decides which rows exist and the recipe decides what a job costs, and neither is something an
        untrusted proposal should be able to invent (docs/modules/04_RESEARCH_LOOP.md §2.6).
        """
        if self.trainer is None:
            return {}
        selection = self.objects.put_json("change", {"seed": self.order_seed, "validation": 0.25,
                                                     "max_per_task": 20, "min_groups": 1})
        recipe = self.objects.put_json("change", {})
        return {"worker.stop.training": {"feedback": ["completion_snapshot@v1"],
                                         "data_selection_ref": selection, "recipe_ref": recipe,
                                         "point": "worker.stop"}}

    # -------------------------------------------------------------- handlers
    def handlers(self) -> dict:
        return {"begin": self._begin, "build": self._build, "screen": self._screen, "evaluate": self._evaluate,
                "install": self._install, "verify_bound": self._verify_bound(),
                "load_receipt": lambda ref: self.objects.load(ref, _ct.EvaluationReceipt)}

    def _begin(self, experiment) -> None:
        """The receipt names the experiment it came from, also when a resumed experiment reuses its build; and the
        comparison is built on the parent it was frozen on, also when a resume finds the pointer moved
        (docs/modules/05_EXECUTION_BUDGET.md §3). The install still compares that parent with the pointer."""
        self._experiment_ref = experiment.ref()
        self._parent_release = experiment.parent_release

    def _parent(self) -> str:
        return self._parent_release or self.releases.active()["release_ref"]

    def _verify_bound(self) -> dict:
        """What a full comparison plus the install probe will cost at least, so a candidate that could not be
        verified is never started (EX-03)."""
        attempts = len(self.dev_tasks) * self.repeats * 2
        return {"model_calls": attempts, "tool_calls": attempts}

    def _build(self, experiment, budget) -> _ct.BuildResult:
        self._experiment_ref = experiment.ref()      # the receipt names the experiment it came from
        return _P.build_candidate(experiment, objects=self.objects,
                                  parent_release=experiment.parent_release, owner=self.owner,
                                  services=self.services(), train=self._train, budget=budget)

    def _train(self, entry: dict, budget) -> dict:
        """The training entry of a proposal: dataset, artifact, inference configuration. No pointer moves."""
        from .evolve.pipeline import train_artifact
        if self.trainer is None:
            raise ValueError("this activity has no trainer: a training proposal cannot be built")
        if entry.get("point") != "worker.stop":
            # The build installs what this trains at worker.stop; a training entry for another target is refused
            # at admission, and refused here for one frozen before that rule.
            raise _ct.ContractError(f"training {entry.get('point')}: this activity trains the worker.stop decision only")
        feedback = _feedback.build_feedback(self.rt, objects=self.objects,
                                           builder=entry.get("feedback_ref", "completion_snapshot.v1")
                                           .replace("@", "."), scope_id=self.scope_id)
        selection = self.objects.get(entry["data_selection_ref"])
        dataset = _build_task_dataset(self.rt, _business_stop_point(), feedback,
                                      out_dir=self.rt.root / "rsi" / "datasets" / entry["point"],
                                      objects=self.objects, splits=self.splits,
                                      seed=int(selection.get("seed", 0)),
                                      validation=float(selection.get("validation", 0.25)),
                                      max_per_task=int(selection.get("max_per_task", 20)),
                                      min_groups=int(selection.get("min_groups", 2)))
        if dataset.status != "ready":
            return {"reason": f"dataset {dataset.status}: {dataset.exclusions}"}
        built = train_artifact(self.rt, self.trainer, dataset, self.objects.get(entry["recipe_ref"]),
                               objects=self.objects, budget=budget)
        if built.status != "ready":
            return {"reason": f"training {built.status}: {'; '.join(built.errors)}"}
        return {"model_ref": built.model_ref, "target": "worker.stop", "artifact_ref": built.artifact_ref,
                "spec": {"backend": built.backend_spec},
                "inference": {"model_id": built.model_ref, "actions": {"yes": "yes", "no": "no"},
                              "threshold_source": "argmax"},
                "costs": built.costs, "dataset_ref": dataset.dataset_ref}

    def _screen(self, candidate_ref: str, budget) -> tuple:
        if not self.screen_ids:
            return "pass", ["no screen in this plan"]
        parent = self._parent()
        plan = self.plan(candidate_ref, self.objects.load(parent, _ct.SystemRelease).worker_ref, parent)
        plan_ref = self.objects.put(plan)
        permit = self.releases.permit(candidate_ref, parent, plan_ref, self.scope_id, self.core_ref)
        arm_release = self.releases.candidate_release(parent, candidate_ref, permit)
        order = [(t, 0) for t in self.screen_ids]
        subject = self.run_arm("screen_subject", arm_release, order, plan_ref=plan_ref, permit=permit)
        control = self.run_arm("screen_control", parent, order, plan_ref=plan_ref)
        return _screen(plan, subject, control)

    def _evaluate(self, candidate_ref: str, budget) -> _ct.EvaluationReceipt:
        parent = self._parent()
        control_set = self.objects.load(parent, _ct.SystemRelease).worker_ref if self.owner == "worker" else \
            self.objects.load(parent, _ct.SystemRelease).research_ref
        plan = self.plan(candidate_ref, control_set, parent)
        plan_ref = self.objects.put(plan)
        permit = self.releases.permit(candidate_ref, parent, plan_ref, self.scope_id, self.core_ref)
        arm_release = self.releases.candidate_release(parent, candidate_ref, permit)
        self._plans[candidate_ref] = (plan_ref, arm_release)

        def run(arm, order, retry=0, supersedes=None):
            if arm == "subject":
                return self.run_arm(arm, arm_release, order, plan_ref=plan_ref, permit=permit,
                                    retry_index=retry, resume=True, supersedes=supersedes)
            return self.run_arm(arm, parent, order, plan_ref=plan_ref, candidate="control", role="base",
                                retry_index=retry, resume=True, supersedes=supersedes)

        receipt = evaluate_task(plan, run=run, objects=self.objects, experiment_ref=self._experiment_ref,
                                families=self.families())
        ref = self.objects.put(receipt)
        self.releases.register_receipt(ref, plan=plan_ref, subject=candidate_ref, verdict=receipt.verdict,
                                      scope=self.scope_id)
        return receipt

    def _install(self, receipt, expected_parent):
        if not self.plan_installs:
            from .evolve.contracts import InstallationReceipt
            return InstallationReceipt(operation_id="none", expected_parent=expected_parent,
                                       target_release=expected_parent or "", status="installation_failed",
                                       reason="this plan reports and does not install")
        ref = self.objects.put(receipt)
        operation = f"install-{_ct.digest({'receipt': ref, 'parent': expected_parent})[:12]}"
        return self.releases.install(ref, expected_parent, operation, preflight=self._preflight)

    def _preflight(self, target_release: str) -> dict:
        """Assemble the release that is about to go live, in isolation, and probe what it loads.

        A package that runs code is installed only where the qualified worker it was evaluated with is there
        (docs/modules/03_EVALUATION_RELEASE.md §3): the package bytes are the evaluated ones by content address,
        and the worker's image must be the one K froze. A missing worker fails the preflight; nothing falls back.
        """
        bound = _P.bind_release(target_release, profile="preflight", services=self.services())
        out = {"release": target_release, "policies": _P.describe(bound), "prompt": bound.system_prompt("")[1]}
        if bound.package is not None:
            out["package"] = bound.package.ref
            if bound.package.runs_code:
                if self.worker is None:
                    raise _P.BindError(f"package {_ct.short(bound.package.ref)} runs code and no qualified isolated "
                                       f"worker is available to run it")
                out["worker"] = self.worker.describe()
        return out


from .research.experiment import frozen_signatures  # noqa: E402 - beside the loop that uses it


def _effective_budget(remaining: dict) -> dict:
    """What a research step is told about money: what is left wherever the binding limit sits, and what it has
    spent — not an empty `limits` of a phase account that spends from the activity's root."""
    return {"remaining": remaining.get("effective_remaining") or remaining.get("remaining") or {},
            "used": remaining.get("used") or {}}


def _collection_batches(root, scope_id: str, segment: str | None, *, any_segment: bool = False) -> int:
    """How many collection batches count toward a quota: every batch booked, finished or not, so neither a crash nor
    a restart gives a segment a batch back. Records from before batches were booked count as they were."""
    def mine(e: dict) -> bool:
        return e.get("scope") == scope_id and (any_segment or e.get("segment") == segment)
    booked = {(e.get("segment"), e.get("request")) for e in _ct.Events(root).of("collection_booked") if mine(e)}
    recorded = {(e.get("segment"), e.get("request")) for e in _experience.read(root)
                if e.get("kind") == "collection" and mine(e)}
    return len(booked | recorded)


def _arm_of(attempt_id: str | None) -> str:
    """The arm an attempt id names (benchmark.attempt_id: `<run>/<task>/r<repeat>/<arm>#<retry>`)."""
    return (attempt_id or "").rpartition("#")[0].rpartition("/")[2]


def _retry_of(attempt_id: str) -> int:
    """The retry index an attempt id carries (benchmark.attempt_id: `...#<retry>`)."""
    _, _, retry = attempt_id.rpartition("#")
    return int(retry) if retry.isdigit() else 0


def _split_of_tasks(splits: SplitManifest, tasks) -> str:
    """The one split these tasks are in. One run reads one split, so a mixed list is a configuration error rather
    than something to sort out silently."""
    found = {splits.split_of(t) for t in tasks}
    if len(found) != 1:
        raise ValueError(f"these tasks span several splits ({sorted(found)}): one run reads one split")
    return found.pop()


def _trace_refs(objects: _ct.Objects) -> tuple:
    directory = objects.dir / "trace"
    return tuple(f"trace:sha256:{p.stem}" for p in sorted(directory.glob("*.json"))) if directory.exists() else ()


# ---------------------------------------------------------------------------- phase setup
@_dataclass
class Activity:
    """What every RSI phase needs: the environment, the frozen splits, the workspace and the config."""
    bench: BenchConfig
    rsi: RsiConfig
    suite: object
    splits: SplitManifest
    drift: list
    root: object
    results: Results
    rt: object
    objects: _ct.Objects
    releases: Releases
    versions: dict
    release_ref: str
    budgets: Budgets
    bootstrapped: bool = False


# ---------------------------------------------------------------------------- the first version's harness table
HARNESS_KEYS = {"package_api", "components", "sandbox", "image", "max_files", "max_package_bytes",
                "max_memory_chars", "max_skill_chars", "max_skills", "test_timeout_seconds", "max_draft_repairs",
                "max_failure_notes", "max_entry_chars", "max_effort"}
# The reasoning-effort tiers, lowest first (the `claude_code` provider's names). K's `max_effort` is the ceiling the
# gateway caps a package's requested effort to (docs/modules/00_CONTRACTS.md §2, v0.3): a request above it is sent
# at the ceiling, never refused, with the requested value on the record. Like every harness key it narrows only.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MAX_EFFORT = "high"


# ---------------------------------------------------------------------------- [improvement] and its presets
# docs/modules/07_BENCH_REPORT.md §2. The reader accepts only what is implemented: an unknown preset, mode or
# method fails here, before anything is paid for, and a second-version method is refused by name.
IMPROVEMENT_KEYS = {"mode", "preset", "allowed_methods", "max_candidates_per_round", "max_collection_batches"}
METHODS = ("policy_edit", "harness_edit")
LATER_METHODS = ("distill_actor", "decision_train")
PRESETS = {
    # The first version's Marketing preset. Its harness numbers are ceilings: [harness_evolution] may narrow the
    # component kinds and lower a limit, never raise one; once frozen into K, R cannot raise them either.
    "marketing_harness_v1": {
        "domain": "marketing",
        "harness": {"package_api": "harness_package.v1",
                    "components": ["memory", "skills", "on_finish", "before_tool", "after_tool", "tools"],
                    "sandbox": "docker", "image": "ouroloop-worker:py3.12", "max_files": 24, "max_package_bytes": 262144, "max_memory_chars": 8000,
                    "max_skill_chars": 8000, "max_skills": 12, "test_timeout_seconds": 30,
                    "max_draft_repairs": 3, "max_failure_notes": 30, "max_entry_chars": 600,
                    "max_effort": DEFAULT_MAX_EFFORT},
        # One worker segment, one research slot, one more worker segment that shows the installed R is used.
        "schedule": {"worker_rounds": 3, "research_slots": 1, "patience": 2, "max_infrastructure_failures": 2},
        # The existing worker gate's own fields; no second regression layer.
        "task_plan": {"tasks": "dev", "repeats": 1, "order_seed": 0,
                      "gate": {"mode": "quality", "min_gain": 0.05, "min_groups": 5, "max_regression_rate": 0.1}},
        # A research slot's comparison: each arm gets the same fixed share of the frozen activity budget, and
        # the frozen dev families are halved by their frozen order — the arms' inner gate reads the first half,
        # the outer comparison the second, which neither arm touches (the research_dev pool of M6 §1).
        # The outer comparison's share (both W*, one infrastructure refill) is its limit, and is set aside before
        # the common inputs and the arms run.
        "campaign": {"worker_round_limit": 2, "arm_share": 0.15, "outer_share": 0.08, "min_gain": 0.05,
                     "min_groups": 1},
    },
}


def improvement_settings(cfg: dict) -> dict | None:
    """`[improvement]`, validated against its preset. None when the table is absent (the older entry points)."""
    table = dict(cfg.get("improvement") or {})
    if not table:
        return None
    unknown = sorted(set(table) - IMPROVEMENT_KEYS)
    if unknown:
        raise ValueError(f"[improvement] does not take {unknown}; it takes {sorted(IMPROVEMENT_KEYS)}")
    mode = table.get("mode", "automatic")
    if mode != "automatic":
        raise ValueError(f"[improvement] mode {mode!r}: this reader runs the automatic loop; a manual proposal "
                         f"goes through `ouroloop propose`, and scripted researchers exist for fixtures only")
    name = table.get("preset")
    if name not in PRESETS:
        raise ValueError(f"[improvement] preset {name!r}: one of {sorted(PRESETS)}")
    if "allowed_methods" in table and not table["allowed_methods"]:
        raise ValueError("[improvement] allowed_methods is empty: name at least one of "
                         f"{list(METHODS)}, or leave the key out for both")
    methods = list(table.get("allowed_methods") or METHODS)
    later = sorted(set(methods) & set(LATER_METHODS))
    if later:
        raise ValueError(f"[improvement] allowed_methods {later} belong to the second version and are not "
                         f"implemented; this version allows {list(METHODS)}")
    unknown = sorted(set(methods) - set(METHODS))
    if unknown:
        raise ValueError(f"[improvement] allowed_methods {unknown}: one of {list(METHODS)}")
    preset = PRESETS[name]
    domain = dict(cfg.get("bench") or {}).get("domain", "marketing")
    if domain != preset["domain"]:
        raise ValueError(f"[improvement] preset {name!r} is for the {preset['domain']} domain, not {domain!r}")
    candidates = int(table.get("max_candidates_per_round", 1))
    batches = int(table.get("max_collection_batches", 2))
    if candidates != 1:
        raise ValueError("[improvement] max_candidates_per_round: this version runs one candidate per round")
    if not 0 <= batches <= 2:
        raise ValueError("[improvement] max_collection_batches: at most 2 per worker segment in this preset")
    return {"mode": mode, "preset": name, "allowed_methods": methods, "candidates_per_round": candidates,
            "collection_batches": batches, **{k: preset[k] for k in ("harness", "schedule", "task_plan",
                                                                         "campaign")}}


def _preset_harness(cfg: dict, preset: dict) -> dict:
    """The preset's harness table narrowed by [harness_evolution]: fewer component kinds, lower limits."""
    table = dict(cfg.get("harness_evolution") or {})
    unknown = sorted(set(table) - HARNESS_KEYS)
    if unknown:
        raise ValueError(f"[harness_evolution] does not take {unknown}; it takes {sorted(HARNESS_KEYS)}")
    out = dict(preset)
    for key, value in table.items():
        if key == "components":
            wider = sorted(set(value) - set(preset["components"]))
            if wider:
                raise ValueError(f"[harness_evolution] components {wider} are outside the preset's "
                                 f"{preset['components']}: a preset is not widened from the config")
            out[key] = [k for k in preset["components"] if k in set(value)]
        elif key in ("sandbox", "image", "package_api"):
            if value != preset.get(key):
                raise ValueError(f"[harness_evolution] {key} {value!r}: the preset fixes {preset.get(key)!r}")
            out[key] = value
        elif key == "max_effort":
            ceiling = preset.get(key, DEFAULT_MAX_EFFORT)
            if value not in EFFORTS:
                raise ValueError(f"[harness_evolution] max_effort {value!r}: one of {list(EFFORTS)}")
            if EFFORTS.index(value) > EFFORTS.index(ceiling):
                raise ValueError(f"[harness_evolution] max_effort = {value} is above the preset's {ceiling}: a "
                                 f"limit may be lowered here, not raised")
            out[key] = value
        elif int(value) > int(preset[key]):
            raise ValueError(f"[harness_evolution] {key} = {value} is above the preset's {preset[key]}: a limit "
                             f"may be lowered here, not raised")
        else:
            out[key] = int(value)
    return out


def harness_settings(cfg: dict) -> dict | None:
    """`[harness_evolution]`, validated, as the object K freezes. None when neither it nor an [improvement]
    preset is set: the activity then runs the v2 loop unchanged. Once frozen into K, rounds read these limits
    from K, never from the config, so R cannot raise them (docs/modules/07_BENCH_REPORT.md §2)."""
    from .agent import harness_package as hp
    from .research.diagnosis import DiagnosisLimits
    improvement_cfg = improvement_settings(cfg)
    if improvement_cfg is not None:
        table = _preset_harness(cfg, improvement_cfg["harness"])
    else:
        table = dict(cfg.get("harness_evolution") or {})
    if not table:
        return None
    unknown = sorted(set(table) - HARNESS_KEYS)
    if unknown:
        raise ValueError(f"[harness_evolution] does not take {unknown}; it takes {sorted(HARNESS_KEYS)}")
    # The package API K freezes is the one its candidates write to; a core frozen to v1 keeps taking v1 manifests
    # on a host that serves v2, and takes no v2 manifest (hp.Limits.package_api, which also fixes the default).
    try:
        limits = hp.Limits(components=tuple(table.get("components") or hp.TEXT_KINDS),
                           package_api=table.get("package_api"),
                           **{k: int(table[k]) for k in ("max_files", "max_package_bytes", "max_memory_chars",
                                                         "max_skill_chars", "max_skills") if k in table})
    except hp.PackageError as e:
        raise ValueError(f"[harness_evolution] {e}") from None
    package_api = limits.package_api
    max_effort = table.get("max_effort", DEFAULT_MAX_EFFORT)
    if max_effort not in EFFORTS:
        raise ValueError(f"[harness_evolution] max_effort {max_effort!r}: one of {list(EFFORTS)}")
    diagnosis = DiagnosisLimits(**{k2: int(table[k1]) for k1, k2 in (("max_failure_notes", "max_notes"),
                                                                     ("max_entry_chars", "max_entry_chars"))
                                   if k1 in table})
    repairs = int(table.get("max_draft_repairs", 3))
    if repairs < 0:
        raise ValueError("[harness_evolution] max_draft_repairs is a count of revisions after the first draft")
    return {"package_api": package_api, "package": limits.to_json(), "diagnosis": diagnosis.to_json(),
            "max_draft_repairs": repairs, "max_effort": max_effort,
            "sandbox": table.get("sandbox"), "image": table.get("image"),
            "test_timeout_seconds": int(table.get("test_timeout_seconds", 30))}


def _host_contract(objects: _ct.Objects, bench: "BenchConfig", rsi: "RsiConfig", harness: dict) -> dict:
    """The first version pins the host it runs on (docs/modules/00_CONTRACTS.md §2): a TargetHarness describing
    the current host's fixed semantics, and the capabilities a runtime needs to load the release."""
    from .agent import harness_package as hp

    def ref(payload: dict) -> str:
        return objects.put_json("change", payload)
    target = objects.put(_ct.TargetHarness(
        runtime_ref=ref({"host": "ouroloop.agent.loop", "adapter": "ouroloop.adapters.automationbench",
                         "domain": bench.domain}),
        tool_schema_ref=ref({"toolset": bench.toolset, "host_tools": list(hp.HOST_TOOLS)}),
        action_schema_ref=ref({"actions": "tool calls", "schema": "chat.turn.v1"}),
        message_schema_ref=ref({"messages": "chat.messages.v1"}),
        context_policy_ref=ref({"compaction": "points.compact", "memory_and_skill_index": "system prompt"}),
        finish_protocol_ref=ref({"stop_view": rsi.stop_view, "hook_sites": list(hp.HOOK_SITES)}),
        permission_ref=ref({"approve": "auto", "gated_tools": [], "package_broker": "base tools; hooks read-only"}),
        # The five hard limits of one attempt (M0 §2), None where the config sets none: a limit a core fixed is a
        # fact of the host contract, and so is a limit it did not fix.
        limits_ref=ref({k: getattr(bench.budget, k) for k in HARD_LIMITS}),
        extension_api_ref=hp.api_ref(objects, harness["package_api"])))
    needs = ["generation", harness["package_api"]]
    if hp.Limits.from_json(harness["package"]).allows_code:
        needs.append("isolated_worker")
    return {"target_harness_ref": target, "required_capabilities": tuple(needs)}


def actor_identity(objects: _ct.Objects, table: dict) -> str:
    """The configured model as the actor W0 pins: its provider configuration less any secret. A hosted alias is
    recorded as mutable with when it was seen, not as frozen weights (docs/modules/00_CONTRACTS.md §2)."""
    config = {k: v for k, v in table.items() if not SECRET_KEY.match(k)}
    return objects.put(_ct.ModelIdentity(role="actor", provider=str(config.get("type", "")),
                                         base_model=str(config.get("model") or config.get("type", "")),
                                         inference_ref=objects.put_json("change", config), mutable=True,
                                         observed_at=now()))


def open_activity(cfg: dict, base: str | Path = ".", *, out: Callable[[str], None] = print,
                  bootstrap: bool = True) -> Activity:
    """Load the environment, freeze the splits if they are not frozen, and find or create the initial release.

    Creating it is a bootstrap authorisation, not an improvement: nothing has been compared at that point, and a
    baseline that called itself better would be claiming a result from a configuration file
    (docs/modules/00_CONTRACTS.md §4).
    """
    bench = BenchConfig.load(cfg, base)
    rsi = RsiConfig.load(cfg)
    versions = ab.require(bench.upstream_path)
    suite = ab.load_suite(bench.domain, bench.upstream_path)
    root = config_mod.workspace_root(cfg, base)
    results = Results(root, bench.domain)
    manifest, drift = freeze_splits(suite, bench, results.manifest_path())
    rt = config_mod.build_runtime(cfg, base)
    config_mod.register_builtin_points(rt)
    objects = _ct.Objects(root)
    releases = Releases(root, objects)
    release_ref, bootstrapped = releases.active()["release_ref"], False
    if rsi.initial_release_ref:
        missing = rsi.check(objects, needs=("initial_release_ref",))
        if missing:
            raise ValueError("; ".join(missing))
        release_ref = release_ref or rsi.initial_release_ref
    elif release_ref is None and bootstrap:
        harness = harness_settings(cfg)
        targets = ()
        capabilities = {"training": bool(cfg.get("trainer"))}
        worker_kw: dict = {}
        if harness is not None:
            # The first version (docs/modules/00_CONTRACTS.md §1): K opens the actor and the package targets and
            # freezes the package limits; W0 pins the configured model and an empty package; R0 writes notes.
            from .agent import harness_package as hp
            if "llm" not in cfg:
                raise ValueError("[harness_evolution] needs an [llm] table: W0 pins it as the actor")
            targets = (*_P.targets_of("worker"), "worker.actor", "worker.harness", *_P.targets_of("research"))
            if harness.get("sandbox") == "docker":
                # K freezes the worker image by content id. With code components allowed and no image to freeze,
                # nothing is bootstrapped: a K without the id would accept any image under the tag later.
                from .agent.worker import DEFAULT_IMAGE, DockerWorker
                image = harness.get("image") or DEFAULT_IMAGE
                harness = {**harness, "image": image, "image_id": DockerWorker(image).image_id()}
                if harness["image_id"] is None and hp.Limits.from_json(harness["package"]).allows_code:
                    raise ValueError(f"K allows code components and the worker image {image} is not available "
                                     f"(is Docker running? build it with: docker build -f docker/worker.Dockerfile "
                                     f"-t {image} .), so there is no image to freeze; nothing was bootstrapped")
            capabilities = {**capabilities, "training": False, "harness": harness}
            settings = improvement_settings(cfg)
            if settings is not None:
                # The methods the activity allows, frozen with K: admission refuses a change of any other kind.
                capabilities["methods"] = list(settings["allowed_methods"])
            worker_kw = {"actor_identity_ref": actor_identity(objects, cfg["llm"]),
                         "package_ref": hp.empty_package(objects)}
        core_ref = rsi.core_ref or _P.default_core(
            objects, domain=bench.domain, split_ref=manifest.hash(),
            adapter="ouroloop.adapters.automationbench", scorer="automationbench.rubric",
            toolset=bench.toolset, targets=targets, capabilities=capabilities)
        release_ref = releases.bootstrap(_P.default_worker_set(objects, **worker_kw),
                                        _P.default_research_set(objects, note_contract=harness is not None),
                                        core_ref, reason="initial version from the configuration",
                                        **(_host_contract(objects, bench, rsi, harness) if harness is not None
                                           else {}))
        if harness is not None:
            # The alias's observation time is kept out of the identity (it would change the ref) and recorded
            # here, where a report can find when the hosted model was seen (docs/modules/00_CONTRACTS.md §2).
            _ct.Events(root).append("model_observed", identity=worker_kw["actor_identity_ref"], observed_at=now())
            out(f"harness    K froze components={harness['package']['components']} "
                f"package_api={harness['package_api']} max_effort={harness['max_effort']} "
                f"max_draft_repairs={harness['max_draft_repairs']} "
                f"max_failure_notes={harness['diagnosis']['max_notes']} "
                f"max_entry_chars={harness['diagnosis']['max_entry_chars']} "
                f"max_files={harness['package']['max_files']} max_package_bytes={harness['package']['max_package_bytes']}")
        bootstrapped = True
        out(f"release    {_ct.short(release_ref)} bootstrapped (W0, R0, K); no comparison has been made yet")
    if release_ref is None:
        raise ValueError("nothing is installed and bootstrap is off: freeze an initial release first")
    out(f"release    {_ct.short(release_ref)} active, core {_ct.short(_ct.SystemRelease.from_json(objects.get(release_ref)).core_ref)}")
    for line in drift:
        out(f"DRIFT      {line}")
    return Activity(bench, rsi, suite, manifest, drift, root, results, rt, objects, releases, versions,
                    release_ref, Budgets(root), bootstrapped)


def budget_limits(frozen: dict | None) -> dict:
    """The frozen budget's limits on the activity's root: every dimension except the allocations it sets aside."""
    return {k: v for k, v in (frozen or {}).items() if k != "allocations"}


def budget_allocations(frozen: dict | None) -> dict:
    """What the frozen budget sets aside on the root, by name: {"final": {...}} keeps the final comparison's share
    out of reach of every other phase (docs/ROADMAP.md §3)."""
    return dict((frozen or {}).get("allocations") or {})


def budget_problems(frozen: dict | None, pricing=None) -> list[str]:
    """What is wrong with a frozen budget's allocations, found before a root is opened with it: a share must limit
    a dimension the budget limits (otherwise it sets nothing aside), be above zero and leave the rest something to
    spend; with prices, one call must fit both in the share and in what is left beside it."""
    from .evolve.execution import call_bound_problem
    limits, problems = budget_limits(frozen), []
    set_aside: dict = {}
    for name, share in budget_allocations(frozen).items():
        for dim, value in share.items():
            if dim not in limits:
                problems.append(f"the {name} share limits {dim}, which the budget does not: it sets nothing aside")
            elif not 0 < float(value):
                problems.append(f"the {name} share of {dim} is {value}: a share is above zero")
            set_aside[dim] = set_aside.get(dim, 0.0) + float(value)
        if pricing is not None and getattr(pricing, "bounded", False):
            problem = call_bound_problem(pricing, share, f"the {name} share")
            if problem:
                problems.append(problem)
    rest = {dim: float(limit) - set_aside.get(dim, 0.0) for dim, limit in limits.items()}
    for dim, left in rest.items():
        if dim in set_aside and left <= 0:
            problems.append(f"the shares set aside {set_aside[dim]} {dim} of a budget of {limits[dim]}: nothing is "
                            f"left for anything else")
    if set_aside and pricing is not None and getattr(pricing, "bounded", False):
        problem = call_bound_problem(pricing, rest, "what is left beside the shares")
        if problem:
            problems.append(problem)
    return problems


def activity_budget(activity: Activity, cfg: dict, phase: str):
    """The phase's account: a child of the activity's one root, whose limits come from the frozen budget object.

    One root per activity is the whole point. Opening another root with the same limits — for a campaign, say —
    would hand that phase a fresh copy of the budget, and the activity could then spend twice what it declared
    (docs/modules/05_EXECUTION_BUDGET.md §2). Without a budget object the root has no limits: spend is still
    measured and recorded, it is just not capped.
    """
    rsi_cfg = RsiConfig.load(cfg)
    frozen = activity.objects.get(rsi_cfg.budget_ref) if rsi_cfg.budget_ref else {}
    problems = budget_problems(frozen)
    if problems:            # refused before the root exists: a root cannot be reopened with other limits later
        raise BudgetMismatch(f"the frozen budget of {rsi_cfg.scope_id}: " + "; ".join(problems))
    root = activity.budgets.account(rsi_cfg.scope_id, budget_limits(frozen))
    # The allocations are part of the root's identity, like its limits: set once, before anything is spent, and
    # never changed, added or withdrawn after.
    wanted = {name: {k: float(v) for k, v in limits.items()} for name, limits in budget_allocations(frozen).items()}
    state = root.remaining()
    if state["allocations"] != wanted:
        if state["allocations"]:
            raise BudgetMismatch(f"{rsi_cfg.scope_id} sets aside {state['allocations']}, and the frozen budget says "
                              f"{wanted}: an open root's allocations do not change")
        if state["used"]:
            raise BudgetMismatch(f"{rsi_cfg.scope_id} has spent without the allocations {sorted(wanted)}: they cannot "
                              f"be added to a root that has already spent")
        for name, limits in wanted.items():
            root.allocate(name, limits)
    return root.child(f"{rsi_cfg.scope_id}:{phase}", {})


def freeze_task_plan(objects: _ct.Objects, *, core_ref: str, split_ref: str, task_ids, gate: GatePlan,
                     repeats: int = 1, order_seed: int = 0, screen_ids=(), installs: bool = True,
                     per_attempt_budget: dict | None = None) -> str:
    """The development plan template: tasks, repeats, order, thresholds and statistics, frozen before the first
    candidate runs. A candidate's own artifact ref is filled in later; the thresholds are not touched again."""
    return objects.put(TaskEvalPlan(candidate_ref="", control_ref="", parent_release="", core_ref=core_ref,
                                    split_ref=split_ref, task_ids=tuple(task_ids), repeats=repeats,
                                    order_seed=order_seed, screen_ids=tuple(screen_ids),
                                    gate_plan=gate.to_json(), installs=installs,
                                    per_attempt_budget=dict(per_attempt_budget or {})))


def _frozen_plan(activity: Activity, ref: str | None = None) -> TaskEvalPlan | None:
    """The frozen task plan `ref` names — the activity's own [rsi] task_plan_ref when none is given — or None when
    there is no such frozen object yet."""
    ref = ref or activity.rsi.task_plan_ref
    if ref and activity.objects.has(ref):
        return TaskEvalPlan.from_json(activity.objects.get(ref))
    return None


def _attempt_budget(activity: Activity, template=None) -> Budget:
    """The per-attempt limits a run uses: the frozen task plan's (`ouroloop freeze` writes them all), so a change to
    [bench.budget] after the freeze changes no comparison of the activity; [bench.budget] only for a plan frozen
    without them. The activity's own plan when none is given."""
    if template is None:
        template = _frozen_plan(activity)
    if template is not None and template.per_attempt_budget:
        return Budget(**{**asdict(activity.bench.budget), **template.per_attempt_budget})
    return activity.bench.budget


def improvement(activity: Activity, *, template_ref: str | None = None, provider=None, trainer=None,
                scope_id: str | None = None, owner: str = "worker", installs: bool | None = None,
                pricing=None, budget=None) -> Improvement:
    """The activity's Improvement, with its comparison policy read from the frozen template."""
    template = TaskEvalPlan.from_json(activity.objects.get(template_ref)) if template_ref else None
    gate = template.gate() if template and template.gate_plan else GatePlan()
    tasks = template.task_ids if template else tuple(activity.splits.ids("dev"))
    attempt = _attempt_budget(activity, template)
    return Improvement(
        rt=activity.rt, suite=activity.suite, splits=activity.splits, objects=activity.objects,
        releases=activity.releases, provider=provider, core_ref=_ct.SystemRelease.from_json(
            activity.objects.get(activity.release_ref)).core_ref,
        gate=gate, dev_tasks=tasks, scope_id=scope_id or activity.rsi.scope_id,
        repeats=template.repeats if template else activity.rsi.repeats,
        order_seed=template.order_seed if template else activity.rsi.order_seed,
        screen_ids=template.screen_ids if template else (), serving=activity.rt.serving, trainer=trainer,
        budget=budget,
        results=activity.results, stop_view=activity.rsi.stop_view, attempt_budget=attempt,
        probe_train=True, train_tasks=tuple(activity.splits.ids("train")), owner=owner, pricing=pricing,
        harness=_harness_of(activity), worker=isolated_worker(_harness_of(activity)), template_ref=template_ref or "",
        concurrency=activity.bench.concurrency,
        plan_installs=template.installs if template and installs is None else (True if installs is None
                                                                               else installs))


def require(problems: list, *, phase: str) -> None:
    """Refuse a phase whose preconditions do not hold, naming every one of them."""
    if problems:
        raise ValueError(f"the {phase} phase cannot start: " + "; ".join(problems))


# ---------------------------------------------------------------------------- baseline
FRONTIER_KEYS = {"llm", "effort", "fallback", "source", "date", "note", "pricing"}


def _harness_of(activity: Activity) -> dict | None:
    rel = _ct.SystemRelease.from_json(activity.objects.get(activity.release_ref))
    return (activity.objects.load(rel.core_ref, _ct.CoreManifest).capabilities or {}).get("harness")


def _five_limits(activity: Activity, plan: TaskEvalPlan | None) -> bool:
    """Whether a phase of this activity runs under the five-limit rule (docs/modules/00_CONTRACTS.md §2, v0.3).
    A plan frozen before the two v0.3 dimensions existed carries neither key (nor any, before M5 §3 put the limits
    in the plan): it keeps its identity and runs with what it has, so reading it refuses nothing and says nothing
    (CT7-06). A plan that carries them was frozen under the rule. Without a plan the rule is K's: a core frozen
    under v0.3 fixes `max_effort`, and every attempt under it is limited in all five dimensions; a K from before
    has only the three."""
    if plan is not None:
        return set(V03_BUDGET_FIELDS) <= set(plan.per_attempt_budget or {})
    harness = _harness_of(activity)
    return bool(harness and "max_effort" in harness)


def limit_problems(budget: Budget, five: bool = True) -> list[str]:
    """Which hard limits an attempt would run without (docs/modules/00_CONTRACTS.md §2 "单次尝试硬上限", v0.3). All
    five for every new freeze and for every phase of an activity whose K is v0.3, whatever components the core
    allows: an agent of its own decides how many model calls and output tokens a step takes, so there only a step
    limit is no limit — and a core that allows no agent today is frozen with the same five, so the comparison
    identity does not change when it does ("新冻结的计划五项齐全，缺一项拒绝冻结"). `five=False` is for what was frozen
    before the two dimensions existed, an old plan or an old K, which keeps the three it had and is refused nothing
    at startup. Named before anything is paid, and a freeze without them is refused rather than frozen short."""
    needed = ["max_steps", "max_tool_calls", "max_seconds"] + (list(V03_BUDGET_FIELDS) if five else [])
    missing = [k for k in needed if not getattr(budget, k)]
    if not missing:
        return []
    return [f"[bench.budget] needs {', '.join(missing)} per attempt"
            + (" (every new plan freezes all five hard limits)" if set(missing) & set(V03_BUDGET_FIELDS) else "")]


def _pinned_actor(activity: Activity):
    """The ModelIdentity the active release pins, or None for a v2 release."""
    rel = _ct.SystemRelease.from_json(activity.objects.get(activity.release_ref))
    for ref in activity.objects.load(rel.worker_ref, _ct.PolicySet).policies:
        p = activity.objects.load(ref, _ct.PolicyDescriptor)
        if p.target == "worker.actor":
            return activity.objects.load(p.implementation["model_identity_ref"], _ct.ModelIdentity)
    return None


def frontier_configs(cfg: dict) -> dict:
    """`[frontier.a]` / `[frontier.b]`: the leading models this experiment compares against, with where the
    choice came from. Two vendors is what a complete C2 needs; one is published as an incomplete C2 rather than
    filled in with a weaker model (docs/modules/07_BENCH_REPORT.md §1)."""
    out = {}
    for name, table in (cfg.get("frontier") or {}).items():
        unknown = sorted(set(table) - FRONTIER_KEYS)
        if unknown:
            raise ValueError(f"[frontier.{name}] does not take {unknown}; it takes {sorted(FRONTIER_KEYS)}")
        if "llm" not in table:
            raise ValueError(f"[frontier.{name}] needs an [frontier.{name}.llm] table")
        out[name] = dict(table)
    return out


def baseline(cfg: dict, base: str | Path = ".", *, out: Callable[[str], None] = print, limit: int | None = None,
             tasks=None) -> dict:
    """A reasonable base run and the frontier models, on development tasks, through the same driver.

    Nothing is weakened on purpose: each model gets the full task, the ordinary tool discovery and its own
    declared fallback, and a model that cannot be called is recorded as absent rather than replaced.
    """
    activity = open_activity(cfg, base, out=out)
    require(startup_checks(activity, cfg, phase="baseline"), phase="baseline")
    dev = list(tasks or activity.splits.ids("dev"))[:limit] if limit else list(tasks or activity.splits.ids("dev"))
    frontier = frontier_configs(cfg)
    out(f"baseline   {len(dev)} dev tasks; frontier configurations: {sorted(frontier) or 'none declared'}")
    if len(frontier) < 2:
        out("NOTE       fewer than two vendors are declared: C2 will be reported as incomplete, and no weaker "
            "model is substituted")
    report = {"tasks": dev, "arms": {}, "c2_complete": len(frontier) >= 2, "absent": [],
              "splits": activity.splits.hash(), "frontier": {k: {kk: vv for kk, vv in v.items() if kk != "llm"}
                                                             for k, v in frontier.items()}}
    arms = [("base", "base", cfg["llm"])] + [(name, f"frontier_{name}", table["llm"])
                                             for name, table in sorted(frontier.items())]
    pinned = _pinned_actor(activity)
    # A first-version baseline runs under the frozen task plan's conditions (repeats, per-attempt limits), so its
    # train run has the same identity as the first round's measurement and is not paid for twice.
    template_ref = activity.rsi.task_plan_ref if _harness_of(activity) and activity.rsi.task_plan_ref and \
        activity.objects.has(activity.rsi.task_plan_ref) else None
    for candidate, role, llm in arms:
        own = candidate in frontier
        provider = None
        if own or pinned is None:
            try:
                provider = config_mod.build_provider(llm)
            except Exception as e:  # noqa: BLE001 - an unavailable model is an absence, never a zero
                report["absent"].append({"arm": candidate, "reason": f"{type(e).__name__}: {e}"[:200]})
                out(f"ABSENT     {candidate}: {type(e).__name__}: {e}"[:160])
                continue
        arm_pricing = pricing_of({"pricing": frontier[candidate].get("pricing")}) if candidate in frontier \
            else pricing_of(cfg)
        work = improvement(activity, template_ref=template_ref, provider=provider, pricing=arm_pricing,
                           budget=activity_budget(activity, cfg, f"baseline:{candidate}"))
        # A frontier arm runs its own model on purpose: a different comparison, never the pinned actor swapped.
        work.actor_override = own
        order = [(t, i) for t in dev for i in range(work.repeats)]
        # Attempts already on record for this run are kept, not paid again: a restarted baseline (a cycle that
        # stopped half way through it) runs only what is missing.
        rows = work.run_arm(candidate, activity.release_ref, order, plan_ref="", candidate=candidate, role=role,
                            resume=True)
        summary = summarise(rows)
        # The label is what actually ran: the pinned actor, unless the arm names its own model.
        label = model_identity(provider, llm) if (own or pinned is None) else \
            {"provider": pinned.provider, "model": pinned.base_model, "actor_identity_ref": pinned.ref()}
        report["arms"][candidate] = {"role": role, "summary": summary, "model": label,
                                     "terminations": summary["terminations"],
                                     "failures": {r.task_id: r.failure_class for r in rows if not r.success}}
        if work.harness and not own:
            # The first version's "before" is train and dev (docs/ROADMAP.md §2 stage A): the train split runs
            # as its own run, never merged into the dev figures. It is the measurement the first worker round reads
            # (Improvement.train_measurement), so W0's train split is paid for once per activity.
            train = list(work.train_tasks or activity.splits.ids("train"))
            train_rows = work.train_measurement(activity.release_ref, train[:limit] if limit else train,
                                                first=True, planned=train)
            report["arms"][candidate]["train"] = summarise(train_rows)
            out(f"arm        {candidate} ({role}) train: {report['arms'][candidate]['train']['passed']}/"
                f"{report['arms'][candidate]['train']['tasks']} passed")
        out(f"arm        {candidate} ({role}): {summary['passed']}/{summary['tasks']} passed, "
            f"partial={summary['partial_credit']}, cost="
            f"{'unmeasured' if summary['cost_usd'] is None else summary['cost_usd']}")
    return report


# ---------------------------------------------------------------------------- collect
def collect(cfg: dict, base: str | Path = ".", *, out: Callable[[str], None] = print, repeats: int = 1,
            tasks=None) -> dict:
    """Run the installed version on training tasks and label the completion decisions offline.

    The audit that follows is the point of the phase: how many labels exist, how many independent groups they
    come from, and what was excluded. A phase that reported only the count would hide that twenty labels came
    from one task.
    """
    activity = open_activity(cfg, base, out=out)
    require(startup_checks(activity, cfg, phase="collect"), phase="collect")
    work = improvement(activity, provider=config_mod.build_provider(cfg["llm"]), pricing=pricing_of(cfg),
                       budget=activity_budget(activity, cfg, "collect"))
    evidence = work.collect(tasks=tasks, repeats=repeats)
    summary = evidence["feedback"].summary()
    rows = summarise(evidence["results"])
    out(f"collect    {rows['tasks']} tasks, {rows['attempts']} attempts, {rows['invalid']} invalid")
    out(f"feedback   {summary['valid']} valid labels over {summary['labels']}, {summary['unknown']} unknown, "
        f"{summary['conflicted']} conflicted; excluded {summary['excluded']}")
    built = _build_task_dataset(activity.rt, _business_stop_point(), evidence["feedback"],
                               out_dir=activity.root / "rsi" / "datasets" / "audit", objects=activity.objects,
                               splits=activity.splits, min_groups=1)
    out(f"data       {built.status}: rows={built.stats.get('rows')} groups={built.groups} "
        f"tasks={built.stats.get('tasks')}")
    for reason, count in sorted((built.exclusions or {}).items()):
        out(f"           excluded {reason}: {count}")
    return {"results": [r.to_json() for r in evidence["results"]], "feedback": summary,
            "dataset": {"status": built.status, "stats": built.stats, "groups": built.groups,
                        "exclusions": built.exclusions, "ref": built.dataset_ref}}


# ---------------------------------------------------------------------------- improve
def improve(cfg: dict, base: str | Path = ".", *, out: Callable[[str], None] = print, rounds: int | None = None,
            researcher=None, segment: str | None = None, stop_after_round: int | None = None) -> dict:
    """The worker loop with a fixed R: collect, diagnose, propose, build, evaluate, install, repeat.

    Every round starts from what the previous one left — the installed release, the appended experience and the
    results that produced it — which is what makes this a loop rather than a series of independent trials.

    With `segment` (the cycle's worker segments, docs/modules/07_BENCH_REPORT.md §1), the segment keeps its own
    record of completed rounds: calling it again resumes at the round it could not complete, a finished segment
    is not run again, and its collection batches are counted per segment across restarts.

    `stop_after_round` (a cycle's stop point, docs/ROADMAP.md §3) stops the segment once that many rounds plus one
    have completed, without finishing it: the next call continues with the following round, with patience and the
    failure limit where they were, as if it had not stopped.
    """
    from .research.loop import RoundContext, run_worker_rounds
    activity = open_activity(cfg, base, out=out)
    missing = activity.rsi.check(activity.objects, needs=("task_plan_ref",))
    if missing:
        raise ValueError("; ".join(missing) + "; freeze one with bench.freeze_task_plan")
    require(startup_checks(activity, cfg, phase="improve"), phase="improve")
    seglog = RoundLog(Path(activity.root) / "rsi" / "segments" / f"{segment}.json",
                      {"segment": segment, "scope": activity.rsi.scope_id,
                       "schedule": activity.rsi.schedule_ref}) if segment else None
    if seglog is not None and seglog.finished:
        # Before the budget root or a model is touched: a finished segment is read back, never re-run, and a
        # failure that belongs to a later step must not be recorded against it.
        out(f"segment    {segment} already finished ({seglog.state['stop_reason']}); not run again")
        return {"rounds": seglog.state["rounds"], "log": [], "budget": None, "segment": segment,
                "release": activity.releases.active()["release_ref"], "stop_reason": seglog.state["stop_reason"],
                "already_finished": True}
    provider = config_mod.build_provider(cfg["llm"])
    trainer = config_mod.build_trainer(cfg, activity.rt.serving) if cfg.get("trainer") else None
    work = improvement(activity, template_ref=activity.rsi.task_plan_ref, provider=provider, trainer=trainer)
    schedule = Schedule(**(activity.objects.get(activity.rsi.schedule_ref) if activity.rsi.schedule_ref
                           else {"worker_rounds": rounds or 1}))
    budget = activity_budget(activity, cfg, "improve")
    work.budget = budget
    work.pricing = pricing_of(cfg)
    researcher = researcher or _default_researcher(cfg, activity, budget=budget)
    # The model that writes the notes is part of each note's identity (docs/modules/04_RESEARCH_LOOP.md §1).
    research_table = dict(cfg.get("research") or {}).get("llm") or cfg.get("llm") or {}
    research_model = {k: v for k, v in research_table.items() if not SECRET_KEY.match(k)}
    log: list = []
    if seglog is not None:
        seglog.watch(researcher)
    done = 0
    if segment:
        work.segment = segment

    measured: dict = {}
    if work.harness:
        previous = work.last_measurement(activity.releases.active()["release_ref"])
        if previous is not None:
            measured["last"] = previous

    def make_round(index: int) -> RoundContext:
        index += done
        release_ref = activity.releases.active()["release_ref"]
        if work.harness:
            evidence = work.measure(release_ref, measured.get("last"))
            measured["last"] = evidence
        else:
            evidence = work.collect()
        experience, experience_ref = _experience.snapshot(activity.root)
        context = work.context(goal="improve the full-task pass rate on the development tasks",
                               release_ref=release_ref, evidence=evidence, experience=experience,
                               experience_ref=experience_ref)
        bound = _P.bind_release(release_ref, profile="task", services=work.services(),
                                authorization="installed")
        diagnosis, drafts = work.round_inputs(index, evidence, experience, bound, root=activity.root,
                                              research_model=research_model)
        log.append({"round": index, "release": release_ref, "feedback": evidence["feedback"].summary()})
        return RoundContext(releases=activity.releases, objects=activity.objects, scope_id=activity.rsi.scope_id,
                            context=context, researcher=researcher, bound=bound, core_ref=work.core_ref,
                            eval_template_ref=work.template_ref, handlers=work.handlers(),
                            experience_ref=experience_ref, experience=tuple(experience), owner="worker",
                            schedule=schedule,
                            history=frozen_signatures(experience, activity.objects, parent=release_ref),
                            root=activity.root,
                            diagnosis=diagnosis, drafts=drafts, round_index=index)

    from .research.loop import resume_unfinished
    # An open experiment finishes under the template and the scope it was frozen with, or the segment pauses.
    resumed = resume_unfinished(activity.root, activity.objects, work.handlers(), budget,
                                eval_template_ref=work.template_ref, budget_scope=work.scope_id)
    # a resume that paused (a refused plan, a halt) or ended (a spent budget) runs no new round behind it
    halted = bool(resumed) and resumed[-1].outcome == "stopped"
    for result in resumed:
        out(f"resume     {result.experiment_id}: {result.outcome} ({result.reason})")
        if seglog is not None:
            seglog.add(result)             # the interrupted round of this segment, finished
    if seglog is not None:
        # Only the rounds this segment has not completed; make_round numbers them after the completed ones.
        done = seglog.done()
        schedule = Schedule(**{**asdict(schedule), "worker_rounds": max(schedule.worker_rounds - done, 0)})
    # The stop point counts the rounds this call may run, after the ones the segment has completed.
    stop_after = max(stop_after_round + 1 - done, 0) if stop_after_round is not None else None
    collector = None
    if work.harness:
        def collector(result, *, batch: int) -> dict:
            return work.collect_batch(measured["last"], batch=batch, root=activity.root)
    collected = _collection_batches(activity.root, activity.rsi.scope_id, segment, any_segment=segment is None) \
        if work.harness else 0
    results = resumed + ([] if halted else run_worker_rounds(
        make_round, budget, schedule=schedule, root=activity.root, collect=collector, collected_before=collected,
        on_result=seglog.add if seglog is not None else None,
        history=tuple(seglog.state["rounds"]) if seglog is not None else (), stop_after=stop_after,
        # a batch booked before a crash is finished first, at its own attempt index, before any round runs
        resume_collections=(lambda: work.finish_collections(activity.root, measured.get("last")))
        if work.harness else None))
    stop_reason = next((r.reason for r in reversed(results) if r.outcome == "stopped"), "rounds_done")
    if stop_reason == "stopped_at":
        pass                                # the stop point, not the segment's end: the segment is not finished
    elif seglog is not None:
        seglog.end(stop_reason)
        stop_reason = seglog.state["stop_reason"]
    for result in results:
        out(f"round      {result.outcome}"
            + (f" experiment={result.experiment_id}" if result.experiment_id else "")
            + (f" release {_ct.short(result.release_before)} -> {_ct.short(result.release_after)}"
               if result.release_after and result.release_after != result.release_before else "")
            + (f" ({result.reason})" if result.reason else ""))
    return {"rounds": [vars(r) for r in results], "log": log, "budget": budget.remaining(),
            "release": activity.releases.active()["release_ref"], "stop_reason": stop_reason,
            **({"segment": segment} if segment else {})}


def _default_researcher(cfg: dict, activity: Activity, *, budget=None):
    """The research step on the configured model. Its calls are reserved and settled on the same book as the
    agent's: research is part of what an improvement costs, not overhead outside it."""
    from .evolve.execution import MeteredProvider
    from .research.basic import PolicyResearcher
    if "llm" not in cfg:
        raise ValueError("a researcher needs an [llm] table, or pass one in")
    table = dict(cfg.get("research") or {}).get("llm") or cfg["llm"]
    provider = config_mod.build_provider(table)
    if budget is not None:
        provider = MeteredProvider(provider, budget, pricing_of(cfg), operation=f"{budget.scope_id}:research",
                                   max_retries=0)
    return PolicyResearcher(_Completions(provider))


class _Completions:
    """A chat provider as the plain completion interface a research step uses."""

    def __init__(self, provider):
        self.provider = provider
        self.name = getattr(provider, "name", "llm")

    def complete(self, system: str, prompt: str) -> str:
        from .providers import ReplyRefused, ReplyTruncated
        turn = self.provider.chat(system, [{"role": "user", "content": prompt}], ())
        if turn.stop == "refusal":
            # Declined, not answered: the research step reads it as an unreadable reply (M4 §1). The call is
            # already settled on the book, so raising here changes nothing that was paid.
            raise ReplyRefused("the research model declined the request" + (f": {turn.text[:200]}" if turn.text
                                                                              else ""))
        if turn.stop == "max_tokens":
            # A cut reply read as a whole one turns half an answer into a decision (the gate run's summary was cut
            # before its proposal and read as "collect"): it is refused instead.
            used = (turn.usage or {}).get("completion_tokens") or (turn.usage or {}).get("output_tokens")
            raise ReplyTruncated("the research reply stopped at the model's output limit"
                                 + (f" ({used} tokens)" if used else "")
                                 + ": raise max_tokens for the research model ([research.llm] or [llm]) and "
                                   "[pricing] max_output_tokens with it")
        return turn.text or ""

    def label(self, step: str):
        """While a research step runs, a metered provider books its calls as `<operation>:<step>`."""
        import contextlib
        provider = self.provider

        @contextlib.contextmanager
        def labelled():
            base = getattr(provider, "operation", None)
            if base is None:
                yield
                return
            provider.operation = f"{base}:{step}"
            try:
                yield
            finally:
                provider.operation = base
        return labelled()


# ---------------------------------------------------------------------------- research (M6)
def research(cfg: dict, base: str | Path = ".", *, out: Callable[[str], None] = print, researcher=None,
             r_candidate_ref: str | None = None, campaign_ref: str | None = None) -> dict:
    """Compare two research policies by the task policies they produce, then install the winner's R.

    Each arm runs the ordinary worker loop in its own workspace, with research slots disabled, from the same W0
    and the same starting experience under an equal fixed budget. The arms cannot read each other or the outer
    tasks; the evaluator runs both sealed W* on the outer pool.
    """
    from .research.campaign import CampaignPair, CampaignPlan, run_campaign
    activity = open_activity(cfg, base, out=out)
    ref = campaign_ref or activity.rsi.research_plan_ref
    if not ref or not activity.objects.has(ref):
        raise ValueError("[rsi] research_plan_ref must name a frozen CampaignPlan (research.campaign)")
    plan = CampaignPlan.from_json(activity.objects.get(ref))
    if r_candidate_ref and r_candidate_ref != plan.r_candidate_ref:
        raise ValueError("the candidate R is part of the frozen plan and cannot be replaced on the command line")
    if researcher is not None and not callable(researcher):
        raise ValueError("research() takes a factory of research steps: each arm gets its own, metered on its own "
                         "budget, so one arm's requests and spend are never the other's")
    # A campaign that already has its receipt is not run again: its arms, its outer comparison and its install
    # are on record, and running them again would pay twice and draw on research_dev twice (M6 §3, M7 §1).
    result_path = Path(activity.root) / "campaigns" / plan.id / "result.json"
    if result_path.exists():
        recorded = json.loads(result_path.read_text())
        out(f"campaign   {plan.id} already has its receipt ({recorded['verdict']}); not run again")
        if recorded["installation"] is None:
            # accepted, and a crash came before the install: install now, from the recorded receipt
            recorded["installation"] = _install_research(activity, plan, recorded, out)
            write_json(result_path, recorded)
        return {**recorded, "release": activity.releases.active()["release_ref"]}
    require(startup_checks(activity, cfg, phase="research"), phase="research")
    provider = config_mod.build_provider(cfg["llm"])
    budget = activity_budget(activity, cfg, f"campaign:{plan.id}")
    out(f"campaign   {plan.id} phase={plan.phase} pairs={len(plan.campaign_pairs)} "
        f"discovery={plan.discovery} R0={_ct.short(plan.r_control_ref)} R1={_ct.short(plan.r_candidate_ref)}")

    def make_researcher(arm_budget):
        """A fresh research step per arm, metered on that arm's budget: its recorded requests are then that arm's
        own, which is how a fixture tells R0's calls from R1's (docs/modules/06_RESEARCH_CAMPAIGN.md §5)."""
        return researcher() if callable(researcher) else _default_researcher(cfg, activity, budget=arm_budget)

    pricing = pricing_of(cfg)

    def evaluate_outer(pair: CampaignPair, sealed: dict) -> dict:
        # The outer runs are the evaluator's, and cost the same for both arms: a child scope of the campaign, so
        # they are counted in the comparison's total and reported apart from either arm (docs/modules/06 §3).
        return _evaluate_outer(activity, plan, pair, sealed, provider=provider, out=out, pricing=pricing,
                               budget=budget.child(f"{plan.id}:{pair.pair_id}:outer",
                                                   dict(plan.outer_budget or {})))

    def run_arm(pair: CampaignPair, arm: str, r_ref: str, arm_budget, shared=None) -> object:
        return _run_campaign_arm(activity, plan, pair, arm, r_ref, arm_budget, provider=provider,
                                 researcher=make_researcher(arm_budget), out=out, pricing=pricing, shared=shared)

    research_table = dict(cfg.get("research") or {}).get("llm") or cfg.get("llm") or {}
    research_model = {k: v for k, v in research_table.items() if not SECRET_KEY.match(k)}

    def prepare_pair(pair: CampaignPair, common_budget) -> dict | None:
        if not _harness_of(activity):
            return None
        return _campaign_common(activity, plan, pair, common_budget, provider=provider, pricing=pricing,
                                researcher=make_researcher(common_budget), research_model=research_model, out=out)

    prompts = {"control": _P.prompts_of(activity.objects, plan.r_control_ref),
               "candidate": _P.prompts_of(activity.objects, plan.r_candidate_ref)}
    by_target = {arm: {activity.objects.load(r, _ct.PolicyDescriptor).target: r
                       for r in activity.objects.load(ref, _ct.PolicySet).policies}
                 for arm, ref in (("control", plan.r_control_ref), ("candidate", plan.r_candidate_ref))}
    changed = tuple(sorted(t for t in set(by_target["control"]) | set(by_target["candidate"])
                           if by_target["control"].get(t) != by_target["candidate"].get(t)))
    receipt = run_campaign(plan, run_arm=run_arm, evaluate_outer=evaluate_outer, objects=activity.objects,
                           budget=budget, events=_ct.Events(activity.root), prepare_pair=prepare_pair,
                           prompts=prompts, changed=changed)
    receipt_ref = activity.objects.put(receipt)
    activity.releases.register_receipt(receipt_ref, plan=receipt.plan_ref, subject=plan.r_candidate_ref,
                                      verdict=receipt.verdict, profile="research")
    out(f"verdict    {receipt.verdict}: {'; '.join(receipt.reasons)[:200]}")
    out(f"metrics    {receipt.metrics}")
    paused = any("paused" in r for r in receipt.reasons) and receipt.verdict == "inconclusive"
    recorded = {"receipt": receipt_ref, "verdict": receipt.verdict, "metrics": receipt.metrics,
                "uncertainty": receipt.uncertainty, "costs": receipt.costs, "installation": None}
    if not paused:
        write_json(result_path, recorded)      # before the install: a crash there does not re-run the campaign
    recorded["installation"] = _install_research(activity, plan, recorded, out)
    if not paused:
        write_json(result_path, recorded)
    return {**recorded, "release": activity.releases.active()["release_ref"]}


def _install_research(activity: Activity, plan, recorded: dict, out) -> dict | None:
    """Install R1 from an accepted research receipt, once: None when the verdict does not install (or the plan is
    the final one), and the recorded installation when R1 is already the active research policy."""
    if recorded["verdict"] != "accepted" or plan.phase == "final":
        return None
    active = activity.releases.active()["release_ref"]
    if _ct.SystemRelease.from_json(activity.objects.get(active)).research_ref == plan.r_candidate_ref:
        out("install    R1 is already the active research policy")
        return {"status": "installed", "target_release": active, "reason": "already active"}
    receipt_ref = recorded["receipt"]
    installation = activity.releases.install(
        receipt_ref, active, f"install-r-{_ct.digest({'receipt': receipt_ref})[:12]}",
        preflight=lambda target: {"release": target,
                                  "policies": _P.describe(_P.bind_release(
                                      target, profile="preflight",
                                      services=_P.Services(activity.objects, serving=activity.rt.serving)))})
    out(f"install    {installation.status} {_ct.short(installation.target_release)}"
        + (f" ({installation.reason})" if installation.reason else ""))
    return {k: v for k, v in vars(installation).items() if isinstance(v, (str, int, float, bool, type(None)))}


def _run_campaign_arm(activity: Activity, plan, pair, arm: str, r_ref: str, arm_budget, *, provider,
                      researcher, out=print, pricing=None, shared=None):
    """One arm's whole improvement loop, in its own workspace.

    The arm gets the pair's W0 and E0, its own R, the shared core and nothing else: no mainline pointer, no other
    arm's experience, no outer task. Its W* is whatever it actually installed (RC-02: an arm that installed nothing
    still returns W0 and its full costs).

    An arm keeps its own record of the rounds it completed. A finished arm is not run again; a paused one
    (docs/modules/06_RESEARCH_CAMPAIGN.md §3) resumes at the round it could not complete, so after the repair it
    reaches the frozen round count and no more.
    """
    from .research.campaign import ArmOutcome, arm_workspace
    from .research.loop import RoundContext, resume_unfinished, run_worker_rounds
    root = arm_workspace(activity.root, pair.pair_id, arm, plan.id)
    log = RoundLog(Path(root) / "rsi" / "arm.json", {"plan": plan.id, "pair": pair.pair_id, "arm": arm, "r": r_ref})
    objects = _ct.Objects(root)
    core_ref = _ct.SystemRelease.from_json(activity.objects.get(activity.release_ref)).core_ref
    _ct.copy_closure(activity.objects, objects, [pair.initial_w_ref, r_ref, core_ref])
    releases = Releases(root, objects)
    rt = config_mod.build_runtime({"workspace": {"root": str(root)}, "backends": {"mock": {"type": "mock"}}}, ".")
    config_mod.register_builtin_points(rt)
    start = releases.bootstrap(pair.initial_w_ref, r_ref, core_ref,
                               reason=f"campaign {plan.id} {pair.pair_id} arm {arm}")
    inner = Improvement(rt=rt, suite=activity.suite, splits=activity.splits, objects=objects, releases=releases,
                        provider=provider, core_ref=core_ref, gate=_gate_of(activity, plan),
                        dev_tasks=tuple(_tasks_of(activity, pair.inner_dev_groups)),
                        scope_id=f"{plan.id}:{pair.pair_id}:{arm}", repeats=pair.repeats,
                        order_seed=pair.seed_schedule[0] if pair.seed_schedule else 0,
                        budget=arm_budget, serving=rt.serving, results=Results(root, activity.bench.domain),
                        pricing=pricing, concurrency=activity.bench.concurrency,
                        stop_view=activity.rsi.stop_view, attempt_budget=_attempt_budget(activity),
                        probe_train=True, train_tasks=tuple(_tasks_of(activity, pair.inner_train_groups)),
                        harness=_harness_of(activity), worker=isolated_worker(_harness_of(activity)))
    # The arm's own template, frozen from what its gate reads — the inner dev families, the campaign's gate, its
    # repeats and seed — so its experiments name the plan they are compared under, not the activity's dev plan.
    inner.template_ref = freeze_task_plan(
        objects, core_ref=core_ref, split_ref=activity.splits.hash(), task_ids=inner.dev_tasks, gate=inner.gate,
        repeats=inner.repeats, order_seed=inner.order_seed,
        per_attempt_budget=inner.attempt_budget.limits())
    step = researcher
    outcome = ArmOutcome(arm=arm, pair_id=pair.pair_id)
    if log.finished:
        return _arm_outcome(outcome, log.state, objects, releases, start, activity, pair, arm_budget, out)
    _seed_experience(root, _starting_experience(activity, pair))      # before a resumed experiment appends to it
    log.watch(step)
    # An experiment a crash left mid-way is finished first, from what it recorded, and counts as its round.
    resumed = resume_unfinished(root, objects, inner.handlers(), arm_budget, eval_template_ref=inner.template_ref,
                                budget_scope=inner.scope_id)
    for result in resumed:
        log.add(result)
    if resumed and resumed[-1].outcome == "stopped":
        log.end(resumed[-1].reason)            # a pause resumes after the repair; a spent budget ends the arm
        return _arm_outcome(outcome, log.state, objects, releases, start, activity, pair, arm_budget, out)
    # Rounds that ended on infrastructure are rerun, not counted: the arms are compared at equal completed rounds.
    done = log.done()
    schedule = Schedule(worker_rounds=max(plan.worker_round_limit - done, 0), research_slots=0,
                        patience=plan.worker_round_limit)
    measured: dict = {}
    if inner.harness and shared:
        # Both arms start from the one W0 measurement (docs/modules/06_RESEARCH_CAMPAIGN.md §2): its grading and
        # transcripts are copied in, so the arm reads only its own workspace, and it stands for the arm's start
        # release because that release runs the same W0. A restarted arm continues from its own measurements.
        _copy_measurement(shared["measurement"], shared["results"], shared["transcripts"], inner.results,
                          Path(rt.root) / "transcripts")
        own = inner.last_measurement(releases.active()["release_ref"])
        measured["last"] = own if own is not None else {**shared["measurement"], "release": start}
    research_model = (shared or {}).get("research_model", {})

    def make_round(index: int) -> RoundContext:
        index += done
        release_ref = releases.active()["release_ref"]
        if inner.harness:
            evidence = inner.measure(release_ref, measured.get("last"))
            measured["last"] = evidence
        else:
            evidence = inner.collect()
        experience, experience_ref = _experience.snapshot(root)
        context = inner.context(goal="improve the full-task pass rate on this campaign's development tasks",
                               release_ref=release_ref, evidence=evidence, experience=experience,
                               experience_ref=experience_ref)
        bound = _P.bind_release(release_ref, profile="task", services=inner.services(),
                                authorization="installed")
        diagnosis, drafts = inner.round_inputs(
            index, evidence, experience, bound, root=root, research_model=research_model,
            shared_notes=shared.get("notes") if shared and index == 0 else None)
        return RoundContext(releases=releases, objects=objects, scope_id=inner.scope_id, context=context,
                            researcher=step, bound=bound, core_ref=core_ref,
                            eval_template_ref=inner.template_ref, handlers=inner.handlers(),
                            experience_ref=experience_ref, experience=tuple(experience), owner="worker",
                            schedule=schedule, history=frozen_signatures(experience, objects, parent=release_ref),
                            root=root,
                            diagnosis=diagnosis, drafts=drafts, round_index=index)

    collector = None
    if inner.harness:
        def collector(result, *, batch: int) -> dict:
            return inner.collect_batch(measured["last"], batch=batch, root=root)
    collected = _collection_batches(root, inner.scope_id, None, any_segment=True) if inner.harness else 0
    rounds = run_worker_rounds(make_round, arm_budget, schedule=schedule, root=root, collect=collector,
                               collected_before=collected, on_result=log.add, history=tuple(log.state["rounds"]),
                               resume_collections=(lambda: inner.finish_collections(root, measured.get("last")))
                               if inner.harness else None)
    log.end(next((r.reason for r in reversed(rounds) if r.outcome == "stopped"), "rounds_done"))
    return _arm_outcome(outcome, log.state, objects, releases, start, activity, pair, arm_budget, out)


class RoundLog:
    """A run of rounds that has to survive a restart — a campaign arm, a cycle's worker segment.

    Every round result is written as it happens, so a crash in round n leaves rounds 1..n-1 on record and a restart
    runs only what is missing. The log belongs to one identity (a campaign plan's arm, a segment of one schedule)
    and refuses state written for another. Completed rounds exclude markers and rounds lost to infrastructure,
    which are run again; a run whose stop is a pause (docs/modules/06_RESEARCH_CAMPAIGN.md §3) is not finished.
    """

    def __init__(self, path: Path, identity: dict):
        self.path = Path(path)
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "identity": identity, "rounds": [], "requests": []}
        if self.state.get("identity") != identity:
            raise ValueError(f"{self.path} records {self.state.get('identity')}, not {identity}: another run's "
                             f"rounds are not this one's")
        self.state.pop("finished", None) if not self.state.get("stop_reason") else None
        self._base = list(self.state["requests"])
        self._step = None

    @property
    def finished(self) -> bool:
        return bool(self.state.get("finished"))

    def watch(self, step) -> None:
        """Keep the research step's recorded requests with the rounds that made them."""
        self._step = step

    def done(self) -> int:
        from .research.loop import completed_round
        return sum(1 for r in self.state["rounds"] if completed_round(r))

    def add(self, result) -> None:
        self.state["rounds"].append({"outcome": result.outcome, "experiment": result.experiment_id,
                                     "reason": result.reason, "release_after": result.release_after,
                                     "next_action": result.next_action})
        self.state["finished"] = False
        self._save()

    def end(self, stop_reason: str) -> None:
        from .research.loop import pause_of
        pause = pause_of(self.state["rounds"], stop_reason)
        self.state["stop_reason"] = pause or stop_reason
        self.state["finished"] = not pause
        self._save()

    def _save(self) -> None:
        if self._step is not None:
            self.state["requests"] = [*self._base, *(dict(r) for r in getattr(self._step, "requests", ()) or ())]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, self.state)


def _arm_outcome(outcome, state: dict, objects, releases, start: str, activity: Activity, pair, arm_budget, out):
    outcome.rounds = tuple(state["rounds"])
    active = releases.active()["release_ref"]
    installed = [r["release_after"] for r in state["rounds"] if r["outcome"] == "installed" and r["release_after"]]
    if active != start and active not in installed:
        installed.append(active)          # installed by an experiment whose round a crash never recorded
    outcome.installed = tuple(installed)
    outcome.stop_reason = state.get("stop_reason", "")
    outcome.costs = arm_budget.remaining()
    outcome.research_requests = tuple(state["requests"])
    # W* is what the arm's own pointer holds: its last install, or W0 when it installed nothing.
    outcome.w_star = _ct.SystemRelease.from_json(objects.get(active)).worker_ref if active != start \
        else pair.initial_w_ref
    _ct.copy_closure(objects, activity.objects, [outcome.w_star])
    out(f"arm        {pair.pair_id}/{outcome.arm}: {len(outcome.installed)} installed, "
        f"W*={_ct.short(outcome.w_star)}, stop={outcome.stop_reason}"
        + (" (paused: resumes on the next run)" if outcome.paused else ""))
    return outcome


def _campaign_common(activity: Activity, plan, pair, budget, *, provider, pricing, researcher, research_model,
                     out=print) -> dict:
    """What both arms of a pair start from, made once in the pair's common workspace and charged to the
    campaign's common scope (docs/modules/06_RESEARCH_CAMPAIGN.md §2).

    W0's train measurement is always common: the arms run the same W0 on the same train tasks, and R takes no
    part in a task run. The first round's note selection is the same rule on the same failures in both arms.
    When every selected failure's note request is identical under R0 and R1 — the full note identity of
    docs/modules/04_RESEARCH_LOOP.md §1 — the notes are written once here and each arm reads them as a shared
    input; otherwise each arm writes its own on the same selection under the same cap, and nothing is made here
    that one arm would get for free.
    """
    from .research import diagnosis as dg
    from .research.campaign import arm_workspace
    from .research.experiment import note_contract
    root = arm_workspace(activity.root, pair.pair_id, "common", plan.id)
    objects = _ct.Objects(root)
    core_ref = _ct.SystemRelease.from_json(activity.objects.get(activity.release_ref)).core_ref
    _ct.copy_closure(activity.objects, objects, [pair.initial_w_ref, plan.r_control_ref, plan.r_candidate_ref,
                                                 core_ref])
    releases = Releases(root, objects)
    rt = config_mod.build_runtime({"workspace": {"root": str(root)}, "backends": {"mock": {"type": "mock"}}}, ".")
    config_mod.register_builtin_points(rt)
    start = releases.bootstrap(pair.initial_w_ref, plan.r_control_ref, core_ref,
                               reason=f"campaign {plan.id} {pair.pair_id} common inputs")
    common = Improvement(rt=rt, suite=activity.suite, splits=activity.splits, objects=objects, releases=releases,
                         provider=provider, core_ref=core_ref, gate=_gate_of(activity, plan),
                         dev_tasks=tuple(_tasks_of(activity, pair.inner_dev_groups)),
                         scope_id=f"{plan.id}:{pair.pair_id}:common", budget=budget, serving=rt.serving,
                         results=Results(root, activity.bench.domain), pricing=pricing,
                         concurrency=activity.bench.concurrency,
                         stop_view=activity.rsi.stop_view, attempt_budget=_attempt_budget(activity),
                         train_tasks=tuple(_tasks_of(activity, pair.inner_train_groups)),
                         harness=_harness_of(activity), worker=isolated_worker(_harness_of(activity)))
    measurement = common.measure(start, None)
    shared: dict = {"measurement": measurement, "results": common.results, "transcripts": Path(rt.root) / "transcripts",
                    "research_model": research_model, "notes": None}
    bound = _P.bind_release(start, profile="task", services=common.services(), authorization="installed")
    diagnosis, _ = common.round_inputs(0, measurement, (), bound, root=root, research_model=research_model)
    selected, _rest = dg.select_failures(diagnosis.summaries, order=diagnosis.order, last_included={},
                                         cap=diagnosis.limits.max_notes)
    prompts = {arm: _P.prompts_of(objects, r_ref).get("research.diagnose")
               for arm, r_ref in (("control", plan.r_control_ref), ("candidate", plan.r_candidate_ref))}
    same = all(p is not None for p in prompts.values()) and all(
        len({dg.note_identity(*researcher.note_request(s, prompts[arm], allowed=tuple(diagnosis.components)),
                              research_model) for arm in prompts}) == 1 for s in selected)
    if same and selected and note_contract(bound):
        diagnosis.run(researcher, prompts["control"])
        shared["notes"] = diagnosis.cache
    _ct.Events(activity.root).append("campaign_common", pair=pair.pair_id, release=start,
                                   measured=len(measurement["train"]), failed=len(measurement["failed"]),
                                   selected=[s["task_id"] for s in selected], notes_shared=shared["notes"] is not None)
    out(f"common     {pair.pair_id}: W0 measured on {len(measurement['train'])} train tasks, "
        f"{len(measurement['failed'])} failed; first-round notes "
        + ("written once for both arms" if shared["notes"] is not None else "written by each arm"))
    return shared


def _starting_experience(activity: Activity, pair) -> list[dict]:
    """The pair's E0, as the plan froze it (docs/modules/06_RESEARCH_CAMPAIGN.md §2)."""
    return list(activity.objects.get(pair.initial_experience_ref)["experience"]) if pair.initial_experience_ref \
        else []


def _seed_experience(root, entries: list[dict]) -> None:
    """E0 as the first lines of an arm's experience log, written once and exactly as frozen, so both arms read the
    same rows under the same snapshot ref. The objects E0 refers to are not copied: nothing in an arm reads them.
    A restarted arm finds its log already begun; a log that begins otherwise was begun from another E0 and is
    refused rather than mixed."""
    path = Path(root) / "experience.jsonl"
    if path.exists():
        if _experience.read(root)[:len(entries)] != entries:
            raise ValueError(f"{path} does not begin with this pair's starting experience: the arm ran from "
                             f"another E0")
    elif entries:
        _ct.durable_write(path, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries).encode())


def _copy_measurement(measurement: dict, source: Results, transcripts: Path, target: Results,
                      target_transcripts: Path) -> None:
    """A common measurement's grading rows and transcripts, copied into one branch's workspace as recorded."""
    import shutil
    rows = measurement.get("train") or ()
    target.copy_grading(source, [r.attempt_id for r in rows])
    target_transcripts.mkdir(parents=True, exist_ok=True)
    for r in rows:
        src = Path(transcripts) / f"{r.evidence}.jsonl"
        dst = target_transcripts / src.name
        if r.evidence and src.exists() and not dst.exists():
            shutil.copyfile(src, dst)


def _evaluate_outer(activity: Activity, plan, pair, sealed: dict, *, provider, out=print, pricing=None,
                    budget=None) -> dict:
    """The evaluator's own run: both sealed W* on the same outer tasks, with the same repeats and order.

    An attempt lost to infrastructure is run once more under the outer plan's frozen retry policy, as the worker
    gate does: the original stays on record and the refill names it (supersedes); a task failure, a candidate
    defect, an evaluator failure or a spent budget is never re-run. Run again, it reuses every attempt on record,
    refills included."""
    from .evolve.task_gate import refill
    parent = activity.release_ref
    core_ref = _ct.SystemRelease.from_json(activity.objects.get(parent)).core_ref
    tasks = _tasks_of(activity, pair.outer_groups)
    order = [(t, i) for t in tasks for i in range(pair.repeats)]
    work = improvement(activity, provider=provider, scope_id=f"{plan.id}:{pair.pair_id}:outer", installs=False,
                       pricing=pricing, budget=budget)
    work.dev_tasks = tuple(tasks)
    rows = {}
    for arm, w_star in sealed.items():
        outer_plan = work.plan(w_star, w_star, parent)
        plan_ref = activity.objects.put(outer_plan)
        permit = activity.releases.permit(w_star, parent, plan_ref, f"{plan.id}:{pair.pair_id}:outer", core_ref)
        release = activity.releases.candidate_release(parent, w_star, permit)

        def run(name, order_, retry=0, supersedes=None, _arm=arm, _release=release, _plan_ref=plan_ref,
                _permit=permit):
            return work.run_arm(name, _release, order_, plan_ref=_plan_ref, split=_split_of(activity, tasks),
                                candidate=f"{_arm}_w_star", role="evolved", permit=_permit,
                                scope=f"{plan.id}:{pair.pair_id}:outer", retry_index=retry, resume=True,
                                supersedes=supersedes)
        rows[arm] = refill(outer_plan, f"outer_{arm}", run(f"outer_{arm}", order), run)
        out(f"outer      {pair.pair_id}/{arm}: {summarise(rows[arm])['passed']}/"
            f"{summarise(rows[arm])['tasks']} passed")
    return rows


def _gate_of(activity: Activity, plan) -> GatePlan:
    if plan.worker_gate_plan_ref and activity.objects.has(plan.worker_gate_plan_ref):
        return GatePlan.from_json(activity.objects.get(plan.worker_gate_plan_ref))
    return GatePlan(min_groups=1, min_gain=0.01)


def _tasks_of(activity: Activity, families) -> list[str]:
    wanted = set(families)
    return [e.task_id for e in activity.splits.entries if e.family_id in wanted]


def _split_of(activity: Activity, tasks) -> str:
    splits = {activity.splits.split_of(t) for t in tasks}
    if len(splits) != 1:
        raise ValueError(f"these tasks span several splits ({sorted(splits)}): one run reads one split")
    return splits.pop()



# ---------------------------------------------------------------------------- finalization (evaluate)
CLAIMS = ("C1", "C2", "C3", "C4")


@_dataclass
class FinalizationPlan(_ct.Record):
    """The final comparison, frozen before the final tasks are touched (docs/modules/07_BENCH_REPORT.md §3)."""
    TYPE = "finalization"
    experiment_id: str = ""
    core_ref: str = ""
    split_ref: str = ""
    claims: tuple[str, ...] = ("C1", "C2")
    comparisons: tuple = ()                   # {"claim", "subject", "control", "release_ref"?, "llm"?}
    release_refs: dict = _field(default_factory=dict)
    manifests: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
    repeats: int = 1
    statistical_plan: dict = _field(default_factory=dict)
    budget_ref: str | None = None
    research_campaign_plan_ref: str | None = None
    stop_rules: dict = _field(default_factory=dict)
    schema_version: int = _ct.SCHEMA_VERSION

    def __post_init__(self) -> None:
        unknown = sorted(set(self.claims) - set(CLAIMS))
        if unknown:
            raise _ct.ContractError(f"unknown claims {unknown}: {list(CLAIMS)}")
        if not self.task_ids:
            raise _ct.ContractError("a finalization plan names the final tasks before it is sealed")
        if "C1" in self.claims and not any(c.get("claim") == "C1" for c in self.comparisons):
            raise _ct.ContractError("C1 needs an evolved/base comparison in `comparisons`")


def seal(plan: FinalizationPlan, root, objects: _ct.Objects) -> dict:
    """Persist the plan and mark it sealed before anything reads a final task.

    Sealing first is what makes the final result a report instead of a choice: after this the version list, the
    task list and the stop rules cannot change without starting a new activity and marking this pool used.
    """
    ref = objects.put(plan)
    path = Path(root) / "rsi" / "finalization" / f"{plan.experiment_id}.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if state and state.get("plan") != ref:
        raise ValueError(f"{path} already seals another plan ({state.get('plan')}): a changed final plan is a "
                         f"new activity, and this pool counts as used")
    write_json(path, {"plan": ref, "status": state.get("status", "sealed"), "attempts": state.get("attempts", []),
                      "sealed": state.get("sealed", now())})
    return {"plan": ref, "path": path}


def evaluate(cfg: dict, base: str | Path = ".", *, out: Callable[[str], None] = print, plan_ref: str | None = None,
             resume: bool = True) -> dict:
    """Run the sealed final plan. It reports; it does not promote, tune or choose.

    A comparison that turns out badly stays in the report: the plan was fixed before these tasks were read, so
    dropping an arm now would make the remaining numbers a selection rather than a measurement (BR-06).

    Run again after an interruption, it runs only what is missing (BR-05): each attempt id is determined by the
    plan, the arm, the task, the repeat and the retry, so the attempts already on record are found and kept as
    they were, with their cost. Attempts that failed for infrastructure reasons are refilled under the plan's
    `stop_rules.max_infrastructure_retries`; a legitimate failure is never re-rolled.
    """
    from .evolve.task_gate import refill_attempts
    activity = open_activity(cfg, base, out=out, bootstrap=False)
    ref = plan_ref or activity.rsi.final_plan_ref
    if not ref or not activity.objects.has(ref):
        raise ValueError("[rsi] final_plan_ref must name a sealed FinalizationPlan (bench.seal)")
    plan = FinalizationPlan.from_json(activity.objects.get(ref))
    require(startup_checks(activity, cfg, phase="evaluate"), phase="evaluate")
    state_path = Path(activity.root) / "rsi" / "finalization" / f"{plan.experiment_id}.json"
    if not state_path.exists():
        seal(plan, activity.root, activity.objects)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state["status"] == "completed" and not resume:
        raise ValueError(f"{plan.experiment_id} is completed: a new comparison is a new activity")
    done = {a["attempt_id"] for a in state["attempts"]}
    out(f"final      {plan.experiment_id} claims={list(plan.claims)} tasks={len(plan.task_ids)} "
        f"repeats={plan.repeats} status={state['status']}")
    write_json(state_path, {**state, "status": "running"})
    rows: dict = {}
    budget = activity_budget(activity, cfg, f"final:{plan.experiment_id}")
    # The final spends the share the frozen budget set aside for it, when there is one, and nothing else.
    rsi_cfg = RsiConfig.load(cfg)
    final_share = "final" if "final" in budget_allocations(activity.objects.get(rsi_cfg.budget_ref)
                                                          if rsi_cfg.budget_ref else {}) else None
    retries = int((plan.stop_rules or {}).get("max_infrastructure_retries", 0))
    # What is already on record, from the results themselves rather than the state file: a run that died inside an
    # arm recorded its attempts one by one, before the state file heard of any of them.
    recorded_before = {r.attempt_id for r in activity.results.all() if r.attempt_id}
    for comparison in plan.comparisons:
        arm = comparison["subject"]
        llm = comparison.get("llm") or cfg.get("llm")
        if llm is None:
            raise ValueError(f"{arm}: no model table for this arm")
        try:
            provider = config_mod.build_provider(llm)
        except Exception as e:  # noqa: BLE001 - an absent arm is reported, never replaced
            out(f"ABSENT     {arm}: {type(e).__name__}: {e}"[:160])
            rows[arm] = {"absent": f"{type(e).__name__}: {e}"[:200]}
            continue
        release_ref = plan.release_refs.get(arm) or activity.release_ref
        arm_pricing = pricing_of({"pricing": comparison["pricing"]}) if comparison.get("pricing") \
            else pricing_of(cfg)
        work = improvement(activity, provider=provider, scope_id=f"final:{plan.experiment_id}", installs=False,
                           pricing=arm_pricing, budget=budget)
        # A comparison that names its own model (a frontier arm) runs that model, a separate comparison; every
        # other arm runs the actor its release pins.
        work.actor_override = bool(comparison.get("llm"))
        work.allocation = final_share
        work.dev_tasks = tuple(plan.task_ids)
        order = [(t, i) for t in plan.task_ids for i in range(plan.repeats)]
        role = comparison.get("role", arm)

        def run(arm_name, pairs, retry=0, supersedes=None, _work=work, _release=release_ref, _role=role):
            return _work.run_arm(arm_name, _release, pairs, plan_ref=ref, split="test", candidate=arm_name,
                                 role=_role, retry_index=retry, resume=resume, supersedes=supersedes)

        attempts = refill_attempts(run(arm, order), run, arm, retries)
        reused = sum(1 for a in attempts if a.attempt_id in recorded_before)
        if reused:
            out(f"resume     {arm}: {reused} attempts already on record were kept, not run again")
        rows[arm] = {"summary": summarise(attempts), "attempts": [a.to_json() for a in attempts]}
        state["attempts"].extend({"attempt_id": a.attempt_id, "arm": arm} for a in attempts
                                 if a.attempt_id not in done)
        write_json(state_path, {**state, "status": "running"})
        out(f"arm        {arm}: {rows[arm]['summary']['passed']}/{rows[arm]['summary']['tasks']} passed, "
            f"partial={rows[arm]['summary']['partial_credit']}")
    write_json(state_path, {**state, "status": "completed"})
    return {"plan": ref, "claims": list(plan.claims), "arms": rows,
            "note": "final results are reported, never used to choose a version"}


# ---------------------------------------------------------------------------- report
def report(cfg: dict, base: str | Path = ".", *, out: Callable[[str], None] = print, write: bool = True) -> dict:
    """Pure offline aggregation of what is already on disk (report.py). No model is called."""
    from .report import build_report, render_markdown, write_report
    bench = BenchConfig.load(cfg, base)
    root = config_mod.workspace_root(cfg, base)
    data = build_report(root, bench.domain)
    out(render_markdown(data))
    if write:
        paths = write_report(root, bench.domain, data)
        out(f"wrote      {paths['json']} and {paths['markdown']}")
    return data


# ---------------------------------------------------------------------------- cycle (M7 §1)
def cycle(cfg: dict, base: str | Path = ".", *, out: Callable[[str], None] = print, researcher=None,
          slot_researcher=None, arm_researcher=None, until: str | None = None) -> dict:
    """`bench --phase cycle`: the automatic loop, as calls to the existing phases in the frozen order.

    Plans the config does not name are frozen first, from [freeze.*] tables or the preset, so no ref has to be
    written by hand. Then: the baseline of the starting version, once; a worker segment; each research slot the
    schedule plans; after every slot, another worker segment — which is where an installed R1 has to show that it
    is used. A segment that ends without a candidate does not cancel a planned slot; a paused segment (the
    isolated worker went away) stops the activity until the repair, and a spent budget ends it. The final split is
    never run from here. Every step is on record, so running the cycle again resumes where it stopped and pays
    for nothing already done.

    `until` is a stop point (docs/ROADMAP.md §3, the first paid activity's protocol): `baseline`, `segment-N`,
    `segment-N/round-K` or `slot-N`. The cycle stops after that step with status "stopped": nothing past it is
    paid for, a segment stopped at a round is not finished, and an install's reuse check is left pending. Running
    the cycle again, with a later stop point or none, continues from there.
    """
    from .research.campaign import PAUSES
    settings = improvement_settings(cfg)
    if settings is None:
        raise ValueError("the cycle phase runs the automatic loop an [improvement] table describes "
                         "(examples/automationbench/improvement.toml)")
    cfg = _cycle_config(cfg, base, settings, out)
    activity = open_activity(cfg, base, out=out)
    # Plans named in [rsi] are checked as much as the ones frozen here: none may loosen the preset.
    problems = _beyond_preset(settings, activity.objects.get(activity.rsi.schedule_ref),
                              TaskEvalPlan.from_json(activity.objects.get(activity.rsi.task_plan_ref)).gate_plan)
    if problems:
        raise ValueError("the cycle's plans go beyond the preset: " + "; ".join(problems))
    require(startup_checks(activity, cfg, phase="improve"), phase="cycle")
    state_path = Path(activity.root) / "rsi" / "cycle.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"steps": {}}

    def save() -> None:
        write_json(state_path, state)

    # The plans an activity started under are the ones it finishes under: a run of the cycle that would freeze
    # others (the preset or a [freeze.*] table changed, or [rsi] names other refs) is refused, not followed.
    plans = {"task_plan_ref": activity.rsi.task_plan_ref, "schedule_ref": activity.rsi.schedule_ref,
             "budget_ref": activity.rsi.budget_ref}
    recorded = state.setdefault("plans", {}).get(activity.rsi.scope_id)
    if recorded is None:
        state["plans"][activity.rsi.scope_id] = plans
        save()
    elif recorded != plans:
        changed = sorted(k for k in plans if plans[k] != recorded.get(k))
        raise ValueError(f"this activity ({activity.rsi.scope_id}) runs under {recorded}, and the config now gives "
                         f"other {changed}: an activity's plans are not frozen again (docs/ROADMAP.md §3). Name the "
                         f"recorded refs under [rsi], or start a new activity (another scope_id or workspace)")

    schedule = Schedule(**activity.objects.get(activity.rsi.schedule_ref))
    stop = _stop_point(until, schedule)
    if schedule.research_slots:
        from .evolve.execution import call_bound_problem
        limits = budget_limits(activity.objects.get(activity.rsi.budget_ref) if activity.rsi.budget_ref else {})
        share, outer = float(settings["campaign"]["arm_share"]), float(settings["campaign"]["outer_share"])
        problem = call_bound_problem(pricing_of(cfg), {k: float(v) * share for k, v in limits.items()},
                                     f"a research-slot arm ({share:.0%} of the activity's budget)") or \
            call_bound_problem(pricing_of(cfg), {k: float(v) * outer for k, v in limits.items()},
                               f"a research slot's outer comparison ({outer:.0%} of the activity's budget)")
        if problem:
            raise ValueError(f"the cycle's research slot cannot run: {problem}")
    def guarded(name: str, run) -> tuple:
        """One step under the pause and end rules of docs/modules/05_EXECUTION_BUDGET.md §3 -> (its result, ""),
        or (None, "paused" | "budget_exhausted") with the step's record saying why. A failure outside a task
        attempt that pauses or ends the activity (research.loop.halt_of) is written into cycle.json rather than
        leaving the cycle as an exception; running the cycle again resumes the step from what it recorded."""
        from .research.loop import halt_of
        try:
            return run(), ""
        except Exception as e:  # noqa: BLE001 - a halt is recorded here; anything else re-raises
            halt = halt_of(e)
            if halt is None:
                raise
        ended = "paused" if halt[0] == "repair_runtime" else halt[0]
        # a segment's record keeps its own shape (stop_reason), which the reuse check and the report read
        state["steps"].setdefault(name, {}).update(outcome=ended, reason=halt[1],
                                                   **({"stop_reason": halt[0]} if name.startswith("segment") else {}))
        save()
        out(f"cycle      {name}: {ended} ({halt[1][:200]})")
        return None, ended

    status, halt = "done", ""
    if state["steps"].get("baseline", {}).get("release"):
        out("cycle      the starting version's baseline is on record; not run again")
    else:
        _report, halt = guarded("baseline", lambda: baseline(cfg, base, out=out))
        if not halt:
            state["steps"]["baseline"] = {"release": activity.releases.active()["release_ref"], "ts": now()}
            save()
    for index in range(schedule.research_slots + 1):
        if halt:
            status = halt
            break
        if stop == ("baseline", None, None):
            status = "stopped"
            break
        segment = f"segment-{index}"
        out(f"cycle      worker {segment}")
        at_round = stop[2] if stop and stop[:2] == ("segment", index) else None
        result, halt = guarded(segment, lambda: improve(cfg, base, out=out, researcher=researcher, segment=segment,
                                                       stop_after_round=at_round))
        if halt:
            status = halt
            break
        record = state["steps"].get(segment)
        if result.get("already_finished") and record is not None:
            # A segment finished in an earlier run keeps the release it was finished with (an install since has
            # moved the pointer); its end is the segment log's, and no pause recorded against it stands.
            record.pop("outcome", None)
            record.pop("reason", None)
            record["stop_reason"] = result["stop_reason"]
        else:
            state["steps"][segment] = {"stop_reason": result["stop_reason"], "release": result["release"]}
        if result["stop_reason"] == "stopped_at":
            save()
            status = "stopped"
            break
        pending = state.get("reuse_pending")
        # The reuse of an installed R1 is read from the segment right after its slot, once that segment has ended
        # without a pause: not from an earlier segment a restart walks past, and not from part of a segment.
        due = (pending or {}).get("segment") or (pending and f"segment-{int(pending['slot'].rsplit('-', 1)[1]) + 1}")
        if pending and segment == due and result["stop_reason"] not in PAUSES \
                and not state["steps"].get(pending["slot"], {}).get("reuse"):
            state["steps"][pending["slot"]]["reuse"] = _reuse_fact(activity, pending["r1"], pending.get("r0"),
                                                                   segment)
            out(f"reuse      {state['steps'][pending['slot']]['reuse']['state']}: "
                f"{state['steps'][pending['slot']]['reuse']['detail']}")
            state.pop("reuse_pending")
        save()
        if result["stop_reason"] in PAUSES:
            status = "paused"
            # the same record a step the guard caught gets: what paused it, in the round that did
            why = next((r.get("reason") for r in reversed(result.get("rounds") or [])
                        if r.get("next_action") == "repair_runtime" and r.get("outcome") != "stopped"), "")
            state["steps"][segment].update(outcome="paused", reason=why or result["stop_reason"])
            save()
            out(f"cycle      paused in {segment} ({result['stop_reason']}): no further segment or research slot "
                f"runs until the runtime is repaired; running the cycle again resumes here")
            break
        if result["stop_reason"] == "budget_exhausted":
            status = "budget_exhausted"
            out("cycle      the activity's budget is spent: the activity ends here")
            break
        if index == schedule.research_slots:
            break
        if stop and stop[:2] == ("segment", index):
            status = "stopped"
            break
        slot = f"slot-{index}"
        outcome, halt = guarded(slot, lambda: _cycle_slot(cfg, base, activity, settings, slot, state, save, out=out,
                                                         researcher=slot_researcher, arm_researcher=arm_researcher))
        outcome = halt or outcome
        if outcome in ("budget_exhausted", "paused"):
            status = outcome
            if outcome == "paused":
                out(f"cycle      paused in research {slot}: repair the runtime and run the cycle again; the slot "
                    f"resumes and the next worker segment runs after it")
            break
        if stop and stop[:2] == ("slot", index):
            status = "stopped"
            break
    if status == "stopped":
        out(f"cycle      stopped at {until}: nothing past it was paid for; run the cycle again (with a later stop "
            f"point, or none) to continue from here")
    elif status != "paused":                 # a paused cycle keeps the reuse check pending for after the repair
        for step in state["steps"].values():
            if isinstance(step, dict) and step.get("reuse") is None and step.get("installed"):
                step["reuse"] = {"state": "not_completed", "detail": "no worker segment ran after the install"}
    state["status"] = status
    save()
    data = report(cfg, base, out=out)
    # A paused cycle needs someone to act, so the command says so (exit 1); a spent budget is a normal end.
    return {"status": status, "ok": status != "paused", "steps": state["steps"],
            "report": data.get("claims") if isinstance(data, dict) else None}


def _stop_point(until: str | None, schedule: Schedule) -> tuple | None:
    """A cycle stop point as (step, index, round): ("baseline", None, None), ("segment", N, K or None) or
    ("slot", N, None). A step this schedule does not have is refused before anything runs."""
    if until is None:
        return None
    m = re.fullmatch(r"baseline|segment-(\d+)(?:/round-(\d+))?|slot-(\d+)", until.strip())
    if not m:
        raise ValueError(f"--until {until!r}: baseline, segment-N, segment-N/round-K or slot-N")
    if until.strip() == "baseline":
        return ("baseline", None, None)
    if m.group(1) is not None:
        n = int(m.group(1))
        if n > schedule.research_slots:
            raise ValueError(f"--until {until!r}: this schedule has segments 0 to {schedule.research_slots}")
        k = int(m.group(2)) if m.group(2) is not None else None
        if k is not None and k >= schedule.worker_rounds:
            raise ValueError(f"--until {until!r}: a segment has rounds 0 to {schedule.worker_rounds - 1}")
        return ("segment", n, k)
    n = int(m.group(3))
    if n >= schedule.research_slots:
        raise ValueError(f"--until {until!r}: this schedule has {schedule.research_slots} research slot(s)")
    return ("slot", n, None)


def _cycle_tables(cfg: dict, settings: dict) -> dict:
    """The plans an automatic activity runs under, as [freeze.*] tables: the config's own where it has them, else
    the preset's. `ouroloop freeze` and the cycle freeze the same tables, so a phase run on its own after the
    freeze and the cycle afterwards run under the same plans and reuse each other's attempts."""
    tables = dict(cfg.get("freeze") or {})
    return {"task_plan": tables.get("task_plan") or settings["task_plan"],
            "schedule": {**settings["schedule"], "candidates_per_round": settings["candidates_per_round"],
                         "collection_batches": settings["collection_batches"], **(tables.get("schedule") or {})},
            **({"budget": tables["budget"]} if "budget" in tables else {})}


def _cycle_config(cfg: dict, base, settings: dict, out) -> dict:
    """The config with every plan the cycle needs named: what [rsi] names is used as it is; the rest is frozen
    from [freeze.*] when present, else from the preset. Freezing is content-addressed, so a restart freezes the
    same objects and gets the same refs. A missing budget is refused before anything is paid for."""
    rsi_table = dict(cfg.get("rsi") or {})
    tables = _cycle_tables(cfg, settings)
    if not rsi_table.get("budget_ref") and "budget" not in tables:
        raise ValueError("the cycle pays for model calls and needs the activity's budget first: set [rsi] "
                         "budget_ref or a [freeze.budget] table")
    need = {name: table for name, table in tables.items() if not rsi_table.get(f"{name}_ref")}
    if not need:
        return cfg
    refs = freeze({**cfg, "freeze": need}, base, out=lambda _line: None, with_preset=False)
    out("cycle      froze " + ", ".join(f"{k}={_ct.short(v)}" for k, v in refs.items()))
    return {**cfg, "rsi": {**rsi_table, **refs}}


def _beyond_preset(settings: dict, schedule: dict | None, gate: dict | None) -> list[str]:
    """What a schedule or gate would loosen beyond [improvement] and its preset: more candidates, collection
    batches or research slots than the reader allows, a lower min_gain or a higher max_regression_rate than the
    preset's."""
    problems = []
    if schedule is not None:
        if int(schedule.get("candidates_per_round", 1)) != settings["candidates_per_round"]:
            problems.append(f"candidates_per_round {schedule.get('candidates_per_round')}: this version runs "
                            f"{settings['candidates_per_round']}")
        if int(schedule.get("collection_batches", 0)) > settings["collection_batches"]:
            problems.append(f"collection_batches {schedule.get('collection_batches')} is above "
                            f"max_collection_batches {settings['collection_batches']}")
        # A second slot would have the installed R1 propose R2: a path this version has neither tested nor planned
        # (docs/DESIGN.md §7), so the preset's count is a ceiling.
        slots = int(settings["schedule"].get("research_slots", 0))
        if int(schedule.get("research_slots", 0)) > slots:
            problems.append(f"research_slots {schedule.get('research_slots')} is above the preset's {slots}")
    if gate is not None:
        preset = settings["task_plan"]["gate"]
        if float(gate.get("min_gain", preset["min_gain"])) < float(preset["min_gain"]):
            problems.append(f"min_gain {gate.get('min_gain')} is below the preset's {preset['min_gain']}")
        if float(gate.get("max_regression_rate", preset["max_regression_rate"])) > float(preset["max_regression_rate"]):
            problems.append(f"max_regression_rate {gate.get('max_regression_rate')} is above the preset's "
                            f"{preset['max_regression_rate']}")
    return problems


def _cycle_slot(cfg, base, activity: Activity, settings: dict, slot: str, state: dict, save, *, out, researcher,
                arm_researcher) -> str:
    """One planned research slot, recorded step by step: the frozen campaign plan before any arm runs, then the
    receipt. A restart resumes the same plan (its arms resume too), and a slot with a receipt is not run again."""
    step = state["steps"].setdefault(slot, {})
    if step.get("receipt") or step.get("outcome") in ("no_proposal", "invalid", "rejected", "failed"):
        out(f"cycle      research {slot} already on record ({step.get('verdict') or step.get('outcome')})")
        return "done"
    if step.get("outcome") in ("paused", "budget_exhausted"):
        # how an earlier run of the slot stopped, not its result: it continues from what it recorded
        step.pop("outcome")
        step.pop("reason", None)
    budget = activity_budget(activity, cfg, slot)
    if not budget.affordable({}):
        step["outcome"] = "budget_exhausted"
        save()
        return "budget_exhausted"
    if not step.get("campaign"):
        pairs = cycle_pairs(activity, cfg)
        campaign = settings["campaign"]
        template = TaskEvalPlan.from_json(activity.objects.get(activity.rsi.task_plan_ref))
        inner_groups = len(pairs[0].inner_dev_groups)
        # The arms' inner gate is the mainline gate on fewer families; the research verdict is over independence
        # groups, and the preset's pair rule makes one, so its min_groups comes from the preset (exploratory).
        gate = GatePlan(min_gain=float(template.gate().min_gain),
                        min_groups=max(1, min(template.gate().min_groups, inner_groups)),
                        max_regression_rate=template.gate().max_regression_rate)
        research_gate = GatePlan(min_gain=float(campaign["min_gain"]), min_groups=int(campaign["min_groups"]))
        limits = budget_limits(activity.objects.get(activity.rsi.budget_ref) if activity.rsi.budget_ref else {})
        arm_budget = {k: round(float(v) * float(campaign["arm_share"]), 6) for k, v in limits.items()}
        outer_budget = {k: round(float(v) * float(campaign["outer_share"]), 6) for k, v in limits.items()}
        # docs/modules/05_EXECUTION_BUDGET.md §3: a slot opens only when the root can cover its outer comparison's
        # share and both arms' shares — and R's own call, when it is the cycle's metered researcher that asks —
        # so neither R nor the common inputs are paid for a comparison that could not be made on equal terms
        need = {k: round(float(outer_budget.get(k, 0)) + 2 * float(arm_budget.get(k, 0)), 6) for k in limits}
        if researcher is None:
            for k, v in pricing_of(cfg).bound().items():
                need[k] = round(float(need.get(k, 0)) + float(v), 6)
        if not budget.affordable(need):
            step.update(outcome="budget_exhausted",
                        reason=f"the root cannot cover the outer comparison's share {outer_budget} and both "
                               f"arms' shares {arm_budget}" + (" and R's call" if researcher is None else ""))
            save()
            return "budget_exhausted"
        discovery = "automatic" if researcher is None else "scripted"
        researcher = researcher or _default_researcher(cfg, activity, budget=budget)
        planned = research_slot(cfg, base, pairs=pairs, researcher=researcher, out=out,
                                worker_rounds=int(campaign["worker_round_limit"]), gate=gate,
                                research_gate=research_gate, arm_budget=arm_budget, discovery=discovery,
                                outer_budget=outer_budget, budget=budget, run=False)
        if planned["outcome"] in ("repair_runtime", "infrastructure_failed"):
            step.update(outcome="paused", reason="; ".join(planned.get("reasons") or ()))
            save()
            return "paused"
        if planned["outcome"] != "planned":
            # where the slot ended — R's reply, admission or the build — so the report can say which (M7 §4)
            step.update(outcome=planned["outcome"], reason="; ".join(planned.get("reasons") or ())
                        or planned.get("reason", ""), stage=planned.get("stage"))
            save()
            out(f"cycle      research {slot}: {planned['outcome']}; the next worker segment runs as scheduled")
            return "done"
        step.update(campaign=planned["campaign"], r1=planned["r1"], experiment=planned["experiment"],
                    r0=_ct.SystemRelease.from_json(activity.objects.get(
                        activity.releases.active()["release_ref"])).research_ref)
        save()
    cfg = {**cfg, "rsi": {**dict(cfg.get("rsi") or {}), "research_plan_ref": step["campaign"]}}
    result = research(cfg, base, out=out, researcher=arm_researcher, campaign_ref=step["campaign"])
    installed = (result.get("installation") or {}).get("status") == "installed"
    step.update(receipt=result["receipt"], verdict=result["verdict"], installed=installed,
                release=result["release"])
    if result["verdict"] == "inconclusive" and any("paused" in r for r in
                                                   activity.objects.load(result["receipt"],
                                                                         _ct.EvaluationReceipt).reasons):
        # A paused arm: not a finished comparison. Keep the plan so the next run resumes it, and suspend the
        # activity: the next worker segment must not run before the slot finishes (docs/modules/07 §1).
        step.pop("receipt")
        step["outcome"] = "paused"
        save()
        return "paused"
    if installed:
        state["reuse_pending"] = {"slot": slot, "r1": step["r1"], "r0": step.get("r0"),
                                  "segment": f"segment-{int(slot.rsplit('-', 1)[1]) + 1}"}
    save()
    return "done"


def cycle_pairs(activity: Activity, cfg: dict) -> list:
    """The research slot's one pair, fixed from frozen objects (the preset's campaign rule): the current W as the
    common start, the train families as the inner training pool, and the development plan's families halved by
    their frozen order — the first half is the arms' inner gate, the second the outer comparison neither arm
    touches."""
    from .research.campaign import CampaignPair
    template = TaskEvalPlan.from_json(activity.objects.get(activity.rsi.task_plan_ref))
    family = {e.task_id: e.family_id for e in activity.splits.entries}
    dev_families = list(dict.fromkeys(family[t] for t in template.task_ids))
    if len(dev_families) < 2:
        raise ValueError("a research slot needs at least two development families: one for the arms' inner gate "
                         "and one for the outer comparison")
    half = len(dev_families) // 2
    train_families = list(dict.fromkeys(family[t] for t in activity.splits.ids("train")))
    active = _ct.SystemRelease.from_json(activity.objects.get(activity.releases.active()["release_ref"]))
    # E0, both arms' starting experience: the mainline log as it stands, each experiment's raw gate reasons left
    # out because they can name development tasks — the second half of which is now the outer pool. The aggregate
    # figures stay (docs/modules/06_RESEARCH_CAMPAIGN.md §3).
    entries = [{k: v for k, v in e.items() if k != "reasons"} if e.get("kind") == "experiment" else e
               for e in _experience.read(activity.root)]
    return [CampaignPair(pair_id="pair-1", independence_group="group-1", initial_w_ref=active.worker_ref,
                         initial_experience_ref=activity.objects.put_json("change", {"experience": entries}),
                         inner_train_groups=tuple(train_families), inner_dev_groups=tuple(dev_families[:half]),
                         outer_groups=tuple(dev_families[half:]), split_ref=activity.splits.hash(), repeats=1,
                         seed_schedule=(0,))]


def _reuse_fact(activity: Activity, r1: str, r0: str | None, segment: str) -> dict:
    """Whether the worker segment after an install actually used R1 (docs/modules/06_RESEARCH_CAMPAIGN.md §4):
    research_steps records of that segment's rounds that name R1 as the loaded R, bound R1's text for every prompt
    R1 changed, and sent at least one request of the step that text drives. A pointer that names R1 is not
    enough, and neither is a round that loaded R1 and sent nothing."""
    from .research.campaign import RESEARCH_STEPS
    new = _P.prompts_of(activity.objects, r1)
    old = _P.prompts_of(activity.objects, r0) if r0 and activity.objects.has(r0) else {}
    changed = [t for t in RESEARCH_STEPS if new.get(t) and new.get(t) != old.get(t)]
    rows = []
    for e in _ct.Events(activity.root).of("research_steps"):
        if e.get("research_ref") != r1 or e.get("scope") != activity.rsi.scope_id \
                or e.get("stage", "propose") != "propose":
            continue
        steps = {s.get("step") for s in e.get("steps") or ()}
        if all((e.get("prompts") or {}).get(t) == _ct.digest({"text": new[t]})[:16]
               and steps & set(RESEARCH_STEPS[t]) for t in changed) and steps:
            rows.append(e)
    if not rows:
        return {"state": "not_completed", "segment": segment,
                "detail": f"no research step of {segment} sent a request from R1's changed prompts "
                          f"{changed or '(none changed)'}: the install is not shown to be used"}
    return {"state": "verified", "segment": segment, "steps": len(rows), "context_refs": [r["context_ref"]
                                                                                          for r in rows],
            "detail": f"{len(rows)} research rounds of {segment} loaded R1 and sent "
                      f"{sum(len(r.get('steps') or ()) for r in rows)} requests"}


def research_slot(cfg: dict, base: str | Path = ".", *, pairs, researcher, arm_researcher=None,
                  out: Callable[[str], None] = print, worker_rounds: int = 1, gate: GatePlan | None = None,
                  arm_budget: dict | None = None, phase: str = "research_dev",
                  discovery: str = "scripted", run: bool = True, research_gate: GatePlan | None = None,
                  outer_budget: dict | None = None, budget=None) -> dict:
    """One research slot: the current R proposes a change to R, the change is compared, and the winner is installed.

    The proposal goes through the same admission and the same build as a worker candidate — only the owner and
    the evaluator differ (docs/modules/04_RESEARCH_LOOP.md §4). The campaign plan is frozen from `pairs` once R1
    exists, because a plan naming a candidate that does not exist yet could not be checked; nothing about the
    thresholds or the task pools is decided after the arms run.

    `discovery` travels into the receipt: a change a person wrote exercises this path and is not evidence that
    the system found it (RC-10).

    With `run=False` the slot stops once the campaign plan is frozen and returns its ref, so a caller that has
    to survive a restart (the cycle) can record the plan before any arm runs and resume that same plan.
    """
    from .research.campaign import CampaignPlan
    from .research.experiment import admit, freeze, propose_experiment
    activity = open_activity(cfg, base, out=out)
    release_ref = activity.releases.active()["release_ref"]
    provider = config_mod.build_provider(cfg["llm"])
    # the budget the slot's R is shown: the slot's own account, whatever binds above it (M4 §5)
    work = improvement(activity, template_ref=activity.rsi.task_plan_ref, provider=provider, owner="research",
                       budget=budget)
    if work.harness:
        # A first-version slot reads how the rounds went, not a fresh run of the tasks: the latest recorded
        # measurement stands for the current version, and R's input is the experience log's projection.
        evidence = work.last_measurement(release_ref) or {
            "results": [], "train": [], "dev": [], "release": release_ref,
            "feedback": _feedback.build_feedback(activity.rt, objects=activity.objects, scope_id=work.scope_id)}
    else:
        evidence = work.collect()
    experience, experience_ref = _experience.snapshot(activity.root)
    context = work.context(goal="improve how this research policy finds and verifies changes",
                           release_ref=release_ref, evidence=evidence, experience=experience,
                           experience_ref=experience_ref)
    bound = _P.bind_release(release_ref, profile="task", services=work.services(), authorization="installed")
    proposal_result = propose_experiment(context, bound, "research", researcher=researcher,
                                         objects=activity.objects, experience=experience)
    if proposal_result.unreadable:
        # docs/modules/04_RESEARCH_LOOP.md §1: R decided nothing, so this is not "no proposal". The slot is not
        # asked again: the next worker segment runs, and the slot's record says why it ended.
        out(f"slot       the research reply is invalid: {proposal_result.reason[:200]}")
        return {"outcome": "invalid", "stage": "reply", "reason": proposal_result.reason, "release": release_ref}
    if not proposal_result.proposals:
        out(f"slot       no research proposal ({proposal_result.next_action}: {proposal_result.reason})")
        return {"outcome": "no_proposal", "stage": "reply", "reason": proposal_result.reason, "release": release_ref}
    core = activity.objects.load(work.core_ref, _ct.CoreManifest)
    admission = admit(proposal_result.proposals[0], context=context, core=core, objects=activity.objects,
                      owner="research", checks=tuple(bound.preflight or ()))
    if not admission.ok:
        out(f"slot       {admission.status}: {'; '.join(admission.reasons)[:200]}")
        return {"outcome": admission.status, "stage": "admission", "reasons": admission.reasons,
                "release": release_ref}
    experiment = freeze(admission.proposal_ref, objects=activity.objects, parent_release=release_ref,
                        experience_ref=experience_ref, core_ref=work.core_ref,
                        eval_template_ref=activity.rsi.task_plan_ref or "", budget_scope=activity.rsi.scope_id)
    activity.objects.put(experiment)
    build = _P.build_candidate(experiment, objects=activity.objects, parent_release=release_ref, owner="research",
                              services=work.services())
    if build.status != "ready":
        out(f"slot       build {build.status}: {'; '.join(build.errors)[:200]}")
        return {"outcome": build.status, "stage": "build", "reasons": list(build.errors), "release": release_ref}
    r0 = _ct.SystemRelease.from_json(activity.objects.get(release_ref)).research_ref
    out(f"slot       R1 {_ct.short(build.candidate_ref)} proposed by R0 {_ct.short(r0)}")
    plan = CampaignPlan(id=f"campaign-{_ct.digest({'r1': build.candidate_ref})[:10]}", phase=phase,
                        core_ref=work.core_ref, r_control_ref=r0, r_candidate_ref=build.candidate_ref,
                        campaign_pairs=tuple(activity.objects.put(p) for p in pairs),
                        arm_budget=dict(arm_budget or {"model_calls": 2000, "tool_calls": 2000}),
                        outer_budget=dict(outer_budget) if outer_budget else None,
                        worker_round_limit=worker_rounds,
                        worker_gate_plan_ref=activity.objects.put(gate or GatePlan(min_groups=1, min_gain=0.01,
                                                                                  bootstrap_replicates=200)),
                        research_gate_plan=(research_gate or gate or GatePlan(min_groups=1, min_gain=0.05,
                                                                             bootstrap_replicates=200)).to_json(),
                        selection_rule="last_installed", discovery=discovery)
    plan_ref = activity.objects.put(plan)
    if not run:
        return {"outcome": "planned", "r1": build.candidate_ref, "campaign": plan_ref, "experiment": experiment.id}
    cfg = {**cfg, "rsi": {**cfg.get("rsi", {}), "research_plan_ref": plan_ref}}
    outcome = research(cfg, base, out=out, researcher=arm_researcher, campaign_ref=plan_ref)
    return {"outcome": outcome["verdict"], "r1": build.candidate_ref, "campaign": plan_ref,
            "experiment": experiment.id, **outcome}


# ---------------------------------------------------------------------------- startup checks
PRICING_KEYS = {"input_per_1k", "output_per_1k", "max_input_tokens", "max_output_tokens", "declared_fallbacks",
                "hidden_fallback", "source", "cache_read_per_1k", "cache_write_per_1k"}


def pricing_of(cfg: dict):
    """The `[pricing]` snapshot: prices, caps and whether the provider may fall back to something it does not
    report. Without it a dollar budget cannot be enforced, and this says so rather than pretending."""
    from .evolve.execution import Pricing
    table = dict(cfg.get("pricing") or {})
    unknown = sorted(set(table) - PRICING_KEYS)
    if unknown:
        raise ValueError(f"[pricing] does not take {unknown}; it takes {sorted(PRICING_KEYS)}")
    table["declared_fallbacks"] = tuple(table.get("declared_fallbacks") or ())
    return Pricing(**table)


def startup_checks(activity: Activity, cfg: dict, *, phase: str, needs_model: bool = True) -> list[str]:
    """What has to hold before an expensive phase starts (docs/modules/07_BENCH_REPORT.md §2).

    Upstream and the scorer contract, the model table, the per-attempt limits, whether the declared budget can
    actually be enforced, whether the plan has enough independent groups, and whether the installed release still
    assembles. A missing piece is named here rather than discovered half way through a paid run.
    """
    from .evolve.execution import check_pricing
    problems = []
    # The [rsi] table is read from the config given here, not from the snapshot the activity was opened with: the
    # caller may have frozen a plan in between, and checking the stale table would check the wrong plan.
    rsi_cfg = RsiConfig.load(cfg)
    if activity.drift:
        problems.append(f"upstream drifted from the frozen manifest: {activity.drift[:3]}")
    checks = ab.contract_check(activity.suite, list(activity.splits.ids("dev"))[:1])
    problems += [f"scorer contract: {line}" for line in checks["failed"]]
    if needs_model and "llm" not in cfg:
        problems.append(f"the {phase} phase needs an [llm] table: it runs real attempts")
    plan = _frozen_plan(activity, rsi_cfg.task_plan_ref)
    problems += limit_problems(_attempt_budget(activity, plan), _five_limits(activity, plan))
    if not isinstance(activity.bench.concurrency, int) or isinstance(activity.bench.concurrency, bool) \
            or activity.bench.concurrency < 1:
        problems.append(f"[bench] concurrency must be a whole number of attempts, at least 1, not "
                        f"{activity.bench.concurrency!r}")
    # A plan the config freezes and [rsi] does not name yet: a phase run before `ouroloop freeze` would open the
    # activity's budget root without its limits (a root cannot be reopened with other limits later) or measure
    # under conditions other than the frozen plan's, so its attempts would not be reused (ROADMAP §3).
    # An automatic activity ([improvement]) runs every phase under the cycle's plans, preset ones included.
    named = {"task_plan": rsi_cfg.task_plan_ref, "schedule": rsi_cfg.schedule_ref, "budget": rsi_cfg.budget_ref}
    required = set(cfg.get("freeze") or {}) & set(named)
    if improvement_settings(cfg) is not None:
        required |= set(named)
    for table in sorted(required):
        if not named[table]:
            problems.append(f"[rsi] names no {table}_ref: run `ouroloop freeze --config ...` and add the [rsi] lines "
                            f"it prints before a paid phase (`--phase cycle` freezes them itself)")
    # The models a paid phase actually calls: the actor the active release pins (its recorded configuration, not
    # whatever [llm] says now) and the research model. A call may not ask for more output than its reservation
    # bounds — a max_tokens above [pricing] max_output_tokens could spend past it, found out only after the money
    # is gone — and a key that is not set fails every call, so both are found here rather than in a paid run.
    if needs_model:
        priced = pricing_of(cfg).max_output_tokens
        for name, table in _called_models(activity, cfg):
            asked = table.get("max_tokens")
            if priced is not None and asked is not None and int(asked) > int(priced):
                problems.append(f"{name} max_tokens {asked} is above [pricing] max_output_tokens {priced}: a "
                                f"call's reservation would not bound what it can spend")
            elif table.get("type") == "claude_code" and priced is not None and asked is not None:
                from .providers.claude_code import REQUESTS_PER_CALL
                if int(asked) * REQUESTS_PER_CALL > int(priced):
                    problems.append(f"{name} max_tokens {asked} times the {REQUESTS_PER_CALL} requests one claude "
                                    f"CLI call can make is above [pricing] max_output_tokens {priced}: a call's "
                                    f"reservation would not bound what it can spend")
            key = table.get("api_key_env")
            if table.get("type") == "openai_compat" and key and not os.environ.get(str(key)):
                problems.append(f"{name} reads its key from {key}, which is not set")
            if table.get("type") == "claude_code":
                from .providers.claude_code import cli_problem
                problem = cli_problem(table)
                if problem:
                    problems.append(f"{name}: {problem}")
    frozen_budget = activity.objects.get(rsi_cfg.budget_ref) if rsi_cfg.budget_ref else {}
    problems += [f"the frozen budget: {p}" for p in budget_problems(frozen_budget, pricing_of(cfg))]
    limits = budget_limits(frozen_budget)
    if limits:
        from .evolve.execution import call_bound_problem
        reason = check_pricing(pricing_of(cfg), limits) or call_bound_problem(pricing_of(cfg), limits,
                                                                               "the activity's budget")
        if reason:
            problems.append(f"the declared budget cannot be enforced: {reason}")
    if rsi_cfg.task_plan_ref and activity.objects.has(rsi_cfg.task_plan_ref):
        plan = TaskEvalPlan.from_json(activity.objects.get(rsi_cfg.task_plan_ref))
        families = {activity.splits.family_of(t) for t in plan.task_ids
                    if t in {e.task_id for e in activity.splits.entries}}
        missing = sorted(set(plan.task_ids) - {e.task_id for e in activity.splits.entries})
        if missing:
            problems.append(f"the plan names tasks the manifest does not have: {missing[:3]}")
        if plan.gate_plan and len(families) < plan.gate().min_groups:
            problems.append(f"the plan runs {len(families)} independent groups and its gate needs "
                            f"{plan.gate().min_groups}")
    try:
        bound = _P.bind_release(activity.release_ref, profile="preflight",
                                services=_P.Services(activity.objects, serving=activity.rt.serving))
        activity.releases.authorize(activity.release_ref, profile="task")
    except Exception as e:  # noqa: BLE001 - a release that does not assemble is not something to run
        problems.append(f"the active release does not assemble: {type(e).__name__}: {e}"[:300])
        return problems
    problems += harness_checks(activity, bound)
    try:
        wanted = harness_settings(cfg)
    except ValueError as e:
        problems.append(str(e))
        wanted = None
    core = activity.objects.load(_ct.SystemRelease.from_json(activity.objects.get(activity.release_ref)).core_ref,
                                 _ct.CoreManifest)
    if wanted is not None and not (core.capabilities or {}).get("harness"):
        problems.append("[harness_evolution] is set, and the active release was bootstrapped without it: a "
                        "first-version activity starts in a fresh workspace (or scope) so W0, R0 and K are "
                        "bootstrapped with it; it is not applied to an existing v2 activity")
    return problems


def _called_models(activity: Activity, cfg: dict) -> list[tuple[str, dict]]:
    """(name, provider table) of each model a paid phase calls: the pinned actor's recorded configuration (or
    [llm] for a release that pins none), and the research model ([research.llm], else [llm])."""
    pinned = _pinned_actor(activity)
    actor = ("the actor the active release pins", activity.objects.get(pinned.inference_ref)) if pinned is not None \
        else ("[llm]", dict(cfg.get("llm") or {}))
    research_table = dict(cfg.get("research") or {}).get("llm")
    research = ("[research.llm]", dict(research_table)) if research_table else \
        ("[llm] as the research model", dict(cfg.get("llm") or {}))
    return [actor, research]


def config_harness_problems(cfg: dict) -> list[str]:
    """BR6-17 before anything is paid, from the config alone (smoke has no activity yet): a first-version table
    that allows code components needs the isolated worker."""
    from .agent import harness_package as hp
    settings = harness_settings(cfg)
    if settings is None:
        return []
    limits = hp.Limits.from_json(settings["package"])
    if limits.allows_code and isolated_worker(settings) is None:
        return [f"[harness_evolution] allows code components "
                f"{[k for k in limits.components if k in hp.CODE_KINDS]} and no qualified isolated worker is "
                f"available"]
    return []


def isolated_worker(settings: dict | None):
    """The qualified isolated worker for package code, or None: the Docker worker of the harness settings, when
    docker runs and its image is built. Never an unisolated fallback (docs/modules/01_POLICY_RUNTIME.md §1)."""
    from .agent.worker import DEFAULT_IMAGE, docker_worker
    if not settings or settings.get("sandbox") != "docker":
        return None
    from .agent.worker import WorkerLimits
    worker = docker_worker(settings.get("image") or DEFAULT_IMAGE)
    if worker is not None and settings.get("image_id") and worker.image_id() != settings["image_id"]:
        return None            # K froze another image: this one is not the qualified worker of this activity
    if worker is not None:
        # K's limits, not the defaults: the per-request time a package test, tool or hook gets, and the image by
        # the content id K froze rather than by its tag.
        worker.limits = WorkerLimits(request_seconds=float(settings.get("test_timeout_seconds") or 30))
        worker.pinned_id = settings.get("image_id")
    return worker


def harness_checks(activity: Activity, bound) -> list[str]:
    """What a first-version activity must satisfy before it pays for anything (docs/modules/00_CONTRACTS.md §3,
    BR6-17): the current package's component kinds are within K, and a worker is there whenever K allows code or
    the current package runs code. The fallback to a text-only preset holds only for a package without code."""
    from .agent import harness_package as hp
    core = activity.objects.load(_ct.SystemRelease.from_json(activity.objects.get(activity.release_ref)).core_ref,
                                 _ct.CoreManifest)
    harness = (core.capabilities or {}).get("harness")
    if not harness:
        return []
    problems = []
    limits = hp.Limits.from_json(harness["package"])
    if bound.package is None:
        return ["a first-version core mounts a capability package, and the active release has none"]
    run, tests = hp.files_of(activity.objects, bound.package.ref)
    outside = sorted(hp.kinds_of(bound.package.manifest, {**run, **tests}) - set(limits.components))
    if outside:
        problems.append(f"the active package uses component kinds {outside} that K does not allow "
                        f"({list(limits.components)})")
    worker = isolated_worker(harness)
    if limits.allows_code and worker is None:
        problems.append(f"K allows code components {[k for k in limits.components if k in hp.CODE_KINDS]} and no "
                        f"qualified isolated worker is available; freeze a text-only preset (memory, skills) or "
                        f"provide the worker")
    if bound.package.runs_code and worker is None:
        problems.append("the active package runs code and no qualified isolated worker is available")
    return problems


# ---------------------------------------------------------------------------- freezing plans
FREEZE_TABLES = {"task_plan", "schedule", "budget", "final"}
TASK_PLAN_KEYS = {"tasks", "repeats", "order_seed", "screen", "installs", "per_attempt_budget", "gate"}
FINAL_KEYS = {"experiment_id", "claims", "comparisons", "release_refs", "tasks", "repeats", "statistical_plan",
              "stop_rules", "research_campaign_plan_ref"}


def _task_ids(activity: Activity, spec, what: str) -> tuple:
    """A split name ("dev", "test") resolved against the frozen manifest, or an explicit list checked against it.
    The plan stores the ids themselves, so a later change to the manifest cannot change what a plan ran."""
    if isinstance(spec, str):
        ids = tuple(activity.splits.ids(spec))
        if not ids:
            raise ValueError(f"[freeze.{what}] tasks = {spec!r}: the frozen manifest has no such tasks")
        return ids
    known = {e.task_id for e in activity.splits.entries}
    missing = sorted(set(spec) - known)
    if missing:
        raise ValueError(f"[freeze.{what}] names tasks the frozen manifest does not have: {missing[:5]}")
    return tuple(spec)


def _only(table: dict, allowed: set, what: str) -> dict:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ValueError(f"[freeze.{what}] does not take {unknown}; it takes {sorted(allowed)}")
    return dict(table)


def freeze(cfg: dict, base: str | Path = ".", *, out: Callable[[str], None] = print,
           with_preset: bool = True) -> dict:
    """Freeze the `[freeze.*]` tables into workspace objects and print the `[rsi]` lines that name them.

    The tables are the input to this command, not the plan: once frozen, a plan is the content-addressed object,
    and editing the table afterwards changes nothing that already ran — it can only produce a different object
    with a different ref, which a phase then has to be told about. Freezing the same tables twice gives the same
    refs. The final plan is sealed as it is frozen; a different final plan under the same experiment id is
    refused (docs/modules/07_BENCH_REPORT.md §3).

    With an [improvement] table, the task plan and the schedule the preset gives are frozen too, as the cycle
    would freeze them, so the [rsi] lines printed here are every plan a paid phase of the activity runs under.
    """
    from .evolve.execution import LIMITS
    tables = dict(cfg.get("freeze") or {})
    unknown = sorted(set(tables) - FREEZE_TABLES)
    if unknown:
        raise ValueError(f"[freeze] has no tables {unknown}; it takes {sorted(FREEZE_TABLES)}")
    settings = improvement_settings(cfg) if with_preset else None
    if settings is not None:
        tables = {**_cycle_tables(cfg, settings), **{k: v for k, v in tables.items() if k == "final"}}
    if not tables:
        raise ValueError("nothing to freeze: add [freeze.task_plan], [freeze.schedule], [freeze.budget] or "
                         "[freeze.final] to the config")
    problems = config_harness_problems(cfg)
    if problems:
        raise ValueError("freeze refused: " + "; ".join(problems))
    activity = open_activity(cfg, base, out=out)
    bound = _P.bind_release(activity.release_ref, profile="preflight", services=_P.Services(activity.objects))
    problems = harness_checks(activity, bound)
    if problems:
        raise ValueError("freeze refused: " + "; ".join(problems))
    core_ref = _ct.SystemRelease.from_json(activity.objects.get(activity.release_ref)).core_ref
    refs: dict = {}
    if "task_plan" in tables:
        t = _only(tables["task_plan"], TASK_PLAN_KEYS, "task_plan")
        gate = GatePlan(**dict(t.get("gate") or {}))
        # A new plan carries all five hard limits, and one missing is refused here, not frozen short (M0 §2, v0.3
        # "缺一项拒绝冻结"): an old plan without the two dimensions keeps its identity and is read as frozen, but
        # nothing new is frozen without them. An unknown limit name is refused by name, before Budget sees it.
        overrides = dict(t.get("per_attempt_budget") or {})
        unknown = sorted(set(overrides) - set(Budget.__dataclass_fields__))
        if unknown:
            raise ValueError(f"freeze refused: [freeze.task_plan] per_attempt_budget does not take {unknown}; it "
                             f"takes {sorted(Budget.__dataclass_fields__)}")
        problems = limit_problems(Budget(**{**asdict(activity.bench.budget), **overrides}))
        if problems:
            raise ValueError("freeze refused: " + "; ".join(problems))
        refs["task_plan_ref"] = freeze_task_plan(
            activity.objects, core_ref=core_ref, split_ref=activity.splits.hash(),
            task_ids=_task_ids(activity, t.get("tasks", "dev"), "task_plan"), gate=gate,
            repeats=int(t.get("repeats", 1)), order_seed=int(t.get("order_seed", 0)),
            screen_ids=tuple(t.get("screen") or ()), installs=bool(t.get("installs", True)),
            # Every per-attempt limit is part of the plan: one not written here would be read from [bench.budget]
            # at run time, and a change to it would finish an open comparison under other limits (M5 §3).
            per_attempt_budget={**{k: v for k, v in asdict(activity.bench.budget).items() if v is not None},
                                **dict(t.get("per_attempt_budget") or {})})
    if "schedule" in tables:
        schedule = Schedule(**dict(tables["schedule"]))       # an unknown key is a TypeError here, on purpose
        refs["schedule_ref"] = activity.objects.put_json("schedule", asdict(schedule))
    if "budget" in tables:
        limits = _only(tables["budget"], set(LIMITS) | {"final"}, "budget")
        frozen = {k: float(v) for k, v in limits.items() if k != "final"}
        if limits.get("final"):
            # The final comparison's share, set aside on the root: the cycle cannot spend it, and the final spends
            # nothing else (docs/ROADMAP.md §3).
            final = _only(dict(limits["final"]), set(LIMITS), "budget.final")
            frozen["allocations"] = {"final": {k: float(v) for k, v in final.items()}}
        problems = budget_problems(frozen, pricing_of(cfg))
        if problems:
            raise ValueError("freeze refused: " + "; ".join(problems))
        refs["budget_ref"] = activity.objects.put_json("budget", frozen)
    if "final" in tables:
        t = _only(tables["final"], FINAL_KEYS, "final")
        plan = FinalizationPlan(
            experiment_id=str(t.get("experiment_id") or ""), core_ref=core_ref, split_ref=activity.splits.hash(),
            claims=tuple(t.get("claims") or ("C1", "C2")), comparisons=tuple(dict(c) for c in t.get("comparisons")
                                                                             or ()),
            release_refs=dict(t.get("release_refs") or {}),
            task_ids=_task_ids(activity, t.get("tasks", "test"), "final"), repeats=int(t.get("repeats", 1)),
            statistical_plan=dict(t.get("statistical_plan") or {}), stop_rules=dict(t.get("stop_rules") or {}),
            research_campaign_plan_ref=t.get("research_campaign_plan_ref"))
        if not plan.experiment_id:
            raise ValueError("[freeze.final] needs an experiment_id: it is what a resumed run finds its state by")
        refs["final_plan_ref"] = seal(plan, activity.root, activity.objects)["plan"]
    out("frozen     name these under [rsi]:")
    out("[rsi]")
    for key, ref in refs.items():
        out(f'{key} = "{ref}"')
    return refs
