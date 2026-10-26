"""The `local_onnx` embedding backend: BAAI/bge-small-en-v1.5 on onnxruntime + tokenizers, in the sidecar process.

Runs in the memory venv only (docs/design/MEMORY.md); imports nothing of reharness. bge-small pools the CLS
token and L2-normalises it (the model's 1_Pooling/config.json; sentence-transformers' Normalize module); inputs are
truncated to `max_tokens` (512, the model's limit). A query may carry an instruction prefix (`query_prefix`, empty
or bge's retrieval instruction, chosen by E0d); documents never do. The prefix is applied here and only here, so the
`/v1/embeddings` route and the retrieval op embed a query the same way.

One engine per model sha in the process: ReMe's backend, the retrieval op and the HTTP route share it.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer


class ModelError(RuntimeError):
    """The model on disk is not the pinned one, or cannot be loaded. The sidecar refuses to start on it."""


# the files BgeOnnx reads besides the model file: each must be pinned, since another tokenizer, config or pooling
# changes every vector without changing the model file's sha
READS = ("tokenizer.json", "config.json", "1_Pooling/config.json")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_dir(repo: str, revision: str) -> Path:
    """The pinned snapshot in the Hugging Face cache, resolved offline (HF_HUB_OFFLINE is set; no network)."""
    from huggingface_hub import snapshot_download

    try:
        return Path(snapshot_download(repo, revision=revision, local_files_only=True))
    except Exception as e:  # noqa: BLE001 - every failure to find it is the same refusal
        raise ModelError(f"{repo}@{revision} is not in the Hugging Face cache ({type(e).__name__}: {e}); "
                         f"run `python -m reharness.memory setup`") from e


def verify(path: Path, *, model_file: str, model_sha256: str, aux_sha256: dict) -> dict:
    """{relative path: sha256} of every file the engine reads, each equal to its pin, or ModelError."""
    unpinned = sorted(set(READS) - set(aux_sha256 or {}))
    if unpinned:
        raise ModelError(f"retriever.aux_sha256 does not pin {unpinned}: every file the embedder reads is pinned")
    want = {model_file: model_sha256, **aux_sha256}
    got = {}
    for rel, sha in sorted(want.items()):
        f = path / rel
        if not f.is_file():
            raise ModelError(f"{f}: missing")
        got[rel] = sha256_file(f)
        if got[rel] != sha:
            raise ModelError(f"{f}: sha256 {got[rel]} is not the pinned {sha}; the sidecar does not start on "
                             f"another model")
    return got


class BgeOnnx:
    """A thread-safe batch embedder over one verified snapshot."""

    def __init__(self, path: Path, *, model_file: str, model_sha256: str, aux_sha256: dict, dims: int,
                 max_tokens: int = 512, query_prefix: str = "", pooling: str = "cls", normalize: bool = True,
                 threads: int = 0):
        self.files = verify(path, model_file=model_file, model_sha256=model_sha256, aux_sha256=aux_sha256)
        pool = json.loads((path / "1_Pooling" / "config.json").read_text(encoding="utf-8"))
        if pooling != "cls" or pool.get("pooling_mode_cls_token") is not True:
            raise ModelError(f"pooling {pooling!r} with the model's {pool}: only CLS pooling is implemented")
        if not normalize:
            raise ModelError("bge-small vectors are L2-normalised (cosine = dot); normalize=false is not supported")
        self.model_sha256 = model_sha256
        self.max_tokens = int(max_tokens)
        self.query_prefix = query_prefix or ""
        self.tok = Tokenizer.from_file(str(path / "tokenizer.json"))
        self.tok.enable_truncation(max_length=self.max_tokens)
        self.tok.enable_padding(pad_id=0, pad_token="[PAD]")
        so = ort.SessionOptions()
        if threads:
            so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(str(path / model_file), so, providers=["CPUExecutionProvider"])
        self.input_names = [i.name for i in self.sess.get_inputs()]
        self.dim = int(json.loads((path / "config.json").read_text(encoding="utf-8"))["hidden_size"])
        if self.dim != int(dims):
            raise ModelError(f"the model has {self.dim} dimensions; K says {dims}")
        self._lock = threading.Lock()

    def as_query(self, text: str) -> str:
        return self.query_prefix + (text or "")

    def embed(self, texts: list[str], *, query: bool = False) -> tuple[list[list[float]], list[int]]:
        """(unit vectors, tokens each input kept after truncation)."""
        if not texts:
            return [], []
        texts = [self.as_query(t) if query else (t or "") for t in texts]
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        with self._lock:
            out = self.sess.run(None, feeds)[0]
        cls = out[:, 0, :] if out.ndim == 3 else out
        cls = cls / np.linalg.norm(cls, axis=1, keepdims=True).clip(min=1e-12)
        return cls.astype(np.float32).tolist(), [int(sum(e.attention_mask)) for e in enc]


_ENGINES: dict[str, BgeOnnx] = {}
_ENGINES_LOCK = threading.Lock()


def engine(retriever: dict | None = None, *, path: Path | None = None) -> BgeOnnx:
    """The process's engine for the retriever's model sha: built (and verified) on first use, shared after; a second
    retriever table for the same sha must agree with the first."""
    if retriever is None:
        if len(_ENGINES) != 1:
            raise ModelError(f"{len(_ENGINES)} engines are loaded; name the retriever")
        return next(iter(_ENGINES.values()))
    sha = retriever["sha256"]
    with _ENGINES_LOCK:
        got = _ENGINES.get(sha)
        if got is None:
            snap = path or snapshot_dir(retriever["model"], retriever["revision"])
            got = BgeOnnx(snap, model_file=retriever["file"], model_sha256=sha,
                          aux_sha256=dict(retriever.get("aux_sha256") or {}), dims=retriever["dims"],
                          max_tokens=retriever.get("max_tokens", 512), query_prefix=retriever.get("query_prefix", ""),
                          pooling=retriever.get("pooling", "cls"), normalize=retriever.get("normalize", True))
            _ENGINES[sha] = got
        elif (got.max_tokens, got.query_prefix) != (int(retriever.get("max_tokens", 512)),
                                                    retriever.get("query_prefix", "") or ""):
            raise ModelError("one model sha, two retriever settings: one engine per model sha")
        return got


def register_reme_backend(name: str = "local_onnx"):
    """Register the engine as a ReMe 0.3.x embedding backend (R.embedding_models[name]). Documents only: ReMe embeds
    stored nodes and dedup texts through it; queries are embedded by the retrieval op with the prefix."""
    import asyncio

    from reme.core.embedding import BaseEmbeddingModel
    from reme.core.registry_factory import R

    class LocalOnnxEmbeddingModel(BaseEmbeddingModel):
        def __init__(self, model_sha256: str = "", **kwargs):
            super().__init__(**kwargs)
            self.engine = _ENGINES.get(model_sha256)
            if self.engine is None:
                raise ModelError(f"no verified engine for model sha {model_sha256!r}; serve.py builds it first")

        async def _get_embeddings(self, input_text: list[str], **kwargs) -> list[list[float]]:
            vectors, _ = await asyncio.to_thread(self.engine.embed, list(input_text))
            return vectors

        def _get_embeddings_sync(self, input_text: list[str], **kwargs) -> list[list[float]]:
            return self.engine.embed(list(input_text))[0]

    R.embedding_models.register(name)(LocalOnnxEmbeddingModel)
    return LocalOnnxEmbeddingModel
