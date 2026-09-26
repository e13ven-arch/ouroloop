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

import contextlib
import json
import os
import shlex
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
    running: there is no reattaching, the round runs again.

    `cloud` ([trainer.cloud], cloud.py) runs each job on a new RunPod or Vast.ai instance instead of a host: it is
    started, tagged with the workspace and the job, waited for (ssh answers) within boot_timeout_s, checked and
    initialized like any host, runs the job over ssh, and is stopped however the job ends. The first job of a process
    stops the instances a killed run of the same workspace left. Its models travel through Hugging Face only
    (transport "hf"); HF_TOKEN reaches the instance as its environment variable, the API key never leaves this
    process. A provider or boot failure is a TrainError, before anything is used up. `api`: an httpx transport for the
    provider's API (tests)."""
    _swept: set = set()                 # (host, jobs_root, workspace) whose leftover jobs this process cancelled
    _stopped: set = set()               # (provider, tag) whose leftover instances this process stopped

    def __init__(self, plugin: str, options: dict | None = None, host: str | None = None,
                 jobs_root: str = "~/.ouroloop/jobs", transport: str = "direct", hf_prefix: str | None = None,
                 deadline_s: float = 28800, poll_s: float = 30, ssh_opts: str = "-o BatchMode=yes",
                 models: dict | None = None, publish_s: float = 900, cloud: dict | None = None, api=None,
                 runner=None, sleep=time.sleep):
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
        self.cloud, self.provider = None, None
        if cloud is not None:
            from . import cloud as clouds
            if host is not None:
                raise ValueError("[trainer] host and [trainer.cloud]: a job runs on one or the other")
            if transport != "hf":           # an instance is gone after its job: its run must be on the Hub by then
                raise ValueError(f'[trainer.cloud] needs transport "hf" (and hf_prefix), not {transport!r}: a cloud '
                                 f'instance is terminated when its job ends, and its run with it')
            self.cloud = clouds.check(cloud)
            self.provider = clouds.make(self.cloud, api, sleep)
        remote = host not in (None, "local") or self.cloud is not None
        where = host or "a cloud instance"
        if remote and not self.models.get("url") and not (transport == "hf" and self.models.get("hf_prefix")):
            raise ValueError(f"the models trained on {where} stay there: [models] needs url (`ouroloop serve` "
                             f"there), or transport \"hf\" and hf_prefix to load them here")
        if remote and not jobs_root.startswith(("/", "~")):     # a job's cwd is its directory, not the home
            raise ValueError(f"jobs_root {jobs_root!r}: absolute, or from ~ (the home on {where})")
        if not remote:
            jobs_root = os.path.abspath(os.path.expanduser(jobs_root))
        self.python = PYTHON if remote else os.path.expanduser(PYTHON) if host == "local" else sys.executable
        self.host, self.jobs_root, self.transport, self.hf_prefix = host, jobs_root, transport, hf_prefix
        self.deadline_s, self.poll_s, self.sleep, self.ssh_opts = float(deadline_s), float(poll_s), sleep, ssh_opts
        self._base_ssh = ssh_opts
        self.publish_s = float(publish_s) if transport == "hf" else 0.0
        self.runner = runner
        self.compute = Compute(host if remote and not self.cloud else None, jobs_root, ssh_opts, runner)

    def train(self, job: TrainJob) -> TrainResult:
        from ..worker import locate
        job_id, spec, result, where = self._run(job, check_only=False)
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
        return TrainResult(ref, backend, {"id": job_id, "host": spec["host"], "dir": self.compute.dir(job_id),
                                          "spec": spec, "result": result, **where})

    def dry_run(self, job: TrainJob) -> dict:
        """Everything up to training, as a job on the training machine: the plugin's validate. Uses no GPU."""
        job_id, spec, result, where = self._run(job, check_only=True)
        return {"job": job_id, "host": spec["host"], "dir": self.compute.dir(job_id), **where, **result}

    def backend(self, result: TrainResult):
        from ..config import build_backend
        return build_backend(result.backend, self.models)

    def _run(self, job: TrainJob, check_only: bool) -> tuple[str, dict, dict, dict]:
        """-> (job id, spec, result.json, where it ran: {} on a host, {"cloud": ...} on a cloud instance)"""
        parent = job.init_from or self.options.get("init_from")
        if getattr(self.plugin, "NEEDS_PARENT", False) and not parent:
            raise ValueError(f"{job.version}: no parent to train from: no champion, and no init_from in [recipe] or "
                             f"[trainers.{self.name}]")
        workspace = str(Path(job.data_dir).resolve().parents[2])    # <workspace>/models/<version>/data
        job_id = new_id(job.version)
        if self.cloud is None:
            if not check_only:          # a dry run may start beside a live one, whose job is not a leftover
                self._sweep(workspace)
            self._ready()
            return (job_id, *self._job(job, job_id, parent, workspace, check_only), {})
        where = {"cloud": {"provider": self.cloud["provider"]}}
        with self._instance(workspace, job_id, where["cloud"], sweep=not check_only):
            spec, result = self._job(job, job_id, parent, workspace, check_only)
        return job_id, spec, result, where

    def _job(self, job: TrainJob, job_id: str, parent: str | None, workspace: str,
             check_only: bool) -> tuple[dict, dict]:
        """The job on the host that self.compute reaches, prepared: launch, wait, its result."""
        from ..release import code_version
        from ..worker import git_state
        spec = {"job": job_id, "version": job.version, "parent": parent, "trainer": self.name,
                "options": self.options, "recipe": job.recipe, "host": self.host, "jobs_root": self.jobs_root,
                "deadline_s": self.deadline_s, "publish_s": self.publish_s, "transport": self.transport,
                "hf_repo": f"{self.hf_prefix}{job_id}" if self.transport == "hf" else None,
                "ouroloop": {"code": code_version(), "git": git_state(CHECKOUT)}, "workspace": workspace,
                "check_only": check_only}
        if not check_only and self.cloud is None:     # a cloud job has a GPU of its own
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
        return spec, status.result

    # ------------------------------------------------------------------ a cloud instance per job
    @contextlib.contextmanager
    def _instance(self, workspace: str, job_id: str, record: dict, sweep: bool = True):
        """A new instance for the job, ready for it (self.host and self.compute reach it while the job runs), and
        always stopped afterwards, with `record` (the card's) filled in: instance, gpu, price_per_hour, uptime_s.
        With `sweep`, first the instances a killed run of the workspace left (a dry run may start beside a live
        job, whose instance is not a leftover)."""
        from . import cloud as clouds
        from .compute import Compute
        name, provider, opts = self.cloud["provider"], self.provider, self.cloud
        tag = clouds.tag(workspace)
        if sweep:
            self._stop_leftovers(tag)
        env = {"PUBLIC_KEY": self._public_key()}
        if self.transport == "hf":
            token = os.environ.get("HF_TOKEN", "").strip()
            if not token:
                raise TrainError('HF_TOKEN is not set here: a cloud job publishes its run to Hugging Face, and the '
                                 'instance gets the token from this environment')
            env["HF_TOKEN"] = token
        id, began = None, None
        with tempfile.TemporaryDirectory() as tmp:
            try:
                try:
                    id = provider.start(tag, job_id, opts, env)
                    began = time.monotonic()
                    record.update({"instance": id, **{k: v for k, v in provider.placed.get(id, {}).items()
                                                      if k in ("gpu", "price_per_hour")}})
                    say(f"{name}: started instance {id} for job {job_id} ({record.get('gpu')}, "
                        f"${record.get('price_per_hour') or 0:.3f}/hr)")
                    host, port = self._boot(id, Path(tmp) / "known_hosts")
                    if provider.placed.get(id, {}).get("price_per_hour") is not None:    # what it bills, once known
                        record["price_per_hour"] = provider.placed[id]["price_per_hour"]
                except clouds.CloudError as e:
                    raise TrainError(str(e)) from None
                self.host = host
                self.ssh_opts = self._instance_ssh(port, Path(tmp) / "known_hosts")
                self.compute = Compute(host, self.jobs_root, self.ssh_opts, self.runner)
                self._ready()
                yield
            finally:
                self._stop(tag, job_id, id)
                if began is not None:
                    record["uptime_s"] = round(time.monotonic() - began, 1)
                self.host, self.ssh_opts = None, self._base_ssh
                self.compute = Compute(None, self.jobs_root, self.ssh_opts, self.runner)

    def _public_key(self) -> str:
        path = os.path.expanduser(str(self.cloud.get("ssh_key", "~/.ssh/id_ed25519"))) + ".pub"
        try:
            key = Path(path).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise TrainError(f"no ssh public key for the cloud instance at {path}: {e.strerror}; set ssh_key in "
                             f"[trainer.cloud] to the private key beside it") from None
        if not key:
            raise TrainError(f"{path} is empty")
        return key

    def _instance_ssh(self, port: int, known_hosts: Path) -> str:
        """ssh to a new instance: its port, the key whose .pub it was given, and its host key taken on first sight
        into a file of this instance alone (a new instance has a new host key; no other host's is trusted by it)."""
        key = os.path.expanduser(str(self.cloud.get("ssh_key", "~/.ssh/id_ed25519")))
        return " ".join([self._base_ssh, "-p", str(int(port)), "-i", shlex.quote(key), "-o",
                         "StrictHostKeyChecking=accept-new", "-o", shlex.quote(f"UserKnownHostsFile={known_hosts}")])

    def _boot(self, id: str, known_hosts: Path) -> tuple[str, int]:
        """Wait for the instance's ssh, within boot_timeout_s: the provider's word, then a real ssh answer."""
        from . import cloud as clouds
        from .compute import Remote
        limit = float(self.cloud.get("boot_timeout_s", 900))
        poll = float(self.cloud.get("boot_poll_s", 10))
        t0, slept = time.monotonic(), 0.0
        while True:
            place = self.provider.ready(id)
            if place is not None:
                host, port = place
                try:
                    Remote(host, self._instance_ssh(port, known_hosts), self.runner).ssh("true", timeout=60)
                    return host, port
                except (OSError, subprocess.SubprocessError):
                    pass                    # sshd not up yet, or start.sh still installing it
            if max(time.monotonic() - t0, slept) >= limit:
                raise clouds.CloudError(f"{self.cloud['provider']}: instance {id} did not answer ssh within "
                                        f"{limit:.0f}s (boot_timeout_s)")
            self.sleep(poll)
            slept += poll

    def _stop(self, tag: str, job_id: str, id: str | None) -> None:
        """Stop the job's instance, and any other the provider made for this job (a create retried after a lost
        answer). One that cannot be stopped is said loudly and left to the next sweep of the workspace."""
        name = self.cloud["provider"]
        try:
            ids = ([id] if id else []) + [i for i in self.provider.running(f"{tag}-{job_id}") if i != id]
        except Exception as e:  # noqa: BLE001 - in a finally: nothing may hide the job's own outcome
            ids = [id] if id else []
            say(f"{name}: could not list the instances of job {job_id}: {e}")
        for i in ids:
            try:
                self.provider.stop(i)
                say(f"{name}: stopped instance {i}")
            except Exception as e:  # noqa: BLE001 - the same
                say(f"{name}: COULD NOT STOP instance {i} (job {job_id}); it may still bill: {e}. It is stopped "
                    f"by the next job of this workspace, or stop it in the {name} console")
                JobTrainer._stopped.discard((name, tag))

    def _stop_leftovers(self, tag: str) -> None:
        """Stop the instances of this workspace that a killed process left, once per process; never another's."""
        key = (self.cloud["provider"], tag)
        if key in JobTrainer._stopped:
            return
        try:
            for i in self.provider.running(tag):
                say(f"{key[0]}: stopping instance {i}, left running by an earlier run of this workspace")
                self.provider.stop(i)
        except Exception as e:  # noqa: BLE001 - whatever it was, no job starts beside what may still bill
            raise TrainError(f"could not stop the instances an earlier run left on {key[0]}: {e}") from None
        JobTrainer._stopped.add(key)

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


def say(message: str) -> None:
    print(f"trainer: {message}", file=sys.stderr)


def _why(e: BaseException) -> str:
    text = (getattr(e, "stderr", None) or str(e) or type(e).__name__).strip()
    return text.splitlines()[-1] if text else type(e).__name__
