"""What has to hold before a paid run (the first group after M0-M7): every model call on the book, a cost per
attempt, a restart that pays only for what is missing, and plans frozen from the command line."""
import json

import pytest
import rsi

from ouroloop import bench, cli
from ouroloop.benchmark import Results
from ouroloop.evolve import contracts as ct
from ouroloop.evolve.execution import Budgets, Pricing, normalize_usage, unfinished

PRICING = {"input_per_1k": 0.003, "output_per_1k": 0.015, "max_input_tokens": 8000, "max_output_tokens": 2000,
           "source": "fixture prices"}


class Crash(BaseException):
    """A process dying mid-run: not an exception any handler in the loop is allowed to swallow."""


class CrashingWorker(rsi.BusinessWorker):
    """The scripted worker, until its n-th call."""

    def __init__(self, crash_at: int, **kw):
        super().__init__(**kw)
        self.crash_at = crash_at

    def chat(self, system, messages, specs):
        if self.calls + 1 >= self.crash_at:
            self.calls += 1
            raise Crash(f"the process died at call {self.calls}")
        return super().chat(system, messages, specs)


def setup(fake_upstream, tmp_path, **kw):
    from test_rsi_integration import prepared
    return prepared(fake_upstream, tmp_path, out=lambda _l: None, **kw)


def book(activity) -> dict:
    return json.loads((activity.root / "rsi" / "budget" / "book.json").read_text())


