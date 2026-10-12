"""What has to hold before a paid run (docs/design/EVALUATE.md §9): every model call on the activity's one book, a cost
per attempt, a restart that pays only for what is missing, the final's share set aside and checked before anything is
paid, and concurrency a setting of the machine. Driven through the real phases on the miniature upstream with the
scripted actor; the improver's cases (research rounds, candidates, the verification bound) wait for stage 3."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import rsi

from ouroloop.activity import cli, phases
from ouroloop.activity.config import ConfigError
from ouroloop.activity.workspace import open_activity
from ouroloop.contracts import BudgetExhausted, BudgetMismatch, ContractError, read_jsonl
from ouroloop.evaluate import attempt as attempt_mod
from ouroloop.evaluate.attempt import make_agent_factory, run_tasks
from ouroloop.evaluate.budget import Budgets, MeteredProvider, Pricing, normalize_usage
from ouroloop.evaluate.final import FinalPlan, final_estimate, share_problem, state_path
from ouroloop.evaluate.gate import GatePlan
from ouroloop.evaluate.release import GateReceipt, Release
from ouroloop.harness import package as pk
from ouroloop.providers import ProviderAccountError, ProviderError
from ouroloop.runtime.worker import ProcessWorker

# One call's reservation under rsi.PRICING: 20000 input tokens at the dearest input-side rate (a cache write) and 5000
# output tokens.
BOUND = 20 * rsi.PRICING["cache_write_per_1k"] + 5 * rsi.PRICING["output_per_1k"]     # $0.26


def quiet(_line: str) -> None:
    pass


def book(activity) -> dict:
    return json.loads(activity.budgets.path.read_text(encoding="utf-8"))


def reservations(activity) -> list[dict]:
    return list(book(activity)["reservations"].values())


def install_w_star(activity, *, operation_id: str = "install-w-star") -> Release:
    """W* as a gate installs it: an accepted receipt, registered by a completed gate, and the release that names it.
    The package is W0's own: the money tests need a second arm, not a better one."""
    parent = activity.active_release()
    package_ref = parent.package_ref
    receipt = activity.objects.put(GateReceipt(subject_ref=package_ref, control_ref=parent.package_ref,
                                               parent_release=parent.ref(), verdict="accepted"))
    activity.releases.register_receipt(receipt)
    candidate = activity.releases.candidate(package_ref, receipt, parent=parent)
    assert activity.releases.install(candidate, receipt, expected_parent=parent.ref(),
                                     operation_id=operation_id).status == "installed"
    # v1.2: the final reads the release line; the gate's promotion puts W* there (EVALUATE §7)
    return activity.releases.promote(candidate, receipt, operation_id=f"promote:{operation_id}")


def reopen(root: Path, upstream: str, *, provider=None, **sections):
    """The activity opened again by a later command: the split frozen on disk is read, not frozen anew."""
    actor = provider if provider is not None else rsi.BusinessWorker()
    return open_activity(rsi.config(root, upstream, **sections), out=quiet,
                         worker_factory=lambda: ProcessWorker(for_tests=True), provider_factory=lambda _t: actor)


def sealed(root: Path, upstream: str, *, provider=None, **sections):
    """An activity whose baseline ran, with W* installed and the final sealed by `freeze`, opened again with
    [rsi] final_plan_ref naming it, as the user does. -> (activity, freeze's refs)."""
    act = rsi.activity(root, upstream, provider=provider, final={}, **sections)
    phases.baseline(act, out=quiet)
    install_w_star(act)
    refs = phases.freeze(act, out=quiet)
    act = reopen(root, upstream, provider=provider, final={}, rsi={"final_plan_ref": refs["final_plan_ref"]},
                 **sections)
    return act, refs


def toml(cfg: dict) -> str:
    """A config dict as the TOML file the command line reads."""
    def value(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, str):
            return json.dumps(v)
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{" + ", ".join(f"{json.dumps(k)} = {value(x)}" for k, x in v.items()) + "}"
        raise TypeError(f"no TOML for {v!r}")
    lines = []
    for section, table in cfg.items():
        if section.startswith("_"):
            continue
        lines += [f"[{section}]", *(f"{json.dumps(k)} = {value(v)}" for k, v in table.items()), ""]
    return "\n".join(lines)


class Crash(BaseException):
    """A process dying mid-run: not an exception any handler in the loop is allowed to swallow."""


class CrashingWorker(rsi.BusinessWorker):
    """The scripted actor, until its n-th call."""

    def __init__(self, crash_at: int, **kw):
        super().__init__(**kw)
        self.crash_at = crash_at

    def chat(self, system, messages, specs, **kw):
        if self.calls + 1 >= self.crash_at:
            self.calls += 1
            raise Crash(f"the process died at call {self.calls}")
        return super().chat(system, messages, specs, **kw)


