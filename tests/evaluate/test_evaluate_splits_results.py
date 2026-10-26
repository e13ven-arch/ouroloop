"""Splits that stay put, run identity, the validity of an attempt, results on disk and a summary that invents nothing
(docs/design/EVALUATE.md §2-§4)."""
import json
from dataclasses import asdict

import pytest

from reharness.evaluate.limits import Budget
from reharness.evaluate.results import (Results, RunManifest, TaskResult, attempt_id, pair_key, summarise)
from reharness.evaluate.splits import Frozen, SplitManifest, TaskEntry, assign_splits, stratified_splits

RATIOS = {"train": 0.5, "dev": 0.25, "test": 0.25}


def entries(n, families=None):
    return [TaskEntry(f"d.t{i}", (families or {}).get(f"d.t{i}", f"d.t{i}"), i, f"contract{i}") for i in range(n)]


def manifest(n=12, families=None, ratios=None, seed=3):
    es = entries(n, families)
    ratios = ratios or RATIOS
    return SplitManifest("d", {"commit": "abc"}, "task_name", es,
                         assign_splits((e.family_id for e in es), ratios, seed), ratios, seed)


# ---------------------------------------------------------------------------- splits
class TestSplits:
    def test_a_family_is_never_split(self):
        fams = {f"d.t{i}": f"d.f{i % 3}" for i in range(12)}
        m = manifest(12, fams)
        by_family = {}
        for e in m.entries:
            by_family.setdefault(e.family_id, set()).add(m.split_of(e.task_id))
        assert all(len(splits) == 1 for splits in by_family.values()) and len(m.splits) == 3
        assert m.family_of("d.t4") == "d.f1" and m.entry("d.t4").contract == "contract4"

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
        for split in (assign_splits, lambda fams, ratios: stratified_splits({f: "s" for f in fams}, ratios)):
            with pytest.raises(ValueError, match="split ratios"):
                split(["f"], {"training": 1.0})
            with pytest.raises(ValueError, match="non-negative"):
                split(["f"], {"train": 0.0, "dev": 0.0})

    def test_ids_takes_no_default_split(self):
        m = manifest()
        assert sorted(m.ids("train", "dev", "test")) == sorted(e.task_id for e in m.entries)
        assert set(m.ids("dev")).isdisjoint(m.ids("test"))
        with pytest.raises(ValueError, match="unknown splits"):
            m.ids("holdout")
        with pytest.raises(ValueError, match="name the splits"):
            m.ids()      # not "everything", and not nothing: a caller says which split it is reading

    def test_counts_report_what_the_split_actually_gave(self):
        fams = {f"d.t{i}": f"d.f{i % 4}" for i in range(12)}
        m = manifest(12, fams)
        counts = m.counts()
        assert sum(c["tasks"] for c in counts.values()) == 12 and sum(c["families"] for c in counts.values()) == 4
        for split, c in counts.items():
            assert c["tasks"] == len(m.ids(split)) and c["tasks"] == 3 * c["families"]

    def test_a_task_in_no_family_and_a_duplicate_task_are_refused(self):
        with pytest.raises(ValueError, match="no split"):
            SplitManifest("d", {}, "task_name", entries(3), {"d.t0": "train"})
        es = entries(2) + [TaskEntry("d.t0", "d.t0", 0, "other")]
        with pytest.raises(ValueError, match="duplicate task ids"):
            SplitManifest("d", {}, "task_name", es, {e.family_id: "train" for e in es})
        with pytest.raises(ValueError, match="unknown splits"):
            SplitManifest("d", {}, "task_name", entries(1), {"d.t0": "holdout"})
        with pytest.raises(ValueError, match="split rule"):
            SplitManifest("d", {}, "task_name", entries(1), {"d.t0": "train"}, split_rule="shuffled")

    def test_an_adapters_plain_entry_reads_as_a_task_entry(self):
        class Entry:
            task_id, family_id, contract = "d.t0", "d.f0", "c0"
        assert TaskEntry.of({"task_id": "d.t0", "family_id": "d.f0", "contract": "c0", "tools": ["a"]}) \
            == TaskEntry("d.t0", "d.f0", None, "c0", ("a",))
        assert TaskEntry.of(Entry()) == TaskEntry("d.t0", "d.f0", None, "c0")


