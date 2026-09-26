"""The benchmark run contract: task groups that stay put, and a summary that does not invent numbers."""
import json

import pytest

from ouroloop.benchmark import (Budget, Frozen, Results, RunManifest, SplitManifest, TaskEntry, TaskResult,
                                assign_splits, summarise)


def entries(n, families=None):
    return [TaskEntry(f"d.t{i}", (families or {}).get(f"d.t{i}", f"d.t{i}"), i, f"contract{i}") for i in range(n)]


def manifest(n=12, families=None, ratios=None, seed=3):
    es = entries(n, families)
    ratios = ratios or {"train": 0.5, "dev": 0.25, "test": 0.25}
    return SplitManifest("d", {"commit": "abc"}, "task_name", es,
                         assign_splits((e.family_id for e in es), ratios, seed), ratios, seed)


class TestSplits:
    def test_a_family_is_never_split(self):
        fams = {f"d.t{i}": f"d.f{i % 3}" for i in range(12)}
        m = manifest(12, fams)
        by_family = {}
        for e in m.entries:
            by_family.setdefault(e.family_id, set()).add(m.split_of(e.task_id))
        assert all(len(splits) == 1 for splits in by_family.values())
        assert len(m.splits) == 3

    def test_assignment_does_not_move_when_a_family_is_added(self):
        before = assign_splits([f"f{i}" for i in range(20)], {"train": 0.6, "dev": 0.4}, 1)
        after = assign_splits([f"f{i}" for i in range(21)], {"train": 0.6, "dev": 0.4}, 1)
        assert all(after[k] == v for k, v in before.items())

    def test_assignment_ignores_the_order_families_are_listed_in(self):
        fams = [f"f{i}" for i in range(30)]
        assert assign_splits(fams, {"train": 0.5, "test": 0.5}, 2) == \
               assign_splits(list(reversed(fams)), {"train": 0.5, "test": 0.5}, 2)

    def test_a_seed_changes_the_assignment(self):
        assert manifest(seed=3).splits != manifest(seed=4).splits

    def test_ratios_must_name_splits(self):
        with pytest.raises(ValueError, match="split ratios"):
            assign_splits(["f"], {"training": 1.0})
        with pytest.raises(ValueError, match="non-negative"):
            assign_splits(["f"], {"train": 0.0, "dev": 0.0})

    def test_ids_takes_no_default_split(self):
        m = manifest()
        assert sorted(m.ids("train", "dev", "test")) == sorted(e.task_id for e in m.entries)
        assert set(m.ids("dev")).isdisjoint(m.ids("test"))
        with pytest.raises(ValueError, match="unknown splits"):
            m.ids("holdout")
        with pytest.raises(ValueError, match="name the splits"):
            m.ids()   # not "everything", and not nothing: a caller says which split it is reading

    def test_counts_report_what_the_split_actually_gave(self):
        counts = manifest().counts()
        assert sum(c["tasks"] for c in counts.values()) == 12
        assert all(c["families"] == c["tasks"] for c in counts.values())   # one task per family here

    def test_roundtrip_and_a_hash_that_covers_the_contents(self):
        m = manifest()
        assert SplitManifest.from_json(m.to_json()).hash() == m.hash()
        broken = m.to_json()
        broken["splits"] = {k: "train" for k in broken["splits"]}
        with pytest.raises(ValueError, match="does not hash"):
            SplitManifest.from_json(broken)

    def test_the_hash_ignores_when_it_was_frozen(self):
        a, b = manifest(), manifest()
        b.created = "2026-01-01T00:00:00.000Z"
        assert a.hash() == b.hash()

    def test_a_task_in_no_family_is_refused(self):
        es = entries(3)
        with pytest.raises(ValueError, match="no split"):
            SplitManifest("d", {}, "task_name", es, {"d.t0": "train"})

    def test_duplicate_task_ids_are_refused(self):
        es = entries(2) + [TaskEntry("d.t0", "d.t0", 0, "other")]
        with pytest.raises(ValueError, match="duplicate task ids"):
            SplitManifest("d", {}, "task_name", es, {e.family_id: "train" for e in es})


class TestFreezing:
    def test_freezing_the_same_split_again_is_not_a_change(self, tmp_path):
        m = manifest()
        m.freeze(tmp_path / "s.json")
        m.freeze(tmp_path / "s.json")
        assert SplitManifest.load(tmp_path / "s.json").hash() == m.hash()

    def test_another_split_over_a_frozen_one_is_refused(self, tmp_path):
        manifest(seed=3).freeze(tmp_path / "s.json")
        with pytest.raises(Frozen, match="new experiment"):
            manifest(seed=9).freeze(tmp_path / "s.json")

    def test_drift_reports_added_removed_and_changed_tasks(self):
        m = manifest(4)
        now = [TaskEntry("d.t0", "d.t0", 0, "contract0"),
               TaskEntry("d.t1", "d.t1", 1, "CHANGED"),
               TaskEntry("d.t9", "d.t9", 9, "contract9")]
        drift = m.drift(now)
        assert any("d.t1: contract" in d for d in drift)
        assert any("d.t2: gone from upstream" in d for d in drift)
        assert any("d.t9: new upstream task" in d for d in drift)


