"""Training hosts (ouroloop/train/jobs/hosts/, bootstrap.sh) and trainer plugins through the real job path, on this
machine: host init and check against target "local" in a temporary home, pip stood in for (OUROLOOP_TEST_PIP), so
nothing is downloaded; the signature plugin from spec.json through `python -m ouroloop.train.jobs.worker` to a trained
artifact that serving loads."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jobs_fixtures import QUESTION, dataset
from ouroloop.train import config as train_config
from ouroloop.train.train import train_artifact
from ouroloop.train.jobs.trainer import CHECKOUT, JobTrainer
from ouroloop.train.train import DEFAULT_RECIPE, TrainError, TrainJob
from ouroloop.train.jobs.hosts import MANIFEST, read, stale
from ouroloop.release import code_version
from ouroloop.train.stores import kind
from ouroloop.train.cli import main
from ouroloop.train.trainers import load
from ouroloop.train.types import Question, Request
from ouroloop.workspace import Workspace

# pip, stood in for: it logs each call to <venv>/pip.log, and "installs" an archive by unpacking it and pointing a .pth
# file at it; of an sdist (an archive with a pyproject.toml) only what pip installs is kept: modules and the declared
# package-data. A requirement that is not an archive is taken as installed. STUB_PIP=fail fails every call.
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
        root = os.path.join(dest, top)
        if os.path.exists(os.path.join(root, "pyproject.toml")):
            import tomllib
            with open(os.path.join(root, "pyproject.toml"), "rb") as f:
                data = tomllib.load(f).get("tool", {}).get("setuptools", {}).get("package-data", {})
            keep = {os.path.join(*pkg.split("."), n) for pkg, names in data.items() for n in names}
            for base, _, names in os.walk(root):
                for n in names:
                    rel = os.path.relpath(os.path.join(base, n), root)
                    if os.sep in rel and not n.endswith(".py") and rel not in keep:
                        os.remove(os.path.join(base, n))
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
    assert m["stores"] == {"dir": {}, "hf": {"hub": False, "token": False}}
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


def installed(home: Path) -> Path:
    """The ouroloop package the stub pip installed into the venv."""
    (top,) = (home / ".ouroloop" / "venv" / "stub" / "ouroloop").iterdir()
    return top / "ouroloop"


def test_a_host_on_another_version_is_initialized_again_before_the_job(home, tmp_path):
    assert main(["host", "init", "local", "--trainer", "signature"]) == 0
    # the venv changed behind host.json's back (an editable install, a hand edit): what it runs now counts
    (installed(home) / "patched.py").write_text("x = 1\n")
    theirs = read("local")["ouroloop"]["code"]
    assert theirs != code_version() and json.loads((home / ".ouroloop" / "host.json").read_text())["ouroloop"][
        "code"] == code_version()
    assert stale(read("local"), {"signature": {}}) == [f"it runs ouroloop {theirs}, this is {code_version()}"]
    before = len(pip_calls(home))
    tr = JobTrainer("signature", host="local", jobs_root="~/jobs", poll_s=0.05)
    result = tr.train(a_job(tmp_path / "ws"))
    assert len(pip_calls(home)) == before + 2 and read("local")["ouroloop"]["code"] == code_version()
    job = result.job
    assert job["result"]["host"]["ouroloop"]["code"] == code_version()        # the host manifest, recorded
    assert (home / "jobs" / job["id"] / "run" / "model.json").exists()
    # the model, fetched from the store by its ref and loaded here through the plugin that trained it
    assert result.backend == {"type": "model", "model": job["id"], "trainer": "signature"}
    assert tr.serving.backend(result.backend).model == job["id"]


def test_a_host_that_cannot_be_initialized_fails_the_build_before_anything_is_used(home, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_PIP", "fail")
    jobs = tmp_path / "jobs"
    cfg = {"models": {"jobs_root": str(jobs)},             # an old config: [models] and no [store]
           "trainer": {"type": "signature", "host": "local", "jobs_root": str(jobs)}}
    rt = Workspace(tmp_path / "ws", serving=lambda: train_config.build_serving(cfg))
    trainer = train_config.build_trainer(cfg, rt.serving)
    built = train_artifact(rt, trainer, dataset(rt.root), version="m_0001")
    assert built.status == "failed" and built.model_ref is None and built.artifact_ref is None
    assert "host init on this machine failed" in built.errors[0] and "No matching distribution" in built.errors[0]
    assert not jobs.exists() and not (home / ".ouroloop" / "host.json").exists()
    with pytest.raises(TrainError, match="host init"):
        trainer.train(a_job(tmp_path / "ws2"))


def test_bootstrap_says_what_is_missing_when_it_cannot_install_it(home, tmp_path):
    from ouroloop.train.jobs.hosts import plan
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
    from ouroloop.train.jobs.hosts import fingerprint, plan, source_id
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("x = 1\n")
    (src / "pkg" / "__pycache__").mkdir()
    (src / "pkg" / "__pycache__" / "junk.pyc").write_bytes(b"\0")
    options = {"source": str(src)}
    d = plan({"signature": options}, tmp_path / "init", kind("hf"))
    assert (d / "requirements.txt").read_text().splitlines() == ["./ouroloop.tar.gz", "./signature.tar.gz",
                                                                  "huggingface_hub"]
    import tarfile
    with tarfile.open(d / "signature.tar.gz") as t:
        assert t.getnames() == ["signature/pkg/__init__.py"]
    assert json.loads((d / "plan.json").read_text())["plugins"] == {"signature": {"source": fingerprint(src),
                                                                                   "options": {}}}
    m = {"ouroloop": {"code": code_version()}, "plugins": {"signature": {"source": source_id(options)}},
         "stores": {"hf": {"hub": True, "token": True}}}
    assert stale(m, {"signature": options}, kind("hf")) == []
    (src / "pkg" / "__init__.py").write_text("x = 2\n")
    assert stale(m, {"signature": options}, kind("hf")) == ["plugin signature is from another source"]
    assert "no Hugging Face token" in stale({**m, "stores": {"hf": {"hub": True, "token": False}}}, {}, kind("hf"))[0]
    assert stale({**m, "stores": {}}, {}, kind("dir")) == []
    url = "git+ssh://git@example.com/org/tde.git@v1"
    assert source_id({"source": url}) == url


def test_a_broken_venv_or_a_failed_plugin_check_is_not_ready(home, tmp_path):
    import shutil
    assert main(["host", "init", "local", "--trainer", "signature"]) == 0
    assert stale(read("local"), {"signature": {}}) == []
    shutil.rmtree(home / ".ouroloop" / "venv")          # host.json stays, the venv is gone
    assert stale(read("local"), {"signature": {}}) == [f"it runs ouroloop none, this is {code_version()}"]
    m = {"ouroloop": {"code": code_version()}, "stores": {"hf": {"hub": True, "token": True}},
         "plugins": {"tde": {"source": None, "check": {"ok": False, "cuda": False, "why": "torch sees no CUDA"}}}}
    assert stale(m, {"tde": {}}) == ["plugin tde's check fails there: torch sees no CUDA"]
    m["plugins"]["tde"]["check"]["ok"] = True
    assert stale(m, {"tde": {}}) == []


def test_a_plugin_check_is_given_its_options_and_a_failing_one_is_recorded(monkeypatch):
    from ouroloop.train.jobs import hosts
    from ouroloop.train.trainers import signature
    seen = []

    def check(options):
        seen.append(options)
        raise ImportError("No module named 'torch'")

    monkeypatch.setattr(signature, "check", check, raising=False)
    m = hosts.manifest({"plugins": {"signature": {"source": None, "options": {"device": "cpu"}}}})
    assert seen == [{"device": "cpu"}]
    assert m["plugins"]["signature"]["check"] == {"ok": False, "why": "ImportError: No module named 'torch'"}
    assert stale(m, {"signature": {}})[-1].startswith("plugin signature's check fails there: ImportError")


def test_a_stray_file_in_the_package_is_neither_hashed_nor_sent(tmp_path, monkeypatch):
    """pip installs the modules and the declared package-data only: code_version and the sdist host init sends hold
    exactly those, so a .DS_Store or an editor's swap file never makes a host look like another ouroloop."""
    import shutil
    import tarfile
    import tomllib

    from ouroloop.train.jobs import hosts
    from ouroloop import release
    copy = tmp_path / "checkout"
    shutil.copytree(CHECKOUT / "ouroloop", copy / "ouroloop", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(CHECKOUT / "pyproject.toml", copy / "pyproject.toml")
    for module in (release, hosts):
        monkeypatch.setattr(module, "PACKAGE", copy / "ouroloop")
    monkeypatch.setattr(hosts, "CHECKOUT", copy)
    before = code_version()
    (copy / "ouroloop" / ".DS_Store").write_bytes(b"\0")
    (copy / "ouroloop" / "evolve" / ".train.py.swp").write_bytes(b"\0")
    (copy / "ouroloop" / "train" / "trainers" / "mytrainer.yaml").write_text("x: 1\n")
    assert code_version() == before
    with tarfile.open(hosts.pack_ouroloop(tmp_path)) as t:
        sent = {n.split("/", 1)[1] for n in t.getnames()}
    assert sent == {"pyproject.toml"} | {p.relative_to(copy).as_posix() for p in release.package_files()}
    assert "ouroloop/train/jobs/hosts/start.sh" in sent and not any(n.endswith((".DS_Store", ".swp", ".yaml")) for n in sent)
    # pyproject's package-data is PACKAGE_DATA: what pip installs besides the modules
    with open(CHECKOUT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)["tool"]["setuptools"]["package-data"]
    assert {f"{pkg.partition('.')[2].replace('.', '/')}/{n}" for pkg, names in data.items() for n in names} == set(
        release.PACKAGE_DATA)


def test_init_over_ssh_sends_one_tar_so_a_host_without_rsync_can_be_prepared(home, tmp_path):
    """user@box: the bundle goes through ssh's stdin, and bootstrap.sh (which installs rsync) runs from it."""
    from ouroloop.train.jobs.hosts import init
    calls = []

    def ssh_here(args, input=None, timeout=None):       # "ssh" to this machine: the command runs in a shell here
        calls.append(args)
        assert args[0] == "ssh", args                   # never rsync
        return subprocess.run(["sh", "-c", args[-1]], input=input, capture_output=True, text=True, check=True,
                              timeout=timeout, cwd=home, env={**os.environ, "HOME": str(home)})

    m, log = init("u@box", {"signature": {}}, runner=ssh_here)
    assert [a[0] for a in calls] == ["ssh", "ssh"]             # the init, then host.json
    assert m["ouroloop"]["code"] == code_version() and "signature" in m["plugins"] and "installing ouroloop" in log
    assert list((home / ".ouroloop" / "incoming").iterdir()) == []       # the bundle is removed after


def _bootstrap_tree(tmp_path, home):
    """A bootstrap plan and a PATH holding only what the script may use: its shell tools, a root `id`, an `apt-get`
    that logs and has no python3.12, and a stub python3 the caller then writes."""
    from ouroloop.train.jobs.hosts import plan
    d = plan({}, tmp_path / "init")
    bare = tmp_path / "bin"
    bare.mkdir()
    for tool in ("sh", "cat", "mkdir", "cp", "mv", "rsync", "git", "ps"):
        (bare / tool).symlink_to(subprocess.run(["which", tool], capture_output=True, text=True).stdout.strip())
    log = tmp_path / "apt.log"
    (bare / "id").write_text("#!/bin/sh\necho 0\n")                     # root
    (bare / "apt-get").write_text(f'#!/bin/sh\necho "$@" >> {log}\n'
                                  'case "$*" in *python3.12*) exit 100;; esac\n')     # jammy has no python3.12
    (bare / "env").write_text('#!/bin/sh\nshift; exec "$@"\n')            # env DEBIAN_FRONTEND=... apt-get ...
    for tool in ("id", "apt-get", "env"):
        (bare / tool).chmod(0o755)
    return d, bare, log


def _run_bootstrap(d, bare, home):
    return subprocess.run([str(bare / "sh"), str(d / "bootstrap.sh"), str(d)], capture_output=True, text=True,
                          env={"HOME": str(home), "PATH": str(bare)})


def test_bootstrap_tries_a_versioned_python_when_the_default_is_too_old(home, tmp_path):
    """Ubuntu 22.04's python3 is 3.10: bootstrap then installs python3.12, else python3.11, each on its own."""
    d, bare, log = _bootstrap_tree(tmp_path, home)
    (bare / "python3").write_text("#!/bin/sh\nexit 1\n")                 # too old
    (bare / "python3").chmod(0o755)
    out = _run_bootstrap(d, bare, home)
    installs = [line.split()[3:] for line in log.read_text().splitlines() if line.startswith("install")]
    assert installs == [["python3", "python3-venv"], ["python3.12", "python3.12-venv"],
                        ["python3.11", "python3.11-venv"]]
    assert out.returncode == 1 and "no usable Python >= 3.11 here" in out.stderr and "OUROLOOP_PYTHON" in out.stderr
    assert "does not run" in out.stderr                     # and it says what is wrong with the python3 that is here


def test_the_bootstrap_says_what_is_wrong_with_the_python_that_is_here(home, tmp_path):
    """A host whose python3 is new enough but has no pip (Debian splits it into python3-venv) once got "no Python
    >= 3.11 with venv here", which sends a person to install a Python they already have. The message names what is
    actually missing."""
    d, bare, _ = _bootstrap_tree(tmp_path, home)
    (bare / "python3").write_text("#!/bin/sh\n"
                                  'case "$*" in\n'
                                  '  *ensurepip*) exit 1;;\n'             # venv is there, pip is not
                                  '  *print*) echo 3.14; exit 0;;\n'
                                  'esac\nexit 0\n')
    (bare / "python3").chmod(0o755)
    out = _run_bootstrap(d, bare, home)
    assert out.returncode == 1 and "is 3.14 with venv but no pip" in out.stderr



def test_a_signature_run_says_what_it_is_so_a_model_server_loads_the_same_model(tmp_path):
    """The key function travels in the run, not in the options: a server without [trainers.signature] (options {})
    loads the model the job trained."""
    from ouroloop.train.trainers import signature
    job = tmp_path / "jobs" / "m_0001_x"
    job.mkdir(parents=True)
    example = {"id": "e", "state": "say yes", "question": "done?", "candidates": [{"name": "yes"}, {"name": "no"}],
               "target": [1.0, 0.0], "meta": {"weight": 1.0}}
    (job / "train.jsonl").write_text(json.dumps(example) + "\n")
    signature.train({"options": {"signature": "test_hosts:says"}}, str(job), 0)
    for options in ({}, {"signature": "ouroloop.train.trainers.signature:signature"}):     # the server's own options
        backend = signature.backend(str(job / "run"), "m_0001_x", options)
        assert backend.signature is says and backend.model == "m_0001_x"
    old = tmp_path / "jobs" / "m_0000_x" / "run"            # a run from before: the table alone, options say the key
    old.mkdir(parents=True)
    (old / "model.json").write_text(json.dumps({"done?": {"yes": {"yes": 1.0}}}))
    backend = signature.backend(str(old), "m_0000_x", {"signature": "test_hosts:says"})
    assert backend.signature is says and backend.table == {"done?": {"yes": {"yes": 1.0}}}


# ---------------------------------------------------------------- the worker
def test_the_worker_refuses_a_job_made_by_another_ouroloop(tmp_path):
    d = tmp_path / "m_0001_x"
    d.mkdir()
    (d / "spec.json").write_text(json.dumps({"job": d.name, "trainer": "signature", "host": "u@gpu",
                                             "ouroloop": {"code": "0" * 16, "git": None}}))
    out = subprocess.run([sys.executable, "-m", "ouroloop.train.jobs.worker", "spec.json", d.name], cwd=d, capture_output=True,
                         text=True, env={**os.environ, "PYTHONPATH": str(CHECKOUT)})
    assert out.returncode == 3 and "run `python -m ouroloop.train host init u@gpu`" in out.stdout
    assert not (d / "result.json").exists()


def test_a_dir_store_takes_the_jobs_run_directory_and_copies_it_to_its_root_once(tmp_path):
    """The ref is the job id, its run <root>/<job id>/run: a plugin that trained elsewhere fails the job; a root
    other than the jobs' directory gets a copy of the run."""
    pkg = tmp_path / "plugins" / "elsewhere"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "trainer.py").write_text(
        "import os\n"
        "def validate(spec, job_dir): return {'ok': True}\n"
        "def train(spec, job_dir, deadline):\n"
        "    run = os.path.join(os.path.dirname(job_dir), 'elsewhere')\n"
        "    os.makedirs(os.path.join(job_dir, 'run'), exist_ok=True)\n"
        "    open(os.path.join(job_dir, 'run', 'model'), 'w').close()\n"
        "    os.makedirs(run, exist_ok=True)\n"
        "    return run if spec['job'].startswith('m_0001') else os.path.join(job_dir, 'run')\n"
        "def backend(run, ref, options): raise NotImplementedError\n")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(CHECKOUT), str(tmp_path / "plugins")])}
    models = tmp_path / "models"
    for job, code, store in (("m_0001_x", 1, None), ("m_0002_x", 0, None),
                             ("m_0003_x", 0, {"type": "dir", "root": str(models)})):
        d = tmp_path / "jobs" / job
        d.mkdir(parents=True)
        (d / "spec.json").write_text(json.dumps({"job": job, "trainer": "elsewhere.trainer", "store": store,
                                                 "deadline_s": 60, "ouroloop": {"code": code_version(), "git": None}}))
        out = subprocess.run([sys.executable, "-m", "ouroloop.train.jobs.worker", "spec.json", job], cwd=d, capture_output=True,
                             text=True, env=env)
        assert out.returncode == code, out.stdout + out.stderr
        assert (d / "result.json").exists() == (code == 0)
        assert ("its run is <job>/run" in out.stdout) == (code == 1)
        if code == 0:
            assert json.loads((d / "result.json").read_text())["model"] == job
    assert (models / "m_0003_x" / "run" / "model").exists() and not (models / "m_0002_x").exists()


