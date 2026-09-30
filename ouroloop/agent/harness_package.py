"""M0/M1: the capability package — what a candidate may change in the harness, and how the host mounts it
(docs/modules/00_CONTRACTS.md §2, docs/modules/01_POLICY_RUNTIME.md §1).

A package is a small directory of fixed shape: `manifest.json`, `skills/**.md`, `memory.md`, and — from the second
delivery stage — `tools/*.py`, `middlewares/*.py` and `tests/*.py`. It is stored content-addressed: every file is an
immutable object, the run files form one bundle and the tests another, so what was checked, evaluated and exported
are the same bytes.

The host reads text components itself: memory is projected into the system prompt, skills are listed there by name
and read through one fixed host tool. Generated code is never imported here. Whether a package needs the isolated
worker is decided on the complete package, inherited entries included, in two ways that are not the same question:
a check of a draft needs the worker whenever the run or test bundle holds Python; running a package needs it only
when the manifest names tools or hooks or the run bundle holds Python (the test bundle is never mounted).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..evolve import contracts as ct

API_VERSION = "harness_package.v1"
HOOK_SITES = ("before_tool", "after_tool", "on_finish")
TEXT_KINDS = ("memory", "skills")
CODE_KINDS = ("on_finish", "before_tool", "after_tool", "tools")
KINDS = (*TEXT_KINDS, *CODE_KINDS)
SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PATTERNS = (
    ("manifest", re.compile(r"^manifest\.json$")),
    ("memory", re.compile(r"^memory\.md$")),
    ("skill", re.compile(r"^skills/[a-z0-9][a-z0-9_/-]*\.md$")),
    ("tool", re.compile(r"^tools/[a-z0-9_]+\.py$")),
    ("middleware", re.compile(r"^middlewares/[a-z0-9_]+\.py$")),
    ("test", re.compile(r"^tests/[a-z0-9_]+\.py$")),
)


class PackageError(ct.ContractError):
    """A package that breaks the fixed shape: a structure error, returned to R as check output."""


@dataclass(frozen=True)
class Limits:
    """What K fixes about packages. R cannot raise any of these (docs/modules/04_RESEARCH_LOOP.md §2)."""
    max_files: int = 24
    max_package_bytes: int = 262144
    max_memory_chars: int = 8000
    max_skill_chars: int = 8000
    max_skills: int = 12
    components: tuple[str, ...] = TEXT_KINDS

    def __post_init__(self) -> None:
        unknown = sorted(set(self.components) - set(KINDS))
        if unknown:
            raise PackageError(f"unknown component kinds {unknown}: {list(KINDS)}")

    @property
    def allows_code(self) -> bool:
        return any(k in CODE_KINDS for k in self.components)

    def to_json(self) -> dict:
        return {"max_files": self.max_files, "max_package_bytes": self.max_package_bytes,
                "max_memory_chars": self.max_memory_chars, "max_skill_chars": self.max_skill_chars,
                "max_skills": self.max_skills, "components": list(self.components)}

    @classmethod
    def from_json(cls, d: dict | None) -> "Limits":
        d = dict(d or {})
        if "components" in d:
            d["components"] = tuple(d["components"])
        return cls(**d)


# ---------------------------------------------------------------------------- shape
def classify(path: str) -> str:
    """The file's role, or a PackageError. Relative POSIX paths only: no absolute path, no `..`, nothing outside
    the fixed directories."""
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        raise PackageError(f"{path!r}: package paths are relative POSIX paths")
    parts = PurePosixPath(path).parts
    if any(p in ("", ".", "..") for p in parts) or str(PurePosixPath(path)) != path:
        raise PackageError(f"{path!r}: no '.', '..' or empty segments")
    for role, pattern in PATTERNS:
        if pattern.match(path):
            return role
    raise PackageError(f"{path!r}: not a package file (manifest.json, memory.md, skills/**.md, tools/*.py, "
                       f"middlewares/*.py, tests/*.py)")


def empty_manifest() -> dict:
    return {"api_version": API_VERSION, "tools": [], "hooks": [], "skills": [], "memory": {"enabled": False}}


def parse_manifest(text: str, files: dict[str, str]) -> dict:
    """The manifest, checked against the files it names. Unknown keys, unknown sites, duplicate names and entries
    outside the bundle are structure errors."""
    try:
        m = json.loads(text)
    except json.JSONDecodeError as e:
        raise PackageError(f"manifest.json is not JSON: {e}") from None
    if not isinstance(m, dict):
        raise PackageError("manifest.json is an object")
    unknown = sorted(set(m) - {"api_version", "tools", "hooks", "skills", "memory"})
    if unknown:
        raise PackageError(f"manifest.json does not take {unknown}")
    if m.get("api_version") != API_VERSION:
        raise PackageError(f"manifest api_version {m.get('api_version')!r}: this host serves {API_VERSION}")
    out = empty_manifest()
    names: set[str] = set()
    for key in ("tools", "hooks", "skills"):
        if not isinstance(m.get(key) or [], list):
            raise PackageError(f"manifest {key} is a list")
    for tool in m.get("tools") or []:
        _need(tool, {"name", "description", "input_schema", "entrypoint"}, "tool")
        _unique(tool["name"], names, "tool")
        if tool["name"] in HOST_TOOLS:
            raise PackageError(f"tool {tool['name']}: the name is the host's own tool")
        _entrypoint(tool["entrypoint"], "tools/", files)
        if not isinstance(tool["input_schema"], dict) or tool["input_schema"].get("type") != "object":
            raise PackageError(f"tool {tool['name']}: input_schema is a JSON object schema")
        out["tools"].append({k: tool[k] for k in ("name", "description", "input_schema", "entrypoint")})
    for hook in m.get("hooks") or []:
        _need(hook, {"name", "site", "entrypoint"}, "hook")
        _unique(hook["name"], names, "hook")
        if hook["site"] not in HOOK_SITES:
            raise PackageError(f"hook {hook['name']}: site {hook['site']!r} is one of {list(HOOK_SITES)}")
        _entrypoint(hook["entrypoint"], "middlewares/", files)
        out["hooks"].append({"name": hook["name"], "site": hook["site"], "entrypoint": hook["entrypoint"],
                             "enabled": bool(hook.get("enabled", True))})
    for skill in m.get("skills") or []:
        _need(skill, {"name", "path"}, "skill", optional={"description", "enabled"})
        if not SKILL_NAME.match(str(skill["name"])):
            raise PackageError(f"skill name {skill['name']!r}: lowercase letters, digits, '-' and '_'")
        _unique(skill["name"], names, "skill")
        if skill["path"] not in files or classify(skill["path"]) != "skill":
            raise PackageError(f"skill {skill['name']}: {skill['path']!r} is not a skills/*.md file of the package")
        out["skills"].append({"name": skill["name"], "path": skill["path"],
                              "description": str(skill.get("description", ""))[:300],
                              "enabled": bool(skill.get("enabled", True))})
    memory = m.get("memory") or {}
    if not isinstance(memory, dict) or sorted(set(memory) - {"enabled"}):
        raise PackageError("memory is {enabled: bool}")
    if memory.get("enabled") and "memory.md" not in files:
        raise PackageError("memory is enabled and there is no memory.md")
    out["memory"] = {"enabled": bool(memory.get("enabled"))}
    return out


def _need(entry, required: set, what: str, optional: set = frozenset()) -> None:
    if not isinstance(entry, dict):
        raise PackageError(f"a {what} entry is an object")
    missing = sorted(required - set(entry))
    extra = sorted(set(entry) - required - set(optional) - {"enabled"})
    if missing or extra:
        raise PackageError(f"a {what} entry needs {sorted(required)}; missing {missing}, unknown {extra}")
    for key in required | (set(optional) & set(entry)):
        if key in ("input_schema", "enabled"):
            continue
        if not isinstance(entry[key], str):
            raise PackageError(f"a {what} entry's {key} is a string")


def _unique(name, seen: set, what: str) -> None:
    if not isinstance(name, str) or not name:
        raise PackageError(f"a {what} needs a name")
    if name in seen:
        raise PackageError(f"{what} name {name!r} is used twice")
    seen.add(name)


def _entrypoint(entry: str, prefix: str, files: dict) -> None:
    module, _, func = str(entry).partition(":")
    if not module.startswith(prefix) or module not in files or not func.isidentifier():
        raise PackageError(f"entrypoint {entry!r}: <{prefix}file.py>:<function> inside the package")


def kinds_of(manifest: dict, files: dict[str, str]) -> set[str]:
    """The component kinds a complete package uses, in K's vocabulary."""
    kinds = set()
    if manifest["memory"]["enabled"] or "memory.md" in files:
        kinds.add("memory")
    if manifest["skills"]:
        kinds.add("skills")
    if manifest["tools"] or any(classify(p) == "tool" for p in files):
        kinds.add("tools")
    for hook in manifest["hooks"]:
        kinds.add(hook["site"])
    if any(classify(p) == "middleware" for p in files) and not manifest["hooks"]:
        kinds.add("on_finish")          # a middleware file no hook names is still code the core must allow
    return kinds


