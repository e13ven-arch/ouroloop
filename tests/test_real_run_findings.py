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
    assert output.startswith("head") and output.endswith("tail")
    # the mark says whose cut it is: a mark that read like the agent's own made R hook against it (2026-10-03)
    assert "characters left out of this summary; the agent had the whole text" in output
    assert s["turns"][-1]["text"] == account                     # the last message, whole under max_task_chars
    assert dg.clip("a" * 10, 20) == "a" * 10


def test_r_is_told_the_package_api_and_a_refusal_names_what_was_expected():
    for fragment in ('"path": "skills/<name>.md"', "before_tool|after_tool|on_finish", "def f(ctx, event)",
                     "ctx.call_tool", "tests/*.py"):
        assert fragment in PACKAGE_API
    assert PACKAGE_API in PACKAGE_FORMAT and PACKAGE_API in REVISE_FORMAT
    with pytest.raises(hp.PackageError, match=r"a skill entry is an object with the keys \['name', 'path'\]"):
        hp.parse_manifest(json.dumps({**hp.empty_manifest(), "skills": ["finish_audit"]}), {})


def test_r_is_told_the_v2_api_the_ctx_surface_and_that_a_v1_package_runs_under_the_default_agent():
    """docs/modules/00_CONTRACTS.md §2 (包 API v2, v0.3): the text R writes from names both manifest versions and the
    optional `agent` entry with its roles, says a package without one runs under the default agent exactly as
    before, describes the five ctx names in the shapes the gateway and broker enforce (role must be declared, effort
    capped at K's max_effort and never refused, the model's calls are the agent's to execute, a limit ends the
    attempt), the view's keys, and that ctx.mark is self-report only — and still carries the v1 rules."""
    from ouroloop.agent.gateway import EFFORTS
    for fragment in ('"api_version": "harness_package.v1" or "harness_package.v2"',
                     '"agent": {"entrypoint": "agent/<file>.py:run", "roles": ["act"] or ["act", "aux"]}',
                     "A package without an agent entry runs under the default agent",
                     "def run(ctx, task) -> str", "returning is finishing",
                     'ctx.model(messages, tools=(), role="act", effort=None) -> .text, .tool_calls [{id, name, args}], .stop',
                     "role must be one the manifest's roles declare, else the call is refused as an error you may catch",
                     "a request above K's max_effort is sent at max_effort, never refused",
                     "the calls the model returns are yours to execute",
                     "ctx.tool(name, args, read_only=False) -> .output, .is_error",
                     "ctx.skills.index", "ctx.skills.read(name)", "ctx.skills.memory",
                     "ctx.limits is the remaining act_calls, model_calls, tool_calls, seconds and tokens",
                     "ctx.view = {task, system, base_tools, package, policies, events, max_output_chars}",
                     "ctx.mark(kind, **data) records a note labelled as the package's own report; nothing reads it as evidence",
                     "|".join(EFFORTS)):
        assert fragment in PACKAGE_API, fragment
    assert PACKAGE_API.count('"agent"') == 1                      # one entry, said once
    from ouroloop.research.basic import NOTE_FORMAT
    assert "memory|skills|on_finish|before_tool|after_tool|tools|agent" in NOTE_FORMAT


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


def test_a_research_slot_proposal_from_the_real_researcher_is_a_valid_proposal():
    """Found by the requirements audit: propose_research stamped origin "automatic", which is not an origin, so
    every real research-slot proposal raised instead of reaching admission. Scripted researchers hid it."""
    class Replies:
        name = "fake"

        def complete(self, system, prompt):
            return json.dumps({"next_action": "none", "reason": "r", "proposals": [
                {"hypothesis": "h", "target": "research.propose", "text": "A better propose prompt."}]})
    context = type("C", (), {"goal": "g", "budget": {}})()
    result = PolicyResearcher(Replies()).propose_research(context, "p", {"research.propose": "old"})
    assert len(result.proposals) == 1 and result.proposals[0].origin == "llm"
    assert result.proposals[0].owner == "research"


