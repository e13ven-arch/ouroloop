"""The read path of an attempt (docs/design/MEMORY.md §4, §6): LibraryReader over a frozen snapshot and over the
stream's live view, on the in-process client (tests/memory/fake_memory.py), and its wiring through the runner shell: the
start's `library_retrieval` line right after the task line, recall answered through MemoryRecall and recorded as site
"recall".

The fake client's vectors are a bag of words: a when_to_use equal to the query scores 1.0, one sharing no word 0.0, so
each case below sets its scores by its wording."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from fake_memory import FakeMemoryClient, bow, cosine

from reharness.contracts import MemorySidecarError, Objects
from reharness.evaluate.attempt import Budgeted
from reharness.evaluate.limits import Budget
from reharness.harness.package import Limits, build
from reharness.memory import retriever_digest, snapshot_digest
from reharness.memory.client import SHORT, canonical_rows
from reharness.memory.retrieval import CITE, INTRO, LibraryReader, TaskMemory, format_block, snapshot_collection
from reharness.memory.rules import content_id
from reharness.memory.settings import Settings
from reharness.runtime.chat import ToolSpec
from reharness.runtime.records import Transcript, read, session_file
from reharness.runtime.runner import PackageAgent
from reharness.runtime.worker import ProcessWorker

LINE = "rsi"
TASK = "a" * 16                       # the task an attempt is at (task_hash: 16 hex)
OTHER = "b" * 16
QUERY = "update the contact then post the phrase in ops"
ID8 = re.compile(r"^[0-9a-f]{8}$")


def row(content: str, when: str, sources=(), *, line: str = LINE, split: str = "train") -> dict:
    """A snapshot row as rules.snapshot_rows writes it: ReMe's fields, the provenance this reader filters on and the
    ledger's sources merged in."""
    return {"memory_id": content_id(content), "when_to_use": when, "content": content,
            "metadata": {"ol_line": line, "ol_split": split, "ol_sources": json.dumps(sorted(sources)),
                         "ol_stats": "{}"}}


def snapshot(*rows) -> str:
    return canonical_rows(list(rows))


def settings(**kw) -> Settings:
    return Settings(**{"min_score": 0.5, **kw})


class Spy(FakeMemoryClient):
    """The fake client, recording each read's count and each (re)load; `drop_once` removes one stored row of the
    collection just before the next read, as another process's reload or the sidecar's LRU would; `fail` answers
    every read with that error."""

    def __init__(self, *, drop_once: bool = False, fail: str | None = None):
        super().__init__()
        self.reads: list[dict] = []
        self.loads: list[str] = []
        self.forgot: list[str] = []
        self.drop_once, self.fail = drop_once, fail

    def retrieve(self, query, collection, *, where=None, top_k=5, query_mode=True, expected_count=None):
        self.reads.append({"query": query, "collection": collection, "where": where, "top_k": top_k,
                           "expected_count": expected_count})
        if self.fail is not None:
            raise MemorySidecarError(self.fail)
        if self.drop_once:
            self.drop_once = False
            self.collections[collection].pop(sorted(self.collections[collection])[0])
        return super().retrieve(query, collection, where=where, top_k=top_k, query_mode=query_mode,
                                expected_count=expected_count)

    def load(self, jsonl, collection, *, clear=True):
        self.loads.append(collection)
        return super().load(jsonl, collection, clear=clear)

    def forget_snapshot(self, collection):
        self.forgot.append(collection)
        super().forget_snapshot(collection)