def needs_worker_for_checks(files: dict[str, str], manifest: dict) -> bool:
    """The check predicate: any Python in the run or test bundle, or any tool/hook entry."""
    return bool(manifest["tools"] or manifest["hooks"]) or any(p.endswith(".py") for p in files)


def needs_worker_at_runtime(run_files: dict[str, str], manifest: dict) -> bool:
    """The runtime predicate: tool/hook entries or Python in the run bundle. Tests are never mounted to run."""
    return bool(manifest["tools"] or manifest["hooks"]) or any(p.endswith(".py") for p in run_files)


def check_shape(files: dict[str, str], limits: Limits) -> dict:
    """Every structural rule of M0 §2 on a complete package. -> the parsed manifest, or a PackageError."""
    if not isinstance(files, dict):
        raise PackageError("a package is a mapping of path to text")
    for path, text in files.items():
        classify(path)
        if not isinstance(text, str):
            raise PackageError(f"{path}: file contents are text")
    if len(files) > limits.max_files:
        raise PackageError(f"{len(files)} files: the core allows {limits.max_files}")
    size = sum(len(t.encode("utf-8")) for t in files.values())
    if size > limits.max_package_bytes:
        raise PackageError(f"{size} bytes: the core allows {limits.max_package_bytes}")
    manifest = parse_manifest(files.get("manifest.json", json.dumps(empty_manifest())), files)
    if len(manifest["skills"]) > limits.max_skills:
        raise PackageError(f"{len(manifest['skills'])} skills: the core allows {limits.max_skills}")
    if len(files.get("memory.md", "")) > limits.max_memory_chars:
        raise PackageError(f"memory.md has {len(files['memory.md'])} characters: the core allows "
                           f"{limits.max_memory_chars}")
    for skill in manifest["skills"]:
        if len(files[skill["path"]]) > limits.max_skill_chars:
            raise PackageError(f"{skill['path']} has {len(files[skill['path']])} characters: the core allows "
                               f"{limits.max_skill_chars}")
    code = sorted(p for p in files if p.endswith(".py"))
    if code and not limits.allows_code:
        raise PackageError(f"code files {code[:5]} are not allowed by this core: it allows "
                           f"{list(limits.components)} only; write memory or skills instead")
    listed = {s["path"] for s in manifest["skills"]}
    stray = sorted(p for p in files if classify(p) == "skill" and p not in listed)
    if stray:
        raise PackageError(f"skill files not listed in the manifest: {stray}")
    if "memory.md" in files and not manifest["memory"]["enabled"]:
        raise PackageError("memory.md is present and the manifest does not enable memory: it would never reach "
                           "the actor")
    used = kinds_of(manifest, files)
    outside = sorted(used - set(limits.components))
    if outside:
        raise PackageError(f"component kinds {outside} are not allowed by this core: {list(limits.components)}")
    if any(classify(p) == "test" for p in files) and not limits.allows_code:
        raise PackageError("tests/*.py are allowed only when the core allows a code component")
    return manifest


