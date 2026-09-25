"""`ouroloop demo <scenario> --generations N`: the whole loop, several generations in a row (DEMO_PLAN §1, PLAN_v0.2
M13), on a scenario pack that the config names. The kernel knows the pack only through this interface:

    [demo] scenario = "module:Class", options from [demo.options]   (path: where the module imports from)
    Class(**options).play(rt, session, seed, generation) -> {metric: number | bool | str}
        one session: open it with rt.session (and what it is played on), ask its points, write the environment's
        outcomes, return its metrics

Each generation: play `games` sessions with the current release (the champion acts where the execution rule lets
it, the points' default answerers elsewhere) -> the post-act monitor on them -> one round: a gate record frozen now,
whose test sessions are the fresh ones (this generation's), build, check, train, calibrate, the gate, promote or
not. The next generation plays with what the round promoted, so its data comes from the model this one produced. One
generation's round is planted: its candidate is trained on shuffled labels and goes through the same gate.

The same games (session ids, seeds, generation) are played again by the defaults alone, no model asked, in
<workspace>/baseline/ ([demo] baseline = false: not), and their metrics printed beside: what a bigger board, other
seeds or luck do shows there too. Every number printed is on disk: each game's metrics as a "metrics" event in the
ledger it was played into, each generation's line as a "generation" event.
"""
from __future__ import annotations

import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

from . import config
from .evolve import monitor
from .evolve.gate import freeze_template, load_template, template_path
from .evolve.pipeline import evolve_model
from .evolve.registry import Registry
from .policy import cp_upper
from .runtime import Runtime, import_object

_ID = re.compile(r"g(\d{3})-\d{4}")


class Shuffled:
    """The planted bad candidate: `trainer` on training labels shuffled across the examples (among those with as many
    candidates), so it can learn nothing about the state."""

    def __init__(self, trainer, seed: int = 0):
        self.trainer, self.seed = trainer, seed
        self.supports_weights = getattr(trainer, "supports_weights", False)

    def train(self, job):
        rng = random.Random(self.seed)
        for path in sorted(Path(job.data_dir).glob("*.jsonl")):
            examples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            by_k = defaultdict(list)
            for e in examples:
                by_k[len(e["candidates"])].append(e)
            for group in by_k.values():
                targets = [e["target"] for e in group]
                rng.shuffle(targets)
                for e, t in zip(group, targets):
                    e["target"] = t
            path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in examples), encoding="utf-8")
        return self.trainer.train(job)

    def backend(self, result):
        return self.trainer.backend(result)


class Defaults(Runtime):
    """A runtime that never asks a model: every ask takes the point's default, as its answerer gives it
    (Runtime.record). The baseline games are played with it."""

    def ask(self, point, ctx, **kw):
        return self.record(point, ctx, **kw)


def _play(scenario, rt, ids: list[str], seeds: list[int], generation: int) -> list[dict]:
    """Play the games, each game's metrics also into the ledger."""
    out = []
    for s, seed in zip(ids, seeds):
        m = scenario.play(rt, s, seed, generation)
        rt.ledger.append({"type": "metrics", "session": s, "generation": generation, "metrics": m})
        out.append(m)
    return out


def _aggregate(metrics: list[dict]) -> dict:
    """Numbers as their mean, flags as [how many are true, of how many], anything else as its values."""
    out = {}
    for key in metrics[0] if metrics else ():
        values = [m[key] for m in metrics]
        if all(isinstance(v, bool) for v in values):
            out[key] = [sum(values), len(values)]
        elif all(isinstance(v, (int, float)) for v in values):
            out[key] = sum(values) / len(values)
        else:
            out[key] = ",".join(sorted({str(v) for v in values}))
    return out


def _metrics(mine: dict, base: dict | None) -> str:
    """key=value (the baseline's value), the bracket left out for a text the baseline shares, such as the board."""
    def show(v) -> str:
        return f"{v[0]}/{v[1]}" if isinstance(v, list) else f"{v:.1f}" if isinstance(v, float) else str(v)
    out = []
    for key, v in mine.items():
        b = show(base[key]) if base and key in base else None
        out.append(f"{key}={show(v)}" + (f" ({b})" if b is not None and not (isinstance(v, str) and b == v) else ""))
    return " ".join(out)


def _risk(errors: int, n: int, budget: float) -> str:
    return f"{errors}/{n}={errors / n:.1%} <={cp_upper(errors, n):.1%} vs {budget:.0%}" if n else "-"