# ---------------------------------------------------------------------------- metering
def test_every_agent_call_is_reserved_and_settled_on_the_activity_book(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    cfg["pricing"] = dict(PRICING)
    bench.improve(cfg, ".", out=lambda _l: None, researcher=rsi.ScriptedResearcher())
    reservations = list(book(activity)["reservations"].values())
    assert reservations and all(r["status"] == "settled" for r in reservations)
    operations = {r["operation_id"].split(":")[1] for r in reservations}     # <scope>:<arm>:<run>#<call>
    assert {"collect", "dev_baseline", "subject", "control"} <= operations
    # the book's total is the sum of what the agent's calls measured, in dollars as well as calls
    used = activity.budgets.account("it", book(activity)["scopes"]["it"]["limits"]).remaining()["used"]
    assert used["model_calls"] == len(reservations) and used["usd"] > 0


def test_one_activity_has_one_budget_root(fake_upstream, tmp_path):
    from test_rsi_integration import campaign_pairs
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    w0 = ct.SystemRelease.from_json(activity.objects.get(release)).worker_ref
    bench.research_slot(cfg, ".", pairs=campaign_pairs(activity, initial_w=w0),
                        researcher=rsi.ResearchPolicyProposer(),
                        arm_researcher=lambda: rsi.ScriptedResearcher(follow_prompt=True), out=lambda _l: None,
                        worker_rounds=1)
    scopes = book(activity)["scopes"]
    roots = [name for name, entry in scopes.items() if entry["parent"] is None]
    # the campaign, its arms and its outer runs all hang under the activity's root: nothing got a second copy
    assert roots == ["it"]
    assert any(name.startswith("it:campaign:") for name in scopes)
    assert all(scopes[name]["parent"] for name in scopes if name != "it")


def test_the_research_step_is_metered_too(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    account = bench.activity_budget(activity, cfg, "improve")
    researcher = bench._default_researcher(cfg, activity, budget=account)
    context = type("C", (), {"prompt_state": lambda self, n=0: "{}", "evidence": ()})()
    researcher.diagnose(context, "diagnose prompt")
    ops = [r["operation_id"] for r in book(activity)["reservations"].values()]
    # booked under the research operation and labelled with its step, so a report can say what each step cost
    assert any(op.endswith(":research:diagnose#0.0") for op in ops)


def test_a_candidate_the_budget_cannot_verify_is_not_started(fake_upstream, tmp_path):
    """After the collection (about 14 calls) four are left, and a full comparison needs at least six: nothing is
    built, and the round says why (EX-03)."""
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    cfg["rsi"]["budget_ref"] = activity.objects.put_json("budget", {"model_calls": 18.0})
    result = bench.improve(cfg, ".", out=lambda _l: None, researcher=rsi.ScriptedResearcher())
    assert result["rounds"][0]["outcome"] == "inconclusive"
    assert not [r for r in Results(activity.root, rsi.DOMAIN).all() if r.pair_key]      # no evaluation ran
    assert sum(1 for r in book(activity)["reservations"].values() if r["status"] == "settled") <= 18


def test_a_budget_that_runs_out_mid_comparison_ends_it_as_budget_exhausted(fake_upstream, tmp_path):
    """Enough to start the comparison (the estimate is one call an attempt), not to finish it (they take two or
    three): the attempts that could not run are budget_exhausted, never refilled, and the verdict is inconclusive."""
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    cfg["rsi"]["budget_ref"] = activity.objects.put_json("budget", {"model_calls": 22.0})
    result = bench.improve(cfg, ".", out=lambda _l: None, researcher=rsi.ScriptedResearcher())
    rows = list(Results(activity.root, rsi.DOMAIN).all())
    exhausted = [r for r in rows if r.failure_class == "budget_exhausted"]
    assert exhausted and all(r.validity == "invalid" and not r.supersedes for r in exhausted)
    receipts = [activity.objects.load(e["receipt"], ct.EvaluationReceipt)
                for e in ct.Events(activity.root).of("evaluation_completed")]
    assert receipts and receipts[-1].verdict == "inconclusive"
    assert "budget_exhausted" in receipts[-1].reasons[0]
    assert "installed" not in [r["outcome"] for r in result["rounds"]]
    assert sum(1 for r in book(activity)["reservations"].values() if r["status"] == "settled") <= 22


# ---------------------------------------------------------------------------- cost
def test_an_attempt_carries_its_cost_when_prices_are_declared(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    work = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker(),
                             pricing=bench.pricing_of({"pricing": PRICING}))
    rows = work.run_arm("priced", release, [("rsi.dev_update_stage", 0)], plan_ref="", split="dev")
    # the scripted worker reports 100 input and 20 output tokens a call
    calls = rows[0].usage.get("input_tokens", 0) // 100
    assert rows[0].cost_usd == pytest.approx(calls * (0.1 * 0.003 + 0.02 * 0.015))
    unpriced = bench.improvement(activity, template_ref=plan_ref, provider=rsi.BusinessWorker())
    assert unpriced.run_arm("unpriced", release, [("rsi.dev_update_stage", 0)], plan_ref="",
                            split="dev")[0].cost_usd is None


def test_usage_vocabularies_are_normalized_and_an_unpriced_cache_is_unknown():
    assert normalize_usage({"prompt_tokens": 1000, "completion_tokens": 50,
                            "prompt_tokens_details": {"cached_tokens": 800}}) == {
        "input_tokens": 200, "output_tokens": 50, "cache_read_tokens": 800, "cache_write_tokens": 0}
    assert normalize_usage({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90}) == {
        "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 90, "cache_write_tokens": 0}
    pricing = Pricing(0.003, 0.015)
    assert pricing.cost(normalize_usage({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90})) \
        is None
    cached = Pricing(0.003, 0.015, cache_read_per_1k=0.0003)
    assert cached.cost(normalize_usage({"input_tokens": 1000, "output_tokens": 0, "cache_read_input_tokens": 1000})) \
        == pytest.approx(0.0033)


def test_a_bound_is_only_breached_on_a_dimension_it_bounded(tmp_path):
    account = Budgets(tmp_path).account("a", {"model_calls": 10})
    reservation = account.reserve("call", {"model_calls": 1})
    receipt = account.settle(reservation.id, {"model_calls": 1, "input_tokens": 5000, "output_tokens": 900})
    assert receipt.breach == {} and not account.remaining()["stopped"]


# ---------------------------------------------------------------------------- resume
def final_config(cfg, activity, release, *, stop_rules=None):
    cfg["freeze"] = {"final": {"experiment_id": "final-resume", "claims": ["C1"],
                               "comparisons": [{"claim": "C1", "subject": "evolved", "control": "base",
                                                "role": "evolved"},
                                               {"claim": "C1", "subject": "base", "control": "base",
                                                "role": "base"}],
                               "release_refs": {"evolved": release, "base": release}, "tasks": "test",
                               "repeats": 2, "stop_rules": stop_rules or {}}}
    refs = bench.freeze(cfg, ".", out=lambda _l: None)
    cfg["rsi"]["final_plan_ref"] = refs["final_plan_ref"]
    return refs


def test_br05_a_final_run_resumed_after_a_crash_runs_only_what_is_missing(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    final_config(cfg, activity, release)
    # each arm gets its own provider and makes about eight calls: this one dies in the first arm
    cfg["llm"] = {"type": "test_paid_run_safety:CrashingWorker", "crash_at": 5}
    with pytest.raises(Crash):
        bench.evaluate(cfg, ".", out=lambda _l: None)
    before = [r for r in Results(activity.root, rsi.DOMAIN).all() if r.split == "test"]
    assert 0 < len(before) < 8                          # 2 arms x 2 tasks x 2 repeats: it died part way
    counter = rsi.BusinessWorker()
    cfg["llm"] = {"type": "rsi:BusinessWorker"}
    lines = []
    result = bench.evaluate(cfg, ".", out=lines.append)
    after = [r for r in Results(activity.root, rsi.DOMAIN).all() if r.split == "test"]
    ids = [r.attempt_id for r in after]
    assert len(ids) == len(set(ids)) == 8               # nothing was run twice
    assert {r.attempt_id for r in before} <= set(ids)   # and nothing that was on record was replaced
    assert any("kept, not run again" in line for line in lines)
    assert all(arm["summary"]["attempts"] == 4 for arm in result["arms"].values())
    del counter


def test_the_final_spends_the_share_set_aside_for_it(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    cfg["rsi"]["budget_ref"] = activity.objects.put_json(
        "budget", {"model_calls": 1000.0, "allocations": {"final": {"model_calls": 200.0}}})
    final_config(cfg, activity, release)
    bench.evaluate(cfg, ".", out=lambda _l: None)
    spent = [r for r in book(activity)["reservations"].values() if r["scope_id"].endswith(":final:final-resume")]
    assert spent and all(r["allocation"] == "final" for r in spent)


def test_a_completed_final_plan_run_again_pays_for_nothing(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    final_config(cfg, activity, release)
    bench.evaluate(cfg, ".", out=lambda _l: None)
    first = len(list(Results(activity.root, rsi.DOMAIN).all()))
    settled = sum(1 for r in book(activity)["reservations"].values() if r["status"] == "settled")
    bench.evaluate(cfg, ".", out=lambda _l: None)
    assert len(list(Results(activity.root, rsi.DOMAIN).all())) == first
    assert sum(1 for r in book(activity)["reservations"].values() if r["status"] == "settled") == settled


def test_a_refill_is_written_with_what_it_supersedes(fake_upstream, tmp_path, monkeypatch):
    from ouroloop.evolve import task_eval
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    final_config(cfg, activity, release, stop_rules={"max_infrastructure_retries": 1})
    original = task_eval.run_attempt
    failed = {"done": False}

    def flaky(*args, **kw):
        if not failed["done"] and kw.get("retry_index", 0) == 0:
            failed["done"] = True
            raise_on = task_eval.TaskResult.broken(args[1], "f", "test", kw.get("attempt", 0), kw["run"],
                                                   kw["splits"].hash(), "provider unavailable",
                                                   failure_class="infrastructure",
                                                   attempt_id=task_eval.bench_attempt_id(
                                                       kw["run"].hash(), args[1], kw.get("attempt", 0),
                                                       kw.get("arm", "subject"), 0),
                                                   pair_key=task_eval.bench_pair_key(kw.get("plan_ref", ""),
                                                                                     args[1], kw.get("attempt", 0)))
            return task_eval.Attempt(raise_on, None)
        return original(*args, **kw)

    monkeypatch.setattr(task_eval, "run_attempt", flaky)
    bench.evaluate(cfg, ".", out=lambda _l: None)
    recorded = [r for r in Results(activity.root, rsi.DOMAIN).all() if r.split == "test"]
    refills = [r for r in recorded if r.supersedes]
    assert len(refills) == 1 and refills[0].validity == "valid"
    assert refills[0].supersedes in {r.attempt_id for r in recorded if r.failure_class == "infrastructure"}


def test_an_interrupted_improvement_resumes_its_experiment_instead_of_starting_over(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    # collect (5 train + 3 dev attempts, about 14 calls) runs, the proposal is frozen, and the process dies a few
    # calls into the evaluation
    cfg["llm"] = {"type": "test_paid_run_safety:CrashingWorker", "crash_at": 18}
    with pytest.raises(Crash):
        bench.improve(cfg, ".", out=lambda _l: None, researcher=rsi.ScriptedResearcher())
    pending = unfinished(activity.root)
    assert len(pending) == 1 and pending[0]["status"] == "evaluating"
    interrupted = pending[0]["experiment_id"]
    evaluated_before = [r for r in Results(activity.root, rsi.DOMAIN).all() if r.pair_key]
    assert evaluated_before
    cfg["llm"] = {"type": "rsi:BusinessWorker"}
    lines = []
    result = bench.improve(cfg, ".", out=lines.append, researcher=rsi.ScriptedResearcher())
    first = result["rounds"][0]
    assert first["experiment_id"] == interrupted and first["outcome"] == "installed"
    assert "resumed from evaluating" in first["reason"] and any("resume" in line for line in lines)
    # the evaluation attempts made before the crash were kept; each planned attempt exists exactly once
    evaluated = [r for r in Results(activity.root, rsi.DOMAIN).all() if r.pair_key]
    keys = [(r.pair_key, r.arm_key) for r in evaluated] if hasattr(evaluated[0], "arm_key") else \
        [r.attempt_id for r in evaluated]
    assert len(keys) == len(set(keys))
    assert {r.attempt_id for r in evaluated_before} <= {r.attempt_id for r in evaluated}
    assert not unfinished(activity.root)
    installs = [e for e in ct.Events(activity.root).of("installed") if e.get("operation")]
    assert len(installs) == 1


def test_an_experiment_with_no_recorded_ref_is_closed_not_guessed(fake_upstream, tmp_path):
    from ouroloop.evolve.execution import Execution
    from ouroloop.research.loop import resume_unfinished
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    Execution(activity.root, "x-orphan").advance("admitted")
    results = resume_unfinished(activity.root, activity.objects, {}, None)
    assert [r.outcome for r in results] == ["cancelled"]
    assert Execution(activity.root, "x-orphan").state()["status"] == "cancelled"


# ---------------------------------------------------------------------------- freezing plans
def test_freeze_writes_the_plans_and_prints_the_rsi_lines(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    cfg["freeze"] = {"task_plan": {"tasks": "dev", "repeats": 2, "order_seed": 3,
                                   "gate": {"min_gain": 0.1, "min_groups": 2, "bootstrap_replicates": 300}},
                     "schedule": {"worker_rounds": 2, "patience": 1},
                     "budget": {"model_calls": 500, "usd": 20}}
    lines = []
    refs = bench.freeze(cfg, ".", out=lines.append)
    assert set(refs) == {"task_plan_ref", "schedule_ref", "budget_ref"}
    plan = activity.objects.get(refs["task_plan_ref"])
    assert plan["task_ids"] == list(rsi.DEV) and plan["repeats"] == 2 and plan["gate_plan"]["min_gain"] == 0.1
    assert activity.objects.get(refs["budget_ref"]) == {"model_calls": 500.0, "usd": 20.0}
    assert "[rsi]" in lines and f'task_plan_ref = "{refs["task_plan_ref"]}"' in lines
    assert bench.freeze(cfg, ".", out=lambda _l: None) == refs          # the same tables freeze to the same refs


def test_freeze_refuses_what_it_cannot_freeze_faithfully(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    for tables, match in (({"task_plan": {"tasks": ["no.such.task"]}}, "does not have"),
                          ({"task_plan": {"threshold": 0.1}}, "does not take"),
                          # an unknown per-attempt limit is refused by name, not as Budget's TypeError
                          ({"task_plan": {"per_attempt_budget": {"max_turns": 3}}},
                           r"freeze refused: \[freeze.task_plan\] per_attempt_budget does not take \['max_turns'\]; "
                           r"it takes \['max_model_calls', 'max_seconds'"),
                          ({"budget": {"dollars": 5}}, "does not take"),
                          ({"plans": {}}, "has no tables"),
                          ({"task_plan": {"gate": {"min_gain": 0.0}}}, "above zero")):
        cfg["freeze"] = tables
        with pytest.raises((ValueError, ct.ContractError), match=match):
            bench.freeze(cfg, ".", out=lambda _l: None)
    cfg["freeze"] = {"schedule": {"rounds": 3}}
    with pytest.raises(TypeError):
        bench.freeze(cfg, ".", out=lambda _l: None)


def test_a_sealed_final_plan_cannot_be_frozen_again_differently(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    final_config(cfg, activity, release)
    cfg["freeze"]["final"]["repeats"] = 3
    with pytest.raises(ValueError, match="new activity"):
        bench.freeze(cfg, ".", out=lambda _l: None)


def test_the_freeze_command_exits_two_on_a_bad_table(fake_upstream, tmp_path, capsys):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    path = tmp_path / "freeze.toml"
    path.write_text(f'[workspace]\nroot = "{activity.root}"\n\n[bench]\ndomain = "rsi"\n'
                    f'upstream_path = "{fake_upstream}"\n\n[freeze.budget]\ndollars = 5\n', encoding="utf-8")
    assert cli.main(["freeze", "--config", str(path)]) == 2
    assert "does not take" in capsys.readouterr().err


def test_the_pricing_table_takes_the_cache_rates():
    pricing = bench.pricing_of({"pricing": {**PRICING, "cache_read_per_1k": 0.0003, "cache_write_per_1k": 0.00375}})
    assert pricing.cache_read_per_1k == 0.0003 and pricing.cache_write_per_1k == 0.00375


class CachedWorker(rsi.BusinessWorker):
    """The scripted worker, reporting its usage the way an OpenAI-compatible API does, with a cache hit."""

    def chat(self, system, messages, specs):
        turn = super().chat(system, messages, specs)
        turn.usage = {"prompt_tokens": 1000, "completion_tokens": 50,
                      "prompt_tokens_details": {"cached_tokens": 800}}
        return turn


def test_an_attempt_keeps_its_cache_counts_so_its_cost_can_be_recomputed(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    work = bench.improvement(activity, template_ref=plan_ref, provider=CachedWorker(),
                             pricing=bench.pricing_of({"pricing": {**PRICING, "cache_read_per_1k": 0.0003}}))
    row = work.run_arm("cached", release, [("rsi.dev_update_stage", 0)], plan_ref="", split="dev")[0]
    calls = row.usage["prompt_tokens"] // 1000
    assert row.usage["cache_read_tokens"] == 800 * calls
    # recomputed from the record alone, the cost is the one the attempt was written with
    recomputed = Pricing(0.003, 0.015, cache_read_per_1k=0.0003).cost(
        {"input_tokens": row.usage["prompt_tokens"] - row.usage["cache_read_tokens"],
         "output_tokens": row.usage["completion_tokens"], "cache_read_tokens": row.usage["cache_read_tokens"],
         "cache_write_tokens": 0})
    assert row.cost_usd == pytest.approx(recomputed)


# ---------------------------------------------------------------------------- v0.3 (M3 §2.1(f), M5 §2): gate size, speed
def test_a_gate_plan_under_min_tasks_is_refused_at_freeze(fake_upstream, tmp_path):
    """M3 §2.1(f) (v0.3), ER6-22: a gate plan over fewer tasks than its `min_tasks` is refused at freeze — the
    mainline's through `ouroloop freeze`, a slot's inner gate through the same `freeze_task_plan` — while a gate
    without the field freezes as it always did, to the same ref, with none of the new keys in its payload."""
    import rsi
    from ouroloop.evolve.task_gate import GatePlan
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    cfg["freeze"] = {"task_plan": {"tasks": "dev", "gate": {"min_gain": 0.05, "min_groups": 1, "min_tasks": 25}}}
    with pytest.raises(ValueError, match=r"runs 3 tasks, fewer than its min_tasks 25"):
        bench.freeze(cfg, ".", out=lambda _l: None)
    cfg["freeze"]["task_plan"]["gate"]["min_tasks"] = 3
    frozen = activity.objects.get(bench.freeze(cfg, ".", out=lambda _l: None)["task_plan_ref"])
    assert frozen["gate_plan"]["min_tasks"] == 3 and "control_pooling" not in frozen
    core = ct.SystemRelease.from_json(activity.objects.get(release)).core_ref
    with pytest.raises(ValueError, match="fewer than its min_tasks 3"):
        bench.freeze_task_plan(activity.objects, core_ref=core, split_ref=activity.splits.hash(),
                               task_ids=tuple(rsi.DEV[:2]), gate=GatePlan(min_groups=1, min_tasks=3))
    # the plan `prepared` froze carries none of the v0.3 fields, so freezing the same tables again finds it
    again = bench.freeze_task_plan(activity.objects, core_ref=core, split_ref=activity.splits.hash(),
                                   task_ids=tuple(rsi.DEV),
                                   gate=GatePlan(min_gain=0.05, min_groups=1, bootstrap_replicates=200))
    assert again == plan_ref
    payload = activity.objects.get(plan_ref)
    assert not {"control_pooling", "confirmation_repeats"} & set(payload)
    assert not {"regression_drop", "cost_beta0", "cost_beta1", "min_tasks"} & set(payload["gate_plan"])
    with pytest.raises(ct.ContractError, match="min_tasks"):
        GatePlan(min_tasks=0)


def test_concurrency_is_a_machine_setting_the_frozen_plan_does_not_carry(fake_upstream, tmp_path, monkeypatch):
    """M5 §2 (v0.3), BR6-24: [bench] concurrency changes no frozen ref; it defaults to 8 under the v3 preset and 1
    otherwise, the config's own value stands, and `ouroloop bench --concurrency N` overrides it for one run without
    touching the config file."""
    import rsi
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    cfg["freeze"] = {"task_plan": {"tasks": "dev", "gate": {"min_gain": 0.05, "min_groups": 1}},
                     "budget": {"model_calls": 500}}
    refs = {n: bench.freeze({**cfg, "bench": {**cfg["bench"], "concurrency": n}}, ".", out=lambda _l: None)
            for n in (1, 8)}
    assert refs[1] == refs[8]
    assert bench.BenchConfig.load(cfg).concurrency == 1
    v3 = {"bench": {"domain": "marketing"}, "improvement": {"preset": "automationbench_harness_v3"}}
    assert bench.preset_concurrency(v3) == 8 and bench.BenchConfig.load(v3).concurrency == 8
    assert bench.BenchConfig.load({**v3, "bench": {"domain": "marketing", "concurrency": 2}}).concurrency == 2
    assert bench.preset_concurrency({"improvement": {"preset": "marketing_harness_v1"}}) == 1
    seen = {}
    monkeypatch.setattr(bench, "run_phase", lambda cfg, base, **kw: seen.update(cfg=cfg) or {"ok": True})
    path = tmp_path / "bench.toml"
    path.write_text(f'[workspace]\nroot = "{activity.root}"\n\n[bench]\ndomain = "{rsi.DOMAIN}"\n'
                    f'upstream_path = "{fake_upstream}"\n', encoding="utf-8")
    assert cli.main(["bench", "--config", str(path), "--phase", "report", "--concurrency", "3"]) == 0
    assert seen["cfg"]["bench"]["concurrency"] == 3
    assert cli.main(["bench", "--config", str(path), "--phase", "report"]) == 0
    assert "concurrency" not in seen["cfg"]["bench"]
    assert "concurrency" not in path.read_text().split("[bench]", 1)[1]      # the file itself is untouched


class InFlight(rsi.BusinessWorker):
    """The scripted worker, counting how many attempts have a model call in flight at once across every caller."""
    lock = __import__("threading").Lock()
    running = 0
    peak = 0

    def chat(self, system, messages, specs):
        import time
        with InFlight.lock:
            InFlight.running += 1
            InFlight.peak = max(InFlight.peak, InFlight.running)
        try:
            time.sleep(0.03)
            return super().chat(system, messages, specs)
        finally:
            with InFlight.lock:
                InFlight.running -= 1


def test_the_in_flight_cap_is_shared_by_the_arms_and_screens_of_one_gate(fake_upstream, tmp_path):
    """M5 §2 (v0.3) 并发的范围: the attempts in flight are capped per process, by one semaphore every `run_tasks`
    of the process draws on — a candidate arm and its control top-up, k screens — so two callers running at once,
    each asking for 2, cannot between them have 4 attempts in flight: the worker sees at most 2 model calls at a
    time. A new size replaces the pool for the attempts that start after it."""
    import threading
    from ouroloop import policies as P
    from ouroloop.evolve import task_eval
    from ouroloop.evolve.task_eval import make_agent_factory, run_tasks
    InFlight.running, InFlight.peak = 0, 0
    suite = rsi.suite(fake_upstream)
    splits = rsi.splits(suite)
    failures: list = []

    def caller(name: str) -> None:
        try:
            root = tmp_path / name
            releases, rel, objects = rsi.workspace(root, split_ref=splits.hash())
            bound = P.bind_release(rel, profile="task", services=P.Services(objects), authorization="bootstrap")
            manifest = rsi.run_manifest(name, "base", "dev", data=splits.hash(), system_release_ref=rel)
            factory = make_agent_factory(rsi.runtime(root), InFlight(), budget=manifest.budget, bound=bound,
                                         objects=objects)
            rows = run_tasks(suite, list(rsi.DEV), factory, run=manifest, splits=splits,
                             results=Results(root, rsi.DOMAIN), release_ref=rel, arm=name, concurrency=2)
            assert len(rows) == len(rsi.DEV) and all(r.validity == "valid" for r in rows)
        except BaseException as e:  # noqa: BLE001 - reported on the main thread
            failures.append(e)
    threads = [threading.Thread(target=caller, args=(arm,)) for arm in ("subject", "control")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not failures, failures
    assert InFlight.peak == 2                       # two callers × concurrency 2, never 4 in flight
    cap = task_eval.in_flight_cap(2)
    assert task_eval.in_flight_cap(2) is cap
    assert task_eval.in_flight_cap(3) is not cap and task_eval.in_flight_cap(3) is task_eval.in_flight_cap(3)
    task_eval.in_flight_cap(1)                       # back to the serial default for the other tests


class RateLimitedResearch(rsi.BusinessWorker):
    """The research model: its first call is turned away with a 429 (unbilled, retryable), then it answers; it asks
    for one retry of its own, which the meter takes over."""
    retries = 1
    refused = 0

    def chat(self, system, messages, specs):
        from ouroloop.providers import ProviderError
        if type(self).refused == 0:
            type(self).refused += 1
            raise ProviderError("API Error: 429 rate_limit_error", retryable=True, unbilled=True)
        return super().chat(system, messages, specs)


class AccountAfterLimit(rsi.BusinessWorker):
    """A 429 on the first call, then the account is refused: the run ends on the account error."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.seen = 0

    def chat(self, system, messages, specs):
        from ouroloop.providers import ProviderAccountError, ProviderError
        self.seen += 1
        if self.seen == 1:
            raise ProviderError("API Error: 429 rate_limit_error", retryable=True, unbilled=True)
        raise ProviderAccountError("You've hit your usage limit")


def test_freeze_refuses_two_confirmations_an_economy_confirmation_and_a_vacuous_regression_drop(fake_upstream,
                                                                                                tmp_path):
    """M3 §2.1(b)(d) (v0.3, after review), through `ouroloop freeze`: a [freeze.task_plan] table asking for two
    confirmation readings, a confirmation under an economy gate, or a regression_drop of 1.0 is refused before
    anything is frozen."""
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    gate = {"min_gain": 0.05, "min_groups": 1}
    for table, match in (({"tasks": "dev", "confirmation_repeats": 2, "gate": gate}, "0 or 1"),
                         ({"tasks": "dev", "confirmation_repeats": 1,
                           "gate": {"mode": "economy", "max_runtime_cost_ratio": 0.9, "min_groups": 1}},
                          "economy gate"),
                         ({"tasks": "dev", "gate": {**gate, "regression_drop": 1.0}}, r"strictly inside \(0, 1\)")):
        cfg["freeze"] = {"task_plan": table}
        with pytest.raises((ValueError, ct.ContractError), match=match):
            bench.freeze(cfg, ".", out=lambda _l: None)
    cfg["freeze"] = {"task_plan": {"tasks": "dev", "confirmation_repeats": 1, "control_pooling": "pooled",
                                   "gate": {**gate, "regression_drop": 0.5}}}
    frozen = activity.objects.get(bench.freeze(cfg, ".", out=lambda _l: None)["task_plan_ref"])
    assert frozen["confirmation_repeats"] == 1 and frozen["gate_plan"]["regression_drop"] == 0.5


def test_the_research_meters_rate_limit_refusals_are_recorded_per_round(fake_upstream, tmp_path, monkeypatch):
    """M5 §2 (v0.3, after review): the research model's 429s are absorbed by its meter like an arm's — and recorded:
    after the round, a `rate_limited` event with arm `research`, the round's index and that round's count; a round
    with none writes nothing."""
    import time
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    RateLimitedResearch.refused = 0
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    cfg["research"] = {"llm": {"type": "test_paid_run_safety:RateLimitedResearch"}}
    bench.improve(cfg, ".", out=lambda _l: None)
    events = list(ct.Events(activity.root).of("rate_limited"))
    research = [e for e in events if e["arm"] == "research"]
    assert [(e["round"], e["count"], e["scope"]) for e in research] == [(0, 1, "it")]
    assert RateLimitedResearch.refused == 1
    # the refusal was released on the book, not booked
    released = [r for r in book(activity)["reservations"].values() if r["status"] == "released"]
    assert len(released) == 1 and ":research" in released[0]["operation_id"]


def test_an_arms_rate_limit_count_survives_an_account_error(fake_upstream, tmp_path):
    """M5 §2 (v0.3, after review): the arm's `rate_limited` event is written in a `finally`, so a 429 absorbed before
    an account error ended the arm is still on record."""
    from ouroloop.providers import ProviderAccountError
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    work = bench.improvement(activity, template_ref=plan_ref, provider=AccountAfterLimit(),
                             budget=bench.activity_budget(activity, cfg, "probe"))
    with pytest.raises(ProviderAccountError):
        work.run_arm("probe", release, [(t, 0) for t in rsi.DEV], plan_ref="", split="dev")
    events = [e for e in ct.Events(activity.root).of("rate_limited") if e["arm"] == "probe"]
    assert len(events) == 1 and events[0]["count"] == 1 and events[0]["scope"] == "it"
