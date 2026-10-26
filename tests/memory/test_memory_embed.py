"""The sidecar's local_onnx embedder: bge-small-en-v1.5 on onnxruntime (docs/design/MEMORY.md §2 `sidecar/embed.py`,
§3 `retriever`, §11).

Unit vectors of 384; inputs cut at 512 tokens; the query prefix on queries only (the /v1/embeddings route and the
retrieval op agree); a model file, a tokenizer or a config that is not the pinned one, or a retriever table that
leaves a file the embedder reads unpinned, stops the sidecar before it serves. All slow: the real sidecar.
"""
from __future__ import annotations

import math

import pytest

from memory_support import sidecar, stamped, venv  # noqa: F401
from reharness.contracts import MemorySidecarError
from reharness.memory import MODEL_AUX_SHA256, retriever_digest
from reharness.memory.client import MemoryClient
from reharness.memory.rules import cosine
from reharness.memory.settings import Settings

pytestmark = pytest.mark.slow
PREFIX = "Represent this sentence for searching relevant passages: "
LONG = " ".join(f"invoice{i} customer reminder" for i in range(400))       # well over 512 tokens


@pytest.fixture(scope="module")
def plain(venv, tmp_path_factory):
    c = sidecar(tmp_path_factory.mktemp("embed"), venv)
    yield c
    c.stop()


def test_vectors_are_l2_normalised_384_floats(plain):
    vecs = plain.embed(["hello world", "Email every customer whose invoice is overdue.", ""])
    assert [len(v) for v in vecs] == [384, 384, 384]
    assert all(abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-5 for v in vecs)
    a, b = plain.embed(["hello world", "hello world"])
    assert cosine(a, b) > 0.9999
    assert plain.embed([]) == []


def test_inputs_are_cut_at_512_tokens(plain):
    info = plain.embed_info([LONG, LONG + " and then something entirely different about quarterly revenue", "short"])
    assert info["ol_tokens"][:2] == [512, 512] and info["ol_tokens"][2] < 10
    assert info["usage"]["prompt_tokens"] == sum(info["ol_tokens"]) and plain.health()["max_tokens"] == 512
    v = [d["embedding"] for d in info["data"]]
    assert cosine(v[0], v[1]) > 0.99999        # the tails past token 512 never reach the model
    assert cosine(v[0], v[2]) < 0.9


def test_the_calibration_holds_on_the_pinned_model(plain):
    """K's thresholds are bge-small's (MEMORY §3): related wordings score above min_score, unrelated ones below it,
    a near-duplicate above the store's dedup threshold."""
    s = Settings()
    task = "Send a payment reminder email to each customer with an unpaid invoice older than 30 days."
    q, = plain.embed([task], query=True)
    related, unrelated, near, near2 = plain.embed([
        "When a task asks to email customers about overdue or unpaid invoices",
        "When the agent needs to compute a moving average of sensor telemetry",
        "When a task asks to email customers about overdue invoices",
        "When a task asks to email customers about their overdue invoices"])
    assert cosine(q, related) >= s.min_score > cosine(q, unrelated)
    assert cosine(near, near2) >= s.store_dedup_threshold


def test_the_query_prefix_is_applied_to_queries_only(venv, tmp_path):
    text = "Send a payment reminder email to each customer with an unpaid invoice."
    when = "When a task asks to email customers about overdue or unpaid invoices"
    retriever = {**Settings().retriever, "query_prefix": PREFIX}
    client = sidecar(tmp_path, venv, retriever=retriever)
    try:
        assert client.health()["query_prefix"] == PREFIX
        assert retriever_digest(retriever) != retriever_digest(Settings().retriever)    # another K, another library
        q, = client.embed([text], query=True)
        d, = client.embed([text])
        p, = client.embed([PREFIX + text])
        assert cosine(q, p) > 0.99999 and cosine(q, d) < 0.999
        # the retrieval op embeds its query the same way, and the stored entry (a document) without the prefix
        row = stamped("List the invoices first.", when)
        client.add([row], "ol-live-prefix01")
        got, = client.retrieve(text, "ol-live-prefix01", top_k=1)
        doc, = client.embed([when])
        assert abs(got["metadata"]["retrieval_score"] - cosine(q, doc)) < 1e-4
        raw, = client.retrieve(text, "ol-live-prefix01", top_k=1, query_mode=False)
        assert abs(raw["metadata"]["retrieval_score"] - cosine(d, doc)) < 1e-4
    finally:
        client.stop()


def test_without_a_prefix_a_query_is_embedded_like_a_document(plain):
    q, = plain.embed(["Update the CRM status."], query=True)
    d, = plain.embed(["Update the CRM status."])
    assert cosine(q, d) > 0.99999


@pytest.mark.parametrize("which,why", [
    ("model", "is not the pinned"),
    ("tokenizer", "is not the pinned"),
    ("pooling", "is not the pinned"),
    ("dims", "the model has 384 dimensions; K says 512"),
])
def test_a_retriever_that_is_not_the_pinned_model_refuses_to_start(venv, tmp_path, which, why):
    r = Settings().retriever
    if which == "model":
        r = {**r, "sha256": "0" * 64}
    elif which == "tokenizer":
        r = {**r, "aux_sha256": {**MODEL_AUX_SHA256, "tokenizer.json": "f" * 64}}
    elif which == "pooling":
        r = {**r, "aux_sha256": {**MODEL_AUX_SHA256, "1_Pooling/config.json": "e" * 64}}
    else:
        r = {**r, "dims": 512}
    c = MemoryClient()
    with pytest.raises(MemorySidecarError, match=r"exited with 3 before it served(.|\n)*refused to start(.|\n)*"
                                                 + why):
        c.start(venv=venv, workdir=tmp_path / "run", chroma_path=tmp_path / "chroma",
                settings=Settings(retriever=r).to_json(), timeout=60)
    assert c.proc.returncode == 3


def test_a_retriever_table_that_leaves_a_read_file_unpinned_refuses_to_start(venv, tmp_path, monkeypatch):
    """The host completes K's table (complete_retriever) and refuses one that leaves the tokenizer or a config
    unpinned; the sidecar checks the same on its own side, since another tokenizer changes every vector."""
    import reharness.memory.settings as settings_module

    class AsGiven(dict):
        def to_json(self):
            return dict(self)
    table = Settings().to_json()
    aux = {k: v for k, v in table["retriever"]["aux_sha256"].items() if k != "tokenizer.json"}
    table["retriever"] = {**table["retriever"], "aux_sha256": aux}
    with pytest.raises(ValueError, match=r"also reads \['tokenizer.json'\]"):
        Settings.from_json(table)
    monkeypatch.setattr(settings_module, "settings_of", lambda s: AsGiven(s))       # past the host's check
    c = MemoryClient()
    with pytest.raises(MemorySidecarError, match=r"exited with 3(.|\n)*does not pin \['tokenizer.json'\]"):
        c.start(venv=venv, workdir=tmp_path / "run", chroma_path=tmp_path / "chroma", settings=table, timeout=60)
    assert c.proc.returncode == 3
