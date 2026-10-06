"""M1: the registered targets, what may be bound to each, and the binding itself
(docs/modules/01_POLICY_RUNTIME.md).

A policy set is not a proposal that something changed; it is what actually runs. So this module answers, for
every enabled policy: which version was loaded, where it is called, what it saw, what it proposed, who executed
the action, and whether something overruled it. An artifact that exists is not a policy that ran.

Two rules shape the whole file:

    Only registered targets, only built-in carriers. A candidate cannot name a module path, an expression or a
    scoring function and have it executed (PR-07); the registry resolves a fixed set of names, and an unknown
    field in a config is an error rather than something ignored.
    A policy that cannot be loaded is an installation failure, never a silent fall back to the default. Falling
    back quietly would let a candidate that never ran be reported as one the gate passed.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .evolve import contracts as ct
from .evolve.contracts import ContractError, PolicyDescriptor, PolicySet, actions_of

# What a prompt may be: text, bounded by K. R cannot raise these; a candidate that wants more is refused.
MAX_PROMPT_CHARS = 20000
PREFLIGHT_CHECKS = ("input_coverage", "label_availability", "duplicate_experiment")
RETRIEVE_SORTS = ("recency", "recurrence")
BUILTINS = ("default.v1", "finish_check.v1", "research_retrieve.v1", "research_preflight.v1",
            "harness_package.v1", "harness_package.v2")
# worker.harness's built-in is the package API the pinned package's manifest targets (M0 §2, v0.3): v1 for a package
# the default agent runs, v2 for one with an agent of its own. Both take package_ref only.
PACKAGE_BUILTINS = ("harness_package.v1", "harness_package.v2")


@dataclass(frozen=True)
class TargetRegistration:
    """Trusted code: where a target is bound and what may be bound there."""
    target: str
    owner: str
    binding: str
    allowed_kinds: tuple[str, ...]
    input_schema: str
    output_schema: str
    site: str                                  # where in the host this policy takes effect, for Inspection
    fallback: str = "default"
    builtins: tuple[str, ...] = ()
    # An optional target is opened only by a core that lists it. The v0.6 targets are optional so a v2 core and
    # its complete policy sets stay complete without them (docs/modules/00_CONTRACTS.md §1).
    optional: bool = False
    # Other (input, output) schema pairs this target accepts. research.diagnose keeps its v2 requirement_list
    # form for old R sets and takes the v0.6 per-task note form; the bound descriptor's own schemas say which
    # path a round runs (docs/modules/04_RESEARCH_LOOP.md §1).
    alternatives: tuple[tuple[str, str], ...] = ()

    def accepts(self, input_schema: str, output_schema: str) -> bool:
        return (input_schema, output_schema) in ((self.input_schema, self.output_schema), *self.alternatives)


REGISTRY: dict[str, TargetRegistration] = {t.target: t for t in (
    TargetRegistration("worker.system_prompt", "worker", "config", ("prompt",), "research.context.v1", "prompt.v1",
                       "agent.system_prompt"),
    TargetRegistration("worker.finish", "worker", "config", ("prompt", "workflow"), "research.context.v1",
                       "finish_check.v1", "agent.system_prompt.finish_check", builtins=("finish_check.v1",)),
    TargetRegistration("worker.stop", "worker", "decision", ("rule", "model"), "stop.business.v1",
                       "action.stop.v1", "stop.on_finish", fallback="yes", builtins=("default.v1",)),
    TargetRegistration("research.diagnose", "research", "research_step", ("prompt",), "research.context.v1",
                       "requirement_list.v1", "research.round.diagnose",
                       alternatives=(("failure_summary.v1", "failure_note.v1"),)),
    TargetRegistration("research.retrieve", "research", "config", ("rule",), "research.context.v1",
                       "research_retrieve.v1", "research.round.retrieve", builtins=("research_retrieve.v1",)),
    TargetRegistration("research.propose", "research", "research_step", ("prompt",), "research.context.v1",
                       "proposal_list.v1", "research.round.propose"),
    TargetRegistration("research.preflight", "research", "config", ("workflow",), "research.context.v1",
                       "research_preflight.v1", "research.round.admission", builtins=("research_preflight.v1",)),
    # v0.6: the actor W pins, and the capability package the host mounts (docs/modules/01_POLICY_RUNTIME.md §1).
    TargetRegistration("worker.actor", "worker", "generation", ("model",), "chat.messages.v1", "chat.turn.v1",
                       "agent.provider", optional=True),
    TargetRegistration("worker.harness", "worker", "config", ("workflow",), "research.context.v1",
                       "harness_package.v1", "agent.harness_package", builtins=PACKAGE_BUILTINS,
                       optional=True),
)}


def resolve(target: str) -> TargetRegistration:
    reg = REGISTRY.get(target)
    if reg is None:
        raise ContractError(f"no registered target {target!r}: the registry has {sorted(REGISTRY)}")
    return reg


def targets_of(owner: str, *, include_optional: bool = False) -> tuple[str, ...]:
    """The targets of an owner. Optional targets only when asked: a core opens them by listing them."""
    return tuple(t for t, reg in REGISTRY.items() if reg.owner == owner and (include_optional or not reg.optional))


def core_targets(core: ct.CoreManifest, owner: str | None = None) -> tuple[str, ...]:
    """The targets this core opens, for one owner or both. A core with no list opens the default targets."""
    listed = tuple(core.targets or (*targets_of("worker"), *targets_of("research")))
    return tuple(t for t in listed if owner is None or (t in REGISTRY and REGISTRY[t].owner == owner))


# ---------------------------------------------------------------------------- configuration payloads
def parse_prompt(config: Any) -> dict:
    if isinstance(config, str):
        config = {"text": config}
    if not isinstance(config, dict) or set(config) != {"text"} or not isinstance(config["text"], str):
        raise ContractError("a prompt is {text: <string>}")
    if len(config["text"]) > MAX_PROMPT_CHARS:
        raise ContractError(f"prompt of {len(config['text'])} characters: the core allows {MAX_PROMPT_CHARS}")
    return {"text": config["text"]}


def parse_builtin(builtin: str, config: Any) -> dict:
    """A built-in's configuration, with unknown fields refused. Bounds come from K: R may narrow, never widen."""
    if not isinstance(config, dict):
        raise ContractError(f"{builtin} takes an object, not {type(config).__name__}")
    if builtin == "default.v1":
        allowed = {"action"}
        unknown = sorted(set(config) - allowed)
        if unknown:
            raise ContractError(f"default.v1 does not take {unknown}")
        return {"action": config.get("action")} if "action" in config else {}
    if builtin == "finish_check.v1":
        # Either the stored instruction or its text. A proposal has no store, so it writes the text and the build
        # step stores it (normalize_builtin); what is installed always holds the ref.
        if set(config) == {"instruction_ref"}:
            ct.parse_ref(config["instruction_ref"])
            return dict(config)
        if set(config) == {"instruction"}:
            return {"instruction": parse_prompt({"text": config["instruction"]})["text"]}
        raise ContractError("finish_check.v1 takes instruction_ref, or instruction as text")
    if builtin == "research_retrieve.v1":
        allowed = {"tags", "targets", "statuses", "max_items", "sort"}
        unknown = sorted(set(config) - allowed)
        if unknown:
            raise ContractError(f"research_retrieve.v1 does not take {unknown}; it takes {sorted(allowed)}")
        out = {"tags": tuple(config.get("tags") or ()), "targets": tuple(config.get("targets") or ()),
               "statuses": tuple(config.get("statuses") or ()),
               "max_items": min(int(config.get("max_items", 20)), 100),
               "sort": config.get("sort", "recency")}
        if out["sort"] not in RETRIEVE_SORTS:
            raise ContractError(f"research_retrieve.v1 sort {out['sort']!r}: one of {list(RETRIEVE_SORTS)}")
        for t in out["targets"]:
            resolve(t)
        return out
    if builtin == "research_preflight.v1":
        if set(config) != {"checks"}:
            raise ContractError("research_preflight.v1 takes checks only")
        checks = list(config["checks"])
        unknown = sorted(set(checks) - set(PREFLIGHT_CHECKS))
        if unknown:
            raise ContractError(f"unknown preflight checks {unknown}: {list(PREFLIGHT_CHECKS)}")
        if len(set(checks)) != len(checks):
            raise ContractError("preflight checks are an ordered list without repeats")
        return {"checks": tuple(checks)}
    if builtin in PACKAGE_BUILTINS:
        if set(config) != {"package_ref"}:
            raise ContractError(f"{builtin} takes package_ref only")
        ct.check_refs([config["package_ref"]], kind="package", what="capability package")
        return {"package_ref": config["package_ref"]}
    raise ContractError(f"unknown built-in {builtin!r}: {list(BUILTINS)}")


