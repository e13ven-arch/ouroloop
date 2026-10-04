"""v0.6 diagnosis input (docs/modules/04_RESEARCH_LOOP.md §1, 03 §2, 07 §4): the grading store (ER6-15, BR6-16),
failure summaries and the per-task notes."""
import json

from ouroloop import policies as P
from ouroloop.benchmark import Results
from ouroloop.evolve import contracts as ct
from ouroloop.evolve.task_eval import make_agent_factory, run_tasks
from tests import rsi


def run(fake_upstream, tmp_path, tasks, split, *, worker=None):
    suite = rsi.suite(fake_upstream)
    splits = rsi.splits(suite)
    releases, rel, objects = rsi.workspace(tmp_path, split_ref=splits.hash())
    bound = P.bind_release(rel, profile="task", services=P.Services(objects), authorization="bootstrap")
    manifest = rsi.run_manifest("base", "base", split, data=splits.hash(), system_release_ref=rel)
    results = Results(tmp_path, rsi.DOMAIN)
    factory = make_agent_factory(rsi.runtime(tmp_path), worker or rsi.BusinessWorker(), budget=manifest.budget,
                                 bound=bound, objects=objects)
    rows = run_tasks(suite, tasks, factory, run=manifest, splits=splits, results=results, release_ref=rel)
    return results, rows, objects


def test_er6_15_every_attempt_writes_its_grading_apart_from_the_result(fake_upstream, tmp_path):
    results, rows, _ = run(fake_upstream, tmp_path, ["rsi.notify_after_update"], "train")
    results_dev, rows_dev, _ = run(fake_upstream, tmp_path / "d", ["rsi.dev_notify_after_update"], "dev")
    for store, row in ((results, rows[0]), (results_dev, rows_dev[0])):
        lines = [json.loads(line) for line in store.grading_path().read_text().splitlines()]
        assert [line["attempt_id"] for line in lines] == [row.attempt_id]
        assert all({"type", "params", "passed", "excluded"} <= set(a) for a in lines[0]["assertions"])
        written = json.loads(store.path.read_text().splitlines()[0])
        assert "assertions" not in written and "grading" not in written       # results.jsonl is unchanged


def test_br6_16_the_reader_hands_out_train_detail_only(fake_upstream, tmp_path):
    results, rows, _ = run(fake_upstream, tmp_path, ["rsi.notify_after_update"], "train")
    assert results.grading(rows[0].attempt_id)                                  # train: detail
    dev, rows_dev, _ = run(fake_upstream, tmp_path / "d", ["rsi.dev_notify_after_update"], "dev")
    assert dev.grading(rows_dev[0].attempt_id) == []                            # dev: nothing by default
    assert dev.grading(rows_dev[0].attempt_id, splits=("final",)) == []


# ---------------------------------------------------------------------------- failure summaries (RL6-17, RL6-25)
import pytest

from ouroloop.research import diagnosis as dg


class Row:
    def __init__(self, split="train", success=False, validity="valid", task_id="t1"):
        self.task_id, self.family_id, self.split, self.success, self.validity = task_id, task_id, split, success, validity
        self.attempt_id, self.termination, self.partial_credit, self.evidence = f"a/{task_id}", "finished", 0.5, ""


def test_rl6_17_only_train_attempts_are_summarised():
    with pytest.raises(ct.ContractError, match="only train"):
        dg.failure_summary(Row(split="dev"), [], [], dg.DiagnosisLimits())

    class Store:
        def grading(self, attempt_id):
            return []
    rows = [Row(split="dev", task_id="d"), Row(split="final", task_id="f"), Row(task_id="t"),
            Row(task_id="ok", success=True), Row(task_id="bad", validity="invalid")]
    got = dg.failure_summaries(rows, store=Store(), transcripts="/nonexistent", limits=dg.DiagnosisLimits())
    assert [s["task_id"] for s in got] == ["t"]


def test_rl6_25_excluded_assertions_are_left_out_and_broken_free_ones_are_failures():
    grading = [{"type": "email_sent", "params": {"to": "a@x.io"}, "passed": False, "excluded": False},
               {"type": "declared_unscored", "params": {}, "passed": True, "excluded": True},
               {"type": "still_true", "params": {}, "passed": True, "excluded": True},
               {"type": "not_emailed_dnc", "params": {"to": "dnc@x.io"}, "passed": False, "excluded": False},
               {"type": "done", "params": {}, "passed": True, "excluded": False}]
    summary = dg.failure_summary(Row(), [], grading, dg.DiagnosisLimits())
    assert [(c["type"], c["passed"]) for c in summary["checks"]] == [
        ("email_sent", False), ("not_emailed_dnc", False), ("done", True)]


