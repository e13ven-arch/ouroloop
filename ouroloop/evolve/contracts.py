"""M0: the types, identity and permissions every other module shares (docs/modules/00_CONTRACTS.md).

Pure types, validation and serialisation: nothing here calls a model, reads the champion pointer or decides
whether a candidate is any good. What it does decide is what two records mean when they claim to be the same
thing, and that is the whole point of the file:

    identity    an immutable object is named by `type:sha256:<64hex>` over its canonical JSON. Reordering the
                keys of a record is the same record; changing one word of a prompt is another one. A timestamp
                or a local path is not part of what an object is, so it never enters the hash.
    boundaries  every piece of evidence carries the partition it came from, and anything derived from it
                inherits the strictest partition of its inputs. A summary of a final-split result is still
                final-split evidence, whatever it is filed as (CT-04).
    authority   a release says what may run: a bootstrap release, a permit that only allows one isolated
                evaluation, or a release an evaluation receipt accepted. "Allowed to be evaluated" and
                "allowed to run" are different words here, because conflating them is how an unevaluated
                candidate goes live (CT-06).

Old records stay readable and keep their old hashes (CT-02): the v2 fields are additions, and a record without
them is a v1 record, not an incomplete v2 one.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator

from ..ledger import _append_jsonl, _read_jsonl, now, redact

SCHEMA_VERSION = 2

OWNERS = ("worker", "research")
BINDINGS = ("decision", "config", "research_step", "generation")
# The schema versions a reader accepts. v0.6 adds fields that default to None instead of a new version
# (docs/modules/00_CONTRACTS.md §1), so anything but these is refused rather than read with a guess.
KNOWN_SCHEMA_VERSIONS = (SCHEMA_VERSION,)
COLLECTION_PURPOSES = ("task_feedback", "decision_feedback", "actor_imitation")
MODEL_ROLES = ("actor", "controller")
# v0.5 (docs/design/V0_5_PLAN.md §1.3, §1.4): "fixed" — a candidate the framework's fixed code declares (an experience
# library snapshot, a refit method table), not R's and not a person's.
ORIGINS = ("manual", "scripted", "llm", "fixed")
# Every partition an evidence record can belong to, from the most freely readable to the one a result may only
# be read out of once (docs/DESIGN.md §3.1), in strictness order.
PARTITIONS = ("train", "dev", "research_dev", "final")
IMPLEMENTATIONS = ("prompt", "rule", "workflow", "model")
PROFILES = ("task", "research")
VERDICTS = ("accepted", "rejected", "inconclusive", "invalid")
FEEDBACK_STATUS = ("valid", "unknown", "conflicted", "invalid")
INSTALL_STATUS = ("installed", "stale_parent", "installation_failed")
AUTHORIZATIONS = ("bootstrap", "evaluation_permit", "accepted")

REF = re.compile(r"[a-z_]+:sha256:[0-9a-f]{64}")


class ContractError(ValueError):
    """A record that does not satisfy the contract. Raised before anything is written or run."""


class CandidateDefect(RuntimeError):
    """The candidate is broken: an illegal action, a schema mismatch, a deterministic misconfiguration.

    Kept apart from an infrastructure failure on purpose. An infrastructure failure may be refilled under a
    fixed rule; a defect may not, because retrying a candidate until the broken task happens to pass is how a
    defective candidate gets promoted (docs/modules/03_EVALUATION_RELEASE.md §3).
    """


# ---------------------------------------------------------------------------- identity
def canonical(obj: Any) -> bytes:
    """The bytes an object's identity is taken over: UTF-8, sorted keys, compact separators, lists in order.

    NaN and Infinity are refused rather than encoded: `Infinity` is not JSON, so a record holding one would
    hash here and fail to parse in anything else that reads it.
    """
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except ValueError as e:
        raise ContractError(f"not canonical JSON: {e}") from None


def digest(obj: Any) -> str:
    return sha256(canonical(obj)).hexdigest()


def ref_for(kind: str, payload: Any) -> str:
    if not re.fullmatch(r"[a-z_]+", kind):
        raise ContractError(f"object type {kind!r}: lowercase letters and underscores")
    return f"{kind}:sha256:{digest(payload)}"


def parse_ref(ref: str) -> tuple[str, str]:
    if not isinstance(ref, str) or not REF.fullmatch(ref):
        raise ContractError(f"{ref!r} is not a content reference (type:sha256:<64 hex>)")
    kind, _, rest = ref.partition(":")
    return kind, rest.partition(":")[2]


def short(ref: str) -> str:
    """For display only. Nothing resolves a short id: two objects may share one."""
    kind, d = parse_ref(ref)
    return f"{kind}:{d[:12]}"


def check_refs(refs, *, kind: str | None = None, what: str = "reference") -> tuple[str, ...]:
    out = []
    for r in refs or ():
        k, _ = parse_ref(r)
        if kind is not None and k != kind:
            raise ContractError(f"{what} {short(r)} is a {k}, not a {kind}")
        out.append(r)
    return tuple(out)


def visible(partition: str, allowed: tuple[str, ...]) -> bool:
    return partition in allowed


# ---------------------------------------------------------------------------- schema registry
# Input and output schemas a policy may name. A descriptor that names anything else is refused before it is
# built, so an unknown view or an invented action vocabulary never reaches a run (CT-03).
SCHEMAS: dict[str, dict] = {
    # views a decision policy may read
    # `required` is what a decision at this point cannot be judged without, declared by the core rather than
    # inferred: a field that is legitimately empty (nothing was truncated, a task needed no tool call) is not a
    # field that never reached the decision (docs/modules/01_POLICY_RUNTIME.md §2).
    "stop.file.v2": {"kind": "input", "fields": ["task", "moment", "message", "files", "turns", "actions"],
                     "required": ["task", "message"]},
    "stop.business.v1": {"kind": "input",
                         "fields": ["task_text", "phase", "step", "final_message", "tool_events",
                                    "budget_remaining", "truncation"],
                         "required": ["task_text", "tool_events"]},
    # action vocabularies
    "action.stop.v1": {"kind": "output", "actions": ["yes", "no", "abstain"]},
    # configuration payloads
    "prompt.v1": {"kind": "output", "fields": ["text"]},
    "finish_check.v1": {"kind": "output", "fields": ["instruction_ref"]},
    "research_retrieve.v1": {"kind": "output",
                             "fields": ["tags", "targets", "statuses", "max_items", "sort"]},
    "research_preflight.v1": {"kind": "output", "fields": ["checks"]},
    # research steps
    "research.context.v1": {"kind": "input", "fields": ["goal", "inspection", "results", "requirements",
                                                        "activity", "feedback", "experience", "budget"]},
    "requirement_list.v1": {"kind": "output", "fields": ["requirements"]},
    "proposal_list.v1": {"kind": "output", "fields": ["proposals", "next_action", "reason"]},
    # v0.6 (docs/modules/00_CONTRACTS.md §2): the actor's generation, the capability package, and the per-task
    # diagnosis of the first version's research step.
    "chat.messages.v1": {"kind": "input", "fields": ["system", "messages", "tools"], "required": ["messages"]},
    "chat.turn.v1": {"kind": "output", "fields": ["text", "tool_calls", "stop"]},
    "harness_package.v1": {"kind": "output", "fields": ["package_ref"]},
    # v0.3: the same config shape for a package with an agent component (docs/modules/00_CONTRACTS.md §2).
    "harness_package.v2": {"kind": "output", "fields": ["package_ref"]},
    "failure_summary.v1": {"kind": "input",
                           "fields": ["task_id", "family_id", "task_text", "termination", "partial_credit",
                                      "turns", "tool_errors", "events", "checks"],
                           "required": ["task_id", "task_text", "turns"]},
    "failure_note.v1": {"kind": "output",
                        "fields": ["task_id", "failure_class", "root_cause", "mechanism", "component",
                                   "generality"]},
    # v0.3 step 3 (docs/modules/04_RESEARCH_LOOP.md §1 R 作为有界编码会话): what R's coding session hands back
    # with `finish` — up to k candidate directories, each with its declaration, or a next_action.
    "candidate_declaration.v1": {"kind": "output", "fields": ["candidates", "next_action", "reason"]},
}


def known_schema(name: str, kind: str | None = None) -> dict:
    entry = SCHEMAS.get(name)
    if entry is None:
        raise ContractError(f"unknown schema {name!r}: the registry has {sorted(SCHEMAS)}")
    if kind is not None and entry["kind"] != kind:
        raise ContractError(f"schema {name!r} is an {entry['kind']} schema, not {kind}")
    return entry


def actions_of(name: str) -> tuple[str, ...]:
    return tuple(known_schema(name, "output").get("actions") or ())


# ---------------------------------------------------------------------------- records
# Fields that describe a record rather than identify it: two records that differ only in these are one object.
VOLATILE = ("created", "ts", "note", "notes", "display", "path_hint", "observed_at")


@dataclass
class Record:
    """A content-addressed object. `TYPE` is the prefix of its ref; `payload` is what the digest is taken over."""
    TYPE = "object"

    def payload(self) -> dict:
        out = {}
        for f in fields(self):
            if f.name in VOLATILE:
                continue
            value = getattr(self, f.name)
            if value is None:
                continue                        # absent and null are the same thing for identity
            out[f.name] = _plain(value)
        return out

    def ref(self) -> str:
        return ref_for(self.TYPE, self.payload())

    def to_json(self) -> dict:
        return {**{k: _plain(v) for k, v in asdict(self).items()}, "ref": self.ref()}

    @classmethod
    def from_json(cls, d: dict) -> "Record":
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(d) - known - {"ref"})
        if unknown:
            raise ContractError(f"{cls.__name__} does not take {unknown}")
        version = d.get("schema_version")
        if version is not None and version not in KNOWN_SCHEMA_VERSIONS:
            raise ContractError(f"{cls.__name__} schema_version {version!r}: this reader knows "
                                f"{list(KNOWN_SCHEMA_VERSIONS)}")
        return cls(**{k: v for k, v in d.items() if k in known})


def _plain(value: Any) -> Any:
    if isinstance(value, Record):
        return value.payload()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


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


@dataclass
class PolicyDescriptor(Record):
    """One replaceable decision, configuration or research step (docs/DESIGN.md §2.2)."""
    TYPE = "policy"
    owner: str = "worker"
    target: str = ""
    binding: str = "config"
    input_schema: str = ""
    output_schema: str = ""
    implementation: dict = field(default_factory=dict)
    applicability: dict = field(default_factory=dict)
    fallback: str = "default"      # what runs when this policy abstains, times out or is unavailable
    limits: dict = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.owner not in OWNERS:
            raise ContractError(f"owner {self.owner!r}: one of {list(OWNERS)}")
        if self.binding not in BINDINGS:
            raise ContractError(f"binding {self.binding!r}: one of {list(BINDINGS)}")
        known_schema(self.input_schema, "input")
        known_schema(self.output_schema)
        check_implementation(self.implementation, binding=self.binding)
        if self.binding != "decision" and actions_of(self.output_schema):
            raise ContractError(f"{self.target}: only a decision policy has an action vocabulary, and "
                                f"{self.output_schema!r} declares one")
        if self.binding == "decision" and not actions_of(self.output_schema):
            raise ContractError(f"{self.target}: a decision policy needs an output schema with actions, "
                                f"{self.output_schema!r} has none")

    @property
    def kind(self) -> str:
        return self.implementation.get("kind", "")


def check_implementation(impl: dict, binding: str | None = None) -> None:
    """The tagged union of what a policy can be. No `eval`, no import path, no arbitrary code.

    A model is dispatched by its binding (docs/modules/00_CONTRACTS.md §2): a decision model carries its spec and
    inference over a finite action set; a generation model (the actor) carries the identity of the provider and
    base model it runs, and a trained actor additionally the artifact that identity was trained into.
    """
    if not isinstance(impl, dict):
        raise ContractError("implementation must be an object")
    kind = impl.get("kind")
    if kind not in IMPLEMENTATIONS:
        raise ContractError(f"implementation kind {kind!r}: one of {list(IMPLEMENTATIONS)}")
    required = {"prompt": {"artifact_ref"}, "rule": {"builtin", "config_ref"},
                "workflow": {"builtin", "config_ref"}, "model": {"model_ref", "spec_ref", "inference_ref"}}[kind]
    optional: set = set()
    if kind == "model" and binding == "generation":
        required, optional = {"model_identity_ref"}, {"artifact_ref"}
    if kind != "model" and binding == "generation":
        raise ContractError("a generation binding takes a model implementation only")
    missing = sorted(required - set(impl))
    if missing:
        raise ContractError(f"a {kind} implementation needs {missing}")
    extra = sorted(set(impl) - required - optional - {"kind"})
    if extra:
        raise ContractError(f"a {kind} implementation does not take {extra}")
    for key in ("artifact_ref", "config_ref", "spec_ref", "inference_ref", "model_identity_ref"):
        if key in impl:
            parse_ref(impl[key])


@dataclass
class PolicySet(Record):
    """A complete W or R: every target's policy, not a diff against something else."""
    TYPE = "policyset"
    owner: str = "worker"
    policies: tuple[str, ...] = ()          # policy refs
    parent_ref: str | None = None
    provenance_ref: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.owner not in OWNERS:
            raise ContractError(f"owner {self.owner!r}: one of {list(OWNERS)}")
        self.policies = tuple(sorted(check_refs(self.policies, kind="policy", what="policy")))


