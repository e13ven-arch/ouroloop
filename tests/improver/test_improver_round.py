"""One round of the improver end to end (docs/design/IMPROVER.md §3 the round, §9 the held-out screen as the acceptance,
§11 the invariants, §13 the v1.2 items; ACTIVITY's round and cycle) on the fake upstream's rsi domain: the forgetful
actor, the scripted R, missed_action mechanizable, the baseline run and the gate plan frozen (tests/improver/rounds.py).
The notify pair is held out from R (rsi.HELD); notify_ticket is the studied failure (rsi.STUDY).

Covered:
- R writes a tool: it climbs V0, V1, V2a; the held-out screen fixes both held-out tasks where the tool was called, the
  accept rule holds and the package is installed on the working line (reason `screen`); the ledger credits the tool
  on the held-out reading; the playbook keeps only the cited lesson; every step is on record; no dev task is read;
- R writes a hook (V2b), and a tool with a hook in one package (V2a and V2b, two components, one mechanism);
- R writes nothing: `no_code_candidate` on record and the round stops `no_candidate`; the cycle stops after
  `schedule.patience` rounds without an acceptance and runs no release gate without `--until gate`;
- the memory on, R writes nothing: the library's snapshot is a fixed candidate, passes V0 and is screened;
- a screen the accept rule refuses: nothing installed, the rejection and its reasons on record;
- the next round after an acceptance measures the new W on the studied tasks only and stops `nothing_to_fix`;
- money: every research call and attempt reserved in the round's account and its step children; an interrupted round
  resumes and pays only for what it had not done (killed at a screen, at the playbook); a done or stopped round run
  again pays nothing; a screen that does not fit what the round has left does not start; a round the budget refuses
  stops on record and the cycle stops there; a round without a baseline measures train itself;
- the install's operation id is `install:<round_id>`, never an Execution operation's id, and replaying it is a no-op;
- the held-out part: whole families, the same for every round; R's inputs name only the studied tasks, its package
  predicts only those, and the screen runs on exactly the held-out tasks.
"""
from __future__ import annotations

import json
import re
from collections import Counter

import pytest
import rsi
from rounds import ledger_rows, quiet, round_activity, round_state
from scripted_r import HOOK_ENTRY, TOOL_ENTRY

from ouroloop.activity import phases
from ouroloop.activity.checks import startup_checks
from ouroloop.contracts import short
from ouroloop.evaluate.budget import operation_id as execution_operation_id
from ouroloop.evaluate.release import InstallReceipt
from ouroloop.harness.package import load
from ouroloop.improver.archive import Archive
from ouroloop.improver.declaration import Declaration
from ouroloop.improver.evidence import Evidence
from ouroloop.improver.round import STEPS, Round
from ouroloop.improver.screen import ScreenResult
from ouroloop.improver.verify import Verdict

ROUND = "rsi-test/r0"
ACCOUNT = "rsi-test:round-0"
INSTALL = "install:rsi-test_r0"
HELD, STUDY = rsi.HELD, rsi.STUDY
TICKET = "rsi.notify_ticket"                      # the studied failure
PASSING = tuple(t for t in rsi.TRAIN if t not in HELD and t != TICKET)
FREE = STEPS[:STEPS.index("screened")]             # measure .. verified: no attempt is paid for before the screen
R0_STAGES = ["measured", "diagnosed", "focused", "declared", "trialed", "verified", "screened", "accepted",
             "installed", "credited", "playbook"]    # a one-package round: the stacked step writes no row; v1.9:
                                                     # one trial (the tool fixes the ticket at once)


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
    s.after, s.seen_after = paid(s), len(s.actor.seen)
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


def declaration_of(a, index: int = 0) -> Declaration:
    (item,) = round_state(a, index)["steps"]["verified"]["passed"]
    return Declaration.from_json(a.objects.get(item["declaration"]))


# ---------------------------------------------------------------------------- one round per mode
@pytest.fixture(scope="module")
def tool(tmp_path_factory, fake_upstream):
    """R writes the notify tool: the round every money test is measured against."""
    return played(round_activity(tmp_path_factory.mktemp("tool"), fake_upstream, "tool", actor=Actor()))


@pytest.fixture(scope="module")
def hook(tmp_path_factory, fake_upstream):
    return played(round_activity(tmp_path_factory.mktemp("hook"), fake_upstream, "hook"))


@pytest.fixture(scope="module")
def both(tmp_path_factory, fake_upstream):
    return played(round_activity(tmp_path_factory.mktemp("both"), fake_upstream, "both"))


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
    """The tool package under an accept rule that asks for more net flips than two held-out tasks can give."""
    return played(round_activity(tmp_path_factory.mktemp("rejected"), fake_upstream, "tool",
                                 improver={"accept_min_net_flips": 3}))


