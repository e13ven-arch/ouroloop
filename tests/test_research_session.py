"""M4 §1 (v0.3) R as a bounded coding session (docs/modules/04_RESEARCH_LOOP.md §1 R 作为有界编码会话, §6 RL6-28/29/36/40).

The session is driven by a scripted research step that only ever answers with tool calls, and its files live in a
ProcessWorker's tree, so what is checked here is the session's rules — its caps, its declaration check, what it can
and cannot reach — and not any model's judgement.
"""
import json

import pytest
import rsi

from ouroloop import bench, policies as P
from ouroloop.agent import harness_package as hp
from ouroloop.agent.worker import ProcessWorker
from ouroloop.chat import ChatTurn, ToolCall
from ouroloop.evolve import contracts as ct
from ouroloop.evolve import experience as xp
from ouroloop.research.session import (SESSION_FORMAT, TOOLS, CodingSession, SessionLimits, TrajectoryReader,
                                       WorkTree, declaration_mismatch, diff_of, split_candidate)

SKILL = "check-before-finishing"
MEMORY = f"Before you finish, {rsi.FINISH_CHECK_MARK}.\n"


def v3_fixture(monkeypatch, *, components=("memory", "skills", "on_finish"), screen=None, harness=None, **gate):
    """The v3 preset on the rsi fixture: text and hook kinds, no image (the worker is a ProcessWorker named for
    tests), the gate's least task count the fixture's three dev tasks, costs measured through a placeholder price.
    `screen` overrides the screen's numbers (a campaign test lowers min_net_flips: the fixture's one gap family is
    the outer pool, which an arm's screen may not run); `harness` other harness values (K's package caps)."""
    preset = json.loads(json.dumps(bench.PRESETS["automationbench_harness_v3"]))
    preset["domain"] = rsi.DOMAIN
    preset["harness"].update(components=list(components), sandbox=None, **(harness or {}))
    preset["harness"]["screen"].update(screen or {})
    preset["task_plan"]["gate"].update({"min_groups": 1, "bootstrap_replicates": 200, "min_tasks": len(rsi.DEV), **gate})
    preset["schedule"].update(worker_rounds=1)
    preset["campaign"].update(worker_round_limit=1, min_groups=1, min_headroom_tasks=1)
    monkeypatch.setitem(bench.PRESETS, "v3_test", preset)
    monkeypatch.setattr(bench, "isolated_worker", lambda settings: ProcessWorker(for_tests=True) if settings else None)
    return preset


def prepared_v3(fake_upstream, tmp_path, monkeypatch, *, rounds: int = 1, k: int | None = None, screen=None,
                llm: dict | None = None, harness=None, **gate):
    """A v3 activity from W0 with the fixture's gap, its plans frozen as the cycle would freeze them, the pricing a
    placeholder that bounds every call: -> (cfg, activity, W0 release ref). `llm` replaces the fixture actor's
    table before the bootstrap pins it."""
    from test_cycle import cycle_config, gap_w0
    preset = v3_fixture(monkeypatch, screen=screen, harness=harness, **gate)
    preset["schedule"].update(worker_rounds=rounds, patience=max(rounds, 2))
    cfg = cycle_config(fake_upstream, tmp_path)
    del cfg["bench"]["budget"]
    cfg["improvement"]["preset"] = "v3_test"
    if llm is not None:
        cfg["llm"] = dict(llm)
    if k is not None:
        cfg["improvement"]["max_candidates_per_round"] = k
    cfg["pricing"] = {"input_per_1k": 0.001, "output_per_1k": 0.002, "max_input_tokens": 100000,
                      "max_output_tokens": 32768, "source": "test"}
    cfg["freeze"]["budget"] = {"model_calls": 4000, "tool_calls": 4000, "usd": 50}
    activity, _ = gap_w0(cfg, tmp_path)
    refs = bench.freeze(cfg, ".", out=lambda _line: None)
    cfg = {**cfg, "rsi": {**cfg["rsi"], **refs}}
    return cfg, activity, activity.releases.active()["release_ref"]


def memory_candidate(inputs, *, name="m1", text=MEMORY, mechanism="check every step before finishing"):
    manifest = json.loads(inputs_package(inputs).get("manifest.json") or json.dumps(hp.empty_manifest()))
    manifest["memory"] = {"enabled": True}
    return {"dir": name, "files": {"manifest.json": json.dumps(manifest), "memory.md": text},
            "declaration": {"mechanism": mechanism, "site": "memory.md", "carrier": "memory"}}


def inputs_package(inputs) -> dict:
    """The scripted step cannot read the tree without tool calls; the fixture's W0 package is the empty one."""
    return {"manifest.json": json.dumps(hp.empty_manifest(), sort_keys=True, indent=1)}