@dataclass
class Authorization(Record):
    """Why a release may run at all. A permit is not an acceptance and never becomes one (CT-06)."""
    TYPE = "authorization"
    kind: str = "bootstrap"
    ref: str | None = None                  # the permit or the accepted receipt
    reason: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.kind not in AUTHORIZATIONS:
            raise ContractError(f"authorization {self.kind!r}: one of {list(AUTHORIZATIONS)}")
        if self.kind == "bootstrap" and self.ref is not None:
            raise ContractError("a bootstrap authorization refers to no receipt")
        if self.kind != "bootstrap" and not self.ref:
            raise ContractError(f"a {self.kind} authorization names the object that granted it")


@dataclass
class SystemRelease(Record):
    """A complete runnable version: one W, one R, the frozen core, and what authorises it.

    There is no experience pointer here: what a run could see is fixed by its manifest, so a release cannot
    quietly widen the evidence a later round reads.
    """
    TYPE = "release"
    worker_ref: str = ""
    research_ref: str = ""
    core_ref: str = ""
    authorization_ref: str = ""
    parent_ref: str | None = None
    provenance_ref: str | None = None
    schema_version: int = SCHEMA_VERSION
    # v0.6, None when unset so a v2 release keeps its ref: the host execution contract the release runs on, and
    # the runtime capabilities it needs (an old runtime refuses the unknown field instead of ignoring it).
    target_harness_ref: str | None = None
    required_capabilities: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.target_harness_ref is not None:
            check_refs([self.target_harness_ref], kind="harness", what="target harness")
        if self.required_capabilities is not None:
            self.required_capabilities = tuple(sorted(set(str(c) for c in self.required_capabilities)))
        check_refs([self.worker_ref], kind="policyset", what="worker policy set")
        check_refs([self.research_ref], kind="policyset", what="research policy set")
        check_refs([self.core_ref], kind="core", what="core manifest")
        check_refs([self.authorization_ref], kind="authorization", what="authorization")
        if self.parent_ref:
            check_refs([self.parent_ref], kind="release", what="parent release")


