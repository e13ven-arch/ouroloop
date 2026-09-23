"""Ledger -> TDE training data (PLAN §4.1). Each labelled decision is re-rendered with the point's current spec and
written as a TDE DecisionExample, so TDE's trainer reads the files directly."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .replay import labelled, splits


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
    ctx = rt.sessions.get(row.decision["ctx"])
    view, truncated = point.render(ctx)
    q = point.question(ctx)
    cands = candidates(q)
    t = [float(target.get(c["name"], 0.0)) for c in cands]
    if sum(t) <= 0:
        return None   # the observed label is not among the current candidates
    return {"id": row.decision["id"], "source_id": row.decision.get("session", ""), "dataset": point.name,
            "split": split, "primitive": q.type, "state": view, "question": q.instructions, "candidates": cands,
            "target": [x / sum(t) for x in t], "template_id": point.spec_hash,
            "meta": {"weight": weight, "truncated": truncated, "ts": row.decision["ts"],
                     "source_spec": row.decision.get("spec_hash")}}


@dataclass
class Dataset:
    dir: Path
    splits: dict[str, list[dict]]    # "train" / "calibration" / "test" -> TDE example dicts

    def stats(self) -> dict:
        out: dict = {}
        for name, examples in self.splits.items():
            for e in examples:
                out.setdefault(e["dataset"], {}).setdefault(name, 0)
                out[e["dataset"]][name] += 1
        return out


def build(rt, out_dir: str | Path, points: list[str] | None = None, test_frac: float = 0.15,
          cal_frac: float = 0.15, min_weight: float = 0.5) -> Dataset:
    parts: dict[str, list[dict]] = {"train": [], "calibration": [], "test": []}
    for name in points or list(rt.points):
        rows = [r for r in labelled(rt, name) if r[2] >= min_weight]
        for split_name, split_rows in splits(rows, test_frac, cal_frac).items():
            for row, target, weight in split_rows:
                ex = to_example(rt, rt.points[name], row, target, weight, split_name)
                if ex is not None:
                    parts[split_name].append(ex)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for split_name, examples in parts.items():
        with open(out / f"{split_name}.jsonl", "w", encoding="utf-8") as f:
            for ex in examples:
                f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    return Dataset(out, parts)