class TestStratified:
    def test_a_stratified_split_gives_the_domain_the_ratios_and_each_stratum_its_share(self):
        """The stratified rule gives the whole domain exactly the ratios' counts and each stratum its share to within
        the rounding of one sequence, where the hash rule only gets the intended proportions on average."""
        sizes = {"a|office": 41, "b|office": 32, "c|app": 15, "d|app": 12}      # Marketing's real stratum sizes
        strata = {f"{k}#{i}": k for k, n in sizes.items() for i in range(n)}
        splits, per = stratified_splits(strata, RATIOS, seed=1)
        counts = {s: sum(1 for v in splits.values() if v == s) for s in ("train", "dev", "test")}
        assert counts == {"train": 50, "dev": 25, "test": 25}
        for key, n in sizes.items():
            assert sum(per[key].values()) == n
            assert all(abs(per[key][s] - n * r) < 2 for s, r in RATIOS.items()), (key, per[key])
            assert {splits[f] for f, k in strata.items() if k == key} == {s for s, c in per[key].items() if c}
        hashed = assign_splits(list(strata), RATIOS, seed=1)
        assert {s: sum(1 for v in hashed.values() if v == s) for s in RATIOS} != counts     # what stratifying fixes

    def test_a_stratified_split_is_deterministic_and_ignores_the_listed_order(self):
        strata = {f"f{i}": f"s{i % 3}" for i in range(30)}
        splits, _ = stratified_splits(strata, RATIOS, seed=1)
        assert stratified_splits(dict(reversed(list(strata.items()))), RATIOS, seed=1)[0] == splits
        assert stratified_splits(strata, RATIOS, seed=2)[0] != splits

    def test_both_rules_still_give_the_assignment_frozen_splits_were_made_with(self):
        """A pinned split ([bench] frozen_splits) must be reproduced by later code, so the assignment of a fixed input
        under a fixed seed may not move: these digests were taken from the rules as they stood at stage 1."""
        from reharness.evaluate.splits import digest
        strata = {f"f{i}": f"s{i % 3}" for i in range(30)}
        assert digest(stratified_splits(strata, RATIOS, seed=1))[:16] == "590534a695735928"
        assert digest(assign_splits(list(strata), RATIOS, seed=1))[:16] == "d9a1a274f38a93dd"

    def test_strata_are_part_of_a_stratified_manifests_identity_and_absent_from_a_hash_rule_one(self):
        es = entries(8)
        strata = {e.family_id: "s" if int(e.task_id[-1]) % 2 else "t" for e in es}
        splits, per = stratified_splits(strata, RATIOS, seed=1)
        stratified = SplitManifest("d", {}, "task_name", es, splits, RATIOS, 1, split_rule="stratified", strata=per)
        plain = SplitManifest("d", {}, "task_name", es, splits, RATIOS, 1)
        assert "strata" not in plain.payload() and "split_rule" not in plain.payload()
        assert stratified.hash() != plain.hash()
        assert SplitManifest.from_json(json.loads(json.dumps(stratified.to_json()))).hash() == stratified.hash()


