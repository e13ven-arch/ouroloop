"""V2b, hook replay (docs/design/IMPROVER.md §8; DESIGN §6.6, §6.20): a hook is checked on what already happened.

A train attempt's transcript is rebuilt into one stream of site events in the order the stirrup seed fired them —
before_run (the task, the system), then per turn before_model (the act call's messages and tools), before_tool and
after_tool for each call in order (and on_finish for a finish), after_turn at the turn's end — each event carrying
`view_events`, the broker calls made before that moment as the attempt's ctx.view["events"] carried them (the latest
VIEW_EVENTS), and `turn`. v1.10 (DESIGN §6.20, after finance-v9 r0): the replay used to hand every hook a smoke view
with no events and replay each site alone, so a before_tool hold that needs what an after_tool observer saw, or what
the agent read, could never act here, and R moved its hold to an after_tool feedback that came after the write. Now a
task is replayed whole: the package is loaded afresh in the worker (module state starts empty), every enabled hook of
the package that does not call the model runs on every event in manifest order, with the view's `events` and `turn`
set for that moment, and at a first-wins site the first hook that blocks or pushes back ends the event's dispatch (as
the seed does; a parent hook that cannot run here stops it where the record says it acted). A trigger is counted only
when one of the candidate's changed hooks at the declared site answers other than none. The rung passes when the
hook triggers on at least `replay_min_triggers` of the failing train tasks it claims and on at most
`replay_max_false_trigger` of the passing train attempts replayed (v1.7, DESIGN §6.17: a hook that fires everywhere
is a hook that changes everything — finance-v7 installed one that spoke on 85% of the attempts; a text the agent
should see every time is a prompt component). The world behind ctx.tool is a fresh episode behind a read-only broker
(a hook looks, it does not act)."""
from __future__ import annotations

import json

from ...harness.contract import SITES, VIEW_EVENTS, HookAnswerError, action_of
from ...runtime.broker import Broker
from ...runtime.worker import PackageCallError, smoke_view
from ..components import components
from ..declaration import component_of
from .unit import worker_files

FIRST_WINS = ("on_finish", "before_tool")   # the first hook that pushes back or blocks ends the site's dispatch
PRIVATE = ("acted", "view_events")          # the replay's own keys on an event: not handed to the hook


TURN_RESULT_CHARS = 4000               # of a tool's output in an after_turn event (the stirrup seed's)
FINISH = "finish"
EXCERPT_CHARS = 4000                   # of a broker call's output in view_events (audit.recorded_events's)


def _mark(e: dict) -> str | None:
    """A mark line's kind (the host keeps a hook that acted as a `hook` line)."""
    if e.get("kind") == "hook":
        return "hook"
    return e.get("mark") if e.get("kind") == "mark" else None


def _view_event(e: dict) -> dict:
    """A broker `tool` line as ctx.view["events"] carries it (runtime/broker.py AttemptBroker.call)."""
    output = str(e.get("output") if "output" in e else e.get("result") or "")
    is_error = bool(e.get("is_error"))
    return {"call_id": str(e.get("call_id")), "name": e.get("name"),
            "args": e.get("args") if isinstance(e.get("args"), dict) else {},
            "result_excerpt": "" if is_error else output[:EXCERPT_CHARS],
            "error": output[:EXCERPT_CHARS] if is_error else "", "truncated": bool(e.get("truncated")),
            "step": int(e.get("step") or 0)}


