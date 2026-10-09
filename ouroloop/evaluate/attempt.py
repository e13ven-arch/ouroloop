"""One attempt and a run of many (docs/design/EVALUATE.md §3, §4).

An attempt is: a fresh episode, a fresh runner shell over that episode's tools (runtime.PackageAgent), the agent's
run, and then the official score of the world the tools left. The agent never learns its score, and the scorer is not
consulted during the run.

Which attempts are evidence: termination `finished`, `budget` (a hard limit, the world scored as it stands) and
`harness_error` (the package's own defect, scored as it stands, failure class `candidate_defect`, which makes a gate
comparison invalid) are valid; `infrastructure` is not, and a fixed rule may refill it. The grader's per-assertion
outcome goes beside the result (grading.jsonl), readable for train only.
"""
from __future__ import annotations

import hashlib
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..contracts import BudgetError, StaleWorker
from ..harness.package import Package
from ..runtime.records import Transcript, session_file
from ..runtime.runner import AgentResult, PackageAgent
from .limits import Budget
from .results import RunManifest, TaskResult, attempt_id as make_attempt_id, pair_key
from .splits import SplitManifest


@dataclass
class Attempt:
    """One task result, the agent's account of the run, and the episode it ran in (the caller closes it)."""
    result: TaskResult
    episode: object = None
    score: object = None
    agent: AgentResult | None = None


class Budgeted:
    """An episode's tool surface with the attempt's limits on it. A refused call tells the model the run is over, and
    `exhausted` makes the attempt end at that dimension. Under a proxy K the calls the egress proxy let out count
    against the same max_tool_calls (`proxied`)."""

    def __init__(self, episode, budget: Budget):
        self.episode, self.budget = episode, budget
        self.started = time.monotonic()
        self.exhausted = ""
        self.limit = ""
        self.external_calls = 0

    def specs(self):
        return self.episode.specs()

    @property
    def tool_calls(self) -> int:
        return self.episode.tool_calls + self.external_calls

    @property
    def base_tool_calls(self) -> int:
        return self.episode.tool_calls

    def proxied(self, used: int, *, refused: bool = False) -> str:
        self.external_calls = max(int(used), 0)
        if refused or (self.budget.max_tool_calls is not None and self.tool_calls > self.budget.max_tool_calls):
            self.exhausted = self.exhausted or (f"a proxied call refused at max_tool_calls {self.budget.max_tool_calls}"
                                                if refused else f"tool calls {self.tool_calls} > "
                                                                f"{self.budget.max_tool_calls}")
            self.limit = self.limit or "tool_calls"
        return self.exhausted

    def mutates(self, name: str, args: dict) -> bool:
        return self.episode.mutates(name, args)

    def run(self, name: str, args: dict):
        calls, seconds = self.tool_calls + 1, time.monotonic() - self.started
        over = self.budget.exceeded(tool_calls=calls, seconds=seconds)
        if over:
            self.exhausted = self.exhausted or over
            self.limit = self.limit or self.budget.limit_hit(tool_calls=calls, seconds=seconds) or "tool_calls"
            return _Refusal(f"This run's budget is exhausted ({over}). Stop and report what you have done.")
        return self.episode.run(name, args)


@dataclass
class _Refusal:
    output: str
    is_error: bool = True


def task_hash(task) -> str:
    """A task's identity for the memory's leave-one-out: its upstream id and contract."""
    return hashlib.sha256(f"{task.task_id}\0{task.contract}".encode()).hexdigest()[:16]


