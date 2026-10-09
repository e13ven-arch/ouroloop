"""Promotion (docs/design/MEMORY.md §8; DESIGN §5.6): an entry that grew past a note becomes a skill candidate (a valid
package of fixed code, the skill named by the entry's id8 and listed in the manifest) or a cause of the improver's
round; once its promotion is installed it is superseded and never shown again."""
from __future__ import annotations

import json

import pytest
from fake_memory import FakeMemoryClient

from ouroloop.contracts import Objects
from ouroloop.harness.package import Limits, build
from ouroloop.harness.seed import build_seed
from ouroloop.memory.client import canonical_rows
from ouroloop.memory.promote import SKILL_DIR, cause_input, mark_superseded, skill_candidate, skill_provenance
from ouroloop.memory.retrieval import LibraryReader
from ouroloop.memory.rules import content_id, new_stats, promotable, stamp
from ouroloop.memory.settings import Settings
from ouroloop.memory.snapshot import library_of, snapshot_candidate
from ouroloop.memory.track import StreamStep
from ouroloop.runtime.runner import SKILL_INDEX, compose_system

CORE = {"model": {"identity": "scripted-1", "effort": "medium", "max_effort": "medium"},
        "memory": {"enabled": False}, "limits": {"max_steps": 8, "max_model_calls": 20, "max_tool_calls": 10,
                                                 "max_seconds": 60, "max_tokens": 10000},
        "worker": {"network": "none"}}
LIMITS = Limits.from_core(CORE)
WHEN = "When a request asks to update a contact and then post a phrase in #ops"
PROCEDURE = "1. Update the contact's stage first.\n2. Then post the requested phrase in #ops before you finish."
TRACES = ["run2/rsi.notify/r0/first#0:1", "run1/rsi.notify/r0/subject#0:1"]


@pytest.fixture
def objects(tmp_path):
    return Objects(tmp_path / "objects")


@pytest.fixture
def w_base(objects):
    return build_seed("stirrup", core=CORE, objects=objects)


def entry(content: str = PROCEDURE, when: str = WHEN, *, fixes: int = 2, distilled=("a" * 16, "b" * 16),
          status: str = "active", origin: str = "success") -> dict:
    """A ledger entry as the stream keeps it (track.StreamStep): its stored row with the provenance, its stats and the
    events of the tasks it was distilled from."""
    memory = {"memory_id": content_id(content), "when_to_use": when, "content": content,
              "author": f"research/reme:{origin}", "score": 0.9, "metadata": {"step_type": "action"}}
    row = stamp(memory, line="rsi", source_line="rsi", split="train", task_hash=distilled[0], activity="rsi-test",
                release_ref="release:sha256:" + "1" * 64, sessions=["run1/rsi.notify/r0/subject#0"],
                trace_refs=TRACES, origin=origin, born_at=0, source_score=1.0, heldout_domains=("operations",))
    events = [{"n": 0, "task_hash": distilled[0], "how": "added"}]
    events += [{"n": i, "task_hash": t, "how": "store_dedup"} for i, t in enumerate(distilled[1:], 1)]
    events += [{"n": 9, "task_hash": "f" * 16, "how": "imported"}]          # not a task it was distilled from
    return {"memory_id": row["memory_id"], "origin": origin, "born_at": 0, "status": status,
            "sources": sorted(distilled), "stats": {**new_stats(), "fixes": fixes, "n_shown": 4,
                                                    "harm_tasks": ["c" * 16]},
            "row": row, "events": events}


