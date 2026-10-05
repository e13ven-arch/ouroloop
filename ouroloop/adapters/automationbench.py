"""Bind the upstream AutomationBench to ouroloop: load its tasks, give the agent its business tools, and score the
world it leaves behind with the official rubric.

Upstream (https://github.com/zapier/AutomationBench) is a separate runtime: it needs pydantic and, for its own
task loader, `datasets`; its `runner` also needs `verifiers`. Nothing here is imported at module import time, so
ouroloop keeps its own dependencies (docs/DESIGN.md §5). `probe()` says exactly what is missing and what it is
needed for.

Three boundaries are structural rather than a matter of care:

    A task the agent runs is a `BenchTask`: the prompt messages and the granted tool names, nothing else. The
    assertions and the initial state live in `Grading`, which the agent path never receives and which only the
    scorer reads (docs/DESIGN.md §3.1.5).
    Scoring runs on the world the tools left, never on what the agent said about it. An agent that writes the
    wrong record and then summarises success scores what it wrote.
    The tool surface is upstream's own functions over upstream's WorldState. This adapter chooses which tools a
    task grants and records the calls; it does not reimplement what a call does, so it cannot accidentally make a
    task easier or harder.

Only the `api` toolset is supported, which is the upstream default (`auto-bench --toolset api`): three tools,
`api_search`, `api_fetch` and `base64_encode`, over every simulated service. `zapier` and `limited_zapier` build
their schemas through another package and are not wired up here.
"""
from __future__ import annotations

import copy
import inspect
import json
import os
import subprocess
import sys
import time
import typing
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from ..agent.tools import ToolResult, clip
from ..benchmark import TaskEntry
from ..chat import ToolSpec

TOOLSET = "api"
# api_fetch takes the world as its first argument; the model never sees it, and the episode injects it.
INJECTED = ("world",)
# Upstream tools that reach a real API when a key happens to be in the environment (a simulated ChatGPT action
# calls OpenAI, and falls back to a canned response without a key). A task whose world is not subscribed to
# `chatgpt` is already refused by upstream's service gate, but an episode also hides these variables while a
# tool runs, so a simulated action cannot become a live call through a path this adapter has not read
# (docs/ROADMAP.md M1: simulated operations must not call real SaaS).
LIVE_KEYS = ("OPENAI_API_KEY",)


class UpstreamError(RuntimeError):
    """The upstream benchmark is not importable, or does not offer what this adapter needs."""


# ---------------------------------------------------------------------------- locating upstream
def _add_path(path: str | Path | None) -> Path | None:
    """Put an AutomationBench checkout on sys.path. A pip-installed package needs no path."""
    if not path:
        return None
    root = Path(path).expanduser().resolve()
    if not (root / "automationbench" / "__init__.py").exists():
        raise UpstreamError(f"{root} is not an AutomationBench checkout (no automationbench/__init__.py)")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def _commit(root: Path | None) -> str:
    """The checkout's own commit, or empty. A directory inside someone else's repository has none of its own:
    reporting the enclosing repository's commit would pin the wrong thing."""
    if root is None:
        return ""
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=20)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
            return ""
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _version(root: Path | None) -> str:
    """Upstream's package version. Its own module does not carry one, so this reads the checkout's pyproject, and
    falls back to the installed distribution's metadata. The version is part of what a result is pinned to, so an
    unknown one is worth seeing rather than defaulting to something."""
    if root is not None:
        try:
            for line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines():
                if line.startswith("version"):
                    return line.partition("=")[2].strip().strip('"\'')
        except OSError:
            pass
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("automation-bench")
    except Exception:  # noqa: BLE001 - not installed as a distribution; the commit still pins it
        return ""


def probe(path: str | Path | None = None) -> dict:
    """What upstream this process would run, and what it cannot do. Never raises: the report is the point.

    `missing` is what a phase needs and does not have; `notes` records the pieces this adapter had to supply
    itself because upstream's own are behind a dependency (`verifiers`), which a contract check then flags.
    """
    root = None
    report: dict = {"path": "", "commit": "", "package": "", "python": sys.version.split()[0], "toolset": TOOLSET,
                    "missing": [], "notes": []}
    try:
        root = _add_path(path or os.environ.get("OUROLOOP_AUTOMATIONBENCH"))
    except UpstreamError as e:
        report["missing"].append(str(e))
        return report
    report["path"], report["commit"] = str(root or ""), _commit(root)
    # What is imported is what is checked, and a missing third-party package names itself in the error: the
    # api toolset needs the openai client (upstream's tools package imports it), and upstream's own task loader
    # needs `datasets`.
    for module, need in (("automationbench.schema.world", "the simulated world state"),
                         ("automationbench.rubric", "the official scorer"),
                         ("automationbench.tools.api", "the business tools"),
                         ("automationbench.task_contract", "task fingerprints"),
                         ("automationbench.domains", "upstream's own task loader")):
        try:
            __import__(module)
        except Exception as e:  # noqa: BLE001 - any import failure is a missing capability, reported not raised
            report["missing"].append(f"{module} ({need}): {type(e).__name__}: {e}"[:300])
    report["package"] = _version(root)
    report["pydantic"] = str(getattr(sys.modules.get("pydantic"), "VERSION", "") or "")
    try:
        import automationbench.runner  # noqa: F401
        report["runner"] = "upstream"
    except Exception as e:  # noqa: BLE001 - verifiers is not needed to run tasks, only to use upstream's runner
        report["runner"] = "ouroloop"
        report["notes"].append(f"automationbench.runner is not importable ({type(e).__name__}); this adapter "
                               f"uses its own copies of strip_none_values and compute_allowed_services, which "
                               f"contract_check compares against upstream when the runner is available")
    return report