def tasks_asked(actor, suite) -> set[str]:
    """The tasks whose request reached this actor."""
    triggers = {suite.tasks[t].trigger: t for t in suite.ids()}
    return {triggers[m["content"]] for seen in actor.seen for m in seen["messages"][:1] if m["content"] in triggers}


# ---------------------------------------------------------------------------- one activity, run once
@pytest.fixture(scope="module")
def paid(tmp_path_factory, fake_upstream):
    """smoke, baseline, W* installed, freeze (the final sealed) and the final, all with one counting actor."""
    root = tmp_path_factory.mktemp("paid")
    actor = rsi.BusinessWorker()
    act = rsi.activity(root, fake_upstream, provider=actor, final={})
    assert phases.smoke(act, out=quiet)["ok"]
    phases.baseline(act, out=quiet)
    install_w_star(act)
    refs = phases.freeze(act, out=quiet)
    act = reopen(root, fake_upstream, provider=actor, final={}, rsi={"final_plan_ref": refs["final_plan_ref"]})
    lines: list[str] = []
    result = phases.evaluate(act, out=lines.append)
    return SimpleNamespace(activity=act, actor=actor, refs=refs, lines=lines, result=result, root=root)


# ---------------------------------------------------------------------------- metering
def test_every_agent_call_is_reserved_and_settled_on_the_activity_book(paid):
    act, actor = paid.activity, paid.actor
    held = reservations(act)
    assert actor.calls > 0 and len(held) == actor.calls                 # one reservation per call the actor got
    assert all(r["status"] == "settled" for r in held)
    assert {r["scope_id"] for r in held} == {"rsi-test:smoke", "rsi-test:baseline", "rsi-test:final"}
    # one gateway_call line per reservation: nothing reached the model around the meter
    lines = [line for path in act.transcripts.glob("*.jsonl") for line in read_jsonl(path)
             if line["kind"] == "gateway_call"]
    assert len(lines) == len(held)
    # the root's total is what the attempts measured, in calls and in dollars
    used = act.root_account().remaining()["used"]
    rows = list(act.results.all())
    assert used["model_calls"] == len(held)
    assert used["usd"] == pytest.approx(sum(r.cost_usd for r in rows), abs=1e-6) and used["usd"] > 0


def test_one_activity_has_one_budget_root(paid, fake_upstream):
    act = paid.activity
    scopes = book(act)["scopes"]
    assert [name for name, s in scopes.items() if s["parent"] is None] == ["rsi-test"]
    assert all(s["parent"] == "rsi-test" for name, s in scopes.items() if name != "rsi-test")
    # the research model's calls hang under the same root, booked under their step
    reply = act.research_provider("diagnose").chat("", [{"role": "user", "content": "what failed?"}], [])
    assert reply.usage
    (research,) = [r for r in reservations(act) if r["scope_id"] == "rsi-test:research:diagnose"]
    assert research["status"] == "settled" and research["operation_id"] == "diagnose#0.0"
    assert book(act)["scopes"]["rsi-test:research:diagnose"]["parent"] == "rsi-test"
    # reopening the root with other limits is refused, on the book and through the activity (another K)
    with pytest.raises(BudgetMismatch, match="other limits"):
        act.budgets.account("rsi-test", {"usd": 500.0})
    with pytest.raises(ConfigError, match="a changed K is a new activity"):
        reopen(paid.root, fake_upstream, final={}, budget={"usd": 500.0})
    # and the same workspace under another scope name does not open a second root beside the first
    renamed = reopen(paid.root, fake_upstream, final={}, rsi={"scope_id": "rsi-again"})
    with pytest.raises(ConfigError, match="one budget root"):
        renamed.account("baseline")
    assert [name for name, s in book(act)["scopes"].items() if s["parent"] is None] == ["rsi-test"]


class BigCalls(rsi.BusinessWorker):
    """The scripted actor, each call reporting 10000 input and 2000 output tokens: $0.08 at rsi.PRICING."""

    def __init__(self):
        super().__init__(usage={"input_tokens": 10000, "output_tokens": 2000})


def test_a_budget_that_runs_out_mid_run_ends_attempts_as_budget_exhausted(tmp_path, fake_upstream):
    """$0.84 with $0.30 set aside for the final leaves $0.54: four calls of $0.08 fit beside one call's $0.26 bound,
    the fifth does not. The attempts that could not run are budget_exhausted, invalid and never refilled, and the
    final's share is untouched."""
    actor = BigCalls()
    act = rsi.activity(tmp_path, fake_upstream, provider=actor, budget={"usd": 0.84, "final_usd": 0.3})
    phases.baseline(act, out=quiet)
    rows = list(act.results.all())
    assert len(rows) == len(rsi.TRAIN) + len(rsi.DEV)
    first = next(i for i, r in enumerate(rows) if r.failure_class == "budget_exhausted")
    assert first > 0 and all(r.validity == "valid" for r in rows[:first])        # it ran out part way
    exhausted = rows[first:]
    assert all(r.failure_class == "budget_exhausted" and r.validity == "invalid" for r in exhausted)
    assert not any(r.supersedes for r in rows)                                   # nothing was refilled
    held = reservations(act)
    assert len(held) == actor.calls == 4 and all(r["status"] == "settled" for r in held)
    root = act.root_account().remaining(allocation="final")
    # the final's share is untouched (a floor under the final, EVALUATE §9): its room is the share and what the root
    # has left besides, nothing of it spent
    assert root["effective_remaining"]["usd"] == pytest.approx(0.84 - 4 * 0.08) and not root["stopped"]
    assert act.budgets.usage(act.scope_id, book(act), allocation="final").get("usd", 0.0) == 0.0
    assert not book(act)["breaches"]


