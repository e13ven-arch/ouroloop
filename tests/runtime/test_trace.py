"""Trace memory over stage 1's transcript lines (docs/design/MEMORY.md §2, §10, §11; runtime/trace.py): references,
train-only access by the frozen split, the trajectory rebuilt as the actor saw it (across Stirrup's summary),
calls(), finish() and the FTS5 index. Ported from v6's tests/test_trace.py (tag v6-gateway) to runtime/records.py's
lines: a gateway_call's `reply` and messages_prefix/messages_tail, `tool` lines with the broker's call id and
read_only, the package's `mark` lines ({mark, data}) and `hook` lines, `egress`, `library_retrieval`, `end`.

Two kinds of transcript, the model scripted in both:
- real ones: the production baseline on the miniature upstream (rsi.activity + phases.baseline, the stirrup seed in
  a ProcessWorker) — one train and one dev attempt; and single attempts of the stirrup seed under the runner shell
  (tests/runtime/runtime_world.py) over a ledger whose reads are far over a tool line's EVENT_CHARS, one of which
  Stirrup itself summarizes (its own bridge and "Got it, thanks!"), one cut by its turn limit, one whose write the
  seed's guard stops;
- synthetic ones, written line by line through runtime.records.Transcript as the host writes them, for what a real
  attempt reaches only by chance: a hook's read-only call, a package tool's own base calls, a bridge R has edited.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import rsi
from runtime_world import Script, core, finish, minimal, seed, turn
from runtime_world import attempt as run_attempt

from reharness.activity import phases
from reharness.memory.retrieval import TaskMemory
from reharness.runtime import trace as T
from reharness.runtime.chat import ToolSpec
from reharness.runtime.records import EVENT_CHARS, Transcript, session_file
from reharness.runtime.trace import TraceAccessError, TraceError, TraceIndexError, TraceStore, parse_ref, plain

NOTHING = {"sessions": 0, "lines": 0, "docs": 0}


def quiet(_line):
    pass


# ---------------------------------------------------------------------------- the OpenAI form, written independently
def _call(c) -> dict:
    return c if isinstance(c, dict) else {"id": c.id, "name": c.name, "args": c.args}


def fn(call_id: str, name: str, args: dict) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, sort_keys=True, ensure_ascii=False)}}


def openai_of(system, history) -> list[dict]:
    """What a provider received (chat.py's history; an assistant entry's calls as ToolCall objects or dicts) in the
    OpenAI chat format. `system` None leaves the system message out."""
    out = [] if system is None else [{"role": "system", "content": system}]
    for m in history:
        if m["role"] == "assistant":
            entry = {"role": "assistant", "content": m.get("text") or ""}
            calls = [_call(c) for c in m.get("tool_calls") or ()]
            if calls:
                entry["tool_calls"] = [fn(c["id"], c["name"], c["args"]) for c in calls]
            out.append(entry)
        elif m["role"] == "tool":
            out += [{"role": "tool", "tool_call_id": r["id"], "content": r["output"]} for r in m["results"]]
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def reply_of(t) -> list[dict]:
    return openai_of(None, [{"role": "assistant", "text": t.text, "tool_calls": t.tool_calls}])


def agent_form(history) -> list[dict]:
    """A provider's history as the agent sent it (its calls as dicts): what calls() resolves."""
    return [{**m, "tool_calls": [_call(c) for c in m["tool_calls"]]} if m.get("role") == "assistant" else m
            for m in history]


# ---------------------------------------------------------------------------- workspaces and the index on disk
def digests(root: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root / "transcripts").glob("*.jsonl"))}


def copy_of(src: Path, dst: Path) -> Path:
    """A workspace holding the transcripts of `src` and no index yet."""
    shutil.copytree(Path(src) / "transcripts", Path(dst) / "transcripts")
    return Path(dst)


def indexed(root: Path, sql: str, *args) -> list:
    db = sqlite3.connect(Path(root) / "index" / "trace.sqlite")
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def only_in_results(store: TraceStore, session: str) -> str:
    """A word of the session's tool results that nothing else the index holds of it contains: its assistant texts,
    tool-call arguments, marks, hooks and egress rows."""
    def words(text: str) -> set:
        return set(re.findall(r"[a-z]{6,}", text.lower()))
    results, other = set(), set()
    for m in store.trajectory(session):
        if m["role"] == "tool":
            results |= words(m["content"])
        elif m["role"] == "assistant":
            other |= words(m["content"])
            for c in m.get("tool_calls") or ():
                other |= words(c["function"]["arguments"])
    for e in store.events(session):
        if e["event"]["kind"] in ("hook", "mark", "egress"):
            other |= words(json.dumps(e["event"], ensure_ascii=False))
    candidates = sorted(results - other)
    assert candidates, "the attempt's tool results share every word with the rest of what is indexed"
    return candidates[0]


class Splits(dict):
    """A split table still being filled (an attempt's result row not written yet), counting what it is asked."""

    def __init__(self, *args):
        super().__init__(*args)
        self.asked: list[str] = []

    def __call__(self, task_id: str) -> str:
        self.asked.append(task_id)
        return self[task_id]


# ---------------------------------------------------------------------------- synthetic transcripts
def sys_(text: str) -> dict:
    return {"role": "system", "content": text}


def user(text) -> dict:
    return {"role": "user", "content": text}


def asst(text: str = "", *calls) -> dict:
    return {"role": "assistant", "text": text, "tool_calls": [{"id": i, "name": n, "args": a} for i, n, a in calls]}


def results(*rows) -> dict:
    return {"role": "tool", "results": [{"id": i, "name": n, "output": o, "is_error": False} for i, n, o in rows]}


class Writer:
    """One session's transcript written as the host writes it (runtime/records.Transcript): the `task` line; each
    model call a `gateway_call` line with its messages as a prefix count against the previous call (any role) plus
    the tail; each world call a `tool` line (its output cut at EVENT_CHARS) and a `broker_event`; the package's
    `mark` and `hook` lines; egress rows, retrievals, the end. Every method returns the line it wrote."""

    def __init__(self, root: Path, session: str, task_id: str, *, split: str = "train", task_hash: str | None = None):
        self.session = session
        self.t = Transcript(session_file(root, session))
        self.previous: list = []
        self.models = self.tools = 0
        self.t.write("task", task_id=task_id, split=split, task_hash=task_hash or f"hash-{task_id}",
                     attempt_id=session, text=f"the task {task_id}")

    @property
    def line(self) -> int:
        return self.t.seq

    def ref(self, line: int) -> str:
        return f"{self.session}:L{line}"

    def call(self, messages: list, text: str = "", *calls, role: str = "act") -> int:
        messages = json.loads(json.dumps(messages))
        prefix = 0
        for a, b in zip(self.previous, messages):
            if a != b:
                break
            prefix += 1
        self.models += 1
        self.t.write("gateway_call", call_id=f"{self.session}:m{self.models}", attempt_id=self.session, role=role,
                     messages_prefix=prefix, messages_tail=messages[prefix:],
                     reply={"text": text, "tool_calls": [{"id": i, "name": n, "args": a} for i, n, a in calls],
                            "stop": "tool_use" if calls else "end"}, usage={})
        self.previous = messages
        return self.line

    def tool(self, name: str, output: str, *, read_only: bool = False, is_error: bool = False,
             broker: bool = True, chars: int | None = None) -> int:
        self.tools += 1
        call_id = f"{self.session}:b{self.tools}"
        self.t.write("tool", call_id=call_id, name=name, args={}, output=output[:EVENT_CHARS], is_error=is_error,
                     read_only=read_only)
        line = self.line
        if broker:
            self.t.write("broker_event", call_id=call_id, name=name, mutates=False, refused=False, reason="",
                         output_chars=len(output) if chars is None else chars, read_only=read_only)
        return line

    def mark(self, mark: str, /, **data) -> int:
        self.t.write("mark", mark=mark, data=data, source="package")
        return self.line

    def hook(self, site: str, name: str, action: str, detail: str = "") -> int:
        self.t.write("hook", site=site, name=name, action=action, detail=detail, turn=1, source="package")
        return self.line

    def egress(self, **row) -> int:
        self.t.write("egress", **row)
        return self.line

    def retrieval(self, site: str, shown: list) -> int:
        self.t.write("library_retrieval", site=site, query="q", shown=list(shown), scores=[0.5] * len(shown))
        return self.line

    def end(self, termination: str = "finished") -> int:
        self.t.write("end", termination=termination, limit=None, error="", turns=self.models)
        return self.line

    def raw(self, text: str) -> None:
        with self.t.path.open("a", encoding="utf-8") as f:
            f.write(text)


