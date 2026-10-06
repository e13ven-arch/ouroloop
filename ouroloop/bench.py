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
from .benchmark import (HARD_LIMITS, SPLIT_RULES, V03_BUDGET_FIELDS, Budget, Results, RunManifest, SplitManifest,
                        assign_splits, digest, pool_manifests, stratified_splits, summarise, task_domain)
from .ledger import now, redact, write_json

# The seven phases, and `cycle`: the thin automatic entry that calls them in the frozen order (M7 §1).
# v0.5 (docs/design/V0_5_PLAN.md §3): `library` (the experience library's stream and snapshot round) and `learn`
# (the method table's learn reading and refit round), train only, run by hand in the order the plan names.
PHASES = ("smoke", "baseline", "collect", "improve", "research", "evaluate", "report", "cycle", "library", "learn")
IMPLEMENTED = PHASES
# Every phase is wired; what a phase needs is checked before it starts (a frozen plan, a model table, two
# frontier configurations), and a missing piece is named rather than quietly skipped (BR-01).
NOT_YET: dict[str, str] = {}
BENCH_KEYS = {"domain", "upstream_path", "toolset", "seed", "ratios", "budget", "families", "smoke_tasks",
              "near_duplicate_threshold", "repeats", "concurrency",
              # v0.3 step 5 (docs/modules/07_BENCH_REPORT.md §2): the domain list, how a domain without a frozen
              # split is split, the domains whose split is pinned to a hash, and the domains whose test is spent
              "domains", "split_rule", "frozen_splits", "spent",
              # v0.4 (docs/design/V0_4_PLAN.md §1.6): how many W0 dev readings the baseline takes for the pooled control
              "control_readings"}
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
    # v0.3 step 5 (M7 §2): the domains, in pooled order. `domain` is derived from them: the one domain's name, or
    # the pooled name `a+b+...` — the directory the activity's results, runs and pooled split live under.
    domains: tuple[str, ...] = ()
    split_rule: str = "hash"        # for a domain with no frozen split; a frozen one is kept whatever this says
    frozen_splits: dict[str, str] = field(default_factory=dict)    # domain -> the hash its hash-rule split must have
    spent: tuple[str, ...] = ()     # domains whose test is spent: a new claim's final never reads it
    # v0.4 (V0_4_PLAN §1.6, M3 §2.1(a) v0.4): the baseline reads W0's dev this many times, unconditionally and before
    # any gate, so the pooled control starts with that many readings; 1 is v0.3's one reading
    control_readings: int = 1

    def __post_init__(self) -> None:
        self.domains = tuple(self.domains) or (self.domain,)
        self.domain = pooled_name(self.domains)
        self.spent = tuple(self.spent)
        if isinstance(self.control_readings, bool) or not isinstance(self.control_readings, int) \
                or self.control_readings < 1:
            raise ValueError(f"[bench] control_readings = {self.control_readings!r}: a whole number of W0 dev "
                             f"readings, at least 1")

    @classmethod
    def load(cls, cfg: dict, base: str | Path = ".") -> "BenchConfig":
        table = dict(cfg.get("bench") or {})
        unknown = sorted(set(table) - BENCH_KEYS)
        if unknown:
            raise ValueError(f"[bench] does not take {unknown}; it takes {sorted(BENCH_KEYS)}")
        table.update(_domain_keys(table))
        # [bench.budget] over the preset's per-attempt limits (preset_budget): a preset that carries them is the
        # ceiling, as its harness numbers are — a value here may lower one, never raise one.
        defaults, given = preset_budget(cfg), dict(table.pop("budget", None) or {})
        raised = sorted(k for k, v in given.items()
                        if k in defaults and v is not None and float(v) > float(defaults[k]))
        if raised:
            raise ValueError(f"[bench.budget] {raised} above the preset's {[defaults[k] for k in raised]}: a "
                             f"per-attempt limit may be lowered under a preset, not raised")
        budget = Budget(**{**defaults, **given})
        # The preset's default for the attempts in flight (preset_concurrency); the table's own value stands.
        table.setdefault("concurrency", preset_concurrency(cfg))
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


def pooled_name(domains) -> str:
    """The name an activity's domains go by: the one domain's own (v0.1/v0.2 configs keep every path and hash they
    had), or the list joined by `+` in pooled order — a different list or order is a different pooled split."""
    domains = tuple(domains)
    return domains[0] if len(domains) == 1 else "+".join(domains)


def _domain_keys(table: dict) -> dict:
    """[bench]'s v0.3 step-5 keys, checked (M7 §2): `domains` is a list and `domain` one name, the same thing when
    both are given; a single `domain` reads as a list of one. -> the BenchConfig fields they set."""
    single, listed = table.pop("domain", None), table.pop("domains", None)
    if listed is not None:
        if isinstance(listed, str) or not isinstance(listed, (list, tuple)) or not listed or \
                not all(isinstance(d, str) and d and "+" not in d and "/" not in d for d in listed):
            raise ValueError(f"[bench] domains = {listed!r}: a non-empty list of upstream domain names")
        if len(set(listed)) != len(listed):
            raise ValueError(f"[bench] domains = {list(listed)}: a domain is listed twice")
        if single is not None and [single] != list(listed):
            raise ValueError(f"[bench] domain = {single!r} and domains = {list(listed)}: give one of them (a single "
                             f"domain is a list of one)")
    domains = tuple(listed) if listed is not None else (single or "marketing",)
    rule = table.pop("split_rule", "hash")
    if rule not in SPLIT_RULES:
        raise ValueError(f"[bench] split_rule = {rule!r}: one of {list(SPLIT_RULES)}")
    frozen = dict(table.pop("frozen_splits", None) or {})
    stray = sorted(set(frozen) - set(domains))
    if stray:
        raise ValueError(f"[bench.frozen_splits] names {stray}, which [bench] domains does not list")
    bad = sorted(d for d, h in frozen.items() if not (isinstance(h, str) and re.fullmatch(r"[0-9a-f]{16}", h)))
    if bad:
        raise ValueError(f"[bench.frozen_splits] {bad}: the 16-hex-digit hash of the frozen split manifest")
    spent = table.pop("spent", None) or ()
    if isinstance(spent, str) or not all(isinstance(d, str) for d in spent):
        raise ValueError(f"[bench] spent = {spent!r}: a list of domain names")
    stray = sorted(set(spent) - set(domains))
    if stray:
        raise ValueError(f"[bench] spent names {stray}, which [bench] domains does not list")
    if spent and len(domains) < 2:
        raise ValueError("[bench] spent: a single domain whose test is spent leaves no test for a new claim; list "
                         "it beside the new domains (M7 §2)")
    if spent and set(spent) == set(domains):
        raise ValueError(f"[bench] spent = {list(spent)} covers every listed domain: a new claim needs a new domain's "
                         f"test")
    return {"domains": domains, "split_rule": rule, "frozen_splits": frozen, "spent": tuple(spent)}


# ---------------------------------------------------------------------------- splits
def freeze_splits(suite: ab.Suite, cfg: BenchConfig, path: str | Path, *,
                  replace_existing: bool = False, out: Callable[[str], None] | None = None
                  ) -> tuple[SplitManifest, list[str]]:
    """The frozen task groups of this domain (`suite.domain`), and what upstream now says that they do not.

    A manifest already on disk is the one that is used, whatever the config now asks for: the groups are fixed
    before the first result is looked at, so a changed ratio, seed or split rule is a new experiment
    (docs/DESIGN.md §3.1) — a changed rule is noted, never applied (M7 §2: `split_rule` has no effect on a frozen
    manifest). A new manifest follows [bench] split_rule — "hash" (v0.1/v0.2) or "stratified" over the adapter's
    static strata (`ab.stratum`: assertion kinds × tool family, from the task definitions alone). A domain
    [bench.frozen_splits] pins must reproduce the pinned hash, under the rule that does (v0.4, docs/design/
    V0_4_PLAN.md §1.1): split_rule first, then the other — Marketing's v0.2 pin is a hash-rule split, the v0.4 configs
    pin the stratified finance and support splits the v0.3 workspace froze. The rule and the seed are in the hashed
    manifest, so at most one rule reproduces a pin; neither doing it is refused, as before.
    """
    path = Path(path)
    domain = suite.domain
    # [bench.families] over several domains names tasks of each; a domain is grouped by its own tasks' entries
    declared = cfg.families if len(cfg.domains) < 2 else \
        {t: f for t, f in cfg.families.items() if task_domain(t) == domain}
    fams, rule = ab.families(suite, declared)
    entries = suite.entries(fams)
    pinned = cfg.frozen_splits.get(domain)
    wanted = cfg.split_rule
    if path.exists() and not replace_existing:
        frozen = SplitManifest.load(path)
        if pinned and frozen.hash() != pinned:
            raise ValueError(f"{path} freezes split {frozen.hash()}, and [bench.frozen_splits] pins {domain} to "
                             f"{pinned}: the frozen split is kept as it is, so this workspace is not the pinned one")
        if out is not None and not pinned and (frozen.split_rule or "hash") != wanted:
            out(f"NOTE       {domain}: the frozen split was made under the {frozen.split_rule or 'hash'} rule and is "
                f"kept; [bench] asks for {wanted}, which only a new workspace would use")
        return frozen, frozen.drift(entries)

    def split_under(rule_name: str) -> SplitManifest:
        strata = None
        if rule_name == "stratified":
            members: dict[str, list[str]] = {}
            for tid in suite.ids():
                members.setdefault(fams[tid], []).append(ab.stratum(suite, tid))
            # a declared family of several tasks is one unit: its stratum is the set of its tasks' strata
            splits, strata = stratified_splits({f: ";".join(sorted(set(k))) for f, k in members.items()},
                                               cfg.ratios, cfg.seed)
        else:
            splits = assign_splits(fams.values(), cfg.ratios, cfg.seed)
        return SplitManifest(domain, cfg.environment(suite.versions), rule, entries, splits, cfg.ratios,
                             cfg.seed, split_rule="stratified" if strata is not None else None, strata=strata)
    manifest = split_under(wanted)
    if pinned and manifest.hash() != pinned:
        other = split_under(next(r for r in SPLIT_RULES if r != wanted))
        manifest = other if other.hash() == pinned else manifest
    if pinned and manifest.hash() != pinned:
        raise ValueError(f"neither split rule reproduces {domain}'s pin: the {wanted} rule splits it into "
                         f"{manifest.hash()} here, and [bench.frozen_splits] pins "
                         f"{pinned}: upstream or this environment (commit, package, python) changed since it was "
                         f"frozen. Copy the frozen manifest from the workspace that froze it into {path}, or pin the "
                         f"new hash in a new activity")
    manifest.freeze(path, replace_existing=replace_existing)
    return manifest, []


def load_domains(bench: BenchConfig, root, *, replace_existing: bool = False,
                 out: Callable[[str], None] | None = None) -> tuple[ab.Suite, SplitManifest, list[str]]:
    """Every listed domain loaded from the upstream checkout and its split frozen, and the activity's suite and
    split (M7 §2): for one domain, that domain's own, as before; for several, the suites side by side and the
    domains' frozen manifests pooled in list order (`pool_manifests`), the pooled manifest frozen beside the
    activity's results under the pooled name. -> (suite, manifest, drift). A domain upstream cannot load is
    refused by name."""
    def load(domain: str) -> ab.Suite:
        try:
            return ab.load_suite(domain, bench.upstream_path)
        except ab.UpstreamError:
            raise
        except Exception as e:  # noqa: BLE001 - upstream's loader refuses an unknown domain with a ValueError
            raise ab.UpstreamError(f"[bench] domains lists {domain!r}, which the upstream checkout cannot load: "
                                   f"{type(e).__name__}: {e}"[:400]) from e
    if len(bench.domains) == 1:
        suite = load(bench.domain)
        manifest, drift = freeze_splits(suite, bench, Results(root, bench.domain).manifest_path(),
                                        replace_existing=replace_existing, out=out)
        return suite, manifest, drift
    stray = sorted(t for t in bench.families if task_domain(t) not in bench.domains)
    if stray:
        raise ValueError(f"[bench.families] names tasks of no listed domain: {stray[:5]}")
    suites = [load(d) for d in bench.domains]
    pooled_suite = ab.pool_suites(bench.domain, suites)
    manifests, drift = [], []
    for suite in suites:
        manifest, found = freeze_splits(suite, bench, Results(root, suite.domain).manifest_path(),
                                        replace_existing=replace_existing, out=out)
        manifests.append(manifest)
        drift += found
    pooled = pool_manifests(bench.domain, manifests, ratios=bench.ratios, seed=bench.seed, spent=bench.spent)
    pooled.freeze(Results(root, bench.domain).manifest_path(), replace_existing=replace_existing)
    return pooled_suite, pooled, drift


def domain_lines(manifest: SplitManifest, *, strata: bool = True) -> list[str]:
    """What `ouroloop freeze`/the smoke print per domain (M7 §5): its split hash and rule, its counts, and — with
    `strata` — each stratum's counts."""
    out = []
    spent = manifest.spent()
    for d in manifest.domains():
        counts = "  ".join(f"{s} {c['tasks']}" for s, c in (d.get("counts") or {}).items())
        out.append(f"domain     {d['domain']} split={d['hash']} rule={d.get('split_rule') or 'hash'} "
                   f"seed={d.get('seed')} {counts}" + ("  test: spent" if d["domain"] in spent else ""))
        for key, c in sorted((d.get("strata") or {}).items()) if strata else ():
            out.append(f"           stratum {key}: " + "  ".join(f"{s} {n}" for s, n in c.items()))
    return out


def _one_dev_task_per_domain(manifest: SplitManifest) -> list[str]:
    """The first dev task of each domain in pooled order: what the scorer-contract checks read when an activity
    spans several domains, so each domain's scorer is checked without any model call."""
    first: dict[str, str] = {}
    for t in manifest.ids("dev"):
        first.setdefault(task_domain(t), t)
    return list(first.values())


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

    root = config_mod.workspace_root(cfg, base)
    results = Results(root, bench.domain)
    if replace_splits and results.path.exists():
        raise ValueError(f"{results.path} already holds results of the frozen split: reassigning the groups now "
                         f"would rescore attempts that were made under the old ones. Start a new workspace.")
    # v0.3 step 5 (M7 §1 smoke): every listed domain is loaded from the checkout, and refused by name if it is not
    suite, manifest, drift = load_domains(bench, root, replace_existing=replace_splits, out=out)
    out(f"tasks      {len(suite)} in domain{'s' if len(bench.domains) > 1 else ''} {', '.join(bench.domains)}")
    counts = manifest.counts()
    out(f"splits     {results.manifest_path()} hash={manifest.hash()} rule={manifest.family_rule} "
        f"seed={manifest.seed}")
    out("           " + "  ".join(f"{s}: {c['tasks']} tasks / {c['families']} families"
                                  for s, c in counts.items()))
    if len(bench.domains) > 1 or manifest.split_rule == "stratified":
        for line in domain_lines(manifest):
            out(line)
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
    # Several domains: the contract checks and the input audit read one dev task of every domain (they call no
    # model), while the attempts stay the ones chosen above.
    checked = list(chosen) + ([t for t in _one_dev_task_per_domain(manifest) if t not in chosen]
                              if len(bench.domains) > 1 and chosen else [])

    checks = ab.contract_check(suite, checked)
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
    for tid in checked:
        ep = suite.episode(tid)
        try:
            found = ab.audit_inputs(suite.tasks[tid], suite.grading(tid), ab.agent_payload(suite.tasks[tid], ep))
        finally:
            ep.close()
        audit["leaks"] += found["leaks"]
        audit["overlaps"] += found["overlaps"]
    out(f"audit      {len(checked)} tasks: {len(audit['leaks'])} leaks, {len(audit['overlaps'])} prompt/assertion "
        f"overlaps for review")
    for line in audit["leaks"]:
        out(f"LEAK       {line}")
    for line in audit["overlaps"][:5]:
        out(f"           {line}")

    report = {"upstream": versions, "tasks": len(suite), "splits": {"hash": manifest.hash(), "counts": counts,
                                                                   "drift": drift, "rule": manifest.family_rule,
                                                                   "domains": manifest.domains(),
                                                                   "spent": sorted(manifest.spent())},
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
                                                             "bench": bench.domain},
                                                       max_effort=smoke_max_effort(cfg, base)),
                     run=run, splits=manifest, results=results, repeats=bench.repeats, on_result=show,
                     concurrency=bench.concurrency)
    total = summarise(rows)
    out(f"summary    {total['passed']}/{total['tasks']} tasks passed, partial={total['partial_credit']}, "
        f"invalid={total['invalid']}, cost={'unmeasured' if total['cost_usd'] is None else total['cost_usd']} "
        f"(missing on {total['cost_missing']} attempts), usage={total['usage']}")
    return {"attempts": [r.to_json() for r in rows], "summary": total, "run": run.to_json()}


