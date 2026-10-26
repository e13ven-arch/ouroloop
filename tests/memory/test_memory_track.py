"""StreamStep, the write path of the train stream (docs/design/MEMORY.md §5, §7; §11 "写入步").

The in-process client (tests/memory/fake_memory.py FakeMemoryClient) makes its summaries as real HTTP calls to a
research scope of the real bridge (reharness/memory/bridge.py), whose provider is a MeteredProvider over ScriptedResearch
on a Budgets book: every research call is metered, booked and recorded, and StreamStep checks each summary against the
scope's record (`scope.calls`, `scope.set_step`). `trajectory_of` gives canned OpenAI-format messages. The scenarios are
v6's (tests/test_memory_track.py and tests/test_memory_pitfalls.py on tag v6-memory) that make sense without the real
sidecar: each of the eight steps, resume after a crash, capacity, retirement, plan, lose, supersede, imports, the
readout and the ledger's identity.

Under the fake's bag-of-words vectors and min_score 0.4 the texts below score (when_to_use against task text):
OPS_WHEN 0.52 on T1, T2, T4; OPP_WHEN 0.48 on T1-T4; INV_WHEN 0.58/0.56 on T5/T6; REV_WHEN 0.78 on T7, T8; ARCH_WHEN
0.57 on T9 and T10 (which are siblings: the same words); LEA_WHEN 0.73 on T3 only; FAR_WHEN under 0.3 everywhere.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from fake_memory import FakeMemoryClient, ScriptedResearch, bow, cosine
from reharness.contracts import MemorySidecarError, TrainOnlyError
from reharness.evaluate.budget import Budgets, MeteredProvider, Pricing
from reharness.memory import snapshot_digest, track
from reharness.memory.bridge import Bridge
from reharness.memory.client import canonical_rows, parse_jsonl
from reharness.memory.retrieval import LibraryReader
from reharness.memory.rules import PROVENANCE_KEYS, REME_META_KEYS, content_id, new_stats, stamp
from reharness.memory.service import LLM_OPS
from reharness.memory.settings import Settings
from reharness.memory.snapshot import fit_snapshot, row_bytes
from reharness.memory.track import MemoryStepError, StreamStep, cut_trajectory

LINE = "crm"
ACTIVITY = "act-track"
COLL = "ol-live-track001"
ACCOUNT = "track:research:memory"
LITERAL = "INV-20417"                  # a value of exactly one train task (T6): that task's answer
TASKS = {
    "T1": "Move Dana Reyes to opportunity, then post deal moved in #ops.",
    "T2": "Move Sam Okafor to opportunity, then post stage changed in #ops.",
    "T3": "Move Lea Fontaine to opportunity.",
    "T4": "Move Kai Mercer to opportunity, then post call booked in #ops.",
    "T5": "Email each customer with an overdue invoice and copy their account manager.",
    "T6": f"Email each customer with an unpaid invoice and log invoice {LITERAL} in the sheet.",
    "T7": "Post weekly revenue totals from the sheet to #finance.",
    "T8": "Post monthly revenue totals from the sheet to #sales.",
    "T9": "Archive the closed tickets of Priya Nair.",
    "T10": "Archive the closed tickets of Priya Nair!",
}
HASH = {n: hashlib.sha256(f"task:{n}".encode()).hexdigest()[:16] for n in TASKS}
TRAIN_TEXTS = {HASH[n]: t for n, t in TASKS.items()}

OPS_WHEN = "When a request asks to move a contact and then post a phrase in #ops"
OPS_SUCCESS = ("1. Move the contact first.\n2. Then post the requested phrase in #ops; the request is not done until "
               "it is posted.")
OPS_FAILURE = "Do not finish after the move alone: post the phrase in #ops before you finish."
OPS_COMPARATIVE = ("The higher-scoring run posted the phrase in #ops after the move; the lower-scoring run stopped "
                   "after it.")
OPP_WHEN = "When a request asks to move a contact to opportunity"
OPP_SUCCESS = "Read the contact, then send one PATCH that sets only the stage field to opportunity."
INV_WHEN = "When asked to email each customer with an overdue or unpaid invoice"
REV_WHEN = "When asked to post revenue totals from the sheet"
REV_SUCCESS = "Compute the totals from the sheet first and post one message with all of them."
ARCH_WHEN = "When asked to archive the closed tickets"
LEA_WHEN = "When asked to move Lea Fontaine"
FAR_WHEN = "When the agent needs to compute a moving average of sensor telemetry"
A, B, C, D = (content_id(x) for x in (OPS_SUCCESS, OPP_SUCCESS, OPS_FAILURE, OPS_COMPARATIVE))

# keyword of the task text -> {op: [(when_to_use, content, step_type)]} (ScriptedResearch's shape)
LESSONS = {"#ops": {"success": [(OPS_WHEN, OPS_SUCCESS, "action")],
                    "failure": [(OPS_WHEN, OPS_FAILURE, "decision")],
                    "comparative": [(OPS_WHEN, OPS_COMPARATIVE, "decision")]},
           "opportunity": {"success": [(OPP_WHEN, OPP_SUCCESS, "action")]}}
BASE = {"min_score": 0.4, "min_other_tasks": 1}
PRICING = Pricing(input_per_1k=0.004, output_per_1k=0.02, max_input_tokens=20000, max_output_tokens=5000,
                  source="test prices")


# ---------------------------------------------------------------------------------------------- the fixture
class Sessions(dict):
    """trajectory_of: session -> OpenAI-format messages; a session not set here is a short standard one."""

    def __init__(self):
        super().__init__()
        self.asked: list[str] = []

    def __call__(self, session: str) -> list[dict]:
        self.asked.append(session)
        if session in self:
            return self[session]
        name, kind = session.split("-", 1)
        return conversation(name, kind != "fail")


def conversation(name: str, passed: bool, *, tool_output: str = '{"ok": true}', steps: int = 1) -> list[dict]:
    msgs = [{"role": "system", "content": "You are the agent; follow the request."},
            {"role": "user", "content": TASKS[name]}]
    for i in range(steps):
        msgs.append({"role": "assistant", "content": f"Step {i}.", "tool_calls": [
            {"id": f"call{i}", "type": "function",
             "function": {"name": "api_fetch", "arguments": json.dumps({"url": "https://api.example/x"})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"call{i}", "content": tool_output})
    msgs.append({"role": "assistant", "content": "Done." if passed else "Stopped after the move."})
    return msgs


@dataclass
class Env:
    root: Path
    research: ScriptedResearch
    provider: MeteredProvider
    book: Budgets
    bridge: Bridge
    scope: object
    client: FakeMemoryClient
    sessions: Sessions

    def record(self, step=None) -> list[dict]:
        rows = parse_jsonl((self.root / "bridge_calls.jsonl").read_text()) if (
            self.root / "bridge_calls.jsonl").exists() else []
        return [r for r in rows if step is None or r["step"] == step]

    def reservations(self) -> Counter:
        return Counter(r["status"] for r in self.book._read()["reservations"].values())

    def booked_calls(self) -> float:
        return self.book.usage(ACCOUNT, self.book._read())["model_calls"]


def make_env(root: Path) -> Env:
    root.mkdir(parents=True, exist_ok=True)
    research = ScriptedResearch(lessons=json.loads(json.dumps(LESSONS)))
    book = Budgets(root)
    provider = MeteredProvider(research, book.account(ACCOUNT, {"usd": 100.0}), PRICING, max_retries=0)
    bridge = Bridge().start()
    scope = bridge.open_scope("research", scope_id="track", step=None, provider=provider,
                              record=root / "bridge_calls.jsonl", models=[f"research/reme:{op}" for op in LLM_OPS],
                              effort_cap="medium", max_concurrency=4)
    return Env(root, research, provider, book, bridge, scope, FakeMemoryClient(), Sessions())


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    try:
        yield e
    finally:
        e.bridge.stop()


def settings(**overrides) -> Settings:
    return Settings(**{**BASE, **overrides})


def stream(env: Env, *, name: str = "main", collection: str = COLL, client=None, forbidden=(LITERAL,),
           bridge_calls_of=None, set_step=None, cls=StreamStep, line: str = LINE, **overrides) -> StreamStep:
    """A StreamStep on the env's scope; `name` is its directory (ledger and live file) under <root>/memory/."""
    s = settings(**overrides)
    client = client if client is not None else env.client
    client.start(settings=s, llm=env.scope)
    d = env.root / "memory" / name
    return cls(client, s, ledger_path=d / "stream.json", live_path=d / "live.jsonl", collection=collection,
               business_line=line, activity=ACTIVITY, heldout_domains=s.heldout_domains, train_texts=TRAIN_TEXTS,
               forbidden_literals=set(forbidden), trajectory_of=env.sessions,
               bridge_calls_of=bridge_calls_of or env.scope.calls, set_step=set_step or env.scope.set_step)