# ---------------------------------------------------------------------------- literals
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PROPER = re.compile(r"^[A-Z\u00C0-\u024F][\w\u00C0-\u024F.&'-]*(\s+[-&]?\s*[A-Z\u00C0-\u024F0-9][\w\u00C0-\u024F.&'-]*)+$")


def literal_kind(text: str) -> str | None:
    """Which kind of task-specific value a string is, or None for ordinary words. E-mail addresses; identifiers
    (a digit, '@', '_' or '-' inside one token); proper names of two or more capitalised words. A single ordinary
    word such as "general" or "update" is never a literal, whatever key it came from."""
    text = text.strip()
    if len(text) < 4 or len(text) > 200:
        return None
    if EMAIL.fullmatch(text):
        return "e-mail address"
    if " " not in text and (any(ch.isdigit() for ch in text) or any(ch in text for ch in "_@")
                            or ("-" in text and any(ch.isupper() for ch in text))):
        return "identifier"
    if PROPER.match(text):
        return "record name"
    return None


def literals_of(values) -> set[str]:
    """The task-specific strings inside assertion params or an initial state, by literal_kind."""
    out: set[str] = set()

    def walk(v) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, str) and literal_kind(v):
            out.add(v.strip())
    walk(values)
    return out


def _texts(files: dict[str, str]) -> str:
    """What the host actually uses: file paths, file bodies, and the decoded strings of the manifest (a JSON
    escape must not hide a literal from the check while the decoded text reaches the prompt)."""
    parts = list(files) + [t for p, t in files.items() if p != "manifest.json"]
    if "manifest.json" in files:
        try:
            parts.append(json.dumps(json.loads(files["manifest.json"]), ensure_ascii=False))
        except (json.JSONDecodeError, TypeError):
            parts.append(files["manifest.json"])
    return "\n".join(parts)


