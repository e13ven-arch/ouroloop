"""V0, the static rung (docs/design/IMPROVER.md §8): what can be told of a candidate without running it.

The package's shape and K's limits (harness.package.check_shape: the manifest, the sizes, the vendored packages'
protected files, the host-only memory left as the parent has it); the kinds and hook sites K allows; the literal check
on the lines changed against the seed (a value of one train task is that task's answer); the code a candidate adds
does not reach for a shell, the network, raw memory or pickles; the declaration says what the files change and the
files change what it says (both ways); the tasks it claims are train tasks R was given; the causes are the round's.
"""
from __future__ import annotations

import ast

from ...harness.kinds import MEMORY_PATH
from ...harness.literals import describe, findings
from ...harness.package import Limits, check_shape, diff
from ..components import components
from ..settings import L5_KINDS

FORBIDDEN_IMPORTS = ("socket", "subprocess", "ctypes", "pickle", "multiprocessing", "shutil", "urllib", "http",
                     "requests", "httpx")
FORBIDDEN_CALLS = ("os.system", "os.popen", "os.fork", "eval", "exec", "compile", "__import__")
FORBIDDEN_PREFIXES = ("os.exec", "os.spawn")         # os.execv, os.spawnl, ...
# a candidate's code reaches the world through ctx.tool only; the egress tool of the seed (agent/http_fetch.py) is the
# package's own and stays as it is


def _reaches(text: str, path: str) -> list[tuple[int, str]]:
    """(line, what) of each forbidden import or call in a module; [(line, "does not parse: ...")] for a bad one."""
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as e:
        return [(e.lineno or 0, f"does not parse: {e.msg}")]
    out, alias = [], {}
    for node in ast.walk(tree):             # `import os as o` and `from os import system` name the same call
        if isinstance(node, ast.Import):
            alias.update({a.asname: a.name for a in node.names if a.asname})
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            alias.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
    for node in ast.walk(tree):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
            [node.module or ""] if isinstance(node, ast.ImportFrom) else []
        out += [(node.lineno, f"imports {n}") for n in names if n.split(".")[0] in FORBIDDEN_IMPORTS]
        if isinstance(node, ast.Call):
            called = _dotted(node.func)
            head, dot, rest = called.partition(".")
            resolved = alias.get(head, head) + dot + rest if called else ""
            if resolved and (resolved in FORBIDDEN_CALLS or resolved.startswith(FORBIDDEN_PREFIXES)):
                out.append((node.lineno, f"calls {resolved}"))
    return out


