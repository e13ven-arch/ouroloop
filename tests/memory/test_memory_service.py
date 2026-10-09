"""The memory of one activity process (docs/design/MEMORY.md §4, §7): MemoryService starts the sidecar (here the
in-process client) on the line's store, and — only for the stream — the bridge and a research scope its ReMe calls go
through; a reading sidecar is restarted with the scope; stream() needs the scope; stop() closes scope, bridge and
client."""
from __future__ import annotations

import json

import pytest
import rsi
from fake_memory import FakeMemoryClient, ScriptedResearch

from ouroloop.evaluate.budget import Budgets, MeteredProvider, Pricing
from ouroloop.memory import digest
from ouroloop.memory.retrieval import LibraryReader
from ouroloop.memory.service import LLM_OPS, MemoryService
from ouroloop.memory.settings import Settings
from ouroloop.memory.track import StreamStep

LINE = "rsi"


class Recording(FakeMemoryClient):
    """The fake client, keeping what each start was given."""

    def __init__(self, *, fail_start: bool = False):
        super().__init__()
        self.starts: list[dict] = []
        self.fail_start = fail_start

    def start(self, **kw):
        self.starts.append(kw)
        if self.fail_start:
            raise RuntimeError("the sidecar did not come up")
        return super().start(**kw)


def research(tmp_path, scripted=None) -> MeteredProvider:
    """The activity's research provider as the bridge takes it: metered on its own account, no retry anywhere."""
    account = Budgets(tmp_path / "book").account("svc", {"usd": 5.0}).child("svc:research:memory", {})
    return MeteredProvider(scripted or ScriptedResearch(), account, Pricing(**rsi.PRICING), operation="memory",
                           max_retries=0)


def service(tmp_path, client, **kw) -> MemoryService:
    return MemoryService(tmp_path / "ws", LINE, Settings(), scope_id="svc", client_factory=lambda: client,
                         venv=tmp_path / "venv", **kw)


def stream_of(svc: MemoryService) -> StreamStep:
    return svc.stream(train_texts={"a" * 16: "move Dana Reyes to the opportunity stage"}, forbidden=(),
                      trajectory_of=lambda session: [], activity="svc")


def test_a_reading_sidecar_has_no_bridge_and_no_research_scope(tmp_path):
    client = Recording()
    svc = service(tmp_path, client)
    assert svc.start() is svc and svc.start() is svc                # started once
    assert len(client.starts) == 1 and client.started == 1 and client.llm is None
    assert (svc.bridge, svc.scope, svc.streaming) == (None, None, False)
    (kw,) = client.starts
    assert kw["llm"] is None and kw["venv"] == tmp_path / "venv"
    assert kw["chroma_path"] == tmp_path / "ws" / "memory" / LINE / "chroma" and svc.dir.is_dir()
    assert kw["settings"] == Settings().to_json()
    assert kw["scope"] == digest({"root": str((tmp_path / "ws").resolve()), "line": LINE})[:16]
    reader = svc.reader(snapshot_text="")
    assert isinstance(reader, LibraryReader) and reader.client is client and reader.line == LINE
    with pytest.raises(RuntimeError, match=r"the stream needs the sidecar started with a research scope"):
        stream_of(svc)


def test_the_reader_starts_a_reading_sidecar_when_none_runs(tmp_path):
    client = Recording()
    svc = service(tmp_path, client)
    svc.reader(live={"collection": svc.collection, "sources": {}})
    assert client.started == 1 and client.starts[0]["llm"] is None and not svc.streaming


def test_the_stream_runs_on_a_research_scope_its_summaries_reach_through_the_bridge(tmp_path):
    client, scripted = Recording(), ScriptedResearch()
    provider = research(tmp_path, scripted)
    svc = service(tmp_path, client, effort_cap="medium")
    svc.start(research=provider, concurrency=3)
    assert svc.streaming and client.llm is svc.scope and client.starts[0]["llm"] is svc.scope
    scope = svc.scope
    assert (scope.kind, scope.scope_id, scope.max_concurrency, scope.effort_cap) == \
        ("research", "svc:memory", 3, "medium")
    assert scope.models == tuple(f"research/reme:{op}" for op in LLM_OPS) and scope.provider is provider
    step = stream_of(svc)
    assert (step.ledger_path, step.live_path) == (svc.dir / "stream.json", svc.dir / "live.jsonl")
    assert step.collection == svc.collection == f"ol-live-{digest({'scope': 'svc', 'line': LINE})[:8]}"
    assert (step.line, step.activity, step.client) == (LINE, "svc", client)
    # the stream's summary is ReMe calling the metered research model through the bridge, every call on record
    scope.set_step("s1")
    got = client.summarize([{"score": 1.0, "metadata": {"query": "move Dana Reyes to the opportunity stage"}}])
    assert len(got["memory_list"]) == 1 and scripted.calls == ["success", "validation"]
    assert [(c["op"], c["status"]) for c in step.bridge_calls_of("s1")] == [("success", 200), ("validation", 200)]
    rows = [json.loads(line) for line in (svc.dir / "bridge_calls.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and all(r["kind"] == "bridge_call" for r in rows)
    assert provider.account.remaining()["used"]["model_calls"] == 2
    svc.stop()


def test_a_reading_sidecar_is_restarted_with_the_scope_and_a_streaming_one_is_kept(tmp_path):
    client = Recording()
    svc = service(tmp_path, client)
    svc.start()
    svc.start(research=research(tmp_path))
    assert client.started == 2 and client.stopped == 1                 # the same store, started again
    assert client.starts[0]["llm"] is None and client.starts[1]["llm"] is svc.scope and svc.streaming
    assert client.starts[1]["chroma_path"] == client.starts[0]["chroma_path"]
    scope = svc.scope
    svc.start(research=research(tmp_path))
    svc.start()
    assert client.started == 2 and svc.scope is scope                  # streaming already: nothing restarted
    svc.stop()


def test_stop_closes_the_scope_the_bridge_and_the_client(tmp_path):
    client = Recording()
    with service(tmp_path, client) as svc:
        svc.start(research=research(tmp_path))
        scope, bridge = svc.scope, svc.bridge
        assert bridge.port > 0
    assert scope.closed and scope.closed_reason == "memory stopped" and client.stopped == 1
    assert (svc.client, svc.bridge, svc.scope, svc.streaming) == (None, None, None, False)
    with pytest.raises(RuntimeError, match="the bridge is not started"):
        bridge.port
    svc.stop()                                                          # stopping twice is nothing
    assert client.stopped == 1


def test_a_sidecar_that_does_not_start_leaves_no_bridge_or_scope_open(tmp_path):
    client = Recording(fail_start=True)
    svc = service(tmp_path, client)
    with pytest.raises(RuntimeError, match="the sidecar did not come up"):
        svc.start(research=research(tmp_path))
    (kw,) = client.starts
    assert kw["llm"].closed and (svc.client, svc.bridge, svc.scope) == (None, None, None)


def test_the_live_collection_is_the_activitys_and_the_lines():
    a, b, c = (MemoryService("/tmp/x", line, Settings(), scope_id=scope).collection
               for line, scope in ((LINE, "svc"), ("crm", "svc"), (LINE, "other")))
    assert a.startswith("ol-live-") and len(a) == len("ol-live-") + 8 and len({a, b, c}) == 3
