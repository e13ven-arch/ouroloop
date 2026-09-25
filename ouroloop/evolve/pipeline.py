"""Model evolution (PLAN §4, PLAN_v0.2 M8): ledger -> build -> check -> train -> calibrate -> evaluate -> the frozen
gate -> promote.

`prepare_model` does everything up to the verdict on the sessions a gate record fixed; `recipe_evaluator` turns it
into an evaluation for gate.run, which the research loop and `evolve_model` (`ouroloop evolve`) both go through.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..types import Question, Request
from . import calibrate, experience
from .build import build
from .checks import CheckReport, check
from .gate import INFRA, GateRecord, open_record, run
from .registry import Registry
from .replay import EvalResult, labelled, scored, summarize
from .train import DEFAULT_RECIPE, PATH_KEYS, RECIPE_KEYS, TrainJob, TrainResult


def model_points(rt) -> list[str]:
    """Points served by the trained model: those on "champion", or every point if none is."""
    chosen = [name for name in rt.points if rt.backend_name(name) == "champion"]
    return chosen or list(rt.points)


@dataclass
class ModelCandidate:
    version: str
    card: dict
    checks: CheckReport | None
    per_point: dict = field(default_factory=dict)       # point -> (champion Brier, challenger Brier, sessions)
    held_out: dict = field(default_factory=dict)        # the same on the held-out tasks' sessions, for the card only
    calibrations: dict = field(default_factory=dict)    # point -> PointCalibration of the new model
    result: TrainResult | None = None
    backend: object = None
    inconclusive: list[str] = field(default_factory=list)   # an infrastructure failure, e.g. the trainer's


def _parts(rt, record: GateRecord, points: list[str], recipe: dict) -> dict:
    """Each point's labelled rows as `record` splits them. The recipe's min_weight thins the training rows only: the
    calibration and test rows are the record's, whatever the candidate."""
    sessions, _ = rt.ledger.sessions()
    parts = {name: record.split(labelled(rt, name), sessions, record.cal_frac(name)) for name in points}
    for part in parts.values():
        part["train"] = [r for r in part["train"] if r[2] >= recipe["min_weight"]]
    return parts


def prepare_model(rt, trainer, recipe: dict | None = None, *, record: GateRecord, points: list[str] | None = None,
                  registry: Registry | None = None, eval_states: list[str] = ()) -> ModelCandidate:
    """Build and check the data as `record` splits it (test sessions fixed at its freezing, held-out tasks never
    trained or calibrated on), train, file the candidate's card, calibrate on the calibration sessions, then use the
    record up and score champion and candidate on its test sessions. Infrastructure failures are caught: the
    candidate is inconclusive, and the record is used only if they came after the first test inference."""
    registry = registry or Registry(rt.root)
    unknown = set(recipe or {}) - RECIPE_KEYS
    if unknown:
        raise ValueError(f"unknown recipe keys: {sorted(unknown)}")
    recipe = {**DEFAULT_RECIPE, **(recipe or {})}
    points = points or model_points(rt)
    params = record.params
    version = registry.next_version()
    parent = registry.champion()
    parts = _parts(rt, record, points, recipe)
    ds = build(rt, registry.dir / version / "data", rows=parts)
    checks = check(ds, eval_states=eval_states, **params["checks"])
    card = {"version": version, "parent": parent["version"] if parent else None, "points": points, "recipe": recipe,
            "status": "candidate", "gate_record": record.hash, "round": record.round,
            "test_sessions": record.test_sessions, "data": ds.stats(), "checks": asdict(checks)}
    cand = ModelCandidate(version, card, checks)
    if not checks.ok:
        return cand
    try:
        cand.result = trainer.train(TrainJob(version, ds.dir, recipe,
                                             parent.get("run") if parent else recipe.get("init_from")))
        cand.backend = trainer.backend(cand.result)
        card.update({"run": cand.result.run, "backend": cand.result.backend, "model_id": cand.result.model_id})
        registry.record(card)                   # the candidate's card, before it is evaluated
        for name in points:
            cal = calibrate.calibrate_point(rt, name, backend=cand.backend, rows=parts[name]["calibration"],
                                            risk=params["risk"], min_sessions=params["min_cal_sessions"])
            if cal is not None:
                cand.calibrations[name] = cal
        record.consume(version)                 # the first inference request on the test sessions uses it up
        for name in points:
            p, cal = rt.points[name], cand.calibrations.get(name)
            t = cal.temperature if cal else 1.0
            cand.per_point[name] = scored(rt, p, p.spec, p.spec, parts[name]["test"], cand.backend, t)
            cand.held_out[name] = scored(rt, p, p.spec, p.spec, parts[name]["held_out"], cand.backend, t)
    except INFRA as e:
        cand.inconclusive = [f"{type(e).__name__}: {e}"[:500]]
    return cand


