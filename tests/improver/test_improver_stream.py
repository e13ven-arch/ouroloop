"""The streaming loop (DESIGN §6.13; IMPROVER §3.1; ACTIVITY §4 `stream`) on the fake upstream's rsi domain under the
two-way split, no baseline: the forgetful actor, the scripted R writing a tool. Under K's order seed 0 the stream's order
is policy_hold, notify_after_update_variant, notify_once, notify_ticket | notify_after_update, update_stage, so with a
batch of four the first batch holds the studied failure (notify_ticket) and a held-out one (the variant), and the second
batch runs on the line the first round installed.

Covered: each task is scored once at its first encounter and never paid for again; a round runs after every batch over
the scored pool without measuring, its studied and held-out parts the activity's split restricted to the pool, its
screen on the pool's held-out tasks; an accepted package is installed before the next batch, which runs on the new
line; the second round diagnoses the stale failure again and R's tool, laid over a manifest that already lists
it, fails V0, so the round stops `no_candidate`; `--until` stops after a batch and a later run continues; the stream's ledger, the `stream_scored`
events, the report's stream section and the per-batch accounts; the release by decision reads what W* lacks; a
configuration this version does not stream is refused before anything runs."""
from __future__ import annotations

import json

import pytest
import rsi
from rounds import ledger_rows, quiet, round_activity, round_state

from ouroloop.activity import phases
from ouroloop.contracts import short
from ouroloop.improver.screen import ScreenResult

TICKET, VARIANT, NOTIFY = "rsi.notify_ticket", "rsi.notify_after_update_variant", "rsi.notify_after_update"
ORDER = ["rsi.policy_hold", VARIANT, "rsi.notify_once", TICKET, NOTIFY, "rsi.update_stage"]


def stream_rows(a) -> list:
    runs = a.results.runs()
    return [r for r in a.results.all() if runs[r.run].role == "stream"]


def stream_state(a) -> dict:
    return json.loads((a.root / "rsi" / "stream.json").read_text(encoding="utf-8"))