def make_agent_factory(*, provider, package: Package, core: dict, worker, root: Path, pricing=None,
                       memory_reader=None, opening=None, release_ref: str | None = None, core_ref: str | None = None,
                       arm: str = "subject"):
    """-> make(surface, task, attempt_id, split) -> PackageAgent: the runner shell over one episode, its transcript at
    <root>/transcripts/<attempt id>.jsonl. `opening(task) -> str` is the memory's opening block (none before the
    memory package)."""
    def make(surface, task, attempt_id: str, split: str) -> PackageAgent:
        meta = {"task_id": task.task_id, "split": split, "task_hash": task_hash(task), "release_ref": release_ref,
                "core_ref": core_ref, "arm": arm}
        return PackageAgent(provider=provider, package=package, core=core, surface=surface, worker=worker,
                            transcript=Transcript(session_file(root, attempt_id)), system=task.system,
                            attempt_id=attempt_id, task_meta=meta, memory_reader=memory_reader,
                            opening=opening(task) if opening is not None else "", pricing=pricing)
    return make


def run_attempt(suite, task_id: str, make_agent: Callable, *, run: RunManifest, splits: SplitManifest,
                attempt: int = 0, arm: str = "subject", retry_index: int = 0, plan_ref: str = "",
                release_ref: str | None = None, supersedes: str = "") -> Attempt:
    """One attempt at one task. Every failure that is not the agent's is an invalid attempt with a reason, never a
    zero. A money error ends the attempt as `budget_exhausted` (the comparison it belonged to is inconclusive); a
    provider's account error and a stale worker image stop the run (raised)."""
    family, split = splits.family_of(task_id), splits.split_of(task_id)
    if split != run.split:
        raise ValueError(f"{task_id} is in {split}, but this run reads {run.split}")
    entry, task = splits.entry(task_id), suite.tasks[task_id]
    ids = {"attempt_id": make_attempt_id(run.hash(), task_id, attempt, arm, retry_index),
           "pair_key": pair_key(plan_ref, task_id, attempt) if plan_ref else "",
           "system_release_ref": release_ref, "supersedes": supersedes}
    t0 = time.monotonic()

    def broken(reason: str, failure_class: str, *, episode=None, res: AgentResult | None = None) -> Attempt:
        return Attempt(TaskResult.broken(
            task_id, family, split, attempt, run, splits.hash(), reason[:400], termination="infrastructure",
            contract=task.contract, evidence=ids["attempt_id"], elapsed=round(time.monotonic() - t0, 2),
            tool_calls=getattr(res, "tool_calls", 0), usage=dict(getattr(res, "usage", {}) or {}),
            cost_usd=getattr(res, "cost", None), failure_class=failure_class, **ids), episode, None, res)

    if entry.contract != task.contract:
        return broken(f"contract: the task changed since the split was frozen ({entry.contract[:12]} -> "
                      f"{task.contract[:12]})", "evaluator")
    episode = None
    try:
        episode = suite.episode(task_id)
        surface = Budgeted(episode, run.budget)
        agent = make_agent(surface, task, ids["attempt_id"], split)
    except Exception as e:  # noqa: BLE001 - the episode or the agent could not be built: no evidence either way
        return broken(f"harness: {type(e).__name__}: {e}", "infrastructure", episode=episode)
    try:
        res = agent.run(task.trigger)
    except BudgetError as e:
        return broken(f"budget: {e}", "budget_exhausted", episode=episode)
    except StaleWorker:
        raise
    if res.termination == "infrastructure":
        return broken(f"infrastructure: {res.error}", "infrastructure", episode=episode, res=res)
    try:
        score = episode.score()
    except Exception as e:  # noqa: BLE001 - the official scorer failed: this evaluation stops; K is not ours to fix
        return broken(f"scorer: {type(e).__name__}: {e}", "evaluator", episode=episode, res=res)
    defect = res.termination == "harness_error"
    failure_class = "candidate_defect" if defect else ("" if score.success else "task_outcome")
    return Attempt(TaskResult(task_id, family, split, attempt, run.candidate, run.hash(), splits.hash(),
                              contract=task.contract, success=score.success,
                              partial_credit=round(score.partial_credit, 6), assertions_passed=score.passed,
                              assertions_scored=score.scored, usage=dict(res.usage), cost_usd=res.cost,
                              elapsed=round(time.monotonic() - t0, 2), steps=res.turns,
                              limit=res.limit if res.termination == "budget" else None,
                              tool_calls=surface.tool_calls, termination=res.termination, stop=res.stop,
                              evidence=ids["attempt_id"], failure_class=failure_class,
                              invalid_reason="", **ids),
                   episode, score, res)


