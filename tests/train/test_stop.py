"""StopGuard (ouroloop/train/stop.py) and a stop decision's record (train/trace.py): the guard's rules on the
general runtime, and a bound worker.stop policy proposing, being overruled and falling back on the fixture domain
(docs/modules/01_POLICY_RUNTIME.md §3, §5 PR-01..PR-03, ER-03).

These were tested inside the v1 loop (tests/test_agent.py, tests/test_policy_runtime.py) until it was removed in P1
step 5. The guard is called directly here, and the episodes are played by stop_fixtures.play, a host that asks the
stop point the way that loop's business path did."""
import json

import pytest
import rsi
from stop_fixtures import play, stop_runtime

from ouroloop import policies as P
from ouroloop.evolve import contracts as ct
from ouroloop.research.inspection import inspect, public
from ouroloop.train.stop import StopGuard
from ouroloop.train.trace import stop_context, tool_event
from ouroloop.train.types import Answer, Request
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
    from ouroloop.train.runtime import Runtime
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
    rt = Runtime_with_yes_after_a_turn(tmp_path)
    verdicts = iter([(False, "not yet"), (True, "")])
    guard = StopGuard(rt, "s1", checker=lambda: next(verdicts), max_turns=8)
    first, second = guard("per_turn", ctx("per_turn", 1), 1), guard("per_turn", ctx("per_turn", 2), 2)
    assert (first.stop, first.checked, second.stop, second.reason, second.checked) == (False, False, True,
                                                                                       "stop_point", True)
    assert [(r["route"], r["action"]) for r in rows(rt)] == [("rule", "no"), ("shadow", "yes")]


def Runtime_with_yes_after_a_turn(root):
    from ouroloop.train.runtime import Runtime
    rt = Runtime(root)
    rt.register(business_stop_point(phases={"per_turn": "yes", "on_finish": "yes"}))
    return rt


def always_yes(_ctx) -> str:
    return "yes"


def test_without_a_policy_the_phases_answerer_decides_and_no_model_is_asked(tmp_path):
    """A rule answerer's "yes" after a turn ends the session; the row is a shadow row with the answerer named, no
    backend and no probabilities (formerly test_act, on the v1 runtime)."""
    from ouroloop.train.runtime import Runtime
    rt = Runtime(tmp_path)
    rt.register(business_stop_point(phases={"per_turn": {"default": "rule:test_stop:always_yes", "label": "no"},
                                            "on_finish": "yes"}))
    assert StopGuard(rt, "s")("per_turn", ctx("per_turn"), 1).stop
    (row,) = rows(rt)
    assert (row["route"], row["action"], row["answerer"], row["backend"], row["probs"]) == (
        "shadow", "yes", "rule:test_stop:always_yes", "none", None)


def test_a_passing_checker_is_never_overruled_by_a_policy_that_says_no(tmp_path, objects_and_release):
    objects, releases, ref = objects_and_release
    backend = Model(answer={"no": 0.99, "yes": 0.01})
    bound = bind(objects, releases, ref, model_worker(objects, backend), backend)
    rt = stop_runtime(tmp_path / "rt")
    call = StopGuard(rt, "s1", checker=lambda: (True, ""), policy=bound.stop)("on_finish", ctx(), 1)
    (row,) = rows(rt)
    assert call.stop and (row["proposed"], row["action"], row["executor"]) == ("no", "yes", "guard")


def test_a_no_on_the_last_turn_is_overruled_as_the_turn_cap(tmp_path, objects_and_release):
    objects, releases, ref = objects_and_release
    backend = Model(answer={"no": 0.99, "yes": 0.01})
    bound = bind(objects, releases, ref, model_worker(objects, backend), backend)
    rt = stop_runtime(tmp_path / "rt")
    call = StopGuard(rt, "s1", max_turns=4, policy=bound.stop)("on_finish", ctx(step=4), 4)
    assert call.stop and [(o["asked_for"], o["reason"]) for o in overrides(rt)] == [("no", "max_turns")]
    assert [(r["proposed"], r["action"], r["executor"]) for r in rows(rt)] == [("no", "no", "policy")]


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


