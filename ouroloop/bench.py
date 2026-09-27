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
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import config as config_mod
from .adapters import automationbench as ab
from .benchmark import Budget, Results, RunManifest, SplitManifest, assign_splits, digest, summarise
from .ledger import now, redact, write_json

PHASES = ("smoke", "baseline", "collect", "improve", "research", "evaluate", "report")
IMPLEMENTED = PHASES
# Every phase is wired; what a phase needs is checked before it starts (a frozen plan, a model table, two
# frontier configurations), and a missing piece is named rather than quietly skipped (BR-01).
NOT_YET: dict[str, str] = {}
BENCH_KEYS = {"domain", "upstream_path", "toolset", "seed", "ratios", "budget", "families", "smoke_tasks",
              "near_duplicate_threshold", "repeats"}
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

    if run_agent and chosen:
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
                     run=run, splits=manifest, results=results, repeats=bench.repeats, on_result=show)
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
              "evaluate": evaluate, "report": report}[phase]
    allowed = {"baseline": {"out", "limit", "tasks"}, "collect": {"out", "repeats", "tasks"},
               "improve": {"out", "rounds", "researcher"},
               "research": {"out", "researcher", "campaign_ref", "r_candidate_ref"},
               "evaluate": {"out", "plan_ref", "resume"}, "report": {"out", "write"}}[phase]
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
from .evolve import feedback as _feedback
from .evolve.build import build_task_dataset as _build_task_dataset
from .evolve.execution import Budgets, Schedule
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
    _plans: dict = _field(default_factory=dict)
    _experiment_ref: str = ""

    # -------------------------------------------------------------- pieces
    def services(self) -> _P.Services:
        return _P.Services(self.objects, runtime=self.rt, serving=self.serving)

    def families(self) -> dict:
        return {e.task_id: e.family_id for e in self.splits.entries}

    def plan(self, candidate_ref: str, control_ref: str, parent_release: str) -> TaskEvalPlan:
        return TaskEvalPlan(candidate_ref=candidate_ref, control_ref=control_ref, parent_release=parent_release,
                            core_ref=self.core_ref, split_ref=self.splits.hash(), task_ids=tuple(self.dev_tasks),
                            repeats=self.repeats, order_seed=self.order_seed,
                            per_attempt_budget={"max_steps": self.attempt_budget.max_steps,
                                                "max_tool_calls": self.attempt_budget.max_tool_calls},
                            retry_policy={"max_infrastructure_retries": 1}, screen_ids=tuple(self.screen_ids),
                            gate_plan=self.gate.to_json(), installs=self.plan_installs)

    def run_arm(self, arm: str, release_ref: str, order, *, plan_ref: str, split: str = "dev",
                candidate: str = "candidate", role: str = "evolved", probe: bool = False,
                permit: str | None = None, scope: str | None = None) -> list:
        """One arm's attempts. Each arm is assembled from its own release, with its own session, tool environment
        and episode; nothing mutable is shared between the two sides (docs/modules/03_EVALUATION_RELEASE.md §2)."""
        from .evolve.task_eval import make_agent_factory, run_tasks
        if permit is not None:
            self.releases.authorize(release_ref, profile="evaluation", plan_ref=plan_ref,
                                    scope_id=scope or self.scope_id)
        else:
            self.releases.authorize(release_ref, profile="task")
        bound = _P.bind_release(release_ref, profile="evaluation" if permit else "task",
                                services=self.services(), authorization=permit or "installed")
        run = RunManifest(candidate, role, split, model_identity(self.provider, {}),
                          prompt_hash=digest(bound.system_prompt("")[1])[:16],
                          environment=self.rt_environment(), budget=self.attempt_budget,
                          data=self.splits.hash(), repeats=self.repeats,
                          system_release_ref=release_ref,
                          worker_ref=self.objects.load(release_ref, _ct.SystemRelease).worker_ref,
                          research_ref=self.objects.load(release_ref, _ct.SystemRelease).research_ref,
                          core_ref=self.core_ref, eval_plan_ref=plan_ref, budget_scope=self.scope_id,
                          input_schema_refs=(self.stop_view,))
        if self.results is not None:
            self.results.put_run(run)
        factory = make_agent_factory(self.rt, self.provider, budget=self.attempt_budget, bound=bound,
                                     objects=self.objects, stop_view=self.stop_view,
                                     meta={"arm": arm, "scope": self.scope_id})
        return run_tasks(self.suite, [t for t, _ in order], factory, run=run, splits=self.splits,
                         results=self.results, probe=probe, arm=arm, plan_ref=plan_ref,
                         release_ref=release_ref, order=list(order))

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
                             budget=self.budget.remaining() if self.budget else {},
                             targets={**{t: {"binding": r.binding, "kinds": list(r.allowed_kinds),
                                            "site": r.site}
                                         for t, r in _P.REGISTRY.items() if r.owner == self.owner},
                                      **self.training_options()})

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
        return {"build": self._build, "screen": self._screen, "evaluate": self._evaluate,
                "install": self._install, "verify_bound": self._verify_bound()}

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
        parent = self.releases.active()["release_ref"]
        plan = self.plan(candidate_ref, self.objects.load(parent, _ct.SystemRelease).worker_ref, parent)
        plan_ref = self.objects.put(plan)
        permit = self.releases.permit(candidate_ref, parent, plan_ref, self.scope_id, self.core_ref)
        arm_release = self.releases.candidate_release(parent, candidate_ref, permit)
        order = [(t, 0) for t in self.screen_ids]
        subject = self.run_arm("screen_subject", arm_release, order, plan_ref=plan_ref, permit=permit)
        control = self.run_arm("screen_control", parent, order, plan_ref=plan_ref)
        return _screen(plan, subject, control)

    def _evaluate(self, candidate_ref: str, budget) -> _ct.EvaluationReceipt:
        parent = self.releases.active()["release_ref"]
        control_set = self.objects.load(parent, _ct.SystemRelease).worker_ref if self.owner == "worker" else \
            self.objects.load(parent, _ct.SystemRelease).research_ref
        plan = self.plan(candidate_ref, control_set, parent)
        plan_ref = self.objects.put(plan)
        permit = self.releases.permit(candidate_ref, parent, plan_ref, self.scope_id, self.core_ref)
        arm_release = self.releases.candidate_release(parent, candidate_ref, permit)
        self._plans[candidate_ref] = (plan_ref, arm_release)

        def run(arm, order):
            if arm == "subject":
                return self.run_arm(arm, arm_release, order, plan_ref=plan_ref, permit=permit)
            return self.run_arm(arm, parent, order, plan_ref=plan_ref, candidate="control", role="base")

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
        """Assemble the release that is about to go live, in isolation, and probe what it loads."""
        bound = _P.bind_release(target_release, profile="preflight", services=self.services())
        return {"release": target_release, "policies": _P.describe(bound),
                "prompt": bound.system_prompt("")[1]}


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
        core_ref = rsi.core_ref or _P.default_core(
            objects, domain=bench.domain, split_ref=manifest.hash(),
            adapter="ouroloop.adapters.automationbench", scorer="automationbench.rubric",
            toolset=bench.toolset,
            capabilities={"training": bool(cfg.get("trainer"))})
        release_ref = releases.bootstrap(_P.default_worker_set(objects), _P.default_research_set(objects),
                                        core_ref, reason="initial version from the configuration")
        bootstrapped = True
        out(f"release    {_ct.short(release_ref)} bootstrapped (W0, R0, K); no comparison has been made yet")
    if release_ref is None:
        raise ValueError("nothing is installed and bootstrap is off: freeze an initial release first")
    out(f"release    {_ct.short(release_ref)} active, core {_ct.short(_ct.SystemRelease.from_json(objects.get(release_ref)).core_ref)}")
    for line in drift:
        out(f"DRIFT      {line}")
    return Activity(bench, rsi, suite, manifest, drift, root, results, rt, objects, releases, versions,
                    release_ref, Budgets(root), bootstrapped)


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


