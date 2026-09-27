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
from dataclasses import dataclass, field
from typing import Any, Callable

from .evolve import contracts as ct
from .evolve.contracts import ContractError, PolicyDescriptor, PolicySet, actions_of

# What a prompt may be: text, bounded by K. R cannot raise these; a candidate that wants more is refused.
MAX_PROMPT_CHARS = 20000
PREFLIGHT_CHECKS = ("input_coverage", "label_availability", "duplicate_experiment")
RETRIEVE_SORTS = ("recency", "recurrence")
BUILTINS = ("default.v1", "finish_check.v1", "research_retrieve.v1", "research_preflight.v1")


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


REGISTRY: dict[str, TargetRegistration] = {t.target: t for t in (
    TargetRegistration("worker.system_prompt", "worker", "config", ("prompt",), "research.context.v1", "prompt.v1",
                       "agent.system_prompt"),
    TargetRegistration("worker.finish", "worker", "config", ("prompt", "workflow"), "research.context.v1",
                       "finish_check.v1", "agent.system_prompt.finish_check", builtins=("finish_check.v1",)),
    TargetRegistration("worker.stop", "worker", "decision", ("rule", "model"), "stop.business.v1",
                       "action.stop.v1", "stop.on_finish", fallback="yes", builtins=("default.v1",)),
    TargetRegistration("research.diagnose", "research", "research_step", ("prompt",), "research.context.v1",
                       "requirement_list.v1", "research.round.diagnose"),
    TargetRegistration("research.retrieve", "research", "config", ("rule",), "research.context.v1",
                       "research_retrieve.v1", "research.round.retrieve", builtins=("research_retrieve.v1",)),
    TargetRegistration("research.propose", "research", "research_step", ("prompt",), "research.context.v1",
                       "proposal_list.v1", "research.round.propose"),
    TargetRegistration("research.preflight", "research", "config", ("workflow",), "research.context.v1",
                       "research_preflight.v1", "research.round.admission", builtins=("research_preflight.v1",)),
)}


def resolve(target: str) -> TargetRegistration:
    reg = REGISTRY.get(target)
    if reg is None:
        raise ContractError(f"no registered target {target!r}: the registry has {sorted(REGISTRY)}")
    return reg


def targets_of(owner: str) -> tuple[str, ...]:
    return tuple(t for t, reg in REGISTRY.items() if reg.owner == owner)


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
        if set(config) != {"instruction_ref"}:
            raise ContractError("finish_check.v1 takes instruction_ref only")
        ct.parse_ref(config["instruction_ref"])
        return dict(config)
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
    raise ContractError(f"unknown built-in {builtin!r}: {list(BUILTINS)}")


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
        if p.input_schema != reg.input_schema or p.output_schema != reg.output_schema:
            v.errors.append(f"{p.target} reads {reg.input_schema} and writes {reg.output_schema}; the policy "
                            f"says {p.input_schema} / {p.output_schema}")
        if p.target not in allowed_targets:
            v.errors.append(f"{p.target} is not a target this core opens: {sorted(allowed_targets)}")
        v.errors.extend(_check_carrier(p, objects, reg))
    missing = sorted(set(targets_of(policy_set.owner)) & allowed_targets - set(seen))
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


# ---------------------------------------------------------------------------- building policy sets
def descriptor(objects: ct.Objects, target: str, kind: str, payload: Any, *, owner: str | None = None,
               evidence_refs: tuple = (), limits: dict | None = None, model_ref: str | None = None,
               inference: dict | None = None) -> PolicyDescriptor:
    """A descriptor with its configuration stored. `payload` is the prompt text, the built-in's config, or the
    model's decision spec; `model_ref`/`inference` complete a model policy."""
    reg = resolve(target)
    if kind == "prompt":
        impl = {"kind": "prompt", "artifact_ref": objects.put_json("change", parse_prompt(payload))}
    elif kind in ("rule", "workflow"):
        builtin = payload.pop("builtin") if isinstance(payload, dict) and "builtin" in payload else reg.builtins[0]
        impl = {"kind": kind, "builtin": builtin,
                "config_ref": objects.put_json("change", parse_builtin(builtin, payload))}
    elif kind == "model":
        spec = {"input_schema": reg.input_schema, **dict(payload or {})}
        impl = {"kind": "model", "model_ref": model_ref or "",
                "spec_ref": objects.put_json("change", spec),
                "inference_ref": objects.put_json("change",
                                                  parse_inference(inference or {}, actions_of(reg.output_schema)))}
    else:
        raise ContractError(f"unknown carrier {kind!r}")
    return PolicyDescriptor(owner=owner or reg.owner, target=target, binding=reg.binding,
                            input_schema=reg.input_schema, output_schema=reg.output_schema, implementation=impl,
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

    def call(self, target: str) -> None:
        self.calls[target] = self.calls.get(target, 0) + 1

    def propose(self, target: str, action: str | None) -> None:
        self.proposed.setdefault(target, {})
        self.proposed[target][str(action)] = self.proposed[target].get(str(action), 0) + 1

    def execute(self, target: str, action: str | None) -> None:
        self.executed.setdefault(target, {})
        self.executed[target][str(action)] = self.executed[target].get(str(action), 0) + 1

    def fallback(self, target: str, reason: str) -> None:
        self.fallbacks.setdefault(target, {})
        self.fallbacks[target][reason] = self.fallbacks[target].get(reason, 0) + 1

    def override(self, target: str, reason: str) -> None:
        self.overrides.setdefault(target, {})
        self.overrides[target][reason] = self.overrides[target].get(reason, 0) + 1

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
        final = "\n\n".join(p for p in parts if p)
        return final, {"base_chars": len(base), "parts": [t for t in ("worker.system_prompt", "worker.finish")
                                                          if self.prompts.get(t)],
                       "hash": ct.digest({"prompt": final})[:16]}


@dataclass
class Services:
    """What binding needs from the host. Nothing here is reachable from a policy's configuration."""
    objects: ct.Objects
    runtime: Any = None
    serving: Any = None
    capabilities: dict = field(default_factory=dict)
    probe_models: bool = True                 # ask a loaded model which model it is (PR-04)


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
        v = validate_policy_set(ps, objects, {**caps, "targets": caps.get("targets") or targets_of(owner)})
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


def default_worker_set(objects: ct.Objects, *, system_prompt: str = "", finish_check: str = FINISH_CHECK) -> str:
    check = objects.put_json("change", {"text": finish_check})
    return policy_set(objects, "worker", [
        descriptor(objects, "worker.system_prompt", "prompt", {"text": system_prompt}),
        descriptor(objects, "worker.finish", "workflow", {"builtin": "finish_check.v1",
                                                          "instruction_ref": check}),
        descriptor(objects, "worker.stop", "rule", {"builtin": "default.v1"}),
    ])


def default_research_set(objects: ct.Objects, *, diagnose: str = DIAGNOSE, propose: str = PROPOSE,
                         checks: tuple[str, ...] = PREFLIGHT_CHECKS) -> str:
    return policy_set(objects, "research", [
        descriptor(objects, "research.diagnose", "prompt", {"text": diagnose}),
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
