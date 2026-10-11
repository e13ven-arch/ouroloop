"""Stage 3: R's coding session, the train screen (V3) and the dev gate (V4) — docs/design/IMPROVER.md §7, §9 and the
"会话" and "筛选" items of §13.

The session (improver/session.py) is driven by a scripted researcher over a real work tree in the test worker, with the
train trajectories of a baseline run on record:
- each cap: session_max_turns and session_max_edits end the session, and a session that ends at a cap keeps what it had
  declared; session_max_read_bytes caps one read_trajectory page and session_max_read_total refuses a read past it;
- read_trajectory refuses a session outside `readable` (another train attempt, a dev attempt), search_trace answers
  only readable sessions;
- finish with an unreadable declaration (fields missing, a dir never written, a kind not assigned, a dir twice, more
  than k, not a list) is the call's error and a later finish ends the session; finish([]) ends it with nothing, and a
  call after a finish in the same turn is not performed;
- revise continues the same session under the same caps and changes files, never declarations;
- transcript() and record(): their shapes, the per-item cut, a reply without tool calls nudged;
- through a whole round (tests/improver/rounds.py, the scripted R in tool mode): run_check answers exactly the text of
  run_ladder's Verdict for the same files and declaration, and session.json holds the record and the transcript.

The screen and the gate (improver/screen.py), on the rsi domain with the forgetful actor (it fails the two notify tasks
unless a package offers it post_channel_message):
- passing_sample: the same (seed, round) draws the same; drawn from the passed tasks, in the frozen order;
- the footprint condition: a fix counts only where the declared component showed — the same two flips are `fixed` for
  the tool that made them and `unconditioned` for a candidate that declares a hook which never acts; with the condition
  dropped (every component, or a whole-package kind) the same screen claims them;
- a regression counts on the sample whatever the footprint; `thin`; the outcomes not_run, invalid, defect and
  budget_exhausted; eligible and pick_best's tie rules;
- price() from the current W's costs on record, (None, why) without them, and gate_attempts from the frozen template;
  an unpriced round stops before any screen and makes no actor call after its train reading;
- the gate over the frozen template with the pooled control: an accepted receipt is registered and installs under
  `install:<round>` with "/" made "_"; a rejected one is registered and installs nothing.

And three found bugs, fixed: a negative read_trajectory limit no longer lifts the read caps; a revision cannot add a
declaration; the gate's price counts the pooled control's top-up (§9).
"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from rounds import ledger_rows, quiet, round_activity, round_state
from scripted_r import TOOL_CODE, TOOL_ENTRY

from ouroloop.activity import phases
from ouroloop.evaluate.gate import TaskEvalPlan, shortfall
from ouroloop.evaluate.release import GateReceipt, InstallReceipt
from ouroloop.harness.kinds import MEMORY_PATH
from ouroloop.harness.package import Limits, build
from ouroloop.improver import screen as sc
from ouroloop.improver.components import components
from ouroloop.improver.declaration import Declaration
from ouroloop.improver.evidence import Evidence
from ouroloop.improver.round import Round, run_round
from ouroloop.improver.session import ITEM_CHARS, NUDGE, CodingSession, WorkTree
from ouroloop.improver.settings import Settings
from ouroloop.improver.verify import Verdict, run_ladder
from ouroloop.runtime.chat import ChatTurn, ToolCall

NOTIFY = ("rsi.notify_after_update", "rsi.notify_after_update_variant")   # the forgetful actor fails these
PASSING = ("rsi.update_stage", "rsi.policy_hold", "rsi.notify_once")       # and passes these (frozen order)
ROUND = "rsi-test/r9"


# ---------------------------------------------------------------------------- the activity with a baseline
@pytest.fixture(scope="module")
def world(tmp_path_factory, fake_upstream):
    """The rsi activity with the baseline read and the gate frozen; nothing installed."""
    s = round_activity(tmp_path_factory.mktemp("world"), fake_upstream, "tool")
    s.base_release = s.a.active_release()
    s.base = s.a.package(s.base_release)
    s.evidence = Evidence(s.a)
    s.reading = s.evidence.reading(s.base_release.package_ref)
    s.order = list(s.a.splits.ids("train"))
    s.sessions = {t: r.evidence or r.attempt_id for t, r in s.reading.items()}
    assert s.order == ["rsi.update_stage", *NOTIFY, "rsi.policy_hold", "rsi.notify_once"]
    assert [t for t in s.order if not s.reading[t].success] == list(NOTIFY)
    return s


# ---------------------------------------------------------------------------- the session's harness
class Script:
    """A scripted researcher: each chat call answers the next of `turns` — a list of (tool, args), or a string for a
    reply without tool calls — and, once they are used up, `then(messages)`. It keeps what it was shown."""

    def __init__(self, *turns, then=None):
        self.turns, self.then, self.seen = list(turns), then, []

    def chat(self, system, messages, specs, **_kw):
        self.seen.append({"system": system, "messages": list(messages), "tools": [s.name for s in specs]})
        if self.turns:
            turn = self.turns.pop(0)
        elif self.then is not None:
            turn = self.then(messages)
        else:
            raise AssertionError("the session asked for a turn the script does not have")
        if isinstance(turn, str):
            return ChatTurn(turn, [], "end", usage={})
        n = len(self.seen)
        return ChatTurn("", [ToolCall(f"s{n}.{i}", name, args) for i, (name, args) in enumerate(turn)], "tool_use",
                        usage={})


@pytest.fixture
def tree(world):
    """A fresh work tree in the test worker, the way round.py sets one up: package/ the current W, inputs/ a file."""
    ws = world.a.worker().start()
    try:
        ws.load({})
        t = WorkTree(ws)
        t.init({p: x for p, x in world.base.files.items() if p != MEMORY_PATH}, {"causes.json": "[]"},
               max_bytes=Limits.from_core(world.a.k).max_package_bytes)
        yield t
    finally:
        ws.close(force=True)


def coding(world, tree, script, *, k=1, kinds=("tool", "hook"), readable=None, **caps) -> CodingSession:
    """A session over `tree`; `check` and `tests` answer a fixed text and keep what they were asked."""
    checked, tested = [], []

    def check(name, raw):
        checked.append((name, raw))
        return f"V0: passed (checked {name})"

    def tests(name):
        tested.append(name)
        return f"the package tests of {name} pass"
    cs = CodingSession(researcher=script, work=tree, trace=world.evidence.trace, check=check, tests=tests,
                       settings=Settings(**caps), k=k, system="You improve the harness package (a test session)",
                       inputs={"round": ROUND},
                       readable={world.sessions[NOTIFY[0]]} if readable is None else readable, kinds=kinds)
    cs.checked, cs.tested = checked, tested
    return cs


def decl(dir="notify", **over) -> dict:
    return {"dir": dir, "cause_id": f"{ROUND}:missed_action", "kind": "tool", "site": "post_channel_message",
            "predicted_affected": list(NOTIFY), "trigger": "a request asks for a message in #ops", **over}


def write(dir="notify", name="tools/x.py", text="X = 1\n"):
    return "write_file", {"path": f"candidates/{dir}/{name}", "text": text}


def finish(*decls):
    return "finish", {"candidates": list(decls)}


def results(cs, name=None) -> list[dict]:
    return [r for m in cs.messages if m.get("role") == "tool" for r in m["results"] if name in (None, r["name"])]


def trajectory_bytes(world, session) -> bytes:
    return json.dumps(world.evidence.trace.trajectory(session), ensure_ascii=False, indent=1).encode("utf-8")


# ---------------------------------------------------------------------------- the caps
def test_the_turn_cap_ends_the_session_with_what_it_declared(world, tree):
    script = Script([write()], [finish(decl())], then=lambda _m: [("list_files", {"path": "inputs"})])
    cs = coding(world, tree, script, session_max_turns=4)
    first = cs.start()
    assert [d.dir for d in first.declarations] == ["notify"] and first.ended == "" and cs.turns == 2
    again = cs.revise("candidates/notify failed its check:\nV1: FAILED — boom")
    assert again.ended == "the session used its 4 turns"
    assert again.declarations == first.declarations
    assert cs.turns == 4 and len(script.seen) == 4
    # an ended session takes no further turn
    third = cs.revise("once more")
    assert len(script.seen) == 4 and third.declarations == first.declarations and third.ended == again.ended
    assert cs.record()["ended"] == again.ended and cs.record()["declared"] == ["notify"]


def test_a_reply_without_tool_calls_is_a_turn_and_is_nudged(world, tree):
    script = Script(then=lambda _m: "Let me think about it.")
    cs = coding(world, tree, script, session_max_turns=3)
    got = cs.start()
    assert got.declarations == [] and got.ended == "the session used its 3 turns"
    assert len(script.seen) == 3
    assert [m for m in cs.messages if m.get("role") == "user"][1:] == [{"role": "user", "content": NUDGE}] * 3


def test_the_edit_cap_ends_the_session_before_the_edit_past_it(world, tree):
    script = Script([write(name="a.py", text="A = 1\n")], [finish(decl())],
                    [("edit_file", {"path": "candidates/notify/a.py", "old": "A = 1", "new": "A = 2"}),
                     ("delete_file", {"path": "candidates/notify/tools/plan_changes.py"}),
                     write(name="b.py"), ("list_files", {})])
    cs = coding(world, tree, script, session_max_edits=3)
    first = cs.start()
    assert [d.dir for d in first.declarations] == ["notify"]
    again = cs.revise("candidates/notify failed its check:\nV0: FAILED")
    assert again.ended == "the session used its 3 edits"
    assert again.declarations == first.declarations
    assert cs.edits == 3 and len(script.seen) == 3
    # write, edit and delete each count; the edit past the cap and what follows it are not performed
    assert [r["name"] for r in cs.messages[-1]["results"]] == ["edit_file", "delete_file"]
    files, _ = tree.fetch("notify")
    assert files["a.py"] == "A = 2\n" and "tools/plan_changes.py" not in files and "b.py" not in files
    # an ended session takes no further turn, though turns are left
    later = cs.revise("candidates/notify failed its check again")
    assert later.ended == again.ended and later.declarations == first.declarations and len(script.seen) == 3


def test_read_trajectory_pages_at_most_session_max_read_bytes(world, tree):
    session = world.sessions[NOTIFY[0]]
    data = trajectory_bytes(world, session)
    assert len(data) > 400
    script = Script([("read_trajectory", {"session": session})],
                    [("read_trajectory", {"session": session, "limit": 1000})],
                    [("read_trajectory", {"session": session, "offset": 100, "limit": 40})], [finish()])
    cs = coding(world, tree, script, session_max_read_bytes=100, session_max_read_total=10_000)
    assert cs.start().ended == ""
    out = results(cs, "read_trajectory")
    assert not any(r["is_error"] for r in out)
    assert out[0]["output"] == (data[:100].decode("utf-8", "replace")
                                + f"\n[... {len(data) - 100} bytes follow; read again with offset 100]")
    assert out[1]["output"] == out[0]["output"]            # a larger limit is cut to the cap
    assert out[2]["output"] == (data[100:140].decode("utf-8", "replace")
                                + f"\n[... {len(data) - 140} bytes follow; read again with offset 140]")
    assert cs.read_bytes == 240 == cs.record()["read_bytes"]


def test_read_trajectory_refuses_a_read_past_session_max_read_total(world, tree):
    session = world.sessions[NOTIFY[0]]
    data = trajectory_bytes(world, session)
    script = Script([("read_trajectory", {"session": session})],
                    [("read_trajectory", {"session": session, "offset": 100})],
                    [("read_trajectory", {"session": session, "offset": 200})],                  # 200 + 100 > 250
                    [("read_trajectory", {"session": session, "offset": 200, "limit": 50})],     # exactly 250
                    [("read_trajectory", {"session": session, "offset": 250, "limit": 1})],      # 251 > 250
                    [finish()])
    cs = coding(world, tree, script, session_max_read_bytes=100, session_max_read_total=250)
    got = cs.start()
    out = results(cs, "read_trajectory")
    assert [r["is_error"] for r in out] == [False, False, True, False, True]
    assert out[2]["output"] == "refused: this read would pass the session's 250 bytes (200 read so far)"
    assert out[4]["output"] == "refused: this read would pass the session's 250 bytes (250 read so far)"
    assert out[3]["output"].startswith(data[200:250].decode("utf-8", "replace") + "\n[... ")
    # a refused read is the call's error, not the session's end
    assert cs.read_bytes == 250 and got.ended == "" and len(script.seen) == 6


def test_a_negative_limit_does_not_lift_the_read_caps(world, tree):
    session = world.sessions[NOTIFY[0]]
    assert len(trajectory_bytes(world, session)) > 400
    script = Script([("read_trajectory", {"session": session, "limit": -1})], [finish()])
    cs = coding(world, tree, script, session_max_read_bytes=100, session_max_read_total=250)
    cs.start()
    assert len(results(cs, "read_trajectory")[0]["output"].encode("utf-8")) <= 100 + 80    # a page and its note
    assert cs.read_bytes <= 100


def test_read_trajectory_refuses_a_session_outside_readable(world, tree):
    mine = world.sessions[NOTIFY[0]]
    other = world.sessions[PASSING[0]]
    dev = next(r.evidence for r in world.a.results.all() if r.split == "dev")
    script = Script([("read_trajectory", {"session": other}), ("read_trajectory", {"session": dev}),
                     ("read_trajectory", {"session": "../../../etc/passwd"})],
                    [("read_trajectory", {"session": mine, "limit": 10}), ("search_trace", {"query": "opportunity"})],
                    [finish()])
    cs = coding(world, tree, script, readable={mine})
    cs.start()
    out = results(cs, "read_trajectory")
    assert [r["is_error"] for r in out] == [True, True, True, False]
    assert all(r["output"].startswith("AccessDenied: ") for r in out[:3])
    assert cs.read_bytes == 10
    # the search answers the readable sessions only, though every train attempt mentions the stage
    assert world.evidence.trace.search("opportunity", limit=20)
    assert {h["session"] for h in world.evidence.trace.search("opportunity", limit=20)} - {mine}
    hits = json.loads(results(cs, "search_trace")[0]["output"])
    assert hits and {h["session"] for h in hits} == {mine}


# ---------------------------------------------------------------------------- finish
def test_an_unreadable_finish_is_the_calls_error_and_a_later_finish_ends_the_session(world, tree):
    script = Script([write()],
                    [finish({"dir": "notify", "kind": "tool"})],                 # fields missing
                    [finish(decl(dir="never"))],                                # a dir it never wrote
                    [finish(decl(kind="skill", site="x"))],                     # a kind this round did not assign
                    [finish(decl(), decl())],                                   # one dir twice
                    [finish(decl(), decl(dir="b"), decl(dir="c"))],             # more than k
                    [("finish", {"candidates": "notify"})],                     # not a list
                    [finish(decl(trigger="the one that stands"))])
    cs = coding(world, tree, script, k=2)
    got = cs.start()
    out = results(cs, "finish")
    assert [r["is_error"] for r in out] == [True] * 6 + [False]
    assert "lacks" in out[0]["output"] and "'never' is not a candidate directory" in out[1]["output"]
    assert "kind 'skill' is not one this round assigned" in out[2]["output"]
    assert "declared twice" in out[3]["output"] and out[4]["output"] == "at most 2 candidates this round"
    assert out[5]["output"].startswith("finish takes candidates: a list")
    assert out[6]["output"] == "declared: notify"
    assert got.ended == "" and [d.trigger for d in got.declarations] == ["the one that stands"]
    assert cs.turns == 8 and len(script.seen) == 8


def test_finish_with_an_empty_list_ends_the_session_with_no_candidates(world, tree):
    script = Script([finish(), write()])
    cs = coding(world, tree, script)
    got = cs.start()
    assert got.declarations == [] and got.ended == ""
    assert cs.turns == 1 and len(script.seen) == 1
    done, after = results(cs)
    assert (done["output"], done["is_error"]) == ("declared: nothing", False)
    # a call after the finish in the same turn is not performed
    assert after["is_error"] and after["output"].startswith("not performed")
    assert cs.edits == 0 and cs.dirs == [] and tree.fetch("notify")[0] is None
    assert cs.record()["declared"] == []


# ---------------------------------------------------------------------------- revise
def test_revise_continues_the_session_and_changes_files_not_declarations(world, tree):
    script = Script([write(text="V = 1\n")], [finish(decl(trigger="first"))],
                    [("edit_file", {"path": "candidates/notify/tools/x.py", "old": "V = 1", "new": "V = 2"})],
                    [finish(decl(trigger="second", kind="hook", site="on_finish", predicted_affected=[NOTIFY[1]]))])
    cs = coding(world, tree, script)
    first = cs.start()
    n = len(cs.messages)
    text = "candidates/notify failed its check:\nV1: FAILED — boom\nEdit its files and call finish again."
    again = cs.revise(text)
    # the same session: the revision's first turn is shown every earlier message, then the verdict
    shown = script.seen[2]["messages"]
    assert len(shown) == n + 1 and shown[:n] == cs.messages[:n] and shown[n] == {"role": "user", "content": text}
    assert cs.turns == 4 and cs.edits == 2                  # counted on under the same caps, not reset
    # the files changed; the re-declared dir keeps its first declaration
    assert tree.fetch("notify")[0]["tools/x.py"] == "V = 2\n"
    assert again.ended == "" and again.declarations == first.declarations
    d = again.declarations[0]
    assert (d.trigger, d.kind, d.site, d.predicted_affected) == ("first", "tool", "post_channel_message", NOTIFY)
    assert cs.record()["declared"] == ["notify"]


def test_a_revision_cannot_add_a_declaration(world, tree):
    script = Script([write()], [finish(decl())], [write(dir="other")], [finish(decl(dir="other"))],
                    [finish(decl())])
    cs = coding(world, tree, script, k=1)
    first = cs.start()
    again = cs.revise("candidates/notify failed its check:\nV0: FAILED")
    assert again.ended == "" and len(script.seen) == 5        # the new dir is a tool error; the next finish ends it
    refused = [r for m in cs.messages if m.get("role") == "tool" for r in m.get("results") or ()
               if "declares no new candidate" in str(r.get("output"))]
    assert len(refused) == 1 and refused[0].get("is_error")
    assert again.declarations == first.declarations
    assert cs.record()["declared"] == ["notify"]


# ---------------------------------------------------------------------------- the transcript and the record
def test_the_transcript_and_the_record(world, tree):
    long = "x" * (ITEM_CHARS + 500)
    script = Script("Let me look first.",
                    [write(name="notes.txt", text=long), ("read_file", {"path": "candidates/notify/notes.txt"}),
                     ("bogus_tool", {}), ("list_files", "not an object")],
                    [("run_check", {"dir": "notify", "declaration": decl()}), ("run_tests", {"dir": "notify"}),
                     ("run_check", {"dir": "nope", "declaration": decl()}), ("run_tests", {"dir": "nope"})],
                    [finish(decl())])
    cs = coding(world, tree, script)
    cs.start()
    # run_check and run_tests: the callables' text for a written dir, the call's error otherwise
    assert cs.checked == [("notify", decl())] and cs.tested == ["notify"]
    rec = cs.record()
    assert set(rec) == {"turns", "edits", "read_bytes", "dirs", "declared", "ended", "calls", "compactions"}
    assert (rec["turns"], rec["edits"], rec["read_bytes"], rec["dirs"], rec["declared"], rec["ended"]) == \
        (4, 1, ITEM_CHARS + 500, ["notify"], ["notify"], "")          # read_file counts toward the session's reads
    assert rec["calls"] == [
        {"turn": 2, "tool": "write_file", "path": "candidates/notify/notes.txt", "ok": True},
        {"turn": 2, "tool": "read_file"}, {"turn": 2, "tool": "bogus_tool"},
        {"turn": 2, "tool": "list_files", "ok": False},
        {"turn": 3, "tool": "run_check"}, {"turn": 3, "tool": "run_tests"},
        {"turn": 3, "tool": "run_check"}, {"turn": 3, "tool": "run_tests"}, {"turn": 4, "tool": "finish"}]
    tr = cs.transcript()
    assert json.loads(json.dumps(tr)) == tr                 # plain data
    assert (tr["schema"], tr["turns"], tr["ended"]) == ("coding_session.v1", 4, "")
    m = tr["messages"]
    assert [x["role"] for x in m] == ["user", "assistant", "user", "assistant", "tool", "assistant", "tool",
                                      "assistant", "tool"]
    assert json.loads(m[0]["content"]) == {"round": ROUND}
    assert m[1] == {"role": "assistant", "text": "Let me look first.", "tool_calls": []}
    assert m[2] == {"role": "user", "content": NUDGE}
    cut = "x" * ITEM_CHARS + "... [500 more characters]"
    assert m[3]["tool_calls"][0] == {"id": "s2.0", "name": "write_file",
                                     "args": {"path": "candidates/notify/notes.txt", "text": cut}}
    wrote, read, bogus, bad = m[4]["results"]
    assert set(wrote) == {"id", "name", "output", "is_error"} and wrote["is_error"] is False
    assert (read["output"], read["is_error"]) == (cut, False)
    assert bogus == {"id": "s2.2", "name": "bogus_tool", "output": "unknown tool 'bogus_tool'", "is_error": True}
    assert bad["is_error"] and bad["output"] == "list_files: the arguments are a JSON object"
    check, tests, no_check, no_tests = m[6]["results"]
    assert (check["output"], check["is_error"]) == ("V0: passed (checked notify)", False)
    assert (tests["output"], tests["is_error"]) == ("the package tests of notify pass", False)
    assert no_check["is_error"] and no_check["output"].startswith("no candidate directory 'nope'")
    assert no_tests["is_error"] and no_tests["output"].startswith("no candidate directory 'nope'")
    assert m[8]["results"][0]["output"] == "declared: notify"


# ---------------------------------------------------------------------------- the session in a round
@pytest.fixture(scope="module")
def done(tmp_path_factory, fake_upstream):
    """Round 0 run whole with the scripted R's tool candidate: screened, gated, installed."""
    s = round_activity(tmp_path_factory.mktemp("done"), fake_upstream, "tool")
    s.base_release = s.a.active_release()
    s.result = phases.round_(s.a, 0, out=quiet)
    s.state = round_state(s.a, 0)
    s.record = json.loads((s.a.root / "rounds" / "r0" / "session.json").read_text(encoding="utf-8"))
    s.transcript = s.a.objects.get(s.record["transcript"])
    assert s.result.status == "done" and s.result.installed
    return s


