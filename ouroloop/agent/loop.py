"""The minimal agent loop (PLAN §3): an LLM with read / write / edit / bash, and decision points around it.

    before each LLM call  route: which model tier is enough for this step
    before a gated tool   deny list, then tool_gate (a person approves what the model does not clear)
    after a failed bash   retry: rerun automatically when the model is confident; outcome = the rerun's result
    context too long      compact: drop older outputs judged unneeded; outcome = whether they were read again
    before finishing      stop: is the task really done? a failing verification (or a confident "no") sends the
                          agent back to work, at most max_pushbacks times
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Callable

from ..chat import ChatProvider, ToolCall
from ..ledger import _append_jsonl, new_id, now, redact
from ..points import denied
from ..runtime import Runtime
from .tools import ToolResult, Tools

SYSTEM = ("You are a coding agent working in {workdir}. Use the tools to complete the task: read files before "
          "editing them, keep changes small, and run commands to check your work. When the task is done, reply "
          "with a short summary and no tool calls.")
DROPPED = "[output removed to save context; run the command or read the file again if you need it]"


@dataclass
class AgentConfig:
    max_turns: int = 30
    approve: str = "ask"                          # gated tools the model does not clear: "ask" a person, or "auto"
    gated_tools: tuple[str, ...] = ("bash", "write", "edit")
    context_chars: int = 60000                    # above this, older outputs become compaction candidates
    keep_recent: int = 4                          # the latest tool outputs are never compacted
    compact_explore: float = 0.1                  # shadow mode: drop this share anyway, to learn from re-reads
    verify_command: str | None = None             # a shell command that exits 0 when the task is done
    max_pushbacks: int = 2                        # how often stop may send the agent back to work
    default_tier: str = "large"                   # the model tier used unless route decides otherwise
    route_explore: float = 0.1                    # shadow mode: share of steps tried on the small tier


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
                 session: str | None = None, system: str | None = None, seed: int | None = None):
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
        self.session = session or new_id("s")
        self.system = (system or SYSTEM).replace("{workdir}", str(tools.workdir))
        self.rng = random.Random(seed)
        self._items: list[dict] = []        # tool outputs that compaction may drop
        self._pending_retry: dict | None = None
        self._steps: list[dict] = []        # route decisions: which tier ran each step
        self._stops: list[dict] = []        # stop decisions and how many changes had been made at the time
        self._actions: list[str] = []
        self._modified: set[str] = set()
        self._changes = 0
        self._errors = 0
        self._pushbacks = 0
        self._verified: bool | None = None
        self._verified_at = -1              # the change count the last verification saw
        self._task = ""

    # ------------------------------------------------------------------ loop
    def run(self, task: str) -> AgentResult:
        self._task = task
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
                if t.stop in ("end", "other") and self._pushback(messages, text, turn):
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
        explored = False
        if d.route == "auto" and d.action in self.tiers:
            tier = d.action
        elif d.route == "shadow" and "small" in self.tiers and self.rng.random() < self.config.route_explore:
            tier, explored = "small", True    # try the cheap tier on some steps so it has outcomes to learn from
        self._steps.append({"decision": d.id, "tier": tier, "explored": explored})
        return self.tiers[tier], tier

    # ------------------------------------------------------------------ stop
    def _pushback(self, messages: list[dict], text: str, turn: int) -> bool:
        """At a would-be end: verify, ask the stop point, and send the agent back to work if the task is not done."""
        verified, verify_out = self._verify()
        d = None
        if "stop" in self.rt.points:
            verification = ("none" if verified is None else "passed" if verified
                            else f"failed:\n{verify_out[-300:]}")
            d = self.rt.ask("stop", {"task": self._task_head(), "turns": turn,
                                     "files": ", ".join(sorted(self._modified))[:500],
                                     "actions": "\n".join(self._actions[-10:]), "verification": verification,
                                     "message": text[:1500]}, session=self.session)
            record = {"decision": d.id, "changes": self._changes, "labelled": verified is not None}
            self._stops.append(record)
            if verified is not None:
                self.rt.outcome(d.id, "yes" if verified else "no", note="verification command")
        if verified is False:
            reason = f"The verification command `{self.config.verify_command}` failed:\n{verify_out}"
        elif verified is None and d is not None and d.route == "auto" and d.action == "no":
            reason = "The task does not look complete yet."    # a passing verification is never overruled
        else:
            return False
        if self._pushbacks >= self.config.max_pushbacks or turn >= self.config.max_turns:
            return False
        self._pushbacks += 1
        messages.append({"role": "user", "content": f"{reason}\nKeep working on the task, or explain why it is "
                                                    f"already done."})
        self._emit("pushback", reason=reason[:300])
        return True

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
        if call.name in self.config.gated_tools and not self._allowed(call):
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

    def _allowed(self, call: ToolCall) -> bool:
        if call.name == "bash":
            rule = denied(str(call.args.get("command", "")))
            if rule:
                self._emit("denied", id=call.id, rule=rule)
                return False
        d = None
        if "tool_gate" in self.rt.points:
            d = self.rt.ask("tool_gate", {"task": self._task_head(), "workdir": str(self.tools.workdir),
                                          "tool": call.name, "detail": describe(call)}, session=self.session)
            if d.route == "auto":
                return True if d.action == "yes" else self._ask_person(call, d)
            if d.route == "fallback":
                return self._ask_person(call, d)
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
        if d.route == "auto" and d.action == "yes":
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
            drop = ((d.route == "auto" and d.action == "no") or d.explored
                    or (d.route == "shadow" and self.rng.random() < self.config.compact_explore))
            if drop and all(p.supports_history_edits for p in self.tiers.values()):
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
                for s in self._stops:   # a stop decision made after the last change saw the final state
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
                                    note="task outcome after a small-tier step", explored=step["explored"])
