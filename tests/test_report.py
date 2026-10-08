"""M7 acceptance (docs/modules/07_BENCH_REPORT.md §5): the frontier configuration, the sealed final plan, and a
report that is a projection rather than a choice."""
import json

import pytest
import rsi

from ouroloop import bench
from ouroloop.benchmark import Results, RunManifest, SplitManifest, TaskResult, summarise
from ouroloop.evolve import contracts as ct
from ouroloop.evolve.execution import Pricing, check_pricing
from ouroloop.report import build_report, claim_states, render_markdown, write_report


def setup(fake_upstream, tmp_path, **kw):
    from test_rsi_integration import prepared
    return prepared(fake_upstream, tmp_path, out=lambda _l: None, **kw)


def record(results: Results, *, candidate, role, split="dev", tasks=None, cost=None, success=True,
           partial=1.0, v2=True):
    run = rsi.run_manifest(candidate, role, split, **({"system_release_ref": "release:sha256:" + "a" * 64,
                                                      "worker_ref": "policyset:sha256:" + "b" * 64,
                                                      "research_ref": "policyset:sha256:" + "c" * 64,
                                                      "core_ref": "core:sha256:" + "d" * 64} if v2 else {}))
    results.put_run(run)
    for task in tasks or ["rsi.dev_update_stage", "rsi.dev_notify_after_update"]:
        results.append(TaskResult(task, f"family_{task}", split, 0, candidate, run.hash(), "man",
                                  success=success, partial_credit=partial, cost_usd=cost))
    return run


# ---------------------------------------------------------------------------- BR-02 / BR-03
def test_br03_a_frontier_table_without_a_model_or_with_extra_keys_is_refused():
    with pytest.raises(ValueError, match="needs an"):
        bench.frontier_configs({"frontier": {"a": {"effort": "max"}}})
    with pytest.raises(ValueError, match="does not take"):
        bench.frontier_configs({"frontier": {"a": {"llm": {"type": "mock"}, "temperature": 1}}})
    parsed = bench.frontier_configs({"frontier": {"a": {"llm": {"type": "mock"}, "source": "official page",
                                                       "date": "2026-09-27", "fallback": "declared"}}})
    assert parsed["a"]["source"] == "official page"


