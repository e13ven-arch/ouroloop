"""Trainers turn a built dataset into a new decision model (PLAN §4.3).

    SignatureTrainer    a tiny in-process model (label frequencies per error signature), for tests and demos
    LocalTdeTrainer     TDE's trainer as a subprocess, on a GPU machine that has the TDE repository
    RemoteTdeTrainer    TDE's trainer on a GPU host over ssh; the checkpoint stays on the host, and
                        RemoteTdeBackend runs batch inference there

TDE, the small decision model these trainers were built around, is a separate project released on its own. Any
other model plugs in through the Trainer protocol below: train(job) -> TrainResult, backend(result) -> a backend.

`prepare_job` and `predict_file` are self-contained (standard library, plus tde for prediction) so the exact same
code runs locally or is sent to the host.
"""
from __future__ import annotations

import inspect
import json
import os
import shlex
import subprocess
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..ledger import new_id
from ..types import Answer, Request, normalize

# 8 GB-GPU defaults from the TDE runs: 1024-token states need batch 2 x accumulation 16 plus checkpointing.
DEFAULT_RECIPE = {
    "epochs": 3.0, "max_steps": None, "lr_backbone": 2e-5, "lr_head": 1e-4, "batch_size": 2, "grad_accum": 16,
    "max_state_tokens": 1024, "gradient_checkpointing": True, "w_rps": 1.0, "w_perm": 0.3,
    "replay": None,          # general training data on the training machine, e.g. "data/general/train.jsonl"
    "replay_ratio": 1.0,     # replay examples per ledger example
    "min_weight": 0.5,       # 1.0 keeps only environment and human labels
    "init_from": None,       # the first model's parent run, e.g. "runs/base"
    "seed": 0,
}
RECIPE_KEYS = set(DEFAULT_RECIPE)
_TDE_KEYS = ("epochs", "max_steps", "lr_backbone", "lr_head", "batch_size", "grad_accum", "max_state_tokens",
             "gradient_checkpointing", "w_rps", "w_perm", "seed")


class TrainError(RuntimeError):
    pass


@dataclass
class TrainJob:
    version: str
    data_dir: Path
    recipe: dict
    init_from: str | None


@dataclass
class TrainResult:
    run: str          # the run directory, on the machine that trained it
    backend: dict     # a backend spec that serves this run (see config.build_backend)


class Trainer(Protocol):
    def train(self, job: TrainJob) -> TrainResult: ...

    def backend(self, result: TrainResult): ...


def tde_config(recipe: dict, out_dir: str, init_from: str | None, version: str) -> dict:
    cfg = {k: recipe[k] for k in _TDE_KEYS if recipe.get(k) is not None}
    cfg.update({"out_dir": out_dir, "readout": "joint", "notes": f"ouroloop {version}"})
    if init_from:
        cfg["init_from"] = init_from
    return cfg