def normalize_builtin(objects: ct.Objects, builtin: str, config: dict) -> dict:
    """A parsed configuration with its inline text stored, so the installed config is all refs."""
    if builtin == "finish_check.v1" and "instruction" in config:
        return {"instruction_ref": objects.put_json("change", {"text": config["instruction"]})}
    return config


def parse_inference(config: Any, actions: tuple[str, ...]) -> dict:
    """How a model's probabilities become an action: the action map, the normalisation, the threshold and where
    the threshold came from. An unknown class or a non-finite probability abstains, and so does anything below
    the threshold (docs/modules/01_POLICY_RUNTIME.md §2)."""
    allowed = {"model_id", "actions", "normalization", "threshold", "threshold_source"}
    if not isinstance(config, dict) or sorted(set(config) - allowed):
        raise ContractError(f"an inference config takes {sorted(allowed)}, not {config!r}")
    mapped = dict(config.get("actions") or {})
    unknown = sorted(set(mapped.values()) - set(actions))
    if unknown:
        raise ContractError(f"inference maps to unknown actions {unknown}: {list(actions)}")
    threshold = config.get("threshold")
    source = config.get("threshold_source", "argmax")
    if threshold is not None and not 0.0 < float(threshold) <= 1.0:
        raise ContractError(f"threshold {threshold!r}: a probability above zero")
    if threshold is not None and source == "argmax":
        raise ContractError("a threshold needs a source: the calibration that chose it, not argmax")
    if config.get("normalization", "none") not in ("none", "temperature"):
        raise ContractError("normalization: none or temperature")
    return {"model_id": config.get("model_id", ""), "actions": mapped,
            "normalization": config.get("normalization", "none"),
            "threshold": None if threshold is None else float(threshold), "threshold_source": source}


# ---------------------------------------------------------------------------- validation
@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    policies: dict = field(default_factory=dict)      # target -> PolicyDescriptor

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_policy_set(policy_set: PolicySet, objects: ct.Objects, capabilities: dict | None = None) -> Validation:
    """Everything that can be checked without calling a model: registered targets, owner, binding, carrier,
    schemas, configuration, and that the set is complete for its owner.

    Completeness matters because a set is installed as a whole: a "diff" release would leave the other targets
    to whatever was there before, and no one could say afterwards what ran.
    """
    v = Validation()
    caps = capabilities or {}
    allowed_targets = set(caps.get("targets") or targets_of(policy_set.owner))
    owned = set(targets_of(policy_set.owner, include_optional=True))
    seen: dict[str, PolicyDescriptor] = {}
    for ref in policy_set.policies:
        try:
            p = objects.load(ref, PolicyDescriptor)
        except (KeyError, ContractError) as e:
            v.errors.append(f"{ct.short(ref)}: {e}")
            continue
        try:
            reg = resolve(p.target)
        except ContractError as e:
            v.errors.append(str(e))
            continue
        if p.target in seen:
            v.errors.append(f"two policies for {p.target}: a set holds one per target")
            continue
        seen[p.target] = p
        if p.owner != reg.owner or p.owner != policy_set.owner:
            v.errors.append(f"{p.target} is a {reg.owner} target; the policy says {p.owner} and the set says "
                            f"{policy_set.owner}")
        if p.binding != reg.binding:
            v.errors.append(f"{p.target} binds as {reg.binding}, not {p.binding}")
        if p.kind not in reg.allowed_kinds:
            v.errors.append(f"{p.target} does not take a {p.kind} policy: {list(reg.allowed_kinds)}")
        if not reg.accepts(p.input_schema, p.output_schema):
            v.errors.append(f"{p.target} reads {reg.input_schema} and writes {reg.output_schema}; the policy "
                            f"says {p.input_schema} / {p.output_schema}")
        if p.target not in allowed_targets:
            v.errors.append(f"{p.target} is not a target this core opens: {sorted(allowed_targets)}")
        v.errors.extend(_check_carrier(p, objects, reg))
    missing = sorted(owned & allowed_targets - set(seen))
    if missing:
        v.errors.append(f"the {policy_set.owner} set has no policy for {missing}: a release installs a complete "
                        f"set, not a diff")
    v.policies = seen
    return v


