"""Accumulation without regression (DESIGN §6.18; IMPROVER §9.5, §13 the v1.8 items): the frontier of partial
packages and their merge, the regression evidence R reads, the research sample, text that only grows, the word-list
signal — on the fake upstream's rsi domain under rsi.QUIET_SPLITS, which holds the quiet update (rsi.QUIET: an
update that must not be announced in #ops) out beside the notify pair. The actor is forgetful and suggestible
(tests/rsi.py): a pushback that names #ops makes it post even where the request forbade it.

Round 0, scripted R in `partial` mode: a global hook ("the request mentions #ops and nothing was posted: push back")
passes V2b (no studied passing task mentions #ops without a post), fixes both held-out notify tasks and breaks the
quiet one, all confirmed; the accept rule rejects it (regressions 0 allowed) and it goes on the frontier with its
regression evidence — one de-identified record (assertion kinds, what the hook said, the writes before and after by
route, no task id) and the research sample (the three studied passing tasks, none regressed).

Round 1, mode `merge`: focus.json names the entry; inputs hold frontier.json (the entry's diff, counts, evidence) and
regressions.json; R hands in candidates/merge/ — the same hook, its trigger narrowed to a planned post not made —
declared merge_of; it fixes the pair, breaks nothing, is installed, and the entry is `merged`.

Also: a rejected merge (mode `merge_bad`: the global hook again) counts an attempt and the entry is `abandoned` at
frontier_max_attempts; a tool package installed afterwards subsumes an entry whose fixes the line now passes; the
protected set's count on the ledger; the Frontier's ordering, cap, pick and prune; the research sample's order and a
hit; the evidence levels; a prompt rewrite refused by V0 unless declared rewrite alone; heuristic_terms."""
from __future__ import annotations

import json

import pytest
import rsi
from rounds import ledger_rows, quiet, round_activity, round_state
from scripted_r import CONDITIONAL_HOOK_CODE, HOOK_ENTRY, PARTIAL_HOOK_CODE, TOOL_CODE, TOOL_ENTRY, \
    WORDLIST_HOOK_CODE

from ouroloop.activity import phases
from ouroloop.activity.report import build_report, render_markdown
from ouroloop.harness.package import Limits, build
from ouroloop.improver.declaration import Component, Declaration
from ouroloop.improver.frontier import Frontier, FrontierEntry
from ouroloop.improver.round import Round
from ouroloop.improver.screen import RegressionEvidence, ScreenResult, partial_of, research_run, research_sample
from ouroloop.improver.settings import Settings
from ouroloop.improver.verify import run_ladder
from ouroloop.improver.verify.static import heuristic_terms, static_check, text_problems

HELD = rsi.HELD_QUIET                       # the notify pair and the quiet update
NOTIFY = rsi.HELD
QUIET = rsi.QUIET
STUDY = rsi.STUDY
TICKET = "rsi.notify_ticket"
PASSING = ("rsi.update_stage", "rsi.policy_hold", "rsi.notify_once")
CAUSE = "missed_action"


def actor():
    return rsi.BusinessWorker(forget_notify=True, suggestible=True)


def screens_of(a, tag: str) -> dict[str, ScreenResult]:
    state = round_state(a, tag)
    return {r["dir"]: ScreenResult.from_json(a.objects.get(r["screen"])) for r in state["steps"]["screened"]["screens"]}


def frontier_file(a) -> dict:
    return json.loads((a.root / "rounds" / "frontier.json").read_text(encoding="utf-8"))


def inputs_read(s, name: str) -> dict:
    """What R read of inputs/<name> in its latest session (the scripted R reads frontier.json and regressions.json)."""
    seen = [m for m in s.r.read if m[0] == name]
    assert seen, f"R did not read inputs/{name}"
    return json.loads(seen[-1][1])


# ---------------------------------------------------------------------------- round 0: the partial package
@pytest.fixture(scope="module")
def partial(tmp_path_factory, fake_upstream):
    s = round_activity(tmp_path_factory.mktemp("partial"), fake_upstream, "partial", actor=actor(),
                       splits_table=rsi.QUIET_SPLITS)
    s.result = phases.round_(s.a, 0, out=quiet)
    return s


