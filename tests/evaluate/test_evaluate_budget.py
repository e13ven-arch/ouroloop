"""The budget book, pricing, the metered provider and the execution log (docs/design/EVALUATE.md §2, §3, §9). Scripted
providers and injected failures only: nothing is paid for."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ouroloop.contracts import BudgetError, BudgetExhausted, BudgetMismatch, Events
from ouroloop.evaluate.budget import (Budgets, Execution, MeteredProvider, Pricing, StateError, call_bound_problem,
                                      check_pricing, operation_id, unfinished)
from ouroloop.providers import ProviderAccountError, ProviderError
from ouroloop.runtime.chat import ChatTurn
from ouroloop.runtime.gateway import normalize_usage

PRICING = Pricing(0.003, 0.015, 4000, 1000, source="test prices")      # one call's bound: 0.012 + 0.015 = 0.027


def turn(input_tokens=1000, output_tokens=100, **usage):
    return ChatTurn("done", [], "end", usage={"input_tokens": input_tokens, "output_tokens": output_tokens, **usage})


class Scripted:
    """A provider that answers with the next item of its script: a ChatTurn, or an exception to raise."""
    name, model = "scripted", "m-1"

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def chat(self, system, messages, specs=(), **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)


# ---------------------------------------------------------------------------- the state machine
def test_states_advance_along_the_machine_and_a_terminal_state_is_final(tmp_path):
    ex = Execution(tmp_path, "exp-1")
    assert ex.state()["status"] == "drafted"
    for status in ("admitted", "building", "evaluating", "evaluated", "accepted", "installing", "installed"):
        ex.advance(status, reason=f"to {status}")
    assert ex.state()["status"] == "installed"
    with pytest.raises(StateError, match="terminal"):
        ex.advance("accepted")
    with pytest.raises(StateError, match="terminal"):
        ex.advance("cancelled")
    assert [e["status"] for e in ex.rebuild()["history"]][-1] == "installed"
    assert [e["status"] for e in Events(tmp_path).read("state")][-1] == "installed"


def test_an_impossible_transition_is_refused_and_cancel_is_always_possible(tmp_path):
    ex = Execution(tmp_path, "exp-2")
    with pytest.raises(StateError, match="not a transition"):
        ex.advance("installed")
    with pytest.raises(StateError, match="unknown state"):
        ex.advance("approved")
    ex.advance("admitted")
    ex.advance("cancelled")
    with pytest.raises(StateError):
        ex.advance("building")


def test_a_confirmation_passes_through_confirming_back_to_evaluated(tmp_path):
    ex = Execution(tmp_path, "exp-c")
    for status in ("admitted", "building", "evaluating", "evaluated", "confirming", "evaluated", "accepted"):
        ex.advance(status)
    assert [e["status"] for e in ex.state()["history"]][-3:] == ["confirming", "evaluated", "accepted"]


def test_a_failed_installation_may_be_retried_but_a_stale_parent_may_not(tmp_path):
    ex = Execution(tmp_path, "exp-3")
    for status in ("admitted", "building", "evaluating", "evaluated", "accepted", "installing",
                   "installation_failed"):
        ex.advance(status)
    ex.advance("installing")            # same accepted artifact, another attempt
    ex.advance("stale_parent")
    with pytest.raises(StateError, match="terminal"):
        ex.advance("installing")


def test_the_snapshot_is_a_cache_of_the_journal(tmp_path):
    ex = Execution(tmp_path, "exp-4")
    ex.advance("admitted", experiment="experiment:sha256:" + "a" * 64)
    ex.advance("building")
    ex.snapshot.unlink()
    assert ex.state()["status"] == "building"
    assert Execution(tmp_path, "exp-4").state()["history"][0]["experiment"] == "experiment:sha256:" + "a" * 64


def test_an_operation_id_is_determined_by_the_experiment_step_and_inputs(tmp_path):
    ex = Execution(tmp_path, "exp-5")
    refs = ("proposal:sha256:" + "a" * 64, "core:sha256:" + "b" * 64)
    first = ex.operation("build", refs)
    assert first.id == operation_id("exp-5", "build", refs) == operation_id("exp-5", "build", tuple(reversed(refs)))
    assert len({first.id, operation_id("exp-6", "build", refs), operation_id("exp-5", "evaluate", refs),
                operation_id("exp-5", "build", refs[:1])}) == 4
    assert first.status == "intent"
    ex.finish(first, "done", output_refs=("package:sha256:" + "c" * 64,))
    again = Execution(tmp_path, "exp-5").operation("build", refs)   # a restart finds the recorded operation
    assert again.id == first.id and again.status == "done" and again.output_refs == ("package:sha256:" + "c" * 64,)


def test_unfinished_lists_the_experiments_a_restart_must_resume(tmp_path):
    done = Execution(tmp_path, "exp-done")
    for status in ("admitted", "building", "evaluating", "evaluated", "rejected"):
        done.advance(status)
    failed = Execution(tmp_path, "exp-failed")
    for status in ("admitted", "building", "evaluating", "evaluated", "accepted", "installing",
                   "installation_failed"):
        failed.advance(status)
    open_ = Execution(tmp_path, "exp-open")
    open_.advance("admitted", experiment="experiment:sha256:" + "e" * 64)
    open_.advance("building")
    bare = Execution(tmp_path, "exp-bare")
    bare.advance("admitted")
    assert unfinished(tmp_path) == [
        {"experiment_id": "exp-bare", "status": "admitted", "experiment_ref": None},
        {"experiment_id": "exp-open", "status": "building", "experiment_ref": "experiment:sha256:" + "e" * 64}]
    assert unfinished(tmp_path / "empty") == []


# ---------------------------------------------------------------------------- one book
def test_two_arms_share_one_book_and_neither_resets_the_parent(tmp_path):
    top = Budgets(tmp_path).account("round", {"usd": 10.0, "model_calls": 20})
    a, c = top.child("arm-a", {"usd": 5.0}), top.child("arm-b", {"usd": 5.0})
    ra = a.reserve("a1", {"usd": 4.0})
    a.settle(ra.id, {"usd": 3.0})
    rc = c.reserve("c1", {"usd": 4.0})
    c.settle(rc.id, {"usd": 4.0})
    assert top.remaining()["remaining"]["usd"] == 3.0            # the parent sees the sum, counted once
    assert a.remaining()["remaining"]["usd"] == 2.0
    with pytest.raises(BudgetExhausted, match="usd of arm-a"):
        a.reserve("a2", {"usd": 2.5})
    deep = a.child("arm-a-inner", {"usd": 5.0})                   # a nested scope cannot exceed its ancestor
    with pytest.raises(BudgetExhausted, match="usd of arm-a"):
        deep.reserve("a3", {"usd": 2.5})
    assert deep.reserve("a4", {"usd": 1.0}).bound == {"usd": 1.0}
    third = top.child("arm-c", {"usd": 5.0})                      # its own limit has room; the parent has not
    with pytest.raises(BudgetExhausted, match="usd of round"):
        third.reserve("c2", {"usd": 3.0})


def test_reopening_a_scope_with_other_limits_or_an_unknown_dimension_is_refused(tmp_path):
    book = Budgets(tmp_path)
    book.account("round", {"usd": 10.0, "tool_calls": None})
    assert book.account("round", {"usd": 10.0}).scope_id == "round"
    with pytest.raises(BudgetMismatch, match="other limits"):
        book.account("round", {"usd": 20.0})
    with pytest.raises(BudgetMismatch, match="other limits"):
        book.account("round", {"usd": 10.0}, parent="elsewhere")
    with pytest.raises(BudgetMismatch, match="unknown budget dimensions"):
        book.account("other", {"dollars": 5})
    with pytest.raises(BudgetMismatch, match="no budget scope"):
        book.scope("nowhere")


def test_a_lost_response_keeps_its_bound_and_a_retry_is_a_new_call(tmp_path):
    account = Budgets(tmp_path).account("round", {"usd": 10.0, "model_calls": 10})
    lost = account.reserve("call-1", {"usd": 3.0, "model_calls": 1})
    account.settle(lost.id, None, "unknown")
    state = account.remaining()
    assert state["remaining"]["usd"] == 7.0 and state["unknown"] == 1
    # a restarted controller reads the same book: the unknown call still occupies its bound
    assert Budgets(tmp_path).account("round", {"usd": 10.0, "model_calls": 10}).remaining()["remaining"]["usd"] == 7.0
    retry = account.reserve("call-1-retry", {"usd": 3.0, "model_calls": 1})
    account.settle(retry.id, {"usd": 1.0, "model_calls": 1})
    assert account.remaining()["remaining"]["usd"] == 6.0        # 3 unknown + 1 measured
    # only a confirmation that the call never happened releases it
    account.settle(lost.id, None, "released")
    assert account.remaining()["remaining"]["usd"] == 9.0
    with pytest.raises(BudgetError, match="unknown settlement"):
        account.settle(retry.id, None, "forgotten")
    with pytest.raises(BudgetError, match="no reservation"):
        account.settle("res_missing", {"usd": 1.0})


def test_spending_over_the_bound_is_a_breach_that_stops_the_scope(tmp_path):
    book = Budgets(tmp_path)
    account = book.account("round", {"usd": 10.0})
    below = account.child("round:arm", {})
    r = account.reserve("op-1", {"usd": 1.0})
    receipt = account.settle(r.id, {"usd": 4.0})
    assert receipt.breach == {"usd": {"bound": 1.0, "measured": 4.0}} and receipt.measured["usd"] == 4.0
    assert account.remaining()["used"]["usd"] == 4.0               # the real spend is what is counted
    assert "budget_breach" in account.remaining()["stopped"]
    with pytest.raises(BudgetExhausted, match="no further paid operations"):
        account.reserve("op-2", {"usd": 1.0})
    with pytest.raises(BudgetExhausted, match="no further paid operations"):
        below.reserve("op-3", {"usd": 0.1})                         # what spends this scope's money stops too
    assert not account.affordable({"usd": 0.1})
    assert [e["event"] for e in Events(tmp_path).all()] == ["budget_breach"]


def test_a_bound_is_only_breached_on_a_dimension_it_bounded(tmp_path):
    account = Budgets(tmp_path).account("a", {"model_calls": 10})
    reservation = account.reserve("call", {"model_calls": 1})
    receipt = account.settle(reservation.id, {"model_calls": 1, "input_tokens": 5000, "output_tokens": 900})
    assert receipt.breach == {} and not account.remaining()["stopped"]
    assert account.remaining()["used"] == {"model_calls": 1.0, "input_tokens": 5000.0, "output_tokens": 900.0}


# ---------------------------------------------------------------------------- allocations and headroom
def test_a_candidate_that_could_only_train_is_not_started(tmp_path):
    account = Budgets(tmp_path).account("experiment", {"usd": 10.0})
    account.allocate("verification", {"usd": 6.0})       # the full dev run and the install probe
    assert not account.affordable({"usd": 5.0})          # training may only use what is left over
    assert account.affordable({"usd": 4.0})
    with pytest.raises(BudgetExhausted, match="would exceed usd"):
        account.reserve("train-1", {"usd": 5.0})
    # the verification itself may spend its allocation, and no more than it
    assert account.reserve("dev-run", {"usd": 6.0}, allocation="verification").bound == {"usd": 6.0}
    with pytest.raises(BudgetExhausted):
        account.reserve("dev-run-2", {"usd": 0.5}, allocation="verification")


def test_an_allocation_larger_than_the_scope_is_refused(tmp_path):
    account = Budgets(tmp_path).account("experiment", {"usd": 3.0})
    with pytest.raises(BudgetMismatch, match="does not fit"):
        account.allocate("verification", {"usd": 4.0})
    assert "verification" not in account.remaining()["allocations"]


def test_the_headroom_is_what_a_reservation_is_actually_allowed(tmp_path):
    """A phase account with no limits of its own spends from the activity's root; what the root set aside for the
    final is not spendable elsewhere and is not shown as such, and the final spends its share from its own account."""
    book = Budgets(tmp_path)
    root = book.account("activity", {"usd": 10.0})
    root.allocate("final", {"usd": 4.0})
    phase, final = root.child("activity:improve", {}), root.child("activity:final", {})
    assert phase.remaining()["effective_remaining"] == {"usd": 6.0}
    assert final.remaining(allocation="final")["effective_remaining"] == {"usd": 4.0}
    with pytest.raises(BudgetExhausted):
        phase.reserve("too-much", {"usd": 6.5})
    r = final.reserve("final-1", {"usd": 3.0}, allocation="final")
    final.settle(r.id, {"usd": 3.0})
    assert final.remaining(allocation="final")["effective_remaining"] == {"usd": 1.0}
    assert phase.remaining()["effective_remaining"] == {"usd": 6.0}      # the final's spend came out of its share
    phase.settle(phase.reserve("improve-1", {"usd": 6.0}).id, {"usd": 6.0})
    assert phase.remaining()["effective_remaining"] == {"usd": 0.0}
    assert final.affordable({"usd": 1.0}, allocation="final") and not final.affordable({"usd": 1.0})


def test_a_hold_is_set_aside_once_per_operation(tmp_path):
    account = Budgets(tmp_path).account("activity", {"usd": 10.0})
    first = account.hold("final-share", {"usd": 4.0})
    assert account.hold("final-share", {"usd": 4.0}) == first                # a restart does not hold twice
    assert account.remaining()["effective_remaining"] == {"usd": 6.0}
    account.settle(first, None, "released")
    assert account.hold("final-share", {"usd": 4.0}) == first                # nor take back what was handed on
    assert account.remaining()["effective_remaining"] == {"usd": 10.0}


# ---------------------------------------------------------------------------- pricing
def test_a_strict_dollar_plan_does_not_start_without_a_derivable_bound(tmp_path):
    unmeasurable = Pricing(hidden_fallback=True)
    assert "derivable upper bound" in check_pricing(unmeasurable, {"usd": 5.0})
    assert "does not report" in check_pricing(Pricing(0.003, 0.015, 4000, 1000, hidden_fallback=True), {"usd": 5.0})
    assert not check_pricing(unmeasurable, {"model_calls": 100}) and not check_pricing(PRICING, {"usd": 5.0})
    account = Budgets(tmp_path).account("round", {"model_calls": 3})
    reservation = account.reserve("call", unmeasurable.bound())
    account.settle(reservation.id, unmeasurable.measure({"input_tokens": 10}))
    state = account.remaining()
    assert state["remaining"]["model_calls"] == 2.0 and "usd" not in state["limits"] and "usd" not in state["used"]


def test_a_calls_bound_covers_its_caps_and_every_declared_fallback():
    assert PRICING.bound() == {"model_calls": 1, "input_tokens": 4000, "output_tokens": 1000,
                               "usd": pytest.approx(0.027)}
    fallback = Pricing(0.003, 0.015, 4000, 1000, declared_fallbacks=("m-2",))
    assert fallback.bound()["usd"] == pytest.approx(0.054)
    assert call_bound_problem(PRICING, {"usd": 0.02}).startswith("one call reserves up to 0.027 usd")
    assert call_bound_problem(PRICING, {"usd": 0.03, "output_tokens": 5000}) == ""


def test_usage_vocabularies_are_normalized_and_an_unpriced_cache_is_unknown():
    assert normalize_usage({"prompt_tokens": 1000, "completion_tokens": 50,
                            "prompt_tokens_details": {"cached_tokens": 800}}) == {
        "input_tokens": 200, "output_tokens": 50, "cache_read_tokens": 800, "cache_write_tokens": 0}
    anthropic = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90,
                 "cache_creation_input_tokens": 20}
    assert normalize_usage(anthropic) == {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 90,
                                          "cache_write_tokens": 20}
    assert Pricing(0.003, 0.015).cost(normalize_usage(anthropic)) is None
    cached = Pricing(0.003, 0.015, cache_read_per_1k=0.0003, cache_write_per_1k=0.00375)
    assert cached.cost(normalize_usage({"input_tokens": 1000, "output_tokens": 0, "cache_read_input_tokens": 1000})) \
        == pytest.approx(0.0033)
    measured = cached.measure(anthropic)
    assert measured["input_tokens"] == 120 and measured["output_tokens"] == 5      # the cache counts as input
    assert measured["usd"] == pytest.approx((10 * 0.003 + 5 * 0.015 + 90 * 0.0003 + 20 * 0.00375) / 1000)
    assert "usd" not in Pricing().measure(anthropic)
    # a bounded pricing charges a call it cannot price its bound, never $0; the bound takes the dearest input rate
    bounded = Pricing(0.003, 0.015, max_input_tokens=1000, max_output_tokens=100, cache_write_per_1k=0.00375)
    assert bounded.bound()["usd"] == pytest.approx(0.00375 + 0.0015)
    assert bounded.cost(normalize_usage(anthropic)) is None
    assert bounded.measure(anthropic)["usd"] == pytest.approx(bounded.bound()["usd"])


# ---------------------------------------------------------------------------- the metered provider
def test_a_call_settles_against_what_it_measured(tmp_path):
    account = Budgets(tmp_path).account("round", {"usd": 10.0, "model_calls": 10})
    inner = Scripted(turn())
    provider = MeteredProvider(inner, account, PRICING)
    assert provider.chat("s", [], effort="low").text == "done"
    assert inner.calls == [{"effort": "low"}]                        # keyword arguments pass through untouched
    assert provider.receipts[0].status == "settled" and provider.receipts[0].measured["usd"] == pytest.approx(0.0045)
    assert account.remaining()["remaining"]["usd"] == pytest.approx(10 - 0.0045)
    book = json.loads((Budgets(tmp_path).path).read_text())
    assert [r["operation_id"] for r in book["reservations"].values()] == ["chat#0.0"]


def test_retries_are_bounded_and_each_one_is_reserved_and_charged(tmp_path, no_backoff):
    account = Budgets(tmp_path).account("round", {"usd": 10.0, "model_calls": 10})
    inner = Scripted(ConnectionError("provider unavailable"), ConnectionError("provider unavailable"), turn())
    provider = MeteredProvider(inner, account, PRICING, max_retries=1, operation="arm")
    with pytest.raises(ProviderError, match="ConnectionError: provider unavailable") as failed:
        provider.chat("s", [])           # whatever the provider raised leaves the meter as a ProviderError
    assert failed.value.retryable and isinstance(failed.value.__cause__, ConnectionError)
    assert [r.status for r in provider.receipts] == ["unknown", "unknown"] and len(inner.calls) == 2
    assert len({r.reservation_id for r in provider.receipts}) == 2
    assert account.remaining()["used"]["model_calls"] == 2.0      # both attempts are charged, at their bound
    book = json.loads(Budgets(tmp_path).path.read_text())
    assert sorted(r["operation_id"] for r in book["reservations"].values()) == ["arm#0.0", "arm#0.1"]
    # the third call of the script succeeds: a retry that works settles and returns
    assert provider.chat("s", []).text == "done" and provider.receipts[-1].status == "settled"


def test_a_request_the_provider_refused_as_malformed_is_not_sent_again(tmp_path, no_backoff):
    class BadRequest(Exception):
        status_code = 400
    account = Budgets(tmp_path).account("round", {"model_calls": 10})
    inner = Scripted(BadRequest("bad request"))
    provider = MeteredProvider(inner, account, PRICING, max_retries=3)
    with pytest.raises(ProviderError) as failed:
        provider.chat("s", [])
    assert len(inner.calls) == 1 and not failed.value.retryable and failed.value.status_code == 400
    assert [r.status for r in provider.receipts] == ["unknown"]


def test_a_rate_limit_refusal_is_released_counted_and_retried(tmp_path, no_backoff):
    """A 429 never reached the model: its reservation is released, not kept as unknown, the meter counts it, and the
    retry settles normally."""
    account = Budgets(tmp_path).account("round", {"usd": 1.0, "model_calls": 10})
    inner = Scripted(ProviderError("429 rate limited", retryable=True, unbilled=True), turn())
    provider = MeteredProvider(inner, account, PRICING, max_retries=1)
    assert provider.chat("s", []).text == "done"
    assert [r.status for r in provider.receipts] == ["released", "settled"] and provider.rate_limited == 1
    assert account.remaining()["used"]["model_calls"] == 1.0
    assert account.remaining()["used"]["usd"] == pytest.approx(0.0045)


def test_an_account_refusal_is_released_and_never_retried(tmp_path, no_backoff):
    account = Budgets(tmp_path).account("round", {"model_calls": 10})
    inner = Scripted(ProviderAccountError("credit balance too low"))
    provider = MeteredProvider(inner, account, PRICING, max_retries=3)
    with pytest.raises(ProviderAccountError):
        provider.chat("s", [])
    assert len(inner.calls) == 1 and [r.status for r in provider.receipts] == ["released"]
    assert provider.rate_limited == 0 and not account.remaining()["used"]


def test_a_call_the_budget_cannot_cover_is_not_made(tmp_path):
    account = Budgets(tmp_path).account("tight", {"usd": 0.01, "model_calls": 10})
    inner = Scripted(turn())
    with pytest.raises(BudgetError):
        MeteredProvider(inner, account, PRICING, max_retries=0).chat("s", [])
    assert inner.calls == []


def test_concurrent_calls_through_one_meter_each_get_their_own_reservation(tmp_path):
    """The attempts of one arm share one meter: every call gets an id of its own and settles once, and the provider's
    own retries stay off while any call is in flight."""
    class Slow:
        name, model = "slow", "m-1"
        retries = 3

        def __init__(self):
            self.seen = []
            self.lock = threading.Lock()

        def chat(self, system, messages, specs=()):
            with self.lock:
                self.seen.append(self.retries)
            time.sleep(0.02)
            return turn(100, 10)

    account = Budgets(tmp_path).account("round", {"usd": 10.0, "model_calls": 100})
    inner = Slow()
    provider = MeteredProvider(inner, account, PRICING, max_retries=0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _i: provider.chat("s", []), range(24)))
    ids = [r.reservation_id for r in provider.receipts]
    assert len(ids) == 24 and len(set(ids)) == 24 and all(r.status == "settled" for r in provider.receipts)
    assert account.remaining()["used"]["model_calls"] == 24.0
    assert set(inner.seen) == {0}            # the provider never retried on its own during a metered call
    assert inner.retries == 3                # and has its own setting back once nothing is in flight
    assert provider.max_retries == 3         # its retries became the meter's, each one reserved


def test_a_reservation_refused_for_headroom_waits_for_a_call_in_flight_rather_than_failing(tmp_path):
    """The bounds of the calls in flight hold the headroom: a refused reservation waits for one of them to settle;
    only with nothing in flight is the budget spent."""
    release = threading.Event()

    class Slow:
        name, model = "slow", "m-1"

        def chat(self, system, messages, specs=()):
            release.wait(5)
            return turn()

    account = Budgets(tmp_path).account("round", {"usd": 0.04, "model_calls": 10})     # room for one bound, not two
    provider = MeteredProvider(Slow(), account, PRICING, max_retries=0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(provider.chat, "s", [])
        second = pool.submit(provider.chat, "s", [])
        time.sleep(0.3)
        assert not second.done()                   # waiting for the first to settle, not refused
        release.set()
        first.result(5)
        second.result(5)                           # the first settled at $0.0045, so the second's bound now fits
    assert [r.status for r in provider.receipts] == ["settled", "settled"]


def test_a_failed_call_is_retried_after_the_meters_wait(tmp_path, monkeypatch):
    """A dropped connection is retried after RETRY_SECONDS (5 s, then 20 s), not at once."""
    import time
    from ouroloop.evaluate import budget as b
    from ouroloop.providers import ProviderError
    from ouroloop.runtime.chat import ChatTurn
    monkeypatch.setattr(b, "RETRY_SECONDS", (5.0, 20.0))
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)

    class Flaky:
        retries, calls = 2, 0

        def chat(self, system, messages, specs, **kw):
            self.calls += 1
            if self.calls < 3:
                raise ProviderError("claude CLI: API Error: Connection dropped (ECONNRESET)", retryable=True)
            return ChatTurn("ok", [], "end", usage={"input_tokens": 1, "output_tokens": 1})
    account = b.Budgets(tmp_path / "book").account("arm", {"model_calls": 10})
    metered = b.MeteredProvider(Flaky(), account, b.Pricing(), operation="arm", max_retries=2)
    assert metered.chat("S", [{"role": "user", "content": "x"}], ()).text == "ok"
    assert waits == [5.0, 20.0]
