"""M4: from what the results show to an experiment the framework will actually run
(docs/modules/04_RESEARCH_LOOP.md).

The researcher's part and the framework's part are kept apart on purpose. A proposal is untrusted: it carries a
hypothesis, the changes it wants and the evidence it read, and it carries no threshold, no verdict and no result.
The framework adds the parent release, the data and feedback refs, the control version, the formal plan and the
budget, and only then is there an experiment.

Admission is two layers. The auxiliary checks are configurable — a research candidate may reorder them or add one
— and the core checks run regardless: owner against the registered target, evidence against the scope's
boundary, schemas against the registry, training against the labels that actually exist. A proposal cannot talk
its way past them, because they are not asked in natural language.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..evolve import contracts as ct
from ..ledger import new_id
from ..policies import REGISTRY, resolve

NEXT_ACTIONS = ("collect", "repair_runtime", "stop", "none")
STATUSES = ("admitted", "invalid", "collect", "repair_runtime")
CHECK_VERDICTS = ("pass", "warn", "block")


@dataclass
class ProposalResult:
    """What a research step returned. An empty list is a recorded outcome, not a crash: "nothing worth trying
    from this evidence" is an answer, and `next_action` says what would change that."""
    proposals: list = field(default_factory=list)
    next_action: str = "none"
    reason: str = ""
    evidence_refs: tuple = ()
    requirements: tuple = ()
    dropped: tuple = ()                       # proposals beyond the round's candidate limit, in order
    diagnosis: dict | None = None             # v0.6: the notes' coverage of this round's failures

    def __post_init__(self) -> None:
        if self.next_action not in NEXT_ACTIONS:
            raise ct.ContractError(f"next_action {self.next_action!r}: one of {list(NEXT_ACTIONS)}")


@dataclass
class Admission:
    status: str = "admitted"
    reasons: list = field(default_factory=list)
    checks: list = field(default_factory=list)          # {"check", "verdict", "detail", "evidence_refs"}
    proposal_ref: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "admitted"


def normalize(proposal: ct.ExperimentProposal, objects: ct.Objects) -> ct.ExperimentProposal:
    """An inline change becomes an immutable artifact. After this a change is a ref, so what was evaluated and
    what is installed are the same bytes."""
    changes = []
    for change in proposal.changes:
        if "change_ref" in change:
            changes.append(dict(change))
            continue
        changes.append({"target": change["target"], "kind": change["kind"],
                        "change_ref": objects.put_json("change", change["change"])})
    return ct.ExperimentProposal(owner=proposal.owner, hypothesis=proposal.hypothesis,
                                 requirements=proposal.requirements, evidence_refs=proposal.evidence_refs,
                                 changes=tuple(changes), training=proposal.training, expected=proposal.expected,
                                 abandon_if=proposal.abandon_if, notes=proposal.notes, origin=proposal.origin)


# ---------------------------------------------------------------------------- auxiliary checks
def check_input_coverage(proposal, *, context, **_kw) -> tuple[str, str, tuple]:
    """Did the fields this change depends on actually reach the decision?

    The check reads the coverage measured from real traces, not the schema: a field that exists in the schema and
    was empty at every decision is a field the policy never saw. It uses no oracle and decides nothing about
    which business requirement matters (docs/modules/01_POLICY_RUNTIME.md §2).
    """
    coverage = (context.inspection or {}).get("coverage") or {}
    if not coverage:
        return "warn", "no trace coverage for this release yet: nothing says which fields were visible", ()
    missing, empty = [], []
    for schema, entry in coverage.items():
        if not entry.get("n"):
            continue
        required = set(ct.SCHEMAS.get(schema, {}).get("required") or ())
        for field_name, share in (entry.get("present") or {}).items():
            if share:
                continue
            (missing if field_name in required else empty).append(f"{schema}.{field_name}")
    if missing and any(t.get("point") for t in proposal.training):
        return "block", f"training was proposed while these fields never reached the decision: {missing[:5]}", ()
    if missing:
        return "warn", f"required fields never present in the observed traces: {missing[:5]}", ()
    if empty:
        return "warn", f"optional fields were always empty: {empty[:5]}", ()
    return "pass", "every field the core requires was present in the observed traces", ()


