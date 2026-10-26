"""Training an artifact (docs/modules/02_EVIDENCE_TRAINING.md §5): the trainer contract, the recipe, and
train_artifact, which turns a built dataset (dataset.py) into a model artifact and checks that it loads, with no
promotion side effect.

Any trainer plugs in through the Trainer protocol below: train(job) -> TrainResult, and supports_weights. A trainer
only trains: its model is asked through serving (serving.py), by the backend spec on its result. The plugins
(trainers/) run as jobs on this machine, a training host or a cloud instance (reharness/train/jobs/: JobTrainer); an
in-process trainer (trainers/signature.SignatureTrainer, a "module:Class") needs none of that.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# The recipe's keys the framework reads itself (full_recipe, the job's parent). A plugin adds its trainer's own
# (trainers/: RECIPE, PATH_KEYS); a trainer's recipe_defaults are both, and only those keys are accepted.
DEFAULT_RECIPE = {
    "min_weight": 0.5,       # 1.0 keeps only environment and human labels
    "init_from": None,       # the first model's parent run on the training machine (or the plugin's init_from)
    "focus": None,           # share of the ledger's training rows that are default errors (the v1 pipeline)
    "seed": 0,
}
# Files and runs on the training machine: set by a person in [recipe], never by a research candidate, which could
# name the calibration split, or a run trained on the test sessions.
PATH_KEYS = {"init_from"}


def recipe_defaults(trainer) -> dict:
    """The recipe keys `trainer` accepts, with their defaults: the framework's, and its plugin's."""
    return getattr(trainer, "recipe_defaults", DEFAULT_RECIPE)


def path_keys(trainer) -> set:
    """The recipe keys of `trainer` that name files or runs on the training machine."""
    return getattr(trainer, "path_keys", PATH_KEYS)


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
    backend: dict           # the backend spec serving asks it by (serving.py): {"type": "model", ...} for a stored one
    job: dict | None = None     # the training job: id, host, dir, spec, result


class Trainer(Protocol):
    supports_weights: bool      # it weighs each example by meta.weight; if not, it is given weight-1.0 rows only

    def train(self, job: TrainJob) -> TrainResult: ...


def full_recipe(trainer, recipe: dict | None) -> dict:
    """`recipe` over the trainer's defaults (the framework's keys and its plugin's); any other key is a ValueError."""
    defaults = recipe_defaults(trainer)
    unknown = set(recipe or {}) - set(defaults)
    if unknown:
        raise ValueError(f"unknown recipe keys: {sorted(unknown)} (this trainer's: {sorted(defaults)})")
    recipe = {**defaults, **(recipe or {})}
    if recipe["focus"] is not None and not 0 < recipe["focus"] <= 1:
        raise ValueError(f"recipe focus {recipe['focus']!r}: a share in (0, 1]")
    return recipe


def check_change(change: dict, trainer=None) -> None:
    """A research candidate's recipe change: hyperparameters of `trainer` only (recipe_defaults: the framework's and
    its plugin's), never the files and runs training reads (path_keys), each of the type of its default (an int for
    a float; a number for one that is None by default). Raises ValueError before any job starts."""
    defaults = recipe_defaults(trainer)
    paths = sorted(set(change) & path_keys(trainer))
    if paths:
        raise ValueError(f"{paths} name data or runs: a person sets them in [recipe]")
    unknown = sorted(set(change) - set(defaults))
    if unknown:
        raise ValueError(f"unknown recipe keys: {unknown}")
    for key, value in change.items():
        default = defaults[key]
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        ok = (isinstance(value, bool) if isinstance(default, bool)
              else number and not isinstance(value, float) if isinstance(default, int)
              else number)                  # a float, or None by default
        if not ok:
            want = {bool: "true or false", int: "an integer"}.get(type(default), "a number")
            raise ValueError(f"recipe {key} = {value!r}: must be {want} (default {default!r})")


# ---------------------------------------------------------------------------- training without promotion
# docs/modules/02_EVIDENCE_TRAINING.md §5. The v1 round (evolve/pipeline.py's prepare_model) did all of it: data,
# checks, training, calibration, the gate record and the champion pointer. The task profile needs only the middle of
# that — an artifact and a load probe — and must not touch a pointer, consume a gate record or run an outer test,
# because in that profile what promotes a version is a full-task receipt (M3), not anything here.
@dataclass
class TrainingBuild:
    status: str = "ready"                   # ready | failed | cancelled
    model_ref: str | None = None            # the store's ref, in the store's own format
    job_ref: dict | None = None
    recipe_ref: str = ""
    dataset_ref: str = ""
    artifact_ref: str | None = None         # the recorded artifact: model id, backend spec, job
    metrics: dict = field(default_factory=dict)
    costs: dict = field(default_factory=dict)
    errors: tuple = ()
    backend_spec: dict | None = None

    def __post_init__(self) -> None:
        if self.status not in ("ready", "failed", "cancelled"):
            raise ValueError(f"training status {self.status!r}")
        if self.status == "ready" and not self.model_ref:
            raise ValueError("a ready training build names its model")
        if self.status != "ready" and not self.errors:
            raise ValueError("a training build that is not ready says why")


