"""A training job (docs/DESIGN.md §2.2, pipeline 2), run the same way on this machine and on a training host:

    <python> -m ouroloop.worker spec.json <job id>         in the job's directory, <jobs_root>/<job id>/

(the job id only marks the process, for Compute). The directory holds spec.json, train.jsonl and val.jsonl: the data
contract every trainer reads (train/dataset.py). The worker is installed on the host (`python -m ouroloop.train host
init`), never copied: it refuses a job made by another ouroloop (spec.ouroloop.code), since the job and the models it
yields are only what that code makes of them.

It loads the trainer plugin spec.trainer names (ouroloop/train/trainers), records the host manifest and the plugin's
version, has the plugin validate the job (spec.check_only stops there: a dry run), trains until the
deadline less publish_s, publishes the run to the model store spec.store names (ouroloop/train/stores; without one, a "dir"
store in the jobs' directory), and writes result.json (by os.replace): {model: the ref the store gave, ouroloop,
plugin, host}.

A plugin's processes run through `run` below: in their own process group, stopped at the deadline, and when the
worker is asked to stop (Compute.cancel).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

GRACE_S = 3.0           # between asking a plugin's process group to stop and killing it
REFUSED = 3             # the exit code of a job this host cannot run: another ouroloop, or a plugin missing


class Failed(Exception):
    """A plugin's training failed: the job exits with `code` (124: it ran out of time)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def git_state(path) -> dict | None:
    """{"head", "dirty"} of the git checkout at `path` (dirty: a tracked file differs from HEAD), or None if it is
    not one."""
    try:
        head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
        status = subprocess.run(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return {"head": head, "dirty": bool(status.strip())}


def store(spec: dict, job_dir: str):
    """The model store of the job: spec.store, or a "dir" store in the jobs' directory."""
    from .train.stores import make
    return make(spec.get("store") or {"type": "dir", "root": os.path.dirname(os.path.abspath(job_dir))})


def parent_run(spec: dict, job_dir: str) -> str | None:
    """The job's parent model as a directory on this machine: a ref of the job's store is fetched from it, anything
    else is a path on this machine (absolute, or from ~)."""
    parent = spec.get("parent")
    if not parent:
        return None
    models = store(spec, job_dir)
    if models.job(parent):
        return models.fetch(parent)
    path = os.path.expanduser(parent)
    if not os.path.isabs(path):
        raise ValueError(f"parent {parent!r}: not a model of the store, and a path on the training machine is "
                         f"absolute, or starts with ~")
    return path


# ---------------------------------------------------------------------- a plugin's processes
def _stop(proc: subprocess.Popen) -> None:
    """Terminate the process group `proc` leads, then kill it after a grace period."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=GRACE_S)
        except subprocess.TimeoutExpired:
            continue
        try:                                # the leader is gone; what it started may not be
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return


def _with_worker() -> None:
    """In a plugin's process, on Linux: be killed when the worker dies, even by kill -9, which never reaches its
    handler."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)       # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def run(args: list, deadline: float, **popen) -> None:
    """Run `args` (no shell) in its own process group until it exits or the time is `deadline` (time.time()), when
    it is stopped: Failed(124). A non-zero exit is Failed(code)."""
    proc = subprocess.Popen(args, start_new_session=True, preexec_fn=_with_worker, **popen)
    left = deadline - time.time()
    try:
        code = proc.wait(timeout=max(left, 0.0))
    except subprocess.TimeoutExpired:
        raise Failed(124, f"training ran out of time ({left:.0f}s left at its start); stopping it") from None
    finally:
        _stop(proc)
    if code != 0:
        raise Failed(code, f"training exited with {code}")


# ---------------------------------------------------------------------- the job
def manifest() -> dict:
    """What this host is: ~/.ouroloop/host.json when this Python is the one `python -m ouroloop.train host init`
    made, else what can be read now."""
    from .hosts import MANIFEST, VENV, gpus
    if os.path.realpath(sys.prefix) == os.path.realpath(os.path.expanduser(VENV)):
        try:
            with open(os.path.expanduser(MANIFEST), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    import platform
    return {"python": platform.python_version(), "platform": platform.platform(), "gpu": gpus()}


def main(path: str) -> int:
    start = time.time()
    job_dir = os.path.dirname(os.path.abspath(path))
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    from .release import code_version
    from .train.trainers import load, version
    mine, want = code_version(), (spec.get("ouroloop") or {}).get("code")
    fix = f"run `python -m ouroloop.train host init {spec.get('host') or 'local'}` from the control plane"
    if mine != want:
        print(f"worker: this host runs ouroloop {mine}, the job was made by ouroloop {want}: {fix}", flush=True)
        return REFUSED
    try:
        plugin = load(spec["trainer"])
    except ImportError as e:
        print(f"worker: no trainer plugin {spec['trainer']!r} here ({e}): {fix} --trainer {spec['trainer']}",
              flush=True)
        return REFUSED

    def asked(signum, frame):
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, asked)
    base = {"ouroloop": mine, "plugin": {"name": spec["trainer"], "version": version(plugin)}, "host": manifest()}
    print(f"worker: job {spec['job']}, trainer {spec['trainer']} {base['plugin']['version']}", flush=True)
    check = plugin.validate(spec, job_dir)
    print(f"worker: validated {json.dumps(check)}", flush=True)
    if spec.get("check_only"):
        _result(job_dir, {**base, "check": check})
        return 0
    try:
        run_dir = plugin.train(spec, job_dir, start + spec["deadline_s"] - spec.get("publish_s", 0))
    except Failed as e:
        print(f"worker: {e}", flush=True)
        return e.code
    try:
        ref = store(spec, job_dir).publish(run_dir, job_dir, spec["job"])
    except ValueError as e:
        print(f"worker: trainer {spec['trainer']}: {e}", flush=True)
        return 1
    _result(job_dir, {"model": ref, **base})
    print(f"worker: done, model {ref}", flush=True)
    return 0


def _result(job_dir: str, result: dict) -> None:
    tmp = os.path.join(job_dir, "result.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f)
    os.replace(tmp, os.path.join(job_dir, "result.json"))


if __name__ == "__main__":
    from ouroloop import worker         # the module plugins import (its Failed), not this __main__ copy of it
    sys.exit(worker.main(sys.argv[1]))
