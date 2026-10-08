"""M1 acceptance (docs/modules/01_POLICY_RUNTIME.md §5) for the parts that need no agent run, plus the release
authorisation of CT-05 / CT-06."""
import pytest

from ouroloop import policies as P
from ouroloop.evolve import contracts as ct
from ouroloop.train.views import business_stop_view
from ouroloop.release import NotAuthorized, Releases, resolve_profile
from ouroloop.train.types import Answer, Request


class ScriptedBackend:
    """A decision backend that answers from the view it is given, so a test proves the wiring and not a name."""

    def __init__(self, name="scripted", model="m-1", answer=None):
        self.name, self.model, self.answer = name, model, answer or {"yes": 0.9, "no": 0.1}
        self.seen: list[str] = []

    def decide(self, requests: list[Request]) -> list[Answer]:
        out = []
        for r in requests:
            self.seen.append(r.state)
            out.append(Answer.of(r.question.labels(), dict(self.answer), self.model))
        return out


class Serving:
    def __init__(self, backend):
        self.backend_obj = backend

    def backend(self, spec):
        if spec.get("model") == "missing":
            raise RuntimeError("no such model in the store")
        return self.backend_obj


def model_policy(objects, *, model_ref="m_0001", model_id="m-1", threshold=None, source="argmax"):
    return P.descriptor(objects, "worker.stop", "model", {"instructions": "The task is complete",
                                                         "criteria": {"true": "done", "false": "not done"},
                                                         "view_params": {"max_task_chars": 2000}},
                        model_ref=model_ref,
                        inference={"model_id": model_id, "actions": {"yes": "yes", "no": "no"},
                                   "threshold": threshold, "threshold_source": source})


# ---------------------------------------------------------------------------- the registry
def test_only_registered_targets_and_builtins_are_accepted(tmp_path):
    objects = ct.Objects(tmp_path)
    with pytest.raises(ct.ContractError):
        P.resolve("worker.route")
    with pytest.raises(ct.ContractError):
        P.parse_builtin("research_retrieve.v1", {"expression": "score(x) > 1"})
    with pytest.raises(ct.ContractError):
        P.parse_builtin("research_preflight.v1", {"checks": ["input_coverage", "input_coverage"]})
    with pytest.raises(ct.ContractError):
        P.parse_builtin("research_preflight.v1", {"checks": ["call_the_scorer"]})
    assert P.parse_builtin("research_retrieve.v1", {"max_items": 500})["max_items"] == 100


def test_pr07_a_scoring_function_in_a_rule_config_is_refused(tmp_path):
    """PR-07: a candidate that names the scorer gets nothing executed."""
    objects = ct.Objects(tmp_path)
    with pytest.raises(ct.ContractError):
        P.descriptor(objects, "research.retrieve", "rule",
                     {"builtin": "research_retrieve.v1", "sort": "automationbench.rubric:score"})
    with pytest.raises(ct.ContractError):
        P.descriptor(objects, "worker.stop", "rule", {"builtin": "call.scorer.v1"})


def test_a_prompt_cannot_declare_an_action_vocabulary(tmp_path):
    objects = ct.Objects(tmp_path)
    with pytest.raises(ct.ContractError):
        ct.PolicyDescriptor(owner="worker", target="worker.finish", binding="config",
                            input_schema="research.context.v1", output_schema="action.stop.v1",
                            implementation={"kind": "prompt",
                                            "artifact_ref": objects.put_json("change", {"text": "x"})})


def test_a_policy_set_is_complete_not_a_diff(tmp_path):
    objects = ct.Objects(tmp_path)
    partial = ct.PolicySet(owner="worker", policies=(objects.put(
        P.descriptor(objects, "worker.stop", "rule", {"builtin": "default.v1"})),))
    v = P.validate_policy_set(partial, objects)
    assert not v.ok and any("complete set" in e for e in v.errors)