def dry_run_model(rt, trainer, recipe: dict | None = None, *, points: list[str] | None = None) -> dict:
    """Build and check the data as the round's gate record splits it, then let the trainer prepare and validate the
    job without training. Uses nothing up."""
    record = open_record(rt)
    recipe = {**DEFAULT_RECIPE, **(recipe or {})}
    version = f"dryrun_{Registry(rt.root).next_version()}"
    parts = _parts(rt, record, points or model_points(rt), recipe)
    ds = build(rt, rt.root / "models" / version / "data", rows=parts)
    checks = check(ds, **record.params["checks"])
    out = {"version": version, "round": record.round, "data": ds.stats(), "checks": asdict(checks)}
    if hasattr(trainer, "dry_run"):
        parent = Registry(rt.root).champion()
        out["job"] = trainer.dry_run(TrainJob(version, ds.dir, recipe,
                                              parent.get("run") if parent else recipe.get("init_from")))
    return out


def promote_model(rt, cand: ModelCandidate, registry: Registry, gate: str) -> None:
    """Calibrations are keyed by the model id the backend reports, so it must be the one on the card. The champion
    pointer moves by compare-and-swap on the candidate's parent; then its calibrations are stored with `gate`, the
    hash of the gate record that promoted it, so its thresholds may act. The runtime needs no change: sessions opened
    from now on run the new champion, sessions already running keep theirs."""
    reported = cand.backend.decide([Request("model id probe", Question("noul", "Is this a probe?"))])[0].model
    if reported != cand.result.model_id:
        raise ValueError(f"{cand.version}: the backend reports model {reported!r}, the card says "
                         f"{cand.result.model_id!r}")
    registry.promote(cand.version, cand.card.get("parent"))
    for cal in cand.calibrations.values():
        calibrate.apply(rt, cal, gate=gate, version=cand.version)


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
                      per_point=mc.per_point, record=file_card)


def recipe_evaluator(trainer, base_recipe: dict | None = None, **kw):
    """Evaluator for research candidates of kind "recipe": train with the changed recipe, gated by gate.run. A
    candidate changes hyperparameters, not the files and runs training reads (PATH_KEYS)."""
    def evaluate(rt, cand, record: GateRecord) -> EvalResult:
        if set(cand.change) & PATH_KEYS:
            raise ValueError(f"{sorted(set(cand.change) & PATH_KEYS)} name data or runs: a person sets them in [recipe]")
        registry = Registry(rt.root)
        mc = prepare_model(rt, trainer, {**(base_recipe or {}), **cand.change}, record=record, registry=registry,
                           **kw)
        return model_evaluation(rt, mc, registry, record, {"candidate": cand.id, "hypothesis": cand.hypothesis})

    return evaluate


def evolve_model(rt, trainer, recipe: dict | None = None, *, record: GateRecord | None = None, seed: int = 0,
                 registry: Registry | None = None, **kw) -> dict:
    """`ouroloop evolve`: one model candidate through the round's gate record (or `record`). -> its card."""
    registry = registry or Registry(rt.root)
    record = record or open_record(rt)
    out: dict = {}

    def evaluate() -> EvalResult:
        out["mc"] = prepare_model(rt, trainer, recipe, record=record, registry=registry, **kw)
        return model_evaluation(rt, out["mc"], registry, record)

    g, ev = run(record, evaluate, seed=seed)
    if ev.record:
        ev.record(g.outcome, g.reasons)
    card = out["mc"].card if "mc" in out else {"version": None, "gate": g.outcome, "reasons": g.reasons, "points": []}
    experience.append(rt.root, {"kind": "model", "version": card["version"], "point": ",".join(card["points"]),
                                "change": {k: v for k, v in card.get("recipe", {}).items()
                                           if v != DEFAULT_RECIPE.get(k)},
                                "gate": g.outcome, "reasons": g.reasons, "record": record.round, "used": g.used,
                                **({"verdict": "inconclusive"} if g.outcome == "inconclusive" else {})})
    return card