# ---------------------------------------------------------------- the plugin contract, end to end
@pytest.mark.parametrize("name", ["signature", "tde"])
def test_a_plugin_has_the_contract(name):
    plugin = load(name)
    assert plugin.NAME == name and isinstance(plugin.REQUIREMENTS, list)
    for fn in ("validate", "train", "backend"):
        assert callable(getattr(plugin, fn))
    with pytest.raises(ValueError, match="not a trainer plugin name"):
        load("../evil")


def test_the_signature_plugin_through_the_job_path_to_a_trained_artifact(tmp_path):
    """spec.json -> python -m ouroloop.train.jobs.worker -> run -> result.json -> the backend by its ref -> the load probe, and
    the model answers as its ref with what it learned (its key function: test_hosts:says)."""
    jobs = tmp_path / "jobs"
    cfg = {"store": {"type": "dir", "root": str(jobs)},
           "trainer": {"type": "signature", "jobs_root": str(jobs), "poll_s": 0.05},
           "trainers": {"signature": {"signature": "test_hosts:says"}}}
    rt = Workspace(tmp_path / "ws", serving=lambda: train_config.build_serving(cfg))
    trainer = train_config.build_trainer(cfg, rt.serving)
    assert trainer.supports_weights
    out = trainer.dry_run(a_job(tmp_path / "ws", "m_0000"))
    assert out["check"] == {"ok": True, "counts": {"train": 5, "val": 1}}
    built = train_artifact(rt, trainer, dataset(rt.root), version="m_0001")
    job = built.job_ref
    assert built.status == "ready" and built.errors == () and built.model_ref == job["id"]
    assert built.backend_spec == {"type": "model", "model": job["id"], "trainer": "signature",
                                  "options": {"signature": "test_hosts:says"}}
    assert job["result"]["plugin"] == {"name": "signature", "version": None}
    assert job["spec"]["ouroloop"]["code"] == code_version() and job["spec"]["trainer"] == "signature"
    assert sorted(p.name for p in (jobs / job["id"]).iterdir()) == ["exit", "log", "pid", "result.json", "run",
                                                                    "spec.json", "train.jsonl", "val.jsonl"]
    model = rt.serving.backend(built.backend_spec)
    yes, no = model.decide([Request(f"task: t1\nsay {label}", Question("noul", QUESTION)) for label in ("yes", "no")])
    assert (yes.model, yes.label, no.label) == (job["id"], "yes", "no") and yes.confidence > 0.9


def test_a_trainer_checks_its_config_before_any_job():
    for kw, why in (({"host": "u@gpu"}, "stay there"),
                    ({"host": "u@gpu", "store": {"type": "hf", "prefix": "o/p-"}, "jobs_root": "jobs"}, "jobs_root"),
                    ({"store": {"type": "hf"}}, "needs prefix")):
        with pytest.raises(ValueError, match=why):
            JobTrainer("signature", **kw)
    with pytest.raises(ModuleNotFoundError):
        JobTrainer("nosuchplugin")
    assert JobTrainer("signature", host="local").python == os.path.expanduser("~/.ouroloop/venv/bin/python")
    assert MANIFEST == "~/.ouroloop/host.json"