def test_summaries_cut_long_outputs_and_record_events():
    transcript = [{"kind": "task", "text": "do it"},
                  {"kind": "assistant", "text": "", "tool_calls": [{"name": "api_search", "args": {"q": "x"}}]},
                  {"kind": "tool", "name": "api_search", "is_error": False, "output": "y" * 5000},
                  {"kind": "assistant", "text": "", "tool_calls": [{"name": "read_skill", "args": {"name": "s"}}]},
                  {"kind": "tool", "name": "read_skill", "is_error": True, "output": "no skill"},
                  {"kind": "pushback", "reason": "check again"}]
    s = dg.failure_summary(Row(), transcript, [], dg.DiagnosisLimits(max_entry_chars=100))
    assert s["task_text"] == "do it" and len(s["turns"]) == 2
    assert len(s["turns"][0]["results"][0]["output"]) < 200
    assert [e["event"] for e in s["events"]] == ["read_skill", "pushback"] and s["tool_errors"][0]["tool"] == "read_skill"


def test_rl6_26_the_literal_check_refuses_a_train_answer_in_the_package(tmp_path):
    import json as _json

    from ouroloop.agent import harness_package as hp
    from ouroloop.research.drafts import Drafts
    objects = ct.Objects(tmp_path)
    parent = hp.empty_package(objects)
    drafts = Drafts(objects, parent_ref=parent, limits=hp.Limits(), literals={"dana.reyes@example.com"})
    manifest = hp.empty_manifest()
    manifest["memory"] = {"enabled": True}
    bad = {"write": {"manifest.json": _json.dumps(manifest), "memory.md": "Always mail dana.reyes@example.com."}}
    good = {"write": {"manifest.json": _json.dumps(manifest), "memory.md": "Mail the contact the request names."}}
    proposal = ct.ExperimentProposal(owner="worker", changes=({"target": "worker.harness", "kind": "workflow",
                                                                "change": {"patch": bad}},))
    seen = []
    out = drafts.resolve(proposal, revise=lambda prompt, patch, check: seen.append(check) or good, prompt="p")
    assert out.status == "ready" and "task-specific values" in seen[0]
    assert "dana.reyes" not in _json.dumps(hp.files_of(objects, out.package_ref))


def test_no_readable_note_makes_an_invalid_round(fake_upstream, tmp_path):
    """docs/modules/04_RESEARCH_LOOP.md §1: when no note of the round could be read, the summary is not asked for
    — it would read a summary of nothing as R's decision — and the round is invalid. The notes were not cached,
    so the next round asks for them again."""
    from test_harness_loop import v06_prepared
    from ouroloop import bench
    from ouroloop.evolve import experience as xp
    from ouroloop.research.basic import NOTE_FORMAT, PolicyResearcher
    from ouroloop.research.loop import counters_of
    cfg, activity, _ = v06_prepared(fake_upstream, tmp_path)
    cfg["rsi"]["schedule_ref"] = activity.objects.put_json("change", {"worker_rounds": 3, "patience": 2,
                                                                      "candidates_per_round": 1})
    note = {"failure_class": "missed_step", "root_cause": "r", "mechanism": "check every step",
            "component": "skills", "generality": "g"}
    asked = []

    class Replies:
        name = "fake"

        def __init__(self):
            self.notes = 0

        def complete(self, system, prompt):
            asked.append("note" if NOTE_FORMAT in system else "summary")
            if asked[-1] == "summary":
                return json.dumps({"next_action": "stop", "reason": "nothing to change"})
            self.notes += 1
            # round 0: one empty reply, one without a mechanism; afterwards readable notes
            return {1: "", 2: json.dumps({"failure_class": "missed_step", "root_cause": "x"})}.get(
                self.notes, json.dumps(note))
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=PolicyResearcher(Replies()))
    rounds = result["rounds"]
    assert [r["outcome"] for r in rounds] == ["invalid", "no_proposal", "stopped"], rounds
    assert rounds[0]["next_action"] == "none" and rounds[0]["reason"].startswith("no readable note")
    assert asked == ["note", "note", "note", "note", "summary"]      # round 0 asked for no summary
    diagnosis = [r for r in xp.read(activity.root) if r.get("kind") == "diagnosis"]
    assert diagnosis[0]["noted"] == 0 and len(diagnosis[0]["invalid"]) == diagnosis[0]["failures"] == 2
    assert diagnosis[1]["noted"] == 2 and diagnosis[1]["reused"] == 0   # not cached: asked again
    assert counters_of(rounds[:1]) == (1, 0)     # a round toward patience, not an infrastructure failure


