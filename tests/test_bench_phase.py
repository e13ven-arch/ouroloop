"""The `ouroloop bench` phases: the config, the frozen split, and what the smoke phase establishes."""
import json
from pathlib import Path

import pytest

from ouroloop import bench, cli
from ouroloop.adapters import automationbench as ab
from ouroloop.benchmark import Results, SplitManifest
from ouroloop.chat import ChatTurn, ScriptedChat

LEAD = "marketing.lead_followup"
HOLD = "marketing.unverified_request_hold"


def config(fake_upstream, **extra):
    return {"workspace": {"root": "ws"}, "backends": {"mock": {"type": "mock"}},
            "bench": {"domain": "marketing", "upstream_path": fake_upstream, "smoke_tasks": 2,
                      "ratios": {"train": 0.5, "dev": 0.5}, "seed": 1, **extra.pop("bench", {})}, **extra}


class TestConfig:
    def test_an_unknown_key_is_refused(self, fake_upstream):
        cfg = config(fake_upstream)
        cfg["bench"]["max_steps"] = 10          # belongs in [bench.budget]
        with pytest.raises(ValueError, match=r"\[bench\] does not take"):
            bench.BenchConfig.load(cfg)

    def test_a_relative_checkout_is_relative_to_the_config(self, tmp_path):
        cfg = {"bench": {"upstream_path": "../AutomationBench"}}
        loaded = bench.BenchConfig.load(cfg, tmp_path / "examples")
        assert loaded.upstream_path == str((tmp_path / "AutomationBench").resolve())

    def test_the_budget_and_the_defaults(self, fake_upstream):
        loaded = bench.BenchConfig.load(config(fake_upstream, bench={"budget": {"max_steps": 12}}))
        assert loaded.budget.max_steps == 12 and loaded.budget.max_usd is None
        assert loaded.toolset == "api"

    def test_the_environment_it_records(self, fake_upstream):
        loaded = bench.BenchConfig.load(config(fake_upstream))
        env = loaded.environment(ab.probe(fake_upstream))
        assert env["toolset"] == "api" and env["package"] == "0.0.0-fake" and env["runner"] == "ouroloop"


class TestFreezing:
    def test_the_first_call_freezes_and_a_later_seed_does_not_move_it(self, tmp_path, fake_upstream):
        suite = ab.load_suite("marketing", fake_upstream)
        cfg = bench.BenchConfig.load(config(fake_upstream))
        first, drift = bench.freeze_splits(suite, cfg, tmp_path / "s.json")
        assert drift == [] and first.family_rule == "task_name"
        again, _ = bench.freeze_splits(suite, bench.BenchConfig.load(config(fake_upstream, bench={"seed": 99})),
                                       tmp_path / "s.json")
        assert again.hash() == first.hash() and again.seed == first.seed

    def test_a_changed_task_is_reported_as_drift(self, tmp_path, fake_upstream, monkeypatch):
        suite = ab.load_suite("marketing", fake_upstream)
        cfg = bench.BenchConfig.load(config(fake_upstream))
        bench.freeze_splits(suite, cfg, tmp_path / "s.json")
        changed = ab.Suite("marketing", {**suite.tasks, LEAD: ab.BenchTask(LEAD, 9001,
                                                                          suite.tasks[LEAD].messages,
                                                                          suite.tasks[LEAD].tools, "newcontract")},
                           {t: suite.grading(t) for t in suite.ids()}, suite.versions)
        _, drift = bench.freeze_splits(changed, cfg, tmp_path / "s.json")
        assert any(LEAD in line and "contract" in line for line in drift)


class TestSmoke:
    def lines(self, report, printed):
        return "\n".join(printed)

    def run(self, tmp_path, fake_upstream, cfg=None, **kw):
        printed = []
        report = bench.smoke(cfg or config(fake_upstream), tmp_path, out=printed.append, **kw)
        return report, "\n".join(printed)

    def test_checks_only_calls_no_model_and_passes(self, tmp_path, fake_upstream):
        report, text = self.run(tmp_path, fake_upstream, run_agent=False)
        assert report["ok"] and report["tasks"] == 2
        assert report["contract"]["failed"] == [] and report["audit"]["leaks"] == []
        assert report["attempts"] == []
        assert "upstream" in text and "0.0.0-fake" in text
        assert "splits" in text and "contract" in text and "audit" in text

    def test_it_freezes_the_split_in_the_workspace(self, tmp_path, fake_upstream):
        self.run(tmp_path, fake_upstream, run_agent=False)
        path = Results(tmp_path / "ws", "marketing").manifest_path()
        assert path.exists()
        frozen = SplitManifest.load(path)
        assert set(frozen.splits.values()) <= {"train", "dev", "test"} and len(frozen.entries) == 2

    def test_it_names_the_inverse_task_so_a_pass_there_is_read_correctly(self, tmp_path, fake_upstream):
        cfg = config(fake_upstream, bench={"ratios": {"dev": 1.0}})
        report, text = self.run(tmp_path, fake_upstream, cfg=cfg, run_agent=False)
        assert report["contract"]["inverse_tasks"] == [HOLD]
        assert "doing nothing already passes" in text

    def test_it_reports_a_test_task_as_off_limits(self, tmp_path, fake_upstream):
        cfg = config(fake_upstream, bench={"ratios": {"dev": 0.5, "test": 0.5}})
        report, text = self.run(tmp_path, fake_upstream, cfg=cfg, run_agent=False)
        manifest = SplitManifest.load(Results(tmp_path / "ws", "marketing").manifest_path())
        for task in manifest.ids("test"):
            assert task not in report["contract"]["checked"]

    def test_a_named_task_outside_dev_is_refused_with_a_note(self, tmp_path, fake_upstream):
        cfg = config(fake_upstream, bench={"ratios": {"test": 1.0}})
        report, text = self.run(tmp_path, fake_upstream, cfg=cfg, run_agent=False, tasks=[LEAD])
        assert "not development tasks" in text and report["contract"]["checked"] == []
        # Nothing was checked, so nothing is established: the phase does not call that a pass.
        assert report["ok"] is False and "no development task to check" in text

    def test_reassigning_the_groups_is_refused_once_results_exist(self, tmp_path, fake_upstream):
        self.run(tmp_path, fake_upstream, run_agent=False)
        results = Results(tmp_path / "ws", "marketing")
        results.path.parent.mkdir(parents=True, exist_ok=True)
        results.path.write_text('{"task_id": "x"}\n')
        with pytest.raises(ValueError, match="already holds results"):
            self.run(tmp_path, fake_upstream, run_agent=False, replace_splits=True)

    def test_without_an_llm_the_checks_still_run(self, tmp_path, fake_upstream):
        report, text = self.run(tmp_path, fake_upstream, run_agent=True)
        assert report["ok"] and report["attempts"] == [] and "no [llm] table" in text

    def test_with_a_model_it_produces_a_task_result(self, tmp_path, fake_upstream, monkeypatch):
        cfg = config(fake_upstream, llm={"type": "anthropic", "model": "scripted-1"},
                     bench={"ratios": {"dev": 1.0}, "smoke_tasks": 1})
        monkeypatch.setattr(bench.config_mod, "build_provider",
                            lambda spec: ScriptedChat([ChatTurn("Nothing to do.", [], "end")]))
        report, text = self.run(tmp_path, fake_upstream, cfg=cfg)
        assert len(report["attempts"]) == 1
        attempt = report["attempts"][0]
        assert attempt["validity"] == "valid" and attempt["termination"] == "finished"
        assert attempt["run"] == report["run"]["hash"] and attempt["split"] == "dev"
        assert report["summary"]["cost_usd"] is None and report["summary"]["cost_missing"] == 1
        assert "attempt" in text and "summary" in text
        assert report["run"]["model"]["model"] == "scripted"      # what the provider reports, not what was asked