# ---------------------------------------------------------------------------- end to end: a tool
def test_a_tool_package_climbs_v0_v1_v2a_is_screened_on_the_held_out_tasks_and_installs(tool):
    a, r = tool.a, tool.result
    assert (r.round_id, r.status, r.causes, r.components, r.candidates, r.stop) == (ROUND, "done", 1, 1, 1, None)
    assert r.ladder == {"V0": 1, "V1": 1, "V2a": 1} and r.accepted and r.screen["dir"] == "r0:package"
    state = round_state(a, 0)
    (item,) = state["steps"]["verified"]["passed"]
    decl = Declaration.from_json(a.objects.get(item["declaration"]))
    verdict = Verdict.from_json(a.objects.get(item["verdict"]))
    assert (decl.dir, decl.kind, decl.site, decl.cause_ids) == ("package", "tool", TOOL_ENTRY["name"],
                                                                (f"{ROUND}:missed_action",))
    (comp,) = decl.components
    assert (comp.kind, comp.site, comp.predicted_affected, comp.uses_model) == ("tool", TOOL_ENTRY["name"], (TICKET,),
                                                                              False)
    assert list(verdict.steps) == ["V0", "V1"] and verdict.components == {f"tool:{TOOL_ENTRY['name']}": {
        "V2a": verdict.components[f"tool:{TOOL_ENTRY['name']}"]["V2a"]}} and verdict.passed()
    # V3: the whole held-out part; both held-out tasks fixed where the tool was called; nothing regressed
    res = screen_of(a)
    assert res.outcome == "screened" and res.candidate == "r0:package"
    assert (res.holdout, res.failing, res.passing) == (HELD, HELD, ())
    assert (res.fixed, res.unconditioned, res.regressed) == (HELD, (), ())
    assert (res.net_flips, res.holdout_score, res.best_so_far, res.accepted, res.reasons) == (2, 2, 0, True, ())
    assert sorted(res.footprints[f"tool:{TOOL_ENTRY['name']}"]) == list(HELD)
    rows = {x.task_id: x for x in results_of_role(a, "screen")}
    assert sorted(rows) == sorted(HELD)                                      # the screen reads nothing else
    evidence, tool_component = Evidence(a), {"kind": "tool", "name": TOOL_ENTRY["name"]}
    assert all(rows[t].success and evidence.footprint(rows[t], tool_component) for t in HELD)
    # accepted: installed on the working line by CAS, reason screen, the screen named by the release
    accepted = state["steps"]["accepted"]
    assert accepted["status"] == "installed" and r.installed == accepted["installed"]
    assert a.releases.active_ref() == r.installed != tool.base.ref()
    new = a.releases.load(r.installed)
    assert (new.parent_ref, new.reason, new.receipt_ref) == (tool.base.ref(), "screen", None)
    assert ScreenResult.from_json(a.objects.get(new.screen_ref)) == res
    assert new.package_ref == res.package_ref == state["steps"]["screened"]["best_package"]
    assert a.releases.released_ref() is None                                 # the release line waits for the gate
    installed = load(a.objects, new.package_ref)
    assert "tools/post_channel_message.py" in installed.files
    assert TOOL_ENTRY["name"] in [t["name"] for t in json.loads(installed.files["manifest.json"])["tools"]]
    assert tool.lines[-1].startswith(f"round      {ROUND}: package installed on the working line as "
                                     f"{short(r.installed)} (held out 2/2, net 2)")
    # no dev task was read by the round
    assert not any(x.split == "dev" for x in results_of_role(a, "screen") + results_of_role(a, "measure"))


def test_the_ledger_credits_the_tool_and_the_playbook_keeps_only_the_cited_lesson(tool):
    a, r = tool.a, tool.result
    rows = ledger_rows(a)
    assert [x["stage"] for x in rows] == R0_STAGES
    assert {x["round_id"] for x in rows} == {ROUND} and {x["line"] for x in rows} == {a.domain}
    by = {x["stage"]: x for x in rows}
    assert (by["measured"]["passed"], by["measured"]["steps"]) == (len(PASSING), len(rsi.TRAIN))
    assert (by["measured"]["holdout_passed"], by["measured"]["holdout"]) == (0, len(HELD))
    assert by["diagnosed"]["causes"] == {"missed_action": 1} and by["diagnosed"]["tasks"] == [TICKET]
    assert by["focused"]["causes"] == [{"cause": "missed_action", "tasks": 1, "kinds": ["tool", "hook", "skill",
                                                                                         "prompt"]}]
    assert by["focused"]["require_code"] is True
    assert by["declared"]["components"] == [{"kind": "tool", "site": TOOL_ENTRY["name"],
                                             "cause_ids": [f"{ROUND}:missed_action"], "uses_model": False}]
    assert by["verified"]["passed"] is True and by["screened"]["fixed"] == list(HELD)
    assert (by["screened"]["accepted"], by["screened"]["holdout_score"], by["screened"]["best_so_far"]) == (True, 2, 0)
    assert (by["accepted"]["release"], by["installed"]["release"]) == (r.installed, r.installed)
    assert (by["credited"]["release"], by["credited"]["component"], by["credited"]["credit"]) == \
        (r.installed, ["tool:post_channel_message"], {"tool:post_channel_message": 2.0})
    assert sorted(round_state(a, 0)["steps"]["credited"]["credit"]["tool:post_channel_message"]["fixed"]) == \
        list(HELD)
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
    assert state["improver_version"] == a.core.improver_version
    assert list(state["steps"]) == list(STEPS)
    steps = state["steps"]
    assert steps["measure"] == {"package": tool.base.package_ref, "measured": 0, "tasks": len(rsi.TRAIN),
                                "passed": len(PASSING), "holdout_passed": 0, "holdout": len(HELD),
                                "protected": 0, "protected_passed": 0}     # v1.8: W0 passed no held-out task
    assert steps["evidence"]["failed_tasks"] == [TICKET] and steps["evidence"]["not_diagnosed"] == []
    assert len(steps["diagnosed"]["causes"]) == 1 and len(steps["diagnosed"]["cases"]) == 1
    assert [c["dir"] for c in steps["implemented"]["candidates"]] == ["package"]
    assert steps["verified"]["fixed"] == [] and steps["screened"]["best"] == "package"
    assert steps["accepted"]["installed"] == steps["credited"]["release"] == tool.result.installed
    assert (a.root / "rounds" / "r0" / "candidates" / "package" / "files.json").exists()


