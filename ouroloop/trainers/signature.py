"""The signature model: label frequencies per question and error signature (replay.signature), or per any key of the
view a scenario defines ("module:function", view -> key). Tiny, standard library only: a plugin that runs through
the real job path in the offline tests, and, as SignatureTrainer, in this process for tests and demos.

Options ([trainers.signature]): signature, the key function (it must import on the host too). A run is
self-describing: its model.json holds the key function it was trained with beside the table, so a model server
without these options loads the same model."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

from ..evolve.train import TrainJob, TrainResult
from ..types import Answer, Request

NAME = "signature"
REQUIREMENTS: list[str] = []
SUPPORTS_WEIGHTS = True
RECIPE: dict = {}               # no hyperparameters: the framework's recipe keys only
MODEL = "model.json"            # in the run directory


class SignatureBackend:
    """Predicts the label distribution seen in training for the same question and error signature."""
    name = "signature"

    def __init__(self, path: str | None = None, table: dict | None = None, signature: str | None = None,
                 model: str | None = None):
        """`signature`: "module:function", view -> key, in place of the error signature (replay.signature).
        `model`: the id the answers report; by default signature:<the model file's directory>."""
        self.path = path
        self.table = table if table is not None else json.loads(Path(path).read_text(encoding="utf-8"))
        self.model = model or f"signature:{Path(path).parent.name if path else 'memory'}"
        self.signature = key_function(signature)

    def decide(self, requests: list[Request]) -> list[Answer]:
        out = []
        for r in requests:
            labels = r.question.labels()
            entry = self.table.get(r.question.instructions, {})
            dist = entry.get(self.signature(r.state)) or entry.get("*") or {}
            smoothed = {lab: dist.get(lab, 0.0) + 0.5 for lab in labels}    # add-half smoothing
            out.append(Answer.of(labels, smoothed, self.model))
        return out


def key_function(ref: str | None):
    from ..evolve.replay import signature
    from ..runtime import import_object
    return import_object(ref) if ref else signature


def fit(train: str | Path, key_of) -> dict:
    """question -> key -> label -> weighted count, over the examples in `train`."""
    counts: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    with open(train, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            e = json.loads(line)
            for key in (key_of(e["state"]), "*"):
                for c, t in zip(e["candidates"], e["target"]):
                    counts[e["question"]][key][c["name"]] += t * float(e["meta"].get("weight", 1.0))
    return counts


class SignatureTrainer:
    """The signature model trained in this process, beside the job's data: no job, no host."""
    supports_weights = True

    def __init__(self, signature: str | None = None):
        self.ref = signature

    def train(self, job: TrainJob) -> TrainResult:
        path = Path(job.data_dir).parent / "signature_model.json"
        path.write_text(json.dumps(fit(Path(job.data_dir) / "train.jsonl", key_function(self.ref)), indent=2),
                        encoding="utf-8")
        spec = {"type": "signature", "path": str(path), **({"signature": self.ref} if self.ref else {})}
        return TrainResult(f"signature:{path.parent.name}", spec)


# ---------------------------------------------------------------------- the plugin: worker side
def validate(spec: dict, job_dir: str) -> dict:
    key_function((spec.get("options") or {}).get("signature"))
    counts = {}
    for name in ("train", "val"):
        n = 0
        with open(os.path.join(job_dir, f"{name}.jsonl"), encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    e = json.loads(line)
                    if len(e["candidates"]) != len(e["target"]):
                        raise ValueError(f"{name}.jsonl, example {e.get('id')}: candidates and target differ")
                    n += 1
        counts[name] = n
    return {"ok": True, "counts": counts}


def train(spec: dict, job_dir: str, deadline: float) -> str:
    run = os.path.join(job_dir, "run")
    os.makedirs(run, exist_ok=True)
    ref = (spec.get("options") or {}).get("signature")
    counts = fit(os.path.join(job_dir, "train.jsonl"), key_function(ref))
    with open(os.path.join(run, MODEL), "w", encoding="utf-8") as f:
        json.dump({"signature": ref, "table": counts}, f)
    return run


# ---------------------------------------------------------------------- the plugin: control side
def backend(run: str, ref: str, options: dict) -> SignatureBackend:
    path = os.path.join(run, MODEL)
    if not os.path.exists(path):
        raise ValueError(f"no model {ref!r} in {run}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and set(data) == {"signature", "table"}:        # the run says what it is
        return SignatureBackend(path, table=data["table"], signature=data["signature"], model=ref)
    return SignatureBackend(path, table=data, signature=options.get("signature"), model=ref)   # an older run
