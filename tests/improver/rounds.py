"""The improver's test activities: the rsi domain of the fake upstream, the forgetful actor (rsi.BusinessWorker with
forget_notify: it updates the record and does not post the requested #ops message unless something reminds it), the
scripted R (scripted_r.ScriptedR), missed_action mechanizable in K, the baseline run and the gate plan frozen. The
notify pair is held out from R (rsi.HELD) and notify_ticket is the studied failure (rsi.STUDY).

`round_activity(..., memory=True)` turns the memory on with the in-process client and answers the bridge's calls with
fake_memory.ScriptedResearch, every other research call with the ScriptedR. `tried` writes an imported estimate:
{kind: n} tried n times on missed_action with no fix, which moves the upper bound to the next kind."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import rsi
from scripted_r import ScriptedR

from ouroloop.activity import phases
from ouroloop.contracts import write_json
from ouroloop.improver.settings import MECHANIZABLE


def quiet(_line: str) -> None:
    return None


class Research:
    """The research model: ReMe's bridge calls (a `[reme:op]` system) to fake_memory.ScriptedResearch, the rest to R."""
    name, model, effort, retries, timeout = "scripted-research", "scripted-research", "medium", 0, 60

    def __init__(self, r: ScriptedR, memory=None):
        self.r, self.memory = r, memory

    def chat(self, system, messages, specs, **kw):
        if self.memory is not None and "[reme:" in (system or ""):
            return self.memory.chat(system, messages, specs, **kw)
        return self.r.chat(system, messages, specs, **kw)


def round_activity(root: Path, upstream: str, mode: str = "tool", *, memory: bool = False, tried: dict | None = None,
                   actor=None, improver: dict | None = None, baseline: bool = True, freeze: bool = True,
                   splits_table: dict | None = None, **sections) -> SimpleNamespace:
    s = SimpleNamespace(r=ScriptedR(mode), actor=actor or rsi.BusinessWorker(forget_notify=True), client=None)
    extra = {}
    if memory:
        from fake_memory import FakeMemoryClient, ScriptedResearch
        s.client = FakeMemoryClient()
        s.research = Research(s.r, ScriptedResearch())
        extra = {"memory_client": lambda: s.client, "memory": {"enabled": True}}
    else:
        s.research = Research(s.r)
    s.a = rsi.activity(root, upstream, provider=s.actor, research=s.research, splits_table=splits_table,
                       improver={"mechanizable": list(MECHANIZABLE) + ["missed_action"], **(improver or {})},
                       **extra, **sections)
    if tried:
        write_json(s.a.root / "ledger" / "estimates.json",
                   {s.a.domain: {"missed_action": {k: {"fixes": 0.0, "cost": 0.0, "n": int(n)}
                                                   for k, n in tried.items()}}})
    if baseline:
        phases.baseline(s.a, out=quiet)
    if memory and baseline:
        phases.library(s.a, out=quiet)
    if freeze:
        phases.freeze(s.a, out=quiet)
    return s


def ledger_rows(a, stage: str | None = None) -> list[dict]:
    path = a.root / "ledger" / "experience.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return [r for r in rows if stage is None or r.get("stage") == stage]


def round_state(a, index: int) -> dict:
    return json.loads((a.root / "rounds" / f"r{index}" / "round.json").read_text(encoding="utf-8"))
