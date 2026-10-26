"""The literal check (docs/design/HARNESS.md §6, §8; docs/DESIGN.md §6.6 V0): no one train task's answer may be
written into a shared component.

The index is built from train evidence only: the grading rows' assertion params (a value the grader checks is a
task's answer) and the train tasks' initial states. A value of exactly one train task is forbidden; a value of two or
more is the domain's vocabulary; field and column names are the schema; a pure number is a constant unless a train
assertion checks for it. A candidate is searched only on the lines it changed against the seed, so upstream's own
lines of a vendored file (Stirrup is full of small integers) are never searched; the seed itself is searched whole
once (`findings(files, None, ...)`), by the activity's smoke checks.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Iterable

from .kinds import MEMORY_PATH

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PROPER = re.compile(r"^[A-ZÀ-ɏ][\wÀ-ɏ.&'-]*(\s+[-&]?\s*[A-ZÀ-ɏ0-9][\wÀ-ɏ.&'-]*)+$")
NUMBER = re.compile(r"[+-]?\d[\d.,]*%?")
HEADER_LISTS = ("headers", "header", "columns", "fieldnames")
MIN_SHARED_TASKS = 2
MAX_LISTED = 12


def literal_kind(text: str) -> str | None:
    """Which kind of task-specific value a string is, or None for ordinary words: an e-mail address; an identifier (a
    digit, '@', '_', or '-' with a capital, inside one token); a proper name of two or more capitalised words."""
    text = text.strip()
    if len(text) < 4 or len(text) > 200:
        return None
    if EMAIL.fullmatch(text):
        return "e-mail address"
    if " " not in text and (any(ch.isdigit() for ch in text) or any(ch in text for ch in "_@")
                            or ("-" in text and any(ch.isupper() for ch in text))):
        return "identifier"
    if PROPER.match(text):
        return "record name"
    return None


def literals_of(values) -> set[str]:
    out: set[str] = set()

    def walk(v) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, str) and literal_kind(v):
            out.add(v.strip())
    walk(values)
    return out


def header_names(values) -> set[str]:
    """Schema words: every string key of a mapping, and the strings of a list under a key named like a header row."""
    out: set[str] = set()

    def walk(v, key=None) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if isinstance(k, str) and k.strip():
                    out.add(k.strip())
                walk(x, k)
        elif isinstance(v, (list, tuple)):
            for x in v:
                if isinstance(x, str) and isinstance(key, str) and key.lower() in HEADER_LISTS and x.strip():
                    out.add(x.strip())
                else:
                    walk(x)
    walk(values)
    return out


@dataclass
class LiteralIndex:
    tasks: dict = field(default_factory=dict)       # literal -> set of train task ids it occurs in
    answers: set = field(default_factory=set)       # literals of train assertion params
    headers: set = field(default_factory=set)       # field and column names


def _values(assertion: dict):
    if "params" in assertion:
        return assertion.get("params")
    return {k: v for k, v in assertion.items() if k not in ("type", "excluded", "passed")}


def literal_index(rows: Iterable[dict]) -> LiteralIndex:
    """From train rows only: {task_id, assertions, state?, prompt?}. An assertion is the grader's own (its values beside
    its `type`) or an attempt's record of it ({type, params, passed, excluded}); the activity adds a train task's
    initial state as `state` and its request as `prompt`. A literal of the assertions and states that another train
    task's request names is that task's too: what two requests name is the domain's vocabulary ("Accounts Payable"),
    not one task's answer. Numbers are left out of that: an amount another request names may be this one's answer."""
    index = LiteralIndex()
    rows = list(rows)
    for row in rows:
        task = str(row.get("task_id") or "")
        params = [_values(a) for a in row.get("assertions") or () if isinstance(a, dict)]
        for lit in literals_of(params):
            index.tasks.setdefault(lit, set()).add(task)
            index.answers.add(lit)
        index.headers |= header_names(params)
        if row.get("state") is not None:
            for lit in literals_of(row["state"]):
                index.tasks.setdefault(lit, set()).add(task)
            index.headers |= header_names(row["state"])
    prompts = [(str(row.get("task_id") or ""), str(row["prompt"])) for row in rows if row.get("prompt")]
    if prompts:
        for lit, tasks in index.tasks.items():
            if NUMBER.fullmatch(lit):       # an amount another request happens to name may still be this one's answer
                continue
            pattern = literal_pattern(lit)
            tasks.update(t for t, text in prompts if pattern.search(text))
    return index


def forbidden(index: LiteralIndex, *, exempt: set[str] = frozenset()) -> set[str]:
    """Literals of fewer than MIN_SHARED_TASKS train tasks, less the schema words and `exempt`, less pure numbers that
    no train assertion checks for."""
    skip = set(index.headers) | set(exempt)
    return {lit for lit, tasks in index.tasks.items()
            if len(tasks) < MIN_SHARED_TASKS and lit not in skip
            and (not NUMBER.fullmatch(lit) or lit in index.answers)}


def changed_lines(text: str, base: str | None) -> set[int]:
    """The 1-based line numbers of `text` that a line diff against `base` inserts or replaces; every line when there
    is no base."""
    lines = text.splitlines()
    if base is None:
        return set(range(1, len(lines) + 1))
    if text == base:
        return set()
    out: set[int] = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, base.splitlines(), lines, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
    return out


def literal_pattern(lit: str) -> re.Pattern:
    """A literal as a whole token; case-sensitive except an e-mail address."""
    flags = re.IGNORECASE if literal_kind(lit) == "e-mail address" else 0
    return re.compile(r"(?<![\w@.-])" + re.escape(lit) + r"(?![\w@-])", flags)



def _strings(value) -> set[str]:
    out: set[str] = set()
    if isinstance(value, str):
        out.add(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            out.add(str(k))
            out |= _strings(v)
    elif isinstance(value, list):
        for v in value:
            out |= _strings(v)
    return out


def findings(files: dict[str, str], seed, forbidden: set[str], *, allowed: set[str] = frozenset()) -> list[dict]:
    """Where each forbidden literal occurs: [{path, line, literal, kind}]. Only what a candidate changed against the
    seed is searched: a new path and its whole text; a changed line of a file the seed has; a decoded manifest string
    the seed's manifest does not have (line None: a JSON escape must not hide a literal). `seed=None` searches every
    file whole. The host-only memory snapshot is the memory package's to check when it writes an entry."""
    wanted = sorted(set(forbidden) - set(allowed))
    if not wanted:
        return []
    base = dict(seed.files) if seed is not None else {}
    patterns = [(lit, literal_pattern(lit)) for lit in wanted]
    out: list[dict] = []
    for path in sorted(files):
        if path == MEMORY_PATH:
            continue
        text = files[path]
        if path not in base:
            for lit, pat in patterns:
                if pat.search(path):
                    out.append({"path": path, "line": 0, "literal": lit, "kind": literal_kind(lit) or "value"})
        lines = text.splitlines()
        for n in sorted(changed_lines(text, base.get(path))):
            for lit, pat in patterns:
                if pat.search(lines[n - 1]):
                    out.append({"path": path, "line": n, "literal": lit, "kind": literal_kind(lit) or "value"})
        if path == "manifest.json":
            try:
                new = _strings(json.loads(text))
                old = _strings(json.loads(base["manifest.json"])) if "manifest.json" in base else set()
            except (json.JSONDecodeError, TypeError):
                continue
            seen = {f["literal"] for f in out if f["path"] == path}
            fresh = "\n".join(sorted(new - old))
            for lit, pat in patterns:
                if lit not in seen and pat.search(fresh):
                    out.append({"path": path, "line": None, "literal": lit, "kind": literal_kind(lit) or "value"})
    return out


def describe(found: list[dict], *, what: str = "the package") -> str:
    """The check output for R: file, line and literal, so R revises the line it wrote."""
    listed = [f"{f['path'] if f.get('line') is None else f['path'] + ':' + str(f['line'])}: {f['literal']!r} "
              f"({f.get('kind') or 'value'})" for f in found]
    more = f"; and {len(listed) - MAX_LISTED} more" if len(listed) > MAX_LISTED else ""
    return (f"{what} contains task-specific values from train tasks — a value that occurs in only one train task is "
            f"that task's answer: " + "; ".join(listed[:MAX_LISTED]) + more
            + ". Replace each with a general rule (match case-sensitively: a lower-case phrase is not the value)")