# ---------------------------------------------------------------------------- the session on its own
class Turns:
    """A session model scripted as a list of turns; each turn is a list of ToolCall."""

    def __init__(self, turns):
        self.turns, self.n, self.seen = list(turns), 0, []

    def __call__(self, system, messages, tools):
        self.seen.append((system, [dict(m) for m in messages], [t.name for t in tools]))
        calls = self.turns[self.n] if self.n < len(self.turns) else []
        self.n += 1
        return ChatTurn("", calls, "tool_use" if calls else "end")


def tree_with(package=None, inputs=None):
    worker = ProcessWorker(for_tests=True)
    session = worker.start()
    session.load({})
    tree = WorkTree(session)
    tree.init(package or {"manifest.json": json.dumps(hp.empty_manifest()), "memory.md": "old\n"},
              inputs or {"notes.json": "[]"})
    return session, tree


def write(path, text, i=0):
    return ToolCall(f"w{i}", "write_file", {"path": path, "text": text})


def finish(*candidates, **extra):
    return ToolCall("f", "finish", {"candidates": list(candidates), **extra})


def declared(d="c1", mechanism="check every step before finishing", site="memory.md", carrier="memory", **more):
    return {"dir": d, "mechanism": mechanism, "site": site, "carrier": carrier, "predicted_affected": ["rsi.notify"],
            "trigger_condition": "always", "notes": ["note:a"], **more}


def test_a_session_stops_at_its_turn_and_edit_caps():
    """RL6-29: a session that reaches `max_turns` model calls or `max_edits` edits without `finish` ends there and
    decides nothing — the round is then invalid — and every call up to the cap is on the host's record."""
    session, tree = tree_with()
    try:
        turns = Turns([[write("candidates/c1/memory.md", "a")], [write("candidates/c1/memory.md", "b")],
                       [write("candidates/c1/memory.md", "c")], [finish(declared())]])
        s = CodingSession(turn=turns, work=tree, limits=SessionLimits(max_turns=3, max_edits=24), k=3,
                          system="s", inputs={"k": 3}, note_refs=["note:a"])
        out = s.start()
        assert out.unreadable == "the session used its 3 model calls without finishing" and s.turns == 3
        assert [c["tool"] for c in s.calls] == ["write_file"] * 3 and s.record()["ended"]
    finally:
        session.close()
    session, tree = tree_with()
    try:
        turns = Turns([[write("candidates/c1/memory.md", "a", 0), write("candidates/c1/skills/x.md", "b", 1)],
                       [write("candidates/c1/memory.md", "c")], [finish(declared())]])
        s = CodingSession(turn=turns, work=tree, limits=SessionLimits(max_turns=40, max_edits=2), k=3,
                          system="s", inputs={}, note_refs=["note:a"])
        out = s.start()
        assert out.unreadable == "the session reached its 2 edits without finishing" and s.edits == 2
        # a revision after the end is refused with the same reason, never a fresh session
        assert s.revise("fix it").unreadable == out.unreadable
    finally:
        session.close()


def test_the_declared_carrier_and_site_must_match_the_diff():
    """RL6-28 / M4 §1: the declaration is checked against the candidate's actual diff by fixed code — carrier=hook
    with only memory changed, carrier=memory with another site, a candidate that changes nothing, carrier=prompt
    without the worker/ file — each is `declaration_mismatch`; a matching one passes."""
    parent = {"manifest.json": json.dumps(hp.empty_manifest()), "memory.md": "old\n", "worker/finish.md": "report"}
    only_memory = {**parent, "memory.md": "new\n"}
    assert declaration_mismatch(declared(carrier="hook", site="on_finish"), parent, only_memory)
    assert declaration_mismatch(declared(carrier="memory", site="skills/a.md"), parent, only_memory)
    assert declaration_mismatch(declared(), parent, dict(parent)) == "the candidate changes nothing"
    assert declaration_mismatch(declared(carrier="prompt", site="finish"), parent, only_memory)
    assert declaration_mismatch(declared(), parent, only_memory) == ""
    assert declaration_mismatch(declared(carrier="prompt", site="finish"), parent,
                                {**parent, "worker/finish.md": "check first"}) == ""
    manifest = {**hp.empty_manifest(), "hooks": [{"name": "h", "site": "on_finish",
                                                 "entrypoint": "middlewares/h.py:run"}]}
    hooked = {**parent, "manifest.json": json.dumps(manifest), "middlewares/h.py": "def run(ctx, e): return {}"}
    assert declaration_mismatch(declared(carrier="hook", site="on_finish"), parent, hooked) == ""
    assert declaration_mismatch(declared(carrier="hook", site="before_tool"), parent, hooked)
    assert declaration_mismatch(declared(carrier="agent", site="verify"), parent, hooked)
    package, prompts = split_candidate(hooked)
    assert "worker/finish.md" not in package and prompts == {"worker.finish": "report"}
    assert diff_of(parent, hooked)["added"] == ["middlewares/h.py"] and diff_of(parent, hooked)["modified"] == ["manifest.json"]


