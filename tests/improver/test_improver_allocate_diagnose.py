"""Diagnosis and the focus list (docs/design/IMPROVER.md §5 diagnose.py, §6 allocate.py v1.2, §13; DESIGN §6.3,
§6.4).

Diagnosis, on the real failed train attempts of the rsi domain's baseline (the forgetful actor misses the #ops message
on the two notify tasks) with a scripted research model: the cases it gives (cause, note, evidence refs of the attempt's
own session, assertion kinds, termination); a cause outside the vocabulary is `other` and its name becomes the
proposal; a vocabulary extension needs two cases (distinct attempts) and is not mechanizable; the note cache
(rounds/notes.jsonl) answers a repeated request without a model call; the memory's entries are classified on the same
vocabulary; the clusters are by cause, largest first, with the mechanizable flag from the settings. One test runs the
round's diagnosis step to see the extension recorded and read by the next round.

Layering, as pure functions over Cause records and an Estimates in a tmp dir: the upper bound (mean + unc) orders the
kinds, ties to the earlier kind (code first); clusters by size, at most k; the exploration slot takes a kind with
n == 0 on its cause, code kinds first; the code guarantee; two code slots after a round that recorded
`no_code_candidate`; L5 only for the loop causes and at most l5_slots; the `l6_unavailable` note only when stalled, and
`stalled` read off the ledger; Estimates get/update/tried with the prior as one observation. One test runs the round's
allocation step to see the previous round's `no_code_candidate` and the `l6_unavailable` row.
"""
from __future__ import annotations


def test_every_cause_of_the_vocabulary_has_a_definition():
    """v1.5 (DESIGN §6.15): the three new causes, like the old ones, enter the diagnosis prompt with a definition."""
    from reharness.improver.diagnose import DEFINITIONS
    from reharness.improver.settings import CAUSES, MECHANIZABLE
    assert set(CAUSES) <= set(DEFINITIONS) and set(MECHANIZABLE) <= set(CAUSES)
    assert {"rule_not_applied", "vocabulary_mismatch", "omitted_report"} <= set(CAUSES)
    assert "vocabulary_mismatch" in MECHANIZABLE and "omitted_report" not in MECHANIZABLE

import json
import math
from dataclasses import replace
from types import SimpleNamespace

import pytest
import rsi
from rounds import quiet, round_activity
from scripted_r import ScriptedR

from reharness.contracts import Objects, read_jsonl, write_json
from reharness.improver import diagnose as dg
from reharness.improver.allocate import Estimates, Focus, focus, kinds_for, stalled
from reharness.improver.diagnose import Case, Cause, NoteCache
from reharness.improver.evidence import Evidence
from reharness.improver.ledger import Ledger
from reharness.improver.round import Round
from reharness.improver.settings import CAUSES, LOOP_CAUSES, MECHANIZABLE, Settings
from reharness.runtime.chat import ChatTurn

NOTIFY = ("rsi.notify_after_update", "rsi.notify_after_update_variant")
FAILING = NOTIFY + ("rsi.notify_ticket",)         # what the forgetful actor fails on train (the pair is held out)
MECH = MECHANIZABLE + ("missed_action",)          # what round_activity's K holds
LINE = "rsi"
ROUND = "rsi-test/r1"
ALL = ("tool", "hook", "skill", "prompt", "agent", "config", "vendor", "memory", "model", "test")
L5 = ("agent", "config", "vendor")


class Aux:
    """A research model for one test: request i gets answer i (the last one repeats); a dict goes out as JSON, a
    callable is called with the request's user object. It keeps every request."""

    def __init__(self, *answers, model: str = "aux-1"):
        self.answers, self.model, self.asked = list(answers), model, []

    def chat(self, system, messages, specs, **_kw) -> ChatTurn:
        user = json.loads(messages[0]["content"])
        self.asked.append({"system": system, "user": user, "specs": list(specs or ())})
        answer = self.answers[min(len(self.asked), len(self.answers)) - 1]
        answer = answer(user) if callable(answer) else answer
        return ChatTurn(answer if isinstance(answer, str) else json.dumps(answer), [], "end",
                        usage={"input_tokens": 10, "output_tokens": 5})


def tail_refs(user: dict) -> list[str]:
    return [m["ref"] for m in user["trajectory_tail"] if m.get("ref")]


def cache_in(path) -> NoteCache:
    return NoteCache(path, Objects(path))


