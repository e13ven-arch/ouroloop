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