# ---------------------------------------------------------------------------- the account ran out mid-gate
def test_an_exhausted_account_is_an_account_error_not_a_retryable_rate_limit():
    """The gate run's third attempt met Zhipu's HTTP 429 code 1113 ("余额不足或无可用资源包,请充值") on every call
    from the screen onwards; it was retried as a rate limit and every attempt recorded as an infrastructure
    failure, until the comparison ended inconclusive."""
    from ouroloop.providers import ProviderAccountError, is_account_error
    body = '{"error":{"code":"1113","message":"余额不足或无可用资源包,请充值。"}}'
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429, text=body)))
    provider = OpenAICompatProvider("glm-5.3", retries=4, client=client)
    with pytest.raises(ProviderAccountError) as caught:
        provider.chat("s", [{"role": "user", "content": "x"}], ())
    assert not caught.value.retryable and caught.value.unbilled and is_account_error(caught.value)
    assert not is_account_error(ProviderError("HTTP 429: slow down", retryable=True))


def test_a_round_that_meets_an_account_error_pauses_and_the_experiment_resumes_after_the_repair(
        fake_upstream, tmp_path, monkeypatch):
    import rsi
    from test_harness_loop import NoteResearcher, v06_prepared
    from ouroloop import bench
    from ouroloop.evolve.execution import unfinished
    from ouroloop.providers import ProviderAccountError
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    state = {"broke": False}
    real_chat = rsi.BusinessWorker.chat

    def chat(self, system, messages, specs):
        if state["broke"]:
            raise ProviderAccountError('HTTP 429: {"error":{"code":"1113","message":"余额不足"}}')
        return real_chat(self, system, messages, specs)
    monkeypatch.setattr(rsi.BusinessWorker, "chat", chat)

    class BreaksTheAccount(NoteResearcher):
        def summarize(self, *a, **kw):
            out = super().summarize(*a, **kw)
            state["broke"] = True               # the gate's attempts are the next calls
            return out
    first = bench.improve(cfg, ".", out=lambda _line: None, researcher=BreaksTheAccount(broken_first_draft=False))
    assert first["rounds"][0]["outcome"] == "infrastructure_failed"
    assert first["rounds"][0]["next_action"] == "repair_runtime" and "refuses the account" in first["rounds"][0]["reason"]
    open_experiments = unfinished(activity.root)
    assert len(open_experiments) == 1                       # left open, not ended as inconclusive
    # the repair (a top-up); the next run finishes that experiment from its recorded state
    state["broke"] = False
    second = bench.improve(cfg, ".", out=lambda _line: None, researcher=NoteResearcher(broken_first_draft=False))
    assert second["rounds"][0]["experiment_id"] == open_experiments[0]["experiment_id"]
    assert second["rounds"][0]["outcome"] in ("installed", "rejected", "inconclusive")
    assert unfinished(activity.root) == []


# ---------------------------------------------------------------------------- every step pauses or ends (M5 §3)
from test_cycle import fixture_preset  # noqa: E402,F401 - the cycle tests' preset, as a fixture here too


def _broken_actor(monkeypatch, state):
    """The fixture's actor, refused by its provider while `state["broke"]` is set."""
    import rsi
    from ouroloop.providers import ProviderAccountError
    real = rsi.BusinessWorker.chat

    def chat(self, system, messages, specs):
        if state["broke"]:
            raise ProviderAccountError('HTTP 429: {"error":{"code":"1113","message":"余额不足"}}')
        return real(self, system, messages, specs)
    monkeypatch.setattr(rsi.BusinessWorker, "chat", chat)


def _breaks_on_entry(monkeypatch, module, name, state):
    real = getattr(module, name)

    def entered(*a, **kw):
        if state["broke"] is not None:             # None: repaired
            state["broke"] = True
        return real(*a, **kw)
    monkeypatch.setattr(module, name, entered)


def _attempt_ids(tmp_path):
    from test_cycle import rows_of
    return [r.attempt_id for r in rows_of(tmp_path)]


