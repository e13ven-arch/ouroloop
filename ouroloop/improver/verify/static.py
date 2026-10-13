"""V0, the static rung (docs/design/IMPROVER.md §8): what can be told of a candidate without running it.

The package's shape and K's limits (harness.package.check_shape: the manifest, the sizes, the vendored packages'
protected files, the host-only memory left as the parent has it); the kinds and hook sites K allows; the literal check
on the lines changed against the seed (a value of one train task is that task's answer); the code a candidate adds
does not reach for a shell, the network, raw memory or pickles; the declaration says what the files change and the
files change what it says (both ways); the tasks it claims are train tasks R was given; the causes are the round's.

v1.8 (DESIGN §6.18): existing text only grows — a prompt or a skill the parent has may be added to, never deleted
from or rewritten, unless the package is that one component alone, declared `rewrite: true` (it is then screened as a
whole-footprint package); and a hook or tool whose trigger is a list of words (a vocabulary in a literal list or a
regex alternation) is noted on the verdict (`heuristic_terms`), not refused: the mechanism's conditions should come
from the task's own documents, and a fixed word list is what broke other tasks on finance-v7.
"""
from __future__ import annotations

import ast
import difflib
import re

from ...harness.kinds import MEMORY_PATH
from ...harness.literals import describe, findings
from ...harness.package import Limits, check_shape, classify, diff
from ..components import components
from ..settings import L5_KINDS

TEXT_KINDS = ("prompt", "skill")               # the kinds whose files only grow (v1.8)
WORD = re.compile(r"^[A-Za-z][A-Za-z'\-]*(?: [A-Za-z][A-Za-z'\-]*){0,3}$")      # one to four natural-language words
RE_CALLS = ("re.compile", "re.search", "re.match", "re.fullmatch", "re.findall", "re.finditer", "re.sub", "re.split")

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


# ---------------------------------------------------------------------------- v1.8: text only grows
def text_problems(parent_files: dict, files: dict, decl) -> list[str]:
    """The prompt and skill files the parent has that the candidate deletes lines from or rewrites, unless the
    package is one prompt or skill component declared `rewrite: true` (then it is screened alone, whole-footprint)."""
    rewriting = [c for c in decl.components if getattr(c, "rewrite", False)]
    out = []
    if rewriting and len(decl.components) > 1:
        out.append(f"{rewriting[0].key} is declared rewrite: a package that deletes or rewrites existing text holds "
                   f"that one component alone (this one declares {len(decl.components)}): hand the other components "
                   f"in as another package")
    if rewriting and len(decl.components) == 1:
        return out
    for path in sorted(set(parent_files) & set(files)):
        if classify(path) not in TEXT_KINDS or files[path] == parent_files[path]:
            continue
        before, after = parent_files[path].splitlines(), files[path].splitlines()
        cut = [op for op in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
               if op[0] in ("delete", "replace")]
        if cut:
            where = ", ".join(f"lines {i1 + 1}-{i2}" if i2 - i1 > 1 else f"line {i1 + 1}" for _, i1, i2, _, _ in cut[:3])
            out.append(f"{path}: existing text only grows (it is deleted or rewritten at {where}); add to it, or hand "
                       f"in the rewrite as a package of this one component declared rewrite: true, screened alone "
                       f"and accepted with accept_min_whole_fixes confirmed fixes")
    return out


# ---------------------------------------------------------------------------- v1.8: a trigger made of a word list
def _regex_terms(pattern: str) -> list[str]:
    """The natural-language alternatives of a regex: `(pending|on leave|reserved)` -> the three terms."""
    body = re.sub(r"\(\?[a-zA-Z:<=!]+", "(", pattern)
    body = re.sub(r"\\b|\^|\$|[()]", " ", body)
    return [t.strip() for t in body.split("|") if WORD.match(t.strip()) and len(t.strip()) > 1]


def _word_list(node, consts: dict) -> list[str]:
    """The natural-language strings of a literal list, tuple or set, or of a module constant bound to one."""
    if isinstance(node, ast.Name):
        node = consts.get(node.id)
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return []
    elts = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    if len(elts) < 2 or len(elts) != len(node.elts):
        return []
    return [e.strip() for e in elts if WORD.match(e.strip()) and len(e.strip()) > 1]


def heuristic_terms(text: str, minimum: int = 3) -> list[str]:
    """The literal words a module's code conditions on, when there are at least `minimum`: a word list (a literal
    list, tuple or set of strings, or a module constant bound to one) that a membership test compares against
    (`status in STATUSES`) or that a loop or comprehension walks (`any(w in text for w in WORDS)`), and the
    alternatives of a regex handed to `re.*`. A list that is only data (a schema's `required`, a dict value) is not a
    condition. Sorted, without repeats; [] under the minimum or for code that does not parse."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    consts = {t.id: node.value for node in tree.body if isinstance(node, ast.Assign)
              for t in node.targets if isinstance(t, ast.Name)}
    terms: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    terms |= set(_word_list(right, consts))
        elif isinstance(node, (ast.For, ast.comprehension)):
            terms |= set(_word_list(node.iter, consts))
        elif isinstance(node, ast.Call) and _dotted(node.func) in RE_CALLS and node.args \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            terms |= set(_regex_terms(node.args[0].value))
    return sorted(terms) if len(terms) >= int(minimum) else []


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
    terms: dict[str, list] = {}
    for path, status in d.changed.items():
        if status != "deleted" and path.endswith(".py"):
            problems += code_problems(path, files[path], ctx.parent.files.get(path))
            if classify(path) in ("tool", "hook"):
                found = heuristic_terms(files[path], int(getattr(ctx.settings, "heuristic_terms_min", 3)))
                if found:
                    terms[path] = found
    if getattr(ctx.settings, "text_additive_only", True) and not decl.fixed:
        problems += text_problems(ctx.parent.files, files, decl)
    problems += declaration_problems(decl, components(ctx.parent, files), train_tasks=ctx.train_tasks,
                                     study_tasks=ctx.study_tasks, cause_ids=ctx.cause_ids,
                                     l5_per_round=int(getattr(ctx.settings, "l5_per_round", 1)))
    out = {"passed": not problems, "detail": "; ".join(problems)[:3000]}
    if terms:        # a soft signal, on the verdict and the ledger, never a refusal (v1.8)
        out["heuristic_terms"] = terms
        out["detail"] = (out["detail"] + ("; " if out["detail"] else "") + "note: the trigger conditions on a word "
                         "list (" + "; ".join(f"{p}: {', '.join(t[:6])}" for p, t in terms.items()) + "): a mechanism "
                         "keyed on fixed words acts against tasks whose documents say otherwise — take the "
                         "conditions from the task's own documents and request")[:3000]
    return out
