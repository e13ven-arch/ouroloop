"""The library as a component of W (docs/design/MEMORY.md §7; DESIGN §5.5).

A package reads memory when its manifest says `memory: {"enabled": true}`: the seed then offers the agent `recall`, and
the host appends the opening block. During the train stream the package is W_base with memory on and no snapshot file:
it reads the stream's live library (`stream_files`). After the stream the library is one candidate: W_base with
`memory/snapshot.jsonl` holding the canonical snapshot and the manifest naming it (`snapshot_candidate`); it goes to
the improver's ladder from V3 like any candidate, and once installed dev and test see only that frozen snapshot.

Both change nothing else of W_base: the manifest's memory entry is rewritten in place, so its other lines (and the
literal scan of a later candidate, which reads changed lines) are untouched.

The snapshot is not the live library's rows as they are (§7.1): a row of it holds only what retrieval and the
platform's rules read (`snapshot_row`; the rest stays in the stream's ledger and live.jsonl), and when the library
does not fit K's max_memory_bytes `fit_snapshot` keeps the longest prefix of the entries by conditioned credit
(fixes, then citations, then id) that does, and records the ones it left out. The live library's capacity is a count
(max_active); the bytes are enforced here, where the library becomes part of W.
"""
from __future__ import annotations

import json
import re

from ..harness.kinds import MEMORY_PATH
from ..harness.package import Package
from .client import canonical_rows, parse_jsonl
from .rules import sources_of_row, stats_of_row

MEMORY_ENTRY = re.compile(r'"memory"\s*:\s*\{[^{}]*\}')
# a snapshot row (§7.1): what retrieval reads (id, type for ReMe's load, trigger, lesson; the filter's line and split;
# leave-one-out's sources), what the import's train-only rule reads (the source line) and the credit it is ranked by
ROW_KEYS = ("memory_id", "memory_type", "when_to_use", "content")
META_KEYS = ("ol_line", "ol_split", "ol_source_line", "ol_sources")
RANK_STATS = ("fixes", "n_cited")
RANK = "fixes, then citations (n_cited), then memory_id"


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


def snapshot_row(row: dict) -> dict:
    """A library row as a package carries it: ROW_KEYS, the metadata's META_KEYS, `ol_imported_from` on an imported
    row, and `ol_stats` reduced to RANK_STATS. Applied to such a row it gives the row back."""
    meta = row.get("metadata") or {}
    out = {k: row[k] for k in ROW_KEYS if k in row}
    kept = {k: meta[k] for k in META_KEYS if k in meta}
    if meta.get("ol_imported_from"):
        kept["ol_imported_from"] = meta["ol_imported_from"]
    stats = stats_of_row(row)
    kept["ol_stats"] = json.dumps({k: int(stats.get(k) or 0) for k in RANK_STATS}, sort_keys=True,
                                  separators=(",", ":"))
    out["metadata"] = kept
    return out


def snapshot_rank(row: dict) -> tuple:
    """Where an entry stands when the snapshot cannot hold them all: more conditioned fixes first, then more
    citations, then by id (the content's hash, independent of the stream's order)."""
    stats = stats_of_row(row)
    return (-int(stats.get("fixes") or 0), -int(stats.get("n_cited") or 0), row["memory_id"])


def row_bytes(row: dict) -> int:
    """The bytes a row takes in the canonical JSONL (its line and the newline)."""
    return len(canonical_rows([row]).encode("utf-8"))


def fit_snapshot(library_text: str, max_bytes: int | None) -> tuple[str, dict]:
    """The package's snapshot of a library (the live library's rows, the ledger's sources and stats merged in): each
    row as snapshot_row, in snapshot_rank order, kept while the canonical JSONL stays within `max_bytes` (None: no
    cap); the first row that does not fit and every row after it are left out, so every kept entry ranks above every
    omitted one. -> (the canonical JSONL, the fit record: entries, kept, bytes, lean_bytes, library_bytes, max_bytes,
    rank, omitted [{memory_id, fixes, n_cited, bytes}], and why when something is omitted)."""
    rows = sorted((snapshot_row(r) for r in parse_jsonl(library_text)), key=snapshot_rank)
    sizes = [row_bytes(r) for r in rows]
    kept, used = 0, 0
    while kept < len(rows) and (max_bytes is None or used + sizes[kept] <= max_bytes):
        used += sizes[kept]
        kept += 1
    text = canonical_rows(rows[:kept])
    omitted = []
    for r, n in zip(rows[kept:], sizes[kept:]):
        stats = stats_of_row(r)
        omitted.append({"memory_id": r["memory_id"], **{k: int(stats.get(k) or 0) for k in RANK_STATS}, "bytes": n})
    fit = {"entries": len(rows), "kept": kept, "bytes": len(text.encode("utf-8")), "lean_bytes": sum(sizes),
           "library_bytes": len((library_text or "").encode("utf-8")), "max_bytes": max_bytes, "rank": RANK,
           "omitted": omitted}
    if omitted:
        fit["why"] = "K's max_memory_bytes"
    return text, fit


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
    """The entries of a package's snapshot, by id: {id, when, content, origin, sources, stats, provenance}. A snapshot
    row (snapshot_row) has no origin (None) and only the credit it is ranked by; an older snapshot's whole rows are
    read the same way."""
    out = []
    for row in parse_jsonl(package.memory_text):
        meta = row.get("metadata") or {}
        out.append({"id": row["memory_id"], "when": row.get("when_to_use") or "", "content": row.get("content") or "",
                    "origin": meta.get("ol_origin"), "sources": sources_of_row(row), "stats": stats_of_row(row),
                    "provenance": {k: v for k, v in meta.items() if k.startswith("ol_")
                                   and k not in ("ol_sources", "ol_stats")}})
    return sorted(out, key=lambda e: e["id"])