class TestResult:
    def test_success_needs_every_scored_assertion(self):
        with pytest.raises(ValueError, match="every scored assertion"):
            TaskResult("t", "f", "dev", 0, "base", "r", "m", success=True, partial_credit=0.5)

    def test_an_infrastructure_failure_cannot_be_a_valid_attempt(self):
        with pytest.raises(ValueError, match="not a task outcome"):
            TaskResult("t", "f", "dev", 0, "base", "r", "m", termination="infrastructure")

    def test_an_invalid_attempt_needs_a_reason(self):
        with pytest.raises(ValueError, match="needs a reason"):
            TaskResult("t", "f", "dev", 0, "base", "r", "m", validity="invalid", termination="harness_error")

    def test_broken_records_the_reason_and_keeps_the_task(self):
        run = RunManifest("base", "base", "dev", {"model": "m"})
        r = TaskResult.broken("t", "f", "dev", 0, run, "m1", "env: would not start")
        assert (r.validity, r.success, r.partial_credit) == ("invalid", False, 0.0)
        assert r.run == run.hash() and r.invalid_reason.startswith("env")


class TestSummary:
    def result(self, task, success=False, partial=0.0, cost=None, attempt=0, **kw):
        return TaskResult(task, task, "dev", attempt, "base", "r", "m", success=success, partial_credit=partial,
                          cost_usd=cost, **kw)

    def test_repeats_of_one_task_count_once(self):
        rows = [self.result("a", True, 1.0, attempt=0), self.result("a", False, 0.5, attempt=1),
                self.result("b", True, 1.0)]
        s = summarise(rows)
        assert s["tasks"] == 2 and s["attempts"] == 3
        assert s["pass_rate"] == 0.75          # task a passed half its attempts, task b all of them
        assert s["passed"] == 1                # only b passed every attempt

    def test_missing_cost_is_missing_not_zero(self):
        s = summarise([self.result("a", cost=0.2), self.result("b")])
        assert s["cost_usd"] == 0.2 and s["cost_missing"] == 1

    def test_no_cost_at_all_reports_none(self):
        assert summarise([self.result("a")])["cost_usd"] is None

    def test_invalid_attempts_are_not_scored(self):
        run = RunManifest("base", "base", "dev", {})
        rows = [self.result("a", True, 1.0), TaskResult.broken("b", "b", "dev", 0, run, "m", "env: gone")]
        s = summarise(rows)
        assert s["tasks"] == 1 and s["invalid"] == 1 and s["pass_rate"] == 1.0
        assert s["invalid_reasons"] == ["env"]

    def test_nothing_valid_reports_no_rate(self):
        run = RunManifest("base", "base", "dev", {})
        s = summarise([TaskResult.broken("a", "a", "dev", 0, run, "m", "env: gone")])
        assert s["pass_rate"] is None and s["partial_credit"] is None


class TestRunIdentity:
    def test_the_hash_covers_the_model_and_the_budget_but_not_a_note(self):
        a = RunManifest("base", "base", "dev", {"model": "x", "effort": "high"}, budget=Budget(max_steps=50))
        assert RunManifest("base", "base", "dev", {"model": "x", "effort": "high"},
                           budget=Budget(max_steps=50), notes="second try").hash() == a.hash()
        assert RunManifest("base", "base", "dev", {"model": "x", "effort": "low"},
                           budget=Budget(max_steps=50)).hash() != a.hash()
        assert RunManifest("base", "base", "dev", {"model": "x", "effort": "high"},
                           budget=Budget(max_steps=25)).hash() != a.hash()

    def test_roundtrip(self):
        run = RunManifest("v1", "evolved", "test", {"model": "x"}, budget=Budget(max_usd=1.5), data="d1")
        assert RunManifest.from_json(json.loads(json.dumps(run.to_json()))).hash() == run.hash()


class TestResults:
    def test_attempts_and_the_run_are_written_where_they_can_be_read_back(self, tmp_path):
        run = RunManifest("base", "base", "dev", {"model": "x"})
        res = Results(tmp_path, "marketing")
        res.put_run(run)
        res.append(TaskResult("t", "f", "dev", 0, "base", run.hash(), "m", success=True, partial_credit=1.0))
        assert [r.task_id for r in res.of_run(run.hash())] == ["t"]
        assert json.loads((tmp_path / "bench" / "marketing" / "runs" / f"{run.hash()}.json")
                          .read_text())["candidate"] == "base"


class TestBudget:
    def test_each_limit_and_an_unmeasured_cost(self):
        b = Budget(max_steps=2, max_tool_calls=3, max_seconds=10, max_usd=1.0)
        assert not b.exceeded(steps=2, tool_calls=3, seconds=10, usd=1.0)
        assert "steps 3" in b.exceeded(steps=3)
        assert "tool calls 4" in b.exceeded(tool_calls=4)
        assert "seconds 11" in b.exceeded(seconds=11)
        assert "cost" in b.exceeded(usd=2.0)
        assert not b.exceeded(usd=None)        # a cost nobody measured stops nothing
