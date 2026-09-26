"""The training job path (docs/DESIGN.md §2.2, pipeline 2): JobTrainer -> Compute -> python -m ouroloop.worker -> the
tde plugin -> TDE, on this machine against a stub TDE checkout, and over a stand-in for ssh; model refs served
in-process and through `ouroloop serve`; Hugging Face through a stand-in package. Nothing trains, nothing leaves this
machine. Hosts and the signature plugin end to end: test_hosts.py."""
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import types
from collections import OrderedDict
from pathlib import Path

import httpx
import pytest

from ouroloop import config
from ouroloop.backends import KeywordBackend, TdeBackend
from ouroloop.backends.http import HttpBackend
from ouroloop.evolve import compute as compute_module
from ouroloop.evolve.compute import SEP, Compute
from ouroloop.evolve.gate import freeze_template, records
from ouroloop.evolve.pipeline import check_change, evolve_model, recipe_evaluator, research_context
from ouroloop.evolve.registry import Registry
from ouroloop.evolve.train import CHECKOUT, DEFAULT_RECIPE, JobTrainer, TrainError, TrainJob
from ouroloop.release import code_version
from ouroloop.research import Candidate
from ouroloop.runtime import Runtime
from ouroloop.serve import make_server
from ouroloop.serving import Serving
from ouroloop.stores import make as make_store
from ouroloop.trainers.signature import SignatureTrainer
from ouroloop.types import Question, Request
from test_gate_record import ACT_GATE, CFG, collect, ctx

pytestmark = pytest.mark.usefixtures("loose_risk")

STUB_TRAIN = """
import json, os, pathlib, sys, time
class TrainConfig:
    def __init__(self, **kw):
        self.__dict__.update(kw)
def train(cfg):
    mode = os.environ.get("STUB_TDE", "")
    if mode == "fail":
        print("CUDA out of memory", flush=True)
        sys.exit(1)
    if mode == "hang":                  # until stopped, or until the worker that started it is gone
        parent = os.getppid()
        while os.getppid() == parent:
            time.sleep(0.05)
        sys.exit(0)
    if mode == "hold":                  # until stopped, whatever becomes of the worker; its pid in STUB_PID
        pathlib.Path(os.environ["STUB_PID"]).write_text(str(os.getpid()))
        time.sleep(60)
        sys.exit(0)
    out = pathlib.Path(cfg.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    n = sum(1 for _ in open(pathlib.Path(cfg.data_dir) / "train.jsonl"))
    (out / "config.json").write_text(json.dumps({**cfg.__dict__, "n_train": n}))
    (out / "best.pt").write_bytes(b"")
    return {"n_train": n}
"""
STUB_SCHEMA = """
import json, os, time
class Example:
    def __init__(self, d):
        self.d = d
    def validate(self):
        assert len(self.d["candidates"]) == len(self.d["target"])
def load_jsonl(path):
    if os.environ.get("STUB_TDE") == "hang_validate":
        time.sleep(60)
    return [Example(json.loads(line)) for line in open(path) if line.strip()]
"""
FAKE_HF = """
import hashlib, json, os, shutil
ROOT = os.environ["FAKE_HF"]
class CommitOperationAdd:
    def __init__(self, path_in_repo, path_or_fileobj):
        self.path_in_repo, self.path_or_fileobj = path_in_repo, path_or_fileobj
class HfApi:
    def create_repo(self, repo_id, private=False, exist_ok=False):
        os.makedirs(os.path.join(ROOT, repo_id), exist_ok=exist_ok)
        with open(os.path.join(ROOT, repo_id, "repo.json"), "w") as f:
            json.dump({"private": private}, f)
    def create_commit(self, repo_id, operations, commit_message):
        h = hashlib.sha1(commit_message.encode())
        for op in operations:
            h.update(op.path_in_repo.encode() + open(op.path_or_fileobj, "rb").read())
        sha = h.hexdigest()
        for op in operations:
            dst = os.path.join(ROOT, repo_id, sha, op.path_in_repo)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(op.path_or_fileobj, dst)
        return type("CommitInfo", (), {"oid": sha})()
def snapshot_download(repo_id, revision):
    path = os.path.join(ROOT, repo_id, revision)
    if not os.path.isdir(path):
        raise FileNotFoundError(path)
    return path
"""
EXAMPLE = json.dumps({"id": "e", "candidates": [{"name": "yes"}, {"name": "no"}], "target": [1.0, 0.0]})


