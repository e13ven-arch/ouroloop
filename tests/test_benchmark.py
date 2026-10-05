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


class TestHardLimits:
    def test_the_v03_dimensions_round_trip_and_none_means_no_limit(self):
        """docs/modules/00_CONTRACTS.md §2 (单次尝试硬上限), M5 §2: max_model_calls and max_tokens are limits when set and
        nothing when None, so a plan frozen before them reads as frozen, and a run's hash does not change."""
        from dataclasses import asdict
        b = Budget(max_steps=2, max_tool_calls=3, max_seconds=10, max_model_calls=5, max_tokens=100)
        assert Budget(**json.loads(json.dumps(asdict(b)))) == b
        assert b.limits() == {"max_steps": 2, "max_model_calls": 5, "max_tool_calls": 3, "max_seconds": 10,
                              "max_tokens": 100}
        assert not b.exceeded(model_calls=5, tokens=100) and b.limit_hit(model_calls=5, tokens=100) is None
        assert "model calls 6" in b.exceeded(model_calls=6) and "tokens 101" in b.exceeded(tokens=101)
        assert b.limit_hit(model_calls=6) == "model_calls" and b.limit_hit(tokens=101) == "tokens"
        assert b.limit_hit(steps=3, tokens=101) == "act_calls" and b.limit_hit(seconds=11) == "seconds"
        old = Budget(max_steps=2)
        assert old.max_model_calls is None and old.max_tokens is None
        assert not old.exceeded(model_calls=10 ** 6, tokens=10 ** 9) and old.limit_hit(tokens=10 ** 9) is None
        assert set(old.limits()) == {"max_steps", "max_tool_calls", "max_seconds"}
        unset = RunManifest("base", "base", "dev", {"model": "x"}, budget=old)
        assert not set(unset.payload()["budget"]) & {"max_model_calls", "max_tokens"}
        assert RunManifest.from_json(unset.to_json()).hash() == unset.hash()
        limited = RunManifest("base", "base", "dev", {"model": "x"}, budget=b)
        assert limited.payload()["budget"]["max_tokens"] == 100 and limited.hash() != unset.hash()

    def test_a_result_records_which_limit_ended_it_and_an_old_record_without_it_loads(self):
        """docs/modules/01_POLICY_RUNTIME.md §1 (硬上限): `limit` names the dimension of a budget termination, is one of
        the five, and is None on every other termination and on a record written before the field."""
        run = RunManifest("base", "base", "dev", {"model": "x"})
        r = TaskResult("t", "f", "dev", 0, "base", run.hash(), "m", termination="budget", limit="tokens")
        assert TaskResult.from_json(json.loads(json.dumps(r.to_json()))).limit == "tokens"
        assert TaskResult.from_json({k: v for k, v in r.to_json().items() if k != "limit"}).limit is None
        assert TaskResult("t", "f", "dev", 0, "base", run.hash(), "m").limit is None
        with pytest.raises(ValueError, match="only on a budget"):
            TaskResult("t", "f", "dev", 0, "base", run.hash(), "m", termination="finished", limit="tokens")
        with pytest.raises(ValueError, match="one of"):
            TaskResult("t", "f", "dev", 0, "base", run.hash(), "m", termination="budget", limit="steps")


# ---------------------------------------------------------------------------- v0.3 step 5 (M7 §2): several domains
def _domain_config(fake_upstream, **bench_table):
    from ouroloop import bench
    table = {"upstream_path": fake_upstream, "seed": 1, "ratios": {"train": 0.5, "dev": 0.25, "test": 0.25},
             **bench_table}
    return bench.BenchConfig.load({"bench": table})