def case_of(evidence, ev, researcher, cache, *, vocabulary=CAUSES, mechanizable=MECH):
    return dg.diagnose_case(evidence, ev, researcher=researcher, vocabulary=vocabulary, mechanizable=mechanizable,
                            cache=cache)


@pytest.fixture(scope="module")
def base(tmp_path_factory, fake_upstream):
    """The rsi activity after its baseline: the three notify tasks failed (missed #ops message), the rest passed."""
    s = round_activity(tmp_path_factory.mktemp("diagnose"), fake_upstream, "tool")
    evidence = Evidence(s.a)
    failed = evidence.failed(s.a.active_release().package_ref)
    assert [e.task_id for e in failed] == list(FAILING)
    return SimpleNamespace(s=s, a=s.a, evidence=evidence, failed=failed)


# ---------------------------------------------------------------------------------------------- diagnosis
def test_a_scripted_research_model_gives_the_cases(base, tmp_path):
    r = ScriptedR("tool")
    cache = cache_in(tmp_path)
    for ev in base.failed:
        ref, case, hit = case_of(base.evidence, ev, r, cache)
        assert not hit
        assert (case.attempt_id, case.task_id) == (ev.attempt_id, ev.task_id)
        assert case.cause == "missed_action" and case.mechanizable
        assert "#ops" in case.explanation and case.proposed == ""
        assert case.evidence_refs and all(x.startswith(ev.session + ":L") for x in case.evidence_refs)
        assert case.assertion_kinds == ("slack_message_contains",)
        assert case.termination == ev.result.termination
        assert case.tool_errors == tuple(sorted(set(ev.tool_errors)))
        assert ref == case.ref() and Case.from_json(cache.objects.get(ref)).ref() == ref
    assert r.calls == ["diagnose"] * len(FAILING)
    # mechanizable is the settings' word on the cause, not the model's
    _, case, _ = case_of(base.evidence, base.failed[0], ScriptedR("tool"), cache_in(tmp_path / "plain"),
                         mechanizable=MECHANIZABLE)
    assert case.cause == "missed_action" and not case.mechanizable


def test_the_request_carries_features_failed_checks_the_tail_and_the_vocabulary(base, tmp_path):
    ev = base.failed[0]
    aux = Aux({"cause": "missed_action"})
    case_of(base.evidence, ev, aux, cache_in(tmp_path))
    (asked,) = aux.asked
    assert asked["system"] == dg.prompt("diagnose") and asked["system"].startswith("You diagnose")
    assert asked["specs"] == []
    user = asked["user"]
    assert user["task_id"] == ev.task_id
    assert user["features"] == json.loads(json.dumps(Evidence.features(ev), default=str))
    assert user["failed_checks"] == [{"type": c["type"], "params": c["params"]} for c in ev.failed_checks]
    assert [c["type"] for c in user["failed_checks"]] == ["slack_message_contains"]
    assert user["trajectory_tail"] and all(m["ref"].startswith(ev.session + ":L") for m in user["trajectory_tail"])
    assert user["vocabulary"] == {name: dg.DEFINITIONS[name] for name in CAUSES}


def test_evidence_refs_outside_the_attempt_are_dropped(base, tmp_path):
    ev = base.failed[0]
    other = base.failed[1]

    def answer(user):
        own = tail_refs(user)[-1]
        return {"cause": "missed_action", "note": "no post",
                "evidence_refs": [own, f"{other.session}:L3", "L3", f"{ev.session}:X1", 7, None]}
    _, case, _ = case_of(base.evidence, ev, Aux(answer), cache_in(tmp_path))
    assert len(case.evidence_refs) == 1 and case.evidence_refs[0].startswith(ev.session + ":L")


@pytest.mark.parametrize("answer, cause, proposed", [
    ({"cause": "Hallucinated Tool", "note": "n"}, "other", "hallucinated_tool"),
    ({"cause": "made-up", "proposed": "Ignored Policy"}, "other", "ignored_policy"),
    ({"cause": "other", "proposed": "unclear_request"}, "other", "unclear_request"),
    ({"cause": "missed_action", "proposed": "something_else"}, "missed_action", ""),
    ({"cause": "other", "proposed": "x"}, "other", ""),
    ({"cause": "other", "proposed": "9lives"}, "other", ""),
    ({"note": "no cause given"}, "other", ""),
    ("I cannot tell.", "other", ""),
    ('```json\n{"cause": "real_date", "note": "used today"}\n```', "real_date", ""),
])
def test_a_cause_outside_the_vocabulary_is_other(base, tmp_path, answer, cause, proposed):
    _, case, _ = case_of(base.evidence, base.failed[0], Aux(answer), cache_in(tmp_path))
    assert (case.cause, case.proposed) == (cause, proposed)
    assert case.mechanizable == (cause in MECH)


