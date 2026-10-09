"""One attempt (docs/design/EVALUATE.md §4; RUNTIME.md §5): a fresh episode, the stirrup seed in a ProcessWorker on the
miniature upstream, the official score afterwards. Its four endings, the evaluator's own failure, the grader's rows
(train only) and attempts resumed by their id."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
import rsi

from ouroloop.activity import phases
from ouroloop.contracts import read_jsonl
from ouroloop.evaluate.attempt import make_agent_factory, run_attempt, run_tasks
from ouroloop.evaluate.limits import Budget
from ouroloop.evaluate.splits import SplitManifest
from ouroloop.harness import package as pk
from ouroloop.runtime.records import session_file
from ouroloop.runtime.worker import ProcessWorker

NOTIFY = "rsi.dev_notify_after_update"            # update a contact, then post in #ops: two writes


@pytest.fixture
def act(tmp_path, fake_upstream):
    return rsi.activity(tmp_path, fake_upstream)


def attempt(act, task_id: str, *, provider=None, package=None, worker=None, splits=None, limits=None):
    """One attempt at `task_id` under the activity's K with `limits` over K's, recorded. -> (TaskResult, transcript)."""
    core = {**act.k, "limits": {**act.k["limits"], **(limits or {})}}
    release = act.active_release()
    splits = splits or act.splits
    run = replace(act.run_manifest(candidate="probe", role="base", split=splits.split_of(task_id), release=release),
                  budget=Budget.from_core(core["limits"]))
    make = make_agent_factory(provider=provider or rsi.BusinessWorker(), package=package or act.package(release),
                              core=core, worker=worker or ProcessWorker(for_tests=True), root=act.root,
                              pricing=act.pricing(), release_ref=release.ref(), core_ref=act.core_ref)
    (row,) = run_tasks(act.suite, [task_id], make, run=run, splits=splits, results=act.results,
                       release_ref=release.ref())
    return row, list(read_jsonl(session_file(act.root, row.attempt_id)))


# ---------------------------------------------------------------------------- the four endings
def test_an_attempt_that_finishes_is_scored_after_the_run(act):
    actor = rsi.BusinessWorker()
    row, lines = attempt(act, NOTIFY, provider=actor)
    assert (row.termination, row.validity, row.failure_class, row.limit) == ("finished", "valid", "", None)
    assert row.success and row.assertions_passed == row.assertions_scored > 0
    assert row.steps == actor.calls and row.tool_calls == 2
    assert row.cost_usd == pytest.approx(act.pricing().cost(row.usage)) and row.cost_usd > 0
    assert [line["kind"] for line in lines][0] == "task" and lines[-1]["kind"] == "end"
    assert lines[-1]["termination"] == "finished" and lines[-1]["cost"] == row.cost_usd


LOOP_AGENT = """def run(ctx, task):
    messages, tools = [{"role": "user", "content": task}], list(ctx.view["base_tools"])
    while True:
        reply = ctx.model(messages, tools)
        if not reply.tool_calls:
            return reply.text
        messages.append({"role": "assistant", "text": reply.text, "tool_calls": reply.tool_calls})
        results = []
        for call in reply.tool_calls:
            result = ctx.tool(call["name"], call["args"])
            results.append({"id": call["id"], "name": call["name"], "output": result.output,
                            "is_error": result.is_error})
        messages.append({"role": "tool", "results": results})