def task(name: str, b=None, stable_pass: bool = False, **extra) -> dict:
    return {"task_hash": HASH[name], "text": TASKS[name], "b": b, "stable_pass": stable_pass, **extra}


def attempt(name: str, kind: str, passed: bool, *, shown=(), reason=None, text=None) -> dict:
    return {"session": f"{name}-{kind}", "reward": 1.0 if passed else 0.0, "passed": passed,
            "release_ref": "rel-base-1", "shown": list(shown), "final_reason": reason,
            "final_text": text if text is not None else ("Done." if passed else "Stopped."),
            "trace_refs": [f"transcript:{name}-{kind}#1"]}


# T1: a first encounter that passed; T2: a baseline that failed and a first that passed (all three extraction ops);
# T3: a first that failed and a retry that passed; T4: a baseline and a first that passed.
STANDARD = {"T1": (True, None, None), "T2": (True, None, False), "T3": (False, True, None), "T4": (True, None, True)}


def items(names=("T1", "T2", "T3", "T4"), **tasks) -> list[tuple]:
    out = []
    for n in names:
        first, retry, base = STANDARD[n]
        out.append((task(n, **tasks.get(n, {})), attempt(n, "first", first),
                    None if retry is None else attempt(n, "retry", retry),
                    None if base is None else attempt(n, "baseline", base)))
    return out


def store_ids(client, collection: str = COLL) -> set:
    return {r["memory_id"] for r in parse_jsonl(client.dump(collection))}


def assert_ledger_matches_store(step: StreamStep, client) -> None:
    led = json.loads(step.ledger_path.read_text())
    active = {k for k, e in led["entries"].items() if e["status"] == "active"}
    assert active == store_ids(client, step.collection)
    assert {r["memory_id"] for r in parse_jsonl(step.live_path.read_text())} == active
    stored = {r["memory_id"]: r for r in parse_jsonl(client.dump(step.collection))}
    assert all(led["entries"][k]["row"] == stored[k] for k in active)
    assert step.live_path.read_text() == step.snapshot_text()


class Crash(BaseException):
    """The host process dies here (a SIGKILL as far as the ledger and the store can tell)."""


# ---------------------------------------------------------------------------------------------- the step, end to end
def test_a_batch_is_metered_checkpointed_and_a_resume_pays_nothing_again(env):
    step = stream(env)
    done = step.process_batch(items())
    label = f"{COLL}:00001"
    assert [d["n"] for d in done] == [0, 1, 2, 3] and {d["step"] for d in done} == {label}
    assert [d["added"] for d in done] == [[A, B], [C, D], [], []]
    assert [[x["dup_of"] for x in d["duplicates"]] for d in done] == [[], [A, B], [B], [A, B]]
    # every research call went through the bridge under this step's label, answered 200, and was booked
    calls = env.scope.calls()
    assert len(calls) == len(env.research.calls) == 17 and {c["step"] for c in calls} == {label}
    assert {c["status"] for c in calls} == {200} and {r["step"] for r in env.record()} == {label}
    assert env.reservations() == {"settled": 17} and env.booked_calls() == 17
    c = step.ledger["counters"]
    assert (c["steps"], c["research_calls"], c["research_calls_failed"], c["research_calls_unattributed"],
            c["summarized"], c["validated"]) == (1, 17, 0, 0, 4, 9)
    # the baseline, first and retry trajectories are rebuilt from the trace
    assert sorted(env.sessions.asked) == ["T1-first", "T2-baseline", "T2-first", "T3-first", "T3-retry",
                                          "T4-baseline", "T4-first"]
    assert step.entries[A]["sources"] == sorted(HASH[n] for n in ("T1", "T2", "T4"))
    assert step.ledger["order"] == [HASH[n] for n in ("T1", "T2", "T3", "T4")] and step.ledger["pending"] is None
    assert_ledger_matches_store(step, env.client)
    # a new process on the same ledger: nothing to reconcile, and the batch again costs nothing
    again = stream(env)
    assert again.resume() == {"orphans_removed": [], "restored": [], "pending": None}
    summaries = env.client.summaries
    assert again.process_batch(items()) == done
    assert len(env.research.calls) == 17 and env.client.summaries == summaries and len(env.scope.calls()) == 17
    assert_ledger_matches_store(again, env.client)


def test_the_cuts_are_counted_and_reme_reads_the_cut_trajectories(env):
    env.sessions["T1-first"] = conversation("T1", False, tool_output="x" * 500, steps=2)     # two outputs over 400
    env.sessions["T1-retry"] = conversation("T1", True, tool_output="y" * 300, steps=30)     # ~13k chars in all
    seen = []
    real = env.client.summarize
    env.client.summarize = lambda trajectories: (seen.append(trajectories), real(trajectories))[1]
    step = stream(env, traj_tool_chars=400, traj_total_chars=6000)
    step.process(task("T1"), attempt("T1", "first", False), attempt("T1", "retry", True),
                 attempt("T1", "baseline", True))
    want = {"tool_outputs_cut": 0, "messages_cut": 0, "chars_cut": 0}
    for s in ("T1-baseline", "T1-first", "T1-retry"):
        _, cuts = cut_trajectory(env.sessions(s), tool_chars=400, total_chars=6000)
        for k in want:
            want[k] += cuts[k]
    got = {k: step.ledger["counters"][k] for k in want}
    assert got == want and got["tool_outputs_cut"] == 2 and got["messages_cut"] > 0 and got["chars_cut"] >= 200
    assert step.ledger["processed"][HASH["T1"]]["n"] == 0
    (trajs,) = seen
    assert [t["metadata"]["kind"] for t in trajs] == ["baseline", "first", "retry"]
    assert [t["score"] for t in trajs] == [1.0, 0.0, 1.0] and {t["task_id"] for t in trajs} == {HASH["T1"]}
    assert all(m["role"] != "system" for t in trajs for m in t["messages"])
    first, retry = trajs[1]["messages"], trajs[2]["messages"]
    tools = [m["content"] for m in first if m["role"] == "tool"]
    assert tools == ["x" * 400 + "\n[... 100 characters cut by the platform]"] * 2
    assert retry[0] == {"role": "user", "content": TASKS["T1"]} and retry[-1]["content"] == "Done."
    assert sum(1 for m in retry if "characters) cut by the platform]" in m["content"]) == 1


