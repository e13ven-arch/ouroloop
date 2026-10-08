"""M1 in a real run (docs/modules/01_POLICY_RUNTIME.md §5): the W0 gap and the finish check closing it, what an
install does to a running session, and a failure in assembling or closing an attempt."""
import pytest
import rsi

from ouroloop import policies as P
from ouroloop.evolve import contracts as ct
from ouroloop.evolve.task_eval import make_agent_factory, run_attempt

NOTIFY = "rsi.notify_after_update"


def run(tmp_path, suite, splits, worker_ref, provider, *, task=NOTIFY, split="train"):
    """One attempt under the runner shell of a release with `worker_ref` as its W."""
    objects = ct.Objects(tmp_path / "ws")
    releases, _, _ = rsi.workspace(tmp_path / "ws", split_ref=splits.hash())
    ref = releases.active()["release_ref"]
    if worker_ref is not None:
        ref = releases.bootstrap(worker_ref, objects.load(ref, ct.SystemRelease).research_ref,
                                 objects.load(ref, ct.SystemRelease).core_ref, activate=False)
    bound = P.bind_release(ref, profile="task", services=P.Services(objects), authorization="bootstrap")
    factory = make_agent_factory(rsi.ws(tmp_path / "ws"), provider, budget=rsi.run_manifest("c", "base", split).budget,
                                 bound=bound)
    manifest = rsi.run_manifest("candidate", "evolved", split, data=splits.hash(), system_release_ref=ref,
                                worker_ref=objects.load(ref, ct.SystemRelease).worker_ref,
                                research_ref=objects.load(ref, ct.SystemRelease).research_ref,
                                core_ref=objects.load(ref, ct.SystemRelease).core_ref)
    attempt = run_attempt(suite, task, factory, run=manifest, splits=splits, release_ref=ref, plan_ref="plan-1")
    if attempt.episode is not None:
        attempt.episode.close()
    return bound, attempt, objects


@pytest.fixture
def suite(fake_upstream):
    return rsi.suite(fake_upstream)


@pytest.fixture
def splits(suite):
    return rsi.splits(suite)


# ---------------------------------------------------------------------------- the gap, and the finish check closing it
# The stop decision's view, a bound worker.stop policy proposing, being overruled and falling back (PR-01..PR-03,
# ER-03) are tested where the stop point is asked now: tests/train/test_stop.py. The runner shell asks none.
def test_w0_updates_the_record_and_never_notifies(tmp_path, suite, splits):
    bound, attempt, objects = run(tmp_path, suite, splits, rsi.w0(ct.Objects(tmp_path / "ws")), rsi.BusinessWorker())
    assert attempt.result.success is False and attempt.result.partial_credit == 0.5


def test_the_same_task_passes_once_the_finish_check_is_bound(tmp_path, suite, splits):
    objects = ct.Objects(tmp_path / "ws")
    bound, attempt, _ = run(tmp_path, suite, splits, rsi.w1(objects), rsi.BusinessWorker())
    assert attempt.result.success is True and attempt.result.partial_credit == 1.0


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


# ---------------------------------------------------------------------------- ER-09
def test_er09_a_failure_in_make_agent_or_close_is_recorded_and_closes_what_it_can(tmp_path, suite, splits):
    from ouroloop.benchmark import Results
    from ouroloop.evolve.task_eval import run_tasks
    rt = rsi.ws(tmp_path / "ws")
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
