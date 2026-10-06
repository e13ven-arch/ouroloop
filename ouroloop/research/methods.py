"""v0.5 method library, the host's side (docs/design/V0_5_PLAN.md §1.1–§1.3; V0_5_METHOD_LIBRARY.md §2–§4).

A method is a package tool named `m_<name>`: read-only, deterministic, evidence with provenance, declared in its
module's `METHODS` literal (family, trigger, kinds, on, footprint, at most one context feature, at most four
variants). Nothing here imports a package's code: declarations and the cell table are read with `ast`, and anything
that runs R's code — a trigger, a method — runs in the isolated worker.

What lives here:
- the kit (`method_kit/`): `kit_complete` gives a candidate with an `m_` tool the router's bytes, the table and the
  router's hooks before its draft check, replacing R's edits to either kit file and saying so; `kit_check` refuses a
  table or a declaration outside the contract;
- the reach check (`reach_check`): recorded train events through the new methods' triggers, and one run of each on a
  fresh train world through a read-only broker; counts back to R, the assertion values counted on the host;
- the applicability learner (`twin_files`, `observations`, `fit_table`, `refit_files`) with its receipts
  (`frozen_table_problems`, `propensity_problems`): explore twins on train only, paired Beta posteriors per cell,
  a frozen greedy table that ships as a candidate like any other;
- R's inputs (`method_telemetry`, `uncovered_clusters`, `research_moves`) and the playbook's citation check
  (`playbook_check`).

Every rule is switched on by K's `methods` table (preset automationbench_harness_v5); a K without it never reaches
this module, so v0.4 activities run as they did.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import random
import re
from dataclasses import dataclass, fields
from pathlib import Path

KIT_DIR = Path(__file__).parent / "method_kit"
PREFIX = "m_"
ROUTER_PATH = "middlewares/m_router.py"
TABLE_PATH = "middlewares/m_table.py"
MLIB_PATH = "tools/_mlib.py"
REACH_PATH = "middlewares/_m_reach.py"
ROUTER_HOOKS = ({"name": "m_router_after", "site": "after_tool", "entrypoint": "middlewares/m_router.py:after"},
                {"name": "m_router_before", "site": "before_tool", "entrypoint": "middlewares/m_router.py:before"},
                {"name": "m_router_finish", "site": "on_finish", "entrypoint": "middlewares/m_router.py:finish"})
FAMILIES = ("search", "analysis", "statistics", "version", "verification")
ACTIONS = ("recommend", "inject", "block_once", "pushback")
FOOTPRINTS = ("body_contains", "not_contains", "value", "exists", "categorical")
DECL_KEYS = {"family", "trigger", "kinds", "on", "footprint", "split", "variants", "default_variant", "couples_with"}
KIND_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
FEATURE_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")


def kit_text(name: str) -> str:
    return (KIT_DIR / name).read_text(encoding="utf-8")


def router_source() -> str:
    return kit_text("m_router.py")


def mlib_source() -> str:
    return kit_text("mlib.py")


def table_stub() -> str:
    return kit_text("m_table.py")


def reach_source() -> str:
    return kit_text("reach.py")


# ---------------------------------------------------------------------------- what K fixes
@dataclass(frozen=True)
class MethodSettings:
    """K's `methods` table (preset automationbench_harness_v5; V0_5_PLAN §1.1, §1.3). Fixed by the preset: the
    config cannot change it, R cannot see past it."""
    method_calls: int = 25            # base calls one run of a method may make
    min_cell_tasks: int = 5           # triggered tasks a cell needs before it leaves its action
    p_decide: float = 0.8             # posterior probability a cell needs to move
    prior_weight: float = 0.5         # weight of earlier readings' counts (another base, another domain)
    max_variants: int = 4
    max_must_not: int = 1             # must-not failures a cell may carry on its on side and still turn on
    max_cost_rise: float = 0.25       # a cell turns on only if its triggered tasks cost at most this much more
    trigger_min_tasks: int = 2        # distinct train tasks a trigger must fire on in replay
    reach_floor: int = 1              # claimed failing train tasks a new method must fire and run on
    learn_max_tasks: int = 24         # train tasks a learn reading runs (the ones replay shows firing)
    unused_call_rate: float = 0.05    # an all-silent method the actor calls less than this is disabled by a refit

    def to_json(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_json(cls, d: dict | None) -> "MethodSettings":
        known = {f.name: f.type for f in fields(cls)}
        out = {}
        for k, v in (d or {}).items():
            if k not in known:
                raise ValueError(f"methods.{k}: not a method setting ({sorted(known)})")
            out[k] = float(v) if isinstance(getattr(cls, k), float) else int(v)
        return cls(**out)


# ---------------------------------------------------------------------------- reading a package
def literal_of(text: str, name: str):
    """The literal assigned to `name` at a module's top level (ast; never executed), or None."""
    try:
        tree = ast.parse(text or "")
    except (SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                return None
    return None


def manifest_of(files: dict) -> dict:
    try:
        m = json.loads(files.get("manifest.json") or "{}")
    except ValueError:
        return {}
    return m if isinstance(m, dict) else {}


def method_tools(files: dict) -> list:
    """The manifest's `m_` tool entries."""
    return [t for t in manifest_of(files).get("tools") or () if isinstance(t, dict)
            and str(t.get("name") or "").startswith(PREFIX)]


def has_methods(files: dict) -> bool:
    return bool(method_tools(files))


def methods_of(files: dict) -> dict:
    """{name: declaration + module + func + enabled} for every `m_` tool whose module declares it in METHODS."""
    out = {}
    for tool in method_tools(files):
        module, _, func = str(tool.get("entrypoint") or "").partition(":")
        decls = literal_of(files.get(module, ""), "METHODS")
        decl = decls.get(tool["name"]) if isinstance(decls, dict) else None
        if isinstance(decl, dict):
            out[tool["name"]] = {**decl, "module": module, "func": func, "enabled": tool.get("enabled", True)}
    return out


def table_of(files: dict) -> dict | None:
    t = literal_of(files.get(TABLE_PATH, ""), "TABLE")
    return t if isinstance(t, dict) else None


def render_table(table: dict) -> str:
    """The table file fixed code writes: a docstring and one literal, keys sorted, so equal tables are equal bytes."""
    ordered = {"mode": table.get("mode", "greedy"),
               **{k: table[k] for k in ("salt", "twin") if k in table},
               "cells": {k: dict(sorted(v.items())) for k, v in sorted((table.get("cells") or {}).items())},
               "splits": {k: dict(sorted(v.items())) for k, v in sorted((table.get("splits") or {}).items())}}
    return ('"""middlewares/m_table.py: the method table, written by fixed code only (docs/design/'
            'V0_5_METHOD_LIBRARY.md §2.5); read with ast.literal_eval, never imported."""\n'
            f"TABLE = {json.dumps(ordered, indent=1, sort_keys=False)}\n").replace(": true", ": True") \
        .replace(": false", ": False").replace(": null", ": None")


def default_cells(decls: dict, existing: dict | None = None) -> dict:
    """The cells of the declared methods: the existing cells of methods still present, and for a (method, kind)
    with no cell yet the method's declared `on` (and its default variant)."""
    existing = dict(existing or {})
    cells = {k: v for k, v in existing.items() if k.split("|", 1)[0] in decls}
    for name, decl in decls.items():
        on = decl.get("on") if decl.get("on") in ACTIONS else "recommend"
        for kind in decl.get("kinds") or ():
            if any(k.startswith(f"{name}|{kind}|") for k in cells):
                continue
            cell = {"action": on}
            if decl.get("variants"):
                cell["variant"] = decl.get("default_variant") or decl["variants"][0]
            cells[f"{name}|{kind}|*"] = cell
    return cells


# ---------------------------------------------------------------------------- kit completion and the kit check
def kit_complete(parent: dict, files: dict) -> tuple[dict, list[str]]:
    """The candidate's files with the kit in place (V0_5_METHOD_LIBRARY §2.5 “kit 补齐”), and notes for R. A
    candidate with an `m_` tool gets the router's bytes, the table (the parent's cells for methods still present, the
    declared `on` for new ones; R's edits replaced), `tools/_mlib.py` when it has none, and the three router hooks
    last in the manifest. A candidate with no `m_` tool left loses the router, the table and the hooks. A package
    the kit never touched comes back as it was."""
    files = dict(files)
    notes = []
    manifest = manifest_of(files)
    hooks = [h for h in manifest.get("hooks") or () if isinstance(h, dict)]
    kit_names = {h["name"] for h in ROUTER_HOOKS}
    if not has_methods(files):
        if ROUTER_PATH in files or TABLE_PATH in files or any(h.get("name") in kit_names for h in hooks):
            files.pop(ROUTER_PATH, None)
            files.pop(TABLE_PATH, None)
            manifest["hooks"] = [h for h in hooks if h.get("name") not in kit_names]
            files["manifest.json"] = json.dumps(manifest, sort_keys=True, indent=1)
            notes.append("no m_ tool is left: the router, its table and its hooks were removed")
        return files, notes
    router = router_source()
    if ROUTER_PATH in files and files[ROUTER_PATH] != router:
        notes.append(f"{ROUTER_PATH} is the kit's file and was replaced with the kit's bytes: the router is fixed")
    files[ROUTER_PATH] = router
    parent_table = table_of(parent) or {"mode": "greedy", "cells": {}, "splits": {}}
    given = files.get(TABLE_PATH)
    if given is not None and given != parent.get(TABLE_PATH) and table_of({TABLE_PATH: given}) != parent_table:
        notes.append(f"{TABLE_PATH} is written by fixed code from train rewards; the edit was replaced")
    decls = methods_of(files)
    table = {"mode": "greedy", "cells": default_cells(decls, parent_table.get("cells")),
             "splits": {k: v for k, v in (parent_table.get("splits") or {}).items() if k in decls}}
    files[TABLE_PATH] = render_table(table)
    if MLIB_PATH not in files:
        files[MLIB_PATH] = mlib_source()
    manifest["hooks"] = [h for h in hooks if h.get("name") not in kit_names] + [dict(h, enabled=True)
                                                                                 for h in ROUTER_HOOKS]
    files["manifest.json"] = json.dumps(manifest, sort_keys=True, indent=1)
    return files, notes


def kit_check(files: dict, settings: MethodSettings | None = None) -> list[str]:
    """The method contract on a completed candidate (V0_5_PLAN §1.1; V0_5_METHOD_LIBRARY §2.2, §4.4): the router is
    the kit's bytes, the table parses and is greedy with one action a cell, every cell names a declared method and
    kind and an action that method may take, every `m_` tool declares itself, and each declaration is within the
    caps. -> errors, empty when the candidate is within the contract or has no method."""
    settings = settings or MethodSettings()
    if not has_methods(files):
        return []
    errors = []
    if files.get(ROUTER_PATH) != router_source():
        errors.append(f"{ROUTER_PATH} is not the kit's router")
    decls = methods_of(files)
    for tool in method_tools(files):
        if tool["name"] not in decls:
            errors.append(f"{tool['name']}: its module {str(tool.get('entrypoint')).partition(':')[0]} declares no "
                          f"METHODS entry for it (a literal dict, read without running the module)")
    for name, decl in decls.items():
        errors += [f"{name}: {e}" for e in declaration_errors(decl, settings)]
        module = files.get(decl["module"], "")
        for attr in (decl.get("trigger"), decl.get("func")):
            if isinstance(attr, str) and not re.search(rf"^def {re.escape(attr)}\(", module, re.MULTILINE):
                errors.append(f"{name}: no top-level function {attr!r} in {decl['module']}")
    table = table_of(files)
    if table is None:
        errors.append(f"{TABLE_PATH} does not hold a TABLE literal")
        return errors
    if table.get("mode") != "greedy":
        errors.append(f"{TABLE_PATH}: mode {table.get('mode')!r}; a package's table is greedy (explore tables exist "
                      f"only in the learn reading's twins)")
    for key, cell in (table.get("cells") or {}).items():
        parts = str(key).split("|")
        if len(parts) != 3 or not isinstance(cell, dict):
            errors.append(f"cell {key!r}: a key is <method>|<kind>|<bucket> and a cell an object")
            continue
        name, kind, _bucket = parts
        decl = decls.get(name)
        if decl is None:
            errors.append(f"cell {key!r} names no declared method")
            continue
        if kind not in (decl.get("kinds") or ()):
            errors.append(f"cell {key!r}: {kind!r} is not one of {name}'s kinds")
        if cell.get("action") not in ("silent", decl.get("on")):
            errors.append(f"cell {key!r}: action {cell.get('action')!r} is neither silent nor {name}'s on")
        if "variant" in cell and cell["variant"] not in (decl.get("variants") or ()):
            errors.append(f"cell {key!r}: variant {cell['variant']!r} is not declared")
    return errors


def declaration_errors(decl: dict, settings: MethodSettings) -> list[str]:
    out = []
    unknown = sorted(set(decl) - DECL_KEYS - {"module", "func", "enabled"})
    if unknown:
        out.append(f"METHODS takes {sorted(DECL_KEYS)}, not {unknown}")
    if decl.get("family") not in FAMILIES:
        out.append(f"family {decl.get('family')!r}: one of {list(FAMILIES)}")
    if decl.get("on") not in ACTIONS:
        out.append(f"on {decl.get('on')!r}: one of {list(ACTIONS)}")
    kinds = decl.get("kinds")
    if not isinstance(kinds, (list, tuple)) or not kinds or not all(isinstance(k, str) and KIND_NAME.fullmatch(k)
                                                                    for k in kinds):
        out.append("kinds is a non-empty list of trigger kind names (lowercase, digits, '_')")
    if not isinstance(decl.get("trigger"), str):
        out.append("trigger names the module's trigger function")
    footprint = decl.get("footprint")
    if not isinstance(footprint, (list, tuple)) or not footprint or not set(footprint) <= set(FOOTPRINTS):
        out.append(f"footprint is a non-empty list of {list(FOOTPRINTS)}")
    split = decl.get("split")
    if split is not None and not (isinstance(split, str) and FEATURE_NAME.fullmatch(split)):
        out.append("split is at most one context feature name (a string)")
    variants = decl.get("variants")
    if variants is not None:
        if not isinstance(variants, (list, tuple)) or not all(isinstance(v, str) for v in variants) \
                or len(set(variants)) != len(variants):
            out.append("variants is a list of distinct names")
        elif len(variants) > settings.max_variants:
            out.append(f"{len(variants)} variants: at most {settings.max_variants}")
        elif decl.get("default_variant") is not None and decl["default_variant"] not in variants:
            out.append("default_variant is one of the variants")
    couples = decl.get("couples_with")
    if couples is not None and not (isinstance(couples, (list, tuple)) and all(isinstance(c, str) for c in couples)):
        out.append("couples_with is a list of method names")
    return out


def changed_methods(parent: dict, files: dict) -> list[str]:
    """The methods a candidate adds or changes: a new name, or a module whose bytes differ from the parent's."""
    before, after = methods_of(parent), methods_of(files)
    out = []
    for name, decl in after.items():
        old = before.get(name)
        if old is None or files.get(decl["module"]) != parent.get(old["module"]) \
                or {k: v for k, v in decl.items() if k != "enabled"} != {k: v for k, v in old.items()
                                                                         if k != "enabled"}:
            out.append(name)
    return sorted(out)


# ---------------------------------------------------------------------------- recorded events and the reach check
EVENT_OUTPUT = 1500
EVENT_ARGS = 1000
MAX_EVENTS = 150


def replay_events(transcript: list[dict]) -> tuple[list[dict], str]:
    """(the tool calls of one recorded attempt as the reach harness replays them, its final message), clipped so one
    attempt fits the protocol line. Calls of the actor and of its package tools alike: what the broker saw."""
    from .api_errors import _calls
    calls = _calls(transcript)[:MAX_EVENTS]
    out = []
    for c in calls:
        args = {k: (v[:EVENT_ARGS] if isinstance(v, str) else v) for k, v in (c.get("args") or {}).items()}
        out.append({"tool": c["tool"], "args": args, "output": (c.get("output") or "")[:EVENT_OUTPUT],
                    "is_error": bool(c.get("is_error"))})
    final = next((str(e.get("text") or "") for e in reversed(transcript) if e.get("kind") == "assistant"), "")
    return out, final[:4000]


def assertion_values(assertions) -> list[str]:
    """The strings and numbers of a task's assertion params: what the reach check counts in a method's output (only
    the count is reported)."""
    values = set()

    def walk(v) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            values.add(str(v))
        elif isinstance(v, str) and 3 <= len(v.strip()) <= 200:
            values.add(v.strip())
    for a in assertions or ():
        walk({k: v for k, v in dict(a).items() if k not in ("type", "scored", "excluded")})
    return sorted(values)


def values_in(text: str, values) -> int:
    low = (text or "").lower()
    return sum(1 for v in values if v and v.lower() in low)


def reach_harness_files(files: dict) -> dict:
    """The check session's files: the candidate's run files and the reach harness (never part of a package)."""
    return {**{p: t for p, t in files.items() if not p.startswith("tests/")}, REACH_PATH: reach_source()}


def reach_check(worker, files: dict, names, *, attempts, predicted, world_of, values_of, task_text_of,
                settings: MethodSettings | None = None) -> tuple[list[str], dict]:
    """The reach check (V0_5_METHOD_LIBRARY §2.7, §4.4) for the methods `names` of a completed candidate.

    `attempts` are the recorded train attempts [{task, events, final}] (every one, failing and passing): each is
    replayed through the triggers alone, and a method must fire on at least `trigger_min_tasks` distinct tasks.
    `predicted` are the candidate's claimed failing train tasks with a recorded attempt: on each, the trigger replay
    runs the method once at its first firing, on a fresh world of that train task (`world_of(task)`) through a
    read-only broker. The floor: at least `reach_floor` claimed tasks where the method fired and ran without error.
    -> (errors, report). The report gives R counts per task — fired, ran, error, output length, how many of the
    task's assertion values the output holds (`values_of(task)`, counted here, never sent to the worker) — and
    those counts are never a target: nothing ranks or admits by them."""
    from ..agent.worker import Broker, PackageCallError
    from .session import _safe
    settings = settings or MethodSettings()
    decls = {n: d for n, d in methods_of(files).items() if n in set(names)}
    if not decls:
        return [], {}
    manifest = manifest_of(files)
    package = {"tools": [dict(t) for t in manifest.get("tools") or () if t.get("enabled", True)],
               "hooks": [dict(h) for h in manifest.get("hooks") or () if h.get("enabled", True)]}
    wire = {n: {k: d.get(k) for k in ("module", "func", "trigger", "kinds", "on", "default_variant")}
            for n, d in decls.items()}
    session = worker.start()
    report = {n: {"fired_tasks": [], "claimed": {}} for n in decls}
    errors = []
    try:
        session.load(reach_harness_files(files))
        for a in attempts:
            try:
                value = session.request({"op": "hook", "entry": f"{REACH_PATH}:replay",
                                         "event": {"methods": wire, "events": a["events"], "task": task_text_of(a["task"]),
                                                   "final_message": a.get("final", ""), "package": package,
                                                   "run": False},
                                         "view": {}})
            except PackageCallError as e:
                return [f"reach_check: the harness failed on a recorded attempt: {str(e)[:200]}"], report
            for n, r in ((value or {}).get("methods") or {}).items():
                if n in report and r.get("fired") and a["task"] not in report[n]["fired_tasks"]:
                    report[n]["fired_tasks"].append(a["task"])
        for task in predicted:
            recorded = next((a for a in attempts if a["task"] == task), None)
            if recorded is None:
                continue
            world = world_of(task)
            try:
                value = session.request({"op": "hook", "entry": f"{REACH_PATH}:replay",
                                         "event": {"methods": wire, "events": recorded["events"],
                                                   "task": task_text_of(task), "final_message": recorded.get("final", ""),
                                                   "package": package, "run": True,
                                                   "max_calls": settings.method_calls},
                                         "view": {}}, broker=Broker(world, read_only=True))
            except PackageCallError as e:
                value = {"methods": {n: {"fired": False, "error": f"harness: {str(e)[:200]}"} for n in decls}}
            finally:
                closer = getattr(world, "close", None)
                if callable(closer):
                    closer()
            values = values_of(task)
            for n, r in ((value or {}).get("methods") or {}).items():
                if n not in report:
                    continue
                output = str(r.get("output") or "")
                report[n]["claimed"][task] = {"fired": bool(r.get("fired")), "kind": r.get("kind"),
                                              "ran": bool(r.get("ran")),
                                              "error": _safe(r.get("error"))[:200] if r.get("error") else None,
                                              "output_chars": len(output), "calls": int(r.get("calls") or 0),
                                              "assertion_values_in_output": values_in(output, values)}
    finally:
        session.close()
    for n, r in report.items():
        reached = [t for t, c in r["claimed"].items() if c["fired"] and c["ran"] and not c["error"]]
        r["reached"] = len(reached)
        if len(r["fired_tasks"]) < settings.trigger_min_tasks:
            errors.append(f"reach_check: {n}'s trigger fires on {len(r['fired_tasks'])} recorded train task(s); a "
                          f"method's trigger must fire on at least {settings.trigger_min_tasks} different train tasks "
                          f"(one task only is a per-task policy in another form)")
        if len(reached) < settings.reach_floor:
            detail = "; ".join(f"{t}: fired {c['fired']}, ran {c['ran']}" + (f", error {c['error']}" if c["error"]
                                                                              else "")
                               for t, c in list(r["claimed"].items())[:6]) or "no claimed task has a recorded attempt"
            errors.append(f"reach_check: {n} fired and ran without error on {len(reached)} of its claimed failing "
                          f"train tasks; the floor is {settings.reach_floor} ({detail})")
    return errors, report


# ---------------------------------------------------------------------------- the learner: twins and observations
def task_digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest(), 16)