# ---------------------------------------------------------------------------- real: the production baseline
class Recording(rsi.BusinessWorker):
    """The rsi scripted actor, keeping each reply beside what it was sent."""

    def __init__(self):
        super().__init__()
        self.replies: list = []

    def chat(self, system, messages, specs, **kw):
        reply = super().chat(system, messages, specs, **kw)
        self.replies.append(reply)
        return reply


@pytest.fixture(scope="module")
def baseline(tmp_path_factory, fake_upstream):
    """W0's baseline over one train and one dev task of the rsi domain: freeze, the startup checks, the budget book,
    the runner shell and the stirrup seed in a ProcessWorker."""
    provider = Recording()
    a = rsi.activity(tmp_path_factory.mktemp("baseline"), fake_upstream, provider=provider)
    phases.baseline(a, limit=1, out=quiet)
    rows = {r.split: r for r in a.results.all()}
    assert sorted(rows) == ["dev", "train"] and len(list(a.results.all())) == 2
    return SimpleNamespace(root=a.root, split_of=a.splits.split_of, train=rows["train"].evidence,
                           dev=rows["dev"].evidence, provider=provider)


def test_a_real_attempts_trajectory_is_what_the_actor_was_sent_and_reading_it_changes_no_record(baseline, tmp_path):
    root = copy_of(baseline.root, tmp_path / "ws")
    before = digests(root)
    store = TraceStore(root, baseline.split_of)
    train, dev = baseline.train, baseline.dev
    task = store.events(train)[0]["event"]
    assert (task["kind"], task["attempt_id"], session_file(root, train).name) == \
        ("task", train, train.replace("/", "__") + ".jsonl")
    calls = [e["event"] for e in store.events(train) if e["event"]["kind"] == "gateway_call"]
    p = baseline.provider
    mine = [(s, r) for s, r in zip(p.seen, p.replies)
            if next(m["content"] for m in s["messages"] if m["role"] == "user") == task["text"]]
    assert len(mine) == len(calls) >= 3
    sent, last = mine[-1]
    want = openai_of(sent["system"], sent["messages"]) + reply_of(last)
    got = store.trajectory(train)
    assert plain(got) == want
    assert store.trajectory(train, annotations=False) == want
    # every message points at the line it came from; every result at the call that carried it in full
    for message in got:
        assert store.get(message["metadata"]["ref"])["event"]["kind"] == "gateway_call"
        if message["role"] == "tool":
            assert message["metadata"]["source"] == "gateway_call" and message["metadata"]["truncated"] is False
    # the attempt ended on its last finish call: nothing answered it
    assert [c.name for c in last.tool_calls] == ["finish"]
    assert got[-1]["metadata"]["unanswered"] == [last.tool_calls[0].id]
    # calls(): the prefix chain resolved, call by call what the provider was sent and what it answered
    chain = store.calls(train)
    assert [c["call_id"] for c in chain] == [c["call_id"] for c in calls] and {c["role"] for c in chain} == {"act"}
    for c, (s, r) in zip(chain, mine):
        assert c["messages"][0] == {"role": "system", "content": s["system"]}
        assert c["messages"][1:] == agent_form(s["messages"])
        assert c["response"] == {"text": r.text, "tool_calls": [_call(x) for x in r.tool_calls], "stop": r.stop}
        assert store.get(c["ref"])["event"]["call_id"] == c["call_id"]
    # finish(): the last finish call's reason, no reply text, the end line's termination, nothing shown
    assert store.finish(train) == {"reason": last.tool_calls[0].args["reason"], "text": "", "termination": "finished",
                                   "shown": []}
    # the dev attempt is refused, whatever the read
    for read in (lambda: store.trajectory(dev), lambda: store.events(dev), lambda: store.get(f"{dev}:L1"),
                 lambda: store.calls(dev), lambda: store.task_hash(dev), lambda: store.finish(dev)):
        with pytest.raises(TraceAccessError, match=r"is not a train session \(dev\)"):
            read()
    assert store.sessions() == [{"session": train, "task_hash": task["task_hash"]}]
    # a word only a tool result of the train attempt holds is found, and only there
    word = only_in_results(store, train)
    hits = store.search(word)
    assert hits and {h["kind"] for h in hits} == {"tool_result"} and {h["session"] for h in hits} == {train}
    assert {h["task_hash"] for h in hits} == {task["task_hash"]}
    # nothing read or indexed changed a transcript; the index is a file of its own and holds the train attempt only
    assert digests(root) == before
    assert indexed(root, "SELECT DISTINCT session FROM docs") == [(train,)]
    assert indexed(root, "SELECT session FROM sessions") == [(train,)]


# ---------------------------------------------------------------------------- real: the stirrup seed over a ledger
SUMMARY = "SUMMARY: searched the renewals ledger and fetched account 7; next, find the owners."
WORDS = {"renewals": "zanzibarite", "owners": "quillwort", "audit": "heliotropic", "limits": "marzipanic"}
USAGE = {"input_tokens": 1000, "output_tokens": 20}
OVER = {"input_tokens": 150_000, "output_tokens": 20}     # past Stirrup's summarize_at: 0.7 of a 200,000-token window
PLAN = {"changes": [{"content": "move the contact to opportunity", "status": "pending"}], "excluded": []}
SPLITS = {"renewals": "train", "audit": "test", "limits": "train", "plan": "train", "blocked": "train"}
TASKS = {"renewals": "Find the renewal owners.", "audit": "Audit the renewals.", "limits": "Check the limits.",
         "plan": "Plan the update.", "blocked": "Move the contact."}