def test_replace_policies_keeps_the_other_targets(tmp_path):
    objects = ct.Objects(tmp_path)
    w0 = P.default_worker_set(objects)
    w1 = P.replace_policies(objects, w0, [P.descriptor(objects, "worker.system_prompt", "prompt",
                                                       {"text": "read the whole request"})])
    a = P.validate_policy_set(objects.load(w0, ct.PolicySet), objects).policies
    b = P.validate_policy_set(objects.load(w1, ct.PolicySet), objects).policies
    assert b["worker.system_prompt"].ref() != a["worker.system_prompt"].ref()
    assert b["worker.stop"].ref() == a["worker.stop"].ref()
    assert objects.load(w1, ct.PolicySet).parent_ref == w0


def test_a_worker_set_refuses_a_research_policy(tmp_path):
    objects = ct.Objects(tmp_path)
    w0 = P.default_worker_set(objects)
    with pytest.raises(ct.ContractError):
        P.replace_policies(objects, w0, [P.descriptor(objects, "research.propose", "prompt", {"text": "x"})])


# ---------------------------------------------------------------------------- binding
def test_the_prompt_is_composed_in_a_fixed_order_and_hashed(tmp_path):
    objects = ct.Objects(tmp_path)
    releases, rel = P.bootstrap_workspace(tmp_path, objects=objects)
    bound = P.bind_release(rel, profile="task", services=P.Services(objects))
    final, record = bound.system_prompt("BASE PROMPT")
    assert final.startswith("BASE PROMPT") and P.FINISH_CHECK in final
    assert record["parts"] == ["worker.finish"] and len(record["hash"]) == 16
    again = P.bind_release(rel, profile="task", services=P.Services(objects)).system_prompt("BASE PROMPT")[1]
    assert again["hash"] == record["hash"]


def test_a_model_policy_proposes_and_abstains_below_its_threshold(tmp_path):
    objects = ct.Objects(tmp_path)
    backend = ScriptedBackend(answer={"yes": 0.55, "no": 0.45})
    services = P.Services(objects, serving=Serving(backend))
    w = P.replace_policies(objects, P.default_worker_set(objects),
                           [model_policy(objects, threshold=0.8, source="calibration:v1")])
    releases, rel = P.bootstrap_workspace(tmp_path, objects=objects, worker_ref=w)
    bound = P.bind_release(rel, profile="task", services=services)
    action, why = bound.stop.propose({"task_text": "requirements", "final_message": "done", "tool_events": []})
    assert action == "abstain" and "below" in why
    assert "task requirements:" in backend.seen[-1] and "rubric" not in backend.seen[-1]


def test_pr04_a_model_that_answers_as_another_model_fails_preflight(tmp_path):
    """PR-04: the configuration says A, serving answers B."""
    objects = ct.Objects(tmp_path)
    services = P.Services(objects, serving=Serving(ScriptedBackend(model="m-2")))
    w = P.replace_policies(objects, P.default_worker_set(objects), [model_policy(objects, model_id="m-1")])
    releases, rel = P.bootstrap_workspace(tmp_path, objects=objects, worker_ref=w)
    with pytest.raises(P.BindError, match="answers as"):
        P.bind_release(rel, profile="task", services=services)


def test_a_model_that_cannot_load_is_never_swapped_for_the_default(tmp_path):
    objects = ct.Objects(tmp_path)
    services = P.Services(objects, serving=Serving(ScriptedBackend()))
    w = P.replace_policies(objects, P.default_worker_set(objects), [model_policy(objects, model_ref="missing")])
    releases, rel = P.bootstrap_workspace(tmp_path, objects=objects, worker_ref=w)
    with pytest.raises(P.BindError, match="could not be loaded"):
        P.bind_release(rel, profile="task", services=services)


def test_a_serving_failure_at_ask_time_is_a_declared_fallback(tmp_path):
    class Broken(ScriptedBackend):
        def decide(self, requests):
            raise TimeoutError("model server timed out")

    objects = ct.Objects(tmp_path)
    services = P.Services(objects, serving=Serving(Broken()), probe_models=False)
    w = P.replace_policies(objects, P.default_worker_set(objects), [model_policy(objects)])
    releases, rel = P.bootstrap_workspace(tmp_path, objects=objects, worker_ref=w)
    bound = P.bind_release(rel, profile="task", services=services)
    action, why = bound.stop.propose({"task_text": "t", "tool_events": []})
    assert action == "abstain" and "unavailable" in why