def test_cut_trajectory_keeps_the_head_and_the_tail_and_counts_the_middle():
    # a system message (left out), the task as text parts, then tool outputs of 100 chars and replies of 10, in turn
    msgs = [{"role": "system", "content": "s" * 99},
            {"role": "user", "content": [{"type": "text", "text": "a" * 4}, {"type": "text", "text": "b" * 5}]}]
    msgs += [{"role": "assistant", "content": str(i) * 10} if i % 2 else
             {"role": "tool", "tool_call_id": f"c{i}", "content": str(i) * 100} for i in range(9)]
    out, counts = cut_trajectory(msgs, tool_chars=100, total_chars=550)    # 10 + 5 x 100 + 4 x 10: nothing cut
    assert [m["content"] for m in out] == ["aaaa\nbbbbb"] + [m["content"] for m in msgs[2:]]
    assert out[1] == {"role": "tool", "tool_call_id": "c0", "content": "0" * 100}
    assert counts == {"tool_outputs_cut": 0, "messages_cut": 0, "chars_cut": 0}
    assert cut_trajectory(msgs, tool_chars=100, total_chars=549)[1]["messages_cut"] > 0
    # each tool output to 20 chars and a note (60 in all), then 350 chars to 200: the head within 100, the tail after
    out, counts = cut_trajectory(msgs, tool_chars=20, total_chars=200)
    tool = lambda i: str(i) * 20 + "\n[... 80 characters cut by the platform]"     # noqa: E731
    assert [m["content"] for m in out] == ["aaaa\nbbbbb", tool(0), "1" * 10,
                                           "[... 5 messages (200 characters) cut by the platform]", "7" * 10, tool(8)]
    assert out[3]["role"] == "user"
    assert counts == {"tool_outputs_cut": 5, "messages_cut": 5, "chars_cut": 5 * 80 + 200}


def test_a_failed_bridge_call_stops_the_step_and_its_label_is_never_reused(env):
    step = stream(env)
    env.research.fail["comparative"] = 1                  # T2's comparative call: the provider fails, the bridge 502s
    with pytest.raises(MemoryStepError, match=r"step ol-live-track001:00001: 1 of 6 research calls failed on the "
                                              r"bridge \(chatcmpl-[0-9a-f]+ comparative HTTP 502\); nothing of the "
                                              r"step is stored; 1 summaries that passed are kept and 1 will be "
                                              r"summarised again$"):
        step.process_batch(items(("T1", "T2")))
    led = json.loads(step.ledger_path.read_text())
    assert step.ledger == led and led["seq"] == 1 and led["processed"] == {} and led["entries"] == {}
    assert list(led["pending"]["tasks"]) == [HASH["T1"]]          # the summary that passed is kept, paid once
    assert store_ids(env.client) == set() and not step.live_path.exists()
    assert env.reservations() == {"settled": 5, "unknown": 1}     # the failed call may have been charged
    paid, summaries = len(env.research.calls), env.client.summaries
    again = stream(env)                                           # a new process: the label is not used again
    done = again.process_batch(items(("T1", "T2")))
    assert [d["step"] for d in done] == [f"{COLL}:00001", f"{COLL}:00002"]
    second = env.scope.calls(f"{COLL}:00002")
    assert env.client.summaries == summaries + 1 and len(env.research.calls) - paid == len(second) == 7
    asked = [json.loads(r["messages"][1]["content"]) for r in env.record(f"{COLL}:00002")]
    assert {q["query"] for q in asked if "query" in q} == {TASKS["T2"]}      # only the failed task is summarised again
    assert {c["status"] for c in second} == {200} and len(env.scope.calls(f"{COLL}:00001")) == 6
    c = again.ledger["counters"]
    assert (c["steps"], c["research_calls"], c["research_calls_failed"], c["summarized"]) == (2, 13, 1, 2)
    assert [d["added"] for d in done] == [[A, B], [C, D]] and env.booked_calls() == 13
    assert_ledger_matches_store(again, env.client)


def test_a_summary_whose_calls_bypass_the_bridge_is_refused(env):
    bypass = FakeMemoryClient(bypass_bridge=True)
    step = stream(env, client=bypass)
    with pytest.raises(MemoryStepError, match=r"not the bridge's 200s of this step \(calls that bypass the bridge are "
                                              r"not metered\).*success reply chatcmpl-bypass-success is not on the "
                                              r"bridge under this step"):
        step.process(*items(("T1",))[0])
    assert env.research.calls == [] and env.scope.calls() == [] and env.reservations() == {}
    assert store_ids(bypass) == set() and step.ledger["entries"] == {} and step.ledger["pending"]["tasks"] == {}


def _drop_comparative(res):
    return {**res, "replies": [r for r in res["replies"] if r["op"] != "comparative"]}


def _unfinished(res):
    return {**res, "replies": [{**r, "done": False} if r["op"] == "failure" else r for r in res["replies"]]}


def _overcounted(res):
    return {**res, "validated": sum(1 for r in res["replies"] if r["op"] == "validation") + 1}


@pytest.mark.parametrize("doctor, why", [
    (_drop_comparative, r"no \['comparative'\] call for trajectories that need one"),
    (_unfinished, r"failure reply chatcmpl-[0-9a-f]+ did not finish"),
    (_overcounted, r"5 validated memories from 4 validation calls"),
    ("op", r"validation reply chatcmpl-[0-9a-f]+ is success HTTP 200 on the bridge"),
], ids=["missing-op", "unfinished", "overcounted", "other-op"])
def test_a_summary_whose_replies_are_not_this_steps_metered_calls_is_refused(env, doctor, why):
    client = FakeMemoryClient()
    calls_of = None
    if doctor == "op":
        calls_of = lambda st: [{**c, "op": "success"} if c["op"] == "validation" else c   # noqa: E731
                               for c in env.scope.calls(st)]
    else:
        real = client.summarize
        client.summarize = lambda trajectories: doctor(real(trajectories))
    step = stream(env, client=client, bridge_calls_of=calls_of)
    with pytest.raises(MemoryStepError, match=why):
        step.process(*items(("T2",))[0])
    assert store_ids(client) == set() and step.ledger["processed"] == {} and step.ledger["pending"]["tasks"] == {}


def test_a_call_still_in_flight_from_another_step_is_not_taken_for_this_ones(env):
    """The bridge labels a call by the step it shows when the call arrives, so a call of an earlier request that
    arrives after the host set this step's label is under this label. Replies are matched by call id: such a call is
    counted as unattributed (paid, recorded), and it never stands in for a call this summary did not make."""
    other = FakeMemoryClient().start(settings=settings(), llm=env.scope)
    stale = []

    def set_step(label):
        env.scope.set_step(label)
        if not stale:                     # an earlier request whose calls arrive under this label
            stale.append(other.summarize([{"task_id": "earlier", "messages": conversation("T4", True), "score": 1.0,
                                           "metadata": {"query": TASKS["T4"]}}]))
    step = stream(env, set_step=set_step)
    done = step.process(*items(("T1",))[0])
    label = done["step"]
    theirs = {r["call_id"] for r in stale[0]["replies"]}
    assert len(theirs) == 3 and theirs <= {c["call_id"] for c in env.scope.calls(label)}
    c = step.ledger["counters"]
    assert c["research_calls_unattributed"] == 3 and c["research_calls"] == len(env.scope.calls(label)) == 6
    assert done["added"] == [A, B]
    # this summary's own calls bypass the bridge while another request's calls of the same ops are under its label
    bypass = FakeMemoryClient(bypass_bridge=True)

    def cover(label):
        env.scope.set_step(label)
        other.summarize([{"task_id": "cover", "messages": conversation("T1", True), "score": 1.0,
                          "metadata": {"query": TASKS["T1"]}}])
    step2 = stream(env, name="bypass", collection="ol-live-bypass01", client=bypass, set_step=cover)
    with pytest.raises(MemoryStepError, match="is not on the bridge under this step"):
        step2.process(*items(("T1",))[0])
    assert {c["op"] for c in env.scope.calls("ol-live-bypass01:00001")} == {"success", "validation"}
    assert store_ids(bypass, "ol-live-bypass01") == set()


