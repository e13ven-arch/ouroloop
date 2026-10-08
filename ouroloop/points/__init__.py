"""Built-in decision points of the v1 harness (agent/loop.py): retry, compact, tool_gate, stop and route; they go with
that loop in P1 step 5. Each declares the phase it is asked in and its default there: the action taken while the point
is shadow, and whenever the model does not act. The business stop point is the training stack's
(ouroloop/train/views.py), tool_gate the Claude Code adapter's (ouroloop/train/claude_code.py); the deterministic deny
list that runs before every tool call is the agent host's (agent/deny.py)."""
from __future__ import annotations

from ..train.claude_code import tool_gate_point
from ..train.decision import Choice, DecisionPoint, Noul


def retry_view(ctx: dict, params: dict) -> str:
    lines = (ctx.get("stderr") or "").splitlines()
    if params.get("tail_lines"):
        lines = lines[-int(params["tail_lines"]):]
    return f"$ {ctx['command']}\n" + "\n".join(lines)


def retry_point(**kw) -> DecisionPoint:
    kw.setdefault("max_view_chars", 2000)
    kw.setdefault("phases", {"after_failure": "no"})       # the agent decides whether to run it again
    return Noul("retry", "Rerunning this failed command unchanged will succeed",
                {"true": "a transient failure: network error, connection reset, timeout, lock held by another "
                         "process, rate limit, service unavailable",
                 "false": "needs a change to the code, the arguments or the environment"},
                view=retry_view, **kw)


def compact_view(ctx: dict, params: dict) -> str:
    head, tail = int(params.get("head_chars", 600)), int(params.get("tail_chars", 600))
    out = ctx["output"]
    excerpt = out if len(out) <= head + tail else f"{out[:head]}\n[...]\n{out[-tail:]}"
    return f"task: {ctx['task']}\ntool: {ctx['tool']} {ctx['target']}\nturns ago: {ctx['age']}\noutput:\n{excerpt}"


def compact_point(**kw) -> DecisionPoint:
    kw.setdefault("phases", {"context_full": "yes"})       # keep the output
    return Noul("compact", "This tool output will be needed again later in the task",
                {"true": "it holds facts the rest of the task still depends on, such as a file about to be edited "
                         "or an error that is still being fixed",
                 "false": "a one-off check, a listing, or output that later steps have superseded"},
                view=compact_view, **kw)


def stop_view(ctx: dict, params: dict) -> str:
    # Most telling first: a long view keeps its head, so the action log goes last. No checker's verdict: where there
    # is one it decides, so it would be the label written into the view. Contexts before view 2 were all on_finish.
    return (f"task: {ctx['task']}\nmoment: {ctx.get('moment', 'the agent wants to finish')}\n"
            f"last message:\n{ctx['message']}\nfiles changed: {ctx['files'] or 'none'}\n"
            f"turns so far: {ctx['turns']}\nrecent actions:\n{ctx['actions'] or '(none)'}")


def stop_point(**kw) -> DecisionPoint:
    kw.setdefault("max_view_chars", 3000)
    kw.setdefault("phases", {"per_turn": "no", "on_finish": "yes"})    # after a turn / when the agent wants to finish
    kw.setdefault("view_version", "2")
    return Noul("stop", "The task is complete",
                {"true": "everything the task asked for has been done and checked",
                 "false": "part of the task is missing, unverified, or still failing"},
                view=stop_view, keep="head", **kw)


def route_view(ctx: dict, params: dict) -> str:
    return (f"task: {ctx['task']}\nturn: {ctx['turn']}\nlast step: {ctx['last'] or '(first step)'}\n"
            f"errors so far: {ctx['errors']}")


def route_point(**kw) -> DecisionPoint:
    kw.setdefault("phases", {"before_step": "large"})
    return Choice("route", "Which model tier is enough for the next step?",
                  {"small": "a routine step: reading files, running commands, a small edit, or wrapping up",
                   "large": "a hard step: debugging a failure, designing a change, or editing several files"},
                  view=route_view, **kw)


BUILTIN = {"retry": retry_point, "compact": compact_point, "tool_gate": tool_gate_point, "stop": stop_point,
           "route": route_point}


def builtin_points() -> list[DecisionPoint]:
    return [make() for make in BUILTIN.values()]


# The deny list is the agent host's (agent/deny.py); these names stay importable from here for older callers.
from ..agent.deny import DENY, denied  # noqa: E402,F401
