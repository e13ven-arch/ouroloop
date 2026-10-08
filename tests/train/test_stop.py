"""StopGuard (ouroloop/train/stop.py), Runtime.propose (train/runtime.py) and a stop decision's record (train/trace.py):
the guard's rules on the general runtime, and a worker.stop policy proposing, being overruled and falling back on the
fixture domain.

The guard is called directly here, and the episodes are played by stop_fixtures.play, a host that asks the stop point.
The policies are the tests' own (stop_fixtures.ViewPolicy): the binding that built one from a release went with
policies.py in stage 1."""
import pytest
import rsi
from stop_fixtures import ViewPolicy, play, stop_runtime

from ouroloop.contracts import CandidateDefect, Objects
from ouroloop.harness.package import Package
from ouroloop.harness.seed import build_seed
from ouroloop.runtime.runner import compose_system
from ouroloop.train import records
from ouroloop.train.runtime import Runtime
from ouroloop.train.stop import StopGuard
from ouroloop.train.trace import stop_context, tool_event
from ouroloop.train.views import business_stop_point

NOTIFY = "rsi.notify_after_update"
TASK = "Move Dana Reyes to opportunity stage, then post a message in #ops containing the phrase stage updated."


def ctx(phase="on_finish", step=1, events=()):
    return stop_context(TASK, "Done: the record is updated.", phase=phase, step=step, tool_events=list(events),
                        budget_remaining={"steps": 3, "tool_calls": 5})


def rows(rt, phase=None):
    return [r.decision for r in rt.ledger.rows("stop") if phase is None or r.decision["phase"] == phase]


def overrides(rt):
    return [e for e in rt.ledger.events() if e.get("type") == "override"]


# ---------------------------------------------------------------------------- the guard's rules
def test_per_turn_takes_its_default_and_the_turn_cap_ends_the_session_without_a_decision(tmp_path):
    rt = stop_runtime(tmp_path)
    guard = StopGuard(rt, "s1", max_turns=3)
    call = guard("per_turn", ctx("per_turn"), 1)
    assert (call.stop, call.decision is not None) == (False, True)
    (row,) = rows(rt)
    assert (row["route"], row["action"], row["backend"], row["probs"]) == ("shadow", "no", "none", None)
    capped = guard("per_turn", ctx("per_turn", 3), 3)
    assert (capped.stop, capped.reason, capped.decision) == (True, "max_turns", None) and len(rows(rt)) == 1


def test_on_finish_ends_on_its_default_and_a_runtime_without_the_point_records_nothing(tmp_path):
    rt = stop_runtime(tmp_path)
    call = StopGuard(rt, "s1", max_turns=3)("on_finish", ctx(), 1)
    assert call.stop and [(r["phase"], r["action"]) for r in rows(rt)] == [("on_finish", "yes")]
    bare = Runtime(tmp_path / "bare")
    failing = StopGuard(bare, "s1", checker=lambda: (False, "the check failed"))
    assert failing("on_finish", ctx(), 1).stop is False and not list(bare.ledger.events())


def test_a_failing_checker_sends_the_agent_back_until_the_cap_and_the_overrule_is_recorded(tmp_path):
    rt = stop_runtime(tmp_path)
    guard = StopGuard(rt, "s1", checker=lambda: (False, "the check failed"), max_pushbacks=2, max_turns=8)
    calls = [guard("on_finish", ctx(step=t), t) for t in (1, 2, 3)]
    assert [(c.stop, c.reason, c.checked) for c in calls] == [(False, "the check failed", False),
                                                              (False, "the check failed", False), (True, "", False)]
    assert [(r["route"], r["action"]) for r in rows(rt)] == [("rule", "no")] * 3       # the checker's veto, each time
    (override,) = overrides(rt)
    assert (override["decision"], override["asked_for"], override["executed"], override["reason"]) == \
        (calls[-1].decision, "no", "yes", "max_pushbacks")
    assert guard.overruled == [{"decision": calls[-1].decision, "asked_for": "no", "executed": "yes",
                                "reason": "max_pushbacks"}]


def test_a_checker_overrules_a_yes_after_a_turn_and_a_passing_one_ends_it(tmp_path):
    """per_turn runs the checker only on a "yes": a failing one keeps the agent working, a passing one ends the
    session at the stop point."""
    rt = Runtime(tmp_path)
    rt.register(business_stop_point(phases={"per_turn": "yes", "on_finish": "yes"}))
    verdicts = iter([(False, "not yet"), (True, "")])
    guard = StopGuard(rt, "s1", checker=lambda: next(verdicts), max_turns=8)
    first, second = guard("per_turn", ctx("per_turn", 1), 1), guard("per_turn", ctx("per_turn", 2), 2)
    assert (first.stop, first.checked, second.stop, second.reason, second.checked) == (False, False, True,
                                                                                       "stop_point", True)
    assert [(r["route"], r["action"]) for r in rows(rt)] == [("rule", "no"), ("shadow", "yes")]