def test_an_added_cause_is_in_the_vocabulary_and_not_mechanizable(base, tmp_path):
    vocabulary = CAUSES + ("unclear_request",)
    aux = Aux({"cause": "unclear_request", "note": "n"})
    _, case, _ = case_of(base.evidence, base.failed[0], aux, cache_in(tmp_path), vocabulary=vocabulary)
    assert (case.cause, case.proposed, case.mechanizable) == ("unclear_request", "", False)
    assert aux.asked[0]["user"]["vocabulary"]["unclear_request"] == \
        "a cause a diagnosis proposed and two cases confirmed"


def test_a_vocabulary_extension_needs_two_cases():
    def c(attempt, proposed, cause="other"):
        return Case(attempt_id=attempt, task_id=attempt.split("#")[0], cause=cause, proposed=proposed)
    assert dg.vocabulary_extension([c("t1#0", "stale_record")], CAUSES) == []
    assert dg.vocabulary_extension([c("t1#0", "stale_record"), c("t2#0", "stale_record")], CAUSES) == ["stale_record"]
    # two cases of one attempt are one
    assert dg.vocabulary_extension([c("t1#0", "stale_record"), c("t1#0", "stale_record")], CAUSES) == []
    # a name already in the vocabulary is not added again
    assert dg.vocabulary_extension([c("t1#0", "real_date"), c("t2#0", "real_date")], CAUSES) == []
    assert dg.vocabulary_extension([c("t1#0", "stale_record"), c("t2#0", "stale_record")],
                                   CAUSES + ("stale_record",)) == []
    got = dg.vocabulary_extension([c(f"t{i}#0", name) for i, name in
                                   enumerate(["zz_name", "aa_name", "zz_name", "aa_name", "mm_name", ""])], CAUSES)
    assert got == ["aa_name", "zz_name"]


def test_the_note_cache_answers_a_repeated_request_without_a_model_call(base, tmp_path):
    ev = base.failed[0]
    aux = Aux({"cause": "missed_action", "note": "first"}, {"cause": "wrong_target", "note": "second"})
    cache = cache_in(tmp_path)
    ref, case, hit = case_of(base.evidence, ev, aux, cache)
    assert not hit and len(aux.asked) == 1
    ref2, case2, hit2 = case_of(base.evidence, ev, aux, cache)
    assert (ref2, case2.ref(), case2.cause, hit2) == (ref, ref, "missed_action", True) and len(aux.asked) == 1
    # the cache is the file, not the instance
    ref2, case2, hit2 = case_of(base.evidence, ev, aux, cache_in(tmp_path))
    assert (ref2, case2.cause, hit2) == (ref, "missed_action", True) and len(aux.asked) == 1
    rows = list(read_jsonl(tmp_path / "rounds" / "notes.jsonl"))
    assert [(r["case"], r["attempt"]) for r in rows] == [(ref, ev.attempt_id)]
    # another model, or another vocabulary, is another request
    other_model = Aux({"cause": "wrong_target"}, model="aux-2")
    _, case2, hit2 = case_of(base.evidence, ev, other_model, cache)
    assert not hit2 and len(other_model.asked) == 1 and case2.cause == "wrong_target"
    assert case_of(base.evidence, ev, other_model, cache)[2] and len(other_model.asked) == 1
    _, case3, hit3 = case_of(base.evidence, ev, aux, cache, vocabulary=CAUSES + ("stale_record",))
    assert not hit3 and len(aux.asked) == 2 and case3.cause == "wrong_target"


def test_a_note_whose_case_is_not_in_the_store_is_a_miss(base, tmp_path):
    ev = base.failed[0]
    case_of(base.evidence, ev, Aux({"cause": "missed_action"}), cache_in(tmp_path))
    aux = Aux({"cause": "missed_action"})
    _, _, hit = case_of(base.evidence, ev, aux, NoteCache(tmp_path, Objects(tmp_path / "elsewhere")))
    assert not hit and len(aux.asked) == 1


def test_a_case_read_back_keeps_its_note(base, tmp_path):
    ev = base.failed[0]
    cache = cache_in(tmp_path)
    ref, case, _ = case_of(base.evidence, ev, Aux({"cause": "missed_action", "note": "it never posted in #ops"}), cache)
    assert case.explanation == "it never posted in #ops"
    _, cached, hit = case_of(base.evidence, ev, Aux({"cause": "other"}), cache)
    assert hit
    assert cached.explanation == case.explanation
    assert Case.from_json(cache.objects.get(ref)).explanation == case.explanation