def test_the_next_round_measures_the_new_w_on_the_studied_tasks_only_and_stops_nothing_to_fix(tool):
    a = tool.a
    before = paid(tool)
    r = phases.round_(a, 1, out=quiet)
    assert (r.status, r.stop) == ("stopped", "nothing_to_fix: the working line passes every studied train task")
    state = round_state(a, 1)
    assert state["base_release"] == tool.result.installed
    # v1.9: the trial read the installed package on the ticket already; the other studied tasks are measured now
    assert state["steps"]["measure"] == {"package": a.releases.load(tool.result.installed).package_ref,
                                         "measured": len(STUDY) - 1, "tasks": len(rsi.TRAIN),
                                         "passed": len(rsi.TRAIN), "holdout_passed": 2, "holdout": 2,
                                         "protected": 0, "protected_passed": 0}
    measured = results_of_role(a, "measure")
    assert sorted(x.task_id for x in measured) == sorted(t for t in STUDY if t != TICKET)   # the held-out: the screen's
    assert Evidence(a).missing(state["steps"]["measure"]["package"]) == []
    after = paid(tool)
    assert after["r"] == before["r"]                                        # no research call
    assert set(settled_by_scope(new_reservations(before, after))) == {"rsi-test:round-1:measure"}
    assert stages(a, "rsi-test/r1") == ["measured", "stopped"]


# ---------------------------------------------------------------------------- end to end: a hook, and both
def test_a_hook_package_climbs_v0_v1_v2b_and_installs(hook):
    a, r = hook.a, hook.result
    assert (r.status, r.candidates, r.ladder, r.accepted, r.stop) == ("done", 1, {"V0": 1, "V1": 1, "V2b": 1}, True,
                                                                     None)
    decl = declaration_of(a)
    assert (decl.kind, decl.site) == ("hook", "on_finish")
    (item,) = round_state(a, 0)["steps"]["verified"]["passed"]
    verdict = Verdict.from_json(a.objects.get(item["verdict"]))
    assert list(verdict.steps) == ["V0", "V1"] and list(verdict.components) == ["hook:on_finish"]
    assert verdict.components["hook:on_finish"]["V2b"]["fired"] == [TICKET]
    res = screen_of(a)
    assert res.outcome == "screened" and res.fixed == HELD and res.regressed == () and res.accepted
    assert sorted(res.footprints["hook:remind_ops"]) == list(HELD)
    assert r.installed == a.releases.active_ref() != hook.base.ref()
    hooks = json.loads(load(a.objects, a.releases.load(r.installed).package_ref).files["manifest.json"])["hooks"]
    assert {"name": "remind_ops", "site": "on_finish"} in [{"name": h["name"], "site": h["site"]} for h in hooks]
    assert stages(a)[-3:] == ["installed", "credited", "playbook"]


def test_a_package_with_a_tool_and_a_hook_climbs_both_rungs_and_credits_each_component(both):
    a, r = both.a, both.result
    assert (r.status, r.components, r.candidates, r.ladder, r.accepted) == ("done", 2, 1, {"V0": 1, "V1": 1, "V2a": 1,
                                                                                            "V2b": 1}, True)
    decl = declaration_of(a)
    assert decl.kind == "tool+hook" and [c.key for c in decl.components] == [f"tool:{TOOL_ENTRY['name']}",
                                                                            "hook:on_finish"]
    (item,) = round_state(a, 0)["steps"]["verified"]["passed"]
    verdict = Verdict.from_json(a.objects.get(item["verdict"]))
    assert {k: list(v) for k, v in verdict.components.items()} == {f"tool:{TOOL_ENTRY['name']}": ["V2a"],
                                                                   "hook:on_finish": ["V2b"]}
    res = screen_of(a)
    # the actor used the tool and said so, so the hook had nothing to push back on: the tool's footprint alone
    assert res.fixed == HELD and res.footprints == {f"tool:{TOOL_ENTRY['name']}": list(HELD)}
    (credited,) = [x for x in ledger_rows(a) if x["stage"] == "credited"]
    assert set(credited["component"]) == {"tool:post_channel_message", f"hook:{HOOK_ENTRY['name']}"}
    assert credited["credit"] == {"tool:post_channel_message": 2.0, f"hook:{HOOK_ENTRY['name']}": 0.0}
    from ouroloop.improver.ledger import Ledger
    board = Ledger(a.root).scoreboard(a.domain)
    assert board["all"]["net"] == board["tool"]["net"] == board["hook"]["net"] == 2    # the package's net, per kind


# ---------------------------------------------------------------------------- two packages a round (§9.4, v1.3)
@pytest.fixture(scope="module")
def two(tmp_path_factory, fake_upstream):
    return played(round_activity(tmp_path_factory.mktemp("two"), fake_upstream, "two"))


