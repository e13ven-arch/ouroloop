"""Decision points: a typed question asked at a fixed place in a loop (PLAN §2.1).

The evolvable text of a point (question wording, candidate descriptions, view parameters) lives in a `Spec`,
so a research agent can propose a changed spec and the framework can replay it offline.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .policy import Policy
from .types import Question


def _digest(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


@dataclass
class Spec:
    instructions: str
    criteria: Any = None
    view_params: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"instructions": self.instructions, "criteria": self.criteria, "view_params": self.view_params}

    @classmethod
    def from_json(cls, d: dict) -> "Spec":
        return cls(d["instructions"], d.get("criteria"), dict(d.get("view_params") or {}))

    def apply(self, change: dict) -> "Spec":
        """A new spec with `change` merged in. Dict criteria merge key by key; other fields replace."""
        unknown = set(change) - {"instructions", "criteria", "view_params"}
        if unknown:
            raise ValueError(f"a spec change may only touch instructions, criteria and view_params, not {sorted(unknown)}")
        criteria = self.criteria
        if "criteria" in change:
            new = change["criteria"]
            criteria = {**criteria, **new} if isinstance(criteria, dict) and isinstance(new, dict) else new
        return Spec(change.get("instructions", self.instructions), criteria,
                    {**self.view_params, **change.get("view_params", {})})


def default_view(ctx: Any, params: dict) -> str:
    return ctx if isinstance(ctx, str) else json.dumps(ctx, ensure_ascii=False)


@dataclass
class DecisionPoint:
    name: str
    type: str
    spec: Spec
    view: Callable[[Any, dict], str] = default_view
    policy: Policy = field(default_factory=Policy)
    max_view_chars: int = 4000                            # keep in line with the backend's state limit
    keep: str = "tail"                                    # which end of an over-long view to keep; errors sit at the end
    candidates: Callable[[Any], dict] | None = None       # runtime-generated choice candidates
    view_version: str = "1"                               # bump when the view function's code changes

    @property
    def spec_hash(self) -> str:
        return _digest([self.type, self.spec.to_json(), self.view_version])

    def question(self, ctx: Any = None, spec: Spec | None = None) -> Question:
        s = spec or self.spec
        criteria = s.criteria
        if self.type == "choice" and self.candidates is not None and ctx is not None:
            criteria = self.candidates(ctx)
        return Question(self.type, s.instructions, criteria)

    def render(self, ctx: Any, spec: Spec | None = None) -> tuple[str, bool]:
        text = self.view(ctx, (spec or self.spec).view_params)
        if len(text) <= self.max_view_chars:
            return text, False
        return (text[-self.max_view_chars:] if self.keep == "tail" else text[:self.max_view_chars]), True


def Noul(name: str, question: str, criteria: dict | None = None, *, view_params: dict | None = None,
         **kw) -> DecisionPoint:
    return DecisionPoint(name, "noul", Spec(question, criteria, view_params or {}), **kw)


def Choice(name: str, question: str, candidates: dict | Callable[[Any], dict], *, view_params: dict | None = None,
           **kw) -> DecisionPoint:
    if callable(candidates):
        return DecisionPoint(name, "choice", Spec(question, None, view_params or {}), candidates=candidates, **kw)
    return DecisionPoint(name, "choice", Spec(question, dict(candidates), view_params or {}), **kw)


def Score(name: str, question: str, levels: list[str], *, view_params: dict | None = None, **kw) -> DecisionPoint:
    return DecisionPoint(name, "score", Spec(question, list(levels), view_params or {}), **kw)


@dataclass
class Decision:
    id: str
    point: str
    label: str
    probs: dict[str, float]
    confidence: float
    route: str
    explored: bool
    propensity: float
    action: str | None      # what was done: the model's label (auto), the fallback's label, or None (host decides)
    model: str
