"""V2b, hook replay (docs/design/IMPROVER.md §8; DESIGN §6.6): a hook is checked on what already happened.

The six sites' events are rebuilt from a train attempt's transcript — before_run (the task, the system), before_model
(each act call's messages and tools), before_tool and after_tool (each tool call, and its result as the next call
carried it), after_turn (a turn's results), on_finish (a finish, as the seed gives it) — and the candidate's
hook at its declared site is called on each through the worker's `hook` op, its world a fresh episode behind a
read-only broker (a hook looks, it does not act). An answer other than none is a trigger. The rung passes when the hook
triggers on at least `replay_min_triggers` of the failing train tasks it claims; its trigger rate on passing train
attempts is reported beside it (a hook that fires everywhere is a hook that changes everything)."""
from __future__ import annotations

import json

from ...harness.contract import SITES, HookAnswerError, action_of
from ...runtime.broker import Broker
from ...runtime.worker import PackageCallError, smoke_view
from ..components import components
from .unit import worker_files

FIRST_WINS = ("on_finish", "before_tool")   # the first hook that pushes back or blocks ends the site's dispatch


TURN_RESULT_CHARS = 4000               # of a tool's output in an after_turn event (the stirrup seed's)
FINISH = "finish"


def _mark(e: dict) -> str | None:
    """A mark line's kind (the host keeps a hook that acted as a `hook` line)."""
    if e.get("kind") == "hook":
        return "hook"
    return e.get("mark") if e.get("kind") == "mark" else None


def site_events(trace, session: str) -> dict[str, list[dict]]:
    """{site: [event]} of one train session, in transcript order, shaped as the stirrup seed passes them to hooks:
    on_finish has reason "finish" (a finish call) or "implicit" (text-only replies the seed turned into a finish),
    final_text the finish's reason, pushbacks the pushbacks before it; finish is no tool call to before_tool,
    after_tool or after_turn; a call stopped before it ran has no after_tool and is not in its turn's results; each
    event carries the hooks that acted on it in the recorded attempt (`acted`, for the manifest order)."""
    out: dict[str, list[dict]] = {s: [] for s in SITES}
    items = trace.events(session)
    events = [item["event"] for item in items]
    task = next((e.get("text") or "" for e in events if e.get("kind") == "task"), "")
    specs, tools_at = [], []
    for e in events:
        if e.get("kind") == "tool_specs":
            specs = list(e.get("specs") or ())
        if e.get("kind") == "gateway_call":
            tools_at.append(specs)
    marks = [(item["line"], _mark(item["event"]), item["event"]) for item in items if _mark(item["event"])]
    skipped = {str(e.get("call_id")) for _, m, e in marks if m == "tool_skipped"}
    pushed = {str(e.get("call_id")) for _, m, e in marks if m == "pushback"}
    blocked = {str(e.get("call_id")) for _, m, e in marks if m == "tool_blocked"}
    calls = trace.calls(session)
    results = {}
    for m in trace.trajectory(session):
        if m.get("role") == "tool":
            results[m.get("tool_call_id")] = (m.get("content") or "", bool((m.get("metadata") or {}).get("is_error")))
    system = next((str(m.get("content") or "") for m in (calls[0]["messages"] if calls else ())
                   if m.get("role") == "system"), "")
    out["before_run"].append({"site": "before_run", "task": task, "system": system, "messages": [], "acted": []})
    lines = [int(str(c.get("ref") or "").rpartition(":L")[2] or 0) for c in calls]
    turn = 0
    for k, call in enumerate(calls):
        if call.get("role") != "act":
            continue
        turn += 1
        here, after = lines[k], (lines[k + 1] if k + 1 < len(lines) else float("inf"))
        acted = {site: [e.get("name") for line, m, e in marks if m == "hook" and here < line < after
                        and e.get("site") == site] for site in SITES}
        # who answered first, per event: the hook that blocked this call, the hook that pushed this finish back
        blockers = [e.get("name") for line, m, e in marks if m == "hook" and here < line < after
                    and e.get("site") == "before_tool" and e.get("action") == "block"]
        pushers = [e.get("name") for line, m, e in marks if m == "hook" and here < line < after
                   and e.get("site") == "on_finish" and e.get("action") == "pushback"]
        pushbacks = sum(1 for line, m, _ in marks if m == "pushback" and line < here)
        out["before_model"].append({"site": "before_model", "turn": turn, "role": "act", "messages": call["messages"],
                                    "tools": tools_at[k] if k < len(tools_at) else [], "acted": acted["before_model"]})
        response = call.get("response") or {}
        turn_results = []
        for c in response.get("tool_calls") or ():
            if not isinstance(c, dict):
                continue
            cid, name, args = str(c.get("id") or ""), c.get("name"), dict(c.get("args") or {})
            if cid in skipped:
                continue
            if name == FINISH:
                out["on_finish"].append({"site": "on_finish", "turn": turn, "reason": "finish",
                                         "final_text": str(args.get("reason") or ""), "pushbacks": pushbacks,
                                         "acted": pushers if cid in pushed else []})
                continue
            out["before_tool"].append({"site": "before_tool", "turn": turn, "call_id": cid, "name": name,
                                       "args": args, "acted": blockers if cid in blocked else []})
            if cid in results and cid not in blocked:
                output, is_error = results[cid]
                out["after_tool"].append({"site": "after_tool", "turn": turn, "call_id": cid, "name": name,
                                          "args": args, "output": output, "is_error": is_error,
                                          "acted": acted["after_tool"]})
                turn_results.append({"call_id": cid, "name": name, "is_error": is_error,
                                     "output": output[:TURN_RESULT_CHARS]})
        for line, m, e in marks:
            if m == "implicit_finish" and here < line < after and str(e.get("call_id")) not in skipped:
                out["on_finish"].append({"site": "on_finish", "turn": turn, "reason": "implicit",
                                         "final_text": str(response.get("text") or ""), "pushbacks": pushbacks,
                                         "acted": pushers if str(e.get("call_id")) in pushed else []})
        out["after_turn"].append({"site": "after_turn", "turn": turn, "tool_results": turn_results,
                                  "acted": acted["after_turn"]})
    return out


