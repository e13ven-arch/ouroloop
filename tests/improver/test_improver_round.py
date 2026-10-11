"""One round of the improver end to end (docs/design/IMPROVER.md §3 the round, §9 V3 and V4 with the install, §11 the
invariants, §13 the end-to-end, paid-safety and operation-id items; ACTIVITY's round and cycle) on the fake upstream's
rsi domain: the forgetful actor, the scripted R, missed_action mechanizable, the baseline run and the gate plan frozen
(tests/improver/rounds.py).

Covered:
- R writes a tool: it climbs V0, V1, V2a; V3 fixes both notify tasks where the tool was called; V4 accepts, the receipt
  is registered and the release installed; the ledger credits the tool; the playbook keeps only the cited lesson;
  every step is on record in rounds/r0/round.json; the RoundResult says so;
- R writes a hook (the tool tried once before without a fix): V0, V1, V2b, screened and installed;
- R writes nothing: `no_code_candidate` on record and the round stops `no_candidate`; under the cycle the next round's
  code slot is doubled and the cycle stops after `schedule.patience` rounds without an install;
- the memory on, R writes nothing: the library's snapshot is a fixed candidate, passes V0 and is screened (V3);
- a gate that rejects: nothing installed, the rejection on record;
- money: every research call and attempt reserved in the round's account and its step children, the round's spend is
  what the book holds for it; an interrupted round resumes and pays only for what it had not done (killed at a screen,
  at the playbook, or while the credit step measures the new W on the moved line); a done or stopped round run again
  pays nothing; no round without the frozen gate plan; screens that cannot be priced, or do not fit what the round has
  left, do not start; a round the budget refuses stops on record and the cycle stops there;
- the install's operation id is `install:<round_id>`, never an Execution operation's id, and replaying it is a no-op;
- a part of train held out: whole families, the same for every round; R's inputs name only the studied tasks (the
  held-out ones are counts), its candidate predicts only those, and V3 screens on the held-out tasks of the cause and a
  held-out passing sample; a cause without enough studied tasks or without a held-out one is not assigned.

Three cases came from bugs found here, now fixed: a fixed candidate lost when the verified step was interrupted, the
gate priced without the control's top-up, the archive missing the screen and the receipt.
"""
from __future__ import annotations

import dataclasses
import json
import re
from collections import Counter

import pytest
import rsi
from rounds import ledger_rows, quiet, round_activity, round_state
from scripted_r import TOOL_ENTRY

from ouroloop.activity import phases, presets
from ouroloop.activity.checks import startup_checks
from ouroloop.contracts import short
from ouroloop.evaluate.budget import operation_id as execution_operation_id
from ouroloop.evaluate.release import GateReceipt
from ouroloop.harness.package import load
from ouroloop.evaluate.attempt import task_hash
from ouroloop.improver.allocate import Assignment
from ouroloop.improver.archive import Archive
from ouroloop.improver.declaration import Declaration
from ouroloop.improver.evidence import Evidence
from ouroloop.improver.round import STEPS, Round
from ouroloop.improver.screen import ScreenResult
from ouroloop.improver.verify import Verdict

ROUND = "rsi-test/r0"
ACCOUNT = "rsi-test:round-0"
INSTALL = "install:rsi-test_r0"
NOTIFY = ("rsi.notify_after_update", "rsi.notify_after_update_variant")
PASSING = tuple(t for t in rsi.TRAIN if t not in NOTIFY)
FREE = STEPS[:STEPS.index("screened")]             # measure .. verified: no attempt is paid for before the screens


class Crash(BaseException):
    """The process dying mid-round: nothing in the round may swallow it."""


class Actor(rsi.BusinessWorker):
    """The forgetful actor, which can be told to die at its first call on one of a set of tasks."""

    def __init__(self):
        super().__init__(forget_notify=True)
        self.crash_on: tuple = ()

    def chat(self, system, messages, specs, **kw):
        task = next((m["content"] for m in messages if m.get("role") == "user"), "")
        if any(text in task for text in self.crash_on):
            self.crash_on = ()
            raise Crash("killed at an attempt's first call")
        return super().chat(system, messages, specs, **kw)


class DieAtPlaybook:
    """R, dying at its first playbook call."""

    def __init__(self, r):
        self.r, self.fired = r, False

    def chat(self, system, messages, specs, **kw):
        if not self.fired and (system or "").startswith("You keep the research playbook"):
            self.fired = True
            raise Crash("killed at the playbook")
        return self.r.chat(system, messages, specs, **kw)


def book(a) -> dict:
    return json.loads((a.root / "rsi" / "budget" / "book.json").read_text(encoding="utf-8"))


def paid(s) -> dict:
    """Everything that costs money or leaves a trace of it: the actor's and R's calls, the book's reservations."""
    return {"actor": s.actor.calls, "r": list(s.r.calls), "reservations": book(s.a)["reservations"]}


def triggers(a, tasks) -> tuple:
    return tuple(a.suite.tasks[t].trigger for t in tasks)


def task_of(seen: dict) -> str:
    return next((m["content"] for m in seen["messages"] if m.get("role") == "user"), "")


def played(s, index: int = 0):
    """Run round `index` and keep what it paid and printed."""
    s.base = s.a.active_release()
    s.lines, s.before, s.seen_before = [], paid(s), len(s.actor.seen)
    s.result = phases.round_(s.a, index, out=s.lines.append)
    s.after = paid(s)
    return s


def new_reservations(before: dict, after: dict) -> dict:
    return {k: v for k, v in after["reservations"].items() if k not in before["reservations"]}


def settled_by_scope(reservations: dict) -> Counter:
    return Counter(r["scope_id"] for r in reservations.values() if r["status"] == "settled")


def stages(a, round_id: str | None = None) -> list[str]:
    return [r["stage"] for r in ledger_rows(a) if round_id is None or r.get("round_id") == round_id]


def screen_of(a, index: int = 0) -> ScreenResult:
    (row,) = round_state(a, index)["steps"]["screened"]["screens"]
    return ScreenResult.from_json(a.objects.get(row["screen"]))


def results_of_role(a, role: str) -> list:
    runs = a.results.runs()
    return [r for r in a.results.all() if runs[r.run].role == role]


# ---------------------------------------------------------------------------- one round per mode
@pytest.fixture(scope="module")
def tool(tmp_path_factory, fake_upstream):
    """R writes the notify tool: the round every money test is measured against."""
    return played(round_activity(tmp_path_factory.mktemp("tool"), fake_upstream, "tool", actor=Actor()))


