"""Ledger -> training data for the v1 champion pipeline (docs/DESIGN.md §2.2, pipeline 1; goes with it in P1 step 5;
the task profile's datasets are ouroloop/train/dataset.py). Each labelled decision is re-rendered with the point's
current spec and written as one example (train/dataset.py's to_example), a JSON line:

    id, source_id (the session), dataset (the point), split ("train" or "val"), primitive (noul, choice, score),
    state (the rendered view), question, candidates [{name, description}], target (a distribution over the
    candidates), template_id (the spec hash), meta {weight, truncated, ts, source_spec}

A plugin maps them to its trainer's format itself (trainers/tde.py: TDE's DecisionExample)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..train.dataset import to_example
from .replay import in_calibration, labelled, not_held_out, splits

# The share of the training sessions the trainer may select its checkpoint on (docs/DESIGN.md invariant 3).
VAL_FRAC = 0.1


@dataclass
class Dataset:
    dir: Path
    splits: dict[str, list[dict]]    # "train" / "val" / "calibration" / "test" -> example dicts

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