@dataclass
class EvaluationPermit(Record):
    """Permission to run one candidate, in one plan's scope, in isolation. Never permission to install."""
    TYPE = "permit"
    subject_ref: str = ""                   # the candidate policy set
    parent_release: str = ""
    plan_ref: str = ""
    scope_id: str = ""
    core_ref: str = ""
    expires_at: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        check_refs([self.subject_ref], kind="policyset", what="subject")
        check_refs([self.parent_release], kind="release", what="parent release")
        check_refs([self.core_ref], kind="core", what="core manifest")


@dataclass
class CoreManifest(Record):
    """K: what the experiment may not change. A change here is a new experiment identity, not a new version."""
    TYPE = "core"
    domain: str = ""
    adapter: str = ""
    scorer: str = ""
    toolset: str = ""
    split_ref: str = ""                     # SplitManifest.hash()
    schema_registry: tuple[str, ...] = tuple(sorted(SCHEMAS))
    targets: tuple[str, ...] = ()           # the registered targets a candidate may address
    capabilities: dict = field(default_factory=dict)
    access_rules: dict = field(default_factory=dict)
    gate_plan: dict = field(default_factory=dict)
    budget_policy: dict = field(default_factory=dict)
    retry_policy: dict = field(default_factory=dict)
    code_version: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass
