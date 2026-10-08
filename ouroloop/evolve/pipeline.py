"""Model evolution: ledger -> build -> check -> train -> calibrate -> evaluate -> the frozen gate -> deploy -> promote.

`prepare_model` does everything up to the verdict on the sessions a gate record fixed; `recipe_evaluator` turns it
into an evaluation for gate.run, which the research loop and `evolve_model` (`ouroloop evolve`) both go through.
Every model is asked through the runtime's serving (serving.py), never through its trainer. `deploy_version`
(`ouroloop deploy`) retries the deploy of an accepted version. Whether a round trains at all is not decided here:
a round trains only what a plan admission passed (evolve/plan.py).
"""
from __future__ import annotations

import random
import time
from dataclasses import asdict, dataclass, field

from ..ledger import PromotionConflict, now
from . import calibrate, experience
from .build import build
from .checks import CheckReport, check
from .gate import INFRA, GateRecord, open_record, run
from .registry import Registry
from .replay import EvalResult, default_error, labelled, not_held_out, scored, summarize
from .train import TrainJob, TrainResult, path_keys, recipe_defaults


def model_points(rt) -> list[str]:
    """Points served by the trained model: those on "champion", or every point if none is."""
    chosen = [name for name in rt.points if rt.backend_name(name) == "champion"]
    return chosen or list(rt.points)


@dataclass
class ModelCandidate:
    version: str
    card: dict
    checks: CheckReport | None
    per_point: dict = field(default_factory=dict)       # point -> replay.scored on the test sessions
    held_out: dict = field(default_factory=dict)        # the same on the held-out tasks' sessions, for the card only
    calibrations: dict = field(default_factory=dict)    # point -> PointCalibration of the new model
    result: TrainResult | None = None
    backend: object = None                              # its backend, from serving
    inconclusive: list[str] = field(default_factory=list)   # an infrastructure failure, e.g. the trainer's


def default_errors(rt, points: list[str] | None = None) -> int:
    """The labelled rows outside held-out tasks, at `points` (default: model_points), that are default errors
    (replay.default_error): the rows a model could add anything on."""
    return sum(default_error(rt, name, row, target)
               for name in points or model_points(rt)
               for row, target, _ in not_held_out(rt, labelled(rt, name)))


def focused(rt, point: str, rows: list[tuple], focus: float, seed: int = 0) -> list[tuple]:
    """The training rows with every default error (a row whose routing default is not among its right answers: the
    only rows a model can add anything on) kept, and a seeded sample of the others, so that the errors make up `focus`
    of them (all the others when there are too few). Time order is kept."""
    p = rt.points[point]
    errors = [default_error(rt, p, row, target) for row, target, _ in rows]
    others = [i for i, e in enumerate(errors) if not e]
    k = round(sum(errors) * (1 - focus) / focus)
    keep = set(random.Random(seed).sample(others, k)) if k < len(others) else set(others)
    return [r for i, r in enumerate(rows) if errors[i] or i in keep]


def _parts(rt, record: GateRecord, points: list[str], recipe: dict, trainer) -> dict:
    """Each point's labelled rows as `record` splits them. The recipe's min_weight thins the training rows only: the
    calibration and test rows are the record's, whatever the candidate. A trainer that does not weigh examples
    (supports_weights) is given full-weight rows only, so it never takes a weak label for a strong one. The recipe's
    focus, if set, keeps the training rows on the default's errors (see focused)."""
    sessions, _ = rt.ledger.sessions()
    parts = {name: record.split(labelled(rt, name), sessions, record.cal_frac(name)) for name in points}
    least = recipe["min_weight"] if getattr(trainer, "supports_weights", False) else max(recipe["min_weight"], 1.0)
    for name, part in parts.items():
        part["train"] = [r for r in part["train"] if r[2] >= least]
        if recipe.get("focus") is not None:
            part["train"] = focused(rt, name, part["train"], recipe["focus"], recipe["seed"])
    return parts


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