def test_run_check_answers_the_text_of_run_ladders_verdict(done):
    a = done.a
    checks = [r for m in done.transcript["messages"] if m["role"] == "tool" for r in m["results"]
              if r["name"] == "run_check"]
    assert len(checks) == 1 and not checks[0]["is_error"]
    (item,) = done.state["steps"]["implemented"]["candidates"]
    declaration = Declaration.from_json(a.objects.get(item["declaration"]))
    files = a.objects.get(item["files"])
    stored = Verdict.from_json(a.objects.get(item["verdict"]))
    again = run_ladder(files, declaration, Round(a, 0, run_arm=phases.run_arm).vctx())
    assert list(stored.steps) == ["V0", "V1", "V2a"] and stored.passed()
    assert checks[0]["output"] == stored.text() == again.text()


def test_a_rounds_session_record_and_transcript(done):
    rec = done.record
    assert set(rec) == {"turns", "edits", "read_bytes", "dirs", "declared", "ended", "calls", "compactions", "transcript"}
    assert (rec["turns"], rec["edits"], rec["dirs"], rec["declared"], rec["ended"]) == (4, 2, ["notify"], ["notify"], "")
    assert [c["tool"] for c in rec["calls"]] == ["read_file", "read_file", "write_file", "write_file", "run_check",
                                                 "finish"]
    tr = done.transcript
    assert (tr["schema"], tr["turns"], tr["ended"]) == ("coding_session.v1", 4, "")
    assert [m["role"] for m in tr["messages"]] == ["user"] + ["assistant", "tool"] * 4
    assert json.loads(tr["messages"][0]["content"])["round"] == "rsi-test/r0"


