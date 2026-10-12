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
from ..contracts import Events, Objects, copy_closure, short, write_json
from ..evaluate.budget import BudgetAccount, Budgets, MeteredProvider, Pricing
from ..evaluate.limits import Budget
from ..evaluate.release import Release, Releases, attempt_version, improver_version
from ..evaluate.results import Results, RunManifest
from ..evaluate.splits import SPLIT_RULES, SplitManifest, TaskEntry, assign_splits, stratified_splits
from ..harness.package import Limits, Package, check_shape, load as load_package
from ..harness.seed import build_seed
from .config import SECTIONS, ConfigError, Core, freeze_core, upstream_path, workspace_root

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
    memory_client_factory: Callable | None = None      # a test's in-process stand-in for the sidecar's client
    _egress: object = field(default=None, repr=False)
    _memory: object = field(default=None, repr=False)

    # ------------------------------------------------------------------ K and identity
    @property
    def k(self) -> dict:
        return self.core.json()

    @property
    def domain(self) -> str:
        return self.k["bench"]["domain"]

    @property
    def final_domains(self) -> list[str]:
        """The domains the final runs on and the release reads (DESIGN §6.14): [final] domains, else every domain of
        the activity."""
        return list(self.k["final"].get("domains") or self.k["bench"].get("domains") or [self.domain])

    def final_tasks(self, split: str) -> list[str]:
        """The split's tasks of the final's domains, in the frozen order."""
        wanted = set(self.final_domains)
        return [t for t in self.splits.ids(split) if self.suite.domain_of(t) in wanted]

    @property
    def scope_id(self) -> str:
        return str((self.config.get("rsi") or {}).get("scope_id") or "activity")

    @property
    def transcripts(self) -> Path:
        return self.root / "transcripts"

    @property
    def concurrency(self) -> int:
        """Attempts in flight: [bench] concurrency of the config, else the preset's; a machine setting, not in K."""
        from .config import resolve
        return max(int(resolve(self.config)["bench"].get("concurrency") or 1), 1)

    def attempt_limits(self) -> Budget:
        return Budget.from_core(self.k["limits"])

    def active_release(self) -> Release:
        """The working line's release (rsi/active.json)."""
        return self.releases.active()

    def released_release(self) -> Release | None:
        """The release line's W* (rsi/released.json), once the release gate passed; None before."""
        return self.releases.released()

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
        roots = sorted(name for name, scope in self.budgets._read()["scopes"].items()
                       if scope.get("parent") is None and name != self.scope_id)
        if roots:
            raise ConfigError(f"this workspace's budget book has the root {roots}, and [rsi] scope_id "
                              f"{self.scope_id!r} would open another: one activity has one budget root")
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

    def research_provider(self, step: str, *, retries: bool = True, account: BudgetAccount | None = None) \
            -> MeteredProvider:
        """K's research model for an improver or memory step, in its own account (`account`, or `research:<step>`
        under the root). Without `retries` neither the provider nor the meter retries and the provider's own timeout
        is kept under the OpenAI SDK's read timeout: what the memory's bridge serves (memory/bridge.py: every request
        is served once)."""
        from ..providers import build_provider
        provider = (self.provider_factory or build_provider)(self.model_table("research_model"))
        if not retries:
            from ..memory.bridge import RESEARCH_SECONDS
            if isinstance(getattr(provider, "retries", None), int):
                provider.retries = 0
            timeout = getattr(provider, "timeout", None)
            if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > RESEARCH_SECONDS:
                provider.timeout = RESEARCH_SECONDS
        return MeteredProvider(provider, account if account is not None else self.account(f"research:{step}"),
                               self.pricing(), operation=step, **({} if retries else {"max_retries": 0}))

    # ------------------------------------------------------------------ memory
    def memory(self):
        """The memory of this process (memory.service.MemoryService over <root>/memory/<domain>/), made on first use;
        its sidecar starts when something reads or writes, and stops with close()."""
        if self._memory is None:
            from ..memory.service import MemoryService
            from ..memory.settings import Settings
            self._memory = MemoryService(self.root, self.domain, Settings.from_core(self.k), scope_id=self.scope_id,
                                         client_factory=self.memory_client_factory,
                                         effort_cap=self.k["model"]["max_effort"])
        return self._memory

    def memory_for(self, package: Package, *, live: dict | None = None, note_of: Callable | None = None):
        """What each attempt of `package` gets of the memory: task, task_hash -> TaskMemory, from the package's
        frozen snapshot or, during the stream, from `live` (StreamStep.live_view()); `note_of(task)` is appended to
        the opening (the stream's retry note). None for a package that reads no memory. A package that reads memory
        but holds no snapshot (W0 under a K with the memory on: HARNESS §7) reads an empty library, and an empty
        library starts no sidecar: no block, no start line, every recall empty."""
        if not package.manifest.memory.get("enabled"):
            return None
        if live is None and not package.memory_text.strip():
            from ..memory.retrieval import TaskMemory
            return lambda task, task_hash: TaskMemory(opening=note_of(task) if note_of is not None else "")
        service = self.memory()
        reader = service.reader(live=live) if live is not None else service.reader(snapshot_text=package.memory_text)

        def of(task, task_hash: str):
            return reader.for_task(task.trigger, task_hash, note=note_of(task) if note_of is not None else "")
        return of

    def close(self) -> None:
        """Stop what this process started for the activity: the memory's sidecar."""
        if self._memory is not None:
            self._memory.stop()

    # ------------------------------------------------------------------ runs
    def run_manifest(self, *, candidate: str, role: str, split: str, release: Release, package: Package | None = None,
                     notes: str = "", repeats: int = 1) -> RunManifest:
        model = self.k["model"]
        return RunManifest(candidate, role, split, {"provider": model["provider"], "model": model["identity"],
                                                    "effort": model["effort"]},
                           environment={"upstream": self.versions.get("commit", ""),
                                        "package": self.versions.get("package", ""),
                                        "python": self.versions.get("python", ""), "toolset": "api"},
                           budget=self.attempt_limits(), data=self.splits.hash(), repeats=repeats, notes=notes,
                           system_release_ref=release.ref(), core_ref=self.core_ref,
                           package_ref=package.ref() if package is not None else release.package_ref,
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
                  provider_factory=None, memory_client_factory=None) -> Activity:
    """The activity of a config. `freeze_k` off (the smoke's checks) reads K without writing core.json or a release."""
    root = workspace_root(config)
    bench = dict(config.get("bench") or {})
    if not bench.get("domain") and not bench.get("domains"):
        raise ConfigError("[bench] domain is required")
    versions = ab.require(upstream_path(config))
    from .config import resolve
    sections = resolve(config)
    bench = sections["bench"]
    suite = ab.load_suites(bench["domains"], upstream_path(config)) if bench.get("domains") else \
        ab.load_suite(bench["domain"], upstream_path(config))
    results = Results(root, sections["bench"]["domain"])
    splits, drift = freeze_split(suite, sections["bench"], results.manifest_path(), versions)
    core = freeze_core(config, split_hash=splits.hash(), attempt_version=attempt_version(),
                       improver_version=improver_version())
    objects = Objects(root)
    core_file = root / "core.json"
    if core_file.exists():
        # The frozen K stands. A config or split that gives other sections is another activity; code that moved
        # under the activity is not refused here — each phase's startup check says whether it may run on this
        # checkout (PLAN rule 13: the rounds and the gate need both code versions, the final family the attempt
        # code; activity/checks.py)
        frozen = _frozen_core(json.loads(core_file.read_text(encoding="utf-8")), objects, core_file)
        if (frozen.preset, frozen.sections, frozen.split_hash) != (core.preset, core.sections, core.split_hash):
            raise ConfigError(f"{core_file} froze K {short(frozen.ref())}, and this config and split give "
                              f"{short(core.ref())}: a changed K is a new activity (PLAN rule 13); run the old K at "
                              f"its checkout, or start a new workspace")
        core = frozen
    core_ref = core.ref()
    pinned = (config.get("rsi") or {}).get("core_ref")
    if pinned and pinned != core_ref:
        raise ConfigError(f"[rsi] core_ref pins K {short(pinned)}, and this config, code and split give "
                          f"{short(core_ref)}: a changed K is a new activity (PLAN rule 13)")
    pinned_split = (config.get("rsi") or {}).get("split_ref")
    if pinned_split and pinned_split != splits.hash():
        raise ConfigError(f"[rsi] split_ref pins {pinned_split}, and the frozen split is {splits.hash()}")
    releases = Releases(root, objects)
    activity = Activity(config, root, core, core_ref, suite, splits, drift, versions, objects, Events(root), releases,
                        Budgets(root), results, worker_factory, provider_factory, memory_client_factory)
    if not freeze_k:
        return activity
    # The seed is resolved before K is written: an import that is refused, or a seed that does not fit K, leaves no
    # core.json behind (2026-10-13: a half-frozen workspace named an improver code version that never ran anything)
    seeding = releases.active_ref() is None
    pending = seed = None
    if seeding:
        seed_from = (config.get("rsi") or {}).get("seed_from")
        if seed_from:
            pending = _resolve_import(activity, seed_from, line=(config.get("rsi") or {}).get("seed_line") or "released")
        else:
            seed = build_seed(core.json()["harness"]["seed"], core=core.json(), objects=objects)
    if not core_file.exists():
        objects.put(core)
        write_json(core_file, {**core.json(), "ref": core_ref})
        activity.events.append("core_frozen", core=core_ref, attempt_version=core.attempt_version,
                               improver_version=core.improver_version, split=splits.hash())
    if seeding:
        if pending is not None:
            release = _install_import(activity, pending, out=out)
        else:
            improver_ref = objects.put("improver", dict(R0))
            release = releases.seed(seed.ref(), improver_ref, core_ref)
            out(f"release    {short(release.ref())} seeded (W0 {short(seed.ref())}, R0, K {short(core_ref)})")
    for line in drift:
        out(f"DRIFT      {line}")
    return activity


