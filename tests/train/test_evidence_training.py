"""M2 acceptance (docs/modules/02_EVIDENCE_TRAINING.md §6): feedback that only observed facts produce, family
grouping, re-rendering from what was stored, and training with no promotion side effect.

The stop decisions these build from are played by stop_fixtures.play, a host that asks the stop point: the runner
shell asks none, and the v1 loop that did was removed in P1 step 5. The task results beside them are real attempts
under the runner shell (rsi.collect)."""
import json

import pytest
import rsi
from stop_fixtures import play, stop_runtime
from train_fixtures import PhraseTrainer, UnknownJobTrainer

from ouroloop import policies as P
from ouroloop.evolve import contracts as ct
from ouroloop.train.dataset import build_task_dataset, check_lineage
from ouroloop.evolve.execution import Budgets, Execution
from ouroloop.train.feedback import COMPLETION_SNAPSHOT, build_feedback
from ouroloop.train.train import train_artifact
from ouroloop.evolve.task_eval import make_agent_factory, run_attempt
from ouroloop.train.views import business_stop_point
from ouroloop.research.context import AccessDenied, make_context, task_results_summary


@pytest.fixture
def suite(fake_upstream):
    return rsi.suite(fake_upstream)


@pytest.fixture
def splits(suite):
    return rsi.splits(suite)


def gather(root, suite, splits, *, tasks=None, repeats=1, worker=None, provider=None, probe=True):
    """The tasks' attempts under the runner shell, and the same tasks played with their stop decisions labelled by
    the completion probe; out["rt"] is the general runtime over the workspace those decisions are on."""
    objects = ct.Objects(root)
    tasks = tasks or ["rsi.notify_after_update"]
    out = rsi.collect(root, suite, splits, worker_ref=worker or rsi.w1(objects), objects=objects,
                      tasks=tasks, repeats=repeats, probe=probe, provider=provider)
    rt = stop_runtime(root)
    for task in tasks:
        for i in range(repeats):
            play(rt, suite, splits, task, provider or rsi.BusinessWorker(), bound=out["bound"], objects=objects,
                 probe=probe, attempt=i)
    out["rt"] = rt
    feedback = build_feedback(rt, objects=objects, scope_id="round-1")
    return out, feedback


def dataset(out, feedback, *, point=None, **kw):
    return build_task_dataset(out["rt"], point or business_stop_point(), feedback,
                              out_dir=out["rt"].root / "data", objects=out["objects"], **kw)


# ---------------------------------------------------------------------------- DT-01
def test_dt01_every_session_and_variant_of_one_task_is_one_family(tmp_path, suite, splits):
    out, feedback = gather(tmp_path / "ws", suite, splits,
                           tasks=["rsi.notify_after_update", "rsi.notify_after_update_variant"], repeats=5)
    assert len({r.family_id for r in feedback.valid()}) == 1
    built = dataset(out, feedback, splits=splits, validation=0.0, min_groups=1)
    assert built.groups["fit"] == ["rsi.notify_after_update"]
    # the sample count and the count of independent groups are reported separately
    assert built.stats["rows"]["fit"] > 5 and built.stats["tasks"] == 2
    assert len(built.manifest.fit_groups) == 1


def test_a_family_cap_keeps_one_chatty_task_from_outvoting_the_others(tmp_path, suite, splits):
    out, feedback = gather(tmp_path / "ws", suite, splits,
                           tasks=["rsi.notify_after_update"], repeats=4)
    built = dataset(out, feedback, splits=splits, validation=0.0, min_groups=1, max_per_task=2)
    assert built.stats["rows"]["fit"] == 2 and built.exclusions["over_task_cap"] >= 1
    assert built.stats["max_per_task"] == 2


# ---------------------------------------------------------------------------- DT-02
def test_dt02_one_episode_yields_a_no_before_the_step_and_a_yes_after_it(tmp_path, suite, splits):
    out, feedback = gather(tmp_path / "ws", suite, splits, tasks=["rsi.notify_after_update"])
    labels = [tuple(r.target)[0] for r in feedback.valid()]
    assert "no" in labels and "yes" in labels
    assert all(r.source == "env" for r in feedback.valid())
    # the online input carries no rubric, no assertion and no world state
    views = [r.decision["view"] for r in out["rt"].ledger.rows("stop")]
    assert views and all("rubric" not in v and "assertion" not in v and "expected_state" not in v for v in views)


