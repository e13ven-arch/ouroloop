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
    factory = make_agent_factory(rsi.ws(tmp_path), worker or rsi.BusinessWorker(), budget=manifest.budget,
                                 bound=bound)
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


# ---------------------------------------------------------------------------- v0.3: R's new inputs (M4 §1 R 的输入)
def test_read_trajectory_is_train_only_read_only_and_byte_capped(fake_upstream, tmp_path):
    """RL6-36: the session's trajectory tool returns a byte range of a train attempt's host-written record, caps each
    read at `max_trajectory_bytes` and the session at `max_trajectory_bytes_total`, refuses a dev attempt as
    AccessDenied (the call still recorded), and never writes: the transcript on disk is unchanged."""
    from ouroloop.research.session import SessionLimits, TrajectoryReader
    run(fake_upstream, tmp_path, [rsi.TRAIN[1]], "train")
    results, _, _ = run(fake_upstream, tmp_path, [rsi.DEV[0]], "dev")
    rows = {r.split: r for r in results.all()}
    path = tmp_path / "transcripts" / f"{rows['train'].evidence}.jsonl"
    before = path.read_bytes()
    reader = TrajectoryReader(results, tmp_path / "transcripts", SessionLimits(max_trajectory_bytes=200,
                                                                               max_trajectory_bytes_total=300))
    text, bad = reader.read(rows["train"].attempt_id, 0, 10_000)
    assert not bad and text.startswith(before[:50].decode()) and reader.total == 200 and "bytes follow" in text
    denied, bad = reader.read(rows["dev"].attempt_id)
    assert bad and denied.startswith("AccessDenied") and reader.calls[-1] == {"ref": rows["dev"].attempt_id,
                                                                              "offset": 0, "bytes": 0,
                                                                              "denied": "not a valid train attempt"}
    refused, bad = reader.read(rows["train"].attempt_id, 200, 200)
    assert bad and "cumulative cap of 300" in refused and reader.total == 200
    tail, bad = reader.read(rows["train"].attempt_id, 200, 100)
    assert not bad and reader.total == 300
    assert (reader.read("not-a-ref")[1], reader.read(rows["train"].attempt_id, "x", 1)[1]) == (True, True)
    assert path.read_bytes() == before
    assert reader.listing([rows["train"].attempt_id, rows["dev"].attempt_id, "none"]) == [
        {"attempt_ref": rows["train"].attempt_id, "task_id": rows["train"].task_id, "bytes": len(before),
         "outcome": "passing" if rows["train"].success else "failing"}]


def test_the_dev_histogram_has_assertion_types_and_counts_only(fake_upstream, tmp_path):
    """RL6-37 / M4 §1: over the installed W's dev readings, the histogram counts failed scored assertions by type —
    excluded and passed assertions not counted — with the readings' and tasks' counts; it carries no params, no task
    ids and no per-task rows, and a test attempt in the same store is never read."""
    from ouroloop.research.session import dev_failure_histogram
    gap = rsi.BusinessWorker(ignore_finish_check=True)            # the fixture's gap: the notification is skipped
    results, dev_rows, _ = run(fake_upstream, tmp_path, rsi.DEV, "dev", worker=gap)
    run(fake_upstream, tmp_path, rsi.FINAL, "test", worker=gap)
    histogram = dev_failure_histogram(results, [r for r in results.all() if r.split == "dev"])
    assert set(histogram) == {"split", "types", "readings", "tasks", "attempts"}
    assert histogram["split"] == "dev" and histogram["readings"] == 1 and histogram["tasks"] == len(rsi.DEV)
    failed = [r for r in dev_rows if not r.success]
    assert failed and sum(histogram["types"].values()) >= len(failed)
    assert all(isinstance(k, str) and isinstance(v, int) for k, v in histogram["types"].items())
    assert "rsi." not in json.dumps(histogram) and "params" not in json.dumps(histogram)
    # a pool of test rows gives nothing: the projection reads the split it is asked for and refuses the rest
    final = dev_failure_histogram(results, [r for r in results.all() if r.split == "test"])
    assert final["types"] == {} and final["readings"] == 0