@pytest.mark.parametrize("where,step", [("baseline", "baseline"), ("resume", "segment-0"), ("slot", "slot-0"),
                                        ("common", "slot-0"), ("outer", "slot-0")])
def test_every_cycle_step_pauses_on_an_account_error(fake_upstream, tmp_path, fixture_preset, monkeypatch, where,
                                                     step):
    """docs/modules/05_EXECUTION_BUDGET.md §3: an account refusal outside a task attempt pauses the activity with
    its reason in cycle.json, never a traceback; after the repair the cycle resumes and pays for nothing twice."""
    import json as _json
    from test_cycle import RProposer, SkillAnyway, Stopper, arms, cycle_config, gap_w0, outer_on_the_gap
    from ouroloop import bench
    from ouroloop.providers import ProviderAccountError
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    outer_on_the_gap(monkeypatch)
    state = {"broke": False}
    _broken_actor(monkeypatch, state)
    slot = RProposer()
    if where == "baseline":
        _breaks_on_entry(monkeypatch, bench, "baseline", state)
    elif where == "common":
        _breaks_on_entry(monkeypatch, bench, "_campaign_common", state)
        # 2026-10-05 review: the common inputs reuse the mainline's readings of the start W, which under this
        # fixture's W0 cover every task the step would read; without them the step makes its paid calls again
        monkeypatch.setattr(bench, "_mainline_readings", lambda *a, **k: ([], []))
    elif where == "outer":
        _breaks_on_entry(monkeypatch, bench, "_evaluate_outer", state)
    elif where == "slot":
        class Refusing(RProposer):
            def propose_research(self, *a, **kw):
                if state["broke"] is not None:
                    raise ProviderAccountError("insufficient balance")
                return super().propose_research(*a, **kw)
        slot = Refusing()
    elif where == "resume":
        # a gate the refusal interrupted leaves its experiment open; the next run meets it again while resuming
        class BreaksTheAccount(SkillAnyway):
            def summarize(self, *a, **kw):
                out = super().summarize(*a, **kw)
                state["broke"] = state["broke"] is not None or None
                return out
        first = bench.cycle(cfg, ".", out=lambda _line: None, researcher=BreaksTheAccount())
        assert first["status"] == "paused"
    factory, _ = arms([Stopper, Stopper, SkillAnyway])
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=slot,
                         arm_researcher=factory)
    assert result["status"] == "paused" and result["ok"] is False
    recorded = _json.loads((tmp_path / "ws" / "rsi" / "cycle.json").read_text())
    assert recorded["status"] == "paused"
    assert recorded["steps"][step]["outcome"] == "paused"
    assert "refuses the account" in recorded["steps"][step]["reason"]
    later = {"baseline": "segment-0", "segment-0": "slot-0", "slot-0": "segment-1"}[step]
    assert later not in recorded["steps"]                        # nothing past the paused step ran
    # the repair: the account is topped up
    state["broke"] = None
    before = _attempt_ids(tmp_path)
    again = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper(), slot_researcher=RProposer(),
                        arm_researcher=arms([Stopper, Stopper, SkillAnyway])[0])
    assert again["status"] == "done", again["steps"]
    assert again["steps"][step].get("outcome") != "paused"
    ids = _attempt_ids(tmp_path)
    assert len(ids) == len(set(ids)) and set(before) <= set(ids)      # what was paid for is not paid again


class _Overloaded:
    """A research model that keeps answering HTTP 503, until `ok` is set (the repair). It retries once by itself,
    which the meter takes over: each try is its own reservation."""
    name, model, retries, ok = "overloaded", "r-1", 1, False

    def __init__(self, **_):
        pass

    def chat(self, system, messages, specs=()):
        if not _Overloaded.ok:
            raise ProviderError("HTTP 503: overloaded", retryable=True)
        return ChatTurn(json.dumps({"next_action": "stop", "reason": "nothing to change"}), [], "end",
                        usage={"input_tokens": 10, "output_tokens": 5})