_CAP_LOCK = threading.Lock()
_CAP: dict = {"size": None, "semaphore": None}


def in_flight_cap(concurrency: int) -> threading.BoundedSemaphore:
    """The process's one cap on attempts in flight: a gate's arms, the screens of k candidates and a confirmation
    reading all draw on this one semaphore, so two callers in one process cannot between them run twice the
    setting. A different size later (a lower one on a resume) replaces it for the attempts that start afterwards."""
    with _CAP_LOCK:
        size = max(int(concurrency or 1), 1)
        if _CAP["semaphore"] is None or _CAP["size"] != size:
            _CAP["size"], _CAP["semaphore"] = size, threading.BoundedSemaphore(size)
        return _CAP["semaphore"]


def run_tasks(suite, task_ids: list[str], make_agent: Callable, *, run: RunManifest, splits: SplitManifest,
              results=None, repeats: int = 1, on_result: Callable[[TaskResult], None] | None = None,
              arm: str = "subject", plan_ref: str = "", release_ref: str | None = None,
              order: list[tuple[str, int]] | None = None, retry_index: int = 0, supersedes: dict | None = None,
              concurrency: int = 1) -> list[TaskResult]:
    """Every attempt of a run, each recorded as it finishes; the list comes back in the plan's order. Results are
    appended as they come, so an interrupted run keeps the attempts it paid for; nothing here reruns an invalid
    attempt (how many to rerun, under which rule, is the plan's). `concurrency` is a setting of the machine, not of
    the plan: an attempt's limits and reservations are the same at any concurrency."""
    pairs = order if order is not None else [(t, i) for t in task_ids for i in range(repeats)]
    cap = in_flight_cap(concurrency) if int(concurrency or 1) > 1 else None

    def attempt_of(task_id, i) -> Attempt:
        kwargs = dict(run=run, splits=splits, attempt=i, arm=arm, plan_ref=plan_ref, release_ref=release_ref,
                      retry_index=retry_index, supersedes=(supersedes or {}).get((task_id, i), ""))
        if cap is None:
            return run_attempt(suite, task_id, make_agent, **kwargs)
        with cap:
            return run_attempt(suite, task_id, make_agent, **kwargs)

    def record(task_id, i, attempt: Attempt) -> TaskResult:
        try:
            if results is not None:
                results.append(attempt.result)
                if attempt.score is not None:
                    results.put_grading(attempt.result, getattr(attempt.score, "assertions", None))
            if on_result:
                on_result(attempt.result)
        finally:
            _close(attempt)
        return attempt.result

    if cap is None:
        return [record(t, i, attempt_of(t, i)) for t, i in pairs]
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
            # Stopped (an interrupt, an account error from one attempt): nothing not yet running starts; what is
            # running finishes and is recorded, so the book and the results agree on what was paid for.
            for future in list(pending):
                if future.cancel():
                    pending.pop(future)
            if pending:
                _say(f"stopped ({type(stop).__name__}): {len(pending)} attempt(s) still running, each recorded when "
                     f"it finishes; a resume runs the rest")
            for future in list(pending):
                n = pending.pop(future)
                try:
                    done[n] = record(pairs[n][0], pairs[n][1], future.result())
                except Exception as e:  # noqa: BLE001 - the first failure is the one reported
                    _say(f"attempt {pairs[n]} could not be recorded: {type(e).__name__}: {e}")
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    return [done[n] for n in range(len(pairs))]


def _close(attempt: Attempt) -> None:
    if attempt.episode is not None:
        try:
            attempt.episode.close()
        except Exception:  # noqa: BLE001 - process-local state; nothing outside to tear down
            pass


def _say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)