def assign(salt: str, task: str, group: str, kind: str, twin: str) -> int:
    """The router's antithetic switch (m_router.assign), mirrored: the bit twin A takes; B takes 1 - bit."""
    bit = _h(salt, task, group, kind) % 2
    return 1 - bit if twin == "B" else bit


def learn_salt(order_seed: int, stage: str) -> str:
    return f"learn:{order_seed}:{stage}"


def twin_files(files: dict, *, salt: str, twin: str) -> dict:
    """An explore twin of a package: the same files with the table in explore mode under `salt` and the twin's
    label. Built by fixed code for a learn reading only; a dev or test run refuses it at assembly."""
    if twin not in ("A", "B"):
        raise ValueError("a twin is A or B")
    table = table_of(files) or {"cells": {}, "splits": {}}
    return {**files, TABLE_PATH: render_table({**table, "mode": "explore", "salt": salt, "twin": twin})}


def frozen_table_problems(files: dict) -> list[str]:
    """Why a package may not run on dev or test (V0_5_METHOD_LIBRARY §3.7 receipt 1): a table that is not greedy,
    or a cell without one action."""
    if TABLE_PATH not in files:
        return []
    table = table_of(files)
    if table is None:
        return [f"{TABLE_PATH} does not parse"]
    problems = []
    if table.get("mode", "greedy") != "greedy" or "salt" in table or "twin" in table:
        problems.append(f"{TABLE_PATH} is an explore table (mode {table.get('mode')!r}): only a learn reading on train "
                        f"runs one")
    for key, cell in (table.get("cells") or {}).items():
        if not isinstance(cell, dict) or not isinstance(cell.get("action"), str):
            problems.append(f"cell {key!r} has no single action")
    return problems