def test_a_cached_case_belongs_to_the_attempt_it_was_asked_for(base, tmp_path):
    bare = SimpleNamespace(features=Evidence.features, trace=SimpleNamespace(trajectory=lambda _session: []))
    one = replace(base.failed[0], attempt_id="run-a/t#0", session="run-a/t#0")
    two = replace(base.failed[0], attempt_id="run-b/t#0", session="run-b/t#0")
    aux = Aux({"cause": "missed_action"})
    cache = cache_in(tmp_path)
    _, first, _ = case_of(bare, one, aux, cache)
    _, second, _ = case_of(bare, two, aux, cache)
    assert first.attempt_id == one.attempt_id
    assert second.attempt_id == two.attempt_id


def test_the_memory_entries_are_classified_on_the_vocabulary():
    entries = [{"memory_id": "m1", "row": {"when_to_use": "after updating a record",
                                           "content": "Post the requested message in #ops before finishing"}},
               {"memory_id": "m2", "row": {"content": "Read the data policy first"}},
               {"id": "m3", "content": "a post in #ops was asked for"}]
    r = ScriptedR("tool")
    assert dg.classify_entries(entries, researcher=r, vocabulary=CAUSES) == \
        {"m1": "missed_action", "m2": "other", "m3": "missed_action"}
    assert r.calls == ["diagnose"] * 3
    aux = Aux({"cause": "real_date"}, {"cause": "Made Up"}, "no json")
    assert dg.classify_entries(entries, researcher=aux, vocabulary=CAUSES) == \
        {"m1": "real_date", "m2": "other", "m3": "other"}
    first = aux.asked[0]
    assert first["system"] == dg.prompt("diagnose")
    assert first["user"]["memory_entry"] == {"when": "after updating a record",
                                             "content": "Post the requested message in #ops before finishing"}
    assert set(first["user"]["vocabulary"]) == set(CAUSES)
    # a cause the vocabulary gained is a cause
    assert dg.classify_entries(entries[:1], researcher=Aux({"cause": "stale_record"}),
                               vocabulary=CAUSES + ("stale_record",)) == {"m1": "stale_record"}


def test_clusters_are_by_cause_largest_first_mechanizable_by_the_settings():
    def c(attempt, task, cause, mech=False):
        return Case(attempt_id=attempt, task_id=task, cause=cause, mechanizable=mech)
    cases = [(f"case:{i}", x) for i, x in enumerate([
        c("a1", "t1", "missed_action", True), c("a2", "t2", "missed_action", True), c("a3", "t1", "missed_action"),
        c("a4", "t3", "real_date"), c("a5", "t4", "other"), c("a6", "t5", "wrong_target"),
        c("a7", "t6", "wrong_target")])]
    got = dg.cluster(cases, round_id=ROUND, mechanizable=("real_date",))
    assert [(x.name, x.tasks) for x in got] == [("missed_action", ("t1", "t2")), ("wrong_target", ("t5", "t6")),
                                                ("other", ("t4",)), ("real_date", ("t3",))]
    by = {x.name: x for x in got}
    assert by["missed_action"].cases == ("case:0", "case:1", "case:2")
    assert by["missed_action"].cause_id == f"{ROUND}:missed_action"
    # the settings decide, whatever the cases said
    assert [x.name for x in got if x.mechanizable] == ["real_date"]
    # the memory's mechanizable entries join their cause's cluster, or make one
    joined = dg.cluster(cases, round_id=ROUND, mechanizable=MECHANIZABLE,
                        memory_inputs={"real_date": [{"memory_id": "m1"}], "wrong_location": [{"memory_id": "m2"}]})
    by = {x.name: x for x in joined}
    assert by["real_date"].memory_entries == ({"memory_id": "m1"},) and by["real_date"].tasks == ("t3",)
    assert (by["wrong_location"].cases, by["wrong_location"].tasks, by["wrong_location"].memory_entries) == \
        ((), (), ({"memory_id": "m2"},))
    assert by["wrong_location"].mechanizable and joined[-1].name == "wrong_location"
    assert by["missed_action"].memory_entries == ()