def test_the_split_holds_the_quiet_update_out_beside_the_notify_pair(partial):
    study, held = Round(partial.a, 0, run_arm=None, out=quiet).split()
    assert sorted(held) == sorted(HELD) and sorted(study) == sorted(STUDY)


def test_a_global_hook_fixes_the_pair_breaks_the_quiet_update_and_is_a_partial_package(partial):
    a, r = partial.a, partial.result
    assert r.status == "done" and not r.accepted and r.ladder == {"V0": 1, "V1": 1, "V2b": 1}
    res = screens_of(a, "r0")["package"]
    assert res.outcome == "screened" and not res.accepted
    assert res.fixed == NOTIFY and res.regressed == (QUIET,) and res.noisy == () and res.unconfirmed == ()
    assert res.reasons == ("1 regressions above accept_max_regressions 0",)
    assert res.partial and not res.whole and res.merge_of == ""
    assert a.active_release().ref() == a.seed_release().ref()


def test_the_partial_package_is_on_the_frontier_with_its_evidence(partial):
    a = partial.a
    f = frontier_file(a)
    (e,) = f["entries"]
    assert (e["id"], e["tag"], e["dir"], e["status"], e["attempts"]) == ("f1", "r0", "package", "active", 0)
    assert tuple(e["fixed"]) == NOTIFY and tuple(e["regressed"]) == (QUIET,) and e["kind"] == "hook"
    assert e["regression_ref"] and e["base_package_ref"] == a.seed_release().package_ref
    rows = ledger_rows(a, "partial")
    assert [(r["entry"], r["dir"], r["fixes"]) for r in rows] == [("f1", "package", 2)]
    assert [x["stage"] for x in ledger_rows(a) if x.get("round_id") == "rsi-test/r0"
            and x["stage"] != "playbook"][-3:] == ["screened", "partial", "rejected"]


def test_the_regression_evidence_is_de_identified_and_the_research_sample_ran(partial):
    a = partial.a
    res = screens_of(a, "r0")["package"]
    obj = a.objects.get(res.regression_ref)
    assert (obj["level"], obj["fixes"], obj["regressions"], obj["dir"]) == ("research", 2, 1, "package")
    (rec,) = obj["records"]
    ev = RegressionEvidence.from_json(rec)
    assert ev.domain == "rsi" and ev.failed_kinds == ("slack_message_count",)
    assert ev.passed_kinds_before == ("hubspot_contact_stage", "slack_message_count")
    (fp,) = ev.footprints
    assert (fp["kind"], fp["name"], fp["site"], fp["action"]) == ("hook", HOOK_ENTRY["name"], "on_finish", "pushback")
    assert "#ops" in fp["said"]
    assert ev.writes["before"] == {"PATCH api.hubapi.com/crm/v3/objects/contacts": 1}
    assert ev.writes["after"] == {"PATCH api.hubapi.com/crm/v3/objects/contacts": 1,
                                  "POST slack.com/api/chat.postMessage": 1}
    assert ev.termination == "finished"
    text = json.dumps(obj["records"])
    assert not any(t in text for t in HELD) and "Lea Fontaine" not in text and "c3" not in text
    # the research level: the studied passing tasks ran under the candidate, none regressed
    assert sorted(res.research_sample) == sorted(PASSING) and res.research_regressed == ()
    assert obj["research"] == {"sample": list(res.research_sample), "regressed": [], "sessions": {}}
    runs = a.results.runs()
    research = [r for r in a.results.all() if runs[r.run].role == "research"]
    assert sorted(r.task_id for r in research) == sorted(PASSING) and all(r.success for r in research)
    assert all(runs[r.run].candidate == "r0:package" for r in research)
    file = json.loads((a.root / "rounds" / "r0" / "regressions.json").read_text(encoding="utf-8"))
    assert [p["dir"] for p in file["packages"]] == ["package"] and file["packages"][0]["regressed"] == [QUIET]
    row = ledger_rows(a, "screened")[-1]
    assert (row["partial"], row["research_sample"], row["research_regressed"]) == (True, 3, [])