def test_a_stratified_split_balances_assertion_mix_and_tool_family(tmp_path, fake_upstream):
    """BR6-19: the stratified rule gives the whole domain exactly the ratios' counts and each stratum its share to
    within the rounding of one sequence, where the hash rule only gets the intended proportions on average; a
    domain's strata are its scored-assertion kinds × its tool family, and the manifest records each stratum's
    counts; the same seed gives the same split, another seed another one."""
    from ouroloop import bench
    from ouroloop.adapters import automationbench as ab
    from ouroloop.benchmark import stratified_splits
    ratios = {"train": 0.5, "dev": 0.25, "test": 0.25}
    # Marketing's real stratum sizes (41/32/15/12 over 100 tasks): exact totals, every stratum within 2 of its share
    sizes = {"a|office": 41, "b|office": 32, "c|app": 15, "d|app": 12}
    strata = {f"{k}#{i}": k for k, n in sizes.items() for i in range(n)}
    splits, per = stratified_splits(strata, ratios, seed=1)
    counts = {s: sum(1 for v in splits.values() if v == s) for s in ("train", "dev", "test")}
    assert counts == {"train": 50, "dev": 25, "test": 25}
    for key, n in sizes.items():
        assert sum(per[key].values()) == n
        assert all(abs(per[key][s] - n * r) < 2 for s, r in ratios.items()), (key, per[key])
    hashed = assign_splits(list(strata), ratios, seed=1)
    assert {s: sum(1 for v in hashed.values() if v == s) for s in ratios} != counts   # what stratifying fixes
    # deterministic, and the listed order does not matter
    assert stratified_splits(dict(reversed(list(strata.items()))), ratios, seed=1)[0] == splits
    assert stratified_splits(strata, ratios, seed=2)[0] != splits
    # through the adapter: the fake `sales` domain's six strata of two tasks, the kinds of the assertions it scores
    # and whether its world has a business application beyond the office services
    suite = ab.load_suite("sales", fake_upstream)
    cfg = _domain_config(fake_upstream, domain="sales", split_rule="stratified")
    m, drift = bench.freeze_splits(suite, cfg, tmp_path / "sales.json")
    assert drift == [] and m.split_rule == "stratified" and m.family_rule == "task_name"
    assert set(m.strata) == {"missing_write|app", "content+missing_write|app", "forbidden_write+missing_write|app",
                             "count+missing_write|app", "content|office", "content+forbidden_write|office"}
    assert all(sum(c.values()) == 2 for c in m.strata.values())
    assert {s: c["tasks"] for s, c in m.counts().items()} == {"train": 6, "dev": 3, "test": 3}
    assert ab.stratum(suite, "sales.notify_quiet_0") == "content+forbidden_write|office"
    assert SplitManifest.load(tmp_path / "sales.json").hash() == m.hash()          # strata are in the identity
    again, _ = bench.freeze_splits(suite, cfg, tmp_path / "again.json")
    assert again.hash() == m.hash()


def test_a_stratified_split_reads_no_result(tmp_path, fake_upstream, monkeypatch):
    """BR6-19: the stratified split is made from the task definitions alone — no TaskResult, no grading row, no
    score: a workspace full of results and one with none freeze the same manifest, and a reader of results or a
    scorer that would raise if touched is never touched."""
    from automationbench import rubric
    from ouroloop import bench
    from ouroloop.adapters import automationbench as ab
    cfg = _domain_config(fake_upstream, domain="support", split_rule="stratified")
    clean, _ = bench.freeze_splits(ab.load_suite("support", fake_upstream), cfg, tmp_path / "clean" / "splits.json")
    busy = Results(tmp_path / "busy", "support")
    busy.path.parent.mkdir(parents=True, exist_ok=True)
    busy.path.write_text(json.dumps({"task_id": "support.update_0", "success": True}) + "\n")
    busy.grading_path().write_text(json.dumps({"attempt_id": "x", "split": "train", "assertions": []}) + "\n")

    def touched(*_a, **_k):
        raise AssertionError("the split read a result or scored a state")
    for target, name in ((Results, "all"), (Results, "grading"), (Results, "of_run"), (ab.Episode, "score"),
                         (rubric, "partial_credit"), (rubric, "task_completed_correctly")):
        monkeypatch.setattr(target, name, touched)
    frozen, _ = bench.freeze_splits(ab.load_suite("support", fake_upstream), cfg, busy.manifest_path())
    assert frozen.hash() == clean.hash() and frozen.splits == clean.splits and frozen.strata == clean.strata


