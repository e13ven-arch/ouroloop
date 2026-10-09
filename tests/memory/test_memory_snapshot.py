"""The library as a component of W (docs/design/MEMORY.md §7; DESIGN §5.5): the stream's package (stream_files), the
snapshot candidate (snapshot_candidate) and a package's entries (library_of). Both packages change nothing of W_base
but the snapshot file and the manifest's memory entry, which is rewritten in place."""
from __future__ import annotations

import json

import pytest

from ouroloop.contracts import Objects
from ouroloop.harness.kinds import MEMORY_PATH
from ouroloop.harness.package import Limits, PackageError, build, check_shape
from ouroloop.harness.seed import build_seed
from ouroloop.memory.client import canonical_rows
from ouroloop.memory.rules import content_id, new_stats, snapshot_rows, stamp
from ouroloop.memory.snapshot import MEMORY_ENTRY, library_of, snapshot_candidate, stream_files, with_memory

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
