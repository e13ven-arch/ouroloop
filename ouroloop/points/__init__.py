"""Built-in decision points of the harness: retry, compact, tool_gate, stop and route. Each declares the phase it is
asked in and its default there: the action taken while the point is shadow, and whenever the model does not act. The
deterministic deny list that runs before every tool call is the agent host's (agent/deny.py)."""
from __future__ import annotations

import json

from ..decision import Choice, DecisionPoint, Noul


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


def tool_gate_view(ctx: dict, params: dict) -> str:
    return f"task: {ctx['task']}\nworking directory: {ctx['workdir']}\ntool: {ctx['tool']}\n{ctx['detail']}"


def tool_gate_point(**kw) -> DecisionPoint:
    kw.setdefault("phases", {"before_call": "no"})         # not cleared: the host asks a person
    return Noul("tool_gate", "This action is safe to run without asking a person",
                {"true": "read-only, or a reversible change inside the working directory that the task calls for",
                 "false": "destructive, irreversible, outside the working directory, exposes credentials, or "
                          "unrelated to the task"},
                view=tool_gate_view, **kw)


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


# The business view (docs/modules/01_POLICY_RUNTIME.md §3, schema stop.business.v1). The file view above was
# written for a coding task in a working directory: it carries the first 300 characters of the task and a short
# action log, which is not enough to judge whether a business task's requirements were met, because a Marketing
# task's requirements run past that and are verified against what the tools actually returned.
#
# Everything here was visible at the decision. No rubric, no assertion, no world snapshot: the order is fixed, the
# budget is deterministic, and whatever is cut is named in the text rather than dropped quietly.
BUSINESS_VIEW_KEYS = {"max_task_chars", "max_events", "max_result_chars", "keep", "requires"}
BUSINESS_VIEW_LIMITS = {"max_task_chars": 6000, "max_events": 40, "max_result_chars": 600}


def _keep(text: str, limit: int, how: str) -> tuple[str, int]:
    """`text` cut to `limit`, and how many characters were dropped. "head_tail" keeps both ends, because a task's
    requirements are at the top and its last constraint is often the final sentence."""
    if len(text) <= limit:
        return text, 0
    dropped = len(text) - limit
    if how == "tail":
        return text[-limit:], dropped
    if how == "head_tail":
        head = limit // 2
        return f"{text[:head]}\n[... {dropped} characters not shown ...]\n{text[-(limit - head):]}", dropped
    return text[:limit], dropped


def business_stop_view(ctx: dict, params: dict) -> str:
    """The rendered input of a business completion decision, from an Observation (contracts.Observation)."""
    unknown = sorted(set(params) - BUSINESS_VIEW_KEYS)
    if unknown:
        raise ValueError(f"the business stop view does not take {unknown}; it takes {sorted(BUSINESS_VIEW_KEYS)}")
    for field in params.get("requires") or ():
        # A view that needs a field the observation never stored is not rendered from whatever is at hand: the
        # row has to be collected again, and reading the field off the final state would be reading the future
        # (docs/modules/02_EVIDENCE_TRAINING.md §4.4).
        if ctx.get(field) in (None, "", [], {}, ()):
            raise KeyError(field)
    limits = {k: min(int(params.get(k, v)), v) for k, v in BUSINESS_VIEW_LIMITS.items()}
    how = params.get("keep", "head_tail")
    if how not in ("head", "tail", "head_tail"):
        raise ValueError(f"keep {how!r}: head, tail or head_tail")
    task, task_dropped = _keep(str(ctx.get("task_text", "")), limits["max_task_chars"], how)
    events = list(ctx.get("tool_events") or [])
    shown = events[-limits["max_events"]:]
    lines = []
    for e in shown:
        args = json.dumps(e.get("args") or {}, ensure_ascii=False, sort_keys=True)
        result, _ = _keep(str(e.get("error") or e.get("result_excerpt") or ""), limits["max_result_chars"], "head")
        lines.append(f"  step {e.get('step')}: {e.get('name')}({args[:400]})"
                     f"{' ERROR' if e.get('error') else ''} -> {' '.join(result.split())}")
    truncation = dict(ctx.get("truncation") or {})
    if task_dropped:
        truncation["task_text"] = {"dropped_chars": task_dropped}
    if len(events) > len(shown):
        truncation["tool_events"] = {"dropped": len(events) - len(shown), "kept": len(shown)}
    budget = ctx.get("budget_remaining") or {}
    return ("task requirements:\n" + task
            + f"\nmoment: {ctx.get('moment') or ctx.get('phase') or 'the agent wants to finish'}"
            + f"\nstep: {ctx.get('step', 0)}"
            + f"\nlast message:\n{ctx.get('final_message', '')}"
            + f"\ntool calls and what they returned ({len(shown)} of {len(events)}):\n"
            + ("\n".join(lines) or "  (none)")
            + f"\nbudget remaining: {json.dumps(budget, sort_keys=True) if budget else 'unrecorded'}"
            + f"\nnot shown: {json.dumps(truncation, sort_keys=True) if truncation else 'nothing'}")


def business_stop_point(**kw) -> DecisionPoint:
    """The stop point over the business view. Same name, same phases and same actions as the file one: what
    changes is what it is allowed to see, so a candidate that changes the view is a new policy, not a new point."""
    kw.setdefault("max_view_chars", 12000)
    kw.setdefault("phases", {"per_turn": "no", "on_finish": "yes"})
    kw.setdefault("view_version", "business.1")
    return Noul("stop", "The task is complete",
                {"true": "everything the task asked for has been done, and the tool results show it",
                 "false": "part of what the task asked for is missing, unverified, or still failing"},
                view=business_stop_view, keep="head", **kw)


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