# ---------------------------------------------------------------------------- the passing sample
def test_passing_sample_draws_passed_tasks_per_seed_and_round_in_the_frozen_order():
    order = [f"t{i:02d}" for i in range(40)]
    reading = {t: SimpleNamespace(success=i % 4 != 0) for i, t in enumerate(order)}      # 30 pass, 10 fail
    reading["outside"] = SimpleNamespace(success=True)                                  # not in the frozen order
    passed = [t for t in order if reading[t].success]
    got = sc.passing_sample(reading, order=order, size=6, seed=7, round_index=2)
    assert len(got) == 6 and set(got) <= set(passed) and got == [t for t in order if t in got]
    # the same seed and round draw the same, whatever order the reading comes in
    assert sc.passing_sample(dict(reversed(list(reading.items()))), order=order, size=6, seed=7, round_index=2) == got
    # the round and the seed move the draw
    assert len({tuple(sc.passing_sample(reading, order=order, size=6, seed=7, round_index=i)) for i in range(8)}) > 1
    assert len({tuple(sc.passing_sample(reading, order=order, size=6, seed=s, round_index=2)) for s in range(8)}) > 1
    assert sc.passing_sample(reading, order=order, size=100, seed=7, round_index=2) == passed
    assert sc.passing_sample(reading, order=order, size=0, seed=7, round_index=2) == []
    rest = sc.passing_sample(reading, order=order, size=6, seed=7, round_index=2, exclude=got)
    assert len(rest) == 6 and not set(rest) & set(got)


