"""middlewares/m_router.py: the method router (a kit file, fixed bytes; docs/design/V0_5_METHOD_LIBRARY.md §2.5).

Standard library only. Kit completion copies these bytes into every package that has an `m_` tool and registers three
hooks: `after` (after_tool), `before` (before_tool) and `finish` (on_finish). R cannot change this file — an edited
copy is replaced before the draft check — because the router executes the policy the fixed code learns, and the
table it reads (`middlewares/m_table.py`, a literal) is written by fixed code only.

What it does, per attempt (the worker loads the bundle once per attempt, so module state is the attempt's):
- reads the enabled `m_` tools from ctx.view["package"], each method's declaration from its module's `METHODS` literal
  (by `ast`, never by executing the declaration) and the cell table;
- at the site of a method's `on` action (recommend and inject after a tool call, block_once before one, pushback at
  finish) runs its trigger — a pure function of the event and the view — and at the first firing of a (method, kind)
  decides once: the cell's action and variant, or in an explore twin (learn readings on train only) the antithetic
  hash assignment with its exact propensity; every decision is marked `method_decision`;
- acts within the caps: per method at most one recommendation or injection an attempt; per attempt at most 3
  recommendations, 2 injections, 2 blocks; a body is blocked once (an unchanged resend goes through); a verification
  method fires on an outbound send only when the attempt's journal holds a decision to check against (the lesson of
  send_review, which acted on every first send and broke 2 of 6 passing tasks);
- appends every successful write (target and fields) to the journal, so a verification method can check effects.

A trigger or method that raises is the method's failure, never the attempt's: it is counted and the router goes on.
"""
import ast
import hashlib
import importlib.util
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TABLE_FILE = os.path.join(HERE, "m_table.py")
JOURNAL_FILE = os.path.join(ROOT, ".m_journal.json")
PREFIX = "m_"
ACTIONS = ("recommend", "inject", "block_once", "pushback")
SITE_OF = {"recommend": "after_tool", "inject": "after_tool", "block_once": "before_tool", "pushback": "on_finish"}
CAPS = {"recommend": 3, "inject": 2, "block_once": 2, "pushback": 1}
METHOD_CALLS = 25
INJECT_CHARS = 1800
MESSAGE_CHARS = 2000
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
STATE = {}


# ---------------------------------------------------------------------------- reading the package
def _literal(path, name):
    """The literal assigned to `name` at a module's top level, read with ast (never executed); None if absent."""
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return None
    return None


def table():
    if "table" not in STATE:
        t = _literal(TABLE_FILE, "TABLE")
        STATE["table"] = t if isinstance(t, dict) else {"mode": "greedy", "cells": {}}
    return STATE["table"]


def methods(view):
    """{name: declaration + module + func} of the enabled m_ tools that declare themselves in METHODS."""
    if "methods" in STATE:
        return STATE["methods"]
    out = {}
    for tool in ((view or {}).get("package") or {}).get("tools") or ():
        name = str(tool.get("name") or "")
        if not name.startswith(PREFIX) or not tool.get("enabled", True):
            continue
        module_path, _, func = str(tool.get("entrypoint") or "").partition(":")
        decls = _literal(os.path.join(ROOT, module_path), "METHODS")
        decl = decls.get(name) if isinstance(decls, dict) else None
        if isinstance(decl, dict):
            out[name] = dict(decl, module=module_path, func=func)
    STATE["methods"] = out
    return out


def _module(path):
    mods = STATE.setdefault("modules", {})
    if path not in mods:
        full = os.path.join(ROOT, path)
        spec = importlib.util.spec_from_file_location("mrouter_" + path.replace("/", "_").replace(".py", ""), full)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        mods[path] = module
    return mods[path]


def _fn(decl, attr):
    try:
        return getattr(_module(decl["module"]), str(attr), None)
    except Exception:  # noqa: BLE001 - a method module that fails to load is the method's failure
        STATE["errors"] = STATE.get("errors", 0) + 1
        return None


# ---------------------------------------------------------------------------- the journal
def journal():
    try:
        with open(JOURNAL_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def journal_size():
    j = journal()
    return sum(len(j.get(s) or ()) for s in ("excluded", "superseded", "required"))


def _record_write(event):
    args = event.get("args") or {}
    if event.get("tool") != "api_fetch" or event.get("is_error"):
        return
    method = str(args.get("method") or "GET").upper()
    if method not in WRITE_METHODS:
        return
    body = args.get("body")
    try:
        data = json.loads(body) if isinstance(body, str) else (body if isinstance(body, dict) else {})
    except ValueError:
        data = {}
    item = {"target": f"{method} {str(args.get('url') or '').split('?')[0]}",
            "fields": sorted(str(k) for k in data)[:30] if isinstance(data, dict) else [],
            "turn": event.get("turn")}
    j = journal()
    writes = list(j.get("writes") or ())
    writes.append(item)
    j["writes"] = writes[-200:]
    tmp = JOURNAL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(j, f, default=str)
    os.replace(tmp, JOURNAL_FILE)


# ---------------------------------------------------------------------------- deciding
def _h(*parts):
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest(), 16)