class TestFreezing:
    def test_freezing_the_same_split_again_is_not_a_change(self, tmp_path):
        m = manifest()
        m.freeze(tmp_path / "s.json")
        m.created = "2026-01-01T00:00:00.000Z"          # the hash ignores when it was frozen
        m.freeze(tmp_path / "s.json")
        assert SplitManifest.load(tmp_path / "s.json").hash() == m.hash() == manifest().hash()

    def test_another_split_over_a_frozen_one_is_refused(self, tmp_path):
        manifest(seed=3).freeze(tmp_path / "s.json")
        before = (tmp_path / "s.json").read_bytes()
        with pytest.raises(Frozen, match="new experiment"):
            manifest(seed=9).freeze(tmp_path / "s.json")
        assert (tmp_path / "s.json").read_bytes() == before

    def test_the_hash_covers_the_contents_and_a_load_checks_it(self, tmp_path):
        m = manifest()
        assert SplitManifest.from_json(m.to_json()).hash() == m.hash()
        broken = m.to_json()
        broken["splits"] = {k: "train" for k in broken["splits"]}
        with pytest.raises(ValueError, match="does not hash"):
            SplitManifest.from_json(broken)
        with pytest.raises(ValueError, match="schema"):
            SplitManifest.from_json({**m.to_json(), "schema": "other"})
        changed = manifest()
        changed.entries[0] = TaskEntry("d.t0", "d.t0", 0, "CHANGED")
        assert changed.hash() != m.hash()

    def test_drift_reports_added_removed_and_changed_tasks(self):
        m = manifest(4)
        now = [TaskEntry("d.t0", "d.t0", 0, "contract0"), TaskEntry("d.t1", "d.t1", 1, "CHANGED"),
               {"task_id": "d.t3", "family_id": "d.t3", "contract": "contract3"},
               TaskEntry("d.t9", "d.t9", 9, "contract9")]
        assert m.drift(now) == ["d.t2: gone from upstream", "d.t9: new upstream task, not in the frozen manifest",
                                "d.t1: contract contract1 -> CHANGED"]
        assert m.drift(m.entries) == []