def _line(e: dict) -> str:
    played = ",".join(e["played"])
    auto = f"{e['acted'] / e['decisions']:.0%}" if e["decisions"] else "-"
    verdict = f"{e['candidate']} {e['gate']}" + (" -> champion" if e["champion"] and e["champion"] == e["candidate"]
                                                 else "")
    notes = [*(["planted: trained on shuffled labels"] if e["planted"] else []),
             *(f"monitor revoked {r['version'] or r['model']} ({r['errors']}/{r['sessions']} wrong, lower bound "
               f"{r['lower']:.1%})" for r in e["revoked"])]
    return (f"{e['generation']:>3}  {played:<9}  {auto:>5}  {_risk(e['errors'], e['judged'], e['risk']):<31}  "
            f"{_metrics(e['metrics'], e['baseline'])}  |  {verdict}{''.join(f'; {x}' for x in notes)}")


def _print(line: str) -> None:
    print(line, flush=True)             # a generation takes minutes: show each line as it comes, also through a pipe


def run(cfg: dict, base: str | Path = ".", *, generations: int | None = None, root: str | Path | None = None,
        out=_print) -> Path:
    """Run `generations` more generations (default [demo] generations) in the config's workspace, or in `root`.
    Returns the workspace."""
    d = cfg.get("demo") or {}
    if d.get("path"):
        sys.path.insert(0, str((Path(base) / d["path"]).resolve()))
    if root is not None:
        cfg = {**cfg, "workspace": {**cfg.get("workspace", {}), "root": str(Path(root).resolve())}}
    rt = config.build_runtime(cfg, base)
    if not template_path(rt.root).exists():
        freeze_template(rt.root, d.get("gate"))       # the demo stands in for the person who freezes the gate
    budget = load_template(rt.root)[0]["risk"]
    scenario = import_object(d["scenario"])(**d.get("options", {}))
    trainer = config.build_trainer(cfg["trainer"])
    games, plant, seed = int(d.get("games", 60)), d.get("plant"), int(d.get("seed", 0))
    done = max((int(m.group(1)) for s in rt.ledger.sessions()[0] if (m := _ID.fullmatch(s))), default=0)
    baseline = Defaults(rt.root / "baseline", {}, "") if d.get("baseline", True) else None
    if baseline is not None:
        baseline.points = rt.points
    registry = Registry(rt.root)
    out(f"workspace {rt.root}; {games} games a generation; risk budget {budget:.0%}")
    out(f"{'gen':>3}  {'data from':<9}  {'auto':>5}  {'realized error <=CP95 vs budget':<31}  metrics"
        f"{' (the same games, defaults only)' if baseline is not None else ''}  |  round")
    for g in range(done + 1, done + 1 + (generations or int(d.get("generations", 3)))):
        ids = [f"g{g:03d}-{i:04d}" for i in range(games)]
        seeds = [seed * 1_000_000 + g * 10_000 + i for i in range(games)]
        metrics = _play(scenario, rt, ids, seeds, g)
        again = _play(scenario, baseline, ids, seeds, g) if baseline is not None else None
        pinned = {json.loads(rt.sessions.path(s, ".release.json").read_text())["champion"] for s in ids}
        played = sorted(f"{v} (revoked)" if rt.revoked.hit(v) else v or "base" for v in pinned)
        mine = set(ids)
        rows = [r for r in rt.ledger.rows() if r.decision.get("session") in mine]
        errors = n = 0
        for point in rt.points:
            e, k = monitor.realized(rt, point, mine)
            errors, n = errors + e, n + k
        revoked = monitor.watch(rt, budget, sessions=mine)
        planted = g == plant
        card = evolve_model(rt, Shuffled(trainer, seed) if planted else trainer, cfg.get("recipe"), seed=seed,
                            meta={"planted": "shuffled labels"} if planted else None)
        summary = rt.ledger.append({
            "type": "generation", "generation": g, "played": played, "decisions": len(rows),
            "acted": sum(r.decision.get("route") == "act" for r in rows), "errors": errors, "judged": n,
            "risk": budget, "metrics": _aggregate(metrics), "baseline": _aggregate(again) if again is not None else None,
            "candidate": card["version"], "gate": card["gate"],
            "champion": (registry.champion() or {}).get("version"), "planted": planted, "revoked": revoked})
        out(_line(summary))
    every = [e for e in rt.ledger.events() if e.get("type") == "generation"]     # a continued workspace's too
    out(f"all {len(every)} generations: realized error "
        f"{_risk(sum(e['errors'] for e in every), sum(e['judged'] for e in every), budget)}")
    out(f"on disk, in {rt.root}:\n"
        f"  ledger/   every session (its release), decision (its route and model) and outcome; each game's metrics "
        f"and each generation's line; gate and monitor events\n"
        f"  gates/    the frozen gate template, and each round's record: test sessions, verdict\n"
        f"  models/   each candidate's card (parent, gate record, test sessions, verdict), data and model; "
        f"champion.json\n"
        f"  harness/  the promoted models' calibrations, and revoked.json" +
        ("\n  baseline/ the same games played by the defaults alone: their ledger" if baseline is not None else ""))
    return rt.root