@pytest.fixture(scope="module")
def hook(tmp_path_factory, fake_upstream):
    return played(round_activity(tmp_path_factory.mktemp("hook"), fake_upstream, "hook", tried={"tool": 1}))


@pytest.fixture(scope="module")
def idle(tmp_path_factory, fake_upstream):
    """R writes nothing: round 0 alone, then the cycle (which finds round 0 stopped and runs round 1)."""
    s = played(round_activity(tmp_path_factory.mktemp("idle"), fake_upstream, "none"))
    s.cycle = phases.cycle(s.a, out=quiet)
    s.end = paid(s)
    return s


@pytest.fixture(scope="module")
def memory(tmp_path_factory, fake_upstream):
    """The memory on (the library phase run after the baseline) and R writing nothing."""
    return played(round_activity(tmp_path_factory.mktemp("memory"), fake_upstream, "none", memory=True))


@pytest.fixture(scope="module")
def rejected(tmp_path_factory, fake_upstream):
    """The tool candidate under a gate that asks for more gain than three dev tasks can show."""
    return played(round_activity(tmp_path_factory.mktemp("rejected"), fake_upstream, "tool", gate={"min_gain": 0.9}))


# ---------------------------------------------------------------------------- end to end: a tool
def test_a_tool_candidate_climbs_v0_v1_v2a_is_screened_on_its_footprint_and_installs(tool):
    a, r = tool.a, tool.result
    assert (r.round_id, r.status, r.causes, r.assignments, r.candidates, r.stop) == (ROUND, "done", 1, 1, 1, None)
    assert r.ladder == {"V0": 1, "V1": 1, "V2a": 1} and r.screened == "notify"
    state = round_state(a, 0)
    (item,) = state["steps"]["verified"]["passed"]
    decl = Declaration.from_json(a.objects.get(item["declaration"]))
    verdict = Verdict.from_json(a.objects.get(item["verdict"]))
    assert (decl.dir, decl.kind, decl.site, decl.cause_id) == ("notify", "tool", TOOL_ENTRY["name"],
                                                               f"{ROUND}:missed_action")
    assert sorted(decl.predicted_affected) == list(NOTIFY)
    assert list(verdict.steps) == ["V0", "V1", "V2a"] and verdict.passed()
    # V3: both notify tasks fixed, each where the tool was called; nothing on the passing sample broke
    res = screen_of(a)
    assert res.outcome == "screened" and res.candidate == "r0:notify"
    assert sorted(res.failing) == list(NOTIFY) and sorted(res.sample) == sorted(PASSING)
    assert sorted(res.fixed) == list(NOTIFY) and list(res.unconditioned) == [] and list(res.regressed) == []
    assert res.net_flips == 2 and res.thin is True          # 2 failing tasks, fewer than thin_screen_tasks
    rows = {x.task_id: x for x in results_of_role(a, "screen")}
    assert sorted(rows) == sorted(rsi.TRAIN)
    evidence, tool_component = Evidence(a), {"kind": "tool", "name": TOOL_ENTRY["name"]}
    assert all(rows[t].success and evidence.footprint(rows[t], tool_component) for t in NOTIFY)
    # V4: accepted, the receipt registered for this round, the release installed on top of the round's W
    gate = r.gate
    assert gate["verdict"] == "accepted" and gate["install"] == "installed" and r.installed == gate["installed"]
    assert a.releases.active_ref() == r.installed != tool.base.ref()
    new = a.releases.load(r.installed)
    assert (new.parent_ref, new.receipt_ref, new.reason) == (tool.base.ref(), gate["receipt"], "gate")
    assert new.package_ref == res.package_ref == state["steps"]["screened"]["best_package"]
    receipt = GateReceipt.from_json(a.objects.get(gate["receipt"]))
    assert receipt.verdict == "accepted" and receipt.subject_ref == res.package_ref
    assert receipt.control_ref == tool.base.package_ref and receipt.parent_release == tool.base.ref()
    assert a.releases.registered_receipt(gate["receipt"])
    assert [e.get("round") for e in a.events.read("evaluation_completed") if e.get("receipt") == gate["receipt"]] \
        == [ROUND]
    installed = load(a.objects, new.package_ref)
    assert "tools/post_channel_message.py" in installed.files
    assert TOOL_ENTRY["name"] in [t["name"] for t in json.loads(installed.files["manifest.json"])["tools"]]
    assert tool.lines[-1] == (f"round      {ROUND}: the gate says accepted (gain {gate['gain']}); installed "
                              f"{short(r.installed)}")


def test_the_ledger_credits_the_tool_and_the_playbook_keeps_only_the_cited_lesson(tool):
    a, r = tool.a, tool.result
    rows = ledger_rows(a)
    assert [x["stage"] for x in rows] == ["measured", "diagnosed", "allocated", "declared", "verified", "screened",
                                         "gated", "installed", "credited", "playbook"]
    assert {x["round_id"] for x in rows} == {ROUND} and {x["line"] for x in rows} == {a.domain}
    by = {x["stage"]: x for x in rows}
    assert by["measured"]["passed"] == len(PASSING) and by["measured"]["steps"] == len(rsi.TRAIN)
    assert by["diagnosed"]["causes"] == {"missed_action": 2} and by["diagnosed"]["tasks"] == list(NOTIFY)
    assert by["allocated"]["assignments"] == [{"cause": "missed_action", "kind": "tool", "slots": 1,
                                               "exploration": True}]
    assert by["verified"]["passed"] is True and by["screened"]["fixed"] == list(NOTIFY)
    assert (by["gated"]["verdict"], by["installed"]["release"]) == ("accepted", r.installed)
    assert (by["credited"]["release"], by["credited"]["component"], by["credited"]["credit"]) == \
        (r.installed, ["tool:post_channel_message"], {"tool:post_channel_message": 2.0})
    assert sorted(round_state(a, 0)["steps"]["credited"]["credit"]["tool:post_channel_message"]["fixed"]) == \
        list(NOTIFY)
    # the playbook: the lesson citing a ledger row stays, the uncited one is dropped
    text = (a.root / "ledger" / "playbook.md").read_text(encoding="utf-8")
    assert text == "- A tool that posts the requested message fixed missed_action [L1]\n"
    assert round_state(a, 0)["steps"]["playbook"] == {"lessons": 1, "dropped": ["- An uncited lesson"]}
    assert by["playbook"]["status"] == "1 lessons"