# ---------------------------------------------------------------------------- one attempt
class TestResult:
    def test_success_needs_every_scored_assertion(self):
        with pytest.raises(ValueError, match="every scored assertion"):
            TaskResult("t", "f", "dev", 0, "base", "r", "m", success=True, partial_credit=0.5)

    def test_an_infrastructure_failure_cannot_be_a_valid_attempt(self):
        with pytest.raises(ValueError, match="not a task outcome"):
            TaskResult("t", "f", "dev", 0, "base", "r", "m", termination="infrastructure")

    def test_a_package_defect_is_a_valid_task_outcome(self):
        """harness_error is the package's own fault, scored as the world stands: evidence about the candidate, not a
        broken run to refill (EVALUATE §4)."""
        for termination in ("finished", "budget", "tool_refused", "harness_error", "step_limit"):
            assert TaskResult("t", "f", "dev", 0, "base", "r", "m", termination=termination).validity == "valid"
        rows = [TaskResult("a", "a", "dev", 0, "base", "r", "m", termination="harness_error"),
                TaskResult("b", "b", "dev", 0, "base", "r", "m", success=True, partial_credit=1.0)]
        s = summarise(rows)
        assert s["tasks"] == 2 and s["invalid"] == 0 and s["pass_rate"] == 0.5
        assert s["terminations"] == {"finished": 1, "harness_error": 1}

    def test_an_invalid_attempt_needs_a_reason_and_a_known_shape(self):
        with pytest.raises(ValueError, match="needs a reason"):
            TaskResult("t", "f", "dev", 0, "base", "r", "m", validity="invalid", termination="harness_error")
        with pytest.raises(ValueError, match="termination"):
            TaskResult("t", "f", "dev", 0, "base", "r", "m", termination="crashed")
        with pytest.raises(ValueError, match="valid or invalid"):
            TaskResult("t", "f", "dev", 0, "base", "r", "m", validity="maybe")

    def test_a_result_records_which_limit_ended_it(self):
        r = TaskResult("t", "f", "dev", 0, "base", "r", "m", termination="budget", limit="tokens")
        assert TaskResult.from_json(json.loads(json.dumps(r.to_json()))).limit == "tokens"
        assert TaskResult.from_json({k: v for k, v in r.to_json().items() if k != "limit"}).limit is None
        with pytest.raises(ValueError, match="only on a budget"):
            TaskResult("t", "f", "dev", 0, "base", "r", "m", termination="finished", limit="tokens")
        with pytest.raises(ValueError, match="one of"):
            TaskResult("t", "f", "dev", 0, "base", "r", "m", termination="budget", limit="steps")

    def test_broken_records_the_reason_and_keeps_the_task(self):
        run = RunManifest("base", "base", "dev", {"model": "m"})
        r = TaskResult.broken("t", "f", "dev", 0, run, "m1", "env: would not start")
        assert (r.validity, r.termination, r.success, r.partial_credit) == ("invalid", "infrastructure", False, 0.0)
        assert r.run == run.hash() and r.invalid_reason.startswith("env")

    def test_an_old_record_still_reads(self):
        old = {"task_id": "t1", "family_id": "f1", "split": "dev", "attempt": 0, "candidate": "base", "run": "r",
               "manifest": "m", "success": False, "partial_credit": 0.5, "termination": "finished",
               "ts": "2026-05-01T00:00:00Z"}
        res = TaskResult.from_json(old)
        assert res.attempt_id == "" and res.pair_key == "" and res.observation_refs == [] and res.validity == "valid"

    def test_the_two_arms_of_a_pair_share_a_pair_key_and_never_an_attempt_id(self):
        plan = "plan:sha256:" + "a" * 64
        assert pair_key(plan, "t", 0) != pair_key(plan, "t", 1) != pair_key(plan, "u", 1)
        ids = {attempt_id("run", "t", repeat, arm, retry)
               for repeat in (0, 1) for arm in ("subject", "control") for retry in (0, 1)}
        assert len(ids) == 8


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
        assert s["partial_credit"] == 0.875

    def test_missing_cost_is_missing_not_zero(self):
        s = summarise([self.result("a", cost=0.2), self.result("b")])
        assert s["cost_usd"] == 0.2 and s["cost_missing"] == 1 and s["cost_per_attempt"] == 0.2
        none = summarise([self.result("a")])
        assert none["cost_usd"] is None and none["cost_per_attempt"] is None

    def test_invalid_attempts_are_not_scored(self):
        run = RunManifest("base", "base", "dev", {})
        rows = [self.result("a", True, 1.0), TaskResult.broken("b", "b", "dev", 0, run, "m", "env: gone")]
        s = summarise(rows)
        assert s["tasks"] == 1 and s["invalid"] == 1 and s["pass_rate"] == 1.0
        assert s["invalid_reasons"] == ["env"]
        nothing = summarise([TaskResult.broken("a", "a", "dev", 0, run, "m", "env: gone")])
        assert nothing["pass_rate"] is None and nothing["partial_credit"] is None

    def test_usage_is_summed_over_valid_attempts(self):
        rows = [self.result("a", usage={"input_tokens": 10, "output_tokens": 2}),
                self.result("b", usage={"input_tokens": 5, "cache_read_tokens": 7})]
        assert summarise(rows)["usage"] == {"cache_read_tokens": 7, "input_tokens": 15, "output_tokens": 2}


