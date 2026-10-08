"""M0/M1: the capability package — what a candidate may change in the harness, and what the host needs to run it
(docs/modules/00_CONTRACTS.md §2, docs/modules/01_POLICY_RUNTIME.md §1).

A package is a small directory of fixed shape: `manifest.json`, `skills/**.md`, `memory.md`, and — from the second
delivery stage — `tools/*.py`, `middlewares/*.py` and `tests/*.py`; from package API v2 (v0.3) also `agent/*.py`,
the agent program that is the loop itself. It is stored content-addressed: every file is an immutable object, the
run files form one bundle and the tests another, so what was checked, evaluated and exported are the same bytes.

The host reads text components itself: memory is projected into the system prompt, skills are listed there by name
and read through one fixed host tool. Generated code is never imported here. Whether a package needs the isolated
worker is decided on the complete package, inherited entries included, in two ways that are not the same question:
a check of a draft needs the worker whenever the run or test bundle holds Python; running a package needs it only
when the manifest names tools, hooks or an agent, or the run bundle holds Python (the test bundle is never mounted).

Two package API versions are served. A `harness_package.v1` manifest has no `agent` entry: it is run by the default
agent that ships with the core, so every v1 package is a v2 package without a loop of its own and needs no
migration (docs/modules/00_CONTRACTS.md §2, docs/DESIGN.md §5). A `harness_package.v2` manifest may carry one
`agent` entry, `agent/<file>.py:run` with the roles it calls the model as.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..evolve import contracts as ct

# Every package API this host serves, oldest first; API_VERSION is the newest. A manifest names the one it targets,
# and a core (K) may fix an older one as the ceiling its candidates write to (Limits.package_api).
API_VERSIONS = ("harness_package.v1", "harness_package.v2")
API_VERSION = API_VERSIONS[-1]
AGENT_API_VERSION = "harness_package.v2"        # the first version with an `agent` entry
HOOK_SITES = ("before_tool", "after_tool", "on_finish")
# The roles an agent calls the model as (M0 §2): `act` is a step of the loop, `aux` a call that produces no
# action (checking, voting, evidence). Written on every gateway record; declared here so freezing can audit them.
ROLES = ("act", "aux")
AGENT_ENTRY = "run"                              # the one callable an agent entrypoint may name
TEXT_KINDS = ("memory", "skills")
CODE_KINDS = ("on_finish", "before_tool", "after_tool", "tools", "agent")
KINDS = (*TEXT_KINDS, *CODE_KINDS)
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")        # matched with fullmatch: "$" lets a trailing newline in
# What every provider this host speaks takes as a tool name (OpenAI-compatible endpoints, GLM among them, and
# Anthropic): any other name is a 400 on every call, which a run can only book as infrastructure (M1 §1).
TOOL_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}")
JSON_TYPES = ("string", "number", "integer", "boolean", "object", "array", "null")
MAX_TOOL_DESCRIPTION = 1024
MAX_SCHEMA_DEPTH = 16
PATTERNS = (
    ("manifest", re.compile(r"^manifest\.json$")),
    ("memory", re.compile(r"^memory\.md$")),
    ("skill", re.compile(r"^skills/[a-z0-9][a-z0-9_/-]*\.md$")),
    ("tool", re.compile(r"^tools/[a-z0-9_]+\.py$")),
    ("middleware", re.compile(r"^middlewares/[a-z0-9_]+\.py$")),
    ("test", re.compile(r"^tests/[a-z0-9_]+\.py$")),
    ("agent", re.compile(r"^agent/[a-z0-9_]+\.py$")),
)


class PackageError(ct.ContractError):
    """A package that breaks the fixed shape: a structure error, returned to R as check output."""


@dataclass(frozen=True)
class Limits:
    """What K fixes about packages. R cannot raise any of these (docs/modules/04_RESEARCH_LOOP.md §2).

    `package_api` is the newest package API a candidate's manifest may target: a core frozen to v1 takes no v2
    manifest, whatever it contains, so the API a release's candidates were written to is a fact of K and not of
    the host that happens to run it. Unnamed, it is the oldest API that has every component the core allows: v1,
    or v2 once the `agent` kind is allowed — so a core recorded before the field existed keeps taking exactly what
    it took, and naming v1 while allowing agents is refused rather than allowing a kind no manifest of that API can
    declare.
    """
    max_files: int = 24
    max_package_bytes: int = 262144
    max_memory_chars: int = 8000
    max_skill_chars: int = 8000
    max_skills: int = 12
    components: tuple[str, ...] = TEXT_KINDS
    package_api: str | None = None

    def __post_init__(self) -> None:
        unknown = sorted(set(self.components) - set(KINDS))
        if unknown:
            raise PackageError(f"unknown component kinds {unknown}: {list(KINDS)}")
        if self.package_api is None:
            object.__setattr__(self, "package_api",
                               AGENT_API_VERSION if "agent" in self.components else API_VERSIONS[0])
        if self.package_api not in API_VERSIONS:
            raise PackageError(f"package_api {self.package_api!r}: this host serves {list(API_VERSIONS)}")
        if "agent" in self.components and AGENT_API_VERSION not in self.api_versions:
            raise PackageError(f"the agent component needs package_api {AGENT_API_VERSION}; this core fixes "
                               f"{self.package_api}")

    @property
    def allows_code(self) -> bool:
        return any(k in CODE_KINDS for k in self.components)

    @property
    def api_versions(self) -> tuple[str, ...]:
        """The manifest api_versions this core takes: every one up to its `package_api`."""
        return API_VERSIONS[:API_VERSIONS.index(self.package_api) + 1]

    def to_json(self) -> dict:
        return {"max_files": self.max_files, "max_package_bytes": self.max_package_bytes,
                "max_memory_chars": self.max_memory_chars, "max_skill_chars": self.max_skill_chars,
                "max_skills": self.max_skills, "components": list(self.components), "package_api": self.package_api}

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
                       f"middlewares/*.py, tests/*.py, agent/*.py)")


def empty_manifest(api_version: str = API_VERSIONS[0]) -> dict:
    """The seed manifest: a v1 package, which the default agent runs. v1 is the default so that the empty package
    W0 pins keeps its ref and parses under a core frozen to either API."""
    return {"api_version": api_version, "tools": [], "hooks": [], "skills": [], "memory": {"enabled": False}}


def parse_manifest(text: str, files: dict[str, str], *, api_versions: tuple[str, ...] = API_VERSIONS) -> dict:
    """The manifest, checked against the files it names. Unknown keys, unknown sites, duplicate names and entries
    outside the bundle are structure errors. `api_versions` are the versions the caller's core takes (every one
    served, by default); an `agent` entry is only ever a v2 entry. The parsed manifest carries `agent` only when the
    entry is there, so a v1 manifest is stored as it always was."""
    try:
        m = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as e:
        raise PackageError(f"manifest.json is not JSON or nests too deep: {str(e)[:200]}") from None
    if not isinstance(m, dict):
        raise PackageError("manifest.json is an object")
    unknown = sorted(set(m) - {"api_version", "tools", "hooks", "skills", "memory", "agent"})
    if unknown:
        raise PackageError(f"manifest.json does not take {unknown}")
    version = m.get("api_version")
    if version not in API_VERSIONS:
        raise PackageError(f"manifest api_version {version!r}: this host serves {list(API_VERSIONS)}")
    if version not in api_versions:
        raise PackageError(f"manifest api_version {version!r}: this core allows {list(api_versions)}; a package "
                           f"is written to the API its core froze, not to the newest the host serves")
    if "agent" in m and API_VERSIONS.index(version) < API_VERSIONS.index(AGENT_API_VERSION):
        raise PackageError(f"manifest has an agent entry and api_version {version!r}: an agent component needs "
                           f"{AGENT_API_VERSION}")
    out = empty_manifest(version)
    names: set[str] = set()
    for key in ("tools", "hooks", "skills"):
        if not isinstance(m.get(key) or [], list):
            raise PackageError(f"manifest {key} is a list")
    for tool in m.get("tools") or []:
        _need(tool, {"name", "description", "input_schema", "entrypoint"}, "tool")
        _unique(tool["name"], names, "tool")
        if tool["name"] in HOST_TOOLS:
            raise PackageError(f"tool {tool['name']}: the name is the host's own tool")
        if not TOOL_NAME.fullmatch(tool["name"]):
            raise PackageError(f"tool name {tool['name']!r}: letters, digits, '_' and '-' only, at most 64 "
                               f"characters; the model providers refuse any other tool name")
        if len(tool["description"]) > MAX_TOOL_DESCRIPTION:
            raise PackageError(f"tool {tool['name']}: a description of {len(tool['description'])} characters; "
                               f"providers take at most {MAX_TOOL_DESCRIPTION}")
        _entrypoint(tool["entrypoint"], "tools/", files)
        _input_schema(tool["input_schema"], f"tool {tool['name']}")
        out["tools"].append({**{k: tool[k] for k in ("name", "description", "input_schema", "entrypoint")},
                             "enabled": tool.get("enabled", True)})
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
        if not SKILL_NAME.fullmatch(str(skill["name"])):
            raise PackageError(f"skill name {skill['name']!r}: lowercase letters, digits, '-' and '_'")
        _unique(skill["name"], names, "skill")
        if skill["path"] not in files or classify(skill["path"]) != "skill":
            raise PackageError(f"skill {skill['name']}: {skill['path']!r} is not a skills/*.md file of the package")
        out["skills"].append({"name": skill["name"], "path": skill["path"],
                              "description": str(skill.get("description", ""))[:300],
                              "enabled": bool(skill.get("enabled", True))})
    memory = m.get("memory") or {}
    if not isinstance(memory, dict) or sorted(set(memory) - {"enabled"}) \
            or not isinstance(memory.get("enabled", False), bool):
        raise PackageError("memory is {enabled: bool}")
    if memory.get("enabled") and "memory.md" not in files:
        raise PackageError("memory is enabled and there is no memory.md")
    out["memory"] = {"enabled": bool(memory.get("enabled"))}
    if "agent" in m:
        out["agent"] = _agent_entry(m["agent"], files)
    return out


def _agent_entry(entry, files: dict) -> dict:
    """The one `agent` entry of a v2 manifest (docs/modules/00_CONTRACTS.md §2 "包 API v2"): `agent/<file>.py:run`
    and a non-empty list of the roles it calls the model as. One entry only — one attempt has one entry point, and
    dispatching between loops is the agent's own code; the callable is named `run` so the worker's protocol has one
    name to resolve; the roles are declared so the gateway can refuse an undeclared one and the frozen package
    says, before it runs, which roles its gateway records will carry."""
    if isinstance(entry, list):
        raise PackageError(f"manifest names {len(entry)} agents: a package has exactly one agent entry, an "
                           f"object {{entrypoint, roles}}; dispatch between loops belongs in the agent's code")
    if not isinstance(entry, dict) or set(entry) != {"entrypoint", "roles"}:
        raise PackageError('the agent entry is {"entrypoint": "agent/<file>.py:run", "roles": [...]}' +
                           (f"; it has {sorted(entry)}" if isinstance(entry, dict) else ""))
    if not isinstance(entry["entrypoint"], str):
        raise PackageError("the agent entrypoint is a string")
    _entrypoint(entry["entrypoint"], "agent/", files)
    func = entry["entrypoint"].partition(":")[2]
    if func != AGENT_ENTRY:
        raise PackageError(f"agent entrypoint {entry['entrypoint']!r}: the agent's callable is named "
                           f"{AGENT_ENTRY}, not {func!r}")
    roles = entry["roles"]
    if not isinstance(roles, list) or not roles or not all(isinstance(r, str) for r in roles):
        raise PackageError(f"agent roles are a non-empty list of {list(ROLES)}")
    unknown = sorted(set(roles) - set(ROLES))
    if unknown:
        raise PackageError(f"agent roles {unknown} are unknown: the roles are {list(ROLES)}")
    if len(set(roles)) != len(roles):
        raise PackageError(f"agent roles {roles} name a role twice")
    return {"entrypoint": entry["entrypoint"], "roles": [r for r in ROLES if r in roles]}


def _need(entry, required: set, what: str, optional: set = frozenset()) -> None:
    if not isinstance(entry, dict):
        raise PackageError(f"a {what} entry is an object with the keys {sorted(required)}"
                           + (f" and optionally {sorted(optional)}" if optional else ""))
    missing = sorted(required - set(entry))
    extra = sorted(set(entry) - required - set(optional) - {"enabled"})
    if missing or extra:
        raise PackageError(f"a {what} entry needs {sorted(required)}; missing {missing}, unknown {extra}")
    for key in required | (set(optional) & set(entry)):
        if key in ("input_schema", "enabled"):
            continue
        if not isinstance(entry[key], str):
            raise PackageError(f"a {what} entry's {key} is a string")
    if "enabled" in entry and not isinstance(entry["enabled"], bool):
        raise PackageError(f"a {what} entry's enabled is true or false")


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


def _input_schema(schema, what: str) -> None:
    """A tool's input schema in the shape the model providers take. Not a full JSON Schema validator: it refuses
    the shapes providers are known to refuse, each named so R can fix it — an object at the top with no
    anyOf/oneOf/allOf, JSON types only, properties that are schemas, a list of required names, items for every
    array, and no deeper nesting than MAX_SCHEMA_DEPTH."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise PackageError(f"{what}: input_schema is a JSON object schema")
    if set(schema) & {"anyOf", "oneOf", "allOf"}:
        raise PackageError(f"{what}: input_schema has anyOf/oneOf/allOf at the top; providers take an object")

    def walk(s, at: str, depth: int) -> None:
        if not isinstance(s, dict):
            raise PackageError(f"{what}: {at} is a schema object")
        if depth > MAX_SCHEMA_DEPTH:
            raise PackageError(f"{what}: input_schema nests deeper than {MAX_SCHEMA_DEPTH}")
        kind = s.get("type")
        kinds = kind if isinstance(kind, list) else [kind] if kind is not None else []
        if (isinstance(kind, list) and not kind) or any(k not in JSON_TYPES for k in kinds):
            raise PackageError(f"{what}: {at} type {kind!r} is one of {list(JSON_TYPES)} or a list of them")
        if "array" in kinds and "items" not in s:
            raise PackageError(f"{what}: {at} is an array and gives no items")
        if "properties" in s and not isinstance(s["properties"], dict):
            raise PackageError(f"{what}: {at}.properties is an object of schemas")
        if "required" in s and not (isinstance(s["required"], list)
                                    and all(isinstance(r, str) for r in s["required"])):
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
    if manifest.get("agent") or any(classify(p) == "agent" for p in files):
        kinds.add("agent")
    return kinds


