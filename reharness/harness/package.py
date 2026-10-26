"""The harness package (docs/design/HARNESS.md §3, §4, §6): what W is, its manifest, its limits, and its checks.

A package is a directory of fixed shape (kinds.PATTERNS) with one `manifest.json` (api_version harness_package.v3)
that lists the components by kind. It is stored content-addressed: every file is a `file` object and the package
object maps each path to its file's ref beside the parsed manifest, so the package ref covers every byte and two
packages share the objects of the files they share (the seed's vendored Stirrup is stored once).

Nothing here imports or runs package code: the control plane never does (CONTRACTS §1). The host composes the system
prompt from these files (runtime.runner); the worker runs the code.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..contracts import ContractError, Objects, Record, check_refs, parse_ref, ref_for, register
from .contract import API_VERSION, EFFORTS, ROLES, SITES
from .kinds import CODE_KINDS, HOST_KINDS, KIND_TABLE, KINDS, MEMORY_PATH, PATTERNS

HOST_TOOLS = ("read_skill", "recall")        # tools the adapter adds; a package tool may not take their names
TOOL_NAME_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
JSON_TYPES = ("string", "number", "integer", "boolean", "object", "array", "null")
DEFAULT_CONFIG = {"max_turns": 50, "summarize_at": 0.7, "shown_k": 2, "subagent_depth": 1}
DEFAULT_CONFIG_RANGES = {"max_turns": (10, 80), "summarize_at": (0.5, 0.9), "shown_k": (0, 5), "subagent_depth": (0, 2)}
MANIFEST_KEYS = ("api_version", "agent", "vendor", "tools", "hooks", "prompt", "skills", "memory", "model", "config",
                 "tests")
PROTECTED_VENDOR_FILES = ("LICENSE", "PROVENANCE.json")


class PackageError(ContractError):
    """A package that breaks the fixed shape. Returned to R as check output; never run."""


# ---------------------------------------------------------------------------- limits
@dataclass(frozen=True)
class Limits:
    """K's harness limits (K `harness.limits`, with `kinds`, `sites`, `config_ranges` and the actor's identity and
    effort cap from K). R cannot raise any of them. The byte limits are from evidence: the vendored Stirrup is about
    490 KiB, and the worker peaked at 80 MB (V0_6 §8)."""
    max_files: int = 96
    max_package_bytes: int = 786432
    max_vendor_bytes: int = 655360
    max_memory_bytes: int = 262144
    max_prompt_chars: int = 8000
    max_skill_chars: int = 8000
    max_skills: int = 32
    max_tools: int = 16
    max_hooks: int = 12
    max_tool_description: int = 1024
    max_schema_depth: int = 16
    max_tests: int = 24
    kinds: tuple[str, ...] = KINDS
    sites: tuple[str, ...] = SITES
    config_ranges: dict = field(default_factory=lambda: dict(DEFAULT_CONFIG_RANGES))
    model_base: str | None = None              # K's model.identity; a manifest's model.base must equal it
    max_effort: str = "medium"                 # K's model.max_effort

    def __post_init__(self) -> None:
        unknown = sorted(set(self.kinds) - set(KINDS))
        if unknown:
            raise PackageError(f"unknown component kinds {unknown}: {list(KINDS)}")
        if sorted(set(self.sites) - set(SITES)):
            raise PackageError(f"unknown hook sites {sorted(set(self.sites) - set(SITES))}: {list(SITES)}")
        if self.max_effort not in EFFORTS:
            raise PackageError(f"max_effort {self.max_effort!r}: one of {list(EFFORTS)}")
        object.__setattr__(self, "kinds", tuple(self.kinds))
        object.__setattr__(self, "sites", tuple(self.sites))
        object.__setattr__(self, "config_ranges", {k: tuple(v) for k, v in dict(self.config_ranges).items()})

    def to_json(self) -> dict:
        out = {k: getattr(self, k) for k in self.__dataclass_fields__}
        out["kinds"], out["sites"] = list(self.kinds), list(self.sites)
        out["config_ranges"] = {k: list(v) for k, v in self.config_ranges.items()}
        return out

    @classmethod
    def from_json(cls, d: dict | None) -> "Limits":
        d = dict(d or {})
        unknown = sorted(set(d) - set(cls.__dataclass_fields__))
        if unknown:
            raise PackageError(f"harness limits do not take {unknown}")
        return cls(**d)

    @classmethod
    def from_core(cls, core: dict) -> "Limits":
        """K's harness section and the actor's identity and effort cap."""
        harness, model = core.get("harness") or {}, core.get("model") or {}
        d = dict(harness.get("limits") or {})
        for key in ("kinds", "sites", "config_ranges"):
            if key in harness:
                d[key] = harness[key]
        d["model_base"] = model.get("identity")
        d["max_effort"] = model.get("max_effort") or model.get("effort") or "medium"
        return cls.from_json(d)


# ---------------------------------------------------------------------------- manifest
@dataclass(frozen=True)
class Manifest:
    api_version: str = API_VERSION
    agent: dict = field(default_factory=dict)
    vendor: tuple = ()
    tools: tuple = ()
    hooks: tuple = ()
    prompt: dict = field(default_factory=dict)
    skills: tuple = ()
    memory: dict = field(default_factory=lambda: {"enabled": False})
    model: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    tests: tuple = ()

    def to_json(self) -> dict:
        return {"api_version": self.api_version, "agent": dict(self.agent), "vendor": [dict(v) for v in self.vendor],
                "tools": [dict(t) for t in self.tools], "hooks": [dict(h) for h in self.hooks],
                "prompt": dict(self.prompt), "skills": [dict(s) for s in self.skills], "memory": dict(self.memory),
                "model": dict(self.model), "config": dict(self.config), "tests": list(self.tests)}

    @classmethod
    def from_json(cls, d: dict) -> "Manifest":
        return cls(api_version=d["api_version"], agent=dict(d.get("agent") or {}),
                   vendor=tuple(d.get("vendor") or ()), tools=tuple(d.get("tools") or ()),
                   hooks=tuple(d.get("hooks") or ()), prompt=dict(d.get("prompt") or {}),
                   skills=tuple(d.get("skills") or ()), memory=dict(d.get("memory") or {"enabled": False}),
                   model=dict(d.get("model") or {}), config=dict(d.get("config") or {}),
                   tests=tuple(d.get("tests") or ()))

    def text(self) -> str:
        return json.dumps(self.to_json(), indent=1, sort_keys=False) + "\n"


def classify(path: str) -> str:
    """The kind of a package path ("manifest" for manifest.json), or PackageError. Relative POSIX paths only."""
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        raise PackageError(f"{path!r}: package paths are relative POSIX paths")
    parts = PurePosixPath(path).parts
    if any(p in ("", ".", "..") for p in parts) or str(PurePosixPath(path)) != path:
        raise PackageError(f"{path!r}: no '.', '..' or empty segments")
    for kind, pattern in PATTERNS:
        if pattern.match(path):
            return kind
    raise PackageError(f"{path!r}: not a package file (manifest.json, agent/*.py, vendor/<name>/**, tools/*.py, "
                       f"middlewares/*.py, prompt/system.md, prompt/finish.md, skills/**.md, {MEMORY_PATH}, "
                       f"tests/*.py)")


def parse_manifest(text: str, files: dict[str, str], *, limits: Limits) -> Manifest:
    """The manifest, checked against the files it names and K's limits. Raises PackageError."""
    try:
        m = json.loads(text)
    except (json.JSONDecodeError, RecursionError, TypeError) as e:
        raise PackageError(f"manifest.json is not JSON: {str(e)[:200]}") from None
    if not isinstance(m, dict):
        raise PackageError("manifest.json is an object")
    unknown = sorted(set(m) - set(MANIFEST_KEYS))
    if unknown:
        raise PackageError(f"manifest.json does not take {unknown}")
    if m.get("api_version") != API_VERSION:
        raise PackageError(f"manifest api_version {m.get('api_version')!r}: this host serves {API_VERSION} only; a "
                           f"v1 or v2 package runs at its tag (v0.4.0, archive-2026-10-08)")
    names: set[str] = set()
    return Manifest(api_version=API_VERSION, agent=_agent(m.get("agent"), files),
                    vendor=_vendor(m.get("vendor"), files), tools=_tools(m.get("tools"), files, limits, names),
                    hooks=_hooks(m.get("hooks"), files, limits, names), prompt=_prompt(m.get("prompt"), files),
                    skills=_skills(m.get("skills"), files, names), memory=_memory(m.get("memory"), files),
                    model=_model(m.get("model"), limits), config=_config(m.get("config"), limits),
                    tests=_tests(m.get("tests"), files))