def git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A TDE checkout that trains nothing, importable by the jobs (PYTHONPATH): its tde.train writes a run from the
    config (STUB_TDE=fail exits 1, STUB_TDE=hang runs until stopped or its worker is gone, STUB_TDE=hold until
    stopped), its tde.schema validates what the job prepared (STUB_TDE=hang_validate: for a minute). A git
    repository, with a parent run and replay data."""
    r = tmp_path / "tde"
    for path, text in {"tde/__init__.py": "", "tde/schema.py": STUB_SCHEMA, "tde/train.py": STUB_TRAIN,
                       "runs/base/config.json": json.dumps({"backbone": "gliclass"}),
                       "data/replay.jsonl": "".join(EXAMPLE + "\n" for _ in range(500))}.items():
        (r / path).parent.mkdir(parents=True, exist_ok=True)
        (r / path).write_text(text)
    git(r, "init", "-q")
    git(r, "add", ".")
    git(r, "commit", "-qm", "stub")
    monkeypatch.setenv("PYTHONPATH", str(r))
    return r


@pytest.fixture
def decider(monkeypatch) -> list:
    """TDE's Decider stood in for in this process: it reads the label from the state ("say yes"), p = 0.9. -> the
    runs it loaded."""
    loads = []

    class Decider:
        @classmethod
        def from_run(cls, run, device=None):
            loads.append(str(run))
            return cls()

        def decide_batch(self, state, questions):
            label = "yes" if "say yes" in state else "no"
            probs = {label: 0.9, "no" if label == "yes" else "yes": 0.1}
            return {k: {"probabilities": probs} for k in questions}

    tde, inference = types.ModuleType("tde"), types.ModuleType("tde.inference")
    tde.__spec__, inference.Decider = importlib.machinery.ModuleSpec("tde", None), Decider
    monkeypatch.setitem(sys.modules, "tde", tde)
    monkeypatch.setitem(sys.modules, "tde.inference", inference)
    monkeypatch.setattr(TdeBackend, "_loaded", OrderedDict())
    return loads


def trainer(repo, jobs, **kw) -> JobTrainer:
    """The tde plugin as a job on this machine, under this Python; the first parent is the stub's runs/base."""
    return JobTrainer("tde", kw.pop("options", {"init_from": str(repo / "runs" / "base")}), jobs_root=str(jobs),
                      poll_s=kw.pop("poll_s", 0.05), **kw)


def a_job(root, version="m_0001", parent=None, **recipe) -> TrainJob:
    """A job as the pipeline hands it over: its data in <workspace>/models/<version>/data."""
    data = Path(root) / "models" / version / "data"
    data.mkdir(parents=True)
    (data / "train.jsonl").write_text((EXAMPLE + "\n") * 5)
    (data / "val.jsonl").write_text(EXAMPLE + "\n")
    return TrainJob(version, data, {**DEFAULT_RECIPE, **recipe}, parent)


