"""The research agent interface. An agent only proposes candidates; the framework evaluates, promotes and records them,
so a custom agent cannot bypass the promotion gate or see test data."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..train.decision import Decision
from ..ledger import new_id
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
    clusters: list[dict]      # dev-split errors grouped by (right answers, predicted, error signature)


class Workspace:
    """Everything a research agent may see and do. The test split is deliberately not reachable from here."""

    def __init__(self, runtime, report: Report, experience: list[dict], llm, judge, kinds: list[str] | None = None,
                 context: dict | None = None):
        self.report = report
        # held-out results are for people: a research agent that chose on them would make held-out part of the
        # selection, no longer an estimate of what it produces
        self.experience = [{**e, "details": {k: v for k, v in e["details"].items() if not k.startswith("held_out")}}
                           if isinstance(e.get("details"), dict) else e for e in experience]
        self.kinds = kinds or ["spec"]      # candidate kinds the framework can evaluate
        self.context = context or {}        # e.g. the current training recipe and its allowed keys
        self._rt = runtime
        self._llm = llm
        self._judge = judge

    def spec(self, point: str) -> dict:
        return self._rt.points[point].spec.to_json()

    def point_type(self, point: str) -> str:
        return self._rt.points[point].type

    def ask(self, name: str, question, state: str, *, default: str | None = None,
            max_state_chars: int = 8000) -> Decision:
        """Ask the judge a typed question; returns the recorded decision. Research points are not switched on:
        the judge's answer is recorded and `default` is the action."""
        q = normalize(question)
        state, truncated = fit_state(state, max_state_chars)
        return self._rt.ask_question(name, q, state, default=default, backend=self._judge,
                                     meta={"truncated": truncated})

    def llm(self, system: str, prompt: str) -> str:
        return self._llm.complete(system, prompt)


class ResearchAgent(Protocol):
    def step(self, ws: Workspace) -> list[Candidate]: ...