class ExperimentProposal(Record):
    """What a researcher submits. Untrusted: it carries no thresholds, no verdict and no results."""
    TYPE = "proposal"
    owner: str = "worker"
    hypothesis: str = ""
    requirements: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    changes: tuple = ()                     # {target, kind, change_ref | change}
    training: tuple = ()                    # {point, feedback_ref, data_selection_ref, recipe_ref}
    expected: str = ""
    abandon_if: str | None = None
    notes: str = ""
    schema_version: int = SCHEMA_VERSION
    # v0.6: which entry point produced the proposal, written by that entry point and never by the model
    # (docs/modules/00_CONTRACTS.md §3). None on proposals made before it existed.
    origin: str | None = None
    # v0.3 step 3 (docs/modules/00_CONTRACTS.md §3): R's declaration of the candidate — {mechanism, site, carrier,
    # predicted_affected, trigger_condition, differs_from?} — stored by the round's entry point before admission;
    # the engine ledger reads it by this ref. None on every proposal made before it existed and on every proposal
    # of the summary-call path, so their refs do not move (CT7-07). Not part of the candidate's signature: the
    # same change under another declaration is the same candidate (CT7-08).
    declaration_ref: str | None = None

    def __post_init__(self) -> None:
        if self.owner not in OWNERS:
            raise ContractError(f"owner {self.owner!r}: one of {list(OWNERS)}")
        if self.origin is not None and self.origin not in ORIGINS:
            raise ContractError(f"origin {self.origin!r}: one of {list(ORIGINS)}")
        if self.declaration_ref is not None:
            check_refs([self.declaration_ref], kind="declaration", what="declaration")
        self.changes = tuple(_change(c) for c in self.changes)
        self.training = tuple(_training(t) for t in self.training)
        if not self.changes and not self.training:
            raise ContractError("a proposal changes at least one target or trains at least one point")