def test_a_threshold_needs_a_source(tmp_path):
    objects = ct.Objects(tmp_path)
    with pytest.raises(ct.ContractError, match="source"):
        model_policy(objects, threshold=0.7, source="argmax")


# ---------------------------------------------------------------------------- PR-01 (the view)
def test_pr01_a_requirement_past_the_first_300_characters_is_not_lost():
    task = "Update the record. " * 20 + "Finally, notify the account owner."
    ctx = {"task_text": task, "final_message": "updated", "phase": "on_finish", "step": 4,
           "tool_events": [{"step": 1, "name": "find_records", "args": {"q": "acme"},
                            "result_excerpt": "1 row"},
                           {"step": 2, "name": "update_record", "args": {"id": 7}, "result_excerpt": "ok"}],
           "budget_remaining": {"steps": 30}}
    view = business_stop_view(ctx, {})
    assert "notify the account owner" in view
    assert "update_record" in view and "1 row" in view
    for word in ("rubric", "assertion", "expected_state", "score"):
        assert word not in view
    short = business_stop_view(ctx, {"max_task_chars": 120})
    assert "not shown" in short and "dropped_chars" in short


def test_the_business_view_refuses_unknown_parameters():
    with pytest.raises(ValueError, match="does not take"):
        business_stop_view({"task_text": "x"}, {"include_rubric": True})


# ---------------------------------------------------------------------------- CT-05 / CT-06
def test_ct05_a_workspace_with_both_pointers_needs_an_explicit_profile(tmp_path):
    objects = ct.Objects(tmp_path)
    P.bootstrap_workspace(tmp_path, objects=objects)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "champion.json").write_text('{"version": "m_0001", "history": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="name the profile"):
        resolve_profile(tmp_path, None)
    assert resolve_profile(tmp_path, "task") == "task"
    assert resolve_profile(tmp_path, "legacy") == "legacy"


def test_ct06_a_permit_evaluates_but_never_installs_or_opens_a_session(tmp_path):
    objects = ct.Objects(tmp_path)
    releases, parent = P.bootstrap_workspace(tmp_path, objects=objects)
    candidate = P.replace_policies(objects, objects.load(parent, ct.SystemRelease).worker_ref,
                                   [P.descriptor(objects, "worker.system_prompt", "prompt", {"text": "check all"})])
    plan_ref = objects.put_json("change", {"plan": "dev-1"})
    permit = releases.permit(candidate, parent, plan_ref, "scope-1",
                             objects.load(parent, ct.SystemRelease).core_ref)
    arm = releases.candidate_release(parent, candidate, permit)
    assert releases.authorize(arm, profile="evaluation", plan_ref=plan_ref,
                              scope_id="scope-1") == "evaluation_permit"
    with pytest.raises(NotAuthorized, match="another permit|plan"):
        releases.authorize(arm, profile="evaluation", plan_ref=objects.put_json("change", {"plan": "other"}))
    with pytest.raises(NotAuthorized, match="may not open a normal session"):
        releases.authorize(arm, profile="task")
    receipt = ct.EvaluationReceipt(experiment_ref="experiment:sha256:" + "a" * 64, profile="task",
                                   subject_ref=candidate, control_ref=candidate, plan_ref=plan_ref,
                                   results_ref="change:sha256:" + "b" * 64, verdict="accepted")
    with pytest.raises(NotAuthorized, match="not registered"):
        releases.install(objects.put(receipt), parent, "op-1")
    assert releases.active()["release_ref"] == parent
    # and an evaluation arm is bindable, which is the point of a permit
    bound = P.bind_release(arm, profile="evaluation", services=P.Services(objects), authorization="permit")
    assert "check all" in bound.system_prompt("BASE")[0]