def test_a_session_cannot_write_outside_its_candidate_dirs():
    """RL6-40: a write to package/, inputs/ or outside the tree is refused by the worker and handed to R as the
    tool's error; the session goes on, nothing of the host changes, and the ledger has no entry a session wrote."""
    session, tree = tree_with()
    try:
        bad = ["package/memory.md", "inputs/notes.json", "../etc/passwd", "/etc/passwd", "candidates/../x.md",
               "candidates/c1", "ledger.json"]
        turns = Turns([[write(p, "x", i) for i, p in enumerate(bad)], [finish(declared())]])
        s = CodingSession(turn=turns, work=tree, limits=SessionLimits(), k=3, system="s", inputs={}, note_refs=["note:a"])
        out = s.start()
        results = [m for m in s.messages if m.get("role") == "tool"][0]["results"]
        assert all(r["is_error"] for r in results) and len(results) == len(bad)
        assert s.edits == len(bad) and s.dirs == []
        # the finish names a dir that was never written: refused as the tool's error, and the session then ends
        # at its turn cap without a declaration
        last = [m for m in s.messages if m.get("role") == "tool"][-1]["results"][0]
        assert last["is_error"] and "is not a candidate directory you wrote" in last["output"]
        assert out.unreadable and not s.declarations
        assert tree.do("read", path="package/memory.md") == ("old\n", False)
        assert tree.do("read", path="inputs/notes.json") == ("[]", False)
    finally:
        session.close()


def test_a_revision_continues_the_same_session():
    """M4 §1: a check output enters the session as a new message and R edits the same candidate directory under the
    same caps; the second finish is a new declaration of the same dir, and no second session was opened."""
    session, tree = tree_with()
    try:
        turns = Turns([[write("candidates/c1/memory.md", "a")], [finish(declared())],
                       [write("candidates/c1/memory.md", "b")], [finish(declared(mechanism="check twice"))]])
        s = CodingSession(turn=turns, work=tree, limits=SessionLimits(max_turns=4, max_edits=2), k=3,
                          system="s", inputs={}, note_refs=["note:a"])
        first = s.start()
        assert first.candidates[0]["mechanism"] == "check every step before finishing"
        second = s.revise("candidates/c1 failed its draft check: ...")
        assert second.candidates[0]["mechanism"] == "check twice" and s.turns == 4 and s.edits == 2
        assert tree.export("c1")["memory.md"] == "b"
        assert turns.seen[2][1][-1] == {"role": "user", "content": "candidates/c1 failed its draft check: ..."}
        assert len(s.declarations) == 2 and s.record()["finishes"] == 2
    finally:
        session.close()


def test_a_session_sees_no_scorer_dev_or_final(fake_upstream, tmp_path):
    """M4 §1: what the session can reach is the tree and the tools; its tool list has no shell and no network, the
    trajectory tool refuses a dev attempt, and the inputs name no dev or final task."""
    assert [t.name for t in TOOLS] == ["list_files", "read_file", "write_file", "edit_file", "delete_file",
                                       "read_trajectory", "finish"]
    assert "there is no shell" in SESSION_FORMAT and "nothing else of dev" in SESSION_FORMAT
    from test_diagnosis import run
    run(fake_upstream, tmp_path, [rsi.TRAIN[1]], "train")
    results, _, _ = run(fake_upstream, tmp_path, [rsi.DEV[0]], "dev")
    rows = {r.split: r for r in results.all()}
    reader = TrajectoryReader(results, tmp_path / "transcripts", SessionLimits(max_trajectory_bytes=64,
                                                                               max_trajectory_bytes_total=100))
    text, bad = reader.read(rows["dev"].attempt_id)
    assert bad and text.startswith("AccessDenied") and reader.calls[-1]["denied"]
    text, bad = reader.read(rows["train"].attempt_id, 0, 1000)
    assert not bad and len(text.encode()) <= 64 + 40 and reader.calls[-1]["bytes"] == 64
    text, bad = reader.read(rows["train"].attempt_id, 64, 64)
    assert bad and "cumulative cap" in text          # 64 + 64 > 100
    assert reader.total == 64 and len(reader.calls) == 3
    listing = reader.listing([rows["dev"].attempt_id, rows["train"].attempt_id])
    assert [e["task_id"] for e in listing] == [rows["train"].task_id]