def test_the_baseline_failures_make_one_mechanizable_cause(base, tmp_path):
    cache = cache_in(tmp_path)
    cases = [case_of(base.evidence, ev, ScriptedR("tool"), cache)[:2] for ev in base.failed]
    settings = Settings.from_core(base.a.k)
    assert "missed_action" in settings.mechanizable
    (only,) = dg.cluster(cases, round_id=ROUND, mechanizable=settings.mechanizable)
    assert (only.name, only.tasks, only.mechanizable) == ("missed_action", FAILING, True)
    assert only.cases == tuple(ref for ref, _ in cases)


def test_two_cases_proposing_a_name_add_it_for_the_next_round(base):
    """Round.diagnosed: the extension goes to the ledger (vocabulary_added) and the next round's vocabulary has it."""
    proposer = Aux(lambda user: {"cause": "other", "proposed": "stale_record", "note": "n",
                                 "evidence_refs": tail_refs(user)[-1:]})
    research = base.s.research
    kept, research.r = research.r, proposer
    try:
        with pytest.MonkeyPatch.context() as mp:        # every failing task studied: two cases propose the name
            mp.setattr(Round, "split", lambda self: (tuple(rsi.TRAIN), ()))
            r = Round(base.a, 1, run_arm=None, out=quiet)
            assert "stale_record" not in r.vocabulary()
            r.state["steps"]["evidence"] = r.gather()
            got = r.diagnosed()
    finally:
        research.r = kept
    assert len(proposer.asked) == len(FAILING)
    rows = [x for x in read_jsonl(base.a.root / "ledger" / "experience.jsonl") if x["stage"] == "vocabulary_added"]
    assert [(x["round_id"], x["names"]) for x in rows] == [(f"{base.a.scope_id}/r1", ["stale_record"])]
    (other,) = [Cause.from_json(base.a.objects.get(ref)) for ref in got["causes"]]
    assert (other.name, tuple(other.tasks), other.mechanizable) == ("other", FAILING, False)
    assert Round(base.a, 2, run_arm=None, out=quiet).vocabulary()[-1] == "stale_record"


# ---------------------------------------------------------------------------------------------- estimates
def test_estimates_start_at_the_prior_which_counts_as_one_observation(tmp_path):
    s = Settings()
    est = Estimates(tmp_path, s)
    e = est.get(LINE, "real_date", "tool")
    assert (e.n, e.fixes, e.cost, e.mean, e.unc, e.upper) == (0, 0.0, 0.0, 0.3, 0.3, 0.6)
    assert est.get(LINE, "real_date", "skill").upper == 0.18
    assert (est.get(LINE, "real_date", "unknown_kind").mean, est.get(LINE, "real_date", "unknown_kind").unc) == \
        (0.0, 0.5)
    assert est.tried(LINE, "real_date") == set()
    est.update(LINE, "real_date", "tool", fixes=0.6, cost=0.25)
    e = est.get(LINE, "real_date", "tool")
    assert (e.n, e.fixes, e.cost, e.mean, e.unc) == (1, 0.6, 0.25, 0.45, round(0.3 / math.sqrt(2), 6))
    est.update(LINE, "real_date", "tool", fixes=0.0, cost=0.1)
    e = est.get(LINE, "real_date", "tool")
    assert (e.n, e.fixes, e.cost, e.mean, e.unc) == (2, 0.6, 0.35, 0.3, round(0.3 / math.sqrt(3), 6))
    # kept on disk, per (line, cause, kind)
    again = Estimates(tmp_path, s)
    assert again.get(LINE, "real_date", "tool") == e
    assert json.loads((tmp_path / "ledger" / "estimates.json").read_text())[LINE]["real_date"]["tool"] == \
        {"fixes": 0.6, "cost": 0.35, "n": 2}
    assert again.get(LINE, "arith_mismatch", "tool").n == 0 and again.get("other_line", "real_date", "tool").n == 0
    assert again.tried(LINE, "real_date") == {"tool"} and again.tried(LINE, "arith_mismatch") == set()
    assert again.tried("other_line", "real_date") == set()


def test_a_keyed_observation_is_one_candidates_and_writing_it_again_replaces_it(tmp_path):
    """The screen's observation of a candidate, then the install's reading in its place; a resumed step writes the
    same key again and counts once."""
    est = Estimates(tmp_path, Settings())
    est.update(LINE, "missed_action", "tool", fixes=1.0, cost=0.2, key="r0:notify")
    est.update(LINE, "missed_action", "tool", fixes=1.0, cost=0.2, key="r0:notify")
    e = est.get(LINE, "missed_action", "tool")
    assert (e.n, e.fixes, e.cost) == (1, 1.0, 0.2)
    est.update(LINE, "missed_action", "tool", fixes=0.5, cost=0.7, key="r0:notify")
    est.update(LINE, "missed_action", "tool", fixes=0.0, cost=0.1, key="r1:other")
    e = Estimates(tmp_path, Settings()).get(LINE, "missed_action", "tool")
    assert (e.n, e.fixes, e.cost) == (2, 0.5, 0.8)


