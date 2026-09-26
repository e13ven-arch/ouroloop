"""Trainers turn a built dataset into a new decision model (PLAN §4.3).

    SignatureTrainer    a tiny in-process model (label frequencies per error signature, or per any key of the view
                        a scenario defines), for tests and demos
    TdeTrainer          TDE's trainer as a job (worker.py) on this machine or on a training host over ssh
                        (compute.py); the model it yields is a ref: the job id, or <repo>@<commit> on Hugging Face

TDE, the small decision model these trainers were built around, is a separate project released on its own. Any
other model plugs in through the Trainer protocol below: train(job) -> TrainResult, backend(result) -> a backend,
and supports_weights. The training data never holds ouroloop's calibration or test sessions (build.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..ledger import new_id
from ..types import Answer, Request
from .worker import TDE_KEYS, git_state, locate, prepare_job, tde_config, validate_job, yaml_json  # noqa: F401

# 8 GB-GPU defaults from the TDE runs: 1024-token states need batch 2 x accumulation 16 plus checkpointing.
DEFAULT_RECIPE = {
    "epochs": 3.0, "max_steps": None, "lr_backbone": 2e-5, "lr_head": 1e-4, "batch_size": 2, "grad_accum": 16,
    "max_state_tokens": 1024, "gradient_checkpointing": True, "w_rps": 1.0, "w_perm": 0.3,
    "replay": None,          # general training data on the training machine, e.g. "data/general/train.jsonl"
    "replay_ratio": 1.0,     # replay examples per ledger example
    "min_weight": 0.5,       # 1.0 keeps only environment and human labels
    "init_from": None,       # the first model's parent run, e.g. "runs/base"
    "train_limit": None,     # train on at most this many examples, a seeded sample of the ledger's and replay's
    "focus": None,           # share of the ledger's training rows that are default errors (pipeline._parts)
    "seed": 0,
}
RECIPE_KEYS = set(DEFAULT_RECIPE)
# Files and runs on the training machine: set by a person in [recipe], never by a research candidate, which could
# name the calibration split, or a run trained on the test sessions.
PATH_KEYS = {"replay", "init_from"}


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
    model_id: str           # the model ref, which the backend reports as Answer.model: calibrations, thresholds,
                            # revocations and session pins are keyed by it, and the next job starts from it
    backend: dict           # a backend spec that serves it (see config.build_backend)
    job: dict | None = None     # the training job: id, host, dir, spec, result


class Trainer(Protocol):
    supports_weights: bool      # it weighs each example by meta.weight; if not, it is given weight-1.0 rows only

    def train(self, job: TrainJob) -> TrainResult: ...

    def backend(self, result: TrainResult): ...


# ---------------------------------------------------------------------- a tiny model for tests and demos
class SignatureBackend:
    """Predicts the label distribution seen in training for the same question and error signature."""
    name = "signature"

    def __init__(self, path: str | None = None, table: dict | None = None, signature: str | None = None):
        """`signature`: "module:function", view -> key, in place of the error signature (replay.signature)."""
        self.path = path
        self.table = table if table is not None else json.loads(Path(path).read_text(encoding="utf-8"))
        self.model = f"signature:{Path(path).parent.name if path else 'memory'}"
        self.signature = _signature(signature)

    def decide(self, requests: list[Request]) -> list[Answer]:
        out = []
        for r in requests:
            labels = r.question.labels()
            entry = self.table.get(r.question.instructions, {})
            dist = entry.get(self.signature(r.state)) or entry.get("*") or {}
            smoothed = {lab: dist.get(lab, 0.0) + 0.5 for lab in labels}    # add-half smoothing
            out.append(Answer.of(labels, smoothed, self.model))
        return out


def _signature(ref: str | None):
    from ..runtime import import_object
    from .replay import signature
    return import_object(ref) if ref else signature


class SignatureTrainer:
    supports_weights = True

    def __init__(self, signature: str | None = None):
        self.ref = signature

    def train(self, job: TrainJob) -> TrainResult:
        key_of = _signature(self.ref)
        counts: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
        for line in open(Path(job.data_dir) / "train.jsonl", encoding="utf-8"):
            e = json.loads(line)
            for key in (key_of(e["state"]), "*"):
                for c, t in zip(e["candidates"], e["target"]):
                    counts[e["question"]][key][c["name"]] += t * float(e["meta"].get("weight", 1.0))
        path = Path(job.data_dir).parent / "signature_model.json"
        path.write_text(json.dumps(counts, indent=2), encoding="utf-8")
        spec = {"type": "signature", "path": str(path), **({"signature": self.ref} if self.ref else {})}
        return TrainResult(f"signature:{path.parent.name}", spec)

    def backend(self, result: TrainResult) -> SignatureBackend:
        return SignatureBackend(result.backend["path"], signature=result.backend.get("signature"))




# ---------------------------------------------------------------------- TDE as a job
WORKER = Path(__file__).with_name("worker.py")
CHECKOUT = Path(__file__).resolve().parents[2]      # this ouroloop checkout, whose HEAD each job records


class TdeTrainer:
    """TDE's trainer as a job (worker.py) on this machine (host None) or on one host over ssh ("user@box"): the same
    job either way, in a new directory <jobs_root>/<job id>/ on that machine with four files: spec.json, worker.py,
    train.jsonl and val.jsonl. `tde_repo` and `python` are the TDE checkout and the Python that has the tde package
    there (paths may start with ~, that machine's home); a recipe's replay and a path init_from are relative to the
    checkout. With transport "direct" the run stays in the job's directory and the model ref is the job id; with
    "hf" the job commits the run once to a private repository <hf_prefix><job id> and the ref is <repo>@<commit>.

    `models` is the config's [models] table: how the trained models are served (config.build_backend), checked here,
    before any job starts. Runs on a host are served by a model server there (url), or with transport "hf" loaded
    in-process from Hugging Face (hf_prefix); this machine's jobs_root never holds them. With "hf", `publish_s` of
    the deadline is kept for the commit.

    Any failure of the job or of reaching it is a TrainError, which the gate takes for inconclusive: a job that exits
    non-zero, is lost (no exit code, its process gone), outlives deadline_s by two polls, or leaves no well-formed
    result.json. A job this process is waiting on is cancelled when the wait ends in any other way (an exception,
    KeyboardInterrupt), and the first job of a process cancels the jobs of the same workspace a killed process left
    running: there is no reattaching, the round runs again."""
    supports_weights = False            # TDE's trainer does not read meta.weight
    _swept: set = set()                 # (host, jobs_root, workspace) whose leftover jobs this process cancelled

    def __init__(self, tde_repo: str, python: str | None = None, host: str | None = None,
                 jobs_root: str = "~/.ouroloop/jobs", transport: str = "direct", hf_prefix: str | None = None,
                 deadline_s: float = 28800, poll_s: float = 30, ssh_opts: str = "-o BatchMode=yes",
                 models: dict | None = None, publish_s: float = 900, runner=None, sleep=time.sleep):
        from ..config import check_models
        from .compute import Compute
        if transport not in ("direct", "hf"):
            raise ValueError(f'transport {transport!r}: "direct" or "hf"')
        if transport == "hf" and not hf_prefix:
            raise ValueError('transport "hf" needs hf_prefix, e.g. "your-org/ouroloop-"')
        if python is None and host:
            raise ValueError("a trainer on a host needs python: the TDE venv's Python there")
        self.models = check_models(models)
        if host and not self.models.get("url") and not (transport == "hf" and self.models.get("hf_prefix")):
            raise ValueError(f"the models trained on {host} stay there: [models] needs url (`ouroloop serve` there), "
                             f"or transport \"hf\" and hf_prefix to load them here")
        python = python or sys.executable
        if host:                    # the job's directory is its cwd: a relative path would be relative to it
            for name, path in (("tde_repo", tde_repo), ("jobs_root", jobs_root), ("python", python)):
                if (name != "python" or "/" in path) and not path.startswith(("/", "~")):
                    raise ValueError(f"{name} {path!r}: absolute, or from ~ (the home on {host})")
        else:
            tde_repo, jobs_root = (os.path.abspath(os.path.expanduser(p)) for p in (tde_repo, jobs_root))
            python = os.path.abspath(os.path.expanduser(python)) if "/" in python else python
        self.tde_repo, self.python = tde_repo, python
        self.host, self.jobs_root, self.transport, self.hf_prefix = host, jobs_root, transport, hf_prefix
        self.deadline_s, self.poll_s, self.sleep = float(deadline_s), float(poll_s), sleep
        self.publish_s = float(publish_s) if transport == "hf" else 0.0
        self.compute = Compute(host, jobs_root, ssh_opts, runner)

    def train(self, job: TrainJob) -> TrainResult:
        job_id, spec, result = self._run(job, check_only=False)
        ref = result.get("model")
        try:
            place = locate(ref, self.jobs_root, self.hf_prefix)
        except ValueError:
            place = None
        mine = ref == job_id if self.transport == "direct" else bool(place and place[0] == spec["hf_repo"])
        if not mine:
            raise TrainError(f"job {job_id}: result.json names model {ref!r}, not this job's")
        return TrainResult(ref, {"type": "model", "model": ref},
                           {"id": job_id, "host": self.host, "dir": self.compute.dir(job_id), "spec": spec,
                            "result": result})

    def dry_run(self, job: TrainJob) -> dict:
        """Everything up to training, as a job on the training machine: the data composed with the replay sample,
        the config, and TDE's own validation of both. Uses no GPU."""
        job_id, _, result = self._run(job, check_only=True)
        return {"job": job_id, "host": self.host, "dir": self.compute.dir(job_id), **result}

    def backend(self, result: TrainResult):
        from ..config import build_backend
        return build_backend(result.backend, self.models)

    def _run(self, job: TrainJob, check_only: bool) -> tuple[str, dict, dict]:
        if not job.init_from:
            raise ValueError(f"{job.version}: no parent to train from: no champion, and no init_from in [recipe]")
        workspace = str(Path(job.data_dir).resolve().parents[2])    # <workspace>/models/<version>/data
        if not check_only:              # a dry run may start beside a live one, whose job is not a leftover
            self._sweep(workspace)
        job_id = new_id(job.version)
        recipe = job.recipe
        spec = {"job": job_id, "version": job.version, "parent": job.init_from,
                "tde": {k: recipe[k] for k in TDE_KEYS if recipe.get(k) is not None},
                "replay": recipe.get("replay"), "replay_ratio": recipe.get("replay_ratio", 1.0),
                "seed": recipe.get("seed", 0), "tde_repo": self.tde_repo, "python": self.python,
                "jobs_root": self.jobs_root, "deadline_s": self.deadline_s, "publish_s": self.publish_s,
                "transport": self.transport,
                "hf_repo": f"{self.hf_prefix}{job_id}" if self.transport == "hf" else None,
                "ouroloop": git_state(CHECKOUT), "workspace": workspace, "check_only": check_only}
        if not check_only:
            self._release()
        where = self.compute.where()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            data = Path(job.data_dir)
            try:
                self.compute.launch(job_id, [Path(tmp) / "spec.json", WORKER, data / "train.jsonl",
                                             data / "val.jsonl"], self.python)
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                self._cancel(job_id, workspace)         # it may have started before the failure
                raise TrainError(f"job {job_id} could not start on {where}: {_why(e)}") from e
        status = self._wait(job_id, workspace)
        if status.code != 0:
            raise TrainError(f"job {job_id} failed on {where} (exit {status.code}); the end of its log:\n{status.log}")
        if not isinstance(status.result, dict):
            raise TrainError(f"job {job_id} on {where} exited 0 without a readable result.json")
        return job_id, spec, status.result

    def _wait(self, job_id: str, workspace: str):
        where, deadline = self.compute.where(), time.time() + self.deadline_s + 2 * self.poll_s
        try:
            while True:
                status = self.compute.poll(job_id)
                if status.state == "exited":
                    return status
                if status.state == "lost":
                    raise TrainError(f"job {job_id} on {where} was lost: no exit code, and its process is gone "
                                     f"(a reboot, kill -9)")
                if time.time() > deadline:
                    raise TrainError(f"job {job_id} on {where} passed its deadline ({self.deadline_s:.0f}s)")
                self.sleep(self.poll_s)
        except BaseException:
            self._cancel(job_id, workspace)
            raise

    def _cancel(self, job_id: str, workspace: str) -> None:
        """Cancel a job as best one can; one that cannot be is left to the next sweep of `workspace`."""
        try:
            self.compute.cancel(job_id)
        except Exception as e:  # noqa: BLE001 - the first failure is the one to report
            print(f"tde trainer: could not cancel job {job_id} on {self.compute.where()}: {_why(e)}", file=sys.stderr)
            TdeTrainer._swept.discard((self.host, self.jobs_root, workspace))

    def _sweep(self, workspace: str) -> None:
        """Cancel the jobs of this workspace that a killed process left unfinished, once per process."""
        key = (self.host, self.jobs_root, workspace)
        if key in TdeTrainer._swept:
            return
        try:
            for job_id in self.compute.unfinished(workspace):
                print(f"tde trainer: cancelling job {job_id} on {self.compute.where()}, left unfinished by an "
                      f"earlier run of this workspace", file=sys.stderr)
                self.compute.cancel(job_id)
        except (OSError, subprocess.SubprocessError) as e:
            raise TrainError(f"could not list the unfinished jobs on {self.compute.where()}: {_why(e)}") from e
        TdeTrainer._swept.add(key)

    def _release(self) -> None:
        """Let go of the models resident on the GPU before a job needs it: in this process, and on the model server."""
        from ..backends.tde import TdeBackend
        TdeBackend.release()
        if self.models and self.models.get("url"):
            import httpx

            from ..backends.http import HttpBackend
            try:
                HttpBackend(self.models["url"], token_env=self.models.get("token_env", "OUROLOOP_TOKEN")).release()
            except httpx.HTTPError as e:
                print(f"tde trainer: the model server did not release its models: {_why(e)}", file=sys.stderr)


def _why(e: BaseException) -> str:
    text = (getattr(e, "stderr", None) or str(e) or type(e).__name__).strip()
    return text.splitlines()[-1] if text else type(e).__name__
