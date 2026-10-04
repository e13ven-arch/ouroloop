"""M5 acceptance (docs/modules/05_EXECUTION_BUDGET.md §5): the state machine, the shared book, unknown spend and
the fixed stop rules. Fake providers and injected failures only — no sleeps, no real cost."""
import json

import pytest

from ouroloop.chat import ChatTurn
from ouroloop.evolve import contracts as ct
from ouroloop.evolve.execution import (Budgets, BudgetError, Execution, MeteredProvider, Pricing, Schedule,
                                       StateError, check_pricing, execute_experiment, operation_id, spend)


def book(tmp_path):
    return Budgets(tmp_path)


# ---------------------------------------------------------------------------- the state machine
def test_the_three_records_are_separate_and_terminal_states_are_final(tmp_path):
    ex = Execution(tmp_path, "exp-1")
    assert ex.state()["status"] == "drafted"
    for status in ("admitted", "building", "evaluating", "evaluated", "accepted", "installing", "installed"):
        ex.advance(status)
    assert ex.state()["status"] == "installed"
    with pytest.raises(StateError, match="terminal"):
        ex.advance("accepted")
    assert [e["status"] for e in ex.rebuild()["history"]][-1] == "installed"


def test_an_impossible_transition_is_refused_and_cancel_is_always_possible(tmp_path):
    ex = Execution(tmp_path, "exp-2")
    with pytest.raises(StateError, match="not a transition"):
        ex.advance("installed")
    ex.advance("admitted")
    ex.advance("cancelled")
    with pytest.raises(StateError):
        ex.advance("building")


def test_a_failed_installation_may_be_retried_but_a_stale_parent_may_not(tmp_path):
    ex = Execution(tmp_path, "exp-3")
    for status in ("admitted", "building", "evaluating", "evaluated", "accepted", "installing",
                   "installation_failed"):
        ex.advance(status)
    ex.advance("installing")            # same accepted artifact, another attempt
    ex.advance("stale_parent")
    with pytest.raises(StateError):
        ex.advance("installing")


def test_the_snapshot_is_a_cache_of_the_journal(tmp_path):
    ex = Execution(tmp_path, "exp-4")
    ex.advance("admitted")
    ex.advance("building")
    ex.snapshot.unlink()
    assert ex.state()["status"] == "building"


def test_an_operation_id_is_determined_by_the_experiment_step_and_inputs(tmp_path):
    ex = Execution(tmp_path, "exp-5")
    refs = ("proposal:sha256:" + "a" * 64,)
    first = ex.operation("build", refs)
    assert first.id == operation_id("exp-5", "build", refs)
    ex.finish(first, "done", output_refs=("policyset:sha256:" + "b" * 64,))
    again = ex.operation("build", refs)       # a restart finds the recorded operation, not a fresh one
    assert again.id == first.id and again.status == "done" and again.output_refs


# ---------------------------------------------------------------------------- EX-01
def test_ex01_two_arms_share_one_book_and_neither_resets_the_parent(tmp_path):
    b = book(tmp_path)
    top = b.account("round", {"usd": 10.0, "model_calls": 20})
    a, c = top.child("arm-a", {"usd": 5.0}), top.child("arm-b", {"usd": 5.0})
    ra = a.reserve("a1", {"usd": 4.0})
    a.settle(ra.id, {"usd": 3.0})
    rc = c.reserve("c1", {"usd": 4.0})
    c.settle(rc.id, {"usd": 4.0})
    assert top.remaining()["remaining"]["usd"] == 3.0            # the parent sees the sum, counted once
    assert a.remaining()["remaining"]["usd"] == 2.0
    with pytest.raises(BudgetError, match="usd of arm-a"):
        a.reserve("a2", {"usd": 2.5})
    deep = a.child("arm-a-inner", {"usd": 5.0})                   # a nested scope cannot exceed its ancestor
    with pytest.raises(BudgetError, match="usd of arm-a"):
        deep.reserve("a3", {"usd": 2.5})
    assert deep.reserve("a4", {"usd": 1.0}).bound == {"usd": 1.0}