# ---------------------------------------------------------------------------- the opening block and its line
def test_the_opening_block_names_each_entry_by_its_8_hex_and_asks_for_the_citation_line():
    own = row("Post the phrase in #ops before you finish.", QUERY, sources=[OTHER])
    near = row("Read the contact first, then patch only the stage.", QUERY + " now")
    far = row("Bring an umbrella.", "the weather report mentions rain tomorrow")
    text = snapshot(own, near, far)
    s = settings()
    client = Spy()
    memory = LibraryReader(client, s, business_line=LINE, snapshot_text=text).for_task(QUERY, TASK)
    ids = [own["memory_id"], near["memory_id"]]                       # by score: 1.0, then the near wording
    assert memory.opening == "\n\n".join([
        INTRO, f"[lib {ids[0][:8]}] When: {QUERY}\nPost the phrase in #ops before you finish.",
        f"[lib {ids[1][:8]}] When: {QUERY} now\nRead the contact first, then patch only the stage.", CITE])
    assert memory.opening == format_block([own, near], cite=True)
    score = round(cosine(bow(QUERY), bow(QUERY + " now")), 4)
    assert memory.event == {
        "query": QUERY, "shown": ids, "scores": [1.0, score], "retriever": retriever_digest(s.retriever),
        "snapshot": snapshot_digest(text), "collection": snapshot_collection(text, s.retriever),
        "filters": {"ol_line": LINE, "ol_split": "train"}, "leave_one_out": True, "library_size": 3,
        "min_score": 0.5, "top_k": 2, "task_hash": TASK, "chars": len(memory.opening), "fetched": 3, "fetches": 1,
        "excluded_as_source": 0}
    assert all(len(i) == 16 for i in memory.event["shown"])           # the line keeps the whole ids
    # every read names the count the collection must hold, and a cold collection is loaded once
    assert [r["expected_count"] for r in client.reads] == [3] and client.loads == [memory.event["collection"]]
    assert client.reads[0]["top_k"] == s.shown_k * s.fetch_factor
    LibraryReader(client, s, business_line=LINE, snapshot_text=text).for_task(QUERY, OTHER)
    assert len(client.loads) == 1                                     # a warm collection is not loaded again


def test_without_citation_the_block_has_no_citation_line_and_the_query_is_cut_at_500():
    entry = row("Post the phrase in #ops.", QUERY)
    long_task = QUERY + " " + "x" * 600
    memory = LibraryReader(Spy(), settings(cite=False, min_score=0.1), business_line=LINE,
                           snapshot_text=snapshot(entry)).for_task(long_task, TASK)
    assert memory.opening == f"{INTRO}\n\n[lib {entry['memory_id'][:8]}] When: {QUERY}\nPost the phrase in #ops."
    assert CITE not in memory.opening and memory.event["query"] == long_task[:500]


def test_the_note_is_appended_after_the_block_and_alone_when_nothing_is_shown():
    entry = row("Post the phrase in #ops.", QUERY)
    reader = LibraryReader(Spy(), settings(), business_line=LINE, snapshot_text=snapshot(entry))
    note = "A previous attempt at this very task was graded and failed these checks."
    shown = reader.for_task(QUERY, TASK, note=note)
    assert shown.opening == reader.for_task(QUERY, TASK).opening + "\n\n" + note
    unrelated = reader.for_task("the weather report", TASK, note=note)
    assert unrelated.opening == note and unrelated.event["shown"] == []     # the library is not empty: a line


@pytest.mark.parametrize("source", ["snapshot", "live"])
def test_an_empty_library_gives_no_block_and_no_line_and_reads_nothing(source):
    client = Spy()
    reader = LibraryReader(client, settings(), business_line=LINE,
                           **({"snapshot_text": ""} if source == "snapshot" else
                              {"live": {"collection": "ol-live-00000000", "sources": {}}}))
    assert reader.empty and reader.count == 0
    memory = reader.for_task(QUERY, TASK)
    assert (memory.opening, memory.event) == ("", None) and memory.recall(QUERY) == []
    assert reader.for_task(QUERY, TASK, note="the note").opening == "the note"
    assert client.reads == [] and client.loads == []


def test_exactly_one_of_a_snapshot_and_a_live_view_is_read():
    for kw in ({}, {"snapshot_text": "", "live": {"collection": "c", "sources": {}}}):
        with pytest.raises(ValueError, match="give exactly one of snapshot_text and live"):
            LibraryReader(Spy(), settings(), business_line=LINE, **kw)