def test_the_ledger_projection_shows_dev_results_as_three_counts_only():
    """RL6-37 / M4 §1 台账投影: R's view of the ledger keeps each candidate's declaration, declaration ref, screen
    (train task ids allowed), verdict and the gate's three classes as counts; the dev task ids of the gate stage
    appear nowhere in it, and an arm's E0 gets the same counts in place of the lists."""
    from ouroloop.evolve import experience as xp
    rows = [xp.ledger_row("declared", "c1", scope="a", round=0, dir="d", declaration={
                "mechanism": "Check every step", "mechanism_normalized": "check every step", "site": "memory.md",
                "carrier": "memory", "predicted_affected": ["rsi.notify_after_update"], "trigger_condition": "always",
                "differs_from": [], "notes": ["note:x"]}, declaration_ref="declaration:sha256:" + "1" * 64,
                diff={"added": ["memory.md"], "modified": [], "deleted": [], "bytes": 3}, status="declared"),
            xp.ledger_row("drafted", "c1", drafts=[{"revision": 0, "package_ref": "package:x", "ok": True}],
                          package_ref="package:x", status="ready"),
            xp.ledger_row("admitted", "c1", status="admitted", reasons=[]),
            xp.ledger_row("screened", "c1", tasks=["rsi.notify_after_update", "rsi.update_stage"],
                          failing=["rsi.notify_after_update"], sample=["rsi.update_stage"],
                          before={"rsi.notify_after_update": False, "rsi.update_stage": True},
                          after={"rsi.notify_after_update": True, "rsi.update_stage": True}, net_flips=1,
                          cost_per_attempt=0.4, thin_screen=True, outcome="selected"),
            xp.ledger_row("gated", "c1", verdict="accepted", receipt="receipt:sha256:" + "2" * 64,
                          reading_kind="confirmed", gain=0.33, control_readings=2, cost_per_attempt={"subject": 0.4},
                          fixed=["rsi.dev_notify_after_update"], regressed=["rsi.dev_policy_hold"],
                          still_failing=["rsi.dev_update_stage"], installed=True)]
    folded = xp.ledger_rows(rows)
    assert len(folded) == 1 and folded[0]["outcome"] == "installed" and folded[0]["gate"]["fixed"] == ["rsi.dev_notify_after_update"]
    shown = xp.ledger_projection(folded)[0]
    assert shown["gate"] == {"verdict": "accepted", "reading_kind": "confirmed", "gain": 0.33, "control_readings": 2,
                             "cost_per_attempt": {"subject": 0.4}, "fixed": 1, "regressed": 1, "still_failing": 1}
    assert shown["screen"]["tasks"] == ["rsi.notify_after_update", "rsi.update_stage"] and shown["screen"]["thin_screen"]
    assert shown["declaration_ref"] == "declaration:sha256:" + "1" * 64 and shown["drafts"] == {"checked": 1, "passed": True}
    assert "rsi.dev" not in json.dumps(shown)
    stripped = xp.strip_dev_task_ids(rows[-1])
    assert (stripped["fixed"], stripped["regressed"], stripped["still_failing"]) == (1, 1, 1)
    # an arm reads the projection of its E0, where the lists are counts already
    assert xp.ledger_projection(xp.ledger_rows([*rows[:-1], stripped]))[0]["gate"]["fixed"] == 1
    assert xp.strip_dev_task_ids(rows[0]) is rows[0]
    with pytest.raises(ValueError, match="ledger stage"):
        xp.ledger_row("judged", "c1")


# ---------------------------------------------------------------------------- v0.3 step 5 (2026-10-06): notes by domain
FIVE = {"marketing": 27, "sales": 33, "support": 27, "hr": 38, "finance": 23}       # stage 2's W0 train failures


def _five(counts=FIVE):
    order = [f"{d}.t{i:02d}" for d, n in counts.items() for i in range(n)]       # the pooled frozen order
    return [{"task_id": t, "checks": [], "turns": []} for t in order], order


