"""The audit's dry run, V2d (DESIGN §6.16; IMPROVER §8, v1.6) on the fake upstream: an on_finish hook that asks the aux
model what is missing. The scripted R writes it (scripted_r mode "audit"); the research model answers from the agent's
final text and writes (rsi.audit_reply): a planned post with no "Message posted." is missing. "audit_noisy" answers
something missing on every attempt.

Covered: the dry run fires on the claimed failing task (the ticket) and on none of the passing studied attempts, names
what the grader marked failed, passes, goes to the screen, where the host's rendered pushback makes the forgetful actor
post and both held-out tasks pass, and the package is installed; the `audited` row and the next round's
inputs/audits.json carry it with its screen; a noisy audit fails the dry run with every passing attempt a false alarm,
the verdict R reads lists each with the task's request, the session is revised once, the round has no candidate."""
from __future__ import annotations

import json

import rsi
from rounds import ledger_rows, quiet, round_activity, round_state

from reharness.activity import phases
from reharness.improver.ledger import Ledger
from reharness.improver.round import Round
from reharness.improver.verify import Verdict

TICKET, HELD, STUDY = "rsi.notify_ticket", rsi.HELD, rsi.STUDY


def verdict_of(a, index=0) -> Verdict:
    item = round_state(a, index)["steps"]["implemented"]["candidates"][0]
    return Verdict.from_json(a.objects.get(item["verdict"]))


def test_an_audit_passes_its_dry_run_and_the_screen_and_is_installed(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "audit")
    result = phases.round_(s.a, 0, out=quiet)
    v = verdict_of(s.a)
    (key,) = v.components
    got = v.components[key]["V2d"]
    assert key.startswith("hook:") and got["passed"] and got["hits"] == [TICKET] and got["named"] == [TICKET]
    assert got["false_alarms"] == [] and got["false_alarm_rate"] == 0.0 and got["errors"] == []
    assert "fires on 1 of 1 claimed failing tasks" in got["detail"]
    assert v.passed() and set(v.rungs()) == {"V0", "V1", "V2d"}
    (row,) = [r for r in ledger_rows(s.a, "audited")]
    assert (row["dir"], row["site"], row["passed"], row["hits"], row["false_alarms"]) == ("package", "on_finish", True, [TICKET], [])
    # the research model answered the dry run's aux calls (one per attempt dry-run: the ticket and the three passing)
    assert s.r.calls.count("audit") >= 4
    # the screen: the host's rendered pushback ("Keep working on the task: ...") makes the forgetful actor post
    assert result.accepted and result.screen["net_flips"] == 2 and result.screen["holdout_score"] == 2
    screen = [r for r in ledger_rows(s.a, "screened")][0]
    assert sorted(screen["fixed"]) == sorted(HELD) and screen["regressed"] == []
    # the next round reads the audit with its screen (Ledger.audits feeds inputs/audits.json), and no held-out task id
    audits = Ledger(s.a.root).audits(s.a.domain)
    (audit,) = audits
    assert (audit["dir"], audit["site"], audit["hits"], audit["passed"]) == ("package", "on_finish", [TICKET], True)
    assert audit["screen"]["accepted"] and audit["screen"]["fixed_held_out"] == 2 and audit["misfires"] == []
    assert "aux model" in audit["mechanism"] and not any(t in json.dumps(audits) for t in HELD)


def test_a_noisy_audit_fails_its_dry_run_with_the_false_alarms_spelled_out(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "audit_noisy")
    result = phases.round_(s.a, 0, out=quiet)
    assert result.status == "stopped" and result.stop.startswith("no_candidate")
    v = verdict_of(s.a)
    (key,) = v.components
    got = v.components[key]["V2d"]
    assert not got["passed"] and got["hits"] == [TICKET] and got["false_alarm_rate"] == 1.0
    assert sorted(got["false_alarms"]) == sorted(t for t in STUDY if t != TICKET)
    assert all(m["request"] and m["missing"] == ["something is missing"] for m in got["misfires"])
    assert "false alarms — each a pushback on a task the agent had right" in got["detail"]
    assert "Dana Reyes" in got["detail"] or "Sam Okafor" in got["detail"]      # a passing task's request, for R
    # the session was asked to revise once and the audit still fails: no screen was paid for
    assert s.r.calls.count("session") >= 2
    assert "screened" not in round_state(s.a, 0)["steps"] and ledger_rows(s.a, "screened") == []
    (row,) = ledger_rows(s.a, "audited")[-1:]
    assert row["passed"] is False and len(row["misfires"]) == 3
    (audit,) = Ledger(s.a.root).audits(s.a.domain)[-1:]
    assert audit["passed"] is False and audit["screen"] is None and len(audit["misfires"]) == 3
    # and round 0's own inputs carry the file (empty before any audit ran on this line)
    inputs, _ = Round(s.a, 0, run_arm=None, out=quiet).inputs()
    assert "audits" in json.loads(inputs["audits.json"])
