"""Decision points: a typed question asked at a fixed place in a loop.

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


ANSWERERS = ("rule:", "llm:")


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
    phases: dict[str, str | dict] = field(default_factory=dict)   # where in a loop it is asked -> its default there

    @property
    def spec_hash(self) -> str:
        return self.hash_of(self.spec)

    def hash_of(self, spec: Spec) -> str:
        """The spec hash with `spec`. Temperatures and thresholds are keyed by it, so it covers everything that shapes
        what the model sees (the view and where it is cut) and the phases' routing defaults (their labels, and the
        rule answerers whose answer is the default instead, when there are any): a threshold only means something
        against the view and the default it was fitted for. A rule's code is not hashed: bump view_version with it."""
        rules = {ph: a for ph in self.phases if (a := self.default_for(ph)[2] or "").startswith("rule:")}
        return _digest([self.type, spec.to_json(), self.view_version, self.max_view_chars, self.keep,
                        {ph: self.default_for(ph)[1] for ph in self.phases}, *([rules] if rules else [])])

    def default_for(self, phase: str | None = None) -> tuple[str | None, str | None, str | None]:
        """-> (phase, its fixed label, its default answerer). A phase declares its default as a label, or as
        {"default": "rule:<module:function>" | "llm:<backend name>", "label": <taken when that answerer fails>}.
        A point whose candidates change with the context may leave the label out, since no one label is always a
        candidate: when its answerer fails the host decides (None), and any answer may act. Without `phase`, a point
        with exactly one phase is asked in that one; a point with none has no default (None: the host decides) and
        can never act."""
        if phase is None:
            if len(self.phases) > 1:
                raise ValueError(f"point {self.name!r} is asked in phases {sorted(self.phases)}: pass phase=")
            if not self.phases:
                return None, None, None
            phase = next(iter(self.phases))
        if phase not in self.phases:
            raise ValueError(f"point {self.name!r} declares no phase {phase!r} (phases: {sorted(self.phases)})")
        d = self.phases[phase]
        default, label = (d.get("default"), d.get("label")) if isinstance(d, dict) else (d, None)
        if not (isinstance(default, str) and default.startswith(ANSWERERS)):
            return phase, default, None
        if label is None and self.candidates is None:
            raise ValueError(f"point {self.name!r}, phase {phase!r}: {default} needs a fixed label to fall back to")
        return phase, label, default

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
    route: str              # "act", "default", "shadow", "rule" or "error"
    explored: bool
    propensity: float
    action: str | None      # what to do: the model's label when it acted, else the rule's or the phase's default
    model: str
    acted: bool = False     # the model's own answer is the action (route "act")