def decisions_of(transcript: list[dict]) -> list[dict]:
    """The `method_decision` marks of one attempt, as the host recorded them, with the trigger kind under `kind`
    again (the mark carries it as `trigger_kind`: a record's own `kind` is the mark's)."""
    return [{**{k: v for k, v in e.items() if k not in ("kind", "trigger_kind")}, "kind": e.get("trigger_kind")}
            for e in transcript if e.get("kind") == "method_decision"]


def propensity_problems(transcript: list[dict]) -> list[str]:
    """Receipt 2 (§3.7): a dev or test attempt whose method decisions are not all greedy (propensity 1)."""
    return [f"{d.get('method')}|{d.get('kind')}: propensity {d.get('propensity')}" for d in decisions_of(transcript)
            if float(d.get("propensity", 1.0) or 0) != 1.0 or d.get("mode") not in (None, "greedy")]


def assertion_class(type_name) -> str:
    """The footprint class of an assertion type (V0_5_METHOD_LIBRARY §2.3), by its name; adapters.automationbench
    re-exports it."""
    name = str(type_name or "").lower()
    if "_not_" in name or name.endswith("_not") or "not_exists" in name:
        return "not_contains"
    if "contains" in name or "body" in name or "subject" in name or "content" in name:
        return "body_contains"
    if any(w in name for w in ("status", "stage", "label", "tag", "state", "priority", "category", "paused",
                               "active", "voided", "cancel", "archived", "is_read", "in_trash")):
        return "categorical"
    if any(w in name for w in ("equals", "value", "field", "count", "amount", "total", "has_")):
        return "value"
    return "exists"


