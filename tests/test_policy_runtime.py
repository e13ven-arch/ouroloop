"""M1 in a real run (docs/modules/01_POLICY_RUNTIME.md §5): the business view, a bound policy proposing and being
overruled, a fallback, and what an install does to a running session."""
import json

import pytest
import rsi

from ouroloop import policies as P
from ouroloop.evolve import contracts as ct
from ouroloop.evolve.task_eval import make_agent_factory, run_attempt
from ouroloop.research.inspection import inspect, public
from ouroloop.types import Answer, Request

NOTIFY = "rsi.notify_after_update"


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


def run(tmp_path, suite, splits, worker_ref, provider, *, task=NOTIFY, serving=None, split="train",
        max_pushbacks=2, objects=None, releases=None):
    rt = rsi.runtime(tmp_path / "ws")
    objects = objects or ct.Objects(tmp_path / "ws")
    if releases is None:
        releases, _, _ = rsi.workspace(tmp_path / "ws", split_ref=splits.hash())
    ref = releases.active()["release_ref"]
    if worker_ref is not None:
        ref = releases.bootstrap(worker_ref, objects.load(ref, ct.SystemRelease).research_ref,
                                 objects.load(ref, ct.SystemRelease).core_ref, activate=False)
    bound = P.bind_release(ref, profile="task", services=P.Services(objects, serving=serving),
                           authorization="bootstrap")
    factory = make_agent_factory(rt, provider, budget=rsi.run_manifest("c", "base", split).budget,
                                 bound=bound, objects=objects)

    def make(surface, t, system):
        agent = factory(surface, t, system)
        agent.config.max_pushbacks = max_pushbacks
        agent.guard.max_pushbacks = max_pushbacks
        return agent

    manifest = rsi.run_manifest("candidate", "evolved", split, data=splits.hash(), system_release_ref=ref,
                                worker_ref=objects.load(ref, ct.SystemRelease).worker_ref,
                                research_ref=objects.load(ref, ct.SystemRelease).research_ref,
                                core_ref=objects.load(ref, ct.SystemRelease).core_ref)
    attempt = run_attempt(suite, task, make, run=manifest, splits=splits, release_ref=ref, plan_ref="plan-1")
    if attempt.episode is not None:
        attempt.episode.close()
    return rt, bound, attempt, objects


@pytest.fixture
def suite(fake_upstream):
    return rsi.suite(fake_upstream)


@pytest.fixture
def splits(suite):
    return rsi.splits(suite)


# ---------------------------------------------------------------------------- PR-01
def test_pr01_the_business_view_carries_the_tail_requirement_and_the_tool_results(tmp_path, suite, splits):
    rt, bound, attempt, objects = run(tmp_path, suite, splits, rsi.w0(ct.Objects(tmp_path / "ws")),
                                      rsi.BusinessWorker())
    rows = [r for r in rt.ledger.rows("stop")]
    assert rows, "the stop decision was recorded"
    view = rows[-1].decision["view"]
    assert "post a message in #ops containing the phrase stage updated" in view   # past 300 characters
    assert "api_fetch" in view and "PATCH" in view
    for word in ("rubric", "assertion", "expected", "excluded"):
        assert word not in view.lower()
    # the gap the loop starts from: W0 updates the record and never notifies
    assert attempt.result.success is False and attempt.result.partial_credit == 0.5


def test_the_same_task_passes_once_the_finish_check_is_bound(tmp_path, suite, splits):
    objects = ct.Objects(tmp_path / "ws")
    rt, bound, attempt, _ = run(tmp_path, suite, splits, rsi.w1(objects), rsi.BusinessWorker())
    assert attempt.result.success is True and attempt.result.partial_credit == 1.0