def improvement(activity: Activity, *, template_ref: str | None = None, provider=None, trainer=None,
                scope_id: str | None = None, owner: str = "worker", installs: bool | None = None) -> Improvement:
    """The activity's Improvement, with its comparison policy read from the frozen template."""
    template = TaskEvalPlan.from_json(activity.objects.get(template_ref)) if template_ref else None
    gate = template.gate() if template and template.gate_plan else GatePlan()
    tasks = template.task_ids if template else tuple(activity.splits.ids("dev"))
    attempt = activity.bench.budget
    if template and template.per_attempt_budget:
        attempt = Budget(**{**asdict(activity.bench.budget), **template.per_attempt_budget})
    return Improvement(
        rt=activity.rt, suite=activity.suite, splits=activity.splits, objects=activity.objects,
        releases=activity.releases, provider=provider, core_ref=_ct.SystemRelease.from_json(
            activity.objects.get(activity.release_ref)).core_ref,
        gate=gate, dev_tasks=tasks, scope_id=scope_id or activity.rsi.scope_id,
        repeats=template.repeats if template else activity.rsi.repeats,
        order_seed=template.order_seed if template else activity.rsi.order_seed,
        screen_ids=template.screen_ids if template else (), serving=activity.rt.serving, trainer=trainer,
        results=activity.results, stop_view=activity.rsi.stop_view, attempt_budget=attempt,
        probe_train=True, train_tasks=tuple(activity.splits.ids("train")), owner=owner,
        plan_installs=template.installs if template and installs is None else (True if installs is None
                                                                               else installs))


