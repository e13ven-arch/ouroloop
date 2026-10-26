"""The round's money and resume paths (docs/design/IMPROVER.md §3, §9, §11; v1.2): a train reading the budget refused
in one round is read again by the next, not handed back; a resumed screen step prices only what it has left to pay; a
round killed after its coding session does not pay for the session again; a session a provider failure stopped goes on
where it stopped; the memory's entry classification is asked once per entry and model; an installed promotion
supersedes its entry; the report shows each round; a claude CLI that changed stops the round before the screen."""
from __future__ import annotations

import json

import pytest
import rsi
from rounds import ledger_rows, quiet, round_activity, round_state

from reharness.activity import phases
from reharness.contracts import Objects, short
from reharness.improver import diagnose as dg
from reharness.improver import round as round_mod
from reharness.improver.screen import price
from reharness.improver.settings import Settings
from reharness.runtime.chat import ChatTurn


class Crash(BaseException):
    """The process dying: nothing in the round may swallow it."""


def squeeze(a, keep: float) -> str:
    """Another holder sets aside all of the root but `keep` dollars; returns the hold to release."""
    other = a.account("other")
    left = other.remaining()["effective_remaining"]["usd"]
    return other.hold(f"someone else's reservation {left}", {"usd": round(left - keep, 6)})


# ---------------------------------------------------------------------------- refusals are not handed back
def test_a_train_reading_the_budget_refused_in_one_round_is_read_again_by_the_next(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "tool", baseline=False)
    hold = squeeze(s.a, 0.01)
    first = phases.round_(s.a, 0, out=quiet)
    assert first.status == "stopped" and first.stop.startswith("budget")
    s.a.budgets.settle(hold, None, "released")
    calls = s.actor.calls
    second = phases.round_(s.a, 1, out=quiet)
    assert not (second.stop or "").startswith("budget") and s.actor.calls > calls
    assert round_state(s.a, 1)["steps"]["measure"]["measured"] > 0


# ---------------------------------------------------------------------------- resumes
class CrashOnce(rsi.BusinessWorker):
    """The forgetful actor, killed once at the first attempt of a task whose request holds `at`, when the package
    offers the candidate's tool (a screen attempt)."""

    def __init__(self):
        super().__init__(forget_notify=True)
        self.at = None

    def chat(self, system, messages, specs, **kw):
        task = next((m["content"] for m in messages if m.get("role") == "user"), "")
        if self.at and self.at in task and rsi.NOTIFY_TOOL in {x.name for x in specs or ()}:
            self.at = None
            raise Crash("killed mid-screen")
        return super().chat(system, messages, specs, **kw)


def test_a_resumed_screen_step_prices_only_what_it_has_left_to_pay(tmp_path, fake_upstream):
    actor = CrashOnce()
    s = round_activity(tmp_path, fake_upstream, "tool", actor=actor)
    actor.at = s.a.suite.tasks[rsi.HELD[1]].trigger            # the second held-out attempt dies
    with pytest.raises(Crash):
        phases.round_(s.a, 0, out=quiet)
    assert "screened" not in round_state(s.a, 0)["steps"]
    base = s.a.active_release().package_ref
    # the confirmation's attempts, priced with it (v1.9: the research sample is priced after the screen, not here)
    allowance = round_mod.confirm_allowance(len(rsi.HELD), Settings())
    whole, _ = price(s.a, base, screen_attempts=len(rsi.HELD) + allowance)
    # the killed attempt; the first held-out task is on record, no confirmation has run yet
    left, _ = price(s.a, base, screen_attempts=1 + allowance)
    result = phases.round_(s.a, 0, out=quiet)
    assert result.status == "done" and result.installed is not None, result.stop
    assert round_state(s.a, 0)["steps"]["screened"]["usd_priced"] == left < whole


def test_a_round_killed_after_its_coding_session_does_not_pay_for_it_again(tmp_path, fake_upstream, monkeypatch):
    s = round_activity(tmp_path, fake_upstream, "tool")
    real, seen = round_mod.run_ladder, []

    def ladder(files, decl, ctx, **kw):
        seen.append(decl.dir)
        if len(seen) == 2:          # the first is the session's own run_check; the second the step's check after it
            raise Crash("the worker host died")
        return real(files, decl, ctx, **kw)
    monkeypatch.setattr(round_mod, "run_ladder", ladder)
    with pytest.raises(Crash):
        phases.round_(s.a, 0, out=quiet)
    sessions = s.r.calls.count("session")
    assert "implemented" not in round_state(s.a, 0)["steps"] and round_state(s.a, 0)["session_result"]
    monkeypatch.setattr(round_mod, "run_ladder", real)
    result = phases.round_(s.a, 0, out=quiet)
    assert s.r.calls.count("session") == sessions
    assert result.status == "done" and result.ladder == {"V0": 1, "V1": 1, "V2a": 1} and result.installed


