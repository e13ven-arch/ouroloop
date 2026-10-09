"""The training stack's evidence and training (docs/design/V2_TRAINING_PIPELINE.md): feedback that only observed facts
produce, family grouping, re-rendering from what was stored, the data boundary, and training with no promotion side
effect.

The task results are real attempts of an rsi activity under the runner shell (the stirrup seed in a ProcessWorker,
evaluate.attempt); the stop decisions the feedback is built from are played by stop_fixtures.play, a host that asks the
stop point (the runner shell asks none), with the seed's system prompt."""
import pytest
import rsi
from stop_fixtures import play, stop_runtime
from train_fixtures import PhraseTrainer, UnknownJobTrainer

from ouroloop.activity.phases import run_arm
from ouroloop.evaluate.budget import Budgets, Execution
from ouroloop.runtime.runner import compose_system
from ouroloop.train import records
from ouroloop.train.dataset import build_task_dataset, check_lineage
from ouroloop.train.feedback import COMPLETION_SNAPSHOT, build_feedback
from ouroloop.train.train import train_artifact
from ouroloop.train.views import business_stop_point


def gather(root, upstream, *, tasks=None, repeats=1, provider=None, probe=True):
    """The tasks' attempts under the runner shell, and the same tasks played with their stop decisions labelled by the
    completion probe; out["rt"] is the general runtime over the activity's workspace."""
    act = rsi.activity(root, upstream, provider=provider or rsi.BusinessWorker())
    tasks = tasks or ["rsi.notify_after_update"]
    release = act.active_release()
    results = run_arm(act, release=release, split="train", candidate="W0", role="base",
                      pairs=[(t, i) for t in tasks for i in range(repeats)], account=act.account("baseline"))
    seed = act.package(release)
    rt = stop_runtime(act.root)
    for task in tasks:
        for i in range(repeats):
            play(rt, act.suite, act.splits, task, provider or rsi.BusinessWorker(),
                 system=lambda s: compose_system(s, seed), probe=probe, attempt=i)
    feedback = build_feedback(rt, objects=act.objects, scope_id="round-1")
    return {"activity": act, "rt": rt, "objects": act.objects, "results": results, "splits": act.splits}, feedback


def dataset(out, feedback, *, point=None, **kw):
    kw.setdefault("splits", out["splits"])
    return build_task_dataset(out["rt"], point or business_stop_point(), feedback,
                              out_dir=out["rt"].root / "data", objects=out["objects"], **kw)


# ---------------------------------------------------------------------------- DT-01
def test_dt01_every_session_and_variant_of_one_task_is_one_family(tmp_path, fake_upstream):
    out, feedback = gather(tmp_path, fake_upstream,
                           tasks=["rsi.notify_after_update", "rsi.notify_after_update_variant"], repeats=5)
    assert len(out["results"]) == 10 and all(r.validity == "valid" and r.success for r in out["results"])
    assert len({r.family_id for r in feedback.valid()}) == 1
    built = dataset(out, feedback, validation=0.0, min_groups=1)
    assert built.groups["fit"] == ["rsi.notify_after_update"]
    # the sample count and the count of independent groups are reported separately
    assert built.stats["rows"]["fit"] > 5 and built.stats["tasks"] == 2
    assert len(built.manifest.fit_groups) == 1


def test_a_family_cap_keeps_one_chatty_task_from_outvoting_the_others(tmp_path, fake_upstream):
    out, feedback = gather(tmp_path, fake_upstream, tasks=["rsi.notify_after_update"], repeats=4)
    built = dataset(out, feedback, validation=0.0, min_groups=1, max_per_task=2)
    assert built.stats["rows"]["fit"] == 2 and built.exclusions["over_task_cap"] >= 1
    assert built.stats["max_per_task"] == 2


# ---------------------------------------------------------------------------- DT-02
def test_dt02_one_episode_yields_a_no_before_the_step_and_a_yes_after_it(tmp_path, fake_upstream):
    out, feedback = gather(tmp_path, fake_upstream, tasks=["rsi.notify_after_update"])
    labels = [tuple(r.target)[0] for r in feedback.valid()]
    assert "no" in labels and "yes" in labels
    assert all(r.source == "env" for r in feedback.valid())
    # the online input carries no rubric, no assertion and no world state
    views = [r.decision["view"] for r in out["rt"].ledger.rows("stop")]
    assert views and all("rubric" not in v and "assertion" not in v and "expected_state" not in v for v in views)


