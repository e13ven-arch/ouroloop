"""The AutomationBench adapter, against the miniature upstream in tests/fake_upstream.

What is checked here is the boundary, not the benchmark: the agent gets the task and the tools and nothing that
grades it, the score comes from the world the tools left, a branch is independent of the world it came from, and a
run that only narrates success is scored on what it wrote.
"""
import json
import os

import pytest

from ouroloop.adapters import automationbench as ab
from ouroloop.agent import AgentConfig
from ouroloop.train.backends import KeywordBackend
from ouroloop.benchmark import Budget, RunManifest, Results, SplitManifest, assign_splits
from ouroloop.chat import ChatTurn, ScriptedChat, ToolCall
from ouroloop.evolve.task_eval import Budgeted, run_attempt, run_tasks
from ouroloop.points import builtin_points
from ouroloop.runtime import Runtime

class NotDone:
    """A decision backend that answers "the task is not done" with a fixed confidence."""
    name = "fixed"

    def __init__(self, p_no: float):
        self.p_no = p_no

    def decide(self, requests):
        from ouroloop.train.types import Answer
        return [Answer({"no": self.p_no, "yes": 1 - self.p_no}, "fixed-1") for _ in requests]


@pytest.fixture
def every_stop_may_act(monkeypatch):
    """v0.2 already lets `stop` act; this pins it so the test does not depend on that staying true."""
    from ouroloop.train import policy
    monkeypatch.setattr(policy, "ACTING", set(policy.ACTING) | {"stop"})


LEAD = "marketing.lead_followup"
HOLD = "marketing.unverified_request_hold"
SLACK = "https://slack.com/api/chat.postMessage"
HUBSPOT = "https://api.hubapi.com/crm/v3/objects/contacts"


@pytest.fixture
def suite(fake_upstream):
    return ab.load_suite("marketing", fake_upstream)


@pytest.fixture
def splits(suite):
    fams, rule = ab.families(suite)
    entries = suite.entries(fams)
    # Both tasks in dev: the point of these tests is the runner, and test must not be read by a development loop.
    return SplitManifest("marketing", suite.versions, rule, entries, {f: "dev" for f in fams.values()})


def solve(world_calls):
    """A scripted model that makes these tool calls, then reports."""
    turns = [ChatTurn("", [ToolCall(f"c{i}", name, args)], "tool_use")
             for i, (name, args) in enumerate(world_calls)]
    return ScriptedChat(turns + [ChatTurn("Done.", [], "end")])


def runtime(tmp_path):
    rt = Runtime(tmp_path / "ws", {"mock": KeywordBackend()}, "mock", seed=0)
    for p in builtin_points():
        rt.register(p)
    return rt