# ---------------------------------------------------------------------------- the round through the session
def test_a_memory_candidate_from_the_session_is_drafted_screened_gated_and_installed(fake_upstream, tmp_path,
                                                                                     monkeypatch):
    """The whole v0.3 round (M4 §4 一轮执行 v0.3 block): notes -> one session in the worker -> finish with one
    declared memory candidate -> the diff matches the declaration -> draft check -> admission -> train-side screen on
    the notes' tasks plus the passing sample -> the dev gate -> install; the ledger has the candidate at every stage
    and the research_steps event shows the session ran under research.propose's text."""
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    step = rsi.SessionScript(lambda inputs: [memory_candidate(inputs)])
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    outcomes = [r["outcome"] for r in result["rounds"]]
    assert "installed" in outcomes, result["rounds"]
    bound = P.bind_release(result["release"], profile="task", services=P.Services(activity.objects), authorization="x")
    assert rsi.FINISH_CHECK_MARK in bound.package.memory
    rows = xp.ledger_rows(xp.read(activity.root))
    assert len(rows) == 1 and rows[0]["outcome"] == "installed"
    entry = rows[0]
    assert entry["declaration"]["carrier"] == "memory" and entry["declaration"]["site"] == "memory.md"
    assert entry["drafts"] == [{"revision": 0, "package_ref": entry["package_ref"], "ok": True}]
    assert entry["admission"]["status"] == "admitted"
    screen = entry["screen"]
    assert screen["outcome"] == "selected" and screen["net_flips"] >= 1
    assert set(screen["failing"]) <= set(rsi.TRAIN) and set(screen["sample"]) <= set(rsi.TRAIN)
    assert all(not screen["before"][t] for t in screen["failing"]) and all(screen["after"][t] for t in screen["failing"])
    gate = entry["gate"]
    assert gate["verdict"] == "accepted" and gate["reading_kind"] == "confirmed" and gate["receipt"]
    assert gate["fixed"] == [rsi.DEV[0]] and gate["regressed"] == [] and gate["still_failing"] == []
    steps = [e for e in ct.Events(activity.root).of("research_steps")]
    assert steps and {s["step"] for s in steps[-1]["steps"]} >= {"note", "session"}
    sessions = list(ct.Events(activity.root).of("research_session"))
    assert len(sessions) == 1 and sessions[0]["finishes"] == 1 and sessions[0]["edits"] == 2
    assert any(r["step"] == "session" and SESSION_FORMAT[:40] in r["system"] for r in step.requests)


# ---------------------------------------------------------------------------- review of 2026-10-05: the session's small rules
def test_tool_arguments_that_are_not_an_object_are_the_tools_error_and_count_as_a_call():
    """M4 §1 (2026-10-05): a tool call whose arguments are not a JSON object is answered as that tool's error and
    counted like any call; nothing is executed and the session goes on."""
    session, tree = tree_with()
    try:
        turns = Turns([[ToolCall("x", "read_file", ["package/memory.md"]), ToolCall("y", "finish", ["c1"])],
                       [write("candidates/c1/memory.md", "a")], [finish(declared())]])
        s = CodingSession(turn=turns, work=tree, limits=SessionLimits(), k=3, system="s", inputs={}, note_refs=["note:a"])
        out = s.start()
        first = [m for m in s.messages if m.get("role") == "tool"][0]["results"]
        assert [r["is_error"] for r in first] == [True, True] and "JSON object, not list" in first[0]["output"]
        assert [c["tool"] for c in s.calls[:2]] == ["read_file", "finish"] and not s.calls[0]["ok"]
        assert out.candidates[0]["dir"] == "c1" and s.edits == 1
    finally:
        session.close()


def test_calls_after_an_accepted_finish_are_not_performed_and_the_declaration_stands():
    """M4 §1 (2026-10-05): once a reply's `finish` is accepted, the reply's remaining calls are answered as not
    performed and not executed — an edit among them would otherwise have crossed max_edits and ended the session
    after the declaration was already accepted."""
    session, tree = tree_with()
    try:
        turns = Turns([[write("candidates/c1/memory.md", "a")],
                       [finish(declared()), write("candidates/c1/memory.md", "b", 1), write("candidates/c1/x.md", "c", 2)]])
        s = CodingSession(turn=turns, work=tree, limits=SessionLimits(max_edits=1), k=3, system="s", inputs={},
                          note_refs=["note:a"])
        out = s.start()
        assert out.candidates and not out.unreadable and s.edits == 1
        results = [m for m in s.messages if m.get("role") == "tool"][-1]["results"]
        assert results[0]["output"] == "declared" and all(r["output"] == "not performed: the session finished"
                                                          and r["is_error"] for r in results[1:])
        assert tree.export("c1")["memory.md"] == "a"
        assert [c["error"] for c in s.calls if c.get("error")] == ["not performed", "not performed"]
    finally:
        session.close()


