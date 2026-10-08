"""M0 acceptance (docs/modules/00_CONTRACTS.md §5) and the v2 run/result fields."""
import json

import pytest

from ouroloop import policies as P
from ouroloop.benchmark import RunManifest, TaskResult
from ouroloop.evolve import contracts as ct


def prompt_policy(objects, text):
    return P.descriptor(objects, "worker.system_prompt", "prompt", {"text": text})


# ---------------------------------------------------------------------------- CT-01
def test_ct01_key_order_is_the_same_object_a_changed_word_is_not(tmp_path):
    objects = ct.Objects(tmp_path)
    a = ct.PolicyDescriptor(owner="worker", target="worker.system_prompt", binding="config",
                            input_schema="research.context.v1", output_schema="prompt.v1",
                            implementation={"kind": "prompt", "artifact_ref": objects.put_json("change", {"text": "x"})},
                            limits={"a": 1, "b": 2})
    b = ct.PolicyDescriptor(owner="worker", target="worker.system_prompt", binding="config",
                            input_schema="research.context.v1", output_schema="prompt.v1",
                            implementation={"artifact_ref": a.implementation["artifact_ref"], "kind": "prompt"},
                            limits={"b": 2, "a": 1})
    assert a.ref() == b.ref()
    one = objects.put_json("change", {"text": "notify the owner"})
    two = objects.put_json("change", {"text": "notify the owners"})
    assert one != two


def test_ct01_nan_and_infinity_are_refused(tmp_path):
    objects = ct.Objects(tmp_path)
    with pytest.raises(ct.ContractError):
        objects.put_json("change", {"threshold": float("nan")})
    with pytest.raises(ct.ContractError):
        ct.canonical({"x": float("inf")})


def test_ct01_a_ref_must_hash_to_its_contents(tmp_path):
    objects = ct.Objects(tmp_path)
    ref = objects.put_json("change", {"text": "original"})
    objects.path(ref).write_text(json.dumps({"text": "tampered"}), encoding="utf-8")
    with pytest.raises(ct.ContractError):
        objects.get(ref)


def test_ct01_volatile_fields_do_not_change_identity():
    a = ct.Requirement(id="r1", context="c", expected_behavior="e", observed_behavior="o", notes="first look")
    b = ct.Requirement(id="r1", context="c", expected_behavior="e", observed_behavior="o", notes="rewritten")
    assert a.ref() == b.ref()


# ---------------------------------------------------------------------------- CT-02
def test_ct02_old_records_keep_their_hash_and_cannot_pose_as_v2():
    old = {"candidate": "base", "role": "base", "split": "dev", "model": {"provider": "p", "model": "m"},
           "prompt_hash": "abc", "decisions": {}, "model_ref": None, "policy": "default",
           "environment": {"upstream": "c"}, "budget": {"max_steps": 10, "max_tool_calls": 20,
                                                        "max_seconds": 30.0, "max_usd": None},
           "data": "d", "repeats": 1}
    run = RunManifest.from_json(old)
    assert run.hash() == RunManifest(**{k: v for k, v in old.items() if k != "budget"},
                                     budget=run.budget).hash()
    assert not run.v2()
    assert "system_release_ref" not in run.payload()
    v2 = RunManifest.from_json({**old, "system_release_ref": "release:sha256:" + "a" * 64,
                                "worker_ref": "policyset:sha256:" + "b" * 64,
                                "research_ref": "policyset:sha256:" + "c" * 64,
                                "core_ref": "core:sha256:" + "d" * 64})
    assert v2.v2() and v2.hash() != run.hash()


def test_ct02_old_task_results_still_read():
    old = {"task_id": "t1", "family_id": "f1", "split": "dev", "attempt": 0, "candidate": "base", "run": "r",
           "manifest": "m", "success": False, "partial_credit": 0.5, "termination": "finished"}
    res = TaskResult.from_json(old)
    assert res.attempt_id == "" and res.pair_key == "" and res.observation_refs == []


# ---------------------------------------------------------------------------- CT-03
def test_ct03_owner_and_schema_are_checked_before_anything_runs(tmp_path):
    objects = ct.Objects(tmp_path)
    research_only = P.descriptor(objects, "worker.system_prompt", "prompt", {"text": "x"}, owner="research")
    ps = ct.PolicySet(owner="research", policies=(objects.put(research_only),))
    v = P.validate_policy_set(ps, objects)
    assert not v.ok and any("worker target" in e for e in v.errors)
    with pytest.raises(ct.ContractError):
        ct.PolicyDescriptor(owner="worker", target="worker.system_prompt", binding="config",
                            input_schema="research.context.v1", output_schema="action.stop.v1",
                            implementation={"kind": "prompt", "artifact_ref": objects.put_json("change", {})})


