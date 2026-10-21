"""Stage 4j (DESIGN §6.19; IMPROVER §9.6 and the v1.9 items of §7, §8, §9, §13): R's package is tried on the studied
failing tasks it claims before the paid held-out screen, the trial's results go back to the session, and only a package
that fixed one of them in-sample is screened; a package of tool/hook/skill components regresses only where one acted;
the merge's replay; the stream window; the stream's first encounters imported; V0's unregistered-hook note; the
word-list signal without the false positives finance-v8 read; the edit reminders; the research sample priced after the
screen — on the fake upstream's rsi domain with the forgetful actor (tests/rsi.py) and the scripted R (scripted_r.py).

Mode `trial`: R first hands in a hook whose pushback the actor does not heed — it acts (V2b passes) and fixes nothing
in the trial on notify_ticket; the host hands the trial back, R rewrites the hook into the one that works, the second
trial fixes the ticket, the package is screened on the held-out pair and installed. Mode `trial_bad` hands the weak
hook in again: `no_trial_fix`, nothing screened, nothing paid for a screen."""
from __future__ import annotations

import json
import shutil

import pytest
import rsi
from rounds import ledger_rows, quiet, round_activity, round_state
from scripted_r import HOOK_CODE, HOOK_ENTRY, LOST_HOOK_CODE, TOOL_ENTRY, WEAK_HOOK_CODE  # noqa: F401

from ouroloop.activity import phases
from ouroloop.activity.report import build_report, render_markdown
from ouroloop.harness.kinds import MEMORY_PATH
from ouroloop.harness.package import Limits, build
from ouroloop.improver import round as round_mod
from ouroloop.improver import screen as sc
from ouroloop.improver.components import components
from ouroloop.improver.declaration import Component, Declaration
from ouroloop.improver.diagnose import Cause
from ouroloop.improver.evidence import Evidence
from ouroloop.improver.frontier import Frontier, FrontierEntry
from ouroloop.improver.ledger import Ledger
from ouroloop.improver.round import Round
from ouroloop.improver.screen import ScreenResult
from ouroloop.improver.session import EDIT_REMINDERS, CodingSession, WorkTree
from ouroloop.improver.settings import Settings
from ouroloop.improver.trial import TrialResult, trial_tasks
from ouroloop.improver.verify import run_ladder
from ouroloop.improver.verify.static import heuristic_terms, static_check, unregistered_hooks
from ouroloop.runtime.chat import ChatTurn, ToolCall

HELD, STUDY, TICKET = rsi.HELD, rsi.STUDY, "rsi.notify_ticket"
INERT_HOOK = "def inert(ctx, event):\n    return None\n"
INERT_ENTRY = {"name": "inert_ops", "site": "on_finish", "entrypoint": "middlewares/inert_ops.py:inert"}
ROUND, ACCOUNT = "rsi-test/r0", "rsi-test:round-0"
NOTIFY_ONCE = "rsi.notify_once"


def rows_of_role(a, role: str) -> list:
    runs = a.results.runs()
    return [r for r in a.results.all() if runs[r.run].role == role]


def screens_of(a, tag: str = "r0") -> dict[str, ScreenResult]:
    state = round_state(a, tag)
    return {r["dir"]: ScreenResult.from_json(a.objects.get(r["screen"]))
            for r in (state["steps"].get("screened") or {}).get("screens") or ()}