def test_read_trajectory_guards_overflow_and_records_the_readers_byte_count(fake_upstream, tmp_path):
    """M4 §1 (2026-10-05): an offset or limit that is not an integer, an overflowing one included, is the call's
    error; the session record's per-call `bytes` is the reader's count of record bytes handed over."""
    from test_diagnosis import run
    results, _, _ = run(fake_upstream, tmp_path, [rsi.TRAIN[1]], "train")
    row = next(iter(results.all()))
    reader = TrajectoryReader(results, tmp_path / "transcripts", SessionLimits(max_trajectory_bytes=50))
    # int() of an infinite float raises OverflowError, of a nan ValueError: both are the call's error, not a crash
    assert reader.read(row.attempt_id, float("inf"), 10)[1] and reader.read(row.attempt_id, 0, float("inf"))[1]
    assert reader.read(row.attempt_id, float("nan"), 10)[1] and reader.read(row.attempt_id, "x", 10)[1]
    session, tree = tree_with()
    try:
        turns = Turns([[ToolCall("r", "read_trajectory", {"attempt_ref": row.attempt_id, "limit": 20})],
                       [write("candidates/c1/memory.md", "a")], [finish(declared())]])
        s = CodingSession(turn=turns, work=tree, limits=SessionLimits(max_trajectory_bytes=50), k=3, system="s",
                          inputs={}, trajectories=reader, note_refs=["note:a"])
        s.start()
        call = next(c for c in s.calls if c["tool"] == "read_trajectory")
        assert call["bytes"] == 20 == reader.calls[-1]["bytes"]
    finally:
        session.close()


def test_a_crlf_file_the_session_never_touched_is_not_a_changed_file():
    """M4 §1 (2026-10-05): the tree reads and writes with newline="" so a CRLF file round-trips as it is; the diff
    of a candidate against the package shows only what R changed."""
    package = {"manifest.json": json.dumps(hp.empty_manifest()), "skills/a.md": "x\r\ny\r\n"}
    session, tree = tree_with(package=package)
    try:
        assert tree.do("write", path="candidates/c1/memory.md", text="m\r\n") == ({"written": 3, "seeded": True}, False)
        exported = tree.export("c1")
        assert exported["skills/a.md"] == "x\r\ny\r\n" and exported["memory.md"] == "m\r\n"
        assert diff_of(package, exported) == {"added": ["memory.md"], "modified": [], "deleted": [], "bytes": 3}
        assert tree.do("read", path="candidates/c1/skills/a.md") == ("x\r\ny\r\n", False)
    finally:
        session.close()


def test_a_declaration_with_blank_or_non_text_fields_is_refused():
    """M4 §1 (2026-10-05): a whitespace-only mechanism, a site that is not text or a blank trigger_condition is a
    declaration the session refuses as the finish call's error."""
    session, tree = tree_with()
    try:
        turns = Turns([[write("candidates/c1/memory.md", "a")],
                       [finish(declared(mechanism="   "))], [finish(declared(site={"path": "memory.md"}))],
                       [finish(declared(trigger_condition="  "))], [finish(declared())]])
        s = CodingSession(turn=turns, work=tree, limits=SessionLimits(), k=3, system="s", inputs={}, note_refs=["note:a"])
        out = s.start()
        errors = [m["results"][0]["output"] for m in s.messages if m.get("role") == "tool"][1:4]
        assert all("must be non-blank text" in e for e in errors) and out.candidates and s.turns == 5
    finally:
        session.close()


# ---------------------------------------------------------------------------- recheck of 2026-10-05: the session's edges
class Replies:
    """A session model scripted as whole replies (ChatTurn), for calls a provider or a hand-written text produces."""

    def __init__(self, replies):
        self.replies, self.n = list(replies), 0

    def __call__(self, system, messages, tools):
        reply = self.replies[self.n] if self.n < len(self.replies) else ChatTurn("", [], "end")
        self.n += 1
        return reply


def test_a_call_that_is_not_strict_utf8_is_the_tools_error_and_every_record_of_it_encodes():
    """M4 §1 (2026-10-05 recheck): a call whose arguments hold a lone surrogate — a write's path, a finish's
    mechanism — is not performed; it is the tool's error, the session's call row carries only a backslash-escaped
    tool name and the error (no raw path), the assistant message keeps an escaped copy, and the session record
    encodes as strict UTF-8, so the round's research_session event can be written; the session then finishes."""
    session, tree = tree_with()
    try:
        turns = Turns([[write("candidates/c1/\udc80.md", "x"), finish(declared(mechanism="check \ud800 first"))],
                       [write("candidates/c1/memory.md", "a")], [finish(declared())]])
        s = CodingSession(turn=turns, work=tree, limits=SessionLimits(), k=3, system="s", inputs={}, note_refs=["note:a"])
        out = s.start()
        first = [m for m in s.messages if m.get("role") == "tool"][0]["results"]
        assert all(r["is_error"] and "not strict UTF-8" in r["output"] for r in first)
        assert s.calls[:2] == [{"turn": 1, "tool": "write_file", "ok": False, "error": "not strict UTF-8"},
                               {"turn": 1, "tool": "finish", "ok": False, "error": "not strict UTF-8"}]
        assistant = [m for m in s.messages if m.get("role") == "assistant"][0]
        assert assistant["tool_calls"][0].args["path"] == "candidates/c1/\\udc80.md"
        json.dumps(s.record(), ensure_ascii=False).encode("utf-8")
        json.dumps(s.messages, ensure_ascii=False, default=str).encode("utf-8")
        assert out.candidates[0]["dir"] == "c1" and s.edits == 1 and s.dirs == ["c1"]
        assert tree.do("list", path="candidates/c1")[0] == ["manifest.json", "memory.md"]
    finally:
        session.close()