def test_the_protected_set_is_the_held_out_tasks_the_first_reading_passed(partial):
    row = ledger_rows(partial.a, "measured")[-1]
    assert (row["protected"], row["protected_passed"], row["holdout"], row["holdout_passed"]) == (1, 1, 3, 1)


# ---------------------------------------------------------------------------- round 1: the merge
@pytest.fixture(scope="module")
def merged(tmp_path_factory, fake_upstream):
    s = round_activity(tmp_path_factory.mktemp("merged"), fake_upstream, "partial", actor=actor(),
                       splits_table=rsi.QUIET_SPLITS)
    s.r0 = phases.round_(s.a, 0, out=quiet)
    s.r.mode = "merge"
    s.r.read = []
    s.lines = []
    s.r1 = phases.round_(s.a, 1, out=s.lines.append)
    return s


def test_the_focus_names_the_entry_and_the_inputs_hold_the_frontier_and_the_regressions(merged):
    a = merged.a
    f = Round(a, 1, run_arm=None, out=quiet).focus()
    (m,) = f.merge
    assert (m["entry"], m["round"], m["dir"], m["kind"], m["fixes"], m["regressions"], m["attempts"],
            m["candidate_dir"]) == ("f1", "r0", "package", "hook", 2, 1, 0, "merge")
    assert m["cause_ids"] == ["rsi-test/r1:missed_action"]           # this round's id of the entry's cause
    frontier = inputs_read(merged, "frontier.json")
    (e,) = frontier["entries"]
    assert (e["entry"], e["fixes"], e["regressions"], e["attempts"], e["status"]) == ("f1", 2, 1, 0, "active")
    assert set(e["diff"]) == {"manifest.json", "middlewares/remind_ops.py"}
    assert "+    if \"#ops\" in said and \"posted\" not in said:" in e["diff"]["middlewares/remind_ops.py"]
    assert e["regressions_evidence"]["evidence"][0]["failed_kinds"] == ["slack_message_count"]
    regressions = inputs_read(merged, "regressions.json")
    assert regressions["level"] == "research"
    (p,) = regressions["packages"]
    assert (p["round"], p["dir"], p["fixes"], p["regressions"], p["partial"], p["accepted"]) == \
        ("rsi-test/r0", "package", 2, 1, True, False)
    assert sorted(p["research"]["sample"]) == sorted(PASSING) and p["research"]["regressed"] == []
    text = json.dumps([frontier, regressions])
    assert not any(t in text for t in HELD)
    assert "merges from the frontier" in merged.r.system_seen and "entry f1" in merged.r.system_seen


def test_the_conditioned_merge_fixes_the_pair_breaks_nothing_and_is_installed(merged):
    a, r = merged.a, merged.r1
    assert r.status == "done" and r.accepted and r.screen["dir"] == "r1:merge"
    res = screens_of(a, "r1")["merge"]
    assert res.fixed == NOTIFY and res.regressed == () and res.accepted and res.merge_of == "f1"
    assert not res.partial and res.regression_ref is None and res.research_sample == ()
    decl = Declaration.from_json(a.objects.get(round_state(a, "r1")["steps"]["verified"]["passed"][0]["declaration"]))
    assert decl.merge_of == "f1" and decl.dir == "merge" and decl.kind == "hook"
    (e,) = frontier_file(a)["entries"]
    assert e["status"] == "merged" and e["attempts"] == 0
    assert e["history"] == [{"round_id": "rsi-test/r1", "dir": "merge", "outcome": "merged"}]
    rows = ledger_rows(a, "merged")
    assert [(x["entry"], x["dir"], x["round_id"]) for x in rows] == [("f1", "merge", "rsi-test/r1")]
    assert any("frontier entry f1 merged by merge" in line for line in merged.lines)
    verified = [x for x in ledger_rows(a, "verified") if x["round_id"] == "rsi-test/r1"]
    assert verified[0]["merge_of"] == "f1" and verified[0]["passed"]
    # the new line passes the protected task and the pair
    assert ledger_rows(a, "measured")[-1]["protected_passed"] == 1
    reading = Round(a, 2, run_arm=None, out=quiet).reading()
    assert all(reading[t].success for t in HELD)


