"""Harness prompt candidates (PLAN §4.8): run the task suite with the candidate prompt, screen it on a few
development tasks, then compare it with the champion on every task, held-out tasks included, through the promotion
gate.

The loss of a run is 1 - the share of checks passed, so lower is better, as with Brier in the other gates. Each run
is recorded once per (prompt, agent model, task, repeat) in harness/runs.jsonl and reused from then on: the
champion's runs carry over between rounds, and a prompt cannot be re-run until it gets lucky.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from ..agent.prompt import HarnessPrompt, PromptStore
from ..agent.suite import Task, run_task
from ..ledger import _append_jsonl, _read_jsonl, now, redact
from .replay import EvalResult


class SuiteRunner:
    def __init__(self, root: str | Path, make_agent: Callable, identity: str):
        """make_agent(workdir, task, prompt) -> Agent. `identity` names the agent's model(s) in the run records."""
        self.root = Path(root)
        self.path = self.root / "harness" / "runs.jsonl"
        self.make_agent = make_agent
        self.identity = identity
        self.cache = {(r["prompt"], r["agent"], r["task_hash"], r["repeat"]): r for r in _read_jsonl(self.path)}
        self.executed = 0

    def runs(self, prompt: HarnessPrompt, tasks: list[Task], repeats: int = 1, *, fresh: bool = False,
             on_run: Callable[[dict], None] | None = None) -> list[dict]:
        out = []
        for task in tasks:
            for r in range(repeats):
                key = (prompt.hash(), self.identity, task.hash(), r)
                row = None if fresh else self.cache.get(key)
                if row is None:
                    run = run_task(task, lambda workdir, t: self.make_agent(workdir, t, prompt))
                    self.executed += 1
                    row = redact({"ts": now(), "prompt": key[0], "agent": key[1], "task_hash": key[2], "repeat": r,
                                  "held_out": task.held_out, **asdict(run)})
                    if run.score is not None and key not in self.cache:   # the first complete run stays on record
                        self.cache[key] = row
                        _append_jsonl(self.path, row)
                if on_run:
                    on_run(row)
                out.append(row)
        return out

    def actions(self, session: str, n: int = 8) -> list[str]:
        """The last tool calls of a recorded agent session, e.g. "bash python3 -m unittest"."""
        calls = []
        for e in _read_jsonl(self.root / "transcripts" / f"{session}.jsonl"):
            for c in e.get("tool_calls") or []:
                target = c["args"].get("command") or c["args"].get("path") or json.dumps(c["args"])[:80]
                calls.append(f"{c['name']} {str(target)[:120]}")
        return calls[-n:]


def prompt_diff(old: HarnessPrompt, new: HarnessPrompt) -> list[str]:
    """Sentences and tool descriptions the new prompt adds (+) or drops (-)."""
    def parts(p: HarnessPrompt) -> list[str]:
        return ([s for s in re.split(r"(?<=[.!?])\s+|\n+", p.system) if s.strip()]
                + [f"[tool {k}] {v}" for k, v in sorted(p.descriptions().items())])
    a, b = parts(old), parts(new)
    return [f"+ {s}" for s in b if s not in a] + [f"- {s}" for s in a if s not in b]


def _effects(ra: list[dict], rb: list[dict], held_out: bool = False) -> dict[str, float]:
    """Mean score change per task where the candidate changed anything, for one side of the split.

    `effects` (held_out=False) is what the research agent reads, so it must never carry a held-out task.
    `held_out_effects` is recorded beside it for the human record: a candidate rejected on a held-out
    regression whose record showed no effects at all was how this split was found.
    """
    changes = defaultdict(list)
    for x, y in zip(ra, rb):
        if bool(x["held_out"]) is held_out and x["score"] is not None and y["score"] is not None:
            changes[x["task"]].append(y["score"] - x["score"])
    return {t: round(sum(v) / len(v), 2) for t, v in changes.items() if any(abs(d) > 1e-9 for d in v)}


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _losses(rows: list[dict]) -> list[float | None]:
    return [None if r["score"] is None else 1.0 - r["score"] for r in rows]


