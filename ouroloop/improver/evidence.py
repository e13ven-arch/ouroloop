"""Evidence (docs/design/IMPROVER.md §4; DESIGN §6.2): what the train attempts of a W say, derived at read time from the
results, the train grading rows and the transcripts (runtime/trace.py). Nothing here reads a dev or test row's content:
every per-attempt reader goes through the trace's split guard, and the one dev reading, `dev_histogram`, keeps only the
failed assertions' types with their counts.

A W's train reading is the latest valid train attempt of its package on each task, under any run of that package but
the memory's stream (whose attempts read a live library the package does not hold): the baseline, a measure, a screen
of the same package all count — the package and K decide what executes, not the release that names them.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..evaluate.results import TaskResult
from ..runtime.trace import TraceAccessError, TraceStore

TRAIN = "train"
NOT_READINGS = ("library",)             # run roles whose attempts are not the package's own reading
WHOLE = ("prompt", "agent", "config", "vendor", "model")     # kinds whose footprint is every attempt


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

    def best_successes(self, package_ref: str, n: int, tasks=None) -> list[AttemptEvidence]:
        """The n cleanest successes (of `tasks` when given): fewest steps, then lowest cost, then the frozen order
        (RRSI: keep what works)."""
        order = {t: i for i, t in enumerate(self.activity.splits.ids(TRAIN))}
        passed = [e for e in self.passed(package_ref) if tasks is None or e.task_id in set(tasks)]
        passed.sort(key=lambda e: (e.result.steps, e.result.cost_usd if e.result.cost_usd is not None else 1e9,
                                   order.get(e.task_id, len(order))))
        return passed[:max(int(n), 0)]

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