# ---------------------------------------------------------------------------- run identity
class TestRunIdentity:
    def test_the_hash_covers_the_model_and_the_limits_but_not_a_note(self):
        a = RunManifest("base", "base", "dev", {"model": "x", "effort": "high"}, budget=Budget(max_steps=50))
        assert RunManifest("base", "base", "dev", {"model": "x", "effort": "high"},
                           budget=Budget(max_steps=50), notes="second try").hash() == a.hash()
        assert RunManifest("base", "base", "dev", {"model": "x", "effort": "low"},
                           budget=Budget(max_steps=50)).hash() != a.hash()
        assert RunManifest("base", "base", "dev", {"model": "x", "effort": "high"},
                           budget=Budget(max_steps=25)).hash() != a.hash()

    def test_unset_identity_fields_stay_out_of_the_hash_and_set_ones_enter_it(self):
        base = RunManifest("base", "base", "dev", {"model": "x"})
        assert not {"core_ref", "package_ref", "mode", "system_release_ref"} & set(base.payload())
        package = RunManifest("base", "base", "dev", {"model": "x"}, package_ref="package:sha256:" + "a" * 64)
        core = RunManifest("base", "base", "dev", {"model": "x"}, core_ref="core:sha256:" + "b" * 64)
        assert len({base.hash(), package.hash(), core.hash()}) == 3
        assert not base.v2() and RunManifest("base", "base", "dev", {}, system_release_ref="r", worker_ref="w",
                                             research_ref="q", core_ref="c").v2()

    def test_an_old_manifest_keeps_its_hash(self):
        old = {"candidate": "base", "role": "base", "split": "dev", "model": {"provider": "p", "model": "m"},
               "prompt_hash": "abc", "decisions": {}, "model_ref": None, "policy": "default",
               "environment": {"upstream": "c"}, "budget": {"max_steps": 10, "max_tool_calls": 20,
                                                            "max_seconds": 30.0, "max_usd": None},
               "data": "d", "repeats": 1}
        from reharness.evaluate.splits import digest
        run = RunManifest.from_json(old)
        assert run.hash() == digest(old)[:16]          # the bytes it was recorded under
        assert RunManifest.from_json(json.loads(json.dumps(run.to_json()))).hash() == run.hash()

    def test_from_json_refuses_a_key_it_does_not_know(self, tmp_path):
        run = RunManifest("v1", "evolved", "test", {"model": "x"}, budget=Budget(max_usd=1.5), data="d1")
        assert RunManifest.from_json(json.loads(json.dumps(run.to_json()))).hash() == run.hash()
        with pytest.raises(ValueError, match="does not take"):
            RunManifest.from_json({**run.to_json(), "invented": 1})
        # a manifest on disk that this reader cannot parse is left out, not read as something it is not
        results = Results(tmp_path, "rsi")
        results.put_run(run)
        (results.root / "runs" / "bad.json").write_text(json.dumps({**run.to_json(), "invented": 1}))
        assert list(results.runs()) == [run.hash()]


class TestHardLimits:
    def test_each_limit_and_an_unmeasured_cost(self):
        b = Budget(max_steps=2, max_tool_calls=3, max_seconds=10, max_usd=1.0)
        assert not b.exceeded(steps=2, tool_calls=3, seconds=10, usd=1.0)
        assert "steps 3" in b.exceeded(steps=3) and "tool calls 4" in b.exceeded(tool_calls=4)
        assert "seconds 11" in b.exceeded(seconds=11) and "cost" in b.exceeded(usd=2.0)
        assert not b.exceeded(usd=None)        # a cost nobody measured stops nothing

    def test_the_five_dimensions_and_none_means_no_limit(self):
        b = Budget(max_steps=2, max_tool_calls=3, max_seconds=10, max_model_calls=5, max_tokens=100)
        assert Budget(**json.loads(json.dumps(asdict(b)))) == b
        assert b.limits() == {"max_steps": 2, "max_model_calls": 5, "max_tool_calls": 3, "max_seconds": 10,
                              "max_tokens": 100}
        assert b.limit_hit(model_calls=5, tokens=100) is None
        assert b.limit_hit(model_calls=6) == "model_calls" and b.limit_hit(tokens=101) == "tokens"
        assert b.limit_hit(steps=3, tokens=101) == "act_calls" and b.limit_hit(seconds=11) == "seconds"
        assert b.limit_hit(tool_calls=4) == "tool_calls"
        old = Budget(max_steps=2)
        assert old.limit_hit(model_calls=10 ** 6, tokens=10 ** 9) is None
        assert set(old.limits()) == {"max_steps", "max_tool_calls", "max_seconds"}
        unset = RunManifest("base", "base", "dev", {"model": "x"}, budget=old)
        assert not set(unset.payload()["budget"]) & {"max_model_calls", "max_tokens"}
        limited = RunManifest("base", "base", "dev", {"model": "x"}, budget=b)
        assert limited.payload()["budget"]["max_tokens"] == 100 and limited.hash() != unset.hash()

    def test_ks_limits_section_round_trips(self):
        b = Budget(max_steps=7, max_tool_calls=8, max_seconds=9.0, max_model_calls=10, max_tokens=11)
        core = b.to_core(max_output_chars=500)
        assert core == {"max_steps": 7, "max_model_calls": 10, "max_tool_calls": 8, "max_seconds": 9.0,
                        "max_tokens": 11, "max_output_chars": 500}
        assert Budget.from_core(core) == b


