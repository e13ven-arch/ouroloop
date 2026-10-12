"""The sealed final (docs/design/EVALUATE.md §6; DESIGN §6.6 V5): the only source of an outside number.

A FinalPlan names, before any test task is read, every arm (W0, W* as "W_star", optionally R1's W*), the test tasks, the repeats
and the claims. `seal` writes it and marks it sealed; its hash is committed to git by the user before the first test
attempt (PLAN rule 6). `run_final` then runs every arm on every task `repeats` times, each attempt independent; it
prints the expected spend first and refuses when the final's share cannot hold it; run again after an interruption it
runs only what is missing. It reports; it does not promote, tune or choose: a comparison that turns out badly stays in
the report, because dropping an arm now would make the remaining numbers a selection rather than a measurement.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..contracts import ContractError, Events, Objects, Record, check_refs, now, register, write_json
from .gate import refill_attempts
from .results import TaskResult, summarise

CLAIMS = ("C1", "C2", "C3", "C4")
ARM_NAME = re.compile(r"\w[\w.-]{0,63}")   # an arm's name is part of every attempt id, which is a session id
STUCK_BOUNDS = 2           # settlements that never come back hold their bound; a share keeps room for this many


@register
@dataclass(frozen=True)
class FinalPlan(Record):
    TYPE = "finalization"
    experiment_id: str = ""
    tasks: tuple = ()
    repeats: int = 1
    arms: dict = field(default_factory=dict)      # arm name -> release ref
    claims: tuple = ("C1",)
    split_hash: str = ""
    core_ref: str = ""
    max_infrastructure_retries: int = 1
    sealed_at: str = ""                           # volatile: not part of the plan's identity

    def __post_init__(self) -> None:
        object.__setattr__(self, "tasks", tuple(self.tasks))
        object.__setattr__(self, "claims", tuple(self.claims))
        unknown = sorted(set(self.claims) - set(CLAIMS))
        if unknown:
            raise ContractError(f"unknown claims {unknown}: {list(CLAIMS)}")
        if not self.tasks:
            raise ContractError("a final plan names the test tasks before it is sealed")
        if self.repeats < 1:
            raise ContractError("repeats is at least 1")
        if not self.arms:
            raise ContractError("a final plan names its arms")
        bad = sorted(a for a in self.arms if not ARM_NAME.fullmatch(a))
        if bad:
            raise ContractError(f"arm names {bad} cannot be part of an attempt id: letters, digits, _ . -")
        check_refs(self.arms.values(), kind="release", what="arm release")
        if "C1" in self.claims and len(self.arms) < 2:
            raise ContractError("C1 compares W* with W0: the plan names both arms")


def state_path(root: Path, plan: FinalPlan) -> Path:
    return Path(root) / "rsi" / "finalization" / f"{plan.experiment_id}.json"


def seal(plan: FinalPlan, root, objects: Objects) -> dict:
    """Persist the plan and mark it sealed before anything reads a test task. A different plan under the same id is
    refused: a changed final plan is a new activity, and this pool counts as used."""
    ref = objects.put(plan)
    path = state_path(root, plan)
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if state and state.get("plan") != ref:
        raise ValueError(f"{path} already seals another plan ({state.get('plan')}): a changed final plan is a new "
                         f"activity, and this pool counts as used")
    write_json(path, {"plan": ref, "status": state.get("status", "sealed"), "attempts": state.get("attempts", []),
                      "sealed": state.get("sealed", now())})
    Events(root).append("final_sealed", plan=ref, experiment=plan.experiment_id, tasks=len(plan.tasks),
                        repeats=plan.repeats, arms=sorted(plan.arms))
    return {"plan": ref, "path": path}


def final_estimate(rows: list[TaskResult], plan: FinalPlan, *, plan_ref: str | None, execution_of: Callable,
                   pricing=None, concurrency: int = 1, fallback=None) -> dict:
    """What a final is expected to spend, from what is on record: for each arm, the attempts still to run times the
    arm's measured cost per valid attempt over every reading of a release that executes as the arm's does
    (`execution_of(release_ref)` -> (package_ref, core_ref)), its dev readings when it has any; and `concurrency`
    reservations of one call's bound, which the share must hold beside the spend. Without a priced reading the cost is
    `fallback` (a number or {arm: number}) when given, else unknown — never 0."""
    planned = len(plan.tasks) * int(plan.repeats)
    out, total, unknown = [], 0.0, []
    for arm, release in plan.arms.items():
        target = execution_of(release)
        readings = [r for r in rows if r.validity == "valid" and r.cost_usd is not None and r.system_release_ref
                    and (r.system_release_ref == release or (target is not None
                                                             and execution_of(r.system_release_ref) == target))]
        pool = [r for r in readings if r.split == "dev"] or readings
        cpa = round(sum(r.cost_usd for r in pool) / len(pool), 6) if pool else None
        source = ("dev" if pool[0].split == "dev" else "all") if pool else None
        if cpa is None and fallback is not None:
            given = fallback.get(arm) if isinstance(fallback, dict) else fallback
            if given is not None:
                cpa, source = round(float(given), 6), "config"
        done = {r.pair_key for r in rows if plan_ref and r.candidate == arm and r.validity == "valid"
                and str(r.pair_key).startswith(f"{plan_ref}/")}
        todo = max(planned - len(done), 0)
        expected = None if cpa is None else round(todo * cpa, 6)
        if expected is None:
            unknown.append(arm)
        else:
            total += expected
        out.append({"arm": arm, "release": release, "attempts": todo, "done": len(done), "readings": len(pool),
                    "readings_split": source or "none", "cost_per_attempt": cpa, "expected": expected})
    bound = pricing.bound().get("usd") if pricing is not None and getattr(pricing, "bounded", False) else None
    reservations = None if bound is None else round(max(int(concurrency or 1), 1) * float(bound), 4)
    expected = None if unknown else round(total, 6)
    return {"arms": out, "expected": expected, "unknown": unknown, "concurrency": max(int(concurrency or 1), 1),
            "bound": None if bound is None else round(float(bound), 4), "reservations": reservations,
            "suggested_share": None if expected is None or reservations is None else round(expected + reservations, 2)}


def share_problem(estimate: dict, room_usd: float | None, *, stuck: int = STUCK_BOUNDS,
                  share_usd: float | None = None) -> str:
    """Why the final's room cannot hold the estimate, or "": the expected spend plus `concurrency` in-flight bounds and
    `stuck` stuck ones must fit `room_usd` — what the final may spend: its share and what the root has left besides
    (EVALUATE §9: the share is a floor, the root the ceiling). Unknown cost is a problem, not zero."""
    if room_usd is None or estimate.get("bound") is None:
        return ""
    if estimate["expected"] is None:
        return f"the final share cannot be checked: no priced reading of {estimate['unknown']} and no estimate"
    need = estimate["expected"] + estimate["reservations"] + stuck * estimate["bound"]
    if need > float(room_usd) + 1e-9:
        where = (f"the final's room ${float(room_usd):.2f} (its share ${float(share_usd):.2f} and what the root has "
                 f"left besides)" if share_usd is not None else f"the final's room ${float(room_usd):.2f}")
        return (f"{where} cannot hold the plan: expected ${estimate['expected']:.2f} + {estimate['concurrency']} "
                f"in-flight and {stuck} stuck reservations of ${estimate['bound']:.4f} = ${need:.2f}")
    return ""


def estimate_lines(estimate: dict, share_left: float | None = None) -> list[str]:
    lines = [f"estimate   {a['arm']}: {a['attempts']} attempts to run ({a['done']} done) × "
             f"{'$' + format(a['cost_per_attempt'], '.4f') if a['cost_per_attempt'] is not None else 'unknown'} "
             f"per attempt ({a['readings']} {a['readings_split']} readings) = "
             f"{'$' + format(a['expected'], '.2f') if a['expected'] is not None else 'unknown'}"
             for a in estimate["arms"]]
    total = "unknown" if estimate["expected"] is None else f"${estimate['expected']:.2f}"
    reserve = "" if estimate["reservations"] is None else f" + {estimate['concurrency']} in flight " \
                                                          f"${estimate['reservations']:.2f}"
    left = "" if share_left is None else f" (left to the final: ${float(share_left):.2f})"
    lines.append(f"estimate   expected {total}{reserve}{left}")
    return lines


def run_final(plan: FinalPlan, *, root: Path, objects: Objects, run_arm: Callable, recorded: set,
              out: Callable[[str], None] = print) -> dict:
    """Run the sealed plan. `run_arm(arm, release_ref, pairs, retry, supersedes) -> [TaskResult]` belongs to the caller
    (the activity builds each arm's runner shell from its release); every attempt id is determined by the plan, the
    arm, the task, the repeat and the retry, so `recorded` (the attempt ids already in the results) are kept as they
    were and only what is missing runs. Infrastructure failures are refilled under the plan's
    `max_infrastructure_retries`; a legitimate failure is never re-rolled."""
    ref = objects.put(plan)
    path = state_path(root, plan)
    if not path.exists():
        seal(plan, root, objects)
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("plan") != ref:
        raise ValueError(f"{path} seals another plan: nothing is run")
    Events(root).append("test_read", plan=ref, experiment=plan.experiment_id, tasks=len(plan.tasks))
    write_json(path, {**state, "status": "running"})
    done = {a["attempt_id"] for a in state["attempts"]}
    rows: dict = {}
    order = [(t, i) for t in plan.tasks for i in range(plan.repeats)]
    for arm, release in plan.arms.items():
        def run(arm_name, pairs, retry=0, supersedes=None, _release=release):
            return run_arm(arm_name, _release, pairs, retry, supersedes)
        attempts = refill_attempts(run(arm, order), run, arm, int(plan.max_infrastructure_retries))
        reused = sum(1 for a in attempts if a.attempt_id in recorded)
        if reused:
            out(f"resume     {arm}: {reused} attempts already on record were kept, not run again")
        rows[arm] = {"summary": summarise(attempts), "attempts": [a.to_json() for a in attempts]}
        state["attempts"].extend({"attempt_id": a.attempt_id, "arm": arm} for a in attempts
                                 if a.attempt_id not in done)
        write_json(path, {**state, "status": "running"})
        s = rows[arm]["summary"]
        out(f"arm        {arm}: {s['passed']}/{s['tasks']} passed, partial={s['partial_credit']}")
    write_json(path, {**state, "status": "completed"})
    return {"plan": ref, "claims": list(plan.claims), "arms": rows,
            "note": "final results are reported, never used to choose a version"}