# ---------------------------------------------------------------------------------------------- step 4: the checks
def test_lengths_literals_and_reach_are_checked_before_anything_is_stored(env):
    fixed_from = f"Look the invoice up by its number (for example {LITERAL}) before writing to the customer."
    env.research.lessons = {"invoice": {"success": [
        (INV_WHEN, fixed_from, f"action {LITERAL}"),                   # one placeholder pass: text and metadata
        (INV_WHEN + " in bulk", "Do it carefully. " * 120, "action"),  # over K's content cap
        (FAR_WHEN, "Use a fixed window and drop the incomplete edges.", "reasoning"),    # reaches no other task
        ("", "A lesson without a trigger.", "action")]}}
    step = stream(env, origins=("success",))
    done = step.process(task("T6"), attempt("T6", "first", True), None, None)
    assert done["validated"] == 4 and len(done["added"]) == 1
    assert sorted(d.split(":")[0] for d in done["dropped"]) == ["empty when_to_use or content", "length", "reach"]
    assert "reach: retrieves 0 train task(s) beyond its sources, fewer than 1" in done["dropped"]
    (row,) = parse_jsonl(env.client.dump(COLL))
    assert row["content"] == fixed_from.replace(LITERAL, "<value>") and row["metadata"]["step_type"] == "action <value>"
    assert row["memory_id"] == done["added"][0] == content_id(row["content"]) != content_id(fixed_from)
    assert step.ledger["counters"]["placeholder_fixed"] == 1
    assert set(row["metadata"]) <= set(PROVENANCE_KEYS) | set(REME_META_KEYS)
    assert "experience" not in row["metadata"] and "when_to_use" not in row["metadata"]
    for text in (json.dumps(row), step.snapshot_text(), step.live_path.read_text(), step.ledger_path.read_text()):
        assert LITERAL not in text
    assert sorted(d["why"].split(":")[0] for d in step.ledger["dropped"]) == sorted(
        d.split(":")[0] for d in done["dropped"])


def test_a_literal_the_placeholder_pass_cannot_remove_drops_the_memory(env):
    # a forbidden value the placeholder pass cannot remove (the placeholder itself): in the text, or in ReMe's metadata
    env.research.lessons = {"#ops": {"success": [
        (OPS_WHEN, "Check the <value> field before you post in #ops.", "action"),
        (OPP_WHEN, OPP_SUCCESS, "<value>"),
        (OPS_WHEN, OPS_SUCCESS, "action")]}}
    step = stream(env, origins=("success",), forbidden=("<value>",))
    done = step.process(task("T1"), attempt("T1", "first", True), None, None)
    assert done["added"] == [A] and step.ledger["counters"]["placeholder_fixed"] == 0
    assert done["dropped"] == ["literal: '<value>'", "literal in metadata.step_type: '<value>'"]


def test_reach_counts_train_tasks_beyond_the_sources_and_their_siblings(env):
    env.research.lessons = {"weekly": {"success": [(REV_WHEN, REV_SUCCESS, "action")]},
                            "Lea": {"success": [(LEA_WHEN, "Look Lea up by her full name first.", "action")]},
                            "Priya": {"success": [(ARCH_WHEN, "Archive only tickets whose status is closed.",
                                                   "action")]}}
    one = stream(env, origins=("success",))                         # REV_WHEN reaches T8 only: exactly 1 is enough
    assert len(one.process(task("T7"), attempt("T7", "first", True), None, None)["added"]) == 1
    # its own source is not counted (LEA_WHEN reaches T3 only), nor a sibling of the source (T10 is T9's words)
    assert one.process(task("T3"), attempt("T3", "first", True), None, None)["dropped"] == [
        "reach: retrieves 0 train task(s) beyond its sources, fewer than 1"]
    assert one.process(task("T9"), attempt("T9", "first", True), None, None)["dropped"] == [
        "reach: retrieves 0 train task(s) beyond its sources, fewer than 1"]
    assert one.siblings(HASH["T9"]) == {HASH["T9"], HASH["T10"]} and one.siblings(HASH["T1"]) == {HASH["T1"]}
    two = stream(env, name="two", collection="ol-live-reach002", origins=("success",), min_other_tasks=2)
    assert two.process(task("T7"), attempt("T7", "first", True), None, None)["dropped"] == [
        "reach: retrieves 1 train task(s) beyond its sources, fewer than 2"]
    # with no reach required, the entry's sources are the task and its siblings (leave-one-out leaves both out)
    zero = stream(env, name="zero", collection="ol-live-reach000", origins=("success",), min_other_tasks=0)
    (mid,) = zero.process(task("T9"), attempt("T9", "first", True), None, None)["added"]
    assert zero.entries[mid]["sources"] == sorted([HASH["T9"], HASH["T10"]])
    apart = stream(env, name="apart", collection="ol-live-reach001", origins=("success",), sibling_sim=1.5)
    (mid,) = apart.process(task("T9"), attempt("T9", "first", True), None, None)["added"]
    assert apart.entries[mid]["sources"] == [HASH["T9"]]


# ---------------------------------------------------------------------------------------------- step 5: dedup
def test_store_dedup_by_when_then_content_and_by_id(env):
    near_when = "When a request asks to move a contact, then post a phrase in #ops"
    near = ("1. Move the contact first.\n2. Then post the requested phrase in #ops: the request is not done until it "
            "has been posted.")
    assert cosine(bow(near_when), bow(OPS_WHEN)) >= 0.9 and cosine(bow(near), bow(OPS_SUCCESS)) >= 0.85
    env.research.lessons = {"deal moved": {"success": [(OPS_WHEN, OPS_SUCCESS, "action")]},
                            "stage changed": {"success": [(near_when, near, "action")]},
                            "call booked": {"failure": [(OPS_WHEN, OPS_FAILURE, "decision")]},
                            "Lea": {"success": [(OPP_WHEN, OPS_SUCCESS, "action")]}}
    step = stream(env, origins=("success", "failure"))
    a, b, c, d = step.process_batch([(task("T1"), attempt("T1", "first", True), None, None),
                                     (task("T2"), attempt("T2", "first", True), None, None),
                                     (task("T4"), attempt("T4", "first", False), None, None),
                                     (task("T3"), attempt("T3", "first", True), None, None)])
    assert a["added"] == [A]
    # a near duplicate (trigger and lesson both close) gains a source, no row
    (dup,) = b["duplicates"]
    assert b["added"] == [] and (dup["memory_id"], dup["dup_of"], dup["how"]) == (content_id(near), A, "store_dedup")
    assert dup["score"] >= 0.9 and dup["content_score"] >= 0.85
    # the same trigger with another lesson is another entry
    assert c["added"] == [C] and c["duplicates"] == [] and step.entries[C]["origin"] == "failure"
    # the same lesson under a trigger that is not close: one id, one row, one more source
    assert d["added"] == [] and d["duplicates"] == [{"memory_id": A, "dup_of": A, "how": "same_id"}]
    assert store_ids(env.client) == {A, C} and env.client.count(COLL) == 2
    assert step.entries[A]["sources"] == sorted(HASH[n] for n in ("T1", "T2", "T3"))
    assert [e["how"] for e in step.entries[A]["events"]] == ["added", "store_dedup", "same_id"]
    assert_ledger_matches_store(step, env.client)