def footprint_score(assertions, footprint) -> float | None:
    """The pass fraction of an attempt's unexcluded assertions in the method's footprint classes; None if it has
    none."""
    wanted = set(footprint or ())
    scored = [a for a in assertions or () if not a.get("excluded") and assertion_class(a.get("type")) in wanted]
    if not scored:
        return None
    return sum(1 for a in scored if a.get("passed")) / len(scored)


def must_not_failures(assertions) -> set:
    return {json.dumps({"type": a.get("type"), "params": a.get("params")}, sort_keys=True, default=str)
            for a in assertions or () if not a.get("excluded") and "_not_" in str(a.get("type") or "")
            and not a.get("passed")}


def observations(pairs, decls: dict) -> list[dict]:
    """Per (task, decision) the paired outcome of a learn reading's twins (V0_5_METHOD_LIBRARY §3.3–§3.4).

    `pairs` is [{task, A: attempt, B: attempt}], an attempt {success, cost, assertions, decisions}. A decision is
    a (method, kind); a twin where it never fired acted as silent there. Only decisions that fired in at least one
    twin are observed (a task where neither fired carries no information). -> [{task, method, kind, cell, bucket,
    context, on: twin, silent: twin, r_task: (on, silent), r_fp: (on, silent), must_not: n, cost: (on, silent),
    variant: (on variant, silent variant) when both twins acted}]."""
    out = []
    for p in pairs:
        a, b = p.get("A"), p.get("B")
        if not a or not b:
            continue
        keys = {(d["method"], d["kind"]) for d in [*a.get("decisions", ()), *b.get("decisions", ())]
                if d.get("method") in decls}
        for method, kind in sorted(keys):
            da = next((d for d in a.get("decisions", ()) if (d["method"], d["kind"]) == (method, kind)), None)
            db = next((d for d in b.get("decisions", ()) if (d["method"], d["kind"]) == (method, kind)), None)
            act_a = da is not None and da.get("action") != "silent"
            act_b = db is not None and db.get("action") != "silent"
            if act_a == act_b:
                if act_a and da.get("variant") != db.get("variant"):
                    out.append({"task": p["task"], "method": method, "kind": kind, "variant_pair": True,
                                "cell": (da or db).get("cell"), "bucket": (da or db).get("bucket"),
                                "variants": (da.get("variant"), db.get("variant")),
                                "r_task": (int(bool(a["success"])), int(bool(b["success"]))),
                                "r_fp": (footprint_score(a["assertions"], decls[method].get("footprint")),
                                         footprint_score(b["assertions"], decls[method].get("footprint")))})
                continue
            on, silent = (a, b) if act_a else (b, a)
            d = da if act_a else db
            fp = decls[method].get("footprint")
            out.append({"task": p["task"], "method": method, "kind": kind, "cell": d.get("cell"),
                        "bucket": d.get("bucket"), "context": d.get("context") or {},
                        "r_task": (int(bool(on["success"])), int(bool(silent["success"]))),
                        "r_fp": (footprint_score(on["assertions"], fp), footprint_score(silent["assertions"], fp)),
                        "must_not": len(must_not_failures(on["assertions"]) - must_not_failures(silent["assertions"])),
                        "cost": (on.get("cost"), silent.get("cost"))})
    return out


