"""The library as a component of W (docs/design/MEMORY.md §7; DESIGN §5.5): the stream's package (stream_files), the
snapshot candidate (snapshot_candidate) and a package's entries (library_of). Both packages change nothing of W_base
but the snapshot file and the manifest's memory entry, which is rewritten in place. The snapshot's rows and K's byte
cap (§7.1): a row holds only what retrieval and the rules read and reads back the same entries; a library over K's
max_memory_bytes becomes a candidate that fits and keeps its credited entries, with what it left out recorded; a
library that fits loses nothing; one none of whose entries fits is no candidate."""
from __future__ import annotations

import json

import pytest

from fake_memory import FakeMemoryClient
from ouroloop.activity.presets import AUTOMATIONBENCH_V1
from ouroloop.contracts import Objects
from ouroloop.harness.kinds import MEMORY_PATH
from ouroloop.harness.package import Limits, PackageError, build, check_shape
from ouroloop.harness.seed import build_seed
from ouroloop.memory import snapshot_digest
from ouroloop.memory.client import canonical_rows, parse_jsonl
from ouroloop.memory.importing import imported_row
from ouroloop.memory.retrieval import LibraryReader, snapshot_collection
from ouroloop.memory.rules import (content_id, new_stats, snapshot_abort, snapshot_rows, sources_of_row, stamp,
                                   stats_of_row)
from ouroloop.memory.settings import Settings
from ouroloop.memory.snapshot import (MEMORY_ENTRY, fit_snapshot, library_of, row_bytes, snapshot_candidate,
                                      snapshot_rank, snapshot_row, stream_files, with_memory)

CORE = {"model": {"identity": "scripted-1", "effort": "medium", "max_effort": "medium"},
        "memory": {"enabled": False}, "limits": {"max_steps": 8, "max_model_calls": 20, "max_tool_calls": 10,
                                                 "max_seconds": 60, "max_tokens": 10000},
        "worker": {"network": "none"}}
LIMITS = Limits.from_core(CORE)
ON = '"memory": {"enabled": true}'
FROZEN = '"memory": {"enabled": true, "snapshot": "memory/snapshot.jsonl"}'


@pytest.fixture
def objects(tmp_path):
    return Objects(tmp_path / "objects")


@pytest.fixture
def w_base(objects):
    """The stirrup seed: W_base of a first stream, its manifest's memory entry `{"enabled": false}` over three
    lines."""
    seed = build_seed("stirrup", core=CORE, objects=objects)
    assert entry_of(seed.files["manifest.json"]) == '"memory": {\n  "enabled": false\n }'
    assert MEMORY_PATH not in seed.files
    return seed


def entry_of(manifest_text: str) -> str:
    """The manifest's memory entry as it is written in the text."""
    (found,) = MEMORY_ENTRY.findall(manifest_text)
    return found


def in_place(before: str, after: str, entry: str) -> None:
    """`after` is `before` with its memory entry replaced by `entry`, every other character as it was."""
    head, tail = before.split(entry_of(before))
    assert after == head + entry + tail


def entry_rows(*lessons) -> list[dict]:
    """Snapshot rows as the stream writes them: stamped with their provenance, the ledger's sources and stats merged
    in."""
    rows, entries = [], {}
    for i, (when, content, origin, sources) in enumerate(lessons):
        memory = {"memory_id": content_id(content), "when_to_use": when, "content": content,
                  "author": f"research/reme:{origin}", "score": 0.9, "metadata": {"step_type": "action"}}
        row = stamp(memory, line="rsi", source_line="rsi", split="train", task_hash=sources[0], activity="rsi-test",
                    release_ref="release:sha256:" + "1" * 64, sessions=[f"run/t{i}/r0/first#0"],
                    trace_refs=[f"run/t{i}/r0/first#0:1"], origin=origin, born_at=i, source_score=1.0,
                    heldout_domains=("operations",))
        rows.append(row)
        entries[row["memory_id"]] = {"sources": list(sources), "stats": {**new_stats(), "fixes": i}}
    return snapshot_rows(rows, entries)


