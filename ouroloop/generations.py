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
from .runtime import import_object

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


def _summary(metrics: list[dict]) -> str:
    """Numbers as their mean, flags as a count, anything else as its values."""
    out = []
    for key in metrics[0] if metrics else ():
        values = [m[key] for m in metrics]
        if all(isinstance(v, bool) for v in values):
            out.append(f"{key}={sum(values)}/{len(values)}")
        elif all(isinstance(v, (int, float)) for v in values):
            out.append(f"{key}={sum(values) / len(values):.1f}")
        else:
            out.append(f"{key}={','.join(sorted({str(v) for v in values}))}")
    return " ".join(out)


def _risk(errors: int, n: int, budget: float) -> str:
    return f"{errors}/{n}={errors / n:.1%} <={cp_upper(errors, n):.1%} vs {budget:.0%}" if n else "-"


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
    registry, pooled = Registry(rt.root), [0, 0]
    out(f"workspace {rt.root}; {games} games a generation; risk budget {budget:.0%}")
    out(f"{'gen':>3}  {'data from':<9}  {'auto':>5}  {'realized error <=CP95 vs budget':<31}  metrics  |  round")
    for g in range(done + 1, done + 1 + (generations or int(d.get("generations", 3)))):
        ids = [f"g{g:03d}-{i:04d}" for i in range(games)]
        metrics = [scenario.play(rt, s, seed * 1_000_000 + g * 10_000 + i, g) for i, s in enumerate(ids)]
        played = {json.loads(rt.sessions.path(s, ".release.json").read_text())["champion"] or "base" for s in ids}
        mine = set(ids)
        rows = [r for r in rt.ledger.rows() if r.decision.get("session") in mine]
        auto = sum(r.decision.get("route") == "act" for r in rows) / max(1, len(rows))
        errors = n = 0
        for point in rt.points:
            e, k = monitor.realized(rt, point, mine)
            errors, n = errors + e, n + k
        pooled = [pooled[0] + errors, pooled[1] + n]
        revoked = monitor.watch(rt, budget, sessions=mine)
        planted = g == plant
        card = evolve_model(rt, Shuffled(trainer, seed) if planted else trainer, cfg.get("recipe"), seed=seed,
                            meta={"planted": "shuffled labels"} if planted else None)
        champion = (registry.champion() or {}).get("version")
        verdict = f"{card['version']} {card['gate']}" + (" -> champion" if champion and champion == card["version"]
                                                          else "")
        notes = [*(["planted: trained on shuffled labels"] if planted else []),
                 *(f"monitor revoked {r['version'] or r['model']} ({r['errors']}/{r['sessions']} wrong, lower bound "
                   f"{r['lower']:.1%})" for r in revoked)]
        out(f"{g:>3}  {','.join(sorted(played)):<9}  {auto:>5.0%}  {_risk(errors, n, budget):<31}  "
            f"{_summary(metrics)}  |  {verdict}{''.join(f'; {x}' for x in notes)}")
    out(f"all generations: realized error {_risk(*pooled, budget)}")
    out(f"on disk, in {rt.root}:\n"
        f"  ledger/   every session (its release), decision (its route and model) and outcome; gate and monitor "
        f"events\n"
        f"  gates/    the frozen gate template, and each round's record: test sessions, verdict\n"
        f"  models/   each candidate's card (parent, gate record, test sessions, verdict), data and model; "
        f"champion.json\n"
        f"  harness/  the promoted models' calibrations, and revoked.json")
    return rt.root