def test_every_step_of_the_round_is_on_record_in_round_json(tool):
    a = tool.a
    state = round_state(a, 0)
    assert (state["round_id"], state["index"], state["base_release"], state["status"]) == \
        (ROUND, 0, tool.base.ref(), "done")
    assert list(state["steps"]) == list(STEPS)
    steps = state["steps"]
    assert steps["measure"] == {"package": tool.base.package_ref, "measured": 0, "tasks": len(rsi.TRAIN),
                                "passed": len(PASSING)}      # round 0 reads the baseline's train reading
    assert steps["evidence"]["failed_tasks"] == list(NOTIFY) and steps["evidence"]["not_diagnosed"] == []
    assert len(steps["diagnosed"]["causes"]) == 1 and len(steps["diagnosed"]["cases"]) == 2
    assert [c["dir"] for c in steps["implemented"]["candidates"]] == ["notify"]
    assert steps["verified"]["fixed"] == [] and steps["screened"]["best"] == "notify"
    assert steps["gated"]["installed"] == steps["credited"]["release"] == tool.result.installed
    assert (a.root / "rounds" / "r0" / "candidates" / "notify" / "files.json").exists()


# ---------------------------------------------------------------------------- end to end: a hook
def test_a_hook_candidate_climbs_v0_v1_v2b_and_installs(hook):
    a, r = hook.a, hook.result
    (allocated,) = [x for x in ledger_rows(a) if x["stage"] == "allocated"]
    assert allocated["assignments"] == [{"cause": "missed_action", "kind": "hook", "slots": 1, "exploration": True}]
    assert (r.status, r.candidates, r.ladder, r.screened, r.stop) == ("done", 1, {"V0": 1, "V1": 1, "V2b": 1},
                                                                     "notify", None)
    (item,) = round_state(a, 0)["steps"]["verified"]["passed"]
    decl = Declaration.from_json(a.objects.get(item["declaration"]))
    assert (decl.kind, decl.site) == ("hook", "on_finish")
    assert list(Verdict.from_json(a.objects.get(item["verdict"])).steps) == ["V0", "V1", "V2b"]
    res = screen_of(a)
    assert res.outcome == "screened" and sorted(res.fixed) == list(NOTIFY) and list(res.regressed) == []
    assert r.gate["verdict"] == "accepted" and r.installed == a.releases.active_ref() != hook.base.ref()
    hooks = json.loads(load(a.objects, a.releases.load(r.installed).package_ref).files["manifest.json"])["hooks"]
    assert {"name": "remind_ops", "site": "on_finish"} in [{"name": h["name"], "site": h["site"]} for h in hooks]
    assert stages(a)[-3:] == ["installed", "credited", "playbook"]


# ---------------------------------------------------------------------------- end to end: no code candidate
def test_no_code_candidate_is_on_record_and_the_round_stops_no_candidate(idle):
    a, r = idle.a, idle.result
    assert (r.status, r.stop) == ("stopped", "no_candidate: no candidate passed its free rungs")
    assert (r.causes, r.assignments, r.candidates, r.ladder, r.screened, r.gate, r.installed) == \
        (1, 1, 0, {}, None, None, None)
    rows = [x for x in ledger_rows(a) if x["round_id"] == ROUND]
    assert [x["stage"] for x in rows] == ["measured", "diagnosed", "allocated", "unused", "no_code_candidate",
                                          "stopped"]
    assert (rows[3]["cause"], rows[3]["kind"]) == ("missed_action", "tool")          # the slot R did not use
    assert rows[4]["cause"] == "missed_action" and rows[5]["reason"] == r.stop
    state = round_state(a, 0)
    assert (state["status"], state["stop"]) == ("stopped", r.stop) and tuple(state["steps"]) == FREE
    # it paid for the diagnosis and R's one turn, nothing else
    assert idle.after["r"][len(idle.before["r"]):] == ["diagnose", "diagnose", "session"]
    assert idle.after["actor"] == idle.before["actor"]
    assert settled_by_scope(new_reservations(idle.before, idle.after)) == {f"{ACCOUNT}:diagnose": 2,
                                                                           f"{ACCOUNT}:session": 1}


def test_under_the_cycle_the_code_slot_doubles_and_the_cycle_stops_after_patience_rounds(idle):
    a, cycle = idle.a, idle.cycle
    patience = int(a.k["schedule"]["patience"])
    assert patience == 2 < int(a.k["schedule"]["rounds"])
    assert (cycle["status"], cycle["at"], cycle["reason"]) == ("stopped", f"round-{patience - 1}",
                                                               f"{patience} rounds without an install")
    assert [k for k in cycle["steps"] if k.startswith("round-")] == ["round-0", "round-1"]
    assert cycle["steps"]["round-0"] == {"status": "stopped", "installed": None, "stop": idle.result.stop,
                                         "gated": False, "carried": None}
    assert not (a.root / "rounds" / "r2").exists()
    # round 1 reads round 0's no_code_candidate (its code slot is doubled) and the tool slot R left unused (an
    # observation of no fix: the exploration moves on to the hook)
    (allocated,) = [x for x in ledger_rows(a) if x["stage"] == "allocated" and x["round_id"] == "rsi-test/r1"]
    (assignment,) = allocated["assignments"]
    assert (assignment["cause"], assignment["kind"], assignment["slots"]) == ("missed_action", "hook", 2)
    assert stages(a, "rsi-test/r1") == ["measured", "diagnosed", "allocated", "unused", "no_code_candidate",
                                        "stopped"]
    # round 0, already stopped, was not run again by the cycle: one stop on record, nothing paid for it
    assert stages(a, ROUND).count("stopped") == 1
    assert idle.end["r"][len(idle.after["r"]):] == ["session"]           # round 1's diagnosis is the cached one
    assert idle.end["actor"] == idle.after["actor"]