def attempt_events(trace, session: str) -> list[dict]:
    """One train attempt as the ordered stream of site events, shaped as the stirrup seed passes them to hooks:
    on_finish has reason "finish" (a finish call) or "implicit" (text-only replies the seed turned into a finish),
    final_text the finish's reason, pushbacks the pushbacks before it; finish is no tool call to before_tool,
    after_tool or after_turn; a call stopped before it ran has no after_tool and is not in its turn's results. Each
    event carries the hooks that acted on it in the recorded attempt (`acted`, for the manifest order) and
    `view_events`: the broker's calls before that moment, the latest VIEW_EVENTS, as ctx.view["events"] held them.
    Within a turn the broker's `tool` lines are aligned to the model's calls by name, in order (a base tool's call is
    its own line; a package tool's call takes the lines between two aligned base calls)."""
    out: list[dict] = []
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
    broker_ids = {str(e.get("call_id")) for e in events if e.get("kind") == "broker_event"}
    broker = [(item["line"], _view_event(item["event"])) for item in items
              if item["event"].get("kind") == "tool" and str(item["event"].get("call_id")) in broker_ids]
    calls = trace.calls(session)
    results = {}
    for m in trace.trajectory(session):
        if m.get("role") == "tool":
            results[m.get("tool_call_id")] = (m.get("content") or "", bool((m.get("metadata") or {}).get("is_error")))
    system = next((str(m.get("content") or "") for m in (calls[0]["messages"] if calls else ())
                   if m.get("role") == "system"), "")
    lines = [int(str(c.get("ref") or "").rpartition(":L")[2] or 0) for c in calls]

    def window(upto: int) -> list[dict]:
        return [dict(ev) for _line, ev in broker[max(0, upto - VIEW_EVENTS):upto]]

    out.append({"site": "before_run", "task": task, "system": system, "messages": [], "acted": [],
                "view_events": [], "turn": 0})
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
        span_start = next((i for i, (line, _) in enumerate(broker) if line > here), len(broker))
        span_end = next((i for i, (line, _) in enumerate(broker) if line > after), len(broker))
        out.append({"site": "before_model", "turn": turn, "role": "act", "messages": call["messages"],
                    "tools": tools_at[k] if k < len(tools_at) else [], "acted": acted["before_model"],
                    "view_events": window(span_start)})
        response = call.get("response") or {}
        model_calls = [c for c in response.get("tool_calls") or () if isinstance(c, dict)
                       and str(c.get("id") or "") not in skipped]
        # align the turn's broker lines to the model's calls: a base tool's call is the next line of its name; a
        # package tool's call (no line of its own) takes what lies before the next aligned base call
        aligned: list = []
        pos = span_start
        for c in model_calls:
            cid, name = str(c.get("id") or ""), c.get("name")
            if name == FINISH or cid in blocked:
                aligned.append(None)
                continue
            j = next((i for i in range(pos, span_end) if broker[i][1]["name"] == name), None)
            aligned.append(j)
            if j is not None:
                pos = j + 1
        turn_results = []
        pos = span_start
        for i, c in enumerate(model_calls):
            cid, name, args = str(c.get("id") or ""), c.get("name"), dict(c.get("args") or {})
            if name == FINISH:
                out.append({"site": "on_finish", "turn": turn, "reason": "finish",
                            "final_text": str(args.get("reason") or ""), "pushbacks": pushbacks,
                            "acted": pushers if cid in pushed else [], "view_events": window(pos)})
                continue
            out.append({"site": "before_tool", "turn": turn, "call_id": cid, "name": name, "args": args,
                        "acted": blockers if cid in blocked else [], "view_events": window(pos)})
            if cid in blocked:
                continue
            j = aligned[i]
            if j is not None:
                pos = j + 1
            else:                       # a package tool: its lines run up to the next aligned base call
                nxt = next((x for x in aligned[i + 1:] if x is not None), span_end)
                pos = max(pos, nxt)
            if cid in results:
                output, is_error = results[cid]
                out.append({"site": "after_tool", "turn": turn, "call_id": cid, "name": name, "args": args,
                            "output": output, "is_error": is_error, "acted": acted["after_tool"],
                            "view_events": window(pos)})
                turn_results.append({"call_id": cid, "name": name, "is_error": is_error,
                                     "output": output[:TURN_RESULT_CHARS]})
        for line, m, e in marks:
            if m == "implicit_finish" and here < line < after and str(e.get("call_id")) not in skipped:
                out.append({"site": "on_finish", "turn": turn, "reason": "implicit",
                            "final_text": str(response.get("text") or ""), "pushbacks": pushbacks,
                            "acted": pushers if str(e.get("call_id")) in pushed else [],
                            "view_events": window(span_end)})
        out.append({"site": "after_turn", "turn": turn, "tool_results": turn_results, "acted": acted["after_turn"],
                    "view_events": window(span_end)})
    return out


def site_events(trace, session: str) -> dict[str, list[dict]]:
    """{site: [event]} of one train session, in transcript order: attempt_events by site (the audit's dry run and
    the merge's replay read one site at a time)."""
    out: dict[str, list[dict]] = {s: [] for s in SITES}
    for e in attempt_events(trace, session):
        out[e["site"]].append(e)
    return out


def hooks_of(manifest: dict) -> dict[str, list[dict]]:
    """The package's enabled hooks by site, in manifest order (the seed's Hooks.by_site)."""
    return {s: [h for h in manifest.get("hooks") or () if h.get("site") == s and h.get("enabled", True)]
            for s in SITES}