@pytest.mark.parametrize("reply,why", [("", "was empty"), (None, "declined the request"),
                                       ("a short note in prose", "not readable JSON"),
                                       (json.dumps({"failure_class": "missed_step", "root_cause": "x"}),
                                        "no failure class or mechanism")])
def test_an_unreadable_note_records_why_it_could_not_be_read(fake_upstream, tmp_path, reply, why):
    """A refused, empty or non-JSON note reply is recorded as that, not as a note with missing fields: whoever reads
    the coverage, or the reason of a round in which no note could be read, sees what actually happened."""
    from test_harness_loop import v06_prepared
    from ouroloop import bench
    from ouroloop.bench import _Completions
    from ouroloop.chat import ChatTurn
    from ouroloop.evolve import experience as xp
    from ouroloop.research.basic import NOTE_FORMAT, PolicyResearcher
    cfg, activity, _ = v06_prepared(fake_upstream, tmp_path)

    class Notes:
        name = model = "fake"
        supports_history_edits = True

        def chat(self, system, messages, tools):
            if NOTE_FORMAT not in system:
                return ChatTurn(json.dumps({"next_action": "stop", "reason": "nothing"}), [], "end")
            return ChatTurn("", [], "refusal") if reply is None else ChatTurn(reply, [], "end")
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=PolicyResearcher(_Completions(Notes())))
    coverage = [r for r in xp.read(activity.root) if r.get("kind") == "diagnosis"][0]
    assert coverage["invalid"] and all(why in i["problem"] for i in coverage["invalid"])
    assert result["rounds"][0]["outcome"] == "invalid" and why in result["rounds"][0]["reason"]


def test_r0s_note_prompt_asks_the_note_to_keep_the_interface_facts():
    """docs/modules/04_RESEARCH_LOOP.md §1: the summary sees only the notes, so R0's note prompt asks each note to
    keep, word for word, what a change would need — tools, endpoints, methods, field names, error text, the agent's
    own words — while task answers stay out. The request a note is written from carries that prompt."""
    from ouroloop import policies as P
    from ouroloop.research.basic import PolicyResearcher
    assert "word for word, the interface facts" in P.DIAGNOSE_NOTE
    assert "never write this task's specific answers" in P.DIAGNOSE_NOTE
    system, _user = PolicyResearcher(None).note_request({"task_id": "t"}, P.DIAGNOSE_NOTE, allowed=("memory",))
    assert system.startswith(P.DIAGNOSE_NOTE)


