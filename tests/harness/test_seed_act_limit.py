"""The stirrup seed under the host's act-call limit (PLAN §3, stage 1's open item): a run Stirrup stops at the host's
limit without finishing ends as budget/act_calls — the host refuses the one extra act call before any model is called
— and the model calls carry the manifest's effort (the gateway caps it at K's)."""
from __future__ import annotations

import rsi

from reharness.activity import phases
from reharness.runtime.records import read, session_file


def quiet(_line):
    pass


def test_a_run_cut_by_the_hosts_act_limit_is_budget_act_calls_and_costs_no_extra_call(tmp_path, fake_upstream):
    actor = rsi.BusinessWorker()
    a = rsi.activity(tmp_path, fake_upstream, provider=actor, limits={"max_steps": 2})
    rows = phases.run_arm(a, release=a.active_release(), split="train", candidate="capped", role="base",
                          pairs=[("rsi.update_stage", 0)], account=a.account("capped"))
    (row,) = rows
    assert (row.termination, row.limit, row.validity) == ("budget", "act_calls", "valid")
    assert actor.calls == 2                                        # the extra act call never reached the model
    lines = read(session_file(a.root, row.attempt_id))
    calls = [line for line in lines if line["kind"] == "gateway_call"]
    assert len(calls) == 2 and {c["effort_requested"] for c in calls} == {"medium"}
    end = [line for line in lines if line["kind"] == "end"][-1]
    assert (end["termination"], end["limit"]) == ("budget", "act_calls")


def test_a_run_that_finishes_inside_the_limit_is_finished(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream)
    (row,) = phases.run_arm(a, release=a.active_release(), split="train", candidate="free", role="base",
                            pairs=[("rsi.update_stage", 0)], account=a.account("free"))
    assert (row.termination, row.limit) == ("finished", None)


class RateLimited(Exception):
    unbilled, retryable = True, True


class OnceLimited(rsi.BusinessWorker):
    """The provider refuses its first request with a rate limit (never charged), then answers."""

    def chat(self, system, messages, specs, **kw):
        if not getattr(self, "refused", False):
            self.refused = True
            raise RateLimited("429: slow down")
        return super().chat(system, messages, specs, **kw)


def test_the_rate_limits_a_run_absorbed_are_on_record(tmp_path, fake_upstream, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    a = rsi.activity(tmp_path, fake_upstream, provider=OnceLimited())
    (row,) = phases.run_arm(a, release=a.active_release(), split="train", candidate="limited", role="base",
                            pairs=[("rsi.update_stage", 0)], account=a.account("limited"))
    assert row.validity == "valid"
    (event,) = list(a.events.read("rate_limited"))
    assert (event["candidate"], event["split"], event["count"]) == ("limited", "train", 1)