# ---------------------------------------------------------------------------- leave-one-out, min_score, the fetch
def test_leave_one_out_drops_the_entries_whose_sources_hold_this_task_on_start_and_on_recall():
    own = row("Post the phrase in #ops before you finish.", QUERY, sources=[TASK, OTHER])
    other = row("Patch only the stage field.", QUERY + " stage")
    reader = LibraryReader(Spy(), settings(), business_line=LINE, snapshot_text=snapshot(own, other))
    mine = reader.for_task(QUERY, TASK)
    assert mine.event["shown"] == [other["memory_id"]] and mine.event["excluded_as_source"] == 1
    assert f"[lib {own['memory_id'][:8]}]" not in mine.opening
    assert [e["id"] for e in mine.recall(QUERY)] == [other["memory_id"][:8]]
    # the same library at a task that is no source of it (a dev or test task never is) excludes nothing
    theirs = LibraryReader(Spy(), settings(), business_line=LINE, snapshot_text=snapshot(own, other)) \
        .for_task(QUERY, "c" * 16)
    assert theirs.event["shown"] == [own["memory_id"], other["memory_id"]]
    assert theirs.event["excluded_as_source"] == 0


def test_min_score_keeps_only_entries_at_or_above_it():
    exact = row("Post the phrase in #ops.", QUERY)
    weak = row("Mind the weather.", "post rain umbrella forecast tomorrow weather report")
    at = cosine(bow(QUERY), bow(weak["when_to_use"]))
    assert 0 < at < 0.5
    reader = LibraryReader(Spy(), settings(min_score=round(at, 6)), business_line=LINE,
                           snapshot_text=snapshot(exact, weak))
    assert reader.for_task(QUERY, TASK).event["shown"] == [exact["memory_id"], weak["memory_id"]]
    above = LibraryReader(Spy(), settings(min_score=round(at, 6) + 0.001), business_line=LINE,
                          snapshot_text=snapshot(exact, weak))
    memory = above.for_task(QUERY, TASK)
    assert memory.event["shown"] == [exact["memory_id"]] and weak["memory_id"][:8] not in memory.opening


def test_the_fetch_is_widened_while_leave_one_out_used_it_up():
    """shown_k 1 and fetch_factor 1 fetch one entry: this task's own; the reader looks further down (2 = the whole
    library) and shows the next."""
    own = row("Post the phrase in #ops before you finish.", QUERY, sources=[TASK])
    next_ = row("Patch only the stage field.", QUERY + " stage")
    client = Spy()
    memory = LibraryReader(client, settings(shown_k=1, fetch_factor=1), business_line=LINE,
                           snapshot_text=snapshot(own, next_)).for_task(QUERY, TASK)
    assert memory.event["shown"] == [next_["memory_id"]]
    assert (memory.event["fetches"], memory.event["fetched"], memory.event["excluded_as_source"]) == (2, 2, 1)
    assert [r["top_k"] for r in client.reads] == [1, 2] and [r["expected_count"] for r in client.reads] == [2, 2]


def test_the_fetch_stops_widening_once_it_reaches_entries_below_min_score_or_the_whole_library():
    own = row("Post the phrase in #ops before you finish.", QUERY, sources=[TASK])
    low = row("Mind the weather.", "post rain umbrella forecast tomorrow weather report")
    lower = row("Bring an umbrella.", "rain umbrella forecast tomorrow weather report")
    client = Spy()
    memory = LibraryReader(client, settings(shown_k=1, fetch_factor=1), business_line=LINE,
                           snapshot_text=snapshot(own, low, lower)).for_task(QUERY, TASK)
    assert memory.event["shown"] == [] and memory.opening == ""
    assert [r["top_k"] for r in client.reads] == [1, 2]                      # below min_score at 2: no third read
    whole = Spy()
    alone = LibraryReader(whole, settings(shown_k=1, fetch_factor=1), business_line=LINE,
                          snapshot_text=snapshot(own)).for_task(QUERY, TASK)
    assert alone.event["shown"] == [] and [r["top_k"] for r in whole.reads] == [1]     # the library is read whole


def test_the_filter_is_this_lines_train_entries():
    theirs = row("Post the phrase in #ops.", QUERY, line="crm")
    mine = row("Patch only the stage field.", QUERY + " stage")
    client = Spy()
    memory = LibraryReader(client, settings(), business_line=LINE, snapshot_text=snapshot(theirs, mine)) \
        .for_task(QUERY, TASK)
    assert memory.event["shown"] == [mine["memory_id"]] and memory.event["library_size"] == 2
    assert client.reads[0]["where"] == {"ol_line": LINE, "ol_split": "train"}