def _code_entries(manifest: dict) -> bool:
    """Whether the manifest names code: tools, hooks, or an agent of its own (M0 §2: a package with an `agent`
    entry runs in the worker; one without is run by the default agent, which is the core's code)."""
    return bool(manifest["tools"] or manifest["hooks"] or manifest.get("agent"))


def needs_worker_for_checks(files: dict[str, str], manifest: dict) -> bool:
    """The check predicate: any Python in the run or test bundle, or any tool/hook/agent entry."""
    return _code_entries(manifest) or any(p.endswith(".py") for p in files)


def needs_worker_at_runtime(run_files: dict[str, str], manifest: dict) -> bool:
    """The runtime predicate: tool/hook/agent entries or Python in the run bundle. Tests are never mounted to run."""
    return _code_entries(manifest) or any(p.endswith(".py") for p in run_files)


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
    manifest = parse_manifest(files.get("manifest.json", json.dumps(empty_manifest())), files,
                              api_versions=limits.api_versions)
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
    # The kinds rule comes before the stray-agent rule: under a core that allows no agent, a stray agent/*.py is
    # refused as the kind it is, not told to name an entrypoint the core would refuse next.
    used = kinds_of(manifest, files)
    outside = sorted(used - set(limits.components))
    if outside:
        raise PackageError(f"component kinds {outside} are not allowed by this core: {list(limits.components)}")
    agent_files = sorted(p for p in files if classify(p) == "agent")
    if agent_files and not manifest.get("agent"):
        # Like a stray skill: code under agent/ that no entry names would never run, and a package is what it runs.
        raise PackageError(f"agent files {agent_files} and no agent entry in the manifest: name the entrypoint "
                           f"agent/<file>.py:run and its roles, or remove the files")
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