LESSONS = (("When a request asks to post a phrase in #ops", "Post the phrase in #ops before you finish.", "failure",
            ["a" * 16, "b" * 16]),
           ("When a request asks to move a contact to the opportunity stage",
            "Read the contact, then patch only the stage.", "success", ["c" * 16]))


# ---------------------------------------------------------------------------- the stream's package
def test_the_streams_package_is_w_base_with_memory_on_and_no_snapshot(objects, w_base):
    files = stream_files(w_base)
    assert set(files) == set(w_base.files)
    assert {p: t for p, t in files.items() if p != "manifest.json"} == \
        {p: t for p, t in w_base.files.items() if p != "manifest.json"}
    in_place(w_base.files["manifest.json"], files["manifest.json"], ON)
    package = build(objects, files, limits=LIMITS, seed=w_base)
    assert package.manifest.memory == {"enabled": True} and package.memory_text == ""
    assert package.manifest.to_json() == {**w_base.manifest.to_json(), "memory": {"enabled": True}}


def test_the_streams_package_of_a_w_base_with_a_snapshot_reads_the_live_library_instead(objects, w_base):
    with_snapshot = build(objects, snapshot_candidate(w_base, canonical_rows(entry_rows(*LESSONS))), limits=LIMITS,
                          seed=w_base)
    files = stream_files(with_snapshot)
    assert MEMORY_PATH not in files and set(files) == set(w_base.files)
    in_place(w_base.files["manifest.json"], files["manifest.json"], ON)
    assert build(objects, files, limits=LIMITS, seed=w_base).manifest.memory == {"enabled": True}


# ---------------------------------------------------------------------------- the snapshot candidate
def test_the_snapshot_candidate_changes_only_the_snapshot_file_and_the_manifests_memory_entry(objects, w_base):
    text = canonical_rows(entry_rows(*LESSONS))
    files = snapshot_candidate(w_base, text)
    assert set(files) == set(w_base.files) | {MEMORY_PATH} and files[MEMORY_PATH] == text
    assert {p: t for p, t in files.items() if p not in ("manifest.json", MEMORY_PATH)} == \
        {p: t for p, t in w_base.files.items() if p != "manifest.json"}
    in_place(w_base.files["manifest.json"], files["manifest.json"], FROZEN)
    candidate = build(objects, files, limits=LIMITS, seed=w_base)
    assert candidate.manifest.memory == {"enabled": True, "snapshot": MEMORY_PATH} and candidate.memory_text == text
    assert candidate.manifest.to_json() == {**w_base.manifest.to_json(),
                                            "memory": {"enabled": True, "snapshot": MEMORY_PATH}}
    # the host writes the snapshot: a candidate of the improver built on W_base may not
    assert any(MEMORY_PATH in p for p in check_shape(files, limits=LIMITS, seed=w_base, parent=w_base))


def test_a_new_snapshot_replaces_the_one_w_base_carries_in_place(objects, w_base):
    first = build(objects, snapshot_candidate(w_base, canonical_rows(entry_rows(LESSONS[0]))), limits=LIMITS,
                  seed=w_base)
    text = canonical_rows(entry_rows(*LESSONS))
    files = snapshot_candidate(first, text)
    assert files[MEMORY_PATH] == text and files["manifest.json"] == first.files["manifest.json"]
    assert {p: t for p, t in files.items() if p != MEMORY_PATH} == \
        {p: t for p, t in first.files.items() if p != MEMORY_PATH}
    assert build(objects, files, limits=LIMITS, seed=w_base).memory_text == text


@pytest.mark.parametrize("text", ["", "\n", "  \n"])
def test_an_empty_library_is_not_a_candidate(w_base, text):
    with pytest.raises(ValueError, match="an empty library is not a candidate"):
        snapshot_candidate(w_base, text)