def check_label_availability(proposal, *, context, **_kw) -> tuple[str, str, tuple]:
    """Is there a label for what this proposal wants to train? Without one, it is not a training experiment."""
    if not proposal.training:
        return "pass", "no training was proposed", ()
    feedback = context.feedback or {}
    valid = int(feedback.get("valid") or 0)
    labels = feedback.get("labels") or {}
    if valid == 0:
        return "block", "training was proposed and no valid label exists yet", ()
    if len(labels) < 2:
        return "block", f"only one label class is present ({sorted(labels)}): nothing to learn from", ()
    return "pass", f"{valid} valid labels over {sorted(labels)}", ()


def check_duplicate_experiment(proposal, *, history=(), objects=None, **_kw) -> tuple[str, str, tuple]:
    """Has this exact proposal been frozen before?

    A repeat under changed inputs or changed feedback is legitimate, so the check compares the changes and the
    training refs; the caller's history is what a round already tried (docs/modules/04_RESEARCH_LOOP.md §3).
    """
    signature = _signature(proposal)
    for earlier in history or ():
        if earlier == signature:
            return "block", "an identical proposal was already frozen in this activity", ()
    return "pass", "no identical proposal in this activity's history", ()


def _signature(proposal: ct.ExperimentProposal) -> str:
    return ct.digest({"owner": proposal.owner,
                      "changes": sorted(ct.canonical(c).decode() for c in proposal.changes),
                      "training": sorted(ct.canonical(t).decode() for t in proposal.training)})


def frozen_signatures(experience, objects: ct.Objects, parent: str | None = None) -> tuple:
    """The signatures of the proposals this activity already ran on `parent`, from its experiment records: what
    the duplicate_experiment check compares a new proposal with. An experiment that produced no valid
    comparison — lost to infrastructure, or accepted and then not installed — is left out: running it again is
    the retry the loop means to make, not a duplicate. The same change on another parent is a new question."""
    from .loop import INFRASTRUCTURE_REASONS
    out = []
    for row in experience or ():
        if row.get("kind") != "experiment" or (parent is not None and row.get("release_before") != parent):
            continue
        reasons = " ".join(str(r) for r in row.get("reasons") or ())
        if row.get("installation") == "installation_failed" or row.get("build") not in (None, "ready", "invalid") \
                or (row.get("verdict") in (None, "inconclusive")
                    and (not row.get("verdict") or any(k in reasons for k in INFRASTRUCTURE_REASONS))):
            continue
        ref = row.get("proposal")
        if ref and objects is not None and objects.has(ref):
            try:
                out.append(_signature(objects.load(ref, ct.ExperimentProposal)))
            except ct.ContractError:
                continue
    return tuple(out)


AUXILIARY = {"input_coverage": check_input_coverage, "label_availability": check_label_availability,
             "duplicate_experiment": check_duplicate_experiment}


# ---------------------------------------------------------------------------- core admission
def registered_builders() -> tuple[str, ...]:
    """The feedback builders this installation has. Trusted code, so the default is what is registered rather than
    nothing: an empty default would make every training proposal invalid for the wrong reason."""
    from ..evolve.feedback import BUILDERS
    return tuple(sorted({builder.ref for builder in BUILDERS.values()}))


def core_checks(proposal: ct.ExperimentProposal, *, context, core: ct.CoreManifest, objects: ct.Objects,
                owner: str, budget=None, feedback_builders=None) -> list[str]:
    """What no research policy may change, reorder or remove. Every reason returned blocks the experiment."""
    reasons = []
    feedback_builders = registered_builders() if feedback_builders is None else feedback_builders
    if proposal.owner != owner:
        reasons.append(f"this round produces {owner} candidates; the proposal says {proposal.owner}")
    for change in proposal.changes:
        target = change["target"]
        try:
            registration = resolve(target)
        except ct.ContractError as e:
            reasons.append(str(e))
            continue
        if registration.owner != proposal.owner:
            reasons.append(f"{target} is a {registration.owner} target and this is a {proposal.owner} proposal")
        if change["kind"] not in registration.allowed_kinds:
            reasons.append(f"{target} does not take a {change['kind']} change: {list(registration.allowed_kinds)}")
        if core.targets and target not in core.targets:
            reasons.append(f"{target} is not a target this core opens")
        if "change_ref" in change and objects is not None and not objects.has(change["change_ref"]):
            reasons.append(f"{target}: the change artifact {ct.short(change['change_ref'])} is not readable")
    methods = (core.capabilities or {}).get("methods")
    if methods and proposal.owner == "worker":
        # The activity's [improvement] allowed_methods, frozen into K (docs/modules/07_BENCH_REPORT.md §2).
        for change in proposal.changes:
            method = ("harness_edit" if change["target"] == "worker.harness" else
                      "decision_train" if change.get("kind") == "model" else "policy_edit")
            if method not in methods:
                reasons.append(f"{change['target']}: a {method} change, and this activity allows only "
                               f"{list(methods)}")
        if proposal.training and "decision_train" not in methods:
            reasons.append(f"a training entry, and this activity allows only {list(methods)}")
    for entry in proposal.training:
        if not core.capabilities.get("training", True):
            reasons.append("this core does not open training")
        if entry["feedback_ref"] not in set(feedback_builders or ()):
            reasons.append(f"unknown feedback builder {entry['feedback_ref']!r}: "
                           f"{sorted(feedback_builders or ())}")
        for ref_name in ("data_selection_ref", "recipe_ref"):
            if objects is not None and not objects.has(entry[ref_name]):
                reasons.append(f"training {entry['point']}: {ref_name} is not readable")
    bad = [ref for ref in proposal.evidence_refs if ref not in set(context.evidence)]
    if bad:
        reasons.append(f"the proposal cites evidence that is not in its context: {[ct.short(r) for r in bad[:3]]}")
    if budget is not None and not budget.affordable({}):
        reasons.append("the budget is exhausted")
    return reasons