# ---------------------------------------------------------------------------- end to end: the library's snapshot
def test_with_the_memory_on_the_librarys_snapshot_is_a_fixed_candidate_that_reaches_v3(memory):
    a, r = memory.a, memory.result
    (event,) = a.events.read("library_snapshot")
    assert event["outcome"] == "candidate"
    (declared,) = [x for x in ledger_rows(a) if x["stage"] == "declared"]
    assert (declared["source"], declared["kind"], declared["dir"], declared["cause"]) == \
        (f"snapshot:{event['snapshot_digest']}", "memory", "memory-snapshot", "memory")
    (verified,) = [x for x in ledger_rows(a) if x["stage"] == "verified"]
    assert verified["dir"] == "memory-snapshot" and verified["passed"] is True
    assert (r.candidates, r.ladder) == (1, {"V0": 1})
    state = round_state(a, 0)
    assert state["steps"]["implemented"]["candidates"] == [] and state["steps"]["verified"]["fixed"] == \
        ["memory-snapshot"]
    # V3: the snapshot was screened and its result is on record
    (row,) = state["steps"]["screened"]["screens"]
    res = screen_of(a)
    assert row["dir"] == "memory-snapshot" and res.outcome == "screened" and res.candidate == "r0:memory-snapshot"
    assert res.package_ref == row["package"] and sorted(res.failing) == list(NOTIFY)
    assert load(a.objects, res.package_ref).memory_text == load(a.objects, event["package_ref"]).memory_text != ""
    (screened,) = [x for x in ledger_rows(a) if x["stage"] == "screened"]
    assert screened["dir"] == "memory-snapshot" and list(screened["fixed"]) == list(res.fixed)
    assert {x.task_id for x in results_of_role(a, "screen")} == set(res.tasks)
    # the snapshot is not code: the mechanizable cause still has no code candidate past V2
    assert [x["cause"] for x in ledger_rows(a) if x["stage"] == "no_code_candidate"] == ["missed_action"]


# ---------------------------------------------------------------------------- a gate that rejects
def test_a_gate_that_rejects_installs_nothing_and_the_rejection_is_on_record(rejected):
    a, r = rejected.a, rejected.result
    assert (r.status, r.screened, r.gate["verdict"], r.installed) == ("done", "notify", "rejected", None)
    assert a.releases.active_ref() == rejected.base.ref()
    assert stages(a)[-4:] == ["screened", "gated", "rejected", "playbook"]
    assert "installed" not in stages(a) and "credited" not in stages(a)
    (row,) = [x for x in ledger_rows(a) if x["stage"] == "rejected"]
    assert (row["dir"], row["verdict"]) == ("notify", "rejected")
    assert a.releases.registered_receipt(r.gate["receipt"])
    assert "credited" not in round_state(a, 0)["steps"] and round_state(a, 0)["status"] == "done"
    assert not (a.root / "rsi" / "operations").exists() or not list((a.root / "rsi" / "operations").iterdir())


# ---------------------------------------------------------------------------- money
def test_every_paid_call_of_the_round_is_reserved_in_its_account_and_the_spend_is_the_books(tool):
    a, r = tool.a, tool.result
    data = book(a)
    share = float(a.k["budget"]["usd"]) * float(a.k["improver"]["round_share"])
    assert data["scopes"][ACCOUNT] == {"parent": a.scope_id, "limits": {"usd": share}, "allocations": {}}
    new = new_reservations(tool.before, tool.after)
    assert new and {x["status"] for x in new.values()} == {"settled"}
    research = Counter(tool.after["r"][len(tool.before["r"]):])
    seen = tool.actor.seen[tool.seen_before:]
    dev = triggers(a, rsi.DEV)
    on_dev = sum(1 for x in seen if any(t in task_of(x) for t in dev))
    assert research == {"diagnose": 2, "session": 4, "playbook": 1} and 0 < on_dev < len(seen)
    # each research call in its step's child, each screen and gate attempt's call in theirs; nothing elsewhere
    assert settled_by_scope(new) == {f"{ACCOUNT}:diagnose": research["diagnose"],
                                     f"{ACCOUNT}:session": research["session"],
                                     f"{ACCOUNT}:policy": research["playbook"],
                                     f"{ACCOUNT}:screen": len(seen) - on_dev, f"{ACCOUNT}:gate": on_dev}
    for scope in settled_by_scope(new):
        assert data["scopes"][scope]["parent"] == ACCOUNT
    for kind in ("diagnose", "session", "policy"):
        ops = [x["operation_id"] for x in new.values() if x["scope_id"] == f"{ACCOUNT}:{kind}"]
        assert ops and all(op.startswith(f"round-0:{kind}#") for op in ops)
    session = json.loads((a.root / "rounds" / "r0" / "session.json").read_text(encoding="utf-8"))
    assert session["turns"] == research["session"]
    # the round's spend is what the book settled under its account
    for dim in ("usd", "model_calls", "input_tokens", "output_tokens"):
        assert r.spend["used"][dim] == pytest.approx(sum(x["measured"].get(dim, 0.0) for x in new.values()))
    assert r.spend["limits"] == {"usd": share} and r.spend["used"]["model_calls"] == len(new)


def test_a_done_round_run_again_pays_nothing_and_returns_the_same_result(tool):
    a = tool.a
    state, ledger = (a.root / "rounds" / "r0" / "round.json").read_bytes(), ledger_rows(a)
    before = paid(tool)
    again = phases.round_(a, 0, out=quiet)
    assert again == tool.result
    assert paid(tool) == before and ledger_rows(a) == ledger
    assert (a.root / "rounds" / "r0" / "round.json").read_bytes() == state


def test_a_stopped_round_run_again_pays_nothing_and_writes_nothing(idle):
    a = idle.a
    state, ledger, before = round_state(a, 0), ledger_rows(a), paid(idle)
    again = phases.round_(a, 0, out=quiet)
    assert (again.status, again.stop) == (idle.result.status, idle.result.stop)
    assert paid(idle) == before and ledger_rows(a) == ledger and round_state(a, 0) == state