class _SdkDown:
    """A research model whose SDK raises its own connection error — not a ProviderError, and without an HTTP
    status — until the repair."""
    name, model, retries, ok = "sdk", "r-2", 1, False

    def __init__(self, **_):
        pass

    def chat(self, system, messages, specs=()):
        if not _SdkDown.ok:
            raise type("APIConnectionError", (Exception,), {})("Connection error.")
        return ChatTurn(json.dumps({"next_action": "stop", "reason": "nothing to change"}), [], "end",
                        usage={"input_tokens": 10, "output_tokens": 5})


def _failing(base, method):
    class Failing(base):
        pass

    def fails(self, *a, **kw):
        raise ProviderError("HTTP 503: overloaded", retryable=True)     # what the meter re-raises after its retries
    setattr(Failing, method, fails)
    return Failing


@pytest.mark.parametrize("where,step", [("metered", "segment-0"), ("sdk", "segment-0"), ("note", "segment-0"),
                                        ("summary", "segment-0"),
                                        ("revise", "segment-0"), ("slot", "slot-0"), ("common", "slot-0"),
                                        ("arm", "slot-0")])
def test_a_research_call_that_keeps_failing_pauses_the_activity(fake_upstream, tmp_path, fixture_preset,
                                                                monkeypatch, where, step):
    """A research call the meter retried and that still failed is a provider failure, not R's decision and not a
    crash: the activity pauses, the round is not counted, and after the repair the same step is asked again."""
    import json as _json
    import time
    from test_cycle import RProposer, SkillAnyway, Stopper, arms, cycle_config, gap_w0, outer_on_the_gap
    from test_harness_loop import NoteResearcher
    from ouroloop import bench
    from ouroloop.evolve.execution import Budgets
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    if where != "common":
        outer_on_the_gap(monkeypatch)        # the default pair keeps a failing family in the arms' train pool
    worker, slot, factory = Stopper(), RProposer(), arms([Stopper, Stopper, SkillAnyway])[0]
    if where == "sdk":
        cfg["research"] = {"llm": {"type": "test_real_run_findings:_SdkDown"}}
        monkeypatch.setattr(_SdkDown, "ok", False)
        worker = None
    if where == "metered":
        cfg["research"] = {"llm": {"type": "test_real_run_findings:_Overloaded"}}
        monkeypatch.setattr(_Overloaded, "ok", False)
        worker = None                                        # the cycle's own researcher, on the activity's book
    elif where in ("note", "summary"):
        worker = _failing(Stopper, "note" if where == "note" else "summarize")()
    elif where == "revise":
        worker = _failing(NoteResearcher, "revise")()
    elif where == "slot":
        slot = _failing(RProposer, "propose_research")()
    elif where == "common":
        factory = arms([_failing(Stopper, "note"), Stopper, SkillAnyway])[0]
    elif where == "arm":
        factory = arms([Stopper, Stopper, _failing(SkillAnyway, "summarize")])[0]
    result = bench.cycle(cfg, ".", out=lambda _line: None, researcher=worker, slot_researcher=slot,
                         arm_researcher=factory)
    assert result["status"] == "paused" and result["ok"] is False
    recorded = _json.loads((tmp_path / "ws" / "rsi" / "cycle.json").read_text())
    assert recorded["status"] == "paused"
    record = recorded["steps"][step]
    assert record.get("outcome") == "paused" or record.get("stop_reason") == "repair_runtime"
    if where == "metered":
        book = Budgets(tmp_path / "ws")._read()
        tries = [r for r in book["reservations"].values() if ":research" in r["operation_id"]]
        assert len(tries) == 2 and {r["status"] for r in tries} == {"unknown"}     # the meter's retry was used
    if step == "segment-0":
        rounds = _json.loads((tmp_path / "ws" / "rsi" / "segments" / "segment-0.json").read_text())["rounds"]
        from ouroloop.research.loop import counters_of
        assert counters_of(rounds) == (0, 0)                    # the paused round is not counted
    # the repair: the same step is asked again, and the cycle goes on
    monkeypatch.setattr(_Overloaded, "ok", True)
    monkeypatch.setattr(_SdkDown, "ok", True)
    again = bench.cycle(cfg, ".", out=lambda _line: None, researcher=Stopper() if worker is not None else None,
                        slot_researcher=RProposer(), arm_researcher=arms([Stopper, Stopper, SkillAnyway])[0],
                        until="segment-0" if step == "segment-0" else None)
    assert again["status"] in ("stopped", "done") and again["steps"][step].get("outcome") != "paused"


