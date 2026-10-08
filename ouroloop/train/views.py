"""The business stop point and its view (docs/modules/01_POLICY_RUNTIME.md §3, schema stop.business.v1): what a
completion decision of a business task may see. policies.py builds it for a bound worker.stop policy, and the task
profile's datasets render their examples through it (dataset.py)."""
from __future__ import annotations

import json

from .decision import DecisionPoint, Noul

# The business view (docs/modules/01_POLICY_RUNTIME.md §3, schema stop.business.v1). The v1 file view
# (points.stop_view) was written for a coding task in a working directory: it carries the first 300 characters of the task and a short
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
