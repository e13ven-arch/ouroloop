"""Run a candidate on full benchmark tasks and record what each attempt did (docs/DESIGN.md §5, evolve/task_eval).

A candidate's score here is the task result, not agreement with recorded labels: a decision model that fits the
ledger better can still leave the world in a worse state, so nothing in this module reads replay accuracy.

One attempt is: a fresh episode, a fresh agent over that episode's business tools, the agent's run, and then the
official score of the world the tools left. The agent never learns its score, and the scorer is not consulted
during the run — an official scorer used as an online checker would quietly correct the agent's mistakes
(docs/DESIGN.md §5.2).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ..agent import Agent, AgentConfig
from ..agent.tools import ToolResult
from ..benchmark import Budget, RunManifest, SplitManifest, TaskResult
from ..benchmark import attempt_id as bench_attempt_id
from ..benchmark import pair_key as bench_pair_key
from .contracts import CandidateDefect
from .execution import BudgetError, normalize_usage

# The agent's stop reason -> how the attempt ended. Anything else is a task outcome: the model stopped, refused,
# or ran out of output, and that is evidence about the agent.
STOPS = {"max_turns": "step_limit"}


@dataclass
class Attempt:
    """One task result and the episode that produced it, for a caller that wants the trace or the end state."""
    result: TaskResult
    episode: object
    score: object = None


class Budgeted:
    """An episode's tool surface with this attempt's limits on it.

    The limits are part of the run manifest, so both sides of a comparison stop at the same place. A refused call
    tells the model the run is over rather than failing silently, and `exhausted` makes the result say so instead
    of reporting a task the agent simply did not finish.
    """

    def __init__(self, episode, budget: Budget):
        self.episode = episode
        self.budget = budget
        self.started = time.monotonic()
        self.exhausted = ""

    def specs(self):
        return self.episode.specs()

    @property
    def tool_calls(self) -> int:
        """The episode's count, so an observation can say how much of the budget is left."""
        return self.episode.tool_calls

    def mutates(self, name: str, args: dict) -> bool:
        """The episode's answer. A wrapper that dropped this would leave the agent's change count at zero, and the
        completion probe would then label every stop decision from the state at the first one."""
        return self.episode.mutates(name, args)

    def run(self, name: str, args: dict) -> ToolResult:
        over = self.budget.exceeded(tool_calls=self.episode.tool_calls + 1,
                                    seconds=time.monotonic() - self.started)
        if over:
            self.exhausted = self.exhausted or over
            return ToolResult(f"This run's budget is exhausted ({over}). Stop and report what you have done.",
                              True)
        return self.episode.run(name, args)


