"""The task profile's training data (docs/modules/02_EVIDENCE_TRAINING.md §4): feedback records and the observations
stored at each decision -> training files and a DataManifest, with no promotion side effect. An example is one JSON
line, the data contract every trainer plugin reads (ouroloop/train/trainers):

    id, source_id (the session), dataset (the point), split ("train" or "val"), primitive (noul, choice, score),
    state (the rendered view), question, candidates [{name, description}], target (a distribution over the
    candidates), template_id (the spec hash), meta {weight, truncated, ts, source_spec, task, family, ...}

A plugin maps them to its trainer's format itself (trainers/tde.py: TDE's DecisionExample)."""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from ..evolve import contracts as ct


def candidates(q) -> list[dict]:
    """Candidates with descriptions, as a model is shown them at inference time (TDE's Decider builds the same)."""
    if q.type == "noul":
        crit = q.criteria or {}
        return [{"name": "yes", "description": str(crit.get("true") or "")},
                {"name": "no", "description": str(crit.get("false") or "")}]
    if q.type == "choice":
        return [{"name": str(k), "description": str(v or "")} for k, v in q.criteria.items()]
    return [{"name": str(i), "description": str(v)} for i, v in enumerate(q.criteria)]


def to_example(rt, point, row, target: dict, weight: float, split: str) -> dict | None:
    """The row as an example, its candidates in the order `predict` presents them (shuffled, seeded on the decision
    id), not in the order they were written: shuffled, as a live ask shuffles them."""
    ctx = rt.sessions.get(row.decision["ctx"])
    view, truncated = point.render(ctx)
    q = point.question(ctx).presented(random.Random(row.decision["id"]))
    cands = candidates(q)
    t = [float(target.get(c["name"], 0.0)) for c in cands]
    if sum(t) <= 0:
        return None   # the observed label is not among the current candidates
    return {"id": row.decision["id"], "source_id": row.decision.get("session", ""), "dataset": point.name,
            "split": split,
            "primitive": q.type, "state": view, "question": q.instructions, "candidates": cands,
            "target": [x / sum(t) for x in t], "template_id": point.spec_hash,
            "meta": {"weight": weight, "truncated": truncated, "ts": row.decision["ts"],
                     "source_spec": row.decision.get("spec_hash")}}


# ---------------------------------------------------------------------------- the task profile's dataset
# docs/modules/02_EVIDENCE_TRAINING.md §4. The grouping rules here are the ones a benchmark quietly breaks:
#
#   A family is the unit. Every session, repeat, prefix branch and declared variant of a task belongs to its
#   task's family, so no sample of a dev or final task can reach a training file through a different session id.
#   The split of the outer train pool into fit, validation and calibration happens once, over families, with the
#   seed and the sorted group ids recorded. Never per row and never per session: rows of one task are not
#   independent, so a row-level split leaks the task across both sides.
#   Repeated decisions do not add independent groups. A cap per task keeps one chatty episode from outvoting
#   twenty others, and the cap is part of the manifest rather than a constant somewhere in the code.
MIN_GROUPS = 2                      # fit and validation each need at least this many families
DEFAULT_MAX_PER_TASK = 20


@dataclass
class DatasetBuild:
    """The result of building one training file set. Nothing here is promoted, deployed or gated."""
    status: str = "ready"                   # ready | insufficient_data | invalid
    dataset_ref: str | None = None
    exclusions: dict = field(default_factory=dict)
    dir: Path | None = None
    stats: dict = field(default_factory=dict)
    manifest: object = None                 # contracts.DataManifest
    groups: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in ("ready", "insufficient_data", "invalid"):
            raise ValueError(f"dataset status {self.status!r}")


def group_families(feedback, *, splits=None, partition: str = "train") -> dict[str, list]:
    """family -> its feedback records, from the frozen family map. A record whose task is not in the manifest, or
    whose family is not in `partition`, is left out and counted by the caller."""
    out: dict[str, list] = {}
    for record in feedback:
        family = record.family_id
        if splits is not None:
            try:
                family = splits.family_of(record.task_id)
            except KeyError:
                continue
            if splits.splits.get(family) != partition:
                continue
        out.setdefault(family or record.task_id, []).append(record)
    return out


def assign_groups(families: list[str], *, seed: int = 0, validation: float = 0.25,
                  calibration: float = 0.0) -> dict[str, list[str]]:
    """One assignment of families to fit / validation / calibration, from the seed and the sorted group ids.

    Sorting first means the assignment does not depend on the order the records happened to arrive in, and the
    seed makes it reproducible; both go into the manifest.
    """
    ordered = sorted(set(families))
    rng = random.Random(f"{seed}:{len(ordered)}")
    shuffled = list(ordered)
    rng.shuffle(shuffled)
    n_val = max(1, round(len(shuffled) * validation)) if validation and len(shuffled) > 1 else 0
    n_cal = max(1, round(len(shuffled) * calibration)) if calibration and len(shuffled) - n_val > 1 else 0
    return {"validation": sorted(shuffled[:n_val]), "calibration": sorted(shuffled[n_val:n_val + n_cal]),
            "fit": sorted(shuffled[n_val + n_cal:])}