class TestPhases:
    def test_br01_a_phase_whose_requirements_are_missing_is_refused_before_a_model_is_called(self, fake_upstream):
        """BR-01: the refusal names the missing piece, and the smoke entry keeps working."""
        with pytest.raises(ValueError, match=r"\[llm\] table"):
            bench.run_phase(config(fake_upstream), phase="baseline")
        with pytest.raises(ValueError, match="task_plan_ref"):
            bench.run_phase(config(fake_upstream, llm={"type": "mock"}), phase="improve")
        with pytest.raises(ValueError, match="research_plan_ref"):
            bench.run_phase(config(fake_upstream, llm={"type": "mock"}), phase="research")
        with pytest.raises(ValueError, match="final_plan_ref"):
            bench.run_phase(config(fake_upstream, llm={"type": "mock"}), phase="evaluate")

    def test_a_phase_refuses_arguments_that_do_not_apply_to_it(self, fake_upstream):
        with pytest.raises(TypeError, match="does not take"):
            bench.run_phase(config(fake_upstream), phase="report", limit=1)

    def test_an_unknown_phase_is_refused(self, fake_upstream):
        with pytest.raises(ValueError, match="unknown phase"):
            bench.run_phase(config(fake_upstream), phase="nope")


class TestCli:
    def write(self, tmp_path, fake_upstream, **extra):
        cfg = [f'[workspace]\nroot = "ws"\n\n[bench]\ndomain = "marketing"\n',
               f'upstream_path = "{fake_upstream}"\nsmoke_tasks = 1\n\n[bench.ratios]\ndev = 1.0\n']
        path = tmp_path / "bench.toml"
        path.write_text("".join(cfg))
        return path

    def test_checks_only_exits_zero_and_writes_a_report(self, tmp_path, fake_upstream, capsys):
        path = self.write(tmp_path, fake_upstream)
        out = tmp_path / "report.json"
        code = cli.main(["bench", "--config", str(path), "--checks-only", "--out", str(out)])
        assert code == 0
        report = json.loads(out.read_text())
        assert report["ok"] and report["splits"]["rule"] == "task_name"

    def test_a_missing_checkout_exits_two(self, tmp_path, capsys):
        path = tmp_path / "bench.toml"
        path.write_text(f'[workspace]\nroot = "ws"\n\n[bench]\nupstream_path = "{tmp_path / "nope"}"\n')
        assert cli.main(["bench", "--config", str(path), "--checks-only"]) == 2
        assert "not an AutomationBench checkout" in capsys.readouterr().err

    def test_a_phase_that_cannot_run_yet_exits_two_and_says_why(self, tmp_path, fake_upstream, capsys):
        path = self.write(tmp_path, fake_upstream)
        assert cli.main(["bench", "--config", str(path), "--phase", "improve"]) == 2
        assert "task_plan_ref" in capsys.readouterr().err
        assert cli.main(["bench", "--config", str(path), "--phase", "nope" if False else "baseline"]) == 2
        assert "[llm] table" in capsys.readouterr().err


class TestRunIdentity:
    def test_the_whole_llm_table_is_recorded_not_a_shortlist(self, tmp_path, fake_upstream, monkeypatch):
        """A reasoning effort passed through a provider's `extra` must not be the setting that went unrecorded."""
        cfg = config(fake_upstream, bench={"ratios": {"dev": 1.0}, "smoke_tasks": 1},
                     llm={"type": "openai_compat", "model": "glm-5.3", "api_key_env": "SOME_KEY",
                          "extra": {"reasoning_effort": "max"}})
        monkeypatch.setattr(bench.config_mod, "build_provider",
                            lambda spec: ScriptedChat([ChatTurn("Nothing to do.", [], "end")]))
        printed = []
        report = bench.smoke(cfg, tmp_path, out=printed.append)
        model = report["run"]["model"]
        assert model["config"]["extra"]["reasoning_effort"] == "max"
        assert model["config"]["model"] == "glm-5.3"
        # Two runs that differ only in that effort are not the same run.
        other = dict(cfg["llm"], extra={"reasoning_effort": "low"})
        assert bench.RunManifest("base", "base", "dev", {"config": other}).hash() != \
               bench.RunManifest("base", "base", "dev", {"config": cfg["llm"]}).hash()


class TestTaskSelection:
    def test_named_tasks_are_not_capped_by_smoke_tasks(self, tmp_path, fake_upstream):
        cfg = config(fake_upstream, bench={"ratios": {"dev": 1.0}, "smoke_tasks": 1})
        printed = []
        report = bench.smoke(cfg, tmp_path, tasks=[LEAD, HOLD], run_agent=False, out=printed.append)
        assert sorted(report["contract"]["checked"]) == sorted([HOLD, LEAD])

    def test_the_default_selection_is_capped(self, tmp_path, fake_upstream):
        cfg = config(fake_upstream, bench={"ratios": {"dev": 1.0}, "smoke_tasks": 1})
        report = bench.smoke(cfg, tmp_path, run_agent=False, out=lambda _line: None)
        assert len(report["contract"]["checked"]) == 1

    def test_limit_overrides_both(self, tmp_path, fake_upstream):
        cfg = config(fake_upstream, bench={"ratios": {"dev": 1.0}, "smoke_tasks": 2})
        report = bench.smoke(cfg, tmp_path, tasks=[LEAD, HOLD], limit=1, run_agent=False, out=lambda _l: None)
        assert report["contract"]["checked"] == [LEAD]


