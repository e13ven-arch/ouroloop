"""A training job (PLAN_v0.2 M9), run the same way on this machine and on a training host:

    <python> -m ouroloop.worker spec.json <job id>         in the job's directory, <jobs_root>/<job id>/

(the job id only marks the process, for Compute). The directory holds spec.json, train.jsonl and val.jsonl: the data
contract every trainer reads (evolve/build.py). The worker is installed on the host (`ouroloop host init`), never
copied: it refuses a job made by another ouroloop (spec.ouroloop.code), since the job and the models it yields are
only what that code makes of them.

It loads the trainer plugin spec.trainer names (ouroloop.trainers), records the host manifest and the plugin's
version, has the plugin validate the job (spec.check_only stops there: `ouroloop evolve --dry-run`), trains until the
deadline less publish_s, publishes the run to a private Hugging Face repository with transport "hf", and writes
result.json (by os.replace): {model: the model ref, ouroloop, plugin, host, hf_commit}. The ref is the job id
(transport "direct": the run stays in <job>/run) or <repo>@<commit> (transport "hf").

A plugin's processes run through `run` below: in their own process group, stopped at the deadline, and when the
worker is asked to stop (Compute.cancel).
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time

JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
HF_REF = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40})")
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


def locate(ref: str, jobs_root: str, hf_prefix: str | None = None) -> tuple[str, str | None]:
    """A model ref -> where its run is: (<jobs_root>/<job id>/run, None) for a job id, or (repo, commit) for
    <repo>@<40 hex> whose repo is <hf_prefix><job id>. Anything else is a ValueError: a ref never names a path."""
    m = HF_REF.fullmatch(ref) if isinstance(ref, str) else None
    if m and hf_prefix and m.group(1).startswith(hf_prefix) and JOB_ID.fullmatch(m.group(1)[len(hf_prefix):]):
        return m.group(1), m.group(2)
    if isinstance(ref, str) and JOB_ID.fullmatch(ref):
        return os.path.join(os.path.expanduser(jobs_root), ref, "run"), None
    raise ValueError(f"not a model ref: {ref!r} (a job id, or <{hf_prefix or 'hf_prefix'}job id>@<commit>)")


def parent_run(parent: str | None, jobs_dir: str) -> str | None:
    """The parent model as a directory on this machine: <repo>@<commit> is downloaded at that commit, a job id is its
    run in `jobs_dir` (the directory this job is in), anything else is a path on this machine (absolute, or from ~)."""
    if not parent:
        return None
    m = HF_REF.fullmatch(parent)
    if m:
        from huggingface_hub import snapshot_download
        return snapshot_download(repo_id=m.group(1), revision=m.group(2))
    path = os.path.expanduser(parent)
    if JOB_ID.fullmatch(parent):
        run = os.path.join(jobs_dir, parent, "run")
        if os.path.isdir(run):
            return run
        if not os.path.exists(path):
            raise FileNotFoundError(f"parent {parent!r}: no run {run}, and no {path}")
    if not os.path.isabs(path):
        raise ValueError(f"parent {parent!r}: a path on the training machine is absolute, or starts with ~")
    return path


def publish(run: str, job_dir: str, repo: str) -> str:
    """One commit to a new private repository: the run at its root, the job's spec and data under job/. -> the
    commit's sha."""
    from huggingface_hub import CommitOperationAdd, HfApi
    api = HfApi()
    api.create_repo(repo, private=True, exist_ok=False)
    ops = []
    for base, _, names in os.walk(run):
        for name in sorted(names):
            path = os.path.join(base, name)
            ops.append(CommitOperationAdd(path_in_repo=os.path.relpath(path, run).replace(os.sep, "/"),
                                          path_or_fileobj=path))
    for name in ("spec.json", "train.jsonl", "val.jsonl"):
        ops.append(CommitOperationAdd(path_in_repo=f"job/{name}", path_or_fileobj=os.path.join(job_dir, name)))
    info = api.create_commit(repo, operations=ops, commit_message=f"ouroloop {os.path.basename(job_dir)}")
    return info.oid


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
    """What this host is: ~/.ouroloop/host.json when this Python is the one `ouroloop host init` made, else what can
    be read now."""
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
    from .trainers import load, version
    mine, want = code_version(), (spec.get("ouroloop") or {}).get("code")
    fix = f"run `ouroloop host init {spec.get('host') or 'local'}` from the control plane"
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
    ref, commit = spec["job"], None
    if spec.get("transport") == "hf":
        commit = publish(run_dir, job_dir, spec["hf_repo"])
        ref = f"{spec['hf_repo']}@{commit}"
    _result(job_dir, {"model": ref, **base, "hf_commit": commit})
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