# ---------------------------------------------------------------------------- the entry classification's cache
class Classifier:
    model = "scripted-classifier"

    def __init__(self):
        self.calls = 0

    def chat(self, system, messages, specs, **kw):
        self.calls += 1
        return ChatTurn(json.dumps({"cause": "missed_action"}), [], "end", usage={})


def test_an_entry_is_classified_once_per_request_and_model(tmp_path):
    entries = [{"memory_id": "e1", "when": "a request asks for #ops", "content": "post it"},
               {"memory_id": "e2", "when": "a refund", "content": "check the order"}]
    cache = dg.NoteCache(tmp_path, Objects(tmp_path / "objects"))
    r = Classifier()
    vocabulary = ("missed_action", "other")
    first = dg.classify_entries(entries, researcher=r, vocabulary=vocabulary, cache=cache)
    assert first == {"e1": "missed_action", "e2": "missed_action"} and r.calls == 2
    assert dg.classify_entries(entries, researcher=r, vocabulary=vocabulary,
                               cache=dg.NoteCache(tmp_path, Objects(tmp_path / "objects"))) == first
    assert r.calls == 2
    other = Classifier()
    other.model = "another-model"
    dg.classify_entries(entries[:1], researcher=other, vocabulary=vocabulary, cache=cache)
    assert other.calls == 1


# ---------------------------------------------------------------------------- an installed promotion
def test_an_installed_promotion_supersedes_its_entry(tmp_path, fake_upstream):
    """MEMORY §8: the entry is superseded in the stream's ledger, retired from the live store, gone from the live
    library; doing it again changes nothing."""
    s = round_activity(tmp_path, fake_upstream, "none", memory=True, freeze=False)
    path = s.a.root / "memory" / s.a.domain / "stream.json"
    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    eid = next(k for k, e in entries.items() if e.get("status") == "active")
    service = s.a.memory()
    service.supersede(eid, by="release:sha256:" + "1" * 64)
    after = json.loads(path.read_text(encoding="utf-8"))["entries"][eid]
    assert after["status"] == "superseded" and after["superseded_by"] == "release:sha256:" + "1" * 64
    live = (s.a.root / "memory" / s.a.domain / "live.jsonl").read_text(encoding="utf-8")
    assert eid not in live and eid not in service.client.dump(service.collection)
    before = path.read_text(encoding="utf-8")
    service.supersede(eid, by="another")
    assert path.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------- R's session stopped by the provider
def test_a_session_a_provider_failure_stopped_goes_on_where_it_stopped(tmp_path, fake_upstream):
    """The subscription's limit at R's third turn: the session is saved and the round raises; run again, it goes on
    from that turn with its candidate directory, and the turns already paid are not paid again."""
    from reharness.providers import ProviderAccountError
    s = round_activity(tmp_path, fake_upstream, "tool")
    real, seen = s.research.chat, {"session": 0, "failed": False}

    def chat(system, messages, specs, **kw):
        if (system or "").startswith("You improve the harness package"):
            seen["session"] += 1
            if seen["session"] == 3 and not seen["failed"]:
                seen["failed"] = True
                raise ProviderAccountError("You've hit your session limit")
        return real(system, messages, specs, **kw)
    s.research.chat = chat
    with pytest.raises(ProviderAccountError):
        phases.round_(s.a, 0, out=quiet)
    state = round_state(s.a, 0)
    assert "implemented" not in state["steps"] and "session_checkpoint" in state
    assert s.r.calls.count("session") == 2
    result = phases.round_(s.a, 0, out=quiet)
    assert s.r.calls.count("session") == 4                     # turns 3 and 4 only: 1 and 2 were not asked again
    assert result.status == "done" and result.ladder == {"V0": 1, "V1": 1, "V2a": 1} and result.installed
    assert "session_checkpoint" not in round_state(s.a, 0)