def require(problems: list, *, phase: str) -> None:
    """Refuse a phase whose preconditions do not hold, naming every one of them."""
    if problems:
        raise ValueError(f"the {phase} phase cannot start: " + "; ".join(problems))


# ---------------------------------------------------------------------------- baseline
FRONTIER_KEYS = {"llm", "effort", "fallback", "source", "date", "note"}


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
    for candidate, role, llm in arms:
        try:
            provider = config_mod.build_provider(llm)
        except Exception as e:  # noqa: BLE001 - an unavailable model is an absence, never a zero
            report["absent"].append({"arm": candidate, "reason": f"{type(e).__name__}: {e}"[:200]})
            out(f"ABSENT     {candidate}: {type(e).__name__}: {e}"[:160])
            continue
        work = improvement(activity, provider=provider)
        order = [(t, i) for t in dev for i in range(work.repeats)]
        rows = work.run_arm(candidate, activity.release_ref, order, plan_ref="", candidate=candidate, role=role)
        summary = summarise(rows)
        report["arms"][candidate] = {"role": role, "summary": summary,
                                     "model": model_identity(provider, llm),
                                     "terminations": summary["terminations"],
                                     "failures": {r.task_id: r.failure_class for r in rows if not r.success}}
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
    work = improvement(activity, provider=config_mod.build_provider(cfg["llm"]))
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
            researcher=None) -> dict:
    """The worker loop with a fixed R: collect, diagnose, propose, build, evaluate, install, repeat.

    Every round starts from what the previous one left — the installed release, the appended experience and the
    results that produced it — which is what makes this a loop rather than a series of independent trials.
    """
    from .research.loop import RoundContext, run_worker_rounds
    activity = open_activity(cfg, base, out=out)
    missing = activity.rsi.check(activity.objects, needs=("task_plan_ref",))
    if missing:
        raise ValueError("; ".join(missing) + "; freeze one with bench.freeze_task_plan")
    require(startup_checks(activity, cfg, phase="improve"), phase="improve")
    provider = config_mod.build_provider(cfg["llm"])
    trainer = config_mod.build_trainer(cfg, activity.rt.serving) if cfg.get("trainer") else None
    work = improvement(activity, template_ref=activity.rsi.task_plan_ref, provider=provider, trainer=trainer)
    schedule = Schedule(**(activity.objects.get(activity.rsi.schedule_ref) if activity.rsi.schedule_ref
                           else {"worker_rounds": rounds or 1}))
    limits = activity.objects.get(activity.rsi.budget_ref) if activity.rsi.budget_ref else {}
    budget = activity.budgets.account(activity.rsi.scope_id, limits)
    work.budget = budget
    researcher = researcher or _default_researcher(cfg, activity)
    history: list = []
    log: list = []

    def make_round(index: int) -> RoundContext:
        release_ref = activity.releases.active()["release_ref"]
        evidence = work.collect()
        experience, experience_ref = __import__("ouroloop.evolve.experience", fromlist=["x"]).snapshot(
            activity.root)
        context = work.context(goal="improve the full-task pass rate on the development tasks",
                               release_ref=release_ref, evidence=evidence, experience=experience,
                               experience_ref=experience_ref)
        bound = _P.bind_release(release_ref, profile="task", services=work.services(),
                                authorization="installed")
        log.append({"round": index, "release": release_ref, "feedback": evidence["feedback"].summary()})
        return RoundContext(releases=activity.releases, objects=activity.objects, scope_id=activity.rsi.scope_id,
                            context=context, researcher=researcher, bound=bound, core_ref=work.core_ref,
                            eval_template_ref=activity.rsi.task_plan_ref, handlers=work.handlers(),
                            experience_ref=experience_ref, experience=tuple(experience), owner="worker",
                            schedule=schedule, history=tuple(history), root=activity.root)

    results = run_worker_rounds(make_round, budget, schedule=schedule, root=activity.root)
    for result in results:
        out(f"round      {result.outcome}"
            + (f" experiment={result.experiment_id}" if result.experiment_id else "")
            + (f" release {_ct.short(result.release_before)} -> {_ct.short(result.release_after)}"
               if result.release_after and result.release_after != result.release_before else "")
            + (f" ({result.reason})" if result.reason else ""))
    return {"rounds": [vars(r) for r in results], "log": log, "budget": budget.remaining(),
            "release": activity.releases.active()["release_ref"]}


