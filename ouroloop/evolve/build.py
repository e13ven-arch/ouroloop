"""Ledger -> TDE training data (PLAN §4.1). Each labelled decision is re-rendered with the point's current spec and
written as a TDE DecisionExample, so TDE's trainer reads the files directly."""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

from .replay import in_calibration, labelled, not_held_out, splits

VAL_FRAC = 0.1      # share of the training sessions the trainer may select its checkpoint on (PLAN_v0.2 M9)


def candidates(q) -> list[dict]:
    """Candidates with descriptions, in the same form TDE's Decider builds them at inference time."""
    if q.type == "noul":
        crit = q.criteria or {}
        return [{"name": "yes", "description": str(crit.get("true") or "")},
                {"name": "no", "description": str(crit.get("false") or "")}]
    if q.type == "choice":
        return [{"name": str(k), "description": str(v or "")} for k, v in q.criteria.items()]
    return [{"name": str(i), "description": str(v)} for i, v in enumerate(q.criteria)]


def to_example(rt, point, row, target: dict, weight: float, split: str) -> dict | None:
    """The row as a TDE example, its candidates in the order `predict` and a live ask present them (shuffled, seeded on
    the decision id), not in the order they were written."""
    ctx = rt.sessions.get(row.decision["ctx"])
    view, truncated = point.render(ctx)
    q = point.question(ctx).presented(random.Random(row.decision["id"]))
    cands = candidates(q)
    t = [float(target.get(c["name"], 0.0)) for c in cands]
    if sum(t) <= 0:
        return None   # the observed label is not among the current candidates
    return {"id": row.decision["id"], "source_id": row.decision.get("session", ""), "dataset": point.name,
            "split": "calibration" if split == "val" else split,      # TDE's name for what it selects on
            "primitive": q.type, "state": view, "question": q.instructions, "candidates": cands,
            "target": [x / sum(t) for x in t], "template_id": point.spec_hash,
            "meta": {"weight": weight, "truncated": truncated, "ts": row.decision["ts"],
                     "source_spec": row.decision.get("spec_hash")}}


@dataclass
class Dataset:
    dir: Path
    splits: dict[str, list[dict]]    # "train" / "val" / "calibration" / "test" -> TDE example dicts

    def stats(self) -> dict:
        out: dict = {}
        for name, examples in self.splits.items():
            for e in examples:
                out.setdefault(e["dataset"], {}).setdefault(name, 0)
                out[e["dataset"]][name] += 1
        return out


def build(rt, out_dir: str | Path, points: list[str] | None = None, test_frac: float = 0.15,
          cal_frac: float = 0.15, min_weight: float = 0.5, *, rows: dict | None = None) -> Dataset:
    """`rows` ({point: {split: rows}}, as a gate record splits them) replaces the split by test_frac and cal_frac of
    the rows outside held-out tasks. Held-out rows never become examples. The directory is the trainer's and holds the
    training sessions only: train.jsonl, and val.jsonl, a part of them by a hash of the session id that the trainer
    may select its checkpoint on. The calibration and test examples stay in the returned Dataset, for the data
    checks: thresholds fitted on rows a checkpoint was selected on would be optimistic."""
    if rows is None:
        rows = {name: splits(not_held_out(rt, [r for r in labelled(rt, name) if r[2] >= min_weight]), test_frac,
                             cal_frac) for name in points or list(rt.points)}
    parts: dict[str, list[dict]] = {"train": [], "val": [], "calibration": [], "test": []}
    for name, split_rows in rows.items():
        for split_name in ("train", "calibration", "test"):
            for row, target, weight in split_rows.get(split_name, []):
                part = split_name
                if split_name == "train" and in_calibration(f"val:{row.decision.get('session', '')}", VAL_FRAC):
                    part = "val"
                ex = to_example(rt, rt.points[name], row, target, weight, part)
                if ex is not None:
                    parts[part].append(ex)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for split_name in ("train", "val"):
        with open(out / f"{split_name}.jsonl", "w", encoding="utf-8") as f:
            for ex in parts[split_name]:
                f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    return Dataset(out, parts)
