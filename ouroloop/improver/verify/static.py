"""V0, the static rung (docs/design/IMPROVER.md §8): what can be told of a candidate without running it.

The package's shape and K's limits (harness.package.check_shape: the manifest, the sizes, the vendored packages'
protected files, the host-only memory left as the parent has it); the kinds and hook sites K allows; the literal check
on the lines changed against the seed (a value of one train task is that task's answer); the code a candidate adds
does not reach for a shell, the network, raw memory or pickles; the declaration says what the files change; the tasks
it claims are train tasks.
"""
from __future__ import annotations

import ast

from ...harness.kinds import MEMORY_PATH
from ...harness.literals import describe, findings
from ...harness.package import Limits, check_shape, diff
from ..components import components

FORBIDDEN_IMPORTS = ("socket", "subprocess", "ctypes", "pickle", "multiprocessing", "shutil", "urllib", "http",
                     "requests", "httpx")
FORBIDDEN_CALLS = ("os.system", "os.popen", "os.fork", "eval", "exec", "compile", "__import__")
FORBIDDEN_PREFIXES = ("os.exec", "os.spawn")         # os.execv, os.spawnl, ...
# a candidate's code reaches the world through ctx.tool only; the egress tool of the seed (agent/http_fetch.py) is the
# package's own and stays as it is
EXTRA_KINDS = ("test",)               # what any candidate may change besides its declared kind


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


def declaration_problems(decl, comps: list[dict], *, train_tasks) -> list[str]:
    if not comps:
        return ["the candidate changes nothing"]
    kinds = {c["kind"] for c in comps}
    out = []
    if decl.kind == "removal":
        gone = [c for c in comps if c["change"] == "deleted" and c["kind"] in ("tool", "hook", "skill")]
        if not gone:
            out.append("a removal deletes a tool, a hook or a skill")
    elif decl.kind not in kinds:
        out.append(f"declared kind {decl.kind} but the files change {sorted(kinds)}")
    else:
        mine = [c for c in comps if c["kind"] == decl.kind]
        if decl.kind == "tool" and not any(c["name"] == decl.site and c["change"] != "deleted" for c in mine):
            out.append(f"a tool candidate's site names the tool it adds or changes; {decl.site!r} is not one of "
                       f"{sorted(c['name'] for c in mine)}")
        if decl.kind == "hook" and not any(c.get("site") == decl.site for c in mine):
            out.append(f"a hook candidate's site is the site of the hook it adds or changes; {decl.site!r} is not one "
                       f"of {sorted({str(c.get('site')) for c in mine})}")
        if decl.kind == "skill" and not any(c["name"] == decl.site and c["change"] != "deleted" for c in mine):
            out.append(f"a skill candidate's site names the skill it adds or changes; {decl.site!r} is not one of "
                       f"{sorted(c['name'] for c in mine)}")
        if decl.kind == "prompt" and decl.site and not any(c["name"] == decl.site for c in mine):
            out.append(f"a prompt candidate's site is system or finish, the file it changes")
    extra = sorted(kinds - {decl.kind} - set(EXTRA_KINDS) - ({"tool", "hook", "skill"} if decl.kind == "removal"
                                                             else set()))
    if extra and not decl.fixed:
        out.append(f"the files also change {extra}: one candidate is one kind of change (declare another candidate)")
    if not decl.fixed:
        outside = sorted(set(decl.predicted_affected) - set(train_tasks))
        if outside:
            out.append(f"predicted_affected names {outside[:5]}, which are not train tasks")
        if not decl.predicted_affected:
            out.append("predicted_affected names no train task")
    return out


def static_check(files: dict[str, str], decl, ctx) -> dict:
    limits = Limits.from_core(ctx.core)
    memory_candidate = decl.kind == "memory"
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
    problems += declaration_problems(decl, components(ctx.parent, files), train_tasks=ctx.train_tasks)
    return {"passed": not problems, "detail": "; ".join(problems)[:3000]}