def _default_researcher(cfg: dict, activity: Activity):
    from .research.basic import PolicyResearcher
    if "llm" not in cfg:
        raise ValueError("a researcher needs an [llm] table, or pass one in")
    table = dict(cfg.get("research") or {}).get("llm") or cfg["llm"]
    provider = config_mod.build_provider(table)
    return PolicyResearcher(_Completions(provider))


class _Completions:
    """A chat provider as the plain completion interface a research step uses."""

    def __init__(self, provider):
        self.provider = provider
        self.name = getattr(provider, "name", "llm")

    def complete(self, system: str, prompt: str) -> str:
        turn = self.provider.chat(system, [{"role": "user", "content": prompt}], ())
        return turn.text or ""


# ---------------------------------------------------------------------------- research (M6)
def research(cfg: dict, base: str | Path = ".", *, out: Callable[[str], None] = print, researcher=None,
             r_candidate_ref: str | None = None, campaign_ref: str | None = None) -> dict:
    """Compare two research policies by the task policies they produce, then install the winner's R.

    Each arm runs the ordinary worker loop in its own workspace, with research slots disabled, from the same W0
    and the same starting experience under an equal fixed budget. The arms cannot read each other or the outer
    tasks; the evaluator runs both sealed W* on the outer pool.
    """
    from .research.campaign import CampaignPair, CampaignPlan, arm_workspace, run_campaign
    activity = open_activity(cfg, base, out=out)
    ref = campaign_ref or activity.rsi.research_plan_ref
    if not ref or not activity.objects.has(ref):
        raise ValueError("[rsi] research_plan_ref must name a frozen CampaignPlan (research.campaign)")
    plan = CampaignPlan.from_json(activity.objects.get(ref))
    if r_candidate_ref and r_candidate_ref != plan.r_candidate_ref:
        raise ValueError("the candidate R is part of the frozen plan and cannot be replaced on the command line")
    require(startup_checks(activity, cfg, phase="research"), phase="research")
    provider = config_mod.build_provider(cfg["llm"])
    limits = activity.objects.get(activity.rsi.budget_ref) if activity.rsi.budget_ref else {}
    budget = activity.budgets.account(f"campaign:{plan.id}", limits)
    out(f"campaign   {plan.id} phase={plan.phase} pairs={len(plan.campaign_pairs)} "
        f"discovery={plan.discovery} R0={_ct.short(plan.r_control_ref)} R1={_ct.short(plan.r_candidate_ref)}")

    def make_researcher():
        """A fresh research step per arm: its recorded requests are then that arm's own, which is how a fixture
        tells R0's calls from R1's (docs/modules/06_RESEARCH_CAMPAIGN.md §5)."""
        return researcher() if callable(researcher) else (researcher or _default_researcher(cfg, activity))

    def run_arm(pair: CampaignPair, arm: str, r_ref: str, arm_budget) -> object:
        return _run_campaign_arm(activity, plan, pair, arm, r_ref, arm_budget, provider=provider,
                                 researcher=make_researcher(), out=out)

    def evaluate_outer(pair: CampaignPair, sealed: dict) -> dict:
        return _evaluate_outer(activity, plan, pair, sealed, provider=provider, out=out)

    receipt = run_campaign(plan, run_arm=run_arm, evaluate_outer=evaluate_outer, objects=activity.objects,
                           budget=budget, events=_ct.Events(activity.root))
    receipt_ref = activity.objects.put(receipt)
    activity.releases.register_receipt(receipt_ref, plan=receipt.plan_ref, subject=plan.r_candidate_ref,
                                      verdict=receipt.verdict, profile="research")
    out(f"verdict    {receipt.verdict}: {'; '.join(receipt.reasons)[:200]}")
    out(f"metrics    {receipt.metrics}")
    installation = None
    if receipt.verdict == "accepted" and plan.phase != "final":
        installation = activity.releases.install(
            receipt_ref, activity.releases.active()["release_ref"],
            f"install-r-{_ct.digest({'receipt': receipt_ref})[:12]}",
            preflight=lambda target: {"release": target,
                                      "policies": _P.describe(_P.bind_release(
                                          target, profile="preflight",
                                          services=_P.Services(activity.objects, serving=activity.rt.serving)))})
        out(f"install    {installation.status} {_ct.short(installation.target_release)}"
            + (f" ({installation.reason})" if installation.reason else ""))
    return {"receipt": receipt_ref, "verdict": receipt.verdict, "metrics": receipt.metrics,
            "uncertainty": receipt.uncertainty, "costs": receipt.costs,
            "installation": None if installation is None else vars(installation),
            "release": activity.releases.active()["release_ref"]}