"""


def with_files(act, files: dict, change) -> pk.Package:
    """The seed with these files added and its manifest changed, built under K."""
    seed = act.package()
    manifest = json.loads(seed.files["manifest.json"])
    change(manifest)
    return pk.build(act.objects, {**seed.files, **files, "manifest.json": json.dumps(manifest, indent=1)},
                    limits=pk.Limits.from_core(act.k), seed=seed)


def loop_agent(act) -> pk.Package:
    """An agent that never reads ctx.limits: it asks for the next turn until the model stops calling tools. (The
    seed caps its own turns at the act calls left, so it finishes before the host's limit.)"""
    return with_files(act, {"agent/loop_agent.py": LOOP_AGENT},
                      lambda m: m["agent"].update(entrypoint="agent/loop_agent.py:run"))


@pytest.mark.parametrize("limits, dimension, writes", [
    ({"max_steps": 1}, "act_calls", 1),            # the update goes through; the second act call is refused unmade
    ({"max_tool_calls": 1}, "tool_calls", 1),      # the update goes through, the post is refused
    ({"max_tokens": 30}, "tokens", 0),             # the call that crosses 30 output tokens is booked, not handed on
])
def test_a_hard_limit_ends_the_attempt_as_budget_named_by_its_dimension(act, limits, dimension, writes):
    actor = rsi.BusinessWorker()
    package = loop_agent(act) if dimension == "act_calls" else None
    row, lines = attempt(act, NOTIFY, provider=actor, package=package, limits=limits)
    assert (row.termination, row.limit, row.validity) == ("budget", dimension, "valid")
    # scored as the world stands: the writes made before the limit count, the rest are missing
    assert not row.success and row.assertions_scored > 0 and row.assertions_passed == writes
    assert row.failure_class == "task_outcome"
    end = lines[-1]
    assert (end["kind"], end["termination"], end["limit"]) == ("end", "budget", dimension)
    assert actor.calls == len([line for line in lines if line["kind"] == "gateway_call"])


def bad_hook_package(act) -> pk.Package:
    """The seed with a before_tool hook that answers outside its site's protocol (block takes {reason})."""
    return with_files(act, {"middlewares/bad_hook.py": 'def bad(ctx, event):\n    return {"block": "no reason"}\n'},
                      lambda m: m["hooks"].append({"name": "bad", "site": "before_tool",
                                                   "entrypoint": "middlewares/bad_hook.py:bad"}))


def test_a_hook_answering_outside_its_protocol_is_the_candidates_defect_and_still_scored(act):
    row, lines = attempt(act, NOTIFY, package=bad_hook_package(act))
    assert (row.termination, row.validity, row.failure_class) == ("harness_error", "valid", "candidate_defect")
    assert row.assertions_scored > 0 and not row.success
    end = lines[-1]
    assert end["termination"] == "harness_error" and "before_tool: block is {reason: str}" in end["error"]


class DyingWorker(ProcessWorker):
    """A ProcessWorker whose process is killed once its first model call has been answered (an OOM kill, a lost
    host): nothing the package did."""

    def start(self, egress=None):
        session = super().start(egress)
        serve = session.serve

        def dying(message, *, handlers, deadline):
            if "model_call" not in handlers:
                return serve(message, handlers=handlers, deadline=deadline)

            def model_call(reply, _answer=handlers["model_call"]):
                answer = _answer(reply)
                session.proc.kill()
                session.proc.wait()
                return answer
            return serve(message, handlers={**handlers, "model_call": model_call}, deadline=deadline)
        session.serve = dying
        return session


def test_a_worker_that_crashes_makes_the_attempt_invalid_infrastructure(act):
    actor = rsi.BusinessWorker()
    row, lines = attempt(act, NOTIFY, provider=actor, worker=DyingWorker(for_tests=True))
    assert (row.termination, row.validity, row.failure_class) == ("infrastructure", "invalid", "infrastructure")
    assert row.invalid_reason.startswith("infrastructure: WorkerCrashed")
    assert not row.success and row.assertions_scored == 0                    # no evidence either way
    # what it paid for is kept on the invalid record
    assert actor.calls == 1 and row.usage["input_tokens"] == 100 and row.cost_usd is not None
    assert lines[-1]["termination"] == "infrastructure"


# ---------------------------------------------------------------------------- the evaluator's own failure
def test_a_task_whose_contract_drifted_since_the_split_froze_is_an_evaluator_failure(act):
    frozen = act.splits.to_json()
    frozen.pop("hash")
    for entry in frozen["entries"]:
        if entry["task_id"] == NOTIFY:
            entry["contract"] = "0" * 64
    drifted = SplitManifest.from_json(frozen)
    run = act.run_manifest(candidate="probe", role="base", split="dev", release=act.active_release())

    def make(*_a):
        raise AssertionError("no agent is built for a task whose contract changed")
    result = run_attempt(act.suite, NOTIFY, make, run=run, splits=drifted)
    row = result.result
    assert (row.termination, row.validity, row.failure_class) == ("infrastructure", "invalid", "evaluator")
    assert row.invalid_reason.startswith("contract: the task changed since the split was frozen (000000000000 -> ")
    assert result.episode is None and result.agent is None


# ---------------------------------------------------------------------------- the grader's rows
def test_grading_rows_are_written_beside_the_results_and_readable_for_train_only(act):
    train, _ = attempt(act, rsi.TRAIN[0])
    dev, _ = attempt(act, NOTIFY)
    graded = act.results.grading(train.attempt_id)
    assert len(graded) == train.assertions_scored and all(set(a) == {"type", "params", "passed", "excluded"}
                                                          for a in graded)
    assert sum(a["passed"] for a in graded if not a["excluded"]) == train.assertions_passed
    assert act.results.grading(dev.attempt_id) == []                          # dev gives summaries only
    assert len(act.results.grading(dev.attempt_id, splits=("dev",))) == dev.assertions_scored   # the raw record
    # results.jsonl never carries the answer key
    assert all("assertions" not in row for row in read_jsonl(act.results.path))


# ---------------------------------------------------------------------------- resuming by attempt id
def test_attempts_on_record_are_kept_by_their_id_and_not_run_again(act):
    actor = rsi.BusinessWorker()
    act.provider_factory = lambda _table: actor
    release, account = act.active_release(), act.account("probe")

    def run(pairs, arm="subject"):
        return phases.run_arm(act, release=release, split="dev", candidate="probe", role="base", pairs=pairs,
                              account=account, arm=arm)
    first = run([(NOTIFY, 0)])
    calls = actor.calls
    again = run([(NOTIFY, 0)])
    assert actor.calls == calls and [r.attempt_id for r in again] == [r.attempt_id for r in first]
    more = run([(NOTIFY, 0), (NOTIFY, 1)])                                   # only the new repeat runs
    assert more[0].attempt_id == first[0].attempt_id and more[1].attempt_id.endswith("/r1/subject#0")
    assert len(list(act.results.all())) == 2
    other = run([(NOTIFY, 0)], arm="control")                                # another arm is another attempt
    assert other[0].attempt_id != first[0].attempt_id and len(list(act.results.all())) == 3
