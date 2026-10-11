"""MemoryClient: the host's handle on the memory sidecar (docs/design/MEMORY.md §2, §4).

httpx and the standard library only. `start` launches sidecar/serve.py with the memory venv's python on a free
127.0.0.1 port, with a clean environment (no proxy variables, no HF token, HF_HUB_OFFLINE=1,
HF_HUB_DISABLE_IMPLICIT_TOKEN=1, ANONYMIZED_TELEMETRY=False) and a working dir outside the repository, and waits for
`/ol/health`, which must report K's pins (memory.backend: lock, flows, ReMe's version; memory.retriever: the model);
a sidecar that fails to start is never left running. Every call carries the sidecar's bearer token; any non-2xx
answer raises MemorySidecarError (contracts: an infrastructure failure), so a ReMe failure is never read as an empty
result. `dump` and `load` check row counts, because ReMe's own dump and load only log their failures. `ensure_snapshot` loads a
snapshot collection once per process however many attempts ask for it at the same time, and a retrieval that names
the count it expects fails on a collection that is missing or holds another count.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx

from ..contracts import MemorySidecarError
from . import COLLECTION_NAME, FLOWS, LOCK, SERVE, cache_root, digest

# the sidecar's marker for a read whose collection is missing or holds another count (sidecar/ops.py SHORT)
SHORT = "OL_COLLECTION_COUNT"
_SNAPSHOT_LOCKS: dict = {}            # (sidecar url, collection) -> lock: one load per snapshot in this process
_SNAPSHOT_LOCKS_LOCK = threading.Lock()

REPO = Path(__file__).resolve().parents[2]
# the variables a sidecar may inherit; everything else (proxies, tokens, API keys, PYTHONPATH) stays behind
ENV_PASS = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "HF_HOME", "HF_HUB_CACHE",
            "XDG_CACHE_HOME")
ENV_SET = {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
           "ANONYMIZED_TELEMETRY": "False", "TOKENIZERS_PARALLELISM": "false", "PYTHONNOUSERSITE": "1",
           "PYTHONUNBUFFERED": "1", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
           "PYTHONDONTWRITEBYTECODE": "1",     # the sidecar runs from the package's own directory: write nothing there
           # no proxy at all: with NO_PROXY=* urllib's getproxies() never falls back to the OS's proxy settings (it
           # does on macOS when no proxy variable is set), and httpx then uses none
           "NO_PROXY": "*", "no_proxy": "*"}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def canonical_rows(rows: list[dict]) -> str:
    """The canonical JSONL of a library: no vectors, rows by memory_id, keys sorted, one compact line each."""
    out = []
    for row in sorted(rows, key=lambda r: r["memory_id"]):
        row = {k: v for k, v in row.items() if k != "vector"}
        out.append(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    return "".join(line + "\n" for line in out)


def parse_jsonl(text: str) -> list[dict]:
    return [json.loads(line) for line in (text or "").splitlines() if line.strip()]


def scope_of(chroma_path) -> str:
    return digest(str(Path(chroma_path).resolve()))[:8]


class MemoryClient:
    """One sidecar process and the calls it serves."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.base_url: str | None = None
        self.workdir: Path | None = None
        self.settings: dict | None = None
        self._token: str | None = None
        self._http: httpx.Client | None = None
        self._local = threading.local()     # one httpx client per calling thread (a batch summarises in threads)
        self._clients: list[httpx.Client] = []
        self._clients_lock = threading.Lock()
        self._start_args: dict | None = None
        self._snapshots: dict = {}          # snapshot collection -> the row count this client has seen loaded

    # ------------------------------------------------------------------------------------------------ process
    def start(self, *, venv, workdir=None, chroma_path, settings: dict, llm=None, lock=LOCK, timeout: float = 180.0,
              scope: str | None = None) -> "MemoryClient":
        """Launch the sidecar. `settings` is K's memory section (settings.Settings or its JSON); `llm` is the bridge's
        research Scope (anything with `.base_url` and `.token`) or None, when every LLM points at a closed port."""
        if self.proc is not None and self.proc.poll() is None:
            raise MemorySidecarError("this client's sidecar is already running")
        from .settings import settings_of
        settings = settings_of(settings).to_json()     # K's table checked and completed (the retriever's pins)
        self._snapshots = {}
        python = Path(venv) / "bin" / "python"
        if not python.exists():
            raise MemorySidecarError(f"{python}: no memory venv there; run `python -m ouroloop.memory setup`")
        chroma_path = Path(chroma_path).resolve()
        workdir = Path(workdir).resolve() if workdir else cache_root() / "memory-run" / (scope or scope_of(chroma_path))
        if workdir == REPO or REPO in workdir.parents:
            raise MemorySidecarError(f"{workdir}: the sidecar's working dir must be outside the repository (ReMe "
                                     f"reads .env files above its working dir when allowed to)")
        workdir.mkdir(parents=True, exist_ok=True)
        self._start_args = dict(venv=venv, workdir=workdir, chroma_path=chroma_path, settings=settings, llm=llm,
                                lock=lock, timeout=timeout, scope=scope)
        self.workdir, self.settings = workdir, settings
        self._token = secrets.token_urlsafe(32)
        env = {k: os.environ[k] for k in ENV_PASS if os.environ.get(k)}
        env.update(ENV_SET)
        env["OL_MEMORY_TOKEN"] = self._token
        env["OL_PARENT_PID"] = str(os.getpid())      # the sidecar exits when this process does
        if llm is not None:
            env["OL_LLM_TOKEN"] = llm.token
        last = ""
        for _ in range(3):   # a port taken between free_port and the bind: try another
            port = free_port()
            cfg = {"port": port, "workdir": str(workdir), "chroma_path": str(chroma_path), "lock": str(lock),
                   "flows": str(FLOWS), "settings": settings,
                   "llm_base_url": None if llm is None else str(llm.base_url)}
            cfg_path = workdir / "sidecar.json"
            cfg_path.write_text(json.dumps(cfg, indent=1, sort_keys=True), encoding="utf-8")
            log = open(workdir / "sidecar.log", "ab")
            self.proc = subprocess.Popen([str(python), str(SERVE), "--config", str(cfg_path)], cwd=str(workdir),
                                         env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         start_new_session=True)
            log.close()
            self.base_url = f"http://127.0.0.1:{port}"
            self._http = httpx.Client(base_url=self.base_url, headers={"Authorization": f"Bearer {self._token}"},
                                      timeout=httpx.Timeout(900.0, connect=5.0), trust_env=False)
            try:
                self._wait_ready(timeout, settings)
                return self
            except BaseException as e:
                self.stop()                            # never leave a sidecar running that did not start right
                last = str(e)
                if not isinstance(e, MemorySidecarError) or "address already in use" not in last.lower():
                    raise
        raise MemorySidecarError(last)

    def _log_tail(self, n: int = 4000) -> str:
        try:
            return (self.workdir / "sidecar.log").read_text(encoding="utf-8", errors="replace")[-n:]
        except OSError:
            return ""

    def _wait_ready(self, timeout: float, settings: dict) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise MemorySidecarError(f"the sidecar exited with {self.proc.returncode} before it served:\n"
                                         f"{self._log_tail()}")
            try:
                r = self._http.get("/ol/health", timeout=2.0)
            except httpx.TransportError:
                time.sleep(0.2)
                continue
            if r.status_code != 200:
                raise MemorySidecarError(f"/ol/health answered {r.status_code}: {r.text[:500]}")
            self._check_health(r.json(), settings)
            return
        raise MemorySidecarError(f"the sidecar did not answer /ol/health within {timeout:.0f} s:\n{self._log_tail()}")

    @staticmethod
    def _check_health(h: dict, settings: dict) -> None:
        """The running sidecar is K's: lock, flows and ReMe's version (memory.backend), the model (memory.retriever)."""
        backend = settings.get("backend") or {}
        want = {"lock_sha256": backend.get("lock_sha256"), "flows_sha256": backend.get("flows_sha256"),
                "reme_version": backend.get("version"), "model_sha256": settings["retriever"]["sha256"]}
        off = {k: (h.get(k), v) for k, v in want.items() if h.get(k) != v}
        if off:
            raise MemorySidecarError("the sidecar is not K's: " + "; ".join(f"{k} {got!r}, K says {v!r}"
                                                                            for k, (got, v) in sorted(off.items())))

    def _close_http(self) -> None:
        with self._clients_lock:
            clients, self._clients = self._clients, []
        for c in clients:
            c.close()
        if self._http is not None:
            self._http.close()
            self._http = None
        self._local = threading.local()

    def _client(self) -> httpx.Client:
        if self._http is None:
            raise MemorySidecarError("the sidecar is not running")
        if threading.current_thread() is threading.main_thread():
            return self._http
        c = getattr(self._local, "client", None)
        if c is None or c.is_closed:
            c = httpx.Client(base_url=self.base_url, headers={"Authorization": f"Bearer {self._token}"},
                             timeout=httpx.Timeout(900.0, connect=5.0), trust_env=False)
            self._local.client = c
            with self._clients_lock:
                self._clients.append(c)
        return c

    def stop(self, timeout: float = 30.0) -> int | None:
        """SIGTERM, so uvicorn's lifespan closes ReMe's application; SIGKILL if it has not exited by `timeout`."""
        code = None
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
                try:
                    self.proc.wait(timeout)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(10)
            code = self.proc.returncode
        self._close_http()
        return code

    def kill(self) -> None:
        """SIGKILL (for crash tests): no shutdown of any kind."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        self._close_http()

    def restart(self) -> "MemoryClient":
        """Stop (if running) and start again with the same arguments: same store, a new token."""
        if self.proc is not None and self.proc.poll() is None:
            self.stop()
        args = dict(self._start_args or {})
        if not args:
            raise MemorySidecarError("restart before start")
        self.proc = None
        return self.start(**args)

    @classmethod
    def attach(cls, connection: dict) -> "MemoryClient":
        """A client of a sidecar another process of this host started (its `connection()`); it does not own the
        process, so stop() only closes this client's connections."""
        c = cls()
        c.base_url, c._token = connection["base_url"], connection["token"]
        c.workdir, c.settings = Path(connection["workdir"]), connection.get("settings")
        c._http = httpx.Client(base_url=c.base_url, headers={"Authorization": f"Bearer {c._token}"},
                               timeout=httpx.Timeout(900.0, connect=5.0), trust_env=False)
        c.health()
        return c

    def connection(self) -> dict:
        """What attach() needs, for another process of this host (the token included: keep it off disk and logs)."""
        return {"base_url": self.base_url, "token": self._token, "workdir": str(self.workdir),
                "settings": self.settings}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    # ------------------------------------------------------------------------------------------------ calls
    def _call(self, method: str, path: str, body: dict | None = None, *, timeout=None) -> dict:
        try:
            r = self._client().request(method, path, json=body, **({"timeout": timeout} if timeout else {}))
        except httpx.TransportError as e:
            raise MemorySidecarError(f"{method} {path}: {type(e).__name__}: {e}") from e
        if not 200 <= r.status_code < 300:
            raise MemorySidecarError(f"{method} {path}: HTTP {r.status_code}: {r.text[:2000]}")
        data = r.json()
        if isinstance(data, dict) and data.get("success") is False:   # belt and braces over the sidecar's own rule
            raise MemorySidecarError(f"{method} {path}: success=false: {str(data.get('answer'))[:2000]}")
        return data

    def flow(self, name: str, **params) -> dict:
        coll = params.get("collection")
        if coll is not None and (not isinstance(coll, str) or not COLLECTION_NAME.fullmatch(coll)
                                 or coll.startswith("ol-sys-")):
            raise ValueError(f"collection {coll!r}: a chroma name ({COLLECTION_NAME.pattern}), not ol-sys-*")
        return self._call("POST", f"/{name}", params)

    def health(self) -> dict:
        return self._call("GET", "/ol/health")

    def collections(self) -> dict:
        return dict(self.health().get("collections") or {})

    def count(self, collection: str) -> int:
        return int(self.collections().get(collection, 0))

    def summarize(self, trajectories: list[dict]) -> dict:
        """ReMe's summary flow over these trajectories (MEMORY §5 step 2): {memory_list (after the batch dedup),
        validated (how many passed validation, before the dedup), replies (each LLM call the flow made: op, the
        bridge's call_id, done, json state), unparsed (replies ReMe could not read)}. The LLM calls go to the
        research scope the sidecar was started with; the caller checks the replies against the bridge's record."""
        # no read timeout: the flow chains research calls that each wait for a bridge slot and run under the
        # provider's own limit, so the host's 900 s would give up on a summary the sidecar still pays for; the sidecar
        # ends with the host (its parent watch)
        data = self._call("POST", "/summary_task_memory_v6", {"trajectories": trajectories},
                          timeout=httpx.Timeout(None, connect=5.0))
        meta = data.get("metadata") or {}
        try:
            validated = len(json.loads(data.get("answer") or "[]"))
        except ValueError:
            validated = None
        return {"memory_list": list(meta.get("memory_list") or []), "validated": validated,
                "replies": list(meta.get("ol_replies") or []), "unparsed": list(meta.get("ol_unparsed") or [])}

    def get(self, ids: list[str], collection: str) -> list[dict]:
        if not ids:
            return []
        return list(self.flow("get_task_memory_v6", collection=collection, ids=list(ids))["metadata"]["memory_list"])

    def add(self, memories: list[dict], collection: str) -> int:
        """Insert memories; the collection must grow by exactly their number (chroma keeps the first copy of an id
        and ignores another add of it without an error). -> the new count."""
        if not memories:
            return self.count(collection)
        ids = [m["memory_id"] for m in memories]
        if len(set(ids)) != len(ids):
            raise MemorySidecarError(f"add: duplicate ids in one call {sorted(i for i in ids if ids.count(i) > 1)}")
        meta = self.flow("add_task_memory_v6", collection=collection, memory_list=memories)["metadata"]
        if int(meta["count"]) - int(meta["count_before"]) != len(memories):
            raise MemorySidecarError(f"add: {len(memories)} memories sent, the collection grew from "
                                     f"{meta['count_before']} to {meta['count']} (an id was already there)")
        return int(meta["count"])

    def retrieve(self, query: str, collection: str, *, where: dict | None = None, top_k: int = 5,
                 query_mode: bool = True, expected_count: int | None = None) -> list[dict]:
        """The top_k memories by similarity, `where` filtering on metadata. With `expected_count` the sidecar fails
        the call (MemorySidecarError with SHORT in it) when the collection is missing or holds another count."""
        body = {"collection": collection, "query": query, "top_k": int(top_k), "query_mode": bool(query_mode)}
        if where:
            body["where"] = dict(where)
        if expected_count is not None:
            body["expected_count"] = int(expected_count)
        return list(self.flow("retrieve_task_memory_v6", **body)["metadata"]["memory_list"])

    # ------------------------------------------------------------------------------------------------ snapshots
    def _snapshot_lock(self, collection: str) -> threading.Lock:
        with _SNAPSHOT_LOCKS_LOCK:
            return _SNAPSHOT_LOCKS.setdefault((self.base_url, collection), threading.Lock())

    def ensure_snapshot(self, snapshot_text: str, collection: str, *, rows: int | None = None) -> int:
        """The snapshot loaded into its collection, once: concurrent callers in this process wait for the one load
        (the collection is named by the snapshot and the retriever, so a complete one is never loaded again); a
        collection holding another count is reloaded. The sidecar's read lock keeps other processes' reads from
        seeing a load half done. -> the row count."""
        n = len(parse_jsonl(snapshot_text)) if rows is None else int(rows)
        with self._snapshot_lock(collection):
            if self._snapshots.get(collection) == n:
                return n
            if self.count(collection) != n:
                self.load(snapshot_text, collection, clear=True)
            self._snapshots[collection] = n
            return n

    def forget_snapshot(self, collection: str) -> None:
        """This client stops taking the collection as loaded (the sidecar dropped it, or it holds another count)."""
        with self._snapshot_lock(collection):
            self._snapshots.pop(collection, None)

    def retire(self, ids: list[str], collection: str) -> list[str]:
        if not ids:
            return []
        return list(self.flow("retire_task_memory_v6", collection=collection, ids=list(ids))["metadata"]["retired"])

    def dump(self, collection: str) -> str:
        """The collection as canonical JSONL (canonical_rows), checked against the collection's count."""
        path = self.workdir / "dumps" / f"{collection}-{uuid.uuid4().hex}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            meta = self.flow("dump_memory_v6", collection=collection, dump_file_path=str(path))["metadata"]
            rows = parse_jsonl(path.read_text(encoding="utf-8"))
        finally:
            path.unlink(missing_ok=True)
        ids = [r.get("memory_id") for r in rows]
        if len(rows) != int(meta["count"]) or len(set(ids)) != len(ids):
            raise MemorySidecarError(f"dump {collection}: {len(rows)} rows ({len(set(ids))} ids) for a collection "
                                     f"of {meta['count']} (ReMe skips a row it cannot convert and only logs it)")
        return canonical_rows(rows)

    def load(self, jsonl: str, collection: str, *, clear: bool = True) -> int:
        """Load canonical JSONL into a collection (emptied first unless clear=False); the collection must then hold
        every row (ReMe skips a line it cannot parse and only logs it). -> the count."""
        rows = parse_jsonl(jsonl)
        before = 0 if clear else self.count(collection)
        path = self.workdir / "dumps" / f"load-{collection}-{uuid.uuid4().hex}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_rows(rows), encoding="utf-8")
        try:
            meta = self.flow("load_memory_v6", collection=collection, load_file_path=str(path),
                             clear_existing=bool(clear))["metadata"]
        finally:
            path.unlink(missing_ok=True)
        if int(meta["count"]) != before + len(rows):
            raise MemorySidecarError(f"load {collection}: {len(rows)} rows into {before}, the collection holds "
                                     f"{meta['count']}")
        return int(meta["count"])

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        """bge-small unit vectors (the query prefix applied when `query`)."""
        if not texts:
            return []
        data = self._call("POST", "/v1/embeddings", {"input": list(texts), "ol_query": bool(query)})
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]

    def embed_info(self, texts: list[str], *, query: bool = False) -> dict:
        return self._call("POST", "/v1/embeddings", {"input": list(texts), "ol_query": bool(query)})
