"""V2d, the audit's dry run (docs/design/IMPROVER.md §8; DESIGN §6.16, v1.6): a hook that calls the aux model is
checked on what already happened, before anything is paid for a screen.

The hook's site events are rebuilt from recorded train attempts as V2b does (replay.site_events), its world a fresh
episode behind a read-only broker, its view the recorded broker calls, and its `ctx.model` answered by the research
model (role aux, metered to the round's verify account, at most `hook_aux_calls` calls per attempt). Firing is answering
a non-empty `missing` (an audit answers nothing else: a free-text answer is the candidate's defect). On the claimed
failing tasks the hook must fire on at least `replay_min_triggers`; on the sample of passing attempts its false-alarm
rate must not exceed `audit_max_false_alarm` — every false alarm is a pushback on a task the agent had right. Beside
these, whether a hit names what the grader marked failed (a value of the failed check's parameters appears in the
missing items) is reported, not judged. A failed dry run lists the misfires — the task's request and what the audit
said — for the session's revision."""
from __future__ import annotations

import json
import re

from ...harness.contract import HookAnswerError, VIEW_EVENTS, action_of
from ...runtime.broker import Broker
from ...runtime.gateway import split_system, tool_spec
from ...runtime.worker import PackageCallError, smoke_view
from ..components import components
from ..declaration import component_of
from .replay import FIRST_WINS, site_events
from .unit import worker_files

REQUEST_CHARS = 300                     # of a misfiring task's request, in the detail R reads
MIN_NAMED_CHARS = 3                     # a check's value this short is not evidence that the audit named it


def recorded_events(trace, session: str, n: int = VIEW_EVENTS) -> list[dict]:
    """The attempt's broker calls as ctx.view["events"] carries them (name, args, the result's excerpt, the error),
    the latest `n`: what an audit at the finish can see of what the agent did."""
    out = []
    events = [item["event"] for item in trace.events(session)]
    base = {str(e.get("call_id")) for e in events if e.get("kind") == "broker_event"}   # the broker's calls only
    for e in events:
        if e.get("kind") != "tool" or str(e.get("call_id")) not in base:
            continue
        out.append({"call_id": str(e.get("call_id")), "name": e.get("name"),
                    "args": e.get("args") if isinstance(e.get("args"), dict) else {},
                    "result_excerpt": str(e.get("output") if "output" in e else e.get("result") or "")[:4000],
                    "error": "error" if e.get("is_error") else "", "truncated": False, "step": 0})
    return out[-n:]


def _model_handler(provider, cap: int, counter: dict):
    """The host's answer to the hook's model_call callbacks: the research model, role aux, `cap` calls per attempt."""
    def handle(reply: dict) -> dict:
        if counter["calls"] >= cap:
            return {"op": "model_result", "id": reply.get("id"), "is_error": True,
                    "error": f"hook aux calls: {counter['calls']} this attempt, the cap (hook_aux_calls)"}
        counter["calls"] += 1
        messages = json.loads(json.dumps(list(reply.get("messages") or []), default=str))
        system, history = split_system(messages)
        specs = [tool_spec(t) for t in reply.get("tools") or ()]
        turn = provider.chat(system, history, specs)
        calls = [{"id": c.id, "name": c.name, "args": dict(c.args or {})} for c in turn.tool_calls]
        return {"op": "model_result", "id": reply.get("id"), "text": turn.text, "tool_calls": calls,
                "stop": turn.stop}
    return handle


def _fires(session, entries, events, *, task_id, suite, manifest, site, trace, sid, ctx) -> tuple[list, str]:
    """-> (the missing items the audit answered on this attempt, or [] when it did not fire; an error)."""
    episode = suite.episode(task_id)
    counter = {"calls": 0}
    try:
        view = {**smoke_view(manifest, episode, session.limits.max_output_chars,
                             hook_aux_calls=int(ctx.hook_aux_calls)), "events": recorded_events(trace, sid)}
        broker = Broker(episode, read_only=True)
        handlers = {"base_call": session.base_call_handler(broker, session.limits.max_base_calls),
                    "model_call": _model_handler(ctx.aux, int(ctx.hook_aux_calls), counter)}
        for event in events:
            sent = {k: v for k, v in event.items() if k != "acted"}
            for _name, entry, earlier in entries:
                if site in FIRST_WINS and set(event.get("acted") or ()) & set(earlier):
                    continue            # an earlier hook at the site acted here: the audit was not reached
                try:
                    value = session.serve({"op": "hook", "entry": entry, "event": sent, "view": view, "aux": True,
                                           "limit": session.limits.max_output_chars},
                                          handlers=handlers, deadline=session.limits.request_seconds)
                    action = action_of(site, value)
                except (PackageCallError, HookAnswerError) as e:
                    return [], f"{entry} on {task_id}: {str(e)[:200]}"
                if action == "none":
                    continue
                if not (isinstance(value, dict) and "missing" in value):
                    return [], f"{entry} on {task_id}: an audit answers {{\"missing\": [...]}}, not {sorted(value)}"
                return [str(i) for i in value["missing"]], ""
        return [], ""
    finally:
        episode.close()