def test_written_calls_hand_their_arguments_to_the_tool_unchanged():
    """M4 §1 (2026-10-05 recheck): a call written in the text — {"tool", "args"} or {"tool_calls": [...]} — and a
    provider's structured call pass `args` through as written; arguments that are a string are the tool's error (a
    finish with args "c1x" is a recorded finish call that failed), never an exception out of the session."""
    session, tree = tree_with()
    try:
        replies = Replies([ChatTurn('{"tool": "read_file", "args": "package/memory.md"}', [], "end"),
                           ChatTurn('{"tool_calls": [{"name": "finish", "args": "c1x"}]}', [], "end"),
                           ChatTurn("", [ToolCall("p", "finish", "c1x")], "tool_use"),
                           ChatTurn("", [write("candidates/c1/memory.md", "a")], "tool_use"),
                           ChatTurn("", [finish(declared())], "tool_use")])
        s = CodingSession(turn=replies, work=tree, limits=SessionLimits(), k=3, system="s", inputs={},
                          note_refs=["note:a"])
        out = s.start()
        outputs = [m["results"][0]["output"] for m in s.messages if m.get("role") == "tool"][:3]
        assert "JSON object, not str" in outputs[0] and all("JSON object, not str" in o for o in outputs[1:])
        assert [(c["tool"], c["ok"]) for c in s.calls[:3]] == [("read_file", False), ("finish", False), ("finish", False)]
        assert out.candidates[0]["dir"] == "c1" and s.turns == 5
    finally:
        session.close()


def test_a_finish_that_declares_one_dir_twice_is_refused_and_may_be_sent_again():
    """M4 §1 (2026-10-05 recheck): one declaration per candidates/<dir> in a finish; a second one for the same dir is
    the finish call's error, and R finishes again within its caps."""
    assert "one declaration per candidates/<dir>" in SESSION_FORMAT
    session, tree = tree_with()
    try:
        turns = Turns([[write("candidates/c1/memory.md", "a")],
                       [finish(declared(), declared(mechanism="a second reading of the request"))],
                       [finish(declared())]])
        s = CodingSession(turn=turns, work=tree, limits=SessionLimits(), k=3, system="s", inputs={}, note_refs=["note:a"])
        out = s.start()
        refused = [m for m in s.messages if m.get("role") == "tool"][1]["results"][0]
        assert refused["is_error"] and "'c1' is declared twice" in refused["output"]
        assert [c["dir"] for c in out.candidates] == ["c1"] and len(s.declarations) == 1
    finally:
        session.close()


def test_a_malformed_trajectory_read_is_on_the_record_with_no_bytes(fake_upstream, tmp_path):
    """M4 §1 (2026-10-05 recheck): a read_trajectory whose offset is not an integer is recorded by the reader (0
    bytes, the reason) before it answers, so the session record's per-call bytes is 0 for it and trajectory_reads
    counts it beside the good read."""
    from test_diagnosis import run
    results, _, _ = run(fake_upstream, tmp_path, [rsi.TRAIN[1]], "train")
    row = next(iter(results.all()))
    reader = TrajectoryReader(results, tmp_path / "transcripts", SessionLimits())
    session, tree = tree_with()
    try:
        turns = Turns([[ToolCall("r1", "read_trajectory", {"attempt_ref": row.attempt_id, "limit": 30}),
                        ToolCall("r2", "read_trajectory", {"attempt_ref": row.attempt_id, "offset": "abc"})],
                       [write("candidates/c1/memory.md", "a")], [finish(declared())]])
        s = CodingSession(turn=turns, work=tree, limits=SessionLimits(), k=3, system="s", inputs={},
                          trajectories=reader, note_refs=["note:a"])
        s.start()
        reads = [c for c in s.calls if c["tool"] == "read_trajectory"]
        assert [c["bytes"] for c in reads] == [30, 0] and [c["ok"] for c in reads] == [True, False]
        assert reader.calls[-1] == {"ref": row.attempt_id, "offset": None, "bytes": 0,
                                    "denied": "offset and limit are integers"}
        assert s.record()["trajectory_reads"] == 2 and s.record()["trajectory_bytes"] == 30
    finally:
        session.close()


# ---------------------------------------------------------------------------- second recheck of 2026-10-05
class FakeAnthropic:
    """The Messages API as AnthropicProvider calls it: each create() answers with the next prepared response and
    keeps what it was sent."""

    def __init__(self):
        self.sent, self.queue = [], []
        self.messages = self

    def create(self, **kw):
        self.sent.append(kw)
        return self.queue.pop(0)


def anthropic_response(text="", calls=()):
    from types import SimpleNamespace as NS
    content = ([NS(type="text", text=text)] if text else []) + [
        NS(type="tool_use", id=c.id, name=c.name, input=c.args) for c in calls]
    return NS(content=content, stop_reason="tool_use" if calls else "end_turn",
              usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0))