def test_a_rounds_screen_runs_its_failing_set_and_its_sample(done):
    a = done.a
    (row,) = done.state["steps"]["screened"]["screens"]
    res = sc.ScreenResult.from_json(a.objects.get(row["screen"]))
    reading = Evidence(a).reading(done.base_release.package_ref)
    sample = sc.passing_sample(reading, order=a.splits.ids("train"), seed=int(a.k["gate"]["order_seed"]),
                               size=Settings.from_core(a.k).screen_passing_sample, round_index=0)
    assert tuple(res.sample) == tuple(sample) == PASSING
    assert tuple(res.failing) == NOTIFY and tuple(res.tasks) == NOTIFY + PASSING
    assert (tuple(res.fixed), tuple(res.regressed), res.net_flips, res.thin, res.outcome) == \
        (NOTIFY, (), 2, True, "screened")


# ---------------------------------------------------------------------------- screens of real candidates
INERT_HOOK = "def inert(ctx, event):\n    return None\n"
INERT_ENTRY = {"name": "inert_ops", "site": "on_finish", "entrypoint": "middlewares/inert_ops.py:inert"}
SILENT_TOOL = "def run(ctx, args):\n    return {'output': 'posted to the ops channel', 'is_error': False}\n"
RAISING_TOOL = "def run(ctx, args):\n    raise RuntimeError('boom')\n"
EXITING_TOOL = "import os\n\n\ndef run(ctx, args):\n    os._exit(3)\n"


