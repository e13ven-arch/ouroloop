"""A training job (PLAN_v0.2 M9), run the same way on this machine and on a training host:

    <python> worker.py spec.json <job id>         in the job's directory, <jobs_root>/<job id>/

(the job id only marks the process, for Compute).

It records the TDE checkout it trains with (git HEAD and whether tracked files differ from it), resolves the parent
model, prepares the data and TDE's config, has TDE validate them (spec.check_only stops there: `ouroloop evolve
--dry-run`), runs TDE's scripts/train.py until the deadline less publish_s, publishes the run to a private Hugging
Face repository with transport "hf", and writes result.json: {model: the model ref, tde_head, tde_dirty,
hf_commit}. The ref is the job id (transport "direct": the run stays in <job>/run) or <repo>@<commit> (transport
"hf").

Standard library and the tde package only (huggingface_hub with transport "hf"): Compute copies this one file into
every job directory, and ouroloop imports it for the same resolution of model refs (locate).
"""
from __future__ import annotations

import json
import os
import random
import re
import signal
import subprocess
import sys
import time

TDE_KEYS = ("epochs", "max_steps", "lr_backbone", "lr_head", "batch_size", "grad_accum", "max_state_tokens",
            "gradient_checkpointing", "w_rps", "w_perm", "train_limit", "seed")
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
HF_REF = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40})")
GRACE_S = 3.0           # between asking a training process group to stop and killing it


def tde_config(recipe: dict, out_dir: str, init_from: str | None, version: str) -> dict:
    cfg = {k: recipe[k] for k in TDE_KEYS if recipe.get(k) is not None}
    cfg.update({"out_dir": out_dir, "readout": "joint", "notes": f"ouroloop {version}"})
    if init_from:
        cfg["init_from"] = init_from
    return cfg