def smoke_max_effort(cfg: dict, base: str | Path) -> str | None:
    """Which loop the smoke's attempts run (docs/modules/01_POLICY_RUNTIME.md §1): the active release's K fixes
    `max_effort` when the workspace has one — the smoke then runs under the runner shell as the activity's attempts
    do, and under the same ceiling — else the configured first-version table's; None (the v1 loop) when neither
    sets one. A smoke binds no release, so this is read from the workspace directly, not from a bound runtime."""
    from .agent.runner import release_max_effort
    root = config_mod.workspace_root(cfg, base)
    release_ref = Releases(root, _ct.Objects(root)).active().get("release_ref")
    if release_ref:
        return release_max_effort(_ct.Objects(root), release_ref)
    settings = harness_settings(cfg)
    return settings["max_effort"] if settings else None


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
              "evaluate": evaluate, "report": report, "cycle": cycle, "library": library_track, "learn": learn}[phase]
    allowed = {"baseline": {"out", "limit", "tasks"}, "collect": {"out", "repeats", "tasks"},
               "improve": {"out", "rounds", "researcher", "segment", "reason"},
               "research": {"out", "researcher", "campaign_ref", "r_candidate_ref"},
               "evaluate": {"out", "plan_ref", "resume"}, "report": {"out", "write"},
               "cycle": {"out", "researcher", "slot_researcher", "arm_researcher", "until"},
               "library": {"out", "distiller", "merger"}, "learn": {"out"}}[phase]
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
from .evolve.task_gate import refill as _refill
from .evolve.task_gate import refill_attempts as _refill_attempts
from .evolve.task_gate import screen as _screen
from .evolve.task_gate import shortfall as _shortfall
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
    # v0.3 (M3 §2.1(a)(b)): the frozen template's control pooling and confirmation count, carried into every plan
    # this activity freezes for a candidate; None (a template frozen without them) is a fresh control, no confirmation.
    control_pooling: str | None = None
    confirmation_repeats: int | None = None
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
                            gate_plan=self.gate.to_json(), installs=self.plan_installs,
                            control_pooling=self.control_pooling, confirmation_repeats=self.confirmation_repeats)

    def control_pool(self, parent_release: str, *, plan: TaskEvalPlan | None = None, tasks=None) -> list:
        """Every valid reading of the installed W under this comparison's conditions and model identity, pooled
        (docs/modules/03_EVALUATION_RELEASE.md §2.1(a), v0.3): the baseline's dev run, every earlier gate's control
        arm, and the candidate and confirmation arms of a gate whose candidate is this W now.

        A reading belongs when its run manifest is this control arm's manifest less what names an arm — the
        candidate/role labels, the plan ref, the budget scope, the release and R (R takes no part in a run, so a
        release that only changed R is the same W, as `_runs_like` reads it) — so the model identity, the package,
        the per-attempt limits, the repeats, the split and the host contract are all equal; and when its own plan,
        if it ran under one, froze the same conditions as this plan (`TaskEvalPlan.conditions`): the same tasks,
        repeats, seed and limits. The baseline's run has no plan and ran under the frozen template's conditions
        (bench.baseline). Only the plan's tasks (or `tasks`) on the plan's split count, and only valid attempts;
        the screen arms (`screen_subject`/`screen_control`: the screen tasks only, attempt 0, not a reading of the
        plan) never count. The pool is the control's starting point: what it is short of, the gate's control arm
        reads (task_gate.shortfall), so an empty pool is simply the whole plan short.
        """
        plan = plan or self.plan("", "", parent_release)
        if self.results is None or not plan.pooled:
            return []
        wanted = set(tasks if tasks is not None else plan.task_ids)
        split = _split_of_tasks(self.splits, plan.task_ids)
        expected = self.run_identity(parent_release, plan_ref="", split=split, candidate="control", role="base")[0]
        labels = RUN_LABELS
        same = {k: v for k, v in expected.payload().items() if k not in labels}
        conditions = plan.conditions()
        runs: set[str] = set()
        for run_hash, run in self.results.runs().items():
            if {k: v for k, v in run.payload().items() if k not in labels} != same:
                continue
            if run.eval_plan_ref:
                if not self.objects.has(run.eval_plan_ref):
                    continue
                try:
                    other = TaskEvalPlan.from_json(self.objects.get(run.eval_plan_ref))
                except _ct.ContractError:
                    continue
                if other.conditions() != conditions:
                    continue
            runs.add(run_hash)
        return [r for r in self.results.all() if r.run in runs and r.validity == "valid" and r.split == split
                and r.task_id in wanted and _arm_of(r.attempt_id) not in SCREEN_ARMS]

    def run_identity(self, release_ref: str, *, plan_ref: str, split: str, candidate: str, role: str,
                     permit: str | None = None, scope: str | None = None, notes: str = "") -> tuple:
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
                          mode=mode, target_harness_ref=release.target_harness_ref, notes=notes)
        return run, bound, actor

    def run_arm(self, arm: str, release_ref: str, order, *, plan_ref: str, split: str = "dev",
                candidate: str = "candidate", role: str = "evolved", probe: bool = False,
                permit: str | None = None, scope: str | None = None, retry_index: int = 0,
                resume: bool = False, supersedes: dict | None = None, notes: str = "",
                extra_system: dict | None = None) -> list:
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
                                              role=role, permit=permit, scope=scope, notes=notes)
        if split != "train" and bound.package is not None:
            # v0.5 receipt 1 (V0_5_PLAN §1.3): dev and test run a frozen greedy method table only — an explore twin
            # exists for a learn reading on train and is refused here, before anything is paid
            from .research.methods import frozen_table_problems
            problems = frozen_table_problems(bound.package.run_files)
            if problems:
                raise _ct.ContractError(f"{arm} on {split}: " + "; ".join(problems))
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
                                     meta={"arm": arm, "scope": self.scope_id}, worker=self.worker,
                                     # v0.5: a library stream's retry note per task (train only); None otherwise
                                     extra_system=extra_system)
        try:
            rows = run_tasks(self.suite, [t for t, _ in order], factory, run=run, splits=self.splits,
                             results=self.results, probe=probe, arm=arm, plan_ref=plan_ref,
                             release_ref=release_ref, order=order, retry_index=retry_index,
                             pricing=self.pricing, supersedes=supersedes, concurrency=self.concurrency)
        finally:
            refused = getattr(provider, "rate_limited", 0)
            if refused:
                # M5 §2 (v0.3): the provider's rate-limit refusals (429/529/overloaded) this arm absorbed as unbilled
                # retries — not pauses, not invalid attempts — on record per arm, written whether the arm finished
                # or an interrupt or an account error ended it, so a report can list them and a person can decide
                # to lower the concurrency; nothing lowers it on its own.
                self.releases.events.append("rate_limited", scope=self.scope_id, arm=arm, run=run.hash(),
                                            count=refused)
        return reused + rows

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
        # v0.3 step 3 (M4 §2 复用, RL6-33): the train-side screen's readings of a candidate that was then installed
        # are this version's valid train readings under the measurement's execution identity; they are returned as
        # recorded and not run again. An unistalled candidate's screen rows match no installed release's identity.
        # v0.3 step 5 (M6 §2, 2026-10-05): so are measurement readings of the same W recorded under another run —
        # a research slot's common inputs reuse the mainline's readings of the start W, copied in as recorded.
        reused = self.prior_readings(release_ref, tasks)
        done = {r.task_id for r in reused}

        def run(arm, order, retry=0, supersedes=None):
            return self.run_arm(arm, release_ref, order, plan_ref="", split="train", candidate="current",
                                role="base", retry_index=retry, resume=True, supersedes=supersedes)
        order = [(t, 0) for t in tasks if t not in done]
        # Stage 0/1 review (M5 §3): an attempt lost to infrastructure — a 429 the provider's retries did not absorb,
        # a worker that went away — is refilled under the measurement's retry policy, written with what it
        # supersedes; resume alone would hand the recorded casualty back as the task's reading forever.
        rows = latest_attempts(_refill_attempts(run("measure", order), run, "measure", self.retries())) \
            if order else []
        return [*reused, *rows]

    def retries(self) -> int:
        """How many times an attempt lost to infrastructure is run again: the comparison plan's frozen retry
        policy (one), the same rule a gate arm's attempts follow (task_gate.refill)."""
        return int((self.plan("", "", "").retry_policy or {}).get("max_infrastructure_retries", 0))

    def screen_readings(self, release_ref: str, tasks) -> list:
        """The valid train-side screen attempts (arm `train_screen`, M4 §2) on `tasks` whose run identity is this
        release's measurement identity less the labels that name an arm (the same comparison `control_pool` makes):
        the same W, model identity, per-attempt limits, split and host contract — a candidate's screen reading of a
        task is the installed version's reading of it once that candidate is the installed version."""
        if self.results is None:
            return []
        wanted = set(tasks)
        expected = self.run_identity(release_ref, plan_ref="", split="train", candidate="current", role="base")[0]
        same = {k: v for k, v in expected.payload().items() if k not in RUN_LABELS}
        runs = {h for h, run in self.results.runs().items()
                if {k: v for k, v in run.payload().items() if k not in RUN_LABELS} == same}
        out: dict = {}
        for r in self.results.all():
            if r.run in runs and r.validity == "valid" and r.task_id in wanted and _arm_of(r.attempt_id) == "train_screen":
                out[r.task_id] = r                      # the latest valid reading of each task
        return list(out.values())

    def prior_readings(self, release_ref: str, tasks) -> list:
        """The valid train readings of this W a measurement need not pay for again: the screen readings above, and —
        v0.3 step 5 (M6 §2, 2026-10-05) — train measurement readings (arm `measure`) recorded under another run whose
        identity is this measurement's less the labels (another budget scope, another release of the same W and K):
        the mainline's readings of a research slot's start W, copied into the slot's common workspace as recorded.
        The measurement's own run is left to `run_arm`'s resume, so what a restarted measurement returns does not
        change. The latest valid reading of each task."""
        if self.results is None:
            return []
        wanted = set(tasks)
        expected = self.run_identity(release_ref, plan_ref="", split="train", candidate="current", role="base")[0]
        same = {k: v for k, v in expected.payload().items() if k not in RUN_LABELS}
        own = expected.hash()
        runs = {h for h, run in self.results.runs().items()
                if {k: v for k, v in run.payload().items() if k not in RUN_LABELS} == same}
        out: dict = {}
        for r in self.results.all():
            if r.run not in runs or r.validity != "valid" or r.task_id not in wanted:
                continue
            arm = _arm_of(r.attempt_id)
            if arm == "train_screen" or (arm == "measure" and r.run != own):
                out[r.task_id] = r                      # the latest valid reading of each task
        return list(out.values())

    def measured_cost_per_attempt(self, release_ref: str) -> float | None:
        """The mean cost of one valid attempt of this W on record (M5 §2, v0.3): every valid, priced reading of a
        release that runs tasks as this one does (the same W and K, `_runs_like`) — the gate's candidate and
        confirmation readings of a W count for it once it is installed under another release ref. None when there
        is none: an unknown cost is never 0."""
        if self.results is None:
            return None
        like: dict = {}
        costs = []
        for r in self.results.all():
            if r.validity != "valid" or r.cost_usd is None or not r.system_release_ref:
                continue
            ref = r.system_release_ref
            if ref not in like:
                like[ref] = ref == release_ref or self._runs_like(ref, release_ref)
            if like[ref]:
                costs.append(float(r.cost_usd))
        return round(sum(costs) / len(costs), 6) if costs else None

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
        rows = latest_attempts([r for r in by_release[latest] if not (plan and plan.get("run")) or r.run == plan["run"]])
        seen = {r.task_id for r in rows}
        planned = list(plan["tasks"]) if plan else sorted(seen)
        # v0.3 step 3 (RL6-33): a planned task the installed candidate's screen already read is on record too
        reused = [r for r in self.screen_readings(latest, [t for t in planned if t not in seen])]
        rows, seen = [*rows, *reused], seen | {r.task_id for r in reused}
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
        """The first version's per-round inputs: the diagnosis of the train failures, the draft check and — under a
        core whose K carries `research_session` (package API v2, v0.3 step 3) — the coding session's inputs. All read
        their limits from K; all three are None for a v2 activity (docs/modules/04_RESEARCH_LOOP.md §1–2), the third
        None under a v1 core, which keeps the summary-call round."""
        if not self.harness:
            return None, None, None
        from .agent import harness_package as hp
        from .research import diagnosis as dg
        from .research.drafts import Drafts
        index_of_round = index
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
            round_index=dg.diagnosis_count(experience, self.scope_id), concurrency=self.concurrency,
            components=package.components, package={**run_files, **test_files},
            scope=evidence.get("scope") or "train failures of the latest measurement",
            measured=len(latest), passed=sum(1 for r in latest.values() if r.success))
        # v0.4 (docs/design/V0_4_PLAN.md §1.5): the literals by train task, from the assertion params and the initial
        # states; only a value of one task is forbidden, and the field and column names are the schema, not answers.
        # The assertion params' own literals are the answers: a number among them stays forbidden (review 2026-10-06)
        index, headers = dg.train_literal_index(self.results)
        answers = set(index)
        state_index, state_headers = self.train_state_index()
        for lit, tasks in state_index.items():
            index.setdefault(lit, set()).update(tasks)
        drafts = Drafts(self.objects, parent_ref=bound.package.ref, limits=package, literals=index,
                        max_repairs=int(self.harness["max_draft_repairs"]), events=_ct.Events(root),
                        worker=self.worker, round_index=index_of_round, scope=self.scope_id,
                        allowed_literals=self.tool_vocabulary() | headers | state_headers, fixture=self.fixture_world,
                        reserved_names=self.tool_vocabulary(names_only=True),
                        image=self.harness.get("image_id") or self.harness.get("image"), tools=self.base_tools(),
                        answers=answers)
        methods = self.method_settings()
        if methods is not None:
            # v0.5 (V0_5_PLAN §1.1): the kit completion, the kit check and the reach check join the draft chain
            drafts.methods, drafts.reach = methods, self.reach_fn()
        session = self.session_inputs(evidence, bound, run_files=run_files, test_files=test_files,
                                      round_ordinal=diagnosis.round_index) \
            if self.harness.get("research_session") else None
        if session is not None and methods is not None:
            session.methods = methods
            session.extra_inputs, session.playbook = self.method_inputs(
                bound, {**run_files, **test_files}, {r.task_id: r for r in evidence.get("train") or ()
                                                     if r.validity == "valid"}, experience)
        return diagnosis, drafts, session

    def session_inputs(self, evidence: dict, bound, *, run_files: dict, test_files: dict, round_ordinal: int = 0):
        """What R's coding session needs this round (research.session.SessionInputs, M4 §1 R 的输入, §2 筛选): K's
        caps and screen numbers, the isolated worker, the current package and W's prompt texts, the dev failure-type
        histogram over the installed W's pooled dev readings, the read-only trajectory reader over this workspace's
        results and transcripts, the train split in its frozen order, the round's passing sample drawn from the
        latest measurement with the plan's order_seed, that measurement's per-task outcomes (the screen's "before"),
        and the two callables that build a candidate W and run its screen arm.

        v0.4 (docs/design/V0_4_PLAN.md §1.2, §1.3, §1.6): the interface error table over the latest train attempts
        (research/api_errors.py), the passing train attempts for the trajectory listing with must-not tasks first,
        and — under a K with the v0.4 screen — a passing sample redrawn for this round (`round_ordinal`, the scope's
        diagnosis count) with must-not tasks first; a v0.3 K keeps the one fixed sample.

        After an install the measurement reruns only the previous failures (measure), so the train tasks it did not
        rerun — all passes — are filled in from the activity's earlier measurements (carried_train_readings) before
        anything is derived: the passing listing, the interface table and, under the v0.4 screen, the sample and its
        "before" see the whole train split, not W0's former failures alone (review 2026-10-06). The measurement, the
        diagnosis and the train score are not touched; a v0.3 K's sample and "before" stay the measurement's."""
        from .agent import harness_package as hp
        from .research.api_errors import api_error_table
        from .research.session import (ScreenSettings, SessionInputs, SessionLimits, TrajectoryReader, passing_sample,
                                       passing_sample_v4)
        limits = SessionLimits.from_json(self.harness["research_session"])
        screen = ScreenSettings.from_json(self.harness.get("screen"))
        order = list(self.splits.ids("train"))
        latest = {r.task_id: r for r in evidence.get("train") or () if r.validity == "valid"}
        parent = self.releases.active()["release_ref"]
        full = {**self.carried_train_readings(parent, [t for t in order if t not in latest]), **latest}
        screened = full if screen.v04 else latest
        must_not = tuple(t for t in order if ab.has_must_not(self.suite, t))
        passing = [full[t] for t in order if t in full and full[t].success]
        passing.sort(key=lambda r: (r.task_id not in must_not, order.index(r.task_id)))
        if screen.v04:
            sample = passing_sample_v4(full.values(), order=order, size=screen.passing_sample, seed=self.order_seed,
                                       round_ordinal=round_ordinal, must_not=must_not)
        else:
            sample = passing_sample(latest.values(), order=order, size=screen.passing_sample, seed=self.order_seed)
        transcripts = Path(self.rt.root) / "transcripts"
        return SessionInputs(
            limits=limits, screen=screen, k=int(getattr(self, "candidates_per_round", 1) or 1), worker=self.worker,
            package_files={**run_files, **test_files},
            prompt_texts={t: bound.prompts[t] for t in ("worker.system_prompt", "worker.finish") if bound.prompts.get(t)},
            histogram=self.dev_histogram(parent),
            trajectories=TrajectoryReader(self.results, transcripts, limits),
            train_tasks=tuple(order), train_order=tuple(order), sample=tuple(sample),
            before={t: bool(r.success) for t, r in screened.items()},
            build=self.screen_build, screen_arm=self.train_screen,
            affordable=(lambda n: self.budget.affordable({"model_calls": n, "tool_calls": n})) if self.budget else None,
            base_tools=self.base_tools(), api_version=self.harness.get("package_api") or hp.AGENT_API_VERSION,
            max_package_bytes=hp.Limits.from_json(self.harness["package"]).max_package_bytes,
            passing_attempts=tuple(r.attempt_id for r in passing), must_not=must_not,
            api_errors=api_error_table(full.values(), transcripts))

    def carried_train_readings(self, release_ref: str, tasks) -> dict:
        """task -> the latest valid train measurement reading (arm `measure`) of each of `tasks` recorded under this
        activity's K — the same `core_ref` as `release_ref`, any W of the lineage — in the order the store holds them
        (review 2026-10-06). After an install, measure reruns only the previous failures, and the passes it does not
        rerun stay the history of the version that produced them; this hands that history to the session's inputs,
        never to a measurement or a score. The same W and K alone (`_runs_like`) would find nothing here: an install
        changed W. Screen readings are not taken (a candidate's, not the lineage's)."""
        if self.results is None or not tasks:
            return {}
        try:
            core = self.objects.load(release_ref, _ct.SystemRelease).core_ref
        except (KeyError, _ct.ContractError):
            return {}
        wanted, cores, out = set(tasks), {}, {}
        for r in self.results.all():
            if r.split != "train" or r.validity != "valid" or r.task_id not in wanted or not r.system_release_ref \
                    or _arm_of(r.attempt_id) != "measure":
                continue
            ref = r.system_release_ref
            if ref not in cores:
                try:
                    cores[ref] = self.objects.load(ref, _ct.SystemRelease).core_ref
                except (KeyError, _ct.ContractError):
                    cores[ref] = None
            if cores[ref] == core:
                out[r.task_id] = r
        return out

    def dev_histogram(self, parent_release: str) -> dict:
        """The dev failure-type histogram (M4 §1 R 的输入): over the installed W's pooled dev readings under the
        activity's plan, failed scored assertions counted by type — the fixed projection that is R's only
        assertion-level view of dev. Nothing else of dev reaches the session."""
        from .research.session import dev_failure_histogram
        plan = self.plan("", "", parent_release)
        pool = self.control_pool(parent_release, plan=plan) if plan.pooled and self.results is not None else \
            [r for r in (self.results.all() if self.results is not None else ())
             if r.split == "dev" and r.validity == "valid" and r.system_release_ref == parent_release
             and r.task_id in set(plan.task_ids) and _arm_of(r.attempt_id) not in SCREEN_ARMS]
        return dev_failure_histogram(self.results, pool)

    def screen_build(self, proposal, experiment_id: str) -> str:
        """The candidate W of an admitted proposal, built for its train-side screen before any Experiment exists
        (M5 §1: the screen is a round-level sub-operation; only the chosen candidate is frozen into an Experiment).
        `experiment_id` is the id the candidate's Experiment gets if it is chosen: the set's provenance names it, so
        the W the screen read and the W the gate reads are one content-addressed candidate, and an install's later
        measurement finds the screen's readings under its own identity (screen_readings). The parent is the pointer —
        the release the session's inputs and the Experiment are built on — never the parent a previous gate left in
        `_parent_release`."""
        parent = self.releases.active()["release_ref"]
        stand_in = _ct.Experiment(id=experiment_id, proposal_ref=self.objects.put(proposal), parent_release=parent,
                                  experience_ref="", core_ref=self.core_ref, eval_template_ref=self.template_ref,
                                  budget_scope=self.scope_id)
        build = _P.build_candidate(stand_in, objects=self.objects, parent_release=parent, owner=self.owner,
                                   services=self.services(), train=self._train, budget=self.budget)
        if build.status != "ready":
            raise _ct.ContractError("; ".join(build.errors)[:300])
        return build.candidate_ref

    def train_screen(self, candidate_ref: str, tasks, *, arm: str = "train_screen", notes: str = "",
                     extra_system: dict | None = None, repeat: int = 0) -> list:
        """One candidate's train-side screen (M4 §2): its arm on the given train tasks, one attempt each, under a
        permit and a plan of its own (`train_screen` arm), so the attempts carry an identity that an install can
        later read as this version's train readings (screen_readings) and that never enters a dev pool. Attempts lost
        to infrastructure are refilled once under the plan's retry policy, as a gate arm's are (task_gate.refill); the
        parent is the pointer, as for screen_build.

        v0.5 (V0_5_PLAN §1.3, §1.4): the same path, under another `arm` and `notes`, runs a learn reading's twins
        (`learn_A`, `learn_B`) and the library stream's attempts (`library_first`, `library_retry`, the retry with its
        note in `extra_system`): train only, never a screen reading an install would reuse."""
        if arm != "train_screen" and arm in SCREEN_ARMS:
            raise ValueError(f"arm {arm!r} is a screen arm")
        parent = self.releases.active()["release_ref"]
        tasks = list(tasks)
        plan = TaskEvalPlan(candidate_ref=candidate_ref,
                            control_ref=self.objects.load(parent, _ct.SystemRelease).worker_ref, parent_release=parent,
                            core_ref=self.core_ref, split_ref=self.splits.hash(), task_ids=tuple(tasks), repeats=1,
                            order_seed=self.order_seed, per_attempt_budget=self.attempt_budget.limits(),
                            retry_policy={"max_infrastructure_retries": 1}, gate_plan=self.gate.to_json(),
                            installs=False)
        plan_ref = self.objects.put(plan)
        permit = self.releases.permit(candidate_ref, parent, plan_ref, self.scope_id, self.core_ref)
        arm_release = self.releases.candidate_release(parent, candidate_ref, permit)

        def run(arm_name, order, retry=0, supersedes=None):
            return self.run_arm(arm_name, arm_release, order, plan_ref=plan_ref, permit=permit, split="train",
                                candidate="candidate", role="evolved", retry_index=retry, resume=True,
                                supersedes=supersedes, notes=notes, extra_system=extra_system)
        return _refill(plan, arm, list(run(arm, [(t, int(repeat)) for t in tasks])), run)

    def fixture_world(self):
        """The world package tests run against: a fresh episode of the first train task, never dev or final
        (docs/modules/05_EXECUTION_BUDGET.md §1)."""
        return self.suite.episode(next(iter(self.train_tasks or self.splits.ids("train"))))

    # -------------------------------------------------------------- v0.5: the method and library tables of K
    def method_settings(self):
        """K's methods table as research.methods.MethodSettings, or None (a K before v0.5)."""
        from .research.methods import MethodSettings
        table = (self.harness or {}).get("methods")
        return MethodSettings.from_json(table) if isinstance(table, dict) else None

    def library_settings(self):
        """K's library table as research.library.LibrarySettings, or None (a K before v0.5)."""
        from .research.library import LibrarySettings
        table = (self.harness or {}).get("library")
        return LibrarySettings.from_json(table) if isinstance(table, dict) else None

    def task_text(self, task_id: str) -> str:
        """The text the agent is given for a task (its trigger)."""
        return self.suite.tasks[task_id].trigger

    def train_rows(self) -> list:
        """Every valid train attempt of this activity, in the store's order (train only, by the record's split)."""
        train = set(self.splits.ids("train"))
        return [r for r in (self.results.all() if self.results is not None else ())
                if r.split == "train" and r.validity == "valid" and r.task_id in train]

    def latest_train(self, rows=None) -> dict:
        """task -> its latest valid train attempt (any arm)."""
        out = {}
        for r in rows if rows is not None else self.train_rows():
            out[r.task_id] = r
        return out

    def transcript_of(self, row) -> list:
        from .research.diagnosis import read_transcript
        return read_transcript(Path(self.rt.root) / "transcripts", row.evidence)

    def recorded_attempts(self) -> list:
        """The reach check's replay set (V0_5_PLAN §1.1): the latest valid train attempt of every train task, as
        [{task, success, attempt_id, events, final}]; train only."""
        from .research.methods import replay_events
        out = []
        for task, row in self.latest_train().items():
            events, final = replay_events(self.transcript_of(row))
            out.append({"task": task, "success": bool(row.success), "attempt_id": row.attempt_id, "events": events,
                        "final": final})
        return out

    def reach_fn(self):
        """(files, names, declaration) -> (errors, report): the reach check over this activity's recorded train
        attempts, the claimed tasks' replays from their latest failed attempt, fresh train worlds, the task's
        assertion values counted on the host."""
        from .research.methods import assertion_values, reach_check, replay_events
        settings = self.method_settings()

        def reach(files, names, declaration):
            attempts = self.recorded_attempts()
            failed = {}
            for r in self.train_rows():
                if not r.success:
                    failed[r.task_id] = r
            claimed = []
            for task in declaration.get("predicted_affected") or ():
                if task in failed:
                    events, final = replay_events(self.transcript_of(failed[task]))
                    claimed.append({"task": task, "events": events, "final": final})
            by_task = {a["task"]: a for a in attempts}
            for c in claimed:
                by_task[c["task"]] = {**by_task.get(c["task"], {}), **c}
            return reach_check(self.worker, files, names, attempts=list(by_task.values()),
                               predicted=[c["task"] for c in claimed], world_of=self.suite.episode,
                               values_of=lambda t: assertion_values(self.suite.grading(t).assertions),
                               task_text_of=self.task_text, settings=settings)
        return reach

    def package_candidate(self, files: dict, *, label: str) -> str:
        """A candidate W whose package is `files` — fixed code's variant of the installed package (a learn reading's
        twin, the library stream's W_base ⊕ L_n) — built as a screened candidate is: on the pointer, never installed
        by itself. Its experiment id is the package's and the label's digest, so a resumed phase finds the same W and
        the attempts it already ran under it."""
        from .agent import harness_package as hp
        limits = hp.Limits.from_json(self.harness["package"])
        package_ref = hp.build_package(self.objects, files, limits,
                                       image=self.harness.get("image_id") or self.harness.get("image"))
        proposal = _ct.ExperimentProposal(
            owner=self.owner, hypothesis=label[:2000], origin="fixed",
            changes=({"target": "worker.harness", "kind": "workflow",
                      "change": {"builtin": hp.load_package(self.objects, package_ref).api_version,
                                 "package_ref": package_ref}},))
        return self.screen_build(proposal, "x" + digest({"package": package_ref, "label": label})[:16])

    def trigger_replay(self, files: dict) -> dict:
        """task -> the methods whose trigger fires on the task's latest recorded train attempt (the reach harness
        with run off, in the worker): the train tasks a learn reading can learn from (V0_5_PLAN §1.3)."""
        from .agent.worker import PackageCallError
        from .research import methods as M
        decls = M.methods_of(files)
        if not decls:
            return {}
        manifest = M.manifest_of(files)
        package = {"tools": [dict(t) for t in manifest.get("tools") or () if t.get("enabled", True)]}
        wire = {n: {k: d.get(k) for k in ("module", "func", "trigger", "kinds", "on", "default_variant")}
                for n, d in decls.items() if d.get("enabled", True)}
        out = {}
        session = self.worker.start()
        try:
            session.load(M.reach_harness_files(files))
            for a in self.recorded_attempts():
                try:
                    value = session.request({"op": "hook", "entry": f"{M.REACH_PATH}:replay",
                                             "event": {"methods": wire, "events": a["events"],
                                                       "task": self.task_text(a["task"]), "final_message": a["final"],
                                                       "package": package, "run": False}, "view": {}})
                except PackageCallError:
                    continue
                fired = [n for n, r in ((value or {}).get("methods") or {}).items() if r.get("fired")]
                if fired:
                    out[a["task"]] = sorted(fired)
        finally:
            session.close()
        return out

    def base_means(self, release_ref: str) -> dict:
        """task -> the mean success of a W's valid train readings (its measurements and the baseline, arm
        `measure`): b(t), the library stream's control and the credit's baseline (V0_5_PLAN §1.4)."""
        sums: dict = {}
        for r in self.train_rows():
            if r.system_release_ref == release_ref and _arm_of(r.attempt_id) == "measure":
                s = sums.setdefault(r.task_id, [0, 0])
                s[0] += int(bool(r.success))
                s[1] += 1
        return {t: s[0] / s[1] for t, s in sums.items() if s[1]}

    def method_inputs(self, bound, package_files: dict, latest: dict, experience=()) -> tuple[dict, str]:
        """R's method-library inputs (V0_5_PLAN §1.1; V0_5_METHOD_LIBRARY §4.2), train only: methods.json,
        uncovered.json, method_research.json, the kit, the playbook so far and — with an experience library in the
        package — library.json. -> (files, the playbook text)."""
        from .evolve import experience as xp
        from .research import library as lib
        from .research import methods as M
        fits = [e for e in experience if e.get("kind") == "method_fit"]
        rows = [(r, self.transcript_of(r)) for r in self.latest_train().values()]
        failing = [(r, self.transcript_of(r), self.results.grading(r.attempt_id)) for r in latest.values()
                   if not r.success]
        playbooks = [e for e in experience if e.get("kind") == "method_playbook"]
        playbook = playbooks[-1].get("text", "") if playbooks else ""
        files = {"methods.json": json.dumps(M.method_telemetry(rows, package_files, fits), ensure_ascii=False,
                                            indent=1, default=str),
                 "uncovered.json": json.dumps(M.uncovered_clusters(failing, package_files, fits), ensure_ascii=False,
                                              indent=1, default=str),
                 "method_research.json": json.dumps(M.research_moves(xp.ledger_rows(experience), fits),
                                                    ensure_ascii=False, indent=1, default=str),
                 "playbook.md": playbook or "(empty: finish with a `playbook` to start one)\n",
                 **M.method_kit_inputs()}
        entries, _ = lib.library_of(package_files)
        if entries:
            files["library.json"] = json.dumps(lib.telemetry(entries, experience), ensure_ascii=False, indent=1,
                                               default=str)
        return files, playbook

    def train_state_index(self) -> tuple[dict, set]:
        """(v0.4, docs/design/V0_4_PLAN.md §1.5) The initial states' literals by train task (literal -> tasks) and their
        header names (record fields, sheet columns): what train_state_literals reads, kept per task so the draft
        check can tell one task's answer from the domain's vocabulary. Train tasks only."""
        from .agent.harness_package import add_literals, header_names
        index: dict = {}
        headers: set = set()
        for task_id in self.train_tasks or self.splits.ids("train"):
            try:
                state = self.suite.grading(task_id).initial_state
            except Exception:  # noqa: BLE001 - a task without a readable initial state adds nothing
                continue
            add_literals(index, task_id, state)
            headers |= header_names(state)
        return index, headers

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
        # A new round starts from the pointer, not from the parent the previous experiment was frozen on: the carried
        # values are cleared before `verify_bound` reads them, and `_begin` sets them again for the experiment that runs.
        self._parent_release, self._experiment_ref = "", ""
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
        verified is never started (EX-03).

        v0.3 (M5 §2 验证预留): the candidate arm × (1 + confirmation_repeats) — the confirmation reading is a
        verification step and belongs in the bound — plus the control pairs the pooled readings are short of
        (task_gate.shortfall, the same count evaluate_task reads; the whole control arm under a plan that does
        not pool)."""
        readings = 1 + int(self.confirmation_repeats or 0)
        parent = self._parent()
        plan = self.plan("", "", parent)
        control = len(_shortfall(plan, self.control_pool(parent, plan=plan))) if plan.pooled else \
            len(self.dev_tasks) * self.repeats
        attempts = len(self.dev_tasks) * self.repeats * readings + control
        bound = {"model_calls": attempts, "tool_calls": attempts}
        # v0.3 step 5 (M5 §2 验证预留, 2026-10-05 review): in dollars too, when the parent W has priced readings —
        # the attempts at its measured cost per attempt, raised by the gate's cost allowance β0 (a candidate may cost
        # that much more before the cost term judges it) — so a gate the budget cannot pay for is refused before it
        # starts rather than cut off half way. `usd_per_attempt` lets the round's screen be priced the same way.
        cpa = self.measured_cost_per_attempt(parent)
        if cpa is not None:
            per = cpa * (1 + float(plan.gate().cost_beta0 or 0.0))
            bound.update(usd=round(attempts * per, 6), usd_per_attempt=round(per, 6))
        return bound

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

    def _evaluate(self, candidate_ref: str, budget, confirming=None) -> _ct.EvaluationReceipt:
        """The gate: the candidate arm under the frozen plan against the control — under a pooled plan the installed
        W's pooled readings (control_pool) topped up by a control reading of exactly the pairs they are short of,
        a fresh reading of the parent otherwise — and, under a plan that confirms, the confirmation reading of the
        candidate arm. The top-up and the confirmation each run under their own run identity (the control's, and
        candidate "confirmation" / arm `confirm`), so a resume finds exactly the attempts it had recorded and never
        mistakes the first reading's for them (M5 §3, the frozen inputs)."""
        parent = self._parent()
        control_set = self.objects.load(parent, _ct.SystemRelease).worker_ref if self.owner == "worker" else \
            self.objects.load(parent, _ct.SystemRelease).research_ref
        plan = self.plan(candidate_ref, control_set, parent)
        plan_ref = self.objects.put(plan)
        permit = self.releases.permit(candidate_ref, parent, plan_ref, self.scope_id, self.core_ref)
        arm_release = self.releases.candidate_release(parent, candidate_ref, permit)
        self._plans[candidate_ref] = (plan_ref, arm_release)
        pool = self.control_pool(parent, plan=plan) if plan.pooled else []

        def run(arm, order, retry=0, supersedes=None):
            if arm == "subject":
                return self.run_arm(arm, arm_release, order, plan_ref=plan_ref, permit=permit,
                                    retry_index=retry, resume=True, supersedes=supersedes)
            if arm.startswith("confirm"):
                return self.run_arm(arm, arm_release, order, plan_ref=plan_ref, permit=permit,
                                    candidate=arm.replace("confirm", "confirmation", 1), retry_index=retry,
                                    resume=True, supersedes=supersedes)
            return self.run_arm(arm, parent, order, plan_ref=plan_ref, candidate="control", role="base",
                                retry_index=retry, resume=True, supersedes=supersedes)

        receipt = evaluate_task(plan, run=run, objects=self.objects, experiment_ref=self._experiment_ref,
                                families=self.families(), control_pool=pool, confirming=confirming)
        ref = self.objects.put(receipt)
        self.releases.register_receipt(ref, plan=plan_ref, subject=candidate_ref, verdict=receipt.verdict,
                                      scope=self.scope_id, reading_kind=receipt.reading_kind,
                                      control_readings=receipt.metrics.get("control_readings"))
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