# v0.4 (docs/design/V0_4_PLAN.md §1.5; docs/modules/00_CONTRACTS.md §3, v0.4): which train literals are a task's
# answer. A value written in two or more train tasks is the domain's vocabulary, not one task's answer; a pure number
# is a constant as often as a value ('3000' of `[:3000]`); a field name or a column header is the schema. Only a
# value of exactly one train task stays forbidden — the v0.3 check refused 14 of 20 drafts on ordinary words.
NUMBER = re.compile(r"[+-]?\d[\d.,]*%?")
HEADER_LISTS = ("headers", "header", "columns", "fieldnames")
MIN_SHARED_TASKS = 2


def header_names(values) -> set[str]:
    """The schema words inside train data: every string key of a mapping (a record's field, a sheet row's column)
    and the strings of a list under a key named like a header row (HEADER_LISTS)."""
    out: set[str] = set()

    def walk(v, key=None) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if isinstance(k, str) and k.strip():
                    out.add(k.strip())
                walk(x, k)
        elif isinstance(v, (list, tuple)):
            for x in v:
                if isinstance(x, str) and isinstance(key, str) and key.lower() in HEADER_LISTS and x.strip():
                    out.add(x.strip())
                else:
                    walk(x)
    walk(values)
    return out


def add_literals(index: dict, task_id: str, values) -> None:
    """Record the literals of one train task's values (assertion params or initial state) in `index`: literal ->
    the set of train tasks it occurs in."""
    for lit in literals_of(values):
        index.setdefault(lit, set()).add(str(task_id))


