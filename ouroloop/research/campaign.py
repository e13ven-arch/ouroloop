"""M6: comparing two research policies by what they produce (docs/modules/06_RESEARCH_CAMPAIGN.md).

A research candidate is not a better-sounding prompt. It has to improve task policies under the same starting
point and the same budget, and be measured on tasks the inner loop never saw. So a campaign is:

    two arms, R0 and R1, from the same W0 and the same starting experience, with equal fixed budgets
    each arm runs the ordinary worker loop, research slots disabled, and seals the W it actually installed
    both sealed W* are then run on the same outer tasks, by the evaluator, and compared

What the module refuses is the interesting part. An arm cannot read the other arm's results, the outer oracle or
the mainline pointer — not because a prompt asks it not to, but because it is handed a different workspace and a
scope that does not include them. Repeating one task pool with ten seeds stays one independence group, because
the seeds do not make the tasks independent. And the W* an arm produced does not reach the mainline: it is
evidence about a research policy, and adopting it means comparing it as a task policy on its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..evolve import contracts as ct
from ..evolve.execution import BudgetExhausted
from ..evolve.task_gate import GatePlan, paired_bootstrap, per_task, regression_rate

PHASES = ("research_dev", "final")
SELECTION_RULES = ("last_installed", "last_accepted")


@dataclass
class CampaignPair(ct.Record):
    """One starting point, run by both arms. `independence_group` is the statistical unit: two pairs over the same
    task pool share one, whatever their seeds."""
    TYPE = "campaignpair"
    pair_id: str = ""
    independence_group: str = ""
    initial_w_ref: str = ""
    initial_experience_ref: str = ""
    inner_train_groups: tuple[str, ...] = ()
    inner_dev_groups: tuple[str, ...] = ()
    outer_groups: tuple[str, ...] = ()
    split_ref: str = ""
    repeats: int = 1
    seed_schedule: tuple[int, ...] = (0,)
    schema_version: int = ct.SCHEMA_VERSION

    def __post_init__(self) -> None:
        overlap = sorted((set(self.inner_train_groups) | set(self.inner_dev_groups)) & set(self.outer_groups))
        if overlap:
            raise ct.ContractError(f"the outer groups of {self.pair_id} are also inner groups: {overlap}; a task "
                                   f"the inner loop optimises on cannot measure what the inner loop produced")
        if not self.outer_groups:
            raise ct.ContractError(f"{self.pair_id} has no outer groups to be measured on")


@dataclass
class CampaignPlan(ct.Record):
    TYPE = "campaign"
    id: str = ""
    phase: str = "research_dev"
    core_ref: str = ""
    r_control_ref: str = ""
    r_candidate_ref: str = ""
    campaign_pairs: tuple[str, ...] = ()          # CampaignPair refs
    arm_budget: dict = field(default_factory=dict)
    # Each pair's outer comparison, one infrastructure refill included: the outer scope's limit, and held on the
    # book before the common inputs and the arms run, so they cannot spend it (docs/modules/05 §2). None: no hold.
    outer_budget: dict | None = None
    worker_round_limit: int = 2
    worker_gate_plan_ref: str = ""
    research_gate_plan: dict = field(default_factory=dict)
    retry_policy: dict = field(default_factory=dict)
    selection_rule: str = "last_installed"
    finalization_ref: str | None = None
    discovery: str = "automatic"                  # automatic | scripted | manual_seed (RC-10)
    schema_version: int = ct.SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ct.ContractError(f"campaign phase {self.phase!r}: one of {list(PHASES)}")
        if self.selection_rule not in SELECTION_RULES:
            raise ct.ContractError(f"selection rule {self.selection_rule!r}: one of {list(SELECTION_RULES)}")
        if not self.campaign_pairs:
            raise ct.ContractError("a campaign plan names its pairs before it runs")
        if self.r_control_ref == self.r_candidate_ref:
            raise ct.ContractError("the two arms would run the same research policy")
        if self.discovery not in ("automatic", "scripted", "manual_seed"):
            raise ct.ContractError(f"discovery {self.discovery!r}")
        GatePlan.from_json(self.research_gate_plan) if self.research_gate_plan else None

    def gate(self) -> GatePlan:
        return GatePlan.from_json(self.research_gate_plan)


class CampaignError(RuntimeError):
    """A campaign that cannot be run as declared, or an arm that reached outside its scope."""


# ---------------------------------------------------------------------------- validation
def validate(plan: CampaignPlan, *, objects: ct.Objects, lineage=None) -> list[str]:
    """What makes this campaign unusable before anything is paid for.

    `lineage(ref) -> set of family ids` reports which task families a starting W or experience was derived from.
    An initial version trained on an outer family makes the outer pool a development pool, and renaming the task
    does not change that (RC-04).
    """
    reasons = []
    for owner, ref in (("control", plan.r_control_ref), ("candidate", plan.r_candidate_ref)):
        try:
            policy_set = objects.load(ref, ct.PolicySet)
        except (KeyError, ct.ContractError) as e:
            reasons.append(f"the {owner} research policy set is unreadable: {e}")
            continue
        if policy_set.owner != "research":
            reasons.append(f"the {owner} arm was given a {policy_set.owner} policy set")
    try:
        same = (set(objects.load(plan.r_control_ref, ct.PolicySet).policies)
                == set(objects.load(plan.r_candidate_ref, ct.PolicySet).policies))
    except (KeyError, ct.ContractError):
        same = False
    if same:
        # IT6-16: a new manifest over R0's own policies would compare R0 with itself under another name.
        reasons.append("the candidate R names exactly the control's policies: only its manifest differs, so there "
                       "is no research method to compare")
    groups: dict[str, list[str]] = {}
    for ref in plan.campaign_pairs:
        try:
            pair = objects.load(ref, CampaignPair)
        except (KeyError, ct.ContractError) as e:
            reasons.append(f"pair {ct.short(ref)} is unreadable: {e}")
            continue
        groups.setdefault(pair.independence_group, []).append(pair.pair_id)
        if lineage is not None:
            seen = set(lineage(pair.initial_w_ref)) | set(lineage(pair.initial_experience_ref))
            leaked = sorted(seen & set(pair.outer_groups))
            if leaked:
                reasons.append(f"{pair.pair_id}: the starting version or experience already comes from the outer "
                               f"families {leaked}")
    if plan.phase == "final" and len(groups) < 2:
        reasons.append(f"a final campaign reports over independence groups and this plan has {len(groups)}: "
                       f"more seeds on one pool do not make it two")
    return reasons


def arm_workspace(root: str | Path, pair_id: str, arm: str, plan_id: str = "") -> Path:
    """Each arm's own workspace: its own pointer, its own experience index, its own objects — per campaign, so a
    later campaign over a pair of the same name starts from nothing of an earlier one's.

    The separation is what keeps the arms independent; it is not a security boundary, and the docs say so. Package
    code itself runs only in the isolated worker.
    """
    return Path(root) / "campaigns" / plan_id / pair_id / arm if plan_id else Path(root) / "campaigns" / pair_id / arm


# ---------------------------------------------------------------------------- execution
@dataclass
class ArmOutcome:
    arm: str
    pair_id: str
    w_star: str = ""                          # the sealed W: the last installed one, or W0
    rounds: tuple = ()
    installed: tuple = ()
    stop_reason: str = ""
    costs: dict = field(default_factory=dict)
    inner_evidence: tuple = ()
    research_requests: tuple = ()             # what the arm's R actually sent, so R0 and R1 are distinguishable

    @property
    def paused(self) -> str:
        """Why the arm is suspended rather than finished, or "" (docs/modules/06_RESEARCH_CAMPAIGN.md §3)."""
        return self.stop_reason if self.stop_reason in PAUSES else ""

    def to_json(self) -> dict:
        return {"arm": self.arm, "pair_id": self.pair_id, "w_star": self.w_star,
                "rounds": [dict(r) for r in self.rounds], "installed": list(self.installed),
                "stop_reason": self.stop_reason, "costs": dict(self.costs),
                "research_requests": list(self.research_requests)}


# An arm that stopped for one of these is paused, not finished: after the repair it resumes under the same frozen
# conditions to the frozen round count, and until then no comparison is made with it.
PAUSES = ("repair_runtime", "infrastructure_failures", "infrastructure")


def select_w(outcome: ArmOutcome, pair: CampaignPair, rule: str = "last_installed") -> str:
    """The W this arm produced, by the rule fixed before the campaign: the last one it actually installed, or the
    starting W when it installed none. Never chosen by the outer score."""
    if rule == "last_installed":
        return outcome.installed[-1] if outcome.installed else pair.initial_w_ref
    accepted = [r for r in outcome.rounds if r.get("verdict") == "accepted"]
    return accepted[-1].get("release_after") or pair.initial_w_ref if accepted else pair.initial_w_ref


def run_campaign(plan: CampaignPlan, *, run_arm, evaluate_outer, objects: ct.Objects, budget,
                 experiment_ref: str = "", lineage=None, events=None, prepare_pair=None,
                 prompts: dict | None = None, changed: tuple = ()) -> ct.EvaluationReceipt:
    """Run every declared pair with both arms, then compare what they produced on the outer tasks.

    `run_arm(pair, arm, r_ref, workspace, budget) -> ArmOutcome` runs the ordinary worker loop in isolation, and
    `evaluate_outer(pair, {arm: w_star}) -> {arm: [TaskResult]}` is the evaluator's own run. Both belong to the
    caller: this function fixes the order, the isolation, the budgets and the verdict. With `prompts`
    ({"control": {target: text}, "candidate": ...}), each prompt R1 changed must be what the candidate arm's
    matching step actually sent, or the comparison is invalid (docs/modules/06_RESEARCH_CAMPAIGN.md §4);
    `changed` names every target R1 changed, so a change no request digest can show is reported as unverified.

    The outer comparison of each pair is charged to `<plan>:<pair>:outer` and the common inputs to
    `<plan>:<pair>:common`; the receipt's costs include both when they cost anything. With `plan.outer_budget`,
    each pair's outer scope holds that much before anything else runs, released only to the outer runs themselves
    or, when a pair's outer comparison does not run, back to the activity. After the common inputs, each arm's
    scope holds its whole share, released to that arm when it starts, so the arm that runs second is not left
    short by what ran before it; when the root cannot hold both, neither arm runs and the verdict is inconclusive.
    A paused campaign keeps its holds, and a restart finds them rather than holding or checking again.

    A pair whose two arms sealed the same W is not run on the outer tasks: it would measure one W twice and say
    nothing about R. The verdict is inconclusive, and no later pair is run (docs/modules/06 §3).
    """
    reasons = validate(plan, objects=objects, lineage=lineage)
    if reasons:
        raise CampaignError("; ".join(reasons))
    plan_ref = objects.put(plan)
    pairs = [objects.load(ref, CampaignPair) for ref in plan.campaign_pairs]
    outer: dict[str, dict[str, list]] = {}
    arms_out: dict[str, list[ArmOutcome]] = {"control": [], "candidate": []}
    extra: dict = {}

    def spent(scope: str, limits: dict | None = None) -> dict:
        used = budget.child(scope, dict(limits or {})).remaining().get("used") or {}
        return {k: round(float(v), 6) for k, v in used.items() if isinstance(v, (int, float)) and v}

    def total(name: str, used: dict) -> None:
        for k, v in used.items():
            extra.setdefault(name, {})[k] = round(extra.get(name, {}).get(k, 0) + v, 6)

    holds: dict = {}

    def hold(pair_id: str, what: str, limits: dict) -> None:
        account = budget.child(f"{plan.id}:{pair_id}:{what}", dict(limits))
        holds[(pair_id, what)] = (account, account.hold(f"{plan.id}:{pair_id}:{what}:hold", dict(limits)))

    def release(pair_id: str, what: str) -> None:
        held = holds.pop((pair_id, what), None)
        if held is not None:
            held[0].settle(held[1], None, "released")

    def release_all() -> None:
        for key in list(holds):
            release(*key)
    if plan.outer_budget:
        for pair in pairs:
            hold(pair.pair_id, "outer", plan.outer_budget)
    for pair in pairs:
        sealed = {}
        # What both arms start from, prepared once and charged to the campaign rather than to whichever arm runs
        # first: W0's measurement and, when the arms diagnose alike, the first round's notes (docs/modules/06 §2).
        shared = prepare_pair(pair, budget.child(f"{plan.id}:{pair.pair_id}:common", {})) if prepare_pair else None
        if prepare_pair:
            total("common", spent(f"{plan.id}:{pair.pair_id}:common"))
        if plan.arm_budget:
            # Each arm is capped by its own share, but both draw on the activity's root: held here, the share of
            # the arm that runs second cannot be spent by the one before it, and a comparison under unequal budgets
            # would say nothing about R (docs/modules/06_RESEARCH_CAMPAIGN.md §3). Found again on a restart.
            try:
                for arm in ("control", "candidate"):
                    hold(pair.pair_id, arm, plan.arm_budget)
            except BudgetExhausted as e:
                release_all()
                return _receipt(plan, plan_ref, experiment_ref, objects, arms_out, outer, "inconclusive",
                                [f"{pair.pair_id}: the activity cannot set aside both arms' shares "
                                 f"{dict(plan.arm_budget)} ({e}), so the arms would not run under equal budgets"],
                                {"discovery": plan.discovery}, {}, extra)
        for arm, r_ref in (("control", plan.r_control_ref), ("candidate", plan.r_candidate_ref)):
            # A fixed child budget per arm: what one arm does not spend is not handed to the other, because equal
            # spend is part of what makes the comparison a comparison (docs/modules/05_EXECUTION_BUDGET.md §2).
            release(pair.pair_id, arm)
            child = budget.child(f"{plan.id}:{pair.pair_id}:{arm}", dict(plan.arm_budget))
            outcome = run_arm(pair, arm, r_ref, child, **({"shared": shared} if prepare_pair else {}))
            outcome.w_star = outcome.w_star or select_w(outcome, pair, plan.selection_rule)
            arms_out[arm].append(outcome)
            sealed[arm] = outcome.w_star
            if events is not None:
                events.append("campaign_arm", campaign=plan.id, pair=pair.pair_id, arm=arm,
                              w_star=outcome.w_star, stop_reason=outcome.stop_reason,
                              installed=list(outcome.installed), paused=bool(outcome.paused))
        paused = [f"{pair.pair_id}/{o.arm} paused ({o.paused})" for outs in arms_out.values() for o in outs
                  if o.pair_id == pair.pair_id and o.paused]
        if paused:
            # Two arms that completed different numbers of rounds are not compared, and a suspended arm is not
            # a loss: nothing is measured on the outer tasks, and running the campaign again resumes it.
            return _receipt(plan, plan_ref, experiment_ref, objects, arms_out, outer, "inconclusive",
                            [*paused, "repair the runtime and run the campaign again: each arm resumes to the "
                                      "frozen round count under the same frozen conditions"],
                            {"discovery": plan.discovery}, {}, extra)
        if sealed["control"] == sealed["candidate"]:
            break
        release(pair.pair_id, "outer")
        outer[pair.pair_id] = evaluate_outer(pair, sealed)
        total("outer", spent(f"{plan.id}:{pair.pair_id}:outer", plan.outer_budget))
    release_all()                       # what no outer comparison will run on goes back to the activity
    verdict, notes, metrics, uncertainty = judge_research(plan, pairs, outer, arms_out)
    if prompts is not None:
        unused, unverified = unused_changes(prompts.get("control") or {}, prompts.get("candidate") or {}, arms_out)
        notes = [*notes, *unverified]
        if unused:
            # A run that named R1 and sent another configuration compared nothing about R1: a protocol error.
            verdict, notes = "invalid", [*unused, *notes]
    for target in changed:
        if target not in RESEARCH_STEPS:
            notes = [*notes, f"R1 changes {target}, which no request digest can show: its use is not verified"]
    return _receipt(plan, plan_ref, experiment_ref, objects, arms_out, outer, verdict, notes, metrics, uncertainty,
                    extra)


# The research steps that render each research prompt target, by the step names researchers record.
RESEARCH_STEPS = {"research.diagnose": ("diagnose", "note"), "research.propose": ("propose", "revise")}


def carries(request: dict, text: str) -> bool:
    """Whether one recorded research request was rendered from this prompt text."""
    system = request.get("system")
    if isinstance(system, str):
        return system == text or system.startswith(text + "\n\n")
    return request.get("prompt_hash") == ct.digest({"system": text})[:16]


def unused_changes(control: dict, candidate: dict, arms_out: dict) -> tuple[list[str], list[str]]:
    """-> (problems, notes). For every prompt target R1 changed, the candidate arm's requests for that step must
    carry R1's text (IT6-16). An arm that never reached the step shows nothing either way; requests that record
    no prompt at all cannot show it, and are reported as unverified rather than passed."""
    problems, notes = [], []
    for target, steps in RESEARCH_STEPS.items():
        new, old = candidate.get(target), control.get(target)
        if not new or new == old:
            continue
        for outcome in arms_out.get("candidate", ()):
            sent = [r for r in outcome.research_requests if r.get("step") in steps]
            recorded = [r for r in sent if "system" in r or "prompt_hash" in r]
            if sent and not recorded:
                notes.append(f"{outcome.pair_id}/candidate: its {'/'.join(steps)} requests record no prompt, so "
                             f"the use of R1's {target} is not verified")
            elif recorded and not any(carries(r, new) for r in recorded):
                problems.append(f"{outcome.pair_id}/candidate: no {'/'.join(steps)} request carried R1's {target}; "
                                f"the arm ran another configuration than the plan names")
    return problems, notes


def _receipt(plan, plan_ref, experiment_ref, objects, arms_out, outer, verdict, notes, metrics,
             uncertainty, extra=None) -> ct.EvaluationReceipt:
    results_ref = objects.put_json("change", {
        "campaign": plan_ref, "arms": {arm: [o.to_json() for o in outs] for arm, outs in arms_out.items()},
        "outer": {pid: {arm: [r.to_json() for r in rows] for arm, rows in by_arm.items()}
                  for pid, by_arm in outer.items()}})
    return ct.EvaluationReceipt(experiment_ref=experiment_ref, profile="research",
                                subject_ref=plan.r_candidate_ref, control_ref=plan.r_control_ref,
                                plan_ref=plan_ref, results_ref=results_ref, verdict=verdict,
                                reasons=tuple(notes), metrics=metrics, uncertainty=uncertainty,
                                costs=_costs(arms_out, extra))


def _costs(arms_out: dict, extra: dict | None = None) -> dict:
    """What each arm used — its budget's `used`, or the plain numbers a caller recorded — plus the common inputs
    and the outer comparison when they cost anything (the whole comparison's cost, docs/modules/06 §3)."""
    out = {}
    for arm, outcomes in arms_out.items():
        total: dict = {}
        for outcome in outcomes:
            costs = outcome.costs or {}
            for key, value in (costs.get("used") if isinstance(costs.get("used"), dict) else costs).items():
                if isinstance(value, (int, float)):
                    total[key] = round(total.get(key, 0) + value, 6)
        out[arm] = total
    for name, used in (extra or {}).items():
        if used:
            out[name] = used
    return out


def judge_research(plan: CampaignPlan, pairs, outer: dict, arms_out: dict) -> tuple[str, list[str], dict, dict]:
    """The research verdict: the outer full-task quality of what each arm produced, paired by task, by
    independence group.

    Within a pair the two arms are compared on the same tasks: a task counts only when each of its repeats has a
    valid attempt on both arms, and one that does not makes the comparison inconclusive, as an incomplete pair
    does in the worker gate — the tasks left would be a selection. Two arms that sealed the same W compared nothing
    about R: inconclusive too. The unit is the independence group, not the pair and not the task: pairs over one
    task pool are correlated, so they are merged before anything is resampled, and a plan with one group can only
    report an exploratory result.
    """
    gate = plan.gate()
    by_id = {pair.pair_id: pair for pair in pairs}
    groups = {pair.pair_id: pair.independence_group for pair in pairs}
    sealed: dict[str, dict[str, str]] = {}
    for arm, outs in arms_out.items():
        for o in outs:
            pair = by_id.get(o.pair_id)
            sealed.setdefault(o.pair_id, {})[arm] = o.w_star or (select_w(o, pair, plan.selection_rule)
                                                                 if pair is not None else "")
    identical = sorted(p for p, w in sealed.items() if len(w) == 2 and w["control"] == w["candidate"])
    per_group: dict[str, dict[str, list[float]]] = {}
    notes: list[str] = []
    incomplete: dict[str, dict[str, int]] = {}
    paired_tasks = 0
    for pair_id, by_arm in outer.items():
        if pair_id in identical:
            continue
        rows = {arm: list(by_arm.get(arm) or []) for arm in ("control", "candidate")}
        expected = {(r.task_id, r.attempt) for arm_rows in rows.values() for r in arm_rows}
        missing: dict = {}
        for arm, arm_rows in rows.items():
            valid = {(r.task_id, r.attempt) for r in arm_rows if r.validity == "valid"}
            if not valid:
                notes.append(f"{pair_id}/{arm}: no valid outer attempt")
            last = {(r.task_id, r.attempt): r.failure_class or r.validity for r in arm_rows}
            for key in sorted(expected - valid):
                missing.setdefault(key, []).append(f"{arm} {last.get(key, 'absent')}")
        if missing:
            classes: dict[str, int] = {}
            for why in missing.values():
                for arm_why in why:
                    kind = arm_why.split(" ", 1)[1]
                    classes[kind] = classes.get(kind, 0) + 1
            incomplete[pair_id] = classes
            shown = [f"{t}/r{i} ({', '.join(why)})" for (t, i), why in list(missing.items())[:5]]
            notes.append(f"{pair_id}: {len(missing)} outer tasks are not complete on both arms: {shown}"
                         + (" ..." if len(missing) > 5 else ""))
        candidate, control = per_task(rows["candidate"]), per_task(rows["control"])
        shared = sorted(set(candidate) & set(control) - {t for t, _ in missing})
        if not shared:
            continue
        paired_tasks += len(shared)
        group = groups.get(pair_id, pair_id)
        for arm, tasks in (("candidate", candidate), ("control", control)):
            per_group.setdefault(group, {}).setdefault(arm, []).append(sum(tasks[t] for t in shared) / len(shared))
    subject = {g: sum(v["candidate"]) / len(v["candidate"]) for g, v in per_group.items() if v.get("candidate")}
    control = {g: sum(v["control"]) / len(v["control"]) for g, v in per_group.items() if v.get("control")}
    shared = sorted(set(subject) & set(control))
    stats = paired_bootstrap(subject, control, {g: g for g in shared}, seed=gate.bootstrap_seed,
                             replicates=gate.bootstrap_replicates, confidence=gate.confidence_level)
    metrics = {"groups": len(shared), "pairs": len(outer), "tasks": paired_tasks,
               "candidate_outer_pass_rate": round(sum(subject.values()) / len(subject), 6) if subject else None,
               "control_outer_pass_rate": round(sum(control.values()) / len(control), 6) if control else None,
               "gain": stats["point"], "regression_rate": regression_rate(subject, control),
               "incomplete": incomplete, "identical_arms": identical,
               "not_reached": [f"{o.pair_id}/{arm}" for arm, outs in arms_out.items() for o in outs
                               if o.stop_reason and o.stop_reason != "rounds_done"],
               "installed": {arm: [len(o.installed) for o in outs] for arm, outs in arms_out.items()},
               "discovery": plan.discovery}
    uncertainty = {"outer": stats, "significant": bool(stats.get("excludes_zero") and len(shared) >= 3),
                   "note": "one campaign pool shows stability within its conditions, never a general "
                           "improvement in research ability"}
    if plan.discovery == "manual_seed":
        notes.append("the research change was written by hand: the engineering chain is exercised, automatic "
                     "discovery is not claimed")
    if identical:
        return "inconclusive", [*notes, f"{', '.join(identical)}: both arms sealed the same W "
                                        f"{ct.short(sealed[identical[0]]['control'])}, so the outer tasks would "
                                        f"measure one W twice and nothing about R was compared"], \
            metrics, uncertainty
    if incomplete:
        return "inconclusive", [*notes, "the outer comparison is not complete on both arms: a task without a valid "
                                        "attempt on each side is not compared, and the tasks left are a selection"], \
            metrics, uncertainty
    if not shared:
        return "inconclusive", [*notes, "no independence group produced outer results on both arms"], metrics, \
            uncertainty
    if len(shared) < gate.min_groups:
        return "inconclusive", [*notes, f"{len(shared)} independence groups, the plan needs {gate.min_groups}: "
                                        f"exploratory only"], metrics, uncertainty
    if stats["point"] < gate.min_gain:
        return "rejected", [*notes, f"the candidate's W produced {stats['point']} more than the control's, below "
                                    f"the plan's {gate.min_gain}"], metrics, uncertainty
    if metrics["regression_rate"] > gate.max_regression_rate:
        return "rejected", [*notes, f"regression rate {metrics['regression_rate']} above "
                                    f"{gate.max_regression_rate}"], metrics, uncertainty
    # Why an acceptance is not significant, as the receipt's own interval shows it: an interval that still holds
    # zero, or one that excludes it over fewer than three independence groups, which is no interval to rely on.
    why = ("" if uncertainty["significant"] else
           "; the interval still contains zero, so C4 is not supported by this run" if not stats.get("excludes_zero")
           else f"; {len(shared)} independence group{'s' if len(shared) != 1 else ''}, fewer than the 3 a significant "
                f"result needs, so C4 is not supported by this run")
    return "accepted", [*notes, "development acceptance of the research policy" + why], metrics, uncertainty
