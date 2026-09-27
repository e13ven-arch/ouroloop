"""The phases of `ouroloop bench`: what has to hold before a benchmark number means anything, and the run that
produces one (docs/ROADMAP.md M1).

`benchmark.py` holds the records a phase writes (splits, run identity, task results); this module is the
sequence that writes them, and `adapters/automationbench.py` is the binding to the upstream environment.

    smoke       what upstream this is, the frozen task groups, the adapter contract checks, the input audit, and
                a few real attempts on development tasks. It makes no claim about capability: it says the runner
                measures what upstream measures, and shows what one attempt looks like end to end.

M2 to M6 (baseline, collect, improve, evaluate, report) are not implemented; the phase names are refused with a
pointer rather than quietly doing something else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config as config_mod
from .adapters import automationbench as ab
from .benchmark import Budget, Results, RunManifest, SplitManifest, assign_splits, digest, summarise
from .ledger import redact

PHASES = ("smoke", "baseline", "collect", "improve", "evaluate", "report")
IMPLEMENTED = ("smoke",)
NOT_YET = {"baseline": "M2", "collect": "M3", "improve": "M5", "evaluate": "M6", "report": "M6"}
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
    if phase in NOT_YET:
        raise NotImplementedError(f"the {phase!r} phase is {NOT_YET[phase]} in docs/ROADMAP.md and is not "
                                  f"implemented; implemented: {list(IMPLEMENTED)}")
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}: one of {list(PHASES)}")
    return smoke(cfg, base, **kw)