# ---------------------------------------------------------------------------- a bound policy on the fixture domain
class Model:
    """A stop model that answers from the view it is shown: "no" while the requested phrase is not in any tool
    result yet. It never sees a rubric; it reads the same text the trace records."""
    name = "stop-model"

    def __init__(self, model="m-1", answer=None, fail=False):
        self.model, self.answer, self.fail = model, answer, fail
        self.views: list[str] = []

    def decide(self, requests: list[Request]) -> list[Answer]:
        if self.fail:
            raise TimeoutError("model server timed out")
        out = []
        for r in requests:
            self.views.append(r.state)
            if self.answer == "illegal":      # a distribution over classes nobody declared
                out.append(Answer({"maybe": 1.0}, self.model))
                continue
            if self.answer is not None:
                out.append(Answer.of(r.question.labels(), dict(self.answer), self.model))
                continue
            wants = "stage updated" in r.state.split("tool calls")[0]
            posted = "postMessage" in r.state.split("tool calls and what they returned")[-1]
            done = posted or not wants
            out.append(Answer.of(r.question.labels(), {"yes": 0.95, "no": 0.05} if done
                                 else {"yes": 0.05, "no": 0.95}, self.model))
        return out


class Serving:
    def __init__(self, backend):
        self._backend = backend

    def backend(self, spec):
        return self._backend


def model_worker(objects, backend, *, threshold=None, source="argmax"):
    """W with worker.stop bound to a model, over the business view."""
    return P.replace_policies(objects, rsi.w0(objects), [
        P.descriptor(objects, "worker.stop", "model", {"instructions": "The task is complete",
                                                       "criteria": {"true": "done", "false": "not done"}},
                     model_ref="m_0001",
                     inference={"model_id": backend.model, "actions": {"yes": "yes", "no": "no"},
                                "threshold": threshold, "threshold_source": source})])


def bind(objects, releases, ref, worker_ref, backend=None):
    core = objects.load(ref, ct.SystemRelease)
    release = releases.bootstrap(worker_ref, core.research_ref, core.core_ref, activate=False)
    return P.bind_release(release, profile="task", authorization="bootstrap",
                          services=P.Services(objects, serving=Serving(backend) if backend else None))


@pytest.fixture
def suite(fake_upstream):
    return rsi.suite(fake_upstream)


@pytest.fixture
def splits(suite):
    return rsi.splits(suite)


@pytest.fixture
def objects_and_release(tmp_path, fake_upstream):
    s = rsi.suite(fake_upstream)
    releases, ref, objects = rsi.workspace(tmp_path / "ws", split_ref=rsi.splits(s).hash())
    return objects, releases, ref


def episode(tmp_path, suite, splits, worker_ref, provider, *, backend=None, max_pushbacks=2, task=NOTIFY):
    releases, ref, objects = rsi.workspace(tmp_path / "ws", split_ref=splits.hash())
    bound = bind(objects, releases, ref, worker_ref(objects), backend)
    rt = stop_runtime(tmp_path / "ws")
    out = play(rt, suite, splits, task, provider, bound=bound, objects=objects, max_pushbacks=max_pushbacks)
    return rt, bound, out, objects


def test_pr01_the_business_view_carries_the_tail_requirement_and_the_tool_results(tmp_path, suite, splits):
    rt, bound, out, objects = episode(tmp_path, suite, splits, rsi.w0, rsi.BusinessWorker())
    view = rows(rt)[-1]["view"]
    assert "post a message in #ops containing the phrase stage updated" in view   # past 300 characters
    assert "api_fetch" in view and "PATCH" in view
    for word in ("rubric", "assertion", "expected", "excluded"):
        assert word not in view.lower()
    # the gap the loop starts from: W0 updates the record and never notifies, and the label of its stop says so
    assert [name for name, _ in out["calls"]] == ["api_fetch"]
    finish = rows(rt, "on_finish")[-1]["id"]
    assert [r.outcomes[0]["label"] for r in rt.ledger.rows("stop") if r.decision["id"] == finish] == ["no"]