def admit(proposal: ct.ExperimentProposal, *, context, core: ct.CoreManifest, objects: ct.Objects,
          owner: str = "worker", checks: tuple[str, ...] = (), history=(), budget=None,
          feedback_builders=None) -> Admission:
    """The auxiliary checks in the order the research policy asked for, then the core checks always.

    An auxiliary `block` sends the round to collect or repair rather than spending the experiment's budget; a
    core failure makes the proposal invalid. The difference matters: "we do not have the evidence yet" and "this
    proposal is not allowed" are different answers, and only the second is the researcher's mistake.
    """
    out = Admission()
    for name in checks or ():
        fn = AUXILIARY.get(name)
        if fn is None:
            out.status, out.reasons = "invalid", [*out.reasons, f"unknown preflight check {name!r}"]
            continue
        verdict, detail, refs = fn(proposal, context=context, history=history, objects=objects)
        out.checks.append({"check": name, "verdict": verdict, "detail": detail, "evidence_refs": list(refs)})
    reasons = core_checks(proposal, context=context, core=core, objects=objects, owner=owner, budget=budget,
                         feedback_builders=feedback_builders)
    out.checks.append({"check": "core", "verdict": "block" if reasons else "pass",
                       "detail": "; ".join(reasons) or "owner, targets, schemas, evidence and budget are in order",
                       "evidence_refs": []})
    if reasons:
        out.status = "invalid"
        out.reasons = [*out.reasons, *reasons]
        return out
    blocked = [c for c in out.checks if c["verdict"] == "block" and c["check"] != "core"]
    if any(c["check"] == "duplicate_experiment" for c in blocked):
        # Not missing evidence and not a runtime problem: the proposal itself is one this activity already ran.
        out.status = "invalid"
        out.reasons = [*out.reasons, *[c["detail"] for c in blocked]]
        return out
    if blocked:
        out.status = "collect" if any("label" in c["detail"] or "reached the decision" in c["detail"]
                                      for c in blocked) else "repair_runtime"
        out.reasons = [*out.reasons, *[c["detail"] for c in blocked]]
        return out
    out.proposal_ref = objects.put(proposal)
    return out


def freeze(proposal_ref: str, *, objects: ct.Objects, parent_release: str, experience_ref: str, core_ref: str,
           eval_template_ref: str, budget_scope: str, experiment_id: str | None = None) -> ct.Experiment:
    """The experiment as the framework fixes it. Everything here is ours: a researcher cannot set the parent, the
    control, the plan or the budget, because those are what make the result mean anything."""
    return ct.Experiment(id=experiment_id or new_id("x"), proposal_ref=proposal_ref,
                         parent_release=parent_release, experience_ref=experience_ref, core_ref=core_ref,
                         eval_template_ref=eval_template_ref, budget_scope=budget_scope)


# ---------------------------------------------------------------------------- the research step
def note_contract(bound) -> bool:
    """Whether the loaded R runs the first version's per-task diagnosis (failure_summary.v1 -> failure_note.v1)."""
    p = bound.research.get("research.diagnose")
    return p is not None and p.output_schema == "failure_note.v1"