class TestModelIdentity:
    def provider(self):
        return ScriptedChat([ChatTurn("", [], "end")])

    def test_a_key_in_the_table_never_reaches_the_manifest(self):
        identity = bench.model_identity(self.provider(), {"type": "openai_compat", "model": "m",
                                                          "api_key": "sk-abcdefghijklmnopqrstuvwxyz",
                                                          "api_key_env": "ZHIPUAI_API_KEY"})
        assert "api_key" not in identity["config"]
        assert identity["config"]["api_key_env"] == "ZHIPUAI_API_KEY"   # a variable name, not a secret
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(identity)

    def test_everything_that_changes_a_result_is_kept(self):
        identity = bench.model_identity(self.provider(), {"type": "anthropic", "model": "m", "max_tokens": 8192,
                                                          "extra": {"reasoning_effort": "max"}})
        assert identity["config"]["max_tokens"] == 8192
        assert identity["config"]["extra"] == {"reasoning_effort": "max"}


# ---------------------------------------------------------------------------- v0.3 (BR6-21): k per round
def test_max_candidates_per_round_up_to_the_preset_is_accepted(fake_upstream):
    """BR6-21 / M7 §2 (v0.3): under the v3 preset `[improvement] max_candidates_per_round` takes 1..3 and defaults
    to the preset's 3; 4 is refused before anything is paid; the v1 preset still runs one candidate a round, and a
    frozen schedule may run fewer than the config allows, never more (`_beyond_preset`)."""
    base = config(fake_upstream, improvement={"mode": "automatic", "preset": "automationbench_harness_v3"})
    assert bench.improvement_settings(base)["candidates_per_round"] == 3
    for k in (1, 2, 3):
        cfg = {**base, "improvement": {**base["improvement"], "max_candidates_per_round": k}}
        assert bench.improvement_settings(cfg)["candidates_per_round"] == k
    with pytest.raises(ValueError, match=r"max_candidates_per_round = 4: this preset runs 1 to 3"):
        bench.improvement_settings({**base, "improvement": {**base["improvement"], "max_candidates_per_round": 4}})
    with pytest.raises(ValueError, match="one candidate per round"):
        bench.improvement_settings({**base, "improvement": {"mode": "automatic", "preset": "marketing_harness_v1",
                                                            "max_candidates_per_round": 2}})
    settings = bench.improvement_settings({**base, "improvement": {**base["improvement"], "max_candidates_per_round": 2}})
    assert bench._beyond_preset(settings, {**settings["schedule"], "candidates_per_round": 1}, None) == []
    assert any("candidates_per_round 3 is above max_candidates_per_round 2" in p
               for p in bench._beyond_preset(settings, {**settings["schedule"], "candidates_per_round": 3}, None))
    # the session's caps and the screen's numbers are the preset's ceilings: lowered, never raised
    lowered = bench._preset_harness({"harness_evolution": {"research_session": {"max_turns": 20},
                                                           "screen": {"passing_sample": 3}}},
                                    bench.PRESETS["automationbench_harness_v3"]["harness"])
    assert lowered["research_session"]["max_turns"] == 20 and lowered["research_session"]["max_edits"] == 24
    assert lowered["screen"] == {"passing_sample": 3, "min_net_flips": 0, "min_fixes": 1, "max_sample_regressions": 1}
    with pytest.raises(ValueError, match="max_turns = 50 is above the preset's 40"):
        bench._preset_harness({"harness_evolution": {"research_session": {"max_turns": 50}}},
                              bench.PRESETS["automationbench_harness_v3"]["harness"])
    with pytest.raises(ValueError, match="has no research_session"):
        bench._preset_harness({"harness_evolution": {"research_session": {"max_turns": 10}}},
                              bench.PRESETS["marketing_harness_v1"]["harness"])


def test_nested_harness_tables_tighten_per_key_and_are_validated_without_a_preset(fake_upstream):
    """M7 §2 / M4 §1–§2 (2026-10-05 review): `screen.min_net_flips` is a floor — lowering it loosens the screen and is
    refused, raising it is allowed — the other numbers are caps; every number has a least value (research_session.*
    ≥ 1, passing_sample ≥ 0); a non-table, an unknown key or a non-integer is a named config error under a preset and
    without one (one helper); an empty table header asks for the session with the default caps."""
    v3 = bench.PRESETS["automationbench_harness_v3"]["harness"]
    # v0.4 (V0_4_PLAN §1.6): the preset's min_net_flips is 0 and its new floor min_fixes 1, its new cap
    # max_sample_regressions 1; the session's read floors are floors too
    with pytest.raises(ValueError, match="min_fixes = 0 is below the preset's 1: a floor may be raised"):
        bench._preset_harness({"harness_evolution": {"screen": {"min_fixes": 0}}}, v3)
    with pytest.raises(ValueError, match="min_passing_reads = 0 is below the preset's 1: a floor may be raised"):
        bench._preset_harness({"harness_evolution": {"research_session": {"min_passing_reads": 0}}}, v3)
    with pytest.raises(ValueError, match="max_sample_regressions = 2 is above the preset's 1"):
        bench._preset_harness({"harness_evolution": {"screen": {"max_sample_regressions": 2}}}, v3)
    raised = bench._preset_harness({"harness_evolution": {"screen": {"min_net_flips": 2}}}, v3)
    assert raised["screen"] == {"passing_sample": 6, "min_net_flips": 2, "min_fixes": 1, "max_sample_regressions": 1}
    with pytest.raises(ValueError, match="max_turns = 0 is below the least value 1"):
        bench._preset_harness({"harness_evolution": {"research_session": {"max_turns": 0}}}, v3)
    with pytest.raises(ValueError, match="passing_sample = -1 is below the least value 0"):
        bench._preset_harness({"harness_evolution": {"screen": {"passing_sample": -1}}}, v3)
    with pytest.raises(ValueError, match=r"research_session is a table \(\[.*max_turns.*\]\), not int"):
        bench._preset_harness({"harness_evolution": {"research_session": 3}}, v3)
    with pytest.raises(ValueError, match=r"\[harness_evolution.screen\] does not take \['flips'\]"):
        bench._preset_harness({"harness_evolution": {"screen": {"flips": 1}}}, v3)
    with pytest.raises(ValueError, match="max_edits = 2.5 is not an integer"):
        bench._preset_harness({"harness_evolution": {"research_session": {"max_edits": 2.5}}}, v3)
    with pytest.raises(ValueError, match="max_edits = True is not an integer"):
        bench._preset_harness({"harness_evolution": {"research_session": {"max_edits": True}}}, v3)
    # without a preset: the same helper, the design's defaults for the keys not written, presence not truthiness
    base = {"harness_evolution": {"package_api": "harness_package.v2", "components": ["agent", "memory", "skills"],
                                  "research_session": {}, "screen": {"min_net_flips": 2}}}
    frozen = bench.harness_settings(base)
    assert frozen["research_session"] == {"max_turns": 40, "max_edits": 24, "max_trajectory_bytes": 65536,
                                          "max_trajectory_bytes_total": 524288}
    assert frozen["screen"] == {"passing_sample": 5, "min_net_flips": 2}
    with pytest.raises(ValueError, match="max_turns = 0 is below the least value 1"):
        bench.harness_settings({"harness_evolution": {**base["harness_evolution"], "research_session": {"max_turns": 0}}})
    with pytest.raises(ValueError, match="screen is a table"):
        bench.harness_settings({"harness_evolution": {**base["harness_evolution"], "screen": [1]}})
    with pytest.raises(ValueError, match="belong to package_api harness_package.v2"):
        bench.harness_settings({"harness_evolution": {"components": ["memory"], "research_session": {}}})
    # an empty header under the v3 preset keeps the preset's caps
    empty = bench._preset_harness({"harness_evolution": {"research_session": {}}}, v3)
    assert empty["research_session"] == v3["research_session"]