def test_the_report_shows_the_accumulation(merged):
    report = build_report(merged.a)
    fr = report["frontier"]
    assert fr["protected_curve"] == [{"round": "rsi-test/r0", "passed": 1, "tasks": 1},
                                     {"round": "rsi-test/r1", "passed": 1, "tasks": 1}]
    assert fr["entries"] == {"total": 1, "merged": 1}
    assert fr["merges"] == {"screened": 1, "accepted": 1}
    assert fr["research"] == {"packages_with_regressions": 1, "sampled": 1, "hits": 0}
    r0, r1 = report["rounds"]
    assert r0["screens"][0]["partial"] and r0["frontier"][0]["stage"] == "partial" and r0["protected"] == \
        {"passed": 1, "tasks": 1}
    assert r1["merge"] == ["f1"] and r1["screens"][0]["merge_of"] == "f1" and r1["frontier"][0]["stage"] == "merged"
    md = render_markdown(report)
    assert "protected set (held-out tasks the first reading passed), round by round: 1/1, 1/1" in md
    assert "screen merge (merge of f1)" in md and "— partial" in md and "frontier merged f1 (merge)" in md


def test_a_merge_of_the_same_hook_is_rejected_again_and_the_entry_is_abandoned(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "partial", actor=actor(), splits_table=rsi.QUIET_SPLITS,
                       improver={"frontier_max_attempts": 1})
    phases.round_(s.a, 0, out=quiet)
    s.r.mode = "merge_bad"
    r1 = phases.round_(s.a, 1, out=quiet)
    assert r1.status == "done" and not r1.accepted
    res = screens_of(s.a, "r1")["merge"]
    assert res.regressed == (QUIET,) and res.partial and res.merge_of == "f1"
    (e,) = frontier_file(s.a)["entries"]
    assert (e["status"], e["attempts"], e["tag"], e["dir"]) == ("abandoned", 1, "r1", "merge")   # replaced, then out
    assert e["history"] == [{"round_id": "rsi-test/r1", "dir": "merge", "outcome": "abandoned"}]
    (row,) = ledger_rows(s.a, "abandoned")
    assert (row["entry"], row["attempts"], row["dir"]) == ("f1", 1, "merge")
    assert Round(s.a, 2, run_arm=None, out=quiet).frontier.picks("rsi", 1) == []
    # round 2: nothing to merge, and R in merge mode hands in nothing
    r2 = phases.round_(s.a, 2, out=quiet)
    assert r2.stop == "no_candidate: no candidate passed its free rungs"
    assert Round(s.a, 2, run_arm=None, out=quiet).focus().merge == ()


def test_a_tool_installed_later_subsumes_the_entry_whose_fixes_the_line_now_passes(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "partial", actor=actor(), splits_table=rsi.QUIET_SPLITS)
    phases.round_(s.a, 0, out=quiet)
    s.r.mode = "tool"
    r1 = phases.round_(s.a, 1, out=quiet)
    assert r1.accepted and r1.screen["dir"] == "r1:package"
    (e,) = frontier_file(s.a)["entries"]
    assert e["status"] == "subsumed" and e["attempts"] == 0
    (row,) = ledger_rows(s.a, "frontier_pruned")
    assert row["entry"] == "f1" and row["reason"].startswith("subsumed")


# ---------------------------------------------------------------------------- the frontier on its own
def entry(i: int, fixes: int, *, attempts: int = 0, line: str = "rsi") -> FrontierEntry:
    return FrontierEntry(id="", round_index=i, tag=f"r{i}", dir="package", line=line, files_ref=f"f{i}",
                         declaration_ref=f"d{i}", base_package_ref="p", fixed=tuple(f"t{j}" for j in range(fixes)),
                         regressed=("q",), attempts=attempts)