def _frozen_core(recorded: dict, objects: Objects, core_file: Path) -> Core:
    """The K core.json froze: the core object by its ref, else rebuilt from the file (an old workspace whose object
    store lacks it); a file that does not hash to its ref is refused."""
    ref = recorded.get("ref")
    if ref and objects.has(ref):
        core = Core.from_json(objects.get(ref))
    else:
        core = Core(preset=recorded.get("preset", ""), sections={k: v for k, v in recorded.items() if k in SECTIONS},
                    split_hash=recorded.get("split_hash", ""), attempt_version=recorded.get("attempt_version", ""),
                    improver_version=recorded.get("improver_version", ""))
    if ref and core.ref() != ref:
        raise ConfigError(f"{core_file} names K {short(ref)} and its contents hash to {short(core.ref())}: the "
                          f"workspace's frozen K is not readable")
    return core


# ---------------------------------------------------------------------------- the working line across activities
POLICY_FILES = ("ledger/estimates.json", "ledger/playbook.md")      # R's learned policy, carried over with W*


def rebase_files(origin: dict, w_star: dict, new_seed: dict) -> tuple[dict, list, list]:
    """W* replayed onto this code's seed (DESIGN §6.7, v1.6): a file R never touched (W* has the origin code seed's
    bytes) takes the new seed's version; a file the new seed adds comes in; a file R changed or added stays as it is
    (R's version also when the code changed it too: those are named); a file R deleted stays deleted.
    -> (files, the paths that changed, the paths kept over a code change)."""
    files = dict(w_star)
    rebased, kept = [], []
    for path, text in new_seed.items():
        if path in origin:
            if path in files and files[path] == origin[path]:
                if text != files[path]:
                    files[path] = text
                    rebased.append(path)
            elif path in files and text != origin[path] and text != files[path]:
                kept.append(path)
        elif path not in files:
            files[path] = text
            rebased.append(path)
    return files, sorted(rebased), sorted(kept)


