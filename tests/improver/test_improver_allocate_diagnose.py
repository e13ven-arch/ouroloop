"""Diagnosis and layering (docs/design/IMPROVER.md §5 diagnose.py, §6 allocate.py, §13 "诊断" and "分层"; DESIGN §6.3,
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

import json
import math
from dataclasses import replace
from types import SimpleNamespace

import pytest
from rounds import quiet, round_activity
from scripted_r import ScriptedR

from ouroloop.contracts import Objects, read_jsonl, write_json
from ouroloop.improver import diagnose as dg
from ouroloop.improver.allocate import Assignment, Estimates, allocate, kinds_for, stalled
from ouroloop.improver.diagnose import Case, Cause, NoteCache
from ouroloop.improver.evidence import Evidence
from ouroloop.improver.ledger import Ledger
from ouroloop.improver.round import Round
from ouroloop.improver.settings import CAUSES, LOOP_CAUSES, MECHANIZABLE, Settings
from ouroloop.runtime.chat import ChatTurn

NOTIFY = ("rsi.notify_after_update", "rsi.notify_after_update_variant")
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
    """The rsi activity after its baseline: the two notify tasks failed (missed #ops message), the rest passed."""
    s = round_activity(tmp_path_factory.mktemp("diagnose"), fake_upstream, "tool")
    evidence = Evidence(s.a)
    failed = evidence.failed(s.a.active_release().package_ref)
    assert [e.task_id for e in failed] == list(NOTIFY)
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
        assert "#ops" in case.note and case.proposed == ""
        assert case.evidence_refs and all(x.startswith(ev.session + ":L") for x in case.evidence_refs)
        assert case.assertion_kinds == ("slack_message_contains",)
        assert case.termination == ev.result.termination
        assert case.tool_errors == tuple(sorted(set(ev.tool_errors)))
        assert ref == case.ref() and Case.from_json(cache.objects.get(ref)).ref() == ref
    assert r.calls == ["diagnose", "diagnose"]
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


@pytest.mark.xfail(strict=True, reason="BUG: Case.note is a VOLATILE field (contracts.py:145), so Objects.put "
                                       "drops it: a cached case (diagnose.py:171) and every case the round reads "
                                       "back for R's inputs/causes.json (round.py:335-339) has note == '' "
                                       "(IMPROVER §5, §7)")
def test_a_case_read_back_keeps_its_note(base, tmp_path):
    ev = base.failed[0]
    cache = cache_in(tmp_path)
    ref, case, _ = case_of(base.evidence, ev, Aux({"cause": "missed_action", "note": "it never posted in #ops"}), cache)
    assert case.note == "it never posted in #ops"
    _, cached, hit = case_of(base.evidence, ev, Aux({"cause": "other"}), cache)
    assert hit
    assert cached.note == case.note
    assert Case.from_json(cache.objects.get(ref)).note == case.note


@pytest.mark.xfail(strict=True, reason="BUG: NoteCache's key is (request digest, model) without the attempt id "
                                       "(IMPROVER §5: (attempt_id, request digest, model) -> Case ref); two attempts "
                                       "whose requests coincide (no trajectory tail) share one case, which names the "
                                       "first attempt")
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
    assert (only.name, only.tasks, only.mechanizable) == ("missed_action", NOTIFY, True)
    assert only.cases == tuple(ref for ref, _ in cases)


def test_two_cases_proposing_a_name_add_it_for_the_next_round(base):
    """Round.diagnosed: the extension goes to the ledger (vocabulary_added) and the next round's vocabulary has it."""
    proposer = Aux(lambda user: {"cause": "other", "proposed": "stale_record", "note": "n",
                                 "evidence_refs": tail_refs(user)[-1:]})
    research = base.s.research
    kept, research.r = research.r, proposer
    try:
        r = Round(base.a, 1, run_arm=None, out=quiet)
        assert "stale_record" not in r.vocabulary()
        r.state["steps"]["evidence"] = r.gather()
        got = r.diagnosed()
    finally:
        research.r = kept
    assert len(proposer.asked) == 2
    rows = [x for x in read_jsonl(base.a.root / "ledger" / "experience.jsonl") if x["stage"] == "vocabulary_added"]
    assert [(x["round_id"], x["names"]) for x in rows] == [(f"{base.a.scope_id}/r1", ["stale_record"])]
    (other,) = [Cause.from_json(base.a.objects.get(ref)) for ref in got["causes"]]
    assert (other.name, tuple(other.tasks), other.mechanizable) == ("other", NOTIFY, False)
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


def test_a_kind_on_record_with_no_reading_is_not_tried(tmp_path):
    write_json(tmp_path / "ledger" / "estimates.json",
               {LINE: {"real_date": {"hook": {"fixes": 0.0, "cost": 0.0, "n": 0},
                                     "tool": {"fixes": 0.0, "cost": 0.0, "n": 1}}}})
    assert Estimates(tmp_path, Settings()).tried(LINE, "real_date") == {"tool"}


# ---------------------------------------------------------------------------------------------- allocation
def cause(name: str, n: int, *, mech: bool | None = None) -> Cause:
    return Cause(cause_id=f"{ROUND}:{name}", name=name, tasks=tuple(f"{name}.{i}" for i in range(n)),
                 mechanizable=(name in MECHANIZABLE) if mech is None else mech)


def tried(n: int = 1, fixes: float = 0.0) -> dict:
    return {"fixes": fixes, "cost": 0.0, "n": n}


def assign(tmp_path, causes, table=None, *, allowed=ALL, no_code=frozenset(), is_stalled=False,
           train_available=False, **settings):
    """allocate() over an estimates table {cause: {kind: row}} for LINE; -> (assignments, notes)."""
    s = Settings(**settings)
    write_json(tmp_path / "ledger" / "estimates.json", {LINE: table or {}})
    return allocate(causes, Estimates(tmp_path, s), settings=s, line=LINE, allowed=allowed, no_code=no_code,
                    stalled=is_stalled, train_available=train_available)


def pairs(out) -> list[tuple[str, str]]:
    return [(a.cause, a.kind) for a in out]


def test_the_kinds_a_cause_may_get():
    assert kinds_for("real_date", mechanizable=True, allowed=ALL) == ["tool", "hook", "skill", "prompt"]
    assert kinds_for("missed_action", mechanizable=False, allowed=ALL) == ["skill", "prompt"]
    assert kinds_for("context_overflow", mechanizable=False, allowed=ALL) == ["skill", "prompt", *L5]
    assert kinds_for("tool_error_loop", mechanizable=True, allowed=ALL) == ["tool", "hook", "skill", "prompt", *L5]
    # never memory or model; only what K allows, in the fixed order whatever K's order
    assert kinds_for("tool_error_loop", mechanizable=True, allowed=("vendor", "prompt", "memory", "model")) == \
        ["prompt", "vendor"]
    assert kinds_for("real_date", mechanizable=True, allowed=tuple(reversed(ALL))) == ["tool", "hook", "skill",
                                                                                       "prompt"]


def test_the_upper_bound_orders_the_kinds(tmp_path):
    c = [cause("real_date", 3)]
    # tool tried and poor (0.225), hook untried (0.3 + 0.3), skill with the best mean (0.39) but the smaller bound
    table = {"real_date": {"tool": tried(3), "skill": tried(1, 0.7)}}
    out, notes = assign(tmp_path, c, table, explore_slots=0)
    assert pairs(out) == [("real_date", "hook")] and notes == []
    (a,) = out
    assert isinstance(a, Assignment) and a.cause_id == f"{ROUND}:real_date" and (a.slots, a.exploration) == (1, False)
    assert a.reason == "upper bound 0.6"
    # a reading that beats the bound moves it (the code guarantee off, to see the order alone)
    table = {"real_date": {"tool": tried(3), "hook": tried(3), "skill": tried(1, 1.0)}}
    out, _ = assign(tmp_path, c, table, explore_slots=0, code_guarantee=False)
    assert pairs(out) == [("real_date", "skill")]


def test_ties_go_to_the_earlier_kind_code_first(tmp_path):
    for allowed in (ALL, tuple(reversed(ALL))):
        out, _ = assign(tmp_path, [cause("real_date", 2)], allowed=allowed, explore_slots=0)
        assert pairs(out) == [("real_date", "tool")]
        out, _ = assign(tmp_path, [cause("missed_action", 2)], allowed=allowed, explore_slots=0)
        assert pairs(out) == [("missed_action", "skill")]
    table = {"real_date": {"tool": tried(3), "hook": tried(3)},
             "missed_action": {"skill": tried(1), "prompt": tried(1)}}
    out, _ = assign(tmp_path, [cause("real_date", 2), cause("missed_action", 1)], table, explore_slots=0)
    assert pairs(out) == [("real_date", "tool"), ("missed_action", "skill")]


def test_clusters_by_size_at_most_k(tmp_path):
    causes = [cause("missed_action", 2), cause("wrong_target", 4), cause("real_date", 1), cause("premature_stop", 4),
              cause("unread_value", 3)]
    out, _ = assign(tmp_path, causes, explore_slots=0, code_guarantee=False)
    assert [a.cause for a in out] == ["premature_stop", "wrong_target", "unread_value"]
    out, _ = assign(tmp_path, causes, explore_slots=0, code_guarantee=False, k=1)
    assert [a.cause for a in out] == ["premature_stop"]
    out, _ = assign(tmp_path, causes, explore_slots=0, code_guarantee=False, k=9)
    assert [a.cause for a in out] == ["premature_stop", "wrong_target", "unread_value", "missed_action", "real_date"]
    # a cause no allowed kind fits takes no slot
    out, _ = assign(tmp_path, [cause("wrong_target", 5), cause("real_date", 1)], allowed=("tool", "hook"), k=1,
                    explore_slots=0)
    assert pairs(out) == [("real_date", "tool")]


def test_the_exploration_slot_takes_an_untried_kind_code_first(tmp_path):
    c = [cause("real_date", 3)]
    table = {"real_date": {"tool": tried(1, 1.0)}}            # tool's bound 0.86 beats the untried hook's 0.6
    out, _ = assign(tmp_path, c, table, explore_slots=0)
    assert [(a.kind, a.exploration) for a in out] == [("tool", False)]
    out, _ = assign(tmp_path, c, table)
    (a,) = out
    assert (a.kind, a.exploration, a.reason) == ("hook", True, "exploration: hook never tried on real_date")
    # code first: with both code kinds tried, a text kind (the code guarantee off: it would put tool back)
    both = {"real_date": {"tool": tried(1, 1.0), "hook": tried(1)}}
    out, _ = assign(tmp_path, c, both, code_guarantee=False)
    assert [(a.kind, a.exploration) for a in out] == [("skill", True)]
    out, _ = assign(tmp_path, c, both)
    assert [(a.kind, a.exploration) for a in out] == [("tool", False)]
    # every kind tried: no exploration on this cause, the slot goes to the next one
    full = {k: tried(1) for k in ("tool", "hook", "skill", "prompt")}
    out, _ = assign(tmp_path, [cause("real_date", 3), cause("arith_mismatch", 2)], {"real_date": full})
    assert [(a.cause, a.kind, a.exploration) for a in out] == [("real_date", "tool", False),
                                                               ("arith_mismatch", "tool", True)]


def test_an_untried_chosen_kind_stays_as_the_exploration(tmp_path):
    # nothing tried on a loop cause: agent's bound (0.4) is the highest, and it is itself never tried
    out, _ = assign(tmp_path, [cause("context_overflow", 2)])
    assert [(a.kind, a.exploration) for a in out] == [("agent", True)]


def test_only_explore_slots_assignments_explore(tmp_path):
    causes = [cause("real_date", 3), cause("arith_mismatch", 2), cause("excluded_mentioned", 1)]
    assert [a.exploration for a in assign(tmp_path, causes)[0]] == [True, False, False]
    assert [a.exploration for a in assign(tmp_path, causes, explore_slots=2)[0]] == [True, True, False]
    assert [a.exploration for a in assign(tmp_path, causes, explore_slots=0)[0]] == [False, False, False]


def test_the_code_guarantee(tmp_path):
    poor_code = {"tool": tried(3), "hook": tried(3), "skill": tried(1, 1.0)}
    out, notes = assign(tmp_path, [cause("real_date", 3)], {"real_date": poor_code}, explore_slots=0)
    (a,) = out
    assert (a.kind, a.reason) == ("tool", "code guarantee: real_date is mechanizable")
    assert notes == ["code guarantee: real_date -> tool"]
    out, notes = assign(tmp_path, [cause("real_date", 3)], {"real_date": poor_code}, explore_slots=0,
                        code_guarantee=False)
    assert pairs(out) == [("real_date", "skill")] and notes == []
    # the mechanizable cause's own slot is the one replaced
    causes = [cause("real_date", 5), cause("wrong_target", 2)]
    out, _ = assign(tmp_path, causes, {"real_date": poor_code}, explore_slots=0)
    assert pairs(out) == [("real_date", "tool"), ("wrong_target", "skill")]
    # a mechanizable cause outside the k chosen: the last slot goes to the largest mechanizable cause
    causes = [cause("wrong_target", 5), cause("unread_value", 4), cause("real_date", 1), cause("arith_mismatch", 2)]
    out, notes = assign(tmp_path, causes, explore_slots=0, k=2)
    assert pairs(out) == [("wrong_target", "skill"), ("arith_mismatch", "tool")]
    assert notes == ["code guarantee: arith_mismatch -> tool"]
    # one code kind in the round is enough
    causes = [cause("real_date", 5), cause("excluded_mentioned", 2)]
    out, notes = assign(tmp_path, causes, {"real_date": poor_code}, explore_slots=0)
    assert pairs(out) == [("real_date", "skill"), ("excluded_mentioned", "tool")] and notes == []
    # the code kind K allows; none allowed, no guarantee
    out, _ = assign(tmp_path, [cause("real_date", 3)], {"real_date": poor_code}, explore_slots=0,
                    allowed=("hook", "skill", "prompt"))
    assert pairs(out) == [("real_date", "hook")]
    out, notes = assign(tmp_path, [cause("real_date", 3)], {"real_date": poor_code}, explore_slots=0,
                        allowed=("skill", "prompt"))
    assert pairs(out) == [("real_date", "skill")] and notes == []
    # no mechanizable cause, no guarantee
    out, notes = assign(tmp_path, [cause("wrong_target", 3)], explore_slots=0)
    assert pairs(out) == [("wrong_target", "skill")] and notes == []


def test_two_code_slots_after_a_round_without_a_code_candidate(tmp_path):
    poor_code = {"tool": tried(3), "hook": tried(3), "skill": tried(1, 1.0)}
    causes = [cause("real_date", 3), cause("excluded_mentioned", 2), cause("arith_mismatch", 1)]
    out, _ = assign(tmp_path, causes, {"arith_mismatch": poor_code}, explore_slots=0,
                    no_code={"real_date", "arith_mismatch"})
    assert [(a.cause, a.kind, a.slots) for a in out] == [("real_date", "tool", 2), ("excluded_mentioned", "tool", 1),
                                                         ("arith_mismatch", "skill", 1)]
    assert out[0].reason.endswith("; two code slots after a round without one")
    assert "two code slots" not in out[1].reason + out[2].reason
    # the guarantee's code slot doubles too
    out, _ = assign(tmp_path, [cause("real_date", 3)], {"real_date": poor_code}, explore_slots=0,
                    no_code={"real_date"})
    assert [(a.kind, a.slots) for a in out] == [("tool", 2)]
    out, _ = assign(tmp_path, causes, {"arith_mismatch": poor_code}, explore_slots=0)
    assert [a.slots for a in out] == [1, 1, 1]


def test_l5_kinds_only_for_the_loop_causes(tmp_path):
    great_agent = {"agent": tried(1, 5.0), "config": tried(1, 5.0), "vendor": tried(1, 5.0)}
    causes = [cause("missed_action", 3), cause("real_date", 2), cause("context_overflow", 1)]
    table = {c.name: great_agent for c in causes}
    out, _ = assign(tmp_path, causes, table, explore_slots=0)
    assert pairs(out) == [("missed_action", "skill"), ("real_date", "tool"), ("context_overflow", "agent")]
    assert set(LOOP_CAUSES) == {"context_overflow", "finish_protocol", "tool_error_loop"}


def test_at_most_l5_slots_l5_kinds_a_round(tmp_path):
    causes = [cause("context_overflow", 3), cause("finish_protocol", 2)]
    out, _ = assign(tmp_path, causes, explore_slots=0)
    assert pairs(out) == [("context_overflow", "agent"), ("finish_protocol", "skill")]
    out, _ = assign(tmp_path, causes, explore_slots=0, l5_slots=2)
    assert pairs(out) == [("context_overflow", "agent"), ("finish_protocol", "agent")]
    out, _ = assign(tmp_path, causes, explore_slots=0, l5_slots=0)
    assert pairs(out) == [("context_overflow", "skill"), ("finish_protocol", "skill")]
    # the exploration slot does not add an L5 kind past the limit either
    table = {"context_overflow": {"agent": tried(1, 1.0), **{k: tried(1) for k in ("skill", "prompt", "config",
                                                                                     "vendor")}},
             "finish_protocol": {"skill": tried(1, 1.0), "prompt": tried(1)}}
    out, _ = assign(tmp_path, causes, table)
    assert [(a.cause, a.kind, a.exploration) for a in out] == [("context_overflow", "agent", False),
                                                               ("finish_protocol", "skill", False)]


@pytest.mark.xfail(strict=True, reason="BUG: allocate.py:100 falls back to the L5 kinds (`... or kinds`) when a loop "
                                       "cause has no other allowed kind, so a round can get more than l5_slots L5 "
                                       "assignments (IMPROVER §6.5: L5 每轮 ≤ l5_slots)")
def test_l5_slots_hold_when_k_allows_only_l5_kinds(tmp_path):
    out, _ = assign(tmp_path, [cause("context_overflow", 3), cause("finish_protocol", 2)], allowed=L5,
                    explore_slots=0)
    assert sum(a.kind in L5 for a in out) <= 1


@pytest.mark.xfail(strict=True, reason="BUG: allocate.py:108-122 does not count the L5 kinds the exploration slots "
                                       "pick (`l5` is not updated), so with explore_slots >= 2 two explorations can "
                                       "both take an L5 kind past l5_slots (IMPROVER §6.5: L5 每轮 ≤ l5_slots)")
def test_l5_slots_hold_across_exploration_slots(tmp_path):
    table = {name: {"skill": tried(1, 1.0), "prompt": tried(1)} for name in ("context_overflow", "finish_protocol")}
    out, _ = assign(tmp_path, [cause("context_overflow", 3), cause("finish_protocol", 2)], table, explore_slots=2)
    assert sum(a.kind in L5 for a in out) <= 1


def test_l6_unavailable_only_when_stalled(tmp_path):
    c = [cause("real_date", 2)]
    _, notes = assign(tmp_path, c, is_stalled=True)
    assert [n for n in notes if n.startswith("l6_unavailable")] == \
        ["l6_unavailable: no install in 3 rounds and the training chain is not bound (stage 5)"]
    _, notes = assign(tmp_path, c, is_stalled=False)
    assert not [n for n in notes if n.startswith("l6_unavailable")]
    _, notes = assign(tmp_path, c, is_stalled=True, train_available=True)
    assert not [n for n in notes if n.startswith("l6_unavailable")]


def test_stalled_reads_no_install_in_the_last_n_allocated_rounds(tmp_path):
    ledger = Ledger(tmp_path)

    def allocated(i):
        ledger.row("allocated", round_id=f"s/r{i}", line=LINE, assignments=[])
    assert not stalled(ledger, 3)
    allocated(0)
    assert stalled(ledger, 1) and not stalled(ledger, 3)
    allocated(1)
    assert not stalled(ledger, 3)
    allocated(2)
    assert stalled(ledger, 3)
    ledger.row("installed", round_id="s/r1", line=LINE, dir="notify", release="release:x")
    assert not stalled(ledger, 3) and stalled(ledger, 1)
    allocated(3)
    assert not stalled(ledger, 3)                     # r1..r3 hold the install
    ledger.row("gated", round_id="s/r3", line=LINE)
    ledger.row("rejected", round_id="s/r3", line=LINE)
    allocated(4)
    assert stalled(ledger, 3)                         # r2..r4: no install; gated and rejected are not installs
    assert not stalled(ledger, 5)


def test_the_round_doubles_the_previous_rounds_no_code_cause_and_records_l6(tmp_path, fake_upstream):
    """Round.allocated: `no_code_candidate` of round i-1 (not older) doubles; `l6_unavailable` goes to the ledger only
    after stall_rounds allocated rounds without an install."""
    a = round_activity(tmp_path, fake_upstream, "tool", baseline=False, freeze=False).a
    ledger = Ledger(a.root)
    rid = lambda i: f"{a.scope_id}/r{i}"  # noqa: E731
    for i in range(3):
        ledger.row("allocated", round_id=rid(i), line=a.domain, assignments=[])
    ledger.row("no_code_candidate", round_id=rid(2), line=a.domain, cause="real_date")
    ledger.row("no_code_candidate", round_id=rid(1), line=a.domain, cause="arith_mismatch")
    causes = [a.objects.put(c) for c in (cause("real_date", 3), cause("arith_mismatch", 2))]

    def allocated(i):
        r = Round(a, i, run_arm=None, out=quiet)
        r.state["steps"]["diagnosed"] = {"causes": causes}
        got = r.allocated()
        return [Assignment.from_json(a.objects.get(ref)) for ref in got["assignments"]], got["notes"]
    out, notes = allocated(3)
    assert [(x.cause, x.kind, x.slots) for x in out] == [("real_date", "tool", 2), ("arith_mismatch", "tool", 1)]
    assert [(x["round_id"], x["reason"].split(":")[0]) for x in ledger.rows(stage="l6_unavailable")] == \
        [(rid(3), "l6_unavailable")]
    assert ledger.rows(stage="allocated")[-1]["round_id"] == rid(3)
    ledger.row("installed", round_id=rid(3), line=a.domain, dir="notify", release="release:x")
    out, notes = allocated(4)
    assert [x.slots for x in out] == [1, 1]
    assert not [n for n in notes if n.startswith("l6_unavailable")]
    assert [x["round_id"] for x in ledger.rows(stage="l6_unavailable")] == [rid(3)]
