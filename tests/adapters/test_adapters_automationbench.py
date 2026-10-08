"""The AutomationBench adapter, against the miniature upstream in tests/fake_upstream.

What is checked here is the boundary, not the benchmark: the agent gets the task and the tools and nothing that
grades it, the score comes from the world the tools left, a branch is independent of the world it came from, and a
run that only narrates success is scored on what it wrote. The attempts at the end run the stirrup seed under the
runner shell in a ProcessWorker (evaluate.attempt, runtime.runner), with a scripted actor.
"""
import json
import os
import threading
import time

import pytest

from ouroloop.adapters import automationbench as ab
from ouroloop.contracts import Objects
from ouroloop.evaluate.attempt import Budgeted, make_agent_factory, run_attempt, run_tasks
from ouroloop.evaluate.limits import Budget
from ouroloop.evaluate.results import Results, RunManifest
from ouroloop.evaluate.splits import SplitManifest, TaskEntry
from ouroloop.harness.seed import build_seed
from ouroloop.runtime.chat import ChatTurn, ScriptedChat, ToolCall
from ouroloop.runtime.records import read as read_transcript, session_file
from ouroloop.runtime.worker import ProcessWorker

LEAD = "marketing.lead_followup"
HOLD = "marketing.unverified_request_hold"
SLACK = "https://slack.com/api/chat.postMessage"
HUBSPOT = "https://api.hubapi.com/crm/v3/objects/contacts"
UPDATE = ("api_fetch", {"method": "PATCH", "url": HUBSPOT, "body": json.dumps({"id": "c1", "stage": "opportunity"})})
POST = ("api_fetch", {"method": "POST", "url": SLACK,
                      "body": json.dumps({"channel": "C1", "text": "quarterly pipeline review"})})
# The seed's write guard blocks the first write of an attempt that has not planned (triage), so a scripted actor plans.
PLAN = ("plan_changes", {"changes": [{"content": "the request's changes", "status": "pending"}], "excluded": []})
BUDGET = Budget(max_steps=8, max_tool_calls=8)


@pytest.fixture
def suite(fake_upstream):
    return ab.load_suite("marketing", fake_upstream)


def manifest_of(suite, split: str) -> SplitManifest:
    fams, rule = ab.families(suite)
    return SplitManifest("marketing", suite.versions, rule, [TaskEntry.of(e) for e in suite.entries(fams)],
                         {f: split for f in fams.values()})


@pytest.fixture
def splits(suite):
    # Both tasks in dev: the point of these tests is the runner, and test must not be read by a development loop.
    return manifest_of(suite, "dev")


def finish(n) -> ChatTurn:
    return ChatTurn("", [ToolCall(f"f{n}", "finish", {"reason": "done"})], "tool_use")


def solve(world_calls):
    """A scripted actor that plans, makes these tool calls one a turn, then finishes (again, when pushed back)."""
    queue = [PLAN, *world_calls]
    turns = [0]

    def script(_messages):
        turns[0] += 1
        if queue:
            name, args = queue.pop(0)
            return ChatTurn("", [ToolCall(f"c{turns[0]}", name, args)], "tool_use")
        return finish(turns[0])
    return ScriptedChat(script)


def factory(root, provider, budget: Budget = BUDGET):
    """The runner shell over the stirrup seed, K's limits from `budget`, transcripts under `root`."""
    core = {"limits": budget.to_core(), "model": {"identity": "scripted", "effort": "medium", "max_effort": "medium"},
            "memory": {"enabled": False}, "worker": {"network": "none"}}
    package = build_seed("stirrup", core=core, objects=Objects(root))
    return make_agent_factory(provider=provider, package=package, core=core, worker=ProcessWorker(for_tests=True),
                              root=root)


def run_of(splits, split="dev", budget: Budget = BUDGET, **kw) -> RunManifest:
    return RunManifest("base", "base", split, {"model": "scripted"}, data=splits.hash(), budget=budget, **kw)


class TestProbe:
    def test_a_directory_that_is_not_a_checkout_is_named(self, tmp_path):
        assert "not an AutomationBench checkout" in ab.probe(tmp_path)["missing"][0]

    def test_the_fake_reports_no_missing_pieces_and_its_own_runner(self, fake_upstream):
        report = ab.probe(fake_upstream)
        assert report["missing"] == [] and report["package"] == "0.0.0-fake"
        # Without upstream's runner this adapter supplies the two functions itself, and says so.
        assert report["runner"] == "ouroloop"
        assert any("compute_allowed_services" in n for n in report["notes"])

    def test_require_raises_with_what_to_do(self, tmp_path):
        with pytest.raises(ab.UpstreamError, match="upstream_path"):
            ab.require(tmp_path)


