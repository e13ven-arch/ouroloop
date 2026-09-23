"""Built-in decision points of the harness (PLAN §2.5): retry, compact, tool_gate, stop and route, plus the
deterministic deny list that runs before tool_gate."""
from __future__ import annotations

import re

from ..decision import Choice, DecisionPoint, Noul
from ..policy import Policy


def retry_view(ctx: dict, params: dict) -> str:
    lines = (ctx.get("stderr") or "").splitlines()
    if params.get("tail_lines"):
        lines = lines[-int(params["tail_lines"]):]
    return f"$ {ctx['command']}\n" + "\n".join(lines)


def retry_point(**kw) -> DecisionPoint:
    kw.setdefault("max_view_chars", 2000)
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
    kw.setdefault("policy", Policy(mode="shadow", fallback="default", default="yes"))
    return Noul("compact", "This tool output will be needed again later in the task",
                {"true": "it holds facts the rest of the task still depends on, such as a file about to be edited "
                         "or an error that is still being fixed",
                 "false": "a one-off check, a listing, or output that later steps have superseded"},
                view=compact_view, **kw)


def tool_gate_view(ctx: dict, params: dict) -> str:
    return f"task: {ctx['task']}\nworking directory: {ctx['workdir']}\ntool: {ctx['tool']}\n{ctx['detail']}"


def tool_gate_point(**kw) -> DecisionPoint:
    kw.setdefault("policy", Policy(mode="shadow", fallback="human", conservative="no"))
    return Noul("tool_gate", "This action is safe to run without asking a person",
                {"true": "read-only, or a reversible change inside the working directory that the task calls for",
                 "false": "destructive, irreversible, outside the working directory, exposes credentials, or "
                          "unrelated to the task"},
                view=tool_gate_view, **kw)


def stop_view(ctx: dict, params: dict) -> str:
    # Most telling first: a long view keeps its head, so the action log goes last.
    return (f"task: {ctx['task']}\nverification: {ctx['verification']}\nfinal message:\n{ctx['message']}\n"
            f"files changed: {ctx['files'] or 'none'}\nturns so far: {ctx['turns']}\n"
            f"recent actions:\n{ctx['actions'] or '(none)'}")


def stop_point(**kw) -> DecisionPoint:
    kw.setdefault("policy", Policy(mode="shadow", fallback="default", default="yes"))
    kw.setdefault("max_view_chars", 3000)
    return Noul("stop", "The task is complete",
                {"true": "everything the task asked for has been done and checked",
                 "false": "part of the task is missing, unverified, or still failing"},
                view=stop_view, keep="head", **kw)


def route_view(ctx: dict, params: dict) -> str:
    return (f"task: {ctx['task']}\nturn: {ctx['turn']}\nlast step: {ctx['last'] or '(first step)'}\n"
            f"errors so far: {ctx['errors']}")


def route_point(**kw) -> DecisionPoint:
    kw.setdefault("policy", Policy(mode="shadow", fallback="default", default="large"))
    return Choice("route", "Which model tier is enough for the next step?",
                  {"small": "a routine step: reading files, running commands, a small edit, or wrapping up",
                   "large": "a hard step: debugging a failure, designing a change, or editing several files"},
                  view=route_view, **kw)


BUILTIN = {"retry": retry_point, "compact": compact_point, "tool_gate": tool_gate_point, "stop": stop_point,
           "route": route_point}


def builtin_points() -> list[DecisionPoint]:
    return [make() for make in BUILTIN.values()]


# Commands that are never run, whatever any model says. Rules first, model second.
DENY = [re.compile(p) for p in (
    r"\brm\s+-\w*r\w*\s+(/|~|\$HOME)(/?\*)?(\s|;|&|$)",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b[^\n]*\bof=/dev/",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\bgit\s+push\b[^\n]*\s(--force|-f)(\s|$)",
    r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(ba|z)?sh\b",
    r"\bchmod\s+-R\s+777\s+/",
)]


def denied(command: str) -> str | None:
    """The deny-list pattern this command matches, or None."""
    for rx in DENY:
        if rx.search(command):
            return rx.pattern
    return None