def test_dt02_the_probe_is_refused_outside_the_train_split(tmp_path, suite, splits):
    objects = ct.Objects(tmp_path / "ws")
    releases, release, _ = rsi.workspace(tmp_path / "ws", split_ref=splits.hash(), worker_ref=rsi.w1(objects))
    bound = P.bind_release(release, profile="task", services=P.Services(objects))
    manifest = rsi.run_manifest("W1", "base", "dev", data=splits.hash())
    factory = make_agent_factory(rsi.ws(tmp_path / "ws"), rsi.BusinessWorker(), budget=manifest.budget, bound=bound)
    with pytest.raises(ValueError, match="only the train split"):
        run_attempt(suite, "rsi.dev_notify_after_update", factory, run=manifest, splits=splits, probe=True)
    with pytest.raises(ValueError, match="only the train split"):
        play(stop_runtime(tmp_path / "ws"), suite, splits, "rsi.dev_notify_after_update", rsi.BusinessWorker())


# ---------------------------------------------------------------------------- DT-03
def test_dt03_a_repeated_label_is_deduplicated_and_a_correction_supersedes_it(tmp_path, suite, splits):
    out, feedback = gather(tmp_path / "ws", suite, splits)
    rt, objects = out["rt"], out["objects"]
    decision = feedback.valid()[0].decision_id
    before = len(build_feedback(rt, objects=objects).valid())
    rt.outcome(decision, tuple(feedback.valid()[0].target)[0], source="env", note="completion probe")
    assert len(build_feedback(rt, objects=objects).valid()) == before      # relabelling the same state adds nothing
    # a human disagreeing without superseding is a conflict: both are kept, neither is trained on
    label = tuple(feedback.valid()[0].target)[0]
    rt.outcome(decision, "no" if label == "yes" else "yes", source="human", note="review")
    conflicted = build_feedback(rt, objects=objects)
    record = next(r for r in conflicted.records if r.decision_id == decision)
    assert record.status == "conflicted" and not record.target
    built = dataset(out, conflicted, splits=splits, validation=0.0, min_groups=1)
    assert built.exclusions["status:conflicted"] == 1
    # and a correction that says it supersedes the earlier label wins
    rt.outcome(decision, "no" if label == "yes" else "yes", source="human", note="review",
               supersedes="completion_snapshot@v1")
    resolved = next(r for r in build_feedback(rt, objects=objects).records if r.decision_id == decision)
    assert resolved.status == "valid" and resolved.source == "human" and resolved.supersedes


def test_a_judge_or_a_gate_verdict_never_becomes_a_label(tmp_path, suite, splits):
    out, feedback = gather(tmp_path / "ws", suite, splits)
    rt, objects = out["rt"], out["objects"]
    fresh = [r for r in rt.ledger.rows("stop") if not r.outcomes]
    target = fresh[0].decision["id"] if fresh else feedback.records[-1].decision_id
    rt.ledger.outcome(target, "yes", source="gate", note="promotion gate")
    rt.ledger.outcome(target, "yes", source="frontier_llm", note="a model's own opinion")
    again = build_feedback(rt, objects=objects)
    assert all(r.source in ("env", "human") for r in again.records)
    if fresh:
        assert not any(r.decision_id == target for r in again.valid())


# ---------------------------------------------------------------------------- DT-04
def test_dt04_a_view_that_needs_a_field_nobody_stored_is_not_rendered_from_the_future(tmp_path, suite, splits):
    out, feedback = gather(tmp_path / "ws", suite, splits, tasks=["rsi.policy_hold"])
    point = business_stop_point()
    point.spec.view_params["requires"] = ["tool_events"]      # this task makes no tool call at all
    built = dataset(out, feedback, point=point, splits=splits, validation=0.0, min_groups=1)
    assert built.status == "insufficient_data"
    assert any(k.startswith("missing_observation_field") for k in built.exclusions)
    assert not (out["rt"].root / "data" / "train.jsonl").exists()


