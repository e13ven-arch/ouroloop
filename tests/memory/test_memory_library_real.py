"""The library phase end to end on the real path (docs/design/MEMORY.md §7, §11 "library 阶段 ... 真 sidecar 一例").

The miniature upstream's rsi domain (tests/rsi.py), the scripted actor that forgets the #ops notification unless a note
in front of it says otherwise (BusinessWorker(forget_notify=True)), and the memory as it runs in an activity: the real
sidecar (ReMe 0.3.1.10, chroma, bge-small) in the memory venv, the real bridge and research scope in front of a
scripted research model that answers ReMe's own prompts (fake_memory.ReMeResearch), K's thresholds as calibrated for
bge-small (min_score 0.62, two other tasks reached). W_base's baseline, then the stream: the lessons of the first
failures reach the later #ops tasks, so D > 0 and the snapshot is a candidate package; run again, nothing is paid twice.
"""
from __future__ import annotations

import json

import pytest

import rsi
from fake_memory import ReMeResearch
from memory_support import venv  # noqa: F401
from reharness.activity import phases
from reharness.harness.kinds import MEMORY_PATH
from reharness.harness.package import load as load_package
from reharness.memory.client import parse_jsonl

MEMORY = {"enabled": True, "min_score": 0.62, "min_other_tasks": 2}


def quiet(_line):
    pass


def open_rsi(tmp_path, upstream, worker, research):
    # the stream's order of tests/activity/test_activity_library.py (seed 349): a failing notify task comes after the
    # opportunity lesson and before the other #ops tasks, so a lesson of its own fixes the next one (D > 0)
    return rsi.activity(tmp_path, upstream, provider=worker, research=research, memory=dict(MEMORY),
                        gate={"order_seed": 349})


def spent(activity, phase: str) -> dict:
    return activity.account(phase).remaining()["used"]


@pytest.mark.slow
def test_the_library_phase_on_the_real_sidecar_makes_a_snapshot_candidate_and_pays_once(venv, tmp_path, fake_upstream,
                                                                                          monkeypatch):
    monkeypatch.setenv("REHARNESS_MEMORY_VENV", str(venv))
    worker, research = rsi.BusinessWorker(forget_notify=True), ReMeResearch()
    a = open_rsi(tmp_path, fake_upstream, worker, research)
    try:
        phases.baseline(a, out=quiet)
        after_baseline = worker.calls
        lines = []
        got = phases.library(a, out=lines.append)
    finally:
        a.close()
    assert a.memory().client is None                 # close() stopped the sidecar, the scope and the bridge
    assert got["status"] == "done", lines
    ro, snap = got["readout"], got["snapshot"]
    assert ro["D"] > 0 and ro["entries"]["active"] > 0 and ro["tasks"] == len(rsi.TRAIN), lines
    assert snap["outcome"] == "candidate", (snap, lines)
    assert worker.calls > after_baseline              # the stream's first encounters ran under the live library

    # every model call ReMe made went through the bridge: answered 200, recorded (train data), metered on the
    # activity's research:memory account
    record = tmp_path / "ws" / "memory" / rsi.DOMAIN / "bridge_calls.jsonl"
    calls = [json.loads(x) for x in record.read_text(encoding="utf-8").splitlines()]
    assert calls and all(c["status"] == 200 for c in calls) and len(calls) == len(research.calls)
    assert {c["op"] for c in calls} <= {"success", "failure", "comparative", "validation"}
    assert ro["counters"]["research_calls"] == len(calls) and ro["counters"]["research_calls_unattributed"] == 0
    research_spend = spent(a, "research:memory")
    assert research_spend["model_calls"] == len(calls) and research_spend["usd"] > 0

    # the candidate: W_base with memory/snapshot.jsonl holding the library (its rows as a package carries them, all
    # within K's max_memory_bytes here) and the manifest naming it; each row's trace refs are the ledger's
    files = load_package(a.objects, snap["package_ref"]).files
    rows = parse_jsonl(files[MEMORY_PATH])
    assert len(rows) == ro["entries"]["active"]
    assert all(r["metadata"]["ol_split"] == "train" and r["metadata"]["ol_line"] == rsi.DOMAIN
               and json.loads(r["metadata"]["ol_sources"]) and "ol_trace_refs" not in r["metadata"] for r in rows)
    ledger = json.loads((tmp_path / "ws" / "memory" / rsi.DOMAIN / "stream.json").read_text(encoding="utf-8"))
    assert all(json.loads(ledger["entries"][r["memory_id"]]["row"]["metadata"]["ol_trace_refs"]) for r in rows)
    assert json.loads(files["manifest.json"])["memory"] == {"enabled": True, "snapshot": MEMORY_PATH}
    base = a.package(a.active_release()).files
    assert {p for p in files if files[p] != base.get(p)} == {MEMORY_PATH, "manifest.json"}
    assert [e["package_ref"] for e in a.events.read("library_snapshot")] == [snap["package_ref"]]

    # again, as a new process on the same workspace: nothing on record is paid for twice
    actor_before, research_before = worker.calls, len(research.calls)
    again = open_rsi(tmp_path, fake_upstream, worker, research)
    try:
        second = phases.library(again, out=quiet)
    finally:
        again.close()
    assert worker.calls == actor_before and len(research.calls) == research_before
    assert spent(again, "research:memory") == research_spend
    assert second["readout"] == ro and second["snapshot"] == snap
    assert len(list(again.events.read("library_snapshot"))) == 1