def replay_task(session, files: dict[str, str], manifest: dict, events: list[dict], *, task_id: str, suite,
                own: set, count_at: set) -> tuple[bool, str]:
    """Replay one attempt's events through the package in `session` (loaded afresh here, so module state starts
    empty): every enabled hook runs on every event in manifest order; a hook of `own` (the candidate's changed hooks)
    at a site of `count_at` that answers other than none is a trigger -> (True, ""). A hook not the candidate's that
    fails here (it calls the model, or raises) is skipped — it passed its own rungs — and, at a first-wins site,
    stops the dispatch where the record says it acted; the candidate's own failure is the candidate's."""
    by_site = hooks_of(manifest)
    episode = suite.episode(task_id)
    try:
        session.load(worker_files(files, tests=False))
        base = smoke_view(manifest, episode, session.limits.max_output_chars)
        broker = Broker(episode, read_only=True)
        for event in events:
            site = event["site"]
            sent = {k: v for k, v in event.items() if k not in PRIVATE}
            view = {**base, "events": list(event.get("view_events") or ()), "turn": int(event.get("turn") or 0)}
            for hook in by_site.get(site) or ():
                name, entry = hook.get("name"), hook.get("entrypoint")
                try:
                    value = session.request({"op": "hook", "entry": entry, "event": sent, "view": view},
                                            broker=broker)
                    action = action_of(site, value)
                except (PackageCallError, HookAnswerError) as e:
                    if name in own:
                        return False, f"{entry} on {task_id}: {str(e)[:200]}"
                    action = "none"
                    if site in FIRST_WINS and name in (event.get("acted") or ()):
                        break           # it blocked or pushed back live: the hooks after it were not reached
                    continue
                if name in own and site in count_at and action != "none":
                    return True, ""
                if site in FIRST_WINS and action in ("block", "pushback"):
                    break
                # v1.11: as the seed does, a hook that modified the arguments or the output hands the next hook the
                # modified event
                if action == "modify" and isinstance(value, dict):
                    if site == "before_tool" and isinstance(value.get("args"), dict):
                        sent = {**sent, "args": dict(value["args"])}
                    elif site == "after_tool" and isinstance(value.get("output"), str):
                        sent = {**sent, "output": value["output"]}
        return False, ""
    finally:
        episode.close()


def changed_hooks(parent, files: dict[str, str]) -> dict[str, str | None]:
    """{name: site} of the candidate's hooks that changed against the parent (added or modified, not deleted)."""
    return {c["name"]: c.get("site") for c in components(parent, files) if c["kind"] == "hook"
            and c["change"] != "deleted"}


def replay_check(files: dict[str, str], decl, ctx) -> dict:
    decl = component_of(decl, "hook")
    manifest = json.loads(files["manifest.json"])
    changed = changed_hooks(ctx.parent, files)
    at_site = {n for n, s in changed.items() if s == decl.site
               and any(h.get("name") == n and h.get("enabled", True) for h in hooks_of(manifest)[decl.site])}
    if not at_site:
        return {"passed": False, "detail": f"no hook of the candidate changed at {decl.site}"}
    claimed = {t: s for t, s in ctx.failed_sessions.items() if t in set(decl.predicted_affected)}
    if not claimed:
        return {"passed": False, "detail": "none of the claimed tasks has a failing train attempt to replay"}
    own = set(changed)
    session = ctx.worker.start()
    try:
        fired, errors = [], []
        for task_id, sid in sorted(claimed.items()):
            hit, error = replay_task(session, files, manifest, attempt_events(ctx.evidence.trace, sid),
                                     task_id=task_id, suite=ctx.suite, own=own, count_at={decl.site})
            fired += [task_id] if hit else []
            errors += [error] if error else []
        sample_fired = []
        for task_id, sid in sorted(ctx.passed_sessions.items()):
            hit, error = replay_task(session, files, manifest, attempt_events(ctx.evidence.trace, sid),
                                     task_id=task_id, suite=ctx.suite, own=own, count_at={decl.site})
            sample_fired += [task_id] if hit else []
            errors += [error] if error else []
    finally:
        session.close()
    # v1.11 (DESIGN §6.21): a claimed task where the hook acted in one of R's own tries this round is a trigger — a
    # real run outranks the replay
    keys = {f"hook:{n}" for n in at_site}
    tried = sorted(t for t in claimed if set((ctx.tried_acted or {}).get(t) or ()) & keys and t not in fired)
    fired = sorted([*fired, *tried])
    rate = round(len(sample_fired) / len(ctx.passed_sessions), 4) if ctx.passed_sessions else None
    need = int(ctx.settings.replay_min_triggers)
    cap = float(getattr(ctx.settings, "replay_max_false_trigger", 1.0))
    noisy = rate is not None and rate > cap
    passed = len(fired) >= need and not errors and not noisy
    detail = (f"the hook acts on {len(fired)} of {len(claimed)} claimed failing tasks (at least {need}): "
              f"{fired}" + (f" ({len(tried)} of them in your tries)" if tried else "")
              + f"; on {len(sample_fired)} of {len(ctx.passed_sessions)} passing attempts (rate {rate}, at most "
              f"{cap})"
              + (f"; it acts on too many passing attempts ({sample_fired}): a hook acts when something is wrong, and "
                 f"a text the agent should see every time is a prompt component" if noisy else "")
              + (f"; the hook failed: {errors[:3]}" if errors else ""))
    return {"passed": passed, "fired": fired, "false_trigger_rate": rate, "false_triggers": sample_fired,
            "detail": detail[:3000]}
