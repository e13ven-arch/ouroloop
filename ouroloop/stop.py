"""StopGuard: whether an agent loop should end (PLAN_v0.2 M6). Any harness can use it; it only needs the runtime,
a context for the stop point's view, and optionally a checker.

    per_turn    after a turn that did something, except the last (nothing follows it); default "no" (keep going).
                A confident "yes" ends the session, unless the checker fails.
    on_finish   when the agent wants to finish; default "yes". A failing checker, or with no checker a confident
                "no", sends the agent back to work, at most max_pushbacks times and never on the last turn.

Order: model -> checker -> pushback -> turn cap (per_turn runs the checker only on a "yes"). A rule overrules the
model, never the other way round, and its verdict is recorded as the decision's veto (route "rule"); a checker that
breaks is recorded on route "error" and the phase's fixed default is taken. The model is asked only when the stop
point's act is on; otherwise the context is recorded, the decision model is not called, and the phase's default (its
answerer's answer, or its fixed label) is the action.
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
        if phase == "per_turn" and capped:
            return StopCall(True, "max_turns")
        seen: dict = {}

        def check(action: str | None) -> str | None:
            """The checker as the decision's veto, so a checker that breaks is recorded on route "error" and the
            default is taken. per_turn: a failing checker overrules a "yes". on_finish: the checker decides."""
            if self.checker is None or (phase == "per_turn" and action != "yes"):
                return None
            seen["passed"], seen["feedback"] = self.checker()
            if phase == "per_turn":
                return None if seen["passed"] else "no"
            return "yes" if seen["passed"] else "no"

        decision, action = self._decide(phase, ctx, check)
        checked = seen.get("passed")
        if phase == "per_turn":
            done = action == "yes"
            return StopCall(done, "stop_point" if done else "", decision, checked)
        if checked is False:
            reason = seen["feedback"]
        elif checked is None and action == "no":
            reason = "The task does not look complete yet."    # a passing checker is never overruled
        else:
            return StopCall(True, "", decision, checked)
        if self.pushbacks >= self.max_pushbacks or capped:
            return StopCall(True, "", decision, checked)
        self.pushbacks += 1
        return StopCall(False, reason, decision, checked)

    def _decide(self, phase: str, ctx: dict, veto) -> tuple[str | None, str | None]:
        """-> (decision id, the action: the model's label when it acted, else the rule's or the phase's default)."""
        p = self.rt.points.get(self.point)
        if p is None:           # no stop point: nothing is recorded, and the checker still decides
            return None, veto(None)
        ctx = {**ctx, "moment": MOMENTS[phase]}
        ask = self.rt.ask if p.policy.act else self.rt.record
        d = ask(p, ctx, session=self.session, phase=phase, veto=veto)
        return d.id, d.action