def test_the_frontier_keeps_the_strongest_entries_orders_picks_and_prunes(tmp_path):
    f = Frontier(tmp_path, Settings(frontier_max=2, frontier_max_attempts=2))
    e1, dropped = f.add(entry(0, 1))
    assert (e1.id, dropped) == ("f1", [])
    e2, dropped = f.add(entry(1, 3))
    assert (e2.id, dropped) == ("f2", [])
    e3, dropped = f.add(entry(2, 2))
    assert (e3.id, dropped) == ("f3", ["f1"])                 # the weakest beyond frontier_max is dropped
    again, dropped = f.add(entry(2, 2))
    assert again is e3 and dropped == []                      # the same candidate again is the entry on record
    assert [e.id for e in f.active("rsi")] == ["f2", "f3"]
    assert [e.id for e in f.picks("rsi", 1)] == ["f2"] and [e.id for e in f.picks("rsi", 5)] == ["f2", "f3"]
    e, outcome = f.failed("f2", round_id="rsi-test/r3", dir_name="merge")
    assert (outcome, e.attempts, e.status) == ("retried", 1, "active")
    assert [e.id for e in f.picks("rsi", 1)] == ["f3"]        # the fewest attempts first
    assert f.failed("f2", round_id="rsi-test/r3", dir_name="merge")[1] == "on_record"
    e, outcome = f.failed("f2", round_id="rsi-test/r4", dir_name="merge", replacement=entry(4, 1))
    assert (outcome, e.status, e.attempts, e.tag, len(e.fixed)) == ("abandoned", "abandoned", 2, "r4", 1)
    assert f.prune("rsi", {"t0", "t1"}) == ["f3"] and f.get("f3").status == "subsumed"
    assert f.active("rsi") == [] and f.failed("f3", round_id="x", dir_name="m")[1] == "not_active"
    assert f.merged("f1", round_id="x", dir_name="m").status == "dropped"
    reloaded = Frontier(tmp_path, Settings())
    assert [(e.id, e.status) for e in reloaded.entries] == [("f1", "dropped"), ("f2", "abandoned"), ("f3", "subsumed")]
    assert reloaded.get("f2").history[-1] == {"round_id": "rsi-test/r4", "dir": "merge", "outcome": "abandoned"}


def test_for_prompt_shows_the_diff_and_counts_and_never_a_held_out_id(tmp_path):
    f = Frontier(tmp_path, Settings())
    e, _ = f.add(entry(0, 2))
    got = f.for_prompt(e, base_files={"a.py": "x = 1\n", "b.md": "keep\n"},
                       files={"a.py": "x = 1\ny = 2\n", "b.md": "keep\n", "c.py": "new\n"},
                       declaration={"components": [{"kind": "hook", "site": "on_finish", "trigger": "t0 failed"}]},
                       regressions={"evidence": [{"failed_kinds": ["x"]}]}, hide={"t0", "t1"},
                       scrub=lambda v: json.loads(json.dumps(v).replace("t0", "<a held-out task>")))
    assert (got["entry"], got["fixes"], got["regressions"], got["attempts"]) == ("f1", 2, 1, 0)
    assert set(got["diff"]) == {"a.py", "c.py"} and "+y = 2" in got["diff"]["a.py"]
    assert got["components"][0]["trigger"] == "<a held-out task> failed"
    assert "t0" not in json.dumps(got) and got["regressions_evidence"] == {"evidence": [{"failed_kinds": ["x"]}]}


def test_partial_of_needs_fixes_and_only_regression_clauses():
    s = Settings()
    base = dict(candidate="r0:p", outcome="screened", holdout=("a", "b", "c"), failing=("a", "b"), passing=("c",),
                fixed=("a",), regressed=("c",), net_flips=0, holdout_score=2, best_so_far=1, accepted=False,
                reasons=("net flips 0 below accept_min_net_flips 1", "1 regressions above accept_max_regressions 0"))
    assert partial_of(ScreenResult(**base), s)
    assert not partial_of(ScreenResult(**{**base, "fixed": ()}), s)                    # no fix
    assert not partial_of(ScreenResult(**{**base, "regressed": ()}), s)                # nothing to merge away
    assert not partial_of(ScreenResult(**{**base, "accepted": True}), s)
    assert not partial_of(ScreenResult(**{**base, "outcome": "invalid"}), s)
    assert not partial_of(ScreenResult(**{**base, "whole": True}), s)                  # a whole package needs 2 fixes
    assert partial_of(ScreenResult(**{**base, "whole": True, "fixed": ("a", "b")}), s)
    assert not partial_of(ScreenResult(**{**base, "holdout_score": 0, "best_so_far": 5}), s)   # the floor clause
    assert not partial_of(ScreenResult(**base), Settings(frontier_min_fixes=2))