def _check_carrier(p: PolicyDescriptor, objects: ct.Objects, reg: TargetRegistration) -> list[str]:
    impl = p.implementation
    try:
        if p.kind == "prompt":
            parse_prompt(objects.get(impl["artifact_ref"]))
        elif p.kind in ("rule", "workflow"):
            if impl["builtin"] not in reg.builtins:
                return [f"{p.target} does not take the built-in {impl['builtin']!r}: {list(reg.builtins)}"]
            parse_builtin(impl["builtin"], objects.get(impl["config_ref"]))
        elif p.kind == "model" and p.binding == "generation":
            identity = objects.load(impl["model_identity_ref"], ct.ModelIdentity)
            if target_role(p.target) and identity.role != target_role(p.target):
                return [f"{p.target} runs an {target_role(p.target)}, and the identity is a {identity.role}"]
            if "artifact_ref" in impl and not objects.has(impl["artifact_ref"]):
                return [f"{p.target}: the trained artifact {ct.short(impl['artifact_ref'])} is not readable"]
        elif p.kind == "model":
            spec = objects.get(impl["spec_ref"])
            unknown = sorted(set(spec) - {"point", "instructions", "criteria", "view_params", "input_schema",
                                          "backend"})
            if unknown:
                return [f"{p.target} model spec does not take {unknown}"]
            if spec.get("input_schema") != reg.input_schema:
                return [f"{p.target} model spec reads {spec.get('input_schema')!r}, not {reg.input_schema!r}"]
            parse_inference(objects.get(impl["inference_ref"]), actions_of(p.output_schema))
    except KeyError as e:
        return [f"{p.target}: no artifact {e}"]
    except (ContractError, ValueError) as e:
        return [f"{p.target}: {e}"]
    return []


def target_role(target: str) -> str:
    """The model role a target runs: the actor generates, the controller decides. Empty for other targets."""
    return {"worker.actor": "actor", "worker.stop": "controller"}.get(target, "")


# ---------------------------------------------------------------------------- building policy sets
def descriptor(objects: ct.Objects, target: str, kind: str, payload: Any, *, owner: str | None = None,
               evidence_refs: tuple = (), limits: dict | None = None, model_ref: str | None = None,
               inference: dict | None = None, schemas: tuple[str, str] | None = None) -> PolicyDescriptor:
    """A descriptor with its configuration stored. `payload` is the prompt text, the built-in's config, the
    model's decision spec, or — for a generation target — {"model_identity_ref"[, "artifact_ref"]};
    `model_ref`/`inference` complete a decision model policy."""
    reg = resolve(target)
    input_schema, output_schema = schemas or (reg.input_schema, reg.output_schema)
    if not reg.accepts(input_schema, output_schema):
        raise ContractError(f"{target} does not take the schemas {input_schema} / {output_schema}")
    if kind == "model" and reg.binding == "generation":
        payload = dict(payload or {})
        unknown = sorted(set(payload) - {"model_identity_ref", "artifact_ref"})
        if unknown or "model_identity_ref" not in payload:
            raise ContractError(f"{target} takes model_identity_ref (and artifact_ref when trained), not {unknown}")
        impl = {"kind": "model", **payload}
        return PolicyDescriptor(owner=owner or reg.owner, target=target, binding=reg.binding,
                                input_schema=reg.input_schema, output_schema=reg.output_schema,
                                implementation=impl, fallback=reg.fallback, limits=dict(limits or {}),
                                evidence_refs=tuple(evidence_refs))
    if kind == "prompt":
        impl = {"kind": "prompt", "artifact_ref": objects.put_json("change", parse_prompt(payload))}
    elif kind in ("rule", "workflow"):
        builtin = payload.pop("builtin") if isinstance(payload, dict) and "builtin" in payload else reg.builtins[0]
        impl = {"kind": kind, "builtin": builtin,
                "config_ref": objects.put_json("change",
                                               normalize_builtin(objects, builtin,
                                                                 parse_builtin(builtin, payload)))}
    elif kind == "model":
        spec = {"input_schema": reg.input_schema, **dict(payload or {})}
        impl = {"kind": "model", "model_ref": model_ref or "",
                "spec_ref": objects.put_json("change", spec),
                "inference_ref": objects.put_json("change",
                                                  parse_inference(inference or {}, actions_of(reg.output_schema)))}
    else:
        raise ContractError(f"unknown carrier {kind!r}")
    return PolicyDescriptor(owner=owner or reg.owner, target=target, binding=reg.binding,
                            input_schema=input_schema, output_schema=output_schema, implementation=impl,
                            fallback=reg.fallback, limits=dict(limits or {}),
                            evidence_refs=tuple(evidence_refs))


def policy_set(objects: ct.Objects, owner: str, descriptors: list[PolicyDescriptor], *,
               parent_ref: str | None = None, provenance_ref: str | None = None) -> str:
    refs = [objects.put(d) for d in descriptors]
    return objects.put(PolicySet(owner=owner, policies=tuple(refs), parent_ref=parent_ref,
                                 provenance_ref=provenance_ref))


def replace_policies(objects: ct.Objects, parent_set_ref: str, changed: list[PolicyDescriptor], *,
                     provenance_ref: str | None = None) -> str:
    """The parent's complete set with these targets replaced: a candidate W or R (docs/DESIGN.md §5.1)."""
    parent = objects.load(parent_set_ref, PolicySet)
    kept = [objects.load(r, PolicyDescriptor) for r in parent.policies]
    by_target = {p.target: p for p in kept}
    for p in changed:
        if p.owner != parent.owner:
            raise ContractError(f"a {parent.owner} set cannot take a {p.owner} policy for {p.target}")
        by_target[p.target] = p
    return policy_set(objects, parent.owner, [by_target[t] for t in sorted(by_target)],
                      parent_ref=parent_set_ref, provenance_ref=provenance_ref)


# ---------------------------------------------------------------------------- binding
@dataclass
class Activity:
    """What a bound policy actually did. Nothing is filled in advance: a target with no event is `observed=False`,
    which is not the same as a target that was asked and stayed silent."""
    calls: dict = field(default_factory=dict)
    proposed: dict = field(default_factory=dict)
    executed: dict = field(default_factory=dict)
    fallbacks: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)
    # one bound release serves every concurrent attempt of an arm; its counts are kept whole
    _lock: object = field(default_factory=threading.Lock, repr=False, compare=False)

    def call(self, target: str) -> None:
        with self._lock:
            self.calls[target] = self.calls.get(target, 0) + 1

    def _count(self, table: dict, target: str, key: str) -> None:
        with self._lock:
            table.setdefault(target, {})
            table[target][key] = table[target].get(key, 0) + 1

    def propose(self, target: str, action: str | None) -> None:
        self._count(self.proposed, target, str(action))

    def execute(self, target: str, action: str | None) -> None:
        self._count(self.executed, target, str(action))

    def fallback(self, target: str, reason: str) -> None:
        self._count(self.fallbacks, target, reason)

    def override(self, target: str, reason: str) -> None:
        self._count(self.overrides, target, reason)

    def observed(self, target: str) -> bool:
        return target in self.calls

    def to_json(self) -> dict:
        return {"calls": dict(self.calls), "proposed": dict(self.proposed), "executed": dict(self.executed),
                "fallbacks": dict(self.fallbacks), "overrides": dict(self.overrides)}