def candidate(world, *, tool=None, hook=False):
    """The current W plus post_channel_message (with `tool` as its code) and/or a hook that never acts."""
    files = {p: x for p, x in world.base.files.items() if p != MEMORY_PATH}
    manifest = json.loads(files["manifest.json"])
    if tool is not None:
        files["tools/post_channel_message.py"] = tool
        manifest["tools"] = [*manifest.get("tools", []), TOOL_ENTRY]
    if hook:
        files["middlewares/inert_ops.py"] = INERT_HOOK
        manifest["hooks"] = [*manifest.get("hooks", []), INERT_ENTRY]
    files["manifest.json"] = json.dumps(manifest, indent=1) + "\n"
    package = build(world.a.objects, files, limits=Limits.from_core(world.a.k),
                    seed=world.a.package(world.a.seed_release()), parent=world.base)
    return files, package


def run_screen(world, name, files, package, kind, *, failing=NOTIFY, sample=PASSING, account=None, thin_tasks=3,
               conditions=None):
    """One screen as round.py runs it; the conditions are condition(declaration, components) unless given."""
    if conditions is None:
        conditions = sc.condition(Declaration(dir=name, cause_id=f"{ROUND}:missed_action", kind=kind),
                                  components(world.base, files))
    return sc.screen(world.a, run_arm=phases.run_arm, name=f"r9:{name}", package=package,
                     base_release=world.base_release, failing=list(failing), sample=list(sample),
                     reading=world.reading, evidence=world.evidence, conditions=conditions,
                     account=account or world.a.account("screen-tests"), retries=0, thin_tasks=thin_tasks)


def screen_rows(world, name) -> dict:
    runs = world.a.results.runs()
    return {r.task_id: r for r in world.a.results.all()
            if runs.get(r.run) is not None and runs[r.run].candidate == f"r9:{name}" and runs[r.run].role == "screen"}


@pytest.fixture(scope="module")
def screens(world):
    """Three screens: the tool that posts (declared a tool); the same tool with a hook that never acts (declared the
    hook); a tool that says it posted and does not, with that hook (declared the hook)."""
    out = {}
    for name, tool, hook, kind in (("tool", TOOL_CODE, False, "tool"), ("mixed", TOOL_CODE, True, "hook"),
                                   ("silent", SILENT_TOOL, True, "hook")):
        files, package = candidate(world, tool=tool, hook=hook)
        out[name] = SimpleNamespace(files=files, package=package, kind=kind,
                                    result=run_screen(world, name, files, package, kind))
    return out


def test_a_fix_counts_only_where_the_candidates_component_showed(world, screens):
    tool, mixed = screens["tool"].result, screens["mixed"].result
    # the tool that posts the message: both notify tasks flip where it was called
    assert tool.fixed == NOTIFY and tool.unconditioned == () and tool.net_flips == 2
    rows = screen_rows(world, "tool")
    assert all(rows[t].success and world.evidence.footprint(rows[t], {"kind": "tool", "name": TOOL_ENTRY["name"]})
               for t in NOTIFY)
    # the same flips under a candidate that declares a hook and carries the tool with it (IMPROVER §7.1): the tool is
    # the mechanism's supporting component, so its footprint conditions the fix too
    hook_comp = {"kind": "hook", "name": "inert_ops", "change": "added", "site": "on_finish"}
    tool_comp = {"kind": "tool", "name": TOOL_ENTRY["name"], "change": "added"}
    assert sc.condition(Declaration(kind="hook"), components(world.base, screens["mixed"].files)) == \
        [hook_comp, tool_comp]
    assert all(screen_rows(world, "mixed")[t].success for t in NOTIFY)
    assert mixed.fixed == NOTIFY and mixed.unconditioned == () and mixed.net_flips == 2
    assert sc.eligible(tool, Settings()) and sc.eligible(mixed, Settings())
    # conditioned on the hook alone (the fixed code's rule, or a hook with nothing else): it never acted, so the same
    # reading of the same screen (recorded, not run again) counts the flips as unconditioned
    before = len(list(world.a.results.all()))
    alone = run_screen(world, "mixed", screens["mixed"].files, screens["mixed"].package, "hook",
                       conditions=[hook_comp])
    assert alone.fixed == () and alone.unconditioned == NOTIFY and alone.net_flips == 0
    assert not sc.eligible(alone, Settings())
    # the mutation: drop the condition — every changed component, or a whole-package kind — and both fixes are claimed
    every = [{"kind": c["kind"], "name": c["name"]} for c in components(world.base, screens["mixed"].files)]
    for conditions in (every, sc.condition(Declaration(kind="prompt"), [])):
        dropped = run_screen(world, "mixed", screens["mixed"].files, screens["mixed"].package, "hook",
                             conditions=conditions)
        assert dropped.fixed == NOTIFY and dropped.unconditioned == () and dropped.net_flips == 2
    assert len(list(world.a.results.all())) == before


def test_condition_is_the_declared_kinds_changed_components_or_every_attempt():
    comps = [{"kind": "tool", "name": "a", "change": "added"}, {"kind": "tool", "name": "b", "change": "deleted"},
             {"kind": "hook", "name": "h", "change": "modified", "site": "on_finish"},
             {"kind": "skill", "name": "s", "change": "added"}, {"kind": "test", "name": "tests", "change": "modified"}]
    # R's candidate: the declared kind's components first, then the supporting tools, hooks and skills it carries
    assert sc.condition(Declaration(kind="tool"), comps) == [comps[0], comps[2], comps[3]]
    assert sc.condition(Declaration(kind="hook"), comps) == [comps[2], comps[0], comps[3]]
    assert sc.condition(Declaration(kind="skill"), comps) == [comps[3], comps[0], comps[2]]
    # the fixed code's: only its own kind
    assert sc.condition(Declaration(kind="skill", fixed="promotion:x"), comps) == [comps[3]]
    for kind in ("prompt", "agent", "config", "vendor", "removal", "model"):
        assert sc.condition(Declaration(kind=kind), comps) == [{"kind": "prompt", "name": "*"}]
    assert sc.condition(Declaration(kind="memory"), comps) == [{"kind": "memory", "name": "snapshot"}]


def test_a_regression_counts_on_the_sample_whatever_the_footprint(world, screens):
    silent = screens["silent"].result
    # the tool says it posted and did not: notify_once, which the current W passes, now fails
    assert silent.regressed == ("rsi.notify_once",) and silent.fixed == () and silent.unconditioned == ()
    assert silent.net_flips == -1 and not sc.eligible(silent, Settings())
    # the declared hook never acted there: the regression counts all the same
    row = screen_rows(world, "silent")["rsi.notify_once"]
    assert not row.success and not world.evidence.footprint(row, {"kind": "hook", "name": "inert_ops"})
    # the rest of the sample held
    assert screens["tool"].result.regressed == () and screens["mixed"].result.regressed == ()


