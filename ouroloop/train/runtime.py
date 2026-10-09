"""The decision runtime (docs/modules/01_POLICY_RUNTIME.md §4): decision points, their default answerers and the
ledger. A decision is recorded without asking a model (`record`, shadow collection), or asked of a bound policy
(`propose`: any object with `enabled(phase)`, `point`, `ref`, `kind` and `propose(ctx) -> (label, reason)`; the v1
policies.StopDecider is on tag archive-2026-10-09), and its outcome recorded (`outcome`). Nothing here reads a
champion pointer or a calibration: those, and the v1 runtime's `ask` through them, went with the champion pointer in
P1 step 5 (tag archive-2026-10-08); what may act is a bound policy's, under its release's authorisation."""
from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Callable

from ..contracts import CandidateDefect, import_object
from .ledger import Ledger, SessionStore
from .decision import Decision, DecisionPoint
from .policy import Route
from .types import Backend, Question, Request


class _NoPolicy(Exception):
    """No policy is bound at this phase: the fixed default is taken, and nothing is recorded as proposed."""


class Runtime:
    def __init__(self, root: str | Path, backends: dict[str, Backend] | None = None, default_backend: str | None = None,
                 point_backends: dict[str, str] | None = None, seed: int | None = None, serving=None):
        """`backends`: the LLM answerers a phase's default may name ("llm:<name>"); `serving`: how trained models are
        asked (serving.Serving), for the policy a caller binds."""
        self.root = Path(root)
        self.ledger = Ledger(self.root / "ledger")
        self.sessions = SessionStore(self.root / "sessions")
        self.backends = backends if backends is not None else {}
        self.serving = serving
        self.default_backend = default_backend
        self.point_backends = dict(point_backends or {})
        self.points: dict[str, DecisionPoint] = {}
        self.rng = random.Random(seed)

    def register(self, point: DecisionPoint) -> DecisionPoint:
        """Add a decision point. Each phase's default must be one of the point's labels, and its default answerer must
        resolve."""
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

    # ------------------------------------------------------------------ decisions
    def record(self, point: str | DecisionPoint, ctx: Any, *, session: str = "s_default", step: int | None = None,
               phase: str | None = None, veto: str | Callable[[str | None], str | None] | None = None) -> Decision:
        """Log a decision point's context without asking the decision model (shadow collection): the action is the
        phase's default, as its answerer gives it, or a rule's `veto` (route "rule"). The model's
        probabilities can be computed later by offline replay; only the context and the outcome are needed. Asking a
        model is a bound policy's (`propose`)."""
        p = self.points[point] if isinstance(point, str) else point
        phase, label, answerer = p.default_for(phase)
        rule = answerer if answerer and answerer.startswith("rule:") else None
        ref = self.sessions.put(session, ctx, step)
        view, truncated, q, extra = None, False, None, {}
        r, default = Route("shadow", False, 1.0), label       # the routing default: the rule's answer, if it has one
        try:
            view, truncated = p.render(ctx)
            q = p.question(ctx)
        except Exception as e:  # noqa: BLE001 - whatever breaks, the default is taken and the row says why
            r, extra["error"] = Route("error", False, 1.0), f"{type(e).__name__}: {e}"[:500]
        try:
            if isinstance(veto, str):
                action = veto
            elif rule:
                action = default = self._default(ctx, view, q, label, rule, extra)
            else:
                action = self._default(ctx, view, q, label, answerer, extra)
            if callable(veto):
                veto = veto(action)
        except Exception as e:  # noqa: BLE001 - a rule that breaks overrules nothing: the fixed default is taken
            r, veto, action = Route("error", False, 1.0), None, label
            extra["error"] = f"{type(e).__name__}: {e}"[:500]
        if veto is not None:
            r, action = Route("rule", False, 1.0), veto
        did = self.ledger.decision(
            session=session, ctx=ref, point=p.name, spec_hash=p.spec_hash, backend="none", model="", view=view,
            view_chars=len(view or ""), truncated=truncated, question=q.to_json() if q else None,
            candidates=q.labels() if q else None, probs=None, route=r.route, explored=r.explored,
            propensity=r.propensity, action=action, usage={}, phase=phase, default=default, **extra)
        return Decision(did, p.name, None, {}, 0.0, r.route, r.explored, r.propensity, action, "", False)

    def propose(self, point: str | DecisionPoint, ctx: Any, *, policy, session: str = "s_default",
                step: int | None = None, phase: str | None = None,
                veto: str | Callable[[str | None], str | None] | None = None,
                extra: dict | None = None, on_record: Callable[[dict], None] | None = None) -> Decision:
        """Ask a bound policy of the task/research profile (the duck type in the module docstring) and record what happened.

        This path never reads the champion pointer or the legacy calibration: what may act in that profile is
        decided by the release's authorisation (docs/modules/01_POLICY_RUNTIME.md §4), and reusing the old
        thresholds here would be borrowing a permission that was granted for another version.

        The row separates the two facts a trace needs: `proposed` is the policy's answer, `action` is what was
        executed, and `executor` says who decided it. A policy that abstains, or whose service is unavailable,
        leaves the phase's fixed default and the reason is recorded — it is not written down as agreement.

        `on_record` receives the row as it was written. A caller that needs it (the agent, to store the decision's
        trace) gets it here rather than reading the ledger back: the ledger is append-only and grows with the run,
        so re-reading it per decision would make a long run quadratic in its own history.
        """
        p = self.points[point] if isinstance(point, str) else point
        phase, label, _ = p.default_for(phase)
        ref = self.sessions.put(session, ctx, step)
        fields: dict = dict(extra or {})
        view, truncated, q, proposed, reason = None, False, None, None, ""
        route_name, executor = "default", "default"
        action = label
        try:
            view, truncated = p.render(ctx)
            q = p.question(ctx)
            if policy is None:      # no policy is bound at this phase: the fixed default, and nothing proposed
                raise _NoPolicy
            proposed, reason = policy.propose(ctx)
            if proposed in (q.labels() if q else ()):
                action, route_name, executor = proposed, "policy", "policy"
            else:
                fields["fallback_reason"] = reason
        except CandidateDefect as e:
            self.ledger.decision(session=session, ctx=ref, point=p.name, spec_hash=p.spec_hash, backend="policy",
                                 model=getattr(policy, "ref", ""), view=view, view_chars=len(view or ""),
                                 truncated=truncated, question=q.to_json() if q else None, probs=None,
                                 candidates=q.labels() if q else None, route="error", explored=False,
                                 propensity=1.0, action=None, usage={}, phase=phase, default=label,
                                 executor="none", policy_ref=getattr(policy, "ref", ""),
                                 defect=f"{e}"[:500], **fields)
            raise                       # a defective candidate does not get the default's result recorded as its own
        except _NoPolicy:
            pass
        except Exception as e:  # noqa: BLE001 - whatever breaks, the fixed default is taken and the row says why
            route_name, fields["error"] = "error", f"{type(e).__name__}: {e}"[:500]
        try:
            if isinstance(veto, str):
                action, route_name, executor = veto, "rule", "guard"
            elif callable(veto) and (verdict := veto(action)) is not None:
                fields["override_reason"] = f"guard overruled {action!r}"
                action, route_name, executor = verdict, "rule", "guard"
        except Exception as e:  # noqa: BLE001 - a checker that breaks overrules nothing
            route_name, executor = "error", "default"
            action, fields["error"] = label, f"{type(e).__name__}: {e}"[:500]
        row = dict(session=session, ctx=ref, point=p.name, spec_hash=p.spec_hash, backend="policy",
                   model=getattr(policy, "ref", ""), view=view, view_chars=len(view or ""), truncated=truncated,
                   question=q.to_json() if q else None, candidates=q.labels() if q else None, probs=None,
                   route=route_name, explored=False, propensity=1.0, action=action, usage={}, phase=phase,
                   default=label, proposed=proposed, executor=executor,
                   policy_ref=getattr(policy, "ref", ""), policy_kind=getattr(policy, "kind", ""), **fields)
        did = self.ledger.decision(**row)
        if on_record is not None:
            on_record({"id": did, **row})
        return Decision(did, p.name, proposed, {}, 0.0, route_name, False, 1.0, action,
                        getattr(policy, "ref", ""), route_name == "policy")

    def _default(self, ctx: Any, view: str | None, q: Question | None, label: str | None, answerer: str | None,
                 extra: dict) -> str | None:
        """The phase's default: its answerer's answer, or its fixed label when it has none or the answerer fails
        (an exception, a timeout, an abstention or a tie, an answer that is not a label). The answerer is recorded
        as the answer's lineage (docs/DESIGN.md §8, invariant 6); until v2.0 replaces the ledger's source gate
        (docs/design/V2_TRAINING_PIPELINE.md §2.5, §3.8) its answer is for evaluation only, never a training
        target."""
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

    def outcome(self, decision: str, label: str | list[str] | None, source: str = "env", weight: float = 1.0,
                **extra) -> None:
        """Record what happened after `decision`: its right answer, a list of answers that are all right, or None
        when none was."""
        self.ledger.outcome(decision, label, source, weight, **extra)