def test_notes_over_five_domains_are_stratified_and_rotate_within_each_domain(tmp_path):
    """M4 §1 (2026-10-06): failures over several domains are selected by domain in turn — 30 over five domains is six
    each, each domain's in its frozen order — where the pooled order gave marketing 27 + sales 3; the next round
    takes each domain's failures not yet noted, and a domain short of failures leaves its turns to the others. The
    coverage counts each domain's failures and inclusions; cached notes are reused by identity."""
    import collections
    from pathlib import Path
    summaries, order = _five()
    first, rest = dg.select_failures(summaries, order=order, last_included={}, cap=30)
    assert collections.Counter(s["task_id"].split(".")[0] for s in first) == {d: 6 for d in FIVE}
    assert [s["task_id"] for s in first[:5]] == ["marketing.t00", "sales.t00", "support.t00", "hr.t00", "finance.t00"]
    assert [s["task_id"] for s in first if s["task_id"].startswith("hr.")] == [f"hr.t{i:02d}" for i in range(6)]
    assert len(first) + len(rest) == len(summaries)
    history = {s["task_id"]: 0 for s in first}
    second, _ = dg.select_failures(summaries, order=order, last_included=history, cap=30)
    assert collections.Counter(s["task_id"].split(".")[0] for s in second) == {d: 6 for d in FIVE}
    assert not {s["task_id"] for s in second} & set(history)                     # every note new this round
    # a domain with two failures: its turns go to the others
    short, short_order = _five({**FIVE, "finance": 2})
    picked, _ = dg.select_failures(short, order=short_order, last_included={}, cap=30)
    counts = collections.Counter(s["task_id"].split(".")[0] for s in picked)
    assert len(picked) == 30 and counts["finance"] == 2 and sorted(counts.values()) == [2, 7, 7, 7, 7]

    class Notes:
        def __init__(self):
            self.asked = []

        def note_request(self, summary, prompt, *, allowed=()):
            return prompt, json.dumps({"failure": summary["task_id"]})

        def note_with_record(self, system, user):
            self.asked.append(json.loads(user)["failure"])
            return {"failure_class": "missed_step", "root_cause": "r", "mechanism": "m", "component": "skills",
                    "generality": "g"}, ""

    def run(history):
        return dg.Diagnosis(summaries=summaries, order=order, history=history, model={"model": "m"},
                            cache=dg.NoteCache(Path(tmp_path), ct.Objects(tmp_path)), components=("skills",),
                            limits=dg.DiagnosisLimits(max_notes=30)).run(asker, "diagnose")
    asker = Notes()
    _, coverage = run({})
    assert coverage["by_domain"] == {d: {"failures": n, "included": 6} for d, n in FIVE.items()}
    asker = Notes()
    _, again = run({})
    assert again["reused"] == 30 and asker.asked == []                          # by identity, nothing asked twice


def test_a_single_domain_selects_notes_exactly_as_before():
    """M4 §1 (2026-10-06): one domain — or ids that name none — selects exactly as the rule always did (least
    recently included first, ties in the frozen order), and its coverage has no per-domain counts, so an older
    workspace's note identities, coverage records and summary digests are what they were."""
    summaries = [{"task_id": f"marketing.t{i:02d}"} for i in range(40)]
    order = [s["task_id"] for s in reversed(summaries)]
    history = {"marketing.t39": 0, "marketing.t10": 1}

    def before(summaries, order, last_included, cap):       # the rule as it stood before 2026-10-06
        position = {t: i for i, t in enumerate(order)}
        ranked = sorted(summaries, key=lambda s: (last_included.get(s["task_id"], -1),
                                                  position.get(s["task_id"], len(position)), s["task_id"]))
        return ranked[:cap], ranked[cap:]
    assert dg.select_failures(summaries, order=order, last_included=history, cap=30) == \
        before(summaries, order, history, 30)
    plain = [{"task_id": t} for t in "dcba"]
    assert dg.select_failures(plain, order=list("abcd"), last_included={"a": 0}, cap=2) == \
        before(plain, list("abcd"), {"a": 0}, 2)
    assert dg.domains_of([s["task_id"] for s in summaries]) == {} and dg.domains_of(list("ab")) == {}