# The screen's arms (Improvement._screen): the predeclared screen tasks only, at attempt 0 — not a reading of the
# plan, so never part of a pooled control (M3 §2.1(a), v0.3). The train-side screen's arm (`train_screen`, M4 §2)
# runs on train and never meets a dev pool.
SCREEN_ARMS = ("screen_subject", "screen_control", "train_screen")
# The run-manifest fields that name an arm rather than an execution identity (M3 §2.1(a)): two runs equal in every
# other field ran the same W under the same model, limits, split and host contract.
RUN_LABELS = ("candidate", "role", "eval_plan_ref", "budget_scope", "notes", "system_release_ref",
              "research_ref", "experience_ref", "mode")


def _research_meter(researcher):
    """The metered provider behind a research step (bench._default_researcher: PolicyResearcher over _Completions
    over MeteredProvider), or None for a scripted researcher that meters nothing."""
    provider = getattr(getattr(researcher, "llm", None), "provider", None)
    return provider if hasattr(provider, "rate_limited") else None


def _rate_limit_recorder(researcher, events: _ct.Events, *, scope: str, first_round: int, on_result=None):
    """-> an `on_result` for run_worker_rounds that, after each completed round, writes the research meter's
    rate-limit refusals of that round as a `rate_limited` event (arm `research`, the round's index, the count),
    only when there were any (M5 §2, v0.3), then hands the result on to `on_result`. The meter's count is
    cumulative; the event carries this round's share of it."""
    from .research.loop import ROUND_MARKERS
    meter = _research_meter(researcher)
    seen = {"count": 0, "round": first_round}

    def record(result) -> None:
        try:
            if meter is not None and result.outcome not in ROUND_MARKERS:
                total = int(getattr(meter, "rate_limited", 0) or 0)
                if total > seen["count"]:
                    events.append("rate_limited", scope=scope, arm="research", round=seen["round"],
                                  count=total - seen["count"])
                    seen["count"] = total
                seen["round"] += 1
        finally:
            if on_result is not None:
                on_result(result)
    return record


def latest_attempts(rows) -> list:
    """One row per (task, repeat): the last one recorded — a refill after the infrastructure casualty it supersedes —
    in first-seen order. A measurement's reading is that; the casualty stays on record in the results file."""
    out: dict = {}
    for r in rows:
        out[(r.task_id, r.attempt)] = r
    return list(out.values())


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
                "max_failure_notes", "max_entry_chars", "max_effort", "research_session", "screen"}
# v0.3 step 3 (M4 §1, §2; M7 §2): the two nested harness tables — R's coding session's caps and the train-side
# screen's numbers — and the keys each takes. research_session.* and screen.passing_sample are caps the config may
# only lower; screen.min_net_flips is a floor it may only raise (HARNESS_TABLE_FLOORS: lowering it loosens the screen).
# v0.4 (docs/design/V0_4_PLAN.md §1.3, §1.6): the read floors admission checks (research_session.min_failing_reads,
# min_passing_reads) and the v0.4 screen's floor and cap (screen.min_fixes, max_sample_regressions). A K frozen before
# them has none of the four and runs v0.3's admission and screen.
HARNESS_TABLES = {"research_session": ("max_turns", "max_edits", "max_trajectory_bytes", "max_trajectory_bytes_total",
                                       "min_failing_reads", "min_passing_reads"),
                  "screen": ("passing_sample", "min_net_flips", "min_fixes", "max_sample_regressions")}
# The reasoning-effort tiers, lowest first (the `claude_code` provider's names). K's `max_effort` is the ceiling the
# gateway caps a package's requested effort to (docs/modules/00_CONTRACTS.md §2, v0.3): a request above it is sent
# at the ceiling, never refused, with the requested value on the record. Like every harness key it narrows only.
from .agent.gateway import DEFAULT_MAX_EFFORT, EFFORTS  # noqa: E402 - the gateway caps by the same order


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
# The v0.3 preset (docs/modules/07_BENCH_REPORT.md §2, M0 §2, v0.3, 2026-10-04): the v1 preset under package API v2 —
# the `agent` component kind, so a candidate may change the loop itself — with K's effort ceiling and the five
# per-attempt hard limits at the design's values as [bench.budget]'s defaults (preset_budget). Its own entry, not a
# change to the v1 one: the v1 numbers are frozen into the v0.1/v0.2 cores and are not revisited. Since v0.3 step 5
# it takes a [bench] domain list (no Marketing lock).
PRESETS["automationbench_harness_v3"] = {
    **json.loads(json.dumps(PRESETS["marketing_harness_v1"])),
    # v0.3 step 5 (M7 §2 “preset 的领域锁”): no domain lock — [bench] domains may list any upstream domains, and the
    # rule is that every one of them loads from the checkout (bench.load_domains, at every open of the activity).
    "domain": None,
    "harness": {**PRESETS["marketing_harness_v1"]["harness"], "package_api": "harness_package.v2",
                "components": ["agent", "memory", "skills", "on_finish", "before_tool", "after_tool", "tools"],
                "max_effort": "high",
                # v0.3 step 3 (M4 §1 R 作为有界编码会话, §2 train 侧筛选; 2026-10-05): R's coding session — one per round,
                # 40 model calls finish included, 24 edits across every candidate and revision, 65536 bytes per
                # read_trajectory and 524288 per session — and the screen's fixed passing sample of 5 train tasks
                # with net flips of at least 1 to pay the dev gate. Frozen into K; R cannot raise them.
                "research_session": {"max_turns": 40, "max_edits": 24, "max_trajectory_bytes": 65536,
                                     "max_trajectory_bytes_total": 524288,
                                     # v0.4 (V0_4_PLAN §1.3): admission's read floors — each candidate grounded
                                     # in a failed trajectory of its predicted tasks, and one passing read a session
                                     "min_failing_reads": 1, "min_passing_reads": 1},
                # v0.4 (V0_4_PLAN §1.6): 6 passing tasks redrawn each round with must-not tasks first; eligible with
                # ≥1 fix and ≤1 sample regression, then ranked by net flips (= fixes − regressions, so 0 is the floor)
                "screen": {"passing_sample": 6, "min_net_flips": 0, "min_fixes": 1, "max_sample_regressions": 1}},
    # v0.3 step 3 (M4 §2): up to three candidates a round, from one session; [improvement] may set 1..3.
    "max_candidates_per_round": 3,
    "budget": {"max_steps": 50, "max_model_calls": 200, "max_tool_calls": 200, "max_seconds": 900.0,
               "max_tokens": 400000},
    # v0.3 step 2 (M3 §2.1, M5 §2, M6 §2; 2026-10-04): the gate's statistics — a pooled control, one confirmation
    # reading before an install, a regression as a drop of 0.5, the cost term β0 0.10 / β1 2.0, no gate under 25
    # tasks — eight attempts in flight, three rounds per research arm and the headroom check before a slot asks R.
    # [freeze.task_plan.gate] may tighten any of these and loosen none (_beyond_preset).
    "task_plan": {"tasks": "dev", "repeats": 1, "order_seed": 0, "control_pooling": "pooled",
                  "confirmation_repeats": 1,
                  "gate": {"mode": "quality", "min_gain": 0.05, "min_groups": 5, "max_regression_rate": 0.1,
                           "regression_drop": 0.5, "cost_beta0": 0.10, "cost_beta1": 2.0, "min_tasks": 25}},
    # arm_share / outer_share re-estimated for step 5 (M6 §2, v0.3; 2026-10-05), replacing v1's 0.15 / 0.08. A share is
    # a fraction of the frozen root. These were derived for the first step-5 plan's single $2,800 root (candidates at
    # effort high, $0.69 an attempt): M6 §2's arm share = a mainline round ($10 R + ~36 x $0.69 screen $25 + 125 x
    # $0.69 gate $86.25 = $121.25) x worker_round_limit 3 + one confirmation $86.25 = $450 -> 450 / 2800 = 0.16; the
    # outer comparison 2 x 63 x $0.69 = $86.9 + 10% refill + 8 lanes and 2 stuck reservations ($72.8) = $168 ->
    # 0.06. The staged plan the user chose (2026-10-05, ROADMAP v0.3 step 5) runs no slot in that workspace and
    # defers it to a copy with a root of its own (stage 5); at K's effort medium the same formula gives an arm
    # $90.5 x 3 + $62.5 = $334, below what an arm can spend on its 62-task inner gate (3 x ($10 + $18 + $31) + a
    # confirmation $31 + an install's rerun $65.5 = $273.5, plus $72.8 of reservations = $346), and the outer
    # comparison 2 x 63 x $0.50 + 10% + $72.8 = $142; so before stage 5's root opens these fractions are re-derived
    # for that root from $346 and $142 (a $1,000 root: 0.35 and 0.15).
    "campaign": {"worker_round_limit": 3, "arm_share": 0.16, "outer_share": 0.06, "min_gain": 0.05,
                 "min_groups": 1, "min_headroom_tasks": 6},
    "concurrency": 8,
}