# ---------------------------------------------------------------------------- the learner: fitting
def _beta_prob_above_half(a: float, b: float, draws: int = 4000, seed: int = 0) -> float:
    """P(p > 1/2) for p ~ Beta(a, b), by a fixed-seed Monte Carlo (deterministic, so a fit is reproducible)."""
    rng = random.Random(seed)
    return sum(1 for _ in range(draws) if rng.betavariate(a, b) > 0.5) / draws


def _counts(obs) -> dict:
    """k (footprint up), m (down), fixes, regressions, must-not, n triggered tasks, mean costs."""
    k = m = fixes = regs = must = 0
    tasks, cost_on, cost_off = set(), [], []
    for o in obs:
        tasks.add(o["task"])
        on, off = o["r_fp"]
        if on is not None and off is not None:
            k += on > off
            m += on < off
        fixes += o["r_task"] == (1, 0)
        regs += o["r_task"] == (0, 1)
        must += int(o.get("must_not") or 0)
        c_on, c_off = (o.get("cost") or (None, None))
        if c_on is not None and c_off is not None:
            cost_on.append(float(c_on))
            cost_off.append(float(c_off))
    rise = None
    if cost_on and sum(cost_off) > 0:
        rise = sum(cost_on) / sum(cost_off) - 1.0
    return {"k": k, "m": m, "fixes": fixes, "regressions": regs, "must_not": must, "n": len(tasks),
            "cost_rise": None if rise is None else round(rise, 4)}


def cell_rule(counts: dict, current: str, on: str, settings: MethodSettings, prior: dict | None = None) -> dict:
    """One switch cell's posterior and the action the freeze rule gives it (V0_5_METHOD_LIBRARY §3.5)."""
    prior = prior or {}
    w = settings.prior_weight
    a = 1 + counts["k"] + w * float(prior.get("k", 0))
    b = 1 + counts["m"] + w * float(prior.get("m", 0))
    p_up = _beta_prob_above_half(a, b)
    action, why = current, "kept: below the evidence the rule needs"
    enough = counts["n"] >= settings.min_cell_tasks
    if counts["must_not"] >= 2:
        action, why = "silent", f"{counts['must_not']} must-not failures on its on side"
    elif enough and current != on and p_up >= settings.p_decide and counts["fixes"] - counts["regressions"] >= 0 \
            and counts["must_not"] <= settings.max_must_not \
            and (counts["cost_rise"] is None or counts["cost_rise"] <= settings.max_cost_rise):
        action, why = on, f"P(helps) {p_up:.2f} on {counts['n']} tasks"
    elif enough and current != "silent" and (1 - p_up) >= settings.p_decide:
        action, why = "silent", f"P(hurts) {1 - p_up:.2f} on {counts['n']} tasks"
    return {**counts, "alpha": round(a, 3), "beta": round(b, 3), "mean": round(a / (a + b), 4),
            "p_helps": round(p_up, 4), "interval90": _interval(a, b), "action": action, "why": why}


def _interval(a: float, b: float, draws: int = 4000, seed: int = 1) -> list:
    rng = random.Random(seed)
    xs = sorted(rng.betavariate(a, b) for _ in range(draws))
    return [round(xs[int(0.05 * draws)], 4), round(xs[int(0.95 * draws) - 1], 4)]


def _variant_rule(obs, current: str, settings: MethodSettings) -> tuple[str, dict]:
    """A variant replaces the current one with n >= min_cell_tasks tasks where the two differ and P(better) >=
    p_decide (§3.5 变体格)."""
    best, info = current, {}
    by = {}
    for o in obs:
        va, vb = o["variants"]
        fa, fb = o["r_fp"]
        if fa is None or fb is None or va == vb:
            continue
        for v, mine, other in ((va, fa, fb), (vb, fb, fa)):
            rival = vb if v == va else va
            if current not in (v, rival) or v == current:
                continue
            s = by.setdefault(v, {"k": 0, "m": 0, "tasks": set()})
            s["k"] += mine > other
            s["m"] += mine < other
            s["tasks"].add(o["task"])
    top = 0.0
    for v, s in sorted(by.items()):
        p = _beta_prob_above_half(1 + s["k"], 1 + s["m"])
        info[v] = {"n": len(s["tasks"]), "k": s["k"], "m": s["m"], "p_better": round(p, 4)}
        if len(s["tasks"]) >= settings.min_cell_tasks and p >= settings.p_decide and p > top:
            best, top = v, p
    return best, info


def _split_candidates(obs, feature: str) -> list[float]:
    values = sorted({float(o["context"][feature]) for o in obs if isinstance(o.get("context"), dict)
                     and isinstance(o["context"].get(feature), (int, float))})
    if "day" in feature:
        return sorted({7.0 * math.ceil(v / 7.0) for v in values if v > 0})
    return sorted({math.ceil(v) for v in values if v > min(values, default=0)})


def fit_table(obs, decls: dict, table: dict | None, settings: MethodSettings | None = None,
              prior: dict | None = None) -> tuple[dict, dict]:
    """The greedy table the observations support (V0_5_METHOD_LIBRARY §3.5), and the fit's record.

    Per (method, kind) the switch rule on the cell `*`; a split on the method's declared feature is kept only if each
    side has min_cell_tasks triggered tasks and the rule gives the two sides different actions; variants by their own
    rule. `prior` is {cell key: {k, m}} from earlier readings, at prior_weight. A cell with too little evidence keeps
    its current action. -> (table, {cell key: posterior and why})."""
    settings = settings or MethodSettings()
    table = dict(table or {"mode": "greedy", "cells": {}, "splits": {}})
    cells = default_cells(decls, table.get("cells"))
    splits = {k: v for k, v in (table.get("splits") or {}).items() if k in decls}
    record = {}
    switch = [o for o in obs if not o.get("variant_pair") and o["method"] in decls]
    for name, decl in sorted(decls.items()):
        on = decl.get("on") if decl.get("on") in ACTIONS else "recommend"
        for kind in decl.get("kinds") or ():
            mine = [o for o in switch if o["method"] == name and o["kind"] == kind]
            star = f"{name}|{kind}|*"
            current = (cells.get(star) or {}).get("action", on)
            whole = cell_rule(_counts(mine), current, on, settings, (prior or {}).get(star))
            record[star] = whole
            cells[star] = {**cells.get(star, {}), "action": whole["action"]}
            feature = decl.get("split")
            if feature:
                for cut in _split_candidates(mine, feature):
                    low = [o for o in mine if float(o["context"].get(feature, -1e9)) < cut]
                    high = [o for o in mine if float(o["context"].get(feature, -1e9)) >= cut]
                    cl, ch = _counts(low), _counts(high)
                    if cl["n"] < settings.min_cell_tasks or ch["n"] < settings.min_cell_tasks:
                        continue
                    rl = cell_rule(cl, current, on, settings, (prior or {}).get(f"{name}|{kind}|{feature}=0"))
                    rh = cell_rule(ch, current, on, settings, (prior or {}).get(f"{name}|{kind}|{feature}=1"))
                    if rl["action"] != rh["action"]:
                        splits[name] = {"feature": feature, "cut": cut}
                        cells[f"{name}|{kind}|{feature}=0"] = {**cells[star], "action": rl["action"]}
                        cells[f"{name}|{kind}|{feature}=1"] = {**cells[star], "action": rh["action"]}
                        record[f"{name}|{kind}|{feature}=0"], record[f"{name}|{kind}|{feature}=1"] = rl, rh
                        break
            if decl.get("variants"):
                pairs = [o for o in obs if o.get("variant_pair") and o["method"] == name and o["kind"] == kind]
                current_v = (cells.get(star) or {}).get("variant") or decl.get("default_variant") \
                    or decl["variants"][0]
                chosen, info = _variant_rule(pairs, current_v, settings)
                for key in [k for k in cells if k.startswith(f"{name}|{kind}|")]:
                    cells[key] = {**cells[key], "variant": chosen}
                record[star]["variants"] = info
    fitted = {"mode": "greedy", "cells": cells, "splits": splits}
    return fitted, record