def test_ct03_unknown_schemas_and_implementations_are_refused(tmp_path):
    objects = ct.Objects(tmp_path)
    with pytest.raises(ct.ContractError):
        ct.PolicyDescriptor(owner="worker", target="worker.stop", binding="decision",
                            input_schema="stop.invented.v9", output_schema="action.stop.v1",
                            implementation={"kind": "rule", "builtin": "default.v1",
                                            "config_ref": objects.put_json("change", {})})
    with pytest.raises(ct.ContractError):
        ct.check_implementation({"kind": "python", "code": "print(1)"})
    with pytest.raises(ct.ContractError):
        ct.check_implementation({"kind": "rule", "builtin": "default.v1"})


# ---------------------------------------------------------------------------- CT-04
def test_ct04_a_summary_of_final_evidence_is_still_final_evidence():
    final = ct.EvidenceRef(id="e1", kind="task_result", scope_id="s", partition="final", producer_ref="p",
                           content_ref="change:sha256:" + "a" * 64)
    train = ct.EvidenceRef(id="e2", kind="task_result", scope_id="s", partition="train", producer_ref="p",
                           content_ref="change:sha256:" + "b" * 64)
    assert ct.combine(final.partition, train.partition) == "final"
    summary = ct.EvidenceRef(id="e3", kind="summary", scope_id="s",
                             partition=ct.combine(final.partition, train.partition), producer_ref="llm",
                             content_ref="change:sha256:" + "c" * 64, parents=(final.id, train.id))
    assert summary.partition == "final"
    assert not ct.visible(summary.partition, ("train", "dev"))
    with pytest.raises(ct.ContractError):
        ct.EvidenceRef(id="e4", kind="summary", scope_id="s", partition="whatever", producer_ref="p",
                       content_ref="change:sha256:" + "d" * 64)


def test_dataset_groups_must_be_disjoint():
    with pytest.raises(ct.ContractError):
        ct.DataManifest(fit_groups=("f1", "f2"), validation_groups=("f2",))


# ---------------------------------------------------------------------------- objects and events
def test_objects_are_written_once_and_read_back(tmp_path):
    objects = ct.Objects(tmp_path)
    ref = objects.put_json("change", {"text": "x"})
    path = objects.path(ref)
    before = path.stat().st_mtime_ns
    assert objects.put_json("change", {"text": "x"}) == ref and path.stat().st_mtime_ns == before
    assert objects.get(ref) == {"text": "x"}
    with pytest.raises(KeyError):
        objects.get("change:sha256:" + "0" * 64)


def test_an_embedded_records_own_ref_is_not_a_reference(tmp_path):
    """A plan embeds its gate whole, own ref included, and never stores the gate: copying the plan's closure does
    not go looking for it. A field named ref that points at another object is still followed."""
    from ouroloop.evolve.task_gate import GatePlan, TaskEvalPlan
    source, target = ct.Objects(tmp_path / "a"), ct.Objects(tmp_path / "b")
    gate = GatePlan(min_groups=1)
    plan_ref = source.put(TaskEvalPlan(task_ids=("t1",), gate_plan=gate.to_json()))
    assert source.get(plan_ref)["gate_plan"]["ref"] == gate.ref() and not source.has(gate.ref())
    assert gate.ref() not in ct.refs_in(source.get(plan_ref))
    receipt_ref = source.put(ct.EvaluationReceipt(plan_ref=plan_ref))
    authorization_ref = source.put(ct.Authorization(kind="accepted", ref=receipt_ref))
    assert ct.copy_closure(source, target, [authorization_ref]) == {authorization_ref, receipt_ref, plan_ref}
    assert target.load(plan_ref, TaskEvalPlan).gate().ref() == gate.ref()
    assert ct.refs_in({"gate": {"ref": receipt_ref, "min_gain": 0.05}}) == {receipt_ref}


def test_events_are_append_only(tmp_path):
    events = ct.Events(tmp_path)
    events.append("installed", release="release:sha256:" + "a" * 64)
    events.append("revoked", target="release:sha256:" + "a" * 64, reason="because")
    assert [e["event"] for e in events.all()] == ["installed", "revoked"]
    assert [e["reason"] for e in events.of("revoked")] == ["because"]


def test_build_result_cannot_hide_an_error():
    with pytest.raises(ct.ContractError):
        ct.BuildResult(status="invalid")
    with pytest.raises(ct.ContractError):
        ct.BuildResult(status="ready")