# The v0.5 preset (docs/design/V0_5_PLAN.md §0, §1): the v3 preset as of v0.4 — the per-domain greedy loop, unchanged —
# plus two tables only this preset has and no config may change: `methods` (the method contract and the applicability
# learner's numbers, research.methods.MethodSettings) and `library` (the experience library's,
# research.library.LibrarySettings). A K frozen under it is a v0.5 K; every new behaviour reads these tables from K,
# so a K without them (v0.1–v0.4) runs exactly as before.
def _v5_preset() -> dict:
    from .research.library import LibrarySettings
    from .research.methods import MethodSettings
    preset = json.loads(json.dumps(PRESETS["automationbench_harness_v3"]))
    preset["harness"]["methods"] = MethodSettings().to_json()
    preset["harness"]["library"] = LibrarySettings().to_json()
    return preset


PRESETS["automationbench_harness_v5"] = _v5_preset()
# The harness tables only a preset sets (v0.5): [harness_evolution] may not name them.
PRESET_ONLY_TABLES = ("methods", "library")
# What the method router needs K to allow: tools for the methods, and the three hook sites it registers.
METHOD_COMPONENTS = ("tools", "after_tool", "before_tool", "on_finish")


def preset_concurrency(cfg: dict) -> int:
    """[bench] concurrency's default under the [improvement] preset (M5 §2, v0.3): 8 under the v3 preset, 1 under
    the v1 preset and without a preset. A setting of the machine, never part of a frozen plan; the config may set
    any count, and `--concurrency` on the command line overrides it for one run."""
    name = dict(cfg.get("improvement") or {}).get("preset")
    preset = PRESETS.get(name) if isinstance(name, str) else None
    return int((preset or {}).get("concurrency") or 1)


def preset_budget(cfg: dict) -> dict:
    """The per-attempt hard limits the [improvement] preset fixes, as [bench.budget]'s defaults (M0 §2 "单次尝试硬上限",
    v0.3): the v3 preset carries all five, so a config under it freezes them without writing them. {} under a preset
    without a `budget` table (the v1 preset, whose cores froze what [bench.budget] alone said), without [improvement],
    and for an unknown preset — improvement_settings names that one. Like the harness numbers they are ceilings:
    BenchConfig.load lets [bench.budget] lower one and refuses to raise one."""
    name = dict(cfg.get("improvement") or {}).get("preset")
    preset = PRESETS.get(name) if isinstance(name, str) else None
    return dict((preset or {}).get("budget") or {})


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
    table_bench = dict(cfg.get("bench") or {})
    listed = table_bench.get("domains") or [table_bench.get("domain", "marketing")]
    listed = [listed] if isinstance(listed, str) else list(listed)
    # The v1 preset is locked to Marketing (its numbers are frozen into the v0.1/v0.2 cores). The v3 preset has no
    # domain lock (docs/modules/07_BENCH_REPORT.md §2, v0.3 step 5): its rule is that every listed domain loads from
    # the upstream checkout, which bench.load_domains checks wherever an activity opens (smoke, freeze, every phase).
    if preset.get("domain") is not None and listed != [preset["domain"]]:
        raise ValueError(f"[improvement] preset {name!r} is for the {preset['domain']} domain, not "
                         f"{listed[0] if len(listed) == 1 else listed!r}")
    # v0.3 step 3 (M4 §2): k candidates a round, the preset's value by default and its ceiling (3 under the v3
    # preset; the v1 preset has none, so 1 as before); fewer is allowed, more is refused before anything is paid.
    ceiling = int(preset.get("max_candidates_per_round", 1))
    candidates = int(table.get("max_candidates_per_round", ceiling))
    batches = int(table.get("max_collection_batches", 2))
    if not 1 <= candidates <= ceiling:
        raise ValueError(f"[improvement] max_candidates_per_round = {candidates}: "
                         + ("this preset runs one candidate per round (it has no coding session)" if ceiling == 1
                            else f"this preset runs 1 to {ceiling} candidates per round"))
    if not 0 <= batches <= 2:
        raise ValueError("[improvement] max_collection_batches: at most 2 per worker segment in this preset")
    return {"mode": mode, "preset": name, "allowed_methods": methods, "candidates_per_round": candidates,
            "collection_batches": batches, **{k: preset[k] for k in ("harness", "schedule", "task_plan",
                                                                         "campaign")}}


def _preset_harness(cfg: dict, preset: dict) -> dict:
    """The preset's harness table narrowed by [harness_evolution]: fewer component kinds, lower limits."""
    table = dict(cfg.get("harness_evolution") or {})
    fixed = sorted(set(table) & set(PRESET_ONLY_TABLES))
    if fixed:
        raise ValueError(f"[harness_evolution] {fixed}: the preset fixes these tables (V0_5_PLAN §1); a config does "
                         f"not change them")
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
        elif key in HARNESS_TABLES:
            # v0.3 step 3: a nested table (research_session, screen) tightens its numbers one by one and exists only
            # where the preset has it (a v1 preset has no session to cap)
            if not isinstance(preset.get(key), dict):
                raise ValueError(f"[harness_evolution] {key}: the preset has no {key} (no coding session under it)")
            out[key] = _harness_table(key, value, preset[key])
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


# The direction each nested harness number tightens in (M4 §1, §2; M7 §2, 2026-10-05): a cap is lowered, a floor is
# raised; and the least value any of them may take, with or without a preset.
HARNESS_TABLE_FLOORS = {("screen", "min_net_flips"), ("screen", "min_fixes"),
                        ("research_session", "min_failing_reads"), ("research_session", "min_passing_reads")}
HARNESS_TABLE_LEAST = {("research_session", "max_turns"): 1, ("research_session", "max_edits"): 1,
                       ("research_session", "max_trajectory_bytes"): 1,
                       ("research_session", "max_trajectory_bytes_total"): 1,
                       ("research_session", "min_failing_reads"): 0, ("research_session", "min_passing_reads"): 0,
                       ("screen", "passing_sample"): 0, ("screen", "min_net_flips"): 0, ("screen", "min_fixes"): 0,
                       ("screen", "max_sample_regressions"): 0}


def _harness_table(key: str, value, preset_table: dict | None) -> dict:
    """One nested [harness_evolution.<key>] table, validated the same way under a preset and without one: it is a
    table of known integer keys; each value is at least its least value; under a preset a cap (every research_session
    number, screen.passing_sample) may only be lowered and a floor (screen.min_net_flips) only raised, and the keys
    not written keep the preset's values; without a preset the keys not written keep the design's defaults (an empty
    table asks for the session with the default caps)."""
    from .research.session import ScreenSettings, SessionLimits
    if not isinstance(value, dict):
        raise ValueError(f"[harness_evolution] {key} is a table ({list(HARNESS_TABLES[key])}), not "
                         f"{type(value).__name__}")
    unknown = sorted(set(value) - set(HARNESS_TABLES[key]))
    if unknown:
        raise ValueError(f"[harness_evolution.{key}] does not take {unknown}; it takes {list(HARNESS_TABLES[key])}")
    defaults = {"research_session": SessionLimits, "screen": ScreenSettings}[key]().to_json()
    out = dict(preset_table if preset_table is not None else defaults)
    for sub, v in value.items():
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"[harness_evolution.{key}] {sub} = {v!r} is not an integer")
        least = HARNESS_TABLE_LEAST[(key, sub)]
        if v < least:
            raise ValueError(f"[harness_evolution.{key}] {sub} = {v} is below the least value {least}")
        if preset_table is not None:
            if sub not in preset_table:
                raise ValueError(f"[harness_evolution.{key}] {sub}: the preset has no {sub}, and a preset is not "
                                 f"widened from the config")
            if (key, sub) in HARNESS_TABLE_FLOORS and v < int(preset_table[sub]):
                raise ValueError(f"[harness_evolution.{key}] {sub} = {v} is below the preset's {preset_table[sub]}: a "
                                 f"floor may be raised here, not lowered")
            if (key, sub) not in HARNESS_TABLE_FLOORS and v > int(preset_table[sub]):
                raise ValueError(f"[harness_evolution.{key}] {sub} = {v} is above the preset's {preset_table[sub]}: a "
                                 f"limit may be lowered here, not raised")
        out[sub] = int(v)
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
    if improvement_cfg is None and set(table) & set(PRESET_ONLY_TABLES):
        raise ValueError(f"[harness_evolution] {sorted(set(table) & set(PRESET_ONLY_TABLES))}: only a preset sets "
                         f"these tables")
    unknown = sorted(set(table) - HARNESS_KEYS - set(PRESET_ONLY_TABLES))
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
    out = {"package_api": package_api, "package": limits.to_json(), "diagnosis": diagnosis.to_json(),
           "max_draft_repairs": repairs, "max_effort": max_effort,
           "sandbox": table.get("sandbox"), "image": table.get("image"),
           "test_timeout_seconds": int(table.get("test_timeout_seconds", 30))}
    # v0.3 step 3 (M4 §1, §2): the coding session's caps and the screen's numbers travel into K under a core whose
    # package API is v2 — the session replaces the summary call there (research.session). A v1 core carries
    # neither and keeps today's R path, so its K and the v0.1/v0.2 workspaces are untouched.
    asked = [k for k in ("research_session", "screen") if k in table]       # presence, not truthiness: {} asks too
    if asked and package_api != hp.AGENT_API_VERSION:
        raise ValueError(f"[harness_evolution] research_session/screen belong to package_api {hp.AGENT_API_VERSION}: "
                         f"a {package_api} core runs the summary-call research step")
    if asked:
        for key in ("research_session", "screen"):
            value = table.get(key, {})
            # under a preset the table is already the preset's narrowed one (a dict); without one it is the config's
            out[key] = _harness_table(key, value, None) if improvement_cfg is None else _harness_table(key, {}, value)
    # v0.5 (V0_5_PLAN §1): the method and library tables of the v5 preset, into K as the preset fixed them. They need
    # the coding session (R writes methods in it), and methods need the router's hook sites and tools.
    if any(key in table for key in PRESET_ONLY_TABLES):
        from .research.library import LibrarySettings
        from .research.methods import MethodSettings
        if not out.get("research_session"):
            raise ValueError("the methods and library tables need the coding session (package_api v2)")
        if "methods" in table:
            missing = sorted(set(METHOD_COMPONENTS) - set(limits.components))
            if missing:
                raise ValueError(f"[harness_evolution] components drop {missing}: the method library needs "
                                 f"{list(METHOD_COMPONENTS)} (methods are tools; the router is three hooks)")
            out["methods"] = MethodSettings.from_json(table["methods"]).to_json()
        if "library" in table:
            out["library"] = LibrarySettings.from_json(table["library"]).to_json()
    return out


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
                  bootstrap: bool = True, strata: bool = False) -> Activity:
    """Load the environment, freeze the splits if they are not frozen, and find or create the initial release.

    Creating it is a bootstrap authorisation, not an improvement: nothing has been compared at that point, and a
    baseline that called itself better would be claiming a result from a configuration file
    (docs/modules/00_CONTRACTS.md §4).
    """
    bench = BenchConfig.load(cfg, base)
    rsi = RsiConfig.load(cfg)
    versions = ab.require(bench.upstream_path)
    root = config_mod.workspace_root(cfg, base)
    results = Results(root, bench.domain)
    # v0.3 step 5 (M7 §2): every listed domain loads and has its own frozen split; several are pooled into one
    suite, manifest, drift = load_domains(bench, root, out=out)
    if len(bench.domains) > 1:
        # each domain's split hash and counts; `ouroloop freeze` asks for the strata too (M7 §5)
        for line in domain_lines(manifest, strata=strata):
            out(line)
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
                                        # v0.4 (V0_4_PLAN §1.7): a core with the coding session gets the session's R0
                                        _P.default_research_set(objects, note_contract=harness is not None,
                                                                session=bool((harness or {}).get("research_session")),
                                                                # v0.5: a K with the method library
                                                                methods=bool((harness or {}).get("methods"))),
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


def final_share_problem(frozen: dict | None, pricing, concurrency: int) -> str:
    """Why the frozen final share cannot hold `concurrency` reservations of one call's bound at once, or ""
    (docs/modules/05_EXECUTION_BUDGET.md §2 “final 份额的定法”, v0.3): every call in flight holds its worst case until
    it settles, so a share of less than concurrency × bound refuses calls while there is money left — v0.2's first
    final ($50 share, 4 × $7.28 in flight) recorded 51 attempts as budget_exhausted in 78 seconds. Checked where a
    budget is frozen, before any root opens with it; only with a bound the prices make derivable."""
    share = budget_allocations(frozen).get("final")
    if not share or pricing is None or not getattr(pricing, "bounded", False):
        return ""
    n = max(int(concurrency or 1), 1)
    bound = pricing.bound()
    short = {dim: (float(value), round(n * float(bound[dim]), 6)) for dim, value in share.items()
             if dim in bound and n * float(bound[dim]) > float(value) + 1e-9}
    if not short:
        return ""
    return ("the final share cannot hold the concurrent reservations: " + "; ".join(
        f"{dim} {have} < concurrency {n} x one call's bound {round(float(bound[dim]), 6)} = {need}"
        for dim, (have, need) in short.items()))


def _execution_of(objects, release_ref: str | None):
    """What a release executes a task with — its W and its K (`Improvement._runs_like`): R takes no part in a run,
    and a gate's candidate release and the release that installs it are two refs of one execution. None for a ref
    this store does not hold."""
    try:
        release = objects.load(release_ref, _ct.SystemRelease)
    except (KeyError, TypeError, _ct.ContractError):
        return None
    return (release.worker_ref, release.core_ref)


def final_estimate(activity: Activity, *, task_ids, repeats: int, arms: dict, plan_ref: str | None = None,
                   pricing=None, concurrency: int = 1, fallback=None) -> dict:
    """What a final is expected to spend, from what is on record (M5 §2 “final 份额的定法”, v0.3): for each arm, the
    attempts still to run (tasks × repeats less the plan's valid attempts already on record) times the arm's
    measured cost per valid attempt — over the readings of every release that executes as the arm's release does
    (the same W and K: a W*'s gate readings were made under its candidate release, which is another ref than the
    release that installed it; 2026-10-05 review), its dev readings (baseline, control, candidate and confirmation
    arms) when it has any, else all of its priced readings — and `concurrency` reservations of one call's bound,
    which the share must hold beside the spend. Without a priced reading the arm's cost is `fallback` (a number, or
    {arm: number} — the config's [estimate] cost_per_attempt) when given, else unknown; never 0."""
    rows = list(activity.results.all()) if activity.results is not None else []
    planned = len(tuple(task_ids)) * int(repeats)
    out, total, unknown = [], 0.0, []
    executions: dict = {}

    def execution(ref):
        if ref not in executions:
            executions[ref] = _execution_of(activity.objects, ref)
        return executions[ref]
    for arm, release in arms.items():
        target = execution(release)
        readings = [r for r in rows if r.validity == "valid" and r.cost_usd is not None and r.system_release_ref
                    and (r.system_release_ref == release or (target is not None
                                                             and execution(r.system_release_ref) == target))]
        pool = [r for r in readings if r.split == "dev"] or readings
        cpa = round(sum(r.cost_usd for r in pool) / len(pool), 6) if pool else None
        source = ("dev" if pool[0].split == "dev" else "all") if pool else None
        if cpa is None and fallback is not None:
            given = fallback.get(arm) if isinstance(fallback, dict) else fallback
            if given is not None:
                cpa, source = round(float(given), 6), "config"
        done = {r.pair_key for r in rows if plan_ref and r.candidate == arm and r.validity == "valid"
                and str(r.pair_key).startswith(f"{plan_ref}/")}
        todo = max(planned - len(done), 0)
        expected = None if cpa is None else round(todo * cpa, 6)
        if expected is None:
            unknown.append(arm)
        else:
            total += expected
        out.append({"arm": arm, "release": release, "attempts": todo, "done": len(done), "readings": len(pool),
                    "readings_split": source or "none", "cost_per_attempt": cpa, "expected": expected})
    bound = pricing.bound().get("usd") if pricing is not None and getattr(pricing, "bounded", False) else None
    reservations = None if bound is None else round(max(int(concurrency or 1), 1) * float(bound), 4)
    expected = None if unknown else round(total, 6)
    return {"arms": out, "expected": expected, "unknown": unknown, "concurrency": max(int(concurrency or 1), 1),
            "bound": None if bound is None else round(float(bound), 4), "reservations": reservations,
            "suggested_share": None if expected is None or reservations is None else round(expected + reservations, 2)}


# Settlements that never come back (a killed `claude` child, an answer that never arrived) keep their reservation:
# v0.1/v0.2 and the ceiling run left 4 in ~9,900 calls, about 1 in 2,250 (2026-10-05 review). A final's share holds
# this many beside its lanes; the root is sized with the same rate (ROADMAP v0.3 step 5).
STUCK_BOUNDS = 2
ESTIMATE_KEYS = {"cost_per_attempt"}


def estimate_settings(cfg: dict):
    """The config's `[estimate] cost_per_attempt` (a number, or {arm: number}): the cost per attempt a final's
    estimate uses for an arm with no priced reading yet (M5 §2, 2026-10-05 review). None when absent. A planning
    number, never a limit and never part of a plan."""
    table = dict(cfg.get("estimate") or {})
    unknown = sorted(set(table) - ESTIMATE_KEYS)
    if unknown:
        raise ValueError(f"[estimate] does not take {unknown}; it takes {sorted(ESTIMATE_KEYS)}")
    value = table.get("cost_per_attempt")
    if value is None:
        return None
    numbers = value.values() if isinstance(value, dict) else [value]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in numbers):
        raise ValueError(f"[estimate] cost_per_attempt = {value!r}: a positive number of dollars, or one per arm")
    return dict(value) if isinstance(value, dict) else float(value)


def final_plan_problem(activity: Activity, cfg: dict, plan: "FinalizationPlan", frozen_budget: dict | None,
                       out: Callable[[str], None]) -> str:
    """Why the final share cannot hold this final plan, or "" (M5 §2 “final 份额的定法”; 2026-10-05 review: the share
    failed silently once W* cost more than the share was sized for). Before the plan is sealed the estimate is
    printed — attempts × each arm's measured cost per attempt from its pooled readings, else the config's
    [estimate] — and checked: the expected spend plus `concurrency` in-flight bounds plus STUCK_BOUNDS stuck ones
    must fit the share's dollars. Only for a share in dollars under prices that give a bound."""
    share = budget_allocations(frozen_budget).get("final") or {}
    pricing = pricing_of(cfg)
    if share.get("usd") is None or not getattr(pricing, "bounded", False):
        return ""
    estimate = final_estimate(activity, task_ids=plan.task_ids, repeats=plan.repeats,
                              arms={c["subject"]: plan.release_refs.get(c["subject"]) or activity.release_ref
                                    for c in plan.comparisons if not c.get("llm")},
                              pricing=pricing, concurrency=activity.bench.concurrency,
                              fallback=estimate_settings(cfg))
    for line in estimate_lines(estimate, float(share["usd"])):
        out(line)
    if estimate["expected"] is None:
        return (f"the final share cannot be checked: no priced reading of {estimate['unknown']} and no [estimate] "
                f"cost_per_attempt for them")
    need = estimate["expected"] + estimate["reservations"] + STUCK_BOUNDS * estimate["bound"]
    if need > float(share["usd"]) + 1e-9:
        return (f"the final share ${float(share['usd']):.2f} cannot hold the plan: expected ${estimate['expected']:.2f} + "
                f"{estimate['concurrency']} in-flight and {STUCK_BOUNDS} stuck reservations of "
                f"${estimate['bound']:.4f} = ${need:.2f}")
    return ""