def test_a_kind_on_record_with_no_reading_is_not_tried(tmp_path):
    write_json(tmp_path / "ledger" / "estimates.json",
               {LINE: {"real_date": {"hook": {"fixes": 0.0, "cost": 0.0, "n": 0},
                                     "tool": {"fixes": 0.0, "cost": 0.0, "n": 1}}}})
    assert Estimates(tmp_path, Settings()).tried(LINE, "real_date") == {"tool"}


# ---------------------------------------------------------------------------------------------- the focus list
def cause(name: str, n: int, *, mech: bool | None = None) -> Cause:
    return Cause(cause_id=f"{ROUND}:{name}", name=name, tasks=tuple(f"{name}.{i}" for i in range(n)),
                 mechanizable=(name in MECHANIZABLE) if mech is None else mech)


def tried(n: int = 1, fixes: float = 0.0) -> dict:
    return {"fixes": fixes, "cost": 0.0, "n": n}


class NoArchive:
    def for_prompt(self, line, cause, n=10, hide=frozenset()):
        return []


def focused(tmp_path, causes, table=None, *, allowed=ALL, archive=None, is_stalled=False, train_available=False,
            **settings) -> Focus:
    """focus() over an estimates table {cause: {kind: row}} for LINE."""
    s = Settings(**settings)
    write_json(tmp_path / "ledger" / "estimates.json", {LINE: table or {}})
    return focus(causes, Estimates(tmp_path, s), archive or NoArchive(), settings=s, line=LINE, allowed=allowed,
                 stalled=is_stalled, train_available=train_available)


def kinds_of(f: Focus) -> dict:
    return {r["name"]: [k["kind"] for k in r["kinds"]] for r in f.ranked}


def test_the_kinds_a_cause_may_get():
    assert kinds_for("real_date", mechanizable=True, allowed=ALL) == ["tool", "hook", "skill", "prompt"]
    # v1.11 (DESIGN §6.21): a hold is a carrier for every cause, before the text kinds
    assert kinds_for("missed_action", mechanizable=False, allowed=ALL) == ["hook", "skill", "prompt"]
    assert kinds_for("context_overflow", mechanizable=False, allowed=ALL) == ["hook", "skill", "prompt", *L5]
    assert kinds_for("tool_error_loop", mechanizable=True, allowed=ALL) == ["tool", "hook", "skill", "prompt", *L5]
    # never memory or model; only what K allows, in the fixed order whatever K's order
    assert kinds_for("tool_error_loop", mechanizable=True, allowed=("vendor", "prompt", "memory", "model")) == \
        ["prompt", "vendor"]
    assert kinds_for("real_date", mechanizable=True, allowed=tuple(reversed(ALL))) == ["tool", "hook", "skill",
                                                                                       "prompt"]


def test_the_list_ranks_causes_by_size_and_kinds_by_score(tmp_path):
    causes = [cause("missed_action", 2), cause("wrong_target", 4), cause("real_date", 1), cause("premature_stop", 4),
              cause("unread_value", 3)]
    f = focused(tmp_path, causes)
    assert [r["name"] for r in f.ranked] == ["premature_stop", "wrong_target", "unread_value", "missed_action",
                                             "real_date"]
    assert [r["tasks"] for r in f.ranked][:2] == [[f"premature_stop.{i}" for i in range(4)],
                                                  [f"wrong_target.{i}" for i in range(4)]]
    # untried: the priors' upper bounds order the kinds, code first; the real_date row is mechanizable
    assert kinds_of(f)["real_date"] == ["tool", "hook", "skill", "prompt"]
    assert kinds_of(f)["wrong_target"] == ["hook", "skill", "prompt"]          # v1.11: a hold for every cause
    row = next(r for r in f.ranked if r["name"] == "real_date")
    assert row["mechanizable"] is True and row["kinds"][0] == {"kind": "tool", "score": 0.6, "mean": 0.3, "n": 0,
                                                                "tried": []}
    assert Focus.from_json(json.loads(json.dumps(f.to_json()))) == f