def test_dt02_the_probe_is_refused_outside_the_train_split(tmp_path, fake_upstream):
    suite = rsi.suite(fake_upstream)
    rt = stop_runtime(tmp_path / "ws")
    with pytest.raises(ValueError, match="only the train split"):
        play(rt, suite, rsi.splits(suite), "rsi.dev_notify_after_update", rsi.BusinessWorker())
    assert not list(rt.ledger.events())                          # refused before anything was recorded


# ---------------------------------------------------------------------------- DT-03
def test_dt03_a_repeated_label_is_deduplicated_and_a_correction_supersedes_it(tmp_path, fake_upstream):
    out, feedback = gather(tmp_path, fake_upstream)
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
    built = dataset(out, conflicted, validation=0.0, min_groups=1)
    assert built.exclusions["status:conflicted"] == 1
    # and a correction that says it supersedes the earlier label wins
    rt.outcome(decision, "no" if label == "yes" else "yes", source="human", note="review",
               supersedes="completion_snapshot@v1")
    resolved = next(r for r in build_feedback(rt, objects=objects).records if r.decision_id == decision)
    assert resolved.status == "valid" and resolved.source == "human" and resolved.supersedes


def test_a_judge_or_a_gate_verdict_never_becomes_a_label(tmp_path, fake_upstream):
    out, feedback = gather(tmp_path, fake_upstream)
    rt, objects = out["rt"], out["objects"]
    did = rt.ledger.decision(session="s_x", point="stop", ctx="s_x#0", phase="on_finish")
    rt.ledger.outcome(did, "yes", source="gate", note="promotion gate")
    rt.ledger.outcome(did, "yes", source="frontier_llm", note="a model's own opinion")
    again = build_feedback(rt, objects=objects)
    assert all(r.source in ("env", "human") for r in again.records)
    assert not any(r.decision_id == did for r in again.records)
    assert len(again.valid()) == len(feedback.valid())


# ---------------------------------------------------------------------------- DT-04
def test_dt04_a_view_that_needs_a_field_nobody_stored_is_not_rendered_from_the_future(tmp_path, fake_upstream):
    out, feedback = gather(tmp_path, fake_upstream, tasks=["rsi.policy_hold"])
    point = business_stop_point()
    point.spec.view_params["requires"] = ["tool_events"]      # this task makes no tool call at all
    built = dataset(out, feedback, point=point, validation=0.0, min_groups=1)
    assert built.status == "insufficient_data"
    assert any(k.startswith("missing_observation_field") for k in built.exclusions)
    assert not (out["rt"].root / "data" / "train.jsonl").exists()


# ---------------------------------------------------------------------------- DT-05
def test_dt05_evidence_from_another_partition_never_reaches_a_training_file(tmp_path, fake_upstream):
    out, feedback = gather(tmp_path, fake_upstream, tasks=["rsi.notify_after_update"])
    objects = out["objects"]
    final = objects.put(records.EvidenceRef(id="final-1", kind="summary", scope_id="round-1", partition="final",
                                            producer_ref="llm", content_ref=objects.put_json("change", {"s": 1})))
    feedback.records.append(records.FeedbackRecord(
        decision_id=feedback.valid()[0].decision_id, task_id="rsi.final_notify_after_update",
        family_id="rsi.final_notify_after_update", source="env", feedback_ref=COMPLETION_SNAPSHOT.ref,
        target={"yes": 1.0}, evidence_refs=(final,), input_schema="stop.business.v1", status="valid"))
    assert check_lineage(feedback, ("train",), objects=objects)
    built = dataset(out, feedback, validation=0.0, min_groups=1)
    assert built.status == "invalid" and built.exclusions["outside_data_boundary"]
    assert not (out["rt"].root / "data" / "train.jsonl").exists()


def test_dt05_a_dev_task_label_is_excluded_by_partition(tmp_path, fake_upstream):
    out, feedback = gather(tmp_path, fake_upstream, tasks=["rsi.notify_after_update"])
    rt, objects = out["rt"], out["objects"]
    # a decision recorded in a dev session, labelled by hand: the builder refuses it on the partition alone
    rt.ledger.append({"type": "session", "session": "s_dev", "task": "rsi.dev_notify_after_update",
                      "family": "rsi.dev_notify_after_update", "split": "dev", "held_out": True})
    did = rt.ledger.decision(session="s_dev", point="stop", ctx="s_dev#0", phase="on_finish")
    rt.ledger.outcome(did, "yes", source="human", note="by hand")
    again = build_feedback(rt, objects=objects)
    assert again.excluded.get("partition:dev") == 1
    assert not any(r.decision_id == did for r in again.records)