@pytest.mark.parametrize("where", ["screen", "playbook"])
def test_an_interrupted_round_resumes_and_pays_only_for_what_it_had_not_done(tmp_path, fake_upstream, tool, where):
    """Killed at the first screen attempt's first actor call (measure .. verified on record), or at the playbook (the
    install done, the line moved). Run again, the round continues: R and the actor are called as often as in the
    uninterrupted round, the ledger holds each step's rows once, and the only extra charge is the killed call's
    reservation, still open at its bound (a call that may have been made is not free)."""
    s = round_activity(tmp_path, fake_upstream, "tool", actor=Actor())
    start = paid(s)
    if where == "screen":
        s.actor.crash_on = triggers(s.a, rsi.TRAIN)
    else:
        s.research.r = DieAtPlaybook(s.r)
    with pytest.raises(Crash):
        phases.round_(s.a, 0, out=quiet)
    cut = round_state(s.a, 0)
    assert cut["status"] == "running"
    assert tuple(cut["steps"]) == (FREE if where == "screen" else STEPS[:-1])
    if where == "playbook":
        assert s.a.releases.active_ref() == cut["steps"]["gated"]["installed"] != cut["base_release"]
    result = phases.round_(s.a, 0, out=quiet)
    end = paid(s)
    # what it did is what the uninterrupted round did
    keep = ("round_id", "status", "causes", "assignments", "candidates", "ladder", "screened", "stop")
    assert {k: getattr(result, k) for k in keep} == {k: getattr(tool.result, k) for k in keep}
    assert {k: result.gate[k] for k in ("verdict", "gain", "fixed_count", "regressed_count", "install")} == \
        {k: tool.result.gate[k] for k in ("verdict", "gain", "fixed_count", "regressed_count", "install")}
    assert result.installed == s.a.releases.active_ref() and result.installed is not None
    assert stages(s.a) == stages(tool.a)
    # and it paid what the uninterrupted round paid, plus the killed call held at its bound
    assert end["r"][len(start["r"]):] == tool.after["r"][len(tool.before["r"]):]
    assert end["actor"] - start["actor"] == tool.after["actor"] - tool.before["actor"]
    new = new_reservations(start, end)
    assert settled_by_scope(new) == settled_by_scope(new_reservations(tool.before, tool.after))
    (open_,) = [x for x in new.values() if x["status"] != "settled"]
    assert (open_["scope_id"], open_["status"]) == (f"{ACCOUNT}:{'screen' if where == 'screen' else 'policy'}", "open")
    bound = s.a.pricing().bound()
    for dim in ("usd", "model_calls", "input_tokens", "output_tokens"):
        assert result.spend["used"][dim] == pytest.approx(tool.result.spend["used"][dim] + bound[dim])
    # and run once more, it pays nothing
    assert phases.round_(s.a, 0, out=quiet) == result and paid(s) == end


def test_a_round_resumed_after_its_install_measures_the_new_w_once_and_credits_against_the_w_it_started_on(
        tmp_path, fake_upstream):
    """A passing sample of one leaves two train tasks the screen did not read: the credit step measures the new W on
    them (role measure, in the round's measure account) and is killed at its first attempt. Run again on the moved
    line, the round measures them once and credits the tool against the W it started on; the next round has nothing
    left to measure."""
    s = round_activity(tmp_path, fake_upstream, "tool", actor=Actor(), improver={"screen_passing_sample": 1})
    base = s.a.active_release()

    def out(line: str) -> None:
        if "the gate says accepted" in line:
            s.actor.crash_on = triggers(s.a, rsi.TRAIN)
    with pytest.raises(Crash):
        phases.round_(s.a, 0, out=out)
    cut = round_state(s.a, 0)
    assert tuple(cut["steps"]) == STEPS[:STEPS.index("credited")] and cut["steps"]["gated"]["installed"]
    unread = set(rsi.TRAIN) - set(screen_of(s.a).tasks)
    assert len(unread) == 2
    result = phases.round_(s.a, 0, out=quiet)
    assert result.status == "done" and result.installed == s.a.releases.active_ref() != base.ref()
    new = s.a.releases.load(result.installed)
    measured = results_of_role(s.a, "measure")
    runs = s.a.results.runs()
    assert sorted(x.task_id for x in measured) == sorted(unread)                      # each once
    assert {runs[x.run].candidate for x in measured} == {f"train:{short(new.ref())}:r0"}
    assert {runs[x.run].package_ref for x in measured} == {new.package_ref}
    reservations = book(s.a)["reservations"].values()
    assert sum(1 for x in reservations if x["scope_id"] == f"{ACCOUNT}:measure" and x["status"] == "settled") > 0
    credited = round_state(s.a, 0)["steps"]["credited"]
    assert credited["release"] == new.ref()
    assert credited["credit"]["tool:post_channel_message"]["credit"] == 2.0
    assert sorted(credited["credit"]["tool:post_channel_message"]["fixed"]) == list(NOTIFY)
    assert Evidence(s.a).missing(new.package_ref) == []
    assert stages(s.a).count("credited") == 1


def test_a_round_interrupted_after_the_snapshot_was_declared_still_screens_it_on_resume(tmp_path, fake_upstream,
                                                                                        memory):
    """Killed as the verified step reports (its ledger rows written, its output not yet in round.json)."""
    s = round_activity(tmp_path, fake_upstream, "none", memory=True)

    def dying(line: str) -> None:
        if "passed their free rungs" in line:
            raise Crash(line)
    with pytest.raises(Crash):
        phases.round_(s.a, 0, out=dying)
    assert "verified" not in round_state(s.a, 0)["steps"]
    result = phases.round_(s.a, 0, out=quiet)
    assert (result.status, result.candidates, result.ladder, result.screened) == \
        (memory.result.status, memory.result.candidates, memory.result.ladder, "memory-snapshot")
    assert stages(s.a) == stages(memory.a)


def test_a_round_without_the_frozen_gate_plan_refuses_before_paying(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "tool", freeze=False)
    before = paid(s)
    (problem,) = startup_checks(s.a, phase="round")
    assert problem.startswith("no frozen gate plan")
    with pytest.raises(RuntimeError, match="the round phase cannot start: no frozen gate plan"):
        phases.round_(s.a, 0, out=quiet)
    assert paid(s) == before and ledger_rows(s.a) == [] and not (s.a.root / "rounds").exists()
    assert ACCOUNT not in book(s.a)["scopes"]
    phases.freeze(s.a, out=quiet)
    assert startup_checks(s.a, phase="round") == []


