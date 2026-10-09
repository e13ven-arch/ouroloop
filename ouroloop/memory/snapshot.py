"""The library as a component of W (docs/design/MEMORY.md §7; DESIGN §5.5).

A package reads memory when its manifest says `memory: {"enabled": true}`: the seed then offers the agent `recall`, and
the host appends the opening block. During the train stream the package is W_base with memory on and no snapshot file:
it reads the stream's live library (`stream_files`). After the stream the whole library is one candidate: W_base with
`memory/snapshot.jsonl` holding the canonical snapshot and the manifest naming it (`snapshot_candidate`); it goes to
the improver's ladder from V3 like any candidate, and once installed dev and test see only that frozen snapshot.

Both change nothing else of W_base: the manifest's memory entry is rewritten in place, so its other lines (and the
literal scan of a later candidate, which reads changed lines) are untouched.
"""
from __future__ import annotations

import json
import re

from ..harness.kinds import MEMORY_PATH
from ..harness.package import Package
from .client import parse_jsonl
from .rules import sources_of_row, stats_of_row

MEMORY_ENTRY = re.compile(r'"memory"\s*:\s*\{[^{}]*\}')


def with_memory(manifest_text: str, memory: dict) -> str:
    """The manifest with its memory entry replaced by `memory`, the rest of the text as it was."""
    value = json.dumps(memory, ensure_ascii=False)
    if len(MEMORY_ENTRY.findall(manifest_text)) == 1:
        return MEMORY_ENTRY.sub(lambda _m: f'"memory": {value}', manifest_text)
    manifest = json.loads(manifest_text)                # an unusual layout: written again, every key kept
    manifest["memory"] = memory
    return json.dumps(manifest, indent=1, ensure_ascii=False) + "\n"


def stream_files(parent: Package) -> dict[str, str]:
    """W_base as the stream runs it: memory on, no snapshot (the live library is read instead)."""
    files = {p: t for p, t in parent.files.items() if p != MEMORY_PATH}
    files["manifest.json"] = with_memory(parent.files["manifest.json"], {"enabled": True})
    return files


def snapshot_candidate(parent: Package, snapshot_text: str) -> dict[str, str]:
    """W_base with the library frozen into it: memory/snapshot.jsonl and the manifest's memory entry naming it; every
    other file as it was."""
    if not snapshot_text.strip():
        raise ValueError("an empty library is not a candidate")
    files = dict(parent.files)
    files[MEMORY_PATH] = snapshot_text
    files["manifest.json"] = with_memory(parent.files["manifest.json"], {"enabled": True, "snapshot": MEMORY_PATH})
    return files


def library_of(package: Package) -> list[dict]:
    """The entries of a package's snapshot, by id: {id, when, content, origin, sources, stats, provenance}."""
    out = []
    for row in parse_jsonl(package.memory_text):
        meta = row.get("metadata") or {}
        out.append({"id": row["memory_id"], "when": row.get("when_to_use") or "", "content": row.get("content") or "",
                    "origin": meta.get("ol_origin"), "sources": sources_of_row(row), "stats": stats_of_row(row),
                    "provenance": {k: v for k, v in meta.items() if k.startswith("ol_")
                                   and k not in ("ol_sources", "ol_stats")}})
    return sorted(out, key=lambda e: e["id"])