def forbidden_literals(index: dict, *, exempt=frozenset(), answers=frozenset()) -> set[str]:
    """The literals a package may not contain: those of exactly one train task (fewer than MIN_SHARED_TASKS), less
    the `exempt` schema words (header_names) and less pure numbers — except a number that is a train assertion's
    value (`answers`: the literals of the train assertion params). A number in one task's state or in code
    (`[:3000]`) is a constant as often as a value; a number the grader checks for in one task ('13,650', a finance
    amount) is that task's answer and stays forbidden — of finance's 50 train tasks, 35 have such an amount (review of
    2026-10-06). An index given as a plain set (the v0.3 form) is every literal, as before."""
    if not isinstance(index, dict):
        return set(index) - set(exempt)
    answers, exempt = set(answers), set(exempt)
    return {lit for lit, tasks in index.items()
            if len(set(tasks)) < MIN_SHARED_TASKS and lit not in exempt
            and (not NUMBER.fullmatch(lit) or lit in answers)}


def _pattern(lit: str) -> re.Pattern:
    """A literal as a whole token. Case-sensitive (v0.4): 'not sent' in a hook's regex is not the sheet value
    'Not Sent'; an e-mail address is matched in any case, since it is the same address."""
    flags = re.IGNORECASE if literal_kind(lit) == "e-mail address" else 0
    return re.compile(r"(?<![\w@.-])" + re.escape(lit) + r"(?![\w@-])", flags)


