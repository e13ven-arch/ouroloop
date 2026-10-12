"""The improver's ledger (docs/design/IMPROVER.md §10): one row per stage of every round, append-only, at
<workspace>/ledger/experience.jsonl. Written by fixed code only; R reads a projection of it.

A row is {id, ts, stage, round_id, ...}; ids are `L<n>` in write order, so a playbook lesson can cite the row it rests
on and the citation can be checked. Rows never hold a dev task id: the release gate's row keeps the verdict, the gain
and the counts of fixed and regressed dev tasks, not which ones (they are in the receipt, which R never reads). Rows
name held-out train tasks (a screen's fixed and regressed lists); the projection R reads turns them into counts.
"""
from __future__ import annotations

import json

from pathlib import Path

from ..contracts import append_jsonl, locked, now, read_jsonl

STAGES = ("measured", "diagnosed", "focused", "declared", "verified", "screened", "accepted", "gated", "installed",
          "rejected", "credited", "removal_proposed", "no_code_candidate", "vocabulary_added", "l6_unavailable",
          "stopped", "playbook")
# What R reads of a row (the projection): train facts and counts. Anything else of a row stays with the host.
TASK_LISTS = ("predicted_affected", "fixed", "regressed")       # the projected fields that name train tasks
PROJECTED = ("id", "stage", "round_id", "candidate", "dir", "cause", "cause_id", "kind", "site", "trigger",
             "mechanism", "components", "predicted_affected", "differs_from", "fixed", "regressed", "net_flips",
             "fixes", "holdout_score", "best_so_far", "accepted", "reasons", "failed_components", "outcome", "verdict",
             "gain", "fixed_count", "regressed_count", "passed", "steps", "reason", "causes", "require_code",
             "component", "credit", "names", "status", "holdout_passed", "holdout")


def _scrub(value, hide: set):
    if isinstance(value, str):
        for t in hide:
            if t in value:
                value = value.replace(t, "<a held-out task>")
        return value
    if isinstance(value, list):
        return [_scrub(v, hide) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, hide) for k, v in value.items()}
    return value


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

    def projection(self, *, line: str | None = None, hide=frozenset()) -> list[dict]:
        """What R reads: every row's projected fields (train task ids and counts only). A task of `hide` (the
        held-out train tasks R never sees) is left out of the lists that name tasks; a `<field>_held_out` count says
        how many there were."""
        hide, out = set(hide), []
        for r in read_jsonl(self.path):
            if line is not None and r.get("line") not in (None, line):
                continue
            row = {k: r[k] for k in PROJECTED if k in r and r[k] not in (None, "", [], {})}
            if hide:                # a held-out id inside a text (a defect screen's reason names its task)
                row = {k: _scrub(v, hide) if k not in TASK_LISTS else v for k, v in row.items()}
            for k in TASK_LISTS:
                if hide and isinstance(row.get(k), list):
                    held = [t for t in row[k] if t in hide]
                    row[k] = [t for t in row[k] if t not in hide]
                    if held:
                        row[f"{k}_held_out"] = len(held)
                    if not row[k]:
                        del row[k]
            out.append(row)
        return out

    def scoreboard(self, line: str | None = None) -> dict:
        """How each kind of candidate did on the train screens, in counts: per kind and in all, the candidates
        screened, the conditioned fixes, the regressions and the net (the screens run on the held-out train tasks,
        which R never sees, so a prediction cannot be matched there)."""
        board: dict = {}
        for r in self.rows(stage="screened"):
            if line is not None and r.get("line") not in (None, line):
                continue
            if r.get("outcome") != "screened":
                continue
            fixed, regressed = len(r.get("fixed") or ()), len(r.get("regressed") or ())
            for key in ("all", *str(r.get("kind") or "?").split("+")):
                slot = board.setdefault(key, {"candidates": 0, "fixed": 0, "regressed": 0, "net": 0})
                slot["candidates"] += 1
                slot["fixed"] += fixed
                slot["regressed"] += regressed
                slot["net"] += fixed - regressed
        return dict(sorted(board.items()))

    def last_round_of(self, task_id: str) -> int:
        """The index of the latest round that diagnosed this task (-1: never): the least recently diagnosed first."""
        last = -1
        for r in self.rows(stage="diagnosed"):
            if task_id in (r.get("tasks") or ()):
                last = max(last, int(r.get("index", -1)))
        return last