def test_screens_that_cannot_be_priced_are_not_started(tmp_path, fake_upstream):
    """No baseline: the round measures the current W's train itself (role measure, in its measure account), and with
    no dev reading on record the gate cannot be priced: the round stops before it pays for diagnosis or a session."""
    s = round_activity(tmp_path, fake_upstream, "tool", baseline=False)
    played(s)
    a, r = s.a, s.result
    assert (r.status, r.stop) == ("stopped", "unpriced: the current W has no priced train or dev reading on record: "
                                             "the screens cannot be priced")
    assert r.ladder == {} and r.screened is None and r.gate is None
    state = round_state(a, 0)
    assert tuple(state["steps"]) == ("measure", "evidence") and state["steps"]["measure"]["measured"] == len(rsi.TRAIN)
    runs = a.results.runs()
    rows = list(a.results.all())
    assert {runs[x.run].role for x in rows} == {"measure"} and {x.split for x in rows} == {"train"}
    assert {runs[x.run].candidate for x in rows} == {f"train:{short(s.base.ref())}:r0"}
    assert results_of_role(a, "screen") == []
    new = new_reservations(s.before, s.after)
    assert settled_by_scope(new) == {f"{ACCOUNT}:measure": s.after["actor"] - s.before["actor"]}
    assert "screened" not in stages(a) and ledger_rows(a)[-1]["reason"] == r.stop


def test_screens_that_do_not_fit_what_the_round_has_left_are_not_started(tmp_path, fake_upstream):
    """Someone else sets aside all but $0.01 while the candidate climbs its free rungs: the screens and the gate are
    priced from the current W's costs on record, do not fit, and nothing is screened."""
    s = round_activity(tmp_path, fake_upstream, "tool", actor=Actor())
    lines = []

    def out(line: str) -> None:
        lines.append(line)
        if "passed their free rungs" in line:
            left = s.a.account("other").remaining()["effective_remaining"]["usd"]
            s.a.account("other").hold("someone else's reservation", {"usd": round(left - 0.01, 6)})
    before = paid(s)
    result = phases.round_(s.a, 0, out=out)
    assert result.status == "stopped" and re.fullmatch(
        r"budget: the screens and the gate would need up to \$\d+\.\d\d \(\$\d+\.\d\d at the means on record, \+15%, and two "
        r"calls' bound\) and the round has \$0\.01", result.stop), result.stop
    assert lines[-1] == f"round      {ROUND} stopped: {result.stop}"
    after = paid(s)
    assert after["actor"] == before["actor"] and after["r"][len(before["r"]):] == ["diagnose", "diagnose"] + \
        ["session"] * 4
    assert results_of_role(s.a, "screen") == [] and tuple(round_state(s.a, 0)["steps"]) == FREE
    assert {x["scope_id"] for x in new_reservations(before, after).values()} == {
        f"{ACCOUNT}:diagnose", f"{ACCOUNT}:session", "rsi-test:other"}


def test_a_round_the_budget_refuses_stops_on_record_and_the_cycle_stops_there(tmp_path, fake_upstream):
    """Someone else sets aside all but $0.01 of the root before the round: the gate and the least screen do not fit,
    so the round stops `budget` on record before its first research call, and the cycle stops at that round."""
    s = round_activity(tmp_path, fake_upstream, "tool")
    left = s.a.account("other").remaining()["effective_remaining"]["usd"]
    s.a.account("other").hold("someone else's reservation", {"usd": round(left - 0.01, 6)})
    before = paid(s)
    cycle = phases.cycle(s.a, out=quiet)
    assert (cycle["status"], cycle["at"]) == ("stopped", "round-0") and "round-1" not in cycle["steps"]
    stop = cycle["steps"]["round-0"]["stop"]
    assert stop.startswith("budget: the gate, the least screen and the research would need up to $") and \
        cycle["steps"]["round-0"]["status"] == "stopped"
    assert paid(s) == before
    assert stages(s.a) == ["measured", "stopped"] and ledger_rows(s.a)[-1]["reason"] == stop
    assert round_state(s.a, 0)["stop"] == stop and tuple(round_state(s.a, 0)["steps"]) == ("measure", "evidence")


def test_the_round_prices_the_gates_control_top_up_before_paying(tmp_path, fake_upstream):
    """A baseline on two of the three dev tasks: the gate tops the pooled control up by one attempt, and the price the
    round checked before the screens holds that attempt too."""
    s = round_activity(tmp_path, fake_upstream, "tool", baseline=False, freeze=False)
    phases.baseline(s.a, limit=2, out=quiet)
    phases.freeze(s.a, out=quiet)
    played(s)
    a = s.a
    runs = a.results.runs()

    def costs(split: str, roles: tuple) -> list:
        return [x.cost_usd for x in a.results.all() if x.split == split and x.validity == "valid"
                and runs[x.run].package_ref == s.base.package_ref and runs[x.run].role in roles]
    train, dev = costs("train", ("base", "measure")), costs("dev", ("base",))
    gap = len(rsi.DEV) * int(a.k["gate"]["repeats"]) - len(dev)
    assert s.result.gate is not None and gap == 1 == len(results_of_role(a, "control"))   # the gate topped it up
    attempts = len(screen_of(a).tasks)
    gate = len(rsi.DEV) * int(a.k["gate"]["repeats"]) * (1 + int(a.k["gate"]["confirmations"])) + gap
    priced = round_state(a, 0)["steps"]["screened"]["usd_priced"]
    assert priced == pytest.approx(attempts * sum(train) / len(train) + gate * sum(dev) / len(dev))


# ---------------------------------------------------------------------------- the install's operation id
def test_the_install_operation_id_is_the_rounds_own_and_installing_again_changes_nothing(tool):
    a, r = tool.a, tool.result
    ops = a.root / "rsi" / "operations"
    assert sorted(p.name for p in ops.iterdir()) == [f"{INSTALL}.json"]
    record = json.loads((ops / f"{INSTALL}.json").read_text(encoding="utf-8"))
    assert (record["operation_id"], record["target_release"], record["expected_parent"], record["status"]) == \
        (INSTALL, r.installed, tool.base.ref(), "installed")
    assert a.releases.pointer()["last_operation_id"] == INSTALL
    assert [e["operation"] for e in a.events.read("installed") if e.get("release") == r.installed] == [INSTALL]
    # an Execution operation is op_<16 hex> of its experiment, step and inputs: never the install's, whatever they are
    for experiment, step, inputs in ((ROUND, "install", [r.gate["receipt"]]), (INSTALL, "install", []),
                                     ("rsi-test_r0", "gate", [r.installed]), (ROUND, "", [])):
        oid = execution_operation_id(experiment, step, inputs)
        assert re.fullmatch(r"op_[0-9a-f]{16}", oid) and oid != INSTALL
    assert "/" not in INSTALL and not INSTALL.startswith("op_")
    # the same operation again: the receipt on record, the pointer and the events untouched (CAS)
    pointer, events = a.releases.pointer(), list(a.events.read("installed"))
    new = a.releases.load(r.installed)
    again = a.releases.install(new, new.receipt_ref, expected_parent=tool.base.ref(), operation_id=INSTALL)
    assert (again.status, again.target_release, again.operation_id) == ("installed", r.installed, INSTALL)
    assert a.releases.pointer() == pointer and list(a.events.read("installed")) == events


