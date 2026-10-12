"""The two command lines (docs/design/ACTIVITY.md §3): `ouroloop` runs an activity (bench, freeze, export); the training
stack's commands are `python -m ouroloop.train`. Exit 1 is a failed check, exit 2 a command that could not run as
asked. Every run here is in a scratch directory, so no operator .env is read."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import rsi

from ouroloop.activity import cli

REPO = Path(__file__).resolve().parents[2]
OLD_COMMANDS = ("run", "suite", "demo", "decide", "smoke", "replay", "research", "evolve", "deploy", "plan",
                "calibrate", "gate", "models", "serve", "host", "hook", "ledger")


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def toml(cfg: dict) -> str:
    """The config's tables as TOML (scalars, lists of scalars and one level of sub-tables)."""
    def value(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        return json.dumps(v)
    out = []
    for name, table in cfg.items():
        if name.startswith("_") or table is None:
            continue
        out.append(f"[{name}]")
        out += [f"{json.dumps(k)} = {value(v)}" for k, v in table.items() if not isinstance(v, dict) and v is not None]
        for sub, inner in ((k, v) for k, v in table.items() if isinstance(v, dict)):
            out += [f"[{name}.{sub}]"] + [f"{json.dumps(k)} = {value(v)}" for k, v in inner.items()]
        out.append("")
    return "\n".join(out)


def write_config(tmp_path, fake_upstream, **sections) -> Path:
    """A config file of the rsi activity, its workspace `ws` beside it, the declared split frozen there."""
    cfg = rsi.config(tmp_path, fake_upstream, **sections)
    cfg["workspace"] = {"root": "ws"}
    rsi.write_split(tmp_path, rsi.suite(fake_upstream))
    path = tmp_path / "activity.toml"
    path.write_text(toml(cfg), encoding="utf-8")
    assert tomllib.loads(path.read_text(encoding="utf-8"))["bench"]["families"] == rsi.FAMILIES
    return path


# ---------------------------------------------------------------------------- the commands
def test_the_ouroloop_command_runs_an_activity_only(capsys):
    with pytest.raises(SystemExit) as shown:
        cli.main(["--help"])
    assert shown.value.code == 0 and "{bench,freeze,export}" in capsys.readouterr().out
    for old in OLD_COMMANDS:
        with pytest.raises(SystemExit) as refused:
            cli.main([old])
        assert refused.value.code == 2, old
    capsys.readouterr()


def test_the_package_version_is_pyprojects():
    """pip names an install after pyproject's version, and host init names the sdist it sends after __version__
    (train/jobs/hosts): the two are one number."""
    import ouroloop
    with open(REPO / "pyproject.toml", "rb") as f:
        assert ouroloop.__version__ == tomllib.load(f)["project"]["version"]


@pytest.mark.parametrize("phase", ["freeze", "baseline", "library", "round", "stream", "evaluate", "report", "cycle"])
def test_checks_only_outside_the_smoke_is_refused_before_anything_runs(phase, tmp_path, monkeypatch, capsys):
    """`--checks-only` is read by the smoke alone: on another phase it would be ignored and the phase would run for
    real, so it is refused before the config is even read."""
    opened = []
    monkeypatch.setattr(cli, "_open", lambda *a, **k: opened.append(1))
    code = cli.main(["bench", "--config", str(tmp_path / "missing.toml"), "--phase", phase, "--checks-only"])
    assert code == 2 and opened == []
    assert "--checks-only is a smoke option" in capsys.readouterr().err


def test_the_smoke_checks_from_a_config_file_freeze_nothing(tmp_path, fake_upstream, capsys):
    path = write_config(tmp_path, fake_upstream)
    out = tmp_path / "smoke.json"
    assert cli.main(["bench", "--config", str(path), "--phase", "smoke", "--checks-only", "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["ok"] and report["attempts"] == [] and report["problems"] == []
    assert "checks passed" in capsys.readouterr().out
    assert not (tmp_path / "ws" / "core.json").exists() and not (tmp_path / "ws" / "rsi" / "active.json").exists()


def test_a_failed_check_exits_one(tmp_path, fake_upstream, capsys, monkeypatch):
    from ouroloop.activity import checks
    path = write_config(tmp_path, fake_upstream)
    monkeypatch.setattr(checks, "attempt_version", lambda: "0000000000000000")
    assert cli.main(["bench", "--config", str(path), "--phase", "smoke", "--checks-only"]) == 1
    assert "REFUSED    K froze the attempt code" in capsys.readouterr().out


def test_freeze_prints_the_rsi_lines_the_config_then_names(tmp_path, fake_upstream, capsys):
    path = write_config(tmp_path, fake_upstream)
    assert cli.main(["freeze", "--config", str(path)]) == 0
    printed = capsys.readouterr().out.splitlines()
    lines = printed[printed.index("[rsi]") + 1:]
    refs = tomllib.loads("\n".join(lines))
    core = json.loads((tmp_path / "ws" / "core.json").read_text(encoding="utf-8"))
    assert refs == {"scope_id": "rsi-test", "core_ref": core["ref"], "split_ref": core["split_hash"]}
    # the user replaces [rsi] with the printed lines: the same activity, frozen again to the same refs
    text = path.read_text(encoding="utf-8")
    path.write_text(text[:text.index("[rsi]")] + "\n".join(printed[printed.index("[rsi]"):]) + "\n", encoding="utf-8")
    assert cli.main(["freeze", "--config", str(path)]) == 0
    assert capsys.readouterr().out.splitlines()[-3:] == lines


def test_a_config_with_an_old_table_exits_two_naming_the_new_one(tmp_path, fake_upstream, capsys):
    path = write_config(tmp_path, fake_upstream)
    path.write_text(path.read_text(encoding="utf-8") + "\n[freeze.budget]\nusd = 5.0\n", encoding="utf-8")
    assert cli.main(["freeze", "--config", str(path)]) == 2
    assert "[freeze] is not read any more: it is [gate], [schedule], [budget] and [final]" in capsys.readouterr().err
    assert not (tmp_path / "ws" / "core.json").exists()


def test_a_phase_of_a_later_stage_exits_two_and_says_which(tmp_path, fake_upstream, capsys):
    path = write_config(tmp_path, fake_upstream)
    assert cli.main(["bench", "--config", str(path), "--phase", "round"]) == 2
    assert "no frozen gate plan" in capsys.readouterr().err
    assert cli.main(["bench", "--config", str(path), "--phase", "export"]) == 2
    assert "`ouroloop export --config C --to DIR`" in capsys.readouterr().err


def test_export_writes_the_active_release_as_an_app(tmp_path, fake_upstream, capsys):
    path = write_config(tmp_path, fake_upstream)
    assert cli.main(["export", "--config", str(path), "--to", str(tmp_path / "app")]) == 0
    active = json.loads((tmp_path / "ws" / "rsi" / "active.json").read_text(encoding="utf-8"))["release_ref"]
    assert "exported" in capsys.readouterr().out
    from ouroloop.runtime.app import App
    assert App(tmp_path / "app").manifest.release_ref == active
    assert cli.main(["export", "--config", str(path), "--release", "release:sha256:" + "0" * 64,
                     "--to", str(tmp_path / "other")]) == 2
    assert "is not a released, unrevoked release" in capsys.readouterr().err


# ---------------------------------------------------------------------------- python -m
def run_module(tmp_path, *args):
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}, "PYTHONPATH": str(REPO)}
    return subprocess.run([sys.executable, "-m", *args], cwd=tmp_path, capture_output=True, text=True, env=env,
                          timeout=120)


def test_python_m_ouroloop_is_the_same_command(tmp_path):
    done = run_module(tmp_path, "ouroloop", "--help")
    assert done.returncode == 0, done.stderr
    assert "{bench,freeze,export}" in done.stdout


def test_python_m_ouroloop_train_runs_the_smoke(tmp_path):
    done = run_module(tmp_path, "ouroloop.train", "smoke", "--jev", "--backend", "mock")
    assert done.returncode == 0, done.stderr
    assert done.stdout.count(" ok") == 3 and "model=mock-keyword" in done.stdout


def test_a_second_process_on_one_activity_is_refused_before_it_pays(tmp_path, fake_upstream, capsys):
    """Two processes on one workspace would both pay for the same missing attempts: while one holds the activity's
    lock, a paid phase of another exits 2; the report, which pays nothing, still runs."""
    path = write_config(tmp_path, fake_upstream)
    held = cli._lock(tmp_path / "ws")
    try:
        assert cli.main(["bench", "--config", str(path), "--phase", "baseline"]) == 2
        assert "another ouroloop process is running this activity" in capsys.readouterr().err
        assert not list((tmp_path / "ws").glob("bench/*/results/*.jsonl"))
        assert cli.main(["bench", "--config", str(path), "--phase", "report"]) == 0
    finally:
        held.close()
    assert cli._lock(tmp_path / "ws") is not None


def test_the_streams_until_names_a_batch():
    assert cli.batch_index(None) is None and cli.batch_index("batch-3") == 3
    with pytest.raises(ValueError, match="batch-K"):
        cli.batch_index("round-1")