# ---------------------------------------------------------------------------------------------- step 6: stamp, order
def test_rows_are_stamped_and_added_serially_in_the_batch_order(env):
    finished, added = [], []
    real_summarize, real_add = env.client.summarize, env.client.add

    def slow(trajectories):                   # T1's summary finishes last of the batch
        if trajectories[0]["metadata"]["query"] == TASKS["T1"]:
            time.sleep(0.3)
        out = real_summarize(trajectories)
        finished.append(trajectories[0]["task_id"])
        return out

    def add(memories, collection):
        added.extend((m["metadata"]["ol_task_hash"], m["metadata"]["ol_born_at"], m["memory_id"]) for m in memories)
        return real_add(memories, collection)
    env.client.summarize, env.client.add = slow, add
    step = stream(env)
    step.process_batch(items())
    assert finished[-1] == HASH["T1"] and len(finished) == 4
    assert added == [(HASH["T1"], 0, A), (HASH["T1"], 0, B), (HASH["T2"], 1, C), (HASH["T2"], 1, D)]
    rows = {r["memory_id"]: r for r in parse_jsonl(env.client.dump(COLL))}
    meta = rows[C]["metadata"]
    assert {k: meta[k] for k in ("ol_line", "ol_source_line", "ol_split", "ol_task_hash", "ol_activity",
                                 "ol_release_ref", "ol_origin", "ol_born_at", "ol_source_score",
                                 "ol_validation_score", "ol_imported_from", "ol_reme_step_type")} == {
        "ol_line": LINE, "ol_source_line": LINE, "ol_split": "train", "ol_task_hash": HASH["T2"],
        "ol_activity": ACTIVITY, "ol_release_ref": "rel-base-1", "ol_origin": "failure", "ol_born_at": 1,
        "ol_source_score": 1.0, "ol_validation_score": 0.9, "ol_imported_from": "", "ol_reme_step_type": "decision"}
    assert json.loads(meta["ol_sessions"]) == ["T2-baseline", "T2-first"]
    assert json.loads(meta["ol_trace_refs"]) == ["transcript:T2-baseline#1", "transcript:T2-first#1"]
    assert rows[C]["author"] == "research/reme:failure" and rows[C]["time_created"] == ""
    assert {e["origin"] for e in step.entries.values()} == {"success", "failure", "comparative"}


# ---------------------------------------------------------------------------------------------- step 7: credit
def test_credit_goes_by_the_first_encounters_citation_line(env):
    step = stream(env, origins=("success",))
    assert step.process(task("T1"), attempt("T1", "first", True), None, None)["added"] == [A, B]
    # cited: only the cited entry is credited (shown ids as the agent saw them, 8 hex, or whole)
    step.process(task("T2", b=0.0), attempt("T2", "first", True, shown=[A[:8], B],
                                            reason=f"Done.\nlibrary-used: {A[:8]}"), None, None)
    sa, sb = step.entries[A]["stats"], step.entries[B]["stats"]
    assert (sa["n_shown"], sa["n_cited"], sa["credit_sum"], sa["fixes"], sa["fix_tasks"]) == (
        1, 1, 1.0, 1, [HASH["T2"]])
    assert (sb["n_shown"], sb["n_cited"], sb["credit_sum"], sb["fixes"]) == (1, 0, 0.0, 0)
    r = step.ledger["readings"][HASH["T2"]]
    assert (r["shown"], r["cited"], r["used"], r["credit_each"], r["origins_used"]) == (
        [A, B], [A[:8]], [A], 1.0, ["success"])
    # no citation line: every shown entry shares r - b; the retry's showing and citation are never credited
    step.process(task("T3", b=1.0, stable_pass=True), attempt("T3", "first", False, shown=[A, B]),
                 attempt("T3", "retry", True, shown=[B], reason=f"library-used: {B[:8]}"), None)
    assert (sa["n_shown"], sa["credit_sum"], sa["harm_tasks"]) == (2, 0.5, [HASH["T3"]])
    assert (sb["n_shown"], sb["n_cited"], sb["credit_sum"], sb["harm_tasks"]) == (2, 0, -0.5, [HASH["T3"]])
    r = step.ledger["readings"][HASH["T3"]]
    assert (r["cited"], r["used"], r["credit_each"], r["retry"]) == (None, [A, B], -0.5, 1)
    # the line in the last text when the finish reason has none; no baseline: shown and cited, credited nothing
    step.process(task("T4"), attempt("T4", "first", True, shown=[A], text=f"ok\nlibrary-used: {A[:8]}, deadbeef"),
                 None, None)
    assert (sa["n_shown"], sa["n_cited"], sa["credit_sum"]) == (3, 2, 0.5)
    r = step.ledger["readings"][HASH["T4"]]
    assert (r["cited"], r["cited_unshown"], r["credit_each"]) == (sorted([A[:8], "deadbeef"]), ["deadbeef"], 0.0)
    # with K's cite off the line is not read: every shown entry shares
    off = stream(env, name="nocite", collection="ol-live-nocite01", origins=("success",), cite=False)
    off.process(task("T1"), attempt("T1", "first", True), None, None)
    off.process(task("T2", b=0.0), attempt("T2", "first", True, shown=[A, B], reason=f"library-used: {A[:8]}"),
                None, None)
    assert [(off.entries[m]["stats"]["credit_sum"], off.entries[m]["stats"]["n_cited"]) for m in (A, B)] == [
        (0.5, 0), (0.5, 0)]


def test_retirement_follows_the_credit_of_the_same_step_and_retired_content_never_comes_back(env):
    step = stream(env, origins=("success",))
    step.process(task("T1"), attempt("T1", "first", True), None, None)
    records = []
    for n in ("T5", "T7", "T8"):     # three cited failures of stable passes: mean credit -1, retired after probation
        records.append(step.process(task(n, b=1.0, stable_pass=True),
                                    attempt(n, "first", False, shown=[A], reason=f"library-used: {A[:8]}"), None, None))
    assert [r["retired"] for r in records] == [[], [], [A]] and records[-1]["n"] == 3
    e = step.entries[A]
    assert e["status"] == "retired" and e["retired"] == "mean credit -1.000 after 3 showings" and e["retired_at"] == 3
    assert e["stats"]["harm_tasks"] == sorted(HASH[n] for n in ("T5", "T7", "T8"))
    assert store_ids(env.client) == {B} and A not in step.live_path.read_text()
    assert step.live_view()["sources"].keys() == {B}
    done = step.process(task("T2"), attempt("T2", "first", True), None, None)      # T2 distils the same lesson again
    assert done["added"] == [] and done["dropped"] == ["retired before: mean credit -1.000 after 3 showings"]
    assert store_ids(env.client) == {B} and step.entries[A]["status"] == "retired"
    assert step.resume() == {"orphans_removed": [], "restored": [], "pending": None}
    assert_ledger_matches_store(step, env.client)


def test_capacity_evicts_the_lowest_proven_entry_only_when_full(env):
    env.research.lessons = {"Dana": {"success": [(OPS_WHEN, OPS_SUCCESS, "action")]},
                            "Lea": {"success": [(OPP_WHEN, OPP_SUCCESS, "action")]},
                            "weekly": {"success": [(REV_WHEN, REV_SUCCESS, "action")]}}
    c_id = content_id(REV_SUCCESS)

    def run(name, max_active, show):
        step = stream(env, name=name, collection=f"ol-live-cap{name}", origins=("success",), max_active=max_active)
        step.process(task("T1"), attempt("T1", "first", True), None, None)
        step.process(task("T3"), attempt("T3", "first", True), None, None)
        if show:      # three showings each: out of probation, A credited +1, B 0
            for n, reason, passed in (("T5", f"library-used: {A[:8]}", True), ("T6", "library-used: none", True),
                                      ("T8", None, False)):
                step.process(task(n, b=0.0), attempt(n, "first", passed, shown=[A, B], reason=reason), None, None)
            assert [step.entries[x]["stats"]["n_shown"] for x in (A, B)] == [3, 3]
        return step, step.process(task("T7"), attempt("T7", "first", True), None, None)

    full, done = run("full", 2, True)
    assert done["added"] == [c_id] and done["evicted"] == [B]
    assert full.entries[B]["status"] == "retired" and full.entries[B]["retired"] == "capacity 2"
    assert store_ids(env.client, "ol-live-capfull") == {A, c_id} and len(full.active()) == 2
    room, done = run("room", 3, True)
    assert done["added"] == [c_id] and done["evicted"] == [] and len(room.active()) == 3
    assert store_ids(env.client, "ol-live-caproom") == {A, B, c_id}
    young, done = run("young", 2, False)
    assert done["added"] == [] and done["dropped"] == ["capacity: 2 active, none out of probation"]
    assert store_ids(env.client, "ol-live-capyoung") == {A, B}