def prepare_job(data_dir, cfg_json, replay=None, ratio=1.0, seed=0):
    """Write <data_dir>/tde/{train,calibration,test}.jsonl (ledger examples plus a seeded replay sample) and the TDE
    config, taking the architecture from the parent run. Returns the config path. Standard library only."""
    import json
    import os
    import random
    import shutil
    data_dir = os.path.expanduser(data_dir)
    out = os.path.join(data_dir, "tde")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(data_dir, "train.jsonl"), encoding="utf-8") as f:
        lines = [line if line.endswith("\n") else line + "\n" for line in f if line.strip()]
    want = int(len(lines) * float(ratio)) if replay else 0
    sample, rng = [], random.Random(seed)
    if want:
        with open(os.path.expanduser(replay), encoding="utf-8") as f:
            for i, line in enumerate(f):
                if len(sample) < want:
                    sample.append(line)
                else:
                    j = rng.randint(0, i)      # reservoir sampling: every line kept with equal probability
                    if j < want:
                        sample[j] = line
    lines += [s if s.endswith("\n") else s + "\n" for s in sample]
    random.Random(seed).shuffle(lines)
    with open(os.path.join(out, "train.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(lines)
    for name in ("calibration.jsonl", "test.jsonl"):
        if os.path.exists(os.path.join(data_dir, name)):
            shutil.copy(os.path.join(data_dir, name), os.path.join(out, name))
    cfg = json.loads(cfg_json)
    cfg["data_dir"] = out
    parent = os.path.join(os.path.expanduser(cfg.get("init_from") or ""), "config.json")
    if cfg.get("init_from") and os.path.exists(parent):
        with open(parent, encoding="utf-8") as f:
            pc = json.load(f)
        for key in ("backbone", "readout", "marker", "pool", "branch_layers", "branch_through_backbone", "finetune"):
            if key in pc and key not in cfg:
                cfg[key] = pc[key]
        cfg["init_from"] = os.path.expanduser(cfg["init_from"])
    path = os.path.join(out, "config.yaml")   # JSON is valid YAML
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return path


def predict_file(run, src, dst, device=None):
    """Batch inference with a TDE run: one JSON request per line in, probabilities per line out."""
    import json
    import os
    from tde.inference import Decider
    decider = Decider.from_run(os.path.expanduser(run), device=device)
    with open(os.path.expanduser(src), encoding="utf-8") as f, open(os.path.expanduser(dst), "w") as out:
        for line in f:
            r = json.loads(line)
            res = decider.decide(r["state"], r["question"])
            out.write(json.dumps({"i": r["i"], "probabilities": res["probabilities"]}) + "\n")


def validate_job(cfg_path):
    """On the training machine: check a prepared job against TDE itself (config fields, example schema, parent run)
    without training. Needs the tde package; imports torch but allocates nothing on the GPU."""
    import json
    import os
    from tde.schema import load_jsonl
    from tde.train import TrainConfig
    with open(os.path.expanduser(cfg_path), encoding="utf-8") as f:
        cfg = json.load(f)
    TrainConfig(**cfg)
    counts = {}
    for name in ("train", "calibration"):
        examples = load_jsonl(os.path.join(cfg["data_dir"], name + ".jsonl"))
        for ex in examples:
            ex.validate()
        counts[name] = len(examples)
    parent = cfg.get("init_from")
    return json.dumps({"ok": True, "counts": counts, "backbone": cfg.get("backbone"),
                       "parent_checkpoint": bool(parent) and os.path.exists(os.path.join(parent, "best.pt"))})


def _script(fn, *args) -> str:
    return inspect.getsource(fn) + f"\nprint({fn.__name__}({', '.join(repr(a) for a in args)}))\n"


# ---------------------------------------------------------------------- a tiny model for tests and demos
class SignatureBackend:
    """Predicts the label distribution seen in training for the same question and error signature."""
    name = "signature"

    def __init__(self, path: str | None = None, table: dict | None = None):
        self.path = path
        self.table = table if table is not None else json.loads(Path(path).read_text(encoding="utf-8"))
        self.model = f"signature:{Path(path).parent.name if path else 'memory'}"

    def decide(self, requests: list[Request]) -> list[Answer]:
        from .replay import signature
        out = []
        for r in requests:
            labels = r.question.labels()
            entry = self.table.get(r.question.instructions, {})
            dist = entry.get(signature(r.state)) or entry.get("*") or {}
            smoothed = {lab: dist.get(lab, 0.0) + 0.5 for lab in labels}    # add-half smoothing
            out.append(Answer(normalize(labels, smoothed), self.model))
        return out


class SignatureTrainer:
    def train(self, job: TrainJob) -> TrainResult:
        from .replay import signature
        counts: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
        for line in open(Path(job.data_dir) / "train.jsonl", encoding="utf-8"):
            e = json.loads(line)
            for key in (signature(e["state"]), "*"):
                for c, t in zip(e["candidates"], e["target"]):
                    counts[e["question"]][key][c["name"]] += t
        path = Path(job.data_dir).parent / "signature_model.json"
        path.write_text(json.dumps(counts, indent=2), encoding="utf-8")
        return TrainResult(str(path.parent), {"type": "signature", "path": str(path)})

    def backend(self, result: TrainResult) -> SignatureBackend:
        return SignatureBackend(result.backend["path"])


# ---------------------------------------------------------------------- TDE on this machine
class LocalTdeTrainer:
    def __init__(self, tde_repo: str, python: str | None = None, runs_dir: str = "runs/ouroloop", runner=None):
        self.repo = Path(tde_repo).expanduser()
        self.python = python or str(self.repo / ".venv" / "bin" / "python")
        self.runs_dir = runs_dir
        self.runner = runner or (lambda args, **kw: subprocess.run(args, check=True, **kw))

    def train(self, job: TrainJob) -> TrainResult:
        out = self.repo / self.runs_dir / job.version
        cfg = tde_config(job.recipe, str(out), job.init_from, job.version)
        cfg_path = prepare_job(str(job.data_dir), json.dumps(cfg), job.recipe.get("replay") and
                               str(self.repo / job.recipe["replay"]), job.recipe.get("replay_ratio", 1.0),
                               job.recipe.get("seed", 0))
        self.runner([self.python, "scripts/train.py", "--config", cfg_path], cwd=self.repo)
        return TrainResult(str(out), {"type": "tde", "run": str(out)})

    def backend(self, result: TrainResult):
        from ..backends import TdeBackend
        return TdeBackend(result.run)


# ---------------------------------------------------------------------- TDE on a GPU host
class Remote:
    """ssh and rsync to one host. `runner(args, input=None)` returns a CompletedProcess (injectable for tests)."""

    def __init__(self, host: str, ssh_opts: str = "-o BatchMode=yes", runner=None):
        self.host = host
        self.ssh_opts = [os.path.expanduser(t) for t in shlex.split(ssh_opts)]
        self.runner = runner or (lambda args, input=None: subprocess.run(args, input=input, capture_output=True,
                                                                          text=True, check=True))

    def ssh(self, command: str, input: str | None = None) -> str:
        return self.runner(["ssh", *self.ssh_opts, self.host, command], input=input).stdout

    def push(self, local: str, remote: str) -> None:
        self.runner(["rsync", "-az", "-e", shlex.join(["ssh", *self.ssh_opts]), local, f"{self.host}:{remote}"])

    def pull(self, remote: str, local: str) -> None:
        self.runner(["rsync", "-az", "-e", shlex.join(["ssh", *self.ssh_opts]), f"{self.host}:{remote}", local])


class RemoteTdeTrainer:
    def __init__(self, host: str, remote_repo: str, python: str = ".venv/bin/python", jobs_dir: str = "ouroloop_jobs",
                 ssh_opts: str = "-o BatchMode=yes", poll: float = 30.0, timeout: float = 8 * 3600, runner=None,
                 sleep=time.sleep):
        """host: "user@machine"; remote_repo: a TDE checkout there; ssh_opts: e.g. "-i ~/.ssh/key -o BatchMode=yes"."""
        self.remote = Remote(host, ssh_opts, runner)
        self.spec = {"host": host, "remote_repo": remote_repo, "python": python, "jobs_dir": jobs_dir,
                     "ssh_opts": ssh_opts}
        self.repo, self.python, self.jobs_dir = remote_repo, python, jobs_dir
        self.poll, self.timeout, self.sleep = poll, timeout, sleep

    def _prepare(self, job: TrainJob, rjob: str) -> str:
        self.remote.ssh(f"mkdir -p {rjob}/data")
        self.remote.push(f"{job.data_dir}/", f"{rjob}/data/")
        cfg = tde_config(job.recipe, f"{rjob}/run", job.init_from, job.version)
        return self.remote.ssh(f"cd {self.repo} && {self.python} -", input=_script(
            prepare_job, f"{rjob}/data", json.dumps(cfg), job.recipe.get("replay"),
            job.recipe.get("replay_ratio", 1.0), job.recipe.get("seed", 0))).strip().splitlines()[-1]

    def dry_run(self, job: TrainJob, keep: bool = False) -> dict:
        """Everything up to training on the real host: push the data, compose it with the replay sample, write the
        config, and have TDE validate both. Uses no GPU. The job directory is removed unless keep=True."""
        rjob = f"{self.repo}/{self.jobs_dir}/{job.version}"
        try:
            cfg_path = self._prepare(job, rjob)
            out = self.remote.ssh(f"cd {self.repo} && {self.python} -", input=_script(validate_job, cfg_path))
            return {**json.loads(out.strip().splitlines()[-1]), "config": cfg_path}
        finally:
            if not keep:
                self.remote.ssh(f"rm -rf {rjob}")

    def train(self, job: TrainJob) -> TrainResult:
        rjob = f"{self.repo}/{self.jobs_dir}/{job.version}"
        cfg_path = self._prepare(job, rjob)
        inner = f"{self.python} scripts/train.py --config {cfg_path} > {rjob}/train.log 2>&1; echo $? > {rjob}/exit_code"
        # Detached, so a dropped connection does not kill training; completion is signalled by exit_code.
        self.remote.ssh(f"cd {self.repo} && nohup bash -c {shlex.quote(inner)} > /dev/null 2>&1 &")
        deadline = time.time() + self.timeout
        while not (code := self.remote.ssh(f"cat {rjob}/exit_code 2>/dev/null || true").strip()):
            if time.time() > deadline:
                raise TrainError(f"timed out; the job is still running on the host in {rjob}")
            self.sleep(self.poll)
        if code != "0":
            raise TrainError(f"training failed (exit {code}):\n{self.remote.ssh(f'tail -n 30 {rjob}/train.log')}")
        return TrainResult(f"{rjob}/run", {"type": "tde_remote", "run": f"{rjob}/run", **self.spec})

    def backend(self, result: TrainResult) -> "RemoteTdeBackend":
        spec = {k: v for k, v in result.backend.items() if k != "type"}
        return RemoteTdeBackend(runner=self.remote.runner, **spec)


class RemoteTdeBackend:
    """TDE inference on the GPU host, one batch per call: fine for evaluation, too slow per decision in a loop."""
    name = "tde_remote"

    def __init__(self, run: str, host: str, remote_repo: str, python: str = ".venv/bin/python",
                 jobs_dir: str = "ouroloop_jobs", ssh_opts: str = "-o BatchMode=yes", device: str | None = None,
                 runner=None):
        self.run, self.repo, self.python, self.jobs_dir = run, remote_repo, python, jobs_dir
        self.device = device       # "cpu" keeps inference off a GPU that a training job is using
        self.remote = Remote(host, ssh_opts, runner)
        self.model = f"tde:{Path(run).parent.name if Path(run).name == 'run' else Path(run).name}"

    def decide(self, requests: list[Request]) -> list[Answer]:
        if not requests:
            return []
        batch = new_id("b")
        rdir = f"{self.repo}/{self.jobs_dir}/predict/{batch}"
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "requests.jsonl", Path(tmp) / "predictions.jsonl"
            src.write_text("".join(json.dumps({"i": i, "state": r.state, "question": r.question.to_json()},
                                              ensure_ascii=False) + "\n" for i, r in enumerate(requests)),
                           encoding="utf-8")
            self.remote.ssh(f"mkdir -p {rdir}")
            self.remote.push(str(src), f"{rdir}/requests.jsonl")
            self.remote.ssh(f"cd {self.repo} && {self.python} -",
                            input=_script(predict_file, self.run, f"{rdir}/requests.jsonl", f"{rdir}/predictions.jsonl",
                                          self.device))
            self.remote.pull(f"{rdir}/predictions.jsonl", str(dst))
            self.remote.ssh(f"rm -rf {rdir}")    # leave nothing behind on the host
            probs = {r["i"]: r["probabilities"] for r in map(json.loads, dst.read_text(encoding="utf-8").splitlines())}
        return [Answer(normalize(req.question.labels(), probs[i]), self.model) for i, req in enumerate(requests)]