# ---------------------------------------------------------------------------- cost
class CachedWorker(rsi.BusinessWorker):
    """The scripted actor, reporting its usage the way an OpenAI-compatible API does, with a cache hit."""

    def chat(self, system, messages, specs, **kw):
        turn = super().chat(system, messages, specs, **kw)
        turn.usage = {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 800}}
        return turn


def test_an_attempt_carries_its_cost_and_its_cache_counts(tmp_path, fake_upstream):
    actor = CachedWorker()
    act = rsi.activity(tmp_path / "priced", fake_upstream, provider=actor)
    pricing = act.pricing()
    assert (pricing.cache_read_per_1k, pricing.cache_write_per_1k) == (rsi.PRICING["cache_read_per_1k"],
                                                                     rsi.PRICING["cache_write_per_1k"])
    report = phases.smoke(act, out=quiet)
    (row,) = list(act.results.all())
    assert report["ok"] and row.validity == "valid"
    calls = actor.calls
    assert row.usage == {"input_tokens": 200 * calls, "output_tokens": 50 * calls, "cache_read_tokens": 800 * calls,
                         "cache_write_tokens": 0}
    # recomputed from the record alone, the cost is the one the attempt was written with, and the one on the book
    assert row.cost_usd == pytest.approx(pricing.cost(row.usage)) and row.cost_usd > 0
    assert act.root_account().remaining()["used"]["usd"] == pytest.approx(row.cost_usd)
    # without a cache price, the cost of a call that read the cache is unknown, never the cost of its uncached part
    unpriced = rsi.activity(tmp_path / "unpriced", fake_upstream, provider=CachedWorker(),
                            pricing={"cache_read_per_1k": None})
    phases.smoke(unpriced, out=quiet)
    (row,) = list(unpriced.results.all())
    assert row.validity == "valid" and row.cost_usd is None and row.usage["cache_read_tokens"] > 0
    # ... and the book charges each such call its bound, not $0: the dollar limit still holds
    settled = [r for r in reservations(unpriced) if r["status"] == "settled"]
    used = unpriced.root_account().remaining()["used"]["usd"]
    assert settled and used == pytest.approx(len(settled) * unpriced.pricing().bound()["usd"])