class TestLoading:
    def test_tasks_carry_the_prompt_the_tools_and_a_fingerprint(self, suite):
        task = suite.tasks[LEAD]
        assert task.example_id == 9001 and len(task.contract) == 64
        assert "workflow automation agent" in task.system
        assert "pricing guide" in task.trigger
        assert "hubspot_update_contact" in task.tools

    def test_a_task_the_agent_sees_carries_no_grading(self, suite):
        task = suite.tasks[LEAD]
        assert not hasattr(task, "assertions") and not hasattr(task, "initial_state")
        assert "assertions" not in json.dumps(task.__dict__, default=str)

    def test_a_split_entry_is_plain_data_the_manifest_reads(self, suite):
        """The adapter imports nothing of the framework: an entry is a dict with TaskEntry's fields."""
        entry = suite.tasks[LEAD].entry("fam")
        assert entry == {"task_id": LEAD, "family_id": "fam", "example_id": 9001,
                         "contract": suite.tasks[LEAD].contract, "tools": list(suite.tasks[LEAD].tools)}
        assert TaskEntry.of(entry).to_json() == entry

    def test_the_services_a_world_is_subscribed_to_come_from_the_task(self, suite):
        assert suite.grading(LEAD).allowed_services == ("hubspot", "slack")
        assert suite.grading(HOLD).allowed_services == ("hubspot",)

    def test_one_family_per_task_by_default(self, suite):
        fams, rule = ab.families(suite)
        assert rule == "task_name" and fams == {t: t for t in suite.ids()}

    def test_declared_families_group_variants(self, suite):
        fams, rule = ab.families(suite, {LEAD: "marketing.followup", HOLD: "marketing.followup"})
        assert rule == "declared" and set(fams.values()) == {"marketing.followup"}

    def test_a_declared_family_for_an_unknown_task_is_refused(self, suite):
        with pytest.raises(ValueError, match="upstream does not have"):
            ab.families(suite, {"marketing.nope": "f"})

    def test_near_duplicates_are_reported_not_grouped(self, suite):
        pairs = ab.near_duplicates(suite, 0.0)
        assert pairs and pairs[0][2] < 1.0        # the two tasks overlap on hubspot, and are not merged
        assert ab.families(suite)[0][LEAD] == LEAD


class TestStrata:
    def test_a_stratum_is_the_scored_assertion_kinds_and_the_tool_family(self, fake_upstream):
        """The stratified split's two static features, read from the task definition: the fake `sales` domain has six
        strata of two tasks."""
        sales = ab.load_suite("sales", fake_upstream)
        strata = {}
        for tid in sales.ids():
            strata.setdefault(ab.stratum(sales, tid), []).append(tid)
        assert set(strata) == {"missing_write|app", "content+missing_write|app", "forbidden_write+missing_write|app",
                               "count+missing_write|app", "content|office", "content+forbidden_write|office"}
        assert all(len(t) == 2 for t in strata.values())
        assert ab.stratum(sales, "sales.notify_quiet_0") == "content+forbidden_write|office"

    def test_must_not_is_a_scored_forbidden_write(self, fake_upstream):
        sales = ab.load_suite("sales", fake_upstream)
        assert ab.has_must_not(sales, "sales.notify_quiet_0") and not ab.has_must_not(sales, "sales.notify_0")
        assert not ab.has_must_not(sales, "sales.nope")          # no readable grading: no known must-not check
        # the same task with its must-not check marked excluded, or unscored, has none: neither does its stratum
        g = sales.grading("sales.notify_quiet_0")
        for mark in ({"excluded": True}, {"scored": False}):
            assertions = tuple({**a, **mark} if "_not_" in a["type"] else a for a in g.assertions)
            other = ab.Suite("sales", {"t": sales.tasks["sales.notify_quiet_0"]},
                             {"t": ab.Grading(g.initial_state, assertions, g.allowed_services, g.info)}, sales.versions)
            assert not ab.has_must_not(other, "t") and ab.stratum(other, "t") == "content|office"
        assert [ab.assertion_kind(t) for t in ("x_not_sent", "row_count", "body_contains", "record_exists")] == [
            "forbidden_write", "count", "content", "missing_write"]