def _change(c: Any) -> dict:
    if not isinstance(c, dict) or "target" not in c or "kind" not in c:
        raise ContractError(f"a change is {{target, kind, change_ref|change}}, not {c!r}")
    if c["kind"] not in IMPLEMENTATIONS:
        raise ContractError(f"change kind {c['kind']!r}: one of {list(IMPLEMENTATIONS)}")
    if "change_ref" in c:
        parse_ref(c["change_ref"])
    elif "change" not in c:
        raise ContractError(f"change for {c['target']!r} carries neither change_ref nor an inline change")
    extra = sorted(set(c) - {"target", "kind", "change_ref", "change"})
    if extra:
        raise ContractError(f"a change does not take {extra}")
    return dict(c)


def _training(t: Any) -> dict:
    required = {"point", "feedback_ref", "data_selection_ref", "recipe_ref"}
    if not isinstance(t, dict) or set(t) != required:
        raise ContractError(f"a training entry is {sorted(required)}, not {t!r}")
    return dict(t)


@dataclass
class Experiment(Record):
    """The framework's frozen record of one attempt. The researcher fills the proposal; these fields are ours."""
    TYPE = "experiment"
    id: str = ""
    proposal_ref: str = ""
    parent_release: str = ""
    experience_ref: str = ""
    core_ref: str = ""
    eval_template_ref: str = ""
    budget_scope: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        check_refs([self.proposal_ref], kind="proposal", what="proposal")
        check_refs([self.parent_release], kind="release", what="parent release")
        check_refs([self.core_ref], kind="core", what="core manifest")


RECEIPT_KINDS = ("full", "confirmed")


@dataclass
class EvaluationReceipt(Record):
    """What a fixed evaluator concluded. Only this, from a completed evaluation, can authorise an install.

    `kind` (M3 §2.1(b), v0.3): "full" — one complete paired reading — or "confirmed" — a first reading that
    cleared the gate and was then read again, whether the confirmation then passed or not. None is how a receipt
    written before the field reads: "full", and its ref unchanged (`Record.payload` leaves None out). A plan with
    `confirmation_repeats` installs only a "confirmed" receipt (release.Releases.install)."""
    TYPE = "receipt"
    experiment_ref: str = ""
    profile: str = "task"
    subject_ref: str = ""
    control_ref: str = ""
    plan_ref: str = ""
    results_ref: str = ""
    verdict: str = "inconclusive"
    reasons: tuple[str, ...] = ()
    metrics: dict = field(default_factory=dict)
    uncertainty: dict = field(default_factory=dict)
    costs: dict = field(default_factory=dict)
    kind: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ContractError(f"profile {self.profile!r}: one of {list(PROFILES)}")
        if self.verdict not in VERDICTS:
            raise ContractError(f"verdict {self.verdict!r}: one of {list(VERDICTS)}")
        if self.kind is not None and self.kind not in RECEIPT_KINDS:
            raise ContractError(f"receipt kind {self.kind!r}: one of {list(RECEIPT_KINDS)}")

    @property
    def reading_kind(self) -> str:
        return self.kind or "full"