# ---------------------------------------------------------------------------- v0.3 step 5 (M7 §2): a domain list
FIVE = {"max_steps": 8, "max_tool_calls": 12, "max_model_calls": 20, "max_tokens": 4000}


def marketing_hash(fake_upstream, tmp_path) -> str:
    """The fake Marketing's hash-rule split under seed 1, as v0.1/v0.2 froze Marketing's: what a step-5 config pins."""
    suite = ab.load_suite("marketing", fake_upstream)
    loaded = bench.BenchConfig.load({"bench": {"domain": "marketing", "upstream_path": fake_upstream, "seed": 1,
                                               "ratios": {"train": 0.5, "dev": 0.25, "test": 0.25}}})
    return bench.freeze_splits(suite, loaded, tmp_path / "pin" / "splits.json")[0].hash()


def domains_config(fake_upstream, tmp_path, **bench_table):
    """Marketing (pinned to its hash-rule split, its test spent) beside the fixture's two new domains, stratified."""
    table = {"domains": ["marketing", "sales", "support"], "upstream_path": fake_upstream, "seed": 1,
             "split_rule": "stratified", "frozen_splits": {"marketing": marketing_hash(fake_upstream, tmp_path)},
             "spent": ["marketing"], "budget": dict(FIVE), "ratios": {"train": 0.5, "dev": 0.25, "test": 0.25},
             **bench_table}
    return {"workspace": {"root": str(tmp_path / "ws")}, "bench": table, "backends": {"mock": {"type": "mock"}},
            "llm": {"type": "rsi:BusinessWorker"}, "rsi": {"scope_id": "md", "repeats": 1},
            "pricing": {"input_per_1k": 0.003, "output_per_1k": 0.015, "max_input_tokens": 8000,
                        "max_output_tokens": 2000, "source": "fixture prices"}}


def test_a_domains_list_freezes_one_split_per_domain_and_pools_them(fake_upstream, tmp_path):
    """BR6-18: each listed domain is frozen on its own under bench/<domain>/ — the pinned one by the hash rule, the
    others stratified — and the activity's train/dev/test are the same-named splits pooled in list order, each
    domain in its own frozen order, under the pooled name; the pooled split records every domain's hash and the
    spent ones, so a domain resplit or un-spent is a different split and is refused in the same workspace."""
    cfg = domains_config(fake_upstream, tmp_path)
    loaded = bench.BenchConfig.load(cfg)
    assert loaded.domains == ("marketing", "sales", "support") and loaded.domain == "marketing+sales+support"
    root = tmp_path / "ws"
    lines = []
    suite, pooled, drift = bench.load_domains(loaded, root, out=lines.append)
    assert drift == [] and len(suite) == 2 + 12 + 12
    own = {d: SplitManifest.load(Results(root, d).manifest_path()) for d in loaded.domains}
    assert own["marketing"].split_rule is None and own["marketing"].hash() == loaded.frozen_splits["marketing"]
    assert own["sales"].split_rule == own["support"].split_rule == "stratified"
    assert SplitManifest.load(Results(root, loaded.domain).manifest_path()).hash() == pooled.hash()
    assert pooled.family_rule == "pooled" and pooled.spent() == {"marketing"}
    assert [d["hash"] for d in pooled.domains()] == [own[d].hash() for d in loaded.domains]
    for split in ("train", "dev", "test"):
        assert pooled.ids(split) == [t for d in loaded.domains for t in own[d].ids(split)]
    assert all(t.split(".")[0] in loaded.domains for t in pooled.ids("train", "dev", "test"))
    assert pooled.claim_ids("test") == own["sales"].ids("test") + own["support"].ids("test")
    # the same config in a fresh workspace freezes the same pooled split; un-spending Marketing is another split
    assert bench.load_domains(loaded, tmp_path / "again")[1].hash() == pooled.hash()
    unspent = bench.BenchConfig.load({**cfg, "bench": {**cfg["bench"], "spent": []}})
    with pytest.raises(Exception, match="already freezes another split"):
        bench.load_domains(unspent, root)
    # domain names and lists that cannot be pooled are refused before anything is loaded
    with pytest.raises(ValueError, match="listed twice"):
        bench.BenchConfig.load({"bench": {"domains": ["sales", "sales"]}})
    with pytest.raises(ValueError, match="spent names"):
        bench.BenchConfig.load({"bench": {"domains": ["sales", "support"], "spent": ["marketing"]}})
    with pytest.raises(ValueError, match="covers every listed domain"):
        bench.BenchConfig.load({"bench": {"domains": ["sales", "support"], "spent": ["sales", "support"]}})
    with pytest.raises(ValueError, match="frozen_splits"):
        bench.BenchConfig.load({"bench": {"domains": ["sales"], "frozen_splits": {"marketing": "a" * 16}}})


def test_a_single_domain_is_a_list_of_one(fake_upstream, tmp_path):
    """BR6-22: `domain = "x"` and `domains = ["x"]` are the same config — the domain's own name, its own split under
    bench/x/ (no pooled manifest), the hash rule, nothing spent — and a `domain` that disagrees with `domains` is
    refused rather than one of them quietly winning."""
    single = bench.BenchConfig.load(config(fake_upstream))
    listed = bench.BenchConfig.load(config(fake_upstream, bench={"domains": ["marketing"]}))
    del_domain = config(fake_upstream, bench={"domains": ["marketing"]})
    del del_domain["bench"]["domain"]
    only_list = bench.BenchConfig.load(del_domain)
    for loaded in (single, listed, only_list):
        assert (loaded.domain, loaded.domains, loaded.split_rule, loaded.spent) == \
               ("marketing", ("marketing",), "hash", ())
    a = bench.load_domains(single, tmp_path / "a")[1]
    b = bench.load_domains(only_list, tmp_path / "b")[1]
    assert a.hash() == b.hash() and a.family_rule == "task_name" and a.domains()[0]["domain"] == "marketing"
    assert sorted(p.name for p in (tmp_path / "a" / "bench").iterdir()) == ["marketing"]
    assert bench.pooled_name(("marketing",)) == "marketing"
    with pytest.raises(ValueError, match="give one of them"):
        bench.BenchConfig.load(config(fake_upstream, bench={"domains": ["sales"]}))
    with pytest.raises(ValueError, match="leaves no test for a new claim"):
        bench.BenchConfig.load(config(fake_upstream, bench={"spent": ["marketing"]}))


