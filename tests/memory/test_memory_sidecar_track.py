"""StreamStep on the real path (docs/design/MEMORY.md §5, §11 "写入步").

Four to seven train tasks with fixed OpenAI-format trajectories through `trajectory_of`, the scripted bridge as ReMe's
LLM (its `calls` as bridge_calls_of, its `set_step` as the research scope's), the real sidecar. A batch is
checkpointed and a resume pays for nothing twice; a SIGKILL of the host between the store write and the ledger write
is reconciled; the ledger matches the store; the cuts are counted; a failed, missing or misattributed bridge call
stops the step; and the platform's answer to each of ReMe's defects that shows in a stream (the batch and store
dedup, the score, the counts, the reply formats, the same id twice). All slow: the full run.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys

import pytest

from fake_openai_server import LESSONS, FakeOpenAI
from memory_support import (HASH, REPO, TASKS, TESTS, TRAIN_TEXTS, Sessions, attempt, conversation,  # noqa: F401
                            items, scope_of, settings, sidecar, standard_sessions, stream, task, venv)
from reharness.contracts import MemorySidecarError
from reharness.memory.client import parse_jsonl
from reharness.memory.retrieval import LibraryReader
from reharness.memory.rules import PROVENANCE_KEYS, REME_META_KEYS, content_id
from reharness.memory.track import MemoryStepError, StreamStep, cut_trajectory

pytestmark = pytest.mark.slow

SIX = [
    ("When a task asks to email customers about overdue or unpaid invoices",
     "List the invoices first, filter them by days overdue, then send exactly one email per distinct customer."),
    ("When a task asks to copy an account manager on customer emails",
     "Look up each customer's account manager in the CRM and add them as cc on that customer's message only."),
    ("When a task asks to log each sent notice in a spreadsheet",
     "Append one row per sent email with the customer, the invoice number and the date taken from the world's "
     "records."),
    ("When a task asks to send payment reminders with a deadline",
     "State the amount due and the due date from the invoice record itself, and never invent a deadline."),
    ("When a task asks to skip customers who already paid",
     "Check the payments ledger before writing to anyone; a paid invoice gets no reminder."),
    ("When a task asks to group several overdue invoices of one customer",
     "Send one message per customer that lists all of their overdue invoices instead of one message per invoice."),
]


@pytest.fixture
def env(venv, tmp_path):
    """A sidecar and a scripted bridge of the test's own (a test may rescript the bridge's lessons)."""
    with FakeOpenAI() as f:
        c = sidecar(tmp_path, venv, llm=scope_of(f))
        try:
            yield c, f
        finally:
            c.stop()


def store_ids(client, collection) -> set:
    return {r["memory_id"] for r in parse_jsonl(client.dump(collection))}


def assert_ledger_matches_store(step, client):
    led = json.loads(step.ledger_path.read_text())
    active = {k for k, e in led["entries"].items() if e["status"] == "active"}
    assert active == store_ids(client, step.collection)
    live = parse_jsonl(step.live_path.read_text())
    assert {r["memory_id"] for r in live} == active
    stored = {r["memory_id"]: r for r in parse_jsonl(client.dump(step.collection))}
    for k in active:
        assert (led["entries"][k]["row"]["when_to_use"], led["entries"][k]["row"]["content"]) == (
            stored[k]["when_to_use"], stored[k]["content"])
    assert step.live_path.read_text() == step.snapshot_text()


# ---------------------------------------------------------------------------------------------- checkpoint, resume
def test_a_batch_is_checkpointed_and_a_resume_makes_no_call_again(env, tmp_path):
    client, f = env
    step = stream(client, f, tmp_path)
    done = step.process_batch(items())
    assert [d["n"] for d in done] == [0, 1, 2, 3] and len({d["step"] for d in done}) == 1
    calls = len(f.chat_calls())
    assert calls >= 4 * 4 and step.ledger["counters"]["summarized"] == 4
    assert step.ledger["counters"]["research_calls"] == calls and step.ledger["pending"] is None
    assert {c["step"] for c in f.chat_calls()} == {done[0]["step"]}
    assert_ledger_matches_store(step, client)
    again = stream(client, f, tmp_path)                    # a new process on the same ledger
    assert again.resume() == {"orphans_removed": [], "restored": [], "pending": None}
    assert again.process_batch(items()) == done and len(f.chat_calls()) == calls
    assert_ledger_matches_store(again, client)


CHILD = r"""
import json, os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import memory_support as ms
from fake_openai_server import Control
from reharness.memory.client import MemoryClient
from reharness.memory.track import StreamStep