def big(word: str, at: int = 4000, size: int = 6000) -> str:
    filler = "row " * (size // 4)
    return (filler[:at] + f" {word} " + filler)[:size]


class Ledger:
    """The business surface: a search whose output is far over a tool line's EVENT_CHARS (its one distinctive word
    past them), and a fetch, a write unless its method is GET."""

    def __init__(self):
        self.calls: list = []

    def specs(self):
        return [ToolSpec("api_search", "search", {"type": "object", "properties": {"q": {"type": "string"}}}),
                ToolSpec("api_fetch", "fetch", {"type": "object", "properties": {"url": {"type": "string"},
                                                                                  "method": {"type": "string"}}})]

    def run(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "api_search":
            return SimpleNamespace(output=big(WORDS.get(args.get("q"), "plainword")), is_error=False)
        return SimpleNamespace(output=f"account {args.get('url')}: owner Dana Reyes", is_error=False)

    def mutates(self, name, args):
        return name == "api_fetch" and str(args.get("method", "GET")).upper() != "GET"

    @property
    def tool_calls(self):
        return len(self.calls)


class Recorded(Script):
    """runtime_world's scripted provider, keeping each reply beside what it was sent."""

    def chat(self, system, messages, specs):
        reply = super().chat(system, messages, specs)
        self.seen[-1]["reply"] = reply
        return reply


def session_id(name: str) -> str:
    return f"a1b2/rsi.{name}/r0/subject#0"


def run_seed(root: Path, name: str, turns: list, *, limits: dict | None = None) -> SimpleNamespace:
    """One attempt of the stirrup seed under the runner shell, in a ProcessWorker, over a Ledger. Its task line says
    split "train" whatever the frozen split says."""
    k = core(limits=limits or {})
    provider, world = Recorded(turns), Ledger()
    a = run_attempt(root, seed(root / "seed", k), provider, world=world, k=k, task=TASKS[name],
                    attempt_id=session_id(name),
                    task_meta={"task_id": name, "split": "train", "task_hash": f"hash-{name}"})
    return SimpleNamespace(session=session_id(name), provider=provider, world=world, result=a.result)


@pytest.fixture(scope="module")
def stirrup(tmp_path_factory):
    root = tmp_path_factory.mktemp("stirrup")
    runs = {
        # two reads, the second reporting a context past the cutoff: Stirrup summarizes (role aux), replaces the
        # history with the task, its bridge and its acknowledgement, and goes on; the first finish is pushed back
        "renewals": run_seed(root, "renewals", [
            turn("Searching the ledger.", ("api_search", {"q": "renewals"}), usage=USAGE),
            turn("Fetching the account.", ("api_fetch", {"url": "acct/7", "method": "GET"}), usage=OVER),
            turn(SUMMARY, usage=USAGE),
            turn("Finding the owners.", ("api_search", {"q": "owners"}), usage=USAGE),
            finish("Done: the renewal owners are known.", usage=USAGE)]),
        # a test-split task (its task line says train)
        "audit": run_seed(root, "audit", [turn("Auditing.", ("api_search", {"q": "audit"}), usage=USAGE),
                                          finish("Audited.", usage=USAGE)]),
        # cut by the turn limit right after a tool turn: no later call carries that turn's results
        "limits": run_seed(root, "limits", [turn("Reading.", ("api_fetch", {"url": "acct/1", "method": "GET"})),
                                            turn("Reading more.", ("api_search", {"q": "limits"}))],
                           limits={"max_steps": 2}),
        # one turn: the package's plan tool and a read, then the turn limit
        "plan": run_seed(root, "plan", [turn("Planning.", ("plan_changes", PLAN),
                                             ("api_fetch", {"url": "acct/9", "method": "GET"}))],
                         limits={"max_steps": 1}),
        # one turn: a write before any plan (the write guard stops it) and a finish (skipped after the stop)
        "blocked": run_seed(root, "blocked", [turn("Writing.", ("api_fetch", {"url": "acct/9", "method": "POST"}),
                                                   ("finish", {"reason": "Moved."}))],
                            limits={"max_steps": 1}),
    }
    assert runs["renewals"].result.text == "Done: the renewal owners are known."
    return SimpleNamespace(root=root, split_of=SPLITS.__getitem__, runs=runs,
                           sessions={n: r.session for n, r in runs.items()})


def test_a_stirrup_summary_rebuilds_whole_without_the_bridge_and_with_full_results(stirrup):
    store = TraceStore(stirrup.root, stirrup.split_of)
    run = stirrup.runs["renewals"]
    session = run.session
    events = store.events(session)
    calls = [e["event"] for e in events if e["event"]["kind"] == "gateway_call"]
    assert [c["role"] for c in calls] == ["act", "act", "aux", "act", "act", "act"]
    assert [e["event"]["phase"] for e in events if e["event"].get("mark") == "stirrup_summary"] == \
        ["start", "call", "done"]
    # what the provider received: the aux call holds everything before the summary, the last act call the rest
    aux, last = run.provider.seen[2], run.provider.seen[-1]
    bridge, ack = last["messages"][1]["content"], last["messages"][2]["content"]
    assert SUMMARY in bridge and ack == "Got it, thanks!"                  # Stirrup's own summarize_messages
    want = (openai_of(aux["system"], aux["messages"][:-1])                # without the summarizer's prompt
            + openai_of(None, last["messages"][3:]) + reply_of(last["reply"]))
    got = store.trajectory(session)
    assert plain(got) == want
    contents = [m["content"] for m in got]
    assert bridge not in contents and ack not in contents and aux["messages"][-1]["content"] not in contents
    assert not any(SUMMARY in c for c in contents)
    # the bridge and the acknowledgement are kept as the annotation of the first act call after the summary
    (after,) = [m for m in got if "after_summary" in m["metadata"]]
    note = after["metadata"]["after_summary"]
    aux_line = next(e["line"] for e in events if e["event"].get("role") == "aux")
    assert note["aux_refs"] == [f"{session}:L{aux_line}"] and note["aux_call_ids"] == [calls[2]["call_id"]]
    assert note["dropped"] == [
        {"role": "user", "chars": len(bridge), "sha256": hashlib.sha256(bridge.encode()).hexdigest()[:16],
         "excerpt": bridge[:200]},
        {"role": "user", "chars": len(ack), "sha256": hashlib.sha256(ack.encode()).hexdigest()[:16], "excerpt": ack}]
    assert "bridge_not_found" not in note and "acknowledgement_not_found" not in note
    assert (after["role"], after["content"], after["metadata"]["call_id"]) == \
        ("assistant", "Finding the owners.", calls[3]["call_id"])
    # every result is the full text the actor saw, not the tool line's excerpt
    tool_lines = [e["event"] for e in events if e["event"]["kind"] == "tool"]
    assert all(len(e["output"]) <= EVENT_CHARS for e in tool_lines)
    answers = [m for m in got if m["role"] == "tool"]
    assert [m["metadata"]["tool"] for m in answers] == ["api_search", "api_fetch", "api_search", "finish"]
    assert [len(m["content"]) for m in answers[:3]] == [6000, len("account acct/7: owner Dana Reyes"), 6000]
    assert "zanzibarite" in answers[0]["content"] and "zanzibarite" not in tool_lines[0]["output"]
    assert all(m["metadata"]["source"] == "gateway_call" and not m["metadata"]["truncated"] for m in answers)
    # the second read's result came from the aux call, the only call that carried it
    assert answers[1]["metadata"]["ref"] == note["aux_refs"][0]
    assert got[-1]["metadata"]["unanswered"] == [last["reply"].tool_calls[0].id]
    # finish(): the last finish call's reason and the last act reply's text
    assert store.finish(session) == {"reason": "Done: the renewal owners are known.", "text": "Finding the owners.",
                                     "termination": "finished", "shown": []}


def test_results_no_later_call_carries_come_from_the_records_of_their_turn_marked_as_excerpts(stirrup):
    store = TraceStore(stirrup.root, stirrup.split_of)
    s = stirrup.sessions
    # cut by the turn limit: the first read's result was carried by the second call; the second's is the tool line's
    first, second = [m for m in store.trajectory(s["limits"]) if m["role"] == "tool"]
    assert first["metadata"]["source"] == "gateway_call" and first["content"] == "account acct/1: owner Dana Reyes"
    line = store.get(second["metadata"]["ref"])["event"]
    assert line["kind"] == "tool" and second["content"] == line["output"] and len(second["content"]) == EVENT_CHARS
    assert (second["metadata"]["source"], second["metadata"]["truncated"]) == ("tool_event", True)
    assert "marzipanic" not in second["content"]
    # a package tool's result from its mark; the base call beside it from its tool line
    plan, fetched = [m for m in store.trajectory(s["plan"]) if m["role"] == "tool"]
    mark = store.get(plan["metadata"]["ref"])["event"]
    assert (mark["kind"], mark["mark"], mark["tool"]) == ("mark", "package_tool", "plan_changes")
    assert plan["content"] == "Plan recorded: 1 changes, 0 excluded"
    assert (plan["metadata"]["source"], plan["metadata"]["truncated"]) == ("package_tool", False)
    assert fetched["content"] == "account acct/9: owner Dana Reyes"
    assert (fetched["metadata"]["source"], fetched["metadata"]["truncated"]) == ("tool_event", False)
    # the write the guard stopped, and the finish skipped after it in the same turn
    blocked, skipped = [m for m in store.trajectory(s["blocked"]) if m["role"] == "tool"]
    b = store.get(blocked["metadata"]["ref"])["event"]
    assert (b["mark"], b["tool"], b["call_id"]) == ("tool_blocked", "api_fetch", blocked["tool_call_id"])
    assert blocked["content"] == "Stopped before execution: " + b["reason"]
    assert blocked["content"].startswith("Stopped before execution: Before your first change, call plan_changes")
    assert (blocked["metadata"]["source"], blocked["metadata"]["is_error"], blocked["metadata"]["truncated"]) == \
        ("tool_blocked", True, True)
    assert skipped["content"] == "Not executed: an earlier call in this turn was stopped"
    assert (skipped["metadata"]["source"], skipped["metadata"]["tool"]) == ("tool_skipped", "finish")
    assert stirrup.runs["blocked"].world.calls == []
    assert not any("unanswered" in m["metadata"] for n in ("limits", "plan", "blocked")
                   for m in store.trajectory(s[n]))
    # the attempts have ended, so the excerpts are indexed: words before the cut are found, words past it are not
    assert [h["ref"] for h in store.search("row", kind="tool_result", task_hash="hash-limits")] == \
        [second["metadata"]["ref"]]
    assert store.search("marzipanic") == []
    (planned,) = store.search("recorded", kind="tool_result", tool="plan_changes")
    assert planned["ref"] == plan["metadata"]["ref"]
    (stopped,) = store.search("executed", kind="tool_result", tool="finish")
    assert stopped["ref"] == skipped["metadata"]["ref"]


def test_references_are_stable_as_the_transcript_grows_and_across_stores(stirrup, tmp_path):
    root = copy_of(stirrup.root, tmp_path / "ws")
    session = stirrup.sessions["renewals"]
    path = session_file(root, session)
    assert path.name == "a1b2__rsi.renewals__r0__subject#0.jsonl"
    store = TraceStore(root, stirrup.split_of)
    before = {e["ref"]: e["event"] for e in store.events(session)}
    assert list(before) == [f"{session}:L{n}" for n in range(1, len(before) + 1)]
    trajectory = store.trajectory(session)
    assert all(store.get(ref) == {"ref": ref, "line": int(ref.rsplit(":L", 1)[1]), "event": event}
               for ref, event in before.items())
    # the transcript is append-only: a later line (an egress row) moves nothing
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "egress", "ts": "x", "seq": len(before) + 1, "method": "GET",
                            "host": "api.vendor.test", "port": 443, "status": 200, "bytes": 12,
                            "decision": "allowed"}) + "\n")
    again = TraceStore(root, stirrup.split_of)
    after = {e["ref"]: e["event"] for e in again.events(session)}
    assert {k: v for k, v in after.items() if k in before} == before and len(after) == len(before) + 1
    assert again.trajectory(session) == trajectory
    (hit,) = again.search("api vendor test", kind="egress")
    assert hit["ref"] == f"{session}:L{len(after)}" and again.get(hit["ref"])["event"]["host"] == "api.vendor.test"
    with pytest.raises(TraceError, match="no event on that line"):
        again.get(f"{session}:L{len(after) + 1}")
    # a reference whose session is not one (a path out of the transcripts): an error, not a read of another split
    with pytest.raises(TraceError, match="not a session id") as info:
        again.get("a1b2/../../x:L1")
    assert not isinstance(info.value, TraceAccessError)


