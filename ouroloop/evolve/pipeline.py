"""Model evolution (PLAN §4, PLAN_v0.2 M8): ledger -> build -> check -> train -> calibrate -> evaluate -> the frozen
gate -> promote.

`prepare_model` does everything up to the verdict on the sessions a gate record fixed; `recipe_evaluator` turns it
into an evaluation for gate.run, which the research loop and `evolve_model` (`ouroloop evolve`) both go through.
`schedule` says whether a round should train at all: the least training that reaches the template's target.
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field

from ..types import Question, Request
from . import calibrate, experience
from .build import build
from .checks import CheckReport, check
from .gate import INFRA, TEMPLATE, GateRecord, load_template, open_record, run
from .registry import Registry
from .replay import EvalResult, labelled, not_held_out, right, routing_default, scored, summarize
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
    per_point: dict = field(default_factory=dict)       # point -> replay.scored on the test sessions
    held_out: dict = field(default_factory=dict)        # the same on the held-out tasks' sessions, for the card only
    calibrations: dict = field(default_factory=dict)    # point -> PointCalibration of the new model
    result: TrainResult | None = None
    backend: object = None
    inconclusive: list[str] = field(default_factory=list)   # an infrastructure failure, e.g. the trainer's


def default_errors(rt, points: list[str] | None = None) -> int:
    """The labelled rows outside held-out tasks, at `points` (default: model_points), whose routing default is not
    among their right answers: the rows a model could add anything on."""
    n = 0
    for name in points or model_points(rt):
        for row, target, _ in not_held_out(rt, labelled(rt, name)):
            try:
                if rt.points[name].default_for(row.decision.get("phase"))[0] is not None:   # no phase: never acts
                    n += routing_default(rt, name, row) not in right(target)
            except ValueError:          # a row whose phase cannot be told never acts
                continue
    return n


def schedule(rt, points: list[str] | None = None) -> tuple[bool, str]:
    """Whether a round trains a candidate, and why: the least training for the template's goal. It trains the first
    candidate (the data checks still decide whether there is enough), after the champion was revoked since the last
    trained candidate (drift), or once the default errors at `points` have at least doubled since it (the data a
    model could learn from). It does not once the latest accepted model fixes the template's target share of the
    default errors on its test rows, at every point that had any (unless revoked since), nor once the template's
    training budget is spent. A planted candidate uses budget but is never the last trained one: it learned nothing
    and says nothing about drift."""
    params = load_template(rt.root)[0]
    registry = Registry(rt.root)
    cards = [registry.card(v) for v in registry.versions() if (registry.dir / v / "card.json").exists()]
    trained = [c for c in cards if c.get("model_id")]
    budget = {**TEMPLATE["budget"], **params.get("budget", {})}["max_trainings"]
    if len(trained) >= budget:
        return False, f"training budget spent ({len(trained)} of {budget})"
    real = [c for c in trained if not c.get("planted")]
    if not real:
        return True, "no candidate trained yet"
    last = real[-1]
    path = rt.revoked.path
    revoked = json.loads(path.read_text(encoding="utf-8"))["revoked"] if path.exists() else []
    if any(e["ts"] >= last["updated"] for e in revoked):
        return True, f"the champion was revoked since {last['version']}"
    target = {**TEMPLATE["target"], **params.get("target", {})}["fixed_share"]
    accepted = next((c for c in reversed(cards) if c.get("gate") == "accepted"), {})
    shares = [s for m in accepted.get("metrics", {}).values()        # a card from before gain has no share
              if (s := (m.get("challenger") or {}).get("fixed_share")) is not None]
    standing = accepted and not rt.revoked.hit(accepted["version"], accepted.get("model_id"))
    if standing and shares and min(shares) >= target:
        return False, f"target reached: {accepted['version']} fixes {min(shares):.0%} of the default errors"
    if "default_errors" not in last:
        return True, f"{last['version']} has no count of default errors to compare with"
    now, before = default_errors(rt, points), last["default_errors"]
    if now and now >= 2 * before:
        return True, f"default errors doubled since {last['version']} ({before} -> {now})"
    return False, f"default errors {now}, not yet twice the {before} at {last['version']}"


def focused(rt, point: str, rows: list[tuple], focus: float, seed: int = 0) -> list[tuple]:
    """The training rows with every default error (a row whose routing default is not among its right answers: the
    only rows a model can add anything on) kept, and a seeded sample of the others, so that the errors make up `focus`
    of them (all the others when there are too few). Time order is kept."""
    p = rt.points[point]
    errors = []
    for row, target, _ in rows:
        try:
            errors.append(p.default_for(row.decision.get("phase"))[0] is not None     # no phase: never acts
                          and routing_default(rt, p, row) not in right(target))
        except ValueError:          # a row whose phase cannot be told never acts either
            errors.append(False)
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
    if recipe["focus"] is not None and not 0 < recipe["focus"] <= 1:
        raise ValueError(f"recipe focus {recipe['focus']!r}: a share in (0, 1]")
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
        cand.result = trainer.train(TrainJob(version, ds.dir, recipe,
                                             parent.get("model_id") if parent else recipe.get("init_from")))
        cand.backend = trainer.backend(cand.result)
        card.update({"model_id": cand.result.model_id, "backend": cand.result.backend, "job": cand.result.job})
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
    recipe = {**DEFAULT_RECIPE, **(recipe or {})}
    version = f"dryrun_{Registry(rt.root).next_version()}"
    parts = _parts(rt, record, points or model_points(rt), recipe, trainer)
    ds = build(rt, rt.root / "models" / version / "data", rows=parts)
    checks = check(ds, **record.params["checks"])
    out = {"version": version, "round": record.round, "data": ds.stats(), "checks": asdict(checks)}
    if hasattr(trainer, "dry_run"):
        parent = Registry(rt.root).champion()
        out["job"] = trainer.dry_run(TrainJob(version, ds.dir, recipe,
                                              parent.get("model_id") if parent else recipe.get("init_from")))
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
                      per_point=mc.per_point, record=file_card,
                      calibrations=[c.gated(rt.points[name].spec_hash) for name, c in mc.calibrations.items()])


def check_change(change: dict) -> None:
    """A research candidate's recipe change: hyperparameters only, never the files and runs training reads
    (PATH_KEYS), each of the type of its default (an int for a float; a number for one that is None by default).
    Raises ValueError before any job starts."""
    paths = sorted(set(change) & PATH_KEYS)
    if paths:
        raise ValueError(f"{paths} name data or runs: a person sets them in [recipe]")
    unknown = sorted(set(change) - RECIPE_KEYS)
    if unknown:
        raise ValueError(f"unknown recipe keys: {unknown}")
    for key, value in change.items():
        default = DEFAULT_RECIPE[key]
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
        check_change(cand.change)
        registry = Registry(rt.root)
        mc = prepare_model(rt, trainer, {**(base_recipe or {}), **cand.change}, record=record, registry=registry,
                           **kw)
        return model_evaluation(rt, mc, registry, record, {"candidate": cand.id, "hypothesis": cand.hypothesis})

    return evaluate


def evolve_model(rt, trainer, recipe: dict | None = None, *, record: GateRecord | None = None, seed: int = 0,
                 registry: Registry | None = None, meta: dict | None = None, **kw) -> dict:
    """`ouroloop evolve`: one model candidate through the round's gate record (or `record`). -> its card, which
    also carries `meta`."""
    registry = registry or Registry(rt.root)
    record = record or open_record(rt)
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
                                           if v != DEFAULT_RECIPE.get(k)},
                                "gate": g.outcome, "reasons": g.reasons, "record": record.round, "used": g.used,
                                **({"verdict": "inconclusive"} if g.outcome == "inconclusive" else {}),
                                **(meta or {})})
    return card