def _named(ctx, session: str, items: list) -> bool:
    """Whether a hit names what the grader marked failed: a string value of a failed check's parameters (or one of a
    list of them), MIN_NAMED_CHARS long at least, appears in the missing items (case-insensitive)."""
    said = " ".join(items).lower()
    values = []
    for check in ctx.evidence.activity.results.grading(session):
        if check.get("passed") or check.get("excluded"):
            continue
        params = check.get("params") if isinstance(check.get("params"), dict) else {}
        for v in params.values():
            for x in (v if isinstance(v, list) else [v]):
                if isinstance(x, str) and len(x.strip()) >= MIN_NAMED_CHARS:
                    values.append(x.strip().lower())
    return any(v in said for v in values)


def audit_check(files: dict[str, str], decl, ctx) -> dict:
    comp = component_of(decl, "hook")
    if ctx.aux is None:
        return {"passed": False, "detail": "the dry run has no research model to answer the audit (VerifyContext.aux)"}
    manifest = json.loads(files["manifest.json"])
    changed = {c["name"] for c in components(ctx.parent, files) if c["kind"] == "hook" and c["change"] != "deleted"
               and c.get("site") == comp.site}
    at_site = [h for h in manifest.get("hooks") or () if h.get("site") == comp.site and h.get("enabled", True)]
    entries = [(h["name"], h["entrypoint"], [x["name"] for x in at_site[:i]]) for i, h in enumerate(at_site)
               if h.get("name") in changed]
    if not entries:
        return {"passed": False, "detail": f"no hook of the candidate changed at {comp.site}"}
    claimed = {t: s for t, s in ctx.failed_sessions.items() if t in set(comp.predicted_affected)}
    if not claimed:
        return {"passed": False, "detail": "none of the claimed tasks has a failing train attempt to dry-run on"}
    trace = ctx.evidence.trace
    session = ctx.worker.start()
    hits, named, misfires, errors = [], [], [], []
    try:
        session.load(worker_files(files, tests=False))
        for task_id, sid in sorted(claimed.items()):
            items, error = _fires(session, entries, site_events(trace, sid)[comp.site], task_id=task_id,
                                  suite=ctx.suite, manifest=manifest, site=comp.site, trace=trace, sid=sid, ctx=ctx)
            if error:
                errors.append(error)
            elif items:
                hits.append(task_id)
                if _named(ctx, sid, items):
                    named.append(task_id)
        for task_id, sid in sorted(ctx.passed_sessions.items()):
            items, error = _fires(session, entries, site_events(trace, sid)[comp.site], task_id=task_id,
                                  suite=ctx.suite, manifest=manifest, site=comp.site, trace=trace, sid=sid, ctx=ctx)
            if error:
                errors.append(error)
            elif items:
                misfires.append({"task_id": task_id, "request": ctx.train_texts.get(task_id, "")[:REQUEST_CHARS],
                                 "missing": items})
    finally:
        session.close()
    sample = len(ctx.passed_sessions)
    rate = round(len(misfires) / sample, 4) if sample else None
    need = int(ctx.settings.replay_min_triggers)
    cap = float(ctx.settings.audit_max_false_alarm)
    passed = len(hits) >= need and (rate is None or rate <= cap + 1e-9) and not errors
    detail = (f"the audit fires on {len(hits)} of {len(claimed)} claimed failing tasks (at least {need}): {hits}"
              f"{' — naming what failed on ' + str(named) if named else ''}; on {len(misfires)} of {sample} passing "
              f"attempts (rate {rate}, at most {cap})")
    if misfires:
        detail += "; false alarms — each a pushback on a task the agent had right:\n" + "\n".join(
            f"  {m['task_id']}: request {m['request']!r}; the audit said {m['missing']}" for m in misfires)
    if errors:
        detail += f"; the hook failed: {errors[:3]}"
    return {"passed": passed, "hits": hits, "named": named, "false_alarms": [m["task_id"] for m in misfires],
            "false_alarm_rate": rate, "misfires": misfires, "errors": errors, "detail": detail[:6000]}