@pytest.mark.parametrize("session", ["a1b2/rsi.renewals/r0/subject#0", "run:L3/x#0", "x@y+z-1.2", "s"])
def test_a_reference_parses_back_to_its_session_and_line(session):
    assert parse_ref(TraceStore.ref(session, 12)) == (session, 12)
    assert parse_ref(TraceStore.ref(session, 999_999_999)) == (session, 999_999_999)


@pytest.mark.parametrize("bad", ["nope", ":L1", "s:L0", "s:L01", "s:1", "s:L", "../s:L1", "s:L1234567890",
                                 "a b:L1", "s:L1 ", "x" * 301 + ":L1", None, 3])
def test_a_malformed_reference_is_refused(bad):
    with pytest.raises(TraceError, match="not a trace reference"):
        parse_ref(bad)


def test_search_finds_words_only_a_full_result_holds_and_nothing_of_a_held_out_session(stirrup):
    store = TraceStore(stirrup.root, stirrup.split_of)
    s = stirrup.sessions
    (hit,) = store.search("zanzibarite")
    assert (hit["session"], hit["kind"], hit["tool"], hit["task_hash"]) == \
        (s["renewals"], "tool_result", "api_search", "hash-renewals")
    assert "[zanzibarite]" in hit["excerpt"] and store.get(hit["ref"])["event"]["kind"] == "gateway_call"
    (owners,) = store.search("quillwort", tool="api_search")
    assert owners["session"] == s["renewals"] and store.search("quillwort", tool="api_fetch") == []
    assert store.search("renewals", kind="tool_call") and store.search("zanzibarite", kind="assistant") == []
    assert store.search("owners", kind="assistant")[0]["session"] == s["renewals"]
    assert store.search("zanzibarite", task_hash="hash-limits") == []
    (stopped,) = store.search("triage", kind="hook")
    assert stopped["session"] == s["blocked"] and store.get(stopped["ref"])["event"]["action"] == "block"
    (planned,) = store.search("recorded", kind="mark", tool="plan_changes")
    assert store.get(planned["ref"])["event"]["mark"] == "package_tool"
    # every word must match, in one document: the two reads' words are in two different results
    assert store.search("zanzibarite quillwort") == []
    assert store.search("zanzibarite row")[0]["ref"] == hit["ref"]
    # no query syntax gets through: a phrase break, a prefix, a column filter, an operator
    for query in ('zanzibarite" OR "quillwort', "zanzib*", "text:zanzibarite", "NEAR(zanzibarite quillwort)", "",
                  "  -- ;"):
        assert store.search(query) == [], query
    with pytest.raises(ValueError, match="kind 'tool' is not one of"):
        store.search("zanzibarite", kind="tool")
    # the test-split attempt (its task line says train): nothing of it is searchable, readable or indexed
    test = s["audit"]
    assert store.search("heliotropic") == [] and store.search("Auditing") == []
    for read in (lambda: store.trajectory(test), lambda: store.events(test), lambda: store.get(f"{test}:L2"),
                 lambda: store.finish(test)):
        with pytest.raises(TraceAccessError, match=r"\(test\)"):
            read()
    train = {s[n] for n in ("renewals", "limits", "plan", "blocked")}
    assert {r[0] for r in indexed(stirrup.root, "SELECT session FROM sessions")} == train
    assert {r[0] for r in indexed(stirrup.root, "SELECT DISTINCT session FROM docs")} == train


def test_the_index_is_incremental_derived_and_rebuildable(stirrup, tmp_path):
    root = copy_of(stirrup.root, tmp_path / "ws")
    store = TraceStore(root, stirrup.split_of)
    train = [stirrup.sessions[n] for n in ("renewals", "limits", "plan", "blocked")]
    first = store.index()
    lines = sum(len(session_file(root, s).read_text(encoding="utf-8").splitlines()) for s in train)
    assert first["sessions"] == 4 and first["lines"] == lines and first["docs"] > 0
    assert store.index() == NOTHING                                   # nothing new: nothing read again
    assert TraceStore(root, stirrup.split_of).index() == NOTHING      # the index's own record, not the store's memory
    hits = store.search("zanzibarite")
    assert hits and indexed(root, "SELECT count(*) FROM docs") == [(first["docs"],)]
    assert store.search("zanzibarite") == hits
    # an index of another schema is built again
    db = sqlite3.connect(root / "index" / "trace.sqlite")
    db.execute("UPDATE meta SET value = '1' WHERE key = 'schema'")
    db.commit()
    db.close()
    assert store.search("zanzibarite") == hits and indexed(root, "SELECT count(*) FROM docs") == [(first["docs"],)]
    # a file that is not a database (torn, foreign) is not trusted: the derived index is built again from the records
    for suffix in ("-wal", "-shm"):
        Path(str(root / "index" / "trace.sqlite") + suffix).unlink(missing_ok=True)
    (root / "index" / "trace.sqlite").write_bytes(b"not a database" * 100)
    assert store.search("zanzibarite") == hits
    assert indexed(root, "SELECT value FROM meta WHERE key = 'schema'") == [(str(T.SCHEMA),)]
    assert indexed(root, "SELECT count(*) FROM docs") == [(first["docs"],)]


@pytest.mark.parametrize("name", ["renewals", "limits"])
def test_indexing_is_incremental_by_session_and_line_and_waits_for_a_torn_line(stirrup, tmp_path, name):
    session = stirrup.sessions[name]
    whole = copy_of(stirrup.root, tmp_path / "whole")
    TraceStore(whole, stirrup.split_of).index()
    want = sorted(indexed(whole, "SELECT text, ref, kind, tool FROM docs WHERE session = ?", session))
    lines = session_file(stirrup.root, session).read_text(encoding="utf-8").splitlines(keepends=True)
    assert json.loads(lines[-1])["kind"] == "end"
    grown = tmp_path / "grown"
    path = session_file(grown, session)
    path.parent.mkdir(parents=True)
    half = len(lines) // 2
    path.write_text("".join(lines[:half]) + lines[half][:20], encoding="utf-8")       # an append caught half-written
    store = TraceStore(grown, stirrup.split_of)
    assert store.index()["lines"] == half
    with path.open("a", encoding="utf-8") as f:
        f.write(lines[half][20:] + "".join(lines[half + 1:-1]))
    assert store.index()["lines"] == len(lines) - 1 - half
    with path.open("a", encoding="utf-8") as f:
        f.write(lines[-1])
    # the end line itself holds nothing to index; it lets in the excerpts of the results no later call carried
    assert store.index() == {"sessions": 1, "lines": 1, "docs": {"renewals": 0, "limits": 1}[name]}
    assert sorted(indexed(grown, "SELECT text, ref, kind, tool FROM docs WHERE session = ?", session)) == want
    assert indexed(grown, "SELECT lines, ended FROM sessions WHERE session = ?", session) == [(len(lines), 1)]


