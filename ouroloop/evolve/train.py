"""Trainers turn a built dataset into a new decision model (PLAN §4.3).

    JobTrainer          a trainer plugin (ouroloop.trainers: tde, signature, ...) as a job (ouroloop/worker.py) on
                        this machine or on a training host over ssh (compute.py); the model it yields is a ref: the
                        job id, or <repo>@<commit> on Hugging Face
    SignatureTrainer    the signature model trained in this process, for tests and demos (trainers/signature.py)

Any trainer plugs in through the Trainer protocol below: train(job) -> TrainResult, backend(result) -> a backend,
and supports_weights; a plugin gets all of the job path for a few functions. The training data never holds
ouroloop's calibration or test sessions (build.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..ledger import new_id

# The recipe: what a plugin maps to its trainer (trainers/tde.py: TDE_KEYS). The defaults are TDE's for an 8 GB GPU:
# 1024-token states need batch 2 x accumulation 16 plus checkpointing.
DEFAULT_RECIPE = {
    "epochs": 3.0, "max_steps": None, "lr_backbone": 2e-5, "lr_head": 1e-4, "batch_size": 2, "grad_accum": 16,
    "max_state_tokens": 1024, "gradient_checkpointing": True, "w_rps": 1.0, "w_perm": 0.3,
    "replay": None,          # general training data on the training machine, e.g. "~/data/general/train.jsonl"
    "replay_ratio": 1.0,     # replay examples per ledger example
    "min_weight": 0.5,       # 1.0 keeps only environment and human labels
    "init_from": None,       # the first model's parent run on the training machine (or the plugin's init_from)
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


# ---------------------------------------------------------------------- a plugin as a job
CHECKOUT = Path(__file__).resolve().parents[2]      # this ouroloop checkout, whose HEAD each job records


class JobTrainer:
    """A trainer plugin (ouroloop.trainers) as a job on this machine or on one host over ssh: the same job either
    way, in a new directory <jobs_root>/<job id>/ on that machine with three files, spec.json, train.jsonl and
    val.jsonl, run by the worker installed there (python -m ouroloop.worker). `host`:

        None        this machine, under this Python, which imports what this process imports; no host.json
        "local"     this machine, under ~/.ouroloop/venv (`ouroloop host init local`)
        "user@box"  that host over ssh, under ~/.ouroloop/venv there (`ouroloop host init user@box`)

    With a host, the trainer reads its host.json before each job and runs host init when the host runs another
    ouroloop, lacks the plugin or has it from another source (hosts.ensure); an init that fails is a TrainError,
    before anything is used up. `options` are the plugin's ([trainers.<name>]): they travel in spec.json, and the
    model's backend spec carries them. jobs_root may start with ~, that machine's home. With transport "direct" the
    run stays in the job's directory and the model ref is the job id; with "hf" the job commits the run once to a
    private repository <hf_prefix><job id> and the ref is <repo>@<commit>.

    `models` is the config's [models] table: how the trained models are served (config.build_backend), checked here,
    before any job starts. Runs on a host over ssh are served by a model server there (url), or with transport "hf"
    loaded in-process from Hugging Face (hf_prefix); this machine's jobs_root never holds them. With "hf",
    `publish_s` of the deadline is kept for the commit.

    Any failure of the job or of reaching it is a TrainError, which the gate takes for inconclusive: a job that exits
    non-zero, is lost (no exit code, its process gone), outlives deadline_s by two polls, or leaves no well-formed
    result.json. A job this process is waiting on is cancelled when the wait ends in any other way (an exception,
    KeyboardInterrupt), and the first job of a process cancels the jobs of the same workspace a killed process left
    running: there is no reattaching, the round runs again."""
    _swept: set = set()                 # (host, jobs_root, workspace) whose leftover jobs this process cancelled

    def __init__(self, plugin: str, options: dict | None = None, host: str | None = None,
                 jobs_root: str = "~/.ouroloop/jobs", transport: str = "direct", hf_prefix: str | None = None,
                 deadline_s: float = 28800, poll_s: float = 30, ssh_opts: str = "-o BatchMode=yes",
                 models: dict | None = None, publish_s: float = 900, runner=None, sleep=time.sleep):
        from ..config import check_models
        from ..hosts import PYTHON
        from ..trainers import load
        from .compute import Compute
        if transport not in ("direct", "hf"):
            raise ValueError(f'transport {transport!r}: "direct" or "hf"')
        if transport == "hf" and not hf_prefix:
            raise ValueError('transport "hf" needs hf_prefix, e.g. "your-org/ouroloop-"')
        self.name, self.plugin, self.options = plugin, load(plugin), dict(options or {})
        self.supports_weights = bool(getattr(self.plugin, "SUPPORTS_WEIGHTS", False))
        self.models = check_models(models)
        remote = host not in (None, "local")
        if remote and not self.models.get("url") and not (transport == "hf" and self.models.get("hf_prefix")):
            raise ValueError(f"the models trained on {host} stay there: [models] needs url (`ouroloop serve` there), "
                             f"or transport \"hf\" and hf_prefix to load them here")
        if remote and not jobs_root.startswith(("/", "~")):     # a job's cwd is its directory, not the home
            raise ValueError(f"jobs_root {jobs_root!r}: absolute, or from ~ (the home on {host})")
        if not remote:
            jobs_root = os.path.abspath(os.path.expanduser(jobs_root))
        self.python = PYTHON if remote else os.path.expanduser(PYTHON) if host == "local" else sys.executable
        self.host, self.jobs_root, self.transport, self.hf_prefix = host, jobs_root, transport, hf_prefix
        self.deadline_s, self.poll_s, self.sleep, self.ssh_opts = float(deadline_s), float(poll_s), sleep, ssh_opts
        self.publish_s = float(publish_s) if transport == "hf" else 0.0
        self.runner = runner
        self.compute = Compute(host if remote else None, jobs_root, ssh_opts, runner)

    def train(self, job: TrainJob) -> TrainResult:
        from ..worker import locate
        job_id, spec, result = self._run(job, check_only=False)
        ref = result.get("model")
        try:
            place = locate(ref, self.jobs_root, self.hf_prefix)
        except ValueError:
            place = None
        mine = ref == job_id if self.transport == "direct" else bool(place and place[0] == spec["hf_repo"])
        if not mine:
            raise TrainError(f"job {job_id}: result.json names model {ref!r}, not this job's")
        backend = {"type": "model", "model": ref, "trainer": self.name,
                   **({"options": self.options} if self.options else {})}
        return TrainResult(ref, backend, {"id": job_id, "host": self.host, "dir": self.compute.dir(job_id),
                                          "spec": spec, "result": result})

    def dry_run(self, job: TrainJob) -> dict:
        """Everything up to training, as a job on the training machine: the plugin's validate. Uses no GPU."""
        job_id, _, result = self._run(job, check_only=True)
        return {"job": job_id, "host": self.host, "dir": self.compute.dir(job_id), **result}

    def backend(self, result: TrainResult):
        from ..config import build_backend
        return build_backend(result.backend, self.models)

    def _run(self, job: TrainJob, check_only: bool) -> tuple[str, dict, dict]:
        from ..release import code_version
        from ..worker import git_state
        parent = job.init_from or self.options.get("init_from")
        if getattr(self.plugin, "NEEDS_PARENT", False) and not parent:
            raise ValueError(f"{job.version}: no parent to train from: no champion, and no init_from in [recipe] or "
                             f"[trainers.{self.name}]")
        workspace = str(Path(job.data_dir).resolve().parents[2])    # <workspace>/models/<version>/data
        if not check_only:              # a dry run may start beside a live one, whose job is not a leftover
            self._sweep(workspace)
        self._ready()
        job_id = new_id(job.version)
        spec = {"job": job_id, "version": job.version, "parent": parent, "trainer": self.name,
                "options": self.options, "recipe": job.recipe, "host": self.host, "jobs_root": self.jobs_root,
                "deadline_s": self.deadline_s, "publish_s": self.publish_s, "transport": self.transport,
                "hf_repo": f"{self.hf_prefix}{job_id}" if self.transport == "hf" else None,
                "ouroloop": {"code": code_version(), "git": git_state(CHECKOUT)}, "workspace": workspace,
                "check_only": check_only}
        if not check_only:
            self._release()
        where = self.compute.where()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            data = Path(job.data_dir)
            try:
                self.compute.launch(job_id, [Path(tmp) / "spec.json", data / "train.jsonl", data / "val.jsonl"],
                                    self.python, self._env())
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                self._cancel(job_id, workspace)         # it may have started before the failure
                raise TrainError(f"job {job_id} could not start on {where}: {_why(e)}") from e
        status = self._wait(job_id, workspace)
        if status.code != 0:
            raise TrainError(f"job {job_id} failed on {where} (exit {status.code}); the end of its log:\n{status.log}")
        if not isinstance(status.result, dict):
            raise TrainError(f"job {job_id} on {where} exited 0 without a readable result.json")
        return job_id, spec, status.result

    def _env(self) -> dict | None:
        """A job on this machine under this Python imports what this process imports; under the host's venv, only
        what is installed there."""
        if self.host is None:
            paths = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
            paths += [os.path.abspath(p) for p in sys.path if p and os.path.isdir(p)]
            return {**os.environ, "PYTHONPATH": os.pathsep.join(dict.fromkeys(paths))}
        if self.host == "local":
            return {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        return None

    def _ready(self) -> None:
        """A host that is not ready for the job is prepared (host init) first; one that cannot be is a TrainError."""
        if self.host is None:
            return
        from ..hosts import HostError, ensure
        try:
            ensure(self.host, {self.name: self.options}, self.ssh_opts, self.transport == "hf", self.runner,
                   say=lambda m: print(f"trainer: {m}", file=sys.stderr))
        except HostError as e:
            raise TrainError(str(e)) from e
        except (OSError, subprocess.SubprocessError) as e:
            raise TrainError(f"could not read the host.json of {self.host}: {_why(e)}") from e

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
            print(f"trainer: could not cancel job {job_id} on {self.compute.where()}: {_why(e)}", file=sys.stderr)
            JobTrainer._swept.discard((self.host, self.jobs_root, workspace))

    def _sweep(self, workspace: str) -> None:
        """Cancel the jobs of this workspace that a killed process left unfinished, once per process."""
        key = (self.host, self.jobs_root, workspace)
        if key in JobTrainer._swept:
            return
        try:
            for job_id in self.compute.unfinished(workspace):
                print(f"trainer: cancelling job {job_id} on {self.compute.where()}, left unfinished by an earlier "
                      f"run of this workspace", file=sys.stderr)
                self.compute.cancel(job_id)
        except (OSError, subprocess.SubprocessError) as e:
            raise TrainError(f"could not list the unfinished jobs on {self.compute.where()}: {_why(e)}") from e
        JobTrainer._swept.add(key)

    def _release(self) -> None:
        """Let go of the models resident on the GPU before a job needs it: in this process, and on the model server."""
        release = getattr(self.plugin, "release", None)
        if callable(release):
            release()
        if self.models and self.models.get("url"):
            import httpx

            from ..backends.http import HttpBackend
            try:
                HttpBackend(self.models["url"], token_env=self.models.get("token_env", "OUROLOOP_TOKEN")).release()
            except httpx.HTTPError as e:
                print(f"trainer: the model server did not release its models: {_why(e)}", file=sys.stderr)


def _why(e: BaseException) -> str:
    text = (getattr(e, "stderr", None) or str(e) or type(e).__name__).strip()
    return text.splitlines()[-1] if text else type(e).__name__