def test_the_v3_preset_checks_every_listed_domain_loads(fake_upstream, tmp_path, monkeypatch):
    """M7 §2 (the preset's domain lock): the v3 preset no longer locks Marketing — any domain list is a valid config —
    and its rule is that every listed domain loads from the checkout: the smoke (and every phase, through the same
    loader) refuses a domain upstream cannot load, by name, before anything is paid; with all of them loadable each
    domain's split hash is printed and recorded. The v1 preset keeps its Marketing lock."""
    from ouroloop.agent.worker import ProcessWorker
    monkeypatch.setattr(bench, "isolated_worker", lambda settings: ProcessWorker(for_tests=True) if settings else None)
    v3 = {"mode": "automatic", "preset": "automationbench_harness_v3"}
    cfg = config(fake_upstream, improvement=v3)
    cfg["bench"] = {**cfg["bench"], "domains": ["sales", "support", "nosuch"]}
    del cfg["bench"]["domain"]
    assert bench.improvement_settings(cfg)["preset"] == "automationbench_harness_v3"
    with pytest.raises(ab.UpstreamError, match="lists 'nosuch', which the upstream checkout cannot load"):
        bench.smoke(cfg, tmp_path, run_agent=False, out=lambda _l: None)
    cfg["bench"]["domains"] = ["sales", "support"]
    printed = []
    report = bench.smoke(cfg, tmp_path, run_agent=False, out=printed.append)
    recorded = {d["domain"]: d["hash"] for d in report["splits"]["domains"]}
    assert set(recorded) == {"sales", "support"}
    for domain, split in recorded.items():
        assert SplitManifest.load(Results(tmp_path / "ws", domain).manifest_path()).hash() == split
        assert any(line.startswith(f"domain     {domain} split={split}") for line in printed)
    # one dev task of each domain had its scorer contract checked, and none leaked
    assert {t.split(".")[0] for t in report["contract"]["checked"]} == {"sales", "support"}
    assert report["contract"]["failed"] == [] and report["audit"]["leaks"] == []
    v1 = config(fake_upstream, improvement={"mode": "automatic", "preset": "marketing_harness_v1"})
    v1["bench"] = {**v1["bench"], "domains": ["marketing", "sales"]}
    del v1["bench"]["domain"]
    with pytest.raises(ValueError, match="is for the marketing domain"):
        bench.improvement_settings(v1)


def test_an_old_config_reads_under_the_new_reader(fake_upstream, tmp_path):
    """BR6-22: the v0.1/v0.2 configs as committed — a single `domain`, preset `marketing_harness_v1`, package API v1,
    the frozen [rsi] refs — read as they did: one domain, the hash rule, nothing spent, the same reader output; a
    split manifest written before the new optional fields loads and hashes as it was written."""
    import tomllib
    from pathlib import Path
    examples = Path(__file__).resolve().parents[1] / "examples" / "automationbench"
    for name, scope in (("claude.toml", "marketing-claude"), ("claude-full.toml", "marketing-claude-full")):
        cfg = tomllib.loads((examples / name).read_text())
        loaded = bench.BenchConfig.load(cfg, examples)
        assert (loaded.domain, loaded.domains, loaded.split_rule, loaded.frozen_splits, loaded.spent) == \
               ("marketing", ("marketing",), "hash", {}, ())
        settings = bench.improvement_settings(cfg)
        assert settings["preset"] == "marketing_harness_v1" and settings["harness"]["package_api"] == "harness_package.v1"
        assert bench.harness_settings(cfg)["package_api"] == "harness_package.v1"
        rsi_cfg = bench.RsiConfig.load(cfg)
        assert rsi_cfg.scope_id == scope and rsi_cfg.task_plan_ref == cfg["rsi"]["task_plan_ref"]
        assert rsi_cfg.final_plan_ref == cfg["rsi"]["final_plan_ref"]
    # an old-style manifest on disk (no split_rule, no strata keys) keeps its hash under the new reader
    suite = ab.load_suite("marketing", fake_upstream)
    fresh, _ = bench.freeze_splits(suite, bench.BenchConfig.load(config(fake_upstream)), tmp_path / "s.json")
    written = json.loads((tmp_path / "s.json").read_text())
    assert "split_rule" not in written and "strata" not in written
    assert SplitManifest.from_json(written).hash() == written["hash"] == fresh.hash()


class Idle:
    """An agent that answers at once and does nothing: every task with something to do is failed, at a measured cost,
    so a slot's start W has headroom on every inner task."""
    name = "scripted"
    model = "scripted-1"

    def chat(self, system, messages, specs):
        return ChatTurn("Nothing to do here.", [], "end", usage={"input_tokens": 100, "output_tokens": 20})


def run_domains_cycle(fake_upstream, tmp_path, monkeypatch, *, lines=None, schedule=None):
    """A whole cycle over Marketing + the two fixture domains under a v3-shaped test preset (no domain lock, the v1
    package kinds so no worker is needed, gates of at least 3 tasks) with an idle agent: baseline, a segment, the
    research slot — which the headroom check closes (99 tasks wanted), so the slot records its headroom per domain —
    and a second segment."""
    import rsi  # noqa: F401 - the scripted worker the config names
    from test_cycle import Stopper
    preset = json.loads(json.dumps(bench.PRESETS["marketing_harness_v1"]))
    preset["domain"] = None
    preset["harness"].update(components=["memory", "skills"], sandbox=None)
    preset["task_plan"]["gate"].update(min_groups=1, bootstrap_replicates=200, min_tasks=3)
    preset["schedule"].update(worker_rounds=1)
    preset["campaign"].update(worker_round_limit=1, min_groups=1, min_headroom_tasks=99)
    monkeypatch.setitem(bench.PRESETS, "domains_test", preset)
    cfg = domains_config(fake_upstream, tmp_path)
    cfg["improvement"] = {"mode": "automatic", "preset": "domains_test"}
    cfg["freeze"] = {"budget": {"model_calls": 4000, "tool_calls": 4000}, **({"schedule": schedule} if schedule else {})}
    cfg["llm"] = {"type": "test_bench_phase:Idle"}
    result = bench.cycle(cfg, ".", out=(lines.append if lines is not None else lambda _l: None), researcher=Stopper())
    return cfg, result