def prepare_job(data_dir, cfg_json, replay=None, ratio=1.0, seed=0):
    """Write <data_dir>/tde/train.jsonl (ledger examples plus a seeded replay sample), <data_dir>/tde/calibration.jsonl
    (the ledger's val split, which TDE selects its checkpoint on; never ouroloop's calibration split; shuffled, since
    TDE reads only its first eval_limit rows, and in ledger order those are the oldest sessions) and the TDE config,
    taking the architecture from the parent run. Paths may start with ~ (the training machine's home). Returns the
    config path."""
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
    val = []                                # TDE reads the file; empty, it selects none
    if os.path.exists(os.path.join(data_dir, "val.jsonl")):
        with open(os.path.join(data_dir, "val.jsonl"), encoding="utf-8") as f:
            val = [line if line.endswith("\n") else line + "\n" for line in f if line.strip()]
    random.Random(seed).shuffle(val)
    with open(os.path.join(out, "calibration.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(val)
    cfg = json.loads(cfg_json)
    cfg["data_dir"] = out
    cfg["out_dir"] = os.path.expanduser(cfg["out_dir"])     # TDE takes it as is: "~/x" would be a directory named ~
    if cfg.get("init_from"):
        cfg["init_from"] = os.path.expanduser(cfg["init_from"])
        parent = os.path.join(cfg["init_from"], "config.json")
        if os.path.exists(parent):
            with open(parent, encoding="utf-8") as f:
                pc = json.load(f)
            for key in ("backbone", "readout", "marker", "pool", "branch_layers", "branch_through_backbone",
                        "finetune"):
                if key in pc and key not in cfg:
                    cfg[key] = pc[key]
    path = os.path.join(out, "config.yaml")   # JSON is valid YAML
    with open(path, "w", encoding="utf-8") as f:
        f.write(yaml_json(cfg))
    return path


def yaml_json(value, depth: int = 0) -> str:
    """JSON that YAML reads back with the same types. PyYAML takes a float without a dot in its mantissa, such as
    json's 2e-05, for a string, and TDE's trainer then multiplies that string by a float."""
    pad, inner = "  " * depth, "  " * (depth + 1)
    if isinstance(value, dict) and value:
        return "{\n" + ",\n".join(f"{inner}{json.dumps(str(k))}: {yaml_json(v, depth + 1)}"
                                    for k, v in value.items()) + f"\n{pad}}}"
    if isinstance(value, (list, tuple)) and value:
        return "[\n" + ",\n".join(f"{inner}{yaml_json(v, depth + 1)}" for v in value) + f"\n{pad}]"
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        mantissa, e, exponent = repr(value).partition("e")
        return f"{mantissa}.0e{exponent}" if e and "." not in mantissa else repr(value)
    return json.dumps(value)


def validate_job(cfg_path):
    """Check a prepared job against TDE itself (config fields, example schema, parent run) without training. Needs
    the tde package; imports torch but allocates nothing on the GPU."""
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


def resolve_parent(parent: str | None, jobs_root: str, tde_repo: str) -> str | None:
    """The parent run as a directory on this machine: <repo>@<commit> is downloaded at that commit, a job id under
    jobs_root (the directory this job is in) is its run, anything else is a path (relative to the TDE checkout). A
    job id with neither a run nor such a path is an error."""
    if not parent:
        return None
    m = HF_REF.fullmatch(parent)
    if m:
        from huggingface_hub import snapshot_download
        return snapshot_download(repo_id=m.group(1), revision=m.group(2))
    path = os.path.join(os.path.expanduser(tde_repo), os.path.expanduser(parent))
    if JOB_ID.fullmatch(parent):
        run = os.path.join(jobs_root, parent, "run")
        if os.path.isdir(run):
            return run
        if not os.path.exists(path):
            raise FileNotFoundError(f"parent {parent!r}: no run {run}, and no {path}")
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
    """In TDE's process, on Linux: be killed when the worker dies, even by kill -9, which never reaches its handler."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)       # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def run_training(tde_repo: str, cfg_path: str, timeout: float) -> int:
    """TDE's scripts/train.py in its own process group, stopped at `timeout` seconds or when this worker is asked to
    stop (Compute.cancel). -> its exit code; 124 when it ran out of time."""
    proc = subprocess.Popen([sys.executable, "scripts/train.py", "--config", cfg_path], cwd=tde_repo,
                            start_new_session=True, preexec_fn=_with_worker)

    def asked(signum, frame):
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, asked)
    try:
        return proc.wait(timeout=max(timeout, 0.0))
    except subprocess.TimeoutExpired:
        print(f"worker: training ran out of time ({timeout:.0f}s left at its start); stopping it", flush=True)
        return 124
    finally:
        _stop(proc)


def main(path: str) -> int:
    start = time.time()
    job_dir = os.path.dirname(os.path.abspath(path))
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    repo = os.path.expanduser(spec["tde_repo"])
    tde = git_state(repo) or {}
    base = {"tde_head": tde.get("head"), "tde_dirty": tde.get("dirty")}
    print(f"worker: job {spec['job']}, TDE {base['tde_head']}{' (dirty)' if base['tde_dirty'] else ''}", flush=True)
    sys.path.insert(0, repo)                # validate with the checkout that trains
    parent = resolve_parent(spec.get("parent"), os.path.dirname(job_dir), repo)
    run = os.path.join(job_dir, "run")
    cfg = tde_config(spec["tde"], run, parent, spec["version"])
    replay = spec.get("replay") and os.path.join(repo, os.path.expanduser(spec["replay"]))
    cfg_path = prepare_job(job_dir, json.dumps(cfg), replay, spec.get("replay_ratio", 1.0), spec.get("seed", 0))
    check = json.loads(validate_job(cfg_path))
    print(f"worker: validated {json.dumps(check)}", flush=True)
    if spec.get("check_only"):
        _result(job_dir, {**base, "check": check, "parent": parent})
        return 0
    code = run_training(repo, cfg_path, spec["deadline_s"] - (time.time() - start) - spec.get("publish_s", 0))
    if code != 0:
        print(f"worker: training exited with {code}", flush=True)
        return code
    ref, commit = spec["job"], None
    if spec.get("transport") == "hf":
        commit = publish(run, job_dir, spec["hf_repo"])
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
    sys.exit(main(sys.argv[1]))