def test_two_accepted_packages_install_one_and_stack_the_other_on_the_working_line(two):
    a, r = two.a, two.result
    assert r.status == "done" and r.accepted and r.candidates == 2
    screens = {row["dir"]: ScreenResult.from_json(a.objects.get(row["screen"]))
               for row in round_state(a, 0)["steps"]["screened"]["screens"]}
    assert set(screens) == {"package", "package-2"} and all(res.accepted for res in screens.values())
    first = round_state(a, 0)["steps"]["screened"]["best"]
    other = next(d for d in screens if d != first)
    stacked = round_state(a, 0)["steps"]["stacked"]
    assert stacked["stacked"] == [other] and stacked["not_stacked"] == [] and r.stacked == [other]
    assert a.releases.active_ref() == stacked["line"] == r.installed != round_state(a, 0)["steps"]["accepted"]["installed"]
    line = a.releases.load(r.installed)
    assert line.reason == "screen" and line.parent_ref == round_state(a, 0)["steps"]["accepted"]["installed"]
    stacked_screen = ScreenResult.from_json(a.objects.get(line.screen_ref))
    assert stacked_screen.outcome == "stacked" and stacked_screen.stacked_on == line.parent_ref
    assert stacked_screen.package_ref == line.package_ref and stacked_screen.net_flips == screens[other].net_flips
    manifest = json.loads(load(a.objects, line.package_ref).files["manifest.json"])
    assert TOOL_ENTRY["name"] in [t["name"] for t in manifest["tools"]]
    assert HOOK_ENTRY["name"] in [h["name"] for h in manifest["hooks"]]
    files = load(a.objects, line.package_ref).files
    assert "tools/post_channel_message.py" in files and "middlewares/remind_ops.py" in files
    rows = [x for x in ledger_rows(a) if x["stage"] in ("accepted", "installed", "not_stacked")]
    assert [(x["stage"], x["dir"]) for x in rows] == [("accepted", first), ("installed", first), ("accepted", other),
                                                       ("installed", other)]
    assert rows[2]["stacked_on"] == round_state(a, 0)["steps"]["accepted"]["installed"]
    ops = sorted(p.stem for p in (a.root / "rsi" / "operations").iterdir())
    assert ops == sorted(["install:rsi-test_r0", f"install:rsi-test_r0:{other}"])
    # the next round works on the stacked line and finds nothing left to fix on the studied part
    r1 = phases.round_(a, 1, out=quiet)
    assert r1.stop.startswith("nothing_to_fix") and round_state(a, 1)["base_release"] == r.installed


def test_a_package_that_collides_with_the_installed_one_is_not_stacked(tmp_path, fake_upstream):
    s = played(round_activity(tmp_path, fake_upstream, "two_conflict"))
    a, r = s.a, s.result
    assert r.accepted and r.stacked == []
    stacked = round_state(a, 0)["steps"]["stacked"]
    first = round_state(a, 0)["steps"]["screened"]["best"]
    other = next(d for d in ("package", "package-2") if d != first)
    assert stacked["not_stacked"] == [other] and stacked["line"] == round_state(a, 0)["steps"]["accepted"]["installed"]
    (row,) = ledger_rows(a, "not_stacked")
    assert row["dir"] == other and row["conflicts"] == ["tools/post_channel_message.py"]
    assert a.releases.active_ref() == r.installed and r.installed == round_state(a, 0)["steps"]["accepted"]["installed"]
    assert len(a.releases.history()) == 2                              # the seed and one install


# ---------------------------------------------------------------------------- end to end: no code candidate
def test_no_code_candidate_is_on_record_and_the_round_stops_no_candidate(idle):
    a, r = idle.a, idle.result
    assert (r.status, r.stop) == ("stopped", "no_candidate: no candidate passed its free rungs")
    assert (r.causes, r.components, r.candidates, r.ladder, r.screen, r.accepted, r.installed) == \
        (1, 0, 0, {}, None, False, None)
    rows = [x for x in ledger_rows(a) if x["round_id"] == ROUND]
    assert [x["stage"] for x in rows] == ["measured", "diagnosed", "focused", "no_code_candidate", "stopped"]
    assert rows[3]["cause"] == "missed_action" and rows[4]["reason"] == r.stop
    state = round_state(a, 0)
    assert (state["status"], state["stop"]) == ("stopped", r.stop) and tuple(state["steps"]) == FREE
    # it paid for the diagnosis and R's one turn, nothing else
    assert idle.after["r"][len(idle.before["r"]):] == ["diagnose", "session"]
    assert idle.after["actor"] == idle.before["actor"]
    assert settled_by_scope(new_reservations(idle.before, idle.after)) == {f"{ACCOUNT}:diagnose": 1,
                                                                           f"{ACCOUNT}:session": 1}


def test_under_the_cycle_the_rounds_stop_after_patience_rounds_without_an_acceptance_and_no_gate_runs(idle):
    a, cycle = idle.a, idle.cycle
    patience = int(a.k["schedule"]["patience"])
    assert patience == 2 < int(a.k["schedule"]["rounds"])
    assert (cycle["status"], cycle["at"], cycle["reason"]) == ("stopped", "rounds", f"{patience} rounds without an "
                                                                                   f"acceptance")
    assert cycle["rounds_ended"] == {"at": f"round-{patience - 1}", "reason": cycle["reason"]}
    assert [k for k in cycle["steps"] if k.startswith("round-")] == ["round-0", "round-1"]
    assert cycle["steps"]["round-0"] == {"status": "stopped", "installed": None, "stop": idle.result.stop,
                                         "accepted": False}
    assert "gate" not in cycle["steps"] and not (a.root / "rsi" / "gate.json").exists()
    assert not (a.root / "rounds" / "r2").exists()
    assert stages(a, "rsi-test/r1") == ["measured", "diagnosed", "focused", "no_code_candidate", "stopped"]
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
        (f"snapshot:{event['snapshot_digest']}", "memory", "memory-snapshot", ["memory"])
    (verified,) = [x for x in ledger_rows(a) if x["stage"] == "verified"]
    assert verified["dir"] == "memory-snapshot" and verified["passed"] is True
    assert (r.candidates, r.ladder) == (1, {"V0": 1})
    state = round_state(a, 0)
    assert state["steps"]["implemented"]["candidates"] == [] and state["steps"]["verified"]["fixed"] == \
        ["memory-snapshot"]
    # V3: the snapshot was screened on the held-out tasks and its result is on record
    (row,) = state["steps"]["screened"]["screens"]
    res = screen_of(a)
    assert row["dir"] == "memory-snapshot" and res.outcome == "screened" and res.candidate == "r0:memory-snapshot"
    assert res.package_ref == row["package"] and res.holdout == HELD
    assert load(a.objects, res.package_ref).memory_text == load(a.objects, event["package_ref"]).memory_text != ""
    (screened,) = [x for x in ledger_rows(a) if x["stage"] == "screened"]
    assert screened["dir"] == "memory-snapshot" and screened["source"] == declared["source"]
    assert {x.task_id for x in results_of_role(a, "screen")} == set(HELD)
    # the snapshot is not code: the mechanizable cause still has no code candidate past V2
    assert [x["cause"] for x in ledger_rows(a) if x["stage"] == "no_code_candidate"] == ["missed_action"]