def test_a_reply_s_native_content_is_kept_under_the_provider_name_and_replayed_on_the_next_turn():
    """M4 §1 (2026-10-05, second recheck): the session keeps a reply's native content as agent/loop.py does —
    {provider name: content} — so the Anthropic provider replays it on the session's next call (a bare list made
    to_anthropic's `.get(provider)` raise on the second call); a reply with a call that is not strict UTF-8 keeps no
    native content, and the next request is rebuilt from the escaped text and calls, every message encodable."""
    from ouroloop.providers.anthropic import AnthropicProvider
    client = FakeAnthropic()
    provider = AnthropicProvider(client=client, fallbacks=False)
    first = anthropic_response("writing", [write("candidates/c1/memory.md", "a")])
    client.queue = [first, anthropic_response("", [finish(declared())])]
    session, tree = tree_with()
    try:
        s = CodingSession(turn=provider.chat, work=tree, limits=SessionLimits(), k=3, system="s", inputs={},
                          note_refs=["note:a"], provider=provider.name)
        out = s.start()
        assert out.candidates[0]["dir"] == "c1" and len(client.sent) == 2
        replayed = [m for m in client.sent[1]["messages"] if m["role"] == "assistant"][0]
        assert replayed["content"] is first.content                       # the provider's own blocks, unchanged
        assert s.messages[1]["native"] == {"anthropic": first.content}
    finally:
        session.close()
    client = FakeAnthropic()
    provider = AnthropicProvider(client=client, fallbacks=False)
    client.queue = [anthropic_response("bad \ud800", [write("candidates/c1/\udc80.md", "x")]),
                    anthropic_response("", [write("candidates/c1/memory.md", "a")]),
                    anthropic_response("", [finish(declared())])]
    session, tree = tree_with()
    try:
        s = CodingSession(turn=provider.chat, work=tree, limits=SessionLimits(), k=3, system="s", inputs={},
                          note_refs=["note:a"], provider=provider.name)
        assert s.start().candidates
        assert "native" not in s.messages[1]
        rebuilt = [m for m in client.sent[1]["messages"] if m["role"] == "assistant"][0]["content"]
        assert rebuilt[0] == {"type": "text", "text": "bad \\ud800"}
        assert rebuilt[1]["input"]["path"] == "candidates/c1/\\udc80.md"
        json.dumps(client.sent[2]["messages"], ensure_ascii=False, default=str).encode("utf-8")
    finally:
        session.close()


def test_a_surrogate_in_a_tool_name_or_the_reply_text_is_escaped_in_every_record():
    """M4 §1 (2026-10-05, second recheck): a lone surrogate in a call's tool name is the tool's error with the name
    escaped on the call row; one in the reply's text is escaped in the assistant message; the session record and
    the messages encode as strict UTF-8."""
    session, tree = tree_with()
    try:
        replies = Replies([ChatTurn("thinking \udcff", [ToolCall("n", "read_\ud800file", {"path": "package/memory.md"})],
                                    "tool_use"),
                           ChatTurn("", [write("candidates/c1/memory.md", "a")], "tool_use"),
                           ChatTurn("", [finish(declared())], "tool_use")])
        s = CodingSession(turn=replies, work=tree, limits=SessionLimits(), k=3, system="s", inputs={},
                          note_refs=["note:a"])
        assert s.start().candidates
        assert s.calls[0] == {"turn": 1, "tool": "read_\\ud800file", "ok": False, "error": "not strict UTF-8"}
        assert s.messages[1]["text"] == "thinking \\udcff" and s.messages[1]["tool_calls"][0].name == "read_\\ud800file"
        json.dumps(s.record(), ensure_ascii=False).encode("utf-8")
        json.dumps(s.messages, ensure_ascii=False, default=str).encode("utf-8")
    finally:
        session.close()


@pytest.mark.parametrize("text", ['{"tool_calls": 1}', '{"tool_calls": true}', '{"tool_calls": "finish"}'])
def test_a_written_tool_calls_field_that_is_not_a_list_is_no_call(text):
    """M4 §1 (2026-10-05, second recheck): `tool_calls` written in the text is read only when it is a list; anything
    else is no call, answered with the session's nudge, never a TypeError. A structured tool call that is not a
    ToolCall (no provider makes one: anthropic, openai_compat, claude_code and manual all answer with ToolCall) is
    no call either."""
    from ouroloop.research.session import NUDGE
    session, tree = tree_with()
    try:
        replies = Replies([ChatTurn(text, [], "end"),
                           ChatTurn("", [{"id": "d", "name": "finish", "args": "c1x"}], "tool_use"),
                           ChatTurn("", [write("candidates/c1/memory.md", "a")], "tool_use"),
                           ChatTurn("", [finish(declared())], "tool_use")])
        s = CodingSession(turn=replies, work=tree, limits=SessionLimits(), k=3, system="s", inputs={},
                          note_refs=["note:a"])
        out = s.start()
        nudges = [m for m in s.messages if m.get("role") == "user" and m.get("content") == NUDGE]
        assert len(nudges) == 2 and out.candidates and [c["tool"] for c in s.calls] == ["write_file", "finish"]
    finally:
        session.close()


