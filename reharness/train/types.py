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
    abstained: bool = False     # no usable answer (nothing to parse, no or malformed probabilities): never acted on

    @classmethod
    def of(cls, labels: list[str], raw: dict[str, float], model: str = "", usage: dict | None = None) -> "Answer":
        """From a backend's raw probabilities. With nothing left after normalising, the backend abstained: the
        answer is uniform, for evaluation, and is never acted on."""
        probs = normalize(labels, raw)
        return cls(probs or {lab: 1.0 / len(labels) for lab in labels}, model, usage or {}, probs is None)

    @property
    def label(self) -> str:
        return max(self.probs, key=self.probs.get)

    @property
    def confidence(self) -> float:
        return max(self.probs.values()) if self.probs else 0.0

    def over(self, labels: list[str]) -> bool:
        """Whether the probabilities are a distribution over exactly `labels`."""
        values = self.probs.values()
        return set(self.probs) == set(labels) and all(0 <= v <= 1 for v in values) and abs(sum(values) - 1) <= 1e-6

    @property
    def decisive(self) -> bool:
        """Not abstained, every probability a number, and one label more likely than every other: only such an
        answer may be acted on."""
        top = self.confidence
        return (not self.abstained and all(map(math.isfinite, self.probs.values()))
                and sum(top - v < 1e-9 for v in self.probs.values()) == 1)


class Backend(Protocol):
    name: str

    def decide(self, requests: list[Request]) -> list[Answer]: ...


def normalize(labels: list[str], raw: dict[str, float]) -> dict[str, float] | None:
    """Probabilities over exactly `labels`: missing labels get 0, the rest is renormalised. None when nothing is
    left (no probabilities, or all zero) or a value is not a probability (NaN, infinite, negative): renormalising
    the others would turn a broken answer into a confident one."""
    p = {lab: float(raw.get(lab, 0.0)) for lab in labels}
    total = sum(p.values())
    if total <= 0 or not all(math.isfinite(v) and v >= 0 for v in p.values()):
        return None
    return {lab: v / total for lab, v in p.items()}


def temper(probs: dict[str, float], temperature: float) -> dict[str, float]:
    """Temperature scaling on log-probabilities: T > 1 softens, T < 1 sharpens. Any other T is an error: T <= 0
    would make the least likely label the most likely one."""
    if not 0 < temperature < math.inf:
        raise ValueError(f"temperature {temperature} is not a positive number")
    if temperature == 1.0 or not probs:
        return probs
    z = {k: math.log(max(v, 1e-12)) / temperature for k, v in probs.items()}
    m = max(z.values())
    e = {k: math.exp(v - m) for k, v in z.items()}
    total = sum(e.values())
    return {k: v / total for k, v in e.items()}


def nll(probs: dict[str, float], target: dict[str, float]) -> float:
    return -sum(t * math.log(max(probs.get(lab, 0.0), 1e-6)) for lab, t in target.items())
