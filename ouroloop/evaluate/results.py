"""What a run was and what each attempt did (docs/design/EVALUATE.md §2, §4): RunManifest, TaskResult, Results on disk,
and the summary. Missing cost is missing, not zero: a summary reports how many attempts have no cost rather than
averaging them in as free. Old records stay readable: a RunManifest refuses an unknown key, so every field an old
manifest carries is still declared, and its hash is taken exactly as it was written.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from ..contracts import append_jsonl, now, read_jsonl, redact, write_json
from .limits import LIMIT_DIMENSIONS, V03_BUDGET_FIELDS, Budget
from .splits import digest

RESULT_SCHEMA = "ouroloop.bench.result.v1"
# Where the attempt stopped. The first four are task outcomes and count; the last is not evidence about the agent and
# makes the attempt invalid. `step_limit` is an old record's name for a budget stop; `harness_error` is the package's
# own defect (a hook outside its protocol, a package tool that raised), scored as the world stands.
TERMINATIONS = ("finished", "step_limit", "budget", "tool_refused", "harness_error", "infrastructure")
VALID_TERMINATIONS = ("finished", "step_limit", "budget", "tool_refused", "harness_error")
# The RunManifest fields of v2 and v0.6: left out of the payload while unset, so every recorded run keeps its hash.
V2_FIELDS = ("system_release_ref", "worker_ref", "research_ref", "experience_ref", "core_ref",
             "eval_plan_ref", "feedback_ref", "input_schema_refs", "data_lineage_ref", "budget_scope",
             "pricing_ref", "capability_ref")
V06_FIELDS = ("mode", "actor_identity_ref", "target_harness_ref", "package_ref")


def attempt_key(run: str, task_id: str, repeat_index: int, arm: str = "subject") -> str:
    return f"{run}/{task_id}/r{repeat_index}/{arm}"


def attempt_id(run: str, task_id: str, repeat_index: int, arm: str = "subject", retry_index: int = 0) -> str:
    return f"{attempt_key(run, task_id, repeat_index, arm)}#{retry_index}"


def pair_key(plan_ref: str, task_id: str, repeat_index: int) -> str:
    return f"{plan_ref}/{task_id}/r{repeat_index}"


# ---------------------------------------------------------------------------- run identity
@dataclass
class RunManifest:
    """Everything that makes two runs comparable or not (docs/DESIGN.md §5.1).

    `model` carries the provider, the exact model id, reasoning effort, sampling and the declared fallback, so a
    comparison cannot later be explained away by an unrecorded setting; an unused fallback still belongs here
    because it is part of what was configured.
    """
    candidate: str                            # the version being run: "base", "prompt_best", a registry version
    role: str                                 # base / prompt_best / evolved (frontier_a/_b: v0.4 records)
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
    # v2 (docs/design/CONTRACTS.md §7): the complete identity of what produced the run. Each one is left out
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
    mode: str | None = None                   # v0.6: task_evaluation / collection / deployment
    actor_identity_ref: str | None = None     # the ModelIdentity the actor actually ran
    target_harness_ref: str | None = None
    package_ref: str | None = None            # the capability package the run mounted

    def payload(self) -> dict:
        d = asdict(self)
        # A v0.3 budget field that is unset is left out, so a run recorded before the field existed keeps the hash
        # its attempts are found under; set, it is part of what the run was limited by.
        d["budget"] = {k: v for k, v in asdict(self.budget).items() if not (k in V03_BUDGET_FIELDS and v is None)}
        d["input_schema_refs"] = list(self.input_schema_refs)
        return {k: v for k, v in d.items()
                if k != "notes" and not (k in V2_FIELDS and not v) and not (k in V06_FIELDS and v is None)}

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
        """Unknown keys are refused, not dropped: a reader that silently dropped a field it does not know would
        report a manifest as something it was not (docs/design/CONTRACTS.md §2)."""
        d = dict(d)
        d.pop("hash", None)
        budget = d.pop("budget", None) or {}
        unknown = sorted(set(d) - set(cls.__dataclass_fields__))
        if unknown:
            raise ValueError(f"RunManifest does not take {unknown}")
        if "input_schema_refs" in d:
            d["input_schema_refs"] = tuple(d["input_schema_refs"] or ())
        return cls(**d, budget=Budget(**budget))


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
    # v0.3: which hard limit ended a `budget` termination (one of LIMIT_DIMENSIONS); None on every other
    # termination and on every record written before the field existed, where `step_limit` says the same thing.
    limit: str | None = None
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
        if self.limit is not None and (self.termination != "budget" or self.limit not in LIMIT_DIMENSIONS):
            raise ValueError(f"limit {self.limit!r}: one of {list(LIMIT_DIMENSIONS)}, and only on a budget "
                             f"termination (this one is {self.termination!r})")
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
        append_jsonl(self.path, redact({"ts": now(), **result.to_json()}))
        return result

    def all(self) -> Iterator[TaskResult]:
        for row in read_jsonl(self.path):
            yield TaskResult.from_json(row)

    def of_run(self, run: str) -> list[TaskResult]:
        return [r for r in self.all() if r.run == run]

    def runs(self) -> dict[str, RunManifest]:
        """hash -> the run manifests recorded beside the results (`put_run`). A manifest this reader cannot parse
        is left out rather than read as something it is not."""
        out: dict[str, RunManifest] = {}
        directory = self.root / "runs"
        for path in sorted(directory.glob("*.json")) if directory.exists() else ():
            try:
                run = RunManifest.from_json(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, TypeError):
                continue
            out[run.hash()] = run
        return out

    def manifest_path(self) -> Path:
        return self.root / "splits.json"

    def put_run(self, run: RunManifest) -> Path:
        """Record a run manifest beside the results its attempts carry the hash of."""
        path = self.root / "runs" / f"{run.hash()}.json"
        write_json(path, run.to_json())
        return path

    # The per-assertion outcome of an attempt is the grader's answer key, so it is kept beside the results rather
    # than in them: results.jsonl never carries it, and the only reader hands out train detail by default
    # (docs/design/EVALUATE.md §4). The raw record keeps every entry, excluded ones too, for audit.
    def grading_path(self) -> Path:
        return self.root / "grading.jsonl"

    def put_grading(self, result: TaskResult, assertions) -> None:
        if not result.attempt_id or not assertions:
            return
        rows = [{"type": a.get("type"), "params": a.get("params"), "passed": bool(a.get("passed")),
                 "excluded": bool(a.get("excluded"))} for a in assertions]
        append_jsonl(self.grading_path(),
                      redact({"ts": now(), "attempt_id": result.attempt_id, "task_id": result.task_id,
                              "split": result.split, "run": result.run, "assertions": rows}))

    def copy_grading(self, source: "Results", attempt_ids) -> int:
        """Copy other workspaces' grading rows for these attempts, as recorded; rows already here are kept.
        -> the rows copied. Used when a measurement made once is a common input of several branches."""
        wanted = set(attempt_ids) - {row.get("attempt_id") for row in read_jsonl(self.grading_path())}
        rows = [row for row in read_jsonl(source.grading_path()) if row.get("attempt_id") in wanted]
        for row in rows:
            append_jsonl(self.grading_path(), row)
        return len(rows)

    def grading(self, attempt_id: str, *, splits: Iterable[str] = ("train",)) -> list[dict]:
        """The recorded assertion outcomes of one attempt; empty when there are none or when its split is not
        among `splits`. The split comes from the record, written from the frozen manifest at run time."""
        allowed = set(splits)
        for row in read_jsonl(self.grading_path()):
            if row.get("attempt_id") == attempt_id:
                return list(row.get("assertions") or []) if row.get("split") in allowed else []
        return []


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
        # M5 §2 (v0.3): the mean cost of one valid attempt, under the one name the gate receipt and the report use.
        "cost_per_attempt": round(sum(costed) / len(costed), 6) if costed else None,
        "cost_missing": len(valid) - len(costed),
        "elapsed": round(sum(r.elapsed for r in valid), 1),
        "tool_calls": sum(r.tool_calls for r in valid),
    }
