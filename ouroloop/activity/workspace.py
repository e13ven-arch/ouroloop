"""An activity's workspace and handles (docs/design/ACTIVITY.md §3; CONTRACTS §3).

`open_activity` loads the environment, freezes the domain's split (or reads the frozen one), freezes K from the config
and its preset, and — the first time, unless `freeze_k` is off — writes `core.json`, the seed release ({W0, R0, K}) and
nothing else. Every later open checks that the config, the code and the split still give the same K: a different K is a
new activity, refused here, so a paid phase never runs a K the activity did not freeze.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..adapters import automationbench as ab
from ..contracts import Events, Objects, short, write_json
from ..evaluate.budget import BudgetAccount, Budgets, MeteredProvider, Pricing
from ..evaluate.limits import Budget
from ..evaluate.release import Release, Releases, code_version
from ..evaluate.results import Results, RunManifest
from ..evaluate.splits import SPLIT_RULES, SplitManifest, TaskEntry, assign_splits, stratified_splits
from ..harness.package import Package, load as load_package
from ..harness.seed import build_seed
from .config import ConfigError, Core, freeze_core, upstream_path, workspace_root

R0 = {"version": "r0", "prompts_ref": None, "estimates_ref": None, "playbook_ref": None, "settings_ref": None}


@dataclass
class Activity:
    config: dict
    root: Path
    core: Core
    core_ref: str
    suite: object
    splits: SplitManifest
    drift: list
    versions: dict
    objects: Objects
    events: Events
    releases: Releases
    budgets: Budgets
    results: Results
    worker_factory: Callable | None = None
    provider_factory: Callable | None = None
    _egress: object = field(default=None, repr=False)

    # ------------------------------------------------------------------ K and identity
    @property
    def k(self) -> dict:
        return self.core.json()

    @property
    def domain(self) -> str:
        return self.k["bench"]["domain"]

    @property
    def scope_id(self) -> str:
        return str((self.config.get("rsi") or {}).get("scope_id") or "activity")

    @property
    def transcripts(self) -> Path:
        return self.root / "transcripts"

    @property
    def concurrency(self) -> int:
        return max(int(self.k["bench"].get("concurrency") or 1), 1)

    def attempt_limits(self) -> Budget:
        return Budget.from_core(self.k["limits"])

    def active_release(self) -> Release:
        return self.releases.active()

    def package(self, release: Release | None = None) -> Package:
        return load_package(self.objects, (release or self.active_release()).package_ref)

    def seed_release(self) -> Release:
        return self.releases.history()[-1]

    # ------------------------------------------------------------------ the worker and its network
    def egress(self):
        network = self.k["worker"].get("network")
        if network == "none" or network is None:
            return None
        if self._egress is None:
            from ..runtime.egress import LazyPool
            self._egress = LazyPool(network, self.scope_id)
        return self._egress

    def worker(self):
        """The qualified worker K describes (its image by the id K pinned), or what a test put in its place."""
        if self.worker_factory is not None:
            return self.worker_factory()
        from ..runtime.worker import WorkerResources, docker_worker
        return docker_worker(WorkerResources.from_core(self.k), egress_pool=self.egress())

    # ------------------------------------------------------------------ money
    def pricing(self) -> Pricing:
        table = {k: v for k, v in (self.k.get("pricing") or {}).items() if v is not None}
        table["declared_fallbacks"] = tuple(table.get("declared_fallbacks") or ())
        return Pricing(**table)

    def root_account(self) -> BudgetAccount:
        """The activity's one budget root: K's dollars, with the final's share set aside; opened once, never with
        other limits."""
        budget = self.k.get("budget") or {}
        limits = {"usd": float(budget["usd"])} if budget.get("usd") is not None else {}
        root = self.budgets.account(self.scope_id, limits)
        if budget.get("final_usd") is not None:
            wanted = {"final": {"usd": float(budget["final_usd"])}}
            state = root.remaining()
            if state["allocations"] != wanted:
                if state["allocations"] or state["used"]:
                    raise ConfigError(f"{self.scope_id} holds allocations {state['allocations']} and has spent "
                                      f"{state['used']}: the final's share is set before anything is spent")
                root.allocate("final", wanted["final"])
        return root

    def account(self, phase: str) -> BudgetAccount:
        return self.root_account().child(f"{self.scope_id}:{phase}", {})

    def model_table(self, section: str = "model") -> dict:
        table = dict(self.k[section])
        return {"type": table["type"], "model": table["model"], "effort": table["effort"],
                "max_tokens": table["max_tokens"], "timeout": table["timeout"]}

    def actor_provider(self, account: BudgetAccount, *, allocation: str | None = None) -> MeteredProvider:
        """K's actor, every call reserved and settled in `account`."""
        from ..providers import build_provider
        provider = (self.provider_factory or build_provider)(self.model_table("model"))
        return MeteredProvider(provider, account, self.pricing(), operation="actor", allocation=allocation)

    def research_provider(self, step: str) -> MeteredProvider:
        """K's research model for an improver or memory step, in its own account."""
        from ..providers import build_provider
        provider = (self.provider_factory or build_provider)(self.model_table("research_model"))
        return MeteredProvider(provider, self.account(f"research:{step}"), self.pricing(), operation=step)

    def memory(self):
        """The memory reader. The memory package comes with stage 2; until then a K with memory enabled refuses to
        run (checks.startup_checks), so there is nothing to return."""
        return None

    # ------------------------------------------------------------------ runs
    def run_manifest(self, *, candidate: str, role: str, split: str, release: Release, notes: str = "",
                     repeats: int = 1) -> RunManifest:
        model = self.k["model"]
        return RunManifest(candidate, role, split, {"provider": model["provider"], "model": model["identity"],
                                                    "effort": model["effort"]},
                           environment={"upstream": self.versions.get("commit", ""),
                                        "package": self.versions.get("package", ""),
                                        "python": self.versions.get("python", ""), "toolset": "api"},
                           budget=self.attempt_limits(), data=self.splits.hash(), repeats=repeats, notes=notes,
                           system_release_ref=release.ref(), core_ref=self.core_ref, package_ref=release.package_ref,
                           mode="task_evaluation")


