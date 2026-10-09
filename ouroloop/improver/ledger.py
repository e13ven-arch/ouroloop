"""The improver's ledger (docs/design/IMPROVER.md §10): one row per stage of every round, append-only, at
<workspace>/ledger/experience.jsonl. Written by fixed code only; R reads a projection of it.

A row is {id, ts, stage, round_id, ...}; ids are `L<n>` in write order, so a playbook lesson can cite the row it rests
on and the citation can be checked. Rows never hold a dev task id: a gate row keeps the verdict, the gain and the
counts of fixed and regressed dev tasks, not which ones (they are in the receipt, which R never reads).
"""
from __future__ import annotations

import json

from pathlib import Path

from ..contracts import append_jsonl, locked, now, read_jsonl

STAGES = ("measured", "diagnosed", "allocated", "declared", "verified", "screened", "gated", "installed", "rejected",
          "credited", "removal_proposed", "no_code_candidate", "vocabulary_added", "l6_unavailable", "stopped",
          "playbook")
# What R reads of a row (the projection): train facts and counts. Anything else of a row stays with the host.
PROJECTED = ("id", "stage", "round_id", "candidate", "dir", "cause", "cause_id", "kind", "site", "trigger",
             "predicted_affected", "differs_from", "fixed", "regressed", "net_flips", "fixes", "outcome", "verdict",
             "gain", "fixed_count", "regressed_count", "passed", "steps", "reason", "assignments", "causes",
             "component", "credit", "names", "status")


class Ledger:
    def __init__(self, root):
        self.path = Path(root) / "ledger" / "experience.jsonl"

    def row(self, stage: str, **fields) -> dict:
        if stage not in STAGES:
            raise ValueError(f"ledger stage {stage!r}: one of {list(STAGES)}")
        with locked(self.path):
            rows = list(read_jsonl(self.path))
            if fields.get("round_id"):      # a resumed step writes its rows again: the same row is the one on record
                same = json.dumps({"stage": stage, **fields}, sort_keys=True, default=str)
                for r in rows:
                    if r.get("round_id") == fields["round_id"] and same == json.dumps(
                            {k: v for k, v in r.items() if k not in ("id", "ts")}, sort_keys=True, default=str):
                        return r
            row = {"id": f"L{len(rows) + 1}", "ts": now(), "stage": stage, **fields}
            append_jsonl(self.path, row)
        return row

    def rows(self, *, stage: str | None = None, round_id: str | None = None) -> list[dict]:
        return [r for r in read_jsonl(self.path) if (stage is None or r.get("stage") == stage)
                and (round_id is None or r.get("round_id") == round_id)]

    def ids(self) -> set[str]:
        return {r.get("id") for r in read_jsonl(self.path)}

    def projection(self, *, line: str | None = None) -> list[dict]:
        """What R reads: every row's projected fields (train task ids and counts only)."""
        out = []
        for r in read_jsonl(self.path):
            if line is not None and r.get("line") not in (None, line):
                continue
            out.append({k: r[k] for k in PROJECTED if k in r and r[k] not in (None, "", [], {})})
        return out

    def scoreboard(self, line: str | None = None) -> dict:
        """How R's predictions held up on the train screens: per kind and in all, the predicted tasks that the
        screen fixed (hits), the predicted that it did not (misses), and the regressions nobody predicted."""
        board: dict = {}
        for r in self.rows(stage="screened"):
            if line is not None and r.get("line") not in (None, line):
                continue
            predicted = set(r.get("predicted_affected") or ())
            fixed, regressed = set(r.get("fixed") or ()), set(r.get("regressed") or ())
            for key in ("all", str(r.get("kind") or "?")):
                slot = board.setdefault(key, {"candidates": 0, "hits": 0, "misses": 0, "unpredicted_regressions": 0})
                slot["candidates"] += 1
                slot["hits"] += len(predicted & fixed)
                slot["misses"] += len(predicted - fixed)
                slot["unpredicted_regressions"] += len(regressed - predicted)
        return dict(sorted(board.items()))

    def last_round_of(self, task_id: str) -> int:
        """The index of the latest round that diagnosed this task (-1: never): the least recently diagnosed first."""
        last = -1
        for r in self.rows(stage="diagnosed"):
            if task_id in (r.get("tasks") or ()):
                last = max(last, int(r.get("index", -1)))
        return last