def parent_ref(champion: dict | None, trainer, recipe: dict) -> str | None:
    """The job's parent: the champion's model when the same plugin trained it into the trainer's store, else [recipe]
    init_from (and then the plugin's own init_from). A model of another kind, or in another store, is no parent:
    after [trainer] type or [store] changes, the first model starts where the config says. A store on the training
    machine ("dir") is another store on another machine: after [trainer] host changes, too. A card from before
    trainer plugins names none: TDE's."""
    if champion:
        backend = champion.get("backend") or {}
        made_by = backend.get("trainer") or ("tde" if backend.get("type") == "model" else None)
        store = getattr(trainer, "store", None)
        here = _machine((champion.get("job") or {}).get("host")) == _machine(getattr(trainer, "host", None))
        if made_by == getattr(trainer, "name", None) and (store is None or (
                store.job(champion.get("model_id")) and (here or not store.ON_TRAINING_HOST))):
            return champion.get("model_id")
    return recipe.get("init_from")


def _machine(host: str | None) -> str | None:
    """A training host as a machine: None and "local" are both this one."""
    return None if host in (None, "local") else host


def research_context(trainer, recipe: dict | None) -> dict:
    """What a research candidate of kind "recipe" is shown: the recipe it changes, and the keys it may set."""
    return {"recipe": {**recipe_defaults(trainer), **(recipe or {})},
            "recipe_keys": sorted(set(recipe_defaults(trainer)) - path_keys(trainer))}


def prepare_model(rt, trainer, recipe: dict | None = None, *, record: GateRecord, points: list[str] | None = None,
                  registry: Registry | None = None, eval_states: list[str] = ()) -> ModelCandidate:
    """Build and check the data as `record` splits it (test sessions fixed at its freezing, held-out tasks never
    trained or calibrated on), train, file the candidate's card, calibrate on the calibration sessions, then use the
    record up and score champion and candidate on its test sessions; the candidate is asked through the runtime's
    serving. Infrastructure failures are caught: the candidate is inconclusive, and the record is used only if they
    came after the first test inference."""
    registry = registry or Registry(rt.root)
    recipe = full_recipe(trainer, recipe)
    points = points or model_points(rt)
    params = record.params
    version = registry.next_version()
    parent = registry.champion()
    parts = _parts(rt, record, points, recipe, trainer)
    ds = build(rt, registry.dir / version / "data", rows=parts)
    checks = check(ds, eval_states=eval_states, **params["checks"])
    card = {"version": version, "parent": parent["version"] if parent else None, "points": points, "recipe": recipe,
            "status": "candidate", "gate_record": record.hash, "round": record.round,
            "test_sessions": record.test_sessions, "data": ds.stats(), "checks": asdict(checks),
            "default_errors": default_errors(rt, points)}
    cand = ModelCandidate(version, card, checks)
    if not checks.ok:
        return cand
    try:
        start = time.monotonic()
        cand.result = trainer.train(TrainJob(version, ds.dir, recipe, parent_ref(parent, trainer, recipe)))
        cand.backend = rt.serving.backend(cand.result.backend)
        card.update({"model_id": cand.result.model_id, "backend": cand.result.backend, "job": cand.result.job,
                     "train_seconds": round(time.monotonic() - start, 1)})    # what the next round's plan costs
        registry.record(card)                   # the candidate's card, before it is evaluated
        for name in points:
            cal = calibrate.calibrate_point(rt, name, backend=cand.backend, rows=parts[name]["calibration"],
                                            risk=params["risk"], min_sessions=params["min_cal_sessions"])
            if cal is not None:
                cand.calibrations[name] = cal
        record.consume(version)                 # the first inference request on the test sessions uses it up
        for name in points:
            p, cal = rt.points[name], cand.calibrations.get(name)
            cand.per_point[name] = scored(rt, p, parts[name]["test"], p.spec, cal, cand.backend)
            cand.held_out[name] = scored(rt, p, parts[name]["held_out"], p.spec, cal, cand.backend)
    except INFRA as e:
        cand.inconclusive = [f"{type(e).__name__}: {e}"[:500]]
    return cand