def table_changed(old: dict | None, new: dict) -> bool:
    keep = ("cells", "splits")
    return {k: (old or {}).get(k) or {} for k in keep} != {k: new.get(k) or {} for k in keep}


def refit_files(files: dict, table: dict, *, call_rates: dict | None = None,
                settings: MethodSettings | None = None) -> tuple[dict, list[str]]:
    """The refit candidate's files (§3.6): only the table changes, and an `m_` tool whose every cell is now silent
    and that the actor called on its own in fewer than `unused_call_rate` of the attempts where it was offered is
    set `enabled: false` (it stays in the package; enabling it again is a candidate of its own). -> (files, the
    disabled tools)."""
    settings = settings or MethodSettings()
    out = {**files, TABLE_PATH: render_table({**table, "mode": "greedy"})}
    disabled = []
    manifest = manifest_of(out)
    rates = call_rates or {}
    for tool in manifest.get("tools") or ():
        name = str(tool.get("name") or "")
        if not name.startswith(PREFIX) or not tool.get("enabled", True):
            continue
        cells = [c for k, c in (table.get("cells") or {}).items() if k.split("|", 1)[0] == name]
        if cells and all(c.get("action") == "silent" for c in cells) \
                and float(rates.get(name, 0.0)) < settings.unused_call_rate:
            tool["enabled"] = False
            disabled.append(name)
    if disabled:
        out["manifest.json"] = json.dumps(manifest, sort_keys=True, indent=1)
    return out, disabled


def all_on_files(files: dict) -> dict:
    """The “table all on” arm (§7.3): every cell at its method's declared `on`."""
    decls = methods_of(files)
    table = table_of(files) or {"cells": {}, "splits": {}}
    cells = {k: {**c, "action": decls[k.split("|", 1)[0]].get("on")} for k, c in (table.get("cells") or {}).items()
             if k.split("|", 1)[0] in decls}
    return {**files, TABLE_PATH: render_table({**table, "mode": "greedy", "cells": cells})}


# ---------------------------------------------------------------------------- R's inputs
def call_rates(rows_with_transcripts, names) -> dict:
    """name -> the share of attempts in which the actor itself called the method, among the attempts that offered it."""
    offered, called = {n: 0 for n in names}, {n: 0 for n in names}
    for _row, transcript in rows_with_transcripts:
        tools = {e.get("tool") for e in transcript if e.get("kind") == "package_tool"}
        for n in names:
            offered[n] += 1
            called[n] += n in tools
    return {n: (called[n] / offered[n] if offered[n] else 0.0) for n in names}


def method_telemetry(rows_with_transcripts, files: dict, fits: list | None = None) -> dict:
    """inputs/methods.json (V0_5_METHOD_LIBRARY §4.2), from train attempts only: per installed method its calls,
    errors, mean output length, the share of outputs with a non-empty `unclassified`; per cell the triggered tasks,
    the action taken, how often a recommendation was followed by a call, injections and blocks, and the latest fit's
    posterior and counts; and the train attempts where the method was called or acted and the task failed (for R to
    read with read_trajectory)."""
    decls = methods_of(files)
    table = table_of(files) or {}
    out = {}
    latest_fit = (fits or [{}])[-1].get("cells", {}) if fits else {}
    for name, decl in sorted(decls.items()):
        calls = errors = unclassified = 0
        chars = []
        cells: dict = {}
        cases = []
        for row, transcript in rows_with_transcripts:
            if getattr(row, "split", "train") != "train":
                continue
            used = False
            for e in transcript:
                if e.get("kind") == "package_tool" and e.get("tool") == name:
                    calls += 1
                    errors += bool(e.get("is_error"))
                    text = str(e.get("output") or "")
                    chars.append(len(text))
                    try:
                        parsed = json.loads(text)
                        unclassified += bool(isinstance(parsed, dict) and parsed.get("unclassified"))
                    except ValueError:
                        pass
                    used = True
                elif e.get("kind") == "method_decision" and e.get("method") == name:
                    c = cells.setdefault(e.get("cell") or f"{name}|{e.get('trigger_kind')}|*",
                                         {"tasks": set(), "acted": 0, "silent": 0, "followed": 0})
                    c["tasks"].add(row.task_id)
                    c["acted" if e.get("action") != "silent" else "silent"] += 1
                    if e.get("action") == "recommend" and any(x.get("kind") == "package_tool" and x.get("tool") == name
                                                              for x in transcript):
                        c["followed"] += 1
                    used = used or e.get("action") != "silent"
            if used and not getattr(row, "success", True):
                cases.append(getattr(row, "attempt_id", ""))
        out[name] = {"family": decl.get("family"), "on": decl.get("on"), "kinds": decl.get("kinds"),
                     "version": hashlib.sha256(files.get(decl["module"], "").encode()).hexdigest()[:12],
                     "enabled": decl.get("enabled", True), "calls": calls, "error_rate":
                         round(errors / calls, 3) if calls else None,
                     "mean_output_chars": round(sum(chars) / len(chars)) if chars else None,
                     "unclassified_share": round(unclassified / calls, 3) if calls else None,
                     "cells": {k: {"triggered_tasks": len(v["tasks"]), "acted": v["acted"], "silent": v["silent"],
                                   "recommend_followed": v["followed"],
                                   "action": ((table.get("cells") or {}).get(k) or {}).get("action"),
                                   **({"posterior": {x: latest_fit[k][x] for x in ("mean", "interval90", "n", "k", "m",
                                                                                   "fixes", "regressions", "must_not")
                                                     if x in latest_fit[k]}} if k in latest_fit else {})}
                               for k, v in sorted(cells.items())},
                     "failing_attempts_where_used": cases[:12]}
    return out


BEHAVIOURS = ("http_401_then_no_other_source", "empty_search_then_stop", "values_read_without_tab",
              "write_without_readback", "system_clock_as_today", "other")