# ---------------------------------------------------------------------------- the skill candidate
def test_a_skill_candidate_is_a_valid_package_whose_skill_keeps_the_entrys_id8_and_is_listed(objects, w_base):
    e = entry()
    id8 = e["memory_id"][:8]
    files = skill_candidate(e, w_base)
    name = f"mem-request-update-contact-and-then-{id8}"
    path = f"{SKILL_DIR}/{name}.md"
    assert set(files) == set(w_base.files) | {path}
    assert {p: t for p, t in files.items() if p not in ("manifest.json", path)} == \
        {p: t for p, t in w_base.files.items() if p != "manifest.json"}
    assert files[path] == (f"# {name}\n\nUse this when: {WHEN}\n\n{PROCEDURE}\n\nSource: the experience library's "
                           f"entry {id8}, which fixed 2 train tasks and was learned from 2.\n")
    candidate = build(objects, files, limits=LIMITS, seed=w_base, parent=w_base)     # fixed code, on W's ladder
    assert candidate.manifest.skills == ({"name": name, "path": path, "description": WHEN, "enabled": True},)
    assert {k: v for k, v in candidate.manifest.to_json().items() if k != "skills"} == \
        {k: v for k, v in w_base.manifest.to_json().items() if k != "skills"}
    assert f"{SKILL_INDEX}\n- {name}: {WHEN}" in compose_system("", candidate)      # the index says when to read it


def test_a_long_trigger_still_names_a_valid_skill_ending_in_the_id8(objects, w_base):
    e = entry(when="When " + " ".join(f"longerstepname{i}" for i in range(40)) + " applies")
    files = skill_candidate(e, w_base)
    candidate = build(objects, files, limits=LIMITS, seed=w_base, parent=w_base)
    (skill,) = candidate.manifest.skills
    assert skill["name"] == f"mem-longerstepname0-longerstepname1-longerst-{e['memory_id'][:8]}"   # a 40-char slug
    assert len(skill["name"]) <= 64 and skill["description"] == e["row"]["when_to_use"][:300]


def test_promoting_an_entry_again_replaces_its_skill_rather_than_listing_it_twice(objects, w_base):
    once = build(objects, skill_candidate(entry(), w_base), limits=LIMITS, seed=w_base, parent=w_base)
    files = skill_candidate(entry(fixes=5), once)
    assert len(json.loads(files["manifest.json"])["skills"]) == 1
    twice = build(objects, files, limits=LIMITS, seed=w_base, parent=once)
    (skill,) = twice.manifest.skills
    assert "which fixed 5 train tasks" in twice.files[skill["path"]]


def test_a_snapshot_entry_is_promoted_from_what_library_of_reads(objects, w_base):
    with_library = build(objects, snapshot_candidate(w_base, canonical_rows([entry()["row"]])), limits=LIMITS,
                         seed=w_base)
    (read,) = library_of(with_library)
    files = skill_candidate({**read, "stats": {"fixes": 3}}, with_library)
    candidate = build(objects, files, limits=LIMITS, seed=w_base, parent=with_library)    # the snapshot untouched
    (skill,) = candidate.manifest.skills
    assert skill["name"].endswith(read["id"][:8]) and PROCEDURE in candidate.files[skill["path"]]


@pytest.mark.parametrize("missing", ["memory_id", "when", "content"])
def test_an_entry_without_an_id_a_trigger_or_a_procedure_is_no_skill(w_base, missing):
    e = entry()
    if missing == "memory_id":
        e = {**e, "memory_id": "", "row": {**e["row"], "memory_id": ""}}
    else:
        e = {**e, "row": {**e["row"], {"when": "when_to_use", "content": "content"}[missing]: "  "}}
    with pytest.raises(ValueError, match="a skill candidate needs an entry with an id, a trigger and a procedure"):
        skill_candidate(e, w_base)


def test_the_skill_names_the_entry_and_the_train_evidence_behind_it():
    e = entry(distilled=("b" * 16, "a" * 16, "d" * 16))
    assert skill_provenance(e) == {"memory_entry": e["memory_id"], "when": WHEN, "fixes": 2,
                                   "distilled_from": ["a" * 16, "b" * 16, "d" * 16], "trace_refs": sorted(TRACES)}
    # a row written elsewhere (an import's) may list its refs in any order: the declaration's are sorted
    unsorted = {**e, "row": {**e["row"], "metadata": {**e["row"]["metadata"], "ol_trace_refs": json.dumps(TRACES)}}}
    assert TRACES != sorted(TRACES) and skill_provenance(unsorted)["trace_refs"] == sorted(TRACES)
    broken = {**e, "row": {**e["row"], "metadata": {**e["row"]["metadata"], "ol_trace_refs": "not json"}}}
    assert skill_provenance(broken)["trace_refs"] == []