# ---------------------------------------------------------------------------------------------- step 8 and resume
def test_the_live_file_is_written_before_the_ledger(env, monkeypatch):
    step = stream(env)
    writes = []
    real = track.write_atomic

    def spy(path, text):
        writes.append((Path(path).name, len(parse_jsonl(env.client.dump(COLL)))))
        real(path, text)
    monkeypatch.setattr(track, "write_atomic", spy)
    step.process(*items(("T1",))[0])
    # the step label, the paid summaries, then the checkpoint: the library (both rows stored), then the ledger
    assert writes == [("stream.json", 0), ("stream.json", 0), ("live.jsonl", 2), ("stream.json", 2)]


def _dying(where):
    class Dying(StreamStep):
        saves = 0

        def snapshot_text(self):
            if where == "after_store":      # every row of the batch is stored; the live file and the ledger are old
                raise Crash(where)
            return super().snapshot_text()

        def _save_ledger(self):
            Dying.saves += 1
            if where == "after_live" and Dying.saves == 3:     # the step label, the summaries, then this one
                raise Crash(where)
            super()._save_ledger()
    return Dying


@pytest.mark.parametrize("where", ["after_store", "after_live"])
def test_a_crash_between_the_store_write_and_the_ledger_write_is_reconciled(env, where):
    with pytest.raises(Crash):
        stream(env, cls=_dying(where)).process_batch(items())
    paid, summaries = len(env.research.calls), env.client.summaries
    stored = store_ids(env.client)
    led = json.loads((env.root / "memory" / "main" / "stream.json").read_text())
    assert paid == 17 and stored == {A, B, C, D} and led["entries"] == {} and led["processed"] == {}
    assert sorted(led["pending"]["tasks"]) == sorted(HASH[n] for n in ("T1", "T2", "T3", "T4"))
    step = stream(env)
    # the store holds rows the ledger does not: a step refuses to commit over them until resume() reconciles
    with pytest.raises(MemoryStepError, match="is in the store and not in the ledger: resume\\(\\) first"):
        step.process_batch(items())
    got = step.resume()
    assert got == {"orphans_removed": sorted(stored), "restored": [], "pending": f"{COLL}:00001"}
    assert store_ids(env.client) == set() and step.live_path.read_text() == ""
    done = step.process_batch(items())                      # committed from the pending summaries
    assert len(env.research.calls) == paid and env.client.summaries == summaries and len(env.scope.calls()) == 17
    assert [d["n"] for d in done] == [0, 1, 2, 3] and {d["step"] for d in done} == {f"{COLL}:00001"}
    assert store_ids(env.client) == stored                  # the library the dead process was writing
    assert step.ledger["counters"]["steps"] == 1 and step.ledger["counters"]["research_calls"] == 17
    assert_ledger_matches_store(step, env.client)


def test_a_failure_inside_a_commit_leaves_the_ledger_as_it_was(env):
    clean_env = make_env(env.root / "clean")
    try:
        want = stream(clean_env).process_batch(items(("T1", "T2")))
    finally:
        clean_env.bridge.stop()
    step = stream(env)
    real, seen = env.client.add, []

    def flaky(memories, collection):
        seen.append(step.ledger_path.read_text())
        if len(seen) == 2:
            raise MemorySidecarError("injected: the sidecar went away mid-commit")
        return real(memories, collection)
    env.client.add = flaky
    with pytest.raises(MemorySidecarError, match="injected"):
        step.process_batch(items(("T1", "T2")))
    env.client.add = real
    assert step.ledger_path.read_text() == seen[0] and step.ledger == json.loads(seen[0])
    assert step.ledger["entries"] == {} and step.ledger["processed"] == {} and step.ledger["pending"]
    assert not step.live_path.exists()
    paid = len(env.research.calls)
    assert step.resume()["orphans_removed"] == [A] and store_ids(env.client) == set()
    got = step.process_batch(items(("T1", "T2")))
    assert len(env.research.calls) == paid
    assert [d["added"] for d in got] == [d["added"] for d in want] == [[A, B], [C, D]]
    assert parse_jsonl(env.client.dump(COLL)) == parse_jsonl(clean_env.client.dump(COLL))


def test_a_lost_store_row_is_restored_from_the_ledger(env):
    step = stream(env)
    step.process_batch(items(("T1", "T2")))
    before = env.client.dump(COLL)
    env.client.retire([B], COLL)                     # the store lost a row the ledger holds
    again = stream(env)
    assert again.resume() == {"orphans_removed": [], "restored": [B], "pending": None}
    assert env.client.dump(COLL) == before
    assert_ledger_matches_store(again, env.client)


def test_a_pending_batch_is_processed_before_anything_else(env):
    step = stream(env)
    env.research.fail["comparative"] = 1
    with pytest.raises(MemoryStepError, match="failed on the bridge"):
        step.process_batch(items(("T1", "T2")))
    for call, why in ((lambda: step.process_batch(items(("T3",))), "is pending for"),
                      (lambda: step.lose(HASH["T3"], "invalid"), "is pending: process it first"),
                      (lambda: step.finish(), "is pending: process it first")):
        with pytest.raises(MemoryStepError, match=why):
            call()
    assert step.ledger["lost"] == {} and step.ledger["status"] == "streaming"
    assert [d["n"] for d in step.process_batch(items(("T1", "T2")))] == [0, 1]


# ---------------------------------------------------------------------------------------------- plan, lose, supersede
def test_plan_fixes_the_order_once_and_refuses_another(env):
    order = [HASH[n] for n in ("T1", "T2", "T3", "T4")]
    step = stream(env)
    assert step.plan(order) == order
    assert json.loads(step.ledger_path.read_text())["plan"]["order"] == order
    again = stream(env)                                       # a resume continues the same stream
    assert again.plan(order) == order
    with pytest.raises(MemoryStepError, match="is fixed; this one is"):
        again.plan(list(reversed(order)))
    for bad in (order + ["0" * 16], order + order[:1]):
        with pytest.raises(MemoryStepError, match="not its train tasks, or one twice"):
            again.plan(bad)
    again.process(*items(("T1",))[0])
    again.lose(HASH["T3"], "invalid after the refill rule")
    assert stream(env).plan(order) == [HASH["T2"], HASH["T4"]]


def test_a_lost_task_is_recorded_without_summary_or_credit(env):
    step = stream(env)
    step.process(*items(("T1",))[0])
    calls = len(env.research.calls)
    record = step.lose(HASH["T3"], "invalid after the refill rule")
    assert record == {"n": 1, "lost": "invalid after the refill rule"}
    assert step.lose(HASH["T3"], "another reason") == record          # recorded once
    led = json.loads(step.ledger_path.read_text())
    assert led["lost"] == {HASH["T3"]: "invalid after the refill rule"} and led["processed"][HASH["T3"]] == record
    # a batch that names it again does nothing for it: no summary, no reading, no credit
    t3 = (task("T3", b=0.0), attempt("T3", "first", True, shown=[A], reason=f"library-used: {A[:8]}"), None, None)
    got = step.process_batch([t3, items(("T4",))[0]])
    assert got[0] == record and got[1]["n"] == 1
    assert len(env.research.calls) - calls == 3 and env.client.summaries == 2
    assert HASH["T3"] not in step.ledger["readings"] and step.entries[A]["stats"]["n_shown"] == 0
    assert step.ledger["order"] == [HASH["T1"], HASH["T4"]] and step.finish()["readout"]["tasks"] == 0