# ---------------------------------------------------------------------------- v0.4: the interface table and the direction
def _transcript(tmp_path, session: str, calls: list) -> None:
    """A host-written transcript under the runner shell: a broker event (the call's args) and a tool event (what came
    back) per call, matched by id."""
    lines = []
    for i, (tool, args, output, error) in enumerate(calls):
        eid = f"{session}:b{i}"
        lines.append({"kind": "broker_event", "event_id": eid, "tool": tool, "args": args})
        lines.append({"kind": "tool", "id": eid, "name": tool, "is_error": error, "output": output})
    (tmp_path / "transcripts").mkdir(exist_ok=True)
    (tmp_path / "transcripts" / f"{session}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def test_the_interface_table_counts_where_the_agent_fights_the_api_from_train_attempts_only(tmp_path):
    """V0_4_PLAN §1.2 (research/api_errors.py): per endpoint — `METHOD host/path`, a segment with a digit or seen in
    one task's calls an `{id}` — the calls, the tasks, the error responses by kind (code and message with numbers,
    quoted values and e-mail addresses replaced), a retry right after an error, a next page left unread, an empty
    result; no task id and no argument value in the table; a dev attempt is never read."""
    from ouroloop.benchmark import TaskResult
    from ouroloop.research.api_errors import api_error_table
    no_account = json.dumps({"error": {"code": 401, "message": "No quickbooks account is connected (id 7781)"}})
    _transcript(tmp_path, "s1", [
        ("api_fetch", {"method": "GET", "url": "https://books.example.com/v3/company/42/query", "params": "{}"},
         no_account, True),
        ("api_fetch", {"method": "GET", "url": "https://books.example.com/v3/company/42/query"}, no_account, True),
        ("api_fetch", {"method": "GET", "url": "https://crm.example.com/contacts"},
         json.dumps({"results": [{"name": "Jane Roe"}], "has_more": True}), False),
        ("api_search", {"query": "contacts Jane Roe"}, json.dumps({"results": []}), False)])
    _transcript(tmp_path, "s2", [
        ("api_fetch", {"method": "GET", "url": "https://books.example.com/v3/company/77/query"},
         json.dumps({"error": {"code": 404, "message": "No row 'Acme Ltd' for jane@acme.com"}}), True),
        ("api_fetch", {"method": "get", "url": "https://crm.example.com/contacts"},
         json.dumps({"results": [], "total": 0}), False)])
    _transcript(tmp_path, "s3", [("api_fetch", {"method": "GET", "url": "https://books.example.com/v3/x"}, "{}", False)])
    rows = [TaskResult("d.a", "a", "train", 0, "W0", "r", "m", success=False, evidence="s1"),
            TaskResult("d.b", "b", "train", 0, "W0", "r", "m", success=True, partial_credit=1.0, evidence="s2"),
            TaskResult("d.c", "c", "dev", 0, "W0", "r", "m", success=False, evidence="s3")]
    table = api_error_table(rows, tmp_path / "transcripts")
    assert (table["split"], table["attempts"], table["tasks"], table["failed_attempts"]) == ("train", 2, 2, 1)
    by = {e["endpoint"]: e for e in table["endpoints"]}
    query = by["GET books.example.com/v3/company/{id}/query"]
    assert (query["calls"], query["tasks"], query["errors"], query["retries"], query["calls_in_failed_attempts"]) == \
        (3, 2, 3, 1, 2)
    assert query["error_kinds"] == {"401 No quickbooks account is connected (id #)": 2,
                                    "404 No row '…' for <email>": 1}
    contacts = by["GET crm.example.com/contacts"]
    assert (contacts["unread_pages"], contacts["empty_results"], contacts["tasks"]) == (1, 1, 2)
    assert by["api_search"]["empty_results"] == 1
    text = json.dumps(table)
    assert "d.a" not in text and "Jane Roe" not in text and "jane@acme.com" not in text and "Acme" not in text
    assert "books.example.com/v3/x" not in text                      # the dev attempt was not read


def test_a_note_s_direction_is_read_normalised_and_counted_in_the_coverage(fake_upstream, tmp_path):
    """V0_4_PLAN §1.4 (M4 §1 v0.4): a note may say which way the agent went wrong — omission, over_action or other,
    any spelling of the three, anything else "other" — and the coverage counts them; a note that says nothing has no
    direction key (an older R0's notes are what they were), and a coverage with no stated direction has no counts.
    The session's R0 asks for it; the v1 R0 and NOTE_FORMAT do not change."""
    from ouroloop.research.basic import NOTE_FORMAT
    from ouroloop.research.diagnosis import direction_counts, note_direction
    assert [note_direction(v) for v in ("Omission", "over-action", "over action", "OVER_ACTION", "both", "", None)] == \
        ["omission", "over_action", "over_action", "over_action", "other", "", ""]
    assert direction_counts([{"direction": "omission"}, {"direction": "over_action"}, {"direction": "omission"},
                             {}]) == {"omission": 2, "over_action": 1, "other": 0, "unstated": 1}
    assert direction_counts([{}, {"failure_class": "x"}]) == {}
    assert '"direction"' in P.DIAGNOSE_SESSION and "over_action" in P.DIAGNOSE_SESSION
    assert "direction" not in P.DIAGNOSE_NOTE and "direction" not in NOTE_FORMAT
    from ouroloop.research import diagnosis as dg
    results, rows, _ = run(fake_upstream, tmp_path, [rsi.TRAIN[1], rsi.TRAIN[2]], "train",
                           worker=rsi.BusinessWorker(ignore_finish_check=True))
    failed = [r for r in rows if not r.success]
    directions = iter(["Over-Action", ""])

    class Noter:
        requests: list = []

        def note_request(self, summary, prompt, *, allowed=()):
            return prompt, json.dumps({"failure": summary["task_id"]})

        def note(self, system, user):
            return {"failure_class": "x", "root_cause": "y", "mechanism": "z", "component": "memory",
                    "generality": "g", "direction": next(directions)}
    summaries = dg.failure_summaries(rows, store=results, transcripts=tmp_path / "transcripts",
                                     limits=dg.DiagnosisLimits())
    assert len(summaries) == len(failed) == 2
    diagnosis = dg.Diagnosis(summaries=summaries, order=list(rsi.TRAIN), components=("memory",),
                             cache=dg.NoteCache(tmp_path, ct.Objects(tmp_path)))
    notes, coverage = diagnosis.run(Noter(), P.DIAGNOSE_SESSION)
    assert len(notes) == 2 and [n.get("direction") for n in notes] == ["over_action", None]
    assert coverage["by_direction"] == {"omission": 0, "over_action": 1, "other": 0, "unstated": 1}


def test_a_url_urlsplit_refuses_is_counted_under_its_tool(tmp_path):
    """V0_4_PLAN §1.2 (review 2026-10-06): a call whose URL urlsplit refuses — a bracketed placeholder host the agent
    copied from the catalogue — neither breaks the table nor invents an endpoint: it is counted under its tool."""
    from ouroloop.benchmark import TaskResult
    from ouroloop.research.api_errors import api_error_table
    _transcript(tmp_path, "s1", [
        ("api_fetch", {"method": "GET", "url": "https://[your-domain].zendesk.com/api/v2/tickets"},
         json.dumps({"error": {"code": 404, "message": "No handler"}}), True)])
    table = api_error_table([TaskResult("d.a", "a", "train", 0, "W0", "r", "m", evidence="s1")], tmp_path / "transcripts")
    assert [(e["endpoint"], e["calls"], e["errors"]) for e in table["endpoints"]] == [("api_fetch", 1, 1)]
