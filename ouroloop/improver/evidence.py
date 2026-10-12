"""Evidence (docs/design/IMPROVER.md §4; DESIGN §6.2): what the train attempts of a W say, derived at read time from the
results, the train grading rows and the transcripts (runtime/trace.py). Nothing here reads a dev or test row's content:
every per-attempt reader goes through the trace's split guard, and the one dev reading, `dev_histogram`, keeps only the
failed assertions' types with their counts.

A W's train reading is the latest valid train attempt of its package on each task, under any run of that package but
the memory's stream (whose attempts read a live library the package does not hold): the baseline, a measure, a screen
of the same package all count — the package and K decide what executes, not the release that names them.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass

from ..evaluate.results import TaskResult
from ..runtime.trace import TraceAccessError, TraceStore

TRAIN = "train"
NOT_READINGS = ("library",)             # run roles whose attempts are not the package's own reading
WHOLE = ("prompt", "agent", "config", "vendor", "model")     # kinds whose footprint is every attempt
NOT_WRITES = ("api_search", "finish", "read_skill", "recall", "plan_changes")   # tool calls a digest counts or skips
REQUEST_CHARS, FINAL_CHARS, MIN_EXCERPT = 1500, 600, 120


@dataclass(frozen=True)
class AttemptEvidence:
    attempt_id: str
    task_id: str
    task_hash: str
    session: str
    result: TaskResult
    failed_checks: tuple = ()           # ({type, params}) of the failed, scored assertions (train grading)
    tool_errors: tuple = ()             # (tool name) of each tool result the model saw as an error
    hooks: tuple = ()                   # ({site, name, action}) of the hooks that acted
    tools_called: tuple = ()            # tool names the model called, in order
    skills_read: tuple = ()             # skill names read and found
    entries_shown: tuple = ()           # memory entry ids the attempt was given
    writes: int = 0                     # base calls that changed the world

    @property
    def passed(self) -> bool:
        return bool(self.result.success)


class Evidence:
    def __init__(self, activity, trace: TraceStore | None = None):
        self.activity = activity
        self.trace = trace or TraceStore(activity.root, activity.splits.split_of)
        self._cache: dict[str, AttemptEvidence] = {}

    # ------------------------------------------------------------------ readings
    def reading(self, package_ref: str) -> dict[str, TaskResult]:
        """{task_id: the package's latest valid train attempt}."""
        runs = self.activity.results.runs()
        out: dict[str, TaskResult] = {}
        for r in self.activity.results.all():
            run = runs.get(r.run)
            if r.split != TRAIN or r.validity != "valid" or run is None or run.package_ref != package_ref \
                    or run.role in NOT_READINGS:
                continue
            out[r.task_id] = r
        return out

    def missing(self, package_ref: str) -> list[str]:
        """The train tasks the package has no valid reading of, in the frozen order."""
        have = self.reading(package_ref)
        return [t for t in self.activity.splits.ids(TRAIN) if t not in have]

    def failed(self, package_ref: str) -> list[AttemptEvidence]:
        rows = self.reading(package_ref)
        return [self.of(rows[t]) for t in self.activity.splits.ids(TRAIN) if t in rows and not rows[t].success]

    def passed(self, package_ref: str) -> list[AttemptEvidence]:
        rows = self.reading(package_ref)
        return [self.of(rows[t]) for t in self.activity.splits.ids(TRAIN) if t in rows and rows[t].success]

    def latest(self, tasks, *, prefer: str | None = None) -> dict[str, TaskResult]:
        """{task_id: its latest valid train attempt} over `tasks`, in their order, under any package but the memory
        stream's — the stream's evidence pool (IMPROVER §3.1), where a task is read once and its reading may be another
        line's. With `prefer` (the working line's package) that package's latest attempt stands where it has one, so a
        rejected candidate's screen attempt never replaces the line's own reading of a held-out task."""
        wanted = set(tasks)
        runs = self.activity.results.runs()
        any_: dict[str, TaskResult] = {}
        own: dict[str, TaskResult] = {}
        for r in self.activity.results.all():
            run = runs.get(r.run)
            if r.split != TRAIN or r.validity != "valid" or r.task_id not in wanted or run is None \
                    or run.role in NOT_READINGS:
                continue
            any_[r.task_id] = r
            if prefer is not None and run.package_ref == prefer:
                own[r.task_id] = r
        return {t: own.get(t, any_[t]) for t in tasks if t in any_}

    def cleanest(self, rows: dict, n: int) -> list[AttemptEvidence]:
        """The n cleanest successes of a reading ({task: TaskResult}): fewest steps, then lowest cost, then the frozen
        order (RRSI: keep what works)."""
        order = {t: i for i, t in enumerate(self.activity.splits.ids(TRAIN))}
        passed = [self.of(r) for r in rows.values() if r.success]
        passed.sort(key=lambda e: (e.result.steps, e.result.cost_usd if e.result.cost_usd is not None else 1e9,
                                   order.get(e.task_id, len(order))))
        return passed[:max(int(n), 0)]

    def best_successes(self, package_ref: str, n: int, tasks=None) -> list[AttemptEvidence]:
        """The n cleanest successes of the package's reading (of `tasks` when given)."""
        wanted = None if tasks is None else set(tasks)
        return self.cleanest({t: r for t, r in self.reading(package_ref).items() if wanted is None or t in wanted}, n)

    # ------------------------------------------------------------------ one attempt
    def of(self, row: TaskResult) -> AttemptEvidence:
        """The evidence of one train attempt. A dev or test attempt raises TraceAccessError."""
        if row.split != TRAIN:
            raise TraceAccessError(f"{row.attempt_id} is a {row.split} attempt: evidence is train only")
        if row.attempt_id in self._cache:
            return self._cache[row.attempt_id]
        session = row.evidence or row.attempt_id
        task_hash = self.trace.task_hash(session)
        failed = tuple({"type": a.get("type"), "params": a.get("params")}
                       for a in self.activity.results.grading(row.attempt_id)
                       if not a.get("passed") and not a.get("excluded"))
        tool_errors, hooks, tools, skills, shown, writes = [], [], [], [], [], 0
        for item in self.trace.events(session):
            e = item["event"]
            kind = e.get("kind")
            if kind == "gateway_call":
                if e.get("role") == "act":
                    tools += [c.get("name") for c in (e.get("response") or {}).get("tool_calls") or ()
                              if isinstance(c, dict)]
                for m in e.get("messages_tail") or ():
                    if isinstance(m, dict) and m.get("role") == "tool":
                        tool_errors += [r.get("name") for r in m.get("results") or ()
                                        if isinstance(r, dict) and r.get("is_error")]
            elif kind == "hook" and e.get("action") not in (None, "none"):
                hooks.append({"site": e.get("site"), "name": e.get("name"), "action": e.get("action")})
            elif kind == "skill_read" and e.get("found"):
                skills.append(str(e.get("name")))
            elif kind == "library_retrieval":
                shown += [str(i) for i in e.get("shown") or () if i]
            elif kind == "broker_event" and e.get("mutates") and not e.get("refused"):
                writes += 1
        ev = AttemptEvidence(row.attempt_id, row.task_id, task_hash, session, row, failed, tuple(tool_errors),
                             tuple(hooks), tuple(tools), tuple(skills), tuple(dict.fromkeys(shown)), writes)
        self._cache[row.attempt_id] = ev
        return ev

    @staticmethod
    def features(ev: AttemptEvidence) -> dict:
        """The deterministic features of one attempt: what a diagnosis reads before any model does."""
        return {"task_id": ev.task_id, "termination": ev.result.termination, "limit": ev.result.limit,
                "success": ev.passed, "partial_credit": ev.result.partial_credit, "steps": ev.result.steps,
                "failed_assertion_types": dict(sorted(Counter(str(c["type"]) for c in ev.failed_checks).items())),
                "tool_errors": dict(sorted(Counter(str(t) for t in ev.tool_errors).items())),
                "writes": ev.writes, "tools_called": sorted(set(ev.tools_called)),
                "hooks": sorted({f"{h['site']}:{h['name']}:{h['action']}" for h in ev.hooks}),
                "skills_read": sorted(set(ev.skills_read)), "entries_shown": list(ev.entries_shown)}

    # ------------------------------------------------------------------ one case side by side (v1.5)
    def digest(self, ev: AttemptEvidence, *, chars: int) -> dict:
        """One failing case side by side for R (DESIGN §6.15; IMPROVER §4): the request, the checks the grader marked
        failed with their parameters, the agent's writes in order (a non-GET api_fetch with its method, URL and body
        excerpt; any other tool call that is not a read or a search, with its arguments), what it said at the end, and
        how much it read and searched. The JSON is cut to `chars` by shrinking the writes' excerpts, never the checks."""
        task = self.activity.suite.tasks[ev.task_id]
        writes, reads, searches = [], 0, 0
        for item in self.trace.events(ev.session):
            e = item["event"]
            if e.get("kind") != "tool":
                continue
            name = str(e.get("name") or "")
            args = e.get("args") if isinstance(e.get("args"), dict) else {}
            if name == "api_search":
                searches += 1
                continue
            if name in NOT_WRITES:
                continue
            if name == "api_fetch":
                method = str(args.get("method") or "GET").upper()
                if method in ("GET", "HEAD"):
                    reads += 1
                    continue
                writes.append({"tool": name, "method": method, "url": re.sub(r"^https?://", "", str(args.get("url") or "")),
                               "body": str(args.get("body") if args.get("body") is not None else ""),
                               "error": bool(e.get("is_error"))})
            else:
                writes.append({"tool": name, "method": "CALL", "body": json.dumps(args, ensure_ascii=False, default=str),
                               "error": bool(e.get("is_error"))})
        said = self.trace.finish(ev.session)
        row = {"task_id": ev.task_id, "session": ev.session, "request": task.trigger[:REQUEST_CHARS],
               "termination": ev.result.termination, "partial_credit": ev.result.partial_credit,
               "failed_checks": [dict(c) for c in ev.failed_checks], "reads": reads, "searches": searches,
               "writes": writes, "final_text": str(said.get("reason") or said.get("text") or "")[:FINAL_CHARS]}
        return _fit(row, int(chars))

    def persistence(self, task_id: str) -> int:
        """Under how many different packages of the working line's history the task has a failing valid train reading
        (DESIGN §6.15: a task that stays failed across installs was not touched by what was installed)."""
        packages = {rel.package_ref for rel in self.activity.releases.history()}
        runs = self.activity.results.runs()
        failed = set()
        for r in self.activity.results.all():
            run = runs.get(r.run)
            if r.task_id == task_id and r.split == TRAIN and r.validity == "valid" and not r.success and run is not None \
                    and run.role not in NOT_READINGS and run.package_ref in packages:
                failed.add(run.package_ref)
        return len(failed)

    def footprint(self, row: TaskResult, component: dict) -> bool:
        """Whether a component showed in this attempt (V3's condition and credit's): a tool the model called, a hook
        that acted, a skill read, memory entries given; prompt, agent, config, vendor and model act on every attempt."""
        kind, name = component.get("kind"), component.get("name")
        if kind in WHOLE:
            return True
        ev = self.of(row)
        if kind == "tool":
            return name in ev.tools_called
        if kind == "hook":
            return any(h["name"] == name for h in ev.hooks)
        if kind == "skill":
            return name in ev.skills_read
        if kind == "memory":
            return bool(ev.entries_shown) if name in (None, "", "snapshot") else \
                any(str(e).startswith(str(name)[:8]) for e in ev.entries_shown)
        return False


def _fit(row: dict, chars: int) -> dict:
    """Shrink the writes' excerpts (the longest first, never under MIN_EXCERPT) until the row's JSON fits `chars`; the
    request, the checks and the counts are kept whole."""
    size = lambda: len(json.dumps(row, ensure_ascii=False, default=str))  # noqa: E731
    while size() > chars:
        longest = max(row["writes"], key=lambda w: len(w["body"]), default=None)
        if longest is None or len(longest["body"]) <= MIN_EXCERPT:
            break
        cut = max(MIN_EXCERPT, len(longest["body"]) // 2)
        shorter = longest["body"][:cut] + f"…(+{len(longest['body']) - cut})"
        if len(shorter) >= len(longest["body"]):            # the suffix would undo the cut: this is the floor
            break
        longest["body"] = shorter
    return row


def dev_histogram(activity, package_ref: str) -> dict:
    """The one thing R sees of dev: over the package's valid dev attempts, the failed scored assertions by type with
    counts, and how many attempts and tasks they come from. Types and counts only — no task ids, no params."""
    runs = activity.results.runs()
    types: Counter = Counter()
    attempts, tasks = 0, set()
    for r in activity.results.all():
        run = runs.get(r.run)
        if r.split != "dev" or r.validity != "valid" or run is None or run.package_ref != package_ref:
            continue
        attempts += 1
        tasks.add(r.task_id)
        for a in activity.results.grading(r.attempt_id, splits=("dev",)):
            if not a.get("passed") and not a.get("excluded"):
                types[str(a.get("type") or "unknown")] += 1
    return {"split": "dev", "types": dict(sorted(types.items())), "attempts": attempts, "tasks": len(tasks)}