def literal_hits(files: dict[str, str], literals: set[str], *, allowed: set[str] = frozenset()) -> list[str]:
    """Which forbidden literals occur in the package, matched as whole tokens, case-insensitively. A hit is a
    structure error: a single task's answer written into a shared component (docs/modules/00_CONTRACTS.md §3)."""
    body = _texts(files)
    hits = []
    for lit in sorted(set(literals) - set(allowed)):
        if re.search(r"(?<![\w@.-])" + re.escape(lit) + r"(?![\w@-])", body, re.IGNORECASE):
            hits.append(lit)
    return hits


def describe_hits(hits: list[str]) -> str:
    """The check output for literal hits: counts by kind, never the values, so the output returned to R carries
    no task data of its own (docs/modules/05_EXECUTION_BUDGET.md §1)."""
    counts: dict = {}
    for h in hits:
        kind = literal_kind(h) or "value"
        counts[kind] = counts.get(kind, 0) + 1
    listed = ", ".join(f"{n} {k}{'es' if n > 1 and k.endswith('ss') else 's' if n > 1 else ''}"
                       for k, n in sorted(counts.items()))
    return f"the package contains task-specific values from train tasks ({listed}); replace them with a general rule"


# ---------------------------------------------------------------------------- storage
def _bundle(objects: ct.Objects, files: dict[str, str]) -> str:
    return objects.put_json("bundle", {"files": {p: objects.put_json("file", {"text": files[p]})
                                                 for p in sorted(files)}})


def api_ref(objects: ct.Objects) -> str:
    return objects.put_json("change", {"package_api": API_VERSION, "hook_sites": list(HOOK_SITES)})


def dependency_ref(objects: ct.Objects, image: str | None = None) -> str:
    return objects.put_json("change", {"dependency_set": "host-only" if image is None else "image",
                                       "image": image})


def build_package(objects: ct.Objects, files: dict[str, str], limits: Limits, *, evidence_refs=(),
                  image: str | None = None) -> str:
    """A complete package, checked and stored. -> its ref. Nothing is imported or executed here."""
    manifest = check_shape(files, limits)
    run = {p: t for p, t in files.items() if classify(p) != "test"}
    tests = {p: t for p, t in files.items() if classify(p) == "test"}
    run.setdefault("manifest.json", json.dumps(manifest, sort_keys=True, indent=1))
    package = ct.HarnessPackage(
        bundle_ref=_bundle(objects, run), manifest_ref=objects.put_json("manifest", manifest),
        package_api_ref=api_ref(objects), dependency_set_ref=dependency_ref(objects, image),
        test_bundle_ref=_bundle(objects, tests) if tests else None, evidence_refs=tuple(evidence_refs))
    return objects.put(package)