def _spend_the_rest(root, scope="cyc"):
    """Book what is left of the activity's budget, as spending elsewhere in the activity would have."""
    from ouroloop.evolve.execution import Budgets
    budgets = Budgets(root)
    budgets.account(scope, {"model_calls": 4000, "tool_calls": 4000})      # the root cycle_config freezes
    left = budgets.headroom(scope, budgets._read())["model_calls"]
    budgets.settle(budgets.reserve(scope, "spent elsewhere", {"model_calls": left}).id, {"model_calls": left})


@pytest.mark.parametrize("where", ["baseline", "improve", "research_slot", "_campaign_common", "_run_campaign_arm",
                                   "_evaluate_outer"])
def test_a_spent_budget_ends_every_cycle_step_without_pausing(fake_upstream, tmp_path, fixture_preset, monkeypatch,
                                                              where):
    """docs/modules/05_EXECUTION_BUDGET.md §3: a spent budget is a bounded end, not a fault. Whichever step it runs
    out in, the activity ends as budget_exhausted (exit 0, a report), nothing pauses, and no exception escapes."""
    import json as _json
    from test_cycle import RProposer, SkillAnyway, Stopper, arms, cycle_config, gap_w0, outer_on_the_gap
    from ouroloop import bench
    cfg = cycle_config(fake_upstream, tmp_path)
    gap_w0(cfg, tmp_path)
    if where != "_campaign_common":
        outer_on_the_gap(monkeypatch)       # the default pair keeps a failing family in the arms' train pool
    # the slot's own research step is the cycle's metered one, so its request meets the spent book
    cfg["research"] = {"llm": {"type": "test_real_run_findings:_Overloaded"}}
    monkeypatch.setattr(_Overloaded, "ok", True)
    real = getattr(bench, where)

    def spent_on_entry(*a, **kw):
        _spend_the_rest(tmp_path / "ws")
        return real(*a, **kw)
    monkeypatch.setattr(bench, where, spent_on_entry)
    if where in ("_campaign_common", "_run_campaign_arm"):
        # The outer comparison's share was set aside before these steps ran, so spending the rest did not take
        # it. Spent at the common inputs, the arms' shares cannot be set aside and neither arm runs; spent at an
        # arm, both arms seal W0 and the outer does not run. Either way the share goes back to the activity, and
        # the next segment finds the book spent again.
        real_improve = bench.improve

        def improve(*a, **kw):
            if kw.get("segment") == "segment-1":
                _spend_the_rest(tmp_path / "ws")
            return real_improve(*a, **kw)
        monkeypatch.setattr(bench, "improve", improve)
    slot = None if where == "research_slot" else RProposer()
    lines = []
    # every research call but the slot's proposal is the cycle's own, metered on the activity's book; the outer
    # comparison runs only when the arms sealed different W, so for it the candidate arm installs a skill
    arm = arms([Stopper, Stopper, SkillAnyway])[0] if where == "_evaluate_outer" else None
    result = bench.cycle(cfg, ".", out=lines.append, researcher=None, slot_researcher=slot, arm_researcher=arm)
    assert result["status"] == "budget_exhausted" and result["ok"] is True, result["steps"]
    recorded = _json.loads((tmp_path / "ws" / "rsi" / "cycle.json").read_text())
    assert recorded["status"] == "budget_exhausted"
    from ouroloop.research.campaign import PAUSES
    assert not any(s.get("outcome") == "paused" or s.get("stop_reason") in PAUSES
                   for s in recorded["steps"].values() if isinstance(s, dict))
    assert any(line.startswith("wrote") for line in lines)               # the report of an ended activity
    if where == "_evaluate_outer":
        # the outer attempts met the spent book: an incomplete comparison, inconclusive, not a pause
        assert recorded["steps"]["slot-0"]["verdict"] == "inconclusive"
        assert "budget_exhausted" in json.dumps(
            bench.open_activity(cfg, ".", out=lambda _l: None).objects.get(recorded["steps"]["slot-0"]["receipt"]))


