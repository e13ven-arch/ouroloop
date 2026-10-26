"""Frozen task groups and splits (docs/design/EVALUATE.md §2, §3, §9): which tasks exist, which family each belongs to,
and which of train / dev / test it is in. Frozen to one file before the first model run; a later run verifies against
it and reports drift instead of quietly resplitting.

A split is assigned per family, never per task and never per trajectory: every attempt, branch and repeat of a task
belongs to its task's family, so nothing derived from a dev task can end up scored as test. The manifest's hash is
taken exactly as it always was (json sorted, default=str), so every frozen split keeps its hash.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..contracts import now, write_json

SPLIT_SCHEMA = "ouroloop.bench.splits.v1"    # the record format's name predates the project's rename (2026-10-26); kept so every frozen split still reads
SPLITS = ("train", "dev", "test")
SPLIT_RULES = ("hash", "stratified")


def digest(obj: Any) -> str:
    """The digest split and run manifests are named by (not contracts.digest: the bytes differ, the hashes stay)."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class Frozen(RuntimeError):
    """A frozen manifest is already on disk and says something else."""


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

    @classmethod
    def of(cls, entry) -> "TaskEntry":
        """An adapter's entry (a dict with these keys, or anything with them as attributes) as a TaskEntry: adapters
        do not import the framework, so they hand plain data."""
        if isinstance(entry, cls):
            return entry
        if isinstance(entry, dict):
            return cls.from_json(entry)
        return cls(entry.task_id, entry.family_id, getattr(entry, "example_id", None), entry.contract,
                   tuple(getattr(entry, "tools", ()) or ()))


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
        x = _hash_point(fam, seed)
        out[fam] = next(name for edge, name in edges if x < edge or edge == edges[-1][0])
    return out


def _hash_point(family: str, seed: int) -> float:
    """Where the hash rule puts a family on [0, 1): the one number both split rules order families by."""
    return int(hashlib.sha256(f"{seed}:{family}".encode()).hexdigest()[:16], 16) / float(1 << 64)


def stratified_splits(strata: dict[str, str], ratios: dict[str, float],
                      seed: int = 0) -> tuple[dict[str, str], dict[str, dict[str, int]]]:
    """family_id -> split under the stratified rule, and each stratum's split counts (what the manifest records).

    `strata` maps each family to its stratum, a key made from the task definition only (the adapter's
    `stratum`: the scored-assertion kinds and the tool family) — no result, no score, nothing a model did; the
    split is frozen before any run (M7 §2). Families are ordered by stratum and, inside a stratum, by the point the
    hash rule gives them under `seed`; one largest-deficit sequence over that order decides how many of each
    stratum's families each split gets, so the whole domain's counts are the ratios' (50/25/25 of 100 at
    0.5/0.25/0.25) and each stratum's are within one or two of its share. Inside a stratum the families then fill
    the splits in SPLITS order by their hash point — the hash rule's order, with the edges at counts rather than at
    fixed fractions. Deterministic under the seed; the order families are listed in does not matter."""
    if set(ratios) - set(SPLITS) or not ratios:
        raise ValueError(f"split ratios name {sorted(SPLITS)}, not {sorted(ratios)}")
    total = sum(ratios.values())
    if total <= 0 or any(v < 0 for v in ratios.values()):
        raise ValueError(f"split ratios must be non-negative and sum above zero: {ratios}")
    names = [s for s in SPLITS if ratios.get(s)]
    share = {s: ratios[s] / total for s in names}
    order = sorted(dict.fromkeys(strata), key=lambda f: (strata[f], _hash_point(f, seed), f))
    sequence, counts = [], {s: 0 for s in names}
    for i in range(len(order)):
        # the split furthest behind its share after i+1 families; a tie goes to the earlier split in SPLITS
        pick = max(names, key=lambda s: ((i + 1) * share[s] - counts[s], -SPLITS.index(s)))
        counts[pick] += 1
        sequence.append(pick)
    out: dict[str, str] = {}
    per: dict[str, dict[str, int]] = {}
    start = 0
    for key in dict.fromkeys(strata[f] for f in order):
        members = [f for f in order if strata[f] == key]
        slots = sequence[start:start + len(members)]
        start += len(members)
        quota = {s: slots.count(s) for s in names}
        per[key] = {s: quota.get(s, 0) for s in SPLITS}
        i = 0
        for s in names:
            for fam in members[i:i + quota[s]]:
                out[fam] = s
            i += quota[s]
    return out, per



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
    # v0.3 step 5 (M7 §2): the rule a new domain was split under and each stratum's counts. Left out of the hashed
    # payload while None, so every manifest frozen before them — Marketing's v0.1/v0.2 split among them — keeps its
    # hash; a stratified manifest carries both.
    split_rule: str | None = None
    strata: dict | None = None

    def __post_init__(self) -> None:
        bad = {s for s in self.splits.values()} - set(SPLITS)
        if bad:
            raise ValueError(f"unknown splits {sorted(bad)}")
        if self.split_rule is not None and self.split_rule not in SPLIT_RULES:
            raise ValueError(f"split rule {self.split_rule!r}: one of {list(SPLIT_RULES)}")
        missing = sorted({e.family_id for e in self.entries} - set(self.splits))
        if missing:
            raise ValueError(f"families with no split: {missing[:5]}")
        dupes = sorted({e.task_id for e in self.entries if sum(x.task_id == e.task_id for x in self.entries) > 1})
        if dupes:
            raise ValueError(f"duplicate task ids: {dupes[:5]}")

    # -------------------------------------------------------------- identity
    def payload(self) -> dict:
        out = {"schema": self.schema, "domain": self.domain, "upstream": self.upstream,
               "family_rule": self.family_rule, "entries": [e.to_json() for e in self.entries],
               "splits": self.splits, "ratios": self.ratios, "seed": self.seed}
        if self.split_rule is not None:
            out["split_rule"] = self.split_rule
        if self.strata is not None:
            out["strata"] = self.strata
        return out

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
                d["splits"], d.get("ratios", {}), d.get("seed", 0), d.get("created", ""),
                split_rule=d.get("split_rule"), strata=d.get("strata"))
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

    def drift(self, entries) -> list[str]:
        """What upstream now says that the manifest does not: tasks added, removed, or whose contract changed.
        Reported, never repaired: a changed task means old results describe a different task."""
        entries = [TaskEntry.of(e) for e in entries]
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