def test_it6_36_domains_are_split_per_domain_pooled_and_stratified_while_marketings_split_is_frozen_and_its_test_is_spent(
        fake_upstream, tmp_path, monkeypatch):
    """IT6-36: an activity over Marketing and new domains runs on the pooled split — Marketing's frozen by the hash
    rule, the new ones stratified, every result under the pooled name with its domain in the task id — the research
    slot halves the pooled dev inside each domain, so both halves span the domains, and records its headroom per
    domain; a final of the new claim reads the new domains' test only — "test" leaves Marketing's out and a list
    naming it is refused. Under the v3 preset the attempts in flight default to 8."""
    cfg, result = run_domains_cycle(fake_upstream, tmp_path, monkeypatch)
    root = tmp_path / "ws"
    pooled = SplitManifest.load(Results(root, "marketing+sales+support").manifest_path())
    assert [d["split_rule"] for d in pooled.domains()] == ["hash", "stratified", "stratified"]
    assert pooled.domains()[0]["hash"] == cfg["bench"]["frozen_splits"]["marketing"]
    rows = list(Results(root, "marketing+sales+support").all())
    assert rows and {r.task_id.split(".")[0] for r in rows} == {"marketing", "sales", "support"}
    assert not (root / "bench" / "sales" / "results.jsonl").exists()        # one pooled store, per-domain splits
    assert not any(r.split == "test" for r in rows)
    slot = result["steps"]["slot-0"]
    assert slot["outcome"] == "no_headroom"
    halves = slot["halves_by_domain"]
    assert set(halves["inner"]) == set(halves["outer"]) == {"sales", "support"}      # Marketing has no dev task
    assert sum(halves["inner"].values()) == sum(halves["outer"].values()) == 3
    # the idle start W never passed any inner task: every one is headroom, counted per domain
    assert slot["headroom_by_domain"] == halves["inner"] and slot["headroom"] == 3
    # the final of a new claim
    named = cfg                         # the cycle's plans are content-addressed: freezing again names the same
    final = {"experiment_id": "md-final", "claims": ["C1"], "repeats": 3, "tasks": "test",
             "comparisons": [{"claim": "C1", "subject": "evolved", "control": "base", "role": "evolved"},
                             {"claim": "C1", "subject": "base", "control": "base", "role": "base"}]}
    refs = bench.freeze({**named, "freeze": {"final": final}}, ".", out=lambda _l: None)
    activity = bench.open_activity(named, ".", out=lambda _l: None)
    plan = bench.FinalizationPlan.from_json(activity.objects.get(refs["final_plan_ref"]))
    assert list(plan.task_ids) == pooled.claim_ids("test") and plan.repeats == 3
    assert not any(t.startswith("marketing.") for t in plan.task_ids)
    spent_task = next(t for t in pooled.ids("test") if t.startswith("marketing."))
    with pytest.raises(ValueError, match="spent domains"):
        bench.freeze({**named, "freeze": {"final": {**final, "experiment_id": "md-final-2",
                                                    "tasks": [spent_task, *pooled.claim_ids("test")]}}},
                     ".", out=lambda _l: None)
    v3 = {**cfg, "improvement": {"mode": "automatic", "preset": "automationbench_harness_v3"}}
    assert bench.BenchConfig.load(v3).concurrency == 8


# ---------------------------------------------------------------------------- 2026-10-05 review and the staged runbook
def test_a_domain_list_changed_after_bootstrap_is_refused_at_startup(fake_upstream, tmp_path):
    """Review finding 8 (M7 §2): the activity's core pins the pooled split it was bootstrapped on. The same workspace
    opened with another domain list (here the same domains in another order: another pooled split) fails the startup
    checks before anything is paid; and a domain's split replaced on disk is refused when the activity opens, since
    the pooled split it was frozen into says otherwise."""
    cfg = domains_config(fake_upstream, tmp_path)
    cfg["bench"] = {**cfg["bench"], "domains": ["sales", "support"], "spent": [], "frozen_splits": {}}
    first = bench.open_activity(cfg, ".", out=lambda _l: None)
    assert not [p for p in bench.domain_problems(first)]
    swapped = {**cfg, "bench": {**cfg["bench"], "domains": ["support", "sales"]}}
    activity = bench.open_activity(swapped, ".", out=lambda _l: None)
    problems = bench.startup_checks(activity, swapped, phase="baseline", needs_model=False)
    assert any("the core pins split" in p and first.splits.hash() in p for p in problems), problems
    # a domain resplit under the same list: the pooled split on disk refuses it
    loaded = bench.BenchConfig.load({**cfg, "bench": {**cfg["bench"], "seed": 2}})
    bench.freeze_splits(ab.load_suite("sales", fake_upstream), loaded,
                        Results(tmp_path / "ws", "sales").manifest_path(), replace_existing=True)
    with pytest.raises(Exception, match="already freezes another split"):
        bench.open_activity(cfg, ".", out=lambda _l: None)


def test_a_schedule_without_the_presets_research_slot_reports_c4_deferred(fake_upstream, tmp_path, monkeypatch):
    """The staged runbook (ROADMAP v0.3 step 5): one segment of the preset's rounds and no research slot, frozen as a
    schedule that runs fewer slots than the preset plans (`_beyond_preset` refuses only more). The cycle runs the
    baseline and the one segment, the slot never comes, and the report's C4 is not_run with the reason "deferred to
    a separate workspace" rather than "no research campaign on record"."""
    cfg, result = run_domains_cycle(fake_upstream, tmp_path, monkeypatch,
                                    schedule={"worker_rounds": 2, "research_slots": 0, "patience": 2})
    assert result["status"] == "done" and sorted(result["steps"]) == ["baseline", "segment-0"]
    c4 = result["report"]["C4"]
    assert c4["state"] == "not_run" and "deferred to a separate workspace" in c4["detail"]
    assert "research_slots 0 where the preset plans 1" in c4["detail"]
    settings = bench.improvement_settings(cfg)
    assert bench._beyond_preset(settings, {**settings["schedule"], "worker_rounds": 6, "research_slots": 0}, None) == []