# ---------------------------------------------------------------------------- the research sample
@pytest.fixture(scope="module")
def world(tmp_path_factory, fake_upstream):
    """The activity with its baseline read; nothing screened."""
    s = round_activity(tmp_path_factory.mktemp("research"), fake_upstream, "tool", actor=actor(),
                       splits_table=rsi.QUIET_SPLITS)
    s.round = Round(s.a, 0, run_arm=phases.run_arm, out=quiet)
    s.reading = s.round.reading()
    return s


def test_research_sample_takes_the_replays_false_triggers_first_then_a_fixed_order(world):
    reading = world.reading
    assert research_sample(STUDY, reading, fired=["rsi.notify_once"], n=2, seed="x")[0] == "rsi.notify_once"
    got = research_sample(STUDY, reading, fired=[], n=6, seed="x")
    assert sorted(got) == sorted(PASSING) and TICKET not in got        # the failing studied task is not sampled
    assert research_sample(STUDY, reading, fired=[], n=6, seed="x") == got         # the same seed, the same order
    assert research_sample(STUDY, reading, fired=[], n=1, seed="x") == got[:1]
    assert research_sample(STUDY, reading, fired=[], n=0, seed="x") == []


def test_research_run_records_a_regressed_studied_task_with_both_sessions(world):
    """A tool that posts twice fixes the notify tasks and breaks notify_once (exactly one message): under the
    candidate the sample's notify_once fails, and R gets the working line's and the candidate's sessions."""
    a = world.a
    twice = TOOL_CODE.replace('    return {"output": f"posted to the ops channel: {res.output}", "is_error": bool(res.is_error)}',
                              '    ctx.tool("api_fetch", {"method": "POST", "url": SLACK,\n'
                              '                           "body": json.dumps({"channel": "C1", "text": phrase + " again"})})\n'
                              '    return {"output": f"posted to the ops channel: {res.output}", "is_error": bool(res.is_error)}')
    assert twice != TOOL_CODE
    files = dict(world.round.package.files)
    m = json.loads(files["manifest.json"])
    m["tools"] = [*m.get("tools", []), TOOL_ENTRY]
    files["manifest.json"] = json.dumps(m, indent=1) + "\n"
    files["tools/post_channel_message.py"] = twice
    package = build(a.objects, files, limits=Limits.from_core(a.k), seed=a.package(a.seed_release()),
                    parent=world.round.package)
    got = research_run(a, run_arm=phases.run_arm, name="r9:twice", package=package, base_release=a.active_release(),
                       sample=list(PASSING), reading=world.reading, account=world.round.child("screen"), retries=0)
    assert got["outcome"] == "ok" and got["sample"] == list(PASSING) and got["regressed"] == ["rsi.notify_once"]
    sessions = got["sessions"]["rsi.notify_once"]
    assert sessions["working_line"] == (world.reading["rsi.notify_once"].evidence
                                        or world.reading["rsi.notify_once"].attempt_id)
    runs = a.results.runs()
    cand = [r for r in a.results.all() if runs[r.run].candidate == "r9:twice"]
    assert {r.task_id for r in cand} == set(PASSING) and all(runs[r.run].role == "research" for r in cand)
    assert sessions["candidate"] == next(r.evidence or r.attempt_id for r in cand if r.task_id == "rsi.notify_once")
    # run again: nothing is paid again (the attempts are on record)
    calls = world.actor.calls
    assert research_run(a, run_arm=phases.run_arm, name="r9:twice", package=package, base_release=a.active_release(),
                        sample=list(PASSING), reading=world.reading, account=world.round.child("screen"),
                        retries=0) == got
    assert world.actor.calls == calls