def always_yes(_ctx) -> str:
    return "yes"


def test_without_a_policy_the_phases_answerer_decides_and_no_model_is_asked(tmp_path):
    """A rule answerer's "yes" after a turn ends the session; the row is a shadow row with the answerer named, no
    backend and no probabilities."""
    rt = Runtime(tmp_path)
    rt.register(business_stop_point(phases={"per_turn": {"default": "rule:test_stop:always_yes", "label": "no"},
                                            "on_finish": "yes"}))
    assert StopGuard(rt, "s")("per_turn", ctx("per_turn"), 1).stop
    (row,) = rows(rt)
    assert (row["route"], row["action"], row["answerer"], row["backend"], row["probs"]) == (
        "shadow", "yes", "rule:test_stop:always_yes", "none", None)


def test_a_passing_checker_is_never_overruled_by_a_policy_that_says_no(tmp_path):
    rt = stop_runtime(tmp_path)
    call = StopGuard(rt, "s1", checker=lambda: (True, ""), policy=ViewPolicy("no"))("on_finish", ctx(), 1)
    (row,) = rows(rt)
    assert call.stop and (row["proposed"], row["action"], row["executor"]) == ("no", "yes", "guard")


def test_a_no_on_the_last_turn_is_overruled_as_the_turn_cap(tmp_path):
    rt = stop_runtime(tmp_path)
    call = StopGuard(rt, "s1", max_turns=4, policy=ViewPolicy("no"))("on_finish", ctx(step=4), 4)
    assert call.stop and [(o["asked_for"], o["reason"]) for o in overrides(rt)] == [("no", "max_turns")]
    assert [(r["proposed"], r["action"], r["executor"]) for r in rows(rt)] == [("no", "no", "policy")]


def test_a_policy_not_enabled_at_a_phase_is_not_asked(tmp_path):
    rt = stop_runtime(tmp_path)
    policy = ViewPolicy("yes")
    assert not StopGuard(rt, "s1", policy=policy)("per_turn", ctx("per_turn"), 1).stop
    (row,) = rows(rt)
    assert policy.views == [] and (row["proposed"], row["action"], row["executor"]) == (None, "no", "default")


def test_an_abstention_or_a_failing_policy_is_recorded_as_a_fallback_not_as_agreement(tmp_path):
    """The phase's fixed default is taken and the row says why; nothing reads as the policy's own "yes"."""
    rt = stop_runtime(tmp_path)
    StopGuard(rt, "s1", policy=ViewPolicy("abstain"))("on_finish", ctx(), 1)
    StopGuard(rt, "s2", policy=ViewPolicy(fail=TimeoutError("model server timed out")))("on_finish", ctx(), 1)
    abstained, failed = rows(rt)
    assert (abstained["proposed"], abstained["action"], abstained["executor"]) == ("abstain", "yes", "default")
    assert abstained["fallback_reason"] == "the policy answers abstain" and abstained["route"] == "default"
    assert (failed["route"], failed["action"], failed["executor"]) == ("error", "yes", "default")
    assert failed["error"].startswith("TimeoutError") and failed["proposed"] is None


def test_er03_a_policy_defect_is_raised_and_its_row_says_so(tmp_path):
    rt = stop_runtime(tmp_path)
    defect = CandidateDefect("the answer is not a distribution over the declared labels")
    with pytest.raises(CandidateDefect, match="not a distribution"):
        StopGuard(rt, "s1", policy=ViewPolicy(fail=defect))("on_finish", ctx(), 1)
    (row,) = rows(rt)
    assert row["defect"] and (row["route"], row["action"], row["executor"]) == ("error", None, "none")


def test_the_stop_context_keeps_what_was_visible_and_names_what_was_cut():
    events = [tool_event("t1", "api_fetch", {"url": "u"}, "x" * 2500, False, 1),
              tool_event("t2", "api_search", {"q": "y"}, "not found", True, 2)]
    context = stop_context(TASK, "m" * 5000, phase="on_finish", step=2, tool_events=events,
                           budget_remaining={"steps": 6})
    assert len(context["final_message"]) == 4000 and context["truncation"] == {"tool_results_cut": ["t1"]}
    assert (len(events[0]["result_excerpt"]), events[1]["error"], events[1]["result_excerpt"]) == (2000, "not found",
                                                                                                  "")
    view, _ = business_stop_point().render(context)
    assert "post a message in #ops containing the phrase stage updated" in view and "tool_results_cut" in view