def test_a_screens_record(world, screens):
    tool = screens["tool"]
    res = tool.result
    assert (res.candidate, res.package_ref, res.outcome, res.reason) == ("r9:tool", tool.package.ref(), "screened", "")
    assert (res.tasks, res.failing, res.sample) == (NOTIFY + PASSING, NOTIFY, PASSING)
    rows = screen_rows(world, "tool")
    assert sorted(rows) == sorted(NOTIFY + PASSING)
    assert res.cost == round(sum(r.cost_usd for r in rows.values()) / len(rows), 6) and res.cost > 0
    assert sc.ScreenResult.from_json(json.loads(json.dumps(res.to_json()))).to_json() == res.to_json()


def test_thin_when_the_failing_set_is_below_thin_screen_tasks(world, screens):
    tool = screens["tool"]
    assert tool.result.thin                                 # 2 failing tasks < 3
    before = len(list(world.a.results.all()))
    again = run_screen(world, "tool", tool.files, tool.package, "tool", thin_tasks=2)
    assert not again.thin and again.fixed == tool.result.fixed
    assert len(list(world.a.results.all())) == before             # the same run, read again: nothing paid
    nothing = run_screen(world, "tool", tool.files, tool.package, "tool", failing=(), thin_tasks=1)
    assert nothing.thin and nothing.outcome == "not_run"


def test_a_screen_with_no_failing_task_is_not_run(world, screens):
    tool = screens["tool"]
    before = len(list(world.a.results.all()))
    res = run_screen(world, "notrun", tool.files, tool.package, "tool", failing=())
    assert (res.outcome, res.fixed, res.regressed, res.net_flips, res.cost) == ("not_run", (), (), 0, None)
    assert res.tasks == PASSING and res.reason
    assert len(list(world.a.results.all())) == before and screen_rows(world, "notrun") == {}
    assert not sc.eligible(res, Settings())


def test_a_tool_that_raises_is_a_defect(world):
    files, package = candidate(world, tool=RAISING_TOOL)
    res = run_screen(world, "defect", files, package, "tool")
    assert res.outcome == "defect" and res.reason.startswith("candidate defect on rsi.notify_after_update")
    assert (res.fixed, res.net_flips) == ((), 0) and not sc.eligible(res, Settings())
    assert screen_rows(world, "defect")["rsi.notify_after_update"].failure_class == "candidate_defect"


def test_no_valid_reading_on_the_failing_set_is_invalid(world):
    files, package = candidate(world, tool=EXITING_TOOL)          # the worker dies: infrastructure, not evidence
    res = run_screen(world, "invalid", files, package, "tool")
    assert (res.outcome, res.reason) == ("invalid", "no valid reading on the failing set")
    rows = screen_rows(world, "invalid")
    assert all(rows[t].validity == "invalid" for t in NOTIFY) and rows["rsi.update_stage"].validity == "valid"
    assert not sc.eligible(res, Settings())


def test_a_screen_the_budget_refuses_is_budget_exhausted(world, screens):
    tool = screens["tool"]
    tiny = world.a.root_account().child("rsi-test:screen-tests:tiny", {"usd": 0.0001})
    res = run_screen(world, "budget", tool.files, tool.package, "tool", account=tiny)
    assert (res.outcome, res.reason) == ("budget_exhausted", "the budget refused a screen attempt")
    assert (res.fixed, res.net_flips) == ((), 0) and not sc.eligible(res, Settings())
    assert {r.failure_class for r in screen_rows(world, "budget").values()} == {"budget_exhausted"}


# ---------------------------------------------------------------------------- eligibility and the best
def result(name, *, fixed=1, regressed=0, net=None, cost=0.01, outcome="screened") -> sc.ScreenResult:
    f = tuple(f"{name}.f{i}" for i in range(fixed))
    r = tuple(f"{name}.r{i}" for i in range(regressed))
    return sc.ScreenResult(candidate=name, fixed=f, regressed=r, net_flips=len(f) - len(r) if net is None else net,
                           cost=cost, outcome=outcome)


def test_eligible_takes_net_flips_fixes_and_sample_regressions_against_the_settings():
    s = Settings()
    assert (s.screen_min_net_flips, s.screen_min_fixes, s.screen_max_sample_regressions) == (0, 1, 1)
    assert sc.eligible(result("a"), s)
    assert sc.eligible(result("b", fixed=1, regressed=1), s)            # net 0, one fix, at the regression cap
    assert not sc.eligible(result("c", fixed=3, regressed=2), s)        # past the regression cap
    assert not sc.eligible(result("d", fixed=0), s)                     # no fix
    assert not sc.eligible(result("e", fixed=1, net=-1), s)             # net below the floor
    for outcome in ("not_run", "invalid", "defect", "budget_exhausted"):
        assert not sc.eligible(result("f", fixed=3, outcome=outcome), s)
    assert not sc.eligible(result("g", fixed=1), Settings(screen_min_fixes=2))
    assert not sc.eligible(result("h", fixed=2, regressed=1), Settings(screen_min_net_flips=2))
    assert sc.eligible(result("i", fixed=2), Settings(screen_min_net_flips=2))
    assert sc.eligible(result("j", fixed=3, regressed=2), Settings(screen_max_sample_regressions=2))


def test_pick_best_most_net_flips_then_lower_cost_unknown_last_then_the_order_given():
    s = Settings()
    two, three = result("two", fixed=2, cost=0.01), result("three", fixed=3, cost=0.09)
    assert sc.pick_best([two, three], s) is three           # more net flips, whatever the cost
    cheap = result("cheap", fixed=3, cost=0.02)
    assert sc.pick_best([three, cheap], s) is cheap         # tied: the lower cost
    unknown = result("unknown", fixed=3, cost=None)
    assert sc.pick_best([unknown, three], s) is three       # an unknown cost goes last
    free = result("free", fixed=3, cost=0.0)
    assert sc.pick_best([unknown, free], s) is free         # a cost of 0 is known
    twin = result("twin", fixed=3, cost=0.02)
    assert sc.pick_best([cheap, twin], s) is cheap and sc.pick_best([twin, cheap], s) is twin    # the order given
    other = result("other", fixed=3, cost=None)
    assert sc.pick_best([unknown, other], s) is unknown and sc.pick_best([other, unknown], s) is other
    defect, regressing = result("defect", fixed=5, cost=0.0, outcome="defect"), result("bad", fixed=5, regressed=2)
    assert sc.pick_best([defect, regressing, two], s) is two     # the ineligible never win
    assert sc.pick_best([defect, regressing], s) is None and sc.pick_best([], s) is None


