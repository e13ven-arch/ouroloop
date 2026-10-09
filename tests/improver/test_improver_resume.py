"""The round's money and resume paths the stage-3 review found open (docs/design/IMPROVER.md §3, §9, §11): a train
reading or a control top-up the budget refused in one round is read again by the next, not handed back; a resumed
screen step prices only what it has left to pay; a round killed between its install and saving its gate step resumes;
a round killed after its coding session does not pay for the session again; the memory's entry classification is
asked once per entry and model."""
from __future__ import annotations

import json

import pytest
import rsi
from rounds import quiet, round_activity, round_state

from ouroloop.activity import phases
from ouroloop.contracts import Objects
from ouroloop.improver import diagnose as dg
from ouroloop.improver import round as round_mod
from ouroloop.improver.screen import gate, price
from ouroloop.runtime.chat import ChatTurn


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


def test_a_control_top_up_the_budget_refused_does_not_make_the_next_gate_inconclusive(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "tool", baseline=False, freeze=False)
    phases.baseline(s.a, limit=2, out=quiet)        # the pool is short of one dev task: the gate tops it up
    phases.freeze(s.a, out=quiet)
    base = s.a.active_release()
    package = s.a.package(base)

    def squeezed(activity, **kw):
        hold = squeeze(s.a, 0.0001) if kw.get("arm") == "control" else None
        try:
            return phases.run_arm(activity, **kw)
        finally:
            if hold:
                s.a.budgets.settle(hold, None, "released")
    account = s.a.account("gates")
    refused = gate(s.a, run_arm=squeezed, round_id="rsi-test/r0", package=package, base_release=base, account=account)
    assert "budget_exhausted" in json.dumps(s.a.objects.get(refused["receipt"])["reasons"])
    calls = s.actor.calls
    again = gate(s.a, run_arm=phases.run_arm, round_id="rsi-test/r1", package=package, base_release=base,
                 account=account)
    assert "budget_exhausted" not in json.dumps(s.a.objects.get(again["receipt"])["reasons"])
    assert again["verdict"] != "inconclusive" and s.actor.calls > calls


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
    actor.at = s.a.suite.tasks["rsi.notify_once"].trigger
    with pytest.raises(Crash):
        phases.round_(s.a, 0, out=quiet)
    assert "screened" not in round_state(s.a, 0)["steps"]
    base = s.a.active_release().package_ref
    whole, _ = price(s.a, base, screen_attempts=len(rsi.TRAIN))
    left, _ = price(s.a, base, screen_attempts=1)  # the killed attempt; the screen read every train task
    result = phases.round_(s.a, 0, out=quiet)
    assert result.status == "done" and result.installed is not None, result.stop
    assert round_state(s.a, 0)["steps"]["screened"]["usd_priced"] == left < whole


def test_a_round_killed_between_its_install_and_saving_its_gate_step_resumes(tmp_path, fake_upstream):
    s = round_activity(tmp_path, fake_upstream, "tool")

    def dying(line: str) -> None:
        if "the gate says" in line:
            raise Crash(line)
    with pytest.raises(Crash):
        phases.round_(s.a, 0, out=dying)
    state = round_state(s.a, 0)
    assert "gated" not in state["steps"] and s.a.releases.active_ref() != state["base_release"]
    calls = s.actor.calls
    result = phases.round_(s.a, 0, out=quiet)
    assert result.status == "done" and result.installed == s.a.releases.active_ref()
    assert round_state(s.a, 0)["steps"]["gated"]["install"] == "installed"
    # the gate replays from the record and the screen read every train task of the new W: nothing is paid again
    assert s.actor.calls == calls


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