def test_the_stream_scores_each_task_once_runs_a_round_every_batch_and_the_next_batch_runs_on_the_new_line(
        tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "tool", baseline=False, splits_table=rsi.NO_DEV_SPLITS,
                       schedule={"stream_batch": 4})
    a = s.a
    seed = a.seed_release()
    assert phases.stream_order_of(a) == ORDER
    lines = []
    report = phases.stream(a, until=0, out=lines.append)
    # batch 0: four first encounters on W0, two pass (the forgetful actor fails the variant and the ticket)
    assert report["status"] == "stopped" and report["at"] == "batch-0"
    state = stream_state(a)
    assert state["order"] == ORDER and state["split"] == "train" and [b["index"] for b in state["batches"]] == [0]
    b0 = state["batches"][0]
    assert b0["tasks"] == ORDER[:4] and b0["release"] == seed.ref() and (b0["passed"], b0["tasks_scored"]) == (2, 4)
    rows = stream_rows(a)
    assert sorted(r.task_id for r in rows) == sorted(ORDER[:4]) and all(r.system_release_ref == seed.ref() for r in rows)
    assert {r.task_id: r.success for r in rows} == {ORDER[0]: True, VARIANT: False, ORDER[2]: True, TICKET: False}
    events = list(a.events.read("stream_scored"))
    assert [(e["batch"], e["task"], e["success"]) for e in events] == [(0, t, t not in (VARIANT, TICKET))
                                                                       for t in ORDER[:4]]
    # round s0 over the pool of four: nothing measured; the ticket studied and failed -> R's tool; the variant held out
    # and fixed by it -> accepted and installed before the next batch
    r0 = round_state(a, "s0")
    assert r0["round_id"] == "rsi-test/s0" and r0["pool"] == ORDER[:4] and r0["status"] == "done"
    assert r0["account"] == "rsi-test:round-s0" and r0["base_release"] == seed.ref()
    measure = r0["steps"]["measure"]
    assert (measure["measured"], measure["tasks"], measure["passed"], measure["pool"]) == (0, 4, 2, 4)
    assert (measure["holdout"], measure["holdout_passed"]) == (1, 0)
    assert r0["steps"]["evidence"]["failed_tasks"] == [TICKET]
    sc = ScreenResult.from_json(a.objects.get(r0["steps"]["screened"]["screens"][0]["screen"]))
    assert sc.candidate == "s0:package" and sc.holdout == (VARIANT,) and sc.fixed == (VARIANT,) and sc.accepted
    assert (sc.holdout_score, sc.best_so_far, sc.net_flips) == (1, 0, 1)
    installed = b0["round"]["installed"]
    assert b0["round"]["accepted"] and installed == a.active_release().ref() != seed.ref()
    assert (a.root / "rounds" / "s0" / "candidates" / "package" / "files.json").exists()
    assert not (a.root / "rounds" / "r0").exists()
    book = json.loads((a.root / "rsi" / "budget" / "book.json").read_text(encoding="utf-8"))["scopes"]
    assert book["rsi-test:stream"]["parent"] == "rsi-test" and book["rsi-test:round-s0"]["parent"] == "rsi-test"
    assert book["rsi-test:round-s0:screen"]["parent"] == "rsi-test:round-s0"
    calls = s.actor.calls
    # batch 1 on the new line: both first encounters pass (the tool posts the message); round s1 over the pool of six
    # sees the stale ticket failure, R's tool again is a duplicate of round s0's: no candidate
    report = phases.stream(a, out=lines.append)
    assert report["status"] == "done" and "at" not in report
    state = stream_state(a)
    b1 = state["batches"][1]
    assert b1["tasks"] == ORDER[4:] and b1["release"] == installed and (b1["passed"], b1["tasks_scored"]) == (2, 2)
    rows = stream_rows(a)
    assert len(rows) == 6 and sorted(r.task_id for r in rows) == sorted(ORDER)
    assert all(r.system_release_ref == installed for r in rows if r.task_id in ORDER[4:])
    assert [(e["batch"], e["task"]) for e in a.events.read("stream_scored")][4:] == [(1, NOTIFY), (1, ORDER[5])]
    r1 = round_state(a, "s1")
    assert r1["pool"] == ORDER and r1["base_release"] == installed and r1["status"] == "stopped"
    assert r1["stop"].startswith("no_candidate") and r1["steps"]["evidence"]["failed_tasks"] == [TICKET]
    assert r1["steps"]["measure"]["measured"] == 0 and (r1["steps"]["measure"]["holdout"],
                                                        r1["steps"]["measure"]["holdout_passed"]) == (2, 2)
    # (R's second tool is laid over a manifest that already lists it: the package fails V0, so nothing is screened)
    rows_v = [r for r in ledger_rows(a, "verified") if r["round_id"] == "rsi-test/s1"]
    assert [r["passed"] for r in rows_v] == [False] and "screened" not in r1["steps"]
    fe = state["first_encounter"]
    assert (fe["tasks"], fe["passed"], fe["pass_rate"]) == (6, 4, 0.6667)
    assert (fe["first_half"], fe["second_half"]) == (0.6667, 0.6667)
    assert state["status"] == "done" and a.active_release().ref() == installed
    assert any(line.startswith("stream     batch 1 on") and "2/2 first encounters passed" in line for line in lines)
    # run again: everything is on record, nothing is paid, the same ledger
    before = s.actor.calls
    again = phases.stream(a, out=quiet)
    assert again["status"] == "done" and s.actor.calls == before and len(stream_rows(a)) == 6
    assert stream_state(a) == state and calls < before
    # the report reads the stream and both rounds, with each round's own account
    rep = phases.report(a, write=False, out=quiet)
    assert [r["round"] for r in rep["rounds"]] == ["rsi-test/s0", "rsi-test/s1"]
    assert rep["rounds"][0]["installed"] == short(installed) and rep["rounds"][0]["spend"].get("usd")
    assert [b["passed"] for b in rep["stream"]["batches"]] == [2, 2]
    assert rep["stream"]["batches"][0]["round"]["installed"] == short(installed)
    assert rep["stream"]["batches"][1]["round"]["status"] == "stopped"
    assert rep["stream"]["first_encounter"] == fe
    # the release by decision: the finished rounds do not refuse it; W*'s package lacks the three studied tasks the
    # seed scored and reads them under its own release
    released = phases.release(a, out=quiet)
    assert released["measured"] == 3 and a.releases.released().parent_ref == installed
    assert phases.unfinished_rounds(a) == []


def test_a_stream_this_version_cannot_run_is_refused_before_anything_is_paid(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "tool", baseline=False, splits_table=rsi.NO_DEV_SPLITS,
                       schedule={"stream_splits": ["train", "test"]})
    with pytest.raises(NotImplementedError, match="train split only"):
        phases.stream(s.a, out=quiet)
    assert s.actor.calls == 0 and not (s.a.root / "rsi" / "stream.json").exists()
    with_memory = round_activity(tmp_path / "m", fake_upstream, "tool", baseline=False, memory=True,
                                 splits_table=rsi.NO_DEV_SPLITS)
    with pytest.raises(NotImplementedError, match="memory"):
        phases.stream(with_memory.a, out=quiet)
    assert with_memory.actor.calls == 0