def test_a_reading_moves_a_kind_in_the_list(tmp_path):
    # tool tried three times without a fix (0.225), hook untried (0.6), skill with the best mean (0.39)
    table = {"real_date": {"tool": tried(3), "skill": tried(1, 0.7)}}
    f = focused(tmp_path, [cause("real_date", 3)], table)
    assert kinds_of(f)["real_date"] == ["hook", "skill", "tool", "prompt"]
    scores = {k["kind"]: k["score"] for k in f.ranked[0]["kinds"]}
    assert scores["hook"] == 0.6 and scores["tool"] == 0.225 and scores["skill"] > scores["tool"]
    # a reading that beats every bound puts a text kind first: the list says so, and the code rule is V0's
    table = {"real_date": {"tool": tried(3), "hook": tried(3), "skill": tried(1, 1.0)}}
    f = focused(tmp_path, [cause("real_date", 3)], table)
    assert kinds_of(f)["real_date"][0] == "skill" and f.require_code


def test_ties_go_to_the_earlier_kind_code_first(tmp_path):
    for allowed in (ALL, tuple(reversed(ALL))):
        assert kinds_of(focused(tmp_path, [cause("real_date", 2)], allowed=allowed))["real_date"][0] == "tool"
        assert kinds_of(focused(tmp_path, [cause("missed_action", 2)], allowed=allowed))["missed_action"][0] == "hook"


def test_the_list_carries_what_the_archive_tried_on_a_cause_and_kind(tmp_path):
    class Shelf:
        def for_prompt(self, line, cause, n=10, hide=frozenset()):
            assert hide == frozenset() and line == LINE
            return [{"round": 0, "dir": "package", "kind": "tool+hook", "screen": {"net_flips": 1}}] \
                if cause == "real_date" else []
    f = focused(tmp_path, [cause("real_date", 2), cause("wrong_target", 1)], archive=Shelf())
    rows = {r["name"]: {k["kind"]: k["tried"] for k in r["kinds"]} for r in f.ranked}
    assert rows["real_date"]["tool"] == rows["real_date"]["hook"] == [{"round": 0, "dir": "package",
                                                                       "kind": "tool+hook", "screen": {"net_flips": 1}}]
    assert rows["real_date"]["skill"] == [] and rows["wrong_target"]["skill"] == []


def test_a_cause_no_allowed_kind_fits_is_left_out(tmp_path):
    f = focused(tmp_path, [cause("wrong_target", 5), cause("real_date", 1)], allowed=("tool",))
    assert [r["name"] for r in f.ranked] == ["real_date"]


def test_require_code_when_a_mechanizable_cause_is_diagnosed_and_k_allows_code(tmp_path):
    assert focused(tmp_path, [cause("real_date", 3)]).require_code
    assert not focused(tmp_path, [cause("wrong_target", 3)]).require_code
    assert not focused(tmp_path, [cause("real_date", 3)], allowed=("skill", "prompt")).require_code
    assert not focused(tmp_path, [cause("real_date", 3)], require_code_component=False).require_code
    f = focused(tmp_path, [cause("real_date", 3)])
    assert [n for n in f.notes if n.startswith("a mechanizable cause is diagnosed")]


def test_l5_is_suggested_for_the_loop_causes_only(tmp_path):
    assert not focused(tmp_path, [cause("missed_action", 3), cause("real_date", 2)]).l5_suggested
    f = focused(tmp_path, [cause("missed_action", 3), cause("context_overflow", 1)])
    assert f.l5_suggested and kinds_of(f)["context_overflow"] == ["hook", "agent", "vendor", "config", "skill", "prompt"]
    assert [n for n in f.notes if "at most 3 such change a round" in n]       # v1.12: l5_per_round 3
    assert not focused(tmp_path, [cause("context_overflow", 1)], allowed=("tool", "skill")).l5_suggested
    assert set(LOOP_CAUSES) == {"context_overflow", "finish_protocol", "tool_error_loop"}