def test_resume_refuses_a_changed_frozen_plan(fake_upstream, tmp_path, monkeypatch):
    """An experiment is finished under the plan, the budget scope and the parent it was frozen with
    (docs/modules/05_EXECUTION_BUDGET.md §3). A plan or scope changed while it was open pauses the activity and leaves
    the experiment open; a moved pointer does not: the comparison still runs on the frozen parent, and the install
    finds the pointer moved and records stale_parent."""
    import rsi
    from test_harness_loop import NoteResearcher, v06_prepared
    from ouroloop import bench
    from ouroloop.benchmark import Results
    from ouroloop.evolve.execution import unfinished
    from ouroloop.evolve.task_gate import GatePlan
    from ouroloop.providers import ProviderAccountError
    from ouroloop.research.campaign import PAUSES
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    state = {"broke": False}
    real_chat = rsi.BusinessWorker.chat

    def chat(self, system, messages, specs):
        if state["broke"]:
            raise ProviderAccountError('HTTP 429: {"error":{"code":"1113","message":"余额不足"}}')
        return real_chat(self, system, messages, specs)
    monkeypatch.setattr(rsi.BusinessWorker, "chat", chat)

    class BreaksTheAccount(NoteResearcher):
        def summarize(self, *a, **kw):
            out = super().summarize(*a, **kw)
            state["broke"] = True               # the gate's attempts are the next calls
            return out
    bench.improve(cfg, ".", out=lambda _line: None, researcher=BreaksTheAccount(broken_first_draft=False))
    [open_experiment] = unfinished(activity.root)
    experiment = activity.objects.load(open_experiment["experiment_ref"], ct.Experiment)
    state["broke"] = False                      # the account is topped up; something else changed meanwhile
    frozen = dict(cfg["rsi"])
    attempts = len(list(Results(activity.root, rsi.DOMAIN).all()))

    def refused(rsi_table: dict, field: str) -> None:
        cfg["rsi"] = {**frozen, **rsi_table}
        researcher = NoteResearcher(broken_first_draft=False)
        result = bench.improve(cfg, ".", out=lambda _line: None, researcher=researcher, segment=field)
        first = result["rounds"][0]
        segment = json.loads((activity.root / "rsi" / "segments" / f"{field}.json").read_text())
        assert segment["stop_reason"] == "repair_runtime" and not segment["finished"]   # a cycle's paused segment
        assert first["experiment_id"] == experiment.id and first["outcome"] == "infrastructure_failed"
        assert first["next_action"] == "repair_runtime" and field in first["reason"]
        assert result["stop_reason"] in PAUSES                      # a pause, which a cycle treats as one
        assert researcher.requests == []                            # no new round ran behind it
        assert len(list(Results(activity.root, rsi.DOMAIN).all())) == attempts   # nothing paid under the new plan
        assert unfinished(activity.root) == [open_experiment]       # the experiment stays open, as it was
        refusals = [e for e in ct.Events(activity.root).of("resume_refused") if e["experiment"] == experiment.id]
        assert refusals and field in refusals[-1]["reason"]          # and why it was not resumed is on record

    other = bench.freeze_task_plan(activity.objects, core_ref=experiment.core_ref, split_ref=activity.splits.hash(),
                                   task_ids=tuple(rsi.DEV), gate=GatePlan(min_gain=0.5, min_groups=1,
                                                                          bootstrap_replicates=200))
    refused({"task_plan_ref": other}, "eval_template_ref")
    refused({"scope_id": "another"}, "budget_scope")
    # The plan as it was, and the account refused again while the experiment resumes: a standalone improve pauses
    # with the reason and leaves the experiment open, rather than leaving with the provider's exception.
    cfg["rsi"] = frozen
    state["broke"] = True
    again = bench.improve(cfg, ".", out=lambda _line: None, researcher=NoteResearcher(broken_first_draft=False))
    assert again["stop_reason"] == "repair_runtime" and again["rounds"][0]["experiment_id"] == experiment.id
    assert "refuses the account" in again["rounds"][0]["reason"]
    assert unfinished(activity.root) == [open_experiment]
    state["broke"] = False
    # The plan restored, and the pointer moved on meanwhile: the experiment resumes on the parent it was frozen on.
    moved = activity.releases.bootstrap(ct.SystemRelease.from_json(activity.objects.get(w0)).worker_ref,
                                        ct.SystemRelease.from_json(activity.objects.get(w0)).research_ref,
                                        experiment.core_ref, reason="another install meanwhile", activate=False)
    from ouroloop.ledger import write_json
    write_json(activity.releases.active_path, {"release_ref": moved, "revision": 9, "last_operation_id": None})
    cfg["rsi"] = frozen
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=NoteResearcher(broken_first_draft=False))
    first = result["rounds"][0]
    assert first["experiment_id"] == experiment.id and first["outcome"] == "stale_parent"
    receipt = activity.objects.load(first["receipt_ref"], ct.EvaluationReceipt)
    plan = activity.objects.get(receipt.plan_ref)
    assert plan["parent_release"] == experiment.parent_release != moved
    assert receipt.control_ref == ct.SystemRelease.from_json(activity.objects.get(experiment.parent_release)).worker_ref
    permits = [activity.objects.load(e["permit"], ct.EvaluationPermit)
               for e in ct.Events(activity.root).of("permit_issued") if e.get("plan") == receipt.plan_ref]
    assert permits and all(p.parent_release == experiment.parent_release for p in permits)
    assert result["stop_reason"] not in PAUSES and unfinished(activity.root) == []