def test_a_superseded_entry_is_gone_from_the_store_and_never_shown_again(env):
    step = stream(env, origins=("success",))
    step.process(task("T1"), attempt("T1", "first", True), None, None)
    reader = LibraryReader(env.client, step.s, business_line=LINE, live=step.live_view())
    assert reader.for_task(TASKS["T2"], HASH["T2"]).event["shown"] == [A, B]
    step.supersede(A, by="rel-skill-1")
    e = step.entries[A]
    assert (e["status"], e["superseded_by"]) == ("superseded", "rel-skill-1")
    assert store_ids(env.client) == {B} and A not in step.live_path.read_text()
    assert json.loads(step.ledger_path.read_text())["entries"][A]["status"] == "superseded"
    assert_ledger_matches_store(step, env.client)

    def shown():
        mem = LibraryReader(env.client, step.s, business_line=LINE, live=step.live_view()).for_task(
            TASKS["T4"], HASH["T4"])
        return mem.event["shown"], [r["id"] for r in mem.recall(OPS_WHEN)]      # A's own trigger
    assert shown() == ([B], [B[:8]])
    assert step.resume() == {"orphans_removed": [], "restored": [], "pending": None}
    # a later task distils the same lesson: it does not come back
    done = step.process(task("T2"), attempt("T2", "first", True), None, None)
    assert done["added"] == [] and done["dropped"] == ["superseded before: rel-skill-1"]
    assert step.entries[A]["status"] == "superseded" and store_ids(env.client) == {B}
    assert shown() == ([B], [B[:8]])
    with pytest.raises(KeyError):
        step.supersede("0" * 16, by="rel-skill-1")


# ---------------------------------------------------------------------------------------------- imports
def sales_row(content: str, when: str = OPS_WHEN) -> dict:
    mem = {"memory_id": content_id(content), "when_to_use": when, "content": content, "memory_type": "procedural",
           "author": "research/reme:success", "score": 0.8, "metadata": {"step_type": "action"}}
    row = stamp(mem, line="sales", source_line="sales", split="train", task_hash="5" * 16, activity="act-sales",
                release_ref="rel-sales-2", sessions=["S1-first"], trace_refs=["transcript:S1-first#1"],
                origin="success", born_at=0, source_score=1.0, heldout_domains=("operations",))
    row["metadata"]["ol_sources"] = json.dumps(["5" * 16])
    row["metadata"]["ol_stats"] = json.dumps({**new_stats(), "n_shown": 7, "credit_sum": 3.0, "fixes": 3})
    return row


SALES = [sales_row("Post the phrase in #ops right after the move, then finish."),
         sales_row(f"Quote the invoice number ({LITERAL}) in the #ops message."),
         sales_row("Be thorough. " * 140)]
SALES_TEXT = canonical_rows(SALES)
SALES_IMPORT = {"release_ref": "rel-sales-3", "snapshot_digest": snapshot_digest(SALES_TEXT)}


def test_only_a_snapshot_k_declares_is_imported(env):
    plain = stream(env)
    with pytest.raises(MemoryStepError, match="not one of K's memory.imports"):
        plain.import_snapshot(SALES_TEXT, source_release_ref="rel-sales-3", source_line="sales")
    declared = stream(env, name="declared", collection="ol-live-import01", imports=(SALES_IMPORT,))
    for text, ref in ((SALES_TEXT + SALES_TEXT.splitlines(True)[0], "rel-sales-3"), (SALES_TEXT, "rel-sales-4")):
        with pytest.raises(MemoryStepError, match="not one of K's memory.imports"):
            declared.import_snapshot(text, source_release_ref=ref, source_line="sales")
    assert store_ids(env.client) == set() and store_ids(env.client, "ol-live-import01") == set()
    assert declared.ledger["imports"] == []


def test_an_import_enters_under_this_lines_checks_and_starts_probation_from_zero(env):
    step = stream(env, imports=(SALES_IMPORT,))
    step.process(*items(("T1",))[0])
    got = step.import_snapshot(SALES_TEXT, source_release_ref="rel-sales-3", source_line="sales")
    fixed = "Quote the invoice number (<value>) in the #ops message."
    want = sorted([SALES[0]["memory_id"], content_id(fixed)])
    assert got["added"] == want and got["skipped"] == [] and got["again"] is False
    assert [(d["memory_id"], d["why"].split(":")[0]) for d in got["dropped"]] == [(SALES[2]["memory_id"], "length")]
    rows = {r["memory_id"]: r for r in parse_jsonl(env.client.dump(COLL))}
    assert set(rows) == {A, B} | set(want) and LITERAL not in env.client.dump(COLL)
    for mid, src in ((SALES[0]["memory_id"], SALES[0]), (content_id(fixed), SALES[1])):
        m = rows[mid]["metadata"]
        assert (m["ol_line"], m["ol_source_line"], m["ol_origin"], m["ol_imported_from"]) == (
            LINE, "sales", "imported", f"rel-sales-3:{src['memory_id']}")
        assert "ol_sources" not in m and "ol_stats" not in m
        e = step.entries[mid]
        assert (e["origin"], e["status"], e["born_at"], e["sources"], e["stats"]) == (
            "imported", "active", 1, [], new_stats())
    assert step.ledger["imports"] == [{**SALES_IMPORT, "source_line": "sales", "added": want, "skipped": [],
                                       "dropped": got["dropped"]}]
    assert_ledger_matches_store(step, env.client)
    again = step.import_snapshot(SALES_TEXT, source_release_ref="rel-sales-3", source_line="sales")
    assert again == {"added": [], "skipped": [], "dropped": [], "again": True} and len(step.ledger["imports"]) == 1
    # credited on this line's train only, from zero
    step.process(task("T2", b=0.0), attempt("T2", "first", True, shown=[want[0]],
                                            reason=f"library-used: {want[0][:8]}"), None, None)
    assert step.entries[want[0]]["stats"]["credit_sum"] == 1.0 and step.entries[want[0]]["stats"]["n_shown"] == 1
    assert step.finish()["readout"]["imports"] == 1


def test_an_import_from_a_held_out_line_is_refused(env):
    ops = [dict(r, metadata={**r["metadata"], "ol_line": "operations", "ol_source_line": "operations"})
           for r in SALES[:1]]
    ops_text = canonical_rows(ops)
    relayed = [dict(r, metadata={**r["metadata"], "ol_source_line": "operations"}) for r in SALES[:1]]
    relayed_text = canonical_rows(relayed)
    k = ({"release_ref": "rel-ops-1", "snapshot_digest": snapshot_digest(ops_text)},
         {"release_ref": "rel-sales-5", "snapshot_digest": snapshot_digest(relayed_text)})
    step = stream(env, imports=k)
    with pytest.raises(TrainOnlyError, match="'operations' is held out"):
        step.import_snapshot(ops_text, source_release_ref="rel-ops-1", source_line="operations")
    with pytest.raises(TrainOnlyError, match="'operations' is held out"):     # a row that came from a held-out line
        step.import_snapshot(relayed_text, source_release_ref="rel-sales-5", source_line="sales")
    assert store_ids(env.client) == set() and step.ledger["imports"] == [] and step.entries == {}


# ---------------------------------------------------------------------------------------------- finish, identity
def _finish_run(env):
    step = stream(env)
    batch = items(T1={"b": 0.0}, T2={"b": 0.0}, T3={"b": 1.0, "stable_pass": True})
    t2_task, t2_first, t2_retry, t2_base = batch[1]
    batch[1] = (t2_task, {**t2_first, "shown": [A[:8]], "final_reason": f"Done.\nlibrary-used: {A[:8]}"},
                t2_retry, t2_base)
    step.process_batch(batch)
    return step, step.finish()