def _run_campaign_arm(activity: Activity, plan, pair, arm: str, r_ref: str, arm_budget, *, provider,
                      researcher, out=print):
    """One arm's whole improvement loop, in its own workspace.

    The arm gets the pair's W0, its own R, the shared core and nothing else: no mainline pointer, no other arm's
    experience, no outer task. Its W* is whatever it actually installed (RC-02: an arm that installed nothing
    still returns W0 and its full costs).
    """
    from .research.campaign import ArmOutcome, arm_workspace
    from .research.loop import RoundContext, run_worker_rounds
    root = arm_workspace(activity.root, pair.pair_id, arm)
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
                        stop_view=activity.rsi.stop_view, attempt_budget=activity.bench.budget,
                        probe_train=True, train_tasks=tuple(_tasks_of(activity, pair.inner_train_groups)))
    step = researcher
    schedule = Schedule(worker_rounds=plan.worker_round_limit, research_slots=0, patience=plan.worker_round_limit)
    history: list = []
    outcome = ArmOutcome(arm=arm, pair_id=pair.pair_id)

    def make_round(index: int) -> RoundContext:
        release_ref = releases.active()["release_ref"]
        evidence = inner.collect()
        experience, experience_ref = __import__("ouroloop.evolve.experience", fromlist=["x"]).snapshot(root)
        context = inner.context(goal="improve the full-task pass rate on this campaign's development tasks",
                               release_ref=release_ref, evidence=evidence, experience=experience,
                               experience_ref=experience_ref)
        bound = _P.bind_release(release_ref, profile="task", services=inner.services(),
                                authorization="installed")
        return RoundContext(releases=releases, objects=objects, scope_id=inner.scope_id, context=context,
                            researcher=step, bound=bound, core_ref=core_ref,
                            eval_template_ref=activity.rsi.task_plan_ref or "", handlers=inner.handlers(),
                            experience_ref=experience_ref, experience=tuple(experience), owner="worker",
                            schedule=schedule, history=tuple(history), root=root)

    rounds = run_worker_rounds(make_round, arm_budget, schedule=schedule, root=root)
    outcome.rounds = tuple({"outcome": r.outcome, "experiment": r.experiment_id, "reason": r.reason,
                            "release_after": r.release_after} for r in rounds)
    outcome.installed = tuple(r.release_after for r in rounds if r.outcome == "installed" and r.release_after)
    outcome.stop_reason = next((r.reason for r in reversed(rounds) if r.outcome == "stopped"), "rounds_done")
    outcome.costs = arm_budget.remaining()
    outcome.research_requests = tuple(dict(r) for r in getattr(step, "requests", ()))
    installed = releases.active()["release_ref"]
    outcome.w_star = _ct.SystemRelease.from_json(objects.get(installed)).worker_ref if outcome.installed \
        else pair.initial_w_ref
    _ct.copy_closure(objects, activity.objects, [outcome.w_star])
    out(f"arm        {pair.pair_id}/{arm}: {len(outcome.installed)} installed, W*={_ct.short(outcome.w_star)}, "
        f"stop={outcome.stop_reason}")
    return outcome