def estimate_lines(estimate: dict, share_left: float | None = None) -> list[str]:
    """The printed form of final_estimate: one line per arm and the total against the share."""
    lines = []
    for a in estimate["arms"]:
        cost = "unknown (no costed reading of this release)" if a["cost_per_attempt"] is None else \
            (f"${a['cost_per_attempt']:.4f} from the config's [estimate] = ${a['expected']:.2f}"
             if a["readings_split"] == "config" else
             f"${a['cost_per_attempt']:.4f} from {a['readings']} {a['readings_split']} readings = ${a['expected']:.2f}")
        lines.append(f"estimate   {a['arm']} ({_ct.short(a['release']) if a['release'] else '?'}): {a['attempts']} "
                     f"attempts to run ({a['done']} on record) x {cost}")
    total = "unknown" if estimate["expected"] is None else f"${estimate['expected']:.2f}"
    held = "" if estimate["reservations"] is None else \
        f" + {estimate['concurrency']} in-flight reservations x ${estimate['bound']:.4f} = ${estimate['reservations']:.2f}"
    tail = ""
    if share_left is not None and estimate["expected"] is not None and estimate["reservations"] is not None:
        need = estimate["expected"] + estimate["reservations"]
        tail = (f"; the share has ${share_left:.2f} left: " +
                ("holds it" if share_left + 1e-9 >= need else f"SHORT by ${need - share_left:.2f} (lanes will wait "
                                                              f"for reservations; lower --concurrency)"))
    lines.append(f"estimate   expected spend {total}{held}{tail}")
    return lines


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
                     per_attempt_budget: dict | None = None, control_pooling: str | None = None,
                     confirmation_repeats: int | None = None) -> str:
    """The development plan template: tasks, repeats, order, thresholds and statistics, frozen before the first
    candidate runs. A candidate's own artifact ref is filled in later; the thresholds are not touched again.

    A gate plan with `min_tasks` refuses to freeze over fewer tasks (M3 §2.1(f), v0.3) — here, so the mainline gate
    and a research slot's inner gate meet the same rule: twelve tasks let a third of empty candidates through, and
    no number read off such a gate tells two research policies apart."""
    task_ids = tuple(task_ids)
    if gate.min_tasks is not None and len(task_ids) < int(gate.min_tasks):
        raise ValueError(f"freeze refused: the gate plan runs {len(task_ids)} tasks, fewer than its min_tasks "
                         f"{gate.min_tasks} (M3 §2.1(f))")
    return objects.put(TaskEvalPlan(candidate_ref="", control_ref="", parent_release="", core_ref=core_ref,
                                    split_ref=split_ref, task_ids=task_ids, repeats=repeats,
                                    order_seed=order_seed, screen_ids=tuple(screen_ids),
                                    gate_plan=gate.to_json(), installs=installs,
                                    per_attempt_budget=dict(per_attempt_budget or {}),
                                    control_pooling=control_pooling, confirmation_repeats=confirmation_repeats))


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
        control_pooling=template.control_pooling if template else None,
        confirmation_repeats=template.confirmation_repeats if template else None,
        plan_installs=template.installs if template and installs is None else (True if installs is None
                                                                               else installs))


def _pooling_of(activity: Activity) -> tuple[str | None, int | None]:
    """The activity's frozen template's (control_pooling, confirmation_repeats), for the comparisons that are built
    beside it — a research slot's inner gates (M6 §2, v0.3) — or (None, None) without a frozen template."""
    template = _frozen_plan(activity)
    return (template.control_pooling, template.confirmation_repeats) if template is not None else (None, None)


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
        def run(arm, pairs, retry=0, supersedes=None, _work=work, _candidate=candidate, _role=role):
            return _work.run_arm(arm, activity.release_ref, pairs, plan_ref="", candidate=_candidate, role=_role,
                                 retry_index=retry, resume=True, supersedes=supersedes)
        # Stage 0/1 review (M5 §3): attempts lost to infrastructure are refilled once, also on a restart that finds
        # them recorded — the 16-way trial's 429 casualties would otherwise be holes in W0's pooled dev reading
        rows = latest_attempts(_refill_attempts(run(candidate, order), run, candidate, work.retries()))
        summary = summarise(rows)
        readings = [summary]
        if not own and activity.bench.control_readings > 1:
            # v0.4 (V0_4_PLAN §1.6, M3 §2.1(a) v0.4): W0's further dev readings, unconditionally, before any gate —
            # each its own run of the same execution identity: only the candidate label differs (`base:reading-2`,
            # in the run's hash, so a reading is a run of its own; a label the pooled control does not compare), so
            # the pool counts them as readings, a restart resumes each, and the first keeps its identity
            n = activity.bench.control_readings
            for i in range(2, n + 1):
                name = f"{candidate}:reading-{i}"

                def again(arm, pairs, retry=0, supersedes=None, _work=work, _name=name, _role=role):
                    return _work.run_arm(arm, activity.release_ref, pairs, plan_ref="", candidate=_name, role=_role,
                                         retry_index=retry, resume=True, supersedes=supersedes,
                                         notes=f"control reading of the baseline ([bench] control_readings = {n})")
                readings.append(summarise(latest_attempts(_refill_attempts(again(candidate, order), again, candidate,
                                                                           work.retries()))))
                out(f"arm        {name} ({role}) dev: {readings[-1]['passed']}/{readings[-1]['tasks']} passed")
        # The label is what actually ran: the pinned actor, unless the arm names its own model.
        label = model_identity(provider, llm) if (own or pinned is None) else \
            {"provider": pinned.provider, "model": pinned.base_model, "actor_identity_ref": pinned.ref()}
        report["arms"][candidate] = {"role": role, "summary": summary, "model": label,
                                     "terminations": summary["terminations"],
                                     "failures": {r.task_id: r.failure_class for r in rows if not r.success},
                                     **({"control_readings": [{k: s[k] for k in ("passed", "tasks", "pass_rate")}
                                                              for s in readings]} if len(readings) > 1 else {})}
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
            researcher=None, segment: str | None = None, stop_after_round: int | None = None,
            reason: str | None = None) -> dict:
    """The worker loop with a fixed R: collect, diagnose, propose, build, evaluate, install, repeat.

    Every round starts from what the previous one left — the installed release, the appended experience and the
    results that produced it — which is what makes this a loop rather than a series of independent trials.

    With `segment` (the cycle's worker segments, docs/modules/07_BENCH_REPORT.md §1), the segment keeps its own
    record of completed rounds: calling it again resumes at the round it could not complete, a finished segment
    is not run again, and its collection batches are counted per segment across restarts.

    `stop_after_round` (a cycle's stop point, docs/ROADMAP.md §3) stops the segment once that many rounds plus one
    have completed, without finishing it: the next call continues with the following round, with patience and the
    failure limit where they were, as if it had not stopped.

    `rounds` with `reason` on a cycle activity whose last worker segment has ended (docs/modules/07_BENCH_REPORT.md
    §1, 2026-10-06) is an explicit extension beyond the frozen schedule: it is recorded (`schedule_extended`), the
    segment's log is reopened, and `rounds` more rounds run under the same plans, root and K with the round and
    diagnosis indices continuing; patience and the failure limit count afresh within the extension.
    """
    from .research.loop import RoundContext, run_worker_rounds
    activity = open_activity(cfg, base, out=out)
    missing = activity.rsi.check(activity.objects, needs=("task_plan_ref",))
    if missing:
        raise ValueError("; ".join(missing) + "; freeze one with bench.freeze_task_plan")
    require(startup_checks(activity, cfg, phase="improve"), phase="improve")
    extension = None
    if segment is None and activity.rsi.schedule_ref:
        segment, extension = _extension_of(activity, None if rounds is None else int(rounds), reason)
    seglog = RoundLog(Path(activity.root) / "rsi" / "segments" / f"{segment}.json",
                      {"segment": segment, "scope": activity.rsi.scope_id,
                       "schedule": activity.rsi.schedule_ref}) if segment else None
    if extension == "new":
        frozen = Schedule(**activity.objects.get(activity.rsi.schedule_ref))
        record = seglog.extend(int(rounds), str(reason), by="user")
        activity.releases.events.append(
            "schedule_extended", scope=activity.rsi.scope_id, segment=segment, by="user", reason=str(reason),
            rounds=int(rounds), at_round=record["at_round"], frozen_worker_rounds=frozen.worker_rounds,
            frozen_patience=frozen.patience,
            extended_total=sum(int(e["rounds"]) for e in seglog.state.get("extensions") or ()))
        out(f"extend     {segment}: {rounds} more round(s) from round {record['at_round']}, beyond the frozen schedule "
            f"(worker_rounds {frozen.worker_rounds}, patience {frozen.patience}); reason: {reason}")
    elif extension == "open":
        out(f"extend     {segment}: continuing the extension begun at round {seglog.open_extension()['at_round']}")
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
        diagnosis, drafts, session = work.round_inputs(index, evidence, experience, bound, root=activity.root,
                                                       research_model=research_model)
        log.append({"round": index, "release": release_ref, "feedback": evidence["feedback"].summary()})
        return RoundContext(releases=activity.releases, objects=activity.objects, scope_id=activity.rsi.scope_id,
                            context=context, researcher=researcher, bound=bound, core_ref=work.core_ref,
                            eval_template_ref=work.template_ref, handlers=work.handlers(),
                            experience_ref=experience_ref, experience=tuple(experience), owner="worker",
                            schedule=schedule,
                            history=frozen_signatures(experience, activity.objects, parent=release_ref),
                            root=activity.root,
                            diagnosis=diagnosis, drafts=drafts, round_index=index, session=session)

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
    history: tuple = tuple(seglog.state["rounds"]) if seglog is not None else ()
    if seglog is not None:
        # Only the rounds this segment has not completed; make_round numbers them after the completed ones.
        done = seglog.done()
        schedule = Schedule(**{**asdict(schedule), "worker_rounds": max(schedule.worker_rounds - done, 0)})
        ext = seglog.open_extension()
        if ext is not None:
            # An extension runs its own rounds, numbered after the segment's; patience and the failure limit count
            # only its own rounds (the segment's idle rounds would stop it before its first round).
            history = tuple(seglog.state["rounds"][ext["history_start"]:])
            from .research.loop import completed_round
            own = sum(1 for r in history if completed_round(r))
            schedule = Schedule(**{**asdict(schedule), "worker_rounds": max(int(ext["rounds"]) - own, 0)})
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
        # the research meter's rate-limit refusals, per round (M5 §2, v0.3), then the segment's own record
        on_result=_rate_limit_recorder(researcher, activity.releases.events, scope=activity.rsi.scope_id,
                                       first_round=done, on_result=seglog.add if seglog is not None else None),
        history=history, stop_after=stop_after,
        # a batch booked before a crash is finished first, at its own attempt index, before any round runs
        resume_collections=(lambda: work.finish_collections(activity.root, measured.get("last")))
        if work.harness else None))
    stop_reason = next((r.reason for r in reversed(results) if r.outcome == "stopped"), "rounds_done")
    if stop_reason == "stopped_at":
        pass                                # the stop point, not the segment's end: the segment is not finished
    elif seglog is not None:
        seglog.end(stop_reason)
        stop_reason = seglog.state["stop_reason"]
        if extension is not None:
            _record_extension(activity, segment, seglog)
    for result in results:
        out(f"round      {result.outcome}"
            + (f" experiment={result.experiment_id}" if result.experiment_id else "")
            + (f" release {_ct.short(result.release_before)} -> {_ct.short(result.release_after)}"
               if result.release_after and result.release_after != result.release_before else "")
            + (f" ({result.reason})" if result.reason else ""))
    return {"rounds": [vars(r) for r in results], "log": log, "budget": budget.remaining(),
            "release": activity.releases.active()["release_ref"], "stop_reason": stop_reason,
            **({"segment": segment} if segment else {}),
            **({"extension": seglog.state["extensions"][-1]} if extension is not None else {})}


# ---------------------------------------------------------------------------- v0.5: the learn reading, the library track
def _phase_work(cfg: dict, base, phase: str, out):
    """(activity, Improvement, budget) of a v0.5 phase: the frozen task plan, the startup checks, the actor the
    release pins, the phase's account on the activity's one root."""
    activity = open_activity(cfg, base, out=out)
    missing = activity.rsi.check(activity.objects, needs=("task_plan_ref",))
    if missing:
        raise ValueError("; ".join(missing) + "; freeze one with `ouroloop freeze`")
    require(startup_checks(activity, cfg, phase=phase), phase=phase)
    provider = config_mod.build_provider(cfg["llm"])
    work = improvement(activity, template_ref=activity.rsi.task_plan_ref, provider=provider)
    budget = activity_budget(activity, cfg, phase)
    work.budget = budget
    work.pricing = pricing_of(cfg)
    return activity, work, budget


def _fixed_context(activity: Activity, work, *, research_model: dict | None = None):
    """The RoundContext of a fixed-code round (research.fixed): this round's measurement (reused when nothing was
    installed since), the experience, the bound release, the draft check and the screen inputs. -> (ctx, evidence)."""
    from .research.fixed import next_ordinal
    from .research.loop import RoundContext
    release_ref = activity.releases.active()["release_ref"]
    evidence = work.measure(release_ref, work.last_measurement(release_ref))
    experience, experience_ref = _experience.snapshot(activity.root)
    context = work.context(goal="improve the full-task pass rate on the development tasks", release_ref=release_ref,
                           evidence=evidence, experience=experience, experience_ref=experience_ref)
    bound = _P.bind_release(release_ref, profile="task", services=work.services(), authorization="installed")
    index = next_ordinal(experience)
    diagnosis, drafts, session = work.round_inputs(index, evidence, experience, bound, root=activity.root,
                                                   research_model=research_model or {})
    ctx = RoundContext(releases=activity.releases, objects=activity.objects, scope_id=activity.rsi.scope_id,
                       context=context, researcher=None, bound=bound, core_ref=work.core_ref,
                       eval_template_ref=work.template_ref, handlers=work.handlers(), experience_ref=experience_ref,
                       experience=tuple(experience), owner="worker",
                       history=frozen_signatures(experience, activity.objects, parent=release_ref),
                       root=activity.root, diagnosis=diagnosis, drafts=drafts, round_index=index, session=session)
    return ctx, evidence


def _research_completion(cfg: dict, budget, step: str):
    """(system, user) -> text on the research model, metered on the activity's book as `<scope>:research:<step>`: the
    library's distillation and merge calls are research, and cost what research costs."""
    from .evolve.execution import MeteredProvider
    table = dict(cfg.get("research") or {}).get("llm") or cfg["llm"]
    provider = MeteredProvider(config_mod.build_provider(table), budget, pricing_of(cfg),
                               operation=f"{budget.scope_id}:research:{step}", max_retries=0)
    return _Completions(provider).complete


def learn(cfg: dict, base: str | Path = ".", *, out: Callable[[str], None] = print) -> dict:
    """`--phase learn` (docs/design/V0_5_PLAN.md §1.3; V0_5_METHOD_LIBRARY.md §3), train only.

    The installed W's methods are replayed over its recorded train attempts (free, in the worker); the train tasks
    where a trigger fires — at most `learn_max_tasks` — run once under each of two explore twins, which differ from W
    only in the table: every switch decision is on in one twin and off in the other by a hash of the task text, with
    exact propensities. The paired outcomes are credited by the methods' footprint classes and fitted into a greedy
    table (Beta posteriors; n >= 5, P >= 0.8, task-level fixes - regressions >= 0, must-not <= 1, the cost guard),
    recorded as a `method_fit` row. A table that differs from the installed one is a refit candidate in its own
    fixed round: screen, gate, confirmation, install, as for any candidate. Dev and test never run a twin (run_arm
    refuses an explore table off train)."""
    from .agent import harness_package as hp
    from .research import methods as M
    from .research.fixed import fixed_candidate, next_ordinal, run_fixed_round
    activity, work, budget = _phase_work(cfg, base, "learn", out)
    settings = work.method_settings()
    if settings is None:
        raise ValueError("--phase learn needs a K with the method library (preset automationbench_harness_v5)")
    scope = activity.rsi.scope_id
    release_ref = activity.releases.active()["release_ref"]
    bound = _P.bind_release(release_ref, profile="task", services=work.services(), authorization="installed")
    run_files, test_files = hp.files_of(activity.objects, bound.package.ref)
    files = {**run_files, **test_files}
    decls = {n: d for n, d in M.methods_of(files).items() if d.get("enabled", True)}
    if not decls:
        out("learn      the installed W has no enabled method: nothing to learn")
        return {"learned": False, "reason": "no method"}
    fired = work.trigger_replay(files)
    order = list(activity.splits.ids("train"))
    tasks = [t for t in order if t in fired][:settings.learn_max_tasks]
    if not tasks:
        out("learn      no recorded train attempt fires a method's trigger: nothing to learn")
        return {"learned": False, "reason": "no trigger fires on train"}
    fits = [e for e in _experience.read(activity.root) if e.get("kind") == "method_fit" and e.get("scope") == scope]
    stage = f"learn{len(fits)}"
    salt = M.learn_salt(work.order_seed, stage)
    out(f"learn      {stage}: {len(tasks)} train tasks where a trigger fires, twins A and B (salt {salt})")
    readings: dict = {}
    for twin in ("A", "B"):
        candidate = work.package_candidate(M.twin_files(files, salt=salt, twin=twin), label=f"learn twin {twin} {stage}")
        rows = work.train_screen(candidate, tasks, arm=f"learn_{twin}", notes=f"v0.5 {stage}")
        if any(r.validity != "valid" and r.failure_class == "budget_exhausted" for r in rows):
            out(f"learn      twin {twin} ran out of budget: nothing is fitted")
            return {"learned": False, "reason": "budget_exhausted", "stage": stage}
        for r in rows:
            if r.validity == "valid":
                readings.setdefault(r.task_id, {})[twin] = r
    pairs = [{"task": t, **{tw: {"success": r.success, "cost": r.cost_usd,
                                 "assertions": activity.results.grading(r.attempt_id),
                                 "decisions": M.decisions_of(work.transcript_of(r))} for tw, r in two.items()}}
             for t, two in ((t, readings.get(t, {})) for t in tasks) if set(two) == {"A", "B"}]
    obs = M.observations(pairs, decls)
    prior = {k: {"k": v.get("k", 0), "m": v.get("m", 0)} for k, v in ((fits[-1].get("cells") or {}).items()
                                                                       if fits else ())}
    current = M.table_of(files) or {"mode": "greedy", "cells": {}, "splits": {}}
    table, record = M.fit_table(obs, decls, current, settings, prior)
    changed = M.table_changed(current, table)
    moved = sorted(k for k in table["cells"] if (current.get("cells") or {}).get(k) != table["cells"][k])
    _experience.append(activity.root, {"kind": "method_fit", "scope": scope, "release": release_ref, "stage": stage,
                                       "salt": salt, "tasks": tasks, "pairs": len(pairs), "observations": len(obs),
                                       "cells": record, "table": table, "changed": changed, "moved": moved})
    out(f"learn      fitted {len(record)} cells from {len(obs)} paired decisions on {len(pairs)} tasks; "
        + (f"moved {moved}" if changed else "the table stays as installed"))
    result = {"learned": True, "stage": stage, "tasks": tasks, "pairs": len(pairs), "cells": record,
              "changed": changed, "moved": moved}
    if not changed:
        return result
    rates = M.call_rates([(r, work.transcript_of(r)) for r in work.latest_train().values()], list(decls))
    refit, disabled = M.refit_files(files, table, call_rates=rates, settings=settings)
    methods_moved = {k.split("|", 1)[0] for k in moved}
    failing = [t for t in tasks if set(fired.get(t) or ()) & methods_moved]
    ctx, _ = _fixed_context(activity, work)
    candidate = fixed_candidate(operator="refit", files=refit, site="after_tool", carrier="hook", failing=failing,
                                mechanism=f"refit method table from paired train rewards {stage}",
                                trigger_condition="the cells the learn reading moved: " + ", ".join(moved)[:400],
                                notes=f"disabled {disabled}" if disabled else "")
    ordinal = next_ordinal(_experience.read(activity.root))
    round_result = run_fixed_round(ctx, budget, [candidate], ordinal=ordinal)
    out(f"refit      {round_result.outcome}" + (f" ({round_result.reason})" if round_result.reason else ""))
    return {**result, "refit": {"outcome": round_result.outcome, "release": activity.releases.active()["release_ref"],
                                "disabled": disabled}}


