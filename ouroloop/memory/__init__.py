"""The memory component (docs/design/MEMORY.md; DESIGN §5): ReMe 0.3.1.10's task memory in a sidecar, the platform's
rules over it, the write path of the train stream, retrieval for an attempt, the snapshot as a candidate, promotion.

The host side (this package except `sidecar/`) uses httpx and the standard library only. It never imports ReMe,
chromadb, onnxruntime or agentscope: those live in the sidecar's own venv, built from the hashed lock by
`python -m ouroloop.memory setup`, and are reached over HTTP. The sidecar's own files (`sidecar/`) run inside that
venv and import nothing of ouroloop. Of the framework this package imports contracts, harness and runtime only
(CONTRACTS §1).

- `settings.Settings`: K's memory section.
- `client.MemoryClient`: start, stop and call the sidecar (ReMe's own HttpService with our ops, chroma, bge-small).
- `bridge.Bridge`: the OpenAI-compatible bridge the sidecar's ReMe calls the metered research model through.
- `rules`: the platform rules (provenance, train-only, literals, citation credit, retirement, capacity, readout,
  promotion).
- `track.StreamStep`: the write path of the stream, checkpointed and resumable.
- `retrieval.LibraryReader`: the read path of an attempt (the opening block and recall).
- `service.MemoryService`: the sidecar, the bridge and the research scope of one activity process.
- `snapshot`, `promote`, `importing`: the snapshot candidate, promotion, cross-line import.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

SIDECAR_DIR = Path(__file__).resolve().parent / "sidecar"
SERVE = SIDECAR_DIR / "serve.py"
FLOWS = SIDECAR_DIR / "flows.yaml"
LOCK = SIDECAR_DIR / "requirements-macos-arm64.lock"

# chroma 1.5.9's collection-name rule; sidecar/ops.py holds the same pattern (it cannot import this package) and a test
# pins the two equal. The live library of an activity is ol-live-<scope8>, a snapshot ol-snap-<16 hex>.
COLLECTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,510}[A-Za-z0-9]$")

REME_VERSION = "0.3.1.10"
REME_WHEEL_SHA256 = "53ec84033fd72ec0151a7dd25b0f52f1c4e708f9a07c80427e13006634b1e210"

# BAAI/bge-small-en-v1.5 (MIT) at a pinned revision. The model file's sha256 is K's (memory.retriever.sha256); the
# other files the embedder reads are pinned as well, since a different tokenizer or pooling config changes every
# vector silently.
MODEL_REPO = "BAAI/bge-small-en-v1.5"
MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
MODEL_FILE = "onnx/model.onnx"
MODEL_SHA256 = "828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35"
MODEL_AUX_SHA256 = {
    "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    "config.json": "094f8e891b932f2000c92cfc663bac4c62069f5d8af5b5278c4306aef3084750",
    "1_Pooling/config.json": "d1caf60c96f5fba2157c0c26b76d80818fad6cf0b8eb5e73ec372ff9818eba5c",
}


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest(obj) -> str:
    """The sha256 of an object's canonical JSON (sorted keys, no whitespace)."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def snapshot_digest(snapshot_text: str) -> str:
    """16 hex of the sha256 of a library snapshot's bytes (the package's memory/snapshot.jsonl as UTF-8). The one
    definition: K's memory.imports[].snapshot_digest, the stream's ledger and the library_retrieval line's
    `snapshot` all use it."""
    return hashlib.sha256((snapshot_text or "").encode("utf-8")).hexdigest()[:16]


def lock_sha256(lock: str | Path = LOCK) -> str:
    return sha256_file(lock)


def flows_sha256() -> str:
    return sha256_file(FLOWS)


def cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ouroloop"


def default_venv(lock: str | Path = LOCK) -> Path:
    """~/.cache/ouroloop/memory-venv/<lock sha16>/: a new lock gets a new venv, an old one is never patched."""
    return cache_root() / "memory-venv" / lock_sha256(lock)[:16]