@dataclass
class InstallationReceipt(Record):
    TYPE = "installation"
    operation_id: str = ""
    expected_parent: str | None = None
    target_release: str = ""
    preflight_ref: str | None = None
    status: str = "installed"
    observed_pointer: str | None = None
    reason: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in INSTALL_STATUS:
            raise ContractError(f"installation status {self.status!r}: one of {list(INSTALL_STATUS)}")


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


# ---------------------------------------------------------------------------- results of the core interfaces
# ---------------------------------------------------------------------------- v0.6 records
# New record types join at the current SCHEMA_VERSION; they are not a second schema (docs/modules/00_CONTRACTS.md).
@dataclass
class ModelIdentity(Record):
    """Which generation or decision model a policy runs, and how far that identity can be pinned.

    A hosted alias is not frozen weights: `mutable=True` says so, with when it was observed, instead of inventing a
    revision. `inference_ref` holds the provider configuration a runtime rebuilds the client from — endpoint,
    model name, output limit and the name of the environment variable that holds the key, never the key.
    """
    TYPE = "model_identity"
    role: str = "actor"
    provider: str = ""
    base_model: str = ""
    inference_ref: str = ""
    revision: str | None = None
    adapter_ref: str | None = None
    tokenizer_ref: str | None = None
    renderer_ref: str | None = None
    artifact_digest: str | None = None
    mutable: bool = True
    observed_at: str | None = None          # volatile: when the alias was seen, not part of the identity
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.role not in MODEL_ROLES:
            raise ContractError(f"model role {self.role!r}: one of {list(MODEL_ROLES)}")
        if not self.provider or not self.base_model:
            raise ContractError("a model identity names its provider and base model")
        check_refs([self.inference_ref], kind="change", what="inference configuration")
        if self.adapter_ref is not None:
            parse_ref(self.adapter_ref)


@dataclass
class TargetHarness(Record):
    """The host execution contract a release runs on: runtime, tools, messages, context, completion and limits.

    It describes fixed semantics only. Everything a candidate may change lives in W (prompts, the capability
    package, the actor); changing anything here is a new contract and a new comparison identity.
    """
    TYPE = "harness"
    runtime_ref: str = ""
    tool_schema_ref: str = ""
    action_schema_ref: str = ""
    message_schema_ref: str = ""
    context_policy_ref: str = ""
    finish_protocol_ref: str = ""
    permission_ref: str = ""
    limits_ref: str = ""
    extension_api_ref: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("runtime_ref", "tool_schema_ref", "action_schema_ref", "message_schema_ref",
                     "context_policy_ref", "finish_protocol_ref", "permission_ref", "limits_ref"):
            check_refs([getattr(self, name)], kind="change", what=name)
        if self.extension_api_ref is not None:
            check_refs([self.extension_api_ref], kind="change", what="extension_api_ref")


@dataclass
class HarnessPackage(Record):
    """A versioned capability package: the run bundle, its manifest, the package API it targets, its fixed
    dependency set, and the test bundle kept apart so evaluation and deployment mount the same run bytes."""
    TYPE = "package"
    bundle_ref: str = ""
    manifest_ref: str = ""
    package_api_ref: str = ""
    dependency_set_ref: str = ""
    test_bundle_ref: str | None = None
    evidence_refs: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        check_refs([self.bundle_ref], kind="bundle", what="run bundle")
        check_refs([self.manifest_ref], kind="manifest", what="manifest")
        check_refs([self.package_api_ref], kind="change", what="package API")
        check_refs([self.dependency_set_ref], kind="change", what="dependency set")
        if self.test_bundle_ref is not None:
            check_refs([self.test_bundle_ref], kind="bundle", what="test bundle")


