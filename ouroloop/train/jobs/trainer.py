"""JobTrainer (docs/DESIGN.md §2.2, pipeline 2): a trainer plugin (ouroloop/train/trainers: tde, signature, ...) as a
job (worker.py) on this machine, on a training host over ssh (compute.py, hosts/) or on a cloud instance (cloud.py);
the model it yields is a ref in the model store (ouroloop/train/stores): the job id, or <repo>@<commit> on Hugging
Face. It implements the Trainer protocol (ouroloop/train/train.py): a plugin gets all of the job path for a few
functions. A trainer only trains: its model is asked through serving (ouroloop/train/serving.py), by the backend spec on
its result. The training data never holds ouroloop's calibration or test sessions (ouroloop/train/dataset.py).
"""
from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ...ledger import new_id
from ..train import DEFAULT_RECIPE, PATH_KEYS, TrainError, TrainJob, TrainResult


# ---------------------------------------------------------------------- a plugin as a job
CHECKOUT = Path(__file__).resolve().parents[3]      # this ouroloop checkout, whose HEAD each job records


class JobTrainer:
    """A trainer plugin (ouroloop/train/trainers) as a job on this machine or on one host over ssh: the same job either
    way, in a new directory <jobs_root>/<job id>/ on that machine with three files, spec.json, train.jsonl and
    val.jsonl, run by the worker installed there (python -m ouroloop.train.jobs.worker). `host`:

        None        this machine, under this Python, which imports what this process imports; no host.json
        "local"     this machine, under ~/.ouroloop/venv (`python -m ouroloop.train host init local`)
        "user@box"  that host over ssh, under ~/.ouroloop/venv there (`python -m ouroloop.train host init user@box`)

    With a host, the trainer reads its host.json before each job and runs host init when the host runs another
    ouroloop, lacks the plugin or has it from another source (hosts.ensure); an init that fails is a TrainError,
    before anything is used up. `options` are the plugin's ([trainers.<name>]): they travel in spec.json, and the
    model's backend spec carries them. jobs_root may start with ~, that machine's home.

    `store` is the model store the job publishes its run to (a [store] table, or a store; default: a "dir" store
    at jobs_root, where the run stays in the job's directory and the ref is the job id). A "dir" store is on the
    machine that trains; an "hf" store commits the run once to a private repository <prefix><job id>, and the ref is
    <repo>@<commit>. `publish_s` of the deadline is kept for publishing, unless the run stays in place (a "dir" store
    at jobs_root). `serving` (a [serving] table, or a Serving; default: in-process) is how the models are asked; it
    is checked here, before any job starts: serving loads a ref in this process, so a job on another machine
    publishes to a store this machine can read ("hf"), never to a "dir" store there. Before a job on this machine the
    trainer asks serving to let go of the models on the GPU (_release).

    Any failure of the job or of reaching it is a TrainError, which the gate takes for inconclusive: a job that exits
    non-zero, is lost (no exit code, its process gone), outlives deadline_s by two polls, or leaves no well-formed
    result.json. A job this process is waiting on is cancelled when the wait ends in any other way (an exception,
    KeyboardInterrupt), and the first job of a process cancels the jobs of the same workspace a killed process left
    running: there is no reattaching, the round runs again.

    `cloud` ([trainer.cloud], cloud.py) runs each job on a new RunPod or Vast.ai instance instead of a host: it is
    started, tagged with the workspace and the job, waited for (its start.sh done) within boot_timeout_s, checked and
    initialized like any host, runs the job over ssh, and is stopped however the job ends. The first job of a process
    stops the instances a killed run of the same workspace left. Its models go to a store that outlives the instance
    ("hf"); what the store needs (its ENV: HF_TOKEN) reaches the instance as environment variables, the API key
    never leaves this process. A provider or boot failure is a TrainError, before anything is used up. `api`: an
    httpx transport for the provider's API (tests)."""
    _swept: set = set()                 # (host, jobs_root, workspace) whose leftover jobs this process cancelled
    _stopped: set = set()               # (provider, tag) whose leftover instances this process stopped

    def __init__(self, plugin: str, options: dict | None = None, host: str | None = None,
                 jobs_root: str = "~/.ouroloop/jobs", store=None, serving=None, deadline_s: float = 28800,
                 poll_s: float = 30, ssh_opts: str = "-o BatchMode=yes", publish_s: float = 900,
                 cloud: dict | None = None, api=None, runner=None, sleep=time.sleep):
        from .hosts import PYTHON
        from ..serving import Serving
        from ..stores import make
        from ..trainers import load
        from .compute import Compute
        self.name, self.plugin, self.options = plugin, load(plugin), dict(options or {})
        self.supports_weights = bool(getattr(self.plugin, "SUPPORTS_WEIGHTS", False))
        self.recipe_defaults = {**DEFAULT_RECIPE, **dict(getattr(self.plugin, "RECIPE", {}))}
        self.path_keys = PATH_KEYS | set(getattr(self.plugin, "PATH_KEYS", ()))
        self.cloud, self.provider = None, None
        if cloud is not None:
            from . import cloud as clouds
            if host is not None:
                raise ValueError("[trainer] host and [trainer.cloud]: a job runs on one or the other")
            self.cloud = clouds.check(cloud)
            self.provider = clouds.make(self.cloud, api, sleep)
        remote = host not in (None, "local") or self.cloud is not None
        where = host or "a cloud instance"
        if remote and not jobs_root.startswith(("/", "~")):     # a job's cwd is its directory, not the home
            raise ValueError(f"jobs_root {jobs_root!r}: absolute, or from ~ (the home on {where})")
        if not remote:
            jobs_root = os.path.abspath(os.path.expanduser(jobs_root))
        store = store if hasattr(store, "publish") else make(store or {"type": "dir", "root": jobs_root})
        self.store = store if remote else store.here()          # a job on this machine: its paths, from here
        if self.cloud is not None and self.store.ON_TRAINING_HOST:
            raise ValueError(f'[trainer.cloud] needs a [store] that outlives the instance (type "hf"), not '
                             f'{self.store.TYPE!r}: a cloud instance is terminated when its job ends, and its run '
                             f'with it')
        root = str(getattr(self.store, "root", "/"))
        if self.store.ON_TRAINING_HOST and remote and not root.startswith(("/", "~")):
            raise ValueError(f"[store] root {root!r}: absolute, or from ~ (the home on {where})")
        self.serving = serving if isinstance(serving, Serving) else Serving(serving, self.store, {plugin: self.options})
        if remote and self.store.ON_TRAINING_HOST:
            # Serving loads a ref from the store in this process, and this machine never holds those runs (the url
            # mode that reached a model server there went in P1 step 5).
            raise ValueError(f"the models trained on {where} stay there (a {self.store.TYPE!r} store), and serving "
                             f"loads them in this process: use a [store] of type \"hf\" to load them here")
        self.python = PYTHON if remote else os.path.expanduser(PYTHON) if host == "local" else sys.executable
        self.host, self.jobs_root = host, jobs_root
        self.deadline_s, self.poll_s, self.sleep, self.ssh_opts = float(deadline_s), float(poll_s), sleep, ssh_opts
        self._base_ssh = ssh_opts
        self.publish_s = 0.0 if self.store.in_place(jobs_root) else float(publish_s)    # a copy or an upload: time
        self.runner = runner
        self._instance_id = None            # the cloud instance the job at hand runs on
        self.compute = Compute(host if remote and not self.cloud else None, jobs_root, ssh_opts, runner)

    def train(self, job: TrainJob) -> TrainResult:
        job_id, spec, result, where = self._run(job, check_only=False)
        ref = result.get("model")
        if self.store.job(ref) != job_id:
            raise TrainError(f"job {job_id}: result.json names model {ref!r}, not this job's in the store")
        backend = {"type": "model", "model": ref, "trainer": self.name,
                   **({"options": self.options} if self.options else {})}
        return TrainResult(ref, backend, {"id": job_id, "host": spec["host"], "dir": self.compute.dir(job_id),
                                          "spec": spec, "result": result, **where})

    def dry_run(self, job: TrainJob) -> dict:
        """Everything up to training, as a job on the training machine: the plugin's validate. Uses no GPU."""
        job_id, spec, result, where = self._run(job, check_only=True)
        return {"job": job_id, "host": spec["host"], "dir": self.compute.dir(job_id), **where, **result}

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
            with _signals_raise():      # a job on a host is cancelled on a signal too, not only a cloud instance
                return (job_id, *self._job(job, job_id, parent, workspace, check_only), {})
        where = {"cloud": {"provider": self.cloud["provider"]}}
        with self._instance(workspace, job_id, where["cloud"], sweep=not check_only):
            spec, result = self._job(job, job_id, parent, workspace, check_only)
        return job_id, spec, result, where

    def _job(self, job: TrainJob, job_id: str, parent: str | None, workspace: str,
             check_only: bool) -> tuple[dict, dict]:
        """The job on the host that self.compute reaches, prepared: launch, wait, its result."""
        from ...release import code_version
        from .worker import git_state
        spec = {"job": job_id, "version": job.version, "parent": parent, "trainer": self.name,
                "options": self.options, "recipe": job.recipe, "host": self.host, "jobs_root": self.jobs_root,
                "deadline_s": self.deadline_s, "publish_s": self.publish_s, "store": self.store.table,
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
        always stopped afterwards, with `record` (the card's) filled in: instance, gpu, price_per_hour, uptime_s,
        and stop_failed, the instances that could not be stopped. With `sweep`, first the instances a killed run of
        the workspace left (a dry run may start beside a live job, whose instance is not a leftover).

        What bounds its life when this process cannot stop it: SIGTERM and SIGHUP end the job by the same path as an
        exception (so the instance is stopped), and the instance ends its own container after OUROLOOP_TTL_S
        (start.sh): the boot, a host init and the job's deadline, with a margin. Only kill -9 of this process and a
        provider that keeps a stopped container billing outlast both; the next job of the workspace stops those."""
        from .hosts import INIT_TIMEOUT_S
        from . import cloud as clouds
        from .compute import Compute
        name, provider, opts = self.cloud["provider"], self.provider, self.cloud
        tag = clouds.tag(workspace)
        if sweep:
            self._stop_leftovers(tag)
        ttl = float(opts.get("boot_timeout_s", 900)) + INIT_TIMEOUT_S + self.deadline_s + 2 * self.poll_s + TTL_MARGIN_S
        env = {"PUBLIC_KEY": self._public_key(), "OUROLOOP_TTL_S": str(int(ttl))}
        for key in self.store.ENV:              # what publishing to the store needs there
            value = os.environ.get(key, "").strip()
            if not value:
                raise TrainError(f'{key} is not set here: a cloud job publishes its run to the {self.store.TYPE!r} '
                                 f'store, and the instance gets {key} from this environment')
            env[key] = value
        id, began = None, None
        with tempfile.TemporaryDirectory() as tmp, _signals_raise():
            try:
                try:
                    id = provider.start(tag, job_id, opts, env)
                    began = time.monotonic()
                    self._instance_id = id
                    record.update({"instance": id, **{k: v for k, v in provider.placed.get(id, {}).items()
                                                      if k in ("gpu", "price_per_hour")}})
                    say(f"{name}: started instance {id} for job {job_id} ({record.get('gpu')}, "
                        f"${record.get('price_per_hour') or 0:.3f}/hr)")
                    if f"{tag}-{job_id}" in provider.unsure:       # a create that lost its answer may have made two
                        for other in provider.running(f"{tag}-{job_id}"):
                            if other != id:
                                say(f"{name}: stopping instance {other}, a second one made for job {job_id}")
                                provider.stop(other)
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
                self._instance_id = None
                try:
                    self._stop(tag, job_id, id, record)
                finally:
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
        """Wait for the instance, within boot_timeout_s: the provider's word, then an ssh answer that start.sh has
        finished (its marker, STARTED): on Vast.ai sshd is up before start.sh ran, and start.sh still installs
        rsync, writes the Hugging Face token and records host.json."""
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
                    Remote(host, self._instance_ssh(port, known_hosts), self.runner).ssh(
                        f'test -f "$HOME"/{STARTED}', timeout=60)
                    return host, port
                except (OSError, subprocess.SubprocessError):
                    pass                    # sshd not up yet, or start.sh not done
            if max(time.monotonic() - t0, slept) >= limit:
                raise clouds.CloudError(f"{self.cloud['provider']}: instance {id} did not answer ssh within "
                                        f"{limit:.0f}s (boot_timeout_s)")
            self.sleep(poll)
            slept += poll

    def _stop(self, tag: str, job_id: str, id: str | None, record: dict) -> None:
        """Stop the job's instance first, then any other the provider made for this job (a create whose answer was
        lost, or one whose id start() never returned). One that cannot be stopped is said loudly and left to the
        next sweep of the workspace. A KeyboardInterrupt (or a signal) meanwhile is held until all is tried, then
        raised. The ids that could not be stopped go into `record` (the card's): stop_failed."""
        name, failed, held = self.cloud["provider"], [], None

        def stop(i: str) -> None:
            nonlocal held
            try:
                self.provider.stop(i)
                say(f"{name}: stopped instance {i}")
            except BaseException as e:  # noqa: BLE001 - in a finally: nothing may hide the job's own outcome
                if not isinstance(e, Exception):
                    held = held or e
                failed.append(i)
                record["stop_failed"] = failed
                say(f"{name}: COULD NOT STOP instance {i} (job {job_id}); it may still bill: {e!r}. It is stopped "
                    f"by the next job of this workspace, or stop it in the {name} console")
                JobTrainer._stopped.discard((name, tag))

        if id:
            stop(id)
        try:
            others = [i for i in self.provider.running(f"{tag}-{job_id}") if i != id]
        except BaseException as e:  # noqa: BLE001 - the same
            if not isinstance(e, Exception):
                held = held or e
            others = []
            say(f"{name}: could not list the instances of job {job_id}: {e!r}; the next job of this workspace "
                f"looks again")
            JobTrainer._stopped.discard((name, tag))
        for i in others:
            stop(i)
        if held is not None:
            raise held

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
        from .hosts import HostError, ensure
        try:
            ensure(self.host, {self.name: self.options}, self.ssh_opts, self.store, self.runner,
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
                self._alive(job_id, where)
                if status.state == "lost":
                    raise TrainError(f"job {job_id} on {where} was lost: no exit code, and its process is gone "
                                     f"(a reboot, kill -9)")
                if time.time() > deadline:
                    raise TrainError(f"job {job_id} on {where} passed its deadline ({self.deadline_s:.0f}s)")
                self.sleep(self.poll_s)
        except BaseException:
            if self.cloud is None:          # a cloud instance is stopped at once, with its job: no ssh to wait on
                self._cancel(job_id, workspace)
            raise

    def _alive(self, job_id: str, where: str) -> None:
        """On a cloud instance, the provider's word that it still runs, each poll: an instance that is gone (a Vast.ai
        host offline, a RunPod pod exited) fails the job now, not at its deadline. An API that does not answer says
        nothing."""
        if self.cloud is None or self._instance_id is None:
            return
        from . import cloud as clouds
        try:
            self.provider.ready(self._instance_id)
        except clouds.Gone as e:
            raise TrainError(f"job {job_id} on {where}: {e}") from None
        except clouds.CloudError:
            pass

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
        """Let go of the models resident on the GPU before a job needs it, only where they share the job's machine:
        this process's, for a job on this machine (no host, or "local"). A job on a host elsewhere leaves them
        loaded."""
        if self.host not in (None, "local"):
            return
        release = getattr(self.plugin, "release", None)
        if callable(release):
            release()
        try:
            self.serving.release()
        except Exception as e:  # noqa: BLE001 - a server that cannot let go leaves the job less GPU, no more
            print(f"trainer: serving did not release its models: {_why(e)}", file=sys.stderr)


STARTED = ".ouroloop/started"          # under the home: start.sh writes it when it is done, on each boot
TTL_MARGIN_S = 1800                     # an instance's life beyond its boot, a host init and its job's deadline


@contextlib.contextmanager
def _signals_raise():
    """SIGTERM and SIGHUP, which would end this process without running a finally, raise SystemExit instead while a
    job runs (in the main thread only: signal handlers are set there). Without this, the way an unattended loop is
    usually stopped (systemd, docker stop, a kill) leaves the training job running and holding the GPU."""
    import threading
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def ended(signum, frame):
        raise SystemExit(128 + signum)

    before = {sig: signal.signal(sig, ended) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)


def say(message: str) -> None:
    print(f"trainer: {message}", file=sys.stderr)


def _why(e: BaseException) -> str:
    text = (getattr(e, "stderr", None) or str(e) or type(e).__name__).strip()
    return text.splitlines()[-1] if text else type(e).__name__
