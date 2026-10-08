"""What a host writes for one stop decision (docs/modules/01_POLICY_RUNTIME.md §3, schema stop.business.v1;
docs/modules/00_CONTRACTS.md §2): the context the business stop view renders from, built from what was visible at the
decision, and — for a bound worker.stop policy — the decision's observation and trace.

The v1 loop (agent/loop.py, on tag archive-2026-10-08) was the only host that asked a stop point; these were its
`_stop_ctx` and `_trace`, moved here first (docs/design/SLIMMING_INVENTORY.md §0.1 A) because nothing else builds an
Observation or a DecisionTrace. A host that asks the stop point again (docs/design/V2_TRAINING_PIPELINE.md §3.8, the
stop bridge) builds its context and trace with them. Nothing here reads a scorer, a reference answer or a later turn.
"""
from __future__ import annotations

import json

STOP_VIEW = "stop.business.v1"
EVENT_RESULT_CHARS = 2000             # per tool result kept in the observation, before the view cuts


def tool_event(call_id: str, name: str, args: dict, output: str, is_error: bool, step: int, *,
               max_chars: int = EVENT_RESULT_CHARS) -> dict:
    """One tool call as the stop context keeps it: the result (or the error) up to `max_chars`, and whether it was
    cut."""
    return {"call_id": call_id, "name": name, "args": args,
            "result_excerpt": "" if is_error else output[:max_chars], "error": output[:max_chars] if is_error else "",
            "truncated": len(output) > max_chars, "step": step}


def stop_context(task_text: str, final_message: str, *, phase: str, step: int, tool_events, budget_remaining: dict
                 ) -> dict:
    """What the stop decision may see at `step`: the whole request, the last message, every tool call so far with
    what it returned, the budget left, and which results were cut."""
    events = list(tool_events)
    cut = [e["call_id"] for e in events if e.get("truncated")]
    return {"task_text": task_text, "final_message": final_message[:4000], "phase": phase, "step": step,
            "tool_events": events, "budget_remaining": dict(budget_remaining),
            "truncation": {"tool_results_cut": cut} if cut else {}}


def record_trace(objects, bound, guard, call, ctx: dict, phase: str, decision: dict, *,
                 input_schema: str = STOP_VIEW) -> tuple[str, str]:
    """One observation and one trace for a stop decision of a bound release (`bound`, policies.BoundRuntime), and the
    release's activity counters. -> (observation ref, trace ref).

    The proposed and the executed action come from two different places — the policy's answer and what the guard
    did with it — which is the only way a later round can tell "the policy was overruled" from "the policy agreed"
    (docs/modules/00_CONTRACTS.md §2). `decision` is the row as the runtime wrote it (StopGuard's on_record).
    """
    from ..evolve import contracts as ct
    observation = ct.Observation(
        task_text=ctx.get("task_text", ""), tool_events=tuple(json.dumps(e, sort_keys=True, default=str)
                                                              for e in ctx.get("tool_events") or ()),
        phase=phase, step=int(ctx.get("step") or 0), final_message=ctx.get("final_message", ""),
        budget_remaining=dict(ctx.get("budget_remaining") or {}), truncation=dict(ctx.get("truncation") or {}))
    obs_ref = objects.put(observation)
    trace = ct.DecisionTrace(
        decision_id=call.decision or "", observation_ref=obs_ref,
        policy_ref=getattr(bound.stop, "ref", ""), input_schema=input_schema,
        rendered_input_ref=objects.put_json("change", {"view": decision.get("view") or ""}),
        proposed_action=decision.get("proposed"), executed_action=decision.get("action"),
        executor=decision.get("executor") or "default",
        fallback_reason=decision.get("fallback_reason"), override_reason=decision.get("override_reason"))
    trace_ref = objects.put(trace)
    if decision.get("policy_ref"):          # a policy was bound at this phase: a call, not only an observation
        target = "worker.stop"
        bound.activity.call(target)
        bound.activity.propose(target, decision.get("proposed"))
        bound.activity.execute(target, decision.get("action"))
        if decision.get("fallback_reason"):
            bound.activity.fallback(target, str(decision["fallback_reason"])[:80])
        if decision.get("override_reason") or any(o["decision"] == call.decision for o in guard.overruled):
            bound.activity.override(target, str(decision.get("override_reason") or "guard limit"))
    return obs_ref, trace_ref