def dry_run_model(rt, trainer, recipe: dict | None = None, *, points: list[str] | None = None) -> dict:
    """Build and check the data as the round's gate record splits it, then let the trainer prepare and validate the
    job without training. Uses nothing up."""
    record = open_record(rt)
    recipe = full_recipe(trainer, recipe)
    version = f"dryrun_{Registry(rt.root).next_version()}"
    parts = _parts(rt, record, points or model_points(rt), recipe, trainer)
    ds = build(rt, rt.root / "models" / version / "data", rows=parts)
    checks = check(ds, **record.params["checks"])
    out = {"version": version, "round": record.round, "data": ds.stats(), "checks": asdict(checks)}
    if hasattr(trainer, "dry_run"):
        parent = Registry(rt.root).champion()
        out["job"] = trainer.dry_run(TrainJob(version, ds.dir, recipe, parent_ref(parent, trainer, recipe)))
    return out


def deploy(rt, registry: Registry, card: dict) -> bool:
    """Serving loads the card's model and asks it the probe; the model id it answers as must be the card's, since
    calibrations, thresholds, pins and revocations are keyed by it. A failure is infrastructure, not a verdict: the
    card says so (deploy_error; deployed false, unless it was deployed before) and nothing changes. -> whether it
    deployed."""
    try:
        reported = rt.serving.deploy(card["backend"])
        if reported != card["model_id"]:
            raise ValueError(f"serving reports model {reported!r}, the card says {card['model_id']!r}")
    except Exception as e:  # noqa: BLE001 - whatever keeps it from serving, nothing is promoted and nothing acts
        card["deploy_error"] = f"{type(e).__name__}: {e}"[:500]
        if not registry.deployed(card):         # a deploy that once succeeded still did: a rollback may return to it
            card["deployed"] = False
        registry.record(card)
        return False
    return True


def promote(rt, registry: Registry, card: dict, calibrations, gate: str) -> bool:
    """Deploy the accepted version on `card`, then promote it: the champion pointer changes by compare-and-swap on its
    parent, and its calibrations are stored with `gate`, the hash of the gate record that accepted it, so its
    thresholds may act. A deploy that fails leaves the champion and the calibrations unchanged (-> False). The
    runtime needs no change: sessions opened from now on run the new champion, sessions already running keep theirs."""
    if not deploy(rt, registry, card):
        return False
    try:
        registry.promote(card["version"], card.get("parent"))
    except PromotionConflict:
        rt.drop(card["version"], card["backend"])
        raise
    card["deployed"] = now()
    card.pop("deploy_error", None)
    registry.record(card)
    for cal in calibrations:
        calibrate.apply(rt, cal, gate=gate, version=card["version"])
    return True


def promote_model(rt, cand: ModelCandidate, registry: Registry, gate: str) -> None:
    """What an accepting gate verdict applies: deploy and promote the candidate (see promote)."""
    promote(rt, registry, cand.card, cand.calibrations.values(), gate)


def deploy_version(rt, version: str, registry: Registry | None = None) -> bool:
    """`ouroloop deploy`: deploy and promote a version later, one a gate record accepted (and that was not rolled
    back) and that is not revoked, over the parent it was judged against (while that is still the champion, else a
    PromotionConflict before anything loads), with the calibrations that record accepted. The champion is deployed
    already. -> whether it is deployed."""
    registry = registry or Registry(rt.root)
    card = registry.card(version)
    if not rt.accepted.promoted(card, standing=True):
        raise ValueError(f"{version}: no gate record accepted it, or it was rolled back; only an accepted version "
                         f"is deployed")
    if rt.revoked.hit(version, card.get("model_id")):
        raise ValueError(f"{version} is revoked")
    champion = (registry.champion() or {}).get("version")
    if champion == version:
        return True
    if card.get("parent") != champion:          # the swap would fail: no deploy for nothing
        raise PromotionConflict(f"{version} was built on {card.get('parent')}, but the champion is now {champion}")
    cals = [calibrate.PointCalibration(point, card["model_id"], c["temperature"], c["thresholds"], c["n"],
                                       c["accuracy"], c.get("sessions", 0))
            for point, c in (card.get("calibration") or {}).items()]
    return promote(rt, registry, card, cals, card["gate_record"])