def run_attempt(suite, task_id: str, make_agent: Callable, *, run: RunManifest, splits: SplitManifest,
                attempt: int = 0, system: str | None = None, probe: bool = False, arm: str = "subject",
                retry_index: int = 0, plan_ref: str = "", release_ref: str | None = None,
                budget_receipt_ref: str | None = None, pricing=None, supersedes: str = "") -> Attempt:
    """One attempt at one task. `make_agent(surface, task, system) -> Agent`.

    Every failure that is not the agent's is an invalid attempt with a reason, never a zero: a benchmark that
    scores its own broken runs reports the runner, not the model (docs/DESIGN.md §5.2). `failure_class` says
    which kind it was, because the kinds are treated differently: an infrastructure failure may be refilled
    under the plan's fixed rule, a candidate defect may not, and an evaluator failure stops the evaluation
    (docs/modules/03_EVALUATION_RELEASE.md §3).

    The episode, the agent and the score share one lifecycle: whatever raises, the episode is handed back to the
    caller to close, and a failure while closing is recorded beside the first error rather than replacing it.

    `probe` attaches the completion probe, which labels this episode's stop decisions from the states they were
    made on. It is refused outside the train split: dev decides between candidates and test is only read at the
    end, so a label from either would be training on the data that is supposed to judge the training
    (docs/DESIGN.md §3.1.3).

    `pricing` (execution.Pricing) turns the attempt's usage into `cost_usd`, call by call, after normalizing each
    provider's usage vocabulary. Without prices, or with a token class that has no price, the cost stays None:
    unknown, not free.
    """
    family, split = splits.family_of(task_id), splits.split_of(task_id)
    if split != run.split:
        raise ValueError(f"{task_id} is in {split}, but this run reads {run.split}")
    if probe and split != "train":
        raise ValueError(f"{task_id} is in {split}: the completion probe writes training labels, and only the "
                         f"train split may produce them")
    entry, task = splits.entry(task_id), suite.tasks[task_id]
    ids = {"attempt_id": bench_attempt_id(run.hash(), task_id, attempt, arm, retry_index),
           "pair_key": bench_pair_key(plan_ref, task_id, attempt) if plan_ref else "",
           "system_release_ref": release_ref, "budget_receipt_ref": budget_receipt_ref,
           "supersedes": supersedes}
    if entry.contract != task.contract:
        return Attempt(TaskResult.broken(task_id, family, split, attempt, run, splits.hash(),
                                         f"contract: the task changed since the split was frozen "
                                         f"({entry.contract[:12]} -> {task.contract[:12]})",
                                         contract=task.contract, failure_class="evaluator", **ids), None)
    episode, agent, t0 = None, None, time.monotonic()
    usage: dict[str, int] = {}
    costs: list = []                  # per call: USD, or None when that call cannot be priced

    def on_event(kind: str, data: dict) -> None:
        raw = data.get("usage") or {}
        for key, value in raw.items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
        if raw and pricing is not None:
            costs.append(pricing.cost(normalize_usage(raw)))

    def cost() -> float | None:
        if pricing is None or not costs or any(c is None for c in costs):
            return None
        return round(sum(costs), 8)

    def broken(reason: str, failure_class: str, termination: str = "infrastructure") -> Attempt:
        if agent is not None and agent.probe is not None:
            agent.probe.label()     # the stop decisions made before the failure still saw a real state
        return Attempt(TaskResult.broken(
            task_id, family, split, attempt, run, splits.hash(), reason[:400], termination=termination,
            contract=task.contract, evidence=agent.session if agent else "",
            elapsed=round(time.monotonic() - t0, 2),
            tool_calls=episode.tool_calls if episode is not None else 0, usage=dict(usage), cost_usd=cost(),
            failure_class=failure_class,
            observation_refs=list(getattr(agent, "_observation_refs", ())), **ids), episode)

    try:
        episode = suite.episode(task_id)
        surface = Budgeted(episode, run.budget)
        agent = make_agent(surface, task, system if system is not None else task.system)
        agent.on_event = on_event
        if probe:
            from ..adapters.automationbench import CompletionProbe
            agent.probe = CompletionProbe(agent.rt, episode, surface=surface)
        # The split travels with the session: anything but train is held out, so the existing data builders can
        # never draw a training sample from a task that is meant to judge the training (ledger.Ledger.sessions).
        agent.session_meta = {**agent.session_meta, "task": task_id, "family": family, "split": split,
                              "held_out": split != "train", "run": run.hash(), "candidate": run.candidate,
                              "attempt": attempt, "arm": arm, "attempt_id": ids["attempt_id"]}
    except Exception as e:  # noqa: BLE001 - the episode or the agent could not be built: no evidence either way
        return broken(f"harness: {type(e).__name__}: {e}", "infrastructure", "harness_error")
    try:
        res = agent.run(task.trigger)
    except CandidateDefect as e:
        return broken(f"candidate: {e}", "candidate_defect", "harness_error")
    except BudgetError as e:
        # The activity's total budget ran out mid-attempt. Not evidence about the agent, and not a transient
        # failure to be refilled either: the comparison it belonged to ends as inconclusive (budget_exhausted).
        return broken(f"budget: {e}", "budget_exhausted", "infrastructure")
    except Exception as e:  # noqa: BLE001 - a provider or harness failure is no evidence about the candidate
        return broken(f"harness: {type(e).__name__}: {e}", "infrastructure", "harness_error")
    labelled = agent.probe.label() if agent.probe is not None else 0
    try:
        score = episode.score()
    except Exception as e:  # noqa: BLE001 - the official scorer failed: this evaluation stops, K is not ours to fix
        return broken(f"scorer: {type(e).__name__}: {e}", "evaluator", "infrastructure")
    termination = "budget" if surface.exhausted else STOPS.get(res.stop, "finished")
    return Attempt(TaskResult(task_id, family, split, attempt, run.candidate, run.hash(), splits.hash(),
                              contract=task.contract, success=score.success,
                              partial_credit=round(score.partial_credit, 6),
                              assertions_passed=score.passed, assertions_scored=score.scored,
                              usage=dict(usage), cost_usd=cost(), elapsed=round(time.monotonic() - t0, 2),
                              steps=res.turns,
                              tool_calls=episode.tool_calls, termination=termination, stop=res.stop,
                              evidence=res.session, decisions=agent.decision_ids(), labelled=labelled,
                              failure_class="" if score.success else "task_outcome",
                              observation_refs=list(agent._observation_refs), **ids),
                   episode, score)