class TestTools:
    def test_the_injected_world_is_not_in_the_schema(self, suite):
        ep = suite.episode(LEAD)
        fetch = next(s for s in ep.specs() if s.name == "api_fetch")
        assert "world" not in fetch.schema["properties"] and "world" not in fetch.description
        assert fetch.schema["required"] == ["method", "url"]
        assert fetch.schema["properties"]["params"]["description"].startswith("Query parameters")

    def test_a_call_changes_the_world(self, suite):
        ep = suite.episode(LEAD)
        res = ep.run("api_fetch", {"method": "POST", "url": SLACK,
                                   "body": json.dumps({"channel": "C1", "text": "quarterly pipeline review"})})
        assert not res.is_error and ep.world.slack.messages[0]["text"] == "quarterly pipeline review"
        assert ep.calls[0]["tool"] == "api_fetch" and ep.tool_calls == 1

    def test_a_service_the_task_is_not_subscribed_to_is_refused(self, suite):
        ep = suite.episode(HOLD)     # hubspot only
        res = ep.run("api_fetch", {"method": "POST", "url": SLACK, "body": json.dumps({"text": "hi"})})
        assert res.is_error and "no slack account" in res.output.lower()

    def test_an_api_error_body_counts_as_an_error(self, suite):
        ep = suite.episode(LEAD)
        assert ep.run("api_fetch", {"method": "GET", "url": "https://example.test/nope"}).is_error
        assert ep.errors == 1

    def test_bad_arguments_end_the_call_not_the_episode(self, suite):
        ep = suite.episode(LEAD)
        assert ep.run("api_fetch", {"method": "POST"}).is_error          # no url
        assert ep.run("nope", {}).is_error
        assert not ep.run("api_search", {"query": "slack"}).is_error

    def test_an_empty_object_means_the_argument_was_omitted(self, suite):
        ep = suite.episode(LEAD)
        res = ep.run("api_search", {"query": "slack", "top_k": {}})
        assert not res.is_error and json.loads(res.output)

    def test_a_long_output_is_clipped_in_the_middle(self, suite):
        ep = ab.Episode(suite.tasks[LEAD], suite.grading(LEAD), suite.versions, max_output_chars=40)
        res = ep.run("base64_encode", {"text": "x" * 300})
        assert "characters omitted" in res.output and len(res.output) < 120

    def test_a_live_api_key_is_hidden_while_a_tool_runs(self, suite, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-visible")
        ep = suite.episode(LEAD)
        ep.world.meta.allowed_services = list(ep.world.meta.allowed_services) + ["chatgpt"]
        res = ep.run("api_fetch", {"method": "POST", "url": "https://api.openai.com/v1/chat/completions",
                                   "body": json.dumps({"prompt": "hi"})})
        assert json.loads(res.output)["live"] is False
        assert os.environ["OPENAI_API_KEY"] == "sk-should-not-be-visible"   # restored afterwards

    def test_a_read_does_not_count_as_a_change(self, suite):
        ep = suite.episode(LEAD)
        assert ep.mutates("api_fetch", {"method": "POST", "url": SLACK})
        assert not ep.mutates("api_fetch", {"method": "GET", "url": SLACK})
        assert not ep.mutates("api_search", {"query": "slack"})
        assert not ep.mutates("base64_encode", {"text": "hi"})


class TestScoring:
    def test_doing_nothing_scores_zero_and_finishing_scores_one(self, suite):
        ep = suite.episode(LEAD)
        assert ep.score().partial_credit == 0.0
        ep.run(*UPDATE)
        assert ep.score().partial_credit == 0.5
        ep.run(*POST)
        score = ep.score()
        assert score.success and score.partial_credit == 1.0 and (score.passed, score.scored) == (2, 2)

    def test_the_inverse_task_passes_by_leaving_the_world_alone(self, suite):
        assert suite.episode(HOLD).score().success is True
        ep = suite.episode(HOLD)
        ep.run("api_fetch", {"method": "PATCH", "url": HUBSPOT,
                             "body": json.dumps({"id": "c1", "stage": "customer"})})
        assert ep.score().success is False

    def test_the_score_is_the_world_not_the_story(self, suite):
        """A wrong write plus a confident summary scores the wrong write."""
        ep = suite.episode(LEAD)
        ep.run("api_fetch", {"method": "PATCH", "url": HUBSPOT,
                             "body": json.dumps({"id": "c2", "stage": "opportunity"})})   # wrong contact
        ep.run("api_fetch", {"method": "POST", "url": SLACK,
                             "body": json.dumps({"channel": "C1", "text": "All done, moved Dana and posted the "
                                                                          "quarterly pipeline review."})})
        score = ep.score()
        assert not score.success and score.partial_credit == 0.5     # the message passed, the stage did not

    def test_the_state_the_scorer_sees_is_upstream_shaped(self, suite):
        state = suite.episode(LEAD).state()
        assert set(state) == {"info", "initial_state", "world"}
        assert state["info"]["task_name"] == LEAD and state["info"]["assertions"]

    def test_the_baseline_of_an_episode_is_independent_of_its_progress(self, suite):
        ep = suite.episode(LEAD)
        ep.run(*UPDATE)
        assert ep.baseline_score().partial_credit == 0.0 and ep.score().partial_credit == 0.5

    def test_a_snapshot_is_scored_as_it_was_taken(self, suite):
        ep = suite.episode(LEAD)
        ep.run(*UPDATE)
        half = ep.snapshot()
        ep.run(*POST)
        assert ep.score(half).partial_credit == 0.5 and ep.score().success


class TestBranching:
    def test_a_snapshot_does_not_move_with_the_world(self, suite):
        ep = suite.episode(LEAD)
        snap = ep.snapshot()
        ep.run("api_fetch", {"method": "POST", "url": SLACK, "body": json.dumps({"text": "first"})})
        assert getattr(snap.slack, "messages", []) == []
        assert len(ep.world.slack.messages) == 1

    def test_restoring_returns_the_world_and_keeps_the_snapshot_reusable(self, suite):
        ep = suite.episode(LEAD)
        snap = ep.snapshot()
        ep.run("api_fetch", {"method": "POST", "url": SLACK, "body": json.dumps({"text": "branch a"})})
        ep.restore(snap)
        assert ep.world.slack.messages == []
        ep.run("api_fetch", {"method": "POST", "url": SLACK, "body": json.dumps({"text": "branch b"})})
        assert [m["text"] for m in ep.world.slack.messages] == ["branch b"]
        assert getattr(snap.slack, "messages", []) == []


class TestAgentInputs:
    def test_the_payload_is_the_prompt_and_the_tools(self, suite):
        ep = suite.episode(LEAD)
        payload = ab.agent_payload(suite.tasks[LEAD], ep)
        assert set(payload) == {"system", "task", "tools", "schemas"}
        assert set(payload["tools"]) == {"api_search", "api_fetch", "base64_encode"}

    def test_nothing_that_grades_the_task_reaches_the_agent(self, suite):
        ep = suite.episode(LEAD)
        found = ab.audit_inputs(suite.tasks[LEAD], suite.grading(LEAD), ab.agent_payload(suite.tasks[LEAD], ep))
        assert found["leaks"] == []

    def test_an_assertion_in_the_prompt_is_a_leak(self, suite):
        grading = suite.grading(LEAD)
        payload = {"system": "You may check " + json.dumps(grading.assertions[0], sort_keys=True), "task": "go"}
        found = ab.audit_inputs(suite.tasks[LEAD], grading, payload)
        assert any("appears verbatim" in line for line in found["leaks"])

    def test_the_expected_state_reaching_the_agent_is_a_leak(self, suite):
        payload = {"system": "s", "task": "go", "extra": {"assertions": []}}
        assert ab.audit_inputs(suite.tasks[LEAD], suite.grading(LEAD), payload)["leaks"]

    def test_a_phrase_the_task_asks_for_is_an_overlap_to_read_not_a_leak(self, suite):
        ep = suite.episode(LEAD)
        found = ab.audit_inputs(suite.tasks[LEAD], suite.grading(LEAD), ab.agent_payload(suite.tasks[LEAD], ep))
        assert found["leaks"] == []
        assert any("quarterly pipeline review" in line for line in found["overlaps"])


class TestContractCheck:
    def test_it_checks_no_task_it_was_not_given(self, suite):
        """The per-task part scores a task's seeded state, so which tasks are read is the caller's decision."""
        checks = ab.contract_check(suite, [])
        assert checks["checked"] == [] and checks.get("baseline") is None
        assert checks["notes"]            # the rubric and schema checks do not need a task and still ran

    def test_the_fake_passes_and_says_what_it_could_not_compare(self, suite):
        checks = ab.contract_check(suite, suite.ids())
        assert checks["failed"] == []
        assert checks["inverse_tasks"] == [HOLD]
        assert any("create_rubric" in n for n in checks["notes"])
        assert checks["baseline"][LEAD]["scored"] == 2

    def test_it_reports_a_snapshot_that_is_not_independent(self, suite, monkeypatch):
        ep = suite.episode(LEAD)
        monkeypatch.setattr(type(ep.world), "model_copy", lambda self, deep=False: self, raising=False)
        monkeypatch.setattr(ab.Suite, "episode", lambda self, tid: ep)
        assert any("branches would not be independent" in f for f in ab.contract_check(suite, [LEAD])["failed"])


class TestMultiTurnPrompt:
    def test_a_task_upstream_sends_in_several_turns_is_reported(self, suite):
        """The agent is given one first message, so flattening several would change what the model was sent."""
        task = suite.tasks[LEAD]
        multi = ab.BenchTask(task.task_id, task.example_id,
                             task.messages + ({"role": "user", "content": "And also post a summary."},),
                             task.tools, task.contract)
        assert multi.turns == 2 and "And also post a summary." in multi.trigger
        widened = ab.Suite("marketing", {LEAD: multi}, {LEAD: suite.grading(LEAD)}, suite.versions)
        assert any("non-system messages" in f for f in ab.contract_check(widened, [LEAD])["failed"])

    def test_the_public_tasks_are_one_turn_each(self, suite):
        assert all(suite.tasks[t].turns == 1 for t in suite.ids())


class TestTaskSignature:
    def test_the_signature_includes_what_the_assertions_expect(self, suite):
        sig = ab.task_signature(suite, LEAD)
        assert "svc:slack" in sig and "a:slack_message_contains" in sig
        assert "v:text=quarterly pipeline review" in sig      # the value, not just the type
        assert "hubspot_update_contact" in sig

    def test_two_tasks_over_the_same_shape_are_not_near_duplicates(self, suite):
        """Sharing every tool, service and assertion type is a shape; a variant also expects the same values."""
        task = suite.tasks[HOLD]
        g = suite.grading(HOLD)
        same_shape = ab.Grading(g.initial_state,
                                ({"type": "hubspot_contact_stage", "contact_id": "c2", "stage": "churned",
                                  "excluded": False},),
                                g.allowed_services, {**g.info, "assertions": [
                                    {"type": "hubspot_contact_stage", "contact_id": "c2", "stage": "churned",
                                     "excluded": False}]})
        other = ab.BenchTask("marketing.other", 9003, task.messages, task.tools, "c3")
        widened = ab.Suite("marketing", {HOLD: task, "marketing.other": other},
                           {HOLD: g, "marketing.other": same_shape}, suite.versions)
        overlap = dict(((a, b), j) for a, b, j in ab.near_duplicates(widened, 0.0))
        assert overlap and max(overlap.values()) < 0.8


class TestBudgeted:
    def test_a_refused_call_tells_the_model_the_run_is_over(self, suite):
        surface = Budgeted(suite.episode(LEAD), Budget(max_tool_calls=0))
        res = surface.run("api_search", {"query": "slack"})
        assert res.is_error and "budget is exhausted" in res.output and surface.exhausted
        assert surface.limit == "tool_calls"
        assert surface.episode.tool_calls == 0      # and the call never reached the world

    def test_it_offers_the_episode_s_tools_and_reports_its_changes(self, suite):
        surface = Budgeted(suite.episode(LEAD), Budget())
        assert [s.name for s in surface.specs()] == ["api_search", "api_fetch", "base64_encode"]
        assert surface.mutates("api_fetch", {"method": "POST", "url": SLACK})
        assert not surface.mutates("api_search", {"query": "slack"})


# ---------------------------------------------------------------------------- attempts under the runner shell
class TestRunAttempt:
    def test_a_solved_task_is_a_valid_passing_result(self, tmp_path, suite, splits):
        run = run_of(splits)
        attempt = run_attempt(suite, LEAD, factory(tmp_path / "ws", solve([UPDATE, POST])), run=run, splits=splits)
        r = attempt.result
        assert (r.validity, r.success, r.partial_credit) == ("valid", True, 1.0)
        assert (r.termination, r.tool_calls, r.family_id, r.split) == ("finished", 2, LEAD, "dev")
        assert r.evidence == r.attempt_id and r.contract == suite.tasks[LEAD].contract
        lines = read_transcript(session_file(tmp_path / "ws", r.attempt_id))
        assert lines[0]["kind"] == "task" and lines[-1]["kind"] == "end"
        assert [line["args"]["method"] for line in lines if line["kind"] == "tool"] == ["PATCH", "POST"]

    def test_a_run_that_only_reports_success_fails(self, tmp_path, suite, splits):
        provider = ScriptedChat(lambda _m: ChatTurn("I moved Dana to opportunity and posted the quarterly pipeline "
                                                    "review.", [], "end"))
        r = run_attempt(suite, LEAD, factory(tmp_path / "ws", provider), run=run_of(splits), splits=splits).result
        assert r.validity == "valid" and not r.success and r.partial_credit == 0.0 and r.tool_calls == 0

    def test_a_provider_failure_is_an_invalid_attempt(self, tmp_path, suite, splits):
        class Broken:
            name, model, supports_history_edits = "broken", "broken", True

            def chat(self, system, messages, tools):
                raise RuntimeError("upstream API 500")

        r = run_attempt(suite, LEAD, factory(tmp_path / "ws", Broken()), run=run_of(splits), splits=splits).result
        assert r.validity == "invalid" and r.termination == "infrastructure"
        assert r.failure_class == "infrastructure" and r.success is False
        assert "upstream API 500" in r.invalid_reason

    def test_a_task_that_changed_upstream_is_not_scored(self, tmp_path, suite, splits):
        stale = SplitManifest.from_json({**splits.to_json(), "hash": None,
                                         "entries": [{**e.to_json(), "contract": "old"} if e.task_id == LEAD
                                                     else e.to_json() for e in splits.entries]})
        provider = solve([UPDATE, POST])
        r = run_attempt(suite, LEAD, factory(tmp_path / "ws", provider), run=run_of(stale), splits=stale).result
        assert r.validity == "invalid" and "changed since the split was frozen" in r.invalid_reason
        assert r.failure_class == "evaluator" and provider.calls == 0      # nothing ran

    def test_a_run_reading_another_split_is_refused(self, tmp_path, suite, splits):
        with pytest.raises(ValueError, match="this run reads test"):
            run_attempt(suite, LEAD, factory(tmp_path / "ws", solve([])), run=run_of(splits, "test"), splits=splits)

    def test_the_budget_stops_the_run_and_the_result_says_so(self, tmp_path, suite, splits):
        budget = Budget(max_steps=8, max_tool_calls=1)
        calls = [("api_search", {"query": "slack"})] * 3
        r = run_attempt(suite, LEAD, factory(tmp_path / "ws", solve(calls), budget), run=run_of(splits, budget=budget),
                        splits=splits).result
        assert (r.termination, r.limit, r.validity, r.tool_calls) == ("budget", "tool_calls", "valid", 1)

    def test_a_test_task_session_records_its_split(self, tmp_path, suite):
        """The session's `task` line carries the split it was run in: what keeps a held-out attempt out of anything
        that reads train evidence."""
        held = manifest_of(suite, "test")
        r = run_attempt(suite, LEAD, factory(tmp_path / "ws", solve([])), run=run_of(held, "test"),
                        splits=held).result
        (task,) = [line for line in read_transcript(session_file(tmp_path / "ws", r.evidence))
                   if line["kind"] == "task"]
        assert (task["task_id"], task["split"], task["attempt_id"]) == (LEAD, "test", r.attempt_id)

    def test_the_scorer_is_never_consulted_during_an_attempt(self, tmp_path, suite, monkeypatch):
        seen = []
        original = ab.Episode.score
        monkeypatch.setattr(ab.Episode, "score",
                            lambda self, world=None: (seen.append(len(self.calls)), original(self, world))[1])
        train = manifest_of(suite, "train")
        result = run_attempt(suite, LEAD, factory(tmp_path / "ws", solve([UPDATE])), run=run_of(train, "train"),
                             splits=train).result
        # Every scoring happened after the last tool call, never between them.
        assert seen and all(n == result.tool_calls == 1 for n in seen)

    def test_run_tasks_records_every_attempt_and_its_grading(self, tmp_path, suite, splits):
        run = run_of(splits, repeats=2)
        results = Results(tmp_path / "ws", "marketing")
        rows = run_tasks(suite, [LEAD], factory(tmp_path / "ws", solve([])), run=run, splits=splits,
                         results=results, repeats=2)
        assert [r.attempt for r in rows] == [0, 1]
        assert len(results.of_run(run.hash())) == 2
        # dev grading is kept beside the results and is not readable as train detail
        assert results.grading(rows[0].attempt_id) == []
        assert results.grading(rows[0].attempt_id, splits=("dev",))


class TestConcurrency:
    def test_concurrent_attempts_are_all_recorded_in_the_plans_order(self, tmp_path, suite, splits):
        """Several attempts at a time, each with its own episode, worker and session; the results and the caller's
        callback are recorded for every one, and the returned list follows the plan's order rather than the finishing
        order."""
        in_flight, peak, lock, turns = [0], [0], threading.Lock(), [0]

        def script(_messages):
            with lock:
                in_flight[0] += 1
                peak[0] = max(peak[0], in_flight[0])
                turns[0] += 1
            time.sleep(0.05)
            with lock:
                in_flight[0] -= 1
            return finish(turns[0])

        run = run_of(splits, repeats=3)
        results = Results(tmp_path / "ws", "marketing")
        seen = []
        order = [(t, i) for i in range(3) for t in (LEAD, HOLD)]
        rows = run_tasks(suite, [LEAD, HOLD], factory(tmp_path / "ws", ScriptedChat(script)), run=run,
                         splits=splits, results=results, order=order, on_result=seen.append, concurrency=4)
        assert [(r.task_id, r.attempt) for r in rows] == order
        assert len(results.of_run(run.hash())) == 6 and len(seen) == 6
        assert len({r.attempt_id for r in rows}) == 6 and all(r.validity == "valid" for r in rows)
        assert peak[0] > 1                                  # they did overlap
        transcripts = list((tmp_path / "ws" / "transcripts").glob("*.jsonl"))
        assert len(transcripts) == 6
        for path in transcripts:
            for line in path.read_text().splitlines():
                json.loads(line)                            # every appended line is whole

    def test_the_live_keys_are_hidden_once_for_the_whole_pool(self, tmp_path, suite, splits, monkeypatch):
        """A per-call pop and restore of os.environ from several threads would race with every other reader (the
        claude CLI's child environment): under concurrency the keys are hidden before the pool starts and restored
        after it ends."""
        monkeypatch.setenv("OPENAI_API_KEY", "live-key-that-must-not-leak")
        seen = []

        def script(_messages):
            seen.append("OPENAI_API_KEY" in os.environ)
            return finish(len(seen))

        run = run_of(splits, repeats=2)
        run_tasks(suite, [LEAD, HOLD], factory(tmp_path / "ws", ScriptedChat(script)), run=run, splits=splits,
                  results=Results(tmp_path / "ws", "marketing"), repeats=2, concurrency=3)
        assert seen and not any(seen)                              # hidden during every attempt
        assert os.environ["OPENAI_API_KEY"] == "live-key-that-must-not-leak"     # and back afterwards

    def test_an_interrupt_lets_running_attempts_finish_and_starts_no_more(self, tmp_path, suite, splits):
        """Stopped mid-run: what is in flight is recorded, so the book and the results agree; the rest never
        starts."""
        started, lock, stop = [], threading.Lock(), threading.Event()

        def script(_messages):
            with lock:
                started.append(1)
                first = len(started) == 1
            if first:
                stop.set()                                  # the first call asks the main thread to interrupt
                time.sleep(0.2)
            return finish(len(started))

        run = run_of(splits, repeats=6)
        results = Results(tmp_path / "ws", "marketing")
        order = [(LEAD, i) for i in range(6)]

        def interrupt():
            stop.wait(5)
            import _thread
            _thread.interrupt_main()

        threading.Thread(target=interrupt, daemon=True).start()
        with pytest.raises(KeyboardInterrupt):
            run_tasks(suite, [LEAD], factory(tmp_path / "ws", ScriptedChat(script)), run=run, splits=splits,
                      results=results, order=order, concurrency=2)
        recorded = results.of_run(run.hash())
        assert 1 <= len(recorded) < 6                       # the running ones were kept, the rest never started
