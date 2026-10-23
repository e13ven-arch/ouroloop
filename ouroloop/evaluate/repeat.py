"""The repetitive stream (DESIGN v2.0 §6.23; EVALUATE §6b): the external number of the last version.

Real work repeats: the same request comes back with other parameters. AutomationBench has no parameter variants (each
task is its own workflow), so the smallest repetition is the same task met again. A RepeatPlan names, before any test
task is read, the test tasks in the stream's order, how many times each is met (`encounters`, 3), the base release
every task starts from and the arms: `learn` — the first encounter runs the base, then the improver corrects the
harness for that task from its own trajectory without reading any score (improver/repeat.py), and the later encounters
run the task's own harness; `control` — the base runs every encounter, so a task that passes on its second try by
chance is not read as learning. `seal` writes the plan and marks it sealed; its hash is committed before the first
test attempt (PLAN rule 6). The report reads the `repeat_scored` events: the pass rate at each encounter per arm, the
paired difference between the first and the last encounter per arm and between the arms at the last one, resampled
over families (gate.paired_bootstrap). No grading row ever reaches the improver: a task's score is read by the host
after its encounters and written as an event.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..contracts import ContractError, Events, Objects, Record, check_refs, now, register, write_json
from .gate import paired_bootstrap

ARMS = ("learn", "control")


@register
@dataclass(frozen=True)
class RepeatPlan(Record):
    TYPE = "repeat_plan"
    experiment_id: str = ""
    tasks: tuple = ()                 # the test tasks, in the stream's order (a rehearsal: train tasks, `split`)
    split: str = "test"              # which split the tasks come from: test for the number, train for a rehearsal
    encounters: int = 3               # how many times each task is met
    base: str = ""                    # the release every task starts from (the seed's W0, or a released W*)
    arms: tuple = ARMS
    split_hash: str = ""
    core_ref: str = ""
    max_infrastructure_retries: int = 1
    max_probes: int = 4               # the learn arm's extra executions a task, at most (the candidates' probes)
    feedback: str = "attempts"       # what the improver learns from besides the run: "attempts" — one bit per
                                      # attempt, whether it was accepted (the first encounter and each probe: a
                                      # request sent back until it is right; the accepted candidate is adopted, no
                                      # composer's guess); "outcome" — the first encounter's bit only, the composer
                                      # judges the probes; "none" — nothing (Harness²'s setting: the run alone)
    sealed_at: str = ""               # volatile: not part of the plan's identity

    def __post_init__(self) -> None:
        object.__setattr__(self, "tasks", tuple(self.tasks))
        object.__setattr__(self, "arms", tuple(self.arms))
        if not self.tasks:
            raise ContractError("a repeat plan names the test tasks before it is sealed")
        if int(self.encounters) < 2:
            raise ContractError("encounters is at least 2: the first reads the base, the rest the learnt harness")
        if not self.arms or sorted(set(self.arms) - set(ARMS)):
            raise ContractError(f"arms are among {list(ARMS)}")
        if self.split not in ("train", "test"):
            raise ContractError("split is train (a rehearsal) or test (the number)")
        if self.feedback not in ("attempts", "outcome", "none"):
            raise ContractError("feedback is attempts (one bit per attempt), outcome (the first encounter's bit) or none")
        check_refs([self.base], kind="release", what="base release")


def state_path(root, plan: RepeatPlan) -> Path:
    return Path(root) / "rsi" / "repeat" / f"{plan.experiment_id}.json"


def seal(plan: RepeatPlan, root, objects: Objects) -> dict:
    """Persist the plan and mark it sealed before anything reads a test task; another plan under the same id is
    refused (as final.seal: a changed plan is a new activity, and this pool counts as used)."""
    ref = objects.put(plan)
    path = state_path(root, plan)
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if state and state.get("plan") != ref:
        raise ValueError(f"{path} already seals another repeat plan ({state.get('plan')}): a changed plan is a new "
                         f"activity, and this pool counts as used")
    write_json(path, {"plan": ref, "status": state.get("status", "sealed"), "sealed": state.get("sealed", now())})
    Events(root).append("repeat_sealed", plan=ref, experiment=plan.experiment_id, tasks=len(plan.tasks),
                        encounters=plan.encounters, arms=list(plan.arms), base=plan.base)
    return {"plan": ref, "path": path}


def scored(events, *, plan_ref: str | None = None) -> dict:
    """{arm: {task: {encounter: {passed, partial_credit}}}} from the `repeat_scored` events (the latest per key)."""
    out: dict = {}
    for e in events:
        if plan_ref is not None and e.get("plan") != plan_ref:
            continue
        out.setdefault(e["arm"], {}).setdefault(e["task"], {})[int(e["encounter"])] = {
            "passed": bool(e.get("passed")), "partial_credit": e.get("partial_credit")}
    return out


def summary(table: dict, *, encounters: int) -> dict:
    """Per arm and encounter: tasks read, passed, pass rate, mean partial credit."""
    out: dict = {}
    for arm, by_task in table.items():
        rows = []
        for k in range(1, int(encounters) + 1):
            got = [v[k] for v in by_task.values() if k in v]
            passed = sum(1 for g in got if g["passed"])
            partial = [g["partial_credit"] for g in got if g.get("partial_credit") is not None]
            rows.append({"encounter": k, "tasks": len(got), "passed": passed,
                         "pass_rate": round(passed / len(got), 4) if got else None,
                         "partial_credit": round(sum(partial) / len(partial), 4) if partial else None})
        out[arm] = rows
    return out


def paired(table: dict, *, families: dict, subject: tuple, control: tuple, seed: int = 0,
           replicates: int = 2000) -> dict | None:
    """The paired difference in pass rate between (arm, encounter) `subject` and `control`, in points, resampled over
    families; None when either side has no reading."""
    s_arm, s_k = subject
    c_arm, c_k = control
    s = {t: 100.0 * float(v[s_k]["passed"]) for t, v in table.get(s_arm, {}).items() if s_k in v}
    c = {t: 100.0 * float(v[c_k]["passed"]) for t, v in table.get(c_arm, {}).items() if c_k in v}
    if not s or not c:
        return None
    got = paired_bootstrap(s, c, families, seed=seed, replicates=replicates)
    return {"subject": f"{s_arm}:e{s_k}", "control": f"{c_arm}:e{c_k}", "points": got["point"], "low": got["low"],
            "high": got["high"], "tasks": got["tasks"], "groups": got["groups"], "confidence": got["confidence"],
            "excludes_zero": got.get("excludes_zero")}