def _triggers(session, entries, events, *, task_id, suite, manifest, site) -> tuple[bool, str]:
    """Whether a candidate hook acts on one of `events`. `entries` are (name, entrypoint, the names of the hooks
    before it at the site, in manifest order): where one of those acted on the event in the recorded attempt and
    answers first live (a pushback, a block), the candidate is not reached and the event is not offered to it."""
    episode = suite.episode(task_id)
    try:
        view = smoke_view(manifest, episode, session.limits.max_output_chars)
        broker = Broker(episode, read_only=True)
        for event in events:
            sent = {k: v for k, v in event.items() if k != "acted"}
            for _name, entry, earlier in entries:
                if site in FIRST_WINS and set(event.get("acted") or ()) & set(earlier):
                    continue
                try:
                    value = session.request({"op": "hook", "entry": entry, "event": sent, "view": view},
                                            broker=broker)
                    if action_of(site, value) != "none":
                        return True, ""
                except (PackageCallError, HookAnswerError) as e:
                    return False, f"{entry} on {task_id}: {str(e)[:200]}"
        return False, ""
    finally:
        episode.close()


def replay_check(files: dict[str, str], decl, ctx) -> dict:
    manifest = json.loads(files["manifest.json"])
    changed = {c["name"] for c in components(ctx.parent, files) if c["kind"] == "hook" and c["change"] != "deleted"
               and c.get("site") == decl.site}
    at_site = [h for h in manifest.get("hooks") or () if h.get("site") == decl.site and h.get("enabled", True)]
    entries = [(h["name"], h["entrypoint"], [x["name"] for x in at_site[:i]]) for i, h in enumerate(at_site)
               if h.get("name") in changed]
    if not entries:
        return {"passed": False, "detail": f"no hook of the candidate changed at {decl.site}"}
    claimed = {t: s for t, s in ctx.failed_sessions.items() if t in set(decl.predicted_affected)}
    if not claimed:
        return {"passed": False, "detail": "none of the claimed tasks has a failing train attempt to replay"}
    session = ctx.worker.start()
    try:
        session.load(worker_files(files, tests=False))
        fired, errors = [], []
        for task_id, sid in sorted(claimed.items()):
            hit, error = _triggers(session, entries, site_events(ctx.evidence.trace, sid)[decl.site],
                                   task_id=task_id, suite=ctx.suite, manifest=manifest, site=decl.site)
            fired += [task_id] if hit else []
            errors += [error] if error else []
        sample_fired = []
        for task_id, sid in sorted(ctx.passed_sessions.items()):
            hit, error = _triggers(session, entries, site_events(ctx.evidence.trace, sid)[decl.site],
                                   task_id=task_id, suite=ctx.suite, manifest=manifest, site=decl.site)
            sample_fired += [task_id] if hit else []
            errors += [error] if error else []
    finally:
        session.close()
    rate = round(len(sample_fired) / len(ctx.passed_sessions), 4) if ctx.passed_sessions else None
    need = int(ctx.settings.replay_min_triggers)
    passed = len(fired) >= need and not errors
    detail = (f"the hook acts on {len(fired)} of {len(claimed)} claimed failing tasks (at least {need}): "
              f"{fired}; on {len(sample_fired)} of {len(ctx.passed_sessions)} passing attempts (rate {rate})"
              + (f"; the hook failed: {errors[:3]}" if errors else ""))
    return {"passed": passed, "fired": fired, "false_trigger_rate": rate, "detail": detail[:3000]}