def behaviour_of(events: list[dict], task_text: str) -> str:
    """The behaviour cluster of one failing train attempt (V0_5_METHOD_LIBRARY §4.2 uncovered.json), from its
    recorded calls only."""
    def url(e):
        return str((e.get("args") or {}).get("url") or "")
    for i, e in enumerate(events):
        out = str(e.get("output") or "")
        if e.get("tool") == "api_fetch" and ("401" in out or "not connected" in out.lower()):
            later = [url(x) for x in events[i + 1:] if x.get("tool") == "api_fetch"]
            if not any(any(h in u for h in ("drive", "sheets", "files")) for u in later):
                return "http_401_then_no_other_source"
    writes = [e for e in events if e.get("tool") == "api_fetch"
              and str((e.get("args") or {}).get("method") or "GET").upper() not in ("GET", "HEAD")]
    for e in events:
        out = str(e.get("output") or "").strip()
        if e.get("tool") == "api_fetch" and ("search" in url(e) or "?q=" in url(e)) and out in ("[]", "{}", '{"files": []}'):
            return "empty_search_then_stop"
    if any("sheets" in url(e) and "/values/" in url(e) and "!" not in url(e) for e in events):
        return "values_read_without_tab"
    if writes and not any(e.get("tool") == "api_fetch" and str((e.get("args") or {}).get("method") or "GET").upper()
                          == "GET" and any(url(e).split("?")[0] == url(w).split("?")[0] for w in writes)
                          for e in events):
        return "write_without_readback"
    if re.search(r"\b(today|this week|last month|overdue|aging|days? ago)\b", task_text or "", re.IGNORECASE):
        return "system_clock_as_today"
    return "other"


ASSERTION_CLASSES = ("wrong_value", "leaked_excluded", "missing_write", "over_action", "wrong_category")


def failure_class(assertions) -> str:
    """The failed-assertion class of an attempt (§4.2): the most frequent among its failed unexcluded assertions."""
    counts = {}
    for a in assertions or ():
        if a.get("excluded") or a.get("passed"):
            continue
        c = assertion_class(a.get("type"))
        name = {"not_contains": "over_action", "body_contains": "wrong_value", "value": "wrong_value",
                "categorical": "wrong_category", "exists": "missing_write"}[c]
        counts[name] = counts.get(name, 0) + 1
    return max(sorted(counts), key=lambda k: counts[k]) if counts else "missing_write"


def uncovered_clusters(failing, files: dict, fits: list | None = None) -> list[dict]:
    """inputs/uncovered.json (§4.2): the failing train attempts of the current W, clustered by failed-assertion class
    x behaviour, each cluster with its size, attempt refs and whether a method covers it (a method whose cells acted
    in those attempts) and how that method's latest posterior reads. `failing` is [(row, transcript, assertions)]."""
    decls = methods_of(files)
    latest = (fits or [{}])[-1].get("cells", {}) if fits else {}
    clusters: dict = {}
    for row, transcript, assertions in failing:
        events, _ = replay_events(transcript)
        text = next((str(e.get("text") or "") for e in transcript if e.get("kind") == "task"), "")
        key = (failure_class(assertions), behaviour_of(events, text))
        c = clusters.setdefault(key, {"failed_class": key[0], "behaviour": key[1], "attempts": [], "covered_by": {}})
        c["attempts"].append(row.attempt_id)
        for d in decisions_of(transcript):
            if d.get("method") in decls and d.get("action") != "silent":
                c["covered_by"][d["method"]] = c["covered_by"].get(d["method"], 0) + 1
    out = []
    for c in clusters.values():
        covered = {m: {"attempts": n, "posterior_mean": max((latest[k]["mean"] for k in latest
                                                             if k.startswith(m + "|")), default=None)}
                   for m, n in c["covered_by"].items()}
        status = "no method covers it" if not covered else (
            "covered, posterior <= 0.5" if all((v["posterior_mean"] or 0.5) <= 0.5 for v in covered.values())
            else "covered")
        out.append({"failed_class": c["failed_class"], "behaviour": c["behaviour"], "size": len(c["attempts"]),
                    "attempts": c["attempts"][:8], "covered_by": covered, "status": status})
    return sorted(out, key=lambda c: (-c["size"], c["failed_class"], c["behaviour"]))


def research_moves(ledger_rows, fits: list | None = None) -> list[dict]:
    """inputs/method_research.json (§4.5 (a)): R's own method candidates by move (new, refine, narrow, retire) —
    proposed, admitted, first draft reaching the floor, revisions, screen fixes and regressions, gate, install —
    from the engine's ledger only."""
    moves: dict = {}
    for row in ledger_rows or ():
        decl = row.get("declaration") or {}
        if decl.get("carrier") != "tool" or not str(decl.get("site") or "").startswith(PREFIX):
            continue
        mech = str(decl.get("mechanism") or "").lower()
        move = "retire" if any(w in mech for w in ("retire", "remove", "disable")) else \
            "narrow" if any(w in mech for w in ("narrow", "trigger", "card")) else \
            "refine" if any(w in mech for w in ("refine", "fix", "improve")) else "new"
        m = moves.setdefault(move, {"move": move, "proposed": 0, "admitted": 0, "first_draft_ok": 0, "revisions": 0,
                                    "screen_fixes": 0, "screen_regressions": 0, "gated": 0, "installed": 0,
                                    "candidates": []})
        m["proposed"] += 1
        m["candidates"].append(row.get("candidate"))
        drafts = row.get("drafts") or []
        m["first_draft_ok"] += bool(drafts and drafts[0].get("ok"))
        m["revisions"] += max(len(drafts) - 1, 0)
        m["admitted"] += (row.get("admission") or {}).get("status") == "admitted"
        screen = row.get("screen") or {}
        m["screen_fixes"] += int(screen.get("fixes") or 0)
        m["screen_regressions"] += int(screen.get("sample_regressions") or 0)
        gate = row.get("gate") or {}
        m["gated"] += bool(gate)
        m["installed"] += gate.get("outcome") == "installed" or gate.get("installed") is True
    return [moves[k] for k in sorted(moves)]


# ---------------------------------------------------------------------------- the playbook (§4.5 (b))
CITATION = re.compile(r"\[([^\[\]:]+):\s*([a-z_]+)\s+(-?[\w.]+)\]\s*$")
PLAYBOOK_CHARS = 4000


def playbook_check(text: str, ledger: dict) -> tuple[str, list[str]]:
    """The lessons whose citation checks out against the ledger, and why each other one was dropped. A lesson is a
    line ending `[<candidate id>: <field> <value>]`; `ledger` is {candidate id: its ledger entry} (the projection R
    reads), and the field is read from the entry, its screen or its gate. -> (kept text, reasons)."""
    kept, dropped = [], []
    for line in str(text or "").splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        m = CITATION.search(line)
        if not m:
            dropped.append(f"no citation: {line[:80]!r}")
            continue
        cand, fld, value = m.group(1).strip(), m.group(2), m.group(3)
        entry = ledger.get(cand)
        if entry is None:
            dropped.append(f"{cand}: no such candidate in the ledger")
            continue
        found = None
        for source in (entry, entry.get("screen") or {}, entry.get("gate") or {}, entry.get("admission") or {}):
            if isinstance(source, dict) and fld in source:
                found = source[fld]
                break
        if found is None or str(found) != value:
            dropped.append(f"{cand}: {fld} is {found!r} in the ledger, not {value!r}")
            continue
        kept.append(line)
    text = "\n".join(kept)
    if len(text) > PLAYBOOK_CHARS:
        dropped.append(f"the playbook is cut to {PLAYBOOK_CHARS} characters")
        text = text[:PLAYBOOK_CHARS].rsplit("\n", 1)[0]
    return text, dropped


