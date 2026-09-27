"""The benchmark run contract: task groups and splits, what a run was, and one full task's result.

Nothing here imports the upstream benchmark, the agent or a provider. These are the records that have to be
readable long after the run, so that a reported number can be recomputed from them (docs/DESIGN.md §5.1):

    SplitManifest   which tasks exist, which family each belongs to, and which of train / dev / test it is in.
                    Frozen to one file before the first model run; a later run verifies against it and reports
                    drift instead of quietly resplitting.
    RunManifest     the complete identity of one evaluation run: candidate, base model and sampling, prompt,
                    decision spec, execution policy, environment versions, budget, and the split manifest it used.
    TaskResult      one attempt at one task: the official metrics, usage, termination and whether the attempt was
                    valid at all.

Two rules are enforced here rather than left to callers, because both are ways a benchmark quietly stops
measuring what it claims:

    A split is assigned per family, never per task and never per trajectory. Every attempt, branch and repeat of
    a task belongs to its task's family, so nothing derived from a dev task can end up scored as test.
    Missing cost is missing, not zero. A summary reports how many attempts have no cost rather than averaging
    them in as free.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from .ledger import _append_jsonl, _read_jsonl, now, redact, write_json

SPLIT_SCHEMA = "ouroloop.bench.splits.v1"
RESULT_SCHEMA = "ouroloop.bench.result.v1"
SPLITS = ("train", "dev", "test")
# Where the agent stopped. The first four are task outcomes and count; the last two are not evidence about the
# agent and make the attempt invalid (docs/DESIGN.md §5.2).
TERMINATIONS = ("finished", "step_limit", "budget", "tool_refused", "harness_error", "infrastructure")
VALID_TERMINATIONS = TERMINATIONS[:4]
# The RunManifest fields added in v2. Unset, they stay out of the hashed payload, so every result already
# recorded keeps the run hash it was written with.
V2_FIELDS = ("system_release_ref", "worker_ref", "research_ref", "experience_ref", "core_ref",
             "eval_plan_ref", "feedback_ref", "input_schema_refs", "data_lineage_ref", "budget_scope",
             "pricing_ref", "capability_ref")


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class Frozen(RuntimeError):
    """A frozen manifest is already on disk and says something else."""


# ---------------------------------------------------------------------------- tasks, families and splits
@dataclass(frozen=True)
class TaskEntry:
    """One task of the upstream domain, as the manifest pinned it.

    `contract` is the upstream fingerprint of everything the rollout depends on (prompt, assertions, initial
    state, granted tools). It is what detects an upstream change that would silently make old results describe a
    different task; it is not shown to anything the agent can read.
    """
    task_id: str
    family_id: str
    example_id: Any
    contract: str
    tools: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {**asdict(self), "tools": list(self.tools)}

    @classmethod
    def from_json(cls, d: dict) -> "TaskEntry":
        return cls(d["task_id"], d["family_id"], d.get("example_id"), d["contract"], tuple(d.get("tools", ())))


def assign_splits(families: Iterable[str], ratios: dict[str, float], seed: int = 0) -> dict[str, str]:
    """family_id -> split, by hashing the family with `seed`.

    Hashing rather than shuffling means the split of a family does not depend on how many other families there
    are or on the order they were listed: adding a task later moves nothing that was already assigned. Ratios
    are the intended proportions of families, so the counts a small domain actually gets are whatever the hash
    gives, and the manifest records them (docs/DESIGN.md §3.1: report the real group counts, not the intended
    ones).
    """
    if set(ratios) - set(SPLITS) or not ratios:
        raise ValueError(f"split ratios name {sorted(SPLITS)}, not {sorted(ratios)}")
    total = sum(ratios.values())
    if total <= 0 or any(v < 0 for v in ratios.values()):
        raise ValueError(f"split ratios must be non-negative and sum above zero: {ratios}")
    edges, acc = [], 0.0
    for name in SPLITS:                       # always in the same order, whatever order the config listed them
        if ratios.get(name):
            acc += ratios[name] / total
            edges.append((acc, name))
    out = {}
    for fam in dict.fromkeys(families):       # deduplicated, order irrelevant
        x = int(hashlib.sha256(f"{seed}:{fam}".encode()).hexdigest()[:16], 16) / float(1 << 64)
        out[fam] = next(name for edge, name in edges if x < edge or edge == edges[-1][0])
    return out


@dataclass
class SplitManifest:
    """The frozen task groups of one domain.

    `family_rule` says how family_id was derived, and is recorded rather than recomputed on load: a later
    version of the rule must not silently regroup tasks a run was already scored against.
    """
    domain: str
    upstream: dict                            # repo, commit, package version, python, scorer, toolset
    family_rule: str
    entries: list[TaskEntry]
    splits: dict[str, str]                    # family_id -> split
    ratios: dict[str, float] = field(default_factory=dict)
    seed: int = 0
    created: str = ""
    schema: str = SPLIT_SCHEMA

    def __post_init__(self) -> None:
        bad = {s for s in self.splits.values()} - set(SPLITS)
        if bad:
            raise ValueError(f"unknown splits {sorted(bad)}")
        missing = sorted({e.family_id for e in self.entries} - set(self.splits))
        if missing:
            raise ValueError(f"families with no split: {missing[:5]}")
        dupes = sorted({e.task_id for e in self.entries if sum(x.task_id == e.task_id for x in self.entries) > 1})
        if dupes:
            raise ValueError(f"duplicate task ids: {dupes[:5]}")

    # -------------------------------------------------------------- identity
    def payload(self) -> dict:
        return {"schema": self.schema, "domain": self.domain, "upstream": self.upstream,
                "family_rule": self.family_rule, "entries": [e.to_json() for e in self.entries],
                "splits": self.splits, "ratios": self.ratios, "seed": self.seed}

    def hash(self) -> str:
        """The identity of the data a result refers to. `created` is deliberately left out: freezing the same
        tasks twice is the same data."""
        return digest(self.payload())[:16]

    def to_json(self) -> dict:
        return {**self.payload(), "created": self.created or now(), "hash": self.hash()}

    @classmethod
    def from_json(cls, d: dict) -> "SplitManifest":
        if d.get("schema") != SPLIT_SCHEMA:
            raise ValueError(f"split manifest schema {d.get('schema')!r}, not {SPLIT_SCHEMA!r}")
        m = cls(d["domain"], d["upstream"], d["family_rule"], [TaskEntry.from_json(e) for e in d["entries"]],
                d["splits"], d.get("ratios", {}), d.get("seed", 0), d.get("created", ""))
        if d.get("hash") and d["hash"] != m.hash():
            raise ValueError(f"split manifest {d['hash']} does not hash to its contents ({m.hash()})")
        return m

    # -------------------------------------------------------------- reading
    def family_of(self, task_id: str) -> str:
        for e in self.entries:
            if e.task_id == task_id:
                return e.family_id
        raise KeyError(task_id)

    def split_of(self, task_id: str) -> str:
        return self.splits[self.family_of(task_id)]

    def entry(self, task_id: str) -> TaskEntry:
        return next(e for e in self.entries if e.task_id == task_id)

    def ids(self, *splits: str) -> list[str]:
        """The task ids of these splits, in manifest order. Naming the split at every call site is the point:
        there is no default that could quietly include test."""
        if not splits:
            raise ValueError("ids(*splits): name the splits to read; an empty call that returned everything, or "
                             "nothing, is how test leaks into a development loop")
        unknown = set(splits) - set(SPLITS)
        if unknown:
            raise ValueError(f"unknown splits {sorted(unknown)}")
        return [e.task_id for e in self.entries if self.splits[e.family_id] in splits]

    def counts(self) -> dict[str, dict[str, int]]:
        out = {s: {"families": 0, "tasks": 0} for s in SPLITS}
        for split in self.splits.values():
            out[split]["families"] += 1
        for e in self.entries:
            out[self.splits[e.family_id]]["tasks"] += 1
        return out

    def drift(self, entries: list[TaskEntry]) -> list[str]:
        """What upstream now says that the manifest does not: tasks added, removed, or whose contract changed.
        Reported, never repaired: a changed task means old results describe a different task."""
        was = {e.task_id: e for e in self.entries}
        now_ = {e.task_id: e for e in entries}
        out = [f"{tid}: gone from upstream" for tid in sorted(set(was) - set(now_))]
        out += [f"{tid}: new upstream task, not in the frozen manifest" for tid in sorted(set(now_) - set(was))]
        out += [f"{tid}: contract {was[tid].contract[:12]} -> {now_[tid].contract[:12]}"
                for tid in sorted(set(was) & set(now_)) if was[tid].contract != now_[tid].contract]
        return out

    # -------------------------------------------------------------- freezing
    def freeze(self, path: str | Path, *, replace_existing: bool = False) -> Path:
        """Write the manifest, refusing to change one that is already there (docs/DESIGN.md §3.1: the groups are
        fixed before the first result is looked at). Freezing identical contents again is not a change."""
        path = Path(path)
        if path.exists() and not replace_existing:
            old = json.loads(path.read_text(encoding="utf-8"))
            if old.get("hash") != self.hash():
                raise Frozen(f"{path} already freezes another split ({old.get('hash')} != {self.hash()}); the "
                             f"groups are fixed before the first run, so this needs a new experiment, not a "
                             f"reassignment")
            return path
        write_json(path, self.to_json())
        return path

    @classmethod
    def load(cls, path: str | Path) -> "SplitManifest":
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------- budget
@dataclass(frozen=True)
class Budget:
    """Per-attempt limits. `max_usd` is a limit, not a measurement: an attempt whose cost cannot be measured is
    not stopped by it, and its result says the cost is unknown."""
    max_steps: int = 50
    max_tool_calls: int = 200
    max_seconds: float = 900.0
    max_usd: float | None = None

    def exceeded(self, *, steps: int = 0, tool_calls: int = 0, seconds: float = 0.0,
                 usd: float | None = None) -> str:
        if steps > self.max_steps:
            return f"steps {steps} > {self.max_steps}"
        if tool_calls > self.max_tool_calls:
            return f"tool calls {tool_calls} > {self.max_tool_calls}"
        if seconds > self.max_seconds:
            return f"seconds {seconds:.0f} > {self.max_seconds:.0f}"
        if self.max_usd is not None and usd is not None and usd > self.max_usd:
            return f"cost {usd:.4f} > {self.max_usd:.4f}"
        return ""


# ---------------------------------------------------------------------------- run identity
@dataclass
class RunManifest:
    """Everything that makes two runs comparable or not (docs/DESIGN.md §5.1).

    `model` carries the provider, the exact model id, reasoning effort, sampling and the declared fallback, so a
    comparison cannot later be explained away by an unrecorded setting; an unused fallback still belongs here
    because it is part of what was configured.
    """
    candidate: str                            # the version being run: "base", "prompt_best", a registry version
    role: str                                 # base / prompt_best / evolved / frontier_a / frontier_b
    split: str                                # which split this run read
    model: dict                               # provider, model, effort, sampling, fallback
    prompt_hash: str = ""
    decisions: dict = field(default_factory=dict)      # point -> spec hash, and the execution policy
    model_ref: str | None = None              # the trained decision model, when one acts
    policy: str = "default"                   # which execution policy ran: "default" or the decision model
    environment: dict = field(default_factory=dict)    # upstream commit and versions, toolset, step limit
    budget: Budget = field(default_factory=Budget)
    data: str = ""                            # SplitManifest.hash()
    repeats: int = 1
    notes: str = ""
    # v2 (docs/modules/00_CONTRACTS.md §2): the complete identity of what produced the run. Each one is left out
    # of the payload while it is unset, so a v1 manifest keeps the hash its results were recorded under.
    system_release_ref: str | None = None
    worker_ref: str | None = None
    research_ref: str | None = None
    experience_ref: str | None = None
    core_ref: str | None = None
    eval_plan_ref: str | None = None
    feedback_ref: str | None = None
    input_schema_refs: tuple[str, ...] = ()
    data_lineage_ref: str | None = None
    budget_scope: str | None = None
    pricing_ref: str | None = None
    capability_ref: str | None = None

    def payload(self) -> dict:
        d = asdict(self)
        d["budget"] = asdict(self.budget)
        d["input_schema_refs"] = list(self.input_schema_refs)
        return {k: v for k, v in d.items()
                if k != "notes" and not (k in V2_FIELDS and not v)}

    def v2(self) -> bool:
        """Whether this manifest carries the v2 identity. A run missing any of it is a v1 run, and saying so is
        the point: a report that needs the release, the core and the plan may not read a manifest that has none
        of them as though it had them (CT-02)."""
        return all(getattr(self, k) for k in ("system_release_ref", "worker_ref", "research_ref", "core_ref"))

    def hash(self) -> str:
        return digest(self.payload())[:16]

    def to_json(self) -> dict:
        return {**self.payload(), "notes": self.notes, "hash": self.hash()}

    @classmethod
    def from_json(cls, d: dict) -> "RunManifest":
        d = dict(d)
        d.pop("hash", None)
        budget = d.pop("budget", None) or {}
        if "input_schema_refs" in d:
            d["input_schema_refs"] = tuple(d["input_schema_refs"] or ())
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__}, budget=Budget(**budget))


# ---------------------------------------------------------------------------- one attempt
@dataclass
class TaskResult:
    """One attempt at one full task.

    `success` is the official `task_completed_correctly`; `partial_credit` is the official assertion fraction and
    is never reported as a pass rate. `validity` separates a task the agent failed (valid evidence) from an
    attempt that never ran properly (no evidence, and refilled under a fixed rule, docs/DESIGN.md §5.2).
    `cost_usd` is None when it was not measured, and stays None all the way into the summary.
    """
    task_id: str
    family_id: str
    split: str
    attempt: int
    candidate: str
    run: str                                  # RunManifest.hash()
    manifest: str                             # SplitManifest.hash()
    contract: str = ""                        # the task contract this attempt actually ran
    success: bool = False
    partial_credit: float = 0.0
    assertions_passed: int = 0
    assertions_scored: int = 0
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    elapsed: float = 0.0
    steps: int = 0
    tool_calls: int = 0
    termination: str = "finished"
    stop: str = ""                            # the harness's own stop reason, as evidence behind `termination`
    validity: str = "valid"
    invalid_reason: str = ""
    evidence: str = ""                        # the session id; its transcript is transcripts/<session>.jsonl
    decisions: list[str] = field(default_factory=list)
    labelled: int = 0                         # decisions the completion probe labelled from this attempt
    # v2: which attempt this was, which pair it belongs to, and what produced it. `pair_key` is the formal
    # pairing key (plan, task, repeat): two arms are compared on it, never on a session id that happens to match.
    attempt_id: str = ""
    pair_key: str = ""
    system_release_ref: str | None = None
    failure_class: str = ""                   # task_outcome | candidate_defect | infrastructure | evaluator
    supersedes: str = ""                      # the attempt_id this refill replaces; the original stays on record
    budget_receipt_ref: str | None = None
    observation_refs: list[str] = field(default_factory=list)
    schema: str = RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.termination not in TERMINATIONS:
            raise ValueError(f"termination {self.termination!r}: one of {list(TERMINATIONS)}")
        if self.validity not in ("valid", "invalid"):
            raise ValueError(f"validity {self.validity!r}: valid or invalid")
        if self.validity == "invalid" and not self.invalid_reason:
            raise ValueError("an invalid attempt needs a reason")
        if self.validity == "valid" and self.termination not in VALID_TERMINATIONS:
            raise ValueError(f"termination {self.termination!r} is an infrastructure failure, not a task outcome: "
                             f"the attempt is invalid")
        if self.success and self.partial_credit < 1.0:
            raise ValueError("task_completed_correctly is 1.0 only when every scored assertion passed")

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "TaskResult":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def broken(cls, task_id: str, family_id: str, split: str, attempt: int, run: RunManifest, manifest: str,
               reason: str, *, termination: str = "infrastructure", **fields) -> "TaskResult":
        """An attempt that produced no evidence about the agent. Kept on record: how often the runner broke is
        part of the report, and a rerun replaces it under a rule fixed in advance, never by deletion."""
        return cls(task_id, family_id, split, attempt, run.candidate, run.hash(), manifest,
                   termination=termination, validity="invalid", invalid_reason=reason[:500], **fields)


# ---------------------------------------------------------------------------- results on disk
class Results:
    """Append-only per-attempt results of one domain, under <root>/bench/<domain>/results.jsonl."""

    def __init__(self, root: str | Path, domain: str):
        self.root = Path(root) / "bench" / domain
        self.path = self.root / "results.jsonl"

    def append(self, result: TaskResult) -> TaskResult:
        _append_jsonl(self.path, redact({"ts": now(), **result.to_json()}))
        return result

    def all(self) -> Iterator[TaskResult]:
        for row in _read_jsonl(self.path):
            yield TaskResult.from_json(row)

    def of_run(self, run: str) -> list[TaskResult]:
        return [r for r in self.all() if r.run == run]

    def manifest_path(self) -> Path:
        return self.root / "splits.json"

    def put_run(self, run: RunManifest) -> Path:
        """Record a run manifest beside the results its attempts carry the hash of."""
        path = self.root / "runs" / f"{run.hash()}.json"
        write_json(path, run.to_json())
        return path


def summarise(results: list[TaskResult]) -> dict:
    """What a run produced, aggregated by task first so repeats of one task do not count as separate tasks.

    Only valid attempts are scored. A task is counted once, as the mean of its valid attempts, because otherwise
    a task that happened to be retried more often would weigh more than the others. Cost is summed over the
    attempts that have one and `cost_missing` says how many did not: an attempt with no cost is not free.
    """
    valid = [r for r in results if r.validity == "valid"]
    per_task: dict[str, list[TaskResult]] = {}
    for r in valid:
        per_task.setdefault(r.task_id, []).append(r)
    successes = [sum(x.success for x in rs) / len(rs) for rs in per_task.values()]
    partials = [sum(x.partial_credit for x in rs) / len(rs) for rs in per_task.values()]
    costed = [r.cost_usd for r in valid if r.cost_usd is not None]
    return {
        "tasks": len(per_task),
        "attempts": len(valid),
        "invalid": len(results) - len(valid),
        "invalid_reasons": sorted({r.invalid_reason.split(":")[0] for r in results if r.validity == "invalid"}),
        # The official pass rate: the mean over tasks of task_completed_correctly.
        "pass_rate": round(sum(successes) / len(successes), 4) if successes else None,
        "passed": sum(1 for s in successes if s == 1.0),
        "partial_credit": round(sum(partials) / len(partials), 4) if partials else None,
        "families": len({r.family_id for r in valid}),
        "terminations": {t: sum(r.termination == t for r in valid) for t in VALID_TERMINATIONS
                         if any(r.termination == t for r in valid)},
        "usage": {k: sum(r.usage.get(k, 0) for r in valid)
                  for k in sorted({k for r in valid for k in r.usage})},
        "cost_usd": round(sum(costed), 4) if costed else None,
        "cost_missing": len(valid) - len(costed),
        "elapsed": round(sum(r.elapsed for r in valid), 1),
        "tool_calls": sum(r.tool_calls for r in valid),
    }