class StopDecider:
    """The bound worker.stop policy: from an observation to a proposed action.

    It proposes; it does not act. Whether the action is taken is StopGuard's business, and the difference is
    recorded (PR-02): a model that says "no" after the last pushback is overruled, and the trace says so instead
    of claiming the model agreed.
    """

    def __init__(self, descriptor: PolicyDescriptor, *, point=None, backend=None, inference: dict | None = None,
                 default: str = "yes"):
        self.descriptor = descriptor
        self.ref = descriptor.ref()
        self.point = point
        self.backend = backend
        self.inference = inference or {}
        self.default = default
        self.actions = actions_of(descriptor.output_schema)

    @property
    def kind(self) -> str:
        return self.descriptor.kind

    def enabled(self, phase: str | None) -> bool:
        """worker.stop is bound at stop/on_finish only. per_turn keeps its fixed default: a learned judgement
        there is a separate target with its own evidence, not a free extension of this one
        (docs/modules/01_POLICY_RUNTIME.md §3)."""
        return phase in (None, "on_finish")

    def propose(self, observation: dict) -> tuple[str, str]:
        """-> (action, why). A rule policy proposes the registered default; a model policy proposes its own
        answer, and abstains on a tie, an unknown class or a confidence below its threshold."""
        if self.kind != "model" or self.backend is None:
            return self.default, "default"
        from .types import Request
        view, truncated = self.point.render(observation)
        q = self.point.question(observation)
        try:
            ans = self.backend.decide([Request(view, q)])[0]
        except Exception as e:  # noqa: BLE001 - a serving failure is a declared fallback, not a verdict
            return "abstain", f"unavailable: {type(e).__name__}"
        if not ans.over(q.labels()):
            raise ct.CandidateDefect(f"{self.descriptor.target}: the model answered {sorted(ans.probs)}, not a "
                                     f"distribution over {q.labels()}")
        if not ans.decisive:
            return "abstain", "no single most likely class"
        label = self.inference.get("actions", {}).get(ans.label, ans.label)
        if label not in self.actions:
            raise ct.CandidateDefect(f"{self.descriptor.target}: {label!r} is not one of {list(self.actions)}")
        threshold = self.inference.get("threshold")
        if threshold is not None and ans.confidence < threshold:
            return "abstain", f"confidence {ans.confidence:.3f} below {threshold}"
        return label, f"model {ans.model or '?'} at {ans.confidence:.3f}" + (" (view truncated)" if truncated else "")


@dataclass
class BoundRuntime:
    """A release as it actually runs. Not a second agent loop: the host's runtime and agent factory stay."""
    release_ref: str
    profile: str
    authorization: str
    worker: dict = field(default_factory=dict)        # target -> PolicyDescriptor
    research: dict = field(default_factory=dict)
    prompts: dict = field(default_factory=dict)       # target -> text
    stop: StopDecider | None = None
    retrieval: dict = field(default_factory=dict)
    preflight: tuple = ()
    activity: Activity = field(default_factory=Activity)
    services: Any = None
    # v0.6: the actor the release pins and the capability package it mounts. `actor` is the provider built from
    # `actor_identity`; it stays None only where nothing runs the actor (a preflight build), and a caller that
    # needs it refuses rather than using a provider of its own (docs/modules/01_POLICY_RUNTIME.md §1).
    actor_identity: Any = None
    actor: Any = None
    package: Any = None

    def policies(self) -> dict:
        return {**self.worker, **self.research}

    def system_prompt(self, base: str) -> tuple[str, dict]:
        """The final system prompt and what it was made of: the host's base template, then
        worker.system_prompt, then worker.finish, each appended once in that fixed order.

        Composition is by appending, never by searching and replacing inside the host's own text: a candidate
        that could rewrite the base prompt by string surgery would be editing the harness, not its policy.
        """
        parts = [base]
        for target in ("worker.system_prompt", "worker.finish"):
            text = self.prompts.get(target)
            if text:
                parts.append(text)
        block = self.package.prompt_block() if self.package is not None else ""
        if block:
            parts.append(block)
        final = "\n\n".join(p for p in parts if p)
        used = [t for t in ("worker.system_prompt", "worker.finish") if self.prompts.get(t)]
        out = {"base_chars": len(base), "parts": used + (["worker.harness"] if block else []),
               "hash": ct.digest({"prompt": final})[:16]}
        if self.package is not None:          # only when mounted: a v2 release keeps its prompt_hash
            out["package_ref"] = self.package.ref
        return final, out


@dataclass
class Services:
    """What binding needs from the host. Nothing here is reachable from a policy's configuration."""
    objects: ct.Objects
    runtime: Any = None
    serving: Any = None
    capabilities: dict = field(default_factory=dict)
    probe_models: bool = True                 # ask a loaded model which model it is (PR-04)
    # (ModelIdentity, provider config dict) -> ChatProvider. The host owns provider construction; binding only
    # decides which identity runs (docs/modules/01_POLICY_RUNTIME.md §1).
    provider_factory: Callable[[Any, dict], Any] | None = None


class BindError(RuntimeError):
    """A release that cannot be assembled. The caller reports installation_failed; it does not run the default
    and call the candidate evaluated."""


def bind_release(release_ref: str, *, profile: str, services: Services, authorization: str = "") -> BoundRuntime:
    """Assemble one release for one session or one evaluation arm.

    The caller has already decided that this release may run (release.authorize): a permit allows an isolated
    evaluation, a bootstrap or accepted authorization allows a normal session. Binding refuses anything it
    cannot load or verify.
    """
    objects = services.objects
    rel = objects.load(release_ref, ct.SystemRelease)
    core = objects.load(rel.core_ref, ct.CoreManifest)
    caps = {**(core.capabilities or {}), **(services.capabilities or {})}
    bound = BoundRuntime(release_ref=release_ref, profile=profile, authorization=authorization, services=services)
    for owner, set_ref in (("worker", rel.worker_ref), ("research", rel.research_ref)):
        ps = objects.load(set_ref, PolicySet)
        v = validate_policy_set(ps, objects, {**caps, "targets": caps.get("targets") or core_targets(core)})
        if not v.ok:
            raise BindError(f"{owner} policy set {ct.short(set_ref)}: {'; '.join(v.errors)}")
        (bound.worker if owner == "worker" else bound.research).update(v.policies)
        for target, p in v.policies.items():
            _bind_one(bound, target, p, services)
    return bound


