"""The runtime that ties decision points, backends, default answerers and the ledger together."""
from __future__ import annotations

import importlib
import json
import random
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from . import policy
from .decision import Decision, DecisionPoint, Spec
from .ledger import CalibrationStore, Ledger, SessionStore, SpecStore, write_json
from .policy import Policy, Route, route
from .release import Accepted, Release, Revoked, harness_hash, spec_hash
from .types import Backend, Question, Request, temper


def import_object(ref: str):
    """"package.module:name" -> the object."""
    module, _, attr = ref.partition(":")
    return getattr(importlib.import_module(module), attr)


@dataclass(frozen=True)
class Pin:
    """What a session runs with, fixed when it opens (PLAN_v0.2 M8): its release, the champion's registry version
    and backend, the points the champion serves, and the calibrations as they were then."""
    release: Release
    version: str | None
    backend: Backend | None
    points: frozenset
    calibration: dict
    error: str = ""         # why the champion's backend could not be built


class Runtime:
    def __init__(self, root: str | Path, backends: dict[str, Backend], default_backend: str,
                 point_backends: dict[str, str] | None = None, seed: int | None = None, serving=None):
        """`serving`: how the trained models on the registry's cards are asked (serving.Serving; default: in-process,
        with no store)."""
        from .serving import Serving
        self.root = Path(root)
        self.ledger = Ledger(self.root / "ledger")
        self.sessions = SessionStore(self.root / "sessions")
        self.specs = SpecStore(self.root / "harness" / "specs.json")
        self.calibration = CalibrationStore(self.root / "harness" / "calibration.json")
        self.revoked = Revoked(self.root / "harness" / "revoked.json")
        self.accepted = Accepted(self.root)
        self.backends = backends
        self.serving = serving or Serving()
        self.default_backend = default_backend
        self.point_backends = dict(point_backends or {})
        self.points: dict[str, DecisionPoint] = {}
        self.rng = random.Random(seed)
        self._pins: dict[str, Pin] = {}
        self._built: dict[str, tuple[Backend | None, str]] = {}     # champion version -> (backend, error)
        self._tally = None      # the post-act monitor's counts (evolve.monitor.Tally), built at its first check

    def register(self, point: DecisionPoint) -> DecisionPoint:
        """Add a decision point. A promoted spec (harness/specs.json) replaces the one in code. Each phase's default
        must be one of the point's labels, and its default answerer must resolve. In v0.2 the harness's built-in
        points other than stop may not act (policy.may_act)."""
        if point.policy.act and not policy.may_act(point.name):
            raise ValueError(f"point {point.name!r} has act on: of the built-in points only "
                             f"{sorted(policy.ACTING)} may act in v0.2")
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

    # ------------------------------------------------------------------ releases
    def champion(self, version: str | None = None) -> tuple[dict, Backend | None, str]:
        """The promoted champion as the registry has it now, or a given `version` of it: (its card, empty before the
        first promotion; its backend; why the backend could not be built). A version's backend is built once, and
        only for a version a gate record accepted: any other never answers, so its points take their defaults."""
        from .evolve.registry import Registry
        registry = Registry(self.root)
        card = registry.card(version) if version else registry.champion() or {}
        if not card:
            return {}, None, ""
        version = card["version"]
        if not self.accepted.promoted(card):
            return card, None, f"{version} was not promoted through the frozen gate"
        if version not in self._built:
            try:
                self._built[version] = (self.serving.backend(card["backend"]), "")
            except Exception as e:  # noqa: BLE001 - a champion that cannot load never answers, so never acts
                self._built[version] = (None, f"{type(e).__name__}: {e}"[:300])
        return card, *self._built[version]

    def drop(self, version: str, spec: dict | None) -> None:
        """Let go of a version that is not the champion (a revoked one, or one that lost a promotion): serving drops
        its model. Sessions pinned to a revoked one keep asking it, and take their defaults."""
        self._built.pop(version, None)
        try:
            self.serving.drop(spec)
        except Exception:  # noqa: BLE001 - a model server that cannot be told still never lets the revoked one act
            pass

    def current(self, harness: dict | None = None) -> Pin:
        """What a session opened now runs with: the champion in models/champion.json and the calibrations in
        harness/calibration.json, both read again."""
        card, backend, error = self.champion()
        return Pin(Release(card.get("model_id"), spec_hash(self.points), harness_hash(harness)), card.get("version"),
                   backend, frozenset(card.get("points", ())), self.calibration.entries(), error)

    def session(self, session: str, meta: dict | None = None, harness: dict | None = None) -> Release:
        """Open a session: fix the release it runs with and record it, with what the session was run with (task,
        held-out, agent models), before any of its decisions. Every later ask of the session uses that model and
        those calibrations: a promotion reaches only sessions opened after it. A session never opened is fixed at its
        first ask, and one opened again keeps what it was fixed at. `harness` describes the prompt, tools and StopGuard
        settings of an ouroloop harness. Before a new session is fixed, the post-act monitor checks the champion it
        would run (evolve.monitor.watch): one revoked then leaves the session its defaults."""
        known = session in self._pins or self.sessions.path(session, ".release.json").exists()
        pin = self._pin(session) if known else self._open(session, harness)
        self.ledger.append({**(meta or {}), "type": "session", "session": session, "release": pin.release.to_json(),
                            "champion": pin.version})
        return pin.release

    def _open(self, session: str, harness: dict | None = None) -> Pin:
        """Fix a new session at the current release, once the post-act monitor has checked the champion (it reads
        only the ledger events appended since its last check)."""
        from .evolve import monitor
        self._tally = self._tally or monitor.Tally(self.ledger)
        monitor.watch(self, self._tally)
        return self._keep(session, self.current(harness))

    def _keep(self, session: str, pin: Pin) -> Pin:
        """Fix `pin` for the session, also on disk beside its contexts: a process that resumes the session (serve
        restarted, the next hook call) runs the same model, never the one promoted since."""
        write_json(self.sessions.path(session, ".release.json"), {"release": pin.release.to_json(),
                                                                    "champion": pin.version})
        self._pins[session] = pin
        return pin

    def _pin(self, session: str) -> Pin:
        if session not in self._pins:
            path = self.sessions.path(session, ".release.json")
            if not path.exists():
                return self._open(session)
            saved = json.loads(path.read_text(encoding="utf-8"))     # before any promotion: the base, still
            card, backend, error = self.champion(saved["champion"]) if saved["champion"] else ({}, None, "")
            self._pins[session] = Pin(Release(**saved["release"]), saved["champion"], backend,
                                      frozenset(card.get("points", ())), self.calibration.entries(), error)
        return self._pins[session]

    def backend_name(self, point: str, pin: Pin | None = None) -> str:
        points = pin.points if pin else frozenset(self.champion()[0].get("points", ()))
        return "champion" if point in points else self.point_backends.get(point, self.default_backend)

    def backend_for(self, point: str, pin: Pin | None = None) -> Backend:
        """The backend that answers `point`: on "champion", the promoted champion (the session's, with `pin`; the
        registry's now, without), or the base before the first promotion; else the configured one."""
        pin = pin or self.current()
        name = self.backend_name(point, pin)
        if name == "champion" and pin.version:
            if pin.backend is None:
                raise RuntimeError(f"the champion {pin.version} has no backend: {pin.error}")
            return pin.backend
        return self.backends[name]

    # ------------------------------------------------------------------ calibration
    def calibration_for(self, point: str | DecisionPoint, model: str, entries: dict | None = None) -> dict:
        """The calibration of this model (as its backend reports it) on the point's current spec, if any: from
        `entries` (a session's snapshot), or as stored now."""
        p = self.points[point] if isinstance(point, str) else point
        return CalibrationStore.lookup(self.calibration.entries() if entries is None else entries, model,
                                       p.spec_hash, p.name)

    def temperature(self, point: str | DecisionPoint, model: str, entries: dict | None = None) -> float:
        return float(self.calibration_for(point, model, entries).get("temperature", 1.0))

    def threshold(self, point: str | DecisionPoint, model: str, phase: str | None,
                  entries: dict | None = None) -> float | None:
        """The lowest confidence at which the point may act on this model's answers in this phase (PLAN_v0.2 §0 rule
        3): only from a calibration that carries the hash of a gate record that accepted the promotion, with the
        temperature and thresholds that record accepted for this model, spec and point, and only for a model that
        names itself (an empty id would match every backend that reports none)."""
        p = self.points[point] if isinstance(point, str) else point
        cal = self.calibration_for(p, model, entries)
        gated = self.accepted.calibration(cal.get("gate"), model, p.spec_hash, p.name)
        as_gated = gated is not None and gated == {"temperature": cal.get("temperature"),
                                                   "thresholds": cal.get("thresholds")}
        return cal["thresholds"].get(phase) if as_gated and phase and model else None

    def set_calibration(self, point: str, model: str, temperature: float, thresholds: dict | None = None,
                        gate: str | None = None, **extra) -> None:
        """Store a calibration of this model on the point's current spec. Its thresholds let the point act only with
        `gate`, the hash of the gate record that promoted it (M8)."""
        self.calibration.set(model, self.points[point].spec_hash, point,
                             {"temperature": temperature, "thresholds": dict(thresholds or {}), "gate": gate, **extra})

    # ------------------------------------------------------------------ decisions
    def ask(self, point: str | DecisionPoint, ctx: Any, *, session: str = "s_default", step: int | None = None,
            phase: str | None = None, veto: str | Callable[[str | None], str | None] | None = None) -> Decision:
        """Ask the point's backend and apply the execution rule (PLAN_v0.2 §0). A phase whose default answerer is a
        rule has it answer first: its answer is the routing default the model's answer is compared with, so a model
        that agrees with the rule never acts, and it is the action when the model does not act. Otherwise, when the
        model does not act, the phase's LLM answerer answers, or its fixed label is taken; the routing default is
        the fixed label (an LLM is asked only when the model does not act). `veto` is a rule's verdict: a label, or a
        function of the action about to be taken that returns a label to overrule it. The rule's label is the action
        (route "rule") and the model's answer is only recorded. Whatever breaks on the way (view, question, backend,
        calibration, routing, the rule) is recorded on route "error" with the raw context, and the default is
        taken. The session's release decides which model answers and which calibration applies; once that model is
        revoked or rolled back, the session takes its defaults."""
        return self._decide(point, ctx, session, step, phase, veto, asking=True)

    def record(self, point: str | DecisionPoint, ctx: Any, *, session: str = "s_default", step: int | None = None,
               phase: str | None = None, veto: str | Callable[[str | None], str | None] | None = None) -> Decision:
        """Log a decision point's context without asking the decision model (shadow collection): the action is the
        phase's default, as its answerer gives it (see `ask`), or a rule's `veto` (route "rule"). The model's
        probabilities can be computed later by offline replay; only the context and the outcome are needed."""
        return self._decide(point, ctx, session, step, phase, veto, asking=False)

    def _decide(self, point, ctx, session, step, phase, veto, asking: bool) -> Decision:
        p = self.points[point] if isinstance(point, str) else point
        phase, label, answerer = p.default_for(phase)
        rule = answerer if answerer and answerer.startswith("rule:") else None
        ref = self.sessions.put(session, ctx, step)
        view, truncated, q, ans, probs, extra = None, False, None, None, None, {}
        backend, r = ("", None) if asking else ("none", Route("shadow", False, 1.0))
        default, ruled = label, False       # the routing default; whether the rule has answered it
        try:
            view, truncated = p.render(ctx)
            q = p.question(ctx)
            if asking:
                pin = self._pin(session)
                q = q.presented(self.rng)   # candidate order is shuffled, and recorded as shown
                if rule:
                    default, ruled = self._default(ctx, view, q, label, rule, extra), True
                b = self.backend_for(p.name, pin)
                backend = b.name
                ans = b.decide([Request(view, q)])[0]
                if not ans.over(q.labels()):     # a backend may build its Answer itself: an unknown label, raw scores
                    raise ValueError(f"answered {ans.probs}, not a distribution over {q.labels()}")
                if pin.version and self.backend_name(p.name, pin) == "champion" and ans.model != pin.release.model:
                    raise ValueError(f"answered as {ans.model!r}; the session runs {pin.release.model!r}")
                ans = replace(ans, probs=temper(ans.probs, self.temperature(p, ans.model, pin.calibration)))
                probs = [ans.probs[lab] for lab in q.labels()]
                revoked = self.revoked.hit(pin.version, pin.release.model, ans.model)
                if revoked:
                    extra["revoked"] = revoked
                threshold = None if revoked else self.threshold(p, ans.model, phase, pin.calibration)
                r = route(p.policy if policy.may_act(p.name) else Policy(), ans, default, threshold, self.rng)
        except Exception as e:  # noqa: BLE001 - whatever breaks, the default is taken and the row says why
            r, extra["error"] = Route("error", False, 1.0), f"{type(e).__name__}: {e}"[:500]
        if ans is not None and ans.abstained:
            extra["abstained"] = True
        try:
            if isinstance(veto, str):
                action = veto
            elif r.route == "act":
                action = ans.label
            elif rule:
                if not ruled:
                    default = self._default(ctx, view, q, label, rule, extra)
                action = default
            else:
                action = self._default(ctx, view, q, label, answerer, extra)
            if callable(veto):
                veto = veto(action)
        except Exception as e:  # noqa: BLE001 - a rule that breaks overrules nothing: the fixed default is taken
            r, veto, action = Route("error", False, 1.0), None, label
            extra["error"] = f"{type(e).__name__}: {e}"[:500]
        if veto is not None:
            r, action = Route("rule", False, 1.0), veto
        model = ans.model if ans else ""
        did = self.ledger.decision(
            session=session, ctx=ref, point=p.name, spec_hash=p.spec_hash, backend=backend, model=model, view=view,
            view_chars=len(view or ""), truncated=truncated, question=q.to_json() if q else None,
            candidates=q.labels() if q else None, probs=probs, route=r.route, explored=r.explored,
            propensity=r.propensity, action=action, usage=ans.usage if ans else {}, phase=phase, default=default,
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

    def outcome(self, decision: str, label: str | list[str] | None, source: str = "env", weight: float = 1.0,
                **extra) -> None:
        """Record what happened after `decision`: its right answer, a list of answers that are all right, or None
        when none was."""
        self.ledger.outcome(decision, label, source, weight, **extra)
