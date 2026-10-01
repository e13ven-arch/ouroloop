"""What the first real runs with GLM-5.3 exposed (docs/JOURNAL.md, 2026-10-01), each fixed and held here.

The failure-path run measured a train task that failed, wrote a note, proposed a skill, revised a refused draft and
built the candidate, stopping before the gate. Five independent checks against its records found these.
"""
import json

import httpx
import pytest

from ouroloop import policies as P
from ouroloop.agent import harness_package as hp
from ouroloop.chat import ChatTurn
from ouroloop.evolve import contracts as ct
from ouroloop.evolve.execution import Budgets, MeteredProvider, Pricing
from ouroloop.providers import ProviderError
from ouroloop.providers.openai_compat import OpenAICompatProvider
from ouroloop.research import diagnosis as dg
from ouroloop.research.basic import PACKAGE_API, PACKAGE_FORMAT, REVISE_FORMAT, PolicyResearcher


# ---------------------------------------------------------------------------- every request is booked
def _meter(tmp_path, provider, **kw):
    account = Budgets(tmp_path).account("run", {"usd": 100.0})
    return MeteredProvider(provider, account, Pricing(0.003, 0.015, 4000, 1000, source="snapshot"), **kw)


def test_a_provider_that_retries_by_itself_hands_its_retries_to_the_meter(tmp_path):
    """The GLM provider retried timeouts four times below the meter: a 236 s summary call was, by its duration and
    its cached tokens, two requests and one reservation. Now every request is its own reservation. The provider
    is not changed for good — it may be shared — its own retries are off only during a metered call."""
    class Counting:
        name, model = "p", "m"

        def __init__(self):
            self.retries, self.seen = 4, []

        def chat(self, system, messages, specs=()):
            self.seen.append(self.retries)
            return ChatTurn("ok", [], "end", usage={"input_tokens": 1, "output_tokens": 1})
    inner = Counting()
    first, second = _meter(tmp_path, inner, max_retries=0), _meter(tmp_path / "b", inner, max_retries=0)
    assert first.max_retries == second.max_retries == 4 and inner.retries == 4
    first.chat("s", [])
    assert inner.seen == [0] and inner.retries == 4


def test_a_failure_that_never_reached_the_model_releases_its_reservation(tmp_path):
    class RateLimited:
        name, model, retries = "p", "m", 0

        def chat(self, system, messages, specs=()):
            raise ProviderError("HTTP 429: slow down", retryable=False, unbilled=True)
    meter = _meter(tmp_path, RateLimited(), max_retries=0)
    with pytest.raises(ProviderError):
        meter.chat("s", [])
    assert [r.status for r in meter.receipts] == ["released"]


def test_a_request_the_provider_refused_is_not_sent_and_paid_for_again(tmp_path):
    class Refuses:
        name, model, retries = "p", "m", 0
        calls = 0

        def chat(self, system, messages, specs=()):
            self.calls += 1
            raise ProviderError("HTTP 400: context too long", retryable=False)
    provider = Refuses()
    meter = _meter(tmp_path, provider, max_retries=3)
    with pytest.raises(ProviderError):
        meter.chat("s", [])
    assert provider.calls == 1 and [r.status for r in meter.receipts] == ["unknown"]


def test_a_retryable_failure_is_retried_and_each_attempt_is_booked(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    class Flaky:
        name, model, retries = "p", "m", 0
        calls = 0

        def chat(self, system, messages, specs=()):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("ReadTimeout", retryable=True)
            return ChatTurn("ok", [], "end", usage={"input_tokens": 10, "output_tokens": 5})
    meter = _meter(tmp_path, Flaky(), max_retries=2)
    assert meter.chat("s", []).text == "ok"
    assert [r.status for r in meter.receipts] == ["unknown", "settled"]


@pytest.mark.parametrize("status,retryable", [(400, False), (503, True), (429, True)])
def test_the_openai_compatible_provider_says_which_failures_are_worth_retrying(status, retryable):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, text="no")))
    provider = OpenAICompatProvider("glm-5.3", retries=0, client=client)
    with pytest.raises(ProviderError) as caught:
        provider.chat("s", [{"role": "user", "content": "x"}], ())
    assert caught.value.retryable is retryable
    assert OpenAICompatProvider("m").timeout >= 600          # a long reasoning reply is not cut at two minutes