def test_reopening_a_scope_with_other_limits_is_refused(tmp_path):
    b = book(tmp_path)
    b.account("round", {"usd": 10.0})
    assert b.account("round", {"usd": 10.0}).scope_id == "round"
    with pytest.raises(BudgetError, match="other limits"):
        b.account("round", {"usd": 20.0})


# ---------------------------------------------------------------------------- EX-02
def test_ex02_a_lost_response_keeps_its_bound_and_a_retry_is_a_new_call(tmp_path):
    b = book(tmp_path)
    account = b.account("round", {"usd": 10.0, "model_calls": 10})
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


def test_a_call_that_raises_settles_as_unknown(tmp_path):
    b = book(tmp_path)
    account = b.account("round", {"usd": 5.0})

    def call():
        raise TimeoutError("no response")

    with pytest.raises(TimeoutError):
        spend(account, "op-1", {"usd": 2.0}, call)
    assert account.remaining()["remaining"]["usd"] == 3.0 and account.remaining()["unknown"] == 1


def test_spending_over_the_bound_is_a_breach_that_stops_paid_work(tmp_path):
    b = book(tmp_path)
    account = b.account("round", {"usd": 10.0})
    r = account.reserve("op-1", {"usd": 1.0})
    receipt = account.settle(r.id, {"usd": 4.0})
    assert receipt.breach and receipt.measured["usd"] == 4.0
    assert "budget_breach" in account.remaining()["stopped"]
    with pytest.raises(BudgetError, match="no further paid operations"):
        account.reserve("op-2", {"usd": 1.0})
    assert [e["event"] for e in ct.Events(tmp_path).all()] == ["budget_breach"]


# ---------------------------------------------------------------------------- EX-03
def test_ex03_a_candidate_that_could_only_train_is_not_started(tmp_path):
    b = book(tmp_path)
    account = b.account("experiment", {"usd": 10.0})
    account.allocate("verification", {"usd": 6.0})       # the full dev run and the install probe
    assert not account.affordable({"usd": 5.0})          # training may only use what is left over
    assert account.affordable({"usd": 4.0})
    with pytest.raises(BudgetError, match="blocked|exceed"):
        account.reserve("train-1", {"usd": 5.0})
    # the verification itself may spend its allocation
    assert account.reserve("dev-run", {"usd": 6.0}, allocation="verification").bound == {"usd": 6.0}


def test_an_allocation_larger_than_the_scope_is_refused(tmp_path):
    b = book(tmp_path)
    account = b.account("experiment", {"usd": 3.0})
    with pytest.raises(BudgetError, match="does not fit"):
        account.allocate("verification", {"usd": 4.0})


def test_execute_experiment_stops_before_building_when_verification_is_unaffordable(tmp_path):
    b = book(tmp_path)
    account = b.account("experiment", {"usd": 1.0})
    account.allocate("verification", {"usd": 1.0})
    ex = Execution(tmp_path, "exp-6")
    calls = []
    out = execute_experiment(_experiment(tmp_path), {"verify_bound": {"usd": 2.0},
                                                    "build": lambda *a: calls.append("build"),
                                                    "evaluate": lambda *a: calls.append("evaluate")},
                             budget=account, execution=ex)
    assert out["states"] == ["admitted", "inconclusive"] and not calls
    assert ex.state()["history"][-1]["reason"] == "budget_exhausted"


def _experiment(root):
    objects = ct.Objects(root)
    proposal = objects.put(ct.ExperimentProposal(owner="worker", hypothesis="h",
                                                 changes=({"target": "worker.finish", "kind": "prompt",
                                                           "change": {"text": "check"}},)))
    core = objects.put(ct.CoreManifest(domain="rsi"))
    release = objects.put(ct.SystemRelease(
        worker_ref="policyset:sha256:" + "a" * 64, research_ref="policyset:sha256:" + "b" * 64, core_ref=core,
        authorization_ref=objects.put(ct.Authorization(kind="bootstrap"))))
    return ct.Experiment(id="exp-6", proposal_ref=proposal, parent_release=release,
                         experience_ref="change:sha256:" + "c" * 64, core_ref=core,
                         eval_template_ref="change:sha256:" + "d" * 64, budget_scope="experiment")