# ---------------------------------------------------------------------------- the price before anything is paid
def test_price_is_the_screens_and_the_gate_at_the_current_ws_costs(world, screens):
    a, ref = world.a, world.base_release.package_ref
    runs = a.results.runs()

    def mean(split):
        costs = [r.cost_usd for r in a.results.all()
                 if r.split == split and r.validity == "valid" and runs[r.run].package_ref == ref]
        return sum(costs) / len(costs)
    train, dev = mean("train"), mean("dev")
    assert train > 0 and dev > 0
    plan = sc.template(a)
    assert sc.gate_attempts(a) == len(plan.task_ids) * plan.repeats * (1 + plan.confirmations) == 3
    assert sc.price(a, ref, screen_attempts=10) == (round(10 * train + 3 * dev, 6), "")
    assert sc.price(a, ref, screen_attempts=10, gate=False) == (round(10 * train, 6), "")
    # no priced reading, no price: a package with train readings and none on dev, and one with none at all
    candidate_ref = screens["tool"].package.ref()
    assert sc.price(a, candidate_ref, screen_attempts=5, gate=False)[0] > 0
    for package_ref in (candidate_ref, "package:sha256:" + "0" * 64):
        usd, why = sc.price(a, package_ref, screen_attempts=5)
        assert usd is None and "cannot be priced" in why


def test_gate_attempts_come_from_the_frozen_template(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "tool", baseline=False, freeze=False,
                       gate={"confirmations": 1, "repeats": 2})
    a = s.a
    assert sc.template(a) is None and sc.gate_attempts(a) == 0
    with pytest.raises(ValueError, match="no frozen gate template"):
        sc.gate(a, run_arm=phases.run_arm, round_id="rsi-test/r0", package=a.package(),
                base_release=a.active_release(), account=a.account("gate-tests"))
    phases.freeze(a, out=quiet)
    plan = sc.template(a)
    assert (len(plan.task_ids), plan.repeats, plan.confirmations) == (3, 2, 1)
    assert sc.gate_attempts(a) == 3 * 2 * 2
    assert sc.price(a, a.active_release().package_ref, screen_attempts=1)[0] is None     # nothing read yet


def test_an_unpriced_round_stops_before_any_screen_is_paid(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "tool", baseline=False)       # no dev reading of W on record
    arms = []

    def run_arm(activity, **kw):
        rows = phases.run_arm(activity, **kw)
        arms.append((kw["role"], kw["split"], s.actor.calls))
        return rows
    got = run_round(s.a, 0, run_arm=run_arm, out=quiet)
    assert got.status == "stopped" and got.stop.startswith("unpriced: ") and "cannot be priced" in got.stop
    state = round_state(s.a, 0)
    assert tuple(state["steps"]) == ("measure", "evidence")         # stopped before it paid for diagnosis or R
    # the train reading is the only arm and its attempts the actor's last calls
    assert [(role, split) for role, split, _ in arms] == [("measure", "train")]
    assert s.actor.calls == arms[-1][2] > 0
    assert {run.role for run in s.a.results.runs().values()} == {"measure"}
    rd = Round(s.a, 0, run_arm=phases.run_arm)
    for kind in ("screen", "gate"):
        assert not (rd.child(kind).remaining().get("used") or {}).get("usd")
    assert [r["reason"] for r in ledger_rows(s.a, "stopped")] == [got.stop]
    # run again, the stopped round stays where it stopped and pays nothing
    calls = s.actor.calls
    assert run_round(s.a, 0, run_arm=run_arm, out=quiet).stop == got.stop
    assert s.actor.calls == calls and len(arms) == 1


def test_after_an_install_the_pooled_control_of_the_new_w_is_short(done):
    """The precondition of the next test: the installed W's dev readings are its gate's subject arm, which the pool
    does not take, so its next gate tops the control up on every dev task."""
    a, w1 = done.a, done.a.active_release()
    plan = sc.template(a)
    assert sc.control_pool(a, w1.package_ref, plan.task_ids) == []
    assert len(shortfall(plan, [])) == len(plan.task_ids) * plan.repeats == 3
    assert sc.cost_per_attempt(a, w1.package_ref, "dev") > 0 and sc.cost_per_attempt(a, w1.package_ref, "train") > 0


def test_the_gate_price_includes_the_pooled_controls_shortfall(done):
    a, w1 = done.a, done.a.active_release()
    plan = sc.template(a)
    short = shortfall(plan, sc.control_pool(a, w1.package_ref, plan.task_ids))
    dev = sc.cost_per_attempt(a, w1.package_ref, "dev")
    assert sc.price(a, w1.package_ref, screen_attempts=0) == (round((sc.gate_attempts(a) + len(short)) * dev, 6), "")


# ---------------------------------------------------------------------------- V4
def test_an_accepted_gate_registers_its_receipt_and_installs_under_its_round(done):
    a, base = done.a, done.base_release
    gated, screened = done.state["steps"]["gated"], done.state["steps"]["screened"]
    assert gated["verdict"] == "accepted" and gated["installed"] == done.result.installed
    receipt = a.objects.load(gated["receipt"], GateReceipt)
    assert a.releases.registered_receipt(gated["receipt"])
    assert [e.get("round") for e in a.events.read("evaluation_completed")
            if e.get("receipt") == gated["receipt"]] == ["rsi-test/r0"]
    # the frozen template with the candidate, the control and the parent filled in, and nothing else
    template = sc.template(a)
    plan = TaskEvalPlan.from_json(a.objects.get(receipt.plan_ref))
    assert (plan.candidate_ref, plan.control_ref, plan.parent_release) == \
        (screened["best_package"], base.package_ref, base.ref())
    blank = {"candidate_ref": "", "control_ref": "", "parent_release": ""}
    assert replace(plan, **blank) == replace(template, **blank)
    # the control is the pool of the current W's dev readings (the baseline's), nothing topped up
    gate_results = a.objects.get(receipt.results_ref)
    runs = a.results.runs()
    baseline_dev = sorted(r.attempt_id for r in a.results.all() if r.split == "dev" and runs[r.run].role == "base")
    assert gate_results["control_pooled"] is True
    assert sorted(x["attempt_id"] for x in gate_results["attempts"]["control"]) == baseline_dev
    assert not any(run.role == "control" for run in runs.values())
    # the subject arm is the candidate on dev
    subject = {x["attempt_id"] for x in gate_results["attempts"]["subject"]}
    assert len(subject) == len(plan.task_ids)
    assert {(runs[r.run].role, runs[r.run].split, runs[r.run].candidate, runs[r.run].package_ref)
            for r in a.results.all() if r.attempt_id in subject} == \
        {("subject", "dev", "rsi-test/r0:gate", screened["best_package"])}
    # installed by CAS from the base, under install:<round> with "/" made "_"
    installed = a.releases.load(gated["installed"])
    assert a.releases.active_ref() == gated["installed"]
    assert (installed.parent_ref, installed.package_ref, installed.receipt_ref, installed.reason) == \
        (base.ref(), screened["best_package"], gated["receipt"], "gate")
    op = "install:rsi-test_r0"
    assert a.releases.pointer()["last_operation_id"] == op
    install = a.releases.recover(op)
    assert isinstance(install, InstallReceipt)
    assert (install.status, install.target_release, install.expected_parent) == \
        ("installed", gated["installed"], base.ref())
    assert gated["install"] == "installed"
    assert [r["release"] for r in ledger_rows(a, "installed")] == [gated["installed"]]


