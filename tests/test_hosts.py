"""Training hosts (hosts/, bootstrap.sh) and trainer plugins through the real job path, on this machine: host init and
check against target "local" in a temporary home, pip stood in for (OUROLOOP_TEST_PIP), so nothing is downloaded;
the signature plugin from spec.json through `python -m ouroloop.worker` to a model the gate promotes."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ouroloop import config
from ouroloop.cli import main
from ouroloop.evolve.gate import freeze_template, records
from ouroloop.evolve.pipeline import evolve_model
from ouroloop.evolve.registry import Registry
from ouroloop.evolve.train import CHECKOUT, DEFAULT_RECIPE, JobTrainer, TrainError, TrainJob
from ouroloop.hosts import MANIFEST, read, stale
from ouroloop.release import code_version
from ouroloop.trainers import load
from test_gate_record import ACT_GATE, CFG, collect, ctx

pytestmark = pytest.mark.usefixtures("loose_risk")

# pip, stood in for: it logs each call to <venv>/pip.log, and "installs" an archive by unpacking it and pointing a .pth
# file at it; a requirement that is not an archive is taken as installed. STUB_PIP=fail fails every call.
STUB_PIP = """
import json, os, shutil, subprocess, sys, tarfile
venv = os.environ["VIRTUAL_ENV"]
with open(os.path.join(venv, "pip.log"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("STUB_PIP") == "fail":
    print("ERROR: No matching distribution found for tde")
    sys.exit(1)
args, reqs, i = sys.argv[2:], [], 0
while i < len(args):
    if args[i] == "-r":
        reqs += [line.strip() for line in open(args[i + 1]) if line.strip()]
        i += 2
    else:
        reqs += [] if args[i].startswith("-") else [args[i]]
        i += 1
site = subprocess.run([os.path.join(venv, "bin", "python"), "-c",
                       "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                      capture_output=True, text=True, check=True).stdout.strip()
for r in reqs:
    if r.endswith(".tar.gz"):
        name = os.path.basename(r)[:-7]
        dest = os.path.join(venv, "stub", name)
        shutil.rmtree(dest, ignore_errors=True)
        with tarfile.open(r) as t:
            t.extractall(dest, filter="data")
        (top,) = os.listdir(dest)
        with open(os.path.join(site, f"stub_{name}.pth"), "w") as f:
            f.write(os.path.join(dest, top) + "\\n")
"""


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A fresh home for host init, with pip stood in for and this Python to make the venv from."""
    h = tmp_path / "home"
    h.mkdir()
    pip = tmp_path / "pip"
    pip.write_text(f"#!{sys.executable}\n{STUB_PIP}")
    pip.chmod(0o755)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("OUROLOOP_TEST_PIP", str(pip))
    monkeypatch.setenv("OUROLOOP_PYTHON", sys.executable)
    monkeypatch.delenv("STUB_PIP", raising=False)
    return h


def says(view: str) -> str:
    """The signature plugin's key for these sessions: what the message says (the job imports it from here)."""
    return "yes" if "say yes" in view else "no"


def pip_calls(home: Path) -> list:
    log = home / ".ouroloop" / "venv" / "pip.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def a_job(root, version="m_0001", parent=None, n=5) -> TrainJob:
    """A job as the pipeline hands it over: its data in <workspace>/models/<version>/data."""
    data = Path(root) / "models" / version / "data"
    data.mkdir(parents=True)
    example = {"id": "e", "state": "say yes", "question": "done?", "candidates": [{"name": "yes"}, {"name": "no"}],
               "target": [1.0, 0.0], "meta": {"weight": 1.0}}
    (data / "train.jsonl").write_text((json.dumps(example) + "\n") * n)
    (data / "val.jsonl").write_text(json.dumps(example) + "\n")
    return TrainJob(version, data, dict(DEFAULT_RECIPE), parent)


# ---------------------------------------------------------------- host init and check
def test_host_init_prepares_this_machine_once_and_again_changes_nothing(home, capsys):
    assert main(["host", "init", "local", "--trainer", "signature"]) == 0
    printed = json.loads(capsys.readouterr().out)
    m = json.loads((home / ".ouroloop" / "host.json").read_text())
    assert printed == m and m["ouroloop"]["code"] == code_version() and m["gpu"] == []
    assert m["plugins"] == {"signature": {"version": None, "source": None}}
    assert m["hf"] == {"hub": False, "token": False}
    calls = pip_calls(home)
    assert calls[0] == ["install", "-q", "-r", "requirements.txt"] and calls[1][-1] == "./ouroloop.tar.gz"
    # the venv runs the ouroloop it was sent, installed: not this checkout
    venv = home / ".ouroloop" / "venv" / "bin" / "python"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    where = subprocess.run([str(venv), "-c", "import ouroloop; print(ouroloop.__file__)"], capture_output=True,
                           text=True, check=True, cwd=home, env=env)
    assert str(home) in where.stdout and str(CHECKOUT) not in where.stdout
    assert main(["host", "check", "local", "--trainer", "signature"]) == 0
    assert capsys.readouterr().err.strip().endswith("local: ready")
    # again: the same venv, the same code
    (home / ".ouroloop" / "venv" / "mark").write_text("kept")
    assert main(["host", "init", "local", "--trainer", "signature"]) == 0
    assert (home / ".ouroloop" / "venv" / "mark").exists() and len(pip_calls(home)) == 4
    assert read("local")["ouroloop"]["code"] == code_version()
    assert main(["host", "check", "local", "--trainer", "tde"]) == 1
    assert "no trainer plugin tde" in capsys.readouterr().err


def test_a_host_on_another_version_is_initialized_again_before_the_job(home, tmp_path):
    assert main(["host", "init", "local", "--trainer", "signature"]) == 0
    m = read("local")
    (home / ".ouroloop" / "host.json").write_text(json.dumps({**m, "ouroloop": {**m["ouroloop"], "code": "0" * 16}}))
    assert stale(read("local"), {"signature": {}}) == [f"it runs ouroloop {'0' * 16}, this is {code_version()}"]
    before = len(pip_calls(home))
    tr = JobTrainer("signature", host="local", jobs_root="~/jobs", poll_s=0.05, models={"jobs_root": "~/jobs"})
    result = tr.train(a_job(tmp_path / "ws"))
    assert len(pip_calls(home)) == before + 2 and read("local")["ouroloop"]["code"] == code_version()
    job = result.job
    assert job["result"]["host"]["ouroloop"]["code"] == code_version()        # the host manifest, recorded
    assert (home / "jobs" / job["id"] / "run" / "model.json").exists()
    # the model, loaded here by its ref through the plugin that trained it
    assert result.backend == {"type": "model", "model": job["id"], "trainer": "signature"}
    assert tr.backend(result).model == job["id"]


def test_a_host_that_cannot_be_initialized_leaves_the_round_inconclusive_before_anything_is_used(home, tmp_path,
                                                                                                  monkeypatch):
    monkeypatch.setenv("STUB_PIP", "fail")
    jobs = tmp_path / "jobs"
    cfg = {**CFG, "models": {"jobs_root": str(jobs)}}
    rt = config.build_runtime(cfg, tmp_path)
    collect(rt, 150)
    freeze_template(rt.root, ACT_GATE)
    trainer = config.build_trainer({"type": "signature", "host": "local", "jobs_root": str(jobs)}, cfg["models"])
    card = evolve_model(rt, trainer)
    (record,) = records(rt.root)
    assert card["gate"] == "inconclusive" and not record.used and "model_id" not in card
    assert "host init on this machine failed" in card["reasons"][0] and "No matching distribution" in card["reasons"][0]
    assert not jobs.exists() and not (home / ".ouroloop" / "host.json").exists()
    with pytest.raises(TrainError, match="host init"):
        trainer.train(a_job(tmp_path / "ws2"))


def test_bootstrap_says_what_is_missing_when_it_cannot_install_it(home, tmp_path):
    from ouroloop.hosts import plan
    d = plan({}, tmp_path / "init")
    assert sorted(p.name for p in d.iterdir()) == ["bootstrap.sh", "ouroloop.tar.gz", "plan.json", "requirements.txt"]
    bare = tmp_path / "bin"                         # a PATH with a shell and no Python, rsync or git
    bare.mkdir()
    for tool in ("sh", "cat", "id", "mkdir", "cp", "mv"):
        (bare / tool).symlink_to(subprocess.run(["which", tool], capture_output=True, text=True).stdout.strip())
    env = {"HOME": str(home), "PATH": str(bare)}
    out = subprocess.run([str(bare / "sh"), str(d / "bootstrap.sh"), str(d)], capture_output=True, text=True, env=env)
    assert out.returncode == 1 and "rsync is missing and could not be installed" in out.stderr


def test_a_local_source_is_sent_and_a_change_to_it_is_a_reason_to_init_again(home, tmp_path):
    from ouroloop.hosts import fingerprint, plan, source_id
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("x = 1\n")
    (src / "pkg" / "__pycache__").mkdir()
    (src / "pkg" / "__pycache__" / "junk.pyc").write_bytes(b"\0")
    options = {"source": str(src)}
    d = plan({"signature": options}, tmp_path / "init", hf=True)
    assert (d / "requirements.txt").read_text().splitlines() == ["./ouroloop.tar.gz", "./signature.tar.gz",
                                                                  "huggingface_hub"]
    import tarfile
    with tarfile.open(d / "signature.tar.gz") as t:
        assert t.getnames() == ["signature/pkg/__init__.py"]
    assert json.loads((d / "plan.json").read_text())["plugins"] == {"signature": {"source": fingerprint(src)}}
    m = {"ouroloop": {"code": code_version()}, "plugins": {"signature": {"source": source_id(options)}},
         "hf": {"hub": True, "token": True}}
    assert stale(m, {"signature": options}, hf=True) == []
    (src / "pkg" / "__init__.py").write_text("x = 2\n")
    assert stale(m, {"signature": options}, hf=True) == ["plugin signature is from another source"]
    assert "no Hugging Face token" in stale({**m, "hf": {"hub": True, "token": False}}, {}, hf=True)[0]
    url = "git+ssh://git@example.com/org/tde.git@v1"
    assert source_id({"source": url}) == url


# ---------------------------------------------------------------- the worker
def test_the_worker_refuses_a_job_made_by_another_ouroloop(tmp_path):
    d = tmp_path / "m_0001_x"
    d.mkdir()
    (d / "spec.json").write_text(json.dumps({"job": d.name, "trainer": "signature", "host": "u@gpu",
                                             "ouroloop": {"code": "0" * 16, "git": None}}))
    out = subprocess.run([sys.executable, "-m", "ouroloop.worker", "spec.json", d.name], cwd=d, capture_output=True,
                         text=True, env={**os.environ, "PYTHONPATH": str(CHECKOUT)})
    assert out.returncode == 3 and "run `ouroloop host init u@gpu`" in out.stdout
    assert not (d / "result.json").exists()


# ---------------------------------------------------------------- the plugin contract, end to end
@pytest.mark.parametrize("name", ["signature", "tde"])
def test_a_plugin_has_the_contract(name):
    plugin = load(name)
    assert plugin.NAME == name and isinstance(plugin.REQUIREMENTS, list)
    for fn in ("validate", "train", "backend"):
        assert callable(getattr(plugin, fn))
    with pytest.raises(ValueError, match="not a trainer plugin name"):
        load("../evil")


def test_the_signature_plugin_through_the_job_path_to_a_promoted_model(tmp_path):
    """spec.json -> python -m ouroloop.worker -> run -> result.json -> the backend by its ref -> calibration and the
    gate -> the champion answers new sessions as its ref."""
    jobs = tmp_path / "jobs"
    cfg = {**CFG, "models": {"jobs_root": str(jobs)},
           "trainer": {"type": "signature", "jobs_root": str(jobs), "poll_s": 0.05},
           "trainers": {"signature": {"signature": "test_hosts:says"}}}
    rt = config.build_runtime(cfg, tmp_path)
    collect(rt, 150)
    freeze_template(rt.root, ACT_GATE)
    trainer = config.build_trainer(cfg["trainer"], cfg["models"], cfg["trainers"])
    assert trainer.supports_weights
    out = trainer.dry_run(a_job(tmp_path / "ws", "m_0000"))
    assert out["check"] == {"ok": True, "counts": {"train": 5, "val": 1}}
    card = evolve_model(rt, trainer)
    job = card["job"]
    assert card["gate"] == "accepted" and card["model_id"] == job["id"]
    assert card["backend"] == {"type": "model", "model": job["id"], "trainer": "signature",
                               "options": {"signature": "test_hosts:says"}}
    assert job["result"]["plugin"] == {"name": "signature", "version": None}
    assert job["spec"]["ouroloop"]["code"] == code_version() and job["spec"]["trainer"] == "signature"
    assert sorted(p.name for p in (jobs / job["id"]).iterdir()) == ["exit", "log", "pid", "result.json", "run",
                                                                    "spec.json", "train.jsonl", "val.jsonl"]
    assert Registry(rt.root).champion()["version"] == card["version"] and card["calibration"]
    assert rt.ask("stop", ctx(), session="s_new", phase="per_turn").model == job["id"]


def test_a_trainer_checks_its_config_before_any_job():
    for kw, why in (({"models": None}, "needs a \\[models\\] table"),
                    ({"host": "u@gpu", "models": {"jobs_root": "j"}}, "stay there"),
                    ({"host": "u@gpu", "models": {"url": "http://gpu:8765"}, "jobs_root": "jobs"}, "jobs_root"),
                    ({"transport": "hf"}, "hf_prefix")):
        with pytest.raises(ValueError, match=why):
            JobTrainer("signature", **{"models": {"jobs_root": "j"}, **kw})
    with pytest.raises(ModuleNotFoundError):
        JobTrainer("nosuchplugin", models={"jobs_root": "j"})
    assert JobTrainer("signature", host="local", models={"jobs_root": "j"}).python == os.path.expanduser(
        "~/.ouroloop/venv/bin/python")
    assert MANIFEST == "~/.ouroloop/host.json"