def literal_findings(files: dict[str, str], literals: set[str], *, allowed: set[str] = frozenset()) -> list[dict]:
    """Where each forbidden literal occurs: [{file, line, literal, kind}], in file order. Searched in what the host
    uses — each file's path (line 0), its body line by line, and the manifest's decoded strings as well (a JSON
    escape must not hide a literal; a hit there that the raw text lacks has line None). A finding is a structure
    error: a single task's answer written into a shared component (docs/modules/00_CONTRACTS.md §3)."""
    whole = _texts(files)
    lower = whole.lower()
    # a substring test over everything first (an e-mail address in any case); only the few that pass get a pattern
    # and the line-by-line pass
    wanted = [(lit, _pattern(lit)) for lit in sorted(set(literals) - set(allowed))
              if lit in whole or ("@" in lit and lit.lower() in lower)]
    out: list[dict] = []
    for path in sorted(files):
        text = files[path] if isinstance(files[path], str) else ""
        found: set[str] = set()
        for lit, pat in wanted:
            if pat.search(path):
                out.append({"file": path, "line": 0, "literal": lit, "kind": literal_kind(lit) or "value"})
                found.add(lit)
        for n, line in enumerate(text.splitlines(), start=1):
            for lit, pat in wanted:
                if pat.search(line):
                    out.append({"file": path, "line": n, "literal": lit, "kind": literal_kind(lit) or "value"})
                    found.add(lit)
        if path == "manifest.json":
            try:
                decoded = json.dumps(json.loads(text), ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                decoded = ""
            for lit, pat in wanted:
                if lit not in found and pat.search(decoded):
                    out.append({"file": path, "line": None, "literal": lit, "kind": literal_kind(lit) or "value"})
    return out


def literal_hits(files: dict[str, str], literals: set[str], *, allowed: set[str] = frozenset()) -> list[str]:
    """Which forbidden literals occur in the package (literal_findings), each once, sorted."""
    return sorted({f["literal"] for f in literal_findings(files, literals, allowed=allowed)})


MAX_LISTED_FINDINGS = 12


def describe_hits(hits, *, what: str = "the package") -> str:
    """The check output for literal findings (v0.4): file, line and the literal, so R revises the line it wrote
    instead of guessing (v0.3 gave counts only, and 14 of R's 25 turns went to blind revisions). The literal is train
    data R already reads in its failure summaries and trajectories; the output goes back to R only, never into a
    package. Plain strings (literal_hits) are listed as they are."""
    listed = []
    for h in hits:
        if isinstance(h, dict):
            where = h["file"] if h.get("line") is None else f"{h['file']}:{h['line']}"
            listed.append(f"{where}: {h['literal']!r} ({h.get('kind') or 'value'})")
        else:
            listed.append(f"{h!r} ({literal_kind(h) or 'value'})")
    more = f"; and {len(listed) - MAX_LISTED_FINDINGS} more" if len(listed) > MAX_LISTED_FINDINGS else ""
    return (f"{what} contains task-specific values from train tasks — a value that occurs in only one train task is "
            f"that task's answer: " + "; ".join(listed[:MAX_LISTED_FINDINGS]) + more
            + ". Replace each with a general rule (match case-sensitively: a lower-case phrase is not the value)")


# ---------------------------------------------------------------------------- storage
def _bundle(objects: ct.Objects, files: dict[str, str]) -> str:
    return objects.put_json("bundle", {"files": {p: objects.put_json("file", {"text": files[p]})
                                                 for p in sorted(files)}})


def api_ref(objects: ct.Objects, api_version: str = API_VERSION) -> str:
    """The package API a package targets, as the record's `package_api_ref`: the package's own `api_version`, not
    this host's newest, so a v1 and a v2 package of the same files are different packages (M0 §2)."""
    if api_version not in API_VERSIONS:
        raise PackageError(f"package_api {api_version!r}: this host serves {list(API_VERSIONS)}")
    return objects.put_json("change", {"package_api": api_version, "hook_sites": list(HOOK_SITES)})


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
        package_api_ref=api_ref(objects, manifest["api_version"]), dependency_set_ref=dependency_ref(objects, image),
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
    def api_version(self) -> str:
        """The package API the manifest targets; a manifest stored before the field was read is a v1 one."""
        return self.manifest.get("api_version") or API_VERSIONS[0]

    @property
    def agent(self) -> dict | None:
        """The package's own agent entry, or None for a package the default agent runs (every v1 package)."""
        return self.manifest.get("agent") or None

    @property
    def roles(self) -> tuple[str, ...]:
        """The roles this package's loop calls the model as. The default agent is today's host loop, which makes one
        `act` call a step and nothing else, so a package without an agent entry declares `act` alone."""
        return tuple(self.agent["roles"]) if self.agent else ("act",)

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


# ---------------------------------------------------------------------------- the host side of a package
# The runner shell (agent/runner.py) runs a package: its default agent or its own agent program, in the isolated worker
# when it holds code. The v1 loop's mount of a package as a tool surface (PackageTools, PackageRuntime, mount) went
# with that loop in P1 step 5 (tag archive-2026-10-08).
HOST_TOOLS = ("read_skill",)          # tools the host adds; a package tool may not take their names


class CapabilityError(RuntimeError):
    """A package that needs a capability this host does not have (the isolated worker). Refused at assembly,
    never run in-process (docs/modules/00_CONTRACTS.md §3)."""


def usable_worker(worker) -> bool:
    """A worker package code may run in: an isolating one, or a protocol-test worker a test named as such."""
    return worker is not None and (bool(getattr(worker, "qualified", False)) or bool(getattr(worker, "for_tests",
                                                                                              False)))