def test_a_rejected_gate_registers_its_receipt_and_installs_nothing(world):
    a = world.a
    _files, package = candidate(world, hook=True)          # a hook that never acts: dev reads as the current W
    pointer = a.releases.pointer()
    got = sc.gate(a, run_arm=phases.run_arm, round_id="rsi-test/r8", package=package,
                  base_release=world.base_release, account=a.account("gate-tests"))
    assert got["verdict"] == "rejected" and got["installed"] is None and "install" not in got
    assert a.releases.registered_receipt(got["receipt"])
    receipt = a.objects.load(got["receipt"], GateReceipt)
    assert (receipt.subject_ref, receipt.control_ref, receipt.parent_release) == \
        (package.ref(), world.base_release.package_ref, world.base_release.ref())
    assert a.releases.pointer() == pointer
    assert a.releases.recover("install:rsi-test_r8") is None


# ---------------------------------------------------------------------------- the context (stage-4 pre-flight)
class Replies:
    """A researcher answering fixed ChatTurns in order (a stop and usage of the test's choosing)."""

    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    def chat(self, system, messages, specs, **_kw):
        self.seen.append([dict(m) for m in messages])
        return self.turns.pop(0)


def calls(*pairs, usage=None) -> ChatTurn:
    return ChatTurn("", [ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(pairs)], "tool_use", usage=usage or {})


def test_read_file_counts_toward_the_sessions_reads_and_is_capped_per_read(world, tree):
    big = "y" * 5000
    script = Script([write(name="notes.txt", text=big)],
                    [("read_file", {"path": "candidates/notify/notes.txt"})],
                    [("read_file", {"path": "candidates/notify/notes.txt", "limit": -3})],
                    [("read_file", {"path": "candidates/notify/notes.txt"})], [finish(decl())])
    cs = coding(world, tree, script, session_max_read_bytes=3000, session_max_read_total=3500)
    cs.start()
    outs = results(cs, "read_file")
    assert outs[0]["output"].startswith("y" * 3000) and not outs[0]["is_error"]       # one read: at most 3000
    assert outs[1]["is_error"] and "positive" in outs[1]["output"]
    left = 3500 - len(outs[0]["output"].encode("utf-8"))           # the first page and its "follows" line
    assert not outs[2]["is_error"] and outs[2]["output"].split("\n[...")[0] == "y" * left
    assert cs.read_bytes >= 3500


def test_a_reply_cut_at_the_output_limit_is_named_and_two_in_a_row_end_the_session(world, tree):
    cut = ChatTurn("", [], "max_tokens", usage={})
    cs = coding(world, tree, Replies(cut, calls(write()), cut, cut))
    got = cs.start()
    users = [m["content"] for m in cs.messages if m.get("role") == "user"][1:]
    assert users[0].startswith("Your last reply was cut at the output limit") and NUDGE not in users
    assert got.ended == "two replies in a row were cut at the output limit" and cs.turns == 4


def test_past_the_compact_threshold_old_outputs_and_writes_are_elided_and_reads_given_back(world, tree):
    """The context is measured from what the next call sends: past the threshold the older written texts and read
    outputs are elided, the newest kept; an elided read gives its bytes back, so reading it again is not refused;
    between compactions each call extends the one before."""
    long = "z" * 2000
    turns = [calls(write(name=f"f{i}.txt", text=long)) for i in range(3)]
    turns += [calls(("read_file", {"path": "candidates/notify/f0.txt"}))]
    turns += [calls(write(name=f"g{i}.txt", text=long)) for i in range(3)]
    turns += [calls(("list_files", {"path": "inputs"})), calls(("read_file", {"path": "candidates/notify/f0.txt"})),
              calls(("list_files", {"path": "inputs"})), calls(finish(decl()))]
    r = Replies(*turns)
    cs = coding(world, tree, r, session_compact_tokens=3500, session_max_read_total=2500)
    cs.start()
    assert cs.compactions >= 1
    writes = [c.args.get("text") for m in cs.messages if m.get("role") == "assistant" for c in m["tool_calls"]
              if c.name == "write_file"]
    assert writes[0].startswith("[elided: 2000 characters")
    reads = results(cs, "read_file")
    assert reads[0]["output"].startswith("[elided") and not reads[1]["is_error"]       # read again, not refused
    assert reads[1]["output"].startswith("z" * 100)
    assert cs._context_tokens() <= 3500                                     # the session stays under its threshold
    # a call that does not extend the one before is a compaction's, and only those
    breaks = sum(1 for a, b in zip(r.seen, r.seen[1:]) if b[:len(a)] != a)
    assert breaks == cs.compactions


def test_a_session_state_restores_into_a_new_session(world, tree):
    script = Script([write()], [("read_file", {"path": "candidates/notify/tools/x.py"})])
    cs = coding(world, tree, script, session_max_turns=2)
    cs.start()
    state = json.loads(json.dumps(cs.state()))                  # through JSON, as the object store keeps it
    other = coding(world, tree, Script([finish(decl())]), session_max_turns=3)
    other.restore(state)
    assert (other.turns, other.edits, other.dirs, other.ended) == (2, 1, ["notify"], cs.ended)
    other.ended = ""
    got = other.resume()
    assert [d.dir for d in got.declarations] == ["notify"] and other.turns == 3
    assert other.messages[:len(cs.messages)] == cs.messages


def test_a_saved_candidate_directory_is_restored_exactly(world, tree):
    """What R left in candidates/<dir> — a file of the package deleted, another changed, a new one — comes back as it
    was; a step the tree refuses stops the restore with an error instead of a partial directory."""
    base = {p: t for p, t in world.base.files.items() if p != MEMORY_PATH}
    gone = sorted(p for p in base if p.startswith("prompt/") or p.endswith(".md"))[0]
    saved = {p: t for p, t in base.items() if p != gone}
    saved["tools/new_tool.py"] = "X = 2\n"
    changed = next(p for p in sorted(saved) if p.endswith(".py") and p != "tools/new_tool.py")
    saved[changed] = saved[changed] + "\n# changed\n"
    fake = SimpleNamespace(package=world.base)
    Round._restore_dir(fake, tree, "back", saved)
    got, error = tree.fetch("back")
    assert error == "" and got == saved
    with pytest.raises(RuntimeError, match="cannot be restored"):
        Round._restore_dir(fake, tree, "bad", {**saved, "../escape.py": "x"})