def book(a) -> dict:
    return json.loads((a.root / "rsi" / "budget" / "book.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------- the trial and the revision
@pytest.fixture(scope="module")
def trial(tmp_path_factory, fake_upstream):
    s = round_activity(tmp_path_factory.mktemp("trial"), fake_upstream, "trial")
    s.lines = []
    s.result = phases.round_(s.a, 0, out=s.lines.append)
    return s


def test_the_weak_hook_fixes_nothing_in_the_trial_r_rewrites_it_and_the_second_trial_fixes_the_ticket(trial):
    a, r = trial.a, trial.result
    assert r.status == "done" and r.accepted and r.screen["dir"] == "r0:package"
    rows = ledger_rows(a, "trialed")
    assert [(x["dir"], x["iteration"], x["outcome"], x["trial_tasks"], x["trial_fixed"], x["passed"]) for x in rows] == \
        [("package", 0, "ok", [TICKET], [], False), ("package", 1, "ok", [TICKET], [TICKET], True)]
    assert [x["attempts"] for x in rows] == [1, 1] and rows[1]["trial_failed"] == []
    # two trial attempts on the studied failure, each under its own package (the files changed in between)
    trials = rows_of_role(a, "trial")
    runs = a.results.runs()
    assert [t.task_id for t in trials] == [TICKET, TICKET] and [t.success for t in trials] == [False, True]
    assert len({runs[t.run].package_ref for t in trials}) == 2 and all(runs[t.run].candidate == "r0:package"
                                                                        for t in trials)
    # what R was told: the task, that it failed, the checks, the session it may read, the revisions left
    (text,) = trial.r.trial_feedback
    assert text.startswith("Trial of candidates/package") and f"- {TICKET}: FAILED" in text
    assert "slack_message_contains" in text and "session " in text and "1 revision(s) left" not in text \
        and "2 revision(s) left" in text
    assert "fixes fewer than 1" in text
    # the final package is the working hook; the implemented step keeps the trial's summary
    (item,) = round_state(a, 0)["steps"]["implemented"]["candidates"]
    files = a.objects.get(item["files"])
    assert files["middlewares/remind_ops.py"] == HOOK_CODE
    assert (item["trial"]["ran"], item["trial"]["fixes"], item["trial"]["passed"], item["trial"]["attempts"]) == \
        (True, 1, True, 2)
    assert [i["iteration"] for i in item["trial"]["iterations"]] == [0, 1]
    first = TrialResult.from_json(a.objects.get(item["trial"]["iterations"][0]["trial"]))
    assert first.failed == (TICKET,) and first.footprints[TICKET] == ["hook:remind_ops"] and first.sessions[TICKET]
    # the trial's session was opened to R
    assert any("trial of package (iteration 0): ok, 0 fixed of 1 tried" in line for line in trial.lines)
    assert any("trial of package (iteration 1): ok, 1 fixed of 1 tried" in line for line in trial.lines)


def test_the_trial_is_paid_from_the_rounds_trial_account_and_the_screen_still_reads_the_held_out_pair(trial):
    a = trial.a
    data = book(a)
    scopes = {x["scope_id"] for x in data["reservations"].values() if x["status"] == "settled"}
    assert f"{ACCOUNT}:trial" in scopes and f"{ACCOUNT}:screen" in scopes
    assert data["scopes"][f"{ACCOUNT}:trial"]["parent"] == ACCOUNT
    res = screens_of(a)["package"]
    assert res.fixed == HELD and res.regressed == () and res.accepted
    # v1.9: the screen reads how much of the held-out part the hook acted on
    assert res.acted == HELD and res.footprint_share == 1.0 and res.unconditioned_regressed == ()
    row = ledger_rows(a, "screened")[-1]
    assert (row["acted"], row["footprint_share"], row["unconditioned_regressed"]) == (2, 1.0, [])
    assert "kept_fixes" not in row
    # the ledger's order: declared, the two trials, verified
    stages = [x["stage"] for x in ledger_rows(a) if x.get("round_id") == ROUND]
    assert stages[:7] == ["measured", "diagnosed", "focused", "declared", "trialed", "trialed", "verified"]
    verified = [x for x in ledger_rows(a, "verified") if x["round_id"] == ROUND]
    assert verified[0]["passed"] and verified[0].get("outcome", "") == "" and verified[0]["trial_fixed"] == [TICKET]
    # the projection R reads next round names the studied task of the trial, never a held-out one
    proj = [r for r in Ledger(a.root).projection(line="rsi", hide=set(HELD)) if r["stage"] == "trialed"]
    assert [p["trial_tasks"] for p in proj] == [[TICKET], [TICKET]] and not any(t in json.dumps(proj) for t in HELD)


def test_the_report_shows_the_trials_the_footprint_share_and_the_regressions_not_charged(trial):
    report = build_report(trial.a)
    (r0,) = report["rounds"]
    assert r0["trials"] == [{"dir": "package", "iteration": 0, "outcome": "ok", "tasks": 1, "fixed": 0, "attempts": 1,
                             "passed": False},
                            {"dir": "package", "iteration": 1, "outcome": "ok", "tasks": 1, "fixed": 1, "attempts": 1,
                             "passed": True}]
    (sc0,) = r0["screens"]
    assert (sc0["acted"], sc0["footprint_share"], sc0["unconditioned_regressed"], sc0["kept_fixes"]) == (2, 1.0, 0, None)
    md = render_markdown(report)
    assert "trial package (iteration 0): ok, fixed 0 of 1 studied failing tasks, 1 attempts — not screened" in md
    assert "trial package (iteration 1): ok, fixed 1 of 1 studied failing tasks, 1 attempts — screened" in md
    assert "acted on 2/2 (share 1.0)" in md


def test_a_package_that_fixes_nothing_in_its_trials_is_not_screened_and_the_screen_is_not_paid(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "trial_bad", improver={"trial_max_rounds": 1})
    r = phases.round_(s.a, 0, out=quiet)
    assert r.status == "stopped" and r.stop == "no_candidate: no candidate passed its free rungs"
    rows = ledger_rows(s.a, "trialed")
    assert [(x["iteration"], x["trial_fixed"], x["passed"]) for x in rows] == [(0, [], False), (1, [], False)]
    (v,) = [x for x in ledger_rows(s.a, "verified") if x["round_id"] == ROUND]
    assert v["passed"] and v["outcome"] == "no_trial_fix" and v["trial_fixed"] == [] and v["attempts"] == 2
    assert rows_of_role(s.a, "screen") == [] and rows_of_role(s.a, "confirm") == []
    assert [t.task_id for t in rows_of_role(s.a, "trial")] == [TICKET, TICKET]
    assert len(s.r.trial_feedback) == 1 and "1 revision(s) left" in s.r.trial_feedback[0]
    assert round_state(s.a, 0)["steps"]["verified"]["passed"] == []
    assert "screened" not in round_state(s.a, 0)["steps"]
    assert s.a.active_release().ref() == s.a.seed_release().ref()


def test_the_rounds_trial_attempts_cap_ends_the_trials_and_trial_min_fixes_zero_keeps_them_as_feedback(
        tmp_path, fake_upstream):
    # one attempt a round: the first trial uses it, no revision is offered, the package is not screened
    s = round_activity(tmp_path / "cap", fake_upstream, "trial_bad", improver={"trial_max_attempts": 1})
    r = phases.round_(s.a, 0, out=quiet)
    assert r.stop == "no_candidate: no candidate passed its free rungs"
    assert len(rows_of_role(s.a, "trial")) == 1 and s.r.trial_feedback == []
    (row,) = ledger_rows(s.a, "trialed")
    assert (row["iteration"], row["passed"]) == (0, False)
    # trial_min_fixes 0: the trial runs, nothing is asked of R, the package is screened as before
    s = round_activity(tmp_path / "feedback", fake_upstream, "trial_bad", improver={"trial_min_fixes": 0})
    r = phases.round_(s.a, 0, out=quiet)
    assert r.status == "done" and not r.accepted
    (row,) = ledger_rows(s.a, "trialed")
    assert (row["iteration"], row["trial_fixed"], row["passed"]) == (0, [], True) and s.r.trial_feedback == []
    assert "package" in screens_of(s.a)


def test_with_the_trial_off_the_package_is_screened_as_before(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "trial_bad", improver={"trial_max_tasks": 0})
    r = phases.round_(s.a, 0, out=quiet)
    assert r.status == "done" and ledger_rows(s.a, "trialed") == [] and rows_of_role(s.a, "trial") == []
    (item,) = round_state(s.a, 0)["steps"]["implemented"]["candidates"]
    assert item["trial"] is None and "package" in screens_of(s.a)


def test_a_round_killed_in_its_screen_reads_its_trial_attempts_again_and_pays_no_trial_twice(tmp_path, fake_upstream):
    class CrashOnce(rsi.BusinessWorker):
        def __init__(self):
            super().__init__(forget_notify=True)
            self.at = None

        def chat(self, system, messages, specs, **kw):
            task = next((m["content"] for m in messages if m.get("role") == "user"), "")
            if self.at and self.at in task:
                self.at = None
                raise KeyboardInterrupt("killed mid-screen")
            return super().chat(system, messages, specs, **kw)
    actor = CrashOnce()
    s = round_activity(tmp_path, fake_upstream, "trial", actor=actor)
    actor.at = s.a.suite.tasks[HELD[1]].trigger
    with pytest.raises(KeyboardInterrupt):
        phases.round_(s.a, 0, out=quiet)
    trials = len(rows_of_role(s.a, "trial"))
    assert trials == 2 and "screened" not in round_state(s.a, 0)["steps"]
    calls = actor.calls
    r = phases.round_(s.a, 0, out=quiet)
    assert r.status == "done" and r.accepted
    assert len(rows_of_role(s.a, "trial")) == trials                    # the saved trial summary stands
    (item,) = round_state(s.a, 0)["steps"]["implemented"]["candidates"]
    assert item["trial"]["passed"] and len(item["trial"]["iterations"]) == 2
    assert actor.calls > calls                                         # the screen's missing attempt was paid


def test_trial_tasks_are_the_claimed_studied_failing_tasks_in_order():
    def comp(kind, predicted=(), causes=("c1",)):
        return Component(kind=kind, site="x" if kind != "agent" else None, cause_ids=tuple(causes),
                         predicted_affected=tuple(predicted), trigger="t")
    causes = [Cause(cause_id="c1", name="missed_action", cases=(), tasks=("t3", "t1", "t9"), memory_entries=(),
                    mechanizable=True),
              Cause(cause_id="c2", name="other", cases=(), tasks=("t5",), memory_entries=(), mechanizable=False)]
    failing = ["t1", "t2", "t3", "t4", "t5"]
    d = Declaration(dir="package", mechanism="m", components=(comp("hook", ("t3", "t2", "t8")),))
    assert trial_tasks(d, causes=causes, study_failing=failing, n=4) == ["t2", "t3"]       # t8 is not failing
    assert trial_tasks(d, causes=causes, study_failing=failing, n=1) == ["t2"]
    assert trial_tasks(d, causes=causes, study_failing=failing, n=0) == []
    d = Declaration(dir="package", mechanism="m", components=(comp("prompt", (), ("c1", "c2")),))
    assert trial_tasks(d, causes=causes, study_failing=failing, n=4) == ["t1", "t3", "t5"]   # the causes' tasks
    d = Declaration(dir="package", mechanism="m", components=(comp("prompt", (), ("c9",)),))
    assert trial_tasks(d, causes=causes, study_failing=failing, n=4) == []


def test_a_session_that_ends_at_its_cap_is_on_the_ledger(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "tool", improver={"session_max_turns": 2})
    r = phases.round_(s.a, 0, out=quiet)
    assert r.stop == "no_candidate: no candidate passed its free rungs"
    (row,) = ledger_rows(s.a, "session_ended")
    assert (row["reason"], row["turns"], row["declared"]) == ("the session used its 2 turns", 2, 0)
    assert row["edits"] >= 1


# ---------------------------------------------------------------------------- regressions by footprint
@pytest.fixture(scope="module")
def world(tmp_path_factory, fake_upstream):
    """The activity with its baseline read and nothing installed; the screen helpers of test_improver_session_screen."""
    s = round_activity(tmp_path_factory.mktemp("world"), fake_upstream, "tool")
    s.base_release = s.a.active_release()
    s.base = s.a.package(s.base_release)
    s.evidence = Evidence(s.a)
    s.reading = s.evidence.reading(s.base_release.package_ref)
    s.round = Round(s.a, 0, run_arm=phases.run_arm, out=quiet)
    return s


def candidate(world, *, hook_code=None, tool=False):
    files = {p: x for p, x in world.base.files.items() if p != MEMORY_PATH}
    manifest = json.loads(files["manifest.json"])
    if hook_code is not None:
        files["middlewares/inert_ops.py"] = hook_code
        manifest["hooks"] = [*manifest.get("hooks", []), INERT_ENTRY]
    if tool:
        files["tools/post_channel_message.py"] = "def run(ctx, args):\n    return {'output': 'posted', 'is_error': False}\n"
        manifest["tools"] = [*manifest.get("tools", []), TOOL_ENTRY]
    files["manifest.json"] = json.dumps(manifest, indent=1) + "\n"
    package = build(world.a.objects, files, limits=Limits.from_core(world.a.k),
                    seed=world.a.package(world.a.seed_release()), parent=world.base)
    return files, package


def run_screen(world, name, files, package, *, holdout, conditions=None, settings=None):
    conditions = sc.condition(components(world.base, files)) if conditions is None else conditions
    return sc.screen(world.a, run_arm=phases.run_arm, name=f"r9:{name}", package=package,
                     base_release=world.base_release, holdout=list(holdout), reading=world.reading,
                     evidence=world.evidence, conditions=conditions, account=world.a.account("screen-tests"),
                     retries=0, best_so_far=0, settings=settings or Settings())


def test_a_confirmed_regression_needs_the_footprint_on_every_candidate_reading(world):
    """A tool the actor calls on every attempt and that breaks notify_once (it says it posted and did not: the actor
    posts nothing itself): the tool acted on both readings, W passes both of its own — a confirmed regression."""
    files, package = candidate(world, tool=True)
    res = run_screen(world, "silent", files, package, holdout=[NOTIFY_ONCE])
    assert res.flipped_regressed == (NOTIFY_ONCE,) and res.regressed == (NOTIFY_ONCE,)
    assert res.acted == (NOTIFY_ONCE,) and res.footprint_share == 1.0 and res.unconditioned_regressed == ()
    assert res.noisy == () and res.unconfirmed == ()


def test_a_passing_task_that_fails_where_no_component_acted_is_not_a_regression(world):
    """The actor posts twice from now on (repeat_notify), so notify_once — exactly one message — fails under any
    package; the candidate's inert hook never acts there: under regression_by_footprint the flip is
    `unconditioned_regressed`, not confirmed, not counted; a whole-footprint package is charged with it."""
    files, package = candidate(world, hook_code=INERT_HOOK)
    world.actor.repeat_notify = True
    try:
        res = run_screen(world, "inert", files, package, holdout=[NOTIFY_ONCE])
        assert res.passing == (NOTIFY_ONCE,) and res.flipped_regressed == () and res.regressed == ()
        assert res.unconditioned_regressed == (NOTIFY_ONCE,) and res.confirm_attempts == 0
        assert res.acted == () and res.footprint_share == 0.0 and res.holdout_score == 0
        assert res.reasons == ("net flips 0 below accept_min_net_flips 1",) and not res.whole
        # the rule is K's: off, the flip is a regression candidate and goes to the confirmation
        res = run_screen(world, "inert-off", files, package, holdout=[NOTIFY_ONCE],
                         settings=Settings(regression_by_footprint=False))
        assert res.flipped_regressed == (NOTIFY_ONCE,) and res.unconditioned_regressed == ()
        assert res.confirm_attempts >= 1
        # a whole-footprint package (a prompt) regresses everywhere, as before
        res = run_screen(world, "whole", files, package, holdout=[NOTIFY_ONCE],
                         conditions=[{"kind": "prompt", "name": "*"}])
        assert res.whole and res.flipped_regressed == (NOTIFY_ONCE,) and res.unconditioned_regressed == ()
        assert res.acted == (NOTIFY_ONCE,) and res.footprint_share == 1.0
    finally:
        world.actor.repeat_notify = False


# ---------------------------------------------------------------------------- V0: unregistered hooks, word lists
def test_v0_notes_a_hook_function_that_no_manifest_hook_names_and_does_not_refuse_it(world):
    ctx = world.round.vctx()
    files = {**ctx.parent.files, "middlewares/remind_ops.py": HOOK_CODE + "\n\ndef lone_request(ctx, event):\n"
                                                                          "    return None\n"}
    m = json.loads(files["manifest.json"])
    m["hooks"] = [*m.get("hooks", []), HOOK_ENTRY]
    files["manifest.json"] = json.dumps(m, indent=1) + "\n"
    d = Declaration(dir="package", mechanism="m", components=(
        Component(kind="hook", site="on_finish", cause_ids=("rsi-test/r0:missed_action",), trigger="t",
                  predicted_affected=(TICKET,)),))
    got = static_check(files, d, ctx)
    assert got["passed"] and got["unregistered_hooks"] == {"middlewares/remind_ops.py": ["lone_request"]}
    assert "lone_request" in got["detail"] and "never run" in got["detail"]
    plain = {**files, "middlewares/remind_ops.py": HOOK_CODE}
    assert "unregistered_hooks" not in static_check(plain, d, ctx)
    manifest = {"hooks": [{"name": "r", "site": "on_finish", "entrypoint": "middlewares/x.py:remind"}]}
    assert unregistered_hooks("middlewares/x.py", "def remind(ctx, event):\n    pass\n\ndef other(a, b):\n    pass\n"
                                                  "\ndef late(ctx, event, more):\n    pass\n", manifest) == []
    assert unregistered_hooks("middlewares/x.py", "def collect(ctx, event):\n    pass\n", manifest) == ["collect"]
    assert unregistered_hooks("middlewares/x.py", "def f(:\n", manifest) == []


@pytest.mark.parametrize("code, terms", [
    # finance-v8's false positives: an HTTP-method tuple, lists of tool names, camelCase
    ('def f(e):\n    return str(e.get("method", "GET")).upper() in ("GET", "HEAD", "POST", "PATCH")\n', []),
    ('TOOLS = ("find", "query", "search", "sendMail")\ndef f(e):\n    return e.get("name") in TOOLS\n', []),
    ('TOOLS = ("find", "query", "search", "sendMail")\ndef f(call):\n    return call.name in TOOLS\n', []),
    ('TOOLS = ("find", "query", "search", "sendMail")\ndef f(name):\n    return name in TOOLS\n', []),
    ('NAMES = ("find", "query", "search", "sendMail")\ndef f(call):\n    for n in NAMES:\n        if n == call.name:\n'
     '            return True\n', []),
    ('WORDS = ("GET", "HEAD", "POST")\ndef f(text):\n    return any(w in text for w in WORDS)\n', []),
    ('WORDS = ("sendMail", "readMail", "listMail")\ndef f(text):\n    return any(w in text for w in WORDS)\n', []),
    ('WORDS = ("on", "off", "n/a", "ok")\ndef f(text):\n    return any(w in text for w in WORDS)\n', []),
    # a vocabulary compared with text or a record's value still counts
    ('WORDS = ("Paid", "Matched", "Investigate", "Reconciled")\ndef f(cell):\n    return cell in WORDS\n',
     ["Investigate", "Matched", "Paid", "Reconciled"]),
    ('WORDS = ("pending", "reserved", "on leave", "ok")\ndef f(text):\n    return any(w in text for w in WORDS)\n',
     ["on leave", "pending", "reserved"]),
    ('STATUSES = ("pending", "reserved", "on leave")\ndef f(row):\n    return row["status"] in STATUSES\n',
     ["on leave", "pending", "reserved"]),
    ('import re\nP = re.compile(r"\\b(pending|reserved|on leave|GET)\\b")\n', ["on leave", "pending", "reserved"]),
])
def test_heuristic_terms_v19(code, terms):
    assert heuristic_terms(code, 3) == terms


# ---------------------------------------------------------------------------- the merge's replay and window
def test_a_merge_that_no_longer_acts_where_the_entry_fixed_is_sent_back_and_not_screened(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "partial", actor=rsi.BusinessWorker(forget_notify=True, suggestible=True),
                       splits_table=rsi.QUIET_SPLITS)
    r0 = phases.round_(s.a, 0, out=quiet)
    assert not r0.accepted
    (e,) = Frontier(s.a.root, Settings()).entries
    assert sorted(e.fixed_sessions) == sorted(rsi.HELD) and all(e.fixed_sessions.values())
    # the fixes' evidence: why the hook worked, de-identified
    obj = s.a.objects.get(e.regression_ref)
    assert len(obj["fixes_evidence"]) == 2
    rec = obj["fixes_evidence"][0]
    assert rec["fixed_kinds"] == ["slack_message_contains"] and rec["footprints"][0]["kind"] == "hook"
    assert "#ops" in rec["footprints"][0]["said"] and "POST slack.com/api/chat.postMessage" in rec["writes"]["after"]
    assert not any(t in json.dumps(obj["fixes_evidence"]) for t in rsi.HELD_QUIET)
    s.r.mode = "merge_lost"
    s.r.read = []
    lines = []
    r1 = phases.round_(s.a, 1, out=lines.append)
    assert r1.stop == "no_candidate: no candidate passed its free rungs"
    (v,) = [x for x in ledger_rows(s.a, "verified") if x["round_id"] == "rsi-test/r1"]
    assert not v["passed"] and "merge: FAILED" in v["reason"]
    assert "the merge acts on 0 of the 2 recorded attempts the entry fixed" in v["reason"]
    assert not any(t in v["reason"] for t in rsi.HELD_QUIET)
    assert rows_of_role(s.a, "screen") == [r for r in rows_of_role(s.a, "screen") if
                                           s.a.results.runs()[r.run].candidate == "r0:package"]
    # R read the fixes' evidence in inputs/frontier.json
    frontier = json.loads([m for m in s.r.read if m[0] == "frontier.json"][-1][1])
    assert frontier["entries"][0]["fixes_evidence"][0]["fixed_kinds"] == ["slack_message_contains"]
    # the session was asked once to revise after the merge check (the second finish hands the same files in)
    assert s.r.calls.count("session") >= 5
    # the entry counts no attempt: the merge never reached the screen
    (e,) = Frontier(s.a.root, Settings()).entries
    assert e.status == "active" and e.attempts == 0


def test_a_stream_rounds_window_holds_the_fixed_and_regressed_tasks_of_the_entries_it_merges(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "tool", baseline=False, splits_table=rsi.NO_DEV_SPLITS,
                       improver={"stream_screen_max": 1})
    order = phases.stream_order_of(s.a)
    rd = Round(s.a, 0, run_arm=None, out=quiet, pool=order)
    held = list(rd.split()[1])
    assert len(held) >= 2 and rd.screen_holdout() == held[-1:]
    f = Frontier(s.a.root, Settings())
    f.add(FrontierEntry(id="", round_index=0, tag="s0", dir="package", line="rsi", files_ref="f", declaration_ref="d",
                        base_package_ref="p", fixed=(held[0],), regressed=()))
    rd = Round(s.a, 0, run_arm=None, out=quiet, pool=order)
    assert rd.screen_holdout() == [held[0], held[-1]] if held[0] != held[-1] else held[-1:]


# ---------------------------------------------------------------------------- the stream's first encounters imported
def test_the_streams_first_encounters_are_imported_and_the_next_stream_pays_none_of_them(tmp_path, fake_upstream):
    provider = rsi.BusinessWorker(forget_notify=True)
    first = round_activity(tmp_path / "first", fake_upstream, "none", actor=provider, baseline=False,
                           splits_table=rsi.NO_DEV_SPLITS, schedule={"stream_batch": 4})
    phases.stream(first.a, out=quiet)
    scored = phases.stream_scored(first.a)
    assert len(scored) == 6
    paid = provider.calls
    target = tmp_path / "second" / "ws" / "bench" / rsi.DOMAIN / "splits.json"
    target.parent.mkdir(parents=True)
    shutil.copy(first.a.results.manifest_path(), target)
    second = round_activity(tmp_path / "second", fake_upstream, "none", actor=provider, baseline=False,
                            splits_table=rsi.NO_DEV_SPLITS, schedule={"stream_batch": 4},
                            rsi={"scope_id": "rsi-test", "baseline_from": str(first.a.root)})
    lines = []
    report = phases.stream(second.a, out=lines.append)
    assert provider.calls == paid                                      # no first encounter paid again
    (event,) = second.a.events.read("baseline_imported")
    assert event["roles"] == {"stream": 6} and event["attempts"] == 6
    assert {r.attempt_id for r in rows_of_role(second.a, "stream")} == {r.attempt_id for r in scored.values()}
    imported = list(second.a.events.read("stream_scored"))
    assert len(imported) == 6 and all(e["imported_from"] == str(first.a.root) for e in imported)
    assert {e["task"] for e in imported} == set(scored)
    assert report["status"] == "done" and [b["tasks_scored"] for b in report["batches"]] == [4, 2]
    assert report["first_encounter"]["passed"] == sum(1 for r in scored.values() if r.success)
    assert any(line.startswith("baseline   imported 6 W0 attempts") and "'stream': 6" in line for line in lines)
    # run again: nothing copied twice
    assert phases.import_baseline(second.a, first.a.root, out=quiet)["attempts"] == 0
    # v1.10 (finance-v10, 2026-10-21): a third workspace imports from the second, whose readings are themselves
    # imported — the events carry `imported_from` already and the chain's last link is recorded; an import cut
    # short after the rows (the first one crashed on exactly this) is completed by the next run
    from ouroloop.runtime.records import session_file
    target = tmp_path / "third" / "ws" / "bench" / rsi.DOMAIN / "splits.json"
    target.parent.mkdir(parents=True)
    shutil.copy(first.a.results.manifest_path(), target)
    third = round_activity(tmp_path / "third", fake_upstream, "none", actor=provider, baseline=False,
                           splits_table=rsi.NO_DEV_SPLITS, schedule={"stream_batch": 4},
                           rsi={"scope_id": "rsi-test", "baseline_from": str(second.a.root)})
    original = third.a.events.append

    def cut(kind, **fields):
        if kind == "stream_scored":
            raise RuntimeError("cut short")
        return original(kind, **fields)
    third.a.events.append = cut
    with pytest.raises(RuntimeError):
        phases.import_baseline(third.a, second.a.root, out=quiet)
    third.a.events.append = original
    assert len(list(rows_of_role(third.a, "stream"))) == 6 and not list(third.a.events.read("stream_scored"))
    got = phases.import_baseline(third.a, second.a.root, out=quiet)
    assert (got["attempts"], got["events"], got["transcripts"]) == (0, 6, 6)
    chained = list(third.a.events.read("stream_scored"))
    assert len(chained) == 6 and all(e["imported_from"] == str(second.a.root) for e in chained)
    for r in rows_of_role(third.a, "stream"):
        assert session_file(third.a.root, r.evidence or r.attempt_id).exists()
    (event,) = third.a.events.read("baseline_imported")
    assert (event["attempts"], event["events"], event["transcripts"]) == (0, 6, 6)
    again = phases.import_baseline(third.a, second.a.root, out=quiet)
    assert (again["attempts"], again["events"], again["transcripts"]) == (0, 0, 0)
    phases.stream(third.a, out=quiet)
    assert provider.calls == paid                                      # the chain pays for no first encounter


# ---------------------------------------------------------------------------- v1.11: R's own probe (try_task)
@pytest.fixture(scope="module")
def debug(tmp_path_factory, fake_upstream):
    s = round_activity(tmp_path_factory.mktemp("debug"), fake_upstream, "debug")
    s.lines = []
    s.result = phases.round_(s.a, 0, out=s.lines.append)
    return s


def test_r_tries_its_package_on_the_studied_failure_reads_the_contrast_and_the_host_trial_pays_nothing(debug):
    """DESIGN §6.21: try_task runs the candidate once on the ticket and answers the contrast with W's reading — the
    weak hook acts and the agent does not heed it (FAILED, the hook in the acted list, the failed check named),
    the working hook flips it (PASSED, marked one rollout); a third try on the same task is refused by try_per_task;
    the host's trial after finish reads the recorded attempt and pays nothing; the package is screened and
    installed; the declaration's scope is on the ledger."""
    a, r = debug.a, debug.result
    assert r.status == "done" and r.accepted and r.screen["dir"] == "r0:package"
    tries = debug.r.tries
    assert [bad for bad, _ in tries] == [False, False, True]
    first, second, third = (t for _, t in tries)
    assert first.startswith(f"try_task candidates/package on {TICKET}: FAILED") and "one rollout" in first
    assert "Your components that acted: hook:remind_ops" in first
    assert "W's reading (" in first and "slack_message_contains" in first and "this run (" in first
    assert "on_finish remind_ops -> pushback" in first and "1 try(ies) left on this task" in first
    assert second.startswith(f"try_task candidates/package on {TICKET}: PASSED") and "not a confirmation" in second
    assert "0 try(ies) left on this task" in second
    assert third.startswith("refused: candidates/package was tried 2 time(s)") and "try_per_task 2" in third
    rows = ledger_rows(a, "tried")
    assert [(x["dir"], x["task"], x["iteration"], x["passed"], x["acted_components"], x["attempts"]) for x in rows] == \
        [("package", TICKET, 0, False, ["hook:remind_ops"], 1), ("package", TICKET, 1, True, ["hook:remind_ops"], 1)]
    # the host's trial after finish: the ticket's attempt under the finished package is on record, nothing paid
    (trialed,) = ledger_rows(a, "trialed")
    assert (trialed["trial_fixed"], trialed["attempts"], trialed["passed"]) == ([TICKET], 0, True)
    trials = rows_of_role(a, "trial")
    assert [t.task_id for t in trials] == [TICKET, TICKET] and [t.success for t in trials] == [False, True]
    assert ledger_rows(a, "declared")[-1]["scope"] == "invariant"
    assert any(f"try of package on {TICKET} (try 1): ok, FAILED, acted ['hook:remind_ops']" in line
               for line in debug.lines)
    assert any(f"try of package on {TICKET} (try 2): ok, PASSED" in line for line in debug.lines)
    # R could read the try's session afterwards
    assert all(t.evidence or t.attempt_id for t in trials)


def test_try_task_refuses_held_out_and_passing_tasks_and_packages_that_do_not_build(debug):
    a = debug.a
    rd = Round(a, 0, run_arm=None, out=quiet)
    vctx = rd.vctx()
    files = {p: t for p, t in rd.package.files.items() if p != MEMORY_PATH}
    text, bad, opened = rd._try_task("package", files, HELD[0], vctx)
    assert bad and text.startswith("AccessDenied") and opened is None
    passing = next(t for t in STUDY if t != TICKET)
    text, bad, _ = rd._try_task("package", files, passing, vctx)
    assert bad and "not a studied task the working line fails" in text
    broken = {**files, "manifest.json": "{"}
    text, bad, _ = rd._try_task("package-9", broken, TICKET, vctx)
    assert bad and "does not build" in text
    # the cap: two tries of package on the ticket are on the ledger
    text, bad, _ = rd._try_task("package", files, TICKET, vctx)
    assert bad and text.startswith("refused: candidates/package was tried 2 time(s)")


# ---------------------------------------------------------------------------- the session's edit reminders
class Script:
    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    def chat(self, system, messages, specs, **_kw):
        self.seen.append(list(messages))
        turn = self.turns.pop(0)
        n = len(self.seen)
        return ChatTurn("", [ToolCall(f"s{n}.{i}", name, args) for i, (name, args) in enumerate(turn)], "tool_use",
                        usage={})


def test_the_tool_result_reminds_r_at_the_edit_shares_and_the_reminders_survive_a_checkpoint(world):
    assert EDIT_REMINDERS == (0.75, 0.9)
    ws = world.a.worker().start()
    try:
        ws.load({})
        tree = WorkTree(ws)
        tree.init({p: x for p, x in world.base.files.items() if p != MEMORY_PATH}, {"causes.json": "[]"},
                  max_bytes=Limits.from_core(world.a.k).max_package_bytes)
        writes = [("write_file", {"path": f"candidates/package/tools/x{i}.py", "text": "X = 1\n"}) for i in range(5)]
        script = Script([writes[0]], [writes[1]], [writes[2]], [writes[3]], [writes[4]], [("finish", {"packages": []})])
        cs = CodingSession(researcher=script, work=tree, trace=world.evidence.trace, check=lambda n, r: "ok",
                           tests=lambda n: "ok", settings=Settings(session_max_edits=10),
                           system="You improve the harness package (a test)", inputs={"round": "r9"}, readable=set(),
                           kinds=("tool",))
        # 8 of 10 is the first share (0.75 -> 8), 9 the second
        cs.edits = 5
        cs.start()
        outputs = [r["output"] for m in cs.messages if m.get("role") == "tool" for r in m["results"]
                   if r["name"] == "write_file"]
        assert len(outputs) == 5
        assert "[note:" not in outputs[0] and "[note:" not in outputs[1]
        assert "[note: 8 of the session's 10 edits are used" in outputs[2]
        assert "[note: 9 of the session's 10 edits are used" in outputs[3]
        assert "[note:" not in outputs[4]
        assert cs.state()["reminded"] == [0.75, 0.9]
        again = CodingSession(researcher=script, work=tree, trace=world.evidence.trace, check=lambda n, r: "ok",
                              tests=lambda n: "ok", settings=Settings(session_max_edits=10), system="x",
                              inputs={}, readable=set(), kinds=("tool",))
        again.restore(cs.state())
        assert again.reminded == [0.75, 0.9] and again._edit_reminder() == ""
    finally:
        ws.close(force=True)


# ---------------------------------------------------------------------------- the research sample priced after
def test_the_research_sample_is_priced_after_the_screen_and_skipped_when_it_does_not_fit(tmp_path, fake_upstream,
                                                                                            monkeypatch):
    s = round_activity(tmp_path, fake_upstream, "partial", actor=rsi.BusinessWorker(forget_notify=True, suggestible=True),
                       splits_table=rsi.QUIET_SPLITS)
    base = s.a.active_release().package_ref
    original = Round._need

    def squeezed(self, usd):
        need, left = original(self, usd)
        # the sample of the three studied passing tasks is the one price of three attempts in this round (the
        # means move as W's confirmations are recorded: read them as the round does)
        train = sc.cost_per_attempt(self.a, self.work_ref, "train")
        if usd and abs(usd / train - 3) < 1e-6:
            return need, 0.0
        return need, left
    monkeypatch.setattr(Round, "_need", squeezed)
    lines = []
    r = phases.round_(s.a, 0, out=lines.append)
    assert r.status == "done" and not r.accepted
    res = screens_of(s.a)["package"]
    assert res.partial and res.research_sample == () and res.research_regressed == ()
    obj = s.a.objects.get(res.regression_ref)
    assert obj["research"]["outcome"] == "budget" and obj["research"]["sample"] == []
    assert "would need up to" in obj["research"]["reason"]
    assert rows_of_role(s.a, "research") == []
    assert any("no research sample for package" in line for line in lines)
    # the screen's own price is the held-out tasks and the confirmation's allowance, no sample
    priced = round_state(s.a, 0)["steps"]["screened"]["usd_priced"]
    train = sc.cost_per_attempt(s.a, base, "train")
    assert 0 < priced <= (len(rsi.HELD_QUIET) + round_mod.confirm_allowance(len(rsi.HELD_QUIET), Settings())) \
        * train * 1.5


def test_settings_v19_are_validated():
    s = Settings()
    assert (s.trial_max_tasks, s.trial_max_rounds, s.trial_max_attempts, s.trial_min_fixes) == (4, 2, 40, 1)   # v1.11: 12 -> 40
    assert s.regression_by_footprint and s.merge_min_acts == 1 and s.replay_sample == 12
    with pytest.raises(ValueError, match="trial_max_tasks"):
        Settings(trial_max_attempts=-1)
    with pytest.raises(ValueError, match="replay_sample"):
        Settings(replay_sample=0)
    assert Settings.from_json({"trial_max_tasks": 0}).trial_max_tasks == 0
