"""The read path of an attempt (docs/design/MEMORY.md §6): the opening block and recall, through one reader.

Two sources of entries. A frozen snapshot (a package's memory/snapshot.jsonl: screen, dev, test, the App) is read from
its own collection, `ol-snap-<16 hex of the snapshot and the retriever digest>`, so one snapshot under one retriever
is one collection and a cached collection can never serve another snapshot. It is loaded on a miss once per process
however many attempts start at the same time (MemoryClient.ensure_snapshot), and every read names the snapshot's row
count: a collection that is missing or holds another count fails the read, which is retried once after a reload,
never answered with an empty library. The live library (during the train stream) is read from the stream's collection
with the ledger's source lists and count (StreamStep.live_view()).

The query is the task text (with the retriever's query prefix, applied in the sidecar) for the opening, the agent's
own words for recall; the filter is this business line's train entries; shown_k * fetch_factor are fetched so that
leave-one-out (entries whose sources hold this task — the platform's side, since chroma cannot filter on membership in
a JSON string) and min_score can still leave enough, and the fetch is widened while they do not and more entries above
min_score may follow. Leave-one-out is always on: dev and test tasks are never sources, so there it excludes nothing.

The agent sees every entry by its first 8 hex (`[lib <id8>]` in the block, `id` from recall) and is asked to end the
finish tool's reason with a `library-used:` line, which rules.parse_citation reads back for the credit.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable

from ..contracts import MemorySidecarError
from . import retriever_digest, snapshot_digest
from .client import SHORT, parse_jsonl
from .rules import sources_of_row
from .settings import settings_of

INTRO = ("Notes from verified earlier work on similar tasks (the framework's experience library). Use one only where "
         "its situation matches this task; the task and its data decide.")
CITE = ("When you call the finish tool, end its `reason` with one line `library-used: <id8,…>` listing the entries "
        "above that you used, or `library-used: none`.")


def snapshot_collection(snapshot_text: str, retriever: dict) -> str:
    key = f"{snapshot_text}\n{retriever_digest(retriever)}".encode("utf-8")
    return "ol-snap-" + hashlib.sha256(key).hexdigest()[:16]


def format_block(entries: list[dict], *, cite: bool) -> str:
    parts = [INTRO]
    for m in entries:
        parts.append(f"[lib {m['memory_id'][:8]}] When: {m['when_to_use'].strip()}\n{m['content'].strip()}")
    if cite:
        parts.append(CITE)
    return "\n\n".join(parts)


@dataclass
class TaskMemory:
    """What one attempt gets of the memory: the text appended to its system prompt, the `library_retrieval` line of
    the start (None when nothing was retrieved), and recall for the agent's own queries under the same rules."""
    opening: str = ""
    event: dict | None = None
    recaller: Callable | None = field(default=None, repr=False)

    def recall(self, query, k=None) -> list[dict]:
        return list(self.recaller(str(query or ""), k)) if self.recaller is not None else []


class LibraryReader:
    """Retrieval over one library (a frozen snapshot or the stream's live collection) for one business line."""

    def __init__(self, client, settings, *, business_line: str, snapshot_text: str | None = None,
                 live: dict | None = None):
        if (snapshot_text is None) == (live is None):
            raise ValueError("LibraryReader: give exactly one of snapshot_text and live")
        self.client, self.s, self.line = client, settings_of(settings), business_line
        self.snapshot_text = snapshot_text
        if snapshot_text is not None:
            rows = parse_jsonl(snapshot_text)
            self.collection = snapshot_collection(snapshot_text, self.s.retriever)
            self.sources = {r["memory_id"]: set(sources_of_row(r)) for r in rows}
            self.snapshot = snapshot_digest(snapshot_text) if rows else None
        else:
            self.collection = live["collection"]
            self.sources = {k: set(v or ()) for k, v in (live.get("sources") or {}).items()}
            self.snapshot = None
        self.count = len(self.sources)
        self.where = {"ol_line": business_line, "ol_split": "train"}

    @property
    def empty(self) -> bool:
        return not self.sources

    def search(self, query: str, task_hash: str, k: int) -> tuple[list[dict], dict]:
        """The top k entries for a query with this task's own sources left out, at min_score or above. -> (entries,
        what the search did: fetched, fetches, excluded_as_source)."""
        if self.empty or not query.strip():
            return [], {"fetched": 0, "fetches": 0, "excluded_as_source": 0}
        if self.snapshot_text is not None:
            self.client.ensure_snapshot(self.snapshot_text, self.collection, rows=self.count)
        want = max(int(k), 1)
        n, fetches = want * self.s.fetch_factor, 0
        while True:
            fetched = self._fetch(query, n)
            fetches += 1
            excluded, kept = 0, []
            for m in fetched:
                if task_hash in self.sources.get(m["memory_id"], ()):
                    excluded += 1
                    continue
                if float(m["metadata"].get("retrieval_score", 0.0)) >= self.s.min_score:
                    kept.append(m)
            exhausted = len(fetched) < n or n >= self.count
            below = bool(fetched) and float(fetched[-1]["metadata"].get("retrieval_score", 0.0)) < self.s.min_score
            if len(kept) >= want or exhausted or below:
                break
            n = min(n * 2, self.count)        # leave-one-out used the fetch up: look further down
        return kept[:want], {"fetched": len(fetched), "fetches": fetches, "excluded_as_source": excluded}

    def _fetch(self, query: str, k: int) -> list[dict]:
        """One read naming the count the collection must hold; a snapshot collection found missing or short (dropped
        by the sidecar's LRU, or another process's reload) is loaded again and read once more."""
        try:
            return self.client.retrieve(query, self.collection, where=self.where, top_k=k, query_mode=True,
                                        expected_count=self.count)
        except MemorySidecarError as e:
            if self.snapshot_text is None or SHORT not in str(e):
                raise
        self.client.forget_snapshot(self.collection)
        self.client.ensure_snapshot(self.snapshot_text, self.collection, rows=self.count)
        return self.client.retrieve(query, self.collection, where=self.where, top_k=k, query_mode=True,
                                    expected_count=self.count)

    def for_task(self, task_text: str, task_hash: str, *, note: str = "") -> TaskMemory:
        """The memory of one attempt: the opening block (and `note`, the stream's retry note, after it), its start
        line, and recall. An empty library gives no block and no line, so the prompt is what it was without one."""
        kept, how = self.search(task_text, task_hash, self.s.shown_k)
        block = format_block(kept, cite=bool(self.s.cite)) if kept else ""
        opening = "\n\n".join(p for p in (block, note) if p)
        event = None
        if not self.empty:
            event = {"query": task_text[:500], "shown": [m["memory_id"] for m in kept],
                     "scores": [round(float(m["metadata"]["retrieval_score"]), 4) for m in kept],
                     "retriever": retriever_digest(self.s.retriever), "snapshot": self.snapshot,
                     "collection": self.collection, "filters": dict(self.where), "leave_one_out": True,
                     "library_size": self.count, "min_score": self.s.min_score, "top_k": self.s.shown_k,
                     "task_hash": task_hash, "chars": len(block), **how}

        def recall(query: str, k=None) -> list[dict]:
            k = self.s.recall_k if not isinstance(k, int) or k <= 0 else min(k, self.s.recall_k)
            found, _ = self.search(query, task_hash, k)
            return [{"id": m["memory_id"][:8], "when": m["when_to_use"].strip(), "content": m["content"].strip(),
                     "score": round(float(m["metadata"]["retrieval_score"]), 4)} for m in found]
        return TaskMemory(opening=opening, event=event, recaller=recall)
