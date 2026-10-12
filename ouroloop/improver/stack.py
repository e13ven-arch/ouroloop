"""Stacking accepted packages (docs/design/IMPROVER.md §9.4; DESIGN §6.1 v1.3): a round may accept several packages,
each screened on its own against the round's base. The first installs as it is; each further one is laid on the
working line as it stands — its changes against the base applied on top — when the two do not touch the same thing.

A conflict is a file both changed (against the base) differently; `manifest.json` is merged by section: tools, hooks and
skills by entry name (the same name changed by both is a conflict), the other sections by key. The result still climbs
V0 and V1 before it installs (round.py); nothing here runs the package.
"""
from __future__ import annotations

import json

from .components import changed_paths

NAMED = {"tools": "name", "hooks": "name", "skills": "name"}
KEYED = ("agent", "prompt", "config", "memory", "model")
MANIFEST = "manifest.json"


def _manifest(text: str | None) -> dict:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _by(entries, key: str) -> dict:
    return {str(e.get(key)): e for e in entries or () if isinstance(e, dict)}


def _same(a, b) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def merge_manifests(base: dict, line: dict, cand: dict) -> tuple[dict, list[str]]:
    """The line's manifest with the candidate's changes against the base laid on it; -> (manifest, conflicts)."""
    out, conflicts = json.loads(json.dumps(line)), []
    for section, key in NAMED.items():
        if section not in base and section not in line and section not in cand:
            continue
        b, l, c = _by(base.get(section), key), _by(line.get(section), key), _by(cand.get(section), key)
        merged = {**l}
        for name in sorted(set(b) | set(c)):
            cand_changed = not _same(b.get(name), c.get(name))
            if not cand_changed:
                continue
            line_changed = not _same(b.get(name), l.get(name))
            if line_changed and not _same(l.get(name), c.get(name)):
                conflicts.append(f"{section}:{name}")
                continue
            if name in c:
                merged[name] = c[name]
            else:
                merged.pop(name, None)
        out[section] = list(merged.values())
    for section in KEYED:
        b, l, c = base.get(section), line.get(section), cand.get(section)
        if _same(b, c):
            continue
        if isinstance(b, dict) and isinstance(c, dict) and isinstance(l, dict):
            merged = dict(l)
            for k in sorted(set(b) | set(c)):
                if _same(b.get(k), c.get(k)):
                    continue
                if not _same(b.get(k), l.get(k)) and not _same(l.get(k), c.get(k)):
                    conflicts.append(f"{section}.{k}")
                    continue
                if k in c:
                    merged[k] = c[k]
                else:
                    merged.pop(k, None)
            out[section] = merged
        elif _same(b, l) or _same(l, c):
            out[section] = c
        else:
            conflicts.append(section)
    tests = list(dict.fromkeys([*(line.get("tests") or ()), *(cand.get("tests") or ())]))
    if tests or "tests" in out:
        out["tests"] = tests
    for k in cand:
        if k not in out:
            out[k] = cand[k]
    return out, conflicts


def stack(base: dict[str, str], line: dict[str, str], cand: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """The line's files with the candidate's changes against the base laid on them. -> (files, conflicts); the files
    are complete when there is no conflict, and not to be used otherwise."""
    line_changes, cand_changes = changed_paths(base, line), changed_paths(base, cand)
    out, conflicts = dict(line), []
    for path, change in cand_changes.items():
        if path == MANIFEST:
            continue
        if path in line_changes and line.get(path) != cand.get(path):
            conflicts.append(path)
            continue
        if change == "deleted":
            out.pop(path, None)
        else:
            out[path] = cand[path]
    if MANIFEST in cand_changes:
        manifest, more = merge_manifests(_manifest(base.get(MANIFEST)), _manifest(line.get(MANIFEST)),
                                         _manifest(cand.get(MANIFEST)))
        conflicts += more
        out[MANIFEST] = json.dumps(manifest, indent=1, ensure_ascii=False) + "\n"
    return out, sorted(conflicts)