def test_the_archive_keeps_the_screened_candidates_screen_and_its_receipt(tool):
    a, r = tool.a, tool.result
    archive = Archive(a.root, a.objects)
    (row,) = [x for x in archive.rows() if x["dir"] == "notify"]
    (screened,) = round_state(a, 0)["steps"]["screened"]["screens"]
    assert row["screen"] == screened["screen"] and row["receipt"] == r.gate["receipt"]
    (shown,) = archive.for_prompt(a.domain, "missed_action")
    assert shown["gated"] is True and shown["screen"]["outcome"] == "screened"
    assert sorted(shown["screen"]["fixed"]) == list(NOTIFY)


# ---------------------------------------------------------------------------- the held-out part of train
VARIANT, STUDY = NOTIFY[1], ("rsi.update_stage", NOTIFY[0], "rsi.notify_once")
HELD = (VARIANT, "rsi.policy_hold")


def test_the_held_out_part_is_whole_families_the_same_for_every_round(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "none", improver={"holdout_share": 0.4})
    study, held = Round(s.a, 0, run_arm=None, out=quiet).split()
    assert sorted(study + held) == sorted(rsi.TRAIN) and not set(study) & set(held)
    assert 0 < len(held) <= round(len(rsi.TRAIN) * 0.4) and study                # never more than its share
    families = lambda tasks: {rsi.FAMILIES[t] for t in tasks}                     # noqa: E731
    assert not families(study) & families(held)
    assert Round(s.a, 1, run_arm=None, out=quiet).split() == (study, held)
    # seed 0 ranks the notify family (2 tasks) first: a share of 1 task skips it rather than overshoot
    small = round_activity(tmp_path / "small", fake_upstream, "none", improver={"holdout_share": 0.2})
    assert len(Round(small.a, 0, run_arm=None, out=quiet).split()[1]) == 1
    nothing = round_activity(tmp_path / "none", fake_upstream, "none")
    assert Round(nothing.a, 0, run_arm=None, out=quiet).split() == (tuple(nothing.a.splits.ids("train")), ())


def test_a_cause_without_enough_studied_tasks_or_a_held_out_one_is_not_assigned(tmp_path, fake_upstream):
    """The notify tasks are one family, so the one cause lies wholly on one side: nothing is assigned, R's session is
    not paid, the round stops without a candidate."""
    s = played(round_activity(tmp_path, fake_upstream, "tool", actor=Actor(), improver={"holdout_share": 0.4}))
    (allocated,) = [x for x in ledger_rows(s.a) if x["stage"] == "allocated"]
    assert allocated["assignments"] == [] and "session" not in s.r.calls
    (why,) = allocated["not_assigned"]
    assert why["cause"] == "missed_action" and sorted((why["studied"], why["held_out"])) == [0, 2]
    assert s.result.status == "stopped" and s.result.stop.startswith("no_candidate")