def test_usage_vocabularies_are_normalized_and_an_unpriced_cache_is_unknown():
    assert normalize_usage({"prompt_tokens": 1000, "completion_tokens": 50,
                            "prompt_tokens_details": {"cached_tokens": 800}}) == {
        "input_tokens": 200, "output_tokens": 50, "cache_read_tokens": 800, "cache_write_tokens": 0}
    assert normalize_usage({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90,
                            "cache_creation_input_tokens": 7}) == {
        "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 90, "cache_write_tokens": 7}
    pricing = Pricing(0.003, 0.015)
    assert pricing.cost(normalize_usage({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90})) \
        is None
    assert pricing.cost(normalize_usage({"input_tokens": 1000, "output_tokens": 0})) == pytest.approx(0.003)
    cached = Pricing(0.003, 0.015, cache_read_per_1k=0.0003, cache_write_per_1k=0.00375)
    assert cached.cost(normalize_usage({"input_tokens": 1000, "output_tokens": 0, "cache_read_input_tokens": 1000,
                                        "cache_creation_input_tokens": 1000})) == pytest.approx(0.00705)


def test_a_bound_is_only_breached_on_a_dimension_it_bounded(tmp_path):
    account = Budgets(tmp_path).account("a", {"model_calls": 10})
    reservation = account.reserve("call", {"model_calls": 1})
    receipt = account.settle(reservation.id, {"model_calls": 1, "input_tokens": 5000, "output_tokens": 900})
    assert receipt.breach == {} and not account.remaining()["stopped"]
    # over a bounded dimension it is a breach: recorded, and the scope pays for nothing more
    reservation = account.reserve("call", {"model_calls": 1})
    receipt = account.settle(reservation.id, {"model_calls": 2})
    assert receipt.breach == {"model_calls": {"bound": 1.0, "measured": 2.0}}
    assert account.remaining()["stopped"]
    with pytest.raises(BudgetExhausted, match="no further paid operations"):
        account.reserve("call", {"model_calls": 1})


# ---------------------------------------------------------------------------- resume
def test_a_baseline_resumed_after_a_crash_runs_only_what_is_missing(tmp_path, fake_upstream):
    act = rsi.activity(tmp_path, fake_upstream, provider=CrashingWorker(crash_at=8))
    with pytest.raises(Crash):
        phases.baseline(act, out=quiet)
    before = list(act.results.all())
    assert 0 < len(before) < len(rsi.TRAIN) + len(rsi.DEV)
    # the call that died holds its bound: nobody confirmed it was never charged
    assert [r["status"] for r in reservations(act)].count("open") == 1
    fresh = rsi.BusinessWorker()
    act = reopen(tmp_path, fake_upstream, provider=fresh)
    phases.baseline(act, out=quiet)
    after = list(act.results.all())
    ids = [r.attempt_id for r in after]
    assert len(ids) == len(set(ids)) == len(rsi.TRAIN) + len(rsi.DEV)       # nothing was run twice
    assert {r.attempt_id for r in before} <= set(ids)                        # nor replaced
    # what was on record cost nothing: the actor was asked exactly the tasks that were missing
    assert tasks_asked(fresh, act.suite) == set(rsi.TRAIN + rsi.DEV) - {r.task_id for r in before}
    assert len([r for r in reservations(act) if r["status"] == "settled"]) == 7 + fresh.calls


def test_a_final_resumed_after_a_crash_runs_only_what_is_missing(tmp_path, fake_upstream):
    act, _refs = sealed(tmp_path, fake_upstream)
    planned = len(rsi.FINAL) * int(act.k["final"]["repeats"])
    act.provider_factory = lambda _table: CrashingWorker(crash_at=6)      # it dies in the first arm, W0
    with pytest.raises(Crash):
        phases.evaluate(act, out=quiet)
    before = [r for r in act.results.all() if r.split == "test"]
    assert 0 < len(before) < planned
    fresh = rsi.BusinessWorker()
    act.provider_factory = lambda _table: fresh
    lines: list[str] = []
    result = phases.evaluate(act, out=lines.append)
    after = [r for r in act.results.all() if r.split == "test"]
    ids = [r.attempt_id for r in after]
    assert len(ids) == len(set(ids)) == 2 * planned
    assert {r.attempt_id for r in before} <= set(ids)
    assert any("kept, not run again" in line for line in lines)
    assert all(arm["summary"]["attempts"] == planned for arm in result["arms"].values())
    estimate = {a["arm"]: a for a in result["estimate"]["arms"]}
    assert estimate["W0"]["done"] == len(before) and estimate["W0"]["attempts"] == planned - len(before)


class FlakyOnce(rsi.BusinessWorker):
    """The scripted actor, whose n-th call fails on the provider's side (not retryable, maybe charged)."""

    def __init__(self, fail_at: int):
        super().__init__()
        self.fail_at = fail_at

    def chat(self, system, messages, specs, **kw):
        if self.calls + 1 == self.fail_at:
            self.calls += 1
            raise ProviderError("upstream returned 500 after the request was accepted", retryable=False)
        return super().chat(system, messages, specs, **kw)


@pytest.mark.parametrize("phase", ["baseline", "final"])
def test_a_refill_is_written_with_what_it_supersedes(phase, tmp_path, fake_upstream):
    """An attempt the provider broke is infrastructure: kept on record, refilled once under K's retry rule, the refill
    naming it; the failed call stays on the book at its bound (unknown, not free)."""
    actor = FlakyOnce(fail_at=2)
    if phase == "baseline":
        act = rsi.activity(tmp_path, fake_upstream, provider=actor)
        phases.baseline(act, out=quiet)
        split = "train"
    else:
        act, _refs = sealed(tmp_path, fake_upstream)
        act.provider_factory = lambda _table: actor
        phases.evaluate(act, out=quiet)
        split = "test"
    rows = [r for r in act.results.all() if r.split == split]
    broken = [r for r in rows if r.failure_class == "infrastructure"]
    refills = [r for r in rows if r.supersedes]
    assert len(broken) == 1 and broken[0].validity == "invalid"
    assert len(refills) == 1 and refills[0].validity == "valid" and refills[0].supersedes == broken[0].attempt_id
    assert (refills[0].task_id, refills[0].attempt) == (broken[0].task_id, broken[0].attempt)
    assert refills[0].attempt_id == broken[0].attempt_id[:-2] + "#1"
    assert [r["status"] for r in reservations(act)].count("unknown") == 1


# ---------------------------------------------------------------------------- the final's share
def test_the_final_spends_the_share_set_aside_for_it(paid):
    act = paid.activity
    held = reservations(act)
    final = [r for r in held if r["scope_id"] == "rsi-test:final"]
    assert final and all(r["allocation"] == "final" for r in final)
    assert not [r for r in held if r["allocation"] == "final" and r not in final]
    spent = act.budgets.usage("rsi-test", book(act), allocation="final")["usd"]
    test_rows = [r for r in act.results.all() if r.split == "test"]
    assert spent == pytest.approx(sum(r.cost_usd for r in test_rows), abs=1e-6) and spent > 0
    assert act.root_account().remaining()["allocations"] == {"final": {"usd": 10.0}}


def test_a_completed_final_run_again_pays_for_nothing(paid):
    act, actor = paid.activity, paid.actor
    rows, held, calls = len(list(act.results.all())), len(reservations(act)), actor.calls
    result = phases.evaluate(act, out=quiet)
    assert len(list(act.results.all())) == rows and len(reservations(act)) == held and actor.calls == calls
    assert [a["attempts"] for a in result["estimate"]["arms"]] == [0, 0] and result["estimate"]["expected"] == 0


def test_the_final_prints_its_estimate_first_from_the_dev_readings(paid):
    """Before the first test attempt it prints, per arm, the attempts to run × the arm's cost per valid attempt over
    the dev readings of what it executes — W*'s own release has none, its package's W0 readings count — and the
    in-flight reservations beside them."""
    act, lines, estimate = paid.activity, paid.lines, paid.result["estimate"]
    first_arm = next(i for i, line in enumerate(lines) if line.startswith("arm "))
    assert [i for i, line in enumerate(lines) if line.startswith("estimate")] and \
        max(i for i, line in enumerate(lines) if line.startswith("estimate")) < first_arm
    dev = [r for r in act.results.all() if r.split == "dev" and r.validity == "valid"]
    cpa = round(sum(r.cost_usd for r in dev) / len(dev), 6)
    planned = len(rsi.FINAL) * int(act.k["final"]["repeats"])
    assert {a["arm"]: (a["attempts"], a["cost_per_attempt"], a["readings_split"]) for a in estimate["arms"]} == \
        {"W0": (planned, cpa, "dev"), "W_star": (planned, cpa, "dev")}
    assert estimate["expected"] == pytest.approx(2 * planned * cpa, abs=1e-6)
    assert estimate["reservations"] == round(BOUND, 4)                 # one attempt in flight at a time here
    # what was left to the final when it started: its share and what the root had left besides (EVALUATE §9)
    ledger = act.budgets._read()
    before = act.budgets.usage(act.scope_id, ledger)["usd"] - act.budgets.usage(f"{act.scope_id}:final", ledger)["usd"]
    room = round(float(act.k["budget"]["usd"]) - before, 2)
    assert any(line.startswith("estimate   expected $") and f"left to the final: ${room:.2f}" in line for line in lines)


def test_the_final_estimate_finds_readings_by_what_a_release_executes(paid):
    act = paid.activity
    plan = FinalPlan.from_json(act.objects.get(paid.refs["final_plan_ref"]))
    rows = list(act.results.all())

    def execution(ref):
        rel = act.releases.load(ref)
        return rel.package_ref, rel.core_ref
    seed = act.seed_release()
    other = pk.build(act.objects, {**act.package(seed).files, "prompt/system.md": "Check twice."},
                     limits=pk.Limits.from_core(act.k), seed=act.package(seed))
    receipt = act.objects.put(GateReceipt(subject_ref=other.ref(), verdict="accepted"))
    another = act.objects.put(Release(parent_ref=seed.ref(), package_ref=other.ref(), improver_ref=seed.improver_ref,
                                      core_ref=seed.core_ref, receipt_ref=receipt, reason="gate"))
    priced = final_estimate(rows, plan, plan_ref=None, execution_of=execution, pricing=act.pricing())
    assert priced["expected"] is not None and not priced["unknown"]
    unread = FinalPlan(experiment_id="x", tasks=plan.tasks, repeats=1, arms={"W0": seed.ref(), "W2": another},
                       claims=("C1",), split_hash=plan.split_hash, core_ref=plan.core_ref)
    estimate = final_estimate(rows, unread, plan_ref=None, execution_of=execution, pricing=act.pricing())
    assert estimate["expected"] is None and estimate["unknown"] == ["W2"]          # unknown, never 0
    assert "cannot be checked" in share_problem(estimate, 10.0)


def test_a_final_plan_refuses_an_arm_name_no_attempt_id_can_carry():
    """An arm's name is part of every attempt id, which is the transcript's session id: an arm named "W*" would fail
    every one of its attempts as infrastructure, so the plan is refused before it is sealed."""
    ref = "release:sha256:" + "0" * 64
    with pytest.raises(ContractError, match="cannot be part of an attempt id"):
        FinalPlan(experiment_id="x", tasks=("t",), repeats=1, arms={"W0": ref, "W*": ref})
    assert FinalPlan(experiment_id="x", tasks=("t",), repeats=1, arms={"W0": ref, "W_star": ref}).arms


def test_a_final_the_share_cannot_hold_is_refused_before_the_first_attempt(tmp_path, fake_upstream):
    act, refs = sealed(tmp_path, fake_upstream)
    root = act.root_account()
    spent = root.reserve("spent on the final", {"usd": 9.9}, allocation="final")
    root.settle(spent.id, {"usd": 9.9})
    # the final's room is its share and what the root has left besides (EVALUATE §9): the rest of the root goes too
    left = root.remaining(allocation="final")["effective_remaining"]["usd"]
    more = root.reserve("spent elsewhere", {"usd": round(left - 0.10, 6)})
    root.settle(more.id, {"usd": round(left - 0.10, 6)})          # $0.10 left: under one call's $0.26 bound
    actor = rsi.BusinessWorker()
    act.provider_factory = lambda _table: actor
    lines: list[str] = []
    with pytest.raises(ValueError, match=r"do not fit the \$0\.10 left to the final; nothing was run"):
        phases.evaluate(act, out=lines.append)
    assert not [r for r in act.results.all() if r.split == "test"] and actor.calls == 0
    assert not list(act.events.read("test_read"))
    assert any(line.startswith("estimate   expected $") and "left to the final: $0.10" in line for line in lines)


def test_a_final_plan_the_share_cannot_hold_is_not_sealed(tmp_path, fake_upstream):
    """The final's room — its share and what the root has left besides (EVALUATE §9) — holds one call's bound in
    flight, not that plus the two stuck ones a share keeps room for and the plan's spend: refused at freeze, and
    nothing is sealed. Without a priced reading the share cannot be checked, and that is refused too."""
    act = rsi.activity(tmp_path, fake_upstream, final={}, budget={"usd": 50.0, "final_usd": 0.5})
    install_w_star(act)
    plan = phases.final_plan(act)
    with pytest.raises(ValueError, match="cannot be checked: no priced reading"):
        phases.freeze(act, out=quiet)
    assert not state_path(act.root, plan).exists()
    phases.baseline(act, out=quiet)
    root = act.root_account()                      # the rounds spent the root down to the final's $0.50 share
    left = root.remaining()["effective_remaining"]["usd"]
    root.settle(root.reserve("spent elsewhere", {"usd": round(left, 6)}).id, {"usd": round(left, 6)})
    assert phases.final_room(act) == 0.5
    lines: list[str] = []
    with pytest.raises(ValueError, match=r"the final's room \$0\.50 \(its share \$0\.50 and what the root has left "
                                         r"besides\) cannot hold the plan: expected \$0\.\d\d \+ 1 in-flight and 2 "
                                         r"stuck reservations of \$0\.2600"):
        phases.freeze(act, out=lines.append)
    assert not state_path(act.root, plan).exists() and not list(act.events.read("final_sealed"))
    assert not any(line.startswith("[rsi]") for line in lines) and any(line.startswith("estimate") for line in lines)


# ---------------------------------------------------------------------------- freezing
def test_freeze_prints_the_rsi_lines_and_the_same_tables_freeze_to_the_same_refs(paid, fake_upstream):
    act = paid.activity
    lines: list[str] = []
    refs = phases.freeze(act, out=lines.append)
    assert refs == paid.refs                                              # the same tables freeze to the same refs
    assert "[rsi]" in lines and f'core_ref = "{act.core_ref}"' in lines and 'scope_id = "rsi-test"' in lines
    assert f'final_plan_ref = "{refs["final_plan_ref"]}"' in lines
    template = act.objects.get(refs["gate_template"])
    assert template["task_ids"] == list(rsi.DEV) and template["core_ref"] == act.core_ref
    assert template["gate_plan"]["min_gain"] == act.k["gate"]["min_gain"]
    plan = act.objects.get(refs["final_plan_ref"])
    assert plan["tasks"] == list(rsi.FINAL) and sorted(plan["arms"]) == ["W0", "W_star"]


def test_a_sealed_final_plan_cannot_be_sealed_again_differently(tmp_path, fake_upstream):
    """Another W* installed after the final was sealed (its readings priced, so the share check passes) makes another
    plan under the same experiment: refused, the sealed one stays."""
    act, refs = sealed(tmp_path, fake_upstream)
    install_w_star(act, operation_id="install-w-two")
    with pytest.raises(ValueError, match="a changed final plan is a new activity"):
        phases.freeze(act, out=quiet)
    state = json.loads(state_path(act.root, phases.final_plan(act)).read_text(encoding="utf-8"))
    assert state["plan"] == refs["final_plan_ref"]


def test_freeze_refuses_what_it_cannot_freeze_faithfully(tmp_path, fake_upstream):
    """A table or key K does not have (a dollar budget by another name, an economy gate, an old [freeze] table) or a
    loosened gate is refused when the activity opens; a gate the plan cannot run (two confirmation readings) when it
    freezes. Either way before anything is frozen."""
    for n, (sections, error, match) in enumerate((
            ({"budget": {"usd": 50.0, "dollars": 5}}, ConfigError, r"\[budget\] does not take \['dollars'\]"),
            ({"limits": {"max_turns": 3}}, ConfigError, r"\[limits\] does not take \['max_turns'\]"),
            ({"gate": {"min_gain": 0.0}}, ConfigError, "below the preset's"),
            ({"gate": {"mode": "economy"}}, ConfigError, r"\[gate\] does not take \['mode'\]"),
            ({"freeze": {"budget": {"usd": 5}}}, ConfigError, "is not read any more"),
            ({"gate": {"confirmations": 2}}, ContractError, "0 or 1"))):
        root = tmp_path / str(n)
        with pytest.raises(error, match=match):
            phases.freeze(rsi.activity(root, fake_upstream, **sections), out=quiet)
        assert not [e for e in read_jsonl(root / "ws" / "events.jsonl") if e.get("event") == "gate_template"]


def test_a_gate_plan_under_min_tasks_is_refused_at_freeze(tmp_path, fake_upstream):
    act = rsi.activity(tmp_path / "over", fake_upstream, gate={"min_tasks": 25})
    lines: list[str] = []
    with pytest.raises(ValueError, match=r"the gate runs 3 dev tasks, fewer than its min_tasks 25"):
        phases.freeze(act, out=lines.append)
    assert not list(act.events.read("gate_template")) and "[rsi]" not in lines
    act = rsi.activity(tmp_path / "fits", fake_upstream, gate={"min_tasks": 3})
    template = act.objects.get(phases.freeze(act, out=quiet)["gate_template"])
    assert template["gate_plan"]["min_tasks"] == 3
    with pytest.raises(ContractError, match="min_tasks"):
        GatePlan(min_tasks=0)


def test_the_freeze_command_exits_two_on_a_bad_table(tmp_path, fake_upstream, capsys):
    cfg = rsi.config(tmp_path, fake_upstream, budget={"usd": 50.0, "dollars": 5})
    rsi.write_split(tmp_path, rsi.suite(fake_upstream))
    path = tmp_path / "freeze.toml"
    path.write_text(toml(cfg), encoding="utf-8")
    assert cli.main(["freeze", "--config", str(path)]) == 2
    assert "does not take ['dollars']" in capsys.readouterr().err


# ---------------------------------------------------------------------------- concurrency
def test_concurrency_is_a_machine_setting_that_enters_no_frozen_plan(tmp_path, fake_upstream, monkeypatch):
    """[bench] concurrency and `--concurrency N` change no frozen ref: K, the gate template and the final plan are
    the same at any concurrency, an activity reopened at another one is the same activity, and the command line's
    value lasts one run without touching the config file."""
    one = rsi.activity(tmp_path / "one", fake_upstream, bench={"concurrency": 1})
    three = rsi.activity(tmp_path / "three", fake_upstream, bench={"concurrency": 3})
    assert (one.concurrency, three.concurrency) == (1, 3)
    assert one.core_ref == three.core_ref
    assert phases.freeze(one, out=quiet)["gate_template"] == phases.freeze(three, out=quiet)["gate_template"]
    assert phases.final_plan(one).ref() == phases.final_plan(three).ref()
    again = reopen(tmp_path / "one", fake_upstream, bench={"concurrency": 4})
    assert again.core_ref == one.core_ref and again.concurrency == 4
    seen = []
    monkeypatch.setattr(phases, "report", lambda activity, **kw: seen.append(activity.concurrency) or {})
    cfg = rsi.config(tmp_path / "one", fake_upstream)
    path = tmp_path / "bench.toml"
    path.write_text(toml(cfg), encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    assert cli.main(["bench", "--config", str(path), "--phase", "report", "--concurrency", "3"]) == 0
    assert cli.main(["bench", "--config", str(path), "--phase", "report"]) == 0
    assert seen == [3, 1] and path.read_text(encoding="utf-8") == text


class InFlight(rsi.BusinessWorker):
    """The scripted actor, counting the attempts that have a model call in flight at once across every caller. The
    first call of an attempt waits a moment for a third to join it, so attempts that could overlap do."""
    lock = threading.Condition()
    running = 0
    peak = 0

    def chat(self, system, messages, specs, **kw):
        with InFlight.lock:
            InFlight.running += 1
            InFlight.peak = max(InFlight.peak, InFlight.running)
            InFlight.lock.notify_all()
            if len(messages) == 1:
                InFlight.lock.wait_for(lambda: InFlight.running >= 3, timeout=0.5)
        try:
            return super().chat(system, messages, specs, **kw)
        finally:
            with InFlight.lock:
                InFlight.running -= 1


def test_the_in_flight_cap_is_shared_by_every_caller_in_the_process(tmp_path, fake_upstream):
    """Two callers at once, each asking for 2 in flight, cannot between them have 4: one semaphore of the process
    caps them, so the actor never sees more than 2 attempts' calls at a time. A new size replaces the pool."""
    InFlight.running, InFlight.peak = 0, 0
    failures: list = []

    def caller(name: str) -> None:
        try:
            act = rsi.activity(tmp_path / name, fake_upstream)
            release = act.active_release()
            run = act.run_manifest(candidate=name, role="base", split="dev", release=release)
            make = make_agent_factory(provider=InFlight(), package=act.package(release), core=act.k,
                                      worker=ProcessWorker(for_tests=True), root=act.root, pricing=act.pricing(),
                                      release_ref=release.ref(), core_ref=act.core_ref, arm=name)
            rows = run_tasks(act.suite, list(rsi.DEV), make, run=run, splits=act.splits, results=act.results,
                             arm=name, release_ref=release.ref(), concurrency=2)
            assert [r.task_id for r in rows] == list(rsi.DEV) and all(r.validity == "valid" for r in rows)
        except BaseException as e:  # noqa: BLE001 - reported on the main thread
            failures.append(e)
    threads = [threading.Thread(target=caller, args=(name,)) for name in ("subject", "control")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not failures, failures
    assert InFlight.peak == 2
    cap = attempt_mod.in_flight_cap(2)
    assert attempt_mod.in_flight_cap(2) is cap
    assert attempt_mod.in_flight_cap(3) is not cap and attempt_mod.in_flight_cap(3) is attempt_mod.in_flight_cap(3)
    attempt_mod.in_flight_cap(1)


# ---------------------------------------------------------------------------- the provider's refusals
class RateLimitedThenRefused(rsi.BusinessWorker):
    """The actor's first call is turned away with a 429 (unbilled, retryable), the next answer, and the n-th finds
    the account refused."""

    def __init__(self, refuse_at: int):
        super().__init__()
        self.refuse_at = refuse_at
        self.sent = 0

    def chat(self, system, messages, specs, **kw):
        self.sent += 1
        if self.sent == 1:
            raise ProviderError("API Error: 429 rate_limit_error", retryable=True, unbilled=True)
        if self.sent == self.refuse_at:
            raise ProviderAccountError("You've hit your usage limit")
        return super().chat(system, messages, specs, **kw)


def test_a_rate_limit_is_released_and_counted_and_an_account_error_stops_the_run(tmp_path, fake_upstream):
    """A 429 is absorbed by the actor's meter: released on the book (never charged), sent again, counted; the count
    survives the account error that then stops the baseline, which is raised, not recorded as a task's failure."""
    actor = RateLimitedThenRefused(refuse_at=6)
    act = rsi.activity(tmp_path, fake_upstream, provider=actor)
    meters: list = []
    build = act.actor_provider

    def keep(account, allocation=None):
        meters.append(build(account, allocation=allocation))
        return meters[-1]
    act.actor_provider = keep
    with pytest.raises(ProviderAccountError):
        phases.baseline(act, out=quiet)
    assert sum(m.rate_limited for m in meters) == 1
    statuses = [r["status"] for r in reservations(act)]
    assert statuses.count("released") == 2 and "unknown" not in statuses       # the 429 and the refusal
    assert statuses.count("settled") == actor.calls
    rows = list(act.results.all())
    assert rows and all(r.validity == "valid" for r in rows)                    # what ran before is kept
    assert not [r for r in rows if "usage limit" in r.invalid_reason]


def test_a_metered_call_is_retried_only_when_worth_it(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    account = Budgets(tmp_path).account("a", {"usd": 1.0})
    pricing = Pricing(**{k: v for k, v in rsi.PRICING.items() if k != "source"})
    actor = RateLimitedThenRefused(refuse_at=3)
    meter = MeteredProvider(actor, account, pricing, operation="actor")
    turn = meter.chat("", [{"role": "user", "content": "hello"}], [])
    assert turn.usage and meter.rate_limited == 1 and actor.sent == 2
    with pytest.raises(ProviderAccountError):
        meter.chat("", [{"role": "user", "content": "hello"}], [])
    assert actor.sent == 3 and meter.rate_limited == 1                     # an account error is never sent again
    book_ = json.loads((tmp_path / "rsi" / "budget" / "book.json").read_text(encoding="utf-8"))
    assert [r["status"] for r in book_["reservations"].values()] == ["released", "settled", "released"]


# ---------------------------------------------------------------------------- the command line
@pytest.mark.parametrize("phase", ["baseline", "library", "round", "evaluate", "cycle"])
def test_checks_only_on_a_paid_phase_is_refused_before_anything_runs(phase, tmp_path, monkeypatch, capsys):
    """`--checks-only` is the smoke's; a paid phase would ignore it and run for real, so it is refused before the
    config is even read: nothing is opened, nothing paid."""
    from ouroloop.activity import workspace
    opened = []
    monkeypatch.setattr(workspace, "open_activity", lambda *a, **k: opened.append(1))
    code = cli.main(["bench", "--config", str(tmp_path / "missing.toml"), "--phase", phase, "--checks-only"])
    assert code == 2 and not opened
    assert "--checks-only is a smoke option" in capsys.readouterr().err