root, where = Path(sys.argv[2]), sys.argv[3]
conn, bridge = json.loads(os.environ["OL_TEST_CONN"]), json.loads(os.environ["OL_TEST_BRIDGE"])
client, ctl = MemoryClient.attach(conn), Control(bridge["base_url"], bridge["token"])


class Dying(StreamStep):
    saves = 0

    def snapshot_text(self):
        if where == "after_store":          # every row of the batch is stored; the live file and ledger are old
            os.kill(os.getpid(), signal.SIGKILL)
        return super().snapshot_text()

    def _save_ledger(self):
        Dying.saves += 1
        if where == "after_live" and Dying.saves == 3:   # the seq bump, the pending summaries, then this
            os.kill(os.getpid(), signal.SIGKILL)
        super()._save_ledger()


step = Dying(client, ms.settings(), ledger_path=root / "memory" / "finance" / "stream.json",
             live_path=root / "memory" / "finance" / "live.jsonl", collection="ol-live-test0001",
             business_line="finance", activity="act-test", heldout_domains=("operations",),
             train_texts=ms.TRAIN_TEXTS, forbidden_literals=set(ms.FORBIDDEN), trajectory_of=ms.standard_sessions(),
             bridge_calls_of=ctl.calls, set_step=ctl.set_step)