def test_a_snapshot_over_ks_memory_bytes_does_not_build(objects, w_base):
    big = canonical_rows(entry_rows(*[(f"When lesson {i} applies to the request", f"Lesson {i}. " + "x" * 1500,
                                       "success", [f"{i:016x}"]) for i in range(200)]))
    assert len(big.encode("utf-8")) > LIMITS.max_memory_bytes
    with pytest.raises(PackageError, match=f"bytes in {MEMORY_PATH}: K allows {LIMITS.max_memory_bytes}"):
        build(objects, snapshot_candidate(w_base, big), limits=LIMITS, seed=w_base)


def test_a_manifest_of_another_layout_is_written_again_with_every_key_kept():
    manifest = {"api_version": "harness_package.v3", "agent": {"entrypoint": "agent/main.py:run", "roles": ["act"]},
                "model": {"base": "scripted-1", "effort": "medium"}}
    for text in (json.dumps(manifest), json.dumps({**manifest, "memory": {"enabled": False}}, separators=(",", ":"))):
        out = with_memory(text, {"enabled": True})
        assert json.loads(out) == {**manifest, "memory": {"enabled": True}}
    compact = json.dumps({**manifest, "memory": {"enabled": False}}, separators=(",", ":"))
    assert with_memory(compact, {"enabled": True}) == compact.replace('"memory":{"enabled":false}',
                                                                      '"memory": {"enabled": true}')


# ---------------------------------------------------------------------------- a package's entries
def test_library_of_lists_a_packages_entries_by_id_with_their_sources_stats_and_provenance(objects, w_base):
    assert library_of(w_base) == []
    rows = entry_rows(*LESSONS)
    candidate = build(objects, snapshot_candidate(w_base, canonical_rows(rows)), limits=LIMITS, seed=w_base)
    out = library_of(candidate)
    assert [e["id"] for e in out] == sorted(r["memory_id"] for r in rows)
    by_content = {e["content"]: e for e in out}
    ops = by_content["Post the phrase in #ops before you finish."]
    assert (ops["when"], ops["origin"], ops["sources"]) == ("When a request asks to post a phrase in #ops",
                                                            "failure", ["a" * 16, "b" * 16])
    assert ops["stats"] == {**new_stats(), "fixes": 0}
    assert by_content["Read the contact, then patch only the stage."]["stats"]["fixes"] == 1
    assert set(ops["provenance"]) == {"ol_line", "ol_source_line", "ol_split", "ol_task_hash", "ol_activity",
                                      "ol_release_ref", "ol_sessions", "ol_trace_refs", "ol_origin",
                                      "ol_reme_step_type", "ol_born_at", "ol_source_score", "ol_validation_score",
                                      "ol_imported_from"}
    assert ops["provenance"]["ol_trace_refs"] == json.dumps(["run/t0/r0/first#0:1"])
    assert ops["provenance"]["ol_split"] == "train" and ops["provenance"]["ol_task_hash"] == "a" * 16


# ---------------------------------------------------------------------------- the snapshot's rows and K's byte cap
PRESET_LIMITS = Limits.from_core({**CORE, "harness": AUTOMATIONBENCH_V1["harness"]})
TOPICS = ("overdue invoice reminders", "vendor payment approvals", "expense report checks", "payroll variance notes",
          "quote to invoice conversion", "intercompany balance review", "lease payment schedules", "receipt capture")