# ---------------------------------------------------------------------------- the kit R is shown, and the reference set
KIT_README = """# The method kit (read-only)

A method is a read-only package tool named m_<name> (V0_5 method library). Copy `_mlib.py` into your candidate as
tools/_mlib.py if it has none, write the method in tools/m_<family>.py with a METHODS literal, add its manifest entry
(name, a card as its description: USE WHEN / INPUT / OUTPUT / FAILS WHEN / NOT FOR, input_schema, entrypoint), and
tests on synthetic data in tests/test_m_<family>.py (see template_test.py).

METHODS = {"m_name": {"family": "search|analysis|statistics|version|verification", "trigger": "<function name>",
           "kinds": ["<trigger kind>", ...], "on": "recommend|inject|block_once|pushback",
           "footprint": ["body_contains"|"not_contains"|"value"|"exists"|"categorical", ...],
           "split": "<one context feature>" (optional), "variants": [<= 4 names] (optional),
           "default_variant": "<one of them>" (optional), "couples_with": ["m_other"] (optional)}}

The trigger is a pure function trigger(event, view) -> None or {"kind", "args", "context"}: event is
{tool, args, result (at most 4000 chars), is_error, turn} after a call, {tool, args, turn} before one (a block_once
method), {tool: None, final_message} at finish (a pushback method); view has the task text and the last 20 calls.
The method is run(ctx, args) -> a JSON string with evidence and an `unclassified` list; it may make at most 25 base
calls, all read-only (a write is refused), and answers a wrong argument with {"output": "usage: ...", "is_error":
true}, never an exception.

m_router.py (here for reference) and m_table.py are put in the candidate by fixed code: the router runs your
triggers and acts by the table; the table is fitted from train rewards. Do not edit either; an edit is replaced.
"""


def method_kit_inputs() -> dict:
    """inputs/method_kit/: what R copies from (§4.2): the README, the router (read-only reference), the table stub,
    the _mlib template and the test template."""
    return {"method_kit/README.md": KIT_README, "method_kit/m_router.py": router_source(),
            "method_kit/m_table.py": table_stub(), "method_kit/_mlib.py": mlib_source(),
            "method_kit/template_test.py": kit_text("template_test.py")}


REFERENCE_CARDS = {
    "m_doc_search": ("[method] USE WHEN a policy, precedent or fact search came back empty, or a service answered 401. "
                     "INPUT query (words), stores (optional: files, wiki, mail, chat, sheets). OUTPUT per document "
                     "store the endpoint, its status and the hits (title, record id, source); the unreachable stores; "
                     "loose matches as `unclassified`. FAILS WHEN a store has no listing endpoint. NOT FOR reading one "
                     "known record. Read-only evidence: you still decide."),
    "m_row_ledger": ("[method] USE WHEN you will count, total or act on rows of a table that may hold filler or test "
                     "rows, per-row notes, or rows to skip. INPUT url (the table's GET url), params, key column, "
                     "amount_columns, variant (notes_only | notes_and_noise | notes_noise_empty). OUTPUT one line per "
                     "row: include|exclude + reason; counts and totals from included rows only; an `unclassified` "
                     "list of notes it could not read. FAILS WHEN notes use words outside its directive list. NOT FOR "
                     "single-record lookups. Read-only evidence: you still decide."),
    "m_policy_timeline": ("[method] USE WHEN two or more documents or messages on one policy were read. INPUT topic "
                          "(words), variant (replace | merge_supplement). OUTPUT the versions in date order with "
                          "current / superseded / supplement, and later messages that lift or override something; "
                          "undated hits as `unclassified`. FAILS WHEN documents carry no dates. NOT FOR deciding what "
                          "the policy allows. Read-only evidence: you still decide."),
    "m_outbound_lint": ("[method] USE WHEN an outbound mail, chat message or note, or a status or label write, is "
                        "about to go out after an analysis method decided something. INPUT tool, args (the call). "
                        "OUTPUT violations: excluded or superseded values in the body, computed totals or counts "
                        "missing from it, a categorical value the records do not use. FAILS WHEN nothing was decided "
                        "earlier (it then has nothing to check). NOT FOR judging tone. Read-only evidence."),
    "m_effect_audit": ("[method] USE WHEN the attempt wrote records and is about to finish. INPUT none. OUTPUT each "
                       "non-message write read back: a target that cannot be read, a written field that is not there. "
                       "FAILS WHEN a write's target has no GET. NOT FOR messages already sent. Read-only evidence."),
}
REFERENCE_MODULES = {"m_doc_search": ("tools/m_search.py", "doc_search"),
                     "m_row_ledger": ("tools/m_tables.py", "row_ledger"),
                     "m_policy_timeline": ("tools/m_policy.py", "policy_timeline"),
                     "m_outbound_lint": ("tools/m_verify.py", "outbound_lint"),
                     "m_effect_audit": ("tools/m_verify.py", "effect_audit")}
REFERENCE_SCHEMAS = {
    "m_doc_search": {"type": "object", "properties": {"query": {"type": "string"},
                                                      "stores": {"type": "array", "items": {"type": "string"}}},
                     "required": ["query"]},
    "m_row_ledger": {"type": "object", "properties": {"url": {"type": "string"}, "params": {"type": "object"},
                                                      "key": {"type": "string"},
                                                      "amount_columns": {"type": "array", "items": {"type": "string"}},
                                                      "variant": {"type": "string"}}, "required": ["url"]},
    "m_policy_timeline": {"type": "object", "properties": {"topic": {"type": "string"},
                                                           "variant": {"type": "string"}}, "required": ["topic"]},
    "m_outbound_lint": {"type": "object", "properties": {"tool": {"type": "string"}, "args": {"type": "object"}},
                        "required": ["tool", "args"]},
    "m_effect_audit": {"type": "object", "properties": {}},
}


def reference_files(names=None, *, base: dict | None = None) -> dict:
    """A package with the reference methods `names` (all by default) on top of `base`: their modules, `_mlib.py` and
    manifest entries. Not completed: kit_complete adds the router, the table and the hooks."""
    names = list(names or REFERENCE_MODULES)
    files = dict(base or {"manifest.json": json.dumps({"api_version": "harness_package.v2", "tools": [],
                                                       "hooks": [], "skills": [], "memory": {"enabled": False}})})
    manifest = manifest_of(files)
    tools = [t for t in manifest.get("tools") or () if t.get("name") not in names]
    for name in names:
        module, func = REFERENCE_MODULES[name]
        files[module] = kit_text(f"reference/{module.split('/')[-1]}")
        tools.append({"name": name, "description": REFERENCE_CARDS[name], "input_schema": REFERENCE_SCHEMAS[name],
                      "entrypoint": f"{module}:{func}"})
    files[MLIB_PATH] = files.get(MLIB_PATH) or mlib_source()
    manifest["tools"] = tools
    files["manifest.json"] = json.dumps(manifest, sort_keys=True, indent=1)
    return files
