"""What a candidate changes, component by component (docs/design/IMPROVER.md §8, §10): the unit V0 checks a declaration
against, V3 conditions a fix on, and credit is written for.

A component is {kind, name, site?, change}: a tool or hook by its manifest name (its entry changed, or the file its
entrypoint names), a skill by name, a prompt file by its key (system, finish), the agent, a config knob, a vendored
package, the memory snapshot, the model binding. Package tests are a component of their own (kind test) that credits
nothing.
"""
from __future__ import annotations

import json

from ..harness.kinds import MEMORY_PATH
from ..harness.package import Package, classify, normal_section


def _manifest(text: str | None) -> dict:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _module(entrypoint) -> str:
    return str(entrypoint or "").partition(":")[0]


def changed_paths(parent: dict, files: dict) -> dict[str, str]:
    out = {}
    for path in sorted(set(parent) | set(files)):
        if path not in files:
            out[path] = "deleted"
        elif path not in parent:
            out[path] = "added"
        elif files[path] != parent[path]:
            out[path] = "modified"
    return out


def _named(old: list, new: list, kind: str, changed: dict, *, key="name", file_of=None) -> list[dict]:
    before = {e.get(key): e for e in old if isinstance(e, dict)}
    after = {e.get(key): e for e in new if isinstance(e, dict)}
    out = []
    for name in sorted(set(before) | set(after), key=str):
        b, a = before.get(name), after.get(name)
        if a is None:
            change = "deleted"
        elif b is None:
            change = "added"
        elif json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True) or \
                (file_of and file_of(a) in changed):
            change = "modified"
        else:
            continue
        entry = a or b
        item = {"kind": kind, "name": str(name), "change": change}
        if kind == "hook":
            item["site"] = entry.get("site")
        out.append(item)
    return out


def components(parent: Package, files: dict[str, str]) -> list[dict]:
    """The components `files` change against `parent`, in a fixed order."""
    changed = changed_paths(parent.files, files)
    raw = _manifest(files.get("manifest.json"))
    old = parent.manifest.to_json()
    new = {k: normal_section(k, raw.get(k)) for k in set(raw) | set(old)}
    out = []
    out += _named(old.get("tools") or [], new.get("tools") or [], "tool", changed,
                  file_of=lambda e: _module(e.get("entrypoint")))
    out += _named(old.get("hooks") or [], new.get("hooks") or [], "hook", changed,
                  file_of=lambda e: _module(e.get("entrypoint")))
    out += _named(old.get("skills") or [], new.get("skills") or [], "skill", changed, file_of=lambda e: e.get("path"))
    for key in ("system", "finish"):
        before, after = (old.get("prompt") or {}).get(key), (new.get("prompt") or {}).get(key)
        if before != after or (after and after in changed):
            out.append({"kind": "prompt", "name": key, "change": "modified" if before and after else
                        ("added" if after else "deleted")})
    agent_files = [p for p in changed if classify(p) == "agent"]
    if agent_files or json.dumps(old.get("agent"), sort_keys=True) != json.dumps(new.get("agent"), sort_keys=True):
        out.append({"kind": "agent", "name": "agent", "change": "modified"})
    for knob in sorted(set(old.get("config") or {}) | set(new.get("config") or {})):
        if (old.get("config") or {}).get(knob) != (new.get("config") or {}).get(knob):
            out.append({"kind": "config", "name": knob, "change": "modified"})
    vendors = sorted({p.split("/")[1] for p in changed if p.startswith("vendor/") and p.count("/") >= 2})
    out += [{"kind": "vendor", "name": v, "change": "modified"} for v in vendors]
    if MEMORY_PATH in changed or json.dumps(old.get("memory"), sort_keys=True) != \
            json.dumps(new.get("memory"), sort_keys=True):
        out.append({"kind": "memory", "name": "snapshot", "change": "modified"})
    if json.dumps(old.get("model"), sort_keys=True) != json.dumps(new.get("model"), sort_keys=True):
        out.append({"kind": "model", "name": "model", "change": "modified"})
    tests = [p for p in changed if classify(p) == "test"]
    if tests or json.dumps(old.get("tests"), sort_keys=True) != json.dumps(new.get("tests"), sort_keys=True):
        out.append({"kind": "test", "name": "tests", "change": "modified"})
    return out


def present(package: Package) -> list[dict]:
    """The components a package holds that credit can be read for (tools, hooks, skills)."""
    m = package.manifest
    return ([{"kind": "tool", "name": t["name"]} for t in m.tools if t.get("enabled", True)]
            + [{"kind": "hook", "name": h["name"], "site": h.get("site")} for h in m.hooks if h.get("enabled", True)]
            + [{"kind": "skill", "name": s["name"]} for s in m.skills if s.get("enabled", True)])


def without(package: Package, component: dict) -> dict[str, str]:
    """The package's files with one tool, hook or skill removed (its manifest entry, and its file when nothing else
    of the package names that file): a removal candidate."""
    files = dict(package.files)
    manifest = _manifest(files["manifest.json"])
    section = {"tool": "tools", "hook": "hooks", "skill": "skills"}[component["kind"]]
    entries = manifest.get(section) or []
    gone = [e for e in entries if e.get("name") == component["name"]]
    manifest[section] = [e for e in entries if e.get("name") != component["name"]]
    for e in gone:
        path = e.get("path") or _module(e.get("entrypoint"))
        still = any((x.get("path") or _module(x.get("entrypoint"))) == path
                    for s in ("tools", "hooks", "skills") for x in manifest.get(s) or ())
        if path and not still:
            files.pop(path, None)
    files["manifest.json"] = json.dumps(manifest, indent=1, ensure_ascii=False) + "\n"
    return files
