"""Core types: typed questions, requests to a decision backend, and answers.

One primitive covers all three question types (same convention as Jev and TDE):
    noul    criteria = {"true": ..., "false": ...} or None     -> probabilities over "yes" / "no"
    choice  criteria = {label: description}                    -> probabilities over labels
    score   criteria = [level description, ...]                -> probabilities over "0".."n-1"
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Protocol

TYPES = ("noul", "choice", "score")


@dataclass
class Question:
    type: str
    instructions: str
    criteria: Any = None

    def labels(self) -> list[str]:
        if self.type == "noul":
            return ["yes", "no"]
        if self.type == "choice":
            return [str(k) for k in self.criteria]
        return [str(i) for i in range(len(self.criteria))]

    def presented(self, rng: random.Random) -> "Question":
        """The same question with its candidates in a shuffled order.

        Presentation order is an artefact of how a question was written, and a dataset built from real decisions
        inherits the writer's habits: the "do nothing" option tends to be written last, and a campaign that
        rarely abandons anything then teaches "never pick the last option". Shuffling at presentation time
        removes the artefact from both the answer and the record. `score` is ordinal and `noul` is fixed, so
        only `choice` is shuffled.
        """
        if self.type != "choice" or not isinstance(self.criteria, dict) or len(self.criteria) < 2:
            return self
        items = list(self.criteria.items())
        rng.shuffle(items)
        return Question(self.type, self.instructions, dict(items))

    def to_json(self) -> dict:
        d = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            d["criteria"] = self.criteria
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Question":
        return cls(d["type"], d["instructions"], d.get("criteria"))


@dataclass
class Request:
    state: str
    question: Question


@dataclass
class Answer:
    probs: dict[str, float]
    model: str = ""
    usage: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return max(self.probs, key=self.probs.get)

    @property
    def confidence(self) -> float:
        return max(self.probs.values()) if self.probs else 0.0


class Backend(Protocol):
    name: str

    def decide(self, requests: list[Request]) -> list[Answer]: ...


def normalize(labels: list[str], raw: dict[str, float]) -> dict[str, float]:
    """Probabilities over exactly `labels`: missing labels get 0, the rest is renormalised (uniform if empty)."""
    p = {lab: max(0.0, float(raw.get(lab, 0.0))) for lab in labels}
    total = sum(p.values())
    if total <= 0:
        return {lab: 1.0 / len(labels) for lab in labels}
    return {lab: v / total for lab, v in p.items()}


def temper(probs: dict[str, float], temperature: float) -> dict[str, float]:
    """Temperature scaling on log-probabilities: T > 1 softens, T < 1 sharpens."""
    if temperature == 1.0 or not probs:
        return probs
    z = {k: math.log(max(v, 1e-12)) / temperature for k, v in probs.items()}
    m = max(z.values())
    e = {k: math.exp(v - m) for k, v in z.items()}
    total = sum(e.values())
    return {k: v / total for k, v in e.items()}


def nll(probs: dict[str, float], target: dict[str, float]) -> float:
    return -sum(t * math.log(max(probs.get(lab, 0.0), 1e-6)) for lab, t in target.items())


def brier(probs: dict[str, float], target: dict[str, float]) -> float:
    labels = set(probs) | set(target)
    return sum((probs.get(lab, 0.0) - target.get(lab, 0.0)) ** 2 for lab in labels)