@pytest.fixture(scope="module")
def held_out(tmp_path_factory, fake_upstream):
    """One notify task studied, the other held out (a split by hand: the real one keeps a family whole, and the
    miniature domain has one family per cause), V2a and V2b asking for the one studied task."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Round, "split", lambda self: (STUDY, HELD))
        for name in ("toolcheck_min_pass", "replay_min_triggers"):
            mp.setitem(presets.PRESETS[rsi.TEST_PRESET]["improver"], name, 1)
        s = played(round_activity(tmp_path_factory.mktemp("held"), fake_upstream, "tool", actor=Actor()))
        s.inputs, s.readable = (lambda r: r.inputs(r.assignments()))(Round(s.a, 0, run_arm=None, out=quiet))
        s.sessions = {(x.evidence or x.attempt_id): x.task_id for x in s.a.results.all()}
        return s


def test_r_reads_only_the_studied_tasks_and_its_candidate_predicts_only_those(held_out):
    a, inputs = held_out.a, held_out.inputs
    (cause,) = json.loads(inputs["causes.json"])
    assert (cause["tasks"], cause["held_out_tasks"]) == ([NOTIFY[0]], 1)
    assert [c["task_id"] for c in cause["cases"]] == [NOTIFY[0]]
    assert not [(name, t) for name, text in inputs.items() for t in HELD if t in text]
    assert {held_out.sessions[x] for x in held_out.readable if x} <= set(STUDY)
    (item,) = round_state(a, 0)["steps"]["verified"]["passed"]
    assert list(Declaration.from_json(a.objects.get(item["declaration"])).predicted_affected) == [NOTIFY[0]]
    assert Round(a, 0, run_arm=None, out=quiet).vctx().cause_ids == (f"{ROUND}:missed_action",)   # V0 checks it
    # what R reads of the screen next round: the held-out fix as a count
    (screened,) = [x for x in json.loads(inputs["ledger.json"]) if x["stage"] == "screened"]
    assert "fixed" not in screened and screened["fixed_held_out"] == 1


def test_v3_screens_on_the_held_out_tasks_of_the_cause_and_a_held_out_passing_sample(held_out):
    a, res = held_out.a, screen_of(held_out.a)
    assert (list(res.failing), list(res.sample), list(res.fixed)) == ([VARIANT], ["rsi.policy_hold"], [VARIANT])
    assert res.net_flips == 1 and res.outcome == "screened"
    assert sorted(x.task_id for x in results_of_role(a, "screen")) == sorted(HELD)


def test_a_memory_entry_distilled_from_a_held_out_task_is_not_shown_to_r(held_out, monkeypatch):
    monkeypatch.setattr(Round, "split", lambda self: (STUDY, HELD))
    r = Round(held_out.a, 0, run_arm=None, out=quiet)
    hashed = {t: task_hash(held_out.a.suite.tasks[t]) for t in rsi.TRAIN}
    entries = ({"entry": "seen", "distilled_from": [hashed[NOTIFY[0]], hashed["rsi.update_stage"]]},
               {"entry": "held", "distilled_from": [hashed[NOTIFY[0]], hashed[VARIANT]]})
    (cause,) = r.causes()
    monkeypatch.setattr(Round, "causes", lambda self: [dataclasses.replace(cause, memory_entries=entries)])
    inputs, _ = r.inputs(r.assignments())
    assert [e["entry"] for e in json.loads(inputs["causes.json"])[0]["memory_entries"]] == ["seen"]


def test_an_empty_assignment_is_unused_only_while_r_had_candidates_to_spare(held_out):
    r = Round(held_out.a, 0, run_arm=None, out=quiet)
    (real,) = r.assignments()
    other = Assignment(cause_id=f"{ROUND}:wrong_target", cause="wrong_target", kind="hook")
    out = round_state(held_out.a, 0)["steps"]["implemented"]["candidates"]
    r.s = dataclasses.replace(r.s, k=1)              # R declared its one candidate: the empty slot is the cap's
    r._unused([real, other], out)
    assert [x for x in ledger_rows(held_out.a, "unused") if x["round_id"] == ROUND] == []
    r.s = dataclasses.replace(r.s, k=3)
    r._unused([real, other], out)
    assert [(x["cause"], x["kind"]) for x in ledger_rows(held_out.a, "unused")] == [("wrong_target", "hook")]


# ---------------------------------------------------------------------------- a cause with nothing held out (§9.1)
def test_a_cause_with_studied_tasks_and_none_held_out_is_screened_in_sample(tmp_path, fake_upstream):
    """IMPROVER §9.1: at a share of 0.2 the notify family (2 tasks) would overshoot and stays studied, so its cause
    has nothing held out: it is assigned all the same, V3 screens on the studied tasks it claims, marked in sample,
    and the gate decides."""
    s = played(round_activity(tmp_path, fake_upstream, "tool", actor=Actor(), improver={"holdout_share": 0.2}))
    (allocated,) = [x for x in ledger_rows(s.a) if x["stage"] == "allocated"]
    assert [a["cause"] for a in allocated["assignments"]] == ["missed_action"]
    assert allocated["in_sample"] == ["missed_action"] and "not_assigned" not in allocated
    res = screen_of(s.a)
    assert res.in_sample is True and sorted(res.failing) == list(NOTIFY) and res.outcome == "screened"
    assert len(res.sample) == 1 and res.sample[0] not in NOTIFY             # the held-out passing task
    (row,) = [x for x in ledger_rows(s.a, "screened") if x["round_id"] == ROUND]
    assert row["in_sample"] is True and sorted(row["fixed"]) == list(NOTIFY)
    assert s.result.installed and s.result.gated


# ---------------------------------------------------------------------------- the gate's cadence (§9.2)
@pytest.fixture(scope="module")
def carry(tmp_path_factory, fake_upstream):
    """A gate every two rounds: round 0 carries its best candidate, round 1 finds nothing left to fix on the carried
    package and gates it."""
    s = played(round_activity(tmp_path_factory.mktemp("carry"), fake_upstream, "tool", actor=Actor(),
                              improver={"gate_every": 2}))
    s.first = s.result
    s.carry_json = json.loads((s.a.root / "rounds" / "carry.json").read_text(encoding="utf-8"))
    s.calls_before = len(s.r.calls)
    played(s, 1)
    return s


def test_a_carry_round_keeps_its_best_candidate_instead_of_paying_the_gate(carry):
    a, r = carry.a, carry.first
    assert r.status == "done" and r.screened == "notify" and not r.gated and r.installed is None
    assert r.carried == round_state(a, 0)["steps"]["screened"]["best_package"]
    assert "gated" not in round_state(a, 0)["steps"] and "carried" in round_state(a, 0)["steps"]
    assert carry.carry_json == {"package_ref": r.carried, "base": carry.base.ref(), "dirs": ["r0:notify"],
                                "since": ROUND}
    (row,) = [x for x in ledger_rows(a, "carried")]
    assert (row["round_id"], row["dir"], row["dirs"]) == (ROUND, "notify", ["r0:notify"])
    runs = a.results.runs()                                             # no gate attempt was paid for round 0
    assert not [x for x in results_of_role(a, "subject") if runs[x.run].candidate == f"{ROUND}:gate"]
    assert carry.first.installed is None and "installed" not in stages(a, ROUND)


def test_the_gate_round_measures_the_carried_package_and_gates_it_when_nothing_is_left_to_fix(carry):
    a, r = carry.a, carry.result
    state = round_state(a, 1)
    assert state["carry"]["package_ref"] == carry.first.carried
    assert state["steps"]["measure"]["package"] == carry.first.carried          # the carried package's train reading
    assert state["steps"]["measure"]["passed"] == len(rsi.TRAIN) and state["steps"]["evidence"]["failed"] == []
    assert "diagnosed" not in state["steps"] and carry.r.calls[carry.calls_before:] == ["playbook"]
    assert r.gated and r.gate["verdict"] == "accepted" and r.installed
    assert a.releases.load(r.installed).package_ref == carry.first.carried
    (gated,) = [x for x in ledger_rows(a, "gated") if x["round_id"] == "rsi-test/r1"]
    assert (gated["dir"], gated["carried"]) == ("carry", ["r0:notify"])
    assert json.loads((a.root / "rounds" / "carry.json").read_text(encoding="utf-8"))["package_ref"] is None
    assert carry.lines[-1].startswith("round      rsi-test/r1: the gate says accepted")


def test_a_carry_the_gate_rejects_is_dropped_and_a_round_that_carried_is_not_idle(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "tool", actor=Actor(), improver={"gate_every": 2},
                       gate={"min_gain": 0.9, "partial_min_gain": 0.9})
    cycle = phases.cycle(s.a, until="round-1", out=quiet)
    steps = cycle["steps"]
    assert steps["round-0"]["carried"] and not steps["round-0"]["gated"]
    assert steps["round-1"]["gated"] and steps["round-1"]["installed"] is None
    assert "reason" not in cycle            # round 0 carried (not idle), round 1 gated without an install: idle once
    (dropped,) = ledger_rows(s.a, "carry_dropped")
    assert dropped["dirs"] == ["r0:notify"] and dropped["verdict"] == "rejected"
    assert json.loads((s.a.root / "rounds" / "carry.json").read_text(encoding="utf-8"))["package_ref"] is None
    assert s.a.releases.active_ref() == s.a.seed_release().ref()