def test_pr02_a_model_that_says_no_sends_the_agent_back_and_the_task_then_ends(tmp_path, suite, splits):
    backend = Model()
    rt, bound, out, objects = episode(tmp_path, suite, splits, lambda o: model_worker(o, backend),
                                      rsi.BusinessWorker(), backend=backend)
    assert [(d["proposed"], d["action"], d["executor"]) for d in rows(rt, "on_finish")] == [
        ("no", "no", "policy"), ("yes", "yes", "policy")]
    per_turn = rows(rt, "per_turn")
    assert per_turn and all(d["proposed"] is None and d["executor"] == "default" for d in per_turn)
    assert [args["url"] for _, args in out["calls"]] == [rsi.HUBSPOT, rsi.SLACK] and out["pushbacks"] == 1
    traces = [objects.load(r, ct.DecisionTrace) for r in out["traces"]]
    assert any(t.proposed_action == "no" and t.executed_action == "no" for t in traces)
    assert all(t.input_schema == "stop.business.v1" for t in traces)
    activity = bound.activity.to_json()
    assert activity["calls"]["worker.stop"] == 2 and activity["proposed"]["worker.stop"] == {"no": 1, "yes": 1}


def test_pr03_the_guard_overrules_a_policy_that_never_agrees(tmp_path, suite, splits):
    backend = Model(answer={"no": 0.99, "yes": 0.01})
    rt, bound, out, objects = episode(tmp_path, suite, splits, lambda o: model_worker(o, backend),
                                      rsi.BusinessWorker(ignore_pushback=True, ignore_finish_check=True),
                                      backend=backend, max_pushbacks=1)
    found = overrides(rt)
    assert found and found[-1]["asked_for"] == "no" and found[-1]["executed"] == "yes"
    assert found[-1]["reason"] in ("max_pushbacks", "max_turns")
    assert bound.activity.to_json()["overrides"]["worker.stop"]


def test_pr03_a_service_failure_is_recorded_as_a_fallback_not_as_agreement(tmp_path, suite, splits):
    rt, bound, out, objects = episode(tmp_path, suite, splits, lambda o: model_worker(o, Model(fail=True)),
                                      rsi.BusinessWorker(), backend=Model(fail=True))
    stops = rows(rt, "on_finish")
    assert stops[-1]["proposed"] == "abstain" and stops[-1]["action"] == "yes"
    assert "unavailable" in stops[-1]["fallback_reason"]
    assert bound.activity.to_json()["fallbacks"]["worker.stop"]


def test_er03_an_illegal_action_is_the_candidates_defect_and_its_row_says_so(tmp_path, suite, splits):
    backend = Model(answer="illegal")
    with pytest.raises(ct.CandidateDefect, match="not a distribution"):
        episode(tmp_path, suite, splits, lambda o: model_worker(o, backend), rsi.BusinessWorker(), backend=backend)
    rt = stop_runtime(tmp_path / "ws")
    defects = [r.decision for r in rt.ledger.rows("stop") if r.decision.get("defect")]
    assert defects and defects[0]["route"] == "error" and defects[0]["action"] is None


def test_inspection_reports_what_ran_and_hides_the_grader(tmp_path, suite, splits):
    backend = Model()
    rt, bound, out, objects = episode(tmp_path, suite, splits, lambda o: model_worker(o, backend),
                                      rsi.BusinessWorker(), backend=backend)
    ins = inspect(bound, tuple(out["traces"]), objects=objects, base_prompt="BASE")
    assert ins.worker["worker.stop"]["observed"] and ins.worker["worker.stop"]["calls"] == 2
    assert ins.coverage["stop.business.v1"]["present"]["task_text"] == 1.0
    assert ins.coverage["stop.business.v1"]["present"]["tool_events"] > 0
    assert not ins.notes
    shown = json.dumps(public(ins))
    for word in ("rubric", "assertion", "world", "scorer"):
        assert word not in shown
