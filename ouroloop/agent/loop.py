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
from ..points import denied
from ..runtime import Runtime
from ..stop import StopCall, StopGuard
from .tools import ToolResult, Tools

SYSTEM = ("You are a coding agent working in {workdir}. Use the tools to complete the task: read files before "
          "editing them, keep changes small, and run commands to check your work. When the task is done, reply "
          "with a short summary and no tool calls.")
DROPPED = "[output removed to save context; run the command or read the file again if you need it]"


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


@dataclass
class AgentResult:
    text: str
    turns: int
    stop: str
    session: str
    verified: bool | None = None


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
                 session_meta: dict | None = None):
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
        self.system = self.prompt.replace("{workdir}", str(tools.workdir))
        self._items: list[dict] = []        # tool outputs that compaction may drop
        self._pending_retry: dict | None = None
        self._steps: list[dict] = []        # route decisions: which tier ran each step
        self._stops: list[dict] = []        # stop decisions and how many changes had been made at the time
        self._actions: list[str] = []
        self._modified: set[str] = set()
        self._changes = 0
        self._errors = 0
        self.guard = StopGuard(runtime, self.session, checker=self._check if self.config.verify_command else None,
                               max_pushbacks=self.config.max_pushbacks, max_turns=self.config.max_turns)
        self._verified: bool | None = None
        self._verified_at = -1              # the change count the last verification saw
        self._task = ""

    # ------------------------------------------------------------------ loop
    def harness(self) -> dict:
        """The harness component of the session's release: prompt, tools and StopGuard settings."""
        return {"system": self.prompt, "tools": {s.name: s.description for s in self.tools.specs()},
                "stop": {"point": self.guard.point, "max_pushbacks": self.guard.max_pushbacks}}

    def run(self, task: str) -> AgentResult:
        self._task = task
        self.rt.session(self.session, self.session_meta, harness=self.harness())
        messages: list[dict] = [{"role": "user", "content": task}]
        self._emit("task", text=task)
        text, stop, turn = "", "max_turns", 0
        for turn in range(1, self.config.max_turns + 1):
            self._compact(messages, turn)
            provider, tier = self._route(turn)
            t = provider.chat(self.system, messages, self.tools.specs())
            messages.append({"role": "assistant", "text": t.text, "tool_calls": t.tool_calls,
                             "native": {provider.name: t.native}})
            self._emit("assistant", text=t.text, stop=t.stop, usage=t.usage, tier=tier,
                       tool_calls=[{"id": c.id, "name": c.name, "args": c.args} for c in t.tool_calls])
            text = t.text
            if t.stop != "tool_use" or not t.tool_calls:
                call = self._stop("on_finish", text, turn) if t.stop in ("end", "other") else None
                if call is not None and not call.stop:
                    messages.append({"role": "user", "content": f"{call.reason}\nKeep working on the task, or "
                                                                f"explain why it is already done."})
                    self._emit("pushback", reason=call.reason[:300])
                    continue
                stop = t.stop
                break
            results = []
            for call in t.tool_calls:
                res = self._call(call)
                results.append({"id": call.id, "name": call.name, "output": res.output, "is_error": res.is_error})
                if call.name in ("read", "bash"):
                    self._items.append({"turn": turn, "key": self._key(call), "name": call.name,
                                        "ref": (len(messages), len(results) - 1), "output": res.output,
                                        "dropped": False, "decision": None, "labelled": False})
            messages.append({"role": "tool", "results": results})
            call = self._stop("per_turn", text, turn)
            if call.stop:
                stop = call.reason
                break
        self._finish(text)
        self._emit("end", stop=stop, turns=turn, verified=self._verified)
        return AgentResult(text, turn, stop, self.session, self._verified)

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
        call = self.guard(phase, {"task": self._task_head(), "turns": turn,
                                  "files": ", ".join(sorted(self._modified))[:500],
                                  "actions": "\n".join(self._actions[-10:]), "message": text[:1500]}, turn)
        if call.decision is not None:
            if self.probe is not None:      # the probe labels it after the session, from the state it saw
                self.probe.snap(call.decision, self._changes)
            elif call.checked is not None:
                self.rt.outcome(call.decision, "yes" if call.checked else "no", note="verification command")
            self._stops.append({"decision": call.decision, "changes": self._changes,
                                "labelled": self.probe is not None or call.checked is not None})
        return call

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
            if call.name in ("write", "edit", "bash"):
                self._changes += 1                  # bash may change files too
                if call.name != "bash" and not res.is_error:
                    self._modified.add(str(call.args.get("path", "")))
        self._errors += res.is_error
        self._actions.append(f"{_short(call)} -> {'error' if res.is_error else 'ok'}")
        self._emit("tool", id=call.id, name=call.name, is_error=res.is_error, output=res.output[:2000])
        return res

    def _gate_ctx(self, call: ToolCall) -> dict:
        return {"task": self._task_head(), "workdir": str(self.tools.workdir), "tool": call.name,
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
