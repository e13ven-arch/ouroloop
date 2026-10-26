"""TDE, a small decision model released on its own, as a trainer plugin: TDE's trainer on the host, TDE's Decider in
the process that asks (backends/tde.py).

Options ([trainers.tde]):
    source      where TDE comes from: a TDE checkout on the control plane (host init sends it), or a pip requirement
                such as "git+ssh://git@github.com/your-org/tde.git@v0.1.1"
    init_from   the parent run of a job given no parent model, a path on the host (absolute, or from ~); a job
                with a parent_model_ref starts from that model. A [recipe] init_from does the same.
    device      TDE's device ("cuda", "cpu", ...); by default a host whose torch sees no CUDA device is not ready,
                and "cpu" allows one. Loading a model in-process uses it too, unless [serving] device says otherwise.

The recipe's TDE_KEYS become TDE's TrainConfig; replay (a path on the host) mixes general data in. TDE selects its
checkpoint on val.jsonl, never on reharness's calibration split, and does not weigh examples.

    python -m reharness.train.trainers.tde <config>        TDE's trainer on a config this plugin prepared (the job runs it)
"""
from __future__ import annotations

import json
import os
import random
import sys

NAME = "tde"
REQUIREMENTS: list[str] = []          # TDE brings torch, transformers and the rest: its source
SUPPORTS_WEIGHTS = False
NEEDS_PARENT = True
# TDE's recipe keys and their defaults, for an 8 GB GPU: 1024-token states need batch 2 x accumulation 16 plus
# checkpointing. The framework's own (min_weight, focus, seed, init_from) come on top (train.py).
RECIPE = {
    "epochs": 3.0, "max_steps": None, "lr_backbone": 2e-5, "lr_head": 1e-4, "batch_size": 2, "grad_accum": 16,
    "max_state_tokens": 1024, "gradient_checkpointing": True, "w_rps": 1.0, "w_perm": 0.3,
    "train_limit": None,     # train on at most this many examples, a seeded sample of the ledger's and replay's
    "replay": None,          # general training data on the host, e.g. "~/data/general/train.jsonl"
    "replay_ratio": 1.0,     # replay examples per ledger example
}
PATH_KEYS = {"replay"}       # a file on the host: a person sets it, never a research candidate
TDE_KEYS = ("epochs", "max_steps", "lr_backbone", "lr_head", "batch_size", "grad_accum", "max_state_tokens",
            "gradient_checkpointing", "w_rps", "w_perm", "train_limit", "seed")
ARCHITECTURE = ("backbone", "readout", "marker", "pool", "branch_layers", "branch_through_backbone", "finetune")


def tde_config(recipe: dict, out_dir: str, init_from: str | None, version: str) -> dict:
    cfg = {k: recipe[k] for k in TDE_KEYS if recipe.get(k) is not None}
    cfg.update({"out_dir": out_dir, "readout": "joint", "notes": f"reharness {version}"})
    if init_from:
        cfg["init_from"] = init_from
    return cfg


def prepare_job(data_dir, cfg_json, replay=None, ratio=1.0, seed=0):
    """Write <data_dir>/tde/train.jsonl (ledger examples plus a seeded replay sample), <data_dir>/tde/calibration.jsonl
    (the ledger's val split, which TDE selects its checkpoint on; never reharness's calibration split; shuffled, since
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
            val = [json.dumps({**json.loads(line), "split": "calibration"}, ensure_ascii=False) + "\n"
                   for line in f if line.strip()]           # TDE's name for the split it selects on
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
            for key in ARCHITECTURE:
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


# ---------------------------------------------------------------------- the plugin: worker side
def _host_path(path: str | None, what: str) -> str | None:
    if not path:
        return None
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        raise ValueError(f"{what} {path!r}: a path on the host is absolute, or starts with ~")
    return path


def _prepare(spec: dict, job_dir: str) -> tuple[str, str | None]:
    """-> (the TDE config's path, the parent run)."""
    from ..jobs.worker import parent_run
    recipe = spec["recipe"]
    parent = parent_run(spec, job_dir)
    cfg = tde_config(recipe, os.path.join(job_dir, "run"), parent, spec["version"])
    if (spec.get("options") or {}).get("device"):
        cfg["device"] = spec["options"]["device"]
    replay = _host_path(recipe.get("replay"), "replay")
    return prepare_job(job_dir, json.dumps(cfg), replay, recipe.get("replay_ratio", 1.0), recipe.get("seed", 0)), parent


def validate(spec: dict, job_dir: str) -> dict:
    cfg_path, parent = _prepare(spec, job_dir)
    return {**json.loads(validate_job(cfg_path)), "parent": parent}


def train(spec: dict, job_dir: str, deadline: float) -> str:
    from ..jobs.worker import run
    cfg_path = os.path.join(job_dir, "tde", "config.yaml")
    if not os.path.exists(cfg_path):
        cfg_path, _ = _prepare(spec, job_dir)
    run([sys.executable, "-m", __name__, cfg_path], deadline, cwd=job_dir)
    return os.path.join(job_dir, "run")


def version() -> str | None:
    """TDE's version as installed, and its git HEAD when it is imported from a checkout (-dirty: a tracked file
    differs from it)."""
    import importlib.metadata
    import importlib.util
    from pathlib import Path

    from ..jobs.worker import git_state
    try:
        installed = importlib.metadata.version("tde")
    except importlib.metadata.PackageNotFoundError:
        installed = None
    spec = importlib.util.find_spec("tde")
    git = git_state(Path(spec.origin).parents[1]) if spec and spec.origin else None
    head = f"git {git['head']}{'-dirty' if git['dirty'] else ''}" if git else None
    return " ".join(x for x in (installed, head) if x) or None


def check(options: dict) -> dict:
    """TDE imports, and torch sees a CUDA device: a host without one is not ready (TDE would train on its CPU until
    the deadline), unless the options say device = "cpu"."""
    import tde  # noqa: F401
    import torch
    cuda = torch.cuda.is_available()
    cpu = (options or {}).get("device") == "cpu"
    return {"ok": cuda or cpu, "torch": torch.__version__, "cuda": cuda,
            "device": torch.cuda.get_device_name(0) if cuda else None,
            **({} if cuda or cpu else {"why": 'torch sees no CUDA device (a driver or torch mismatch?); set device = '
                                              '"cpu" in [trainers.tde] to train on the CPU'})}


# ---------------------------------------------------------------------- the plugin: control side
def backend(run: str, ref: str, options: dict):
    """The run the store fetched for a ref, loaded in-process by TDE's Decider (at most two stay loaded)."""
    from ..backends.tde import TdeBackend
    return TdeBackend(run, device=options.get("device"), model=ref)


def release() -> None:
    from ..backends.tde import TdeBackend
    TdeBackend.release()


def _main(cfg_path: str) -> None:
    """TDE's trainer on a prepared config (as TDE's scripts/train.py runs it, without its command line)."""
    from tde.train import TrainConfig, train as tde_train
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    result = tde_train(TrainConfig(**cfg))
    print(json.dumps(result, indent=2, default=str), flush=True)


if __name__ == "__main__":
    _main(sys.argv[1])
