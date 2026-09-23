"""Model evolution (PLAN §4): ledger -> build -> check -> train -> calibrate -> evaluate -> gate -> promote.

`prepare_model` does everything up to the gate, so the research loop can evaluate a training-recipe candidate with
the same code and apply its own gate; `evolve_model` is the whole pipeline for `ouroloop evolve`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..types import brier
from . import calibrate, experience
from .build import build
from .checks import CheckReport, check
from .gate import decide_points
from .registry import Registry
from .replay import labelled, predict, splits
from .train import DEFAULT_RECIPE, RECIPE_KEYS, TrainJob, TrainResult


def model_points(rt) -> list[str]:
    """Points served by the trained model: those whose backend is "champion", or every point if none is."""
    chosen = [name for name in rt.points if rt.point_backends.get(name) == "champion"]
    return chosen or list(rt.points)


@dataclass
class ModelCandidate:
    version: str
    card: dict
    checks: CheckReport
    per_point: dict = field(default_factory=dict)       # point -> (champion Brier, challenger Brier, sessions)
    calibrations: dict = field(default_factory=dict)    # point -> PointCalibration of the new model
    result: TrainResult | None = None
    backend: object = None


def prepare_model(rt, trainer, recipe: dict | None = None, *, points: list[str] | None = None,
                  registry: Registry | None = None, eval_states: list[str] = (), check_kwargs: dict | None = None,
                  test_frac: float = 0.15, cal_frac: float = 0.15) -> ModelCandidate:
    registry = registry or Registry(rt.root)
    unknown = set(recipe or {}) - RECIPE_KEYS
    if unknown:
        raise ValueError(f"unknown recipe keys: {sorted(unknown)}")
    recipe = {**DEFAULT_RECIPE, **(recipe or {})}
    points = points or model_points(rt)
    version = registry.next_version()
    parent = registry.champion()
    ds = build(rt, registry.dir / version / "data", points, test_frac, cal_frac, recipe["min_weight"])
    checks = check(ds, eval_states=eval_states, **(check_kwargs or {}))
    card = {"version": version, "parent": parent["version"] if parent else None, "points": points, "recipe": recipe,
            "data": ds.stats(), "checks": asdict(checks)}
    cand = ModelCandidate(version, card, checks)
    if not checks.ok:
        return cand
    init_from = parent.get("run") if parent else recipe.get("init_from")
    cand.result = trainer.train(TrainJob(version, ds.dir, recipe, init_from))
    cand.backend = trainer.backend(cand.result)
    card.update({"run": cand.result.run, "backend": cand.result.backend})
    for name in points:
        p = rt.points[name]
        cal = calibrate.calibrate_point(rt, name, backend=cand.backend, test_frac=test_frac, cal_frac=cal_frac)
        if cal is not None:
            cand.calibrations[name] = cal
        test = splits(labelled(rt, name), test_frac, cal_frac)["test"]
        _, champion = predict(rt, p, p.spec, test)
        _, challenger = predict(rt, p, p.spec, test, backend=cand.backend, temperature=cal.temperature if cal else 1.0)
        cand.per_point[name] = ([brier(pr, t) for pr, (_, t, _) in zip(champion, test)],
                                [brier(pr, t) for pr, (_, t, _) in zip(challenger, test)],
                                [r.decision.get("session", "") for r, _, _ in test])
    return cand


def dry_run_model(rt, trainer, recipe: dict | None = None, *, points: list[str] | None = None,
                  check_kwargs: dict | None = None, test_frac: float = 0.15, cal_frac: float = 0.15) -> dict:
    """Build and check the data, then let the trainer prepare and validate the job without training."""
    recipe = {**DEFAULT_RECIPE, **(recipe or {})}
    version = f"dryrun_{Registry(rt.root).next_version()}"
    ds = build(rt, rt.root / "models" / version / "data", points or model_points(rt), test_frac, cal_frac,
               recipe["min_weight"])
    checks = check(ds, **(check_kwargs or {}))
    out = {"version": version, "data": ds.stats(), "checks": asdict(checks)}
    if hasattr(trainer, "dry_run"):
        parent = Registry(rt.root).champion()
        out["job"] = trainer.dry_run(TrainJob(version, ds.dir, recipe,
                                              parent.get("run") if parent else recipe.get("init_from")))
    return out


def promote_model(rt, cand: ModelCandidate, registry: Registry) -> None:
    registry.promote(cand.version)
    rt.backends["champion"] = cand.backend
    for name in cand.per_point:
        rt.point_backends[name] = "champion"
    for cal in cand.calibrations.values():
        calibrate.apply(rt, cal, model=cand.version)


def recipe_evaluator(trainer, base_recipe: dict | None = None, **kw):
    """Evaluator for research candidates of kind "recipe": train with the changed recipe, gate in the research loop."""
    from .replay import EvalResult

    def evaluate(rt, cand, test_frac: float = 0.15) -> EvalResult:
        registry = Registry(rt.root)
        mc = prepare_model(rt, trainer, {**(base_recipe or {}), **cand.change}, registry=registry,
                           test_frac=test_frac, **kw)

        def record(outcome: str, reasons: list) -> None:
            mc.card.update({"gate": outcome, "reasons": reasons, "metrics": summarize(mc.per_point),
                            "candidate": cand.id, "hypothesis": cand.hypothesis})
            registry.record(mc.card)

        if not mc.checks.ok:
            return EvalResult(cand.point, [], [], [], lambda: None, blocked=mc.checks.blocking,
                              insufficient=mc.checks.insufficient, record=record)
        flat = [[x for v in mc.per_point.values() for x in v[i]] for i in range(3)]
        return EvalResult(cand.point, *flat, apply=lambda: promote_model(rt, mc, registry), per_point=mc.per_point,
                          record=record)

    return evaluate


def summarize(per_point: dict) -> dict:
    return {name: {"n": len(a), "champion_brier": sum(a) / len(a) if a else None,
                   "challenger_brier": sum(b) / len(b) if b else None} for name, (a, b, _) in per_point.items()}


def evolve_model(rt, trainer, recipe: dict | None = None, *, min_items: int = 30, seed: int = 0,
                 registry: Registry | None = None, **kw) -> dict:
    registry = registry or Registry(rt.root)
    cand = prepare_model(rt, trainer, recipe, registry=registry, **kw)
    if not cand.checks.ok:
        outcome = "blocked" if cand.checks.blocking else "insufficient_data"
        cand.card.update({"gate": outcome, "reasons": cand.checks.blocking or cand.checks.insufficient})
    else:
        g = decide_points(cand.per_point, min_items=min_items, seed=seed)
        cand.card.update({"gate": g.outcome, "reasons": g.reasons, "metrics": summarize(cand.per_point),
                          "calibration": {k: {"temperature": c.temperature, "threshold": c.threshold, **c.entry()}
                                          for k, c in cand.calibrations.items()}})
        if g.outcome == "accepted":
            promote_model(rt, cand, registry)
    registry.record(cand.card)
    experience.append(rt.root, {"kind": "model", "version": cand.version, "point": ",".join(cand.card["points"]),
                                "change": {k: v for k, v in cand.card["recipe"].items() if v != DEFAULT_RECIPE.get(k)},
                                "gate": cand.card["gate"], "reasons": cand.card["reasons"]})
    return cand.card
