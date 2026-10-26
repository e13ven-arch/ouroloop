"""The memory of one activity process (docs/design/MEMORY.md §4, §7): one sidecar on the activity's store, and, while
the train stream runs, the bridge and its research scope.

The store is `<workspace>/memory/<line>/chroma` (the sidecar holds its lock: one store, one writer); the stream's
ledger, its live library and the bridge's call records sit beside it. A sidecar started for reading only has no
research scope: every model call it would make goes to a closed port. Starting the stream on a reading sidecar
restarts it with the scope (same store, a new token). `stop()` closes the scope, the bridge and the sidecar.
"""
from __future__ import annotations

import threading
from pathlib import Path

from . import digest, memory_venv
from .bridge import RESEARCH_SECONDS, Bridge
from .client import MemoryClient
from .retrieval import LibraryReader
from .settings import settings_of
from .track import StreamStep

LLM_OPS = ("success", "failure", "comparative", "validation")    # sidecar/serve.py: one LLM per ReMe op
SLOT_MARGIN = 30.0         # seconds of the client's read timeout kept free of the wait for a slot and the call


class MemoryService:
    def __init__(self, root, line: str, settings, *, scope_id: str, venv=None, client_factory=None,
                 effort_cap: str | None = None):
        self.root, self.line, self.s = Path(root), line, settings_of(settings)
        self.dir = self.root / "memory" / line
        self.scope_id = scope_id
        self.venv = Path(venv) if venv else None
        self.client_factory = client_factory or MemoryClient
        self.effort_cap = effort_cap
        self.client = None
        self.bridge: Bridge | None = None
        self.scope = None
        self._lock = threading.Lock()

    @property
    def collection(self) -> str:
        """The stream's live collection: ol-live-<8 hex of the activity and the line>."""
        return f"ol-live-{digest({'scope': self.scope_id, 'line': self.line})[:8]}"

    @property
    def streaming(self) -> bool:
        return self.scope is not None and not self.scope.closed

    def start(self, *, research=None, concurrency: int = 1) -> "MemoryService":
        """The sidecar, running; with `research` (the activity's metered research provider, every retry off) also the
        bridge and a research scope the sidecar's ReMe calls through."""
        with self._lock:
            if self.client is not None and (research is None or self.streaming):
                return self
            if self.client is not None:
                self._stop_locked()                 # a reading sidecar: started again with the scope
            self.dir.mkdir(parents=True, exist_ok=True)
            if research is not None:
                self.bridge = Bridge().start()
                self.scope = self.bridge.open_scope(
                    "research", scope_id=f"{self.scope_id}:memory", step=None, provider=research,
                    record=self.dir / "bridge_calls.jsonl", models=[f"research/reme:{op}" for op in LLM_OPS],
                    effort_cap=self.effort_cap, max_concurrency=max(int(concurrency or 1), 1),
                    max_wait=self.s.llm_timeout_s - RESEARCH_SECONDS - SLOT_MARGIN)
            try:
                client = self.client_factory()
                client.start(venv=self.venv or memory_venv(), chroma_path=self.dir / "chroma",
                             settings=self.s.to_json(), llm=self.scope,
                             scope=digest({"root": str(self.root.resolve()), "line": self.line})[:16])
            except BaseException:
                self._stop_locked()
                raise
            self.client = client
            return self

    def stream(self, *, train_texts: dict, forbidden, allowed=(), trajectory_of, activity: str,
               concurrency: int = 1) -> StreamStep:
        """The write path of the stream over the live collection (MEMORY §5); needs a started research scope."""
        if self.client is None or not self.streaming:
            raise RuntimeError("the stream needs the sidecar started with a research scope (start(research=...))")
        return StreamStep(self.client, self.s, ledger_path=self.dir / "stream.json",
                          live_path=self.dir / "live.jsonl", collection=self.collection, business_line=self.line,
                          activity=activity, heldout_domains=self.s.heldout_domains, train_texts=train_texts,
                          forbidden_literals=forbidden, allowed_literals=allowed, trajectory_of=trajectory_of,
                          bridge_calls_of=self.scope.calls, set_step=self.scope.set_step, concurrency=concurrency)

    def supersede(self, entry_id: str, by: str) -> None:
        """A promoted entry whose skill was installed (MEMORY §8): superseded in the stream's ledger, retired from the
        live store and gone from the live library, never shown or promoted again. Needs no research scope."""
        import json
        from .client import canonical_rows, parse_jsonl
        from .promote import mark_superseded
        from .rules import snapshot_rows
        from .track import write_atomic
        path = self.dir / "stream.json"
        ledger = json.loads(path.read_text(encoding="utf-8"))
        entries = ledger.get("entries") or {}
        if (entries.get(entry_id) or {}).get("status") == "superseded":
            return
        mark_superseded(entries, entry_id, by)
        if self.client is None:
            self.start()
        self.client.retire([entry_id], self.collection)
        rows = parse_jsonl(self.client.dump(self.collection))
        write_atomic(self.dir / "live.jsonl", canonical_rows(snapshot_rows(rows, entries)))   # the library first
        write_atomic(path, json.dumps(ledger, indent=1, sort_keys=True, ensure_ascii=False))

    def healthy(self) -> bool:
        """Whether the sidecar answers (a cheap call: its health route)."""
        if self.client is None:
            return False
        try:
            self.client.health()
            return True
        except Exception:  # noqa: BLE001 - any failure to answer is "not healthy"
            return False

    def reader(self, *, snapshot_text: str | None = None, live: dict | None = None) -> LibraryReader:
        if self.client is None:
            self.start()
        return LibraryReader(self.client, self.s, business_line=self.line, snapshot_text=snapshot_text, live=live)

    def _stop_locked(self) -> None:
        if self.scope is not None:
            self.scope.close("memory stopped")
        if self.bridge is not None:
            self.bridge.stop()
        if self.client is not None:
            self.client.stop()
        self.client, self.bridge, self.scope = None, None, None

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