def test_the_list_suggests_an_audit_hook_when_text_only_causes_hold_enough_of_the_cases(tmp_path):
    """IMPROVER §6 (v1.3): causes with text carriers only (not mechanizable, not of the loop) at audit_hook_share of
    the cases → audit_suggested, with a note naming them; a loop cause or a mechanizable one does not count."""
    def with_cases(name, n, **kw):
        c = cause(name, n, **kw)
        return Cause(cause_id=c.cause_id, name=c.name, tasks=c.tasks, cases=tuple(f"case:{name}.{i}" for i in range(n)),
                     mechanizable=c.mechanizable)
    text = [with_cases("missed_action", 3), with_cases("unread_value", 2)]
    code = [with_cases("real_date", 4), with_cases("context_overflow", 1)]
    f = focused(tmp_path, text + code)
    assert f.text_only_share == 0.5 and f.audit_suggested
    assert f.notes[0].startswith("audit hook suggested: 50% of the cases are in causes with text carriers only "
                                 "(missed_action, unread_value)")
    f = focused(tmp_path, text[:1] + code + [with_cases("arith_mismatch", 4)])          # 3 of 12
    assert f.text_only_share == 0.25 and not f.audit_suggested and not [n for n in f.notes if "audit" in n]
    assert focused(tmp_path, text[:1] + code + [with_cases("arith_mismatch", 4)], audit_hook_share=0.25).audit_suggested
    assert not focused(tmp_path, text, allowed=("tool", "skill", "prompt")).audit_suggested     # K allows no hook
    assert not focused(tmp_path, code).audit_suggested and focused(tmp_path, code).text_only_share == 0.0
    assert Focus.from_json(json.loads(json.dumps(f.to_json()))) == f


def test_l6_unavailable_only_when_stalled(tmp_path):
    c = [cause("real_date", 2)]
    f = focused(tmp_path, c, is_stalled=True)
    assert [n for n in f.notes if n.startswith("l6_unavailable")] == \
        ["l6_unavailable: no acceptance in 3 rounds and the training chain is not bound (stage 5)"]
    assert not f.l6_suggested
    assert not [n for n in focused(tmp_path, c, is_stalled=False).notes if n.startswith("l6_unavailable")]
    f = focused(tmp_path, c, is_stalled=True, train_available=True)
    assert f.l6_suggested and not [n for n in f.notes if n.startswith("l6_unavailable")]


def test_stalled_reads_no_acceptance_in_the_last_n_focused_rounds(tmp_path):
    ledger = Ledger(tmp_path)

    def focused_round(i):
        ledger.row("focused", round_id=f"s/r{i}", line=LINE, causes=[])
    assert not stalled(ledger, 3)
    focused_round(0)
    assert stalled(ledger, 1) and not stalled(ledger, 3)
    focused_round(1)
    assert not stalled(ledger, 3)
    focused_round(2)
    assert stalled(ledger, 3)
    ledger.row("accepted", round_id="s/r1", line=LINE, dir="package", release="release:x")
    assert not stalled(ledger, 3) and stalled(ledger, 1)
    focused_round(3)
    assert not stalled(ledger, 3)                     # r1..r3 hold the acceptance
    ledger.row("screened", round_id="s/r3", line=LINE, accepted=False)
    ledger.row("rejected", round_id="s/r3", line=LINE)
    focused_round(4)
    assert stalled(ledger, 3)                         # r2..r4: no acceptance; screened and rejected are not one
    assert not stalled(ledger, 5)


def test_the_round_writes_the_focus_list_and_records_l6(tmp_path, fake_upstream):
    """Round.focused: the list on record as an object and a ledger row; `l6_unavailable` goes to the ledger only
    after stall_rounds focused rounds without an acceptance."""
    a = round_activity(tmp_path, fake_upstream, "tool", baseline=False, freeze=False).a
    ledger = Ledger(a.root)
    rid = lambda i: f"{a.scope_id}/r{i}"  # noqa: E731
    for i in range(3):
        ledger.row("focused", round_id=rid(i), line=a.domain, causes=[])
    causes = [a.objects.put(c) for c in (cause("real_date", 3), cause("arith_mismatch", 2))]

    def run(i) -> Focus:
        r = Round(a, i, run_arm=None, out=quiet)
        r.state["steps"]["diagnosed"] = {"causes": causes}
        r.state["steps"]["focused"] = r.focused()
        return r.focus()
    f = run(3)
    assert [x["name"] for x in f.ranked] == ["real_date", "arith_mismatch"] and f.require_code
    assert [(x["round_id"], x["reason"].split(":")[0]) for x in ledger.rows(stage="l6_unavailable")] == \
        [(rid(3), "l6_unavailable")]
    row = ledger.rows(stage="focused")[-1]
    assert row["round_id"] == rid(3) and row["require_code"] is True
    assert row["causes"] == [{"cause": "real_date", "tasks": 3, "kinds": ["tool", "hook", "skill", "prompt"]},
                             {"cause": "arith_mismatch", "tasks": 2, "kinds": ["tool", "hook", "skill", "prompt"]}]
    ledger.row("accepted", round_id=rid(3), line=a.domain, dir="package", release="release:x")
    run(4)
    assert [x["round_id"] for x in ledger.rows(stage="l6_unavailable")] == [rid(3)]