def test_br03_one_vendor_is_an_incomplete_c2_and_no_weaker_model_is_substituted(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    cfg["frontier"] = {"a": {"llm": {"type": "rsi:BusinessWorker"}, "source": "fixture", "date": "2026-09-27"}}
    lines = []
    report = bench.baseline(cfg, ".", out=lines.append, tasks=["rsi.dev_update_stage"])
    assert report["c2_complete"] is False
    assert any("fewer than two vendors" in line for line in lines)
    assert set(report["arms"]) == {"base", "a"}
    built = build_report(activity.root, rsi.DOMAIN)
    assert built["claims"]["C2"]["state"] in ("inconclusive", "not_run")
    assert any("frontier" in line for line in built["identity"]["missing"])


def test_a_model_that_cannot_be_built_is_recorded_as_absent(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    cfg["frontier"] = {"a": {"llm": {"type": "rsi:BusinessWorker"}},
                       "b": {"llm": {"type": "rsi:NoSuchProvider"}}}
    report = bench.baseline(cfg, ".", out=lambda _l: None, tasks=["rsi.dev_update_stage"])
    assert report["absent"] and report["absent"][0]["arm"] == "b"
    assert "b" not in report["arms"]


def test_br02_identical_configurations_share_one_run_identity(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    results = Results(activity.root, rsi.DOMAIN)
    base = record(results, candidate="base", role="base")
    same = record(results, candidate="base", role="base")
    other = record(results, candidate="prompt_best", role="prompt_best")
    assert base.hash() == same.hash() and other.hash() != base.hash()
    built = build_report(activity.root, rsi.DOMAIN)
    assert built["task_effect"]["arms"][base.hash()]["summary"]["attempts"] == 4    # one identity, both runs
    assert other.hash() in built["task_effect"]["arms"]


# ---------------------------------------------------------------------------- BR-04
def test_br04_unknown_cost_stays_unknown_and_partial_credit_is_not_a_pass_rate(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    results = Results(activity.root, rsi.DOMAIN)
    run = record(results, candidate="evolved", role="evolved", success=False, partial=0.9, cost=None)
    built = build_report(activity.root, rsi.DOMAIN)
    arm = built["task_effect"]["arms"][run.hash()]
    assert arm["summary"]["pass_rate"] == 0.0 and arm["summary"]["partial_credit"] == 0.9
    assert built["run_cost"][run.hash()]["cost_usd"] is None
    assert built["run_cost"][run.hash()]["cost_missing"] == 2
    text = render_markdown(built)
    assert "unknown" in text


def test_a_run_from_before_the_v2_manifest_is_named_as_such(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    results = Results(activity.root, rsi.DOMAIN)
    record(results, candidate="old", role="base", v2=False)
    built = build_report(activity.root, rsi.DOMAIN)
    assert any("predate the v2 manifest" in line for line in built["identity"]["missing"])
    assert not list(built["task_effect"]["arms"].values())[0]["v2"]


# ---------------------------------------------------------------------------- BR-05 / BR-06
def test_br05_a_sealed_plan_resumes_and_br06_cannot_be_changed_afterwards(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    plan = bench.FinalizationPlan(
        experiment_id="final-1", core_ref=activity.objects.load(release, ct.SystemRelease).core_ref,
        split_ref=activity.splits.hash(), claims=("C1",),
        comparisons=({"claim": "C1", "subject": "evolved", "control": "base", "role": "evolved"},
                     {"claim": "C1", "subject": "base", "control": "base", "role": "base"}),
        release_refs={"evolved": release, "base": release}, task_ids=tuple(rsi.FINAL), repeats=1,
        statistical_plan={"paired": True, "confidence": 0.95})
    sealed = bench.seal(plan, activity.root, activity.objects)
    cfg["rsi"]["final_plan_ref"] = sealed["plan"]
    out = bench.evaluate(cfg, ".", out=lambda _l: None)
    assert set(out["arms"]) == {"evolved", "base"}
    state = json.loads(sealed["path"].read_text())
    assert state["status"] == "completed" and len(state["attempts"]) == 4
    # a changed final plan is a new activity, not a reseal
    changed = bench.FinalizationPlan(**{**{k: getattr(plan, k) for k in plan.__dataclass_fields__},
                                        "task_ids": (rsi.FINAL[0],)})
    with pytest.raises(ValueError, match="new activity"):
        bench.seal(changed, activity.root, activity.objects)
    # rerunning the same plan does not change the versions it compares
    again = bench.evaluate(cfg, ".", out=lambda _l: None)
    assert set(again["arms"]) == {"evolved", "base"}


def test_a_final_plan_needs_its_tasks_and_a_c1_comparison():
    with pytest.raises(ct.ContractError, match="names the final tasks"):
        bench.FinalizationPlan(experiment_id="x", claims=("C1",))
    with pytest.raises(ct.ContractError, match="C1 needs"):
        bench.FinalizationPlan(experiment_id="x", claims=("C1",), task_ids=("t1",), comparisons=())


# ---------------------------------------------------------------------------- BR-07 / BR-08
def test_br07_the_same_directory_reports_the_same_numbers_without_a_model(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    bench.improve(cfg, ".", out=lambda _l: None, researcher=rsi.ScriptedResearcher())
    first = bench.report(cfg, ".", out=lambda _l: None)
    second = bench.report(cfg, ".", out=lambda _l: None)
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(second, sort_keys=True, default=str)
    paths = write_report(activity.root, rsi.DOMAIN, first)
    assert paths["json"].exists() and "# ouroloop report" in paths["markdown"].read_text()


def test_br08_each_claim_has_its_own_state(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    results = Results(activity.root, rsi.DOMAIN)
    record(results, candidate="base", role="base", success=False, partial=0.5)
    record(results, candidate="evolved", role="evolved", success=True)
    record(results, candidate="prompt_best", role="prompt_best", success=True)
    built = build_report(activity.root, rsi.DOMAIN)
    claims = built["claims"]
    # the prompt worked, the decision model added nothing over the same prompt, and C4 was never run
    assert claims["C1"]["state"] == "inconclusive" and "sealed final plan" in claims["C1"]["detail"]
    assert claims["C3"]["state"] == "not_supported"
    assert claims["C2"]["state"] == "not_run" and claims["C4"]["state"] == "not_run"
    text = render_markdown(built)
    for claim in ("C1", "C2", "C3", "C4"):
        assert f"**{claim}**" in text


def test_a_claim_with_only_one_side_on_record_is_not_run():
    report = {"task_effect": {"arms": {"r1": {"role": "evolved", "summary": {"pass_rate": 1.0}}}},
              "improvement_evidence": {"receipts": []}}
    states = claim_states(report)
    assert states["C1"]["state"] == "not_run" and states["C3"]["state"] == "not_run"


def test_a_research_receipt_that_is_not_significant_is_not_c4_support():
    report = {"task_effect": {"arms": {}},
              "improvement_evidence": {"receipts": [
                  {"profile": "research", "verdict": "accepted", "reasons": ["development acceptance"],
                   "metrics": {"gain": 0.5}, "uncertainty": {"significant": False}}]}}
    assert claim_states(report)["C4"]["state"] == "inconclusive"
    report["improvement_evidence"]["receipts"][0]["uncertainty"]["significant"] = True
    assert claim_states(report)["C4"]["state"] == "supported"


CAMPAIGN = "campaign:sha256:" + "e" * 64


@pytest.mark.parametrize("step,says", [
    ({"outcome": "no_proposal", "stage": "reply", "reason": "the rounds went well"},
     "slot-0: the research policy proposed no change to itself (the rounds went well)"),
    ({"outcome": "invalid", "stage": "reply", "reason": "the research reply was empty"},
     "slot-0: the research slot's reply was invalid (the research reply was empty)"),
    ({"outcome": "invalid", "stage": "admission", "reason": "research may not change worker.harness"},
     "slot-0: admission refused the proposed R (research may not change worker.harness)"),
    ({"outcome": "invalid", "stage": "build", "reason": "research.propose: empty text"},
     "slot-0: the proposed R did not build (research.propose: empty text)"),
    ({"outcome": "budget_exhausted", "reason": "the root cannot hold the outer share"},
     "slot-0: the budget was spent before a campaign ran (the root cannot hold the outer share)"),
    ({"outcome": "budget_exhausted", "reason": "the budget is spent", "campaign": CAMPAIGN},
     f"slot-0: the budget was spent during campaign {ct.short(CAMPAIGN)} (the budget is spent)"),
    ({"outcome": "paused", "reason": "the model provider refuses the account"},
     "slot-0: paused before a campaign ran (the model provider refuses the account)"),
    ({"outcome": "paused", "reason": "a model call failed", "campaign": CAMPAIGN},
     f"slot-0: paused during campaign {ct.short(CAMPAIGN)} (a model call failed)"),
    ({"campaign": CAMPAIGN, "r1": "policyset:sha256:" + "f" * 64, "experiment": "x1"},
     f"slot-0: campaign {ct.short(CAMPAIGN)} is planned and has no receipt")],
    ids=["no_proposal", "invalid_reply", "admission_refused", "build_failed", "budget_exhausted",
         "budget_exhausted_in_campaign", "paused", "paused_in_campaign", "planned"])
def test_c4_says_how_a_slot_without_a_campaign_ended(tmp_path, step, says):
    """docs/modules/07_BENCH_REPORT.md §4: with no research receipt, C4 stays not_run and its detail says how the
    slot ended, from rsi/cycle.json — not "no research campaign on record", and an invalid reply never reads as R
    proposing nothing."""
    from ouroloop.ledger import write_json
    write_json(tmp_path / "rsi" / "cycle.json", {"status": "done", "steps": {
        "baseline": {"release": "release:x"}, "segment-0": {"stop_reason": "no_proposal"}, "slot-0": step}})
    c4 = build_report(tmp_path, rsi.DOMAIN)["claims"]["C4"]
    assert c4["state"] == "not_run" and says in c4["detail"], c4
    assert ("proposed no change" in c4["detail"]) == (step.get("outcome") == "no_proposal")


def test_a_strict_dollar_plan_is_refused_when_the_price_cannot_be_bounded():
    assert check_pricing(Pricing(hidden_fallback=True), {"usd": 5.0})
    assert not check_pricing(Pricing(0.003, 0.015, 1000, 500, source="snapshot"), {"usd": 5.0})


# ---------------------------------------------------------------------------- startup checks
def test_a_phase_refuses_to_start_when_the_declared_budget_cannot_be_enforced(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    cfg["rsi"]["budget_ref"] = activity.objects.put_json("change", {"usd": 5.0, "model_calls": 100})
    cfg["pricing"] = {"hidden_fallback": True}
    problems = bench.startup_checks(activity, cfg, phase="improve")
    assert any("cannot be enforced" in p for p in problems)
    with pytest.raises(ValueError, match="cannot start"):
        bench.improve(cfg, ".", out=lambda _l: None, researcher=rsi.ScriptedResearcher())
    # with a bounded price snapshot the same budget is fine
    cfg["pricing"] = {"input_per_1k": 0.003, "output_per_1k": 0.015, "max_input_tokens": 4000,
                      "max_output_tokens": 1000, "source": "vendor page 2026-09-27"}
    assert not [p for p in bench.startup_checks(activity, cfg, phase="improve") if "enforced" in p]


def test_a_plan_with_too_few_independent_groups_is_named_before_the_run(fake_upstream, tmp_path):
    from ouroloop.evolve.task_gate import GatePlan
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path,
                                             gate=GatePlan(min_gain=0.05, min_groups=9,
                                                           bootstrap_replicates=100))
    problems = bench.startup_checks(activity, cfg, phase="improve")
    assert any("independent groups" in p for p in problems)


def test_a_pricing_table_with_an_unknown_key_is_refused():
    with pytest.raises(ValueError, match="does not take"):
        bench.pricing_of({"pricing": {"input_per_1k": 0.001, "discount": 0.5}})


# ---------------------------------------------------------------------------- formal support
def test_only_the_sealed_final_plan_can_make_a_claim_supported(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    results = Results(activity.root, rsi.DOMAIN)
    # development records: better on record, still not a claim
    record(results, candidate="base", role="base", success=False, partial=0.5)
    record(results, candidate="evolved", role="evolved", success=True)
    assert build_report(activity.root, rsi.DOMAIN)["claims"]["C1"]["state"] == "inconclusive"
    # the same versions under a sealed final plan, on the final tasks
    plan = bench.FinalizationPlan(
        experiment_id="final-2", core_ref=activity.objects.load(release, ct.SystemRelease).core_ref,
        split_ref=activity.splits.hash(), claims=("C1",),
        comparisons=({"claim": "C1", "subject": "evolved", "control": "base", "role": "evolved"},),
        release_refs={"evolved": release}, task_ids=tuple(rsi.FINAL), repeats=1,
        statistical_plan={"confidence": 0.95, "bootstrap_replicates": 300, "bootstrap_seed": 1})
    bench.seal(plan, activity.root, activity.objects)
    record(results, candidate="evolved", role="evolved", split="test", tasks=list(rsi.FINAL), success=True)
    record(results, candidate="base", role="base", split="test", tasks=list(rsi.FINAL), success=False,
           partial=0.5)
    claims = build_report(activity.root, rsi.DOMAIN)["claims"]
    assert claims["C1"]["state"] == "supported" and claims["C1"]["interval"]["low"] > 0
    assert "sealed final plan" in claims["C1"]["detail"]


def test_a_final_comparison_that_goes_the_other_way_is_not_supported(fake_upstream, tmp_path):
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    results = Results(activity.root, rsi.DOMAIN)
    plan = bench.FinalizationPlan(
        experiment_id="final-3", core_ref=activity.objects.load(release, ct.SystemRelease).core_ref,
        split_ref=activity.splits.hash(), claims=("C1",),
        comparisons=({"claim": "C1", "subject": "evolved", "control": "base", "role": "evolved"},),
        task_ids=tuple(rsi.FINAL), repeats=1,
        statistical_plan={"confidence": 0.95, "bootstrap_replicates": 300})
    bench.seal(plan, activity.root, activity.objects)
    record(results, candidate="evolved", role="evolved", split="test", tasks=list(rsi.FINAL), success=False,
           partial=0.5)
    record(results, candidate="base", role="base", split="test", tasks=list(rsi.FINAL), success=True)
    claims = build_report(activity.root, rsi.DOMAIN)["claims"]
    assert claims["C1"]["state"] == "not_supported" and claims["C1"]["interval"]["high"] < 0


# ---------------------------------------------------------------------------- v0.3: what the runner shell's records say
def test_pr7_11_the_report_reads_the_agent_entry_placement_roles_effort_caps_and_limits_from_the_gateway_log():
    """docs/modules/01_POLICY_RUNTIME.md §4 (Inspection, v0.3), report.gateway_summary: from the host-written lines of
    two shell attempts — the runner's `agent` record, the `assistant` events with role/effort/requested_effort and
    usage, the `end` event's `limit` — the capability-package section tallies the entry and its placement, the model
    calls and output tokens by role (as Gateway.by_role does), the efforts used, how many requests the gateway capped
    at K's max_effort, and which hard limit ended an attempt; an attempt under the v1 loop (no gateway line) adds
    nothing, and nothing is read from a package's own mark."""
    from ouroloop.report import gateway_summary
    worker = [
        {"kind": "task", "text": "t"},
        {"kind": "agent", "entry": "agent/main.py:run", "placement": "worker", "roles": ["act", "aux"]},
        {"kind": "assistant", "role": "aux", "effort": "high", "requested_effort": "max",
         "usage": {"input_tokens": 10, "output_tokens": 7}, "tool_calls": [], "stop": "end"},
        {"kind": "gateway_call", "call_id": "c1", "role": "aux"},
        {"kind": "assistant", "role": "act", "effort": "medium", "requested_effort": None,
         "usage": {"prompt_tokens": 3, "completion_tokens": 5}, "tool_calls": [], "stop": "end"},
        {"kind": "gateway_call", "call_id": "c2", "role": "act"},
        {"kind": "package_mark", "mark": "assistant", "role": "act", "source": "package"},   # not a model call
        {"kind": "end", "stop": "act_calls", "turns": 1, "limit": "act_calls"},
    ]
    host = [
        {"kind": "task", "text": "t"},
        {"kind": "agent", "entry": "agent/default_agent.py:run", "placement": "host", "roles": ["act"]},
        {"kind": "assistant", "role": "act", "effort": "medium", "requested_effort": None,
         "usage": {"output_tokens": 2}, "tool_calls": [], "stop": "end"},
        {"kind": "gateway_call", "call_id": "c3", "role": "act"},
        {"kind": "end", "stop": "end", "turns": 1},
    ]
    old_loop = [{"kind": "task", "text": "t"}, {"kind": "assistant", "text": "x", "tool_calls": []},
                {"kind": "end", "stop": "end", "turns": 1}]
    summary = gateway_summary([worker, host, old_loop])
    assert summary == {"attempts": 2,
                       "entries": {"agent/main.py:run": 1, "agent/default_agent.py:run": 1},
                       "placements": {"worker": 1, "host": 1},
                       "by_role": {"aux": {"calls": 1, "tokens": 7}, "act": {"calls": 2, "tokens": 7}},
                       "efforts": {"high": 1, "medium": 2}, "effort_caps": 1, "limits_hit": {"act_calls": 1}}
    assert gateway_summary([old_loop]) == gateway_summary([])        # observed nothing: no zeros posing as calls
    assert gateway_summary([])["attempts"] == 0
    # the markdown line carries the same facts, and a report without shell attempts has no such line
    harness = {"packages": {"release:sha256:" + "a" * 64: {
        "package_ref": "package:sha256:" + "b" * 64, "files": 2, "test_files": 0, "kinds": ["agent"],
        "skills": [], "hooks": [], "tools": [], "memory": False, "agent": "agent/main.py:run", "roles": ["act", "aux"]}},
        "behaviour": {"sessions": 2, "skill_reads": {}, "hooks": {}, "package_tools": {}, "worker_sessions": 1,
                      "worker_seconds": 1.5, "gateway": summary},
        "measurements": [], "measurement_note": "n", "notes": [], "drafts": {"checks": 0, "refused": 0,
                                                                            "infrastructure": 0, "finished": {}},
        "gates": [], "research_cost": {}, "cycle": None, "reuse": [], "not_run": {}}
    report = {"protocol": "p", "identity": {"domain": "d", "attempts": 2, "runs": 1, "splits": None, "releases": [],
                                            "missing": []},
              "task_effect": {"arms": {}}, "run_cost": {}, "improvement_evidence": {"experiments": [],
                                                                                   "installations": []},
              "rsi_evidence": {"research_receipts": [], "discovery": None, "installed_releases": []}, "claims": {},
              "harness": harness, "improvement_cost": {}}
    text = render_markdown(report)
    assert "agent agent/main.py:run roles ['act', 'aux']" in text
    assert ("runner shell over 2 attempts: agent {'agent/main.py:run': 1, 'agent/default_agent.py:run': 1} placed "
            "{'worker': 1, 'host': 1}; model calls by role {'aux': {'calls': 1, 'tokens': 7}, 'act': {'calls': 2, "
            "'tokens': 7}}; efforts {'high': 1, 'medium': 2}, 1 requests capped at K's max_effort; limits hit "
            "{'act_calls': 1}") in text
    harness["behaviour"]["gateway"] = gateway_summary([])
    assert "runner shell" not in render_markdown(report)


def test_each_arm_shows_its_cost_per_attempt(fake_upstream, tmp_path):
    """M7 §4, M5 §2 (v0.3): the report's run_cost gives each run its `cost_per_attempt` — the mean of the priced
    valid attempts, None when none was priced, the unpriced ones counted — and its rate-limit refusals summed from
    the `rate_limited` events of that run alone; the improvement cost sums them for the attempts and for the
    research steps apart."""
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    priced = record(activity.results, candidate="base", role="base", cost=0.5)
    unpriced = record(activity.results, candidate="evolved", role="evolved", cost=None)
    events = ct.Events(activity.root)
    events.append("rate_limited", scope="it", arm="dev_baseline", run=priced.hash(), count=3)
    events.append("rate_limited", scope="it", arm="control", run=priced.hash(), count=2)
    events.append("rate_limited", scope="it", arm="subject", run="0" * 16, count=7)        # another run's
    events.append("rate_limited", scope="it", arm="research", round=0, count=4)
    report = build_report(activity.root, rsi.DOMAIN)
    assert report["run_cost"][priced.hash()]["cost_per_attempt"] == 0.5
    assert report["run_cost"][priced.hash()]["rate_limited"] == 5
    assert report["run_cost"][unpriced.hash()]["cost_per_attempt"] is None
    assert report["run_cost"][unpriced.hash()]["cost_missing"] == 2
    assert report["run_cost"][unpriced.hash()]["rate_limited"] == 0
    assert report["improvement_cost"]["rate_limited"] == {"attempts": 12, "research": 4}
    assert "rate-limit refusals absorbed as unbilled retries" in render_markdown(report)


# ---------------------------------------------------------------------------- v0.3 (M7 §4, BR6-23): the ledger in the report
def test_the_ledger_is_listed_per_candidate_without_dev_task_ids(fake_upstream, tmp_path):
    """M7 §4 / BR6-23: the report's capability-package section lists the engine ledger per candidate — declaration,
    screen result, verdict, the gate's three classes as counts — with the carriers' distribution and how many
    admissions `repeated_mechanism` refused; the dev task ids of the gate stage are in no report field and not in
    the markdown; the session records are listed too."""
    from ouroloop.evolve import experience as xp
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    root = activity.root
    xp.append(root, {"kind": "diagnosis", "scope": "it", "round": 0, "noted": 1, "failures": 1, "reused": 0,
                     "invalid": [], "included": ["rsi.notify_after_update"], "not_included": [],
                     "working_set": "all", "measured": 5, "passed": 3})
    declaration = {"mechanism": "check every step", "mechanism_normalized": "check every step", "site": "memory.md",
                   "carrier": "memory", "predicted_affected": ["rsi.notify_after_update"],
                   "trigger_condition": "always", "differs_from": [], "notes": []}
    for stage, fields in (("declared", {"declaration": declaration, "status": "declared", "dir": "m1",
                                        "declaration_ref": "declaration:sha256:" + "a" * 64}),
                          ("screened", {"tasks": ["rsi.notify_after_update"], "net_flips": 1, "thin_screen": True,
                                        "outcome": "selected"}),
                          ("gated", {"verdict": "accepted", "receipt": "receipt:sha256:" + "b" * 64,
                                     "reading_kind": "confirmed", "gain": 0.2, "control_readings": 3,
                                     "fixed": ["rsi.dev_notify_after_update"], "regressed": [],
                                     "still_failing": ["rsi.dev_policy_hold"], "installed": True})):
        xp.append(root, xp.ledger_row(stage, "it/r0/m1", scope="it", round=0, **fields))
    xp.append(root, xp.ledger_row("declared", "it/r1/m2", scope="it", round=1, dir="m2", status="declared",
                                  declaration={**declaration, "carrier": "hook", "site": "on_finish"}))
    xp.append(root, xp.ledger_row("admitted", "it/r1/m2", scope="it", round=1, status="invalid",
                                  reasons=["repeated_mechanism: 'check every step' at 'on_finish' was rejected"]))
    ct.Events(root).append("research_session", scope="it", round=0, turns=5, edits=2, trajectory_reads=1,
                           trajectory_bytes=1200, finishes=1, ended="")
    report = build_report(root, rsi.DOMAIN)
    ledger = report["harness"]["ledger"]
    assert ledger["carriers"] == {"memory": 1, "hook": 1} and ledger["repeated_mechanism_refusals"] == 1
    assert ledger["outcomes"] == {"installed": 1, "invalid": 1}
    first = ledger["candidates"][0]
    assert first["gate"] == {"verdict": "accepted", "reading_kind": "confirmed", "gain": 0.2, "control_readings": 3,
                             "cost_per_attempt": None, "fixed": 1, "regressed": 0, "still_failing": 1}
    assert first["screen"]["tasks"] == ["rsi.notify_after_update"]
    assert "rsi.dev" not in json.dumps(ledger)
    assert report["harness"]["sessions"] == [{"scope": "it", "round": 0, "turns": 5, "edits": 2, "trajectory_reads": 1,
                                              "trajectory_bytes": 1200, "finishes": 1, "ended": ""}]
    markdown = render_markdown(report)
    assert "- ledger: 2 candidates" in markdown and "it/r0/m1: memory at memory.md" in markdown
    assert "fixed 1, regressed 0, still failing 1" in markdown and "rsi.dev" not in markdown
    assert "- session round 0: 5 turns, 2 edits, 1 trajectory reads (1200 bytes), 1 finish" in markdown


# ---------------------------------------------------------------------------- v0.3 step 5 (M7 §4, BR6-20/23): per domain
POOLED = "marketing+sales+support"


def pooled_workspace(fake_upstream, tmp_path):
    """The pooled split of tests/test_bench_phase.py's domain config (Marketing pinned and spent, sales and support
    stratified) frozen in a workspace, with nothing run yet. -> (root, pooled manifest)."""
    from test_bench_phase import domains_config
    loaded = bench.BenchConfig.load(domains_config(fake_upstream, tmp_path))
    _suite, pooled, _ = bench.load_domains(loaded, tmp_path / "ws")
    return tmp_path / "ws", pooled


def record_final(results: Results, pooled, *, role, passes) -> RunManifest:
    """One final-split run of `role` over every test task of the pooled split, a task passing when `passes(task)`."""
    run = rsi.run_manifest(role, role, "test", data=pooled.hash())
    results.put_run(run)
    for task in pooled.ids("test"):
        results.append(TaskResult(task, task, "test", 0, role, run.hash(), pooled.hash(), success=passes(task),
                                  partial_credit=1.0 if passes(task) else 0.0, cost_usd=0.4 if role == "base" else 0.6,
                                  pair_key=f"plan/{task}/r0"))
    return run


def seal_final(root, pooled) -> None:
    objects = ct.Objects(root)
    plan = bench.FinalizationPlan(experiment_id="md", claims=("C1",), task_ids=tuple(pooled.ids("test")),
                                  comparisons=({"claim": "C1", "subject": "evolved", "control": "base"},),
                                  statistical_plan={"bootstrap_replicates": 200, "bootstrap_seed": 1})
    bench.seal(plan, root, objects)


def test_task_effect_is_broken_down_per_domain(fake_upstream, tmp_path):
    """BR6-23 (按领域分列): every run of a pooled activity carries its pass rate, passes and cost per attempt per
    domain beside the pooled row; the identity lists each domain's split — hash, rule, counts, strata — and the
    markdown has the per-domain table. A one-domain report has none of it, as before."""
    root, pooled = pooled_workspace(fake_upstream, tmp_path)
    results = Results(root, POOLED)
    run = rsi.run_manifest("base", "base", "dev", data=pooled.hash())
    results.put_run(run)
    for task in pooled.ids("dev"):
        ok = task.startswith("sales.")
        results.append(TaskResult(task, task, "dev", 0, "base", run.hash(), pooled.hash(), success=ok,
                                  partial_credit=1.0 if ok else 0.5, cost_usd=0.3 if ok else 0.5))
    report = build_report(root, POOLED)
    arm = report["task_effect"]["arms"][run.hash()]
    assert set(arm["per_domain"]) == {"sales", "support"}
    assert arm["per_domain"]["sales"]["pass_rate"] == 1.0 and arm["per_domain"]["support"]["pass_rate"] == 0.0
    assert arm["per_domain"]["sales"]["cost_per_attempt"] == 0.3 and arm["per_domain"]["support"]["cost_per_attempt"] == 0.5
    assert report["run_cost"][run.hash()]["per_domain"] == {"sales": 0.3, "support": 0.5}
    assert arm["summary"]["tasks"] == sum(v["tasks"] for v in arm["per_domain"].values())
    domains = report["identity"]["domains"]
    assert [d["domain"] for d in domains] == ["marketing", "sales", "support"]
    assert [d["spent"] for d in domains] == [True, False, False] and domains[1]["strata"]
    markdown = render_markdown(report)
    assert "### By domain" in markdown and "| sales |" in markdown and "(spent)" in markdown
    # one domain: nothing per domain
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path / "one")
    record(activity.results, candidate="base", role="base", cost=0.5)
    single = build_report(activity.root, rsi.DOMAIN)
    assert single["identity"]["domains"] == [] and "final_by_domain" not in single
    assert all("per_domain" not in a for a in single["task_effect"]["arms"].values())


def test_marketing_test_rows_are_marked_spent_and_kept_out_of_the_claim(fake_upstream, tmp_path):
    """BR6-20: when a final's runs hold test rows of a spent domain, the final table marks that domain `spent`, the
    claim row pools only the domains a new claim may read, and C1's interval is computed over those tasks alone —
    here Marketing's test task would flip the sign of the claim if it were counted."""
    root, pooled = pooled_workspace(fake_upstream, tmp_path)
    results = Results(root, POOLED)
    marketing = [t for t in pooled.ids("test") if t.startswith("marketing.")]
    assert marketing                                        # the fixture's Marketing has a test task under seed 1
    # evolved wins every new-domain task and loses Marketing's
    record_final(results, pooled, role="base", passes=lambda t: t.startswith("marketing."))
    record_final(results, pooled, role="evolved", passes=lambda t: not t.startswith("marketing."))
    seal_final(root, pooled)
    report = build_report(root, POOLED)
    final = report["final_by_domain"]
    assert final["domains"]["marketing"]["spent"] is True and final["domains"]["sales"]["spent"] is False
    assert final["domains"]["marketing"]["difference"] == -1.0
    claim_tasks = len(pooled.claim_ids("test"))
    assert final["claim"]["evolved"] == {"tasks": claim_tasks, "pass_rate": 1.0}
    assert final["claim"]["base"] == {"tasks": claim_tasks, "pass_rate": 0.0}
    c1 = report["claims"]["C1"]
    assert c1["interval"]["tasks"] == claim_tasks and c1["interval"]["point"] == 1.0
    assert "spent: not in the claim" in render_markdown(report)


def test_the_final_table_lists_the_best_of_k_arm_only_when_it_ran(fake_upstream, tmp_path):
    """BR6-23 / M3 §2.2: the optional matched-cost best-of-k W0 arm is listed in the final table only when a run of
    it is on record (role `matched_cost`); otherwise the table says not_run, and it never enters the claim row."""
    root, pooled = pooled_workspace(fake_upstream, tmp_path)
    results = Results(root, POOLED)
    record_final(results, pooled, role="base", passes=lambda t: False)
    record_final(results, pooled, role="evolved", passes=lambda t: True)
    seal_final(root, pooled)
    report = build_report(root, POOLED)
    assert report["final_by_domain"]["best_of_k"] == "not_run"
    assert "matched-cost best-of-k arm: not_run" in render_markdown(report)
    record_final(results, pooled, role="matched_cost", passes=lambda t: True)
    report = build_report(root, POOLED)
    assert report["final_by_domain"]["best_of_k"] == "ran"
    assert "matched_cost" in report["final_by_domain"]["domains"]["sales"]["roles"]
    assert set(report["final_by_domain"]["claim"]) >= {"base", "evolved"}
    assert report["claims"]["C1"]["interval"]["point"] == 1.0        # evolved against base, never the k arm


def test_it6_36_the_report_lists_cost_per_attempt_the_ledger_and_headroom_per_domain(fake_upstream, tmp_path,
                                                                                      monkeypatch):
    """IT6-36: after a cycle over Marketing and the two fixture domains, the report gives every run its cost per
    attempt per domain beside the pooled one, the research slot's headroom per domain and how its halves split, and
    the ledger's fixed/regressed/still-failing per domain — counts, no dev task id. The helper a gate receipt's
    `metrics.per_domain` is made with is checked at the end; the receipt itself is
    test_task_gate.py::test_a_receipt_over_several_domains_carries_per_domain_means (2026-10-05 review)."""
    from ouroloop.evolve import experience as xp
    from ouroloop.evolve.task_gate import per_domain
    from test_bench_phase import run_domains_cycle
    cfg, result = run_domains_cycle(fake_upstream, tmp_path, monkeypatch)
    root = tmp_path / "ws"
    dev = SplitManifest.load(Results(root, POOLED).manifest_path()).ids("dev")
    sales, support = [t for t in dev if t.startswith("sales.")], [t for t in dev if t.startswith("support.")]
    xp.append(root, xp.ledger_row("declared", "md/r0/m1", scope="md", round=0, dir="m1", status="declared",
                                  declaration={"mechanism": "check", "site": "memory.md", "carrier": "memory"}))
    xp.append(root, xp.ledger_row("gated", "md/r0/m1", scope="md", round=0, verdict="rejected",
                                  receipt="receipt:sha256:" + "e" * 64, gain=0.0, fixed=sales[:1],
                                  regressed=support[:1], still_failing=sales[1:] + support[1:]))
    report = build_report(root, POOLED)
    runs = report["task_effect"]["arms"]
    assert runs and all(set(a["per_domain"]) <= {"marketing", "sales", "support"} for a in runs.values())
    priced = [r for r in report["run_cost"].values() if r.get("per_domain")]
    assert priced and all(v is not None and v > 0 for r in priced for v in r["per_domain"].values())
    headroom = report["harness"]["headroom"]
    assert headroom[0]["slot"] == "slot-0" and headroom[0]["outcome"] == "no_headroom"
    assert set(headroom[0]["by_domain"]) == {"sales", "support"}
    assert sum(headroom[0]["by_domain"].values()) == headroom[0]["headroom"]
    assert set(headroom[0]["halves_by_domain"]["inner"]) == {"sales", "support"}
    by_domain = report["harness"]["ledger"]["by_domain"]
    assert by_domain["sales"]["fixed"] == 1 and by_domain["support"]["regressed"] == 1
    assert sum(d["still_failing"] for d in by_domain.values()) == len(sales) + len(support) - 2
    assert not any(t in json.dumps(report["harness"]["ledger"]) for t in dev)
    markdown = render_markdown(report)
    assert "- headroom slot-0:" in markdown and "ledger by domain" in markdown
    assert not any(t in markdown for t in dev)
    # a gate receipt over several domains carries the paired means per domain; over one domain it carries nothing
    split = per_domain({"sales.a": 1.0, "sales.b": 0.0, "support.a": 1.0}, {"sales.a": 0.0, "sales.b": 0.0,
                                                                         "support.a": 1.0})
    assert split == {"sales": {"tasks": 2, "subject": 0.5, "control": 0.0, "gain": 0.5},
                     "support": {"tasks": 1, "subject": 1.0, "control": 1.0, "gain": 0.0}}
    assert per_domain({"rsi.a": 1.0}, {"rsi.a": 0.0}) == {}


def test_a_claim_before_the_final_reads_the_dev_readings_only(fake_upstream, tmp_path):
    """Stage 0/1 review, item 2: before a final exists a claim compares the dev readings of the two roles only — a
    W's train-side screen or train measurement (role `evolved` or `base` too) is another task set and is not
    averaged in. Here the evolved dev reading equals the base one; its perfect train screen used to lift it above."""
    cfg, activity, release, plan_ref = setup(fake_upstream, tmp_path)
    record(activity.results, candidate="base", role="base", split="dev", success=False, partial=0.5)
    record(activity.results, candidate="candidate", role="evolved", split="dev", success=False, partial=0.5)
    record(activity.results, candidate="screened", role="evolved", split="train", tasks=["rsi.update_stage"])
    c1 = build_report(activity.root, rsi.DOMAIN)["claims"]["C1"]
    assert c1["state"] == "not_supported" and c1["detail"].endswith("0.0 against 0.0"), c1
    # a dev reading that is higher is still only a development record
    record(activity.results, candidate="better", role="evolved", split="dev")
    c1 = build_report(activity.root, rsi.DOMAIN)["claims"]["C1"]
    assert c1["state"] == "inconclusive" and "on development records" in c1["detail"]


# ---------------------------------------------------------------------------- v0.4: the equal-compute reference
def test_the_final_reports_w0s_pass_at_k_from_its_own_repeats_beside_w_star(tmp_path):
    """V0_4_PLAN §1.8, M3 §2.2 (v0.4): from the final's own W0 repeats (n per task, c passes) the report computes W0's
    oracle pass@k for k = 1..n — the unbiased 1 − C(n−c, k)/C(n, k), averaged over tasks — beside W*'s pass rate and
    the cost ratio, labelled the equal-compute reference; it costs no attempt and is not a claim. Worked by hand: t1
    passes 1 of 3 and t2 0 of 3 for W0, so pass@1 = (1/3 + 0)/2, pass@2 = (2/3 + 0)/2, pass@3 = (1 + 0)/2; W* passes
    3 of 3 and 2 of 3, 0.8333; W* costs 0.6 an attempt against W0's 0.4, a ratio of 1.5. A dev-only record has none."""
    results = Results(tmp_path, rsi.DOMAIN)
    runs = {}
    for role, cost in (("base", 0.4), ("evolved", 0.6)):
        runs[role] = rsi.run_manifest(role, role, "test")
        results.put_run(runs[role])
    outcomes = {"base": {"t1": [True, False, False], "t2": [False, False, False]},
                "evolved": {"t1": [True, True, True], "t2": [True, False, True]}}
    for role, tasks in outcomes.items():
        for task, passes in tasks.items():
            for i, ok in enumerate(passes):
                results.append(TaskResult(task, task, "test", i, role, runs[role].hash(), "man", success=ok,
                                          partial_credit=1.0 if ok else 0.5, cost_usd=0.4 if role == "base" else 0.6))
    reference = build_report(tmp_path, rsi.DOMAIN)["final_reference"]
    assert reference["tasks"] == 2 and reference["repeats"] == 3
    assert reference["base_pass_at_k"] == {"1": 0.1667, "2": 0.3333, "3": 0.5}
    assert reference["evolved_pass_rate"] == 0.8333 and reference["cost_ratio"] == 1.5
    assert "equal-compute reference" in reference["label"] and "not part of C1" in reference["label"]
    text = render_markdown(build_report(tmp_path, rsi.DOMAIN))
    assert "Equal-compute reference" in text and "pass@3 0.5" in text
    dev = Results(tmp_path / "dev", rsi.DOMAIN)
    record(dev, candidate="base", role="base", split="dev")
    assert "final_reference" not in build_report(tmp_path / "dev", rsi.DOMAIN)