# ---------------------------------------------------------------------------- splits
def freeze_split(suite, bench: dict, path: Path, versions: dict) -> tuple[SplitManifest, list[str]]:
    """The frozen task groups of the domain, and what upstream now says that they do not. A manifest on disk is kept
    whatever the config asks now; a pinned hash ([bench] frozen_splits) must be reproduced by one of the two rules."""
    domain = bench["domain"]
    fams, rule = ab.families(suite, bench.get("families") or {})
    entries = [TaskEntry.of(e) for e in suite.entries(fams)]
    pinned = (bench.get("frozen_splits") or {}).get(domain)
    if path.exists():
        frozen = SplitManifest.load(path)
        if pinned and frozen.hash() != pinned:
            raise ConfigError(f"{path} freezes split {frozen.hash()}, and [bench] frozen_splits pins {domain} to "
                              f"{pinned}")
        return frozen, frozen.drift(entries)
    environment = {"upstream": versions.get("commit", ""), "package": versions.get("package", ""),
                   "python": versions.get("python", ""), "toolset": "api", "runner": versions.get("runner", ""),
                   "tool_schemas": "ouroloop"}

    def split_under(name: str) -> SplitManifest:
        strata = None
        if name == "stratified":
            members: dict[str, list[str]] = {}
            for tid in suite.ids():
                members.setdefault(fams[tid], []).append(ab.stratum(suite, tid))
            splits, strata = stratified_splits({f: ";".join(sorted(set(k))) for f, k in members.items()},
                                               bench["ratios"], bench["seed"])
        else:
            splits = assign_splits(fams.values(), bench["ratios"], bench["seed"])
        return SplitManifest(domain, environment, rule, entries, splits, bench["ratios"], bench["seed"],
                             split_rule="stratified" if strata is not None else None, strata=strata)
    wanted = bench.get("split_rule") or "hash"
    manifest = split_under(wanted)
    if pinned and manifest.hash() != pinned:
        other = split_under(next(r for r in SPLIT_RULES if r != wanted))
        manifest = other if other.hash() == pinned else manifest
    if pinned and manifest.hash() != pinned:
        raise ConfigError(f"neither split rule reproduces {domain}'s pin {pinned} (this environment gives "
                          f"{manifest.hash()}): copy the frozen manifest from the workspace that froze it into {path}")
    manifest.freeze(path)
    return manifest, []


# ---------------------------------------------------------------------------- opening
def open_activity(config: dict, *, out: Callable[[str], None] = print, freeze_k: bool = True, worker_factory=None,
                  provider_factory=None) -> Activity:
    """The activity of a config. `freeze_k` off (the smoke's checks) reads K without writing core.json or a release."""
    root = workspace_root(config)
    bench = dict(config.get("bench") or {})
    if not bench.get("domain"):
        raise ConfigError("[bench] domain is required")
    versions = ab.require(upstream_path(config))
    suite = ab.load_suite(bench["domain"], upstream_path(config))
    from .config import resolve
    sections = resolve(config)
    results = Results(root, sections["bench"]["domain"])
    splits, drift = freeze_split(suite, sections["bench"], results.manifest_path(), versions)
    core = freeze_core(config, split_hash=splits.hash(), code_version=code_version())
    objects = Objects(root)
    core_ref = core.ref()
    pinned = (config.get("rsi") or {}).get("core_ref")
    if pinned and pinned != core_ref:
        raise ConfigError(f"[rsi] core_ref pins K {short(pinned)}, and this config, code and split give "
                          f"{short(core_ref)}: a changed K is a new activity (PLAN rule 13)")
    pinned_split = (config.get("rsi") or {}).get("split_ref")
    if pinned_split and pinned_split != splits.hash():
        raise ConfigError(f"[rsi] split_ref pins {pinned_split}, and the frozen split is {splits.hash()}")
    core_file = root / "core.json"
    if core_file.exists():
        recorded = json.loads(core_file.read_text(encoding="utf-8")).get("ref")
        if recorded != core_ref:
            raise ConfigError(f"{core_file} froze K {short(recorded)}, and this config, code and split give "
                              f"{short(core_ref)}: a changed K is a new activity (PLAN rule 13); run the old K at its "
                              f"checkout, or start a new workspace")
    releases = Releases(root, objects)
    activity = Activity(config, root, core, core_ref, suite, splits, drift, versions, objects, Events(root), releases,
                        Budgets(root), results, worker_factory, provider_factory)
    if not freeze_k:
        return activity
    if not core_file.exists():
        objects.put(core)
        write_json(core_file, {**core.json(), "ref": core_ref})
        activity.events.append("core_frozen", core=core_ref, code_version=core.code_version, split=splits.hash())
    if releases.active_ref() is None:
        seed = build_seed(core.json()["harness"]["seed"], core=core.json(), objects=objects)
        improver_ref = objects.put("improver", dict(R0))
        release = releases.seed(seed.ref(), improver_ref, core_ref)
        out(f"release    {short(release.ref())} seeded (W0 {short(seed.ref())}, R0, K {short(core_ref)})")
    for line in drift:
        out(f"DRIFT      {line}")
    return activity