# ---------------------------------------------------------------------------- the cause
def test_a_cause_input_carries_the_lesson_its_record_and_where_it_came_from():
    e = entry(origin="failure", fixes=3)
    assert cause_input(e) == {"kind": "memory_entry", "entry": e["memory_id"], "when": WHEN, "content": PROCEDURE,
                              "origin": "failure", "fixes": 3, "harm_tasks": 1, "shown": 4,
                              "distilled_from": ["a" * 16, "b" * 16], "provenance": skill_provenance(e)}


def test_promotable_gives_a_skill_for_a_procedure_and_a_cause_when_the_diagnosis_can_mechanise_it():
    s = Settings()
    procedure, note = entry(), entry("Do not finish before the phrase is posted.")
    young, single = entry(PROCEDURE + "\n3. Check.", fixes=1), entry(PROCEDURE + "\n- Done.", distilled=("a" * 16,))
    entries = {e["memory_id"]: e for e in (procedure, note, young, single)}
    assert [(e["memory_id"], kind) for e, kind in promotable(entries, s)] == [(procedure["memory_id"], "skill")]
    mech = promotable(entries, s, mechanizable=lambda e: e["memory_id"] == note["memory_id"])
    assert sorted((e["memory_id"], kind) for e, kind in mech) == sorted(
        [(procedure["memory_id"], "skill"), (note["memory_id"], "cause")])
    mark_superseded(entries, procedure["memory_id"], "release:x")
    assert promotable(entries, s) == []


# ---------------------------------------------------------------------------- superseded
def test_mark_superseded_names_what_superseded_the_entry_once():
    entries = {"e1": entry(), "e2": entry("Another lesson. It has two sentences here.")}
    mark_superseded(entries, "e1", "release:sha256:" + "2" * 64)
    assert (entries["e1"]["status"], entries["e1"]["superseded_by"]) == ("superseded", "release:sha256:" + "2" * 64)
    mark_superseded(entries, "e1", "release:other")
    assert entries["e1"]["superseded_by"] == "release:sha256:" + "2" * 64        # the first promotion stays named
    assert entries["e2"]["status"] == "active" and "superseded_by" not in entries["e2"]
    with pytest.raises(KeyError, match="no memory entry e3"):
        mark_superseded(entries, "e3", "release:x")


def test_a_superseded_entry_leaves_the_live_store_and_is_never_shown_again(tmp_path):
    client = FakeMemoryClient()
    task = "update the contact then post the phrase in #ops"
    step = StreamStep(client, Settings(min_score=0.3), ledger_path=tmp_path / "stream.json",
                      live_path=tmp_path / "live.jsonl", collection="ol-live-00000001", business_line="rsi",
                      activity="rsi-test", heldout_domains=("operations",), train_texts={"e" * 16: task},
                      forbidden_literals=(), trajectory_of=lambda s: [], bridge_calls_of=lambda step: [],
                      set_step=lambda step: None)
    kept, promoted = entry("Patch only the stage field. Then read it back."), entry()
    for e in (kept, promoted):
        step.entries[e["memory_id"]] = e
        client.add([e["row"]], step.collection)
    shown = LibraryReader(client, step.s, business_line="rsi", live=step.live_view()).for_task(task, "e" * 16)
    assert promoted["memory_id"] in shown.event["shown"]
    step.supersede(promoted["memory_id"], "release:sha256:" + "3" * 64)
    assert set(step.active()) == {kept["memory_id"]} and client.get([promoted["memory_id"]], step.collection) == []
    on_disk = json.loads((tmp_path / "stream.json").read_text(encoding="utf-8"))["entries"][promoted["memory_id"]]
    assert (on_disk["status"], on_disk["superseded_by"]) == ("superseded", "release:sha256:" + "3" * 64)
    assert promoted["memory_id"] not in step.snapshot_text()
    after = LibraryReader(client, step.s, business_line="rsi", live=step.live_view()).for_task(task, "e" * 16)
    assert after.event["shown"] == [kept["memory_id"]]
    assert all(e["id"] != promoted["memory_id"][:8] for e in after.recall(task))