@dataclass
class CollectionRequest(Record):
    """A bounded request for more evidence, handed back to the same driver instead of ending the round.

    The first version collects `task_feedback`: plain train attempts whose task results re-enter the diagnosis.
    Reviewed collection belongs to the second version and needs the plan it pins to.
    """
    TYPE = "collection_request"
    purpose: str = "task_feedback"
    release_ref: str = ""
    task_selection_ref: str = ""
    limits_ref: str = ""
    mode: str = "plain"
    target_harness_ref: str | None = None
    plan_ref: str | None = None
    requirement_refs: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.purpose not in COLLECTION_PURPOSES:
            raise ContractError(f"collection purpose {self.purpose!r}: one of {list(COLLECTION_PURPOSES)}")
        if self.mode not in ("plain", "reviewed"):
            raise ContractError(f"collection mode {self.mode!r}: plain or reviewed")
        if self.mode == "reviewed" and not self.plan_ref:
            raise ContractError("a reviewed collection needs the distillation plan it pins to")
        if self.purpose == "task_feedback" and self.mode != "plain":
            raise ContractError("task_feedback runs plain train attempts only")
        check_refs([self.release_ref], kind="release", what="release")
        check_refs([self.task_selection_ref], kind="change", what="task selection")
        check_refs([self.limits_ref], kind="change", what="limits")


@dataclass
class DeploymentManifest(Record):
    """What an exported app is: the runnable subset of one installed release (docs/modules/03_EVALUATION_RELEASE.md
    §4). A whitelist, never a copy of the release's whole object closure: no R, no teacher material, no data, no
    tests, no oracle, no credentials, no local paths."""
    TYPE = "deployment"
    source_release_ref: str = ""
    worker_ref: str = ""
    required_core_ref: str = ""               # the runtime projection of K: targets, permissions, limits
    validation_ref: str = ""                  # what the release's acceptance says, as a summary
    target_harness_ref: str | None = None
    package_ref: str | None = None
    model_refs: tuple[str, ...] = ()
    external_dependencies: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        check_refs([self.source_release_ref], kind="release", what="source release")
        check_refs([self.worker_ref], kind="policyset", what="worker policy set")
        check_refs([self.required_core_ref], kind="change", what="runtime core projection")
        check_refs([self.validation_ref], kind="change", what="validation summary")


@dataclass
class BuildResult:
    status: str = "ready"                   # ready | invalid | infrastructure_failed
    candidate_ref: str | None = None
    artifacts: dict = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    costs: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in ("ready", "invalid", "infrastructure_failed"):
            raise ContractError(f"build status {self.status!r}")
        if self.status == "ready" and not self.candidate_ref:
            raise ContractError("a ready build names its candidate")
        if self.status != "ready" and not self.errors:
            raise ContractError("a build that is not ready says why; an empty candidate is not an explanation")


@dataclass
class RoundResult:
    outcome: str = "no_proposal"
    experiment_id: str | None = None
    release_before: str | None = None
    release_after: str | None = None
    evidence_refs: tuple[str, ...] = ()
    budget_receipt_ref: str | None = None
    reason: str = ""
    receipt_ref: str | None = None
    # v0.6: what the driver should do next, kept as a field instead of folded into `reason`. RoundResult is not a
    # Record and never enters the object store; what must survive a restart is the CollectionRequest and the
    # operation log that action_ref points to (docs/modules/00_CONTRACTS.md §1).
    next_action: str | None = None
    action_ref: str | None = None


TYPES = {c.TYPE: c for c in (EvidenceRef, PolicyDescriptor, PolicySet, Authorization, SystemRelease,
                             EvaluationPermit, CoreManifest, ExperimentProposal, Experiment,
                             EvaluationReceipt, InstallationReceipt, DataManifest, Observation, DecisionTrace,
                             FeedbackRecord, ModelIdentity, TargetHarness, HarnessPackage,
                             CollectionRequest, DeploymentManifest)}


# ---------------------------------------------------------------------------- storage
def durable_write(path: Path, data: bytes) -> None:
    """Write through a temporary file in the same directory, fsync it, rename, then fsync the directory: a
    reader sees the whole file or no file, and a crash after the rename cannot lose it."""
    import threading
    path.parent.mkdir(parents=True, exist_ok=True)
    # one temporary per writer: two threads storing the same object must not share a half-written file
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:                          # not every filesystem allows it; the rename is still atomic
        pass
    finally:
        os.close(fd)