def whole_rows(n: int, stats=None, sources=None) -> list[dict]:
    """A live library of n lessons as StreamStep writes live.jsonl: rows stamped with their whole provenance (ReMe's
    own fields too), the ledger's sources and stats merged in; each row about 2.4 KB, as finance-v2's were.
    `stats(i)` and `sources(i)` give entry i's credit and source tasks."""
    rows, entries = [], {}
    for i in range(n):
        topic = TOPICS[i % len(TOPICS)]
        when = f"When a request asks about {topic}, case {i}, and the records disagree with the policy email"
        content = (f"Lesson {i} on {topic}. " + " ".join(f"Check field {j} of the record before acting on it."
                                                         for j in range(18)))
        memory = {"memory_id": content_id(content), "memory_type": "procedural", "when_to_use": when,
                  "content": content, "author": "research/reme:success", "score": 0.66, "memory_target": "",
                  "ref_memory_id": "", "metadata": {"step_type": "reasoning", "confidence": 0.6,
                                                    "tags": json.dumps([f"tag-{k}" for k in range(7)]),
                                                    "tools_used": json.dumps(["api_fetch", "finish"])}}
        task = f"{i:016x}"
        row = stamp(memory, line="rsi", source_line="rsi", split="train", task_hash=task, activity="rsi-test",
                    release_ref="release:sha256:" + "1" * 64,
                    sessions=[f"{'2' * 16}/rsi.case_{i}/r0/{arm}#0" for arm in ("subject", "first", "retry")],
                    trace_refs=[f"{'2' * 16}/rsi.case_{i}/r0/{arm}#0:L1" for arm in ("subject", "first", "retry")],
                    origin="success", born_at=i, source_score=1.0, heldout_domains=("operations",))
        rows.append(row)
        entries[row["memory_id"]] = {"sources": list(sources(i)) if sources else [task],
                                     "stats": {**new_stats(), **(stats(i) if stats else {})}}
    return snapshot_rows(rows, entries)


def test_a_snapshot_row_holds_only_what_retrieval_and_the_platforms_rules_read():
    (whole,) = whole_rows(1, stats=lambda i: {"n_shown": 4, "n_cited": 2, "credit_sum": 1.5, "fixes": 1,
                                                  "fix_tasks": ["c" * 16], "harm_tasks": ["d" * 16]})
    lean = snapshot_row(whole)
    assert set(lean) == {"memory_id", "memory_type", "when_to_use", "content", "metadata"}
    assert {k: lean[k] for k in ("memory_id", "memory_type", "when_to_use", "content")} == \
        {k: whole[k] for k in ("memory_id", "memory_type", "when_to_use", "content")}
    assert lean["metadata"] == {"ol_line": "rsi", "ol_split": "train", "ol_source_line": "rsi",
                                "ol_sources": whole["metadata"]["ol_sources"],
                                "ol_stats": '{"fixes":1,"n_cited":2}'}
    assert snapshot_row(lean) == lean
    assert row_bytes(whole) - row_bytes(lean) > 1000           # the provenance and ReMe's fields, about 1 KB a row
    # an imported row keeps its pointer back to the release and row it came from; its live row has no clock
    imported = imported_row(lean, source_release_ref="release:sha256:" + "3" * 64, target_line="crm")
    assert (imported["time_created"], imported["time_modified"], imported["message_time"]) == ("", "", "")
    assert "ol_sources" not in imported["metadata"] and "ol_stats" not in imported["metadata"]
    again = snapshot_row(imported)
    assert again["metadata"]["ol_imported_from"] == f"release:sha256:{'3' * 64}:{lean['memory_id']}"
    assert (again["metadata"]["ol_line"], again["metadata"]["ol_source_line"]) == ("crm", "rsi")


