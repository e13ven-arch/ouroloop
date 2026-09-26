"""The `ouroloop bench` phases: the config, the frozen split, and what the smoke phase establishes."""
import json

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
    def test_an_unimplemented_phase_is_refused_with_the_milestone(self, fake_upstream):
        with pytest.raises(NotImplementedError, match="M2 in docs/ROADMAP.md"):
            bench.run_phase(config(fake_upstream), phase="baseline")

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

    def test_an_unimplemented_phase_exits_two(self, tmp_path, fake_upstream, capsys):
        path = self.write(tmp_path, fake_upstream)
        assert cli.main(["bench", "--config", str(path), "--phase", "report"]) == 2
        assert "M6 in docs/ROADMAP.md" in capsys.readouterr().err


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