# ---------------------------------------------------------------------------- DT-05
def test_dt05_evidence_from_another_partition_never_reaches_a_training_file(tmp_path, suite, splits):
    out, feedback = gather(tmp_path / "ws", suite, splits, tasks=["rsi.notify_after_update"])
    objects = out["objects"]
    final = objects.put(ct.EvidenceRef(id="final-1", kind="summary", scope_id="round-1", partition="final",
                                       producer_ref="llm", content_ref=objects.put_json("change", {"s": 1})))
    feedback.records.append(ct.FeedbackRecord(
        decision_id=feedback.valid()[0].decision_id, task_id="rsi.final_notify_after_update",
        family_id="rsi.final_notify_after_update", source="env", feedback_ref=COMPLETION_SNAPSHOT.ref,
        target={"yes": 1.0}, evidence_refs=(final,), input_schema="stop.business.v1", status="valid"))
    assert check_lineage(feedback, ("train",), objects=objects)
    built = dataset(out, feedback, splits=splits, validation=0.0, min_groups=1)
    assert built.status == "invalid" and built.exclusions["outside_data_boundary"]
    assert not (out["rt"].root / "data" / "train.jsonl").exists()


def test_dt05_a_dev_task_label_is_excluded_by_partition(tmp_path, suite, splits):
    out, feedback = gather(tmp_path / "ws", suite, splits, tasks=["rsi.notify_after_update"])
    rt, objects = out["rt"], out["objects"]
    # a decision recorded in a dev session, labelled by hand: the builder refuses it on the partition alone
    rt.ledger.append({"type": "session", "session": "s_dev", "task": "rsi.dev_notify_after_update",
                      "family": "rsi.dev_notify_after_update", "split": "dev", "held_out": True})
    did = rt.ledger.decision(session="s_dev", point="stop", ctx="s_dev#0", phase="on_finish")
    rt.ledger.outcome(did, "yes", source="human", note="by hand")
    again = build_feedback(rt, objects=objects)
    assert again.excluded.get("partition:dev") == 1
    assert not any(r.decision_id == did for r in again.records)


def test_the_research_context_refuses_final_evidence_and_hides_the_grader(tmp_path, suite, splits):
    out, feedback = gather(tmp_path / "ws", suite, splits)
    objects = out["objects"]
    final = objects.put(ct.EvidenceRef(id="f1", kind="task_result", scope_id="s", partition="final",
                                       producer_ref="p", content_ref=objects.put_json("change", {})))
    with pytest.raises(AccessDenied):
        make_context("round-1", goal="close the gap", release_ref=out["release"], evidence_refs=(final,),
                     objects=objects)
    context = make_context("round-1", goal="close the gap", release_ref=out["release"],
                           evidence_refs=tuple(feedback.valid()[0].evidence_refs), objects=objects,
                           results=task_results_summary(out["results"], splits=splits),
                           feedback=feedback.summary())
    text = json.dumps(context.to_json())
    for word in ("rubric", "assertion", "world", "scorer", "secret"):
        assert word not in text
    assert context.results["tasks"] and context.feedback["valid"] >= 1


def test_the_context_shows_no_task_outside_its_partitions(tmp_path, suite, splits):
    out, feedback = gather(tmp_path / "ws", suite, splits)
    summary = task_results_summary(out["results"], partitions=("dev",), splits=splits)
    assert summary["tasks"] == {} and summary["excluded"] == len(out["results"])


# ---------------------------------------------------------------------------- DT-06 / DT-08
def test_dt06_training_produces_an_artifact_and_moves_no_pointer(tmp_path, suite, splits):
    root = tmp_path / "ws"
    out, feedback = gather(root, suite, splits, tasks=rsi.TRAIN, repeats=2)
    built = dataset(out, feedback, splits=splits, validation=0.25, min_groups=1)
    assert built.status == "ready" and built.dataset_ref
    trainer = PhraseTrainer()
    account = Budgets(root).account("experiment", {"train_jobs": 2, "training_seconds": 600})
    before = out["releases"].active()
    build = train_artifact(out["rt"], trainer, built, {}, objects=out["objects"], budget=account)
    assert build.status == "ready" and build.model_ref == "trained-1" and build.job_ref["id"].startswith("job-")
    assert trainer.jobs and trainer.jobs[0]["rows"] == built.stats["rows"]["fit"]
    assert out["releases"].active() == before                     # no pointer moved
    assert not (root / "models" / "champion.json").exists()       # no legacy champion was written
    assert not list((root / "gates").glob("*")) if (root / "gates").exists() else True
    assert account.remaining()["used"]["train_jobs"] == 1.0       # the job's cost is on the book
    artifact = out["objects"].get(build.artifact_ref)
    assert artifact["model_id"] == "trained-1" and artifact["dataset"] == built.dataset_ref