def test_a_live_attempt_placed_too_early_is_placed_once_it_can_be_and_a_final_answer_is_kept(stirrup, tmp_path):
    """A store that met an attempt before it could be placed — its first line half-written, or its task not yet in
    the split table — places it once it can: neither is kept. A split given, or content before any task, is."""
    session = stirrup.sessions["renewals"]
    lines = session_file(stirrup.root, session).read_text(encoding="utf-8").splitlines(keepends=True)
    live = tmp_path / "live"
    path = session_file(live, session)
    path.parent.mkdir(parents=True)
    known = Splits()
    store = TraceStore(live, known)
    path.write_text(lines[0][:15], encoding="utf-8")                  # the task line, torn
    assert store.sessions() == [] and store.search("zanzibarite") == [] and known.asked == []
    path.write_text("".join(lines), encoding="utf-8")                 # the whole attempt, its split not yet known
    assert store.sessions() == [] and store.search("zanzibarite") == []
    asked = len(known.asked)
    assert asked >= 2 and store.split(session) is None and len(known.asked) == asked + 1    # asked again each time
    known["renewals"] = "train"                                       # the result row is written: train
    assert [s["session"] for s in store.sessions()] == [session]
    (hit,) = store.search("zanzibarite")
    assert hit["session"] == session
    asked = len(known.asked)
    assert store.split(session) == "train" and store.task_hash(session) == "hash-renewals"
    assert len(known.asked) == asked                                  # a split given is kept
    # content before any task: never an attempt this store can place, and the answer is kept
    odd = live / "transcripts" / "odd.jsonl"
    odd.write_text(json.dumps({"kind": "tool", "name": "n", "output": "o"}) + "\n", encoding="utf-8")
    assert store.split("odd") is None
    odd.write_text(json.dumps({"kind": "task", "task_id": "renewals", "attempt_id": "odd"}) + "\n", encoding="utf-8")
    assert store.split("odd") is None and TraceStore(live, known).split("odd") == "train"