step.process_batch(ms.items())
print("not killed")
"""


@pytest.mark.parametrize("where", ["after_store", "after_live"])
def test_a_sigkill_between_the_store_write_and_the_ledger_write_is_reconciled(env, tmp_path, where):
    client, f = env
    run = subprocess.run([sys.executable, "-c", CHILD, str(TESTS), str(tmp_path), where], cwd=REPO,
                         env={**os.environ, "PYTHONPATH": str(REPO), "OL_TEST_CONN": json.dumps(client.connection()),
                              "OL_TEST_BRIDGE": json.dumps({"base_url": f.base_url, "token": f.token})},
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == -signal.SIGKILL, run.stdout + run.stderr
    paid = len(f.chat_calls())
    stored = store_ids(client, "ol-live-test0001")
    led = json.loads((tmp_path / "memory" / "finance" / "stream.json").read_text())
    assert paid >= 16 and stored and led["entries"] == {} and led["processed"] == {} and led["pending"]
    if where == "after_live":                                # the live file went first: it is ahead of the ledger
        assert {r["memory_id"] for r in parse_jsonl((tmp_path / "memory" / "finance" / "live.jsonl")
                                                    .read_text())} == stored
    step = stream(client, f, tmp_path)
    got = step.resume()
    assert got["orphans_removed"] == sorted(stored) and got["restored"] == [] and got["pending"]
    assert store_ids(client, step.collection) == set()
    done = step.process_batch(items())                      # committed from the pending summaries
    assert len(f.chat_calls()) == paid and [d["n"] for d in done] == [0, 1, 2, 3]
    assert store_ids(client, step.collection) == stored     # the same library the dead process was writing
    assert_ledger_matches_store(step, client)


def test_a_failure_inside_a_commit_leaves_the_ledger_as_it_was(env, tmp_path):
    client, f = env
    clean = stream(client, f, tmp_path / "clean", collection="ol-live-clean001")
    want = clean.process_batch(items(("T1", "T3")))
    step = stream(client, f, tmp_path / "flaky", collection="ol-live-flaky001")
    real, calls = client.add, []

    def flaky(memories, collection):
        calls.append(collection)
        if len(calls) == 2:
            raise MemorySidecarError("injected: the sidecar went away mid-commit")
        return real(memories, collection)
    client.add = flaky
    try:
        with pytest.raises(MemorySidecarError, match="injected"):
            step.process_batch(items(("T1", "T3")))
    finally:
        del client.add
    paid = len(f.chat_calls())
    assert step.ledger["entries"] == {} and step.ledger["processed"] == {} and step.ledger["pending"]
    orphan = store_ids(client, step.collection)
    assert len(orphan) == 1 and step.resume()["orphans_removed"] == sorted(orphan)
    got = step.process_batch(items(("T1", "T3")))
    assert len(f.chat_calls()) == paid
    assert [d["added"] for d in got] == [d["added"] for d in want]
    assert store_ids(client, step.collection) == store_ids(client, clean.collection)


def test_a_lost_store_row_is_restored_from_the_ledger(env, tmp_path):
    client, f = env
    step = stream(client, f, tmp_path, collection="ol-live-restore1")
    step.process_batch(items(("T1", "T3")))
    before = parse_jsonl(client.dump(step.collection))
    lost = before[0]["memory_id"]
    client.retire([lost], step.collection)           # the store lost a row the ledger holds
    again = stream(client, f, tmp_path, collection="ol-live-restore1")
    assert again.resume()["restored"] == [lost]
    assert parse_jsonl(client.dump(step.collection)) == before
    assert_ledger_matches_store(again, client)


def test_a_stream_is_not_continued_under_another_k_or_into_another_collection(env, tmp_path):
    client, f = env
    step = stream(client, f, tmp_path, collection="ol-live-other001")
    step.process(*items(("T1",))[0])
    with pytest.raises(ValueError, match="settings_digest"):
        stream(client, f, tmp_path, collection="ol-live-other001", min_score=0.7)
    with pytest.raises(ValueError, match="collection"):
        stream(client, f, tmp_path, collection="ol-live-other002")


# ---------------------------------------------------------------------------------------------- the bridge check
def test_the_cuts_are_counted_as_cut(env, tmp_path):
    client, f = env
    sessions = Sessions(standard_sessions(("T1",)))
    sessions["T1-first"] = conversation("T1", False, tool_output="x" * 5000, steps=2)     # two outputs over 4,000
    sessions["T1-retry"] = conversation("T1", True, tool_output="y" * 3000, steps=30)     # ~94k chars in all
    step = stream(client, f, tmp_path, collection="ol-live-cuts0001", sessions=sessions)
    step.process(*items(("T1",))[0])
    want = {"tool_outputs_cut": 0, "messages_cut": 0, "chars_cut": 0}
    for s in ("T1-baseline", "T1-first", "T1-retry"):
        _, c = cut_trajectory(sessions[s], tool_chars=4000, total_chars=60000)
        for k in want:
            want[k] += c[k]
    got = {k: step.ledger["counters"][k] for k in want}
    assert got == want and got["tool_outputs_cut"] == 2 and got["messages_cut"] > 0
    assert got["chars_cut"] >= 2000 + 30000
    # what ReMe was sent is the cut trajectory: the first encounter's outputs, the retry's middle
    calls = f.requests(f"{step.collection}:00001")
    failure = [x["prompt"] for x in calls if x["op"] == "failure"]
    success = [x["prompt"] for x in calls if x["op"] == "success"]
    assert failure and all("x" * 4001 not in p and "1000 characters cut by the platform" in p for p in failure)
    assert any("messages (" in p and "characters) cut by the platform" in p for p in success)


def test_a_failed_bridge_call_stops_the_step_and_its_label_is_never_reused(env, tmp_path):
    client, f = env
    step = stream(client, f, tmp_path)
    f.fail("comparative", 503)
    with pytest.raises(MemoryStepError, match=r"failed on the bridge \(chatcmpl-[0-9a-f]+ comparative HTTP 503\); "
                                              r"nothing of the step is stored; 1 summaries that passed are kept"):
        step.process_batch(items(("T1", "T2")))
    led = step.ledger                                     # reloaded from disk after the failure
    assert led["seq"] == 1 and led["processed"] == {} and led["entries"] == {}
    assert store_ids(client, step.collection) == set()
    kept, = led["pending"]["tasks"]                       # the summary that passed is kept, paid once
    failed = ({HASH["T1"], HASH["T2"]} - {kept}).pop()
    first_calls = len(f.chat_calls())
    done = step.process_batch(items(("T1", "T2")))            # a new step label: the failed call does not count
    by_task = {d["n"]: d for d in done}
    assert {by_task[0]["step"], by_task[1]["step"]} == {f"{step.collection}:00001", f"{step.collection}:00002"}
    again = f.requests(f"{step.collection}:00002")
    assert again and len(f.chat_calls()) - first_calls == len(again)
    # only the failed task's trajectories were summarised again
    texts = {HASH[n]: TASKS[n] for n in ("T1", "T2")}
    assert all(texts[failed] in c["prompt"] and texts[kept] not in c["prompt"] for c in again if c["op"] != "validation")
    c = step.ledger["counters"]
    assert c["steps"] == 2 and c["research_calls_failed"] == 1 and c["summarized"] == 2
    assert c["research_calls"] == len(f.chat_calls())


def test_a_500_on_validation_stops_the_step(env, tmp_path):
    client, f = env
    step = stream(client, f, tmp_path, collection="ol-live-valstop1")
    f.fail("validation", 500, times=1)
    with pytest.raises(MemoryStepError, match=r"failed on the bridge \(.* validation HTTP 500"):
        step.process(*items(("T1",))[0])
    led = json.loads(step.ledger_path.read_text())
    assert led["processed"] == {} and led["pending"]["tasks"] == {} and led["entries"] == {}
    assert client.count(step.collection) == 0


def test_calls_that_bypass_the_bridge_are_refused(env, tmp_path):
    client, f = env
    step = stream(client, f, tmp_path, bridge_calls_of=lambda step: [])
    with pytest.raises(MemoryStepError, match="bypass the bridge"):
        step.process(*items(("T1",))[0])
    assert store_ids(client, step.collection) == set()
    # a reply whose call id the bridge holds under another op, or does not hold under this step, or answered with
    # another status, is no metered call of this step
    for i, (doctor, why) in enumerate((
            (lambda c: {**c, "op": "success"} if c["op"] == "validation" else c, "is success HTTP 200"),
            (lambda c: {**c, "call_id": c["call_id"][:-4] + "beef"}, "is not on the bridge"),
            (lambda c: {**c, "step": "another"}, "is not on the bridge"))):
        name = f"ol-live-doctor{i:02d}"
        other = stream(client, f, tmp_path / name, collection=name,
                       bridge_calls_of=lambda st, d=doctor: [x for x in (d(c) for c in f.calls(st))
                                                             if x["step"] == st])
        with pytest.raises(MemoryStepError, match=why):
            other.process(*items(("T1",))[0])
        assert store_ids(client, other.collection) == set()


def test_a_call_still_in_flight_from_another_step_is_not_taken_for_this_ones(env, tmp_path):
    """The bridge labels a call by the step it shows when the call arrives; a call of an earlier request that arrives
    after the host set this step's label is under this label. Replies are matched by call id: such a call counts as
    unattributed (paid, recorded) and its reply's format is not read as this step's."""
    client, f = env
    stale = []

    def set_step(label):
        f.set_step(label)
        if not stale:            # a request of an earlier step whose calls arrive now, one with a prose validation
            f.no_json("validation", times=1)
            stale.append(client.summarize([{"task_id": "earlier", "messages": conversation("T3", True),
                                            "score": 1.0, "metadata": {"query": TASKS["T3"]}}]))
    step = stream(client, f, tmp_path, collection="ol-live-stale001", set_step=set_step)
    done = step.process(*items(("T1",))[0])
    label = done["step"]
    theirs = {r["call_id"] for r in stale[0]["replies"]}
    assert theirs and theirs <= {c["call_id"] for c in f.calls(label)}       # under this step's label
    c = step.ledger["counters"]
    assert c["research_calls_unattributed"] == len(theirs) and c["no_json_validation"] == 0
    assert c["research_calls"] == len(f.calls(label)) and done["added"]


# ---------------------------------------------------------------------------------------------- credit and retirement
def test_credit_retirement_and_the_readout_through_the_stream(env, tmp_path):
    client, f = env
    step = stream(client, f, tmp_path, origins=("success",))
    first = step.process(task("T1"), attempt("T1", "first", True), None, None)
    target = first["added"][0]
    # three stable passes of W_base the first encounter then fails, each citing the entry: harm, credit -1 each
    for n in ("T2", "T3", "T4"):
        step.process(task(n, b=1.0, stable_pass=True), attempt(n, "first", False, shown=[target],
                                                               reason=f"tried\nlibrary-used: {target[:8]}"),
                     None, attempt(n, "baseline", True))
    e = step.entries[target]
    assert e["stats"]["n_shown"] == 3 and e["stats"]["credit_sum"] == -3.0 and len(e["stats"]["harm_tasks"]) == 3
    assert e["status"] == "retired" and e["retired"].startswith("mean credit -1.000 after 3")
    assert target not in store_ids(client, step.collection) and target not in step.live_view()["sources"]
    out = step.finish()
    assert out["abort"] == f"D {out['readout']['D']} <= 0.0" and out["readout"]["D"] == -2.5
    assert out["readout"]["D_by_origin"] == {"success": {"D": -3.0, "n": 3}} and out["readout"]["B"] == 3
    assert target not in {r["memory_id"] for r in parse_jsonl(out["snapshot"])}
    with pytest.raises(MemoryStepError, match="the stream is done"):
        step.process(*items(("T1",))[0])


def test_credit_goes_to_the_cited_entry_not_to_everything_shown(env, tmp_path):
    client, f = env
    step = stream(client, f, tmp_path, collection="ol-live-cite0001")
    shown = step.process(*items(("T1",))[0])["added"]
    assert len(shown) >= 2
    cited = shown[1]
    step.process(task("T2", b=0.0), attempt("T2", "first", True, shown=[m[:8] for m in shown],
                                            reason=f"ok\nlibrary-used: {cited[:8]}"),
                 None, attempt("T2", "baseline", False))
    stats = {m: step.entries[m]["stats"] for m in shown}
    assert stats[cited]["credit_sum"] == 1.0 and stats[cited]["n_cited"] == 1 and stats[cited]["fixes"] == 1
    assert all(stats[m]["credit_sum"] == 0.0 and stats[m]["n_shown"] == 1 for m in shown if m != cited)
    # the counts live in the ledger and the snapshot, never in ReMe's metadata
    rows = parse_jsonl(client.dump(step.collection))
    assert not any(k in r["metadata"] for r in rows for k in ("freq", "utility", "ol_stats", "ol_sources"))
    snap = {r["memory_id"]: json.loads(r["metadata"]["ol_stats"]) for r in parse_jsonl(step.snapshot_text())}
    assert all(snap[m]["n_shown"] == 1 for m in shown) and snap[cited]["n_cited"] == 1


def test_capacity_evicts_the_lowest_proven_entry_only_when_full(env, tmp_path):
    client, f = env
    f.lessons = {
        "account manager": {"success": [("When a task asks to email customers about overdue or unpaid invoices",
                                         "List the invoices first, then email each customer once.", "action")]},
        "engaged": {"success": [("When a task asks to update CRM records for contacts who replied to a campaign",
                                 "Map each reply to its CRM record by email before any write.", "action")]},
        "revenue": {"success": [("When a task asks to post computed totals to a team chat channel",
                                 "Compute the totals from the sheet first and post one message with all of them.",
                                 "action")]}}
    sessions = standard_sessions(("T1", "T2", "T3", "T4", "T5", "T7"))
    step = stream(client, f, tmp_path, collection="ol-live-capac001", origins=("success",), max_active=2,
                  min_score=0.3, sessions=sessions)
    a, = step.process(task("T1"), attempt("T1", "first", True), None, None)["added"]
    b, = step.process(task("T3"), attempt("T3", "first", True), None, None)["added"]
    for n, reason, passed in (("T2", f"library-used: {a[:8]}", True), ("T4", "library-used: none", True),
                              ("T5", None, False)):
        step.process(task(n, b=0.0), attempt(n, "first", passed, shown=[a, b], reason=reason), None, None)
    assert [step.entries[x]["stats"]["n_shown"] for x in (a, b)] == [3, 3]
    done = step.process(task("T7"), attempt("T7", "first", True), None, None)
    c, = done["added"]
    assert done["evicted"] == [b] and step.entries[b]["retired"] == "capacity 2"
    assert store_ids(client, step.collection) == {a, c} and len(step.active()) == 2


def test_retired_content_does_not_come_back(env, tmp_path):
    client, f = env
    step = stream(client, f, tmp_path, collection="ol-live-noreturn", origins=("success",),
                  sessions=standard_sessions(("T1", "T2", "T3", "T4", "T6")))
    target, = step.process(task("T1"), attempt("T1", "first", True), None, None)["added"]
    for n in ("T3", "T4", "T6"):          # three cited failures of stable passes: retired after probation
        step.process(task(n, b=1.0, stable_pass=True), attempt(n, "first", False, shown=[target],
                                                               reason=f"library-used: {target[:8]}"), None, None)
    assert step.entries[target]["status"] == "retired" and target not in store_ids(client, step.collection)
    done = step.process(task("T2"), attempt("T2", "first", True), None, None)   # T2 distils the same lesson
    assert done["added"] == [] and done["dropped"] == [f"retired before: {step.entries[target]['retired']}"]
    assert target not in store_ids(client, step.collection)


def test_a_superseded_entry_leaves_the_store_and_is_never_shown_again(env, tmp_path):
    client, f = env
    step = stream(client, f, tmp_path, collection="ol-live-supers01", origins=("success",))
    target, = step.process(task("T1"), attempt("T1", "first", True), None, None)["added"]
    reader = LibraryReader(client, step.s, business_line="finance", live=step.live_view())
    assert reader.for_task(TASKS["T5"], HASH["T5"]).event["shown"] == [target]
    step.supersede(target, "skill:invoices")
    assert step.entries[target]["status"] == "superseded" and target not in store_ids(client, step.collection)
    assert target not in {r["memory_id"] for r in parse_jsonl(step.live_path.read_text())}
    reader = LibraryReader(client, step.s, business_line="finance", live=step.live_view())
    assert reader.empty and reader.for_task(TASKS["T5"], HASH["T5"]).event is None
    assert_ledger_matches_store(step, client)


def test_a_lost_first_encounter_is_recorded_and_never_summarised(env, tmp_path):
    client, f = env
    step = stream(client, f, tmp_path, collection="ol-live-lost0001")
    assert step.plan([HASH[n] for n in ("T1", "T2")]) == [HASH["T1"], HASH["T2"]]
    step.lose(HASH["T1"], "the first encounter is infrastructure")
    assert step.plan([HASH[n] for n in ("T1", "T2")]) == [HASH["T2"]]
    with pytest.raises(MemoryStepError, match="is fixed"):
        step.plan([HASH[n] for n in ("T2", "T1")])
    step.process(*items(("T2",))[0])
    assert f.chat_calls() and all(TASKS["T1"] not in c["prompt"] for c in f.requests())
    out = step.finish()
    assert out["readout"]["tasks"] == 1 and step.ledger["lost"] == {HASH["T1"]: "the first encounter is "
                                                                                "infrastructure"}


# ---------------------------------------------------------------------------------------------- the memory checks
def test_the_platform_checks_literal_length_and_reach_before_anything_is_stored(env, tmp_path):
    client, f = env
    f.lessons = {"invoice": {"success": [
        # a train task's own value: replaced by <value> in one placeholder pass, then stored
        ("When a task asks to email customers about overdue or unpaid invoices",
         "Look the invoice up by its number (for example INV-20417) before writing to the customer.", "action"),
        # longer than K's content cap
        ("When a task asks to email customers about their invoices", "Do it carefully. " * 120, "action"),
        # reaches no train task beyond its source at min_score
        ("When the agent needs to compute a moving average of sensor telemetry",
         "Use a fixed window and drop the incomplete edges.", "reasoning")]}}
    step = stream(client, f, tmp_path, collection="ol-live-checks01", origins=("success",))
    done = step.process(task("T1"), attempt("T1", "first", True), None, None)
    assert done["validated"] == 3 and len(done["added"]) == 1
    assert sorted(d.split(":")[0] for d in done["dropped"]) == ["length", "reach"]
    row, = parse_jsonl(client.dump(step.collection))
    assert "<value>" in row["content"] and row["metadata"]["step_type"] == "action"
    assert row["memory_id"] == done["added"][0] and step.ledger["counters"]["placeholder_fixed"] == 1
    assert row["memory_id"] == content_id(row["content"])     # the id follows the fixed content
    # the literal is nowhere: not in ReMe's metadata (the model's raw item), the snapshot, the live file, the ledger
    assert set(row["metadata"]) <= set(PROVENANCE_KEYS) | set(REME_META_KEYS)
    assert "experience" not in row["metadata"] and "when_to_use" not in row["metadata"]
    for text in (json.dumps(row), step.snapshot_text(), step.live_path.read_text(), step.ledger_path.read_text()):
        assert "INV-20417" not in text
    assert {d["why"].split(":")[0] for d in step.ledger["dropped"]} == {"length", "reach"}


def test_the_models_extra_keys_never_override_the_stored_memory(env, tmp_path):
    """ReMe spreads metadata over the vector node's own fields (memory_type, content, score); a model item carrying
    such keys, or a literal in its tags, must leave the stored row and retrieval intact."""
    client, f = env
    f.lessons = {"invoice": {"success": [
        ("When a task asks to email customers about overdue or unpaid invoices",
         "List the invoices first, filter them by days overdue, then email each distinct customer once.", "action",
         {"memory_type": "bogus", "content": "overridden", "score": 99, "tags": ["INV-20417", "email"],
          "tools_used": ["invoices_list"], "author": "the model"})]}}
    step = stream(client, f, tmp_path, collection="ol-live-extra001", origins=("success",))
    done = step.process(task("T1"), attempt("T1", "first", True), None, None)
    row, = parse_jsonl(client.dump(step.collection))
    assert row["memory_type"] == "procedural" and row["author"] == "research/reme:success"
    assert row["content"].startswith("List the invoices first") and row["memory_id"] == done["added"][0]
    assert json.loads(row["metadata"]["tags"]) == ["<value>", "email"] and "INV-20417" not in json.dumps(row)
    got = client.retrieve(TASKS["T5"], step.collection, where={"ol_line": "finance"}, top_k=3)
    assert [m["memory_id"] for m in got] == done["added"]


def test_the_validation_score_survives_retrieval(env, tmp_path):
    client, f = env
    step = stream(client, f, tmp_path, collection="ol-live-score001")
    step.process(*items(("T1",))[0])
    for _ in range(2):
        got = client.retrieve(TASKS["T5"], step.collection, where={"ol_line": "finance"}, top_k=5)
        assert got and all(m["metadata"]["retrieval_score"] == m["score"] for m in got)
        assert all(m["score"] != 0.8 for m in got)            # chroma's similarity replaced ReMe's score
    rows = parse_jsonl(client.dump(step.collection))
    assert rows and all(r["metadata"]["ol_validation_score"] == 0.8 for r in rows)
    assert all("retrieval_score" not in r["metadata"] for r in rows)


def test_no_segmentation_op_and_no_segments(env, tmp_path):
    client, f = env
    cfg = json.loads((client.workdir / "reme_config.yaml").read_text())
    flow = cfg["flows"]["summary_task_memory_v6"]["flow_content"]
    assert "TrajectorySegmentation" not in flow and flow.endswith("BatchOnlyDeduplication(raise_exception=True, "
                                                                  "max_retries=1)")
    step = stream(client, f, tmp_path, collection="ol-live-noseg001")
    trajs, _ = step._trajectories(*items(("T1",))[0])
    assert trajs and all("segments" not in t["metadata"] for t in trajs)
    assert [t["metadata"]["kind"] for t in trajs] == ["baseline", "first", "retry"]


# ---------------------------------------------------------------------------------------------- dedup
def test_batch_dedup_at_085_keeps_distinct_memories_where_05_kept_one(venv, tmp_path):
    lessons = {"invoice": {"success": [(w, e, "action") for w, e in SIX]}}
    one = [{"task_id": HASH["T1"], "messages": conversation("T1", True), "score": 1.0,
            "metadata": {"query": TASKS["T1"]}}]
    with FakeOpenAI(lessons=lessons) as f:
        kept = {}
        for thr in (0.85, 0.5):
            c = sidecar(tmp_path, venv, llm=scope_of(f), name=f"thr{thr}", batch_dedup_threshold=thr)
            try:
                got = c.summarize(one)
                assert got["validated"] == 6
                kept[thr] = len(got["memory_list"])
            finally:
                c.stop()
    assert kept[0.85] == 5        # the one pair above 0.85 (0.867) is the only merge
    assert kept[0.5] == 1         # ReMe's default threshold: every pair is above 0.5


def test_near_duplicates_written_twice_are_one_entry(env, tmp_path):
    client, f = env
    f.lessons = {"account manager": {"success": [("When a task asks to email customers about overdue invoices",
                                                  "Send one email to each customer that has an overdue invoice.",
                                                  "action")]},
                 "payment reminder": {"success": [("When a task asks to email customers about their overdue invoices",
                                                 "Send exactly one email to every customer with an overdue invoice.",
                                                 "action")]}}
    step = stream(client, f, tmp_path, collection="ol-live-dup00001", origins=("success",))
    a, b = step.process_batch([(task(n), attempt(n, "first", True), None, None) for n in ("T1", "T2")])
    assert len(a["added"]) == 1 and b["added"] == [] and b["duplicates"][0]["how"] == "store_dedup"
    assert b["duplicates"][0]["dup_of"] == a["added"][0] and b["duplicates"][0]["score"] >= 0.9
    assert b["duplicates"][0]["content_score"] >= 0.85
    assert client.count(step.collection) == 1
    assert set(step.entries[a["added"][0]]["sources"]) >= {HASH["T1"], HASH["T2"]}


def test_the_same_trigger_with_another_lesson_is_a_second_entry(env, tmp_path):
    """when_to_use alone would fold a failure lesson into the success lesson of the same situation (its content
    lost, its credit read as the other's): the contents must be close as well."""
    client, f = env
    f.lessons = {"account manager": {"success": [("When a task asks to email customers about overdue invoices",
                                                  "Send one email to each customer that has an overdue invoice.",
                                                  "action")]},
                 "payment reminder": {"failure": [("When a task asks to email customers about their overdue invoices",
                                                   "Derive today's date from the newest timestamps in the world's "
                                                   "records, never from the system clock.", "reasoning")]}}
    step = stream(client, f, tmp_path, collection="ol-live-two00001", origins=("success", "failure"))
    a, b = step.process_batch([(task("T1"), attempt("T1", "first", True), None, None),
                               (task("T2"), attempt("T2", "first", False), None, None)])
    assert len(a["added"]) == 1 and len(b["added"]) == 1 and b["duplicates"] == []
    assert client.count(step.collection) == 2
    assert {step.entries[m]["origin"] for m in a["added"] + b["added"]} == {"success", "failure"}


def test_the_same_content_twice_is_one_row_with_two_sources(env, tmp_path):
    client, f = env
    content = "Check the recipient list against the records before the first write and touch only those."
    f.lessons = {"invoice": {"success": [("When a task asks to email customers about overdue or unpaid invoices",
                                          content, "action")]},
                 "crm": {"success": [("When a task asks to update CRM records for contacts who replied to a "
                                      "campaign", content, "action")]}}
    step = stream(client, f, tmp_path, collection="ol-live-sameid01", origins=("success",))
    a, b = step.process_batch([(task(n), attempt(n, "first", True), None, None) for n in ("T1", "T3")])
    assert len(a["added"]) == 1 and b["added"] == [] and b["duplicates"] == [
        {"memory_id": a["added"][0], "dup_of": a["added"][0], "how": "same_id"}]
    mid = a["added"][0]
    assert client.count(step.collection) == 1 and set(step.entries[mid]["sources"]) >= {HASH["T1"], HASH["T3"]}
    assert [ev["how"] for ev in step.entries[mid]["events"]] == ["added", "same_id"]


# ---------------------------------------------------------------------------------------------- reply formats
def test_a_validation_reply_without_json_is_rejected_and_counted(env, tmp_path):
    client, f = env
    step = stream(client, f, tmp_path, collection="ol-live-json0001", origins=("success",))
    f.no_json("validation", times=1)
    done = step.process(task("T1"), attempt("T1", "first", True), None, None)
    assert step.ledger["counters"]["no_json_validation"] == 1
    assert done["validated"] == 0 and done["added"] == [] and client.count(step.collection) == 0
    assert step.finish()["readout"]["reply_format"] == ["no_json_validation: 1"]


def test_an_extraction_reply_without_json_is_counted_not_silent(env, tmp_path):
    """ReMe's parse_json_experience_response only warns on a reply without JSON and extracts nothing: the step goes
    on (a model can rightly find no lesson), and the reply is counted for the canary."""
    client, f = env
    step = stream(client, f, tmp_path, collection="ol-live-extr0001", origins=("success",))
    f.no_json("success", times=1)
    done = step.process(task("T1"), attempt("T1", "first", True), None, None)
    assert done["validated"] == 0 and done["added"] == []
    c = step.ledger["counters"]
    assert c["no_json_extraction"] == 1 and c["no_json_validation"] == 0
    # a bare JSON reply (no fence) is what ReMe's fallback reads: not counted
    f.reply_with("success", json.dumps([{"when_to_use": "When a task asks to email customers about overdue or "
                                                        "unpaid invoices", "experience": "List the invoices first, "
                                                        "then email each customer once.", "step_type": "action"}]))
    done = step.process(task("T2"), attempt("T2", "first", True), None, None)
    assert done["validated"] == 1 and step.ledger["counters"]["no_json_extraction"] == 1
    assert step.finish()["readout"]["reply_format"] == ["no_json_extraction: 1"]


def test_a_validation_reply_remes_parser_cannot_read_is_counted_not_a_failed_step(env, tmp_path):
    """A score that is not a number makes ReMe's parse_validation log at ERROR and call the memory invalid: a model's
    format, counted (unparsed_validation), not a failure of the whole paid step."""
    client, f = env
    step = stream(client, f, tmp_path, collection="ol-live-bad00001", origins=("success",))
    f.reply_with("validation", '```json\n{"is_valid": true, "score": "high", "feedback": "x"}\n```')
    done = step.process(task("T1"), attempt("T1", "first", True), None, None)
    assert done["validated"] == 0 and done["added"] == []
    c = step.ledger["counters"]
    assert c["unparsed_validation"] == 1 and c["no_json_validation"] == 0 and c["steps"] == 1


def test_more_validated_memories_than_validation_calls_is_refused(env, tmp_path):
    """A summary that reports memories validated without a validation call for each did not run ReMe's validation
    through the bridge."""
    client, f = env
    real = client.summarize

    def fewer(trajectories):             # one validation call left out of the summary's report
        got = real(trajectories)
        first = next(r for r in got["replies"] if r["op"] == "validation")
        return {**got, "replies": [r for r in got["replies"] if r is not first]}
    client.summarize = fewer
    try:
        step = stream(client, f, tmp_path, collection="ol-live-fewer001")
        with pytest.raises(MemoryStepError, match="validated memories from"):
            step.process(*items(("T1",))[0])
    finally:
        del client.summarize
    assert client.count("ol-live-fewer001") == 0


def test_a_summary_without_a_call_its_trajectories_need_is_refused(env, tmp_path):
    """A success trajectory needs ReMe's success extraction: a summary with no such call, on the bridge or in the
    sidecar's report, did not run it through the metered scope."""
    client, f = env
    real = client.summarize

    def without_success(trajectories):
        got = real(trajectories)
        return {**got, "replies": [r for r in got["replies"] if r["op"] != "success"]}
    client.summarize = without_success
    try:
        step = stream(client, f, tmp_path, collection="ol-live-noop0001",
                      bridge_calls_of=lambda st: [c for c in f.calls(st) if c["op"] != "success"])
        with pytest.raises(MemoryStepError, match=r"no \['success'\] call for trajectories that need one"):
            step.process(*items(("T1",))[0])
    finally:
        del client.summarize
    assert client.count("ol-live-noop0001") == 0