# ---------------------------------------------------------------------------- 2026-10-06: continuing past a patience stop
def test_an_extension_after_a_patience_stop_runs_the_next_round_and_is_recorded(fake_upstream, tmp_path, monkeypatch):
    """M7 §1 (2026-10-06): a cycle whose one segment ended by patience (worker_rounds 6, patience 2, two rejected
    rounds) is continued in the same workspace by `--phase improve --rounds 1 --reason ...`: a `schedule_extended`
    event records the deviation, the segment's log is reopened, and round 2 runs — numbered after the segment's two,
    its notes stratified by domain (1 per domain under a cap of 3, each domain's next failure), its gate on the
    pooled control already read (no control arm paid) — under the same plans and root; patience
    counts afresh inside the extension, which ends after its one round. The report lists the extension, the cycle
    reads the segment as ended and runs nothing more, and an extension without a reason is refused."""
    import rsi  # noqa: F401
    from ouroloop.evolve import contracts as ct
    from ouroloop.evolve import experience as xp
    from test_cycle import SkillAnyway

    class FreshSkill(SkillAnyway):
        """A different skill text each round, so each round's candidate reaches the gate."""
        n = 0

        def summarize(self, *a, **kw):
            FreshSkill.n += 1
            out = super().summarize(*a, **kw)
            for p in out.proposals:
                patch = p.changes[0]["change"]["patch"]["write"]
                key = next(k for k in patch if k.startswith("skills/"))
                patch[key] += f" (version {FreshSkill.n})"
            return out
    preset = json.loads(json.dumps(bench.PRESETS["marketing_harness_v1"]))
    preset["domain"] = None
    preset["harness"].update(components=["memory", "skills"], sandbox=None, max_failure_notes=3)
    preset["task_plan"].update(control_pooling="pooled", confirmation_repeats=1)
    preset["task_plan"]["gate"].update(min_groups=1, bootstrap_replicates=200, min_tasks=3)
    preset["schedule"].update(worker_rounds=6, research_slots=0, patience=2)
    monkeypatch.setitem(bench.PRESETS, "extend_test", preset)
    cfg = domains_config(fake_upstream, tmp_path)
    cfg.update(improvement={"mode": "automatic", "preset": "extend_test"}, llm={"type": "test_bench_phase:Idle"},
               freeze={"budget": {"model_calls": 4000, "tool_calls": 4000}})
    first = bench.cycle(cfg, ".", out=lambda _l: None, researcher=FreshSkill())
    assert first["steps"]["segment-0"]["stop_reason"] == "patience", first["steps"]
    named = bench._cycle_config(cfg, ".", bench.improvement_settings(cfg), lambda _l: None)
    root = tmp_path / "ws"
    rows_before = {r.attempt_id for r in Results(root, "marketing+sales+support").all()}
    with pytest.raises(ValueError, match="--reason is required"):
        bench.run_phase(named, ".", phase="improve", rounds=1, researcher=FreshSkill(), out=lambda _l: None)
    lines = []
    result = bench.run_phase(named, ".", phase="improve", rounds=1, researcher=FreshSkill(), out=lines.append,
                             reason="notes stratified by domain (2026-10-06); one more round")
    assert result["extension"]["at_round"] == 2 and result["extension"]["finished"] is True
    assert result["stop_reason"] == "rounds_done"
    assert [r["outcome"] for r in result["rounds"] if r["outcome"] != "stopped"] == ["rejected"]
    events = [e for e in ct.Events(root).of("schedule_extended")]
    assert len(events) == 1 and {k: events[0][k] for k in ("segment", "rounds", "at_round", "frozen_worker_rounds",
                                                          "frozen_patience", "extended_total", "by")} == \
        {"segment": "segment-0", "rounds": 1, "at_round": 2, "frozen_worker_rounds": 6, "frozen_patience": 2,
         "extended_total": 1, "by": "user"}
    assert any(line.startswith("extend     segment-0: 1 more round(s) from round 2") for line in lines)
    # round 2's notes: one per domain, each domain's failure not noted in rounds 0 and 1 where it has one
    diagnoses = [e for e in xp.read(root) if e.get("kind") == "diagnosis"]
    assert [d["round"] for d in diagnoses] == [0, 1, 2]
    domains = lambda d: sorted(t.split(".")[0] for t in d["included"])  # noqa: E731
    assert all(domains(d) == ["marketing", "sales", "support"] for d in diagnoses)
    earlier = set(diagnoses[0]["included"]) | set(diagnoses[1]["included"])
    assert {t for t in diagnoses[2]["included"] if not t.startswith("marketing.")}.isdisjoint(earlier)
    assert diagnoses[2]["by_domain"]["sales"] == {"failures": 6, "included": 1}
    experiments = [e for e in xp.read(root) if e.get("kind") == "experiment"]
    assert len(experiments) == 3 and experiments[-1]["verdict"] == "rejected"           # round 2's own gate
    # the gate read the pooled control: nothing paid for a control arm, the round's new attempts are the candidate's
    new = [r for r in Results(root, "marketing+sales+support").all() if r.attempt_id not in rows_before]
    assert new and not [r for r in new if bench._arm_of(r.attempt_id) == "control"]
    # the record: the segment's log, the cycle's step, the report
    state = json.loads((root / "rsi" / "cycle.json").read_text())
    assert state["steps"]["segment-0"]["stop_reason"] == "rounds_done"
    assert state["steps"]["segment-0"]["extensions"][0]["at_round"] == 2
    report = bench.report(named, ".", out=lambda _l: None, write=False)
    assert report["harness"]["extensions"][0]["reason"].startswith("notes stratified")
    assert "- schedule extended: segment-0 by 1 round(s) from round 2" in bench_markdown(report)
    again = bench.cycle(named, ".", out=lambda _l: None, researcher=FreshSkill())
    assert again["status"] == "done" and len([e for e in xp.read(root) if e.get("kind") == "diagnosis"]) == 3


def bench_markdown(report):
    from ouroloop.report import render_markdown
    return render_markdown(report)


def test_an_improve_without_rounds_on_a_cycle_activity_is_refused(fake_upstream, tmp_path, monkeypatch):
    """M7 §1 (2026-10-06 review): on an activity the cycle has run, `--phase improve` without --rounds would run the
    frozen schedule from round 0 outside the segment's log; it is refused, naming the two legal ways on."""
    from ouroloop import bench as b
    import json as _json
    from pathlib import Path as _Path
    calls = {}

    class Act:
        root = str(tmp_path)

    (tmp_path / "rsi").mkdir()
    (tmp_path / "rsi" / "cycle.json").write_text(_json.dumps({"status": "done", "steps": {"segment-0": {}}}))
    with pytest.raises(ValueError, match="continue it with --phase cycle"):
        b._extension_of(Act(), None, "a reason")


