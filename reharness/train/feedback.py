"""M2: the registered feedback builders (docs/modules/02_EVIDENCE_TRAINING.md §2).

A builder turns observed facts into labels for one decision point, and nothing else may. What is refused here is
as much of the contract as what is allowed:

    the final task reward, expanded into a label for every step along the way
    a model's own opinion, a judge's verdict, a promotion's outcome
    a guess where the scorer could not run — that is `unknown`, never `no`

`completion_snapshot.v1` answers one question: was the task complete at this moment? It does not answer whether
continuing would have been worth it; that needs branch comparison, which is another builder with its own replay
equivalence to prove, and the loop closes without it.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import contracts
from . import records
from .records import FeedbackSet

# Labels a completion decision can carry, and the actions they correspond to.
COMPLETION_LABELS = ("yes", "no")
PROBE_NOTE = "completion probe"


@dataclass(frozen=True)
class FeedbackBuilder:
    """Trusted code. `eligible_partitions` is enforced by the caller before anything is built: a label drawn from
    a development or final task would be training on the data that is meant to judge the training."""
    id: str
    version: str
    eligible_partitions: tuple[str, ...]
    required_evidence: tuple[str, ...]
    label_schema: str
    point: str

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


COMPLETION_SNAPSHOT = FeedbackBuilder("completion_snapshot", "v1", ("train",),
                                      ("decision_context", "post_episode_score"), "action.stop.v1", "stop")
BUILDERS = {COMPLETION_SNAPSHOT.id + "." + COMPLETION_SNAPSHOT.version: COMPLETION_SNAPSHOT,
            "completion_snapshot.v1": COMPLETION_SNAPSHOT}


def resolve(name: str) -> FeedbackBuilder:
    builder = BUILDERS.get(name)
    if builder is None:
        raise contracts.ContractError(f"no feedback builder {name!r}: {sorted(set(BUILDERS))}")
    return builder


def _labels(row) -> tuple[dict, dict]:
    """(source -> label, source -> the outcome event) for the trainable sources of one decision."""
    labels, events = {}, {}
    for ev in row.outcomes:
        source, label = ev.get("source"), ev.get("label")
        if source not in ("env", "human"):
            continue                       # a judge's or a gate's verdict is evaluation-only (ledger.EVAL_ONLY)
        if label is None:
            labels.setdefault(source, None)
            events.setdefault(source, ev)
            continue
        if isinstance(label, list):
            label = label[0] if len(label) == 1 else None
        if label not in COMPLETION_LABELS:
            continue
        if source in labels and labels[source] != label and not ev.get("supersedes"):
            labels[source] = None          # one source contradicting itself is not a label
        else:
            labels[source] = label
        events[source] = ev
    return labels, events


def build_feedback(rt, *, builder: FeedbackBuilder | str = COMPLETION_SNAPSHOT, objects: contracts.Objects,
                   partition_of=None, policy_ref: str = "", input_schema: str = "stop.business.v1",
                   scope_id: str = "") -> FeedbackSet:
    """Every decision of the builder's point that carries an observed label, as FeedbackRecords.

    The id of a record is determined by the decision, the builder and the evidence, so a controller that labels
    the same episode again after a restart produces the same object rather than a second sample. A human
    correction that explicitly supersedes the earlier label wins; a disagreement nobody resolved is `conflicted`,
    which keeps both and trains on neither.
    """
    builder = resolve(builder) if isinstance(builder, str) else builder
    sessions, _ = rt.ledger.sessions()
    out = FeedbackSet(builder_ref=builder.ref)
    for row in rt.ledger.rows(builder.point):
        decision = row.decision
        info = sessions.get(decision.get("session")) or {}
        partition = (partition_of(decision) if partition_of else info.get("split")) or "unknown"
        if partition not in builder.eligible_partitions:
            out.exclude(f"partition:{partition}")
            continue
        if not decision.get("ctx"):
            out.exclude("no_stored_context")
            continue
        labels, events = _labels(row)
        if not labels:
            out.exclude("no_observed_label")
            continue
        human, env = labels.get("human"), labels.get("env")
        status, label, source = "valid", None, "env"
        if human is not None and env is not None and human != env:
            if events["human"].get("supersedes"):
                status, label, source = "valid", human, "human"
            else:
                status, label, source = "conflicted", None, "human"
        elif human is not None:
            status, label, source = "valid", human, "human"
        elif env is not None:
            status, label, source = "valid", env, "env"
        else:
            status, label = "unknown", None                 # the scorer could not read the state: not a "no"
        evidence = objects.put_json("change", {
            "decision": decision["id"], "point": builder.point, "phase": decision.get("phase"),
            "outcomes": [{k: v for k, v in ev.items() if k not in ("ts",)} for ev in row.outcomes],
            "builder": builder.ref})
        evidence_ref = objects.put(records.EvidenceRef(
            id=f"{decision['id']}:{builder.ref}", kind="completion_snapshot", scope_id=scope_id,
            partition=partition, producer_ref=builder.ref, content_ref=evidence,
            task_id=info.get("task"), family_id=info.get("family")))
        record = records.FeedbackRecord(
            decision_id=decision["id"], task_id=info.get("task") or "", family_id=info.get("family") or "",
            source=source, feedback_ref=builder.ref,
            target={label: 1.0} if label else {}, evidence_refs=(evidence_ref,),
            observed_policy_ref=decision.get("policy_ref") or policy_ref, input_schema=input_schema,
            status=status,
            supersedes=(events.get("human") or {}).get("supersedes") if status == "valid" and source == "human"
            and isinstance((events.get("human") or {}).get("supersedes"), str) else None)
        out.records.append(record)
        out.refs.append(objects.put(record))
        if status != "valid":
            out.exclude(f"status:{status}")
    return out