def origin_seed(src_root: Path) -> tuple[Path, Release]:
    """The code seed a chain of imports started from: the first release of `src_root`'s line when its reason is `seed`,
    else the same of the workspace its `seed_imported` event names (v5 -> v6 -> v7). A broken chain is refused."""
    seen = []
    root = src_root
    while True:
        if root in seen or not (root / "rsi").is_dir():
            raise ConfigError(f"[rsi] seed_from: the chain of imports behind {src_root} is broken at {root}: the "
                              f"origin's code seed is the baseline of the replay and cannot be read")
        seen.append(root)
        first = Releases(root).history()[-1]
        if first.reason != "import":
            return root, first
        imported = next(iter(Events(root).read("seed_imported")), None)
        if imported is None:
            raise ConfigError(f"[rsi] seed_from: {root}'s seed is an import without a seed_imported event")
        root = Path(imported["source"])


@dataclass
class PendingImport:
    """A seed import resolved and checked, not yet installed (open_activity writes K between the two)."""
    src_root: Path
    line: str
    released: Release
    w_star: Package
    package: Package
    rebased: list
    kept: list
    origin_root: Path
    origin_release: Release


def _resolve_import(activity: Activity, seed_from: str, *, line: str = "released") -> PendingImport:
    """The seed of an activity that starts where another one released (DESIGN §6.7 v1.3, EVALUATE §7): the source
    workspace's release line W* (`line` "working": the head of its working line, for a source that closed without a
    final and so was never released) is copied into this object store (its closure), replayed onto this code's seed
    (`rebase_files` against the chain's origin code seed: the framework's files R never touched take this code's
    version, so the new host's contract and the package's adapter agree), and checked against this K's shape with the
    code's seed as the vendored baseline. Refused when the source has no such line, the chain is broken or W* does not
    fit this K; nothing but content-addressed objects is written before `_install_import`."""
    base = Path(activity.config.get("_base") or ".")
    src_root = Path(seed_from).expanduser()
    src_root = src_root if src_root.is_absolute() else (base / src_root).resolve()
    if not (src_root / "rsi").is_dir():
        raise ConfigError(f"[rsi] seed_from names {src_root}, which is not an activity's workspace")
    source = Releases(src_root)
    if line == "working":
        if source.active_ref() is None:
            raise ConfigError(f"[rsi] seed_from names {src_root}, which has no working line")
        released = source.active()
    else:
        released = source.released()
        if released is None:
            raise ConfigError(f"[rsi] seed_from names {src_root}, which has no release line (W*): release it first "
                              f"(or import its working line's head: [rsi] seed_line = \"working\")")
    origin_root, origin_release = origin_seed(src_root)
    copy_closure(source.objects, activity.objects, [released.package_ref])
    w_star = load_package(activity.objects, released.package_ref)
    code_seed = build_seed(activity.k["harness"]["seed"], core=activity.k, objects=activity.objects)
    origin = load_package(Releases(origin_root).objects, origin_release.package_ref)
    files, rebased, kept = rebase_files(origin.files, w_star.files, code_seed.files)
    problems = check_shape(files, limits=Limits.from_core(activity.k), seed=code_seed)
    if problems:
        raise ConfigError(f"[rsi] seed_from: the released W* {short(released.package_ref)} does not fit this K: "
                          f"{'; '.join(problems)[:500]}")
    from ..harness.package import build
    package = build(activity.objects, files, limits=Limits.from_core(activity.k), seed=code_seed) if rebased else w_star
    return PendingImport(src_root, line, released, w_star, package, rebased, kept, origin_root, origin_release)