def test_notes_written_concurrently_enter_the_summary_in_the_frozen_order(tmp_path):
    """M5 §2 (v0.3) 逐题笔记并发, EX6-19: notes asked for four at a time come back in another order than they were
    sent, and the summary still lists them in the frozen coverage order — the same notes, refs and coverage a
    serial run gives — each cached once; and a note that could not be read is charged to its own request, not to
    whichever request finished last."""
    import threading
    import time
    from pathlib import Path
    from ouroloop.research.basic import NOTE_FORMAT, PolicyResearcher
    delays = {"a": 0.06, "b": 0.0, "c": 0.03, "d": 0.01}

    class SlowNotes:
        def __init__(self):
            self.requests, self.order, self.lock = [], [], threading.Lock()

        def note_request(self, summary, prompt, *, allowed=()):
            return prompt, json.dumps({"failure": summary}, sort_keys=True)

        def note_with_record(self, system, user):
            task = json.loads(user)["failure"]["task_id"]
            time.sleep(delays[task])
            with self.lock:
                self.order.append(task)
                self.requests.append({"step": "note", "system": system})
            return {"failure_class": "missed_step", "root_cause": "r", "mechanism": f"m {task}",
                    "component": "skills", "generality": "g"}, ""
    summaries = [{"task_id": t, "checks": [], "turns": []} for t in "abcd"]

    def diagnosis(root, concurrency):
        return dg.Diagnosis(summaries=summaries, order=list("abcd"), cache=dg.NoteCache(Path(root), ct.Objects(root)),
                            model={"model": "m"}, components=("skills",), concurrency=concurrency)
    fast = SlowNotes()
    notes, coverage = diagnosis(tmp_path / "c", 4).run(fast, "diagnose")
    assert [n["task_id"] for n in notes] == list("abcd") and fast.order == list("bdca")
    slow = SlowNotes()
    serial_notes, serial_coverage = diagnosis(tmp_path / "s", 1).run(slow, "diagnose")
    assert notes == serial_notes and coverage == serial_coverage and slow.order == list("abcd")
    assert coverage["noted"] == 4 and coverage["included"] == list("abcd")
    again = SlowNotes()
    _, reused = diagnosis(tmp_path / "c", 4).run(again, "diagnose")
    assert reused["reused"] == 4 and again.order == []
    assert len(list(dg._read_jsonl(Path(tmp_path / "c") / "rsi" / "notes.jsonl"))) == 4

    class Model:
        """The research model: an empty reply for task b, a note for the others, each after its own delay."""
        name = "fake"

        def complete(self, system, text):
            task = json.loads(text)["failure"]["task_id"]
            time.sleep(delays[task])
            if task == "b":
                return ""
            return json.dumps({"failure_class": "missed_step", "root_cause": "r", "mechanism": f"m {task}",
                               "component": "skills", "generality": "g"})
    researcher = PolicyResearcher(Model())
    notes, coverage = diagnosis(tmp_path / "u", 4).run(researcher, "diagnose")
    assert [n["task_id"] for n in notes] == list("acd")
    assert coverage["invalid"] == [{"task_id": "b", "problem": "the research reply was empty"}]
    assert [r.get("unreadable") for r in researcher.requests if NOTE_FORMAT in r["system"]].count(
        "the research reply was empty") == 1


def test_a_failing_note_is_raised_after_the_answered_notes_are_cached(tmp_path):
    """M5 §2 (v0.3, after review): one note's provider error does not lose the others. Concurrently, the three
    answers that arrived are cached before the error is raised; serially, the two answered before it are; either way
    a restart asks only for what is not on disk."""
    import time
    from pathlib import Path
    from ouroloop.providers import ProviderError

    class Notes:
        def __init__(self, failing: str | None):
            self.failing, self.asked, self.requests = failing, [], []

        def note_request(self, summary, prompt, *, allowed=()):
            return prompt, json.dumps({"failure": summary}, sort_keys=True)

        def note_with_record(self, system, user):
            task = json.loads(user)["failure"]["task_id"]
            self.asked.append(task)
            time.sleep({"a": 0.0, "b": 0.01, "c": 0.02, "d": 0.03}[task])
            if task == self.failing:
                raise ProviderError("API Error: 500 Internal server error", retryable=True)
            return {"failure_class": "missed_step", "root_cause": "r", "mechanism": f"m {task}",
                    "component": "skills", "generality": "g"}, ""
    summaries = [{"task_id": t, "checks": [], "turns": []} for t in "abcd"]

    def diagnosis(root, concurrency):
        return dg.Diagnosis(summaries=summaries, order=list("abcd"), cache=dg.NoteCache(Path(root), ct.Objects(root)),
                            model={"model": "m"}, components=("skills",), concurrency=concurrency)

    def cached(root):
        return [row["identity"] for row in dg._read_jsonl(Path(root) / "rsi" / "notes.jsonl")]
    with pytest.raises(ProviderError):
        diagnosis(tmp_path / "c", 4).run(Notes("c"), "diagnose")
    assert len(cached(tmp_path / "c")) == 3
    with pytest.raises(ProviderError):
        diagnosis(tmp_path / "s", 1).run(Notes("c"), "diagnose")
    assert len(cached(tmp_path / "s")) == 2
    for root, missing in ((tmp_path / "c", ["c"]), (tmp_path / "s", ["c", "d"])):
        again = Notes(None)
        notes, coverage = diagnosis(root, 4).run(again, "diagnose")
        assert sorted(again.asked) == missing and coverage["noted"] == 4 and coverage["reused"] == 4 - len(missing)
        assert [n["task_id"] for n in notes] == list("abcd") and len(cached(root)) == 4