# ---------------------------------------------------------------------------- what R is given
class Row:
    task_id = family_id = "t1"
    split, success, validity, termination, partial_credit, evidence = "train", False, "valid", "finished", 0.75, ""
    attempt_id = "a/t1"


def test_the_summary_keeps_the_end_of_an_entry_and_the_agents_last_message_whole():
    """A head-only cut at 600 characters hid the agent's own account of the email it chose not to send, and the
    note then called the omission silent."""
    account = "I checked every item. " + "x" * 2000 + " I did NOT send the email because Legal was revoked."
    transcript = [{"kind": "task", "text": "do it"},
                  {"kind": "assistant", "text": "", "tool_calls": [{"name": "api_search", "args": {}}]},
                  {"kind": "tool", "name": "api_search", "is_error": False, "output": "head " + "y" * 3000 + " tail"},
                  {"kind": "assistant", "text": account, "tool_calls": []}]
    s = dg.failure_summary(Row(), transcript, [], dg.DiagnosisLimits(max_entry_chars=600))
    output = s["turns"][0]["results"][0]["output"]
    assert output.startswith("head") and output.endswith("tail") and "characters cut" in output
    assert s["turns"][-1]["text"] == account                     # the last message, whole under max_task_chars
    assert dg.clip("a" * 10, 20) == "a" * 10


def test_r_is_told_the_package_api_and_a_refusal_names_what_was_expected():
    for fragment in ('"path": "skills/<name>.md"', "before_tool|after_tool|on_finish", "def f(ctx, event)",
                     "ctx.call_tool", "tests/*.py"):
        assert fragment in PACKAGE_API
    assert PACKAGE_API in PACKAGE_FORMAT and PACKAGE_API in REVISE_FORMAT
    with pytest.raises(hp.PackageError, match=r"a skill entry is an object with the keys \['name', 'path'\]"):
        hp.parse_manifest(json.dumps({**hp.empty_manifest(), "skills": ["finish_audit"]}), {})


def test_r_sees_the_instruction_text_w_runs_with_so_it_does_not_repeat_it(fake_upstream, tmp_path):
    from test_harness_loop import v06_prepared
    from ouroloop import bench
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    work = bench.improvement(activity)
    evidence = work.measure(w0, None)
    context = work.context(goal="g", release_ref=w0, evidence=evidence)
    finish = context.targets["worker.finish"]
    assert finish["current"] and "finish" in finish["current"].lower()


def test_a_revision_is_shown_the_parent_package_it_patches(tmp_path):
    from ouroloop.research.drafts import Drafts
    objects = ct.Objects(tmp_path)
    parent = hp.build_package(objects, {"manifest.json": json.dumps({**hp.empty_manifest(), "memory": {
        "enabled": True}}), "memory.md": "Existing note."}, hp.Limits())
    seen = {}

    def revise(prompt, patch, check_output, parent=None):
        seen["parent"] = parent
        return {"write": {"skills/s.md": "x"}}
    drafts = Drafts(objects, parent_ref=parent, limits=hp.Limits(), max_repairs=1)
    proposal = ct.ExperimentProposal(owner="worker", hypothesis="h", changes=(
        {"target": "worker.harness", "kind": "workflow", "change": {"patch": {"write": {"skills/s.md": "x"}}}},))
    drafts.resolve(proposal, revise=revise, prompt="p")
    assert seen["parent"]["memory.md"] == "Existing note."


def test_the_summary_reads_the_latest_experience_retrieval_hands_it():
    """research.retrieve returns the newest rows first; the summary keeps the first ten of that order."""
    from ouroloop.research.experiment import retrieve

    class Replies:
        name = "fake"

        def complete(self, system, prompt):
            Replies.state = json.loads(prompt)
            return json.dumps({"next_action": "stop", "reason": "r"})
    rows = [{"kind": "experiment", "experiment": f"x{i}", "verdict": "rejected", "reasons": ["regress"],
             "hypothesis": f"h{i}", "ts": f"2026-10-01T00:00:{i:02d}Z"} for i in range(25)]
    retrieved = retrieve(rows, {"builtin": "research_retrieve.v1", "max_items": 20, "sort": "recency"})
    context = type("C", (), {"goal": "g", "targets": {}, "budget": {}, "evidence": ()})()
    PolicyResearcher(Replies()).summarize(context, "p", [], {}, retrieved)
    assert [r["hypothesis"] for r in Replies.state["experience"]] == [f"h{i}" for i in range(24, 14, -1)]


