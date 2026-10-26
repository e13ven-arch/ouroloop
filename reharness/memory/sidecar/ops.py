"""Platform ops registered into ReMe 0.3.1.10's op registry (docs/design/MEMORY.md).

They run inside the sidecar's ReMe process, depend on ReMe's public classes and registry only, and import nothing of
reharness (§1.3 rule 2). ReMe's own ops keep their logic: the subclasses only bind them to a per-request chroma
collection (a name chroma accepts, never the sidecar's own ol-sys-*), read the request's filter and keep the
similarity. A request without a field a flow declares required is a 422 (the flow's parameters); a field ReMe's op
finds empty (a dump without a path) it logs as an error and answers success, which serve.py's flow guard turns into a
failed call.

Every op on a collection holds that collection's lock for its whole run, the store's lookup included: a write (add,
load, retire) alone, reads (retrieve, get, dump) together. So no read sees a load half done (a load empties the
collection first), and a read that names the row count it expects (`expected_count`, the snapshot's or the ledger's)
fails when the collection is missing or holds another count, instead of answering an empty or partial library.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import time

from reme.core.op import BaseOp
from reme.core.registry_factory import R
from reme.core.schema.memory_node import MemoryNode
from reme.core.vector_store.chroma_vector_store import ChromaVectorStore
from reme.extension.procedural_memory import (DumpMemory, LoadMemory, MemoryAddition, MemoryDeduplication,
                                              MemoryRetrieval)

from embed import engine  # the sidecar's own module: serve.py runs with this directory on sys.path

# chroma 1.5.9's collection-name rule (a bad name is an error there, so it is one here first)
COLLECTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,510}[A-Za-z0-9]$")
SNAPSHOT_PREFIX = "ol-snap-"
SYSTEM_PREFIX = "ol-sys-"
MAX_SNAPSHOTS = 8


# the marker of a read whose collection is missing or holds another count than the caller expects; the host matches
# it to reload a snapshot collection once (reharness/memory/retrieval.py)
SHORT = "OL_COLLECTION_COUNT"


class RWLock:
    """Readers together or one writer, on the sidecar's event loop; a waiting writer goes before new readers."""

    def __init__(self):
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting = 0

    @contextlib.asynccontextmanager
    async def read(self):
        async with self._cond:
            await self._cond.wait_for(lambda: not self._writer and not self._waiting)
            self._readers += 1
        try:
            yield
        finally:
            async with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    @contextlib.asynccontextmanager
    async def write(self):
        async with self._cond:
            self._waiting += 1
            try:
                await self._cond.wait_for(lambda: not self._writer and not self._readers)
            finally:
                self._waiting -= 1
            self._writer = True
        try:
            yield
        finally:
            async with self._cond:
                self._writer = False
                self._cond.notify_all()


class Shared:
    """The process's one chroma client and the stores, locks and last uses of its collections (set by serve.py)."""
    client = None
    chroma_path = None
    stores: dict = {}
    locks: dict = {}
    last_used: dict = {}

    @classmethod
    def exists(cls, name: str) -> bool:
        try:
            cls.client.get_collection(name)
            return True
        except Exception:  # noqa: BLE001 - chroma raises NotFoundError/ValueError for a missing collection
            return False

    @classmethod
    def names(cls) -> list[str]:
        return sorted(c if isinstance(c, str) else c.name for c in cls.client.list_collections())

    @classmethod
    def lock(cls, name: str) -> RWLock:
        return cls.locks.setdefault(name, RWLock())

    @classmethod
    def forget(cls, name: str) -> None:
        cls.stores.pop(name, None)
        cls.last_used.pop(name, None)


class SharedChromaVectorStore(ChromaVectorStore):
    """ReMe's ChromaVectorStore on the process's one PersistentClient (the `default` store the dedup op embeds
    through; no memory is ever written to it)."""

    def __init__(self, **kwargs):
        kwargs["client"] = Shared.client
        super().__init__(**kwargs)


R.vector_stores.register("ol_chroma")(SharedChromaVectorStore)


