"""A host that asks the stop point, for the tests of what the training stack builds from stop decisions: feedback,
datasets, a decision's trace, and StopGuard under a bound worker.stop policy.

The runner shell asks no decision point (docs/modules/01_POLICY_RUNTIME.md §1), and the v1 loop that did was removed
in P1 step 5, so no attempt writes a stop decision any more: the bridge from an attempt to a stop point is v2 work
(docs/design/V2_TRAINING_PIPELINE.md §3.8). `play` stands in for a host that has one. A scripted worker works one
episode's tools; StopGuard decides after every turn that called a tool (per_turn) and at every finish request
(on_finish), over the context train/trace.py builds; a bound release's decisions get their observation and trace;
and the completion probe labels each decision from the state it was made on. That is what the v1 loop's business path
did around its stop point, and nothing else of it.
"""
from __future__ import annotations

from ouroloop.adapters.automationbench import CompletionProbe
from ouroloop.benchmark import Budget
from ouroloop.evolve.task_eval import Budgeted
from ouroloop.ledger import new_id
from ouroloop.train.runtime import Runtime
from ouroloop.train.stop import StopGuard
from ouroloop.train.trace import record_trace, stop_context, tool_event
from ouroloop.train.views import business_stop_point

PUSHBACK = "{reason}\nKeep working on the task, or explain why it is already done."
BUDGET = Budget(max_steps=8, max_tool_calls=12)


def stop_runtime(root, **kw) -> Runtime:
    """The general runtime over a workspace root, with the business stop point registered under its name, "stop", and
    in-process serving (a trained artifact's load probe asks a model through it)."""
    from ouroloop.train.serving import Serving
    kw.setdefault("serving", Serving())
    rt = Runtime(root, seed=0, **kw)
    rt.register(business_stop_point())
    return rt


def play(rt, suite, splits, task_id: str, worker, *, bound=None, objects=None, probe: bool = True,
         budget: Budget = BUDGET, max_pushbacks: int = 2, attempt: int = 0) -> dict:
    """One episode of `task_id` played by `worker`, with the stop decisions a host that asks the stop point records.
    `bound` (policies.BoundRuntime): the release whose prompt the worker is given and whose worker.stop policy
    StopGuard asks; with `objects`, each decision's observation and trace are stored there.

    -> {"session", "decisions", "observations", "traces", "pushbacks", "turns", "labelled", "calls"}."""
    family, split = splits.family_of(task_id), splits.split_of(task_id)
    if probe and split != "train":
        raise ValueError(f"{task_id} is in {split}: the completion probe writes training labels, and only the train "
                         f"split may produce them")
    task = suite.tasks[task_id]
    session = new_id("s")
    rt.ledger.append({"type": "session", "session": session, "task": task_id, "family": family, "split": split,
                      "held_out": split != "train", "attempt": attempt})
    system = bound.system_prompt(task.system)[0] if bound is not None else task.system
    guard = StopGuard(rt, session, max_pushbacks=max_pushbacks, max_turns=budget.max_steps,
                      policy=bound.stop if bound is not None else None)
    episode = suite.episode(task_id)
    surface = Budgeted(episode, budget)
    labeller = CompletionProbe(rt, episode, surface=surface) if probe else None
    out = {"session": session, "decisions": [], "observations": [], "traces": [], "pushbacks": 0, "calls": []}
    events: list = []
    changes = [0]

    def stop(phase: str, text: str, turn: int):
        remaining = {"steps": max(budget.max_steps - turn, 0),
                     "tool_calls": max(budget.max_tool_calls - surface.tool_calls, 0)}
        ctx = stop_context(task.trigger, text, phase=phase, step=turn, tool_events=events, budget_remaining=remaining)
        rows: list = []
        call = guard(phase, ctx, turn, on_record=rows.append)
        if call.decision is not None:
            out["decisions"].append(call.decision)
            if bound is not None and objects is not None:
                obs, trace = record_trace(objects, bound, guard, call, ctx, phase, rows[-1] if rows else {})
                out["observations"].append(obs)
                out["traces"].append(trace)
            if labeller is not None:
                labeller.snap(call.decision, changes[0])
        return call

    messages: list = [{"role": "user", "content": task.trigger}]
    turn = 0
    try:
        for turn in range(1, budget.max_steps + 1):
            t = worker.chat(system, messages, surface.specs())
            messages.append({"role": "assistant", "text": t.text, "tool_calls": t.tool_calls})
            if t.stop != "tool_use" or not t.tool_calls:
                call = stop("on_finish", t.text, turn) if t.stop in ("end", "other") else None
                if call is not None and not call.stop:
                    out["pushbacks"] += 1
                    messages.append({"role": "user", "content": PUSHBACK.format(reason=call.reason)})
                    continue
                break
            results = []
            for c in t.tool_calls:
                res = surface.run(c.name, c.args)
                if surface.mutates(c.name, c.args):
                    changes[0] += 1
                out["calls"].append((c.name, dict(c.args)))
                events.append(tool_event(c.id, c.name, c.args, res.output, res.is_error, turn))
                results.append({"id": c.id, "name": c.name, "output": res.output, "is_error": res.is_error})
            messages.append({"role": "tool", "results": results})
            if stop("per_turn", t.text, turn).stop:
                break
        out["turns"] = turn
        out["labelled"] = labeller.label() if labeller is not None else 0
    finally:
        episode.close()
    return out