# ---------------------------------------------------------------------------- the count named on every read
def test_a_snapshot_read_short_is_reloaded_once_and_read_again():
    entries = [row(f"Lesson {i}: post the phrase in #ops.", QUERY + f" {i}") for i in range(3)]
    text = snapshot(*entries)
    client = Spy(drop_once=True)
    reader = LibraryReader(client, settings(), business_line=LINE, snapshot_text=text)
    memory = reader.for_task(QUERY, TASK)
    assert len(memory.event["shown"]) == 2 and client.count(reader.collection) == 3
    assert client.forgot == [reader.collection] and client.loads == [reader.collection] * 2
    assert [r["expected_count"] for r in client.reads] == [3, 3]


def test_a_snapshot_still_short_after_its_reload_fails_the_read_and_never_answers_an_empty_library():
    text = snapshot(row("Post the phrase in #ops.", QUERY))
    client = Spy(fail=f"{SHORT}: collection holds 0; the caller expects 1")
    with pytest.raises(MemorySidecarError, match=SHORT):
        LibraryReader(client, settings(), business_line=LINE, snapshot_text=text).for_task(QUERY, TASK)
    assert len(client.reads) == 2 and len(client.forgot) == 1


@pytest.mark.parametrize("case", ["live short", "snapshot other error"])
def test_a_live_read_short_or_another_sidecar_error_is_raised_without_a_reload(case):
    entry = row("Post the phrase in #ops.", QUERY)
    if case == "live short":
        client = Spy(fail=f"{SHORT}: collection ol-live-1 holds 0; the caller expects 1")
        reader = LibraryReader(client, settings(), business_line=LINE,
                               live={"collection": "ol-live-1", "sources": {entry["memory_id"]: []}})
    else:
        client = Spy(fail="the sidecar answered HTTP 500")
        reader = LibraryReader(client, settings(), business_line=LINE, snapshot_text=snapshot(entry))
    with pytest.raises(MemorySidecarError):
        reader.for_task(QUERY, TASK)
    assert len(client.reads) == 1 and client.forgot == []


def test_the_live_view_is_read_from_the_streams_collection_with_its_sources_and_count():
    own = row("Post the phrase in #ops before you finish.", QUERY)
    other = row("Patch only the stage field.", QUERY + " stage")
    client = Spy()
    client.add([own, other], "ol-live-12345678")
    live = {"collection": "ol-live-12345678",
            "sources": {own["memory_id"]: [TASK], other["memory_id"]: [OTHER]}}
    reader = LibraryReader(client, settings(), business_line=LINE, live=live)
    memory = reader.for_task(QUERY, TASK)
    assert memory.event["shown"] == [other["memory_id"]] and memory.event["excluded_as_source"] == 1
    assert (memory.event["collection"], memory.event["snapshot"], memory.event["library_size"]) == \
        ("ol-live-12345678", None, 2)
    assert client.loads == [] and [r["expected_count"] for r in client.reads] == [2]
    client.retire([other["memory_id"]], "ol-live-12345678")             # the store and the view disagree
    with pytest.raises(MemorySidecarError, match=SHORT):
        reader.for_task(QUERY, OTHER)


def test_a_snapshot_collection_is_named_by_the_snapshot_and_the_retriever():
    text, s = snapshot(row("Post the phrase in #ops.", QUERY)), settings()
    name = snapshot_collection(text, s.retriever)
    assert re.fullmatch(r"ol-snap-[0-9a-f]{16}", name) and name == snapshot_collection(text, dict(s.retriever))
    assert name != snapshot_collection(text + snapshot(row("Other.", "other")), s.retriever)
    assert name != snapshot_collection(text, {**s.retriever, "query_prefix": "query: "})


# ---------------------------------------------------------------------------- recall
def test_recall_is_capped_at_recall_k_and_returns_8_hex_ids():
    entries = [row(f"Lesson {i}: post the phrase in #ops.", QUERY + f" {i}") for i in range(5)]
    memory = LibraryReader(Spy(), settings(recall_k=2), business_line=LINE, snapshot_text=snapshot(*entries)) \
        .for_task(QUERY, TASK)
    for k, n in ((None, 2), (1, 1), (2, 2), (9, 2), (0, 2), (-1, 2), ("3", 2)):
        assert len(memory.recall(QUERY, k)) == n, k
    found = memory.recall(QUERY)
    by_id = {e["memory_id"][:8]: e for e in entries}
    assert [set(e) for e in found] == [{"id", "when", "content", "score"}] * 2
    for e in found:
        assert ID8.match(e["id"]) and (e["when"], e["content"]) == (by_id[e["id"]]["when_to_use"],
                                                                      by_id[e["id"]]["content"])
        assert e["score"] == round(cosine(bow(QUERY), bow(by_id[e["id"]]["when_to_use"])), 4)
    assert memory.recall("") == [] and TaskMemory().recall(QUERY) == []