def empty_package(objects: ct.Objects) -> str:
    return build_package(objects, {"manifest.json": json.dumps(empty_manifest(), sort_keys=True, indent=1)},
                         Limits())


def files_of(objects: ct.Objects, package_ref: str) -> tuple[dict[str, str], dict[str, str]]:
    """-> (run files, test files) of a stored package."""
    package = objects.load(package_ref, ct.HarnessPackage)

    def read(bundle_ref):
        listing = objects.get(bundle_ref)["files"]
        return {p: objects.get(ref)["text"] for p, ref in listing.items()}
    # An exported app ships no test bundle (tests never run in a session), so a missing one reads as empty.
    tests = read(package.test_bundle_ref) if package.test_bundle_ref and objects.has(package.test_bundle_ref) else {}
    return read(package.bundle_ref), tests


def apply_patch(objects: ct.Objects, parent_ref: str, patch: dict) -> dict[str, str]:
    """The complete draft: the parent's files with this patch applied. `patch` is {"write": {path: text},
    "delete": [path]}; unknown keys and paths outside the package shape are structure errors."""
    if not isinstance(patch, dict) or not patch or sorted(set(patch) - {"write", "delete"}):
        raise PackageError('a package patch is a non-empty {"write": {path: text}, "delete": [path]}')
    write, delete = patch.get("write") or {}, patch.get("delete") or []
    if not isinstance(write, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in write.items()):
        raise PackageError('"write" maps paths to text')
    if not isinstance(delete, list) or not all(isinstance(p, str) for p in delete):
        raise PackageError('"delete" is a list of paths')
    if not write and not delete:
        raise PackageError("the patch changes nothing")
    run, tests = files_of(objects, parent_ref)
    files = {**run, **tests}
    for path in delete:
        classify(path)
        if path not in files:
            raise PackageError(f"delete {path!r}: the parent package has no such file")
        del files[path]
    for path, text in write.items():
        classify(path)
        files[path] = text
    if files == {**run, **tests}:
        raise PackageError("the patch leaves the package exactly as it was")
    return files


# ---------------------------------------------------------------------------- what the host mounts
@dataclass
class PackageView:
    """A stored package as the host reads it. Text components only; code entries are for the isolated worker."""
    ref: str
    manifest: dict
    run_files: dict = field(default_factory=dict)
    test_files: dict = field(default_factory=dict)

    @property
    def memory(self) -> str:
        return self.run_files.get("memory.md", "") if self.manifest["memory"]["enabled"] else ""

    @property
    def skills(self) -> list[dict]:
        return [s for s in self.manifest["skills"] if s["enabled"]]

    def skill_text(self, name: str) -> str | None:
        for s in self.skills:
            if s["name"] == name:
                return self.run_files[s["path"]]
        return None

    @property
    def runs_code(self) -> bool:
        return needs_worker_at_runtime(self.run_files, self.manifest)

    def prompt_block(self) -> str:
        """What the host adds to the system prompt: the memory, then the skill index. Skill bodies are read on
        demand through read_skill, so the prompt carries names and one-line descriptions only."""
        parts = []
        if self.memory:
            parts.append("Working notes for this kind of task:\n" + self.memory.strip())
        if self.skills:
            index = "\n".join(f"- {s['name']}: {s['description']}" if s["description"] else f"- {s['name']}"
                              for s in self.skills)
            parts.append("Skills you can read with the read_skill tool before you act:\n" + index)
        return "\n\n".join(parts)


def load_package(objects: ct.Objects, package_ref: str) -> PackageView:
    run, tests = files_of(objects, package_ref)
    package = objects.load(package_ref, ct.HarnessPackage)
    manifest = objects.get(package.manifest_ref)
    return PackageView(package_ref, manifest, run, tests)