def test_dt06_a_failed_job_is_not_ready_and_still_costs(tmp_path, suite, splits):
    root = tmp_path / "ws"
    out, feedback = gather(root, suite, splits, tasks=rsi.TRAIN, repeats=2)
    built = dataset(out, feedback, splits=splits, validation=0.25, min_groups=1)
    account = Budgets(root).account("experiment", {"train_jobs": 2, "training_seconds": 600})
    build = train_artifact(out["rt"], PhraseTrainer(fail=True), built, {}, objects=out["objects"],
                           budget=account)
    assert build.status == "failed" and build.model_ref is None and "CUDA" in build.errors[0]
    assert account.remaining()["used"]["train_jobs"] == 1.0


def test_a_model_that_answers_as_another_model_fails_the_load_probe(tmp_path, suite, splits):
    root = tmp_path / "ws"
    out, feedback = gather(root, suite, splits, tasks=rsi.TRAIN, repeats=2)
    built = dataset(out, feedback, splits=splits, validation=0.25, min_groups=1)

    class Mislabelled(PhraseTrainer):
        def train(self, job):
            result = super().train(job)
            return type(result)("another-model", result.backend, result.job)

    build = train_artifact(out["rt"], Mislabelled(), built, {}, objects=out["objects"])
    assert build.status == "failed" and "answers as" in build.errors[0]


def test_dt08_an_unreadable_remote_job_is_queried_not_resubmitted(tmp_path, suite, splits):
    root = tmp_path / "ws"
    out, feedback = gather(root, suite, splits, tasks=rsi.TRAIN, repeats=2)
    built = dataset(out, feedback, splits=splits, validation=0.25, min_groups=1)
    execution = Execution(root, "exp-1")
    op = execution.operation("train", (built.dataset_ref,))
    op.attempts = ({"job_id": "job-remote-1"},)
    execution.put(op)
    trainer = UnknownJobTrainer()
    build = train_artifact(out["rt"], trainer, built, {}, objects=out["objects"], operation=op)
    assert build.status == "failed" and "still unknown" in build.errors[0]
    assert trainer.attached == ["job-remote-1"] and not trainer.jobs      # nothing was submitted twice


# ---------------------------------------------------------------------------- DT-07
def test_dt07_a_scorer_that_raises_produces_unknown_and_never_a_no(tmp_path, suite, splits, monkeypatch):
    from ouroloop.adapters import automationbench as ab

    def broken(self, world=None):
        raise RuntimeError("the rubric could not read this state")

    monkeypatch.setattr(ab.Episode, "score", broken)
    root = tmp_path / "ws"
    out, feedback = gather(root, suite, splits)
    assert out["results"][0].validity == "invalid" and out["results"][0].failure_class == "evaluator"
    assert feedback.records and all(r.status == "unknown" and not r.target for r in feedback.records)
    assert not feedback.valid()
    built = build_task_dataset(out["rt"], business_stop_point(), feedback, out_dir=root / "data",
                              objects=out["objects"], splits=splits, min_groups=1)
    assert built.status == "insufficient_data" and built.exclusions["status:unknown"] >= 1


def test_insufficient_groups_does_not_fall_back_to_a_row_split(tmp_path, suite, splits):
    out, feedback = gather(tmp_path / "ws", suite, splits, tasks=["rsi.notify_after_update"], repeats=3)
    built = dataset(out, feedback, splits=splits, validation=0.25, min_groups=3)
    assert built.status == "insufficient_data" and built.exclusions["insufficient_groups"]["needed"] == 3
    assert not (out["rt"].root / "data" / "train.jsonl").exists()