def propose_experiment(context, bound, owner: str = "worker", *, researcher, objects: ct.Objects,
                       max_candidates: int = 1, experience=(), diagnosis=None, live_budget=None) -> ProposalResult:
    """inspect -> diagnose -> retrieve -> propose, through the loaded research policy.

    These are stages of one researcher, not four services. What the loaded R changes is the prompt or the
    configuration of a registered stage, so a round using R1 makes its real requests with R1's text — a manifest
    that merely names R1 while calling R0's prompt fails the acceptance (RL-07).
    """
    diagnose_prompt = bound.prompts.get("research.diagnose", "")
    propose_prompt = bound.prompts.get("research.propose", "")
    if owner == "research" and note_contract(bound):
        # A research slot under the first version's R: no per-task notes (those diagnose task failures), one
        # propose call over R's own prompts and the experience log's train-restricted projection.
        if not hasattr(researcher, "propose_research"):
            raise ct.ContractError("the research step has no research-slot proposal: a first-version R proposes "
                                   "changes to itself through propose_research")
        bound.activity.call("research.propose")
        result = researcher.propose_research(context, propose_prompt, dict(bound.prompts), experience)
        requirements = list(getattr(result, "requirements", ()) or ())
    elif note_contract(bound):
        # docs/modules/04_RESEARCH_LOOP.md §1: one note per selected train failure, then one summary call that
        # returns the Requirements and at most one change.
        if diagnosis is None:
            raise ct.ContractError("the loaded R writes per-task notes and this round has no diagnosis input")
        notes, coverage = diagnosis.run(researcher, diagnose_prompt)
        if live_budget is not None:
            # The notes just spent money: the summary sees the budget as it is now, not as the round began.
            from dataclasses import replace
            context = replace(context, budget=live_budget())
        for _ in notes:
            bound.activity.call("research.diagnose")
        retrieved = retrieve(experience, bound.retrieval, targets=tuple(REGISTRY))
        bound.activity.call("research.retrieve")
        bound.activity.call("research.propose")
        result = researcher.summarize(context, propose_prompt, notes, coverage, retrieved,
                                      package=diagnosis.package, allowed=tuple(diagnosis.components))
        requirements = list(result.requirements)
        result.diagnosis = coverage
        result.budget_seen = getattr(context, "budget", None)     # recorded with the round: what R was shown
    else:
        bound.activity.call("research.diagnose")
        requirements = list(researcher.diagnose(context, diagnose_prompt) or ())
        retrieved = retrieve(experience, bound.retrieval, targets=tuple(REGISTRY))
        bound.activity.call("research.retrieve")
        bound.activity.call("research.propose")
        result = researcher.propose(context, propose_prompt, requirements, retrieved)
    if not isinstance(result, ProposalResult):
        raise ct.ContractError("a research step returns a ProposalResult")
    result.requirements = tuple(requirements)
    if len(result.proposals) > max_candidates:
        result.dropped = tuple(result.proposals[max_candidates:])
        result.proposals = result.proposals[:max_candidates]
    result.proposals = [normalize(p, objects) for p in result.proposals]
    if not result.proposals and result.next_action == "none" and not getattr(result, "unreadable", False):
        result.next_action = "collect" if not requirements else "stop"
        result.reason = result.reason or "the research step proposed nothing"
    return result


def retrieve(experience, config: dict, *, targets: tuple[str, ...] = ()) -> list[dict]:
    """`research_retrieve.v1` over an already authorised snapshot: filter, then a stable sort.

    It can only narrow what the scope already allows — there is no path from here to the workspace, an index of
    another arm, or anything outside the snapshot the caller passed in.
    """
    config = config or {}
    tags = set(config.get("tags") or ())
    wanted = set(config.get("targets") or ())
    statuses = set(config.get("statuses") or ())
    out = []
    for entry in experience or ():
        if tags and not tags & set(entry.get("tags") or ()):
            continue
        if wanted and not wanted & set(entry.get("targets") or ()):
            continue
        if statuses and entry.get("status") not in statuses:
            continue
        out.append(entry)
    if config.get("sort") == "recurrence":
        out.sort(key=lambda e: (-int(e.get("recurrence") or 0), str(e.get("id") or "")))
    else:
        out.sort(key=lambda e: (str(e.get("ts") or ""), str(e.get("id") or "")), reverse=True)
    return out[:int(config.get("max_items", 20))]