def run_tasks(suite, task_ids: list[str], make_agent: Callable, *, run: RunManifest, splits: SplitManifest,
              results=None, repeats: int = 1, system: str | None = None, probe: bool = False,
              on_result: Callable[[TaskResult], None] | None = None, arm: str = "subject", plan_ref: str = "",
              release_ref: str | None = None, order: list[tuple[str, int]] | None = None,
              retry_index: int = 0, pricing=None, supersedes: dict | None = None) -> list[TaskResult]:
    """Every attempt of a run, in a fixed order, each one recorded as it finishes.

    Results are appended as they come so an interrupted run keeps the attempts it paid for; nothing here reruns
    an invalid attempt, because how many to rerun and under which rule belongs to the run's protocol, not to the
    loop that executes it. `order` runs a given (task, repeat) sequence instead of the default nesting, which is
    how a plan's seeded order alternates two arms.
    """
    out = []
    for task_id, i in (order if order is not None else [(t, i) for t in task_ids for i in range(repeats)]):
        attempt = run_attempt(suite, task_id, make_agent, run=run, splits=splits, attempt=i, system=system,
                              probe=probe, arm=arm, plan_ref=plan_ref, release_ref=release_ref,
                              retry_index=retry_index, pricing=pricing,
                              supersedes=(supersedes or {}).get((task_id, i), ""))
        try:
            out.append(attempt.result)
            if results is not None:
                results.append(attempt.result)
            if on_result:
                on_result(attempt.result)
        finally:
            if attempt.episode is not None:
                try:
                    attempt.episode.close()
                except Exception as e:  # noqa: BLE001 - a failure to clean up never replaces the first error
                    if results is not None:
                        results.append(TaskResult.broken(
                            task_id, splits.family_of(task_id), splits.split_of(task_id), i, run, splits.hash(),
                            f"cleanup: {type(e).__name__}: {e}", failure_class="infrastructure"))
    return out


def make_agent_factory(rt, provider, *, budget: Budget, tiers: dict | None = None, meta: dict | None = None,
                       bound=None, objects=None, stop_view: str | None = None):
    """-> make_agent(surface, task, system): an agent over a benchmark episode's business tools.

    Tool approval is off: there is nobody to ask in a benchmark run, and a declined call would be recorded as
    the agent's choice. The deny list and the tool gate key off the file and shell tools of a working directory,
    which this surface does not have.

    `bound` is a release assembled by policies.bind_release. With one, its worker prompt policies compose the
    system prompt, its worker.stop policy answers at stop/on_finish, and each decision's observation and trace
    are stored in `objects`. Without one, nothing changes: the harness runs as it did.
    """
    view = stop_view or ("stop.business.v1" if bound is not None else "stop.file.v2")

    def make(surface, task, system: str):
        prompt = system
        if bound is not None:
            prompt, _ = bound.system_prompt(system)
        return Agent(provider, rt, surface, tiers=tiers, system=prompt,
                     config=AgentConfig(approve="auto", max_turns=budget.max_steps, gated_tools=(),
                                        stop_view=view),
                     session_meta={**(meta or {}), "task": task.task_id}, policy=bound, objects=objects)

    return make