def test_two_stores_indexing_one_workspace_at_once_insert_each_document_once(stirrup, tmp_path):
    want = TraceStore(copy_of(stirrup.root, tmp_path / "reference"), stirrup.split_of).index()["docs"]
    assert want > 0
    shared = copy_of(stirrup.root, tmp_path / "shared")
    for round_ in range(3):
        for suffix in ("", "-wal", "-shm"):
            Path(str(shared / "index" / "trace.sqlite") + suffix).unlink(missing_ok=True)
        barrier = threading.Barrier(6)
        errors: list = []

        def one():
            store = TraceStore(shared, stirrup.split_of)
            try:
                barrier.wait(10)
                store.search("zanzibarite")
            except Exception as e:   # noqa: BLE001 - reported below
                errors.append(e)
        threads = [threading.Thread(target=one) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert errors == [], errors
        got = indexed(shared, "SELECT count(*) FROM docs")[0][0]
        dupes = indexed(shared, "SELECT count(*) FROM (SELECT text, ref, kind FROM docs GROUP BY text, ref, kind "
                                "HAVING count(*) > 1)")[0][0]
        assert (got, dupes) == (want, 0), round_


def test_a_store_waits_for_another_connection_switching_a_new_index_to_wal(stirrup, tmp_path):
    """While another store switches the same new index file to WAL, it holds that file's write lock in the rollback
    journal the file starts in; SQLite answers a second switch then with "database is locked" at once, without its
    busy wait (it could deadlock). The store waits for the lock instead of failing the search."""
    root = copy_of(stirrup.root, tmp_path / "ws")
    path = root / "index" / "trace.sqlite"
    path.parent.mkdir(parents=True)
    held, released = threading.Event(), threading.Event()

    def hold():
        db = sqlite3.connect(path, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            held.set()
            time.sleep(0.3)
            db.execute("ROLLBACK")
        finally:
            db.close()
            released.set()
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(10)
        (hit,) = TraceStore(root, stirrup.split_of).search("zanzibarite")
    finally:
        thread.join(10)
    assert hit["session"] == stirrup.sessions["renewals"] and released.is_set()


def test_without_fts5_search_raises_and_does_not_fall_back(stirrup, tmp_path, monkeypatch):
    root = copy_of(stirrup.root, tmp_path / "ws")

    class NoFts:
        OperationalError = sqlite3.OperationalError
        DatabaseError = sqlite3.DatabaseError
        Connection = sqlite3.Connection
        sqlite_version = sqlite3.sqlite_version

        @staticmethod
        def connect(*args, **kwargs):
            db = sqlite3.connect(*args, **kwargs)

            class Guard:
                def execute(self, sql, *a):
                    if "fts5" in sql.lower():
                        raise sqlite3.OperationalError("no such module: fts5")
                    return db.execute(sql, *a)

                def close(self):
                    db.close()
            return Guard()
    monkeypatch.setattr(T, "sqlite3", NoFts)
    store = TraceStore(root, stirrup.split_of)
    for read in (lambda: store.search("zanzibarite"), store.index):
        with pytest.raises(TraceIndexError, match="FTS5"):
            read()
    assert not (root / "index" / "trace.sqlite").exists()
    assert store.trajectory(stirrup.sessions["renewals"])                 # the rest of the store needs no index


# ---------------------------------------------------------------------------- real: an agent that edits its history
COMPACTING = '''
def run(ctx, task):
    tools = [dict(t) for t in ctx.view["base_tools"]]
    msgs = [{"role": "system", "content": ctx.view["system"]}, {"role": "user", "content": task}]
    for turn in range(1, 9):
        reply = ctx.model(msgs, tools, role="act")
        msgs.append({"role": "assistant", "text": reply.text, "tool_calls": reply.tool_calls})
        if not reply.tool_calls:
            return reply.text
        results = []
        for c in reply.tool_calls:
            out = ctx.tool(c["name"], c["args"])
            results.append({"id": c["id"], "name": c["name"], "output": out.output, "is_error": out.is_error})
        msgs.append({"role": "tool", "results": results})
        if turn == 1:
            msgs.append({"role": "user", "content": "Note: look up the account next."})
        if turn == 2:
            # compaction, as an agent does it for old outputs: the first read's text replaced in place
            msgs[3]["results"][0]["output"] = "[output compacted to save context]"
    return "out of turns"
'''


def test_an_agent_that_compacts_old_outputs_still_rebuilds_full_and_indexes_each_result_once(tmp_path):
    provider = Recorded([turn("Searching.", ("api_search", {"q": "renewals"})),
                         turn("Fetching.", ("api_fetch", {"url": "acct/7", "method": "GET"})), turn("Done.")])
    a = run_attempt(tmp_path, minimal(tmp_path / "pkg", COMPACTING), provider, world=Ledger(),
                    task="Find the renewal owners.", attempt_id="run/compact#0",
                    task_meta={"task_id": "compact", "split": "train", "task_hash": "hash-compact"})
    assert a.result.text == "Done."
    store = TraceStore(tmp_path, {"compact": "train"}.__getitem__)
    second, third = provider.seen[1], provider.seen[2]
    assert third["messages"][2]["results"][0]["output"].startswith("[output compacted")     # what was sent last
    want = openai_of(second["system"], second["messages"]) + openai_of(None, third["messages"][4:]) + \
        reply_of(third["reply"])
    got = store.trajectory("run/compact#0")
    assert plain(got) == want
    assert [m["content"] for m in got if m["role"] == "user"] == ["Find the renewal owners.",
                                                                 "Note: look up the account next."]
    assert "zanzibarite" in [m for m in got if m["role"] == "tool"][0]["content"]
    # the gateway sent the edited entry again (the third call's prefix stops before it); its copy is not indexed
    third_line = [e["event"] for e in store.events("run/compact#0") if e["event"]["kind"] == "gateway_call"][2]
    assert third_line["messages_prefix"] == 3
    assert len(store.search("zanzibarite")) == 1
    assert store.search("compacted", kind="tool_result") == []


# ---------------------------------------------------------------------------- real: what the memory showed
def test_finish_reads_the_last_finish_the_last_text_and_the_entries_the_memory_showed_in_order(tmp_path):
    """The start's library_retrieval line (right after the task line) and each recall's, as the runner writes them
    from a TaskMemory; the seed's verify hook pushes the first finish back, so the second one is the attempt's."""
    k = core(memory={"enabled": True, "recall_k": 2})
    entries = [{"id": "e1bbbbbb", "when": "after an update", "content": "Post in #ops after the update.", "score": 0.9},
               {"id": "e2cccccc", "when": "a notice", "content": "Name the record.", "score": 0.8}]
    memory = TaskMemory(opening="OPENING BLOCK",
                        event={"query": "Post the note.", "shown": ["e0aaaaaa-full", "e1bbbbbb-full"],
                               "scores": [0.7, 0.66]},
                        recaller=lambda query, n: entries[:n])
    provider = Script([turn("Checking the notes.", ("recall", {"query": "ops"})),
                       turn("Checking again.", ("recall", {"query": "ops update"})),
                       finish("Posted."), finish("Posted the note.\nlibrary-used: e1bbbbbb")])
    a = run_attempt(tmp_path, seed(tmp_path / "seed", k), provider, k=k, task="Post the note.",
                    attempt_id="run/memory#0", memory=memory,
                    task_meta={"task_id": "memory", "split": "train", "task_hash": "hash-memory"})
    assert a.result.termination == "finished"
    store = TraceStore(tmp_path, {"memory": "train"}.__getitem__)
    lines = [e["event"] for e in store.events("run/memory#0")]
    assert [line["kind"] for line in lines[:2]] == ["task", "library_retrieval"]
    assert [(line["site"], line["shown"]) for line in lines if line["kind"] == "library_retrieval"] == [
        ("start", ["e0aaaaaa-full", "e1bbbbbb-full"]), ("recall", ["e1bbbbbb", "e2cccccc"]),
        ("recall", ["e1bbbbbb", "e2cccccc"])]
    assert any(line["kind"] == "hook" and line["action"] == "pushback" for line in lines)
    assert store.finish("run/memory#0") == {
        "reason": "Posted the note.\nlibrary-used: e1bbbbbb", "text": "Checking again.", "termination": "finished",
        "shown": ["e0aaaaaa-full", "e1bbbbbb-full", "e1bbbbbb", "e2cccccc"]}


# ---------------------------------------------------------------------------- synthetic: who may read what
def test_train_only_follows_the_frozen_split_of_the_task_lines_task_id_not_the_lines_own_split(tmp_path):
    table = {"t-train": "train", "t-dev": "dev", "t-test": "test", "t-odd": 7}

    def split_of(task_id: str):
        if task_id == "t-bad":
            raise ValueError("not a task of this manifest")
        return table[task_id]
    cases = (("s/train#0", "t-train", "test", "train"), ("s/dev#0", "t-dev", "train", "dev"),
             ("s/test#0", "t-test", "train", "test"), ("s/unknown#0", "t-unknown", "train", None),
             ("s/bad#0", "t-bad", "train", None), ("s/odd#0", "t-odd", "train", None))
    for session, task_id, line_split, _ in cases:
        w = Writer(tmp_path, session, task_id, split=line_split)
        w.call([sys_("S"), user("Go.")], "Done.")
        w.end()
    store = TraceStore(tmp_path, split_of)
    assert [store.split(c[0]) for c in cases] == [c[3] for c in cases]
    assert store.sessions() == [{"session": "s/train#0", "task_hash": "hash-t-train"}]
    assert store.task_hash("s/train#0") == "hash-t-train" and store.trajectory("s/train#0")[-1]["content"] == "Done."
    for session, _, _, split in cases[1:]:
        refused = rf"session {re.escape(session)} is not a train session \({split or 'split unknown'}\)"
        for read in (lambda: store.get(f"{session}:L1"), lambda: store.events(session),
                     lambda: store.trajectory(session), lambda: store.calls(session),
                     lambda: store.finish(session), lambda: store.task_hash(session)):
            with pytest.raises(TraceAccessError, match=refused):
                read()
    # no transcript, or not a session id: an error of its own, not a reading of another split
    for missing, why in (("s/none#0", "no transcript"), ("a/../b", "not a session id")):
        with pytest.raises(TraceError, match=why) as info:
            store.events(missing)
        assert not isinstance(info.value, TraceAccessError)


def test_sessions_lists_the_train_transcripts_by_their_task_lines_attempt_id(tmp_path):
    assert TraceStore(tmp_path, {}.__getitem__).sessions() == []          # no transcripts directory yet
    for session in ("b/2#0", "a/1#0", "c:3#0"):
        Writer(tmp_path, session, "t1").end()
    Writer(tmp_path, "d/4#0", "t2").end()                                  # a dev attempt
    t = tmp_path / "transcripts"
    assert sorted(p.name for p in t.glob("*.jsonl")) == ["a__1#0.jsonl", "b__2#0.jsonl", "c:3#0.jsonl", "d__4#0.jsonl"]
    shutil.copy(t / "a__1#0.jsonl", t / "copy.jsonl")                      # a copy: its task line names another file
    for name, lines in (("anon", [{"kind": "task", "task_id": "t1"}]),                          # no attempt id
                        ("bad", [{"kind": "task", "task_id": "t1", "attempt_id": "../bad"}]),   # not a session id
                        ("x__..__y", [{"kind": "task", "task_id": "t1", "attempt_id": "x/../y"}]),
                        ("early", [{"kind": "gateway_call"},                    # content before the task
                                   {"kind": "task", "task_id": "t1", "attempt_id": "early"}])):
        (t / f"{name}.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    (t / "notes.txt").write_text("not a transcript", encoding="utf-8")
    store = TraceStore(tmp_path, {"t1": "train", "t2": "dev"}.__getitem__)
    assert store.sessions() == [{"session": s, "task_hash": "hash-t1"} for s in ("a/1#0", "b/2#0", "c:3#0")]


def test_events_give_each_line_with_its_reference_a_marks_fields_beside_its_kind_and_a_reply_as_response(tmp_path):
    w = Writer(tmp_path, "s/ev#0", "t1")
    call = w.call([sys_("S"), user("Go.")], "Looking.", ("c1", "api_search", {"q": "x"}))
    # a mark whose data names the line's own fields: the line's win
    mark = w.mark("package_tool", tool="plan_changes", output="Plan recorded", kind="end", mark="forged", seq=99)
    w.hook("after_tool", "ledger", "feedback", "noted")
    end = w.end("budget")
    store = TraceStore(tmp_path, {"t1": "train"}.__getitem__)
    events = store.events("s/ev#0")
    assert [(e["ref"], e["line"]) for e in events] == [(f"s/ev#0:L{n}", n) for n in range(1, end + 1)]
    m = events[mark - 1]["event"]
    assert (m["kind"], m["mark"], m["seq"], m["tool"], m["output"], m["source"]) == \
        ("mark", "package_tool", mark, "plan_changes", "Plan recorded", "package")
    g = events[call - 1]["event"]
    assert g["response"] == g["reply"] == {"text": "Looking.", "stop": "tool_use",
                                           "tool_calls": [{"id": "c1", "name": "api_search", "args": {"q": "x"}}]}
    assert store.get(f"s/ev#0:L{mark}") == events[mark - 1]
    with pytest.raises(TraceError, match="no event on that line"):
        store.get(f"s/ev#0:L{end + 1}")


# ---------------------------------------------------------------------------- synthetic: the trajectory
def test_the_trajectory_takes_each_result_from_the_first_later_call_that_carries_it(tmp_path):
    w = Writer(tmp_path, "s/one#0", "t1")
    task = [sys_("You are an agent."),
            user([{"type": "text", "text": "Find the owners."}, {"type": "text", "text": "Be brief."}])]
    calls1 = (("c1", "api_search", {"q": "owners"}), ("c2", "api_fetch", {"url": "acct/7"}))
    full = "rows " * 600 + "deepword"
    l1 = w.call(task, "Looking.", *calls1)
    w.tool("api_search", full)
    w.tool("api_fetch", "account 7")
    sent2 = task + [asst("Looking.", *calls1), results(("c1", "api_search", full), ("c2", "api_fetch", "account 7")),
                    user("Note: check the owner.")]
    l2 = w.call(sent2, "Next.", ("c3", "api_search", {"q": "more"}))
    w.tool("api_search", "more rows")
    sent3 = sent2 + [asst("Next.", ("c3", "api_search", {"q": "more"})), results(("c3", "api_search", "more rows")),
                     user("You have 2 turns left.")]
    l3 = w.call(sent3, "Done: the owner is Dana.")
    w.end()
    store = TraceStore(tmp_path, {"t1": "train"}.__getitem__)
    got = store.trajectory("s/one#0")
    assert plain(got) == [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "Find the owners.\nBe brief."},
        {"role": "assistant", "content": "Looking.", "tool_calls": [fn(*c) for c in calls1]},
        {"role": "tool", "tool_call_id": "c1", "content": full},
        {"role": "tool", "tool_call_id": "c2", "content": "account 7"},
        {"role": "user", "content": "Note: check the owner."},
        {"role": "assistant", "content": "Next.", "tool_calls": [fn("c3", "api_search", {"q": "more"})]},
        {"role": "tool", "tool_call_id": "c3", "content": "more rows"},
        {"role": "user", "content": "You have 2 turns left."},
        {"role": "assistant", "content": "Done: the owner is Dana."}]
    assert store.trajectory("s/one#0", annotations=False) == plain(got)
    assert [m["metadata"]["ref"] for m in got] == [w.ref(n) for n in (l1, l1, l1, l2, l2, l2, l2, l3, l3, l3)]
    assert [m["metadata"] for m in got if m["role"] == "tool"] == [
        {"ref": w.ref(l2), "tool": "api_search", "is_error": False, "source": "gateway_call", "truncated": False},
        {"ref": w.ref(l2), "tool": "api_fetch", "is_error": False, "source": "gateway_call", "truncated": False},
        {"ref": w.ref(l3), "tool": "api_search", "is_error": False, "source": "gateway_call", "truncated": False}]
    assert [m["metadata"] for m in got if m["role"] == "assistant"] == [
        {"ref": w.ref(l1), "call_id": "s/one#0:m1", "stop": "tool_use"},
        {"ref": w.ref(l2), "call_id": "s/one#0:m2", "stop": "tool_use"},
        {"ref": w.ref(l3), "call_id": "s/one#0:m3", "stop": "end"}]
    # calls(): each call's full messages, the prefix chain resolved
    chain = store.calls("s/one#0")
    assert [c["messages"] for c in chain] == json.loads(json.dumps([task, sent2, sent3]))
    assert [(c["ref"], c["call_id"], c["role"]) for c in chain] == \
        [(w.ref(n), f"s/one#0:m{i}", "act") for i, n in enumerate((l1, l2, l3), 1)]
    assert chain[2]["response"] == {"text": "Done: the owner is Dana.", "tool_calls": [], "stop": "end"}


BRIDGE = ("**Context Continuation**\n\nDue to context window limitations, the previous conversation has been "
          "summarized. Below is a summary of what happened before:\n\n---\n\n{summary}\n\n---\n\nContinue the task.")
SYNTHETIC_SUMMARY = "SUMMARY: the owners were searched; Dana Reyes is one."
WARNING = "You have 2 turns remaining to complete the task."


def summarized(root: Path, session: str, after: list, *, aux=("", SYNTHETIC_SUMMARY)) -> SimpleNamespace:
    """One read; then the aux calls `aux` (their reply texts: Stirrup asks its summarizer again when a reply has no
    text); then an act call whose history is the task and `after`, and the end."""
    w = Writer(root, session, "t1")
    task = [sys_("S"), user("Find the owners.")]
    w.call(task, "Searching.", ("c1", "api_search", {"q": "owners"}))
    read = w.tool("api_search", "owners: Dana Reyes")
    sent = task + [asst("Searching.", ("c1", "api_search", {"q": "owners"})),
                   results(("c1", "api_search", "owners: Dana Reyes"))]
    lines = [w.call(sent + [user("Summarize the conversation.")], text, role="aux") for text in aux]
    w.call(task + after, "Done: Dana Reyes.")
    w.end()
    return SimpleNamespace(w=w, read=read, aux=lines)


def test_after_a_replaced_history_only_the_bridge_that_carries_the_summary_and_its_acknowledgement_are_dropped(
        tmp_path):
    store = TraceStore(tmp_path, {"t1": "train"}.__getitem__)
    bridge = BRIDGE.format(summary=SYNTHETIC_SUMMARY)
    head = [{"role": "system", "content": "S"}, {"role": "user", "content": "Find the owners."},
            {"role": "assistant", "content": "Searching.", "tool_calls": [fn("c1", "api_search", {"q": "owners"})]},
            {"role": "tool", "tool_call_id": "c1", "content": "owners: Dana Reyes"}]
    done = [{"role": "assistant", "content": "Done: Dana Reyes."}]

    def dropped(*texts):
        return [{"role": "user", "chars": len(t), "sha256": hashlib.sha256(t.encode()).hexdigest()[:16],
                 "excerpt": t[:200]} for t in texts]

    def note(s):
        got = store.trajectory(s.w.session)
        assert got[3]["metadata"]["ref"] == s.w.ref(s.aux[0] if s.aux else s.read)
        return plain(got), got[-1]["metadata"].get("after_summary")
    # Stirrup's form (the summarizer asked twice, the first reply without text): bridge and acknowledgement dropped
    s = summarized(tmp_path, "s/stirrup#0", [user(bridge), user("Got it, thanks!"), user(WARNING)])
    assert note(s) == (head + [{"role": "user", "content": WARNING}] + done,
                       {"aux_refs": [s.w.ref(n) for n in s.aux], "aux_call_ids": ["s/stirrup#0:m2", "s/stirrup#0:m3"],
                        "dropped": dropped(bridge, "Got it, thanks!")})
    # R's edit removed the acknowledgement: the message after the bridge is a real one and stays
    s = summarized(tmp_path, "s/no-ack#0", [user(bridge), user(WARNING)])
    trajectory, annotation = note(s)
    assert trajectory == head + [{"role": "user", "content": WARNING}] + done
    assert annotation["dropped"] == dropped(bridge) and annotation["acknowledgement_not_found"] is True
    # a first message that does not carry the summary is not guessed to be a bridge: nothing is dropped
    s = summarized(tmp_path, "s/no-bridge#0", [user("Continue from where you left off."), user("Got it, thanks!")])
    trajectory, annotation = note(s)
    assert trajectory == head + [{"role": "user", "content": "Continue from where you left off."},
                                 {"role": "user", "content": "Got it, thanks!"}] + done
    assert annotation["dropped"] == [] and annotation["bridge_not_found"] is True
    # a history replaced without a summary (the agent dropped old turns): its new messages are the trajectory's, and
    # the read no later call carried comes from its tool line
    s = summarized(tmp_path, "s/dropped#0", [user("Earlier turns were dropped."), user(WARNING)], aux=())
    trajectory, annotation = note(s)
    assert annotation is None
    assert trajectory == head + [{"role": "user", "content": "Earlier turns were dropped."},
                                 {"role": "user", "content": WARNING}] + done
    assert store.trajectory("s/dropped#0")[3]["metadata"]["source"] == "tool_event"


def test_a_turns_own_lines_answer_the_calls_no_later_call_carries_in_order_and_never_with_a_read_not_the_models(
        tmp_path):
    w = Writer(tmp_path, "s/turn#0", "t1")
    calls = (("c1", "plan_changes", PLAN), ("c2", "api_search", {"q": "a"}), ("c3", "api_search", {"q": "b"}),
             ("c4", "api_fetch", {"url": "x", "method": "POST"}), ("c5", "finish", {"reason": "Done."}),
             ("c6", "api_fetch", {"url": "y"}))
    w.call([sys_("S"), user("Go.")], "Working.", *calls)
    w.tool("api_search", "the plan tool's own read")                 # a package tool's base call, before its mark
    plan = w.mark("package_tool", tool="plan_changes", is_error=False, output="Plan recorded: 1 change",
                  broker_calls=[{"tool": "api_search", "is_error": False}])
    w.tool("api_search", "the hook's read", read_only=True)           # a before_tool hook's read for c2
    long = "z" * 3000
    c2 = w.tool("api_search", long)
    c3 = w.tool("api_search", "the second read failed", is_error=True, broker=False)
    w.hook("before_tool", "triage", "block", "plan first")
    c4 = w.mark("tool_blocked", tool="api_fetch", call_id="c4", turn=1, reason="Plan your changes first.")
    c5 = w.mark("tool_skipped", tool="finish", call_id="c5", turn=1)
    w.end()
    got = TraceStore(tmp_path, {"t1": "train"}.__getitem__).trajectory("s/turn#0")
    reply = got[2]
    assert len(reply["tool_calls"]) == 6 and reply["metadata"]["unanswered"] == ["c6"]

    def meta(line, tool, source, is_error=False, truncated=False):
        return {"ref": w.ref(line), "tool": tool, "is_error": is_error, "source": source, "truncated": truncated}
    answers = {m["tool_call_id"]: (m["content"], m["metadata"]) for m in got if m["role"] == "tool"}
    assert list(answers) == ["c1", "c2", "c3", "c4", "c5"]
    assert answers == {
        "c1": ("Plan recorded: 1 change", meta(plan, "plan_changes", "package_tool")),
        "c2": (long[:EVENT_CHARS], meta(c2, "api_search", "tool_event", truncated=True)),
        "c3": ("the second read failed", meta(c3, "api_search", "tool_event", is_error=True)),
        "c4": ("Stopped before execution: Plan your changes first.", meta(c4, "api_fetch", "tool_blocked", True, True)),
        "c5": ("Not executed: an earlier call in this turn was stopped",
               meta(c5, "finish", "tool_skipped", True, True))}


def test_an_excerpt_is_marked_truncated_by_what_the_broker_measured_else_by_its_length(tmp_path):
    w = Writer(tmp_path, "s/cut#0", "t1")
    calls = (("d1", "api_search", {}), ("d2", "api_search", {}), ("d3", "api_search", {}), ("d4", "plan_changes", {}),
             ("d5", "plan_changes", {}))
    w.call([sys_("S"), user("Go.")], "", *calls)
    exact = "e" * EVENT_CHARS
    w.tool("api_search", exact)                                       # the broker measured exactly this much
    w.tool("api_search", exact, broker=False)                         # no measure: as long as an excerpt gets
    w.tool("api_search", "short", broker=False)
    w.mark("package_tool", tool="plan_changes", is_error=True, output="p" * (EVENT_CHARS - 1))
    w.mark("package_tool", tool="plan_changes", is_error=False, output="q" * EVENT_CHARS)
    w.end()
    got = TraceStore(tmp_path, {"t1": "train"}.__getitem__).trajectory("s/cut#0")
    assert [(m["tool_call_id"], m["metadata"]["truncated"], m["metadata"]["is_error"]) for m in got
            if m["role"] == "tool"] == [("d1", False, False), ("d2", True, False), ("d3", False, False),
                                        ("d4", False, True), ("d5", True, False)]


def test_finish_takes_the_last_finish_reason_the_last_act_text_and_the_shown_ids_in_order(tmp_path):
    w = Writer(tmp_path, "s/fin#0", "t1")
    w.retrieval("start", ["e1-full", "e2-full"])
    task = [sys_("S"), user("Go.")]
    w.call(task, "Looking at the notes.", ("c1", "recall", {"query": "ops"}))
    w.retrieval("recall", ["e2-full", "e3", ""])
    s2 = task + [asst("Looking at the notes.", ("c1", "recall", {"query": "ops"})), results(("c1", "recall", "n"))]
    w.call(s2, "Checked again.", ("c2", "finish", {"reason": "first"}))
    s3 = s2 + [asst("Checked again.", ("c2", "finish", {"reason": "first"})),
               results(("c2", "finish", "Keep working on the task"))]
    w.call(s3, "", ("c3", "finish", {"reason": "second\nlibrary-used: e1"}))
    w.call(s3 + [user("Summarize.")], "SUMMARY of it all", role="aux")
    w.retrieval("recall", ["e3", "e4"])
    w.end("budget")
    store = TraceStore(tmp_path, {"t1": "train", "t2": "train"}.__getitem__)
    assert store.finish("s/fin#0") == {"reason": "second\nlibrary-used: e1", "text": "Checked again.",
                                       "termination": "budget", "shown": ["e1-full", "e2-full", "e3", "e4"]}
    # no finish call, no end line yet; no model call at all
    w = Writer(tmp_path, "s/text#0", "t2")
    w.call(task, "Just text.")
    assert store.finish("s/text#0") == {"reason": None, "text": "Just text.", "termination": None, "shown": []}
    w = Writer(tmp_path, "s/none#0", "t2")
    w.end("infrastructure")
    assert store.finish("s/none#0") == {"reason": None, "text": "", "termination": "infrastructure", "shown": []}
    with pytest.raises(TraceError, match="no gateway_call records"):
        store.trajectory("s/none#0")


def test_a_broken_record_is_an_error_not_a_guess(tmp_path):
    w = Writer(tmp_path, "s/chain#0", "t1")
    w.call([sys_("S"), user("Go.")], "One.")
    w.t.write("gateway_call", call_id="x", role="act", messages_prefix=5, messages_tail=[], reply={"text": "Two."})
    store = TraceStore(tmp_path, {"t1": "train"}.__getitem__)
    for read in (lambda: store.trajectory("s/chain#0"), lambda: store.calls("s/chain#0")):
        with pytest.raises(TraceError, match="messages_prefix 5 is longer than the previous call's 2 messages"):
            read()
    w = Writer(tmp_path, "s/garbled#0", "t1")
    w.call([sys_("S"), user("Go.")], "One.")
    w.raw("{not json\n")
    for read in (lambda: store.events("s/garbled#0"), lambda: store.trajectory("s/garbled#0"),
                 lambda: store.finish("s/garbled#0")):
        with pytest.raises(TraceError, match=r"__garbled#0\.jsonl:L3 is not a JSON line"):
            read()


# ---------------------------------------------------------------------------- synthetic: the index
def test_the_index_holds_what_the_actor_saw_and_did_and_an_excerpt_only_once_the_attempt_ended(tmp_path):
    w = Writer(tmp_path, "s/idx#0", "t1")
    task = [sys_("S"), user("Go.")]
    c1 = w.call(task, "Searching the quarterly ledger.", ("c1", "api_search", {"q": "quarterly"}))
    w.tool("api_search", "ledger rows with tangerine")
    hook = w.hook("after_tool", "ledger", "feedback", "noted the frobnicate write")
    mark = w.mark("budget_note", tool="api_search", info="glimmerstone")
    egress = w.egress(method="GET", host="api.vendor.test", port=443, status=200, bytes=10, decision="allowed")
    s2 = task + [asst("Searching the quarterly ledger.", ("c1", "api_search", {"q": "quarterly"})),
                 results(("c1", "api_search", "ledger rows with tangerine"))]
    c2 = w.call(s2, "Fetching.", ("c2", "api_fetch", {"url": "acct/7"}))
    fetched = w.tool("api_fetch", "fetched pomegranate")
    store = TraceStore(tmp_path, {"t1": "train"}.__getitem__)

    def one(query, **kw):
        (hit,) = store.search(query, **kw)
        assert hit["session"] == "s/idx#0" and hit["task_hash"] == "hash-t1"
        return hit["ref"], hit["kind"], hit["tool"]
    assert one("tangerine") == (w.ref(c2), "tool_result", "api_search")      # from the call that carried it in full
    assert one("quarterly", kind="assistant") == (w.ref(c1), "assistant", None)
    assert one("quarterly", kind="tool_call") == (w.ref(c1), "tool_call", "api_search")
    assert one("frobnicate") == (w.ref(hook), "hook", None)
    assert one("glimmerstone") == (w.ref(mark), "mark", "api_search")
    assert one("vendor") == (w.ref(egress), "egress", None)
    assert store.search("pomegranate") == []           # the last turn's result, while a later call may still carry it
    w.end()
    assert one("pomegranate") == (w.ref(fetched), "tool_result", "api_fetch")
    assert store.index() == NOTHING and len(store.search("pomegranate")) == 1
    assert len(store.search("ledger")) >= 2
    assert len(store.search("ledger", limit=1)) == 1 and len(store.search("ledger", limit=0)) == 1
    # the frozen split decides at every search: what the index holds of a session that is not train is not returned
    assert TraceStore(tmp_path, {"t1": "dev"}.__getitem__).search("tangerine") == []
    assert indexed(tmp_path, "SELECT count(*) FROM docs WHERE docs MATCH 'tangerine'") == [(1,)]


def test_an_attempt_run_again_under_its_id_reads_as_its_last_run(tmp_path):
    """A run that died without a result is run again under the same id, appending to the same file: the trajectory,
    the events and the calls are the last run's (from the last task line), never the two spliced; references keep
    the file's line numbers."""
    w = Writer(tmp_path, "s/again#0", "t1")
    w.call([sys_("S"), user("Go.")], "First try.", ("c1", "api_search", {"q": "x"}))
    w.tool("api_search", '{"hits": 1}')                              # ... and the process died here
    first = w.line
    w.t.write("task", task_id="t1", split="train", task_hash="hash-t1", attempt_id="s/again#0", text="the task t1")
    w.previous = []
    second = w.call([sys_("S"), user("Go.")], "Second try.")
    w.end()
    store = TraceStore(tmp_path, {"t1": "train"}.__getitem__)
    events = store.events("s/again#0")
    assert events[0]["line"] == first + 1 and events[0]["event"]["kind"] == "task"
    assert [c["ref"] for c in store.calls("s/again#0")] == [f"s/again#0:L{second}"]
    texts = [m.get("content") for m in store.trajectory("s/again#0") if m.get("role") == "assistant"]
    assert texts == ["Second try."] and "First try." not in json.dumps(store.trajectory("s/again#0"))
    # the search index forgets the dead run too
    assert store.search("First try") == [] and [h["ref"] for h in store.search("Second try")] == \
        [f"s/again#0:L{second}"]
