"""The training stack's record types (docs/design/V2_TRAINING_PIPELINE.md), moved from evolve/contracts.py in stage 1:
the evidence reference with its partition, the data manifest of a training file, and the observation, decision trace
and feedback record of a decision point. Only the training stack reads or writes them (CONTRACTS §2)."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import ContractError, Record, register

SCHEMA_VERSION = 2
# Every partition an evidence record can belong to, from the most freely readable to the one a result may only be read
# out of once, in strictness order.
PARTITIONS = ("train", "dev", "research_dev", "final")
FEEDBACK_STATUS = ("valid", "unknown", "conflicted", "invalid")


def visible(partition: str, allowed: tuple[str, ...]) -> bool:
    return partition in allowed


@register
@dataclass
class EvidenceRef(Record):
    TYPE = "evidence"
    id: str = ""
    kind: str = ""                 # task_result, decision_trace, feedback, summary, receipt, ...
    scope_id: str = ""
    partition: str = "train"
    producer_ref: str = ""
    content_ref: str = ""
    task_id: str | None = None
    family_id: str | None = None
    parents: tuple[str, ...] = ()  # evidence ids this was derived from; the partition already combines theirs
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.partition not in PARTITIONS:
            raise ContractError(f"partition {self.partition!r}: one of {list(PARTITIONS)}")


@register
@dataclass
class DataManifest(Record):
    """Which rows a training file holds and where each came from (docs/modules/02_EVIDENCE_TRAINING.md §4)."""
    TYPE = "dataset"
    source_split_ref: str = ""
    family_map_ref: str = ""
    feedback_ref: str = ""
    input_schema: str = ""
    transform_ref: str | None = None
    fit_groups: tuple[str, ...] = ()
    validation_groups: tuple[str, ...] = ()
    calibration_groups: tuple[str, ...] = ()
    row_refs: tuple[str, ...] = ()
    exclusions: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        groups = [set(self.fit_groups), set(self.validation_groups), set(self.calibration_groups)]
        for a, b in ((0, 1), (0, 2), (1, 2)):
            overlap = sorted(groups[a] & groups[b])
            if overlap:
                raise ContractError(f"fit, validation and calibration groups must be disjoint; shared: "
                                    f"{overlap[:5]}")


@register
@dataclass
class Observation(Record):
    """What was visible at a decision. Nothing here may come from a scorer, a reference answer or the future."""
    TYPE = "observation"
    task_text: str = ""
    tool_events: tuple = ()
    phase: str = ""
    step: int = 0
    final_message: str = ""
    budget_remaining: dict = field(default_factory=dict)
    visible_refs: tuple[str, ...] = ()
    truncation: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION


@register
@dataclass
class DecisionTrace(Record):
    """Proposed and executed are two facts from two places, never one field written twice."""
    TYPE = "trace"
    decision_id: str = ""
    observation_ref: str = ""
    policy_ref: str = ""
    input_schema: str = ""
    rendered_input_ref: str = ""
    proposed_action: str | None = None
    executed_action: str | None = None
    executor: str = ""                      # "policy", "guard", "default", "rule"
    fallback_reason: str | None = None
    override_reason: str | None = None
    schema_version: int = SCHEMA_VERSION


@register
@dataclass
class FeedbackRecord(Record):
    TYPE = "feedback"
    decision_id: str = ""
    task_id: str = ""
    family_id: str = ""
    source: str = "env"                     # env or human; a model's own opinion is not feedback
    feedback_ref: str = ""                  # the builder that produced it
    target: dict = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    observed_policy_ref: str = ""
    input_schema: str = ""
    status: str = "valid"
    supersedes: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.source not in ("env", "human"):
            raise ContractError(f"feedback source {self.source!r}: env or human (a model's answer is not "
                                f"feedback, docs/DESIGN.md §4.2)")
        if self.status not in FEEDBACK_STATUS:
            raise ContractError(f"feedback status {self.status!r}: one of {list(FEEDBACK_STATUS)}")


@dataclass
class FeedbackSet:
    """One builder's FeedbackRecords over the ledger (train/feedback.py's build_feedback), and why decisions were left out.
    Here rather than with the builders: a measurement that builds no feedback still hands R the empty set of the same
    shape (bench.Improvement.feedback), without importing them."""
    records: list = field(default_factory=list)          # FeedbackRecord
    refs: list = field(default_factory=list)
    excluded: dict = field(default_factory=dict)         # reason -> count
    builder_ref: str = ""

    def valid(self) -> list:
        return [r for r in self.records if r.status == "valid"]

    def exclude(self, reason: str) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + 1

    def summary(self) -> dict:
        labels: dict = {}
        for r in self.valid():
            for label in r.target:
                labels[label] = labels.get(label, 0) + 1
        return {"records": len(self.records), "valid": len(self.valid()), "labels": labels,
                "excluded": dict(self.excluded), "builder": self.builder_ref,
                "conflicted": sum(r.status == "conflicted" for r in self.records),
                "unknown": sum(r.status == "unknown" for r in self.records)}
