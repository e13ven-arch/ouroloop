"""Run a candidate on full benchmark tasks and record what each attempt did (docs/DESIGN.md §5, evolve/task_eval).

A candidate's score here is the task result, not agreement with recorded labels: a decision model that fits the
ledger better can still leave the world in a worse state, so nothing in this module reads replay accuracy.

One attempt is: a fresh episode, a fresh agent over that episode's business tools, the agent's run, and then the
official score of the world the tools left. The agent never learns its score, and the scorer is not consulted
during the run — an official scorer used as an online checker would quietly correct the agent's mistakes
(docs/DESIGN.md §5.2).

v0.3 (docs/modules/01_POLICY_RUNTIME.md §1 宿主作为运行壳): under a K frozen with `max_effort`, the agent is the
runner shell (agent.runner.PackageAgent) — the package's own agent program in the isolated worker, or the default
agent — with every model call through the host's gateway and every tool call through its broker; a hard limit ends
the attempt as termination `budget` with the dimension in `limit`, and a crash of the agent at run time is
infrastructure. An older K keeps the v1 loop (agent.loop.Agent) and its records (PR7-13).
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable

from ..agent import Agent, AgentConfig
from ..agent.harness_package import mount
from ..agent.runner import PackageAgent, k_max_effort, k_methods
from ..agent.tools import ToolResult
from ..benchmark import Budget, RunManifest, SplitManifest, TaskResult
from ..benchmark import attempt_id as bench_attempt_id
from ..benchmark import pair_key as bench_pair_key
from .contracts import CandidateDefect
from ..providers import is_account_error
from .execution import BudgetError, normalize_usage

# The v1 loop's stop reason -> how the attempt ended. Anything else is a task outcome: the model stopped, refused,
# or ran out of output, and that is evidence about the agent. Only the v1 loop (agent.loop.Agent, an older K) still
# writes `step_limit`; under the runner shell the same end is termination `budget` with `limit` = act_calls, and
# `step_limit` appears in old records only (docs/modules/00_CONTRACTS.md §2, v0.3).
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
        self.limit = ""                   # the dimension of the first refusal (tool_calls or seconds), for `limit`

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
        calls, seconds = self.episode.tool_calls + 1, time.monotonic() - self.started
        over = self.budget.exceeded(tool_calls=calls, seconds=seconds)
        if over:
            self.exhausted = self.exhausted or over
            self.limit = self.limit or self.budget.limit_hit(tool_calls=calls, seconds=seconds) or "tool_calls"
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
        # Usage is read from the `assistant` event alone: under the runner shell the `gateway_call` line carries the
        # same usage again, and counting both would book every call twice.
        raw = (data.get("usage") or {}) if kind == "assistant" else {}
        for key, value in raw.items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
        if raw:
            # The cache counts sit in nested fields for some providers (OpenAI's prompt_tokens_details), and a
            # flat sum drops them. Kept here under one name, so the attempt's cost can be recomputed later from
            # its own record at any price, rather than only from the transcript.
            normalized = normalize_usage(raw)
            for key in ("cache_read_tokens", "cache_write_tokens"):
                if normalized[key]:
                    usage[key] = usage.get(key, 0) + normalized[key]
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
    except CandidateDefect as e:
        # The candidate could not be assembled for a reason of its own (a package tool shadowing a base tool).
        return broken(f"candidate: {e}", "candidate_defect", "harness_error")
    except Exception as e:  # noqa: BLE001 - the episode or the agent could not be built: no evidence either way
        if is_account_error(e):
            raise
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
        if is_account_error(e):
            # The provider refuses the account (no balance, a revoked key): every further attempt would fail the
            # same way, so the run stops here rather than recording each one as an infrastructure failure.
            raise
        return broken(f"harness: {type(e).__name__}: {e}", "infrastructure", "harness_error")
    labelled = agent.probe.label() if agent.probe is not None else 0
    try:
        score = episode.score()
    except Exception as e:  # noqa: BLE001 - the official scorer failed: this evaluation stops, K is not ours to fix
        return broken(f"scorer: {type(e).__name__}: {e}", "evaluator", "infrastructure")
    # A hard limit ends the attempt as `budget` and names its dimension: the runner shell's LimitHit (res.limit), or
    # the surface's refusal of a tool call (either loop). The v1 loop's turn cap stays `step_limit` (PR7-13).
    limit = getattr(res, "limit", None) or (surface.limit if surface.exhausted else None)
    termination = "budget" if limit or surface.exhausted else STOPS.get(res.stop, "finished")
    return Attempt(TaskResult(task_id, family, split, attempt, run.candidate, run.hash(), splits.hash(),
                              contract=task.contract, success=score.success,
                              partial_credit=round(score.partial_credit, 6),
                              assertions_passed=score.passed, assertions_scored=score.scored,
                              usage=dict(usage), cost_usd=cost(), elapsed=round(time.monotonic() - t0, 2),
                              steps=res.turns, limit=limit if termination == "budget" else None,
                              tool_calls=episode.tool_calls, termination=termination, stop=res.stop,
                              evidence=res.session, decisions=agent.decision_ids(), labelled=labelled,
                              failure_class="" if score.success else "task_outcome",
                              observation_refs=list(agent._observation_refs), **ids),
                   episode, score)


_CAP_LOCK = threading.Lock()
_CAP: dict = {"size": None, "semaphore": None}


def in_flight_cap(concurrency: int) -> threading.BoundedSemaphore:
    """The process's one cap on attempts in flight (docs/modules/05_EXECUTION_BUDGET.md §2 并发的范围, v0.3).

    `[bench] concurrency` bounds the attempts running at once in this process, not per arm or per call of
    `run_tasks`: a gate's candidate arm and its fresh control reading, the screens of k candidates, and a
    confirmation reading all draw on this one semaphore, so two callers in one process cannot between them run
    twice the setting. The pool is created here, on first use, at the size asked for; a different size later (a
    lower one on a resume) replaces it for the attempts that start afterwards, and the attempts already holding the
    old one finish under it. A research slot's two arms run one after the other in this process, each in its own
    workspace (campaign.run_campaign), and their `run_tasks` calls draw on this same cap; no two `run_tasks` calls
    overlap today, so the cap and the setting coincide.
    """
    with _CAP_LOCK:
        size = max(int(concurrency or 1), 1)
        if _CAP["semaphore"] is None or _CAP["size"] != size:
            _CAP["size"], _CAP["semaphore"] = size, threading.BoundedSemaphore(size)
        return _CAP["semaphore"]


def run_tasks(suite, task_ids: list[str], make_agent: Callable, *, run: RunManifest, splits: SplitManifest,
              results=None, repeats: int = 1, system: str | None = None, probe: bool = False,
              on_result: Callable[[TaskResult], None] | None = None, arm: str = "subject", plan_ref: str = "",
              release_ref: str | None = None, order: list[tuple[str, int]] | None = None,
              retry_index: int = 0, pricing=None, supersedes: dict | None = None,
              concurrency: int = 1) -> list[TaskResult]:
    """Every attempt of a run, each one recorded as it finishes; the list comes back in the plan's order.

    Results are appended as they come so an interrupted run keeps the attempts it paid for; nothing here reruns
    an invalid attempt, because how many to rerun and under which rule belongs to the run's protocol, not to the
    loop that executes it. `order` runs a given (task, repeat) sequence instead of the default nesting, which is
    how a plan's seeded order alternates two arms.

    `concurrency` attempts run at a time (docs/modules/05_EXECUTION_BUDGET.md §2). Each has its own episode,
    agent and session; what they share (the meter, the bound release, the stores) is written under locks. The
    recording itself — results, grading, the caller's `on_result` — stays on this thread, in completion order.
    It is a setting of the machine, not of the plan: an attempt's limits and its reservation are the same at any
    concurrency. Interrupted, the attempts already running finish and are recorded; the rest never start. The
    cap is the process's (in_flight_cap), shared by every arm and screen of a gate, not this call's alone.
    """
    pairs = list(order if order is not None else [(t, i) for t in task_ids for i in range(repeats)])
    cap = in_flight_cap(concurrency) if int(concurrency or 1) > 1 else None

    def attempt_of(task_id, i):
        if cap is None:
            return run_attempt(suite, task_id, make_agent, run=run, splits=splits, attempt=i, system=system,
                               probe=probe, arm=arm, plan_ref=plan_ref, release_ref=release_ref,
                               retry_index=retry_index, pricing=pricing,
                               supersedes=(supersedes or {}).get((task_id, i), ""))
        with cap:
            return run_attempt(suite, task_id, make_agent, run=run, splits=splits, attempt=i, system=system,
                               probe=probe, arm=arm, plan_ref=plan_ref, release_ref=release_ref,
                               retry_index=retry_index, pricing=pricing,
                               supersedes=(supersedes or {}).get((task_id, i), ""))

    def record(task_id, i, attempt) -> TaskResult:
        try:
            if results is not None:
                results.append(attempt.result)
                if attempt.score is not None and hasattr(results, "put_grading"):
                    # M3 keeps the grader's per-assertion outcome apart from the result (grading.jsonl).
                    results.put_grading(attempt.result, getattr(attempt.score, "assertions", None))
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
        return attempt.result

    if int(concurrency or 1) <= 1:
        return [record(task_id, i, attempt_of(task_id, i)) for task_id, i in pairs]
    from ..adapters.automationbench import hide_live_keys
    done: dict[int, TaskResult] = {}
    executor = ThreadPoolExecutor(max_workers=int(concurrency), thread_name_prefix="attempt")
    with hide_live_keys():
        pending = {executor.submit(attempt_of, t, i): n for n, (t, i) in enumerate(pairs)}
        try:
            while pending:
                finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for future in finished:
                    n = pending.pop(future)
                    done[n] = record(pairs[n][0], pairs[n][1], future.result())
        except BaseException as stop:
            # Stopped (an interrupt, an account error from one attempt): nothing not yet running starts. What is
            # running finishes and is recorded, so the book and the results agree on what was paid for — except an
            # attempt the interrupt's signal itself reached (its model call died under it): that is no evidence and
            # is left unrecorded, so a resume runs the pair again.
            for future in list(pending):
                if future.cancel():
                    pending.pop(future)
            interrupted = isinstance(stop, KeyboardInterrupt)
            if pending:
                _say(f"stopped ({type(stop).__name__}): {len(pending)} attempt(s) still running, each recorded when "
                     f"it finishes (at most {run.budget.max_seconds}s); interrupt again to stop waiting, and a resume "
                     f"runs them again")
            current = None
            try:
                for future in list(pending):
                    current = n = pending.pop(future)
                    try:
                        attempt = future.result()
                    except Exception:  # noqa: BLE001 - the attempt's own failure: the first failure is the one reported
                        continue
                    if interrupted and attempt.result.termination == "harness_error":
                        _close(attempt)
                        continue
                    try:
                        done[n] = record(pairs[n][0], pairs[n][1], attempt)
                    except Exception as e:  # noqa: BLE001 - a paid attempt that could not be recorded is named, not lost silently
                        _say(f"attempt {pairs[n]} finished but could not be recorded: {type(e).__name__}: {e}")
            except KeyboardInterrupt:
                still = [pairs[m] for m in pending.values()] + ([pairs[current]] if current is not None else [])
                _say(f"interrupted again: {len(still)} running attempt(s) not waited for: {still}; the process ends "
                     f"when they finish, and a resume runs them again")
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    return [done[n] for n in range(len(pairs))]


def _close(attempt) -> None:
    if attempt.episode is not None:
        try:
            attempt.episode.close()
        except Exception:  # noqa: BLE001 - process-local state; nothing outside to tear down
            pass


def _say(line: str) -> None:
    import sys
    print(line, file=sys.stderr, flush=True)


def make_agent_factory(rt, provider, *, budget: Budget, tiers: dict | None = None, meta: dict | None = None,
                       bound=None, objects=None, stop_view: str | None = None, worker=None,
                       max_effort: str | None = None, methods: dict | None = None,
                       extra_system: dict | None = None):
    """-> make_agent(surface, task, system): an agent over a benchmark episode's business tools.

    Tool approval is off: there is nobody to ask in a benchmark run, and a declined call would be recorded as
    the agent's choice. The deny list and the tool gate key off the file and shell tools of a working directory,
    which this surface does not have.

    `bound` is a release assembled by policies.bind_release. With one, its worker prompt policies compose the
    system prompt. Which loop runs is K's (docs/modules/01_POLICY_RUNTIME.md §1, v0.3): a release whose core fixes
    `max_effort` — frozen under v0.3 — runs under the runner shell (agent.runner.PackageAgent: the package's own
    agent in the isolated worker, or the default agent, every model call through the gateway and every tool call
    through the broker, no decision point asked); `max_effort` given here says the same without reading K. An older
    K, and a run with no release at all, keep the v1 loop (agent.loop.Agent): its worker.stop policy answers at
    stop/on_finish, each decision's observation and trace are stored in `objects`, and its records keep their old
    shapes (PR7-13). Without a release nothing changes: the harness runs as it did.
    """
    view = stop_view or ("stop.business.v1" if bound is not None else "stop.file.v2")
    shell = max_effort if max_effort is not None else k_max_effort(bound)
    # v0.5 (docs/design/V0_5_PLAN.md §1.1, §1.4): K's methods for the view, and per task the text a library stream's
    # retry adds to the system prompt; a v0.4 K has neither and composes as before
    method_view = methods if methods is not None else (k_methods(bound) if shell is not None else None)

    def make(surface, task, system: str):
        prompt = system
        if bound is not None:
            prompt, parts = bound.system_prompt(system)
            if "worker.harness" in parts["parts"]:
                bound.activity.call("worker.harness")       # the package's text reached the actor's prompt
        if shell is not None:
            return PackageAgent(provider, rt, surface, bound=bound, system=prompt, budget=budget, max_effort=shell,
                                worker=worker, session_meta={**(meta or {}), "task": task.task_id},
                                methods=method_view,
                                extra_system=str((extra_system or {}).get(getattr(task, "task_id", None)) or ""))
        if bound is not None:
            # The capability package: skills are read through the host's read_skill, and a package that runs
            # code needs the isolated worker, refused here otherwise (docs/modules/01_POLICY_RUNTIME.md §1).
            surface = mount(surface, bound, worker=worker, on_read=lambda name, found: bound.activity.execute(
                "worker.harness", f"read_skill:{name}" if found else "read_skill:missing"))
        agent = Agent(provider, rt, surface, tiers=tiers, system=prompt,
                      config=AgentConfig(approve="auto", max_turns=budget.max_steps, gated_tools=(),
                                         stop_view=view),
                      session_meta={**(meta or {}), "task": task.task_id}, policy=bound, objects=objects)
        runtime = getattr(surface, "runtime", None)
        if runtime is not None:
            # Every hook answer and package tool call, with its broker calls, goes to the session transcript.
            def record(kind, data, agent=agent, bound=bound):
                agent._emit(kind, **data)
                bound.activity.execute("worker.harness", f"{kind}:{data.get('hook') or data.get('tool')}")
            runtime.on_event = record
        return agent

    return make