def _install_import(activity: Activity, p: PendingImport, *, out) -> Release:
    """The resolved import installed as the first release (reason `import`); R's estimates and playbook come along;
    the `seed_imported` event says what came from where and what the replay changed."""
    improver_ref = activity.objects.put("improver", dict(R0))
    release = activity.releases.import_seed(p.package.ref(), improver_ref, activity.core_ref)
    copied = []
    for rel in POLICY_FILES:
        src, dst = p.src_root / rel, activity.root / rel
        if src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            copied.append(rel)
    activity.events.append("seed_imported", source=str(p.src_root), source_line=p.line,
                           source_release=p.released.ref(),
                           origin={"workspace": str(p.origin_root), "release": p.origin_release.ref()},
                           source_package=p.w_star.ref(), package=p.package.ref(), rebased=p.rebased,
                           kept_over_code=p.kept, policy_files=copied)
    out(f"release    {short(release.ref())} imported: {p.src_root}'s {p.line} {short(p.released.ref())} is this "
        f"activity's seed (W {short(p.package.ref())}, K {short(activity.core_ref)}; policy files {copied or 'none'}"
        + (f"; {len(p.rebased)} file(s) of the framework's taken from this code's seed: {p.rebased[:6]}" if p.rebased
           else "")
        + (f"; R's version kept where the code changed too: {p.kept}" if p.kept else "") + ")")
    return release