def test_finish_reads_out_and_the_same_stream_gives_the_same_bytes(env):
    step, out = _finish_run(env)
    ro = out["readout"]
    assert out["abort"] is None and (ro["tasks"], ro["D"], ro["B"]) == (3, 1.0, 1)
    assert ro["D_by_origin"] == {"success": {"D": 1.0, "n": 1}}       # T2 used A, committed in the same batch
    assert ro["entries"]["active"] == 4 and ro["counters"]["research_calls"] == 17 and ro["reply_format"] == []
    assert ro["dropped"] == {} and ro["imports"] == 0
    led = json.loads(step.ledger_path.read_text())
    assert led["status"] == "done" and led["abort"] is None
    assert led["snapshot_digest"] == snapshot_digest(out["snapshot"])
    # the live file holds the library's whole rows; the snapshot is those rows as a package carries them, all kept
    live = step.live_path.read_text()
    assert out["snapshot"] == fit_snapshot(live, None)[0] and led["snapshot_fit"] == out["fit"]
    assert (out["fit"]["entries"], out["fit"]["kept"], out["fit"]["omitted"]) == (4, 4, [])
    for text in (live, out["snapshot"]):
        rows = parse_jsonl(text)
        assert [r["memory_id"] for r in rows] == sorted([A, B, C, D])
        assert {r["memory_id"]: json.loads(r["metadata"]["ol_sources"]) for r in rows}[A] == sorted(
            HASH[n] for n in ("T1", "T2", "T4"))
    stats = {r["memory_id"]: json.loads(r["metadata"]["ol_stats"]) for r in parse_jsonl(live)}
    assert stats[A]["credit_sum"] == 1.0
    assert {r["memory_id"]: json.loads(r["metadata"]["ol_stats"]) for r in parse_jsonl(out["snapshot"])} == \
        {k: {"fixes": s["fixes"], "n_cited": s["n_cited"]} for k, s in stats.items()}
    with pytest.raises(MemoryStepError, match="the stream is done"):
        step.process(*items(("T1",))[0])
    # the same stream again, on another bridge, book and store: the same bytes
    other = make_env(env.root / "again")
    try:
        step2, out2 = _finish_run(other)
        assert out2 == out and step2.live_path.read_bytes() == step.live_path.read_bytes()
        assert step2.ledger_path.read_bytes() == step.ledger_path.read_bytes()
    finally:
        other.bridge.stop()


def test_finish_keeps_the_snapshot_within_max_bytes_by_credit_and_records_what_it_left_out(env):
    """K's max_memory_bytes holds one of the four entries: the one T2 cited and was fixed with (A) is kept; the live
    library keeps all four; the ledger records the fit. A cap no entry fits gives no snapshot and an abort."""
    step, out = _finish_run(env)
    sizes = {r["memory_id"]: row_bytes(r) for r in parse_jsonl(out["snapshot"])}
    cap = sizes[A] + min(n for k, n in sizes.items() if k != A) - 1
    capped = step.finish(max_bytes=cap)
    assert [r["memory_id"] for r in parse_jsonl(capped["snapshot"])] == [A] and capped["abort"] is None
    fit = capped["fit"]
    assert (fit["entries"], fit["kept"], fit["bytes"], fit["max_bytes"], fit["why"]) == \
        (4, 1, sizes[A], cap, "K's max_memory_bytes")
    assert sorted(o["memory_id"] for o in fit["omitted"]) == sorted([B, C, D])
    assert len(parse_jsonl(step.live_path.read_text())) == 4
    led = json.loads(step.ledger_path.read_text())
    assert led["snapshot_fit"] == fit and led["snapshot_digest"] == snapshot_digest(capped["snapshot"])
    none = step.finish(max_bytes=10)
    assert none["snapshot"] == "" and none["abort"] == "no entry of the library fits K's max_memory_bytes (10)"
    assert json.loads(step.ledger_path.read_text())["abort"] == none["abort"]


def test_finish_aborts_on_d_at_most_abort_d_or_an_empty_library(env):
    low = stream(env, origins=("success",))
    low.process(task("T1", b=1.0, stable_pass=True), attempt("T1", "first", False), None, None)
    out = low.finish()
    assert out["abort"] == "D -1.0 <= 0.0" and out["snapshot"] == ""
    empty = stream(env, name="empty", collection="ol-live-empty001", origins=("success",))
    empty.process(task("T5", b=0.0), attempt("T5", "first", True), None, None)       # no lesson for T5
    out = empty.finish()
    assert out["readout"]["D"] == 1.0 and out["abort"] == "the library is empty" and out["snapshot"] == ""


def test_the_ledger_refuses_another_k_another_collection_or_line(env):
    order = [HASH["T1"], HASH["T2"]]
    stream(env).plan(order)
    for kw, key in (({"min_score": 0.5}, "settings_digest"), ({"collection": "ol-live-other001"}, "collection"),
                    ({"line": "sales"}, "business_line")):
        with pytest.raises(ValueError, match=f"the stream's {key} is .*a stream is not continued under another K"):
            stream(env, **kw)
    path = env.root / "memory" / "main" / "stream.json"
    path.write_text(path.read_text().replace('"memory_stream.v1"', '"memory_stream.v0"'))
    with pytest.raises(ValueError, match="schema 'memory_stream.v0'"):
        stream(env)


def test_only_train_tasks_of_a_line_that_is_not_held_out_enter(env):
    with pytest.raises(TrainOnlyError, match="'operations' is held out"):
        stream(env, line="operations")
    step = stream(env)
    with pytest.raises(TrainOnlyError, match="split 'dev'"):
        step.process(task("T1", split="dev"), attempt("T1", "first", True), None, None)
    stranger = {"task_hash": "f" * 16, "text": "Something else.", "b": None, "stable_pass": False}
    with pytest.raises(MemoryStepError, match="is not one of the stream's train tasks"):
        step.process(stranger, attempt("T1", "first", True), None, None)
    with pytest.raises(MemoryStepError, match="a batch holds a task twice"):
        step.process_batch(items(("T1",)) + items(("T1",)))
    assert env.research.calls == [] and env.client.summaries == 0 and step.ledger["seq"] == 0


# ---------------------------------------------------------------------------- the review's cases (2026-10-09)
def test_an_attempt_without_a_trajectory_is_summarised_without_it_and_blocks_nothing(env):
    """A valid attempt that made no model call (a harness_error before its first call) has no gateway_call lines:
    its trajectory is left out and counted; a task with no trajectory at all costs no research call."""
    from reharness.runtime.trace import TraceError

    def no_calls(session):
        if session.startswith("T1-"):
            raise TraceError(f"session {session} has no gateway_call records")
        return env.sessions(session)
    step = stream(env)
    step.trajectory_of = no_calls
    done = step.process_batch(items(("T1", "T2")))
    assert [d["added"] for d in done][0] == [] and step.ledger["counters"]["no_trajectory"] == 1
    assert set(step.ledger["processed"]) == {HASH["T1"], HASH["T2"]} and step.ledger["pending"] is None
    assert env.client.summaries == 1                       # T2's only: T1 had nothing to summarise


def test_an_import_does_not_bring_back_content_this_stream_retired_or_superseded(env):
    step = stream(env, imports=(SALES_IMPORT,))
    step.process(*items(("T1",))[0])
    target = SALES[0]["memory_id"]
    step.entries[target] = {"memory_id": target, "origin": "success", "born_at": 0, "status": "superseded",
                            "superseded_by": "rel-skill-1", "sources": [], "stats": new_stats(), "events": []}
    got = step.import_snapshot(SALES_TEXT, source_release_ref="rel-sales-3", source_line="sales")
    assert target not in got["added"] and step.entries[target]["status"] == "superseded"
    assert {d["memory_id"]: d["why"] for d in got["dropped"]}[target] == "superseded before in this stream"
    assert target not in store_ids(env.client)