def require(path: str | Path | None = None) -> dict:
    report = probe(path)
    if report["missing"]:
        raise UpstreamError("AutomationBench is not usable here:\n  " + "\n  ".join(report["missing"]) +
                            "\n  clone it beside ouroloop, install pydantic, datasets and openai into this "
                            "environment (examples/automationbench/README.md), and point [bench] upstream_path "
                            "or OUROLOOP_AUTOMATIONBENCH at the checkout")
    return report


# ---------------------------------------------------------------------------- upstream pieces this adapter needs
def _strip_none(obj):
    """Upstream `runner.strip_none_values`: a HuggingFace Dataset fills missing keys with None, which defeats
    pydantic's defaults. Used only when upstream's runner is not importable."""
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(v) for v in obj if v is not None]
    return obj


def _allowed_services(initial_state: dict, assertions: list[dict], tools: list[str]) -> list[str]:
    """Upstream `runner.compute_allowed_services`: the services a task's world is subscribed to. Used only when
    upstream's runner is not importable; a call to any other service is refused with a credentials error, which
    is also what keeps a simulated write out of untracked state."""
    from automationbench.schema.world import WorldState
    fields = sorted((str(f) for f in WorldState.model_fields if f != "meta"), key=len, reverse=True)

    def service_of(name: str) -> str | None:
        return next((f for f in fields if name == f or name.startswith(f + "_")), None)

    allowed = {k for k in initial_state if k != "meta" and k in WorldState.model_fields}
    allowed |= {s for a in assertions or [] if (s := service_of(str(a.get("type", ""))))}
    allowed |= {s for t in tools or [] if (s := service_of(t))}
    return sorted(allowed)


def _runner_fns() -> tuple[Callable, Callable, str]:
    try:
        from automationbench.runner import compute_allowed_services, strip_none_values
        return strip_none_values, compute_allowed_services, "upstream"
    except Exception:  # noqa: BLE001 - verifiers missing: this adapter's copies, reported in the manifest
        return _strip_none, _allowed_services, "ouroloop"