@pytest.mark.parametrize("field,value", [
    ("differs_from", {"ref": "declaration:sha256:" + "a" * 64, "how": "x"}), ("differs_from", "abc"),
    ("differs_from", [1]), ("differs_from", [{"ref": "r"}]), ("predicted_affected", 5),
    ("predicted_affected", True), ("notes", 7)])
def test_a_declaration_field_of_the_wrong_type_is_the_finish_calls_error(field, value):
    """M4 §1 (2026-10-05, second recheck): once a candidate's declaration has a problem it is not built, so a
    wrong-typed differs_from, predicted_affected or notes comes back to R as the finish call's error — never a
    TypeError or KeyError out of the round — and R may finish again."""
    session, tree = tree_with()
    try:
        turns = Turns([[write("candidates/c1/memory.md", "a")], [finish(declared(**{field: value}))],
                       [finish(declared())]])
        s = CodingSession(turn=turns, work=tree, limits=SessionLimits(), k=3, system="s", inputs={},
                          note_refs=["note:a"])
        out = s.start()
        refused = [m for m in s.messages if m.get("role") == "tool"][1]["results"][0]
        assert refused["is_error"] and refused["output"].startswith("the declaration is not readable") and field in \
            refused["output"]
        assert out.candidates[0]["dir"] == "c1" and len(s.declarations) == 1
    finally:
        session.close()


def test_a_call_written_in_the_text_of_a_native_tool_use_reply_is_no_call():
    """M4 §1 (2026-10-05, third recheck): the Anthropic provider has native tool use and replays its own content, so
    a JSON call written in the reply's text is not read as a call — a tool_result for it would follow replayed
    content with no matching tool_use, which the API refuses (a 400 that pauses the activity). The reply gets the
    nudge, and every tool_result the provider is sent answers a tool_use in the assistant message before it."""
    from ouroloop.providers.anthropic import AnthropicProvider
    from ouroloop.research.session import NUDGE
    client = FakeAnthropic()
    provider = AnthropicProvider(client=client, fallbacks=False)
    written = json.dumps({"tool": "write_file", "args": {"path": "candidates/c1/memory.md", "text": "a"}})
    client.queue = [anthropic_response(written, []), anthropic_response("", [write("candidates/c1/memory.md", "a")]),
                    anthropic_response("", [finish(declared())])]
    session, tree = tree_with()
    try:
        s = CodingSession(turn=provider.chat, work=tree, limits=SessionLimits(), k=3, system="s", inputs={},
                          note_refs=["note:a"], provider=provider.name)
        assert s.start().candidates[0]["dir"] == "c1"
        assert s.messages[2] == {"role": "user", "content": NUDGE}
        for request in client.sent:
            messages = request["messages"]
            for i, m in enumerate(messages):
                if m["role"] != "user" or not isinstance(m["content"], list):
                    continue
                answered = {b["tool_use_id"] for b in m["content"] if isinstance(b, dict)
                            and b.get("type") == "tool_result"}
                if answered:
                    before = messages[i - 1]["content"]
                    uses = {getattr(b, "id", None) if not isinstance(b, dict) else b.get("id") for b in before
                            if (getattr(b, "type", None) if not isinstance(b, dict) else b.get("type")) == "tool_use"}
                    assert answered <= uses
    finally:
        session.close()
    # a text-only provider (no native content) still has its written calls read
    replies = Replies([ChatTurn(written, [], "end"), ChatTurn("", [finish(declared())], "tool_use")])
    session, tree = tree_with()
    try:
        s = CodingSession(turn=replies, work=tree, limits=SessionLimits(), k=3, system="s", inputs={},
                          note_refs=["note:a"])
        assert s.start().candidates[0]["dir"] == "c1" and s.calls[0]["tool"] == "write_file"
    finally:
        session.close()


def test_a_draft_check_error_is_strict_utf8(tmp_path):
    """M4 §1 (2026-10-05, third recheck): a package's own exception text may hold a lone surrogate (the worker's
    JSON escapes it, the host's loads restores it); Drafts.check hands back backslash-escaped errors, so the draft
    events and R's revision prompt encode and the round goes on to the revision."""
    from ouroloop.research.drafts import Drafts
    drafts = Drafts.__new__(Drafts)
    drafts._check = lambda files: (["hook failed: RuntimeError: bad \udc80"], None)
    errors, ref = drafts.check({})
    assert errors == ["hook failed: RuntimeError: bad \\udc80"] and ref is None
    errors[0].encode("utf-8")