# ---------------------------------------------------------------------------- PR-02 / PR-03
def test_pr02_a_model_that_says_no_sends_the_agent_back_and_the_task_then_ends(tmp_path, suite, splits):
    objects = ct.Objects(tmp_path / "ws")
    backend = Model()
    rt, bound, attempt, objects = run(tmp_path, suite, splits, model_worker(objects, backend),
                                      rsi.BusinessWorker(), serving=Serving(backend), objects=objects)
    stops = [r.decision for r in rt.ledger.rows("stop") if r.decision["phase"] == "on_finish"]
    assert [(d["proposed"], d["action"], d["executor"]) for d in stops] == [("no", "no", "policy"),
                                                                           ("yes", "yes", "policy")]
    per_turn = [r.decision for r in rt.ledger.rows("stop") if r.decision["phase"] == "per_turn"]
    assert per_turn and all(d["proposed"] is None and d["executor"] == "default" for d in per_turn)
    assert attempt.result.success is True
    traces = [objects.load(r, ct.DecisionTrace) for r in _trace_refs(objects)]
    assert any(t.proposed_action == "no" and t.executed_action == "no" for t in traces)
    assert all(t.input_schema == "stop.business.v1" for t in traces)
    activity = bound.activity.to_json()
    assert activity["calls"]["worker.stop"] == 2 and activity["proposed"]["worker.stop"] == {"no": 1, "yes": 1}


def _trace_refs(objects):
    d = objects.dir / "trace"
    return [f"trace:sha256:{p.stem}" for p in sorted(d.glob("*.json"))] if d.exists() else []


def test_pr03_the_guard_overrules_a_policy_that_never_agrees(tmp_path, suite, splits):
    objects = ct.Objects(tmp_path / "ws")
    backend = Model(answer={"no": 0.99, "yes": 0.01})
    rt, bound, attempt, objects = run(tmp_path, suite, splits, model_worker(objects, backend),
                                      rsi.BusinessWorker(ignore_pushback=True, ignore_finish_check=True),
                                      serving=Serving(backend), objects=objects, max_pushbacks=1)
    overrides = [e for e in rt.ledger.events() if e.get("type") == "override"]
    assert overrides and overrides[-1]["asked_for"] == "no" and overrides[-1]["executed"] == "yes"
    assert overrides[-1]["reason"] in ("max_pushbacks", "max_turns")
    assert attempt.result.validity == "valid" and attempt.result.success is False
    assert bound.activity.to_json()["overrides"]["worker.stop"]


def test_pr03_a_service_failure_is_recorded_as_a_fallback_not_as_agreement(tmp_path, suite, splits):
    objects = ct.Objects(tmp_path / "ws")
    backend = Model(fail=True)
    rt, bound, attempt, objects = run(tmp_path, suite, splits,
                                      model_worker(objects, backend), rsi.BusinessWorker(),
                                      serving=Serving(Model(fail=True)), objects=objects)
    stops = [r.decision for r in rt.ledger.rows("stop") if r.decision["phase"] == "on_finish"]
    assert stops[-1]["proposed"] == "abstain" and stops[-1]["action"] == "yes"
    assert "unavailable" in stops[-1]["fallback_reason"]
    assert bound.activity.to_json()["fallbacks"]["worker.stop"]


def test_er03_an_illegal_action_makes_the_attempt_invalid_and_is_not_infrastructure(tmp_path, suite, splits):
    objects = ct.Objects(tmp_path / "ws")
    backend = Model(answer="illegal")
    rt, bound, attempt, objects = run(tmp_path, suite, splits, model_worker(objects, backend),
                                      rsi.BusinessWorker(), serving=Serving(backend), objects=objects)
    result = attempt.result
    assert result.validity == "invalid" and result.failure_class == "candidate_defect"
    assert "candidate" in result.invalid_reason
    assert any(r.decision.get("defect") for r in rt.ledger.rows("stop"))