def library_track(cfg: dict, base: str | Path = ".", *, out: Callable[[str], None] = print, distiller=None,
                  merger=None) -> dict:
    """`--phase library` (docs/design/V0_5_PLAN.md §1.4; V0_5_EVOLVING_SEARCH.md §2.4), train only.

    A stream over the train split in a frozen order (seeded by the plan's order_seed and the domain, its hash
    recorded): at each task the W_base readings of it already on record are distilled (a success, or a success and
    a failure as a contrast pair); the task is read once under W_base ⊕ L_n — the library so far, retrieved by the
    host with the task's own entries left out — and once more with the failed checks as a note when that failed;
    every new success is distilled (one call of the fixed prompt, at most `max_per_distill` entries), each entry
    checked (schema, the literal check with one placeholder pass, at least `min_other_tasks` train tasks reached
    beyond its sources), merged into a near-duplicate or added; every entry shown in an attempt is credited
    r − b(t); entries that harm or do not pay are retired. The stream's state is saved after every task, so an
    interrupted phase continues where it stopped. At its end D and B are read (V0_5_PLAN §3): with D <= abort_d or
    B >= abort_b the snapshot is not gated; otherwise the library is compiled into W_base's package and offered in a
    fixed round — screen with leave-one-out retrieval, gate, confirmation, install — like any candidate."""
    import random
    from .agent import harness_package as hp
    from .research import library as lib
    from .research.diagnosis import DiagnosisLimits, failure_summary
    from .research.fixed import fixed_candidate, next_ordinal, run_fixed_round
    activity, work, budget = _phase_work(cfg, base, "library", out)
    settings = work.library_settings()
    if settings is None:
        raise ValueError("--phase library needs a K with the experience library (preset automationbench_harness_v5)")
    root = Path(activity.root)
    state_path = root / "rsi" / "library_stream.json"
    order = list(activity.splits.ids("train"))
    stream = list(order)
    random.Random(f"library:{work.order_seed}:{activity.bench.domain}").shuffle(stream)
    order_hash = digest({"order": stream})[:16]
    release_ref = activity.releases.active()["release_ref"]
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
        "schema": "library_stream.v1", "order": stream, "order_hash": order_hash, "base_release": release_ref,
        "cursor": 0, "entries": {}, "readings": {}, "distill_calls": 0, "dropped": [], "status": "streaming",
        "utility_at_half": None, "distill_prompt": digest({"distill": lib.DISTILL, "merge": lib.MERGE})[:16]}
    if state["order_hash"] != order_hash:
        raise ValueError(f"{state_path}: the stream's order {state['order_hash']} is not this plan's {order_hash}")
    if state["status"] == "done":
        out(f"library    the stream and its snapshot round are done ({state.get('snapshot')})")
        return {"status": "done", "readout": state.get("readout"), "snapshot": state.get("snapshot")}
    if state["base_release"] != release_ref and state["status"] == "streaming":
        raise ValueError(f"the stream runs on W_base {_ct.short(state['base_release'])}, and the pointer is now "
                         f"{_ct.short(release_ref)}: a stream is not continued over another W")

    def save() -> None:
        write_json(state_path, state)
    base_bound = _P.bind_release(state["base_release"], profile="task", services=work.services(),
                                 authorization="installed")
    run_files, test_files = hp.files_of(activity.objects, base_bound.package.ref)
    base_files = {**run_files, **test_files}
    limits = hp.Limits.from_json(work.harness["package"])
    b = work.base_means(state["base_release"])
    texts = {t: work.task_text(t) for t in order}
    ctx, _ = _fixed_context(activity, work)
    forbidden, allowed = ctx.drafts.forbidden(), set(ctx.drafts.allowed_literals)
    distiller = distiller or _research_completion(cfg, budget, "distill")
    note_limits = DiagnosisLimits(**work.harness["diagnosis"])
    entries = list(state["entries"].values())

    def active() -> list:
        return [e for e in entries if e.get("status") == "active"]

    def summary(row) -> dict:
        return failure_summary(row, work.transcript_of(row), activity.results.grading(row.attempt_id), note_limits)

    def merge_call(old: dict, new: dict):
        if merger is not None:
            return merger(old, new)
        got = lib.parse_distilled(distiller(lib.MERGE, json.dumps({"a": old, "b": new}, default=str)), max_entries=1)
        return got[0] if got else None

    def distill(task: str, success, failure, origin: str, n: int) -> None:
        system, user = lib.distill_request(summary(success), summary(failure) if failure is not None else None,
                                           max_entries=settings.max_per_distill)
        state["distill_calls"] += 1
        sources = {lib.task_hash(texts[x]) for x in lib.near_duplicates(task, texts, settings.sibling_sim)}
        for raw in lib.parse_distilled(distiller(system, user), max_entries=settings.max_per_distill):
            entry = lib.make_entry(raw, sources=sources, origin=origin, n=n,
                                   provenance=[{"task": task, "attempt": success.attempt_id}])
            checked, why = lib.check_entry(entry, forbidden=forbidden, train_texts=texts, settings=settings,
                                           allowed=allowed, library=active())
            if checked is None:
                state["dropped"].append({"n": n, "origin": origin, "why": why})
                continue
            dup = lib.duplicate_of(checked, active(), settings)
            if dup is not None:
                joined = lib.merged(dup, checked, merge_call)
                joined["stats"] = dict(dup.get("stats") or {})
                dup["status"], dup["merged_into"] = "merged", joined["entry_id"]
                entries.append(joined)
            else:
                entries.append(checked)
        for e in lib.shelf_overflow(entries, settings):
            e["status"], e["retired"] = "retired", "pushed off a full shelf"

    def shown_in(row) -> list:
        return next((list(e.get("entries") or ()) for e in work.transcript_of(row)
                     if e.get("kind") == "library_retrieval"), [])

    def credit(row, task: str) -> None:
        by_id = {e["entry_id"]: e for e in entries}
        base_mean = b.get(task)
        for entry_id in shown_in(row):
            if entry_id in by_id:
                lib.credit(by_id[entry_id], int(bool(row.success)), base_mean, stable_pass=base_mean == 1.0)

    def attempt(task: str, arm: str, extra=None):
        """One train attempt of `task` under W_base ⊕ the active library (W_base itself while the library is
        empty): the first valid row, or (None, why)."""
        files_n = lib.with_library(base_files, active(), settings, max_chars=limits.max_skill_chars,
                                   max_skills=limits.max_skills)
        # the retry is the second attempt at the task under the same package: repeat index 1, its own attempt id
        repeat = 1 if arm == "library_retry" else 0
        if files_n == base_files:
            rows = work.run_arm(arm, state["base_release"], [(task, repeat)], plan_ref="", split="train",
                                candidate="current", role="evolved", resume=True, notes=f"v0.5 library stream {arm}",
                                extra_system=extra)
        else:
            candidate = work.package_candidate(files_n, label=f"library stream {lib.task_hash(texts[task])}")
            rows = work.train_screen(candidate, [task], arm=arm, notes=f"v0.5 library stream {arm}", extra_system=extra,
                                     repeat=repeat)
        valid = [r for r in rows if r.validity == "valid"]
        if valid:
            return valid[0], None
        return None, ("budget_exhausted" if any(r.failure_class == "budget_exhausted" for r in rows) else "lost")

    from .evolve.execution import BudgetExhausted
    try:
        for i in range(int(state["cursor"]), len(stream)):
            task = stream[i]
            mine = [r for r in work.train_rows() if r.task_id == task and r.system_release_ref == state["base_release"]
                    and _arm_of(r.attempt_id) == "measure"]
            had_success = next((r for r in reversed(mine) if r.success), None)
            had_failure = next((r for r in reversed(mine) if not r.success), None)
            if had_success is not None:
                distill(task, had_success, had_failure, "contrast" if had_failure is not None else "success", i)
            first, why = attempt(task, "library_first")
            if first is None and why == "budget_exhausted":
                raise BudgetExhausted(f"the first encounter of stream position {i} could not run")
            reading = {"n": i, "control": b.get(task), "lib": None, "retry": None, "shown": []}
            if first is not None:
                credit(first, task)
                reading.update(lib=int(bool(first.success)), shown=shown_in(first), attempt=first.attempt_id)
                if first.success and had_success is None:
                    distill(task, first, had_failure, "contrast" if had_failure is not None else "success", i)
                elif not first.success and settings.retry:
                    note = lib.retry_note(activity.results.grading(first.attempt_id))
                    second, why = attempt(task, "library_retry", {task: note}) if note else (None, "no failed check")
                    if second is None and why == "budget_exhausted":
                        raise BudgetExhausted(f"the retry of stream position {i} could not run")
                    if second is not None:
                        credit(second, task)
                        reading.update(retry=int(bool(second.success)), retry_attempt=second.attempt_id)
                        if second.success:
                            distill(task, second, first, "reflexion", i)
            for e in active():
                why_retired = lib.retire_reason(e, settings)
                if why_retired:
                    e["status"], e["retired"] = "retired", why_retired
            state["readings"][task] = reading
            if i + 1 == len(stream) // 2:
                state["utility_at_half"] = {e["entry_id"]: lib.utility(e) for e in active()}
            state["cursor"] = i + 1
            state["entries"] = {e["entry_id"]: e for e in entries}
            save()
            out(f"library    n={i + 1}/{len(stream)} first={reading['lib']} retry={reading['retry']} "
                f"control={reading['control']} active={len(active())} shown={len(reading['shown'])}")
    except BudgetExhausted as e:
        state["entries"] = {x["entry_id"]: x for x in entries}
        save()
        out(f"library    stopped: {e}")
        return {"status": "budget_exhausted", "cursor": state["cursor"]}
    readout = lib.stream_readout(state["readings"], stream)
    readout["p_second_half_better"] = lib.bootstrap_halves(state["readings"], stream)
    half = len(stream) // 2
    later: dict = {}
    for t in stream[half:]:
        r = state["readings"].get(t) or {}
        if r.get("lib") is None or r.get("control") is None:
            continue
        for entry_id in r.get("shown") or ():
            later.setdefault(entry_id, []).append(r["lib"] - r["control"])
    at_half = state.get("utility_at_half") or {}
    paired = [(at_half[k], sum(v) / len(v)) for k, v in sorted(later.items()) if k in at_half]
    readout["credit_rank_correlation"] = lib.rank_correlation([a for a, _ in paired], [b_ for _, b_ in paired])
    readout["entries"] = {"active": len(active()), "retired": sum(1 for e in entries if e.get("status") == "retired"),
                          "merged": sum(1 for e in entries if e.get("status") == "merged"),
                          "dropped_at_check": len(state["dropped"]), "distill_calls": state["distill_calls"]}
    readout["shown_per_attempt"] = round(sum(len((state["readings"][t] or {}).get("shown") or ())
                                             for t in state["readings"]) / max(len(state["readings"]), 1), 3)
    state["readout"] = readout
    for e in entries:
        # the entry's own kind (workflow, insight, api_note) travels as `entry_kind`: the row's `kind` is the record's
        _experience.append(activity.root, {**{k: e.get(k) for k in ("entry_id", "shelf", "title", "when", "body",
                                                                    "avoid", "origin", "born_at_n", "status", "stats",
                                                                    "retired", "merged_into", "source_hashes",
                                                                    "provenance")},
                                           "kind": "library_entry", "entry_kind": e.get("kind"),
                                           "scope": activity.rsi.scope_id})
    _experience.append(activity.root, {"kind": "library_stream", "scope": activity.rsi.scope_id,
                                       "base_release": state["base_release"], "order_hash": order_hash,
                                       "distill_prompt": state["distill_prompt"], "readout": readout,
                                       "readings": state["readings"]})
    out(f"library    stream done: D={readout['D']} B={readout['B']} halves {readout['first_half']} -> "
        f"{readout['second_half']} (P {readout['p_second_half_better']}) active={readout['entries']['active']}")
    snapshot = lib.with_library(base_files, active(), settings, max_chars=limits.max_skill_chars,
                                max_skills=limits.max_skills)
    abort = None
    if readout["D"] <= settings.abort_d or readout["B"] >= settings.abort_b:
        abort = f"D {readout['D']} <= {settings.abort_d} or B {readout['B']} >= {settings.abort_b}"
    elif snapshot == base_files:
        abort = "the library is empty"
    if abort:
        state.update(status="done", snapshot={"outcome": "not_gated", "why": abort})
        save()
        _experience.append(activity.root, {"kind": "library_snapshot", "scope": activity.rsi.scope_id,
                                           "outcome": "not_gated", "why": abort, "readout": readout})
        out(f"library    the snapshot is not gated: {abort}")
        return {"status": "done", "readout": readout, "snapshot": state["snapshot"]}
    state["status"] = "snapshot"
    save()
    ctx, _ = _fixed_context(activity, work)
    failing = [t for t, ok in ctx.session.before.items() if not ok]
    candidate = fixed_candidate(operator="library", files=snapshot, site="skills/library/", carrier="skill",
                                failing=failing, mechanism="experience library snapshot from verified train successes",
                                trigger_condition="retrieved by task similarity at the start of every attempt",
                                notes=f"{len(active())} entries")
    ordinal = next_ordinal(_experience.read(activity.root))
    result = run_fixed_round(ctx, budget, [candidate], ordinal=ordinal)
    state.update(status="done", snapshot={"outcome": result.outcome, "reason": result.reason,
                                          "release": activity.releases.active()["release_ref"]})
    save()
    _experience.append(activity.root, {"kind": "library_snapshot", "scope": activity.rsi.scope_id,
                                       "outcome": result.outcome, "reason": result.reason, "entries": len(active()),
                                       "release_after": activity.releases.active()["release_ref"]})
    out(f"library    snapshot round: {result.outcome}" + (f" ({result.reason})" if result.reason else ""))
    return {"status": "done", "readout": readout, "snapshot": state["snapshot"]}


def _extension_of(activity: Activity, rounds: int | None, reason: str | None) -> tuple:
    """The worker segment an explicit extension continues and whether it is new or resumed (M7 §1, 2026-10-06), or
    (None, None) for an activity the cycle never ran (`rounds` then keeps its older meaning). Refused: a cycle that is
    paused (resume it with the cycle first), a last segment that has not ended (continue it with the cycle), a new
    extension without a reason, and a resumed one asked for another number of rounds."""
    path = Path(activity.root) / "rsi" / "cycle.json"
    if not path.exists():
        return None, None
    state = json.loads(path.read_text(encoding="utf-8"))
    segments = sorted((int(name.split("-", 1)[1]), name) for name in (state.get("steps") or {})
                      if re.fullmatch(r"segment-\d+", name))
    if not segments:
        return None, None
    if rounds is None:
        # A cycle activity runs its worker rounds in segments: an improve without --rounds would run the frozen
        # schedule from round 0 outside the segment's log, unrecorded (2026-10-06 review).
        raise ValueError(f"this activity runs its worker rounds in cycle segments ({segments[-1][1]}): continue it "
                         f"with --phase cycle, or extend it with --phase improve --rounds N --reason ...")
    if rounds < 1:
        raise ValueError(f"--rounds {rounds}: an extension runs at least one round")
    if state.get("status") == "paused":
        raise ValueError("the activity is paused: resume it with --phase cycle before extending it")
    segment = segments[-1][1]
    log = RoundLog(Path(activity.root) / "rsi" / "segments" / f"{segment}.json",
                   {"segment": segment, "scope": activity.rsi.scope_id, "schedule": activity.rsi.schedule_ref})
    ext = log.open_extension()
    if ext is not None:
        if int(ext["rounds"]) != rounds:
            raise ValueError(f"{segment} has an extension of {ext['rounds']} round(s) from round {ext['at_round']} "
                             f"still open: finish it with --rounds {ext['rounds']}")
        return segment, "open"
    if not log.finished:
        raise ValueError(f"{segment} has not ended: continue it with --phase cycle; --rounds extends a segment the "
                         f"frozen schedule has ended")
    if not (reason or "").strip():
        raise ValueError("--reason is required: rounds beyond the frozen schedule are a recorded decision")
    return segment, "new"


def _record_extension(activity: Activity, segment: str, seglog: "RoundLog") -> None:
    """The cycle's record of a segment an extension continued: its end is the extension's, and its extensions are
    listed, so the cycle reads the segment as ended (not run again) and the report sees the deviation."""
    path = Path(activity.root) / "rsi" / "cycle.json"
    if not path.exists():
        return
    state = json.loads(path.read_text(encoding="utf-8"))
    step = state.setdefault("steps", {}).setdefault(segment, {})
    step["stop_reason"] = seglog.state.get("stop_reason")
    step["release"] = activity.releases.active()["release_ref"]
    step["extensions"] = [{k: e.get(k) for k in ("rounds", "at_round", "reason", "by", "ts", "stop_reason",
                                                 "finished")} for e in seglog.state.get("extensions") or ()]
    write_json(path, state)


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
        return self.turn(system, [{"role": "user", "content": prompt}], ()).text or ""

    def turn(self, system: str, messages: list, tools=()):
        """One chat turn with tools, as R's coding session needs it (v0.3): the provider's ChatTurn, a refusal as
        ReplyRefused and a reply cut at the output limit as ReplyTruncated, exactly as `complete` reads them."""
        from .providers import ReplyRefused, ReplyTruncated
        turn = self.provider.chat(system, list(messages), list(tools or ()))
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
        return turn

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


def _campaign_host(activity: Activity, objects: _ct.Objects, worker_ref: str) -> dict:
    """The host contract a research slot's releases carry (M6 §2; 2026-10-05 review): the mainline release's target
    harness and required capabilities, copied into the slot's store, so a campaign release runs — and is recorded —
    on the same host as the mainline. Its runs then have the mainline's execution identity, and the mainline's
    readings of the start W can be reused as the slot's common inputs instead of being paid for again. A mainline
    release without a host contract (a v2 activity) gives none."""
    from .release import required_capabilities_of
    active = _ct.SystemRelease.from_json(activity.objects.get(activity.releases.active()["release_ref"]))
    if not active.target_harness_ref:
        return {}
    _ct.copy_closure(activity.objects, objects, [active.target_harness_ref])
    return {"target_harness_ref": active.target_harness_ref,
            "required_capabilities": required_capabilities_of(objects, active.required_capabilities, worker_ref)}


def _mainline_readings(activity: Activity, provider, train_tasks, inner_tasks) -> tuple[list, list]:
    """The mainline's valid readings of a research slot's start W that its common inputs reuse (M6 §2; 2026-10-05
    review) -> (train rows, inner dev rows). Train: the start W's measurement and screen readings under the
    measurement's execution identity (the labels aside), the latest of each task — every train task when the start
    W is W0 (the baseline measured them all), the reruns and screens when it is an installed W. Dev: the start W's
    readings of the inner tasks from runs without a plan (the baseline's dev run), the only ones an arm's inner gate
    may pool: a gate's readings were made under its own plan's task list (M3 §2.1(a) conditions)."""
    start = activity.releases.active()["release_ref"]
    if activity.results is None:
        return [], []
    work = improvement(activity, template_ref=activity.rsi.task_plan_ref, provider=provider)

    def runs_like(split: str, *, planless: bool) -> set:
        expected = work.run_identity(start, plan_ref="", split=split, candidate="current", role="base")[0]
        same = {k: v for k, v in expected.payload().items() if k not in RUN_LABELS}
        return {h for h, run in activity.results.runs().items() if not (planless and run.eval_plan_ref)
                and {k: v for k, v in run.payload().items() if k not in RUN_LABELS} == same}
    rows = list(activity.results.all())
    train: dict = {}
    if train_tasks:
        # a screen reading ran under its own plan: for a train measurement only the execution identity matters
        runs, wanted = runs_like("train", planless=False), set(train_tasks)
        for r in rows:
            if r.run in runs and r.validity == "valid" and r.task_id in wanted \
                    and _arm_of(r.attempt_id) in ("measure", "train_screen"):
                train[r.task_id] = r                    # the latest valid reading of each task
    dev = []
    if inner_tasks:
        runs, wanted = runs_like(_split_of_tasks(activity.splits, inner_tasks), planless=True), set(inner_tasks)
        dev = [r for r in rows if r.run in runs and r.validity == "valid" and r.task_id in wanted
               and _arm_of(r.attempt_id) not in SCREEN_ARMS]
    return list(train.values()), dev