def _evaluate_outer(activity: Activity, plan, pair, sealed: dict, *, provider, out=print) -> dict:
    """The evaluator's own run: both sealed W* on the same outer tasks, with the same repeats and order."""
    parent = activity.release_ref
    core_ref = _ct.SystemRelease.from_json(activity.objects.get(parent)).core_ref
    tasks = _tasks_of(activity, pair.outer_groups)
    order = [(t, i) for t in tasks for i in range(pair.repeats)]
    work = improvement(activity, provider=provider, scope_id=f"{plan.id}:{pair.pair_id}:outer", installs=False)
    work.dev_tasks = tuple(tasks)
    rows = {}
    for arm, w_star in sealed.items():
        plan_ref = activity.objects.put(work.plan(w_star, w_star, parent))
        permit = activity.releases.permit(w_star, parent, plan_ref, f"{plan.id}:{pair.pair_id}:outer", core_ref)
        release = activity.releases.candidate_release(parent, w_star, permit)
        rows[arm] = work.run_arm(f"outer_{arm}", release, order, plan_ref=plan_ref, split=_split_of(activity,
                                                                                                   tasks),
                                candidate=f"{arm}_w_star", role="evolved", permit=permit,
                                scope=f"{plan.id}:{pair.pair_id}:outer")
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
    """
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
        work = improvement(activity, provider=provider, scope_id=f"final:{plan.experiment_id}", installs=False)
        work.dev_tasks = tuple(plan.task_ids)
        order = [(t, i) for t in plan.task_ids for i in range(plan.repeats)]
        attempts = work.run_arm(arm, release_ref, order, plan_ref=ref, split="test", candidate=arm,
                                role=comparison.get("role", arm))
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


def research_slot(cfg: dict, base: str | Path = ".", *, pairs, researcher, arm_researcher=None,
                  out: Callable[[str], None] = print, worker_rounds: int = 1, gate: GatePlan | None = None,
                  arm_budget: dict | None = None, phase: str = "research_dev",
                  discovery: str = "scripted") -> dict:
    """One research slot: the current R proposes a change to R, the change is compared, and the winner is installed.

    The proposal goes through the same admission and the same build as a worker candidate — only the owner and
    the evaluator differ (docs/modules/04_RESEARCH_LOOP.md §4). The campaign plan is frozen from `pairs` once R1
    exists, because a plan naming a candidate that does not exist yet could not be checked; nothing about the
    thresholds or the task pools is decided after the arms run.

    `discovery` travels into the receipt: a change a person wrote exercises this path and is not evidence that
    the system found it (RC-10).
    """
    from .research.campaign import CampaignPlan
    from .research.experiment import admit, freeze, propose_experiment
    from .research.campaign import CampaignPair  # noqa: F401 - pairs are already CampaignPair objects
    activity = open_activity(cfg, base, out=out)
    release_ref = activity.releases.active()["release_ref"]
    provider = config_mod.build_provider(cfg["llm"])
    work = improvement(activity, template_ref=activity.rsi.task_plan_ref, provider=provider, owner="research")
    evidence = work.collect()
    experience, experience_ref = __import__("ouroloop.evolve.experience", fromlist=["x"]).snapshot(activity.root)
    context = work.context(goal="improve how this research policy finds and verifies changes",
                           release_ref=release_ref, evidence=evidence, experience=experience,
                           experience_ref=experience_ref)
    bound = _P.bind_release(release_ref, profile="task", services=work.services(), authorization="installed")
    proposal_result = propose_experiment(context, bound, "research", researcher=researcher,
                                         objects=activity.objects, experience=experience)
    if not proposal_result.proposals:
        out(f"slot       no research proposal ({proposal_result.next_action}: {proposal_result.reason})")
        return {"outcome": "no_proposal", "reason": proposal_result.reason, "release": release_ref}
    core = activity.objects.load(work.core_ref, _ct.CoreManifest)
    admission = admit(proposal_result.proposals[0], context=context, core=core, objects=activity.objects,
                      owner="research", checks=tuple(bound.preflight or ()))
    if not admission.ok:
        out(f"slot       {admission.status}: {'; '.join(admission.reasons)[:200]}")
        return {"outcome": admission.status, "reasons": admission.reasons, "release": release_ref}
    experiment = freeze(admission.proposal_ref, objects=activity.objects, parent_release=release_ref,
                        experience_ref=experience_ref, core_ref=work.core_ref,
                        eval_template_ref=activity.rsi.task_plan_ref or "", budget_scope=activity.rsi.scope_id)
    activity.objects.put(experiment)
    build = _P.build_candidate(experiment, objects=activity.objects, parent_release=release_ref, owner="research",
                              services=work.services())
    if build.status != "ready":
        out(f"slot       build {build.status}: {'; '.join(build.errors)[:200]}")
        return {"outcome": build.status, "reasons": list(build.errors), "release": release_ref}
    r0 = _ct.SystemRelease.from_json(activity.objects.get(release_ref)).research_ref
    out(f"slot       R1 {_ct.short(build.candidate_ref)} proposed by R0 {_ct.short(r0)}")
    plan = CampaignPlan(id=f"campaign-{_ct.digest({'r1': build.candidate_ref})[:10]}", phase=phase,
                        core_ref=work.core_ref, r_control_ref=r0, r_candidate_ref=build.candidate_ref,
                        campaign_pairs=tuple(activity.objects.put(p) for p in pairs),
                        arm_budget=dict(arm_budget or {"model_calls": 2000, "tool_calls": 2000}),
                        worker_round_limit=worker_rounds,
                        worker_gate_plan_ref=activity.objects.put(gate or GatePlan(min_groups=1, min_gain=0.01,
                                                                                  bootstrap_replicates=200)),
                        research_gate_plan=(gate or GatePlan(min_groups=1, min_gain=0.05,
                                                            bootstrap_replicates=200)).to_json(),
                        selection_rule="last_installed", discovery=discovery)
    plan_ref = activity.objects.put(plan)
    cfg = {**cfg, "rsi": {**cfg.get("rsi", {}), "research_plan_ref": plan_ref}}
    outcome = research(cfg, base, out=out, researcher=arm_researcher or researcher, campaign_ref=plan_ref)
    return {"outcome": outcome["verdict"], "r1": build.candidate_ref, "campaign": plan_ref,
            "experiment": experiment.id, **outcome}


# ---------------------------------------------------------------------------- startup checks
PRICING_KEYS = {"input_per_1k", "output_per_1k", "max_input_tokens", "max_output_tokens", "declared_fallbacks",
                "hidden_fallback", "source"}


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
    budget = activity.bench.budget
    if not (budget.max_steps and budget.max_tool_calls and budget.max_seconds):
        problems.append("[bench.budget] needs a step, tool-call and time limit per attempt")
    limits = activity.objects.get(rsi_cfg.budget_ref) if rsi_cfg.budget_ref else {}
    if limits:
        reason = check_pricing(pricing_of(cfg), limits)
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
        _P.bind_release(activity.release_ref, profile="preflight",
                        services=_P.Services(activity.objects, serving=activity.rt.serving))
        activity.releases.authorize(activity.release_ref, profile="task")
    except Exception as e:  # noqa: BLE001 - a release that does not assemble is not something to run
        problems.append(f"the active release does not assemble: {type(e).__name__}: {e}"[:300])
    return problems
