"""`ouroloop demo <scenario> --generations N`: the whole loop, several generations in a row (DEMO_PLAN §1, PLAN_v0.2
M13), on a scenario pack that the config names. The kernel knows the pack only through this interface:

    [demo] scenario = "module:Class", options from [demo.options]   (path: where the module imports from)
    Class(**options).play(rt, session, seed, generation) -> {metric: number | bool | str}
        one session: open it with rt.session (and what it is played on), ask its points, write the environment's
        outcomes, return its metrics

Each generation: play `games` sessions with the current release (the champion acts where the execution rule lets
it, the points' default answerers elsewhere; the runtime's post-act monitor checks the champion as each session opens,
and one it revokes leaves the rest of the generation its defaults) -> one round, which trains only what a plan
admission passed (evolve/plan.py: data -> diagnosis -> hypothesis -> plan -> admission -> training): a gate record
frozen now, whose test sessions are the latest fresh ones (this generation's, at most
budget.sessions_per_generation), build, check, train, calibrate, the gate, promote or not. The next generation plays
with what the round promoted, so its data comes from the model this one produced. One generation's round is planted,
whatever the plan says: its candidate is trained on shuffled labels and goes through the same gate.

Each line reports what the champion added over the defaults: the share of decisions it acted on, the sessions it
harmed (any acted decision wrong) of those it acted in against the risk budget, and its gain, the fixes (acted and
right where the default was not) less the harms (acted and wrong) per session; and the revocations the monitor made
while the generation was played (its "monitor" events in the ledger).

The same games (session ids, seeds, generation) are played again by the defaults alone, no model asked, in
<workspace>/baseline/ ([demo] baseline = false: not), and their metrics printed beside: what a changed environment,
other seeds or luck do shows there too. Every number printed is on disk: each game's metrics as a "metrics" event in
the ledger it was played into, each generation's line as a "generation" event.
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
from .evolve.plan import decide
from .evolve.registry import Registry
from .evolve.replay import right
from .ledger import now
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
    """key=value (the baseline's value), the bracket left out for a text the baseline shares."""
    def show(v) -> str:
        return f"{v[0]}/{v[1]}" if isinstance(v, list) else f"{v:.1f}" if isinstance(v, float) else str(v)
    out = []
    for key, v in mine.items():
        b = show(base[key]) if base and key in base else None
        out.append(f"{key}={show(v)}" + (f" ({b})" if b is not None and not (isinstance(v, str) and b == v) else ""))
    return " ".join(out)


def _risk(bad: int, n: int, budget: float) -> str:
    return f"{bad}/{n}={bad / n:.1%} <={cp_upper(bad, n):.1%} vs {budget:.0%}" if n else "-"


def _judged(rows) -> tuple[int, int]:
    """(fixes, harms) of the labelled acts among `rows`: acted and right where the default was not, acted and wrong.
    An act that swaps a right default for another right answer is neither."""
    fixes = harms = 0
    for r in rows:
        target = r.target() if r.decision.get("route") == "act" else None
        if target:
            good = right(target[0])
            fixes += r.decision.get("action") in good and r.decision.get("default") not in good
            harms += r.decision.get("action") not in good
    return fixes, harms


def _line(e: dict) -> str:
    played = ",".join(e["played"])
    share = f"{e['acted'] / e['decisions']:.0%}" if e["decisions"] else "-"
    gain = f"{(e['fixes'] - e['harms']) / e['sessions']:+.2f}" if e["sessions"] else "-"
    if e.get("skipped"):
        verdict = f"skipped: {e['skipped']}"
    else:
        verdict = (f"{e['candidate'] or '-'} {e['gate']}" +
                   (" -> champion" if e["candidate"] and e["champion"] == e["candidate"] else "") +
                   (" (not deployed)" if e.get("deployed") is False else ""))
    notes = [*(["planted: trained on shuffled labels"] if e["planted"] else []),
             *(f"monitor revoked {r['version'] or r['model']} ({r['harmed']}/{r['sessions']} sessions harmed, lower "
               f"bound {r['lower']:.1%})" for r in e["revoked"])]
    return (f"{e['generation']:>3}  {played:<9}  {share:>5}  {_risk(e['harmed'], e['acted_sessions'], e['risk']):<32}  "
            f"{gain:>5}  {_metrics(e['metrics'], e['baseline'])}  |  {verdict}{''.join(f'; {x}' for x in notes)}")


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
    trainer = config.build_trainer(cfg, rt.serving)
    games, plant, seed = int(d.get("games", 60)), d.get("plant"), int(d.get("seed", 0))
    done = max((int(m.group(1)) for s in rt.ledger.sessions()[0] if (m := _ID.fullmatch(s))), default=0)
    baseline = Defaults(rt.root / "baseline", {}, "") if d.get("baseline", True) else None
    if baseline is not None:
        baseline.points = rt.points
    registry = Registry(rt.root)
    out(f"workspace {rt.root}; {games} games a generation; risk budget {budget:.0%}")
    out(f"{'gen':>3}  {'data from':<9}  {'acted':>5}  {'harmed sessions <=CP95 vs budget':<32}  {'gain':>5}  metrics"
        f"{' (the same games, defaults only)' if baseline is not None else ''}  |  round")
    for g in range(done + 1, done + 1 + (generations or int(d.get("generations", 3)))):
        ids = [f"g{g:03d}-{i:04d}" for i in range(games)]
        seeds = [seed * 1_000_000 + g * 10_000 + i for i in range(games)]
        start = now()
        metrics = _play(scenario, rt, ids, seeds, g)
        revoked = [e for e in rt.ledger.events() if e.get("type") == "monitor" and e["ts"] >= start]
        again = _play(scenario, baseline, ids, seeds, g) if baseline is not None else None
        pinned = {json.loads(rt.sessions.path(s, ".release.json").read_text())["champion"] for s in ids}
        played = sorted(f"{v} (revoked)" if rt.revoked.hit(v) else v or "base" for v in pinned)
        mine = set(ids)
        rows = [r for r in rt.ledger.rows() if r.decision.get("session") in mine]
        fixes, harms = _judged(rows)
        harm = [monitor.realized(rt, point, mine) for point in rt.points]
        planted = g == plant
        proposed, train, reasons, _ = (None, True, ["planted"], None) if planted else decide(rt)
        card = (evolve_model(rt, Shuffled(trainer, seed) if planted else trainer, cfg.get("recipe"), seed=seed,
                             plan=proposed, meta={"planted": "shuffled labels"} if planted else None)
                if train else {"version": None, "gate": None})
        summary = rt.ledger.append({
            "type": "generation", "generation": g, "played": played, "sessions": len(ids), "decisions": len(rows),
            "acted": sum(r.decision.get("route") == "act" for r in rows),
            "fixes": fixes, "harms": harms,
            "harmed": sum(b for b, _ in harm), "acted_sessions": sum(n for _, n in harm),
            "risk": budget, "metrics": _aggregate(metrics),
            "baseline": _aggregate(again) if again is not None else None,
            "candidate": card["version"], "gate": card["gate"], "deployed": card.get("deployed"),
            "plan": proposed.hypothesis if proposed is not None else None,
            "skipped": None if train else "; ".join(reasons),
            "champion": (registry.champion() or {}).get("version"), "planted": planted, "revoked": revoked})
        out(_line(summary))
    every = {s for s in rt.ledger.sessions()[0] if _ID.fullmatch(s)}     # a continued workspace's too, from the ledger
    harm = [monitor.realized(rt, point, every) for point in rt.points]
    out(f"all {len({s[:4] for s in every})} generations: harmed sessions "
        f"{_risk(sum(b for b, _ in harm), sum(n for _, n in harm), budget)}")
    out(f"on disk, in {rt.root}:\n"
        f"  ledger/   every session (its release), decision (its route and model) and outcome; each game's metrics "
        f"and each generation's line; gate and monitor events\n"
        f"  gates/    the frozen gate template, and each round's record: test sessions, verdict\n"
        f"  models/   each candidate's card (parent, gate record, test sessions, verdict), data and model; "
        f"champion.json\n"
        f"  harness/  the promoted models' calibrations, and revoked.json" +
        ("\n  baseline/ the same games played by the defaults alone: their ledger" if baseline is not None else ""))
    return rt.root
