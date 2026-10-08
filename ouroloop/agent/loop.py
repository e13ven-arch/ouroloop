"""The minimal agent loop: an LLM with read / write / edit / bash, and decision points around it.

    before each LLM call  route: which model tier is enough for this step
    before any tool       the deny list (a rule: never run, whatever any model says)
    before a gated tool   tool_gate (a person approves what is not cleared)
    after a failed bash   retry: rerun automatically on a "yes"; outcome = the rerun's result
    context too long      compact: drop older outputs judged unneeded; outcome = whether they were read again
    after a turn          stop (per_turn): is the task done? a "yes" ends the session, unless the verification fails
    before finishing      stop (on_finish): is the task really done? a failing verification (or a "no") sends the
                          agent back to work, at most max_pushbacks times

Each point's action is `Decision.action`: the model's label where it acts, else the phase's default (a shadow point
always takes it), so nothing here changes what the agent does until a point is switched on and has a threshold.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from ..chat import ChatProvider, ToolCall
from ..ledger import _append_jsonl, new_id, now, redact
from ..runtime import Runtime
from ..train.stop import StopCall, StopGuard
from .deny import denied
from .runner import AgentResult
from .tools import ToolResult, Tools

SYSTEM = ("You are a coding agent working in {workdir}. Use the tools to complete the task: read files before "
          "editing them, keep changes small, and run commands to check your work. When the task is done, reply "
          "with a short summary and no tool calls.")
DROPPED = "[output removed to save context; run the command or read the file again if you need it]"


# An after_tool hook is handed at most this much of a result; the actor itself is handed all of it.
HOOK_RESULT_CHARS = 4000

@dataclass
class AgentConfig:
    max_turns: int = 30
    approve: str = "ask"                          # gated tools that are not cleared: "ask" a person, or "auto"
    gated_tools: tuple[str, ...] = ("bash", "write", "edit")   # the deny list applies to every tool call
    context_chars: int = 60000                    # above this, older outputs become compaction candidates
    keep_recent: int = 4                          # the latest tool outputs are never compacted
    verify_command: str | None = None             # a shell command that exits 0 when the task is done
    max_pushbacks: int = 2                        # how often stop may send the agent back to work
    default_tier: str = "large"                   # the model tier used unless route decides otherwise
    # Which input schema the stop decision reads (contracts.SCHEMAS). "stop.file.v2" is the original coding-task
    # view; "stop.business.v1" carries the whole request and the tool results, which is what a business task's
    # requirements live in (docs/modules/01_POLICY_RUNTIME.md §3).
    stop_view: str = "stop.file.v2"
    max_event_result_chars: int = 2000            # per tool result kept in the observation, before the view cuts


def describe(call: ToolCall) -> str:
    a = call.args
    if call.name == "bash":
        return f"command: {a.get('command', '')}"
    if call.name == "write":
        content = str(a.get("content", ""))
        return f"write {a.get('path')} ({len(content)} characters):\n{content[:800]}"
    if call.name == "edit":
        return f"edit {a.get('path')}\nreplace:\n{str(a.get('old_text', ''))[:400]}\nwith:\n{str(a.get('new_text', ''))[:400]}"
    return json.dumps(a, ensure_ascii=False)[:800]


def _short(call: ToolCall) -> str:
    target = call.args.get("command") or call.args.get("path") or json.dumps(call.args, ensure_ascii=False)
    return f"{call.name} {' '.join(str(target).split())[:120]}"


class Agent:
    def __init__(self, provider: ChatProvider, runtime: Runtime, tools: Tools, *, config: AgentConfig | None = None,
                 approve_fn: Callable[[ToolCall], bool] | None = None,
                 confirm_fn: Callable[[str, str], bool | None] | None = None, confirm_source: str = "human",
                 on_event: Callable[[str, dict], None] | None = None, tiers: dict[str, ChatProvider] | None = None,
                 session: str | None = None, system: str | None = None, probe=None,
                 session_meta: dict | None = None, policy=None, objects=None):
        self.config = config or AgentConfig()
        self.provider = provider
        self.tiers = tiers or {self.config.default_tier: provider}
        if self.config.default_tier not in self.tiers:
            raise ValueError(f"tiers {sorted(self.tiers)} do not include the default tier {self.config.default_tier!r}")
        self.rt = runtime
        self.tools = tools
        self.approve_fn = approve_fn
        self.confirm_fn = confirm_fn        # asked at the end: was the task done? (True / False / None to skip)
        self.confirm_source = confirm_source  # "human" for a person's answer, "env" for checks run at the end
        self.on_event = on_event
        self.probe = probe                  # labels every stop decision from a copy of the working directory
        self.session = session or new_id("s")
        self.session_meta = dict(session_meta or {})   # recorded with the session: task, held-out, agent models
        self.prompt = system or SYSTEM      # before {workdir} is filled in: the same prompt in every directory
        # A tool surface that is not a working directory (the business tools of a benchmark episode) has no
        # workdir, and a prompt written for it has no {workdir} to fill in.
        self.workdir = getattr(tools, "workdir", None)
        self.system = self.prompt.replace("{workdir}", str(self.workdir)) if self.workdir else self.prompt
        self._items: list[dict] = []        # tool outputs that compaction may drop
        self._pending_retry: dict | None = None
        self._steps: list[dict] = []        # route decisions: which tier ran each step
        self._stops: list[dict] = []        # stop decisions and how many changes had been made at the time
        self._actions: list[str] = []
        self._modified: set[str] = set()
        self._changes = 0
        self._errors = 0
        self._turn = 0
        # The task/research profile: `policy` is the bound release (policies.BoundRuntime), whose worker.stop
        # policy answers at stop/on_finish and whose activity counters record what it did. `objects` is where the
        # observation and the trace of each decision are stored, so a later round can say what was visible.
        self.policy = policy
        self.objects = objects
        self._tool_events: list[dict] = []
        self._trace_refs: list[str] = []
        self._observation_refs: list[str] = []
        self.guard = StopGuard(runtime, self.session, checker=self._check if self.config.verify_command else None,
                               max_pushbacks=self.config.max_pushbacks, max_turns=self.config.max_turns,
                               policy=policy.stop if policy is not None else None)
        self._verified: bool | None = None
        self._verified_at = -1              # the change count the last verification saw
        self._task = ""

    # ------------------------------------------------------------------ loop
    def harness(self) -> dict:
        """The harness component of the session's release: prompt, tools and StopGuard settings."""
        return {"system": self.prompt, "tools": {s.name: s.description for s in self.tools.specs()},
                "stop": {"point": self.guard.point, "max_pushbacks": self.guard.max_pushbacks}}

    def run(self, task: str) -> AgentResult:
        try:
            return self._run(task)
        finally:
            ender = getattr(self.tools, "end_session", None)
            if callable(ender):
                ender()                     # a mounted package's worker session ends with the episode

    def _package_hooks(self):
        """The mounted package's hook runner, when the package has hooks (docs/modules/01_POLICY_RUNTIME.md §1)."""
        return getattr(self.tools, "hooks", None) if not isinstance(self.tools, Tools) else None

    def _hook_view(self, hooks, turn: int) -> None:
        """What a hook may read: the request, the step and the tool events so far. No scorer, no later turn."""
        hooks.state = {"task": self._task[:4000], "turn": turn, "events": list(self._tool_events[-20:])}

    def _run(self, task: str) -> AgentResult:
        self._task = task
        self.rt.session(self.session, self.session_meta, harness=self.harness())
        messages: list[dict] = [{"role": "user", "content": task}]
        self._emit("task", text=task)
        text, stop, turn = "", "max_turns", 0
        for turn in range(1, self.config.max_turns + 1):
            self._turn = turn
            self._compact(messages, turn)
            provider, tier = self._route(turn)
            t = provider.chat(self.system, messages, self.tools.specs())
            messages.append({"role": "assistant", "text": t.text, "tool_calls": t.tool_calls,
                             "native": {provider.name: t.native}})
            self._emit("assistant", text=t.text, stop=t.stop, usage=t.usage, tier=tier,
                       tool_calls=[{"id": c.id, "name": c.name, "args": c.args} for c in t.tool_calls])
            text = t.text
            hooks = self._package_hooks()
            if t.stop != "tool_use" or not t.tool_calls:
                if hooks is not None and t.stop in ("end", "other") and turn < self.config.max_turns and \
                        self.guard.pushbacks < self.guard.max_pushbacks:
                    # on_finish of the package: a block or feedback is a bounded pushback, sharing the guard's
                    # limit; the guard and the verifier still decide after it.
                    self._hook_view(hooks, turn)
                    action, notes = hooks.run_hooks("on_finish", {"final_message": text[:4000], "turn": turn})
                    if action != "continue":
                        self.guard.pushbacks += 1
                        reason = " ".join(notes)
                        messages.append({"role": "user", "content": f"{reason}\nKeep working on the task, or "
                                                                    f"explain why it is already done."})
                        self._emit("pushback", reason=reason[:300], source="package")
                        continue
                call = self._stop("on_finish", text, turn) if t.stop in ("end", "other") else None
                if call is not None and not call.stop:
                    messages.append({"role": "user", "content": f"{call.reason}\nKeep working on the task, or "
                                                                f"explain why it is already done."})
                    self._emit("pushback", reason=call.reason[:300])
                    continue
                stop = t.stop
                break
            results = []
            blocked, feedback = None, []
            for call in t.tool_calls:
                if blocked is not None:
                    # A before_tool hook stopped an earlier call of this turn: the rest are not executed, and what
                    # already ran stays as it is — nothing is rolled back.
                    results.append({"id": call.id, "name": call.name, "is_error": True,
                                    "output": f"Not executed: an earlier call in this turn was stopped ({blocked})"})
                    self._emit("tool", id=call.id, name=call.name, is_error=True, output="not executed",
                               skipped=True)
                    continue
                if hooks is not None:
                    self._hook_view(hooks, turn)
                    action, notes = hooks.run_hooks("before_tool", {"tool": call.name, "args": call.args,
                                                                    "turn": turn})
                    if action != "continue":
                        blocked = " ".join(notes)[:2000]
                        results.append({"id": call.id, "name": call.name, "is_error": True,
                                        "output": f"Stopped before execution: {blocked}"})
                        self._emit("tool", id=call.id, name=call.name, is_error=True, output=blocked[:2000],
                                   blocked=True)
                        continue
                runtime = getattr(self.tools, "runtime", None) if not isinstance(self.tools, Tools) else None
                if runtime is not None:
                    self._hook_view(runtime, turn)      # a package tool reads the same view a hook does
                res = self._call(call)
                results.append({"id": call.id, "name": call.name, "output": res.output, "is_error": res.is_error})
                if hooks is not None:
                    self._hook_view(hooks, turn)
                    _, notes = hooks.run_hooks("after_tool", {"tool": call.name, "args": call.args,
                                                              "result": res.output[:HOOK_RESULT_CHARS],
                                                              "is_error": res.is_error, "turn": turn})
                    feedback += notes
                if call.name in ("read", "bash"):
                    self._items.append({"turn": turn, "key": self._key(call), "name": call.name,
                                        "ref": (len(messages), len(results) - 1), "output": res.output,
                                        "dropped": False, "decision": None, "labelled": False})
            messages.append({"role": "tool", "results": results})
            if feedback:
                # after_tool feedback is its own visible message; the tool results above are unchanged.
                messages.append({"role": "user", "content": "\n".join(feedback)[:4000]})
            call = self._stop("per_turn", text, turn)
            if call.stop:
                stop = call.reason
                break
        self._finish(text)
        self._emit("end", stop=stop, turns=turn, verified=self._verified)
        return AgentResult(text, turn, stop, self.session, self._verified)

    def decision_ids(self) -> list[str]:
        """The decisions this session made at the points the loop calls, in the order they were made. The ledger
        holds them; this is the join a task result records without reading it back."""
        return [d["decision"] for d in self._steps] + [s["decision"] for s in self._stops if s["decision"]]

    def _emit(self, kind: str, **data) -> None:
        _append_jsonl(self.rt.root / "transcripts" / f"{self.session}.jsonl",
                      redact({"ts": now(), "kind": kind, **data}))
        if self.on_event:
            self.on_event(kind, data)

    def _key(self, call: ToolCall) -> tuple[str, str]:
        if call.name == "bash":
            return "bash", str(call.args.get("command", ""))
        try:
            return "file", str(self.tools.resolve(str(call.args.get("path", ""))))
        except Exception:  # noqa: BLE001 - an invalid path still needs a key
            return "file", str(call.args.get("path", ""))

    def _task_head(self) -> str:
        return self._task[:300]

    # ------------------------------------------------------------------ route
    def _route(self, turn: int) -> tuple[ChatProvider, str]:
        tier = self.config.default_tier
        if len(self.tiers) < 2 or "route" not in self.rt.points:
            return self.tiers.get(tier, self.provider), tier
        d = self.rt.ask("route", {"task": self._task_head(), "turn": turn,
                                  "last": self._actions[-1] if self._actions else "", "errors": self._errors},
                        session=self.session)
        if d.action in self.tiers:
            tier = d.action
        self._steps.append({"decision": d.id, "tier": tier})
        return self.tiers[tier], tier

    # ------------------------------------------------------------------ stop
    def _stop(self, phase: str, text: str, turn: int) -> StopCall:
        ctx = self._stop_ctx(phase, text, turn)
        rows: list = []
        call = self.guard(phase, ctx, turn, on_record=rows.append)
        if call.decision is not None and self.policy is not None:
            self._trace(call, ctx, phase, rows[-1] if rows else {})
        if call.decision is not None:
            if self.probe is not None:      # the probe labels it after the session, from the state it saw
                self.probe.snap(call.decision, self._changes)
            elif call.checked is not None:
                self.rt.outcome(call.decision, "yes" if call.checked else "no", note="verification command")
            self._stops.append({"decision": call.decision, "changes": self._changes,
                                "labelled": self.probe is not None or call.checked is not None})
        return call

    def _stop_ctx(self, phase: str, text: str, turn: int) -> dict:
        """What the stop decision may see. Nothing derived from a scorer, a reference answer or a later turn."""
        if self.config.stop_view != "stop.business.v1":
            return {"task": self._task_head(), "turns": turn,
                    "files": ", ".join(sorted(self._modified))[:500],
                    "actions": "\n".join(self._actions[-10:]), "message": text[:1500]}
        cut = [e["call_id"] for e in self._tool_events if e.get("truncated")]
        budget = getattr(self.tools, "budget", None)
        remaining = {"steps": max(self.config.max_turns - turn, 0)}
        if budget is not None:
            remaining["tool_calls"] = max(budget.max_tool_calls - getattr(self.tools, "tool_calls",
                                                                         len(self._tool_events)), 0)
        return {"task_text": self._task, "final_message": text[:4000], "phase": phase, "step": turn,
                "tool_events": list(self._tool_events), "budget_remaining": remaining,
                "truncation": {"tool_results_cut": cut} if cut else {}}

    def _trace(self, call: StopCall, ctx: dict, phase: str, decision: dict) -> None:
        """One observation and one trace per decision of a bound policy.

        The proposed and the executed action come from two different places — the policy's answer and what the
        guard did with it — which is the only way a later round can tell "the policy was overruled" from "the
        policy agreed" (docs/modules/00_CONTRACTS.md §2). `decision` is the row as the runtime wrote it.
        """
        from ..evolve import contracts as ct
        if self.objects is None:
            return
        observation = ct.Observation(
            task_text=ctx.get("task_text", ""), tool_events=tuple(json.dumps(e, sort_keys=True, default=str)
                                                                  for e in ctx.get("tool_events") or ()),
            phase=phase, step=int(ctx.get("step") or 0), final_message=ctx.get("final_message", ""),
            budget_remaining=dict(ctx.get("budget_remaining") or {}),
            truncation=dict(ctx.get("truncation") or {}))
        obs_ref = self.objects.put(observation)
        trace = ct.DecisionTrace(
            decision_id=call.decision or "", observation_ref=obs_ref,
            policy_ref=getattr(self.policy.stop, "ref", ""), input_schema=self.config.stop_view,
            rendered_input_ref=self.objects.put_json("change", {"view": decision.get("view") or ""}),
            proposed_action=decision.get("proposed"), executed_action=decision.get("action"),
            executor=decision.get("executor") or "default",
            fallback_reason=decision.get("fallback_reason"), override_reason=decision.get("override_reason"))
        self._observation_refs.append(obs_ref)
        self._trace_refs.append(self.objects.put(trace))
        target = "worker.stop"
        if not decision.get("policy_ref"):
            return                      # no policy was bound at this phase: an observation, not a call
        self.policy.activity.call(target)
        self.policy.activity.propose(target, decision.get("proposed"))
        self.policy.activity.execute(target, decision.get("action"))
        if decision.get("fallback_reason"):
            self.policy.activity.fallback(target, str(decision["fallback_reason"])[:80])
        if decision.get("override_reason") or any(o["decision"] == call.decision for o in self.guard.overruled):
            self.policy.activity.override(target, str(decision.get("override_reason") or "guard limit"))

    def _check(self) -> tuple[bool, str]:
        verified, out = self._verify()
        return bool(verified), f"The verification command `{self.config.verify_command}` failed:\n{out}"

    def _verify(self) -> tuple[bool | None, str]:
        if not self.config.verify_command:
            return None, ""
        res = self.tools.run("bash", {"command": self.config.verify_command})
        self._verified, self._verified_at = res.meta.get("exit_code") == 0, self._changes
        self._emit("verify", passed=self._verified, output=res.output[-2000:])
        return self._verified, res.output[-1500:]

    # ------------------------------------------------------------------ tools
    def _call(self, call: ToolCall) -> ToolResult:
        self._note_reuse(call)
        rule = denied(str(call.args.get("command", ""))) if call.name == "bash" else None
        if rule:
            self._deny(call, rule)
            self._pending_retry = None
            res = ToolResult(f"This command is on the deny list and was not run ({rule}).", True)
        elif call.name in self.config.gated_tools and not self._allowed(call):
            self._pending_retry = None
            res = ToolResult("The user declined this action.", True)
        else:
            res = self._retry(call, self.tools.run(call.name, call.args))
            if self._mutates(call):
                self._changes += 1
                target = call.args.get("path") or call.args.get("url")
                if target and not res.is_error:
                    self._modified.add(str(target))
        self._errors += res.is_error
        self._actions.append(f"{_short(call)} -> {'error' if res.is_error else 'ok'}")
        limit = self.config.max_event_result_chars
        self._tool_events.append({"call_id": call.id, "name": call.name, "args": call.args,
                                  "result_excerpt": "" if res.is_error else res.output[:limit],
                                  "error": res.output[:limit] if res.is_error else "",
                                  "truncated": len(res.output) > limit, "step": self._turn})
        extra = {"broker_calls": res.meta["broker_calls"]} if (res.meta or {}).get("broker_calls") else {}
        self._emit("tool", id=call.id, name=call.name, is_error=res.is_error, output=res.output[:2000], **extra)
        return res

    def _mutates(self, call: ToolCall) -> bool:
        """Whether the call may have changed the world, as its surface says (Tools: write, edit and bash). The
        change count is what the probe snapshots on, so a surface that never answers would leave every stop
        decision labelled from one state."""
        mutates = getattr(self.tools, "mutates", None)
        return bool(mutates(call.name, call.args)) if mutates else call.name in ("write", "edit", "bash")

    def _gate_ctx(self, call: ToolCall) -> dict:
        return {"task": self._task_head(), "workdir": str(self.workdir or ""), "tool": call.name,
                "detail": describe(call)}

    def _deny(self, call: ToolCall, rule: str) -> None:
        """A rule overrules any model: the call never runs, and tool_gate records the veto (route "rule")."""
        self._emit("denied", id=call.id, rule=rule)
        if "tool_gate" in self.rt.points:
            self.rt.ask("tool_gate", self._gate_ctx(call), session=self.session, veto="no")

    def _allowed(self, call: ToolCall) -> bool:
        d = None
        if "tool_gate" in self.rt.points:
            d = self.rt.ask("tool_gate", self._gate_ctx(call), session=self.session)
            if d.action == "yes":           # cleared: nobody is asked, and nobody's label is recorded
                return True
        return self._ask_person(call, d) if self.config.approve == "ask" else True

    def _ask_person(self, call: ToolCall, d) -> bool:
        if self.approve_fn is None:         # nobody to ask: decline, and record no label nobody gave
            self._emit("approval", id=call.id, approved=False, by="nobody")
            return False
        ok = bool(self.approve_fn(call))
        if d is not None:
            self.rt.outcome(d.id, "yes" if ok else "no", source="human", note="approval")
        self._emit("approval", id=call.id, approved=ok)
        return ok

    def _retry(self, call: ToolCall, res: ToolResult) -> ToolResult:
        pending, self._pending_retry = self._pending_retry, None
        if call.name != "bash":
            return res
        cmd, code = str(call.args.get("command", "")), res.meta.get("exit_code", 0)
        if pending and pending["command"] == cmd:
            # Only an immediate, identical rerun counts: anything in between may have changed the outcome.
            self.rt.outcome(pending["decision"], "yes" if code == 0 else "no", note="rerun by the agent")
            return res
        if code == 0 or "retry" not in self.rt.points:
            return res
        d = self.rt.ask("retry", {"command": cmd, "stderr": res.meta.get("stderr") or res.meta.get("stdout", ""),
                                  "exit_code": code}, session=self.session)
        if d.action == "yes":
            again = self.tools.run("bash", call.args)
            ok = again.meta.get("exit_code", 1) == 0
            self.rt.outcome(d.id, "yes" if ok else "no", note="automatic rerun")
            self._emit("retry", id=call.id, succeeded=ok)
            return ToolResult(f"{res.output}\n[ouroloop reran the command]\n{again.output}", again.is_error, again.meta)
        self._pending_retry = {"command": cmd, "decision": d.id}
        return res

    # ------------------------------------------------------------------ compaction
    def _note_reuse(self, call: ToolCall) -> None:
        if call.name not in ("read", "bash"):
            return
        key = self._key(call)
        for it in self._items:
            if it["dropped"] and not it["labelled"] and it["key"] == key and it["decision"]:
                self.rt.outcome(it["decision"], "yes", note="read again after it was dropped")
                it["labelled"] = True

    def _compact(self, messages: list[dict], turn: int) -> None:
        live = [it for it in self._items if not it["dropped"]]
        if "compact" not in self.rt.points or sum(len(it["output"]) for it in live) <= self.config.context_chars:
            return
        for it in live[:-self.config.keep_recent] if self.config.keep_recent else live:
            if it["decision"] is not None:
                continue
            d = self.rt.ask("compact", {"task": self._task_head(), "tool": it["name"], "target": it["key"][1],
                                        "age": turn - it["turn"], "output": it["output"]}, session=self.session)
            it["decision"] = d.id
            if d.action == "no" and all(p.supports_history_edits for p in self.tiers.values()):
                mi, ri = it["ref"]
                messages[mi]["results"][ri]["output"] = DROPPED
                it["dropped"] = True
                self._emit("compact", target=it["key"][1])

    # ------------------------------------------------------------------ end of the task
    def _finish(self, text: str) -> None:
        for it in self._items:
            if it["dropped"] and not it["labelled"] and it["decision"]:
                self.rt.outcome(it["decision"], "no", note="not needed again before the session ended")
                it["labelled"] = True
        if self.config.verify_command and self._verified_at != self._changes:
            self._verify()                  # ran out of turns, or changed files after the last check
        done = self._verified
        if self.confirm_fn is not None:
            answer = self.confirm_fn(self._task, text)
            if answer is not None:
                # Without a probe: a stop decision made after the last change saw the final state.
                for s in self._stops:
                    if not s["labelled"] and s["changes"] == self._changes:
                        self.rt.outcome(s["decision"], "yes" if answer else "no", source=self.confirm_source,
                                        note="task outcome at the end")
                        s["labelled"] = True
                done = bool(answer) if done is None else done
        if done is not None:
            # Weak credit: a finished task says the small tier was enough for the steps it took, a failed one not.
            for step in self._steps:
                if step["tier"] == "small":
                    self.rt.outcome(step["decision"], "small" if done else "large", weight=0.5,
                                    note="task outcome after a small-tier step")