# ---------------------------------------------------------------------------- a policy on the fixture domain
@pytest.fixture
def suite(fake_upstream):
    return rsi.suite(fake_upstream)


@pytest.fixture
def splits(suite):
    return rsi.splits(suite)


def seed_prompt(root):
    """The system prompt the stirrup seed gives the worker: the task's own, then its prompt/system.md and finish.md."""
    seed: Package = build_seed("stirrup", core={"model": {"identity": "scripted-1"}}, objects=Objects(root))
    return lambda task_system: compose_system(task_system, seed)


def episode(tmp_path, suite, splits, worker, *, policy=None, system=None, max_pushbacks=2, task=NOTIFY):
    rt = stop_runtime(tmp_path / "ws")
    objects = Objects(tmp_path / "ws")
    out = play(rt, suite, splits, task, worker, system=system, policy=policy, objects=objects,
               max_pushbacks=max_pushbacks)
    return rt, out, objects


def test_pr01_the_business_view_carries_the_tail_requirement_and_the_tool_results(tmp_path, suite, splits):
    rt, out, objects = episode(tmp_path, suite, splits, rsi.BusinessWorker())
    view = rows(rt)[-1]["view"]
    assert "post a message in #ops containing the phrase stage updated" in view   # past 300 characters
    assert "api_fetch" in view and "PATCH" in view
    for word in ("rubric", "assertion", "expected", "excluded"):
        assert word not in view.lower()
    # the gap the loop starts from: given only the task's prompt the worker updates the record and never notifies,
    # and the label of its stop says so
    assert [name for name, _ in out["calls"]] == ["api_fetch"]
    finish = rows(rt, "on_finish")[-1]["id"]
    assert [r.outcomes[0]["label"] for r in rt.ledger.rows("stop") if r.decision["id"] == finish] == ["no"]


def test_the_seed_s_finish_check_closes_the_gap_and_the_late_stop_is_labelled_yes(tmp_path, suite, splits):
    rt, out, _ = episode(tmp_path, suite, splits, rsi.BusinessWorker(), system=seed_prompt(tmp_path / "seed"))
    assert [args["url"] for _, args in out["calls"]] == [rsi.HUBSPOT, rsi.SLACK] and out["pushbacks"] == 0
    labels = {r.decision["id"]: r.outcomes[0]["label"] for r in rt.ledger.rows("stop")}
    assert [labels[d] for d in out["decisions"]] == ["no", "yes", "yes"]       # after the update, the post, the finish


def test_pr02_a_policy_that_says_no_sends_the_agent_back_and_the_task_then_ends(tmp_path, suite, splits):
    policy = ViewPolicy()
    rt, out, objects = episode(tmp_path, suite, splits, rsi.BusinessWorker(), policy=policy)
    assert [(d["proposed"], d["action"], d["executor"]) for d in rows(rt, "on_finish")] == [
        ("no", "no", "policy"), ("yes", "yes", "policy")]
    per_turn = rows(rt, "per_turn")
    assert per_turn and all(d["proposed"] is None and d["executor"] == "default" for d in per_turn)
    assert [args["url"] for _, args in out["calls"]] == [rsi.HUBSPOT, rsi.SLACK] and out["pushbacks"] == 1
    # the trace keeps the proposed and the executed action apart, from the observation that was visible
    traces = [objects.load(r, records.DecisionTrace) for r in out["traces"]]
    assert len(traces) == len(out["decisions"]) and all(t.input_schema == "stop.business.v1" for t in traces)
    assert any(t.proposed_action == "no" and t.executed_action == "no" and t.executor == "policy" for t in traces)
    assert all(t.policy_ref == policy.ref for t in traces)
    observation = objects.load(traces[0].observation_ref, records.Observation)
    assert observation.task_text.endswith("stage updated.") and observation.phase == "per_turn"
    counted = out["traced"].activity.seen
    assert len(counted["call"]) == 2 and sorted(v for _, v in counted["propose"]) == ["no", "yes"]


def test_pr03_the_guard_overrules_a_policy_that_never_agrees(tmp_path, suite, splits):
    rt, out, objects = episode(tmp_path, suite, splits, rsi.BusinessWorker(ignore_pushback=True, forget_notify=True),
                               policy=ViewPolicy("no"), max_pushbacks=1)
    found = overrides(rt)
    assert found and found[-1]["asked_for"] == "no" and found[-1]["executed"] == "yes"
    assert found[-1]["reason"] in ("max_pushbacks", "max_turns")
    assert out["pushbacks"] == 1 and out["traced"].activity.seen["override"]