# ---------------------------------------------------------------------------- the report's rounds
def test_the_report_shows_each_round_and_is_the_same_bytes_twice(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "tool")
    result = phases.round_(s.a, 0, out=quiet)
    first = phases.report(s.a, out=quiet)
    written = {p: (s.a.root / p).read_bytes() for p in ("report.json", "report.md")}
    assert phases.report(s.a, out=quiet) == first
    assert {p: (s.a.root / p).read_bytes() for p in ("report.json", "report.md")} == written
    (r,) = first["rounds"]
    assert (r["round"], r["status"], r["stop"]) == ("rsi-test/r0", "done", None)
    assert r["causes"] == {"missed_action": 1} and r["focus"][0]["kinds"][0] == "tool" and r["require_code"]
    assert r["candidates"] == [{"dir": "package", "kind": "tool", "passed": True}]
    assert r["screens"] == [{"dir": "package", "outcome": "screened", "fixed": 2, "unconditioned": 0, "regressed": 0,
                             "net_flips": 2, "holdout_score": 2, "holdout": 2, "best_so_far": 0, "accepted": True,
                             "reasons": [], "partial": False, "merge_of": None, "research_sample": 0,
                             "research_regressed": 0, "acted": 2, "footprint_share": 1.0,
                             "unconditioned_regressed": 0, "kept_fixes": None}]
    assert r["best"] == "package" and r["installed"] == short(result.installed)
    assert r["spend"]["usd"] == pytest.approx(result.spend["used"]["usd"])
    assert first["gate"] is None and first["released"] is None and first["release"] == short(result.installed)
    md = written["report.md"].decode()
    assert "- rsi-test/r0 done: causes {'missed_action': 1}" in md and "held out 2/2 (best so far 0)" in md
    assert "installed package on the working line" in md and "## Release gate\n- not run" in md
    # the spend's phases are the root's children, each with its subtree: they sum to the root
    spend = first["spend"]
    assert "round-0" in spend["phases"] and not any(":" in p for p in spend["phases"])
    assert sum(p.get("usd", 0) for p in spend["phases"].values()) == pytest.approx(spend["root"]["usd"])


# ---------------------------------------------------------------------------- screens priced one by one
def test_the_screens_that_fit_are_paid_and_the_rest_are_recorded_as_not_run(tmp_path, fake_upstream):
    """R's package and the library's snapshot both pass their free rungs; the round has room for one screen: R's
    (first in the order) is screened, the snapshot's is on record as not run for the budget."""
    from reharness.improver.round import PRICE_MARGIN
    s = round_activity(tmp_path, fake_upstream, "tool", memory=True)

    def squeezing(line: str) -> None:
        if "passed their free rungs" in line:
            base = s.a.active_release().package_ref
            one, _ = price(s.a, base, screen_attempts=len(rsi.HELD) + round_mod.confirm_allowance(len(rsi.HELD),
                                                                                                 Settings()))
            bound = float(s.a.pricing().bound()["usd"])
            squeeze(s.a, one * (1 + PRICE_MARGIN) + 2 * bound + 0.001)
    result = phases.round_(s.a, 0, out=squeezing)
    rows = {r["dir"]: r for r in ledger_rows(s.a, "screened")}
    assert rows["package"]["outcome"] == "screened" and rows["package"]["accepted"] is True
    assert rows["memory-snapshot"]["outcome"] == "not_run" and rows["memory-snapshot"]["reason"].startswith("budget")
    assert result.accepted and result.screen["dir"] == "r0:package"


def test_an_accepted_snapshot_not_selected_comes_back_as_a_fixed_candidate(tmp_path, fake_upstream):
    """Both R's package and the snapshot are accepted: one install a round, R's first; the snapshot is `not_selected`
    and the next round declares it again."""
    s = round_activity(tmp_path, fake_upstream, "tool", memory=True)
    result = phases.round_(s.a, 0, out=quiet)
    rows = {r["dir"]: r for r in ledger_rows(s.a, "screened") if r["round_id"] == "rsi-test/r0"}
    assert result.accepted and result.screen["dir"] == "r0:package"
    if rows.get("memory-snapshot", {}).get("accepted"):
        assert any(r["dir"] == "memory-snapshot" and r["outcome"] == "not_selected"
                   for r in ledger_rows(s.a, "screened"))
        from reharness.improver.round import Round
        rd = Round(s.a, 1, run_arm=phases.run_arm, out=quiet)
        assert "memory-snapshot" not in rd._fixed_done() or not any(
            r.get("source", "").startswith("snapshot:") for r in rd._fixed_done())


def test_a_claude_cli_that_changed_during_the_round_stops_it_before_the_screen(tmp_path, fake_upstream, monkeypatch):
    """The screen compares the package with the working line's earlier readings: when the CLI on PATH is not the one
    the activity recorded, the round stops before the screen pays anything."""
    import reharness.providers.claude_code as cc
    from reharness.improver.round import Round, Stop
    s = round_activity(tmp_path, fake_upstream, "tool")
    s.a.events.append("cli_version", version="2.1.288 (Claude Code)", phase="baseline")
    monkeypatch.setattr(s.a, "provider_factory", None)              # as in a real run: the CLI is the transport
    monkeypatch.setattr(cc, "cli_version", lambda binary="claude": "2.2.0 (Claude Code)")
    rd = Round(s.a, 0, run_arm=phases.run_arm, out=quiet)
    rd.state["steps"]["verified"] = {"passed": [], "fixed": [], "ladder": {}}
    calls = s.actor.calls
    with pytest.raises(Stop, match="cli: the claude CLI is 2.2.0"):
        rd.screened()
    assert s.actor.calls == calls
