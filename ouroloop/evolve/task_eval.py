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
                attempt: int = 0, system: str | None = None, probe: bool = False) -> Attempt:
    """One attempt at one task. `make_agent(surface, task, system) -> Agent`.

    Every failure that is not the agent's is an invalid attempt with a reason, never a zero: a benchmark that
    scores its own broken runs reports the runner, not the model (docs/DESIGN.md §5.2).

    `probe` attaches the completion probe, which labels this episode's stop decisions from the states they were
    made on. It is refused outside the train split: dev decides between candidates and test is only read at the
    end, so a label from either would be training on the data that is supposed to judge the training
    (docs/DESIGN.md §3.1.3).
    """
    family, split = splits.family_of(task_id), splits.split_of(task_id)
    if split != run.split:
        raise ValueError(f"{task_id} is in {split}, but this run reads {run.split}")
    if probe and split != "train":
        raise ValueError(f"{task_id} is in {split}: the completion probe writes training labels, and only the "
                         f"train split may produce them")
    entry, task = splits.entry(task_id), suite.tasks[task_id]
    if entry.contract != task.contract:
        return Attempt(TaskResult.broken(task_id, family, split, attempt, run, splits.hash(),
                                         f"contract: the task changed since the split was frozen "
                                         f"({entry.contract[:12]} -> {task.contract[:12]})",
                                         contract=task.contract), None)
    episode, t0 = suite.episode(task_id), time.monotonic()
    usage: dict[str, int] = {}

    def on_event(kind: str, data: dict) -> None:
        for key, value in (data.get("usage") or {}).items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value

    surface = Budgeted(episode, run.budget)
    agent = make_agent(surface, task, system if system is not None else task.system)
    agent.on_event = on_event
    if probe:
        from ..adapters.automationbench import CompletionProbe
        agent.probe = CompletionProbe(agent.rt, episode, surface=surface)
    # The split travels with the session: anything but train is held out, so the existing data builders can never
    # draw a training sample from a task that is meant to judge the training (ledger.Ledger.sessions).
    agent.session_meta = {**agent.session_meta, "task": task_id, "family": family, "split": split,
                          "held_out": split != "train", "run": run.hash(), "candidate": run.candidate,
                          "attempt": attempt}
    try:
        res = agent.run(task.trigger)
    except Exception as e:  # noqa: BLE001 - a provider or harness failure is no evidence about the candidate
        if agent.probe is not None:
            agent.probe.label()     # the stop decisions made before the failure still saw a real state
        return Attempt(TaskResult.broken(task_id, family, split, attempt, run, splits.hash(),
                                         f"harness: {type(e).__name__}: {e}"[:400],
                                         termination="harness_error", contract=task.contract,
                                         evidence=agent.session, elapsed=round(time.monotonic() - t0, 2),
                                         tool_calls=episode.tool_calls, usage=dict(usage)), episode)
    labelled = agent.probe.label() if agent.probe is not None else 0
    score = episode.score()
    termination = "budget" if surface.exhausted else STOPS.get(res.stop, "finished")
    return Attempt(TaskResult(task_id, family, split, attempt, run.candidate, run.hash(), splits.hash(),
                              contract=task.contract, success=score.success,
                              partial_credit=round(score.partial_credit, 6),
                              assertions_passed=score.passed, assertions_scored=score.scored,
                              usage=dict(usage), elapsed=round(time.monotonic() - t0, 2), steps=res.turns,
                              tool_calls=episode.tool_calls, termination=termination, stop=res.stop,
                              evidence=res.session, decisions=agent.decision_ids(), labelled=labelled),
                   episode, score)


def run_tasks(suite, task_ids: list[str], make_agent: Callable, *, run: RunManifest, splits: SplitManifest,
              results=None, repeats: int = 1, system: str | None = None, probe: bool = False,
              on_result: Callable[[TaskResult], None] | None = None) -> list[TaskResult]:
    """Every attempt of a run, in a fixed order, each one recorded as it finishes.

    Results are appended as they come so an interrupted run keeps the attempts it paid for; nothing here reruns
    an invalid attempt, because how many to rerun and under which rule belongs to the run's protocol, not to the
    loop that executes it.
    """
    out = []
    for task_id in task_ids:
        for i in range(repeats):
            attempt = run_attempt(suite, task_id, make_agent, run=run, splits=splits, attempt=i, system=system,
                                  probe=probe)
            if attempt.episode is not None:
                attempt.episode.close()
            out.append(attempt.result)
            if results is not None:
                results.append(attempt.result)
            if on_result:
                on_result(attempt.result)
    return out


def make_agent_factory(rt, provider, *, budget: Budget, tiers: dict | None = None, meta: dict | None = None):
    """-> make_agent(surface, task, system): an agent over a benchmark episode's business tools.

    Tool approval is off: there is nobody to ask in a benchmark run, and a declined call would be recorded as
    the agent's choice. The deny list and the tool gate key off the file and shell tools of a working directory,
    which this surface does not have.
    """
    def make(surface, task, system: str):
        return Agent(provider, rt, surface, tiers=tiers, system=system,
                     config=AgentConfig(approve="auto", max_turns=budget.max_steps, gated_tools=()),
                     session_meta={**(meta or {}), "task": task.task_id})

    return make
