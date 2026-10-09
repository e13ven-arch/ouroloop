"""Cross-business-line import (docs/design/MEMORY.md §4).

One platform serves many business lines; a library released on line A can seed line B. The import is an input K
declares (`memory.imports = [{release_ref, snapshot_digest}]`), never a side command: the stream of B calls it at
its start, records it in its ledger and so in its identity, and B's snapshot still passes B's own gate.

`import_entries` copies the rows of A's released snapshot into B's live collection: `ol_line` becomes B (retrieval
on B filters on it), `ol_source_line` stays A, `ol_origin` becomes `imported` and `ol_imported_from` names A's
release and the row's id; A's sources and stats are not carried (they are A's train tasks and A's credit), so every
imported entry starts B's probation from zero and is credited on B's train only. Of a row's metadata only the
provenance keys and ReMe's kept keys (rules.reme_metadata) are copied. A held-out source line, or any row that is not
train experience, refuses the whole import; `fix` (StreamStep's: B's lengths and literal check) drops or fixes a row.
"""
from __future__ import annotations

import json

from ..contracts import TrainOnlyError
from .client import canonical_rows, parse_jsonl
from .rules import IMPORTED, PROVENANCE_KEYS, reme_metadata, require_train


def imported_row(row: dict, *, source_release_ref: str, target_line: str) -> dict:
    src = row.get("metadata") or {}
    meta = {**reme_metadata(src), **{k: src[k] for k in PROVENANCE_KEYS if k in src}}
    meta.update(ol_line=target_line, ol_source_line=meta.get("ol_source_line") or meta.get("ol_line") or "",
                ol_origin=IMPORTED, ol_imported_from=f"{source_release_ref}:{row['memory_id']}")
    return {**{k: v for k, v in row.items() if k != "vector"}, "metadata": meta}


def import_entries(snapshot_text: str, *, source_release_ref: str, source_line: str, target_line: str, client,
                   collection: str, heldout_domains, fix=None) -> dict:
    """Copy a released snapshot of `source_line` into `collection` as `target_line` entries. -> {added: [ids],
    skipped: [ids already in the collection], dropped: [{memory_id, why}] (by `fix`), rows: [the rows added]}.
    Refuses (TrainOnlyError) a held-out source line, a row of another line or split, and refuses (ValueError) an
    import of a line into itself."""
    if source_line == target_line:
        raise ValueError(f"an import copies another line's library; {source_line!r} is the target itself")
    require_train("train", source_line, heldout_domains)
    rows = parse_jsonl(snapshot_text)
    for row in rows:
        meta = row.get("metadata") or {}
        if meta.get("ol_split") != "train":
            raise TrainOnlyError(f"row {row.get('memory_id')}: ol_split {meta.get('ol_split')!r}; only train "
                                 f"experience is imported")
        origin_line = meta.get("ol_source_line") or meta.get("ol_line")
        if meta.get("ol_line") != source_line:
            raise TrainOnlyError(f"row {row.get('memory_id')}: ol_line {meta.get('ol_line')!r} in a snapshot of "
                                 f"{source_line!r}")
        require_train("train", origin_line, heldout_domains)
    candidates, dropped = [], []
    for r in rows:
        row = imported_row(r, source_release_ref=source_release_ref, target_line=target_line)
        if fix is not None:
            row, why = fix(row)
            if row is None:
                dropped.append({"memory_id": r["memory_id"], "why": why})
                continue
        candidates.append(row)
    present = {m["memory_id"] for m in client.get([r["memory_id"] for r in candidates], collection)}
    new, seen = [], set()
    for row in candidates:              # a fix can make two rows one content: the first is kept
        if row["memory_id"] not in present and row["memory_id"] not in seen:
            seen.add(row["memory_id"])
            new.append(row)
    if new:
        client.add(new, collection)
    return {"added": sorted(r["memory_id"] for r in new), "skipped": sorted(present), "dropped": dropped,
            "rows": json.loads(json.dumps(new)), "text": canonical_rows(new)}