# ---------------------------------------------------------------------------- PR-05
def test_pr05_an_install_reaches_only_sessions_that_start_after_it(tmp_path, suite, splits):
    root = tmp_path / "ws"
    objects = ct.Objects(root)
    releases, r0, _ = rsi.workspace(root, split_ref=splits.hash(), worker_ref=rsi.w0(objects))
    binder = P.SessionBinder(releases, P.Services(objects))
    first = binder.open("s1")
    # a second release installed while s1 is open
    core = objects.load(r0, ct.SystemRelease).core_ref
    receipt = ct.EvaluationReceipt(experiment_ref="experiment:sha256:" + "a" * 64, profile="task",
                                   subject_ref=rsi.w1(objects), control_ref=rsi.w0(objects),
                                   plan_ref=objects.put_json("change", {"plan": "dev"}),
                                   results_ref=objects.put_json("change", {"results": []}), verdict="accepted")
    receipt_ref = objects.put(receipt)
    releases.register_receipt(receipt_ref, plan=receipt.plan_ref)
    installed = releases.install(receipt_ref, r0, "op-install-1")
    assert installed.status == "installed"
    assert binder.current("s1").release_ref == first.release_ref        # the open session keeps its version
    assert binder.open("s2").release_ref == installed.target_release    # a new session gets the new one
    # revoking the version a session runs leaves that session the defaults, not whatever is active now
    releases.revoke(first.release_ref, "rolled back for the test")
    fallen = binder.current("s1")
    # the defaults release here is the one that was revoked, so the session runs the registered defaults, which
    # are not a release and are never written to the pointer
    assert fallen.authorization == "fixed_defaults" and fallen.stop.kind == "rule"
    assert "revoked" in binder.reason("s1") and releases.active()["release_ref"] == installed.target_release


def test_inspection_reports_what_ran_and_hides_the_grader(tmp_path, suite, splits):
    objects = ct.Objects(tmp_path / "ws")
    backend = Model()
    rt, bound, attempt, objects = run(tmp_path, suite, splits, model_worker(objects, backend),
                                      rsi.BusinessWorker(), serving=Serving(backend), objects=objects)
    ins = inspect(bound, tuple(_trace_refs(objects)), objects=objects, base_prompt="BASE")
    assert ins.worker["worker.stop"]["observed"] and ins.worker["worker.stop"]["calls"] == 2
    assert ins.coverage["stop.business.v1"]["present"]["task_text"] == 1.0
    assert ins.coverage["stop.business.v1"]["present"]["tool_events"] > 0
    assert not ins.notes
    shown = json.dumps(public(ins))
    for word in ("rubric", "assertion", "world", "scorer"):
        assert word not in shown


# ---------------------------------------------------------------------------- ER-09
def test_er09_a_failure_in_make_agent_or_close_is_recorded_and_closes_what_it_can(tmp_path, suite, splits):
    from ouroloop.benchmark import Results
    from ouroloop.evolve.task_eval import run_tasks
    rt = rsi.runtime(tmp_path / "ws")
    manifest = rsi.run_manifest("c", "base", "train")

    def broken_factory(surface, task, system):
        raise RuntimeError("the agent could not be built")

    attempt = run_attempt(suite, "rsi.update_stage", broken_factory, run=manifest, splits=splits)
    assert attempt.result.validity == "invalid" and attempt.result.failure_class == "infrastructure"
    assert "could not be built" in attempt.result.invalid_reason
    if attempt.episode is not None:
        attempt.episode.close()

    # a failure while closing is recorded beside the first result, not instead of it
    closed = []

    class Failing(dict):
        pass

    factory = make_agent_factory(rt, rsi.BusinessWorker(), budget=manifest.budget)
    rows: list = []

    def make(surface, task, system):
        agent = factory(surface, task, system)
        original = surface.episode.close

        def close():
            closed.append(task.task_id)
            original()
            raise RuntimeError("the episode could not be closed")

        surface.episode.close = close
        return agent

    out = run_tasks(suite, ["rsi.update_stage"], make, run=manifest, splits=splits, results=rows)
    assert out[0].validity == "valid" and closed == ["rsi.update_stage"]
    assert rows[-1].validity == "invalid" and "cleanup" in rows[-1].invalid_reason
    assert rows[0].task_id == "rsi.update_stage" and rows[0].validity == "valid"