# ---------------------------------------------------------------------------- a screen the accept rule refuses
def test_a_screen_the_accept_rule_refuses_installs_nothing_and_the_rejection_is_on_record(rejected):
    a, r = rejected.a, rejected.result
    assert (r.status, r.accepted, r.installed) == ("done", False, None)
    assert r.screen == {"dir": None, "net_flips": None, "holdout_score": None, "best_so_far": None, "accepted": None} \
        or r.screen is None
    res = screen_of(a)
    assert res.fixed == HELD and res.net_flips == 2 and not res.accepted
    assert res.reasons == ("net flips 2 below accept_min_net_flips 3",)
    assert a.releases.active_ref() == rejected.base.ref()
    assert stages(a)[-3:] == ["screened", "rejected", "playbook"]
    assert "installed" not in stages(a) and "credited" not in stages(a) and "accepted" not in stages(a)
    (row,) = [x for x in ledger_rows(a) if x["stage"] == "screened"]
    assert row["accepted"] is False and row["reasons"] == list(res.reasons)
    assert round_state(a, 0)["steps"]["screened"]["best"] is None
    assert round_state(a, 0)["steps"]["accepted"] == {"installed": None} and round_state(a, 0)["status"] == "done"
    assert not (a.root / "rsi" / "operations").exists() or not list((a.root / "rsi" / "operations").iterdir())
    # the estimates learnt from the screen all the same (an observation of the tool on missed_action)
    estimates = json.loads((a.root / "ledger" / "estimates.json").read_text(encoding="utf-8"))
    assert estimates[a.domain]["missed_action"]["tool"]["n"] == 1


# ---------------------------------------------------------------------------- money
def test_every_paid_call_of_the_round_is_reserved_in_its_account_and_the_spend_is_the_books(tool):
    a, r = tool.a, tool.result
    data = book(a)
    share = float(a.k["budget"]["usd"]) * float(a.k["improver"]["round_share"])
    assert data["scopes"][ACCOUNT] == {"parent": a.scope_id, "limits": {"usd": share}, "allocations": {}}
    new = new_reservations(tool.before, tool.after)
    assert new and {x["status"] for x in new.values()} == {"settled"}
    research = Counter(tool.after["r"][len(tool.before["r"]):])
    seen = tool.actor.seen[tool.seen_before:tool.seen_after]
    dev = triggers(a, rsi.DEV)
    assert research == {"diagnose": 1, "session": 4, "playbook": 1}
    assert seen and not any(any(t in task_of(x) for t in dev) for x in seen)          # no dev attempt in a round
    assert {t for x in seen for t in triggers(a, HELD) if t in task_of(x)} == set(triggers(a, HELD))   # the screen's
    # v1.9: the trial's calls are the studied failure's (the ticket), the screen's the held-out pair's
    trial_calls = [x for x in seen if triggers(a, (TICKET,))[0] in task_of(x)]
    assert trial_calls and len(trial_calls) < len(seen)
    # each research call in its step's child, each attempt's call in the trial's or the screen's; nothing elsewhere
    assert settled_by_scope(new) == {f"{ACCOUNT}:diagnose": research["diagnose"],
                                     f"{ACCOUNT}:session": research["session"],
                                     f"{ACCOUNT}:policy": research["playbook"],
                                     f"{ACCOUNT}:trial": len(trial_calls),
                                     f"{ACCOUNT}:screen": len(seen) - len(trial_calls)}
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
        s.actor.crash_on = triggers(s.a, HELD)
    else:
        s.research.r = DieAtPlaybook(s.r)
    with pytest.raises(Crash):
        phases.round_(s.a, 0, out=quiet)
    cut = round_state(s.a, 0)
    assert cut["status"] == "running"
    assert tuple(cut["steps"]) == (FREE if where == "screen" else STEPS[:-1])
    if where == "playbook":
        assert s.a.releases.active_ref() == cut["steps"]["accepted"]["installed"] != cut["base_release"]
    result = phases.round_(s.a, 0, out=quiet)
    end = paid(s)
    # what it did is what the uninterrupted round did
    keep = ("round_id", "status", "causes", "components", "candidates", "ladder", "screen", "accepted", "stop")
    assert {k: getattr(result, k) for k in keep} == {k: getattr(tool.result, k) for k in keep}
    assert result.installed == s.a.releases.active_ref() and result.installed is not None
    assert stages(s.a, ROUND) == stages(tool.a, ROUND)
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


def test_a_round_killed_between_its_install_and_saving_its_accepted_step_resumes(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "tool")

    def dying(line: str) -> None:
        if "installed on the working line" in line:
            raise Crash(line)
    with pytest.raises(Crash):
        phases.round_(s.a, 0, out=dying)
    state = round_state(s.a, 0)
    assert "accepted" not in state["steps"] and s.a.releases.active_ref() != state["base_release"]
    calls = s.actor.calls
    result = phases.round_(s.a, 0, out=quiet)
    assert result.status == "done" and result.installed == s.a.releases.active_ref()
    assert round_state(s.a, 0)["steps"]["accepted"]["status"] == "installed"
    assert s.actor.calls == calls                      # the install replays from the record: nothing is paid again
    assert stages(s.a).count("installed") == 1


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
    assert (result.status, result.candidates, result.ladder) == \
        (memory.result.status, memory.result.candidates, memory.result.ladder)
    assert [r["dir"] for r in round_state(s.a, 0)["steps"]["screened"]["screens"]] == ["memory-snapshot"]
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