def _bind_one(bound: BoundRuntime, target: str, p: PolicyDescriptor, services: Services) -> None:
    objects, impl = services.objects, p.implementation
    if p.kind == "prompt":
        bound.prompts[target] = parse_prompt(objects.get(impl["artifact_ref"]))["text"]
        return
    if target == "worker.finish":
        config = parse_builtin(impl["builtin"], objects.get(impl["config_ref"]))
        bound.prompts[target] = parse_prompt(objects.get(config["instruction_ref"]))["text"]
        return
    if target == "research.retrieve":
        bound.retrieval = parse_builtin(impl["builtin"], objects.get(impl["config_ref"]))
        return
    if target == "research.preflight":
        bound.preflight = parse_builtin(impl["builtin"], objects.get(impl["config_ref"]))["checks"]
        return
    if target == "worker.stop":
        bound.stop = _bind_stop(p, services)
        return
    if target == "worker.actor":
        identity = objects.load(impl["model_identity_ref"], ct.ModelIdentity)
        bound.actor_identity = identity
        if services.provider_factory is not None:
            config = dict(objects.get(identity.inference_ref))
            try:
                bound.actor = services.provider_factory(identity, config)
            except Exception as e:  # noqa: BLE001 - a provider that cannot be built is a failed assembly
                raise BindError(f"worker.actor: the provider could not be built: {type(e).__name__}: {e}") from None
        return
    if target == "worker.harness":
        from .agent.harness_package import load_package
        config = parse_builtin(impl["builtin"], objects.get(impl["config_ref"]))
        try:
            bound.package = load_package(objects, config["package_ref"])
        except (KeyError, ContractError) as e:
            raise BindError(f"worker.harness: {e}") from None
        if impl["builtin"] != bound.package.api_version:
            # The built-in is the package API the manifest targets (M0 §2, v0.3): a v2 package declared as v1 would
            # let a release hide the agent capability it needs, so the two are checked against each other here.
            raise BindError(f"worker.harness: the policy binds {impl['builtin']} and the package's manifest targets "
                            f"{bound.package.api_version}: the built-in is the package's own API")
        return
    raise BindError(f"{target}: nothing binds a {p.kind} policy here")


def _bind_stop(p: PolicyDescriptor, services: Services) -> StopDecider:
    """The bound completion policy, always over the view its descriptor declares.

    A rule policy gets the same point as a model policy: what is recorded at the decision must be the input the
    registration names, or a later comparison between a default arm and a model arm would be comparing two
    different records of the same moment.
    """
    from .decision import Spec
    from .points import business_stop_point
    reg = resolve(p.target)
    point = business_stop_point()
    if p.kind == "rule":
        return StopDecider(p, point=point, default=reg.fallback)
    spec = services.objects.get(p.implementation["spec_ref"])
    inference = parse_inference(services.objects.get(p.implementation["inference_ref"]),
                               actions_of(p.output_schema))
    point.spec = Spec(spec.get("instructions") or point.spec.instructions,
                      spec.get("criteria") or point.spec.criteria, dict(spec.get("view_params") or {}))
    backend_spec = spec.get("backend") or {"type": "model", "model": p.implementation["model_ref"]}
    if services.serving is None:
        raise BindError(f"{p.target} is a model policy and there is no serving to load it with")
    try:
        backend = services.serving.backend(backend_spec)
    except Exception as e:  # noqa: BLE001 - reported as a failed assembly, never as a fallback to the default
        raise BindError(f"{p.target}: the model could not be loaded: {type(e).__name__}: {e}") from None
    if services.probe_models and inference.get("model_id"):
        reported = _identity(backend)
        if reported and reported != inference["model_id"]:
            raise BindError(f"{p.target}: the configuration says model {inference['model_id']!r} and serving "
                            f"answers as {reported!r}")
    return StopDecider(p, point=point, backend=backend, inference=inference, default=reg.fallback)


def _identity(backend) -> str:
    from .serving import PROBE
    try:
        return backend.decide([PROBE])[0].model
    except Exception:  # noqa: BLE001 - an identity that cannot be read is reported as unknown, not as a match
        return ""


def describe(bound: BoundRuntime) -> dict:
    """The bound policies, for a manifest or a report: target -> version, carrier and where it is bound."""
    out = {}
    for target, p in bound.policies().items():
        reg = resolve(target)
        out[target] = {"policy_ref": p.ref(), "owner": p.owner, "kind": p.kind, "site": reg.site,
                       "input_schema": p.input_schema, "output_schema": p.output_schema}
    return out


# ---------------------------------------------------------------------------- the initial versions
# W0 and R0: declarative, no training, no model. A cold start may tailor these from the task description and
# whatever material is allowed, but before the first run there is no execution feedback, so nothing here is a
# verified improvement (docs/DESIGN.md §2.2).
FINISH_CHECK = ("Before you report the task as finished, re-read the request and check every requirement it "
                "states, including any at the end. For each one, name the tool result that shows it is done. If "
                "one is missing, do it now instead of finishing.")
DIAGNOSE = ("You read the results of a fixed agent version and name behaviour gaps. For each gap, state the "
            "context, the behaviour the task required, the behaviour that was observed, and the evidence ids it "
            "comes from. Do not propose implementations here, and do not widen the task beyond what it asks.")
PROPOSE = ("You propose one experiment that would close a named behaviour gap. Choose the simplest carrier that "
           "can work: a changed input first, a prompt or workflow for a missed step, a rule for a definite "
           "condition, and training only where the judgement depends on context and a reliable label exists. "
           "State the hypothesis, the expected effect on full-task results, and what would make you abandon it.")


def package_builtin(objects: ct.Objects, package_ref: str) -> str:
    """The worker.harness built-in for a stored package: the package API its manifest targets, `harness_package.v1`
    or `harness_package.v2` (M0 §2, v0.3). Read from the package, never assumed: a v2 package bound as v1 would hide
    the capability its release needs."""
    from .agent.harness_package import load_package
    return load_package(objects, package_ref).api_version