# ---------------------------------------------------------------------------- EX-04 / EX-07
def test_ex04_retries_are_bounded_and_each_one_is_charged(tmp_path):
    class Flaky:
        name, model = "flaky", "m-1"

        def __init__(self):
            self.calls = 0

        def chat(self, system, messages, specs=()):
            self.calls += 1
            if self.calls < 3:
                raise ConnectionError("provider unavailable")
            return ChatTurn("done", [], "end", usage={"input_tokens": 1000, "output_tokens": 100})

    b = book(tmp_path)
    account = b.account("round", {"usd": 10.0, "model_calls": 10})
    pricing = Pricing(0.003, 0.015, 4000, 1000, source="snapshot")
    provider = MeteredProvider(Flaky(), account, pricing, max_retries=1)
    from ouroloop.providers import ProviderError
    with pytest.raises(ProviderError, match="ConnectionError: provider unavailable") as failed:
        provider.chat("s", [])            # whatever the provider raised leaves the meter as a ProviderError
    assert failed.value.retryable and isinstance(failed.value.__cause__, ConnectionError)
    assert [r.status for r in provider.receipts] == ["unknown", "unknown"]
    assert account.remaining()["used"]["model_calls"] == 2.0      # both attempts are charged
    provider = MeteredProvider(Flaky(), account, pricing, max_retries=0)
    with pytest.raises(ProviderError, match="ConnectionError"):
        provider.chat("s", [])


def test_ex04_a_call_settles_against_what_it_measured(tmp_path):
    class Fine:
        name, model = "fine", "m-1"

        def chat(self, system, messages, specs=()):
            return ChatTurn("done", [], "end", usage={"input_tokens": 1000, "output_tokens": 100})

    b = book(tmp_path)
    account = b.account("round", {"usd": 10.0, "model_calls": 10})
    provider = MeteredProvider(Fine(), account, Pricing(0.003, 0.015, 4000, 1000, source="snapshot"))
    provider.chat("s", [])
    assert provider.receipts[0].measured["usd"] == pytest.approx(0.0045)
    assert account.remaining()["remaining"]["usd"] == pytest.approx(10 - 0.0045)


def test_ex07_a_strict_dollar_plan_does_not_start_without_a_derivable_bound(tmp_path):
    unmeasurable = Pricing(hidden_fallback=True)
    assert check_pricing(unmeasurable, {"usd": 5.0})
    assert not check_pricing(unmeasurable, {"model_calls": 100})
    b = book(tmp_path)
    account = b.account("round", {"model_calls": 3})
    reservation = account.reserve("call", unmeasurable.bound())
    account.settle(reservation.id, unmeasurable.measure({"input_tokens": 10}))
    state = account.remaining()
    assert state["remaining"]["model_calls"] == 2.0 and "usd" not in state["limits"]


# ---------------------------------------------------------------------------- EX-08
def test_ex08_the_stop_rules_are_fixed_and_named():
    s = Schedule(worker_rounds=3, patience=2, max_infrastructure_failures=2)
    base = {"round_index": 0, "idle": 0, "infra_failures": 0, "proposals": True, "affordable": True}
    assert s.stop(**base) == ""
    assert s.stop(**{**base, "affordable": False}) == "budget_exhausted"
    assert s.stop(**{**base, "round_index": 3}) == "rounds_done"
    assert s.stop(**{**base, "infra_failures": 2}) == "infrastructure_failures"
    assert s.stop(**{**base, "idle": 2}) == "patience"
    assert s.stop(**{**base, "proposals": False}) == "no_proposal"