def test_a_round_without_a_baseline_measures_train_itself_and_goes_on(tmp_path, fake_upstream):
    """No baseline: the round measures the working line's train (role measure, in its measure account), prices the
    screen from that reading (no dev reading is needed inside a round) and goes on to install."""
    s = round_activity(tmp_path, fake_upstream, "tool", baseline=False)
    played(s)
    a, r = s.a, s.result
    assert r.status == "done" and r.accepted, r.stop
    state = round_state(a, 0)
    assert state["steps"]["measure"]["measured"] == len(rsi.TRAIN)
    runs = a.results.runs()
    measured = [x for x in a.results.all() if runs[x.run].role == "measure"]
    assert sorted(x.task_id for x in measured) == sorted(rsi.TRAIN) and {x.split for x in measured} == {"train"}
    assert {runs[x.run].candidate for x in measured} == {f"train:{short(s.base.ref())}:r0"}
    assert {runs[x.run].role for x in a.results.all()} == {"measure", "trial", "screen", "confirm"}
    new = new_reservations(s.before, s.after)
    assert settled_by_scope(new)[f"{ACCOUNT}:measure"] > 0 and f"{ACCOUNT}:screen" in settled_by_scope(new)


def test_a_screen_that_does_not_fit_what_the_round_has_left_is_not_started(tmp_path, fake_upstream):
    """Someone else sets aside all but $0.01 while the package climbs its free rungs: the screen is priced from the
    working line's train costs on record, does not fit, and nothing is screened."""
    s = round_activity(tmp_path, fake_upstream, "tool", actor=Actor())
    lines = []

    def out(line: str) -> None:
        lines.append(line)
        if "passed their free rungs" in line:
            left = s.a.account("other").remaining()["effective_remaining"]["usd"]
            s.a.account("other").hold("someone else's reservation", {"usd": round(left - 0.01, 6)})
    before, seen_before = paid(s), len(s.actor.seen)
    result = phases.round_(s.a, 0, out=out)
    assert result.status == "stopped" and re.fullmatch(
        r"budget: the held-out screen would need up to \$\d+\.\d\d \(\$\d+\.\d\d at the means on record, \+15%, and "
        r"two calls' bound\) and the round has \$0\.01", result.stop), result.stop
    assert lines[-1] == f"round      {ROUND} stopped: {result.stop}"
    after = paid(s)
    # v1.9: the actor's only calls are the trial's, on the studied failure; no held-out task was paid for
    assert after["actor"] > before["actor"] and after["r"][len(before["r"]):] == ["diagnose"] + ["session"] * 4
    assert {task_of(x) for x in s.actor.seen[seen_before:]} == set(triggers(s.a, (TICKET,)))
    assert results_of_role(s.a, "screen") == [] and tuple(round_state(s.a, 0)["steps"]) == FREE
    assert {x["scope_id"] for x in new_reservations(before, after).values()} == {
        f"{ACCOUNT}:diagnose", f"{ACCOUNT}:session", f"{ACCOUNT}:trial", "rsi-test:other"}   # v1.9: the trial ran


def test_a_round_the_budget_refuses_stops_on_record_and_the_cycle_stops_there(tmp_path, fake_upstream):
    """Someone else sets aside all but $0.01 of the root before the round: one held-out screen and the research do not
    fit, so the round stops `budget` on record before its first research call, and the cycle stops at that round."""
    s = round_activity(tmp_path, fake_upstream, "tool")
    left = s.a.account("other").remaining()["effective_remaining"]["usd"]
    s.a.account("other").hold("someone else's reservation", {"usd": round(left - 0.01, 6)})
    before = paid(s)
    cycle = phases.cycle(s.a, out=quiet)
    assert (cycle["status"], cycle["at"]) == ("stopped", "round-0") and "round-1" not in cycle["steps"]
    stop = cycle["steps"]["round-0"]["stop"]
    assert stop.startswith("budget: one held-out screen, the trials and the research would need up to $") and \
        cycle["steps"]["round-0"]["status"] == "stopped"
    assert paid(s) == before
    assert stages(s.a) == ["measured", "stopped"] and ledger_rows(s.a)[-1]["reason"] == stop
    assert round_state(s.a, 0)["stop"] == stop and tuple(round_state(s.a, 0)["steps"]) == ("measure", "evidence")


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
    receipt = a.objects.load(round_state(a, 0)["steps"]["accepted"]["receipt"], InstallReceipt)
    assert (receipt.status, receipt.operation_id, receipt.target_release) == ("installed", INSTALL, r.installed)
    # an Execution operation is op_<16 hex> of its experiment, step and inputs: never the install's, whatever they are
    for experiment, step, inputs in ((ROUND, "install", [r.installed]), (INSTALL, "install", []),
                                     ("rsi-test_r0", "screen", [r.installed]), (ROUND, "", [])):
        oid = execution_operation_id(experiment, step, inputs)
        assert re.fullmatch(r"op_[0-9a-f]{16}", oid) and oid != INSTALL
    assert "/" not in INSTALL and not INSTALL.startswith("op_")
    # the same operation again: the receipt on record, the pointer and the events untouched (CAS)
    pointer, events = a.releases.pointer(), list(a.events.read("installed"))
    new = a.releases.load(r.installed)
    again = a.releases.install_screened(new, new.screen_ref, expected_parent=tool.base.ref(), operation_id=INSTALL)
    assert (again.status, again.target_release, again.operation_id) == ("installed", r.installed, INSTALL)
    assert a.releases.pointer() == pointer and list(a.events.read("installed")) == events