def _failures(rows: list[dict]) -> list[str]:
    failed = [r for r in rows if r["score"] is None]
    return [f"{len(failed)} runs broke before their checks, e.g. {failed[0]['task']}: {failed[0]['error']}"] if failed else []


def prompt_evaluator(tasks: list[Task], runner: SuiteRunner, *, repeats: int = 2, screen: int = 3,
                     min_items: int = 10):
    """Evaluator for research candidates of kind "prompt"; change = {"system": ..., "tools": {name: description}}.

    The gate sees the development split only. Held-out tasks are still run, and their result is recorded, but it
    takes no part in accepting or rejecting: a split that vetoes candidates is part of the selection procedure,
    not an independent estimate of what that procedure produces. Screening is a cost device, not a filter with
    standing: it only stops a candidate that is strictly worse on the screening subset, and it has no power to
    stop one whose screening tasks sit at their floor.
    """
    dev = [t for t in tasks if not t.held_out]

    def evaluate(rt, cand, test_frac: float = 0.15) -> EvalResult:
        store = PromptStore(rt.root)
        champion = store.get()
        challenger = champion.apply(cand.change)

        def apply() -> None:
            store.set(challenger, note=f"{cand.id}: {cand.hypothesis}"[:500])

        details = {"diff": prompt_diff(champion, challenger)}
        if challenger.same_as(champion):
            return EvalResult("harness", [], [], [], apply, details=details,
                              rejected=["no effective change: the same prompt once whitespace is ignored"])
        if screen and dev:
            part = dev[:screen]
            ra, rb = runner.runs(champion, part), runner.runs(challenger, part)
            if _failures(ra + rb):
                return EvalResult("harness", [], [], [], apply, details=details, insufficient=_failures(ra + rb))
            a, b = _losses(ra), _losses(rb)
            if sum(b) > sum(a):
                return EvalResult("harness", a, b, [t.name for t in part], apply,
                                  details={**details, "effects": _effects(ra, rb), "stage": "screening",
                                           "screening_is": "a cost device, not a validated filter"},
                                  rejected=[
                                      f"screening: solved {len(part) - sum(b):.2f} of {len(part)} dev tasks, "
                                      f"the champion {len(part) - sum(a):.2f}"])
        ra, rb = runner.runs(champion, tasks, repeats), runner.runs(challenger, tasks, repeats)
        if _failures(ra + rb):
            return EvalResult("harness", [], [], [], apply, details=details, insufficient=_failures(ra + rb))
        dev_i = [i for i, r in enumerate(ra) if not r["held_out"]]
        held_i = [i for i, r in enumerate(ra) if r["held_out"]]
        a, b = _losses(ra), _losses(rb)
        groups = [r["task"] for r in ra]
        held_delta = None
        if held_i:
            held_delta = _mean([b[i] - a[i] for i in held_i])
        details = {**details, "effects": _effects(ra, rb),
                   "held_out_effects": _effects(ra, rb, held_out=True), "held_out_delta": held_delta,
                   "held_out_n": len(held_i), "gated_on": "dev"}
        # The gate's items are the development split; held-out is measured and reported, never selected on.
        return EvalResult("harness", [a[i] for i in dev_i], [b[i] for i in dev_i], [groups[i] for i in dev_i],
                          apply, details=details,
                          gate={"min_items": min_items, "metric": "task failure rate"})

    return evaluate


def suite_context(tasks: list[Task], runner: SuiteRunner) -> Callable[[], dict]:
    """What the research agent sees about the harness prompt: the champion and its results on the development
    tasks. Held-out tasks are never shown."""
    dev = {t.name: t for t in tasks if not t.held_out}

    def context() -> dict:
        prompt = PromptStore(runner.root).get()
        rows = runner.runs(prompt, list(dev.values()))
        return {"prompt": prompt.to_json(), "tools": prompt.descriptions(), "suite": [
            {"task": r["task"], "score": r["score"], "prompt": dev[r["task"]].prompt[:400], "turns": r["turns"],
             "stop": r["stop"], "actions": runner.actions(r["session"]) if r["session"] else [],
             "failed_checks": r["check_output"][:800], "final_message": r["text"][:400], "error": r["error"][:200]}
            for r in rows]}

    return context