# ---------------------------------------------------------------------------- the settings' levels
def test_the_evidence_levels_hold_each_other_and_an_unknown_level_is_refused():
    assert Settings(regression_evidence="counts").evidence_at_least("counts")
    assert not Settings(regression_evidence="counts").evidence_at_least("abstract")
    assert Settings(regression_evidence="abstract").evidence_at_least("abstract")
    assert not Settings(regression_evidence="abstract").evidence_at_least("research")
    assert Settings().evidence_at_least("research") and Settings().replay_max_false_trigger == 0.2
    with pytest.raises(ValueError, match="regression_evidence"):
        Settings(regression_evidence="all")
    with pytest.raises(ValueError, match="frontier_min_fixes"):
        Settings(frontier_min_fixes=0)
    with pytest.raises(ValueError, match="counts"):
        Settings(frontier_max=-1)
    with pytest.raises(ValueError, match="not an improver setting"):
        Settings.from_json({"frontier": 1})


def test_the_counts_level_gives_r_counts_only(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "partial", actor=actor(), splits_table=rsi.QUIET_SPLITS,
                       improver={"regression_evidence": "counts"})
    phases.round_(s.a, 0, out=quiet)
    res = screens_of(s.a, "r0")["package"]
    assert res.partial and res.research_sample == () and res.regression_ref
    obj = s.a.objects.get(res.regression_ref)
    assert obj["level"] == "counts" and obj["records"] == [] and obj["research"]["sample"] == []
    runs = s.a.results.runs()
    assert not any(runs[r.run].role == "research" for r in s.a.results.all())
    s.r.mode = "merge"
    s.r.read = []
    phases.round_(s.a, 1, out=quiet)
    (p,) = inputs_read(s, "regressions.json")["packages"]
    assert "evidence" not in p and "research" not in p and (p["fixes"], p["regressions"]) == (2, 1)


# ---------------------------------------------------------------------------- V0: text only grows, word lists
@pytest.fixture(scope="module")
def ctx(world):
    return world.round.vctx()


def decl(components, *, dir_name="package", merge_of=""):
    return Declaration(dir=dir_name, mechanism="m", merge_of=merge_of, components=tuple(
        Component(kind=k, site=site, cause_ids=("rsi-test/r0:missed_action",), trigger="t", rewrite=rewrite,
                  predicted_affected=(TICKET,) if k in ("tool", "hook") else ())
        for k, site, rewrite in components))


def test_v0_refuses_a_prompt_that_deletes_or_rewrites_existing_lines_and_takes_an_addition(ctx):
    parent = ctx.parent
    path = "prompt/finish.md"                      # the seed's system prompt is empty: the finish text is the one to grow
    lines = parent.files[path].splitlines()
    added = {**parent.files, path: parent.files[path] + "\nCheck every planned item before you finish.\n"}
    assert static_check(added, decl([("prompt", "finish", False)]), ctx)["passed"]
    cut = {**parent.files, path: "\n".join(lines[1:]) + "\n"}
    got = static_check(cut, decl([("prompt", "finish", False)]), ctx)
    assert not got["passed"] and "existing text only grows" in got["detail"] and "line 1" in got["detail"]
    rewritten = {**parent.files, path: "\n".join(["Something else."] + lines[1:]) + "\n"}
    got = static_check(rewritten, decl([("prompt", "finish", False)]), ctx)
    assert not got["passed"] and "rewrite: true" in got["detail"]
    # declared rewrite, alone: it passes V0 (and is screened as a whole-footprint package)
    assert static_check(rewritten, decl([("prompt", "finish", True)]), ctx)["passed"]
    # declared rewrite beside another component: refused
    with_tool = {**rewritten, "tools/post_channel_message.py": TOOL_CODE}
    m = json.loads(with_tool["manifest.json"])
    m["tools"] = [*m.get("tools", []), TOOL_ENTRY]
    with_tool["manifest.json"] = json.dumps(m, indent=1) + "\n"
    got = static_check(with_tool, decl([("prompt", "finish", True), ("tool", TOOL_ENTRY["name"], False)]), ctx)
    assert not got["passed"] and "holds that one component alone" in got["detail"]
    # the rule is K's: off, a rewrite is an ordinary change
    problems = text_problems(parent.files, rewritten, decl([("prompt", "finish", False)]))
    assert len(problems) == 1
    assert text_problems(parent.files, added, decl([("prompt", "finish", False)])) == []