# ---------------------------------------------------------------------------- the host side of a mounted package
HOST_TOOLS = ("read_skill",)          # tools the host adds; a package tool may not take their names
HOOK_ACTIONS = {"before_tool": ("continue", "block", "feedback"), "after_tool": ("continue", "feedback"),
                "on_finish": ("continue", "block", "feedback")}


class CapabilityError(RuntimeError):
    """A package that needs a capability this host does not have (the isolated worker). Refused at assembly,
    never run in-process (docs/modules/00_CONTRACTS.md §3)."""


class PackageRuntime:
    """One episode's view of a mounted package: its worker session (started on first use, closed at the end),
    the broker over the episode's base tools, and what every tool and hook call did."""

    def __init__(self, surface, view: PackageView, worker=None, on_event=None):
        self.surface, self.package, self.worker, self.on_event = surface, view, worker, on_event
        self._session = None
        self.state: dict = {}                       # the episode view the agent keeps current

    def session(self):
        if self._session is None:
            if self.worker is None:
                raise CapabilityError(f"package {ct.short(self.package.ref)} runs code and no isolated worker is "
                                      f"available")
            import time
            self._started, self._requests = time.monotonic(), 0
            self._session = self.worker.start()
            self._session.load({**self.package.run_files})          # the run bundle only: tests never run here
        self._requests = getattr(self, "_requests", 0) + 1
        return self._session

    def end(self) -> None:
        if self._session is not None:
            import time
            self._session.close()
            self._session = None
            # Worker time is part of what a candidate costs (docs/modules/05_EXECUTION_BUDGET.md §2).
            self.emit("worker_session", seconds=round(time.monotonic() - self._started, 3),
                      requests=self._requests - 1, worker=self.worker.describe().get("kind"))

    def emit(self, kind: str, **data) -> None:
        if self.on_event is not None:
            self.on_event(kind, data)

    # -------------------------------------------------------------- tools
    def call_tool(self, tool: dict, args: dict):
        from .tools import ToolResult
        from .worker import Broker
        broker = Broker(self.surface, read_only=False)
        session = self.session()
        value = session.request({"op": "call_tool", "entry": tool["entrypoint"], "args": args,
                                 "view": self.state}, broker=broker)
        if not isinstance(value, dict) or "output" not in value:
            from .worker import PackageCallError
            raise PackageCallError(f"tool {tool['name']} answered {type(value).__name__}, not a tool result")
        self.emit("package_tool", tool=tool["name"], broker_calls=broker.calls)
        # Clipped here too, whatever the worker sent: a package tool's output has the same bound as a base tool's.
        output = str(value["output"])[:session.limits.max_output_chars]
        return ToolResult(output, bool(value.get("is_error")), meta={"broker_calls": broker.calls})

    # -------------------------------------------------------------- hooks
    def hooks(self, site: str) -> list[dict]:
        return [h for h in self.package.manifest["hooks"] if h["site"] == site and h.get("enabled", True)]

    def run_hooks(self, site: str, event: dict) -> tuple[str, list[str]]:
        """-> (action, messages). The hooks at `site` in manifest order; the first block or feedback of a
        before_tool/on_finish hook ends the chain. An answer outside the site's protocol is the package's defect,
        never read as continue (docs/modules/01_POLICY_RUNTIME.md §1)."""
        from .worker import Broker, PackageCallError
        messages: list[str] = []
        for hook in self.hooks(site):
            broker = Broker(self.surface, read_only=True)
            value = self.session().request({"op": "hook", "entry": hook["entrypoint"], "event": event,
                                            "view": self.state}, broker=broker)
            if not isinstance(value, dict) or value.get("action") not in HOOK_ACTIONS[site] or \
                    sorted(set(value) - {"action", "message"}):
                raise PackageCallError(f"hook {hook['name']} at {site} answered {str(value)[:120]}: an action "
                                       f"of {list(HOOK_ACTIONS[site])} with an optional message")
            action, message = value["action"], str(value.get("message") or "")[:2000]
            self.emit("hook", hook=hook["name"], site=site, action=action, message=message[:300],
                      broker_calls=broker.calls)
            if action == "continue":
                continue
            messages.append(message or f"{hook['name']} asks you to check before going on")
            if site != "after_tool":
                return action, messages
        return ("feedback" if messages else "continue"), messages