# ---------------------------------------------------------------------------- DT-06 / DT-08
def test_dt06_training_produces_an_artifact_and_moves_no_pointer(tmp_path, fake_upstream):
    out, feedback = gather(tmp_path, fake_upstream, tasks=rsi.TRAIN, repeats=2)
    root, releases = out["rt"].root, out["activity"].releases
    built = dataset(out, feedback, validation=0.25, min_groups=1)
    assert built.status == "ready" and built.dataset_ref
    trainer = PhraseTrainer()
    account = Budgets(root).account("experiment", {"train_jobs": 2, "training_seconds": 600})
    before, history = releases.active_ref(), len(releases.history())
    build = train_artifact(out["rt"], trainer, built, {}, objects=out["objects"], budget=account)
    assert build.status == "ready" and build.model_ref == "trained-1" and build.job_ref["id"].startswith("job-")
    assert trainer.jobs and trainer.jobs[0]["rows"] == built.stats["rows"]["fit"]
    assert releases.active_ref() == before and len(releases.history()) == history      # no pointer moved
    assert not (root / "models" / "champion.json").exists()       # no legacy champion was written
    assert account.remaining()["used"]["train_jobs"] == 1.0       # the job's cost is on the book
    artifact = out["objects"].get(build.artifact_ref)
    assert artifact["model_id"] == "trained-1" and artifact["dataset"] == built.dataset_ref


def test_dt06_a_failed_job_is_not_ready_and_still_costs(tmp_path, fake_upstream):
    out, feedback = gather(tmp_path, fake_upstream, tasks=rsi.TRAIN, repeats=2)
    built = dataset(out, feedback, validation=0.25, min_groups=1)
    account = Budgets(out["rt"].root).account("experiment", {"train_jobs": 2, "training_seconds": 600})
    build = train_artifact(out["rt"], PhraseTrainer(fail=True), built, {}, objects=out["objects"], budget=account)
    assert build.status == "failed" and build.model_ref is None and "CUDA" in build.errors[0]
    assert account.remaining()["used"]["train_jobs"] == 1.0


def test_a_model_that_answers_as_another_model_fails_the_load_probe(tmp_path, fake_upstream):
    out, feedback = gather(tmp_path, fake_upstream, tasks=rsi.TRAIN, repeats=2)
    built = dataset(out, feedback, validation=0.25, min_groups=1)

    class Mislabelled(PhraseTrainer):
        def train(self, job):
            result = super().train(job)
            return type(result)("another-model", result.backend, result.job)

    build = train_artifact(out["rt"], Mislabelled(), built, {}, objects=out["objects"])
    assert build.status == "failed" and "answers as" in build.errors[0]


def test_dt08_an_unreadable_remote_job_is_queried_not_resubmitted(tmp_path, fake_upstream):
    out, feedback = gather(tmp_path, fake_upstream, tasks=rsi.TRAIN, repeats=2)
    built = dataset(out, feedback, validation=0.25, min_groups=1)
    execution = Execution(out["rt"].root, "exp-1")
    op = execution.operation("train", (built.dataset_ref,))
    op.attempts = ({"job_id": "job-remote-1"},)
    execution.put(op)
    trainer = UnknownJobTrainer()
    build = train_artifact(out["rt"], trainer, built, {}, objects=out["objects"], operation=op)
    assert build.status == "failed" and "still unknown" in build.errors[0]
    assert trainer.attached == ["job-remote-1"] and not trainer.jobs      # nothing was submitted twice


# ---------------------------------------------------------------------------- DT-07
def test_dt07_a_scorer_that_raises_produces_unknown_and_never_a_no(tmp_path, fake_upstream, monkeypatch):
    from ouroloop.adapters import automationbench as ab

    def broken(self, world=None):
        raise RuntimeError("the rubric could not read this state")

    monkeypatch.setattr(ab.Episode, "score", broken)
    out, feedback = gather(tmp_path, fake_upstream)
    assert out["results"][0].validity == "invalid" and out["results"][0].failure_class == "evaluator"
    assert feedback.records and all(r.status == "unknown" and not r.target for r in feedback.records)
    assert not feedback.valid()
    built = dataset(out, feedback, min_groups=1)
    assert built.status == "insufficient_data" and built.exclusions["status:unknown"] >= 1


def test_insufficient_groups_does_not_fall_back_to_a_row_split(tmp_path, fake_upstream):
    out, feedback = gather(tmp_path, fake_upstream, tasks=["rsi.notify_after_update"], repeats=3)
    built = dataset(out, feedback, validation=0.25, min_groups=3)
    assert built.status == "insufficient_data" and built.exclusions["insufficient_groups"]["needed"] == 3
    assert not (out["rt"].root / "data" / "train.jsonl").exists()