def test_the_archive_keeps_the_screened_packages_screen_and_its_install_receipt(tool):
    a = tool.a
    archive = Archive(a.root, a.objects)
    (row,) = [x for x in archive.rows() if x["dir"] == "package"]
    (screened,) = round_state(a, 0)["steps"]["screened"]["screens"]
    assert row["screen"] == screened["screen"] and row["receipt"] == round_state(a, 0)["steps"]["accepted"]["receipt"]
    assert (row["kind"], row["causes"], row["site"]) == ("tool", ["missed_action"], TOOL_ENTRY["name"])
    (shown,) = archive.for_prompt(a.domain, "missed_action", hide=set(HELD))
    assert shown["installed"] is True and shown["screen"]["outcome"] == "screened" and shown["screen"]["accepted"]
    assert shown["screen"]["fixed"] == [] and shown["screen"]["fixed_held_out"] == 2          # counted, never named


# ---------------------------------------------------------------------------- the held-out part of train
def test_the_held_out_part_is_whole_families_the_same_for_every_round(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "none", baseline=False, freeze=False)
    study, held = Round(s.a, 0, run_arm=None, out=quiet).split()
    assert (study, held) == (STUDY, HELD)
    assert sorted(study + held) == sorted(rsi.TRAIN) and not set(study) & set(held)
    assert 0 < len(held) <= round(len(rsi.TRAIN) * 0.4)                           # never more than its share
    families = lambda tasks: {rsi.FAMILIES[t] for t in tasks}                     # noqa: E731
    assert not families(study) & families(held)
    assert Round(s.a, 1, run_arm=None, out=quiet).split() == (study, held)
    # a share of one task skips the notify family (2 tasks) rather than overshoot it
    small = round_activity(tmp_path / "small", fake_upstream, "none", baseline=False, freeze=False,
                           improver={"holdout_share": 0.5})
    assert len(Round(small.a, 0, run_arm=None, out=quiet).split()[1]) == 3
    with pytest.raises(ValueError, match="holdout_share"):
        round_activity(tmp_path / "none", fake_upstream, "none", baseline=False, freeze=False,
                       improver={"holdout_share": 0.0})


def test_r_reads_only_the_studied_tasks_and_its_package_predicts_only_those(tool):
    a = tool.a
    r = Round(a, 0, run_arm=None, out=quiet)
    inputs, readable = r.inputs()
    (cause,) = json.loads(inputs["causes.json"])
    assert (cause["tasks"], cause["held_out_tasks"]) == ([TICKET], 0)
    assert [c["task_id"] for c in cause["cases"]] == [TICKET]
    focus = json.loads(inputs["focus.json"])
    assert focus["ranked"][0]["tasks"] == [TICKET] and focus["require_code"] is True
    assert not [(name, t) for name, text in inputs.items() for t in HELD if t in text]
    sessions = {(x.evidence or x.attempt_id): x.task_id for x in a.results.all()}
    assert {sessions[x] for x in readable if x} <= set(STUDY)
    assert declaration_of(a).predicted_affected == (TICKET,)
    assert r.vctx().cause_ids == (f"{ROUND}:missed_action",) and r.vctx().study_tasks == STUDY   # V0 checks them
    # what R reads of the screen next round: the held-out fixes as a count
    (screened,) = [x for x in json.loads(inputs["ledger.json"]) if x["stage"] == "screened"]
    assert "fixed" not in screened and screened["fixed_held_out"] == 2
    assert not any(t in json.dumps(json.loads(inputs["ledger.json"])) for t in HELD)
    # v1.5 (DESIGN §6.15): the studied failure side by side — the request, the failed check with its parameters, the
    # agent's writes in order, what it said; the held-out failures are not there
    failures = json.loads(inputs["failures.json"])
    (case,) = failures["cases"]
    assert case["task_id"] == TICKET and case["cause"] == "missed_action" and case["cause_id"] == f"{ROUND}:missed_action"
    assert "Kai Mercer" in case["request"] and "stage updated" in case["request"]
    assert [c["type"] for c in case["failed_checks"]] == ["slack_message_contains"]
    assert case["failed_checks"][0]["params"]              # the grader's expected values, train only
    assert [(w["tool"], w["method"]) for w in case["writes"]] == [("api_fetch", "PATCH")] and "hubapi" in case["writes"][0]["url"]
    assert case["reads"] == 0 and case["searches"] == 0 and case["termination"] == "finished"
    assert case["final_text"].startswith("Done") and case["note"]
    assert (case["persistence"], case["persistent"]) == (1, False)       # one package (W0) failed it so far
    assert len(json.dumps(case)) <= a.k["improver"]["failures_case_chars"]


def test_a_task_that_keeps_failing_under_another_installed_package_is_persistent(tool):
    """`persistence` counts the working line's packages a task has a failing valid train reading under (DESIGN §6.15):
    the installed package's reading of the ticket, failed again, makes it persistent and first in failures.json."""
    import dataclasses
    a = tool.a
    ev = Evidence(a)
    assert ev.persistence(TICKET) == 1
    new = a.active_release()                                   # round 0's tool is installed
    run = a.run_manifest(candidate="synthetic", role="measure", split="train", release=new)
    a.results.put_run(run)
    base_row = ev.reading(a.seed_release().package_ref)[TICKET]
    a.results.append(dataclasses.replace(base_row, run=run.hash(), candidate="synthetic", success=False,
                                         partial_credit=0.5, attempt_id=f"{run.hash()}/{TICKET}/r0/subject#0",
                                         system_release_ref=new.ref()))
    assert ev.persistence(TICKET) == 2 and ev.persistence(STUDY[0]) == 0