def code_problems(path: str, text: str, before: str | None = None) -> list[str]:
    """What a changed module newly reaches for (imports and calls the parent's version of the file did not have):
    package code reaches the world through ctx.tool."""
    had = {what for _, what in _reaches(before, path)} if before is not None else set()
    return [f"{path}:{line}: {what}" + ("" if what.startswith("does not parse") else
                                        ": package code reaches the world through ctx.tool")
            for line, what in _reaches(text, path) if what not in had]


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.insert(0, node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.insert(0, node.id)
        return ".".join(parts)
    return ""


def _covered(comp: dict, declared) -> bool:
    """Whether a changed component of the diff is one a declared component speaks for."""
    kind, name, change = comp["kind"], comp.get("name"), comp.get("change")
    if change == "deleted" and kind in ("tool", "hook", "skill"):
        return any(d.kind == "removal" and d.site == name for d in declared)
    if kind == "hook":
        return any(d.kind == "hook" and d.site == comp.get("site") for d in declared)
    if kind in ("tool", "skill", "prompt", "config", "vendor"):
        return any(d.kind == kind and d.site == name for d in declared)
    if kind in ("agent", "memory"):
        return any(d.kind == kind for d in declared)
    return False


def _matches(d, comps: list[dict]) -> bool:
    """Whether a declared component is one the diff shows."""
    if d.kind == "removal":
        return any(c["change"] == "deleted" and c["kind"] in ("tool", "hook", "skill") and c["name"] == d.site
                   for c in comps)
    if d.kind == "hook":
        return any(c["kind"] == "hook" and c["change"] != "deleted" and c.get("site") == d.site for c in comps)
    if d.kind in ("tool", "skill"):
        return any(c["kind"] == d.kind and c["change"] != "deleted" and c["name"] == d.site for c in comps)
    if d.kind in ("prompt", "config", "vendor"):             # a dropped prompt file is that prompt's change
        return any(c["kind"] == d.kind and c["name"] == d.site for c in comps)
    return any(c["kind"] == d.kind for c in comps)


def declaration_problems(decl, comps: list[dict], *, train_tasks, study_tasks=(), cause_ids=(), l5_per_round=1) \
        -> list[str]:
    """The declaration against the diff, both ways (IMPROVER §7, §8): every declared component is a change the files
    make, every change the files make (tests aside) is declared; the loop's own kinds within l5_per_round; the model
    is never a candidate's; the claimed tasks are studied train tasks and the causes the round's."""
    if not comps:
        return ["the candidate changes nothing"]
    out = []
    declared = list(decl.components)
    for d in declared:
        if not _matches(d, comps):
            what = {"tool": f"a tool named {d.site!r}", "hook": f"a hook at {d.site!r}", "skill": f"a skill named "
                    f"{d.site!r}", "prompt": f"the {d.site!r} prompt", "config": f"the config knob {d.site!r}",
                    "vendor": f"the vendored package {d.site!r}", "removal": f"a deleted tool, hook or skill named "
                    f"{d.site!r}", "agent": "the agent", "memory": "the memory snapshot"}.get(d.kind, d.kind)
            out.append(f"the declaration names {what} and the files do not change it (they change "
                       f"{sorted({c['kind'] + ':' + str(c.get('site') if c['kind'] == 'hook' else c['name']) for c in comps})})")
    for c in comps:
        if c["kind"] == "test":
            continue
        if c["kind"] == "model":
            out.append("the model binding is the training chain's, not a candidate's")
            continue
        if not _covered(c, declared):
            name = c.get("site") if c["kind"] == "hook" else c["name"]
            out.append(f"the files change the {c['kind']} {name!r} ({c['change']}) and the declaration does not name "
                       f"it: declare every component the package changes")
    l5 = [d for d in declared if d.kind in L5_KINDS]
    if len(l5) > int(l5_per_round):
        out.append(f"{len(l5)} changes of the loop's own kinds ({[d.key for d in l5]}): at most {l5_per_round} of "
                   f"agent, config or vendor a round")
    if not decl.fixed:
        studied = set(study_tasks or train_tasks)
        outside = sorted(set(decl.predicted_affected) - studied)
        if outside:             # one message for a held-out task and any other id: it does not say which is which
            out.append(f"predicted_affected names {outside[:5]}, which are not among the train tasks you were given")
        if cause_ids:
            unknown = sorted(set(decl.cause_ids) - set(cause_ids))
            if unknown:
                out.append(f"cause_ids {unknown[:5]} are not this round's causes: one of {sorted(cause_ids)}")
    return out


def static_check(files: dict[str, str], decl, ctx) -> dict:
    limits = Limits.from_core(ctx.core)
    memory_candidate = "memory" in decl.kinds
    shape = check_shape(files, limits=limits, seed=ctx.seed, parent=None if memory_candidate else ctx.parent)
    if shape:
        return {"passed": False, "detail": "; ".join(shape)[:2000]}
    problems = []
    d = diff(ctx.parent, files)
    harness = ctx.core.get("harness") or {}
    not_allowed = sorted(set(d.kinds) - set(harness.get("kinds") or ()))
    if not_allowed:
        problems.append(f"K does not allow the kinds {not_allowed}")
    sites = sorted(set(d.sites) - set(harness.get("sites") or ()))
    if sites:
        problems.append(f"K does not allow the hook sites {sites}")
    if MEMORY_PATH in d.changed and not memory_candidate:
        problems.append(f"{MEMORY_PATH} is written by the memory from train evidence, never by a candidate")
    found = findings(files, ctx.seed, set(ctx.forbidden or ()))
    if found:
        problems.append(describe(found, what="the candidate"))
    for path, status in d.changed.items():
        if status != "deleted" and path.endswith(".py"):
            problems += code_problems(path, files[path], ctx.parent.files.get(path))
    problems += declaration_problems(decl, components(ctx.parent, files), train_tasks=ctx.train_tasks,
                                     study_tasks=ctx.study_tasks, cause_ids=ctx.cause_ids,
                                     l5_per_round=int(getattr(ctx.settings, "l5_per_round", 1)))
    return {"passed": not problems, "detail": "; ".join(problems)[:3000]}