def test_the_summary_sees_the_budget_as_it_is_after_the_notes_ran(tmp_path):
    from ouroloop.research.experiment import propose_experiment
    seen = {}

    class Researcher:
        requests: list = []

        def note_request(self, summary, prompt, *, allowed=()):
            return prompt, json.dumps(summary)

        def note(self, system, user):
            return {"failure_class": "c", "root_cause": "r", "mechanism": "m", "component": "skills",
                    "generality": "g"}

        def summarize(self, context, prompt, notes, coverage, experience=(), *, package=None, allowed=()):
            from ouroloop.research.experiment import ProposalResult
            seen["budget"] = context.budget
            return ProposalResult(next_action="stop", reason="r")
    objects = ct.Objects(tmp_path)
    bound = P.BoundRuntime(release_ref="", profile="task", authorization="x", services=P.Services(objects))
    bound.research["research.diagnose"] = type("D", (), {"output_schema": "failure_note.v1"})()
    diagnosis = dg.Diagnosis(summaries=[{"task_id": "t1", "checks": []}], order=["t1"], components=("skills",))
    context = type("C", (), {})()
    import dataclasses

    @dataclasses.dataclass
    class Ctx:
        budget: dict
    propose_experiment(Ctx(budget={"stale": True}), bound, researcher=Researcher(), objects=objects,
                       diagnosis=diagnosis, live_budget=lambda: {"remaining": {"usd": 4.85}})
    assert seen["budget"] == {"remaining": {"usd": 4.85}}


# ---------------------------------------------------------------------------- the duplicate check works
def test_a_proposal_this_activity_already_froze_is_refused_as_a_duplicate(tmp_path):
    from ouroloop.research.experiment import admit, frozen_signatures
    objects = ct.Objects(tmp_path)
    proposal = ct.ExperimentProposal(owner="worker", hypothesis="h", changes=(
        {"target": "worker.system_prompt", "kind": "prompt", "change": {"text": "x"}},))
    ref = objects.put(proposal)
    ran = {"kind": "experiment", "proposal": ref, "release_before": "rel-1", "build": "ready",
           "verdict": "rejected", "reasons": ["below the plan's min_gain"]}
    history = frozen_signatures([ran], objects, parent="rel-1")
    assert len(history) == 1
    context = type("C", (), {"inspection": {}, "evidence": (), "feedback": {}})()
    admission = admit(proposal, context=context, core=ct.CoreManifest(domain="rsi"), objects=objects,
                      checks=("duplicate_experiment",), history=history)
    assert admission.status == "invalid" and "already frozen" in admission.reasons[0]
    # the same change on another parent is a new question, and a run that produced no comparison is a retry
    assert frozen_signatures([ran], objects, parent="rel-2") == ()
    lost = {**ran, "verdict": "inconclusive", "reasons": ["3 pairs are incomplete: ['t1']"]}
    not_installed = {**ran, "verdict": "accepted", "installation": "installation_failed"}
    assert frozen_signatures([lost, not_installed], objects, parent="rel-1") == ()


def test_the_round_records_the_revision_requests_and_rs_requirements_whole(fake_upstream, tmp_path):
    from test_harness_loop import NoteResearcher, v06_prepared
    from ouroloop import bench
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    bench.improve(cfg, ".", out=lambda _line: None, researcher=NoteResearcher())      # its first draft is refused
    steps = list(ct.Events(activity.root).of("research_steps"))
    propose = [e for e in steps if e.get("stage") == "propose"]
    revise = [e for e in steps if e.get("stage") == "revise"]
    assert propose and propose[0]["requirements"][0]["observed_behavior"] == "stops after the update"
    assert revise and [s["step"] for s in revise[0]["steps"]] == ["revise"]


