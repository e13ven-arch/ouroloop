"""StopGuard: whether an agent loop should end (PLAN_v0.2 M6). Any harness can use it; it only needs the runtime,
a context for the stop point's view, and optionally a checker.

    per_turn    after a turn that did something, except the last (nothing follows it); default "no" (keep going).
                A confident "yes" ends the session, unless the checker fails.
    on_finish   when the agent wants to finish; default "yes". A failing checker, or with no checker a confident
                "no", sends the agent back to work, at most max_pushbacks times and never on the last turn.

Order: checker -> model -> pushback -> turn cap (per_turn runs the checker only on a confident "yes"). A rule
overrules the model, never the other way round. The model is asked only when the stop point's policy is "auto";
otherwise the context is recorded and no model is called.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

MOMENTS = {"per_turn": "after a turn", "on_finish": "the agent wants to finish"}


@dataclass
class StopCall:
    stop: bool                      # end the session now
    reason: str = ""                # on_finish: what to tell the agent when it is sent back; per_turn: why it ends
    decision: str | None = None     # the stop decision's id; None when the runtime has no stop point
    checked: bool | None = None     # the checker's verdict, when it ran


class StopGuard:
    def __init__(self, rt, session: str, *, checker: Callable[[], tuple[bool, str]] | None = None,
                 max_pushbacks: int = 2, max_turns: int | None = None, point: str = "stop"):
        """checker() -> (passed, what to tell the agent when it failed)."""
        self.rt, self.session, self.point = rt, session, point
        self.checker = checker
        self.max_pushbacks, self.max_turns = max_pushbacks, max_turns
        self.pushbacks = 0

    def __call__(self, phase: str, ctx: dict, turn: int) -> StopCall:
        capped = self.max_turns is not None and turn >= self.max_turns
        if phase == "per_turn":
            if capped:
                return StopCall(True, "max_turns")
            decision, action = self._decide(phase, ctx)
            checked = self.checker()[0] if self.checker and action == "yes" else None
            done = action == "yes" and checked is not False
            return StopCall(done, "stop_point" if done else "", decision, checked)
        checked, feedback = self.checker() if self.checker else (None, "")
        decision, action = self._decide(phase, ctx)
        if checked is False:
            reason = feedback
        elif checked is None and action == "no":
            reason = "The task does not look complete yet."    # a passing checker is never overruled
        else:
            return StopCall(True, "", decision, checked)
        if self.pushbacks >= self.max_pushbacks or capped:
            return StopCall(True, "", decision, checked)
        self.pushbacks += 1
        return StopCall(False, reason, decision, checked)

    def _decide(self, phase: str, ctx: dict) -> tuple[str | None, str | None]:
        """-> (decision id, the action taken on the model's answer, or None when it did not act)."""
        p = self.rt.points.get(self.point)
        if p is None:
            return None, None
        ctx = {**ctx, "moment": MOMENTS[phase]}
        if p.policy.mode != "auto":
            return self.rt.record(p, ctx, session=self.session, phase=phase), None
        d = self.rt.ask(p, ctx, session=self.session, phase=phase)
        return d.id, d.action if d.route == "auto" else None
