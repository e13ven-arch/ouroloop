"""Promotion: memory -> skill -> tool or hook (docs/design/MEMORY.md §8; DESIGN §5.6).

An entry that fixed tasks from several sources has grown past a note (rules.promotable). When the improver's diagnosis
can mechanise it, it becomes a cause of the round (`cause_input`) and R implements it as a tool or a hook; otherwise,
when it reads as a procedure, fixed code rewrites it as a skill candidate (`skill_candidate`) that goes up the same
ladder as any candidate (V0 literals, V2c reach, V3 onward paid). Once a promotion is installed the entry is
superseded (`mark_superseded`): the live store retires it and it is never shown again. Wiring this into the rounds is
the improver's (stage 3).
"""
from __future__ import annotations

import json
import re

from ..harness.package import Package
from .rules import distilled_from

SLUG = re.compile(r"[^a-z0-9]+")
SKILL_DIR = "skills/memory"


def _slug(entry: dict) -> str:
    when = str((entry.get("row") or {}).get("when_to_use") or entry.get("when") or "")
    words = [w for w in SLUG.split(when.lower()) if w and w not in ("when", "a", "an", "the", "task", "asks", "to")]
    return ("-".join(words[:5]) or "lesson")[:40].strip("-")


def _text(entry: dict) -> tuple[str, str, str]:
    row = entry.get("row") or {}
    eid = str(entry.get("memory_id") or row.get("memory_id") or entry.get("id") or "")
    return eid, str(row.get("when_to_use") or entry.get("when") or "").strip(), \
        str(row.get("content") or entry.get("content") or "").strip()


def skill_candidate(entry: dict, parent: Package) -> dict[str, str]:
    """The parent's files with the entry as a skill: skills/memory/<slug>-<id8>.md (when to use it, then the
    procedure, then where it came from) and its manifest entry. Its description is the entry's trigger, so the skill
    index tells the agent when to read it."""
    eid, when, content = _text(entry)
    if not eid or not when or not content:
        raise ValueError("a skill candidate needs an entry with an id, a trigger and a procedure")
    name = f"mem-{_slug(entry)}"[:55].rstrip("-") + f"-{eid[:8]}"
    path = f"{SKILL_DIR}/{name}.md"
    text = (f"# {name}\n\nUse this when: {when}\n\n{content}\n\n"
            f"Source: the experience library's entry {eid[:8]}, which fixed {int((entry.get('stats') or {}).get('fixes') or 0)} "
            f"train tasks and was learned from {len(distilled_from(entry))}.\n")
    manifest = json.loads(parent.files["manifest.json"])
    skills = [s for s in manifest.get("skills") or () if s.get("name") != name]
    skills.append({"name": name, "path": path, "description": when[:300]})
    manifest["skills"] = skills
    files = dict(parent.files)
    files[path] = text
    files["manifest.json"] = json.dumps(manifest, indent=1, ensure_ascii=False) + "\n"
    return files


def skill_provenance(entry: dict) -> dict:
    """What the candidate's declaration names as its source: the entry and the train evidence behind it."""
    eid, when, _ = _text(entry)
    row = entry.get("row") or {}
    meta = row.get("metadata") or {}
    try:
        traces = sorted(json.loads(meta.get("ol_trace_refs") or "[]"))
    except ValueError:
        traces = []
    return {"memory_entry": eid, "when": when, "fixes": int((entry.get("stats") or {}).get("fixes") or 0),
            "distilled_from": sorted(distilled_from(entry)), "trace_refs": traces}


def cause_input(entry: dict) -> dict:
    """The entry as an input of the improver's clustering (IMPROVER §4 `memory_entries`): the lesson, how often it
    fixed and harmed, where it came from."""
    eid, when, content = _text(entry)
    stats = entry.get("stats") or {}
    return {"kind": "memory_entry", "entry": eid, "when": when, "content": content, "origin": entry.get("origin"),
            "fixes": int(stats.get("fixes") or 0), "harm_tasks": len(stats.get("harm_tasks") or ()),
            "shown": int(stats.get("n_shown") or 0), "distilled_from": sorted(distilled_from(entry)),
            "provenance": skill_provenance(entry)}


def mark_superseded(entries: dict, entry_id: str, by: str) -> None:
    """The entry's promotion was installed (`by` names the release or candidate): it is superseded, not shown again."""
    e = entries.get(entry_id)
    if e is None:
        raise KeyError(f"no memory entry {entry_id}")
    if e.get("status") == "superseded":
        return
    e.update(status="superseded", superseded_by=str(by))