def harness_descriptor(objects: ct.Objects, package_ref: str) -> PolicyDescriptor:
    """The worker.harness policy pinning `package_ref`, under the built-in of the package's own API."""
    return descriptor(objects, "worker.harness", "workflow", {"builtin": package_builtin(objects, package_ref),
                                                              "package_ref": package_ref})


def default_worker_set(objects: ct.Objects, *, system_prompt: str = "", finish_check: str = FINISH_CHECK,
                       actor_identity_ref: str | None = None, package_ref: str | None = None) -> str:
    """W0. The v0.6 targets are included when their content is given: the actor's identity (from the configured
    provider at bootstrap) and the capability package (empty at bootstrap)."""
    check = objects.put_json("change", {"text": finish_check})
    policies = [
        descriptor(objects, "worker.system_prompt", "prompt", {"text": system_prompt}),
        descriptor(objects, "worker.finish", "workflow", {"builtin": "finish_check.v1",
                                                          "instruction_ref": check}),
        descriptor(objects, "worker.stop", "rule", {"builtin": "default.v1"}),
    ]
    if actor_identity_ref is not None:
        policies.append(descriptor(objects, "worker.actor", "model", {"model_identity_ref": actor_identity_ref}))
    if package_ref is not None:
        policies.append(harness_descriptor(objects, package_ref))
    return policy_set(objects, "worker", policies)


# The first version's R0 (docs/modules/04_RESEARCH_LOOP.md §1): research.diagnose writes one note per failed
# attempt and research.propose reads the notes and the package. Different text, so a different R0 identity.
DIAGNOSE_NOTE = (
    "You read one failed attempt of the current agent version: the request, what the agent did turn by turn "
    "(its last message at length: its own account of what it did and did not do), the tool errors, and every "
    "scored "
    "check with whether it passed. Say why it failed and name one general mechanism "
    "that would prevent this class of failure: a working note (memory), a skill, a check before finishing, a check "
    "around a tool call, or a tool wrapper. Argue why the mechanism applies to a class of tasks, not only this one. "
    "The step that turns notes into a change sees only the notes, so keep in yours, word for word, the interface "
    "facts a change would need: the tools and endpoints the agent called with their methods and field names, the "
    "error text a call returned, and the words of the agent's last message that show what it did not do. These "
    "are not answers; never write this task's specific answers (names, e-mail addresses, ids, values).")
PROPOSE_PACKAGE = (
    "You read the notes on this round's failures, how many failures they cover, and the current capability "
    "package. Propose at most one change to the package that addresses the most common general mechanism. Prefer, "
    "in this order and only among the kinds the core allows: memory notes and skills; a check before finishing; "
    "checks around tool calls; tool wrappers. Change or merge existing components before adding new ones, and "
    "remove what the notes show is useless. State the hypothesis, the expected effect on full-task results, and "
    "what would make you abandon it. Never write a specific task's answers into the package.")


# v0.4 (docs/design/V0_4_PLAN.md §1.4, §1.7; M4 §1 v0.4): R0 under a core whose K carries the coding session. The
# session's system text is research.propose's text followed by SESSION_FORMAT: the protocol (the tree, the inputs, the
# tools, the caps, the declaration, the admission rules) is SESSION_FORMAT's; the method — how many candidates, which
# evidence first, which way to fix — is this text's. v0.3 kept the summary call's PROPOSE_PACKAGE here ("Propose at
# most one change… Prefer… memory notes"), which contradicted the session's k candidates (the five-domain diagnosis,
# RC4). The note prompt asks for the failure's direction; NOTE_FORMAT itself is unchanged, so an older R0's notes keep
# their identities. A v1 core (the summary call) keeps DIAGNOSE_NOTE and PROPOSE_PACKAGE.
DIAGNOSE_SESSION = (
    DIAGNOSE_NOTE + " Also say which way the agent went wrong, as \"direction\" in the JSON: \"omission\" (it left "
    "out something the task required: a read, a write, a step), \"over_action\" (it did something the task, the data "
    "or a policy said not to do: a write, a send, a change to a record it should have left alone) or \"other\".")
PROPOSE_SESSION = (
    "You improve an agent's capability package from its failures on this domain's train tasks, in one coding session. "
    "Method. (1) Start where the agent fights the interface: inputs/api_errors.json counts, per endpoint and tool, "
    "the calls, the error responses by kind, the retries after an error, the responses with a next page the agent "
    "never read, and the empty results. When failures cluster on an endpoint or a tool, make a fix at the interface "
    "first — a package tool that calls it the right way (the right method and fields, every page) or a before_tool or "
    "after_tool hook that corrects or explains the call — before any behavioural text: it works for every task that "
    "uses the endpoint and does not depend on the agent remembering an instruction. (2) Read raw trajectories, "
    "failing and passing: for each candidate read at least one failed attempt among the tasks it predicts to fix, and "
    "read at least one passing attempt — preferably one listed with must-not checks — to see what the agent does "
    "right and must keep doing. Summaries alone guided changes no better than scores did; raw trajectories did "
    "(Meta-Harness: 34.6 with scores, 34.9 with summaries, 50.0 with trajectories). (3) Fix each failure in its "
    "direction: the notes say omission (a read, a write or a step left out), over_action (a write, a send or a change "
    "the task, the data or a policy said not to make) or other. An over_action failure is fixed by restraint — a "
    "condition under which the agent does not write, send or change — never by more reading or writing; a change that "
    "makes the agent do more everywhere mends omissions and breaks the tasks where doing less is right, and the "
    "screen's passing sample holds tasks with must-not checks to catch exactly that. (4) Make up to k candidates, "
    "each on a different cause; for each, say when it acts and when it does not (trigger_condition), and prefer a "
    "change that acts under a condition to one that always acts. Change, merge or delete existing components before "
    "adding new ones. (5) Never write a task's specific answers (names, e-mail addresses, ids, values) into a "
    "candidate: a rule must hold for the domain, not for one task.")