def factory(rt, provider, budget=None):
    from ouroloop.evolve.task_eval import make_agent_factory
    return make_agent_factory(rt, provider, budget=budget or Budget(max_steps=8, max_tool_calls=8))


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

    def test_a_live_api_key_is_hidden_while_a_tool_runs(self, suite, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-visible")
        ep = suite.episode(LEAD)
        ep.world.meta.allowed_services = list(ep.world.meta.allowed_services) + ["chatgpt"]
        res = ep.run("api_fetch", {"method": "POST", "url": "https://api.openai.com/v1/chat/completions",
                                   "body": json.dumps({"prompt": "hi"})})
        assert json.loads(res.output)["live"] is False
        assert os.environ["OPENAI_API_KEY"] == "sk-should-not-be-visible"   # restored afterwards


class TestScoring:
    def test_doing_nothing_scores_zero_and_finishing_scores_one(self, suite):
        ep = suite.episode(LEAD)
        assert ep.score().partial_credit == 0.0
        ep.run("api_fetch", {"method": "PATCH", "url": HUBSPOT,
                             "body": json.dumps({"id": "c1", "stage": "opportunity"})})
        assert ep.score().partial_credit == 0.5
        ep.run("api_fetch", {"method": "POST", "url": SLACK,
                             "body": json.dumps({"channel": "C1", "text": "quarterly pipeline review"})})
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
        ep.run("api_fetch", {"method": "PATCH", "url": HUBSPOT,
                             "body": json.dumps({"id": "c1", "stage": "opportunity"})})
        assert ep.baseline_score().partial_credit == 0.0 and ep.score().partial_credit == 0.5


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


class TestRunAttempt:
    def test_a_solved_task_is_a_valid_passing_result(self, tmp_path, suite, splits):
        rt = runtime(tmp_path)
        run = RunManifest("base", "base", "dev", {"model": "scripted"}, data=splits.hash(),
                          budget=Budget(max_steps=8, max_tool_calls=8))
        provider = solve([("api_fetch", {"method": "PATCH", "url": HUBSPOT,
                                         "body": json.dumps({"id": "c1", "stage": "opportunity"})}),
                          ("api_fetch", {"method": "POST", "url": SLACK,
                                         "body": json.dumps({"channel": "C1",
                                                             "text": "quarterly pipeline review done"})})])
        attempt = run_attempt(suite, LEAD, factory(rt, provider), run=run, splits=splits)
        r = attempt.result
        assert (r.validity, r.success, r.partial_credit) == ("valid", True, 1.0)
        assert (r.termination, r.tool_calls, r.family_id, r.split) == ("finished", 2, LEAD, "dev")
        assert r.evidence and r.contract == suite.tasks[LEAD].contract

    def test_a_run_that_only_reports_success_fails(self, tmp_path, suite, splits):
        run = RunManifest("base", "base", "dev", {"model": "scripted"}, data=splits.hash())
        provider = ScriptedChat([ChatTurn("I moved Dana to opportunity and posted the quarterly pipeline review.",
                                          [], "end")])
        r = run_attempt(suite, LEAD, factory(runtime(tmp_path), provider), run=run, splits=splits).result
        assert r.validity == "valid" and not r.success and r.partial_credit == 0.0 and r.tool_calls == 0

    def test_a_provider_failure_is_an_invalid_attempt(self, tmp_path, suite, splits):
        class Broken:
            name, model, supports_history_edits = "broken", "broken", True

            def chat(self, system, messages, tools):
                raise RuntimeError("upstream API 500")

        run = RunManifest("base", "base", "dev", {"model": "broken"}, data=splits.hash())
        r = run_attempt(suite, LEAD, factory(runtime(tmp_path), Broken()), run=run, splits=splits).result
        assert r.validity == "invalid" and r.termination == "harness_error"
        assert "upstream API 500" in r.invalid_reason and r.success is False

    def test_a_task_that_changed_upstream_is_not_scored(self, tmp_path, suite, splits):
        stale = SplitManifest.from_json({**splits.to_json(), "hash": None,
                                         "entries": [{**e.to_json(), "contract": "old"} if e.task_id == LEAD
                                                     else e.to_json() for e in splits.entries]})
        run = RunManifest("base", "base", "dev", {"model": "scripted"}, data=stale.hash())
        r = run_attempt(suite, LEAD, factory(runtime(tmp_path), solve([])), run=run, splits=stale).result
        assert r.validity == "invalid" and "changed since the split was frozen" in r.invalid_reason

    def test_a_run_reading_another_split_is_refused(self, tmp_path, suite, splits):
        run = RunManifest("base", "base", "test", {"model": "scripted"}, data=splits.hash())
        with pytest.raises(ValueError, match="this run reads test"):
            run_attempt(suite, LEAD, factory(runtime(tmp_path), solve([])), run=run, splits=splits)

    def test_the_budget_stops_the_run_and_the_result_says_so(self, tmp_path, suite, splits):
        budget = Budget(max_steps=8, max_tool_calls=1)
        run = RunManifest("base", "base", "dev", {"model": "scripted"}, data=splits.hash(), budget=budget)
        calls = [("api_search", {"query": "slack"})] * 3
        r = run_attempt(suite, LEAD, factory(runtime(tmp_path), solve(calls), budget), run=run,
                        splits=splits).result
        assert r.termination == "budget" and r.validity == "valid" and r.tool_calls == 1

    def test_a_test_task_session_is_held_out(self, tmp_path, suite):
        fams, rule = ab.families(suite)
        held = SplitManifest("marketing", suite.versions, rule, suite.entries(fams),
                             {f: "test" for f in fams.values()})
        run = RunManifest("base", "base", "test", {"model": "scripted"}, data=held.hash())
        rt = runtime(tmp_path)
        r = run_attempt(suite, LEAD, factory(rt, solve([])), run=run, splits=held).result
        sessions, _ = rt.ledger.sessions()
        assert sessions[r.evidence]["held_out"] is True and sessions[r.evidence]["task"] == LEAD

    def test_run_tasks_records_every_attempt_as_it_finishes(self, tmp_path, suite, splits):
        run = RunManifest("base", "base", "dev", {"model": "scripted"}, data=splits.hash(), repeats=2)
        results = Results(tmp_path / "ws", "marketing")
        rows = run_tasks(suite, [LEAD], factory(runtime(tmp_path), solve([])), run=run, splits=splits,
                         results=results, repeats=2)
        assert [r.attempt for r in rows] == [0, 1]
        assert len(results.of_run(run.hash())) == 2


class TestBudgeted:
    def test_a_refused_call_tells_the_model_the_run_is_over(self, suite):
        surface = Budgeted(suite.episode(LEAD), Budget(max_tool_calls=0))
        res = surface.run("api_search", {"query": "slack"})
        assert res.is_error and "budget is exhausted" in res.output and surface.exhausted
        assert surface.episode.tool_calls == 0      # and the call never reached the world

    def test_it_offers_the_episode_s_tools(self, suite):
        surface = Budgeted(suite.episode(LEAD), Budget())
        assert [s.name for s in surface.specs()] == ["api_search", "api_fetch", "base64_encode"]


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


UPDATE = ("api_fetch", {"method": "PATCH", "url": HUBSPOT, "body": json.dumps({"id": "c1", "stage": "opportunity"})})
POST = ("api_fetch", {"method": "POST", "url": SLACK,
                      "body": json.dumps({"channel": "C1", "text": "quarterly pipeline review"})})


class TestCompletionProbe:
    """The stop point's environment label: was the task actually complete when the agent decided to stop?

    The probe is called directly here, on an episode and a decision the test records: the runner shell asks no
    decision point, so no attempt makes a stop decision for it to label (docs/design/SLIMMING_INVENTORY.md §0.1 D;
    the bridge from an attempt to the stop point is v2 work). What an attempt does with `probe` is checked at the
    end: refused outside train, and the scorer never consulted while the agent works."""

    def probe(self, tmp_path, suite, **kw):
        from ouroloop.train.runtime import Runtime as TrainRuntime
        rt = TrainRuntime(tmp_path / "ws")
        episode = suite.episode(LEAD)
        surface = Budgeted(episode, Budget())
        return rt, surface, ab.CompletionProbe(rt, episode, surface=surface, **kw)

    def stop(self, rt, probe, surface, changes: int) -> str:
        """A stop decision made now, snapped as a host would snap it."""
        did = rt.ledger.decision(session="s1", point="stop", ctx="s1#0", phase="on_finish")
        probe.snap(did, changes)
        return did

    def labels(self, rt) -> dict:
        return {e["decision"]: e for e in rt.ledger.events() if e["type"] == "outcome"}

    def test_a_finished_task_labels_its_stop_decision_yes(self, tmp_path, suite):
        rt, surface, probe = self.probe(tmp_path, suite)
        for name, args in (UPDATE, POST):
            surface.run(name, args)
        did = self.stop(rt, probe, surface, 2)
        assert probe.label() == 1
        label = self.labels(rt)[did]
        assert (label["label"], label["score"], label["assertions"]) == ("yes", 1.0, "2/2")

    def test_stopping_too_early_is_labelled_no(self, tmp_path, suite):
        """One of the two required writes done: the task is not complete, and the stop decision says so."""
        rt, surface, probe = self.probe(tmp_path, suite)
        surface.run(*UPDATE)
        did = self.stop(rt, probe, surface, 1)
        assert probe.label() == 1
        assert (self.labels(rt)[did]["label"], self.labels(rt)[did]["score"]) == ("no", 0.5)

    def test_a_label_is_an_env_outcome_that_training_may_read(self, tmp_path, suite):
        rt, surface, probe = self.probe(tmp_path, suite)
        self.stop(rt, probe, surface, 0)
        probe.label()
        outcomes = list(self.labels(rt).values())
        assert outcomes and all(e["source"] == "env" and e["note"] == "completion probe" for e in outcomes)
        assert rt.ledger.training_rows("stop")          # the builder can draw from these

    def test_each_state_is_labelled_from_its_own_snapshot(self, tmp_path, suite):
        """The decision made before the second write must not be labelled from the state after it, and decisions on
        one state share its snapshot."""
        rt, surface, probe = self.probe(tmp_path, suite)
        surface.run(*UPDATE)
        early, same = self.stop(rt, probe, surface, 1), self.stop(rt, probe, surface, 1)
        surface.run(*POST)
        late = self.stop(rt, probe, surface, 2)
        assert len(probe.snaps) == 2 and probe.label() == 3
        labels = self.labels(rt)
        assert [(labels[d]["label"], labels[d]["score"]) for d in (early, same, late)] == [("no", 0.5), ("no", 0.5),
                                                                                         ("yes", 1.0)]
        assert all(s["world"] is None for s in probe.snaps)        # the worlds are let go once labelled

    def test_the_probe_scores_only_when_it_labels(self, tmp_path, suite, monkeypatch):
        """An official scorer used as an online checker would quietly correct the agent's mistakes: a snapshot is a
        copy, and scoring happens in label(), after the episode."""
        seen = []
        original = ab.Episode.score
        monkeypatch.setattr(ab.Episode, "score", lambda self, world=None: (seen.append(1), original(self, world))[1])
        rt, surface, probe = self.probe(tmp_path, suite)
        surface.run(*UPDATE)
        self.stop(rt, probe, surface, 1)
        assert seen == []
        probe.label()
        assert seen == [1]

    def test_a_world_that_cannot_be_copied_stays_unlabelled(self, tmp_path, suite, monkeypatch):
        rt, surface, probe = self.probe(tmp_path, suite)
        monkeypatch.setattr(surface.episode, "snapshot", lambda: (_ for _ in ()).throw(RuntimeError("no copy")))
        did = self.stop(rt, probe, surface, 0)
        assert probe.label() == 0
        label = self.labels(rt)[did]
        assert label["label"] is None and "could not be copied" in label["error"]
        assert not rt.ledger.training_rows("stop")

    def test_a_scorer_that_fails_leaves_the_label_unknown_never_no(self, tmp_path, suite, monkeypatch):
        rt, surface, probe = self.probe(tmp_path, suite)
        did = self.stop(rt, probe, surface, 0)
        monkeypatch.setattr(ab.Episode, "score", lambda self, world=None: 1 / 0)
        assert probe.label() == 0
        label = self.labels(rt)[did]
        assert label["label"] is None and label["error"].startswith("ZeroDivisionError")

    def test_snapshots_are_bounded(self, tmp_path, suite):
        rt, surface, probe = self.probe(tmp_path, suite, max_snapshots=2)
        for i in range(5):
            probe.snap(f"d{i}", i)
        assert sum(s["world"] is not None for s in probe.snaps) == 2 and probe.dropped == 3

    def test_a_read_does_not_count_as_a_change(self, suite):
        ep = suite.episode(LEAD)
        assert ep.mutates("api_fetch", {"method": "POST", "url": SLACK})
        assert not ep.mutates("api_fetch", {"method": "GET", "url": SLACK})
        assert not ep.mutates("api_search", {"query": "slack"})
        assert not ep.mutates("base64_encode", {"text": "hi"})

    def test_the_wrapped_surface_still_reports_changes(self, suite):
        surface = Budgeted(suite.episode(LEAD), Budget())
        assert surface.mutates("api_fetch", {"method": "POST", "url": SLACK})
        assert not surface.mutates("api_search", {"query": "slack"})

    def test_a_surface_that_cannot_report_changes_is_refused(self, tmp_path, suite):
        class Opaque:
            def specs(self):
                return []

            def run(self, name, args):
                return None

        with pytest.raises(ab.UpstreamError, match="which calls change the world"):
            ab.CompletionProbe(None, suite.episode(LEAD), surface=Opaque())

    # ---------------------------------------------------------------- what an attempt does with `probe`
    def dev(self, suite) -> SplitManifest:
        fams, rule = ab.families(suite)
        return SplitManifest("marketing", suite.versions, rule, suite.entries(fams), {f: "dev" for f in fams.values()})

    def test_an_attempt_refuses_the_probe_outside_train(self, tmp_path, suite):
        dev = self.dev(suite)
        run = RunManifest("base", "base", "dev", {"model": "scripted"}, data=dev.hash())
        with pytest.raises(ValueError, match="only the train split"):
            run_attempt(suite, LEAD, factory(runtime(tmp_path), solve([])), run=run, splits=dev, probe=True)

    def test_only_the_train_split_is_trainable(self, tmp_path, suite):
        """dev decides between candidates, so its sessions are held out from training just as test's are."""
        dev = self.dev(suite)
        run = RunManifest("base", "base", "dev", {"model": "scripted"}, data=dev.hash())
        rt = runtime(tmp_path)
        result = run_attempt(suite, LEAD, factory(rt, solve([])), run=run, splits=dev).result
        sessions, _ = rt.ledger.sessions()
        assert sessions[result.evidence]["held_out"] is True

    def test_the_scorer_is_never_consulted_during_an_attempt(self, tmp_path, suite, monkeypatch):
        seen = []
        original = ab.Episode.score
        monkeypatch.setattr(ab.Episode, "score",
                            lambda self, world=None: (seen.append(len(self.calls)), original(self, world))[1])
        fams, rule = ab.families(suite)
        train = SplitManifest("marketing", suite.versions, rule, suite.entries(fams),
                              {f: "train" for f in fams.values()})
        run = RunManifest("base", "base", "train", {"model": "scripted"}, data=train.hash(),
                          budget=Budget(max_steps=8, max_tool_calls=8))
        result = run_attempt(suite, LEAD, factory(runtime(tmp_path), solve([UPDATE])), run=run, splits=train,
                             probe=True).result
        # Every scoring happened after the last tool call, never between them.
        assert seen and all(n == result.tool_calls == 1 for n in seen)


class TestTheInterventionWorks:
    """Whether the stop point, once it acts, can change a benchmark task's outcome.

    A Marketing task's `task_completed_correctly` is a conjunction over a median of twelve scored assertions, so a
    task is lost by leaving one requirement undone. This is the mechanism that is supposed to catch that; these
    tests check that it is wired, not that a model would judge well.
    """

    def train(self, suite):
        fams, rule = ab.families(suite)
        return SplitManifest("marketing", suite.versions, rule, suite.entries(fams),
                             {f: "train" for f in fams.values()})

    def half_then_finish_when_pushed(self):
        """Stops after one of the two required writes; does the second only if sent back to work."""
        def script(messages):
            done = {json.loads(m["results"][0]["output"]).get("ok") for m in messages if m.get("role") == "tool"}
            writes = sum(1 for m in messages if m.get("role") == "tool")
            pushed = any(m.get("role") == "user" and "Keep working" in str(m.get("content", ""))
                         for m in messages[1:])
            if writes == 0:
                return ChatTurn("", [ToolCall("c0", "api_fetch",
                                              {"method": "PATCH", "url": HUBSPOT,
                                               "body": json.dumps({"id": "c1", "stage": "opportunity"})})],
                                "tool_use")
            if pushed and writes == 1:
                return ChatTurn("", [ToolCall("c1", "api_fetch",
                                              {"method": "POST", "url": SLACK,
                                               "body": json.dumps({"channel": "C1",
                                                                   "text": "quarterly pipeline review"})})],
                                "tool_use")
            return ChatTurn("All done." if done else "Done.", [], "end")
        return ScriptedChat(script)

    def attempt(self, tmp_path, suite, points, backends=None, gated=None, confidence=0.9):
        splits = self.train(suite)
        run = RunManifest("base", "base", "train", {"model": "scripted"}, data=splits.hash(),
                          budget=Budget(max_steps=8, max_tool_calls=8))
        rt = Runtime(tmp_path / "ws", backends or {"mock": KeywordBackend()},
                     "mock" if not backends else next(iter(backends)), seed=0)
        for p in points:
            rt.register(p)
        if gated is not None:
            gated(rt, "stop", "fixed-1", confidence - 0.3)
        return rt, run_attempt(suite, LEAD, factory(rt, self.half_then_finish_when_pushed()), run=run,
                               splits=splits, probe=True)

    def test_without_the_point_acting_the_task_is_lost(self, tmp_path, suite, every_stop_may_act):
        rt, at = self.attempt(tmp_path, suite, builtin_points())
        assert at.result.success is False and at.result.partial_credit == 0.5
        assert at.result.labelled > 0          # and the miss is on record as a "no" for the stop point
        assert all(d.get("no") == 1.0 for d in [r.target()[0] for r in rt.ledger.rows() if r.target()])

    def test_a_confident_not_done_sends_the_agent_back_and_the_task_passes(self, tmp_path, suite, gated,
                                                                          every_stop_may_act):
        from ouroloop.points import stop_point
        from ouroloop.train.policy import Policy
        rt, at = self.attempt(tmp_path, suite, [stop_point(policy=Policy(act=True, explore=0.0))],
                              backends={"fixed": NotDone(0.9)}, gated=gated)
        assert at.result.success is True and at.result.partial_credit == 1.0
        assert at.result.steps > 2             # it was sent back to work
        # The same scripted model, the same task: the only difference is the point acting.
        scores = sorted(e["score"] for e in rt.ledger.events() if e["type"] == "outcome")
        assert scores[0] == 0.5 and scores[-1] == 1.0

    def test_an_unconfident_answer_takes_the_default_and_changes_nothing(self, tmp_path, suite, gated,
                                                                        every_stop_may_act):
        from ouroloop.points import stop_point
        from ouroloop.train.policy import Policy
        rt, at = self.attempt(tmp_path, suite, [stop_point(policy=Policy(act=True, explore=0.0))],
                              backends={"fixed": NotDone(0.55)}, gated=gated, confidence=0.9)
        assert at.result.success is False      # below the threshold: on_finish keeps its default, "yes"


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


class TestConcurrency:
    def test_concurrent_attempts_are_all_recorded_in_the_plans_order(self, tmp_path, suite, splits):
        """[bench] concurrency (M5 §2): several attempts at a time, each with its own episode and session; the
        results, the grading rows and the caller's callback are recorded for every one, and the returned list
        follows the plan's order rather than the finishing order."""
        import threading
        import time
        in_flight, peak, lock = [0], [0], threading.Lock()

        def script(messages):
            with lock:
                in_flight[0] += 1
                peak[0] = max(peak[0], in_flight[0])
            time.sleep(0.05)
            with lock:
                in_flight[0] -= 1
            return ChatTurn("Done.", [], "end")

        run = RunManifest("base", "base", "dev", {"model": "scripted"}, data=splits.hash(), repeats=3)
        results = Results(tmp_path / "ws", "marketing")
        seen = []
        order = [(t, i) for i in range(3) for t in (LEAD, HOLD)]
        rows = run_tasks(suite, [LEAD, HOLD], factory(runtime(tmp_path), ScriptedChat(script)), run=run,
                         splits=splits, results=results, order=order, on_result=seen.append, concurrency=4)
        assert [(r.task_id, r.attempt) for r in rows] == order
        assert len(results.of_run(run.hash())) == 6 and len(seen) == 6
        assert len({r.attempt_id for r in rows}) == 6 and len({r.evidence for r in rows}) == 6
        assert peak[0] > 1                                  # they did overlap
        transcripts = list((tmp_path / "ws" / "transcripts").glob("*.jsonl"))
        assert len(transcripts) == 6
        for path in transcripts:
            for line in path.read_text().splitlines():
                json.loads(line)                            # every appended line is whole

    def test_the_live_keys_are_hidden_once_for_the_whole_pool(self, tmp_path, suite, splits, monkeypatch):
        """A per-call pop and restore of os.environ from several threads would race with every other reader
        (the claude CLI's child environment): under concurrency the keys are hidden before the pool starts and
        restored after it ends."""
        import os
        monkeypatch.setenv("OPENAI_API_KEY", "live-key-that-must-not-leak")
        seen = []

        def script(messages):
            seen.append("OPENAI_API_KEY" in os.environ)
            return ChatTurn("Done.", [], "end")

        run = RunManifest("base", "base", "dev", {"model": "scripted"}, data=splits.hash(), repeats=2)
        run_tasks(suite, [LEAD, HOLD], factory(runtime(tmp_path), ScriptedChat(script)), run=run, splits=splits,
                  results=Results(tmp_path / "ws", "marketing"), repeats=2, concurrency=3)
        assert seen and not any(seen)                              # hidden during every attempt
        assert os.environ["OPENAI_API_KEY"] == "live-key-that-must-not-leak"     # and back afterwards

    def test_concurrency_one_is_the_serial_loop(self, tmp_path, suite, splits):
        run = RunManifest("base", "base", "dev", {"model": "scripted"}, data=splits.hash(), repeats=2)
        results = Results(tmp_path / "ws", "marketing")
        rows = run_tasks(suite, [LEAD], factory(runtime(tmp_path), solve([])), run=run, splits=splits,
                         results=results, repeats=2, concurrency=1)
        assert [r.attempt for r in rows] == [0, 1] and len(results.of_run(run.hash())) == 2

    def test_an_interrupt_lets_running_attempts_finish_and_starts_no_more(self, tmp_path, suite, splits):
        """Stopped mid-run: what is in flight is recorded, so the book and the results agree; the rest never
        starts."""
        import threading
        import time
        started, lock, stop = [], threading.Lock(), threading.Event()

        def script(messages):
            with lock:
                started.append(1)
                first = len(started) == 1
            if first:
                stop.set()                                  # the first call asks the main thread to interrupt
                time.sleep(0.2)
            return ChatTurn("Done.", [], "end")

        run = RunManifest("base", "base", "dev", {"model": "scripted"}, data=splits.hash(), repeats=6)
        results = Results(tmp_path / "ws", "marketing")
        order = [(LEAD, i) for i in range(6)]
        main = threading.main_thread()

        def interrupt():
            stop.wait(5)
            import _thread
            _thread.interrupt_main()

        threading.Thread(target=interrupt, daemon=True).start()
        with pytest.raises(KeyboardInterrupt):
            run_tasks(suite, [LEAD], factory(runtime(tmp_path), ScriptedChat(script)), run=run, splits=splits,
                      results=results, order=order, concurrency=2)
        recorded = results.of_run(run.hash())
        assert 1 <= len(recorded) < 6                       # the running ones were kept, the rest never started
        assert main is threading.main_thread()