def _attempt_means(rows) -> dict:
    """The mean of one valid attempt in each budget dimension it can be read in — dollars, act calls (as model
    calls), tool calls, input and output tokens — over these rows; a dimension no row measured is left out."""
    from .evolve.execution import normalize_usage
    valid = [r for r in rows if r.validity == "valid"]
    if not valid:
        return {}
    out = {"model_calls": sum(r.steps for r in valid) / len(valid),
           "tool_calls": sum(r.tool_calls for r in valid) / len(valid)}
    priced = [r.cost_usd for r in valid if r.cost_usd is not None]
    if priced:
        out["usd"] = sum(priced) / len(priced)
    usage = [normalize_usage(r.usage) for r in valid if r.usage]
    if usage:
        out["input_tokens"] = sum(u["input_tokens"] + u["cache_read_tokens"] + u["cache_write_tokens"]
                                  for u in usage) / len(usage)
        out["output_tokens"] = sum(u["output_tokens"] for u in usage) / len(usage)
    return out


def common_inputs_estimate(activity: Activity, cfg: dict, pair, limits: dict) -> dict:
    """What a research slot's common inputs will cost at least (M6 §2; 2026-10-05 review): the start W's train tasks
    and inner dev tasks the mainline has no reusable reading of (`_mainline_readings`), times the start W's measured
    mean per attempt (the activity's, when the start W has none), in each dimension the root limits. The slot opens
    only when the root covers this beside the arms' and the outer comparison's shares."""
    train = _tasks_of(activity, pair.inner_train_groups)
    inner = _tasks_of(activity, pair.inner_dev_groups)
    try:
        reused_train, reused_dev = _mainline_readings(activity, _pinned_provider(activity, cfg), train, inner)
    except Exception:  # noqa: BLE001 - an estimate that cannot read the records assumes nothing is reused
        reused_train, reused_dev = [], []
    missing = (len(set(train) - {r.task_id for r in reused_train})
               + (len(set(inner) - {r.task_id for r in reused_dev}) if _pooling_of(activity)[0] == "pooled" else 0))
    start = activity.releases.active()["release_ref"]
    rows = [r for r in (activity.results.all() if activity.results is not None else ())
            if r.system_release_ref == start] or list(activity.results.all() if activity.results is not None else ())
    means = _attempt_means(rows)
    return {k: round(missing * float(means[k]), 6) for k in limits if k in means and missing}


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
                               reason=f"campaign {plan.id} {pair.pair_id} arm {arm}",
                               **_campaign_host(activity, objects, pair.initial_w_ref))
    pooling, confirmations = _pooling_of(activity)
    inner = Improvement(rt=rt, suite=activity.suite, splits=activity.splits, objects=objects, releases=releases,
                        provider=provider, core_ref=core_ref, gate=_gate_of(activity, plan),
                        dev_tasks=tuple(_tasks_of(activity, pair.inner_dev_groups)),
                        scope_id=f"{plan.id}:{pair.pair_id}:{arm}", repeats=pair.repeats,
                        order_seed=pair.seed_schedule[0] if pair.seed_schedule else 0,
                        budget=arm_budget, serving=rt.serving, results=Results(root, activity.bench.domain),
                        pricing=pricing, concurrency=activity.bench.concurrency,
                        stop_view=activity.rsi.stop_view, attempt_budget=_attempt_budget(activity),
                        probe_train=True, train_tasks=tuple(_tasks_of(activity, pair.inner_train_groups)),
                        harness=_harness_of(activity), worker=isolated_worker(_harness_of(activity)),
                        control_pooling=pooling, confirmation_repeats=confirmations)
    # The arm's own template, frozen from what its gate reads — the inner dev families, the campaign's gate, its
    # repeats and seed, the activity's pooling and confirmation (M6 §2, v0.3) — so its experiments name the plan
    # they are compared under, not the activity's dev plan.
    inner.template_ref = freeze_task_plan(
        objects, core_ref=core_ref, split_ref=activity.splits.hash(), task_ids=inner.dev_tasks, gate=inner.gate,
        repeats=inner.repeats, order_seed=inner.order_seed,
        per_attempt_budget=inner.attempt_budget.limits(), control_pooling=pooling,
        confirmation_repeats=confirmations)
    step = researcher
    outcome = ArmOutcome(arm=arm, pair_id=pair.pair_id)
    if log.finished:
        return _arm_outcome(outcome, log.state, objects, releases, start, activity, pair, arm_budget, out)
    _seed_experience(root, _starting_experience(activity, pair))      # before a resumed experiment appends to it
    if shared and shared.get("dev"):
        # M6 §2 (v0.3): the start W's dev readings are a common input, copied into this arm's record as recorded, so
        # the arm's pooled control starts as the other arm's does — the common readings plus, later, its own; the
        # other arm's readings are never here. Rows already on record (a restarted arm) are not written twice.
        _copy_readings(shared["dev"], shared["dev_runs"], inner.results, source=shared["results"])
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
                        patience=plan.worker_round_limit,
                        candidates_per_round=int(getattr(plan, "candidates_per_round", 1) or 1))
    measured: dict = {}
    if inner.harness and shared:
        # Both arms start from the one W0 measurement (docs/modules/06_RESEARCH_CAMPAIGN.md §2): its grading and
        # transcripts are copied in, so the arm reads only its own workspace, and it stands for the arm's start
        # release because that release runs the same W0. A restarted arm continues from its own measurements.
        _copy_measurement(shared["measurement"], shared["results"], shared["transcripts"], inner.results,
                          Path(rt.root) / "transcripts")
        # the common release the copied rows name, so the arm reads them as a measurement of its own start (the same
        # W and K: Improvement._runs_like) rather than paying for the failures again
        _ct.copy_closure(shared["objects"], objects, [shared["measurement"]["release"]])
        own = None
        if not any(e.get("scope") == inner.scope_id for e in releases.events.of("measurement_planned")):
            # The arm has measured nothing of its own yet: it starts from the common measurement as it was made,
            # whose rows may name several releases of the start W (the mainline's readings it reused, 2026-10-05),
            # plus the collection batches the arm itself ran on `start` before a pause.
            held = {r.attempt_id for r in shared["measurement"]["train"]}
            mine = [r for r in inner.results.all() if _arm_of(r.attempt_id) == "collect"
                    and r.system_release_ref == start and r.attempt_id not in held]
            common = shared["measurement"]
            own = {**common, "release": start, "results": [*common["results"], *mine],
                   "train": [*common["train"], *mine], "missing": [],
                   "feedback": _feedback.build_feedback(inner.rt, objects=inner.objects, scope_id=inner.scope_id)}
        elif (own := inner.last_measurement(releases.active()["release_ref"])) is not None \
                and own["release"] != start and inner._runs_like(own["release"], start):
            # The copied rows name the common release; the arm's own collection batches ran on `start` (the release
            # its measurement stood for) and last_measurement filed them under that release, not the common one.
            # They join the measurement here, or a restart would lose evidence paid for before the pause. A batch
            # finished after the restart (finish_collections) skips these rows: _run_collection holds what the
            # measurement already has.
            held = {r.attempt_id for r in own["train"]}
            mine = [r for r in inner.results.all() if _arm_of(r.attempt_id) == "collect"
                    and r.system_release_ref == start and r.attempt_id not in held]
            own = {**own, "release": start, "results": [*own["results"], *mine], "train": [*own["train"], *mine]}
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
        diagnosis, drafts, session = inner.round_inputs(
            index, evidence, experience, bound, root=root, research_model=research_model,
            shared_notes=shared.get("notes") if shared and index == 0 else None)
        return RoundContext(releases=releases, objects=objects, scope_id=inner.scope_id, context=context,
                            researcher=step, bound=bound, core_ref=core_ref,
                            eval_template_ref=inner.template_ref, handlers=inner.handlers(),
                            experience_ref=experience_ref, experience=tuple(experience), owner="worker",
                            schedule=schedule, history=frozen_signatures(experience, objects, parent=release_ref),
                            root=root,
                            diagnosis=diagnosis, drafts=drafts, round_index=index, session=session)

    collector = None
    if inner.harness:
        def collector(result, *, batch: int) -> dict:
            return inner.collect_batch(measured["last"], batch=batch, root=root)
    collected = _collection_batches(root, inner.scope_id, None, any_segment=True) if inner.harness else 0
    rounds = run_worker_rounds(make_round, arm_budget, schedule=schedule, root=root, collect=collector,
                               collected_before=collected,
                               on_result=_rate_limit_recorder(step, _ct.Events(root), scope=inner.scope_id,
                                                              first_round=done, on_result=log.add),
                               history=tuple(log.state["rounds"]),
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
        ext = self.open_extension()
        if ext is not None and not pause:
            ext.update(finished=True, stop_reason=self.state["stop_reason"])
        self._save()

    # -------------------------------------------------------------- explicit extensions (M7 §1, 2026-10-06)
    def open_extension(self) -> dict | None:
        """The extension in progress, if the last one has not finished."""
        exts = self.state.get("extensions") or []
        return exts[-1] if exts and not exts[-1].get("finished") else None

    def extend(self, rounds: int, reason: str, *, by: str) -> dict:
        """Reopen an ended run for `rounds` more rounds, recorded: from which round (the completed rounds so far),
        where its own rounds start in the log, why, by whom, and the end it reopens."""
        record = {"rounds": int(rounds), "at_round": self.done(), "history_start": len(self.state["rounds"]),
                  "reason": reason, "by": by, "ts": now(), "after": self.state.get("stop_reason"), "finished": False}
        self.state.setdefault("extensions", []).append(record)
        self.state.pop("stop_reason", None)
        self.state["finished"] = False
        self._save()
        return record

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
                               reason=f"campaign {plan.id} {pair.pair_id} common inputs",
                               **_campaign_host(activity, objects, pair.initial_w_ref))
    pooling, confirmations = _pooling_of(activity)
    common = Improvement(rt=rt, suite=activity.suite, splits=activity.splits, objects=objects, releases=releases,
                         provider=provider, core_ref=core_ref, gate=_gate_of(activity, plan),
                         dev_tasks=tuple(_tasks_of(activity, pair.inner_dev_groups)),
                         scope_id=f"{plan.id}:{pair.pair_id}:common", budget=budget, serving=rt.serving,
                         results=Results(root, activity.bench.domain), pricing=pricing,
                         concurrency=activity.bench.concurrency,
                         stop_view=activity.rsi.stop_view, attempt_budget=_attempt_budget(activity),
                         train_tasks=tuple(_tasks_of(activity, pair.inner_train_groups)),
                         harness=_harness_of(activity), worker=isolated_worker(_harness_of(activity)),
                         control_pooling=pooling, confirmation_repeats=confirmations)
    # 2026-10-05 review (M6 §2): the mainline's valid readings of the start W are common inputs already paid for —
    # copied in as recorded (rows, run manifests, grading, transcripts and the releases they name), so the
    # measurement below runs only the train tasks they do not cover (Improvement.prior_readings), and the inner
    # dev reading only the tasks the baseline's dev run did not read. Under W0 that is nothing at all.
    reused_train, reused_dev = _mainline_readings(activity, provider, common.train_tasks,
                                                  common.dev_tasks if pooling == "pooled" else ())
    if reused_train or reused_dev:
        _ct.copy_closure(activity.objects, objects,
                         sorted({r.system_release_ref for r in [*reused_train, *reused_dev] if r.system_release_ref}))
        _copy_measurement({"train": [*reused_train, *reused_dev]}, activity.results,
                          Path(activity.rt.root) / "transcripts", common.results, Path(rt.root) / "transcripts")
    measurement = common.measure(start, None)
    shared: dict = {"measurement": measurement, "results": common.results, "transcripts": Path(rt.root) / "transcripts",
                    "research_model": research_model, "notes": None, "dev": [], "dev_runs": {}, "objects": objects}
    if pooling == "pooled" and common.dev_tasks:
        # M6 §2 (v0.3): under a pooled control, the start W's one reading of the inner dev tasks is a common input
        # too — taken here, charged to the common scope, and copied into both arms — so both inner gates start
        # from the same pooled control and neither pays for a control arm the other already has. Under a fresh
        # control (an older plan) each arm's gate reads its own control, as before. The mainline's plan-less dev
        # readings of the start W stand for the tasks they cover (2026-10-05 review).
        read = {r.task_id for r in reused_dev}
        order = [(t, i) for t in common.dev_tasks if t not in read for i in range(common.repeats)]
        fresh = common.run_arm("dev_baseline", start, order, plan_ref="",
                               split=_split_of_tasks(activity.splits, common.dev_tasks),
                               candidate="current", role="base", resume=True) if order else []
        shared["dev"] = [*reused_dev, *fresh]
        shared["dev_runs"] = {h: run for h, run in common.results.runs().items()
                              if h in {r.run for r in shared["dev"]}}
    bound = _P.bind_release(start, profile="task", services=common.services(), authorization="installed")
    diagnosis, _, _ = common.round_inputs(0, measurement, (), bound, root=root, research_model=research_model)
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
                                   reused_train=len(reused_train), reused_dev=len(reused_dev),
                                   selected=[s["task_id"] for s in selected], notes_shared=shared["notes"] is not None)
    out(f"common     {pair.pair_id}: W0 measured on {len(measurement['train'])} train tasks "
        f"({len(reused_train)} train and {len(reused_dev)} dev readings reused from the mainline), "
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


def _copy_readings(rows: list, runs: dict, target: Results, *, source: Results | None = None) -> int:
    """Common readings and their run manifests, copied into one arm's record as recorded (M6 §2, v0.3): the rows
    keep their attempt ids, run hashes and release, so the arm's pool rule (Improvement.control_pool) reads them as
    what they are — readings of the same W under the same conditions — and, with `source`, their grading rows too,
    so the arm's dev failure-type histogram (M4 §1) has the types the common readings carry. Rows already there
    are kept. -> the rows written."""
    present = {r.attempt_id for r in target.all() if r.attempt_id}
    for run in runs.values():
        target.put_run(run)
    written = 0
    for r in rows:
        if r.attempt_id and r.attempt_id not in present:
            target.append(r)
            written += 1
    if source is not None:
        target.copy_grading(source, [r.attempt_id for r in rows if r.attempt_id])
    return written


def _copy_measurement(measurement: dict, source: Results, transcripts: Path, target: Results,
                      target_transcripts: Path) -> None:
    """A common measurement's TaskResult rows, run manifests, grading rows and transcripts, copied into one branch's
    workspace as recorded: the arm's trajectory reader (M4 §1, v0.3) resolves a W0 train attempt by its row in the
    arm's own store, and the grading is what its failure summaries read."""
    import shutil
    rows = measurement.get("train") or ()
    runs = {h: run for h, run in source.runs().items() if h in {r.run for r in rows}}
    _copy_readings(list(rows), runs, target, source=source)
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
    spent = sorted(t for t in plan.task_ids if task_domain(t) in activity.splits.spent()
                   and t in {e.task_id for e in activity.splits.entries} and activity.splits.split_of(t) == "test")
    if spent:
        # M7 §2: a spent domain's test is never read by a new claim's final, however the plan came to name it
        raise ValueError(f"{plan.experiment_id} names {len(spent)} test tasks of spent domains "
                         f"{sorted(activity.splits.spent())} ({spent[:3]}): nothing is run")
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
    rows: dict = {}
    budget = activity_budget(activity, cfg, f"final:{plan.experiment_id}")
    # The final spends the share the frozen budget set aside for it, when there is one, and nothing else.
    rsi_cfg = RsiConfig.load(cfg)
    final_share = "final" if "final" in budget_allocations(activity.objects.get(rsi_cfg.budget_ref)
                                                          if rsi_cfg.budget_ref else {}) else None
    # M5 §2 (v0.3): before anything is paid, what the final is expected to spend at the costs on record — attempts
    # still to run times each arm's measured cost per attempt — and whether what is left of the share holds it
    # beside the in-flight reservations. 2026-10-05 review: when it does not, the final is refused here, before the
    # first attempt — a final whose lanes run out of reservations half way is not cheaper, it is void (final-1).
    estimate = final_estimate(activity, task_ids=plan.task_ids, repeats=plan.repeats,
                              arms={c["subject"]: plan.release_refs.get(c["subject"]) or activity.release_ref
                                    for c in plan.comparisons if not c.get("llm")},
                              plan_ref=ref, pricing=pricing_of(cfg), concurrency=activity.bench.concurrency,
                              fallback=estimate_settings(cfg))
    left = budget.remaining(allocation=final_share).get("effective_remaining", {}).get("usd")
    for line in estimate_lines(estimate, left):
        out(line)
    if left is not None and estimate["expected"] is not None and estimate["reservations"] is not None \
            and estimate["expected"] + estimate["reservations"] > float(left) + 1e-9:
        raise ValueError(f"{plan.experiment_id}: the expected spend ${estimate['expected']:.2f} and "
                         f"{estimate['concurrency']} in-flight reservations (${estimate['reservations']:.2f}) do not "
                         f"fit the ${float(left):.2f} left to the final; nothing was run. Lower --concurrency, or "
                         f"seal a plan the share holds")
    write_json(state_path, {**state, "status": "running"})
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
    return {"plan": ref, "claims": list(plan.claims), "arms": rows, "estimate": estimate,
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
    frozen_plan = activity.objects.get(activity.rsi.task_plan_ref)
    problems = _beyond_preset(settings, activity.objects.get(activity.rsi.schedule_ref),
                              TaskEvalPlan.from_json(frozen_plan).gate_plan, frozen_plan)
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
    planned_slots = int(settings["schedule"].get("research_slots", 0) or 0)
    if schedule.research_slots < planned_slots and not state.get("research_deferred"):
        # v0.3 step 5 (ROADMAP, staged runbook, 2026-10-05): a frozen schedule may run fewer research slots than the
        # preset plans (_beyond_preset refuses only more). Said on the record, so the report's C4 reads "deferred to
        # a separate workspace" rather than a slot that silently never came.
        state["research_deferred"] = {
            "preset": planned_slots, "schedule": schedule.research_slots,
            "reason": f"deferred to a separate workspace: this activity's frozen schedule runs research_slots "
                      f"{schedule.research_slots} where the preset plans {planned_slots}"}
        save()
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


def _beyond_preset(settings: dict, schedule: dict | None, gate: dict | None,
                   plan: dict | None = None) -> list[str]:
    """What a schedule, gate or task plan would loosen beyond [improvement] and its preset: more candidates,
    collection batches or research slots than the reader allows, a lower min_gain or a higher max_regression_rate
    than the preset's, a looser v0.3 clause, or — `plan`, the frozen TaskEvalPlan's JSON (M3 §2.1(a)(b), v0.3) — a
    control not pooled or fewer confirmation readings than the preset's task plan sets, which leaving the field out
    of a [freeze.task_plan] table also does."""
    problems = []
    if plan is not None:
        preset_plan = settings["task_plan"]
        if preset_plan.get("control_pooling") == "pooled" and plan.get("control_pooling") != "pooled":
            problems.append(f"control_pooling {plan.get('control_pooling')} is looser than the preset's pooled")
        wanted = int(preset_plan.get("confirmation_repeats") or 0)
        if int(plan.get("confirmation_repeats") or 0) < wanted:
            problems.append(f"confirmation_repeats {plan.get('confirmation_repeats')} is below the preset's {wanted}")
    if schedule is not None:
        # v0.3 step 3 (M4 §2): k is a ceiling — a frozen schedule may run fewer candidates a round than
        # [improvement] allows, never more.
        if int(schedule.get("candidates_per_round", 1)) > settings["candidates_per_round"]:
            problems.append(f"candidates_per_round {schedule.get('candidates_per_round')} is above "
                            f"max_candidates_per_round {settings['candidates_per_round']}")
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
        # v0.3 (M3 §2.1): the gate's new clauses tighten only — a larger drop before a task counts as regressed, a
        # larger cost allowance or a smaller least task count would each let more through than the preset does.
        for key, looser in (("regression_drop", lambda v, p: v > p), ("cost_beta0", lambda v, p: v > p),
                            ("cost_beta1", lambda v, p: v > p), ("min_tasks", lambda v, p: v < p)):
            if preset.get(key) is None:
                continue
            value = gate.get(key)
            if value is None or looser(float(value), float(preset[key])):
                problems.append(f"{key} {value} is looser than the preset's {preset[key]}")
    return problems


def _cycle_slot(cfg, base, activity: Activity, settings: dict, slot: str, state: dict, save, *, out, researcher,
                arm_researcher) -> str:
    """One planned research slot, recorded step by step: the frozen campaign plan before any arm runs, then the
    receipt. A restart resumes the same plan (its arms resume too), and a slot with a receipt is not run again."""
    step = state["steps"].setdefault(slot, {})
    if step.get("receipt") or step.get("outcome") in ("no_proposal", "invalid", "rejected", "failed",
                                                      "no_slot_pool", "no_headroom"):
        out(f"cycle      research {slot} already on record ({step.get('verdict') or step.get('outcome')})")
        return "done"
    if step.get("outcome") in ("paused", "budget_exhausted"):
        # how an earlier run of the slot stopped, not its result: it continues from what it recorded
        step.pop("outcome")
        step.pop("reason", None)
    campaign = settings["campaign"]
    if not step.get("campaign"):
        pairs = cycle_pairs(activity, cfg)
        template = TaskEvalPlan.from_json(activity.objects.get(activity.rsi.task_plan_ref))
        # docs/modules/06_RESEARCH_CAMPAIGN.md §2 (v0.3), in this order, before R is asked, before any share is
        # held and before the budget is even looked at: the slot's pools (each half of dev no smaller than the
        # gate's min_tasks), then the headroom, then the budget. Each is the slot's own outcome, not a budget end
        # and not an invalid round; the next worker segment runs as scheduled and the report's C4 says why.
        halves = {"inner": _tasks_of(activity, pairs[0].inner_dev_groups),
                  "outer": _tasks_of(activity, pairs[0].outer_groups)}
        least = template.gate().min_tasks
        short = {half: len(tasks) for half, tasks in halves.items() if least is not None and len(tasks) < least}
        if short:
            # the inequality that failed, per half; the dev-wide one only when it holds too
            failed = ", ".join(f"the {half} half has {n} task{'s' if n != 1 else ''}, fewer than min_tasks {least}"
                               for half, n in short.items())
            whole = f"; dev {len(template.task_ids)} < 2 × {least}" if len(template.task_ids) < 2 * least else ""
            step.update(outcome="no_slot_pool",
                        reason=f"the research slot needs two gates of at least min_tasks {least} tasks: {failed}{whole}")
            save()
            out(f"cycle      research {slot}: no_slot_pool ({step['reason']}); the next worker segment runs")
            return "done"
        unsolved = never_passed(activity, cfg, halves["inner"])
        headroom = len(unsolved)
        step["headroom"] = headroom
        if len(activity.bench.domains) > 1:
            # v0.3 step 5 (M7 §4, IT6-36): the same count per domain, and how the halves split across domains;
            # counts only, never the task ids
            never = per_domain_counts(unsolved)
            step["headroom_by_domain"] = {d: never.get(d, 0) for d in per_domain_counts(halves["inner"])}
            step["halves_by_domain"] = {half: per_domain_counts(tasks) for half, tasks in halves.items()}
        least_headroom = int(campaign.get("min_headroom_tasks") or 0)
        if least_headroom and headroom < least_headroom:
            step.update(outcome="no_headroom",
                        reason=f"the start W never passed {headroom} of the {len(halves['inner'])} inner dev tasks "
                               f"in its pooled readings, fewer than min_headroom_tasks {least_headroom}")
            save()
            out(f"cycle      research {slot}: no_headroom ({step['reason']}); the next worker segment runs")
            return "done"
    budget = activity_budget(activity, cfg, slot)
    if not budget.affordable({}):
        step["outcome"] = "budget_exhausted"
        save()
        return "budget_exhausted"
    if not step.get("campaign"):
        inner_groups = len(pairs[0].inner_dev_groups)
        # The arms' inner gate is the mainline gate on fewer families — its every clause (min_gain, the regression
        # rule, the cost term, min_tasks: M6 §2, v0.3) — while the research verdict is over independence groups,
        # and the preset's pair rule makes one, so its min_groups comes from the preset (exploratory).
        gate = GatePlan(**{**{k: v for k, v in template.gate().to_json().items() if k != "ref"},
                           "min_groups": max(1, min(template.gate().min_groups, inner_groups))})
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
        # 2026-10-05 review (M6 §2): the common inputs too — the start W's train tasks and inner dev tasks the
        # mainline has no reusable reading of, at the start W's measured mean per attempt — or the slot could open,
        # pay them, and then be unable to set aside both arms (an inconclusive comparison paid for)
        common = common_inputs_estimate(activity, cfg, pairs[0], limits)
        for k, v in common.items():
            need[k] = round(float(need.get(k, 0)) + float(v), 6)
        if not budget.affordable(need):
            step.update(outcome="budget_exhausted",
                        reason=f"the root cannot cover the outer comparison's share {outer_budget} and both "
                               f"arms' shares {arm_budget}" + (" and R's call" if researcher is None else "")
                               + (f" and the common inputs' estimate {common}" if common else ""))
            save()
            return "budget_exhausted"
        step["common_estimate"] = common
        discovery = "automatic" if researcher is None else "scripted"
        researcher = researcher or _default_researcher(cfg, activity, budget=budget)
        # k for the arms is the mainline's frozen schedule's (M4 §2): the same k in every round of the activity
        frozen_schedule = activity.objects.get(activity.rsi.schedule_ref) if activity.rsi.schedule_ref else {}
        planned = research_slot(cfg, base, pairs=pairs, researcher=researcher, out=out,
                                worker_rounds=int(campaign["worker_round_limit"]), gate=gate,
                                research_gate=research_gate, arm_budget=arm_budget, discovery=discovery,
                                outer_budget=outer_budget, budget=budget, run=False,
                                candidates_per_round=int(frozen_schedule.get("candidates_per_round")
                                                         or settings["candidates_per_round"]))
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


def slot_headroom(activity: Activity, cfg: dict, inner_tasks) -> int:
    """How many of the inner dev tasks the start W never passed in its pooled readings (M6 §2 余量检查, v0.3): what a
    research slot's candidates could still fix. Read from the activity's results through the same pool rule as a
    gate's control (Improvement.control_pool), so the number is the one the arms' inner gates would start from; a
    task with no reading at all counts as never passed — no evidence is not evidence of a pass. The W is the
    active release's."""
    return len(never_passed(activity, cfg, inner_tasks))


def never_passed(activity: Activity, cfg: dict, inner_tasks) -> list[str]:
    """The inner dev tasks the start W never passed in its pooled readings, in the order given (slot_headroom's
    count; per domain in the slot's record and the report, v0.3 step 5)."""
    parent = activity.releases.active()["release_ref"]
    work = improvement(activity, template_ref=activity.rsi.task_plan_ref, provider=_pinned_provider(activity, cfg))
    inner = list(inner_tasks)
    if not inner:
        return []
    template = work.plan("", "", parent)
    if not template.pooled:
        # a template that takes fresh controls still has readings of the start W (the baseline's dev run, the
        # gates' control arms): the same pool rule reads them, for the headroom only
        template = TaskEvalPlan.from_json({**{k: v for k, v in template.to_json().items() if k != "ref"},
                                           "control_pooling": "pooled"})
    rows = work.control_pool(parent, plan=template, tasks=inner)
    passed = {r.task_id for r in rows if r.success}
    return [t for t in inner if t not in passed]


def per_domain_counts(task_ids) -> dict[str, int]:
    """Tasks counted by their domain, in first-seen order: how a per-domain number is recorded without task ids."""
    out: dict[str, int] = {}
    for t in task_ids:
        out[task_domain(t)] = out.get(task_domain(t), 0) + 1
    return out


def _pinned_provider(activity: Activity, cfg: dict):
    """The configured model's provider, for an Improvement that only reads records: the release pins its own actor
    and a read never calls it, so a provider that cannot be built is no provider rather than a refusal."""
    try:
        return config_mod.build_provider(cfg["llm"]) if "llm" in cfg else None
    except Exception:  # noqa: BLE001 - nothing here is paid for; the startup checks name a model that cannot run
        return None


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
    inner, outer = halve_dev(dev_families, {f: task_domain(t) for t, f in family.items()})
    train_families = list(dict.fromkeys(family[t] for t in activity.splits.ids("train")))
    active = _ct.SystemRelease.from_json(activity.objects.get(activity.releases.active()["release_ref"]))
    # E0, both arms' starting experience: the mainline log as it stands, each experiment's raw gate reasons left
    # out because they can name development tasks — the second half of which is now the outer pool. The aggregate
    # figures stay (docs/modules/06_RESEARCH_CAMPAIGN.md §3).
    entries = [{k: v for k, v in e.items() if k != "reasons"} if e.get("kind") == "experiment"
               # v0.3 step 3 (M6 §2): a ledger row's per-task gate lists name dev tasks; the arms read counts
               else _experience.strip_dev_task_ids(e) for e in _experience.read(activity.root)]
    return [CampaignPair(pair_id="pair-1", independence_group="group-1", initial_w_ref=active.worker_ref,
                         initial_experience_ref=activity.objects.put_json("change", {"experience": entries}),
                         inner_train_groups=tuple(train_families), inner_dev_groups=tuple(inner),
                         outer_groups=tuple(outer), split_ref=activity.splits.hash(), repeats=1,
                         seed_schedule=(0,))]


def halve_dev(dev_families: list, domain_of: dict) -> tuple[list, list]:
    """The research slot's two halves of the frozen dev families (M6 §2): the first half the arms' inner gate, the
    second the outer comparison. One domain: the frozen order cut at len // 2, as v0.1/v0.2 cut it. Several domains
    (v0.3 step 5): each domain's dev families cut in their frozen order — its first part to the inner half, the rest
    to the outer — so both halves span every domain rather than the first domains landing wholly inside and the last
    wholly outside; a domain with an odd count gives its extra family to the outer half on even positions in the
    list and to the inner half on odd ones, so the halves stay within a family of each other. Each half keeps the
    pooled order."""
    by_domain: dict[str, list] = {}
    for f in dev_families:
        by_domain.setdefault(domain_of.get(f, ""), []).append(f)
    if len(by_domain) < 2:
        half = len(dev_families) // 2
        return list(dev_families[:half]), list(dev_families[half:])
    inner, outer = [], []
    for i, fams in enumerate(by_domain.values()):
        cut = len(fams) // 2 if i % 2 == 0 else (len(fams) + 1) // 2
        inner += fams[:cut]
        outer += fams[cut:]
    return inner, outer


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
                  outer_budget: dict | None = None, budget=None, candidates_per_round: int = 1) -> dict:
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
                        worker_round_limit=worker_rounds, candidates_per_round=int(candidates_per_round or 1),
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
    several = len(activity.bench.domains) > 1
    checks = ab.contract_check(activity.suite, _one_dev_task_per_domain(activity.splits) if several
                               else list(activity.splits.ids("dev"))[:1])
    problems += [f"scorer contract: {line}" for line in checks["failed"]]
    problems += domain_problems(activity)
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
    problems += effort_problems(activity, core)
    return problems


def domain_problems(activity: Activity) -> list[str]:
    """The listed domains against what the activity froze (M7 §2, v0.3 step 5): each domain's split on disk is the one
    the pooled split recorded by hash, the core pins the activity's split, and every domain's split hash is on
    record. Loading itself was checked when the activity opened (load_domains refuses a domain upstream cannot
    load). A one-domain activity is its own domain's split, as in v0.1/v0.2, and has nothing more to check here."""
    problems = []
    if len(activity.bench.domains) < 2:
        return problems
    recorded = {d["domain"]: d.get("hash") for d in activity.splits.domains()}
    if list(recorded) != list(activity.bench.domains):
        problems.append(f"the activity's split covers {list(recorded)}, and [bench] lists {list(activity.bench.domains)}")
    for domain in activity.bench.domains:
        path = Results(activity.root, domain).manifest_path()
        if not path.exists():
            problems.append(f"{domain}: no frozen split at {path}")
            continue
        own = SplitManifest.load(path).hash()
        if own != recorded.get(domain):
            problems.append(f"{domain}: the split frozen at {path} is {own}, and the pooled split recorded "
                            f"{recorded.get(domain)}")
    try:
        core = activity.objects.load(_ct.SystemRelease.from_json(activity.objects.get(activity.release_ref)).core_ref,
                                     _ct.CoreManifest)
    except Exception:  # noqa: BLE001 - a release that does not assemble is reported by the caller
        return problems
    if core.split_ref and core.split_ref != activity.splits.hash():
        problems.append(f"the core pins split {core.split_ref} and the activity's split is {activity.splits.hash()}: "
                        f"the domain list or a domain's split changed after the activity began")
    return problems


def effort_problems(activity: Activity, core: _ct.CoreManifest) -> list[str]:
    """The pinned actor's default effort against K's `max_effort` (docs/modules/00_CONTRACTS.md §2, v0.3): the gateway
    caps every call at `max_effort` and never refuses, so a pinned default above it would run at the cap on every
    call and the inference configuration W0 records would be one no call was made at. Found before anything is
    paid; a K without the v0.3 harness capabilities fixes no ceiling and is not checked."""
    harness = (core.capabilities or {}).get("harness")
    pinned = _pinned_actor(activity)
    if not isinstance(harness, dict) or pinned is None:
        return []
    ceiling = harness.get("max_effort")
    if ceiling is None:
        return []          # a K frozen before v0.3: its workspace runs the v1 loop, and no gateway caps anything
    effort = (activity.objects.get(pinned.inference_ref) or {}).get("effort")
    if effort in EFFORTS and ceiling in EFFORTS and EFFORTS.index(effort) > EFFORTS.index(ceiling):
        return [f"the actor the active release pins has effort {effort!r}, above K's max_effort {ceiling!r}: the "
                f"gateway caps every call at max_effort, so the pinned default would never run as configured"]
    return []


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
    worker = isolated_worker(settings)
    if limits.allows_code and worker is None:
        return [f"[harness_evolution] allows code components "
                f"{[k for k in limits.components if k in hp.CODE_KINDS]} and no qualified isolated worker is "
                f"available"]
    return worker_problems(worker) if limits.allows_code else []


def worker_problems(worker) -> list[str]:
    """The qualified worker's runner against this host's (docs/modules/01_POLICY_RUNTIME.md §1 默认 agent 即种子包):
    an image built from another worker_runner.py would fail a run in ways that look like the package's, so a
    startup check names it, with the command that rebuilds the image, before anything is paid. One session is
    started for the check; a worker that cannot start or answer is named the same way."""
    if worker is None:
        return []
    problem = worker.runner_problem()
    return [f"the isolated worker {worker.describe().get('image') or worker.describe().get('kind')} cannot serve "
            f"this host: {problem}"] if problem else []


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
    the current package runs code — one whose image runs this host's worker_runner.py (worker_problems). The
    fallback to a text-only preset holds only for a package without code."""
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
    if limits.allows_code or bound.package.runs_code:
        problems += worker_problems(worker)        # the image's runner is this host's, or the operator rebuilds it
    return problems


# ---------------------------------------------------------------------------- freezing plans
FREEZE_TABLES = {"task_plan", "schedule", "budget", "final"}
TASK_PLAN_KEYS = {"tasks", "repeats", "order_seed", "screen", "installs", "per_attempt_budget", "gate",
                  "control_pooling", "confirmation_repeats"}
FINAL_KEYS = {"experiment_id", "claims", "comparisons", "release_refs", "tasks", "repeats", "statistical_plan",
              "stop_rules", "research_campaign_plan_ref"}


def _task_ids(activity: Activity, spec, what: str) -> tuple:
    """A split name ("dev", "test") resolved against the frozen manifest, or an explicit list checked against it.
    The plan stores the ids themselves, so a later change to the manifest cannot change what a plan ran."""
    # v0.3 step 5 (M7 §2, M3 §2.2): a final never reads a spent domain's test — "test" resolves to the claim's
    # tasks (the new domains' test), and a list naming a spent domain's task is refused
    spent = activity.splits.spent() if what == "final" else set()
    if isinstance(spec, str):
        ids = tuple(activity.splits.claim_ids(spec) if spent else activity.splits.ids(spec))
        if not ids:
            raise ValueError(f"[freeze.{what}] tasks = {spec!r}: the frozen manifest has no such tasks")
        return ids
    known = {e.task_id for e in activity.splits.entries}
    missing = sorted(set(spec) - known)
    if missing:
        raise ValueError(f"[freeze.{what}] names tasks the frozen manifest does not have: {missing[:5]}")
    stale = sorted(t for t in spec if task_domain(t) in spent and activity.splits.split_of(t) == "test")
    if stale:
        raise ValueError(f"[freeze.{what}] names test tasks of spent domains {sorted(spent)}: {stale[:5]}; their test "
                         f"was read before and supports no new claim (M7 §2)")
    return tuple(spec)


def _final_estimate_at_freeze(activity: Activity, table: dict | None, cfg: dict) -> dict:
    """final_estimate for the final plan a freeze knows of: the [freeze.final] table being frozen, else the plan
    [rsi] names. With neither, no arm is known yet: the spend is unknown and only the reservations are given."""
    if table:
        task_ids = _task_ids(activity, table.get("tasks", "test"), "final")
        repeats = int(table.get("repeats", 1))
        refs = dict(table.get("release_refs") or {})
        arms = {c["subject"]: refs.get(c["subject"]) or activity.release_ref
                for c in table.get("comparisons") or () if not c.get("llm")}
        plan_ref = None
    elif activity.rsi.final_plan_ref and activity.objects.has(activity.rsi.final_plan_ref):
        plan = FinalizationPlan.from_json(activity.objects.get(activity.rsi.final_plan_ref))
        task_ids, repeats, plan_ref = plan.task_ids, plan.repeats, activity.rsi.final_plan_ref
        arms = {c["subject"]: plan.release_refs.get(c["subject"]) or activity.release_ref
                for c in plan.comparisons if not c.get("llm")}
    else:
        return final_estimate(activity, task_ids=(), repeats=0, arms={}, pricing=pricing_of(cfg),
                              concurrency=activity.bench.concurrency) | {"expected": None, "suggested_share": None}
    return final_estimate(activity, task_ids=task_ids, repeats=repeats, arms=arms, plan_ref=plan_ref,
                          pricing=pricing_of(cfg), concurrency=activity.bench.concurrency,
                          fallback=estimate_settings(cfg))


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
    activity = open_activity(cfg, base, out=out, strata=True)
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
                                **dict(t.get("per_attempt_budget") or {})},
            # v0.3 (M3 §2.1(a)(b)): left out of the table, both stay None — a fresh control, no confirmation — and
            # the plan's ref is the one the same table froze before these fields existed.
            control_pooling=t.get("control_pooling"),
            confirmation_repeats=None if t.get("confirmation_repeats") is None else int(t["confirmation_repeats"]))
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
        # M5 §2 (v0.3): the final share holds the configured concurrency's reservations, or nothing is frozen; the
        # refusal says what the final is expected to spend at the costs on record and what share would do
        problem = final_share_problem(frozen, pricing_of(cfg), activity.bench.concurrency)
        if problem:
            estimate = _final_estimate_at_freeze(activity, tables.get("final"), cfg)
            for line in estimate_lines(estimate):
                out(line)
            suggestion = (f"suggested final share at least ${estimate['suggested_share']:.2f} (expected spend plus "
                          f"the reservations)" if estimate.get("suggested_share") is not None else
                          f"suggested final share: the final's expected spend (unknown until its releases have "
                          f"costed readings) plus {activity.bench.concurrency} x one call's bound")
            raise ValueError(f"freeze refused: {problem}; {suggestion}")
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
        # 2026-10-05 review (M5 §2): before it is sealed, the plan's expected spend against the final share — the
        # budget frozen in this call, else the one [rsi] names. A plan already sealed is not checked again.
        state_path = Path(activity.root) / "rsi" / "finalization" / f"{plan.experiment_id}.json"
        sealed = state_path.exists() and json.loads(state_path.read_text(encoding="utf-8")).get("plan") == plan.ref()
        if not sealed:
            budget_object = activity.objects.get(refs["budget_ref"]) if refs.get("budget_ref") else \
                (activity.objects.get(activity.rsi.budget_ref) if activity.rsi.budget_ref and
                 activity.objects.has(activity.rsi.budget_ref) else {})
            problem = final_plan_problem(activity, cfg, plan, budget_object, out)
            if problem:
                raise ValueError(f"freeze refused: {problem}; the plan was not sealed")
        refs["final_plan_ref"] = seal(plan, activity.root, activity.objects)["plan"]
    out("frozen     name these under [rsi]:")
    out("[rsi]")
    for key, ref in refs.items():
        out(f'{key} = "{ref}"')
    return refs
