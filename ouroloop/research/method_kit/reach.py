"""middlewares/_m_reach.py: the reach check's harness (docs/design/V0_5_METHOD_LIBRARY.md §2.7). Loaded only into a
draft check's worker session beside the candidate's files, never into a package. Standard library only.

`replay(ctx, event)` runs through the worker's existing `hook` op: it replays one recorded train attempt's tool events
through the triggers of the methods named, in the shape each site sees (a before_tool method gets the call before it
runs, an after_tool method the call with its result, an on_finish method the final message), and — when asked — runs
each method once at its first firing, through ctx.call_tool: the host binds that broker, read-only, to a fresh world
of the same train task. It answers counts and the method's output text; the host counts the task's assertion values in
that text itself, so no assertion value ever enters the worker.
"""
import importlib.util
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_OF = {"recommend": "after_tool", "inject": "after_tool", "block_once": "before_tool", "pushback": "on_finish"}
VIEW_EVENTS = 20
MODULES = {}


def _module(path):
    if path not in MODULES:
        spec = importlib.util.spec_from_file_location("mreach_" + path.replace("/", "_").replace(".py", ""),
                                                      os.path.join(ROOT, path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        MODULES[path] = module
    return MODULES[path]


class _Capped:
    def __init__(self, ctx, view, cap):
        self.view, self._ctx, self._cap, self.calls = view, ctx, cap, 0

    def call_tool(self, name, args=None):
        if self.calls >= self._cap:
            return _Res(f"refused: a method makes at most {self._cap} base calls per run", True)
        self.calls += 1
        return self._ctx.call_tool(name, args)


class _Res:
    def __init__(self, output, is_error):
        self.output, self.is_error = output, is_error


def _text(value):
    if isinstance(value, dict) and isinstance(value.get("output"), str):
        return value["output"], bool(value.get("is_error"))
    if isinstance(value, str):
        return value, False
    out = getattr(value, "output", None)
    if isinstance(out, str):
        return out, bool(getattr(value, "is_error", False))
    return json.dumps(value, default=str), False


JOURNAL = os.path.join(ROOT, ".m_journal.json")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _record_write(call, result):
    """What the router does after a call (m_router._record_write): a successful write joins the journal's writes,
    so a trigger that reads the journal fires in the replay as it would in the attempt."""
    args = call.get("args") or {}
    if call.get("tool") != "api_fetch" or result.get("is_error"):
        return
    method = str(args.get("method") or "GET").upper()
    if method not in WRITE_METHODS:
        return
    try:
        body = json.loads(args.get("body")) if isinstance(args.get("body"), str) else (args.get("body") or {})
    except ValueError:
        body = {}
    try:
        with open(JOURNAL, encoding="utf-8") as f:
            journal = json.load(f)
    except (OSError, ValueError):
        journal = {}
    journal.setdefault("writes", []).append({"target": f"{method} {str(args.get('url') or '').split('?')[0]}",
                                             "fields": sorted(str(k) for k in body)[:30] if isinstance(body, dict)
                                             else [], "turn": call.get("turn")})
    with open(JOURNAL, "w", encoding="utf-8") as f:
        json.dump(journal, f)


def replay(ctx, event):
    if os.path.exists(JOURNAL):
        os.remove(JOURNAL)                     # every replayed attempt starts with an empty journal, as an attempt does
    methods = event.get("methods") or {}
    events = event.get("events") or []
    task = str(event.get("task") or "")
    package = event.get("package") or {}
    run = bool(event.get("run"))
    cap = int(event.get("max_calls") or 25)
    max_output = int(event.get("max_output") or 20000)
    out = {name: {"fired": False, "kind": None, "at": None, "ran": False, "error": None, "output": "", "calls": 0,
                  "trigger_errors": 0} for name in methods}
    seen = []
    loaded = {}
    for name, decl in methods.items():
        try:
            loaded[name] = _module(decl["module"])
        except Exception as e:  # noqa: BLE001 - reported as the method's error
            out[name]["error"] = f"load: {type(e).__name__}: {str(e)[:200]}"

    def view_at(turn):
        return {"task": task, "events": list(seen[-VIEW_EVENTS:]), "package": package, "turn": turn}

    def try_fire(name, decl, ev, turn):
        if name not in loaded or out[name]["fired"]:
            return
        trig = getattr(loaded[name], str(decl.get("trigger")), None)
        if not callable(trig):
            out[name]["error"] = out[name]["error"] or f"no trigger function {decl.get('trigger')!r}"
            return
        try:
            fired = trig(ev, view_at(turn))
        except Exception:  # noqa: BLE001 - a trigger that raises does not fire (as in the router)
            out[name]["trigger_errors"] += 1
            return
        if not isinstance(fired, dict) or not isinstance(fired.get("kind"), str):
            return
        kinds = decl.get("kinds")
        if isinstance(kinds, (list, tuple)) and kinds and fired["kind"] not in kinds:
            return
        out[name].update(fired=True, kind=fired["kind"], at=turn)
        if not run:
            return
        fn = getattr(loaded[name], str(decl.get("func")), None)
        if not callable(fn):
            out[name]["error"] = f"no method function {decl.get('func')!r}"
            return
        args = dict(fired.get("args") or {}) if isinstance(fired.get("args"), dict) else {}
        if decl.get("default_variant"):
            args.setdefault("variant", decl["default_variant"])
        if SITE_OF.get(decl.get("on")) == "before_tool":
            args.update(tool=ev.get("tool"), args=ev.get("args"))
        capped = _Capped(ctx, view_at(turn), cap)
        try:
            text, is_error = _text(fn(capped, args))
        except Exception as e:  # noqa: BLE001 - reported to R as the method's error
            out[name].update(error=f"{type(e).__name__}: {str(e)[:200]}", calls=capped.calls)
            return
        out[name].update(ran=not is_error, output=text[:max_output], calls=capped.calls,
                         error=None if not is_error else f"answered is_error: {text[:200]}")

    for turn, ev in enumerate(events, start=1):
        call = {"tool": ev.get("tool"), "args": ev.get("args") or {}, "turn": turn}
        for name, decl in methods.items():
            if SITE_OF.get(decl.get("on")) == "before_tool":
                try_fire(name, decl, call, turn)
        result = {**call, "result": str(ev.get("output") or "")[:4000], "is_error": bool(ev.get("is_error"))}
        _record_write(call, result)
        seen.append({"call_id": f"r{turn}", "name": call["tool"], "args": call["args"],
                     "result_excerpt": "" if result["is_error"] else result["result"][:2000],
                     "error": result["result"][:2000] if result["is_error"] else "", "step": turn})
        for name, decl in methods.items():
            if SITE_OF.get(decl.get("on"), "after_tool") == "after_tool":
                try_fire(name, decl, result, turn)
    final = {"tool": None, "final_message": str(event.get("final_message") or "")[:4000], "turn": len(events) + 1}
    for name, decl in methods.items():
        if SITE_OF.get(decl.get("on")) == "on_finish":
            try_fire(name, decl, final, len(events) + 1)
    return {"methods": out}