@contextmanager
def hide_live_keys() -> Iterator[None]:
    """Hide the keys that would turn a simulated action into a live API call, for a whole run of episodes.

    Entered once, on the main thread, around concurrent attempts (task_eval.run_tasks): a per-call pop and restore
    from several threads would race with every other reader of os.environ (the claude CLI's child environment,
    the worker's), and a KeyError there would turn a paid call into an infrastructure failure. With the keys gone
    for the whole pool, the per-call guard below has nothing to pop.
    """
    saved = {k: os.environ.pop(k) for k in LIVE_KEYS if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


@contextmanager
def _no_live_keys() -> Iterator[None]:
    """The same guard for the duration of one tool call, for a single-threaded episode. Under concurrency the keys
    are already hidden by hide_live_keys and this is a no-op; it must never be the only guard in a threaded run."""
    saved = {k: os.environ.pop(k) for k in LIVE_KEYS if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


# ---------------------------------------------------------------------------- tool schemas
_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", dict: "object", list: "array"}


def _doc_args(doc: str) -> dict[str, str]:
    """The `Args:` block of a Google-style docstring: parameter -> description."""
    out, current = {}, None
    lines = doc.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() in ("Args:", "Arguments:")) + 1
    except StopIteration:
        return out
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith(":") and " " not in stripped[:-1]:     # Returns: / Raises: ends the block
            break
        name, sep, rest = stripped.partition(":")
        if sep and " " not in name.strip():
            current = name.strip()
            out[current] = rest.strip()
        elif current:
            out[current] = f"{out[current]} {stripped}".strip()
        else:
            break
    return out


def _json_type(annotation) -> str:
    """The JSON type of a parameter annotation, treating `X | None` as X: an optional argument is simply not
    required, and a model that is told a field may be null tends to send null where it should omit it."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union or str(origin) == "types.UnionType":
        inner = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(inner) == 1:
            return _json_type(inner[0])
        raise UpstreamError(f"unsupported parameter type {annotation!r}")
    if annotation in _JSON_TYPES:
        return _JSON_TYPES[annotation]
    raise UpstreamError(f"unsupported parameter type {annotation!r}")


def tool_spec(func: Callable) -> ToolSpec:
    """One upstream tool function as a spec for the agent's provider.

    The description is the whole docstring less the injected `world` argument, so the model reads exactly what
    upstream documents. Upstream builds its own schemas through another package; `contract_check` compares this
    one against it when that package is installed.
    """
    doc = inspect.getdoc(func) or func.__name__
    descriptions = _doc_args(doc)
    hints = typing.get_type_hints(func)
    props, required = {}, []
    for name, p in inspect.signature(func).parameters.items():
        if name in INJECTED:
            continue
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise UpstreamError(f"{func.__name__} takes *args/**kwargs, which has no schema")
        entry: dict[str, Any] = {"type": _json_type(hints.get(name, str))}
        if name in descriptions:
            entry["description"] = descriptions[name]
        if p.default is p.empty:
            required.append(name)
        elif p.default is not None:          # an optional argument's default is its absence, not a null
            entry["default"] = p.default
        props[name] = entry
    # The docstring keeps its Args block: it is what the model is told about each field, and cutting it would
    # make this surface weaker than upstream's without saying so.
    text = "\n".join(ln for ln in doc.splitlines() if not ln.strip().startswith("world:"))
    return ToolSpec(func.__name__, text.strip(), {"type": "object", "properties": props, "required": required,
                                                  "additionalProperties": False})


# ---------------------------------------------------------------------------- tasks
@dataclass(frozen=True)
class BenchTask:
    """One upstream task, as far as the agent may see it."""
    task_id: str                              # upstream info.task_name, e.g. "marketing.lead_enrichment"
    example_id: Any
    messages: tuple[dict, ...]                # the upstream prompt: a system message and the trigger
    tools: tuple[str, ...]                    # the tools this task grants (the api toolset grants all three)
    contract: str                             # upstream task_contract_sha256

    @property
    def system(self) -> str:
        return "\n\n".join(str(m.get("content", "")) for m in self.messages if m.get("role") == "system")

    @property
    def trigger(self) -> str:
        """Everything that is not the system prompt, as the agent's first message. Upstream's public tasks are one
        trigger message each; `contract_check` reports a task that is not, because flattening several turns into
        one is a change to what the model was given."""
        return "\n\n".join(str(m.get("content", "")) for m in self.messages if m.get("role") != "system")

    @property
    def turns(self) -> int:
        return sum(1 for m in self.messages if m.get("role") != "system")

    def entry(self, family_id: str) -> TaskEntry:
        return TaskEntry(self.task_id, family_id, self.example_id, self.contract, tuple(self.tools))


@dataclass(frozen=True)
class Grading:
    """What the task is scored against. Never handed to anything the agent reads.

    `info` is upstream's whole task info, normalised as its runner normalises it, because that is the shape the
    official scorer is called with; `assertions` is the part of it the rubric reads.
    """
    initial_state: dict
    assertions: tuple[dict, ...]
    allowed_services: tuple[str, ...]
    info: dict = field(default_factory=dict)


@dataclass
class Score:
    success: bool                             # upstream task_completed_correctly
    partial_credit: float
    passed: int
    scored: int
    excluded: int
    assertions: list[dict] = field(default_factory=list)   # per assertion: type, passed, excluded (evidence)
    end_state: dict = field(default_factory=dict)


class Suite:
    """A domain's tasks, with the grading kept apart from them."""

    def __init__(self, domain: str, tasks: dict[str, BenchTask], grading: dict[str, Grading], versions: dict):
        self.domain = domain
        self.tasks = tasks
        self.versions = versions
        self._grading = grading

    def __len__(self) -> int:
        return len(self.tasks)

    def ids(self) -> list[str]:
        return list(self.tasks)

    def entries(self, families: dict[str, str]) -> list[TaskEntry]:
        return [t.entry(families[tid]) for tid, t in self.tasks.items()]

    def episode(self, task_id: str) -> "Episode":
        return Episode(self.tasks[task_id], self._grading[task_id], self.versions)

    def grading(self, task_id: str) -> Grading:
        """The scorer's view. A caller that reaches for this on the agent's behalf is the bug this split is for."""
        return self._grading[task_id]


def load_suite(domain: str, path: str | Path | None = None) -> Suite:
    """Every public task of one upstream domain, through upstream's own loader.

    The loader is upstream's because the task data is upstream's: the noise injection, the per-row schema and the
    fingerprint all come from it, and a second loader here would be a second version of the benchmark.
    """
    versions = require(path)
    from automationbench.domains import get_domain_dataset
    from automationbench.task_contract import task_contract_sha256
    strip_none, allowed_services, source = _runner_fns()
    versions = {**versions, "runner": source}
    tasks: dict[str, BenchTask] = {}
    grading: dict[str, Grading] = {}
    for row in get_domain_dataset(domain):
        info = row["info"]
        info = json.loads(info) if isinstance(info, str) else info
        prompt = list(row["prompt"])
        task_id = info.get("task_name") or f"{domain}.{row.get('example_id')}"
        if task_id in tasks:
            raise UpstreamError(f"upstream domain {domain} has two tasks called {task_id!r}")
        # The fingerprint is taken from the task as upstream hands it over, before any normalisation: it has to
        # describe what a run actually received.
        contract = task_contract_sha256(example_id=row.get("example_id"), prompt=prompt, info=info)
        initial_state = strip_none(info.get("initial_state", {}))
        assertions = [strip_none(a) for a in info.get("assertions", [])]
        granted = tuple(info.get("zapier_tools", ()))
        tasks[task_id] = BenchTask(task_id, row.get("example_id"), tuple(prompt), granted, contract)
        normalised = {**info, "initial_state": initial_state, "assertions": assertions}
        grading[task_id] = Grading(initial_state, tuple(assertions),
                                   tuple(allowed_services(initial_state, assertions, list(granted))), normalised)
    if not tasks:
        raise UpstreamError(f"upstream domain {domain} has no tasks")
    return Suite(domain, tasks, grading, versions)


def families(suite: Suite, declared: dict[str, str] | None = None) -> tuple[dict[str, str], str]:
    """task_id -> family_id, and the rule used.

    Upstream ships no variant or family field: each public task is its own workflow, so one task is one family
    and grouping only keeps a task's own attempts, branches and repeats on one side of the split. Where tasks are
    known to be variants of one workflow, the config declares the groups and the rule says so; `near_duplicates`
    is what to look at before declaring them (docs/ROADMAP.md M1).
    """
    if not declared:
        return {tid: tid for tid in suite.ids()}, "task_name"
    unknown = sorted(set(declared) - set(suite.ids()))
    if unknown:
        raise ValueError(f"declared families name tasks upstream does not have: {unknown[:5]}")
    return {tid: declared.get(tid, tid) for tid in suite.ids()}, "declared"


def task_signature(suite: Suite, task_id: str) -> set[str]:
    """What a task is over: its granted tools, the services its world is subscribed to, its assertion types, and
    the values those assertions expect.

    The values are what make this discriminating. Many distinct Marketing workflows share a shape -- read a
    spreadsheet, email a recommendation -- and so share every tool, service and assertion type; two variants of
    one workflow also expect the same recipients, records and phrases.
    """
    g = suite.grading(task_id)
    sig = set(suite.tasks[task_id].tools) | {f"svc:{s}" for s in g.allowed_services}
    for a in g.assertions:
        sig.add(f"a:{a.get('type')}")
        for key, value in a.items():
            if key != "type" and isinstance(value, (str, int, float, bool)):
                text = str(value)
                if 2 <= len(text) <= 120:
                    sig.add(f"v:{key}={text}")
    return sig


# v0.3 step 5 (docs/modules/07_BENCH_REPORT.md §2 “新领域的划分分层”): the two static features a new domain's split
# is stratified on. Both are read from the task definition — the assertion list and the services the task's world
# is subscribed to — never from a result or a score.
#
# The kinds are the failure kinds the v0.2 analysis and the ceiling experiment read failures by
# (examples/automationbench/ceiling/tasks.json, rule.failure_kinds), applied to a type name: '_not_' ->
# forbidden_write, 'count' -> count, 'contains' -> content, else missing_write. A task's composition is the set of
# kinds among its scored assertions, not the exact multiset of types: upstream's 100 tasks of a domain have 56 to 98
# distinct type multisets, so strata of exact multisets would hold one task each and balance nothing.
ASSERTION_KINDS = ("missing_write", "forbidden_write", "content", "count")
# The tool family: whether the task's world is subscribed to a business application beyond the office services
# every domain shares (mail, drive, sheets, calendar, chat) — Salesforce, HubSpot, a help desk, an accounting book.
OFFICE_SERVICES = ("gmail", "google_drive", "google_sheets", "google_calendar", "slack")


def assertion_kind(type_name) -> str:
    """The failure kind of an assertion type, by its name (ASSERTION_KINDS)."""
    name = str(type_name or "")
    if "_not_" in name:
        return "forbidden_write"
    if "count" in name:
        return "count"
    if "contains" in name:
        return "content"
    return "missing_write"


def stratum(suite: Suite, task_id: str) -> str:
    """A task's stratum for the stratified split: `<kinds>|<tool family>`, e.g.
    `content+forbidden_write+missing_write|app`. The kinds are those of the assertions the task scores (an
    assertion its definition marks unscored or excluded is left out; the scorer's own free-assertion exclusion is
    not computed — that would mean scoring a state); the family is `app` when the world is subscribed to any
    service outside OFFICE_SERVICES, else `office`."""
    g = suite.grading(task_id)
    kinds = sorted({assertion_kind(a.get("type")) for a in g.assertions
                    if a.get("scored") is not False and a.get("excluded") is not True})
    family = "app" if set(g.allowed_services) - set(OFFICE_SERVICES) else "office"
    return f"{'+'.join(kinds) or 'none'}|{family}"


def pool_suites(name: str, suites: list[Suite]) -> Suite:
    """Several domains' suites as one (M7 §2): the tasks and their grading side by side, each task under its own
    upstream name, which carries its domain. Two tasks with one name, or a task whose name does not carry its
    domain, are refused rather than merged. The versions are the first suite's; every suite came from the same
    checkout, which is checked."""
    tasks: dict[str, BenchTask] = {}
    grading: dict[str, Grading] = {}
    for s in suites:
        stray = [t for t in s.ids() if not t.startswith(f"{s.domain}.")]
        if stray:
            raise UpstreamError(f"upstream domain {s.domain} names tasks {stray[:3]} without the domain prefix, so "
                                f"pooled task ids could collide")
        both = sorted(set(s.tasks) & set(tasks))
        if both:
            raise UpstreamError(f"upstream domains share the task ids {both[:3]}")
        if s.versions.get("commit") != suites[0].versions.get("commit") or \
                s.versions.get("path") != suites[0].versions.get("path"):
            raise UpstreamError(f"domain {s.domain} was loaded from another checkout than {suites[0].domain}")
        tasks.update(s.tasks)
        grading.update({t: s.grading(t) for t in s.ids()})
    return Suite(name, tasks, grading, suites[0].versions)


def near_duplicates(suite: Suite, threshold: float = 0.8) -> list[tuple[str, str, float]]:
    """Task pairs whose signatures overlap enough to be worth reading as possible variants of one workflow,
    highest overlap first.

    A hint for a human before the split is frozen, never a grouping: a high overlap says two tasks are over the
    same things, not that one is a variant of the other. Nothing declares a family on this alone.
    """
    sig = {tid: task_signature(suite, tid) for tid in suite.ids()}
    out = []
    ids = suite.ids()
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            union = sig[a] | sig[b]
            j = len(sig[a] & sig[b]) / len(union) if union else 0.0
            if j >= threshold:
                out.append((a, b, round(j, 3)))
    return sorted(out, key=lambda r: -r[2])


# ---------------------------------------------------------------------------- one episode
class Episode:
    """One attempt at one task: the tool surface the agent acts through, and the scorer the harness calls.

    The agent loop uses this as its `ToolSurface` (`specs` and `run`). `score` is not part of that surface, so an
    agent cannot ask how it is doing, and neither an assertion nor the expected state is reachable from anything
    it is given.
    """

    def __init__(self, task: BenchTask, grading: Grading, versions: dict, *, max_output_chars: int = 20000):
        from automationbench.schema.world import WorldState
        from automationbench.tools.api import API_TOOLS
        if versions.get("toolset", TOOLSET) != TOOLSET:
            raise UpstreamError(f"toolset {versions['toolset']!r}: only {TOOLSET!r} is wired up here")
        self.task = task
        self.versions = versions
        self._grading = grading
        self._funcs = {f.__name__: f for f in API_TOOLS}
        self._takes_world = {f.__name__: "world" in inspect.signature(f).parameters for f in API_TOOLS}
        self._specs = [tool_spec(f) for f in API_TOOLS]
        self.max_output_chars = max_output_chars
        # Both copies are deep, as upstream's runner makes them: the world the tools write to must not share a
        # nested list with the seeded state the scorer rebuilds, or an assertion the agent satisfied would look
        # like one that was already true and be excluded from the denominator.
        self._initial = copy.deepcopy(grading.initial_state)
        self.world = WorldState(**copy.deepcopy(grading.initial_state))
        self.world.meta.allowed_services = list(grading.allowed_services)
        self.calls: list[dict] = []            # the tool trace: name, args, error, elapsed
        self.errors = 0

    # -------------------------------------------------------------- ToolSurface
    def specs(self) -> list[ToolSpec]:
        return list(self._specs)

    def run(self, name: str, args: dict) -> ToolResult:
        func = self._funcs.get(name)
        if func is None:
            return ToolResult(f"unknown tool {name!r}", True)
        # Upstream drops an empty object before a call: several models emit {} for an argument they mean to omit,
        # and the parameter's own default is what they meant. Mirrored here so a run behaves as upstream's would.
        args = {k: v for k, v in (args or {}).items() if not (isinstance(v, dict) and not v)}
        if self._takes_world[name]:
            args = {**args, "world": self.world}
        t0 = time.monotonic()
        try:
            with _no_live_keys():
                out = func(**args)
        except TypeError as e:                 # wrong or missing arguments: the model's error, not the runner's
            return self._record(name, args, ToolResult(f"bad arguments for {name}: {e}", True), t0)
        except Exception as e:  # noqa: BLE001 - a tool that raises ends this call, not the episode
            return self._record(name, args, ToolResult(f"{type(e).__name__}: {e}"[:500], True), t0)
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
        return self._record(name, args, ToolResult(clip(text, self.max_output_chars), _is_api_error(text)), t0)

    def _record(self, name: str, args: dict, res: ToolResult, t0: float) -> ToolResult:
        self.errors += res.is_error
        self.calls.append({"tool": name, "args": {k: _short(v) for k, v in args.items() if k not in INJECTED},
                           "is_error": res.is_error, "elapsed": round(time.monotonic() - t0, 3)})
        return res

    def mutates(self, name: str, args: dict) -> bool:
        """A read cannot change the world, so only a writing `api_fetch` counts. The loop's change count is what
        the completion probe snapshots on: counting reads would snapshot identical states, and counting nothing
        would label every stop decision from the state at the end."""
        return name == "api_fetch" and str((args or {}).get("method", "")).upper() not in ("GET", "HEAD")

    @property
    def tool_calls(self) -> int:
        return len(self.calls)

    # -------------------------------------------------------------- scoring
    def score(self, world=None) -> Score:
        """The official metrics on the world the tools left, or on a snapshot of an earlier one.

        Nothing the agent wrote in a message is an input here. Upstream's `task_completed_correctly` reads the
        `partial_credit` its sibling caches on the state, so the two are called in that order on one state.
        """
        from automationbench import rubric
        partial = float(rubric.partial_credit(state := self.state(world)))
        success = bool(rubric.task_completed_correctly(state) == 1.0)
        results = list(state.get("_assertion_results", []))
        scored = [r for r in results if not r.get("excluded")]
        return Score(success, partial, sum(bool(r["passed"]) for r in scored), len(scored),
                     len(results) - len(scored), results, state.get("_end_state", {}))

    def state(self, world=None) -> dict:
        """The state dict the official scorer is called with, in the shape upstream's runner builds it."""
        info = self._grading.info or {"assertions": [dict(a) for a in self._grading.assertions]}
        return {"info": copy.deepcopy(info), "initial_state": self._initial,
                "world": self.world if world is None else world}

    def baseline_score(self) -> Score:
        """What doing nothing scores. Upstream excludes assertions that already pass, so this is normally 0; a
        task whose correct action is to do nothing is the exception, and a baseline above 0 is worth reading
        before a result is called a success (docs/DESIGN.md §4.1)."""
        return Episode(self.task, self._grading, self.versions).score()

    # -------------------------------------------------------------- branching
    def snapshot(self):
        """An independent copy of the world, for trying another action from the same state (docs/DESIGN.md §4.2).
        pydantic's deep copy is a real copy; `contract_check` verifies that a write through one does not reach
        the other rather than trusting it."""
        return self.world.model_copy(deep=True)

    def restore(self, snapshot) -> None:
        self.world = snapshot.model_copy(deep=True)

    def close(self) -> None:
        """The simulated world is process-local state; there is nothing outside this process to tear down."""
        self.world = None


def _is_api_error(text: str) -> bool:
    """Upstream returns a refusal as a JSON body, the way the real API would, rather than raising. Reading it
    keeps the agent's error count meaningful; the text the model sees is unchanged."""
    if not text.startswith("{"):
        return False
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    err = body.get("error") if isinstance(body, dict) else None
    return bool(err)


def _short(value: Any, limit: int = 400) -> Any:
    return value if not isinstance(value, str) or len(value) <= limit else value[:limit] + "..."


# ---------------------------------------------------------------------------- what the agent is given
def agent_payload(task: BenchTask, episode: Episode, system: str | None = None) -> dict:
    """Exactly what reaches the model: the system prompt, the trigger, and the tool surface."""
    return {"system": system if system is not None else task.system, "task": task.trigger,
            "tools": {s.name: s.description for s in episode.specs()}, "schemas": [s.schema for s in
                                                                                   episode.specs()]}


def _strings(obj) -> Iterator[str]:
    """Every string in a structure, and every key. A leak has to be looked for in the values the model will read,
    not in a JSON rendering of them: an assertion pasted into a prompt comes back escaped there and a substring
    search over the rendering misses it."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _strings(value)


def _keys(obj) -> Iterator[str]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key)
            yield from _keys(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _keys(value)


def audit_inputs(task: BenchTask, grading: Grading, payload: dict) -> dict:
    """Whether anything the agent is given carries grading information (docs/ROADMAP.md M1).

    `leaks` are structural and must be empty: an assertion, an expected value or the seeded state reaching the
    prompt or a tool description. `overlaps` are long strings that appear both in an assertion and in the prompt;
    a task legitimately tells the agent what to write, so these are for a human to read, not a failure.
    """
    texts = list(_strings(payload))
    keys = set(_keys(payload))
    leaks, overlaps = [], []
    for a in grading.assertions:
        canonical = json.dumps(a, ensure_ascii=False, sort_keys=True, default=str)
        if any(canonical in t for t in texts):
            leaks.append(f"{task.task_id}: the assertion {a.get('type')} appears verbatim in the agent's input")
        kind = str(a.get("type", ""))
        if kind and (kind in keys or any(kind in t for t in texts)):
            leaks.append(f"{task.task_id}: the assertion type {kind!r} appears in the agent's input")
        for key, value in a.items():
            if key != "type" and isinstance(value, str) and len(value) >= 24 and any(value in t for t in texts):
                overlaps.append(f"{task.task_id}: {key} of {a.get('type')} also appears in the prompt "
                                f"({value[:40]!r})")
    for key in ("assertions", "initial_state", "partial_credit", "_assertion_results", "_end_state"):
        if key in keys:
            leaks.append(f"{task.task_id}: the agent's input carries a {key!r} key")
    return {"leaks": sorted(set(leaks)), "overlaps": sorted(set(overlaps))}


# ---------------------------------------------------------------------------- contract checks
def _rubric_entry() -> tuple[list[str], list[float], str]:
    """The official rubric's scoring functions and weights, when its own entry point is importable."""
    from automationbench.rubric import create_rubric
    r = create_rubric()
    return [getattr(f, "__name__", str(f)) for f in r.funcs], list(r.weights), "create_rubric"


def contract_check(suite: Suite, task_ids: list[str]) -> dict:
    """Whether this adapter measures what upstream measures (docs/ROADMAP.md M1).

    Every finding is a fact about this installation, reported rather than raised, so that a phase can print the
    whole picture and a caller can decide what is disqualifying. `failed` is what must be empty before a number
    from this adapter is comparable with an upstream one.

    `task_ids` is named, never defaulted to every task: the per-task part of this check scores a task's seeded
    state, and a held-out task's score is not something a development run may look at (docs/DESIGN.md §3.1).

    Checked here:
      the official rubric entry point is the pair of functions `score` calls, with the weights it assumes;
      the services a task's world is subscribed to are upstream's, not this adapter's copy;
      a tool schema built here matches the one upstream's own generator builds;
      a snapshot is independent of the world it was taken from, so a branch cannot leak into the original;
      scoring reads the world and nothing else: an empty run scores the baseline however it is narrated, and a
        wrong write scores what was written.
    """
    ids = list(task_ids)
    out: dict[str, Any] = {"checked": ids, "failed": [], "notes": [], "inverse_tasks": []}

    try:
        funcs, weights, entry = _rubric_entry()
        out["rubric"] = {"entry": entry, "funcs": funcs, "weights": weights}
        if funcs != ["partial_credit", "task_completed_correctly"]:
            out["failed"].append(f"the official rubric scores with {funcs}; this adapter calls "
                                 f"partial_credit and task_completed_correctly")
        elif weights[:2] != [1.0, 0.0]:
            out["notes"].append(f"the official rubric weights changed to {weights}; the reported pass rate is "
                                f"task_completed_correctly either way")
    except Exception as e:  # noqa: BLE001 - the entry point lives behind verifiers; the functions do not
        out["rubric"] = {"entry": "", "funcs": [], "weights": []}
        out["notes"].append(f"the official rubric entry point (create_rubric) is not importable "
                            f"({type(e).__name__}); partial_credit and task_completed_correctly were called "
                            f"directly, so a change to which functions score a task would not be noticed here")

    try:
        from automationbench.runner import compute_allowed_services
        for tid in ids:
            g = suite.grading(tid)
            theirs = compute_allowed_services(g.initial_state, [dict(a) for a in g.assertions],
                                              list(suite.tasks[tid].tools))
            if sorted(theirs) != sorted(g.allowed_services):
                out["failed"].append(f"{tid}: services subscribed here {list(g.allowed_services)} != upstream "
                                     f"{sorted(theirs)}")
    except Exception as e:  # noqa: BLE001 - verifiers missing: this adapter's copy ran, and says so
        out["notes"].append(f"upstream's compute_allowed_services is not importable ({type(e).__name__}); this "
                            f"adapter's copy decided which services each task's world is subscribed to")

    try:
        from agents.function_schema import function_schema
        from automationbench.tool_wrapper import _create_tool_wrapper
        from automationbench.tools.api import API_TOOLS
        for func in API_TOOLS:
            mine = tool_spec(func).schema
            theirs = function_schema(_create_tool_wrapper(func, args_to_skip=list(INJECTED))).params_json_schema
            theirs.pop("$defs", None)
            if sorted(mine["properties"]) != sorted(theirs.get("properties", {})):
                out["failed"].append(f"{func.__name__}: parameters here {sorted(mine['properties'])} != upstream "
                                     f"{sorted(theirs.get('properties', {}))}")
            elif {k: v.get("type") for k, v in mine["properties"].items()} != \
                    {k: _plain_type(v) for k, v in theirs.get("properties", {}).items()}:
                out["notes"].append(f"{func.__name__}: parameter types are written differently than upstream's "
                                    f"generator writes them; the names and requiredness match")
    except Exception as e:  # noqa: BLE001 - upstream's generator needs another package; the names are checked
        out["notes"].append(f"upstream's tool schema generator is not importable ({type(e).__name__}); the tool "
                            f"schemas offered to the model were built here from the same functions")

    for tid in ids:
        if suite.tasks[tid].turns != 1:
            out["failed"].append(f"{tid}: upstream gives it {suite.tasks[tid].turns} non-system messages; this "
                                 f"adapter hands the agent one, so the model would not see what upstream sends")
        ep = suite.episode(tid)
        try:
            base = ep.score()
            if base.success:
                # Doing nothing already satisfies every scored assertion: the correct action is to leave the world
                # alone, so a pass on this task is not evidence that the agent did anything.
                out["inverse_tasks"].append(tid)
            snap = ep.snapshot()
            before = json.dumps(snap.model_dump(mode="json"), sort_keys=True, default=str)
            res = ep.run("api_fetch", {"method": "POST", "url": "https://slack.com/api/chat.postMessage",
                                       "body": json.dumps({"channel": "ouroloop-check", "text": "contract check"})})
            after = json.dumps(snap.model_dump(mode="json"), sort_keys=True, default=str)
            if before != after:
                out["failed"].append(f"{tid}: a write after the snapshot changed the snapshot, so branches would "
                                     f"not be independent")
            # The write went to a channel no assertion mentions (and, for a world not subscribed to Slack, was
            # refused): either way the score must be what doing nothing scores. A run that only talks cannot move
            # it, because nothing the agent says is an input to the scorer.
            wrote = ep.score()
            if (wrote.success, round(wrote.partial_credit, 6)) != (base.success, round(base.partial_credit, 6)):
                out["failed"].append(f"{tid}: an unrelated write moved the score from {base.partial_credit} to "
                                     f"{wrote.partial_credit}; the scorer is not reading the assertions alone")
            out.setdefault("baseline", {})[tid] = {"partial_credit": round(base.partial_credit, 4),
                                                  "success": base.success, "scored": base.scored,
                                                  "excluded": base.excluded, "refused": res.is_error}
        finally:
            ep.close()
    return out


def _plain_type(entry: dict) -> str | None:
    """The JSON type another generator wrote, ignoring how it spelled "or null"."""
    t = entry.get("type")
    if isinstance(t, list):
        rest = [x for x in t if x != "null"]
        return rest[0] if len(rest) == 1 else None
    if t is None and isinstance(entry.get("anyOf"), list):
        rest = [x.get("type") for x in entry["anyOf"] if x.get("type") != "null"]
        return rest[0] if len(rest) == 1 else None
    return t


# ---------------------------------------------------------------------------- the completion probe
class CompletionProbe:
    """An environment label for every stop decision of an episode: was the task actually complete at that moment?

    This is the first of the two feedback paths in docs/DESIGN.md §4.2 -- a locally verifiable outcome. At each
    stop decision it keeps a snapshot of the world; after the episode it scores each snapshot with the official
    rubric and labels the decisions that were made on that state. What it trains is the completion judgement, not
    a claim that continuing is always worth it.

    It is not a checker. The scoring happens after the run, so neither StopGuard nor the view nor the agent can
    see it, and an official scorer is never consulted while the agent is working (§5.2). Nor can it fail a run: a
    state it cannot copy or score stays unlabelled.

    Why this point and this label: a Marketing task's `task_completed_correctly` is a conjunction over a median of
    twelve scored assertions, so a task is lost by leaving one requirement undone. Whether the work is finished is
    exactly the judgement that decides that, it is visible from the state at the time, and on a training task it
    can be checked. Whether a model learns it is a separate question, which the task results answer.
    """

    def __init__(self, rt, episode: Episode, max_snapshots: int = 60, surface=None):
        # `surface` is what the agent actually calls, which may wrap the episode. The probe snapshots on the
        # agent's change count, and that count comes from the surface's `mutates`: a surface that cannot answer
        # would leave the count at zero and every label would describe the state at the first stop decision. That
        # is a silent wrong label on every training example, so it is refused here rather than discovered later.
        if surface is not None and getattr(surface, "mutates", None) is None:
            raise UpstreamError(f"{type(surface).__name__} does not report which calls change the world, so the "
                                f"probe cannot tell one state from another")
        self.rt = rt
        self.episode = episode
        self.max_snapshots = max_snapshots        # a bound on memory, not a judgement: ~0.1 MB per world
        self.snaps: list[dict] = []               # {"changes", "world", "decisions"}
        self.dropped = 0

    def snap(self, decision: str, changes: int) -> None:
        """At a stop decision. `changes` is the agent's count of calls that may have changed the world: an
        unchanged state is not copied again, and the decisions made on it share one snapshot."""
        if not self.snaps or self.snaps[-1]["changes"] != changes:
            world = None
            if len(self.snaps) < self.max_snapshots:
                try:
                    world = self.episode.snapshot()
                except Exception:  # noqa: BLE001 - a state that cannot be copied stays unlabelled
                    world = None
            else:
                self.dropped += 1
            self.snaps.append({"changes": changes, "world": world, "decisions": []})
        self.snaps[-1]["decisions"].append(decision)

    def label(self) -> int:
        """After the episode: label every stop decision from its own snapshot. Returns how many were labelled.

        A snapshot the scorer cannot read is recorded as a label nobody could give (label None), not skipped and
        certainly not guessed: a feedback builder then reports `unknown`, and a "no" that came from a broken
        scorer would be a wrong label on a real decision (docs/modules/02_EVIDENCE_TRAINING.md §2).
        """
        labelled = 0
        for s in self.snaps:
            if s["world"] is None:
                for d in s["decisions"]:
                    self.rt.outcome(d, None, source="env", note="completion probe",
                                    error="the state at this decision could not be copied")
                continue
            try:
                score = self.episode.score(s["world"])
            except Exception as e:  # noqa: BLE001 - the scorer failed: unknown, never a guessed label
                for d in s["decisions"]:
                    self.rt.outcome(d, None, source="env", note="completion probe",
                                    error=f"{type(e).__name__}: {e}"[:300])
                continue
            for d in s["decisions"]:
                self.rt.outcome(d, "yes" if score.success else "no", source="env", note="completion probe",
                                score=round(score.partial_credit, 6), assertions=f"{score.passed}/{score.scored}")
                labelled += 1
        self.snaps = [{**s, "world": None} for s in self.snaps]      # the worlds are not needed again
        return labelled