def test_a_frozen_hash_split_is_kept_when_the_rule_changes(tmp_path, fake_upstream):
    """BR6-18, the Marketing branch: a split frozen under the hash rule stays as it is when the config later asks
    for the stratified rule (noted, not applied: a changed rule is a new workspace), its hash is v0.1/v0.2's — the
    new optional fields stay out of a hash-rule manifest — and a domain pinned by [bench.frozen_splits] is split by
    the hash rule whatever split_rule says and must reproduce the pinned hash, here and in a fresh workspace."""
    from ouroloop import bench
    from ouroloop.adapters import automationbench as ab
    suite = ab.load_suite("marketing", fake_upstream)
    path = tmp_path / "ws" / "splits.json"
    old, _ = bench.freeze_splits(suite, _domain_config(fake_upstream, domain="marketing"), path)
    assert old.split_rule is None and old.strata is None
    assert "split_rule" not in json.loads(path.read_text()) and "strata" not in json.loads(path.read_text())
    fams = {t: t for t in suite.ids()}
    by_hand = SplitManifest("marketing", old.upstream, "task_name", suite.entries(fams),
                            assign_splits(fams.values(), old.ratios, 1), old.ratios, 1)
    assert by_hand.hash() == old.hash()                  # the v0.1/v0.2 manifest, field for field
    lines = []
    kept, drift = bench.freeze_splits(suite, _domain_config(fake_upstream, domain="marketing",
                                                            split_rule="stratified"), path, out=lines.append)
    assert kept.hash() == old.hash() and drift == []
    assert any("kept" in line and "stratified" in line for line in lines)
    # pinned: the hash rule, the pinned hash, in a workspace that has never seen it
    pinned = _domain_config(fake_upstream, domains=["marketing", "sales"], split_rule="stratified",
                            frozen_splits={"marketing": old.hash()})
    fresh, _ = bench.freeze_splits(suite, pinned, tmp_path / "fresh" / "splits.json")
    assert fresh.hash() == old.hash() and fresh.split_rule is None
    wrong = _domain_config(fake_upstream, domains=["marketing", "sales"], frozen_splits={"marketing": "0" * 16})
    with pytest.raises(ValueError, match="pins"):
        bench.freeze_splits(suite, wrong, tmp_path / "other" / "splits.json")
    with pytest.raises(ValueError, match="pins marketing"):
        bench.freeze_splits(suite, wrong, path)          # on disk and pinned to another hash: not this workspace


def test_grading_of_a_pooled_split_still_returns_train_only(tmp_path, fake_upstream):
    """M7 §4 first paragraph under a pooled split: the grading reader hands out train rows only, whichever domain
    the attempt is of; a dev or test row of any domain comes back empty unless its split is named."""
    from ouroloop import bench
    cfg = _domain_config(fake_upstream, domains=["sales", "support"], split_rule="stratified")
    suite, pooled, _ = bench.load_domains(cfg, tmp_path)
    assert pooled.family_rule == "pooled" and pooled.domain == "sales+support"
    results = Results(tmp_path, cfg.domain)
    seen = {}
    for task in [*pooled.ids("train"), *pooled.ids("dev"), *pooled.ids("test")]:
        r = TaskResult(task, task, pooled.split_of(task), 0, "W0", "run", pooled.hash(), attempt_id=f"run/{task}/r0#0")
        results.put_grading(r, [{"type": "slack_message_contains", "params": {"text": "x"}, "passed": False}])
        seen[(task.split(".")[0], r.split)] = r.attempt_id
    assert {d for d, _s in seen} == {"sales", "support"} and {s for _d, s in seen} == {"train", "dev", "test"}
    for (domain, split), attempt in seen.items():
        rows = results.grading(attempt)
        assert bool(rows) == (split == "train"), (domain, split)
        assert results.grading(attempt, splits=(split,))           # named, it is there