def train_artifact(rt, trainer, dataset, recipe: dict | None = None, *, parent_model_ref: str | None = None,
                   objects=None, budget=None, operation=None, version: str | None = None,
                   probe: bool = True) -> TrainingBuild:
    """Train one artifact from a built dataset and check that it loads. -> TrainingBuild.

    The budget is reserved before the job is submitted, not after it returns: a job that was submitted has
    already committed the GPU time whether or not anyone measured it. A failed or abandoned job still costs, so
    its reservation settles rather than disappearing.

    On a restart, an operation that already carries a job id is queried through the trainer's `attach`; when the
    remote state cannot be read, this returns `failed` with that reason instead of submitting a second job, since
    two jobs are two bills and only one of them was ever planned (DT-08).
    """
    from .. import contracts as ct
    objects = objects or ct.Objects(rt.root)
    if getattr(dataset, "status", "ready") != "ready":
        return TrainingBuild(status="failed", dataset_ref=getattr(dataset, "dataset_ref", "") or "",
                             errors=(f"the dataset is {dataset.status}",))
    recipe = full_recipe(trainer, recipe)
    recipe_ref = objects.put_json("change", {"recipe": recipe, "trainer": getattr(trainer, "name", "")})
    version = version or f"t_{ct.digest({'dataset': dataset.dataset_ref, 'recipe': recipe_ref})[:12]}"
    reservation = None
    if budget is not None:
        reservation = budget.reserve(f"train:{version}", {"train_jobs": 1,
                                                          "training_seconds": recipe.get("max_seconds")})
    started = time.monotonic()
    saved_job = (operation.attempts[-1] if operation is not None and operation.attempts else {}) or {}
    try:
        if saved_job.get("job_id") and hasattr(trainer, "attach"):
            result = trainer.attach(saved_job["job_id"])
            if result is None:
                raise TrainingUnknown(f"job {saved_job['job_id']} is still unknown: query it before submitting "
                                      f"another one")
        else:
            result = trainer.train(TrainJob(version, dataset.dir, recipe, parent_model_ref))
    except BaseException as e:  # noqa: BLE001 - a failed job still costs, and its artifacts are not "ready"
        if reservation is not None:
            budget.settle(reservation.id, {"train_jobs": 1,
                                           "training_seconds": round(time.monotonic() - started, 1)},
                          "settled" if isinstance(e, (TrainingUnknown, ValueError)) else "unknown")
        if isinstance(e, KeyboardInterrupt):
            return TrainingBuild(status="cancelled", dataset_ref=dataset.dataset_ref or "",
                                 recipe_ref=recipe_ref, errors=("cancelled",))
        return TrainingBuild(status="failed", dataset_ref=dataset.dataset_ref or "", recipe_ref=recipe_ref,
                             errors=(_reason(e),))
    seconds = round(time.monotonic() - started, 1)
    receipt = budget.settle(reservation.id, {"train_jobs": 1, "training_seconds": seconds}) if reservation else None
    metrics, errors = {"train_seconds": seconds, **(result.job or {}).get("result", {})}, ()
    if probe:
        errors = _probe_artifact(rt, result, dataset)
    artifact = objects.put_json("change", {"model_id": result.model_id, "backend": result.backend,
                                          "job": result.job, "dataset": dataset.dataset_ref,
                                          "recipe": recipe_ref})
    if errors:
        return TrainingBuild(status="failed", model_ref=None, job_ref=result.job, recipe_ref=recipe_ref,
                             dataset_ref=dataset.dataset_ref or "", metrics=metrics, errors=tuple(errors),
                             costs={"training_seconds": seconds}, artifact_ref=artifact)
    return TrainingBuild(status="ready", model_ref=result.model_id, job_ref=result.job, recipe_ref=recipe_ref,
                         dataset_ref=dataset.dataset_ref or "", metrics=metrics,
                         costs={"training_seconds": seconds,
                                **({"receipt": receipt.reservation_id} if receipt else {})},
                         artifact_ref=artifact, backend_spec=result.backend)


def _reason(e: BaseException, limit: int = 400) -> str:
    """Why a job failed, in at most about `limit` characters: its head (which job, where, the exit code) and its tail.
    A failed job's message ends with the end of its log, which is where the reason is; cutting the tail off would
    keep the job's id and lose why it failed."""
    text = f"{type(e).__name__}: {e}"
    return text if len(text) <= limit else f"{text[:limit // 3]} [...] {text[-(limit - limit // 3):]}"


class TrainingUnknown(RuntimeError):
    """A remote job whose state cannot be read. Never a reason to submit another one."""


def _probe_artifact(rt, result, dataset) -> list[str]:
    """Load the model through serving and check its identity and its output classes against the dataset.

    A model that answers as another model, or over classes the data does not use, is not the artifact the
    descriptor will claim it is; catching it here keeps that mismatch out of an evaluation.
    """
    from .types import Question, Request
    errors = []
    try:
        backend = rt.serving.backend(result.backend)
    except Exception as e:  # noqa: BLE001
        return [f"the model could not be loaded: {type(e).__name__}: {e}"[:300]]
    labels = sorted({name for part in (dataset.stats.get("labels") or {}).values() for name in part})
    question = Question("noul", "The task is complete", {"true": "done", "false": "not done"})
    try:
        answer = backend.decide([Request("probe", question)])[0]
    except Exception as e:  # noqa: BLE001
        return [f"the model did not answer the load probe: {type(e).__name__}: {e}"[:300]]
    if answer.model and result.model_id and answer.model != result.model_id:
        errors.append(f"the model answers as {answer.model!r}, the job produced {result.model_id!r}")
    if not answer.over(question.labels()):
        errors.append(f"the model answered over {sorted(answer.probs)}, not {question.labels()}")
    unknown = sorted(set(labels) - set(question.labels()))
    if unknown:
        errors.append(f"the dataset carries labels the point does not offer: {unknown}")
    return errors