def test_a_library_over_ks_memory_bytes_becomes_a_candidate_that_fits_and_keeps_its_credited_entries(objects,
                                                                                                    w_base):
    """finance-v2's library (JOURNAL 2026-10-10): its whole rows came to 550 KB against K's 256 KiB and the library
    never became a candidate. Here 300 entries; the credited ones are the last by id, so a cut by id would lose
    them."""
    ids = sorted(content_id(r["content"]) for r in whole_rows(300))
    order = {r["content"]: i for i, r in enumerate(whole_rows(300))}
    by_id = {content_id(c): i for c, i in order.items()}
    fixed = {by_id[k]: f for k, f in zip(ids[-4:], (4, 1, 1, 1))}          # the last four by id fixed tasks
    cited = {by_id[k]: c for k, c in zip(ids[-24:-4], range(20, 0, -1))}   # twenty more were cited
    rows = whole_rows(300, stats=lambda i: {"fixes": fixed.get(i, 0), "n_cited": cited.get(i, 0) + (i in fixed),
                                            "n_shown": 5 if (i in fixed or i in cited) else 0})
    whole = canonical_rows(rows)
    cap = PRESET_LIMITS.max_memory_bytes
    assert cap == 262144 and len(whole.encode("utf-8")) > 2 * cap
    with pytest.raises(PackageError, match=f"bytes in {MEMORY_PATH}: K allows {cap}"):     # what happened then
        build(objects, snapshot_candidate(w_base, whole), limits=PRESET_LIMITS, seed=w_base)
    text, fit = fit_snapshot(whole, cap)
    assert len(text.encode("utf-8")) == fit["bytes"] <= cap < fit["lean_bytes"] < fit["library_bytes"]
    candidate = build(objects, snapshot_candidate(w_base, text), limits=PRESET_LIMITS, seed=w_base)
    assert check_shape(dict(candidate.files), limits=PRESET_LIMITS, seed=w_base) == []
    kept = {e["id"] for e in library_of(candidate)}
    credited = {r["memory_id"] for r in rows if order[r["content"]] in fixed or order[r["content"]] in cited}
    assert len(credited) == 24 and credited <= kept
    # the record: what was left out, by how much, and why; every kept entry ranks above every omitted one
    assert (fit["entries"], fit["kept"], fit["max_bytes"], fit["why"]) == (300, len(kept), cap, "K's max_memory_bytes")
    assert fit["rank"] == "fixes, then citations (n_cited), then memory_id"
    omitted = fit["omitted"]
    assert len(omitted) == 300 - len(kept) > 0 and not {o["memory_id"] for o in omitted} & kept
    assert all(o["fixes"] == o["n_cited"] == 0 for o in omitted)
    rank = {r["memory_id"]: snapshot_rank(r) for r in rows}
    assert max(rank[k] for k in kept) < min(rank[o["memory_id"]] for o in omitted)
    assert [o["memory_id"] for o in omitted] == sorted(o["memory_id"] for o in omitted)
    assert fit["bytes"] + omitted[0]["bytes"] > cap                                       # the next one does not fit
    assert fit["bytes"] + sum(o["bytes"] for o in omitted) == fit["lean_bytes"]
    assert {o["memory_id"]: o["bytes"] for o in omitted} == \
        {r["memory_id"]: row_bytes(snapshot_row(r)) for r in rows if r["memory_id"] not in kept}


def test_the_snapshot_is_a_strict_prefix_of_the_rank_within_an_inclusive_cap():
    """Fixes rank above citations; a cap of exactly the kept rows' bytes keeps them; a smaller entry ranked below
    one that does not fit stays out even when it would fit."""
    def row(name, content, fixes, cited):
        return {"memory_id": name * 16, "memory_type": "procedural", "when_to_use": f"When case {name} applies",
                "content": content, "metadata": {"ol_line": "rsi", "ol_split": "train", "ol_source_line": "rsi",
                                                 "ol_sources": "[]", "ol_stats": json.dumps({"fixes": fixes,
                                                                                            "n_cited": cited})}}
    rows = [row("a", "x" * 900, 2, 0), row("b", "y" * 900, 1, 1), row("c", "z" * 900, 0, 9), row("d", "w", 0, 0)]
    whole = canonical_rows(rows)
    size = {r["memory_id"][0]: row_bytes(snapshot_row(r)) for r in rows}
    assert [r["memory_id"][0] for r in sorted(rows, key=snapshot_rank)] == ["a", "b", "c", "d"]
    for cap in (size["a"] + size["b"], size["a"] + size["b"] + size["d"]):
        text, fit = fit_snapshot(whole, cap)
        assert [r["memory_id"][0] for r in parse_jsonl(text)] == ["a", "b"] and fit["bytes"] == size["a"] + size["b"]
        assert [o["memory_id"][0] for o in fit["omitted"]] == ["c", "d"]
    assert fit_snapshot(whole, size["a"] + size["b"] - 1)[1]["kept"] == 1