# v0.5 (docs/design/V0_5_PLAN.md §1.1; V0_5_METHOD_LIBRARY.md §4.2): R0's method text under a K with the method
# library — PROPOSE_SESSION and the methods paragraph. The families it names (search, table analysis, date or
# threshold, policy version, pre-send check) are a human prior written into R0 and booked as such in the autonomy
# accounting; no method, pattern or example is given. A K without the methods table bootstraps PROPOSE_SESSION.
METHODS_PARAGRAPH = (
    "(6) Methods. When the actor did the same search, table analysis, date or threshold choice, policy-version choice "
    "or pre-send check by hand and got it wrong in two or more failing train tasks, you may write a method: a "
    "read-only package tool named m_<name>, built from inputs/method_kit/. A method returns evidence with provenance "
    "and an `unclassified` part; it never writes and never decides. Declare its trigger (a pure function of the tool "
    "event and the view), its `on` action, its footprint, at most one context feature and at most four variants in "
    "the module's METHODS literal, and test it on synthetic data. Do not edit middlewares/m_router.py or "
    "middlewares/m_table.py: the router is fixed and the table is fitted from train rewards. inputs/methods.json and "
    "inputs/uncovered.json show where your methods fired, were used, helped or hurt; refine, narrow or retire them "
    "from it, and inputs/method_research.json shows how your own earlier method candidates fared. A new or changed "
    "method must fire, in a replay of the recorded train attempts, on at least two train tasks and on one of the "
    "failing tasks you predict, and run there without error; the check output gives you counts. Judgment failures "
    "(what a policy allows, what not to do) belong in memory or skills, not in a method. (7) Your playbook: finish may "
    "carry `playbook`, lessons about your own research, one per line, each ending with a citation of the ledger "
    "[<candidate id>: <field> <value>]; the host keeps only the lessons whose citation matches the ledger and shows "
    "them to your next session.")
PROPOSE_SESSION_V5 = PROPOSE_SESSION + " " + METHODS_PARAGRAPH


def prompts_of(objects: ct.Objects, set_ref: str) -> dict:
    """target -> the prompt text each prompt policy of a policy set renders with, as bind_release reads it."""
    out = {}
    for ref in objects.load(set_ref, PolicySet).policies:
        p = objects.load(ref, PolicyDescriptor)
        if p.kind == "prompt":
            out[p.target] = parse_prompt(objects.get(p.implementation["artifact_ref"]))["text"]
    return out


def default_research_set(objects: ct.Objects, *, diagnose: str | None = None, propose: str | None = None,
                         checks: tuple[str, ...] = PREFLIGHT_CHECKS, note_contract: bool = False,
                         session: bool = False, methods: bool = False) -> str:
    """R0. With `note_contract`, the first version's R0: research.diagnose takes one failure summary and writes a
    failure note; otherwise the v2 R0 with its requirement list. With `session` too (v0.4: K carries the coding
    session), the note asks for the failure's direction and research.propose is the session's method text; with
    `methods` (v0.5: K carries the method library) that text has the methods paragraph."""
    if note_contract and session:
        default_diagnose, default_propose = DIAGNOSE_SESSION, (PROPOSE_SESSION_V5 if methods else PROPOSE_SESSION)
    elif note_contract:
        default_diagnose, default_propose = DIAGNOSE_NOTE, PROPOSE_PACKAGE
    else:
        default_diagnose, default_propose = DIAGNOSE, PROPOSE
    diagnose = diagnose if diagnose is not None else default_diagnose
    propose = propose if propose is not None else default_propose
    schemas = ("failure_summary.v1", "failure_note.v1") if note_contract else None
    return policy_set(objects, "research", [
        descriptor(objects, "research.diagnose", "prompt", {"text": diagnose}, schemas=schemas),
        descriptor(objects, "research.retrieve", "rule", {"builtin": "research_retrieve.v1", "max_items": 20,
                                                          "sort": "recency"}),
        descriptor(objects, "research.propose", "prompt", {"text": propose}),
        descriptor(objects, "research.preflight", "workflow", {"builtin": "research_preflight.v1",
                                                              "checks": list(checks)}),
    ])


def default_core(objects: ct.Objects, *, domain: str = "marketing", split_ref: str = "", adapter: str = "",
                 scorer: str = "", toolset: str = "", gate_plan: dict | None = None,
                 budget_policy: dict | None = None, retry_policy: dict | None = None,
                 targets: tuple[str, ...] = (), capabilities: dict | None = None,
                 access_rules: dict | None = None) -> str:
    """K for one experiment. Changing any of it is a new experiment identity, not a new version."""
    from .release import code_version
    return objects.put(ct.CoreManifest(
        domain=domain, adapter=adapter, scorer=scorer, toolset=toolset, split_ref=split_ref,
        targets=tuple(targets or (*targets_of("worker"), *targets_of("research"))),
        capabilities={"training": True, "max_prompt_chars": MAX_PROMPT_CHARS, **(capabilities or {})},
        access_rules=dict(access_rules or {"researcher_partitions": ["train", "dev"]}),
        gate_plan=dict(gate_plan or {}), budget_policy=dict(budget_policy or {}),
        retry_policy=dict(retry_policy or {"max_infrastructure_retries": 1}), code_version=code_version()))


def bootstrap_workspace(root, *, objects: ct.Objects | None = None, worker_ref: str | None = None,
                        research_ref: str | None = None, core_ref: str | None = None, **core_kw):
    """A workspace with W0, R0, K and a bootstrap release installed. -> (Releases, release ref)."""
    from .release import Releases
    objects = objects or ct.Objects(root)
    releases = Releases(root, objects)
    worker_ref = worker_ref or default_worker_set(objects)
    research_ref = research_ref or default_research_set(objects)
    core_ref = core_ref or default_core(objects, **core_kw)
    return releases, releases.bootstrap(worker_ref, research_ref, core_ref)


class SessionBinder:
    """Which release each session runs (docs/modules/01_POLICY_RUNTIME.md §4).

    A new session reads the active pointer once; a session already open keeps the release it opened with, so an
    install reaches only the sessions that start after it. Before each step the revocation list is checked, and a
    session whose release was revoked continues on the defaults release — the fixed safe version — rather than
    hot-swapping to whatever is active now.
    """

    def __init__(self, releases, services: Services, *, profile: str = "task"):
        self.releases = releases
        self.services = services
        self.profile = profile
        self._pins: dict[str, BoundRuntime] = {}
        self._reasons: dict[str, str] = {}

    def open(self, session: str) -> BoundRuntime:
        if session not in self._pins:
            ref = self.releases.active()["release_ref"]
            if ref is None:
                raise BindError("nothing is installed: bootstrap a release before opening a session")
            kind = self.releases.authorize(ref, profile=self.profile)
            self._pins[session] = bind_release(ref, profile=self.profile, services=self.services,
                                               authorization=kind)
        return self._pins[session]

    def current(self, session: str) -> BoundRuntime:
        """The session's release, or the fixed safe version when it has been revoked since the session opened.

        The defaults release if there is one that still stands, otherwise the registered defaults themselves
        (fixed_defaults). Either way the session does not hot-swap to whatever is active now: that version was
        never the one this session was pinned to, and mid-session swapping is how two halves of one run end up
        attributed to one version.
        """
        bound = self.open(session)
        if not self.releases.revoked(bound.release_ref):
            return bound
        self._reasons[session] = f"{ct.short(bound.release_ref)} revoked"
        fallback = self.releases.defaults()
        if fallback is None or self.releases.revoked(fallback):
            self._pins[session] = fixed_defaults(self.services, profile=self.profile)
        else:
            self._pins[session] = bind_release(fallback, profile=self.profile, services=self.services,
                                               authorization="defaults_after_revocation")
        return self._pins[session]

    def reason(self, session: str) -> str:
        """Why this session is no longer on the release it opened with."""
        return self._reasons.get(session, "")