def test_a_revision_record_is_not_counted_as_a_round_that_used_r1(fake_upstream, tmp_path):
    from test_cycle import R1_PROPOSE, cycle_config, gap_w0
    from ouroloop import bench
    preset = json.loads(json.dumps(bench.PRESETS["marketing_harness_v1"]))
    preset["domain"] = "rsi"
    preset["harness"].update(components=["memory", "skills"], sandbox=None)
    bench.PRESETS["rsi_harness_test"] = preset
    try:
        cfg = cycle_config(fake_upstream, tmp_path)
        activity, booted = gap_w0(cfg, tmp_path)
        r1 = P.default_research_set(activity.objects, note_contract=True, propose=R1_PROPOSE)
        digest = ct.digest({"text": R1_PROPOSE})[:16]
        events = ct.Events(activity.root)
        events.append("research_steps", scope="cyc", stage="revise", research_ref=r1, context_ref="",
                      steps=[{"step": "revise"}], prompts={"research.propose": digest})
        assert bench._reuse_fact(activity, r1, booted.research_ref, "segment-1")["state"] == "not_completed"
    finally:
        bench.PRESETS.pop("rsi_harness_test", None)


def test_r_is_told_the_change_shape_of_a_prompt_target_too():
    """The gate run's proposal edited worker.finish in a package patch's shape and the build refused it: the
    format had only ever shown the package change's shape."""
    assert '"kind": "prompt", "change": {"text": "<the complete new text>"}' in PACKAGE_FORMAT
    assert P.parse_prompt({"text": "Before finishing, check every deliverable."})["text"]
    with pytest.raises(Exception):
        P.parse_prompt({"patch": {"write": {"instruction": "x"}}})


# ---------------------------------------------------------------------------- a reply cut at the output limit
def test_a_research_reply_cut_at_the_output_limit_is_refused_not_read_as_a_decision():
    """The gate run's summary call spent its 16384 output tokens mostly reasoning; its JSON was cut before the
    proposal, read as nothing, and turned into "collect" by the fallback."""
    from ouroloop.bench import _Completions
    from ouroloop.providers import ReplyTruncated

    class Cut:
        name = "glm"

        def chat(self, system, messages, specs=()):
            return ChatTurn('{"requirements": [{"id": "r1", "observed_behavior": "x"}], "next_action": "coll',
                            [], "max_tokens", usage={"completion_tokens": 16384})
    with pytest.raises(ReplyTruncated, match="raise max_tokens"):
        _Completions(Cut()).complete("s", "p")


def test_an_unreadable_summary_reply_is_an_invalid_round_not_a_collect_request():
    from ouroloop.research.experiment import propose_experiment

    class Garbled:
        name = "fake"

        def complete(self, system, prompt):
            return "I think we should merge the finish check {not json"
    researcher = PolicyResearcher(Garbled())
    context = type("C", (), {"goal": "g", "targets": {}, "budget": {}, "evidence": ()})()
    result = researcher.summarize(context, "p", [], {})
    assert getattr(result, "unreadable", False) and result.reason == "the research reply was not readable JSON"
    objects = ct.Objects.__new__(ct.Objects)
    bound = P.BoundRuntime(release_ref="", profile="task", authorization="x", services=P.Services(None))
    bound.research["research.diagnose"] = type("D", (), {"output_schema": "failure_note.v1"})()
    diagnosis = dg.Diagnosis(summaries=[], order=[], components=("skills",))
    import dataclasses

    @dataclasses.dataclass
    class Ctx:
        goal: str = "g"
        targets: dict = dataclasses.field(default_factory=dict)
        budget: dict = dataclasses.field(default_factory=dict)
        evidence: tuple = ()
    out = propose_experiment(Ctx(), bound, researcher=researcher, objects=objects, diagnosis=diagnosis)
    assert out.next_action == "none" and out.proposals == []          # not turned into "collect"


def test_a_model_may_not_ask_for_more_output_than_its_reservation_prices(fake_upstream, tmp_path):
    from test_harness_loop import v06_prepared
    from ouroloop import bench
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    cfg["llm"] = {**cfg["llm"], "max_tokens": 32768}
    cfg["pricing"] = {"input_per_1k": 0.0014, "output_per_1k": 0.0044, "max_input_tokens": 100000,
                      "max_output_tokens": 16384, "source": "test"}
    problems = bench.startup_checks(activity, cfg, phase="improve")
    assert any("max_tokens 32768 is above [pricing] max_output_tokens 16384" in p for p in problems)