def memory_venv() -> Path:
    """The sidecar's venv on this machine: OUROLOOP_MEMORY_VENV, else the default for the lock. A machine setting, not
    K: K pins the lock's and the model's hashes, and the sidecar refuses a venv that is not the lock."""
    path = os.environ.get("OUROLOOP_MEMORY_VENV")
    return Path(path).expanduser() if path else default_venv()


def hf_hub_cache() -> Path:
    """The Hugging Face hub cache the sidecar reads the model from (huggingface_hub's own resolution order)."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "huggingface" / "hub"


def model_snapshot(repo: str = MODEL_REPO, revision: str = MODEL_REVISION) -> Path:
    return hf_hub_cache() / ("models--" + repo.replace("/", "--")) / "snapshots" / revision


def model_problems(retriever: dict | None = None) -> list[str]:
    """What is wrong with the cached model against the pins (empty when it is the pinned model)."""
    r = retriever or {}
    snap = model_snapshot(r.get("model", MODEL_REPO), r.get("revision", MODEL_REVISION))
    want = {r.get("file", MODEL_FILE): r.get("sha256", MODEL_SHA256), **(r.get("aux_sha256") or MODEL_AUX_SHA256)}
    out = []
    for rel, sha in sorted(want.items()):
        path = snap / rel
        if not path.is_file():
            out.append(f"{path}: missing (download {r.get('model', MODEL_REPO)}@{r.get('revision', MODEL_REVISION)} "
                       f"into the Hugging Face cache: python -m ouroloop.memory setup)")
            continue
        got = sha256_file(path)
        if got != sha:
            out.append(f"{path}: sha256 {got}, pinned {sha}")
    return out


def default_retriever() -> dict:
    """K's memory.retriever table: the pinned model and how a vector is made from it."""
    return {"model": MODEL_REPO, "revision": MODEL_REVISION, "file": MODEL_FILE, "sha256": MODEL_SHA256,
            "aux_sha256": dict(MODEL_AUX_SHA256), "dims": 384, "pooling": "cls", "normalize": True,
            "max_tokens": 512, "query_prefix": ""}


def complete_retriever(table: dict) -> dict:
    """K's retriever table with every file the embedder reads pinned. A table written without `aux_sha256` is
    completed with the built-in pins only when it names the pinned model file itself; for any other model the table
    must pin the tokenizer and both configs, since a different one changes every vector without changing the model
    file's sha. -> the completed table (ValueError otherwise)."""
    table = dict(table or {})
    out = {**default_retriever(), **table}
    pinned = (out["model"], out["revision"], out["file"], out["sha256"]) == (MODEL_REPO, MODEL_REVISION, MODEL_FILE,
                                                                             MODEL_SHA256)
    aux = dict(table.get("aux_sha256") or {})       # never the default's: another model's files are not these
    if not aux and pinned:
        aux = dict(MODEL_AUX_SHA256)
    missing = sorted(set(MODEL_AUX_SHA256) - set(aux))
    if missing:
        raise ValueError(f"memory.retriever.aux_sha256 pins {sorted(aux)}; the embedder also reads {missing} "
                         f"(each file it reads must be pinned)")
    out["aux_sha256"] = aux
    return out


def retriever_digest(retriever: dict) -> str:
    """16 hex of the retriever's identity: everything that changes a vector or a score."""
    return digest({k: retriever.get(k) for k in ("model", "revision", "file", "sha256", "aux_sha256", "dims",
                                                 "pooling", "normalize", "max_tokens", "query_prefix")})[:16]


def default_backend() -> dict:
    """K's memory.backend table: ReMe's version and wheel, and the sha256 of this checkout's lock and flows files."""
    return {"kind": "reme", "version": REME_VERSION, "wheel_sha256": REME_WHEEL_SHA256,
            "lock_sha256": lock_sha256(), "flows_sha256": flows_sha256()}


def backend_problems(backend: dict) -> list[str]:
    """Where K's backend pins differ from this checkout's sidecar files (the sidecar refuses to start on them too)."""
    here = default_backend()
    return [f"memory.backend.{k} is {backend.get(k)!r}; this checkout's is {v!r}" for k, v in sorted(here.items())
            if (backend or {}).get(k) != v]