def test_a_task_the_working_line_also_passed_is_divergent_and_comes_first_with_a_passing_session(tool):
    """v1.10 (DESIGN §6.20): `readings_of` counts W's own passing and failing readings of a task under the packages
    of the line's history — after round 0 the ticket failed under W0 and passed in the installed package's trial
    (the line's reading of it now), so it is divergent: failures.json marks it, names the passing session to read
    beside the failure, and the session is readable; a rejected candidate's trial reading is not W's; a task the line
    only passed is not divergent."""
    import dataclasses
    a = tool.a
    ev = Evidence(a)
    got = ev.readings_of(TICKET)
    failed = got["failed"]                      # W0's, plus what earlier tests of this module appended under the line
    assert (got["passed"], got["divergent"]) == (1, True) and failed >= 1
    assert got["failing_session"] and got["passing_session"] and got["failing_session"] != got["passing_session"]
    runs = a.results.runs()
    trial_sessions = {(r.evidence or r.attempt_id) for r in a.results.all() if runs[r.run].role == "trial"
                      and r.task_id == TICKET and r.success}
    assert got["passing_session"] in trial_sessions
    quiet_task = ev.readings_of(STUDY[0])
    assert quiet_task["failed"] == 0 and quiet_task["divergent"] is False and quiet_task["failing_session"] is None
    r = Round(a, 0, run_arm=None, out=quiet)
    inputs, readable = r.inputs()
    (case,) = json.loads(inputs["failures.json"])["cases"]
    assert case["divergent"] is True and case["w_readings"] == {"passed": 1, "failed": failed}
    assert case["passing_session"] == got["passing_session"] and case["passing_session"] in readable
    assert "divergent first" in json.loads(inputs["failures.json"])["note"]
    # a trial reading of a package that is not the line's says nothing of W
    base_row = ev.reading(a.seed_release().package_ref)[TICKET]
    other = a.run_manifest(candidate="r9:package", role="trial", split="train", release=a.seed_release())
    a.results.put_run(other)
    a.results.append(dataclasses.replace(base_row, run=other.hash(), candidate="r9:package", success=True,
                                         partial_credit=1.0, attempt_id=f"{other.hash()}/{TICKET}/r0/subject#0"))
    assert ev.readings_of(TICKET)["passed"] == 1


def test_the_system_prompt_asks_for_the_two_scopes_only_with_two_packages_or_more(tool):
    """v1.10 (DESIGN §6.20): with packages_per_round >= 2 the prompt names candidates/package/ invariant and
    candidates/package-2/ domain; with one package, or scoped_packages off, it does not; the evidence order is always
    there."""
    import dataclasses
    r = Round(tool.a, 0, run_arm=None, out=quiet)
    r.s = dataclasses.replace(r.s, packages_per_round=1)
    one = r.system_prompt()
    assert "two scopes" not in one and "{scopes}" not in one and "The order of evidence" in one
    assert "divergent" in one and "cheapest" in one
    r.s = dataclasses.replace(r.s, packages_per_round=2)
    two = r.system_prompt()
    assert "two scopes" in two and "candidates/package/ is invariant" in two and "candidates/package-2/ is domain" in two
    r.s = dataclasses.replace(r.s, packages_per_round=2, scoped_packages=False)
    assert "two scopes" not in r.system_prompt()


def test_a_digest_shrinks_its_writes_to_the_cap_and_keeps_the_checks():
    """`_fit` (IMPROVER §4, v1.5): the longest write's excerpt is halved first, never under MIN_EXCERPT; the request and
    the checks are kept whole; a row that cannot shrink any further is returned as it is."""
    from ouroloop.improver.evidence import MIN_EXCERPT, _fit
    row = lambda: {"task_id": "t", "request": "r" * 300, "failed_checks": [{"type": "x", "params": {"v": 1}}],  # noqa: E731
                   "writes": [{"tool": "api_fetch", "method": "POST", "url": "u", "body": "a" * 3000, "error": False},
                              {"tool": "api_fetch", "method": "PUT", "url": "u", "body": "b" * 1000, "error": False}],
                   "final_text": "done"}
    fitted = _fit(row(), 2000)
    assert len(json.dumps(fitted)) <= 2000 and fitted["failed_checks"] == row()["failed_checks"]
    assert fitted["request"] == "r" * 300 and fitted["writes"][0]["body"].endswith(")")
    assert len(fitted["writes"][0]["body"]) < 3000 and MIN_EXCERPT <= len(fitted["writes"][1]["body"]) <= 1000 + 12
    floor = _fit(row(), 10)
    assert all(len(w["body"]) >= MIN_EXCERPT for w in floor["writes"])         # it stops at the floor, no loop
    assert _fit(row(), 100000) == row()


def test_v3_screens_on_exactly_the_held_out_tasks(tool):
    a, res = tool.a, screen_of(tool.a)
    assert (res.holdout, res.failing, res.passing) == (HELD, HELD, ())
    assert sorted(x.task_id for x in results_of_role(a, "screen")) == sorted(HELD)


def test_a_memory_entry_distilled_from_a_held_out_task_is_not_shown_to_r(tool, monkeypatch):
    from ouroloop.evaluate.attempt import task_hash
    import dataclasses
    r = Round(tool.a, 0, run_arm=None, out=quiet)
    hashed = {t: task_hash(tool.a.suite.tasks[t]) for t in rsi.TRAIN}
    entries = ({"entry": "seen", "distilled_from": [hashed[TICKET], hashed["rsi.update_stage"]]},
               {"entry": "held", "distilled_from": [hashed[TICKET], hashed[HELD[0]]]})
    (cause,) = r.causes()
    monkeypatch.setattr(Round, "causes", lambda self: [dataclasses.replace(cause, memory_entries=entries)])
    inputs, _ = r.inputs()
    assert [e["entry"] for e in json.loads(inputs["causes.json"])[0]["memory_entries"]] == ["seen"]