def test_a_library_that_fits_keeps_every_entry_and_its_snapshot_is_stable(objects, w_base):
    rows = whole_rows(40, stats=lambda i: {"fixes": i % 3, "n_cited": i % 5})
    whole = canonical_rows(rows)
    text, fit = fit_snapshot(whole, PRESET_LIMITS.max_memory_bytes)
    assert (fit["entries"], fit["kept"], fit["omitted"]) == (40, 40, []) and "why" not in fit
    assert text == fit_snapshot(whole, None)[0] == canonical_rows([snapshot_row(r) for r in rows])
    assert [r["memory_id"] for r in parse_jsonl(text)] == sorted(r["memory_id"] for r in rows)
    # the same library gives the same bytes; a snapshot fitted again is itself
    assert fit_snapshot(whole, PRESET_LIMITS.max_memory_bytes) == (text, fit)
    assert fit_snapshot(text, PRESET_LIMITS.max_memory_bytes)[0] == text
    assert snapshot_digest(fit_snapshot(text, None)[0]) == snapshot_digest(text)
    capped, _ = fit_snapshot(whole, fit["bytes"] // 2)
    assert fit_snapshot(capped, fit["bytes"] // 2)[0] == capped == fit_snapshot(text, fit["bytes"] // 2)[0]
    candidate = build(objects, snapshot_candidate(w_base, text), limits=PRESET_LIMITS, seed=w_base)
    assert [(e["id"], e["when"], e["content"], e["sources"]) for e in library_of(candidate)] == \
        sorted((r["memory_id"], r["when_to_use"], r["content"], sources_of_row(r)) for r in rows)
    assert {e["id"]: e["stats"] for e in library_of(candidate)} == \
        {r["memory_id"]: {"fixes": stats_of_row(r)["fixes"], "n_cited": stats_of_row(r)["n_cited"]} for r in rows}


def test_lean_rows_read_the_same_entries_as_the_whole_rows():
    """The opening block, its start line and recall over the lean snapshot are those over the whole rows, leave-one-out
    included; only the snapshot's digest and collection differ (they name the bytes)."""
    rows = whole_rows(40, sources=lambda i: [f"{i:016x}", f"{i % 4:015x}f"])
    whole = canonical_rows(rows)
    text, _ = fit_snapshot(whole, None)
    client, settings = FakeMemoryClient(), Settings(min_score=0.3)
    readers = [LibraryReader(client, settings, business_line="rsi", snapshot_text=t) for t in (whole, text)]
    seen = 0
    for i, topic in enumerate(TOPICS):
        query = f"A request about {topic}, case {i}: the records disagree with the policy email"
        for task in (f"{i:016x}", f"{i % 4:015x}f", "a-dev-task"):
            got = [r.for_task(query, task) for r in readers]
            assert got[0].opening == got[1].opening
            assert {k: v for k, v in got[0].event.items() if k not in ("snapshot", "collection")} == \
                {k: v for k, v in got[1].event.items() if k not in ("snapshot", "collection")}
            assert got[0].recall(f"{topic} field 3") == got[1].recall(f"{topic} field 3")
            seen += len(got[1].event["shown"]) + got[1].event["excluded_as_source"]
    assert seen > 0
    lean = readers[1].for_task("overdue invoice reminders", "a-dev-task").event
    assert lean["snapshot"] == snapshot_digest(text) and lean["collection"] == snapshot_collection(
        text, settings.retriever) and lean["library_size"] == 40


def test_a_library_none_of_whose_entries_fits_is_no_candidate():
    whole = canonical_rows(whole_rows(3))
    text, fit = fit_snapshot(whole, 100)
    assert text == "" and (fit["entries"], fit["kept"], len(fit["omitted"])) == (3, 0, 3)
    readout = {"D": 1.0}
    assert snapshot_abort(readout, Settings(), empty=False, fit=fit) == \
        "no entry of the library fits K's max_memory_bytes (100)"
    assert snapshot_abort(readout, Settings(), empty=False, fit=fit_snapshot(whole, None)[1]) is None
    assert snapshot_abort(readout, Settings(), empty=True, fit=fit_snapshot("", 100)[1]) == "the library is empty"