class PackageTools:
    """The episode's tool surface plus what the mounted package adds: the host's `read_skill` when the package
    lists skills, and the package's own tools (run in the worker) when it has any.

    read_skill is a host read of the package's own text: it never reaches the business world, is not a business
    tool call, and cannot read anything but an enabled skill of this package. Every read is reported through
    `on_read`, so the run records which skill text the actor actually saw (docs/modules/01_POLICY_RUNTIME.md §1).
    """

    SPEC_NAME = "read_skill"

    def __init__(self, surface, view: PackageView, on_read=None, runtime: PackageRuntime | None = None):
        self.surface = surface
        self.view = view
        self.on_read = on_read
        self.runtime = runtime
        self.reads: list[str] = []
        self.tools = {t["name"]: t for t in view.manifest["tools"]}
        taken = sorted(set(self.tools) & ({s.name for s in surface.specs()} | {self.SPEC_NAME}))
        if taken:
            # A package tool that shadows a base tool would replace it silently, or send the provider two tools
            # of one name: the candidate's defect, refused before the episode starts (M1 §1).
            from .worker import PackageCallError
            raise PackageCallError(f"package tools {taken} take the names of this host's tools")

    @property
    def hooks(self) -> PackageRuntime | None:
        return self.runtime if self.runtime is not None and self.view.manifest["hooks"] else None

    def end_session(self) -> None:
        if self.runtime is not None:
            self.runtime.end()

    def specs(self):
        from ..chat import ToolSpec
        specs = [*self.surface.specs()]
        for tool in self.tools.values():
            specs.append(ToolSpec(tool["name"], tool["description"], tool["input_schema"]))
        if self.view.skills:
            names = [s["name"] for s in self.view.skills]
            specs.append(ToolSpec(self.SPEC_NAME, "Read one of the skills listed in your instructions. Returns its "
                                  "text.", {"type": "object", "properties": {"name": {"type": "string", "enum": names}},
                                            "required": ["name"], "additionalProperties": False}))
        return specs

    def run(self, name: str, args: dict):
        from .tools import ToolResult
        if name in self.tools and self.runtime is not None:
            return self.runtime.call_tool(self.tools[name], args or {})
        if name != self.SPEC_NAME or not self.view.skills:
            return self.surface.run(name, args)
        skill = str((args or {}).get("name", ""))
        text = self.view.skill_text(skill)
        self.reads.append(skill)
        if self.on_read is not None:
            self.on_read(skill, text is not None)
        if text is None:
            return ToolResult(f"no skill named {skill!r}; the skills are "
                              f"{[s['name'] for s in self.view.skills]}", True)
        return ToolResult(text)

    def mutates(self, name: str, args: dict) -> bool:
        if name == self.SPEC_NAME:
            return False
        if name in self.tools:
            return True            # a composite tool may write through the broker: counted as a change
        return self.surface.mutates(name, args)

    def __getattr__(self, attr):
        return getattr(self.surface, attr)


def usable_worker(worker) -> bool:
    """A worker package code may run in: an isolating one, or a protocol-test worker a test named as such."""
    return worker is not None and (bool(getattr(worker, "qualified", False)) or bool(getattr(worker, "for_tests",
                                                                                              False)))


def mount(surface, bound, *, worker=None, on_read=None, on_event=None):
    """The tool surface an agent gets for a bound release. A package that runs code needs the qualified worker;
    without one the assembly is refused before any task tool runs (never an in-process fallback)."""
    view = getattr(bound, "package", None) if bound is not None else None
    if view is None:
        return surface
    if view.runs_code and not usable_worker(worker):
        raise CapabilityError(f"package {ct.short(view.ref)} runs code and no qualified isolated worker is "
                              f"available")
    if not (view.skills or view.runs_code):
        return surface
    runtime = PackageRuntime(surface, view, worker, on_event=on_event) if view.runs_code else None
    return PackageTools(surface, view, on_read=on_read, runtime=runtime)
