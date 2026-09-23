"""The research agent interface (PLAN §5.4). An agent only proposes candidates; the framework evaluates,
promotes and records them, so a custom agent cannot bypass the promotion gate or see test data."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..ledger import new_id
from ..types import Answer
from .ask import fit_state, normalize


@dataclass
class Candidate:
    kind: str                 # "spec", "recipe", "prompt", or a custom kind that comes with its own evaluator
    point: str
    change: dict
    hypothesis: str
    cost: str = "low"         # resource estimate
    meta: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("c"))


@dataclass
class Report:
    points: dict              # point -> {"n", "brier", "accuracy"} on the dev split
    clusters: list[dict]      # dev-split errors grouped by (gold, predicted, error signature)


class Workspace:
    """Everything a research agent may see and do. The test split is deliberately not reachable from here."""

    def __init__(self, runtime, report: Report, experience: list[dict], llm, judge, kinds: list[str] | None = None,
                 context: dict | None = None):
        self.report = report
        self.experience = experience
        self.kinds = kinds or ["spec"]      # candidate kinds the framework can evaluate
        self.context = context or {}        # e.g. the current training recipe and its allowed keys
        self._rt = runtime
        self._llm = llm
        self._judge = judge

    def spec(self, point: str) -> dict:
        return self._rt.points[point].spec.to_json()

    def point_type(self, point: str) -> str:
        return self._rt.points[point].type

    def ask(self, name: str, question, state: str, *, max_state_chars: int = 8000) -> tuple[Answer, str]:
        """Ask the judge a typed question; returns the answer and the id of the recorded decision."""
        q = normalize(question)
        state, truncated = fit_state(state, max_state_chars)
        return self._rt.ask_question(name, q, state, backend=self._judge, meta={"truncated": truncated})

    def llm(self, system: str, prompt: str) -> str:
        return self._llm.complete(system, prompt)


class ResearchAgent(Protocol):
    def step(self, ws: Workspace) -> list[Candidate]: ...
