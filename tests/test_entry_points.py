"""The two command lines (docs/PLAN.md P1 step 3): `ouroloop` runs the benchmark (bench, freeze, export); the training
stack's commands are `python -m ouroloop.train` (decide, smoke --jev, host; host is tested in test_hosts.py)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ouroloop import cli
from ouroloop.train import cli as train_cli

REPO = Path(__file__).resolve().parents[1]


def test_the_ouroloop_command_runs_the_benchmark_only(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "{bench,freeze,export}" in capsys.readouterr().out
    for old in ("run", "suite", "demo", "decide", "smoke", "replay", "research", "evolve", "deploy", "plan",
                "calibrate", "gate", "models", "serve", "host", "hook", "ledger"):
        with pytest.raises(SystemExit) as refused:
            cli.main([old])
        assert refused.value.code == 2, old
    capsys.readouterr()


def test_the_package_version_is_pyprojects():
    """pip names an install after pyproject's version, and host init names the sdist it sends after __version__
    (train/jobs/hosts): the two are one number."""
    import tomllib

    import ouroloop
    with open(REPO / "pyproject.toml", "rb") as f:
        assert ouroloop.__version__ == tomllib.load(f)["project"]["version"]


def test_smoke_jev_asks_the_three_primitives_and_checks_each_answer(capsys):
    assert train_cli.main(["smoke", "--jev", "--backend", "mock"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == list(train_cli.SMOKE_QUESTIONS)
    assert all(line.endswith(" ok") and "model=mock-keyword" in line for line in lines)
    with pytest.raises(SystemExit) as refused:            # the mode is named: the chain smoke of v2 joins as another
        train_cli.main(["smoke", "--backend", "mock"])
    assert refused.value.code == 2


def test_a_malformed_answer_fails_the_smoke(capsys):
    assert train_cli.main(["smoke", "--jev", "--backend", "test_entry_points:Lopsided"]) == 1
    assert capsys.readouterr().out.count("MALFORMED") == 3


class Lopsided:
    """Answers every question with probabilities that do not sum to one."""
    name = "lopsided"

    def decide(self, requests):
        from ouroloop.train.types import Answer
        return [Answer({label: 0.9 for label in r.question.labels()}, "lopsided") for r in requests]


def test_decide_takes_its_backend_from_the_type_or_from_a_config(tmp_path, capsys):
    q = ["--type", "noul", "--question", "Is it urgent?", "--state", "before Friday please"]
    assert train_cli.main(["decide", *q]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["model"] == "mock-keyword" and set(answer["probs"]) == {"yes", "no"}
    path = tmp_path / "decide.toml"
    path.write_text('[backends.judge]\ntype = "mock"\nsharpness = 3.0\n')
    assert train_cli.main(["decide", *q, "--config", str(path), "--backend", "judge"]) == 0
    assert json.loads(capsys.readouterr().out)["model"] == "mock-keyword"
    assert train_cli.main(["decide", *q, "--config", str(path), "--backend", "jev"]) == 2
    assert "has no [backends.jev] (it has judge)" in capsys.readouterr().err


def test_python_m_ouroloop_train_runs_the_smoke(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    done = subprocess.run([sys.executable, "-m", "ouroloop.train", "smoke", "--jev", "--backend", "mock"],
                          cwd=tmp_path, capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.count(" ok") == 3