# ---------------------------------------------------------------------------- through the runner shell
LIMITS = {"max_steps": 8, "max_model_calls": 20, "max_tool_calls": 10, "max_seconds": 60, "max_tokens": 10000,
          "max_output_chars": 20000}
CORE = {"model": {"identity": "scripted-1", "effort": "medium", "max_effort": "medium"},
        "memory": {"enabled": True, "recall_k": 3}, "limits": LIMITS, "worker": {"network": "none"}}
AGENT = ("import json\n\n\ndef run(ctx, task):\n    found = ctx.memory.recall('post the phrase in ops', 9)\n"
         "    return json.dumps({'system': ctx.view['system'], 'found': found})\n")


class NoModel:
    """The actor of an agent that never asks for a model call."""
    name, model = "scripted", "scripted-1"

    def chat(self, system, messages, specs):
        raise AssertionError("this agent calls no model")


class World:
    def specs(self):
        return [ToolSpec("api_search", "search", {"type": "object", "properties": {"q": {"type": "string"}}})]

    def run(self, name, args):
        return SimpleNamespace(output="ok", is_error=False)

    def mutates(self, name, args):
        return False

    @property
    def tool_calls(self):
        return 0


def test_an_attempt_gets_the_block_after_its_system_prompt_its_start_line_after_the_task_and_recall_on_record(
        tmp_path):
    entries = [row(f"Lesson {i}: post the phrase in #ops after the update.", QUERY + f" {i}") for i in range(3)]
    reader = LibraryReader(FakeMemoryClient(), settings(recall_k=2), business_line=LINE,
                           snapshot_text=snapshot(*entries))
    memory = reader.for_task(QUERY, TASK)
    manifest = {"api_version": "harness_package.v3", "agent": {"entrypoint": "agent/main.py:run", "roles": ["act"]},
                "model": {"base": "scripted-1", "effort": "medium"}, "memory": {"enabled": True}}
    package = build(Objects(tmp_path / "objects"), {"manifest.json": json.dumps(manifest), "agent/main.py": AGENT},
                    limits=Limits.from_core(CORE), seed=None)
    transcript = Transcript(session_file(tmp_path, "run1:t1:0:subject"))
    agent = PackageAgent(provider=NoModel(), package=package, core=CORE,
                         surface=Budgeted(World(), Budget.from_core(LIMITS)), worker=ProcessWorker(for_tests=True),
                         transcript=transcript, system="TASK SYSTEM", attempt_id="run1:t1:0:subject",
                         task_meta={"task_id": "t1", "split": "dev", "task_hash": TASK}, memory=memory)
    result = agent.run(QUERY)
    assert result.termination == "finished"
    said = json.loads(result.text)
    assert said["system"] == "TASK SYSTEM\n\n" + memory.opening                  # the block after the prompt
    lines = read(transcript.path)
    assert [line["kind"] for line in lines][:2] == ["task", "library_retrieval"]
    start, recalled = [line for line in lines if line["kind"] == "library_retrieval"]
    assert start["site"] == "start" and start["shown"] == memory.event["shown"] and start["task_hash"] == TASK
    assert all(len(i) == 16 for i in start["shown"]) and start["scores"] == memory.event["scores"]
    # recall: K's recall_k (3) caps the agent's 9, the reader's recall_k (2) caps that; the agent sees 8 hex
    assert [e["id"] for e in said["found"]] == [i[:8] for i in memory.event["shown"]]
    assert all(ID8.match(e["id"]) for e in said["found"]) and len(said["found"]) == 2
    assert (recalled["site"], recalled["query"]) == ("recall", "post the phrase in ops")
    assert recalled["shown"] == [e["id"] for e in said["found"]]
    assert recalled["scores"] == [e["score"] for e in said["found"]]