def fixed_defaults(services: Services, *, profile: str = "task") -> BoundRuntime:
    """The registered default at every target: no extra prompt, no model, the fallback action.

    This is what a session runs when its release is revoked and no installed ancestor stands. It is deliberately
    not a release and is never written to the pointer: a pointer may only name a version that was evaluated and
    installed (docs/modules/03_EVALUATION_RELEASE.md §4).
    """
    default = descriptor(services.objects, "worker.stop", "rule", {"builtin": "default.v1"})
    from .points import business_stop_point
    return BoundRuntime(release_ref="", profile=profile, authorization="fixed_defaults",
                        worker={"worker.stop": default},
                        stop=StopDecider(default, point=business_stop_point(),
                                         default=resolve("worker.stop").fallback),
                        services=services)


# ---------------------------------------------------------------------------- a proposal as a candidate version
def build_candidate(experiment, *, objects: ct.Objects, parent_release: str, owner: str = "worker",
                    services: Services | None = None, train=None, budget=None) -> ct.BuildResult:
    """An admitted proposal becomes a complete candidate policy set (the `build_candidate` of M0 §3).

    Nothing is promoted here and nothing is installed: the result is a policy set that an evaluation may be given
    a permit for. A change that cannot be turned into a valid descriptor is an invalid build with the reason —
    never an empty candidate that later reads as "the default did fine" (docs/modules/00_CONTRACTS.md §3).

    `train(entry, budget) -> {"model_ref", "spec", "inference", "artifacts", "costs"}` is the caller's training
    step (pipeline.train_artifact over a built dataset). It is called only for the proposal's training entries,
    and a training failure is a failed build, not a silent prompt-only candidate.
    """
    proposal = objects.load(experiment.proposal_ref, ct.ExperimentProposal)
    parent = objects.load(parent_release, ct.SystemRelease)
    parent_set = parent.worker_ref if owner == "worker" else parent.research_ref
    artifacts: dict = {}
    costs: dict = {}
    changed: list[PolicyDescriptor] = []
    errors: list[str] = []
    parent_policies = {p.target: p for p in (objects.load(r, PolicyDescriptor)
                                             for r in objects.load(parent_set, PolicySet).policies)}
    for change in proposal.changes:
        target, kind = change["target"], change["kind"]
        try:
            payload = objects.get(change["change_ref"]) if "change_ref" in change else change["change"]
            if kind == "model":
                # Admission refuses these (research/experiment.py core_checks); an experiment frozen before it did
                # is refused here by the same rule, before the preflight bind would import a backend it names.
                from .research.experiment import model_change_reasons
                refused = model_change_reasons(change, proposal, objects)
                if refused:
                    errors.extend(refused)
                    continue
            # A replaced policy keeps its parent's contract: an R candidate that rewrites the diagnose prompt of a
            # note-contract R stays on the per-task note path (docs/modules/04_RESEARCH_LOOP.md §1).
            old = parent_policies.get(target)
            schemas = (old.input_schema, old.output_schema) if old is not None else None
            changed.append(descriptor(objects, target, kind, dict(payload) if isinstance(payload, dict)
                                      else payload, evidence_refs=proposal.evidence_refs, schemas=schemas))
            artifacts[target] = change.get("change_ref")
        except (ct.ContractError, KeyError, TypeError, ValueError) as e:
            errors.append(f"{target}: {e}")
    for entry in proposal.training:
        if train is None:
            errors.append(f"training {entry['point']} was proposed and this build has no training step")
            continue
        try:
            trained = train(entry, budget)
        except Exception as e:  # noqa: BLE001 - a training failure is a failed build with its reason
            errors.append(f"training {entry['point']}: {type(e).__name__}: {e}"[:300])
            continue
        if not trained or not trained.get("model_ref"):
            errors.append(f"training {entry['point']}: {trained.get('reason') if trained else 'no artifact'}")
            continue
        target = trained.get("target", "worker.stop")
        changed.append(descriptor(objects, target, "model", trained.get("spec") or {},
                                  model_ref=trained["model_ref"], inference=trained.get("inference") or {},
                                  evidence_refs=proposal.evidence_refs))
        artifacts[target] = trained.get("artifact_ref")
        costs.update(trained.get("costs") or {})
    if errors:
        return ct.BuildResult(status="invalid", artifacts=artifacts, errors=tuple(errors), costs=costs)
    try:
        candidate = replace_policies(objects, parent_set, changed,
                                     provenance_ref=objects.put_json("change", {
                                         "experiment": experiment.id, "proposal": experiment.proposal_ref,
                                         "parent_release": parent_release}))
    except ct.ContractError as e:
        return ct.BuildResult(status="invalid", artifacts=artifacts, errors=(str(e),), costs=costs)
    core = objects.load(parent.core_ref, ct.CoreManifest)
    validation = validate_policy_set(objects.load(candidate, PolicySet), objects,
                                     {**(core.capabilities or {}), "targets": core.targets or None})
    if not validation.ok:
        return ct.BuildResult(status="invalid", artifacts=artifacts, errors=tuple(validation.errors), costs=costs)
    if services is not None:
        # An artifact that cannot be assembled is caught here rather than during the comparison, where it would
        # cost a full dev run to discover.
        from .release import required_capabilities_of
        try:
            worker_ref = candidate if owner == "worker" else parent.worker_ref
            release = ct.SystemRelease(
                worker_ref=worker_ref,
                research_ref=candidate if owner == "research" else parent.research_ref,
                core_ref=parent.core_ref, authorization_ref=parent.authorization_ref, parent_ref=parent_release,
                target_harness_ref=parent.target_harness_ref,
                required_capabilities=required_capabilities_of(objects, parent.required_capabilities, worker_ref))
            bind_release(objects.put(release), profile="preflight", services=services)
        except BindError as e:
            return ct.BuildResult(status="invalid", artifacts=artifacts, errors=(str(e),), costs=costs)
    return ct.BuildResult(status="ready", candidate_ref=candidate, artifacts=artifacts, costs=costs)
