"""A TDE run directory loaded in-process. Needs the `tde` package, torch and a checkpoint, so run it on a GPU host."""
from __future__ import annotations

from pathlib import Path

from ..types import Answer, Request, normalize


class TdeBackend:
    name = "tde"

    def __init__(self, run: str, device: str | None = None, decider=None):
        self.run = run
        self.device = device
        self._decider = decider
        self.model = f"tde:{Path(run).name}"

    def _load(self):
        if self._decider is None:
            from tde.inference import Decider
            self._decider = Decider.from_run(self.run, device=self.device)
        return self._decider

    def decide(self, requests: list[Request]) -> list[Answer]:
        decider = self._load()
        by_state: dict[str, list[int]] = {}
        for i, r in enumerate(requests):
            by_state.setdefault(r.state, []).append(i)
        out: list[Answer | None] = [None] * len(requests)
        for state, idx in by_state.items():
            res = decider.decide_batch(state, {f"q{j}": requests[i].question.to_json() for j, i in enumerate(idx)})
            for j, i in enumerate(idx):
                q = requests[i].question
                out[i] = Answer(normalize(q.labels(), res[f"q{j}"]["probabilities"]), self.model)
        return out