def test_execute_experiment_records_every_state_and_its_operations(tmp_path):
    objects = ct.Objects(tmp_path)
    b = book(tmp_path)
    account = b.account("experiment", {"usd": 10.0})
    ex = Execution(tmp_path, "exp-7")
    candidate = "policyset:sha256:" + "e" * 64
    receipt = ct.EvaluationReceipt(experiment_ref="experiment:sha256:" + "f" * 64, profile="task",
                                   subject_ref=candidate, control_ref=candidate,
                                   plan_ref=objects.put_json("change", {"plan": 1}),
                                   results_ref=objects.put_json("change", {"results": []}), verdict="rejected",
                                   reasons=("no gain",))
    out = execute_experiment(_experiment(tmp_path), {
        "build": lambda experiment, budget: ct.BuildResult(status="ready", candidate_ref=candidate),
        "screen": lambda ref, budget: ("pass", []),
        "evaluate": lambda ref, budget: receipt}, budget=account, execution=ex)
    assert out["states"] == ["admitted", "building", "screening", "evaluating", "evaluated", "rejected"]
    assert out["receipt"].verdict == "rejected" and out["installation"] is None
    saved = json.loads((tmp_path / "rsi" / "operations" /
                        f"{operation_id('exp-7', 'evaluate', (candidate,))}.json").read_text())
    assert saved["status"] == "done" and saved["output_refs"] == [receipt.ref()]


# ---------------------------------------------------------------------------- EX-06
def test_ex06_a_research_arm_that_proposes_a_research_change_is_refused(tmp_path):
    """Inside a campaign the research policy is fixed: a nested research proposal is a candidate defect, and it
    does not open a third level of budget."""
    from ouroloop.evolve.contracts import CoreManifest, ExperimentProposal, Objects
    from ouroloop.research.context import make_context
    from ouroloop.research.experiment import admit
    objects = Objects(tmp_path)
    context = make_context("campaign:pair-1:candidate", goal="improve W", release_ref="release:sha256:" + "a" * 64,
                           objects=objects)
    nested = ExperimentProposal(owner="research", hypothesis="change how I propose",
                                changes=({"target": "research.propose", "kind": "prompt",
                                          "change": {"text": "try harder"}},))
    admission = admit(nested, context=context, core=CoreManifest(domain="rsi"), objects=objects, owner="worker")
    assert admission.status == "invalid"
    assert any("this round produces worker candidates" in r for r in admission.reasons)
    book = Budgets(tmp_path)
    top = book.account("campaign", {"model_calls": 100})
    arm = top.child("campaign:pair-1:candidate", {"model_calls": 50})
    assert set(json.loads((tmp_path / "rsi" / "budget" / "book.json").read_text())["scopes"]) == {
        "campaign", "campaign:pair-1:candidate"}
    assert arm.remaining()["limits"] == {"model_calls": 50.0}


def test_concurrent_calls_through_one_meter_each_get_their_own_reservation(tmp_path):
    """[bench] concurrency (M5 §2): the attempts of one arm share one meter. Every call still gets an id of its
    own and settles once, and the provider's own retries stay off while any call is in flight."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    class Slow:
        name, model = "slow", "m-1"
        retries = 3

        def __init__(self):
            self.seen = []
            self.lock = threading.Lock()

        def chat(self, system, messages, specs=()):
            import time
            with self.lock:
                self.seen.append(self.retries)
            time.sleep(0.02)
            return ChatTurn("done", [], "end", usage={"input_tokens": 100, "output_tokens": 10})

    b = book(tmp_path)
    account = b.account("round", {"usd": 10.0, "model_calls": 100})
    inner = Slow()
    provider = MeteredProvider(inner, account, Pricing(0.003, 0.015, 4000, 1000, source="snapshot"), max_retries=0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _i: provider.chat("s", []), range(24)))
    ids = [r.reservation_id for r in provider.receipts]
    assert len(ids) == 24 and len(set(ids)) == 24 and all(r.status == "settled" for r in provider.receipts)
    assert account.remaining()["used"]["model_calls"] == 24.0
    assert set(inner.seen) == {0}            # the provider never retried on its own during a metered call
    assert inner.retries == 3                # and has its own setting back once nothing is in flight