def test_v0_notes_a_hook_whose_trigger_is_a_word_list_and_does_not_refuse_it(ctx):
    files = {**ctx.parent.files, "middlewares/remind_ops.py": WORDLIST_HOOK_CODE}
    m = json.loads(files["manifest.json"])
    m["hooks"] = [*m.get("hooks", []), HOOK_ENTRY]
    files["manifest.json"] = json.dumps(m, indent=1) + "\n"
    got = static_check(files, decl([("hook", "on_finish", False)]), ctx)
    assert got["passed"] and got["heuristic_terms"] == {"middlewares/remind_ops.py": ["archived", "on leave", "pending",
                                                                                     "reserved"]}
    assert "note: the trigger conditions on a word list" in got["detail"]
    verdict = run_ladder(files, decl([("hook", "on_finish", False)]), ctx)
    assert verdict.passed("V0") and verdict.steps["V0"]["heuristic_terms"]
    plain = {**files, "middlewares/remind_ops.py": CONDITIONAL_HOOK_CODE}
    assert "heuristic_terms" not in static_check(plain, decl([("hook", "on_finish", False)]), ctx)
    assert "heuristic_terms" not in static_check({**files, "middlewares/remind_ops.py": PARTIAL_HOOK_CODE},
                                                 decl([("hook", "on_finish", False)]), ctx)


@pytest.mark.parametrize("code, terms", [
    ('STATUSES = ("pending", "reserved", "on leave")\ndef f(x):\n    return x in STATUSES\n',
     ["on leave", "pending", "reserved"]),
    ('def f(xs):\n    return [x for x in xs if x in ("pending", "reserved", "on leave", "n/a")]\n',
     ["on leave", "pending", "reserved"]),
    ('import re\nP = re.compile(r"\\b(pending|reserved|on leave)\\b")\n', ["on leave", "pending", "reserved"]),
    ('def f(t):\n    return any(w in t for w in ["pending", "reserved", "on leave"])\n',
     ["on leave", "pending", "reserved"]),
    ('SCHEMA = {"required": ["pending", "reserved", "on leave"]}\n', []),          # data, not a condition
    ('def f(x):\n    return x in ("pending", "reserved")\n', []),                    # under the minimum
    ('STATUSES = ("pending", "in_progress", "completed")\ndef f(c):\n    return c in STATUSES\n', []),
    ('def f(:\n', []),
])
def test_heuristic_terms(code, terms):
    assert heuristic_terms(code, 3) == terms
    if terms:
        assert heuristic_terms(code, len(terms) + 1) == []


def test_a_declaration_reads_rewrite_and_merge_of():
    from ouroloop.improver.declaration import read_declaration
    raw = {"mechanism": "m", "components": [{"kind": "prompt", "site": "system", "cause_ids": ["c"], "trigger": "t",
                                             "rewrite": True}]}
    d, why = read_declaration(raw, dirs=["package"], kinds_allowed=["prompt", "hook"])
    assert not why and d.components[0].rewrite
    bad = {**raw, "components": [{**raw["components"][0], "kind": "hook", "site": "on_finish",
                                  "predicted_affected": ["t"]}]}
    assert "rewrite is a prompt's or a skill's" in read_declaration(bad, dirs=["package"],
                                                                     kinds_allowed=["prompt", "hook"])[1]
    assert "merge_of 'f9' is not a frontier entry" in read_declaration({**raw, "merge_of": "f9"}, dirs=["package"],
                                                                        kinds_allowed=["prompt"])[1]
    d, why = read_declaration({**raw, "merge_of": "f1"}, dirs=["package"], kinds_allowed=["prompt"],
                              merge_ids=("f1",))
    assert not why and d.merge_of == "f1"
