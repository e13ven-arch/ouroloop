"""The runtime that ties decision points, backends, default answerers and the ledger together."""
from __future__ import annotations

import importlib
import random
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .decision import Decision, DecisionPoint, Spec
from .ledger import CalibrationStore, Ledger, SessionStore, SpecStore
from .policy import Policy, Route, route
from .types import Backend, Question, Request, temper


def import_object(ref: str):
    """"package.module:name" -> the object."""
    module, _, attr = ref.partition(":")
    return getattr(importlib.import_module(module), attr)


class Runtime:
    def __init__(self, root: str | Path, backends: dict[str, Backend], default_backend: str,
                 point_backends: dict[str, str] | None = None, seed: int | None = None):
        self.root = Path(root)
        self.ledger = Ledger(self.root / "ledger")
        self.sessions = SessionStore(self.root / "sessions")
        self.specs = SpecStore(self.root / "harness" / "specs.json")
        self.calibration = CalibrationStore(self.root / "harness" / "calibration.json")
        self.backends = backends
        self.default_backend = default_backend
        self.point_backends = dict(point_backends or {})
        self.points: dict[str, DecisionPoint] = {}
        self.rng = random.Random(seed)

    def register(self, point: DecisionPoint) -> DecisionPoint:
        """Add a decision point. A promoted spec (harness/specs.json) replaces the one in code. Each phase's default
        must be one of the point's labels, and its default answerer must resolve."""
        stored = self.specs.get(point.name)
        if stored is not None:
            point.spec = Spec.from_json(stored)
        labels = None if point.candidates else point.question().labels()
        for phase in point.phases:
            _, label, answerer = point.default_for(phase)
            if labels is not None and label not in labels:
                raise ValueError(f"point {point.name!r}, phase {phase!r}: default {label!r} is not one of {labels}")
            kind, _, ref = (answerer or "").partition(":")
            if kind == "llm" and ref not in self.backends:
                raise ValueError(f"point {point.name!r}, phase {phase!r}: no backend {ref!r} for {answerer}")
            if kind == "rule":
                import_object(ref)
        self.points[point.name] = point
        return point

    def backend_for(self, point: str) -> Backend:
        return self.backends[self.point_backends.get(point, self.default_backend)]

    def calibration_for(self, point: str | DecisionPoint, model: str) -> dict:
        """The calibration of this model (as its backend reports it) on the point's current spec, if any."""
        p = self.points[point] if isinstance(point, str) else point
        return self.calibration.get(model, p.spec_hash, p.name)

    def temperature(self, point: str | DecisionPoint, model: str) -> float:
        return float(self.calibration_for(point, model).get("temperature", 1.0))

    def threshold(self, point: str | DecisionPoint, model: str, phase: str | None) -> float | None:
        """The lowest confidence at which the point may act on this model's answers in this phase (PLAN_v0.2 §0 rule
        3): only from a calibration that carries the hash of the gate record that promoted it, and only for a model
        that names itself (an empty id would match every backend that reports none)."""
        cal = self.calibration_for(point, model)
        return cal.get("thresholds", {}).get(phase) if cal.get("gate") and phase and model else None

    def set_calibration(self, point: str, model: str, temperature: float, thresholds: dict | None = None,
                        gate: str | None = None, **extra) -> None:
        """Store a calibration of this model on the point's current spec. Its thresholds let the point act only with
        `gate`, the hash of the gate record that promoted it (M8)."""
        self.calibration.set(model, self.points[point].spec_hash, point,
                             {"temperature": temperature, "thresholds": dict(thresholds or {}), "gate": gate, **extra})

    def session(self, session: str, meta: dict | None = None) -> None:
        """Record what a session was run with (task, held-out, agent models) before any of its decisions."""
        self.ledger.append({**(meta or {}), "type": "session", "session": session})

    def ask(self, point: str | DecisionPoint, ctx: Any, *, session: str = "s_default", step: int | None = None,
            phase: str | None = None, veto: str | Callable[[str | None], str | None] | None = None) -> Decision:
        """Ask the point's backend and apply the execution rule (PLAN_v0.2 §0). When the model does not act, the
        phase's default answerer answers, or its fixed label is taken. `veto` is a rule's verdict: a label, or a
        function of the action about to be taken that returns a label to overrule it. The rule's label is the action
        (route "rule") and the model's answer is only recorded. Whatever breaks on the way (view, question, backend,
        calibration, routing) is recorded on route "error" with the raw context, and the default is taken."""
        p = self.points[point] if isinstance(point, str) else point
        phase, label, answerer = p.default_for(phase)
        ref = self.sessions.put(session, ctx, step)
        view, truncated, q, ans, probs, backend, extra = None, False, None, None, None, "", {}
        try:
            view, truncated = p.render(ctx)
            q = p.question(ctx).presented(self.rng)   # candidate order is shuffled, and recorded as shown
            b = self.backend_for(p.name)
            backend = b.name
            ans = b.decide([Request(view, q)])[0]
            if not ans.over(q.labels()):     # a backend may build its Answer itself: an unknown label, raw scores
                raise ValueError(f"answered {ans.probs}, not a distribution over {q.labels()}")
            ans = replace(ans, probs=temper(ans.probs, self.temperature(p, ans.model)))
            probs = [ans.probs[lab] for lab in q.labels()]
            r = route(p.policy, ans, label, self.threshold(p, ans.model, phase), self.rng)
        except Exception as e:  # noqa: BLE001 - whatever breaks, the default is taken and the row says why
            r, extra["error"] = Route("error", False, 1.0), f"{type(e).__name__}: {e}"[:500]
        if ans is not None and ans.abstained:
            extra["abstained"] = True
        if isinstance(veto, str):
            action = veto
        elif r.route == "act":
            action = ans.label
        else:
            action = self._default(ctx, view, q, label, answerer, extra)
        if callable(veto):
            veto = veto(action)
        if veto is not None:
            r, action = Route("rule", False, 1.0), veto
        model = ans.model if ans else ""
        did = self.ledger.decision(
            session=session, ctx=ref, point=p.name, spec_hash=p.spec_hash, backend=backend, model=model, view=view,
            view_chars=len(view or ""), truncated=truncated, question=q.to_json() if q else None,
            candidates=q.labels() if q else None, probs=probs, route=r.route, explored=r.explored,
            propensity=r.propensity, action=action, usage=ans.usage if ans else {}, phase=phase, default=label,
            **extra)
        return Decision(did, p.name, ans.label if ans and ans.decisive else None, ans.probs if ans else {},
                        ans.confidence if ans else 0.0, r.route, r.explored, r.propensity, action, model,
                        r.route == "act")

    def _default(self, ctx: Any, view: str | None, q: Question | None, label: str | None, answerer: str | None,
                 extra: dict) -> str | None:
        """The phase's default: its answerer's answer, or its fixed label when it has none or the answerer fails
        (an exception, a timeout, an abstention or a tie, an answer that is not a label). The answerer is recorded;
        its answer is for evaluation only, never a training target."""
        if answerer is None:
            return label
        extra["answerer"] = answerer
        kind, _, ref = answerer.partition(":")
        try:
            if kind == "rule":
                out = import_object(ref)(ctx)
            else:
                a = self.backends[ref].decide([Request(view, q)])[0]
                out = a.label if a.decisive else None
            if out not in q.labels():
                raise ValueError(f"answered {out!r}")
            return out
        except Exception as e:  # noqa: BLE001 - the fixed label is the default of last resort
            extra["answerer_error"] = f"{type(e).__name__}: {e}"[:300]
            return label

    def record(self, point: str | DecisionPoint, ctx: Any, *, session: str = "s_default", step: int | None = None,
               phase: str | None = None, veto: str | Callable[[str | None], str | None] | None = None) -> Decision:
        """Log a decision point's context without asking the decision model (shadow collection): the action is the
        phase's default, as its answerer gives it (see `ask`), or a rule's `veto` (route "rule"). The model's
        probabilities can be computed later by offline replay; only the context and the outcome are needed."""
        p = self.points[point] if isinstance(point, str) else point
        phase, label, answerer = p.default_for(phase)
        view, truncated = p.render(ctx)
        q = p.question(ctx)
        extra: dict = {}
        action = veto if isinstance(veto, str) else self._default(ctx, view, q, label, answerer, extra)
        if callable(veto):
            veto = veto(action)
        r, action = ("shadow", action) if veto is None else ("rule", veto)
        did = self.ledger.decision(
            session=session, ctx=self.sessions.put(session, ctx, step), point=p.name, spec_hash=p.spec_hash,
            backend="none", model="", view=view, view_chars=len(view), truncated=truncated, question=q.to_json(),
            candidates=q.labels(), probs=None, route=r, explored=False, propensity=1.0, action=action, usage={},
            phase=phase, default=label, **extra)
        return Decision(did, p.name, None, {}, 0.0, r, False, 1.0, action, "")

    def ask_question(self, name: str, question: Question, state: str, *, default: str | None = None,
                     backend: Backend | None = None, session: str = "research", meta: dict | None = None) -> Decision:
        """A one-off typed question (no decision point, no view), recorded like any other decision. It goes through
        the execution rule as a point that is not switched on: the answer is recorded and `default` is the action."""
        backend = backend or self.backend_for(name)
        question = question.presented(self.rng)
        ans = backend.decide([Request(state, question)])[0]
        r = route(Policy(), ans, default, None, self.rng)
        did = self.ledger.decision(session=session, point=name, backend=backend.name, model=ans.model, view=state,
                                   view_chars=len(state), question=question.to_json(), candidates=question.labels(),
                                   probs=[ans.probs[lab] for lab in question.labels()], route=r.route,
                                   explored=r.explored, propensity=r.propensity, action=default, usage=ans.usage,
                                   phase=None, default=default, **({"abstained": True} if ans.abstained else {}),
                                   **(meta or {}))
        return Decision(did, name, ans.label if ans.decisive else None, ans.probs, ans.confidence, r.route,
                        r.explored, r.propensity, default, ans.model)

    def outcome(self, decision: str, label: str | None, source: str = "env", weight: float = 1.0, **extra) -> None:
        self.ledger.outcome(decision, label, source, weight, **extra)