def model_evaluation(rt, mc: ModelCandidate, registry: Registry, record: GateRecord, meta: dict | None = None
                     ) -> EvalResult:
    """A prepared model candidate as gate.run sees it. Its `record` callback files the card with the verdict."""
    def file_card(outcome: str, reasons: list) -> None:
        mc.card.update({"gate": outcome, "status": outcome, "reasons": reasons, **(meta or {}),
                        "metrics": {name: {**m, "held_out": summarize(mc.held_out).get(name)}
                                    for name, m in summarize(mc.per_point).items()},
                        "calibration": {k: {"temperature": c.temperature, "thresholds": c.thresholds, **c.entry()}
                                        for k, c in mc.calibrations.items()}})
        registry.record(mc.card)

    if mc.inconclusive:
        return EvalResult("model", [], [], [], lambda: None, inconclusive=mc.inconclusive, record=file_card)
    if not mc.checks.ok:
        return EvalResult("model", [], [], [], lambda: None, blocked=mc.checks.blocking,
                          insufficient=mc.checks.insufficient, record=file_card)
    flat = [[x for v in mc.per_point.values() for x in v[i]] for i in range(3)]
    return EvalResult("model", *flat, apply=lambda: promote_model(rt, mc, registry, record.hash),
                      per_point=mc.per_point, record=file_card,
                      calibrations=[c.gated(rt.points[name].spec_hash) for name, c in mc.calibrations.items()])


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


def recipe_evaluator(trainer, base_recipe: dict | None = None, **kw):
    """Evaluator for research candidates of kind "recipe": train with the changed recipe, gated by gate.run. A
    candidate changes hyperparameters of the right types, not the files and runs training reads (check_change)."""
    def evaluate(rt, cand, record: GateRecord) -> EvalResult:
        check_change(cand.change, trainer)
        registry = Registry(rt.root)
        mc = prepare_model(rt, trainer, {**(base_recipe or {}), **cand.change}, record=record, registry=registry,
                           **kw)
        return model_evaluation(rt, mc, registry, record, {"candidate": cand.id, "hypothesis": cand.hypothesis})

    return evaluate


def evolve_model(rt, trainer, recipe: dict | None = None, *, record: GateRecord | None = None, seed: int = 0,
                 registry: Registry | None = None, meta: dict | None = None, plan=None, **kw) -> dict:
    """`ouroloop evolve`: one model candidate through the round's gate record (or `record`). -> its card, which
    also carries `meta`. `plan` is the round's plan (evolve/plan.py): it goes on the card and its prediction is
    recorded against what the gate measured, which is what makes plan quality visible."""
    registry = registry or Registry(rt.root)
    record = record or open_record(rt)
    meta = {**(meta or {}), **({"plan": plan.to_json()} if plan is not None else {})}
    if plan is not None and plan.kind == "recipe" and plan.change:
        check_change(plan.change, trainer)          # a plan changes hyperparameters, never data or runs
        recipe = {**(recipe or {}), **plan.change}
    out: dict = {}

    def evaluate() -> EvalResult:
        out["mc"] = prepare_model(rt, trainer, recipe, record=record, registry=registry, **kw)
        return model_evaluation(rt, out["mc"], registry, record, meta)

    g, ev = run(record, evaluate, seed=seed)
    if ev.record:
        ev.record(g.outcome, g.reasons)
    card = out["mc"].card if "mc" in out else {"version": None, "gate": g.outcome, "reasons": g.reasons, "points": []}
    experience.append(rt.root, {"kind": "model", "version": card["version"], "point": ",".join(card["points"]),
                                "change": {k: v for k, v in card.get("recipe", {}).items()
                                           if v != recipe_defaults(trainer).get(k)},
                                "gate": g.outcome, "reasons": g.reasons, "record": record.round, "used": g.used,
                                **({"verdict": "inconclusive"} if g.outcome == "inconclusive" else {}),
                                **(meta or {}),
                                **({"prediction": plan.measured(card)} if plan is not None else {})})
    return card


# ---------------------------------------------------------------------------- training without promotion
# docs/modules/02_EVIDENCE_TRAINING.md §5. `prepare_model` above still does the whole legacy round: data, checks,
# training, calibration, the gate record and the champion pointer. The task profile needs only the middle of that
# — an artifact and a load probe — and must not touch a pointer, consume a gate record or run an outer test,
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
    from . import contracts as ct
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
    from ..types import Question, Request
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