def test_only_a_model_providers_failure_is_read_as_a_refused_account():
    """An internal error whose text happens to say "billing" or "quota" is not the provider refusing the account:
    it is not paused on as one, and it is not classified at all — it re-raises."""
    from ouroloop.evolve.execution import BudgetError, BudgetMismatch
    from ouroloop.providers import is_account_error
    from ouroloop.research.loop import halt_of
    assert not is_account_error(KeyError("billing_address")) and halt_of(KeyError("billing_address")) is None
    assert not is_account_error(ValueError("quota exceeded for the export"))
    assert is_account_error(ProviderError("HTTP 429: quota exceeded"))
    sdk = type("APIStatusError", (Exception,), {"status_code": 402})("payment required")
    assert is_account_error(sdk)                                   # an SDK error with an HTTP status still counts
    assert halt_of(BudgetMismatch("scope 'x' already exists with other limits"))[0] == "repair_runtime"
    assert halt_of(BudgetError("no reservation res_1")) is None    # a misuse of the ledger is not a pause


def test_a_gateway_that_answers_200_without_choices_is_a_provider_failure():
    from ouroloop.providers import ProviderAccountError

    def gateway(body: str):
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)))
    with pytest.raises(ProviderError, match="not JSON") as failed:
        OpenAICompatProvider("m", client=gateway("<html>busy</html>"), retries=0).chat("s", [], [])
    assert failed.value.retryable
    with pytest.raises(ProviderAccountError):
        OpenAICompatProvider("m", client=gateway(json.dumps({"error": {"code": "1113", "message": "余额不足"}})),
                             retries=0).chat("s", [], [])
    with pytest.raises(ProviderError, match="without choices"):
        OpenAICompatProvider("m", client=gateway(json.dumps({"error": {"message": "upstream reset"}})),
                             retries=0).chat("s", [], [])