class Objects:
    """The immutable object store: <workspace>/rsi/objects/<type>/<digest>.json.

    Writing the same object twice is a no-op, so a retry after a crash produces the same ref and no second copy.
    """

    def __init__(self, root: str | Path):
        self.dir = Path(root) / "rsi" / "objects"

    def path(self, ref: str) -> Path:
        kind, d = parse_ref(ref)
        return self.dir / kind / f"{d}.json"

    def put(self, record: Record) -> str:
        return self.put_json(record.TYPE, record.payload())

    def put_json(self, kind: str, payload: Any) -> str:
        ref = ref_for(kind, payload)
        path = self.path(ref)
        if not path.exists():
            durable_write(path, canonical(payload))
        return ref

    def has(self, ref: str) -> bool:
        return self.path(ref).exists()

    def get(self, ref: str) -> dict:
        path = self.path(ref)
        if not path.exists():
            raise KeyError(f"no object {short(ref)} in {self.dir}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if ref_for(parse_ref(ref)[0], payload) != ref:
            raise ContractError(f"{short(ref)} does not hash to its contents")
        return payload

    def load(self, ref: str, cls: type | None = None) -> Record:
        kind, _ = parse_ref(ref)
        cls = cls or TYPES.get(kind)
        if cls is None:
            raise ContractError(f"no record type for {kind!r}")
        return cls.from_json(self.get(ref))


class Events:
    """<workspace>/rsi/events.jsonl: what happened, in order, referring to objects rather than repeating them."""

    def __init__(self, root: str | Path):
        self.path = Path(root) / "rsi" / "events.jsonl"

    def append(self, kind: str, **fields) -> dict:
        event = redact({"ts": now(), "event": kind, **fields})
        _append_jsonl(self.path, event)
        return event

    def all(self) -> list[dict]:
        return list(_read_jsonl(self.path))

    def of(self, kind: str) -> Iterator[dict]:
        for e in self.all():
            if e.get("event") == kind:
                yield e


def refs_in(payload: Any) -> set[str]:
    """Every content reference inside a payload, at any depth.

    A record embedded whole by its to_json() (a TaskEvalPlan's gate_plan) carries its own identity under "ref".
    That names the embedded copy itself, not another object: it was never stored, and nothing needs it, because
    the content is right there. So it is not a reference (docs/modules/00_CONTRACTS.md §1).
    """
    out: set[str] = set()
    if isinstance(payload, str):
        if REF.fullmatch(payload):
            out.add(payload)
    elif isinstance(payload, dict):
        own = _own_ref(payload)
        for key, value in payload.items():
            if key != "ref" or own is None:
                out |= refs_in(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            out |= refs_in(value)
    return out


def _own_ref(d: dict) -> str | None:
    """The "ref" of a dict that is a record's to_json(), when it is that record's own identity: the digest of the
    rest of the dict taken as Record.payload() takes it (no "ref", no volatile field, no None). Recognition only;
    nothing is rewritten. A field named ref that points elsewhere (Authorization.ref) does not hash to the dict
    that holds it, and stays a reference."""
    ref = d.get("ref")
    if not isinstance(ref, str) or not REF.fullmatch(ref):
        return None
    body = {k: v for k, v in d.items() if k != "ref" and k not in VOLATILE and v is not None}
    try:
        return ref if ref_for(parse_ref(ref)[0], body) == ref else None
    except ContractError:
        return None


def copy_closure(source: Objects, target: Objects, refs) -> set[str]:
    """Copy these objects and everything they refer to into another store. -> the refs copied.

    Identity travels with the bytes: the same object has the same ref in both stores, which is what lets an
    isolated arm hand a version back to the evaluator without either side trusting the other's directory
    (docs/modules/06_RESEARCH_CAMPAIGN.md §3).
    """
    seen: set[str] = set()
    frontier = [r for r in refs if r]
    while frontier:
        ref = frontier.pop()
        if ref in seen:
            continue
        seen.add(ref)
        payload = source.get(ref)
        copied = target.put_json(parse_ref(ref)[0], payload)
        if copied != ref:
            raise ContractError(f"{short(ref)} changed identity when copied: {short(copied)}")
        frontier.extend(r for r in refs_in(payload) if r not in seen)
    return seen