def _list(value, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PackageError(f"manifest {what} is a list")
    return value


def _entry(entry, required: set, what: str, optional: set = frozenset()) -> dict:
    if not isinstance(entry, dict):
        raise PackageError(f"a {what} entry is an object with {sorted(required)}")
    missing, extra = sorted(required - set(entry)), sorted(set(entry) - required - set(optional))
    if missing or extra:
        raise PackageError(f"a {what} entry needs {sorted(required)}"
                           + (f" and may have {sorted(optional)}" if optional else "")
                           + f"; missing {missing}, unknown {extra}")
    return entry


def _unique(name, seen: set, what: str) -> None:
    if not isinstance(name, str) or not name:
        raise PackageError(f"a {what} needs a name")
    if name in seen:
        raise PackageError(f"{what} name {name!r} is used twice")
    seen.add(name)


def _entrypoint(spec, prefix: str, files: dict) -> str:
    module, _, func = str(spec).partition(":")
    if not isinstance(spec, str) or not module.startswith(prefix) or module not in files or not func.isidentifier():
        raise PackageError(f"entrypoint {spec!r}: <{prefix}file.py>:<function> inside the package")
    return spec


def _bool(entry: dict, key: str, default: bool, what: str) -> bool:
    value = entry.get(key, default)
    if not isinstance(value, bool):
        raise PackageError(f"a {what} entry's {key} is true or false")
    return value


def _agent(entry, files) -> dict:
    if entry is None:
        raise PackageError('manifest has no agent entry: {"entrypoint": "agent/<file>.py:run", "roles": [...]}')
    _entry(entry, {"entrypoint", "roles"}, "agent")
    spec = _entrypoint(entry["entrypoint"], "agent/", files)
    if spec.partition(":")[2] != "run":
        raise PackageError(f"agent entrypoint {spec!r}: the agent's callable is named run")
    roles = entry["roles"]
    if not isinstance(roles, list) or not roles or not all(isinstance(r, str) for r in roles) \
            or sorted(set(roles) - set(ROLES)) or len(set(roles)) != len(roles):
        raise PackageError(f"agent roles {roles!r}: a non-empty list of distinct roles of {list(ROLES)}")
    return {"entrypoint": spec, "roles": [r for r in ROLES if r in roles]}


def _vendor(value, files) -> tuple:
    out, declared = [], set()
    for entry in _list(value, "vendor"):
        _entry(entry, {"name", "provenance"}, "vendor")
        name = entry["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]+", name):
            raise PackageError(f"vendor name {name!r}: lowercase letters, digits and '_'")
        if name in declared:
            raise PackageError(f"vendor {name!r} is declared twice")
        declared.add(name)
        if entry["provenance"] != f"vendor/{name}/PROVENANCE.json" or entry["provenance"] not in files:
            raise PackageError(f"vendor {name}: provenance is vendor/{name}/PROVENANCE.json and is in the package")
        out.append({"name": name, "provenance": entry["provenance"]})
    present = {p.split("/")[1] for p in files if p.startswith("vendor/")}
    if present - declared:
        raise PackageError(f"vendored packages {sorted(present - declared)} are not declared in the manifest")
    return tuple(out)


def _tools(value, files, limits: Limits, names: set) -> tuple:
    out = []
    for entry in _list(value, "tools"):
        _entry(entry, {"name", "description", "input_schema", "entrypoint"}, "tool", {"writes", "enabled"})
        name = entry["name"]
        _unique(name, names, "tool")
        if name in HOST_TOOLS:
            raise PackageError(f"tool {name}: the name is the host's own tool")
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or set(name) - set(TOOL_NAME_CHARS):
            raise PackageError(f"tool name {name!r}: letters, digits, '_' and '-', at most 64 characters")
        if not isinstance(entry["description"], str) or len(entry["description"]) > limits.max_tool_description:
            raise PackageError(f"tool {name}: a description of at most {limits.max_tool_description} characters")
        _input_schema(entry["input_schema"], f"tool {name}", limits.max_schema_depth)
        out.append({"name": name, "description": entry["description"], "input_schema": entry["input_schema"],
                    "entrypoint": _entrypoint(entry["entrypoint"], "tools/", files),
                    "writes": _bool(entry, "writes", False, "tool"), "enabled": _bool(entry, "enabled", True, "tool")})
    return tuple(out)


def _hooks(value, files, limits: Limits, names: set) -> tuple:
    out = []
    for entry in _list(value, "hooks"):
        _entry(entry, {"name", "site", "entrypoint"}, "hook", {"enabled"})
        _unique(entry["name"], names, "hook")
        if entry["site"] not in limits.sites:
            raise PackageError(f"hook {entry['name']}: site {entry['site']!r} is one of {list(limits.sites)}")
        out.append({"name": entry["name"], "site": entry["site"],
                    "entrypoint": _entrypoint(entry["entrypoint"], "middlewares/", files),
                    "enabled": _bool(entry, "enabled", True, "hook")})
    return tuple(out)


def _prompt(value, files) -> dict:
    value = value or {}
    if not isinstance(value, dict) or sorted(set(value) - {"system", "finish"}):
        raise PackageError('manifest prompt is {"system": "prompt/system.md", "finish": "prompt/finish.md"}')
    for key, path in value.items():
        if path != f"prompt/{key}.md" or path not in files:
            raise PackageError(f"prompt {key}: prompt/{key}.md, in the package")
    stray = sorted(p for p in files if classify(p) == "prompt" and p not in value.values())
    if stray:
        raise PackageError(f"prompt files not named in the manifest: {stray}")
    return dict(value)


def _skills(value, files, names: set) -> tuple:
    out = []
    for entry in _list(value, "skills"):
        _entry(entry, {"name", "path"}, "skill", {"description", "enabled"})
        name = entry["name"]
        if not isinstance(name, str) or not name or len(name) > 64 or set(name) - set("abcdefghijklmnopqrstuvwxyz0123456789_-") \
                or not name[0].isalnum():
            raise PackageError(f"skill name {name!r}: lowercase letters, digits, '-' and '_'")
        _unique(name, names, "skill")
        if entry["path"] not in files or classify(entry["path"]) != "skill":
            raise PackageError(f"skill {name}: {entry['path']!r} is not a skills/*.md file of the package")
        out.append({"name": name, "path": entry["path"], "description": str(entry.get("description", ""))[:300],
                    "enabled": _bool(entry, "enabled", True, "skill")})
    listed = {s["path"] for s in out}
    stray = sorted(p for p in files if classify(p) == "skill" and p not in listed)
    if stray:
        raise PackageError(f"skill files not listed in the manifest: {stray}")
    return tuple(out)


def _memory(value, files) -> dict:
    value = {"enabled": False} if value is None else value
    if not isinstance(value, dict) or sorted(set(value) - {"enabled", "snapshot"}) \
            or not isinstance(value.get("enabled", False), bool):
        raise PackageError(f'manifest memory is {{"enabled": bool, "snapshot": "{MEMORY_PATH}"}}')
    snapshot = value.get("snapshot")
    if snapshot is not None and snapshot != MEMORY_PATH:
        raise PackageError(f"memory snapshot is {MEMORY_PATH}")
    if (snapshot is not None) != (MEMORY_PATH in files):
        raise PackageError(f"memory.snapshot names {MEMORY_PATH} exactly when the package holds it")
    return {"enabled": bool(value.get("enabled")), **({"snapshot": snapshot} if snapshot else {})}


def _model(value, limits: Limits) -> dict:
    if not isinstance(value, dict):
        raise PackageError('manifest model is {"base", "effort", "max_tokens", "artifact_ref"}')
    _entry(value, {"base", "effort"}, "model", {"max_tokens", "artifact_ref"})
    if not isinstance(value["base"], str) or not value["base"]:
        raise PackageError("model.base names the actor model")
    if limits.model_base is not None and value["base"] != limits.model_base:
        raise PackageError(f"model.base {value['base']!r} is not K's model {limits.model_base!r}")
    if value["effort"] not in EFFORTS or EFFORTS.index(value["effort"]) > EFFORTS.index(limits.max_effort):
        raise PackageError(f"model.effort {value['effort']!r}: at most K's {limits.max_effort!r}")
    out = {"base": value["base"], "effort": value["effort"]}
    if value.get("max_tokens") is not None:
        if isinstance(value["max_tokens"], bool) or not isinstance(value["max_tokens"], int) or value["max_tokens"] <= 0:
            raise PackageError("model.max_tokens is a positive integer")
        out["max_tokens"] = value["max_tokens"]
    artifact = value.get("artifact_ref")
    if artifact is not None:
        try:
            check_refs([artifact], kind="train_artifact", what="model.artifact_ref")
        except ContractError as e:
            raise PackageError(str(e)) from None
    out["artifact_ref"] = artifact
    return out


def _config(value, limits: Limits) -> dict:
    value = value or {}
    if not isinstance(value, dict):
        raise PackageError("manifest config is an object")
    out = {}
    for key, v in value.items():
        if key not in limits.config_ranges:
            raise PackageError(f"config {key!r}: K allows {sorted(limits.config_ranges)}")
        lo, hi = limits.config_ranges[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise PackageError(f"config {key} = {v!r}: K's range is [{lo}, {hi}]")
        if isinstance(lo, int) and isinstance(hi, int) and not isinstance(v, int):
            raise PackageError(f"config {key} = {v!r}: an integer")
        out[key] = v
    return out


def _tests(value, files) -> tuple:
    listed = []
    for path in _list(value, "tests"):
        if not isinstance(path, str) or path not in files or classify(path) != "test":
            raise PackageError(f"test {path!r} is not a tests/*.py file of the package")
        if path in listed:
            raise PackageError(f"test {path} is listed twice")
        listed.append(path)
    stray = sorted(p for p in files if classify(p) == "test" and p not in listed)
    if stray:
        raise PackageError(f"test files not listed in the manifest: {stray}")
    return tuple(listed)


def _input_schema(schema, what: str, max_depth: int) -> None:
    """A tool's input schema in the shape the model providers take: an object at the top with no anyOf/oneOf/allOf,
    JSON types only, properties that are schemas, a list of required names, items for every array."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise PackageError(f"{what}: input_schema is a JSON object schema")
    if set(schema) & {"anyOf", "oneOf", "allOf"}:
        raise PackageError(f"{what}: input_schema has anyOf/oneOf/allOf at the top; providers take an object")

    def walk(s, at: str, depth: int) -> None:
        if not isinstance(s, dict):
            raise PackageError(f"{what}: {at} is a schema object")
        if depth > max_depth:
            raise PackageError(f"{what}: input_schema nests deeper than {max_depth}")
        kind = s.get("type")
        kinds = kind if isinstance(kind, list) else [kind] if kind is not None else []
        if (isinstance(kind, list) and not kind) or any(k not in JSON_TYPES for k in kinds):
            raise PackageError(f"{what}: {at} type {kind!r} is one of {list(JSON_TYPES)} or a list of them")
        if "array" in kinds and "items" not in s:
            raise PackageError(f"{what}: {at} is an array and gives no items")
        if "properties" in s and not isinstance(s["properties"], dict):
            raise PackageError(f"{what}: {at}.properties is an object of schemas")
        if "required" in s and not (isinstance(s["required"], list) and all(isinstance(r, str) for r in s["required"])):
            raise PackageError(f"{what}: {at}.required is a list of property names")
        for name, sub in (s.get("properties") or {}).items():
            walk(sub, f"{at}.properties.{name}", depth + 1)
        if "items" in s:
            walk(s["items"], f"{at}.items", depth + 1)
        for key in ("anyOf", "oneOf", "allOf"):
            for i, sub in enumerate(s.get(key) or []):
                walk(sub, f"{at}.{key}[{i}]", depth + 1)
        if isinstance(s.get("additionalProperties"), dict):
            walk(s["additionalProperties"], f"{at}.additionalProperties", depth + 1)
    walk(schema, "input_schema", 1)


# ---------------------------------------------------------------------------- the package record
def file_ref(text: str) -> str:
    return ref_for("file", {"text": text})


@register
@dataclass(frozen=True)
class Package(Record):
    """A stored harness package: its parsed manifest and every file's text. The stored payload names each file by
    its `file` object's ref, so the package ref covers all bytes while files are stored once."""
    TYPE = "package"
    manifest: Manifest = field(default_factory=Manifest)
    files: dict = field(default_factory=dict)

    def payload(self) -> dict:
        return {"manifest": self.manifest.to_json(), "files": {p: file_ref(self.files[p]) for p in sorted(self.files)}}

    def to_json(self) -> dict:
        return {**self.payload(), "ref": self.ref()}

    @classmethod
    def from_json(cls, d: dict):
        raise ContractError("a package is read with harness.package.load(objects, ref): its files are objects")

    @property
    def kinds(self) -> set[str]:
        return kinds_of(self.manifest, self.files)

    def component(self, kind: str) -> list[dict]:
        """The manifest entries of one kind (a one-entry list for the dict-shaped ones)."""
        value = getattr(self.manifest, KIND_TABLE[kind].manifest_key)
        if isinstance(value, dict):
            return [dict(value)] if value else []
        return [v if isinstance(v, dict) else {"path": v} for v in value]

    @property
    def memory_text(self) -> str:
        return self.files.get(MEMORY_PATH, "")


def kinds_of(manifest: Manifest, files: dict[str, str]) -> set[str]:
    """The kinds a complete package uses: those of its files, plus config when it sets any knob, and model when it
    binds a trained artifact (every package names its model; only a binding is a change of the model kind)."""
    kinds = {classify(p) for p in files} - {"manifest"}
    if manifest.tools:
        kinds.add("tool")
    if manifest.hooks:
        kinds.add("hook")
    if manifest.config:
        kinds.add("config")
    if manifest.model.get("artifact_ref"):
        kinds.add("model")
    if manifest.memory.get("enabled"):
        kinds.add("memory")
    return kinds


def _text_ok(path: str, text) -> None:
    if not isinstance(text, str):
        raise PackageError(f"{path}: file contents are text")
    if "\x00" in text:
        raise PackageError(f"{path}: a NUL character; package files are UTF-8 text")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise PackageError(f"{path}: not UTF-8 text (it holds a lone surrogate)") from None


def protected(seed: "Package") -> dict[str, str]:
    """{vendor/<name>/LICENSE | PROVENANCE.json: the seed's text}: what no candidate may change."""
    return {p: t for p, t in seed.files.items()
            if p.startswith("vendor/") and p.count("/") == 2 and p.rsplit("/", 1)[1] in PROTECTED_VENDOR_FILES}


def check_shape(files: dict[str, str], *, limits: Limits, seed: "Package | None",
                parent: "Package | None" = None) -> list[str]:
    """Every structural rule on a complete package. -> the problems ([] when it passes).

    `seed` fixes the vendored packages' LICENSE and PROVENANCE.json; `parent`, when given, fixes the host-only
    memory snapshot (a candidate may not change it; the memory package builds without a parent)."""
    if not isinstance(files, dict) or not files:
        return ["a package is a non-empty mapping of path to text"]
    problems: list[str] = []
    try:
        for path, text in files.items():
            classify(path)
            _text_ok(path, text)
    except PackageError as e:
        return [str(e)]
    if "manifest.json" not in files:
        return ["the package has no manifest.json"]
    try:
        manifest = parse_manifest(files["manifest.json"], files, limits=limits)
    except PackageError as e:
        return [str(e)]
    body = {p: t for p, t in files.items() if classify(p) not in HOST_KINDS}
    size = sum(len(t.encode("utf-8")) for t in body.values())
    vendor = sum(len(t.encode("utf-8")) for p, t in body.items() if p.startswith("vendor/"))
    memory = len(files.get(MEMORY_PATH, "").encode("utf-8"))
    for n, cap, what in ((len(body), limits.max_files, "files"), (size, limits.max_package_bytes, "bytes"),
                         (vendor, limits.max_vendor_bytes, "vendored bytes"),
                         (memory, limits.max_memory_bytes, f"bytes in {MEMORY_PATH}"),
                         (len(manifest.tools), limits.max_tools, "tools"),
                         (len(manifest.hooks), limits.max_hooks, "hooks"),
                         (len(manifest.skills), limits.max_skills, "skills"),
                         (len(manifest.tests), limits.max_tests, "tests")):
        if n > cap:
            problems.append(f"{n} {what}: K allows {cap}")
    for path in manifest.prompt.values():
        if len(files[path]) > limits.max_prompt_chars:
            problems.append(f"{path} has {len(files[path])} characters: K allows {limits.max_prompt_chars}")
    for skill in manifest.skills:
        if len(files[skill["path"]]) > limits.max_skill_chars:
            problems.append(f"{skill['path']} has {len(files[skill['path']])} characters: K allows "
                            f"{limits.max_skill_chars}")
    outside = sorted(kinds_of(manifest, files) - set(limits.kinds))
    if outside:
        problems.append(f"component kinds {outside} are not allowed by K: {list(limits.kinds)}")
    if seed is not None:
        for path, text in protected(seed).items():
            prefix = path.rsplit("/", 1)[0] + "/"
            if any(p.startswith(prefix) for p in files) and files.get(path) != text:
                problems.append(f"{path} differs from the seed's: the license and provenance of a vendored package "
                                f"are fixed byte for byte; edit the vendored code, not these files")
    if parent is not None and files.get(MEMORY_PATH) != parent.files.get(MEMORY_PATH):
        problems.append(f"{MEMORY_PATH} is written by the memory package from train evidence; a candidate may not "
                        f"change it")
    return problems


def build(objects: Objects, files: dict[str, str], *, limits: Limits, seed: "Package | None",
          parent: "Package | None" = None) -> Package:
    """A complete package, checked and stored. Raises PackageError with every problem; nothing is stored then."""
    problems = check_shape(files, limits=limits, seed=seed, parent=parent)
    if problems:
        raise PackageError("; ".join(problems))
    manifest = parse_manifest(files["manifest.json"], files, limits=limits)
    package = Package(manifest=manifest, files=dict(sorted(files.items())))
    for path in sorted(files):
        objects.put_json("file", {"text": files[path]})
    ref = objects.put_json("package", package.payload())
    assert ref == package.ref()
    return package


def load(objects: Objects, package_ref: str) -> Package:
    kind, _ = parse_ref(package_ref)
    if kind != "package":
        raise ContractError(f"{package_ref} is a {kind}, not a package")
    payload = objects.get(package_ref)
    if set(payload) != {"manifest", "files"}:
        raise ContractError(f"{package_ref}: not a harness_package.v3 package (an older package runs at its tag)")
    files = {p: objects.get(r)["text"] for p, r in payload["files"].items()}
    package = Package(manifest=Manifest.from_json(payload["manifest"]), files=files)
    if package.ref() != package_ref:
        raise ContractError(f"{package_ref} does not hash to its files")
    return package


def files_of(objects: Objects, package_ref: str) -> dict[str, str]:
    return dict(load(objects, package_ref).files)


def run_files(package: Package) -> dict[str, str]:
    """What the worker loads: everything but tests/ and the host-only memory snapshot."""
    return {p: t for p, t in package.files.items() if classify(p) not in ("test", *HOST_KINDS)}


def needs_worker(package: Package) -> bool:
    """Always: every v3 package carries its agent, which runs only in the worker."""
    return True


def apply_patch(parent: Package, patch: dict) -> dict[str, str]:
    """The parent's files with `patch` ({path: text, or None to delete}) applied. A patch that touches the host-only
    memory snapshot, deletes a missing file or changes nothing is refused."""
    if not isinstance(patch, dict) or not patch:
        raise PackageError("a package patch is a non-empty {path: text | None}")
    files = dict(parent.files)
    for path, text in patch.items():
        if classify(path) in HOST_KINDS:
            raise PackageError(f"{path}: written by the memory package from train evidence, never by a candidate")
        if text is None:
            if path not in files:
                raise PackageError(f"delete {path!r}: the parent package has no such file")
            del files[path]
        elif isinstance(text, str):
            files[path] = text
        else:
            raise PackageError(f"{path}: a patch maps a path to text or None")
    if files == parent.files:
        raise PackageError("the patch leaves the package exactly as it was")
    return files


@dataclass(frozen=True)
class Diff:
    changed: dict                       # path -> "added" | "modified" | "deleted"
    kinds: frozenset
    sites: frozenset
    vendor_touched: bool


SECTION_KIND = {"agent": "agent", "vendor": "vendor", "tools": "tool", "hooks": "hook", "prompt": "prompt",
                "skills": "skill", "memory": "memory", "model": "model", "config": "config", "tests": "test"}


def diff(parent: Package, files: dict[str, str]) -> Diff:
    """What a candidate's files change against its parent: paths, the kinds those changes belong to (file kinds and
    changed manifest sections), the hook sites whose hooks or hook code changed, and whether vendored code moved."""
    changed = {}
    for path in sorted(set(parent.files) | set(files)):
        if path not in files:
            changed[path] = "deleted"
        elif path not in parent.files:
            changed[path] = "added"
        elif files[path] != parent.files[path]:
            changed[path] = "modified"
    kinds = {classify(p) for p in changed} - {"manifest"}
    new_manifest = None
    if "manifest.json" in changed and "manifest.json" in files:
        try:
            new_manifest = json.loads(files["manifest.json"])
        except json.JSONDecodeError:
            new_manifest = None
        old = parent.manifest.to_json()
        for key, kind in SECTION_KIND.items():
            if new_manifest is None or json.dumps(old.get(key), sort_keys=True) != \
                    json.dumps(_normal(key, new_manifest.get(key)), sort_keys=True):
                kinds.add(kind)
    hooks = list(parent.manifest.hooks) + list((new_manifest or {}).get("hooks") or [])
    old_hooks = {json.dumps(h, sort_keys=True) for h in parent.manifest.hooks}
    sites = set()
    for h in hooks:
        if not isinstance(h, dict):
            continue
        module = str(h.get("entrypoint", "")).partition(":")[0]
        if module in changed or json.dumps(_normal_hook(h), sort_keys=True) not in old_hooks:
            if h.get("site") in SITES:
                sites.add(h["site"])
    if new_manifest is not None:
        new_hooks = {json.dumps(_normal_hook(h), sort_keys=True) for h in new_manifest.get("hooks") or []
                     if isinstance(h, dict)}
        sites |= {h["site"] for h in parent.manifest.hooks if json.dumps(h, sort_keys=True) not in new_hooks}
    return Diff(changed=changed, kinds=frozenset(kinds), sites=frozenset(sites),
                vendor_touched=any(p.startswith("vendor/") for p in changed))


def _normal_hook(h: dict) -> dict:
    return {"name": h.get("name"), "site": h.get("site"), "entrypoint": h.get("entrypoint"),
            "enabled": h.get("enabled", True)}


def _normal(key: str, value):
    """A raw manifest section as parse_manifest would store it, for comparison (defaults filled where cheap)."""
    if key == "hooks":
        return [_normal_hook(h) if isinstance(h, dict) else h for h in value or []]
    if key == "tools":
        return [{**t, "writes": t.get("writes", False), "enabled": t.get("enabled", True)} if isinstance(t, dict)
                else t for t in value or []]
    if key == "skills":
        return [{"name": s.get("name"), "path": s.get("path"), "description": str(s.get("description", ""))[:300],
                 "enabled": s.get("enabled", True)} if isinstance(s, dict) else s for s in value or []]
    if key in ("vendor", "tests"):
        return list(value or [])
    if key in ("prompt", "config"):
        return dict(value or {})
    if key == "memory":
        if not isinstance(value, dict):
            return value if value is not None else {"enabled": False}
        return {**{k: v for k, v in value.items() if not (k == "snapshot" and v is None)},
                "enabled": bool(value.get("enabled", False))}
    if key == "agent" and isinstance(value, dict) and isinstance(value.get("roles"), list):
        roles = value["roles"]
        return {**value, "roles": [r for r in ROLES if r in roles] + [r for r in roles if r not in ROLES]}
    if key == "model" and isinstance(value, dict):
        return {**value, "artifact_ref": value.get("artifact_ref")}
    return value


normal_section = _normal              # public: a raw manifest section as parse_manifest stores it


def code_kinds(kinds) -> set[str]:
    return set(kinds) & set(CODE_KINDS)