class CollectionMixin:
    """Binds a ReMe op to the chroma collection the request names (`collection`), one ChromaVectorStore per name on
    the shared client and embedding model, under the collection's lock (see the module docstring). A read of a
    collection that does not exist reads an empty one (and does not create it) unless it names `expected_count`;
    snapshot collections beyond MAX_SNAPSHOTS go least recently used first, each under its own lock."""
    creates = False      # a write op creates its collection
    locked = False       # a write op holds the collection's lock alone

    def _name(self) -> str:
        name = self.context.get("collection")
        if not isinstance(name, str) or not COLLECTION_NAME.fullmatch(name) or name.startswith(SYSTEM_PREFIX):
            raise ValueError(f"collection {name!r}: a name matching {COLLECTION_NAME.pattern}, not {SYSTEM_PREFIX}*")
        return name

    async def before_execute(self):
        await super().before_execute()
        self._collection = self._name()
        self._store, self._created = None, False

    async def _resolve(self):
        name = self._collection
        Shared.last_used[name] = time.monotonic()
        store = Shared.stores.get(name)
        if store is not None and not Shared.exists(name):      # dropped (snapshot LRU) since it was opened
            Shared.forget(name)
            store = None
        if store is None and (self.creates or Shared.exists(name)):
            store = ChromaVectorStore(collection_name=name, db_path=Shared.chroma_path,
                                      embedding_model=self.service_context.embedding_models["default"],
                                      client=Shared.client)
            await store.start()
            Shared.stores[name] = store
            self._created = name.startswith(SNAPSHOT_PREFIX)
        Shared.last_used[name] = time.monotonic()
        return store

    async def _prune_snapshots(self, keep: str) -> None:
        snaps = [n for n in Shared.names() if n.startswith(SNAPSHOT_PREFIX) and n != keep]
        snaps.sort(key=lambda n: (Shared.last_used.get(n, 0.0), n))
        for name in snaps[:max(0, len(snaps) + 1 - MAX_SNAPSHOTS)]:
            async with Shared.lock(name).write():     # one lock at a time: two loads never wait on each other
                if Shared.exists(name):
                    Shared.client.delete_collection(name)
                Shared.forget(name)

    @property
    def vector_store(self):
        return self._store

    async def execute(self):
        lock = Shared.lock(self._collection)
        async with (lock.write() if self.locked else lock.read()):
            self._store = await self._resolve()
            await self._expect()
            out = await self._execute()
            self.context.response.metadata["count"] = await self._count()
        if self._created:
            await self._prune_snapshots(keep=self._collection)
        return out

    async def _expect(self) -> None:
        want = self.context.get("expected_count")
        if want is None:
            return
        have = None if self._store is None else await self._store.count()
        if have != int(want):
            raise RuntimeError(f"{SHORT}: collection {self._collection} "
                               f"{'does not exist' if have is None else f'holds {have} rows'}; the caller expects "
                               f"{int(want)}")

    async def _execute(self):
        return await super().execute()

    async def _count(self) -> int:
        return 0 if self._store is None else await self._store.count()


class OlMemoryAddition(CollectionMixin, MemoryAddition):
    creates, locked = True, True

    async def _execute(self):
        self.context.response.metadata["count_before"] = await self._count()
        return await super()._execute()


class OlDumpMemory(CollectionMixin, DumpMemory):
    async def _execute(self):
        if self._store is None:  # an empty library: an empty file, as ReMe writes for an empty store
            with open(self.context.dump_file_path, "w", encoding="utf-8"):
                pass
            return None
        return await super()._execute()


class OlLoadMemory(CollectionMixin, LoadMemory):
    creates, locked = True, True


class OlFilteredMemoryRetrieval(CollectionMixin, MemoryRetrieval):
    """ReMe's MemoryRetrieval with the request's `where` in place of its `filters=None` (memory_retrieval.py:43-47)
    and the similarity kept in metadata["retrieval_score"] (chroma overwrites `score`, §5.7 row 2). With `query_mode`
    (default) the query carries the retriever's query prefix; a dedup lookup compares like with like and sets it
    false. The rest of the body is ReMe's."""
    async def _execute(self):
        if self._store is None:
            self.context.response.metadata["memory_list"] = []
            return None
        top_k = int(self.context.get("top_k", 5))
        query = self.context.get("query", "")
        assert query, "query should be not empty!"
        if self.context.get("query_mode", True):
            query = engine().as_query(query)
        nodes = await self.vector_store.search(query=query, limit=top_k, filters=self.context.get("where") or None)
        memory_list, seen = [], set()
        for node in nodes:
            memory = MemoryNode.from_vector_node(node)
            if memory.content not in seen:
                memory.metadata["retrieval_score"] = memory.score
                memory_list.append(memory)
                seen.add(memory.content)
        self.context.response.metadata["memory_list"] = memory_list


class OlGetById(CollectionMixin, BaseOp):
    """The stored memories with these ids (ChromaVectorStore.get), vectors left out: the platform's pre-write
    check, since chroma keeps the first copy of an id and ignores a second add silently (§5.7 row 12)."""
    async def _execute(self):
        ids = list(self.context.ids)
        nodes = [] if self._store is None or not ids else await self.vector_store.get(ids)
        out = []
        for node in nodes:
            memory = MemoryNode.from_vector_node(node)
            memory.vector = None
            out.append(memory)
        self.context.response.metadata["memory_list"] = out


class OlRetireById(CollectionMixin, BaseOp):
    """Delete these ids from the collection (ChromaVectorStore.delete); reports which of them were there."""
    locked = True

    async def _execute(self):
        ids = list(self.context.ids)
        if self._store is None or not ids:
            self.context.response.metadata["retired"] = []
            return None
        present = [n.vector_id for n in await self.vector_store.get(ids)]
        if present:
            await self.vector_store.delete(present)
        self.context.response.metadata["retired"] = sorted(present)


class BatchOnlyDeduplication(MemoryDeduplication):
    """ReMe's deduplication within the batch only. The comparison against the stored library embeds two different
    texts (stored `when_to_use` vs new `when_to_use + content`) and skips itself silently on a store error
    (memory_deduplication.py:85-117); the platform does that comparison in one space instead (§5.7 row 1)."""

    async def _get_existing_task_memory_embeddings(self):
        return []


for _cls in (OlMemoryAddition, OlDumpMemory, OlLoadMemory, OlFilteredMemoryRetrieval, OlGetById, OlRetireById,
             BatchOnlyDeduplication):
    R.ops.register()(_cls)