def bucket_of(name, context, t):
    split = (t.get("splits") or {}).get(name)
    if not isinstance(split, dict) or not split.get("feature"):
        return "*"
    try:
        value = float((context or {}).get(split["feature"]))
        cut = float(split.get("cut"))
    except (TypeError, ValueError):
        return "*"
    return f"{split['feature']}={'1' if value >= cut else '0'}"


def cell_of(name, kind, bucket, t):
    cells = t.get("cells") or {}
    for key in (f"{name}|{kind}|{bucket}", f"{name}|{kind}|*"):
        if isinstance(cells.get(key), dict):
            return key, cells[key]
    return f"{name}|{kind}|*", None


def assign(salt, task, group, kind, twin):
    """The explore twins' antithetic switch: twin A takes the hash bit, twin B its complement (propensity 0.5)."""
    bit = _h(salt, task, group, kind) % 2
    return 1 - bit if twin == "B" else bit


def assign_variant(salt, task, name, kind, variants, twin):
    """Twin A takes H mod k; twin B another variant, also by the hash (propensity 1/k)."""
    k = len(variants)
    h = _h(salt, task, name, kind, "variant")
    a = h % k
    if twin != "B":
        return variants[a]
    return variants[(a + 1 + (h // k) % (k - 1)) % k]


def decide(ctx, name, decl, fired, site):
    kind = str(fired.get("kind"))
    key = f"{name}|{kind}"
    done = STATE.setdefault("decisions", {})
    if key in done:
        return done[key]
    t = table()
    bucket = bucket_of(name, fired.get("context"), t)
    cell_key, cell = cell_of(name, kind, bucket, t)
    on = decl.get("on") if decl.get("on") in ACTIONS else "recommend"
    variants = [v for v in decl.get("variants") or () if isinstance(v, str)][:4]
    variant = (cell or {}).get("variant") or decl.get("default_variant") or (variants[0] if variants else None)
    mode = t.get("mode", "greedy")
    view = getattr(ctx, "view", None) or {}
    variant_propensity = 1.0
    if mode == "explore":
        couples = [c for c in decl.get("couples_with") or () if isinstance(c, str)]
        group = "+".join(sorted({name, *couples}))
        bit = assign(t.get("salt", ""), str(view.get("task", "")), group, kind, t.get("twin"))
        action, propensity = (on if bit else "silent"), 0.5
        if len(variants) >= 2:
            variant = assign_variant(t.get("salt", ""), str(view.get("task", "")), name, kind, variants, t.get("twin"))
            variant_propensity = 1.0 / len(variants)
    else:
        action = (cell or {}).get("action", on)
        if action not in ACTIONS + ("silent",):
            action = "silent"
        propensity = 1.0
    decision = {"method": name, "kind": kind, "cell": cell_key, "bucket": bucket, "action": action,
                "variant": variant, "propensity": propensity, "variant_propensity": variant_propensity,
                "mode": mode, "twin": t.get("twin"), "site": site, "turn": view.get("turn"),
                "args": fired.get("args") if isinstance(fired.get("args"), dict) else {},
                "context": fired.get("context") if isinstance(fired.get("context"), dict) else {}}
    done[key] = decision
    # the record's own `kind` is the mark's: the trigger kind travels as `trigger_kind`
    _mark(ctx, "method_decision", {("trigger_kind" if k == "kind" else k): decision[k] for k in decision if k != "args"})
    return decision


def _mark(ctx, kind, data):
    mark = getattr(ctx, "mark", None)
    if callable(mark):
        try:
            mark(kind, **json.loads(json.dumps(data, default=str)))
        except Exception:  # noqa: BLE001 - a record that cannot be written is not the attempt's failure
            pass


def _take(name, action):
    counts = STATE.setdefault("counts", {})
    per = STATE.setdefault("per_method", {})
    if counts.get(action, 0) >= CAPS.get(action, 0):
        return False
    if action in ("recommend", "inject", "pushback") and per.get((name, action), 0) >= 1:
        return False
    counts[action] = counts.get(action, 0) + 1
    per[(name, action)] = per.get((name, action), 0) + 1
    return True


def fire(name, decl, event, view):
    trig = _fn(decl, decl.get("trigger"))
    if not callable(trig):
        return None
    try:
        fired = trig(event, view)
    except Exception:  # noqa: BLE001 - a trigger that raises does not fire
        STATE["errors"] = STATE.get("errors", 0) + 1
        return None
    if not isinstance(fired, dict) or not isinstance(fired.get("kind"), str):
        return None
    kinds = decl.get("kinds")
    if isinstance(kinds, (list, tuple)) and kinds and fired["kind"] not in kinds:
        return None
    if decl.get("family") == "verification" and fired["kind"] == "outbound_send" and journal_size() == 0:
        return None
    return fired


# ---------------------------------------------------------------------------- running a method
class _Capped:
    """The hook's read-only ctx with the method's call cap."""

    def __init__(self, ctx, cap):
        self.view, self._ctx, self._cap, self.calls = getattr(ctx, "view", {}), ctx, cap, 0

    def call_tool(self, name, args=None):
        if self.calls >= self._cap:
            return _Res(f"refused: a method makes at most {self._cap} base calls per run", True)
        self.calls += 1
        return self._ctx.call_tool(name, args)


class _Res:
    def __init__(self, output, is_error):
        self.output, self.is_error = output, is_error


def run_method(ctx, decl, args):
    """-> (parsed answer dict, text). The method's answer as JSON when it is, else {"text": ...}."""
    fn = _fn(decl, decl.get("func"))
    if not callable(fn):
        return {"error": "the method could not be loaded"}, ""
    try:
        value = fn(_Capped(ctx, METHOD_CALLS), dict(args))
    except Exception as e:  # noqa: BLE001 - the method's failure, counted, never the attempt's
        STATE["errors"] = STATE.get("errors", 0) + 1
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}, ""
    if isinstance(value, dict) and isinstance(value.get("output"), str):
        text = value["output"]
    elif isinstance(value, str):
        text = value
    else:
        text = str(getattr(value, "output", value))
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    return (parsed if isinstance(parsed, dict) else {"text": text}), text


def _summary(parsed, text):
    if isinstance(parsed.get("summary"), str):
        return parsed["summary"][:INJECT_CHARS]
    return (text or json.dumps(parsed, default=str))[:INJECT_CHARS]


def _call_args(decision):
    args = dict(decision.get("args") or {})
    if decision.get("variant"):
        args["variant"] = decision["variant"]
    return args


# ---------------------------------------------------------------------------- the three hooks
def after(ctx, event):
    view = getattr(ctx, "view", None) or {}
    _record_write(event or {})
    messages = []
    for name, decl in sorted(methods(view).items()):
        if SITE_OF.get(decl.get("on")) != "after_tool":
            continue
        fired = fire(name, decl, event, view)
        if not fired:
            continue
        d = decide(ctx, name, decl, fired, "after_tool")
        if d["action"] == "recommend" and _take(name, "recommend"):
            messages.append(f"[method] {name} may help here ({d['kind']}): call {name} with "
                            f"{json.dumps(_call_args(d), default=str)[:300]}. It returns evidence with its sources "
                            f"and what it could not classify; you decide.")
        elif d["action"] == "inject" and _take(name, "inject"):
            parsed, text = run_method(ctx, decl, _call_args(d))
            _mark(ctx, "method_inject", {"method": name, "chars": len(text), "error": parsed.get("error")})
            if not parsed.get("error"):
                messages.append(f"[method] {name} ({d['kind']}) found:\n{_summary(parsed, text)}\n"
                                f"Call {name} for its full output. It is evidence; you decide.")
    if messages:
        return {"action": "feedback", "message": "\n".join(messages)[:MESSAGE_CHARS]}
    return {"action": "continue"}


def before(ctx, event):
    view = getattr(ctx, "view", None) or {}
    for name, decl in sorted(methods(view).items()):
        if decl.get("on") != "block_once":
            continue
        fired = fire(name, decl, event, view)
        if not fired:
            continue
        d = decide(ctx, name, decl, fired, "before_tool")
        if d["action"] != "block_once":
            continue
        body = hashlib.sha256(json.dumps(event.get("args"), sort_keys=True, default=str).encode()).hexdigest()
        blocked = STATE.setdefault("blocked", set())
        if body in blocked or STATE.get("counts", {}).get("block_once", 0) >= CAPS["block_once"]:
            continue
        parsed, text = run_method(ctx, decl, {**_call_args(d), "tool": event.get("tool"), "args": event.get("args")})
        problems = parsed.get("violations") if isinstance(parsed.get("violations"), list) else []
        if not problems:
            continue
        blocked.add(body)
        _take(name, "block_once")
        _mark(ctx, "method_block", {"method": name, "violations": len(problems)})
        listed = "; ".join(json.dumps(p, default=str)[:200] for p in problems[:6])
        return {"action": "block", "message": (f"[method] {name} checked this before it goes out and found: {listed}. "
                                               f"Fix it and send again; an unchanged call goes through.")
                [:MESSAGE_CHARS]}
    return {"action": "continue"}


def finish(ctx, event):
    view = getattr(ctx, "view", None) or {}
    for name, decl in sorted(methods(view).items()):
        if decl.get("on") != "pushback":
            continue
        fired = fire(name, decl, {**(event or {}), "tool": None}, view)
        if not fired:
            continue
        d = decide(ctx, name, decl, fired, "on_finish")
        if d["action"] != "pushback" or not _take(name, "pushback"):
            continue
        parsed, text = run_method(ctx, decl, _call_args(d))
        problems = parsed.get("discrepancies") if isinstance(parsed.get("discrepancies"), list) else []
        if not problems:
            continue
        _mark(ctx, "method_pushback", {"method": name, "discrepancies": len(problems)})
        listed = "; ".join(json.dumps(p, default=str)[:200] for p in problems[:6])
        return {"action": "feedback", "message": (f"[method] {name} read back what this attempt wrote and found: "
                                                  f"{listed}. Check these before you finish.")[:MESSAGE_CHARS]}
    return {"action": "continue"}