# ---------------------------------------------------------------------------- results on disk
class TestResults:
    def test_attempts_and_the_run_are_written_where_they_can_be_read_back(self, tmp_path):
        run = RunManifest("base", "base", "dev", {"model": "x"})
        res = Results(tmp_path, "rsi")
        res.put_run(run)
        res.append(TaskResult("t", "f", "dev", 0, "base", run.hash(), "m", success=True, partial_credit=1.0))
        res.append(TaskResult("u", "f", "dev", 0, "base", "other", "m"))
        assert [r.task_id for r in res.of_run(run.hash())] == ["t"]
        assert [r.task_id for r in Results(tmp_path, "rsi").all()] == ["t", "u"]
        assert res.runs()[run.hash()].hash() == run.hash()

    def test_results_are_redacted_on_the_way_to_disk(self, tmp_path):
        res = Results(tmp_path, "rsi")
        res.append(TaskResult("t", "f", "dev", 0, "base", "r", "m", stop="api_key=sk-" + "a" * 24))
        assert "sk-" + "a" * 24 not in res.path.read_text() and "[REDACTED]" in next(res.all()).stop

    def test_grading_is_read_for_train_only_unless_a_split_is_named(self, tmp_path):
        """The per-assertion outcome is the grader's answer key: results.jsonl never carries it, and its reader hands
        out train rows by default; dev and test rows come back empty unless their split is named (EVALUATE §4)."""
        results = Results(tmp_path, "rsi")
        assertion = [{"type": "slack_message_contains", "params": {"text": "x"}, "passed": False, "secret": "kept out"}]
        for split in ("train", "dev", "test"):
            r = TaskResult(f"{split}.t", f"{split}.t", split, 0, "W0", "run", "m", attempt_id=f"run/{split}.t/r0#0")
            results.append(r)
            results.put_grading(r, assertion)
        assert "slack_message_contains" not in results.path.read_text()
        assert results.grading("run/train.t/r0#0") == [{"type": "slack_message_contains", "params": {"text": "x"},
                                                        "passed": False, "excluded": False}]
        assert results.grading("run/dev.t/r0#0") == [] and results.grading("run/test.t/r0#0") == []
        assert results.grading("run/dev.t/r0#0", splits=("dev",)) and results.grading("run/test.t/r0#0",
                                                                                      splits=("test",))
        assert results.grading("run/absent#0") == []
        before = results.grading_path().read_text()
        results.put_grading(TaskResult("x", "x", "train", 0, "W0", "run", "m"), assertion)    # no attempt id
        results.put_grading(TaskResult("x", "x", "train", 0, "W0", "run", "m", attempt_id="run/x/r0#0"), [])
        assert results.grading_path().read_text() == before

    def test_grading_rows_copied_from_another_workspace_are_copied_once(self, tmp_path):
        source, target = Results(tmp_path / "a", "rsi"), Results(tmp_path / "b", "rsi")
        r = TaskResult("t", "t", "train", 0, "W0", "run", "m", attempt_id="run/t/r0#0")
        source.put_grading(r, [{"type": "k", "params": {}, "passed": True}])
        assert target.copy_grading(source, ["run/t/r0#0", "run/none#0"]) == 1
        assert target.copy_grading(source, ["run/t/r0#0"]) == 0
        assert target.grading("run/t/r0#0")[0]["passed"] is True