def build_task_dataset(rt, point, feedback, *, out_dir, objects, splits=None, seed: int = 0,
                       validation: float = 0.25, calibration: float = 0.0, max_per_task: int = DEFAULT_MAX_PER_TASK,
                       min_groups: int = MIN_GROUPS, input_schema: str = "stop.business.v1",
                       transform_ref: str | None = None,
                       allowed_partitions: tuple[str, ...] = ("train",)) -> DatasetBuild:
    """Feedback + stored observations -> training files and a DataManifest, with no promotion side effect.

    Each example is re-rendered from the observation that was stored at the decision, through the candidate's own
    view. A view that needs a field the observation does not have is refused rather than filled in from the final
    state: the missing field means the data has to be collected again (DT-04).
    """
    out = DatasetBuild(dir=Path(out_dir))
    records = [r for r in feedback.records if r.status == "valid" and r.target]
    out.exclusions = dict(feedback.excluded)
    outside = check_lineage(feedback, allowed_partitions, objects=objects)
    if outside:
        out.status = "invalid"
        out.exclusions["outside_data_boundary"] = outside[:10]
        return out                  # nothing is written and no trainer is started (DT-05)

    def exclude(reason: str) -> None:
        out.exclusions[reason] = out.exclusions.get(reason, 0) + 1

    by_family = group_families(records, splits=splits)
    if splits is not None:
        for r in records:
            if r not in [x for group in by_family.values() for x in group]:
                exclude("outside_train_split")
    groups = assign_groups(list(by_family), seed=seed, validation=validation, calibration=calibration)
    rows = {"fit": [], "validation": [], "calibration": []}
    per_task: dict[str, int] = {}
    seen: set[str] = set()
    ledger_rows = {r.decision["id"]: r for r in rt.ledger.rows(point.name)}
    for part, family_ids in groups.items():
        for family in family_ids:
            for record in by_family.get(family, []):
                if record.decision_id in seen:
                    exclude("duplicate_decision")
                    continue
                seen.add(record.decision_id)
                if per_task.get(record.task_id, 0) >= max_per_task:
                    exclude("over_task_cap")
                    continue
                row = ledger_rows.get(record.decision_id)
                if row is None or not row.decision.get("ctx"):
                    exclude("no_stored_observation")
                    continue
                try:
                    example = to_example(rt, point, row, record.target, 1.0, part)
                except KeyError as e:
                    exclude(f"missing_observation_field:{e}")       # collect again; never fill from the future
                    continue
                if example is None:
                    exclude("label_not_a_candidate")
                    continue
                if not example["state"].strip():
                    exclude("empty_input")
                    continue
                example["meta"] = {**example["meta"], "task": record.task_id, "family": family,
                                   "feedback": record.feedback_ref, "source": record.source,
                                   "input_schema": input_schema}
                rows[part].append(example)
                per_task[record.task_id] = per_task.get(record.task_id, 0) + 1
    fit_groups = sorted({e["meta"]["family"] for e in rows["fit"]})
    val_groups = sorted({e["meta"]["family"] for e in rows["validation"]})
    cal_groups = sorted({e["meta"]["family"] for e in rows["calibration"]})
    out.groups = {"fit": fit_groups, "validation": val_groups, "calibration": cal_groups}
    out.stats = {"rows": {k: len(v) for k, v in rows.items()}, "tasks": len(per_task),
                 "labels": _label_counts(rows), "truncated": sum(bool(e["meta"].get("truncated"))
                                                                 for e in rows["fit"]),
                 "max_per_task": max_per_task, "seed": seed}
    manifest = ct.DataManifest(
        source_split_ref=splits.hash() if splits is not None else "",
        family_map_ref=objects.put_json("change", {"families": {r.task_id: r.family_id for r in records}}),
        feedback_ref=feedback.builder_ref, input_schema=input_schema, transform_ref=transform_ref,
        fit_groups=tuple(fit_groups), validation_groups=tuple(val_groups), calibration_groups=tuple(cal_groups),
        row_refs=tuple(e["id"] for e in rows["fit"]), exclusions=dict(out.exclusions))
    out.manifest = manifest
    if len(fit_groups) < min_groups or len(val_groups) < 1:
        out.status = "insufficient_data"
        out.exclusions["insufficient_groups"] = {"fit": len(fit_groups), "validation": len(val_groups),
                                                 "needed": min_groups}
        return out
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for name, part in (("train", "fit"), ("val", "validation")):
        with open(Path(out_dir) / f"{name}.jsonl", "w", encoding="utf-8") as f:
            for example in rows[part]:
                f.write(json.dumps({**example, "split": "train" if part == "fit" else "val"},
                                   ensure_ascii=False) + "\n")
    if rows["calibration"]:          # kept apart: thresholds fitted on the rows a checkpoint was chosen on would
        with open(Path(out_dir) / "calibration.jsonl", "w", encoding="utf-8") as f:   # be optimistic
            for example in rows["calibration"]:
                f.write(json.dumps(example, ensure_ascii=False) + "\n")
    out.dataset_ref = objects.put(manifest)
    return out


def _label_counts(rows: dict) -> dict:
    out: dict = {}
    for part, examples in rows.items():
        for e in examples:
            for name, value in zip([c["name"] for c in e["candidates"]], e["target"]):
                if value > 0:
                    out.setdefault(part, {}).setdefault(name, 0)
                    out[part][name] += 1
    return out


def check_lineage(feedback, allowed: tuple[str, ...] = ("train",), *, objects=None) -> list[str]:
    """Which records may not be trained on because of where their evidence came from.

    The check follows the evidence, not the file it is filed in: a summary built from a final-split result carries
    that partition with it, so relabelling it as training data changes nothing (CT-04, DT-05).
    """
    bad = []
    for record in feedback.records:
        for ref in record.evidence_refs:
            try:
                evidence = ct.EvidenceRef.from_json(objects.get(ref)) if objects else None
            except (KeyError, ct.ContractError):
                bad.append(f"{record.decision_id}: evidence {ct.short(ref)} is unreadable")
                continue
            if evidence is not None and not ct.visible(evidence.partition, allowed):
                bad.append(f"{record.decision_id}: evidence is {evidence.partition} data, and this dataset may "
                           f"only read {list(allowed)}")
    return bad