# ---------------------------------------------------------------------------- v0.4 (docs/design/V0_4_PLAN.md §1.1)
def test_a_pinned_split_is_reproduced_under_whichever_rule_made_it_and_a_wrong_pin_is_still_refused(fake_upstream,
                                                                                                  tmp_path):
    """V0_4_PLAN §1.1, M7 §2 (v0.4): a domain [bench.frozen_splits] pins is split under the rule that reproduces the
    pin — the config's split_rule first, then the other — so a stratified split (the v3 workspace's finance and
    support) can be pinned as Marketing's hash-rule split always could; no rule reproducing it is refused as before."""
    suite = ab.load_suite("sales", fake_upstream)
    table = {"domains": ["sales"], "upstream_path": fake_upstream, "seed": 1,
             "ratios": {"train": 0.5, "dev": 0.25, "test": 0.25}}
    stratified, _ = bench.freeze_splits(suite, bench.BenchConfig.load({"bench": {**table, "split_rule": "stratified"}}),
                                        tmp_path / "a.json")
    hashed, _ = bench.freeze_splits(suite, bench.BenchConfig.load({"bench": {**table, "split_rule": "hash"}}),
                                    tmp_path / "b.json")
    assert stratified.hash() != hashed.hash() and stratified.split_rule == "stratified"
    for rule in ("stratified", "hash"):
        for pin, want in ((stratified, "stratified"), (hashed, None)):
            cfg = bench.BenchConfig.load({"bench": {**table, "split_rule": rule, "frozen_splits": {"sales": pin.hash()}}})
            got, _ = bench.freeze_splits(suite, cfg, tmp_path / f"{rule}-{pin.hash()}.json")
            assert got.hash() == pin.hash() and got.split_rule == want, (rule, want)
    wrong = bench.BenchConfig.load({"bench": {**table, "split_rule": "stratified",
                                              "frozen_splits": {"sales": "0123456789abcdef"}}})
    with pytest.raises(ValueError, match="neither split rule reproduces sales's pin"):
        bench.freeze_splits(suite, wrong, tmp_path / "wrong.json")


V04_CONFIGS = {"claude-v4-finance.toml": ("finance", "4fd91eaf9fb8daf5", "finance-claude-v4"),
               "claude-v4-support.toml": ("support", "3da11232f5d5ff38", "support-claude-v4")}


def test_the_v04_configs_are_single_domain_activities_on_the_v3_workspaces_frozen_splits():
    """V0_4_PLAN §1.1, §1.6, §3: each v0.4 config is one domain with its own workspace, the stratified rule and seed 1,
    pinned to the hash workspace-claude-v3 froze for it (so its test is the same never-read 25 tasks); the v3 preset
    with K's effort at medium, k = 3, v0.4's screen and read floors; one segment of 3 rounds, patience 2, no slot;
    three W0 dev readings; the root $450 with a $160 final share; the measured unit costs as the estimate."""
    import tomllib
    examples = Path(__file__).resolve().parents[1] / "examples" / "automationbench"
    for name, (domain, pin, scope) in V04_CONFIGS.items():
        cfg = tomllib.loads((examples / name).read_text())
        loaded = bench.BenchConfig.load(cfg, examples)
        assert (loaded.domains, loaded.split_rule, loaded.seed, loaded.frozen_splits, loaded.control_readings) == \
            ((domain,), "stratified", 1, {domain: pin}, 3)
        assert loaded.ratios == {"train": 0.5, "dev": 0.25, "test": 0.25} and cfg["workspace"]["root"].endswith(domain)
        settings = bench.improvement_settings(cfg)
        assert settings["preset"] == "automationbench_harness_v3" and settings["candidates_per_round"] == 3
        harness = bench.harness_settings(cfg)
        assert harness["max_effort"] == "medium" and harness["package_api"] == "harness_package.v2"
        assert harness["screen"] == {"passing_sample": 6, "min_net_flips": 0, "min_fixes": 1,
                                     "max_sample_regressions": 1}
        assert harness["research_session"]["min_failing_reads"] == harness["research_session"]["min_passing_reads"] == 1
        assert cfg["freeze"]["schedule"] == {"worker_rounds": 3, "research_slots": 0, "patience": 2}
        assert cfg["freeze"]["budget"]["usd"] == 450.0 and cfg["freeze"]["budget"]["final"]["usd"] == 160.0
        assert cfg["estimate"]["cost_per_attempt"] == {"base": 0.466, "evolved": 0.49}
        assert bench.RsiConfig.load(cfg).scope_id == scope
        assert cfg["llm"] == tomllib.loads((examples / "claude-v3.toml").read_text())["llm"]
        assert cfg["pricing"] == tomllib.loads((examples / "claude-v3.toml").read_text())["pricing"]


def test_the_v04_configs_reproduce_the_v3_workspaces_finance_and_support_splits_offline(tmp_path):
    """V0_4_PLAN §1.1: under each v0.4 config the upstream checkout splits its domain into exactly the manifest
    workspace-claude-v3 froze (the pinned hash, the same 25 test tasks), offline — no model, nothing written but a
    temporary manifest. Run as its own process (the upstream package from the real checkout, never the fixture's) and
    skipped without the checkout."""
    import subprocess
    import sys
    if not Path("~/workspace/AutomationBench").expanduser().exists():
        pytest.skip("the upstream checkout is not here")
    examples = Path(__file__).resolve().parents[1] / "examples" / "automationbench"
    code = (
        "import json, sys, tomllib\n"
        "from pathlib import Path\n"
        "from ouroloop import bench\n"
        "from ouroloop.adapters import automationbench as ab\n"
        "from ouroloop.benchmark import SplitManifest\n"
        "examples, out = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "result = {}\n"
        "for name in sys.argv[3:]:\n"
        "    cfg = bench.BenchConfig.load(tomllib.loads((examples / name).read_text()), examples)\n"
        "    domain = cfg.domains[0]\n"
        "    m, _ = bench.freeze_splits(ab.load_suite(domain, cfg.upstream_path), cfg, out / f'{domain}.json')\n"
        "    v3 = examples / 'workspace-claude-v3' / 'bench' / domain / 'splits.json'\n"
        "    result[domain] = {'hash': m.hash(), 'test': m.ids('test'),\n"
        "                      'v3_test': SplitManifest.load(v3).ids('test') if v3.exists() else None}\n"
        "print(json.dumps(result))\n")
    done = subprocess.run([sys.executable, "-c", code, str(examples), str(tmp_path), *V04_CONFIGS],
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    got = json.loads(done.stdout.strip().splitlines()[-1])
    for name, (domain, pin, _scope) in V04_CONFIGS.items():
        assert got[domain]["hash"] == pin and len(got[domain]["test"]) == 25
        if got[domain]["v3_test"] is not None:
            assert got[domain]["test"] == got[domain]["v3_test"]