def gone(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


def held(path: Path) -> int:
    """The pid STUB_TDE=hold wrote: TDE's, the leader of its own process group."""
    for _ in range(200):
        if path.exists() and path.read_text():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError("TDE never started")


def left(tmp_path, jobs, name, tag) -> Path:
    """A job of workspace `tag` whose worker sleeps a minute (a "python" that does), started by a process that is
    gone by now, as a killed control plane leaves its jobs."""
    sleeper = tmp_path / "left" / "python"
    sleeper.parent.mkdir(exist_ok=True)
    sleeper.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    sleeper.chmod(0o755)
    spec = tmp_path / "left" / name / "spec.json"
    spec.parent.mkdir()
    spec.write_text(json.dumps({"workspace": tag}))
    subprocess.run([sys.executable, "-c", "import sys; from ouroloop.evolve.compute import Compute; "
                    "Compute(None, sys.argv[1]).launch(sys.argv[2], sys.argv[3:-1], sys.argv[-1])",
                    str(jobs), name, str(spec), str(sleeper)], check=True, cwd=CHECKOUT)
    return jobs / name


def on_this_machine(args, input=None, timeout=None):
    """ssh to a host that is this machine: the command runs in a local shell."""
    if args[0] != "ssh":
        raise AssertionError(args)
    return subprocess.run(["sh", "-c", args[-1]], input=input, capture_output=True, text=True, check=True)


# ---------------------------------------------------------------- a round, end to end
def test_an_evolve_round_trains_a_job_and_the_next_one_starts_from_its_model(tmp_path, repo, decider):
    jobs = tmp_path / "jobs"
    base = str(repo / "runs" / "base")
    cfg = {**CFG, "store": {"type": "dir", "root": str(jobs)},
           "trainer": {"type": "tde", "jobs_root": str(jobs), "poll_s": 0.05}, "trainers": {"tde": {"init_from": base}}}
    rt = config.build_runtime(cfg, tmp_path)
    collect(rt, 150)
    freeze_template(rt.root, ACT_GATE)
    tde = config.build_trainer(cfg, rt.serving)
    recipe = {"replay": str(repo / "data" / "replay.jsonl"), "replay_ratio": 0.5, "lr_head": 0.000123}
    first = evolve_model(rt, tde, recipe)
    job = first["job"]
    assert first["gate"] == "accepted" and Registry(rt.root).champion()["version"] == first["version"]
    assert first["model_id"] == job["id"]
    assert first["backend"] == {"type": "model", "model": job["id"], "trainer": "tde", "options": {"init_from": base}}
    assert "run" not in first and job["host"] is None and job["id"].startswith("m_0001_")
    result = job["result"]
    assert (result["model"], result["ouroloop"]) == (job["id"], code_version()) and job["spec"]["store"] == {
        "type": "dir", "root": str(jobs)}
    assert result["plugin"] == {"name": "tde", "version": f"git {git(repo, 'rev-parse', 'HEAD').strip()}"}
    assert result["host"]["gpu"] == [] and result["host"]["python"]
    spec = job["spec"]
    assert spec["workspace"] == str(rt.root.resolve()) and spec["recipe"]["lr_head"] == 0.000123
    assert spec["ouroloop"]["code"] == code_version() and spec["parent"] == base
    # the job's directory: the three files it was given, and what the job wrote
    d = jobs / job["id"]
    assert sorted(p.name for p in d.iterdir()) == ["exit", "log", "pid", "result.json", "run", "spec.json", "tde",
                                                   "train.jsonl", "val.jsonl"]
    assert (d / "exit").read_text().strip() == "0"
    run = json.loads((d / "run" / "config.json").read_text())
    n = sum(1 for _ in open(rt.root / "models" / "m_0001" / "data" / "train.jsonl"))
    assert run["init_from"] == str(repo / "runs" / "base") and run["backbone"] == "gliclass"
    assert run["n_train"] == n + int(n * 0.5) and run["lr_head"] == 0.000123
    # the champion answers new sessions as its ref, loaded from its run
    d_new = rt.ask("stop", ctx(), session="s_new", phase="per_turn")
    assert d_new.model == job["id"] and str(d / "run") in decider

    # the next round starts from the champion's model, and records that the TDE checkout changed
    (repo / "tde" / "train.py").write_text(STUB_TRAIN + "# changed\n")
    collect(rt, 40, start=150)
    second = evolve_model(rt, tde, recipe)
    assert second["parent"] == first["version"] and second["job"]["spec"]["parent"] == job["id"]
    assert second["job"]["result"]["plugin"]["version"].endswith("-dirty")
    run = json.loads((jobs / second["job"]["id"] / "run" / "config.json").read_text())
    assert run["init_from"] == str(d / "run") and run["backbone"] == "gliclass"


# ---------------------------------------------------------------- failures are TrainError: inconclusive
def test_a_failed_job_leaves_the_round_inconclusive_and_the_record_unused(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("STUB_TDE", "fail")
    rt = config.build_runtime({**CFG, "store": {"type": "dir", "root": str(tmp_path / "jobs")}}, tmp_path)
    collect(rt, 150)
    freeze_template(rt.root, ACT_GATE)
    card = evolve_model(rt, trainer(repo, tmp_path / "jobs"))
    (record,) = records(rt.root)
    assert card["gate"] == "inconclusive" and not record.used and "model_id" not in card
    assert "exit 1" in card["reasons"][0] and "CUDA out of memory" in card["reasons"][0]   # the end of its log


def test_a_job_that_hangs_is_stopped_at_its_deadline(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("STUB_TDE", "hold")
    monkeypatch.setenv("STUB_PID", str(tmp_path / "tde.pid"))
    start = time.time()
    with pytest.raises(TrainError, match="exit 124") as e:
        trainer(repo, tmp_path / "jobs", deadline_s=1, poll_s=0.5).train(a_job(tmp_path / "ws"))
    assert "ran out of time" in str(e.value) and time.time() - start < 10
    (d,) = (tmp_path / "jobs").iterdir()
    assert gone(int((d / "pid").read_text())) and gone(held(tmp_path / "tde.pid"))     # the worker stopped TDE


def test_hf_keeps_publish_s_of_the_deadline_for_the_commit(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("STUB_TDE", "hold")
    monkeypatch.setenv("STUB_PID", str(tmp_path / "tde.pid"))
    tde = trainer(repo, tmp_path / "jobs", deadline_s=600, publish_s=600, store={"type": "hf", "prefix": "org/o-"})
    start = time.time()
    with pytest.raises(TrainError, match="exit 124"):          # no time left to train: all of it is the commit's
        tde.train(a_job(tmp_path / "ws"))
    assert time.time() - start < 10


def test_a_job_stuck_before_training_is_cancelled_at_the_trainers_deadline(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("STUB_TDE", "hang_validate")          # the worker's own deadline covers only training
    with pytest.raises(TrainError, match="passed its deadline"):
        trainer(repo, tmp_path / "jobs", deadline_s=1, poll_s=0.2).train(a_job(tmp_path / "ws"))
    (d,) = (tmp_path / "jobs").iterdir()
    assert (d / "exit").read_text().strip() == "143" and gone(int((d / "pid").read_text()))


def test_a_job_killed_without_an_exit_code_is_lost_at_once(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("STUB_TDE", "hang")
    jobs = tmp_path / "jobs"

    def kill_it(seconds):                   # kill -9 of the job's process group, as a reboot would
        (d,) = jobs.iterdir()
        os.killpg(int((d / "pid").read_text()), signal.SIGKILL)
        time.sleep(0.05)

    start = time.time()
    with pytest.raises(TrainError, match="lost"):
        trainer(repo, jobs, deadline_s=3600, sleep=kill_it).train(a_job(tmp_path / "ws"))
    assert time.time() - start < 10         # not at the deadline


def test_an_interrupted_wait_cancels_the_job(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("STUB_TDE", "hold")
    monkeypatch.setenv("STUB_PID", str(tmp_path / "tde.pid"))
    jobs = tmp_path / "jobs"

    def interrupt(seconds):                 # once TDE is training
        held(tmp_path / "tde.pid")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        trainer(repo, jobs, sleep=interrupt).train(a_job(tmp_path / "ws"))
    (d,) = jobs.iterdir()
    assert gone(int((d / "pid").read_text())) and (d / "exit").read_text().strip() == "143"
    assert gone(held(tmp_path / "tde.pid"))                          # the worker stopped TDE on its way out
    assert Compute(None, str(jobs)).poll(d.name).code == 143


def test_the_first_job_cancels_what_a_killed_run_of_the_same_workspace_left(tmp_path, repo):
    jobs, ws = tmp_path / "jobs", tmp_path / "ws"
    mine, theirs = left(tmp_path, jobs, "old_mine", str(ws.resolve())), left(tmp_path, jobs, "old_theirs", "other")
    compute = Compute(None, str(jobs))
    assert sorted(compute.unfinished(str(ws.resolve()))) == ["old_mine"]
    trainer(repo, jobs).dry_run(a_job(ws, "m_0000"))       # a dry run cancels nothing: it may run beside a live job
    assert compute.poll("old_mine").state == "running"
    result = trainer(repo, jobs).train(a_job(ws))
    try:
        assert result.model_id not in ("old_mine", "old_theirs")
        assert (mine / "exit").read_text().strip() == "143" and gone(int((mine / "pid").read_text()))
        assert not (theirs / "exit").exists() and compute.poll("old_theirs").state == "running"
    finally:
        compute.cancel("old_theirs")
    assert compute.poll("old_theirs").state == "exited"


@pytest.mark.parametrize("over_ssh", [False, True])
def test_a_job_whose_leader_alone_was_killed_is_lost_and_its_cancel_stops_the_rest(tmp_path, over_ssh):
    jobs = tmp_path / "jobs"
    d = left(tmp_path, jobs, "old", "ws")
    pid = int((d / "pid").read_text())
    while not (d / "log").exists():                     # the leader has started the worker
        time.sleep(0.01)
    os.kill(pid, signal.SIGKILL)                        # the leader only: the worker stays in its group
    compute = Compute("u@here", str(jobs), runner=on_this_machine) if over_ssh else Compute(None, str(jobs))
    for _ in range(100):
        if compute.poll("old").state == "lost":
            break
        time.sleep(0.05)
    assert compute.poll("old").state == "lost" and not gone(pid)
    compute.cancel("old", grace=0.5)
    assert gone(pid) and (d / "exit").read_text().strip() == "143"


def test_a_recorded_pid_that_now_leads_another_job_is_not_this_job(tmp_path):
    jobs = tmp_path / "jobs"
    other = left(tmp_path, jobs, "other", "ws")
    stale = jobs / "stale"                              # its leader died in a reboot; its pid went to `other`
    stale.mkdir()
    (stale / "spec.json").write_text(json.dumps({"workspace": "ws"}))
    (stale / "pid").write_text((other / "pid").read_text())
    for compute in (Compute(None, str(jobs)), Compute("u@here", str(jobs), runner=on_this_machine)):
        assert compute.poll("stale").state == "lost" and compute.poll("other").state == "running"
    Compute(None, str(jobs)).cancel("stale")
    Compute("u@here", str(jobs), runner=on_this_machine).cancel("stale", grace=0.1)
    compute = Compute(None, str(jobs))
    assert compute.poll("other").state == "running" and compute.poll("stale").code == 143
    compute.cancel("other")
    assert gone(int((other / "pid").read_text()))


def test_a_dry_run_is_the_job_up_to_training(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("STUB_TDE", "fail")                  # it never gets to TDE's trainer
    out = trainer(repo, tmp_path / "jobs").dry_run(a_job(tmp_path / "ws", replay=str(repo / "data/replay.jsonl")))
    assert out["check"] == {"ok": True, "counts": {"train": 10, "calibration": 1}, "backbone": "gliclass",
                            "parent_checkpoint": False, "parent": str(repo / "runs" / "base")}
    assert not (tmp_path / "jobs" / out["job"] / "run").exists()
    with pytest.raises(TrainError, match="a path on the host is absolute"):       # no checkout to be relative to
        trainer(repo, tmp_path / "jobs").dry_run(a_job(tmp_path / "ws", "m_0002", replay="data/replay.jsonl"))


def test_a_job_without_a_parent_is_refused(tmp_path, repo):
    job = a_job(tmp_path / "ws", parent=None)
    with pytest.raises(ValueError, match="no parent"):
        trainer(repo, tmp_path / "jobs", options={}).train(job)
    assert not (tmp_path / "jobs").exists()


def test_a_parent_job_resolves_in_the_jobs_directory_whatever_the_paths_were_relative_to(tmp_path, repo,
                                                                                          monkeypatch):
    monkeypatch.chdir(tmp_path)
    tde = JobTrainer("tde", {"init_from": str(repo / "runs" / "base")}, jobs_root="jobs", poll_s=0.05)
    first = tde.train(a_job(tmp_path / "ws"))
    out = tde.dry_run(a_job(tmp_path / "ws", "m_0002", parent=first.model_id))
    assert out["check"]["parent"] == str(tmp_path / "jobs" / first.model_id / "run")
    with pytest.raises(TrainError, match="no run"):             # a job id without a run is not a path
        tde.dry_run(a_job(tmp_path / "ws", "m_0003", parent="m_0001_gone"))


def test_a_trainer_checks_how_its_models_are_stored_and_served_before_any_job(tmp_path):
    host = {"host": "u@gpu", "runner": Host()}
    url = {"serving": {"url": "http://gpu:8765"}}
    for kw, why in (({"serving": {"url": "http://gpu:8765", "port": 1}}, "may only set"),
                    ({"store": {"type": "s3"}}, '"dir" or "hf"'),
                    ({"store": {"type": "hf"}}, "needs prefix"),
                    ({"store": {"type": "dir", "jobs_root": "j"}}, "may only set"),
                    (host, "stay there"),
                    ({**host, "store": {"type": "dir", "root": "~/.ouroloop/jobs"}}, "stay there"),
                    ({**host, **url, "jobs_root": "jobs"}, "jobs_root 'jobs'"),
                    ({**host, **url, "store": {"type": "dir", "root": "models"}}, "root 'models'")):
        with pytest.raises(ValueError, match=why):
            JobTrainer("tde", **kw)
    JobTrainer("tde", **host, **url)                                            # a model server on the host
    JobTrainer("tde", **host, store={"type": "hf", "prefix": "o/p-"})          # loaded here, from the Hub
    # a host over ssh runs the jobs under the venv host init made there
    assert JobTrainer("tde", **host, **url).python == "~/.ouroloop/venv/bin/python"


# ---------------------------------------------------------------- over ssh
class Host:
    """Stands in for ssh and rsync to a host whose home is /home/u, prepared by host init for this ouroloop and the
    tde plugin. Status reads fail `drop` times first."""

    def __init__(self, drop: int = 2, fail_launch: bool = False, fail_cancel: bool = False):
        self.commands, self.drop, self.polls, self.job = [], drop, 0, None
        self.fail_launch, self.fail_cancel = fail_launch, fail_cancel

    def __call__(self, args, input=None, timeout=None):
        self.commands.append(args)
        out = ""
        command = args[-1] if args[0] == "ssh" else ""
        if "/.ouroloop/host.json" in command:
            out = json.dumps({"ouroloop": {"code": code_version()}, "plugins": {"tde": {"source": None}}})
        elif 'echo "$HOME"' in command:
            out = "/home/u\n"
        elif command.startswith("mkdir"):
            self.job = re.search(r"(m_0001_[0-9a-f]+)", command).group(1)
        elif "setsid" in command and self.fail_launch:
            raise subprocess.CalledProcessError(255, args, stderr="Connection reset by peer")
        elif "kill -s TERM" in command and self.fail_cancel:
            raise subprocess.CalledProcessError(255, args, stderr="Connection reset by peer")
        elif "ps -ww -o args=" in command and "echo exited" in command:
            self.polls += 1
            if self.polls <= self.drop:
                raise subprocess.CalledProcessError(255, args, stderr="ssh: connect to host gpu: timed out")
            out = "running\n" if self.polls == self.drop + 1 else "exited\n"
        elif command.startswith("cd") and SEP in command:
            result = {"model": self.job, "plugin": {"name": "tde", "version": "0.1.1"}, "hf_commit": None}
            out = f"0\n{SEP}\n{json.dumps(result)}\n\n{SEP}\nworker: done\n"
        return subprocess.CompletedProcess(args, 0, out, "")


def test_over_ssh_status_reads_may_fail_and_no_spec_value_reaches_a_shell(tmp_path):
    host = Host(drop=2)
    tde = JobTrainer("tde", {"init_from": "~/tde/runs/base"}, host="user@gpu", runner=host, sleep=lambda s: None,
                     serving={"url": "http://127.0.0.1:9"})
    job = a_job(tmp_path / "ws", parent="runs/base", lr_head=0.000123)
    result = tde.train(job)
    assert result.model_id == host.job and result.job["host"] == "user@gpu" and host.polls == 4
    ssh = [a[-1] for a in host.commands if a[0] == "ssh"]
    launch = next(c for c in ssh if "setsid" in c)
    assert f"jobs/{host.job} " in launch and "setsid nohup sh -c" in launch and "-m ouroloop.worker spec.json" in launch
    assert '"$HOME"/.ouroloop/venv/bin/python' in launch        # the worker host init installed there
    assert launch.split(" > /dev/null")[0].endswith(f" {host.job}")    # the job id marks its processes
    for a in host.commands:                             # a dead peer is noticed in about a minute
        ssh = a if a[0] == "ssh" else shlex.split(a[a.index("-e") + 1])
        assert "ConnectTimeout=20" in ssh and "ServerAliveInterval=15" in ssh
    assert '"$HOME"' in launch                           # ~ is the host's home, not this machine's
    pushed = sorted(a[-1] for a in host.commands if a[0] == "rsync")
    assert pushed == [f"user@gpu:/home/u/.ouroloop/jobs/{host.job}/{n}" for n in ("spec.json", "train.jsonl",
                                                                                  "val.jsonl")]
    shell = " ".join(ssh + pushed)          # what a shell on either side reads: spec values only reach it as a file
    for value in (str(tmp_path), "runs/base", "0.000123"):
        assert value not in shell


def test_a_job_whose_launch_failed_is_cancelled_or_left_to_the_next_sweep(tmp_path):
    for fail_cancel in (False, True):
        host = Host(fail_launch=True, fail_cancel=fail_cancel)
        ws = tmp_path / f"ws{fail_cancel}"
        tde = JobTrainer("tde", {"init_from": "~/tde/runs/base"}, host="user@gpu", runner=host,
                         sleep=lambda s: None, serving={"url": "http://127.0.0.1:9"})
        with pytest.raises(TrainError, match="could not start.*Connection reset"):
            tde.train(a_job(ws))
        assert any("kill -s TERM" in a[-1] for a in host.commands)          # it may have started
        swept = ("user@gpu", "~/.ouroloop/jobs", str(ws.resolve())) in JobTrainer._swept
        assert swept is not fail_cancel


def test_an_ssh_command_has_a_time_limit(monkeypatch):
    seen = []
    monkeypatch.setattr(compute_module.subprocess, "run",
                        lambda args, **kw: seen.append((args[0], kw)) or subprocess.CompletedProcess(args, 0, "", ""))
    compute_module.Remote("u@gpu", "-o ConnectTimeout=5").ssh("true")
    compute_module.Remote("u@gpu").push("a", "b")
    assert seen[0][1]["timeout"] == compute_module.SSH_TIMEOUT_S and seen[1][1]["timeout"] is None
    assert compute_module.Remote("u@gpu", "-o ConnectTimeout=5").ssh_opts[:2] == ["-o", "ConnectTimeout=5"]


# ---------------------------------------------------------------- Hugging Face
def test_hf_commits_the_run_once_and_the_ref_loads_at_that_commit(tmp_path, repo, decider, monkeypatch):
    fake = tmp_path / "fake_hf"
    (fake / "huggingface_hub").mkdir(parents=True)
    (fake / "huggingface_hub" / "__init__.py").write_text(FAKE_HF)
    monkeypatch.setenv("FAKE_HF", str(tmp_path / "hub"))
    monkeypatch.setenv("PYTHONPATH", f"{fake}{os.pathsep}{repo}")      # the job's Python finds the stand-ins
    spec = importlib.util.spec_from_file_location("huggingface_hub", fake / "huggingface_hub" / "__init__.py")
    hub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hub)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    jobs = tmp_path / "jobs"
    tde = trainer(repo, jobs, store={"type": "hf", "prefix": "org/ouroloop-"})
    result = tde.train(a_job(tmp_path / "ws"))
    repo_id, _, sha = result.model_id.partition("@")
    assert repo_id == f"org/ouroloop-{result.job['id']}" and re.fullmatch(r"[0-9a-f]{40}", sha)
    assert result.job["spec"]["store"] == {"type": "hf", "prefix": "org/ouroloop-"}
    stored = tmp_path / "hub" / repo_id
    assert json.loads((stored / "repo.json").read_text()) == {"private": True}
    assert sorted(str(p.relative_to(stored / sha)) for p in (stored / sha).rglob("*") if p.is_file()) == [
        "best.pt", "config.json", "job/spec.json", "job/train.jsonl", "job/val.jsonl"]
    # served in-process at that commit, reporting its ref
    ans = tde.serving.backend(result.backend).decide([Request("say yes", Question("noul", "done?"))])
    assert ans[0].model == result.model_id and decider[-1] == str(stored / sha)
    # the next job starts from it, downloaded at that commit
    second = tde.train(a_job(tmp_path / "ws", "m_0002", parent=result.model_id))
    run = json.loads((jobs / second.job["id"] / "run" / "config.json").read_text())
    assert run["init_from"] == str(stored / sha)
    # a ref outside the prefix, or anything that is not a ref, is refused
    for bad in (f"else/where@{sha}", f"{repo_id}@main", "../x", "/etc", "a/b"):
        with pytest.raises(ValueError, match="not a model ref"):
            tde.serving.backend({"type": "model", "model": bad, "trainer": "tde"})


# ---------------------------------------------------------------- serving refs
def test_serve_loads_a_job_by_its_ref_and_refuses_anything_else(tmp_path, decider):
    jobs = tmp_path / "jobs"
    (jobs / "m_0001_abc" / "run").mkdir(parents=True)
    rt = Runtime(tmp_path / "ws", {"mock": KeywordBackend()}, "mock",
                 serving=Serving(store=make_store({"type": "dir", "root": str(jobs)}), plugins={"tde": {}}))
    server = make_server(rt, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    q = Question("noul", "done?")
    try:
        sizes, pauses = [], []
        client = httpx.Client(event_hooks={"request": [
            lambda r: sizes.append(len(json.loads(r.content)["requests"]))]})
        ans = HttpBackend(url, model="m_0001_abc", client=client, sleep=pauses.append).decide(
            [Request(f"say {'yes' if i % 2 else 'no'} {i}", q) for i in range(600)])
        assert sizes == [256, 256, 88] and len(ans) == 600 and not pauses
        assert {a.model for a in ans} == {"m_0001_abc"} and ans[1].label == "yes" and ans[0].label == "no"
        assert decider == [str(jobs / "m_0001_abc" / "run")]
        assert HttpBackend(url).decide([Request("x", q)])[0].model != "m_0001_abc"     # the server's default
        for bad in ("../m_0001_abc", "m_0001_abc/run", "/etc/passwd", "m_0002_none", "evil/x@" + "0" * 40,
                    "org/ouroloop-x@HEAD", 7):
            resp = httpx.post(f"{url}/decide", json={"model": bad, "requests": [
                {"state": "s", "question": q.to_json()}]})
            assert resp.status_code == 400, bad
        for trainer in ("os", "ouroloop.cli", "nosuch"):                 # a request never names a module to import
            resp = httpx.post(f"{url}/decide", json={"model": "m_0001_abc", "trainer": trainer, "requests": [
                {"state": "s", "question": q.to_json()}]})
            assert resp.status_code == 400 and "no trainer plugin" in resp.json()["error"], trainer
        signature = HttpBackend(url, model="m_0001_abc", trainer="signature")    # this package's: loads, or says no
        with pytest.raises(httpx.HTTPStatusError):
            signature.decide([Request("x", q)])
        with pytest.raises(httpx.HTTPStatusError):                   # a bad request is not sent again
            HttpBackend(url, model="../x", sleep=pauses.append).decide([Request("x", q)])
        assert not pauses
        assert TdeBackend._loaded
        HttpBackend(url).release()                                   # before a training job on its GPU
        assert not TdeBackend._loaded
    finally:
        server.shutdown()


def test_the_http_backend_sends_a_chunk_again_after_a_failure(tmp_path):
    calls = []

    def serve(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        if len(calls) == 2:
            return httpx.Response(503)
        n = len(json.loads(request.content)["requests"])
        return httpx.Response(200, json={"answers": [{"probs": {"yes": 0.8, "no": 0.2}, "model": "m"}] * n})

    pauses = []
    backend = HttpBackend("http://serve", client=httpx.Client(transport=httpx.MockTransport(serve)),
                          sleep=pauses.append)
    assert [a.label for a in backend.decide([Request("s", Question("noul", "q?"))] * 3)] == ["yes"] * 3
    assert pauses == [1.0, 2.0] and len(calls) == 3
    down = HttpBackend("http://serve", sleep=pauses.append, client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(502))))
    with pytest.raises(httpx.HTTPStatusError):
        down.decide([Request("s", Question("noul", "q?"))] * 2)
    assert pauses == [1.0, 2.0, 1.0, 2.0, 4.0]                      # three retries, then the failure
    with pytest.raises(httpx.HTTPStatusError):                       # a single decision: its caller takes the default
        down.decide([Request("s", Question("noul", "q?"))])
    assert pauses == [1.0, 2.0, 1.0, 2.0, 4.0]
    ref = {"type": "model", "model": "m_0001_abc"}
    served = Serving({"url": "http://serve"}).backend(ref)
    assert served.timeout.read == 600 and served.timeout.connect == 10          # long enough for a first load
    assert Serving({"url": "http://serve", "timeout": 60}).backend(ref).timeout.read == 60


# ---------------------------------------------------------------- research candidates
def test_a_research_recipe_sets_only_hyperparameters_of_their_types():
    tde = JobTrainer("tde")
    for good in ({"epochs": 2}, {"lr_head": 1e-4}, {"max_steps": 100}, {"focus": 0.5}, {"batch_size": 4},
                 {"gradient_checkpointing": False}):
        check_change(good, tde)
    for bad, why in (({"epochs": "3"}, "must be a number"), ({"batch_size": 2.5}, "must be an integer"),
                     ({"batch_size": True}, "must be an integer"),
                     ({"gradient_checkpointing": 1}, "must be true or false"),
                     ({"max_steps": None}, "must be a number"), ({"train_limit": "all"}, "must be a number"),
                     ({"init_from": "runs/x"}, "a person"), ({"replay": "cal.jsonl"}, "a person"),
                     ({"out_dir": "/tmp"}, "unknown")):
        with pytest.raises(ValueError, match=why):
            check_change(bad, tde)
    # the keys are the framework's and the plugin's own: signature has no TDE knobs, nor does an in-process trainer
    signature = JobTrainer("signature")
    for trainer in (signature, SignatureTrainer(), None):
        check_change({"min_weight": 1.0, "focus": 0.5, "seed": 3}, trainer)
        with pytest.raises(ValueError, match="unknown recipe keys: \\['lr_backbone'\\]"):
            check_change({"lr_backbone": 5e-5}, trainer)
    assert research_context(signature, {"min_weight": 1.0})["recipe_keys"] == ["focus", "min_weight", "seed"]
    ctx = research_context(tde, {"replay": "~/general.jsonl"})
    assert ctx["recipe"]["replay"] == "~/general.jsonl" and ctx["recipe"]["lr_head"] == 1e-4
    keys = ctx["recipe_keys"]
    assert "replay" not in keys and "init_from" not in keys and "w_rps" in keys

    class Untouched:
        def train(self, job):
            raise AssertionError("no job may start")

    with pytest.raises(ValueError, match="must be a number"):
        recipe_evaluator(Untouched())(None, Candidate("recipe", "stop", {"min_weight": "fast"}, "h"), None)


def test_a_sigterm_during_a_job_cancels_it_before_the_process_ends(tmp_path, repo, monkeypatch):
    """systemd, docker stop and a plain kill all send SIGTERM. Left to its default it ends the process without
    running any finally, so the training job would keep the GPU with nothing left to collect it."""
    monkeypatch.setenv("STUB_TDE", "hold")
    monkeypatch.setenv("STUB_PID", str(tmp_path / "tde.pid"))
    jobs = tmp_path / "jobs"

    def sigterm(seconds):                   # the signal a service manager sends, once TDE is training
        held(tmp_path / "tde.pid")
        os.kill(os.getpid(), signal.SIGTERM)

    with pytest.raises(SystemExit):
        trainer(repo, jobs, sleep=sigterm).train(a_job(tmp_path / "ws"))
    (d,) = jobs.iterdir()
    assert gone(int((d / "pid").read_text())) and (d / "exit").read_text().strip() == "143"
    assert gone(held(tmp_path / "tde.pid"))
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL       # the handler is not left behind
