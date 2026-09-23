"""The runtime that ties decision points, backends, the fallback and the ledger together."""
from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from .decision import Decision, DecisionPoint, Spec
from .ledger import CalibrationStore, Ledger, SessionStore, SpecStore, new_id
from .policy import route
from .types import Answer, Backend, Question, Request, temper


class Runtime:
    def __init__(self, root: str | Path, backends: dict[str, Backend], default_backend: str,
                 point_backends: dict[str, str] | None = None, fallback: Backend | None = None,
                 seed: int | None = None):
        self.root = Path(root)
        self.ledger = Ledger(self.root / "ledger")
        self.sessions = SessionStore(self.root / "sessions")
        self.specs = SpecStore(self.root / "harness" / "specs.json")
        self.calibration = CalibrationStore(self.root / "harness" / "calibration.json")
        self.backends = backends
        self.default_backend = default_backend
        self.point_backends = dict(point_backends or {})
        self.fallback = fallback
        self.points: dict[str, DecisionPoint] = {}
        self.rng = random.Random(seed)

    def register(self, point: DecisionPoint) -> DecisionPoint:
        """Add a decision point. A promoted spec (harness/specs.json) replaces the one in code, and a fitted
        threshold (harness/calibration.json) replaces the policy's."""
        stored = self.specs.get(point.name)
        if stored is not None:
            point.spec = Spec.from_json(stored)
        cal = self.calibration.get(point.name)
        if cal.get("threshold") is not None:
            point.policy.threshold = cal["threshold"]
        self.points[point.name] = point
        return point

    def backend_for(self, point: str) -> Backend:
        return self.backends[self.point_backends.get(point, self.default_backend)]

    def temperature(self, point: str) -> float:
        return float(self.calibration.get(point).get("temperature", 1.0))

    def set_calibration(self, point: str, temperature: float, threshold: float | None, **extra) -> None:
        self.calibration.set(point, {"temperature": temperature, "threshold": threshold, **extra})
        if point in self.points:
            self.points[point].policy.threshold = threshold

    def ask(self, point: str | DecisionPoint, ctx: Any, *, session: str = "s_default",
            step: int | None = None) -> Decision:
        p = self.points[point] if isinstance(point, str) else point
        view, truncated = p.render(ctx)
        q = p.question(ctx)
        backend = self.backend_for(p.name)
        ans = backend.decide([Request(view, q)])[0]
        ans = Answer(temper(ans.probs, self.temperature(p.name)), ans.model, ans.usage)
        r = route(p.policy, ans.label, ans.confidence, self.rng)
        action = ans.label if r.route == "auto" else None
        escalation = None
        if r.route == "fallback":
            if p.policy.fallback == "default":
                action = p.policy.default
            elif p.policy.fallback == "llm" and self.fallback is not None:
                fa = self.fallback.decide([Request(view, q)])[0]
                action = fa.label
                escalation = (fa.label, fa.probs, f"{self.fallback.name}:{fa.model}",
                              getattr(self.fallback, "source", "frontier_llm"))
            # fallback == "human": the host asks a person and reports it with outcome(source="human")
        did = self.ledger.decision(
            session=session, ctx=self.sessions.put(session, ctx, step), point=p.name, spec_hash=p.spec_hash,
            backend=backend.name, model=ans.model, view=view, view_chars=len(view), truncated=truncated,
            question=q.to_json(), candidates=q.labels(), probs=[ans.probs[lab] for lab in q.labels()],
            route=r.route, explored=r.explored, propensity=r.propensity, action=action, usage=ans.usage)
        if escalation is not None:
            self.ledger.escalation(did, *escalation)
        return Decision(did, p.name, ans.label, ans.probs, ans.confidence, r.route, r.explored, r.propensity,
                        action, ans.model)

    def record(self, point: str | DecisionPoint, ctx: Any, *, session: str = "s_default",
               step: int | None = None) -> str:
        """Log a decision point's context without asking any model (shadow collection at zero cost). The model's
        probabilities can be computed later by offline replay; only the context and the outcome are needed."""
        p = self.points[point] if isinstance(point, str) else point
        view, truncated = p.render(ctx)
        q = p.question(ctx)
        return self.ledger.decision(
            session=session, ctx=self.sessions.put(session, ctx, step), point=p.name, spec_hash=p.spec_hash,
            backend="none", model="", view=view, view_chars=len(view), truncated=truncated, question=q.to_json(),
            candidates=q.labels(), probs=None, route="shadow", explored=False, propensity=1.0, action=None, usage={})

    def ask_question(self, name: str, question: Question, state: str, *, backend: Backend | None = None,
                     session: str = "research", meta: dict | None = None) -> tuple[Answer, str]:
        """A one-off typed question (no decision point, no view), recorded like any other decision."""
        backend = backend or self.backend_for(name)
        ans = backend.decide([Request(state, question)])[0]
        did = new_id("d")
        self.ledger.decision(did, session=session, point=name, backend=backend.name, model=ans.model, view=state,
                             view_chars=len(state), question=question.to_json(), candidates=question.labels(),
                             probs=[ans.probs[lab] for lab in question.labels()], route="auto", explored=False,
                             propensity=1.0, action=ans.label, usage=ans.usage, **(meta or {}))
        return ans, did

    def outcome(self, decision: str, label: str | None, source: str = "env", weight: float = 1.0, **extra) -> None:
        self.ledger.outcome(decision, label, source, weight, **extra)
