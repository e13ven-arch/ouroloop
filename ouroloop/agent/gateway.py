"""M1 §1 宿主作为运行壳: the host side of one attempt — the gateway behind `ctx.model`, the broker behind `ctx.tool`, the
reader behind `ctx.skills`, the marks behind `ctx.mark`, and the in-process ctx the default agent runs over when a
package has no code (docs/modules/01_POLICY_RUNTIME.md §1, docs/modules/00_CONTRACTS.md §2).

Every model call of an attempt goes through Gateway.model: the role is checked against the manifest, the effort is
capped at K's `max_effort` (the requested value is recorded, never refused), the act and model-call limits are checked
before the provider is called, the first `system` message becomes the provider's system argument, and the call goes
through the provider the host gave — a MeteredProvider in the bench, so reserving and settling are what they were.
Afterwards the host writes two records into the attempt's transcript: today's `assistant` event, with the role, the
effort, the requested effort and a host-generated call id added, and one `gateway_call` line (M0 §2 gateway_call.v1)
holding the attempt, the model identity, what the agent sent (as a prefix count against the previous call plus the
tail; the tools by name and schema digest, their full specs once per attempt as a `tool_specs` line when first seen
or changed) and what came back. The output tokens are summed against `max_tokens`. Every tool call goes through
AttemptBroker.call: the deny-list veto, the base-tool and read-only checks of worker.Broker, the episode's own
permission and budget checks (the Budgeted surface), then a `tool` event and a `broker_event` line. Nothing here is
reachable from package code except through the five names.

What the agent sends is checked for shape before anything else reads it (malformed): a malformed call is answered
as an error the agent may catch and recorded as a protocol event, never raised on the host — the worker's
base_call_handler already answers a malformed tool call this way, and a model call is the same case.

The limits are the host's: a call past one raises LimitHit with the dimension, the attempt ends as termination
`budget`, and the package cannot catch it — in the worker the session is closed under it; in-process it crosses the
agent's code as HostFault, a BaseException.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import time
import uuid

from ..chat import ToolCall, ToolSpec
from ..evolve.execution import normalize_usage
from .deny import denied
from .worker import Broker, LimitHit

# The reasoning-effort tiers, lowest first (the `claude_code` provider's names). K's `max_effort` is the ceiling the
# gateway caps a package's request at (M0 §2); W0 pins its own default below it.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MAX_EFFORT = "high"
TIER = "large"                        # the route point is gone (M1 §1): one tier, under the name today's events use
EVENT_CHARS = 2000                    # of a tool output, in the `tool` event and the view's result excerpt
VIEW_EVENTS = 20                      # the broker events a hook or package tool may see in ctx.view["events"]
# What a package may report about its own loop, labelled: a pushback, a hook's answer, a package tool's run, and a
# model call the agent stopped before execution (tool_blocked) or never reached (tool_skipped) — the broker saw no
# call for those, so without the mark the record would show nothing where the model saw a result.
# v0.5 (docs/design/V0_5_PLAN.md §1.3): `method_decision`, the router's decision at a method's first firing — the
# method, its trigger kind, the cell, the action, the variant and the exact propensity — which the learner and the
# frozen-table receipt read. Only a package with the method kit marks it.
MARK_KINDS = ("pushback", "hook", "package_tool", "tool_blocked", "tool_skipped", "method_decision")
RESERVED_KEYS = ("kind", "ts", "source", "mark")     # the record's own fields: package data may not set them


class ModelCallError(RuntimeError):
    """The gateway refused a ctx.model call (an undeclared role, an unknown effort name). Raised in the agent's
    hands in-process, as the worker's ModelCallError is; the agent may catch it. A hard limit is never this."""


class HostFault(BaseException):
    """The host's own failure (a provider error, a hard limit) crossing the agent's code in-process. A BaseException,
    so `except Exception` in the agent cannot swallow it: in the worker the agent never sees a host failure either,
    the session is closed under it. The runner shell unwraps `error`."""

    def __init__(self, error: BaseException):
        super().__init__(str(error))
        self.error = error


# ---------------------------------------------------------------------------- effort
def cap_effort(requested: str | None, max_effort: str | None) -> str | None:
    """The effort a call is made at: the request, capped at K's ceiling by the EFFORTS order; None stays None (the
    provider's own, W0's pinned default)."""
    if requested is None or max_effort is None:
        return requested
    return requested if EFFORTS.index(requested) <= EFFORTS.index(max_effort) else max_effort


def takes_effort(provider) -> bool:
    """Whether the provider's `chat` takes an `effort` keyword, looking through meters (a `.provider` whose chat
    passes **kwargs on) to the provider that makes the call. Read once per gateway: ScriptedChat and the test
    workers keep their three-argument chat and are never handed a keyword they do not take."""
    seen: set[int] = set()
    while provider is not None and id(provider) not in seen:
        seen.add(id(provider))
        try:
            params = inspect.signature(provider.chat).parameters
        except (AttributeError, TypeError, ValueError):
            return False
        if "effort" in params:
            return True
        if not any(p.kind is p.VAR_KEYWORD for p in params.values()):
            return False
        provider = getattr(provider, "provider", None)
    return False


def own_effort(provider) -> str | None:
    """The effort the provider itself is pinned at, looking through meters: what a call made with no request runs
    at, recorded as the call's `effort`."""
    seen: set[int] = set()
    while provider is not None and id(provider) not in seen:
        seen.add(id(provider))
        effort = getattr(provider, "effort", None)
        if isinstance(effort, str) and effort:
            return effort
        provider = getattr(provider, "provider", None)
    return None


# ---------------------------------------------------------------------------- the shapes the agent sends
def malformed(messages, tools) -> str | None:
    """Why a ctx.model call cannot be made, or None. Messages are a list of objects with a string role; an assistant
    entry's tool_calls are objects with a string name and an object of args; a tool entry carries a list of results;
    tools are objects with a string name and, when given, an object schema (or ToolSpec instances in-process). The
    gateway checks this before the provider or the record sees the call, so a bad shape is the agent's error to catch
    and never the host's to raise."""
    if not isinstance(messages, (list, tuple)):
        return f"messages is a list, not {type(messages).__name__}"
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            return f"messages[{i}] is an object, not {type(m).__name__}"
        if not isinstance(m.get("role"), str):
            return f"messages[{i}] has no string role"
        if m["role"] == "assistant":
            calls = m.get("tool_calls")
            if calls is not None and not isinstance(calls, list):
                return f"messages[{i}].tool_calls is a list, not {type(calls).__name__}"
            for j, c in enumerate(calls or ()):
                if not isinstance(c, dict) or not isinstance(c.get("name"), str):
                    return f"messages[{i}].tool_calls[{j}] is an object with a string name"
                if c.get("args") is not None and not isinstance(c["args"], dict):
                    return f"messages[{i}].tool_calls[{j}].args is an object, not {type(c['args']).__name__}"
        if m["role"] == "tool":
            results = m.get("results")
            if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
                return f"messages[{i}] (role tool) carries a list of result objects"
    if not isinstance(tools, (list, tuple)):
        return f"tools is a list, not {type(tools).__name__}"
    for i, t in enumerate(tools):
        if isinstance(t, ToolSpec):
            continue
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t["name"]:
            return f"tools[{i}] is an object with a string name, not {type(t).__name__}"
        schema = t.get("schema", t.get("input_schema"))
        if schema is not None and not isinstance(schema, dict):
            return f"tools[{i}] ({t['name']}) has a schema that is not an object"
    return None


# ---------------------------------------------------------------------------- the history as the provider takes it
def tool_spec(tool) -> ToolSpec:
    """A tool as the agent listed it ({name, description, schema|input_schema}, or a ToolSpec) for the provider."""
    if isinstance(tool, ToolSpec):
        return tool
    schema = tool.get("schema", tool.get("input_schema"))
    return ToolSpec(str(tool.get("name", "")), str(tool.get("description") or ""),
                    dict(schema) if isinstance(schema, dict) else {"type": "object"})


def split_system(messages: list) -> tuple[str, list[dict]]:
    """(system, history): the first `role: system` message is the provider's system argument (the gateway injects
    none and rewrites none, M1 §1); the rest is the history in chat.py's shape, an assistant entry's tool calls as
    ToolCall objects, tool and user entries as they came."""
    system, history = "", []
    for m in messages:
        if not isinstance(m, dict):
            raise TypeError(f"a message is an object, not {type(m).__name__}")
        role = m.get("role")
        if role == "system" and not system and not history:
            system = str(m.get("content") or "")
            continue
        if role == "assistant":
            calls = [ToolCall(str(c.get("id") or ""), str(c.get("name") or ""), dict(c.get("args") or {}))
                     for c in m.get("tool_calls") or () if isinstance(c, dict)]
            history.append({"role": "assistant", "text": str(m.get("text") or ""), "tool_calls": calls})
            continue
        history.append(dict(m))
    return system, history


def _snapshot(value):
    """Protocol data: what the agent sent, as JSON values (the worker's messages already are; the in-process agent's
    lists are its own and go on changing, so the record keeps a copy)."""
    return json.loads(json.dumps(value, default=str))


def common_prefix(previous: list, current: list) -> int:
    n = 0
    for a, b in zip(previous, current):
        if a != b:
            break
        n += 1
    return n


def _left(cap, used) -> int | None:
    return None if cap is None else max(int(cap) - int(used), 0)


# ---------------------------------------------------------------------------- the gateway
class Gateway:
    """ctx.model on the host. `provider` is what the host calls (the pinned actor, metered in the bench); `roles` the
    manifest's declared roles; `max_effort` K's ceiling; `budget` the attempt's Budget; `record(kind, data)` the
    attempt's transcript channel; `call_prefix` goes before every host-generated call id; `surface` the Budgeted
    tool surface, read for the remaining tool calls and the attempt's clock; `attempt_id` and `model_identity_ref`
    go on every gateway_call line (M0 §2), None where the host runs no attempt (a unit test)."""

    def __init__(self, provider, *, roles, max_effort, budget, record, call_prefix: str = "", surface=None,
                 attempt_id: str | None = None, model_identity_ref: str | None = None):
        self.provider = provider
        self.roles = tuple(roles or ())
        self.max_effort = max_effort
        self.budget = budget
        self.record = record
        self.call_prefix = call_prefix
        self.surface = surface
        self.attempt_id, self.model_identity_ref = attempt_id, model_identity_ref
        self.started = getattr(surface, "started", None) or time.monotonic()
        self._effort_kw = takes_effort(provider)
        self._own_effort = own_effort(provider)
        self.seq = 0
        self.act_calls = 0
        self.model_calls = 0
        self.tokens = 0                       # output tokens of every call so far, thinking included
        self.by_role: dict[str, dict] = {}    # role -> {calls, tokens} (M1 §4)
        self.last_call_id: str | None = None
        self.last_act_stop: str | None = None
        self._previous: list = []             # the previous call's messages, for the prefix count
        self._specs_digest: str | None = None  # the tool specs on record, so they are written once per change

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def limits(self) -> dict:
        """The remaining amount of each hard limit (M0 §2 ctx.limits); None where the frozen plan set none."""
        b = self.budget
        tool_calls = None
        if b.max_tool_calls is not None and self.surface is not None and getattr(self.surface, "tool_calls", None) is not None:
            tool_calls = _left(b.max_tool_calls, self.surface.tool_calls)
        return {"act_calls": _left(b.max_steps, self.act_calls),
                "model_calls": _left(b.max_model_calls, self.model_calls),
                "tool_calls": tool_calls,
                "seconds": None if b.max_seconds is None else max(int(b.max_seconds - self.elapsed()), 0),
                "tokens": _left(b.max_tokens, self.tokens)}

    def _refuse(self, error: str, role, effort) -> dict:
        """An undeclared role or an unknown effort: answered as an error the agent may catch, recorded as a protocol
        event (the draft smoke should have found it, M0 §3), the model never called."""
        self.record("protocol", {"error": error, "role": role, "requested_effort": effort})
        return {"is_error": True, "error": error, "limits": self.limits()}

    def _tool_specs(self, specs: list[ToolSpec]) -> list[dict]:
        """The call's tools for the gateway_call line: name and schema digest. The full specs go on their own
        `tool_specs` line the first time this list is seen and whenever it changes, so the log stays readable
        without a schema on every call and nothing the model was offered is lost."""
        full = [{"name": s.name, "description": s.description, "schema": s.schema} for s in specs]
        digest = hashlib.sha256(json.dumps(full, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        if digest != self._specs_digest:
            self._specs_digest = digest
            self.record("tool_specs", {"attempt_id": self.attempt_id, "digest": digest, "specs": full})
        return [{"name": s.name,
                 "schema_digest": hashlib.sha256(json.dumps(s.schema, sort_keys=True, default=str)
                                                 .encode("utf-8")).hexdigest()[:16]} for s in specs]

    def model(self, messages, tools=(), role: str = "act", effort: str | None = None) -> dict:
        """One model call. -> {text, tool_calls: [{id, name, args}], stop, limits}, or {is_error, error, limits}.
        Raises LimitHit before the call at `max_steps` (act calls) or `max_model_calls`, and after it at
        `max_tokens`; the call that crossed the token limit is recorded and booked, its response is not handed on."""
        problem = malformed(messages, tools)
        if problem:
            return self._refuse(f"malformed model call: {problem}", role, effort)
        messages, tools = list(messages), list(tools)
        if role not in self.roles:
            return self._refuse(f"role {role!r} is not declared in the manifest's roles {list(self.roles)}", role, effort)
        if effort is not None and effort not in EFFORTS:
            return self._refuse(f"effort {effort!r} is not one of {list(EFFORTS)}", role, effort)
        used = cap_effort(effort, self.max_effort)
        b = self.budget
        if role == "act" and b.max_steps is not None and self.act_calls + 1 > b.max_steps:
            raise LimitHit("act_calls")
        if b.max_model_calls is not None and self.model_calls + 1 > b.max_model_calls:
            raise LimitHit("model_calls")
        if b.max_seconds is not None and self.elapsed() > b.max_seconds:
            raise LimitHit("seconds")
        system, history = split_system(messages)
        specs = [tool_spec(t) for t in tools]
        kwargs = {"effort": used} if used is not None and self._effort_kw else {}
        t0 = time.monotonic()
        turn = self.provider.chat(system, history, specs, **kwargs)
        elapsed = round(time.monotonic() - t0, 3)
        self.seq += 1
        self.model_calls += 1
        if role == "act":
            self.act_calls += 1
            self.last_act_stop = turn.stop
        usage = dict(turn.usage or {})
        out_tokens = normalize_usage(usage)["output_tokens"] if usage else 0
        self.tokens += out_tokens
        tally = self.by_role.setdefault(role, {"calls": 0, "tokens": 0})
        tally["calls"] += 1
        tally["tokens"] += out_tokens
        call_id = f"{self.call_prefix}{uuid.uuid4().hex}"
        self.last_call_id = call_id
        calls = [{"id": c.id, "name": c.name, "args": dict(c.args or {})} for c in turn.tool_calls]
        actual = used if used is not None else self._own_effort
        self.record("assistant", {"text": turn.text, "stop": turn.stop, "usage": usage, "tier": TIER,
                                  "tool_calls": calls, "role": role, "effort": actual, "requested_effort": effort,
                                  "call_id": call_id, "seq": self.seq})
        sent = _snapshot(messages)
        prefix = common_prefix(self._previous, sent)
        self.record("gateway_call", {"call_id": call_id, "attempt_id": self.attempt_id, "seq": self.seq, "role": role,
                                     "effort": actual, "requested_effort": effort,
                                     "model_identity_ref": self.model_identity_ref,
                                     "response": {"text": turn.text, "tool_calls": calls, "stop": turn.stop},
                                     "usage": usage, "messages_prefix": prefix, "messages_tail": sent[prefix:],
                                     "tools": self._tool_specs(specs), "elapsed": elapsed})
        self._previous = sent
        if b.max_tokens is not None and self.tokens > b.max_tokens:
            raise LimitHit("tokens")
        return {"text": turn.text, "tool_calls": calls, "stop": turn.stop, "limits": self.limits()}


# ---------------------------------------------------------------------------- the broker
class AttemptBroker:
    """ctx.tool on the host: every call through the deny-list veto (a K rule, loop.py's `_deny`, kept as the broker's
    hook although the business surface has no shell), worker.Broker's base-tool and read-only checks, and the
    Budgeted surface's own permission and budget checks; then a `tool` event in today's shape and a `broker_event`
    line, and the view's recent events refreshed. A surface that reports its budget exhausted ends the attempt at
    that dimension (LimitHit) once the refusal is on record."""

    def __init__(self, surface, *, record, gateway: Gateway, view: dict | None = None, event_prefix: str = "",
                 max_output_chars: int = 20000, attempt_id: str | None = None):
        self.surface, self.record, self.gateway, self.view = surface, record, gateway, view
        self.event_prefix, self.max_output_chars, self.attempt_id = event_prefix, max_output_chars, attempt_id
        self.seq = 0
        self.events: list[dict] = []          # today's hook-view shape, the last VIEW_EVENTS of them in the view

    def call(self, name: str, args: dict | None, *, read_only: bool = False) -> tuple[str, bool]:
        args = dict(args or {})
        self.seq += 1
        event_id = f"{self.event_prefix}b{self.seq}"
        t0 = time.monotonic()
        veto = denied(str(args.get("command", ""))) if name == "bash" else None
        gate: dict = {"decision": "run"}
        if veto:
            output, is_error = f"This command is on the deny list and was not run ({veto}).", True
            gate = {"decision": "veto", "veto": veto}
            self.record("denied", {"id": event_id, "rule": veto})
        else:
            broker = Broker(self.surface, read_only=read_only)
            output, is_error = broker(name, args)
            if broker.calls and broker.calls[-1].get("refused"):
                gate = {"decision": "refused", "reason": broker.calls[-1]["refused"]}
            elif getattr(self.surface, "exhausted", ""):
                # The surface refused this call for the attempt's budget (Budgeted answers an error and marks itself
                # exhausted; the attempt ends below): the record says refused, not run, since nothing ran.
                gate = {"decision": "refused", "reason": "budget",
                        "limit": getattr(self.surface, "limit", "") or "tool_calls"}
        output = str(output)[:self.max_output_chars]
        self.record("tool", {"id": event_id, "name": name, "is_error": bool(is_error), "output": output[:EVENT_CHARS]})
        self.record("broker_event", {"event_id": event_id, "attempt_id": self.attempt_id, "seq": self.seq,
                                     "after_call_id": self.gateway.last_call_id,
                                     "tool": name, "args": args, "output_chars": len(output),
                                     "output_digest": hashlib.sha256(output.encode("utf-8")).hexdigest()[:16],
                                     "is_error": bool(is_error), "gate": gate,
                                     # v0.5 (2026-10-07 review): whether the caller asked for a read-only call (a
                                     # hook, a method), so the host's record shows what the worker asked for
                                     "read_only": bool(read_only),
                                     "elapsed": round(time.monotonic() - t0, 3)})
        self.events.append({"call_id": event_id, "name": name, "args": args,
                            "result_excerpt": "" if is_error else output[:EVENT_CHARS],
                            "error": output[:EVENT_CHARS] if is_error else "", "truncated": len(output) > EVENT_CHARS,
                            "step": self.gateway.act_calls})
        if self.view is not None:
            self.view["events"] = list(self.events[-VIEW_EVENTS:])
        if getattr(self.surface, "exhausted", ""):
            raise LimitHit(getattr(self.surface, "limit", "") or "tool_calls")
        return output, bool(is_error)

    def handler(self, session):
        """The worker's base_call handler: WorkerSession.base_call_handler (a malformed call is answered, never
        raised) over this broker, the request's `read_only` flag honoured, the answer carrying the limits and the
        one view key a tool call changes — `events` — which the worker's ctx merges into its view; the rest of the
        projection went once with `run` and is not resent on every call."""
        def handle(reply: dict) -> dict:
            read_only = bool(reply.get("read_only"))
            base = session.base_call_handler(lambda name, args: self.call(name, args, read_only=read_only))
            answer = base(reply)
            view = {"view": {"events": list(self.view.get("events") or ())}} if self.view is not None else {}
            return {**answer, "limits": self.gateway.limits(), **view}
        return handle


# ---------------------------------------------------------------------------- skills and marks
class SkillReader:
    """ctx.skills on the host: the package's enabled skill index, its memory text, and read(name), which answers
    from the package's own text and records every read — a `skill_read` event and, so the diagnosis keeps seeing
    what the model saw, today's `tool` event under the name read_skill (M1 §1)."""

    def __init__(self, package, *, record, on_read=None, event_prefix: str = ""):
        self.package, self.record, self.on_read, self.event_prefix = package, record, on_read, event_prefix
        self.index = [{"name": s["name"], "description": s.get("description", "")}
                      for s in (package.skills if package is not None else ())]
        self.memory = (package.memory or None) if package is not None else None
        self.seq = 0
        self.reads: list[str] = []

    def read(self, name) -> str | None:
        name = str(name)
        text = self.package.skill_text(name) if self.package is not None else None
        self.seq += 1
        self.reads.append(name)
        names = [s["name"] for s in self.index]
        output = text if text is not None else f"no skill named {name!r}; the skills are {names}"
        self.record("skill_read", {"name": name, "found": text is not None, "chars": len(text or "")})
        self.record("tool", {"id": f"{self.event_prefix}k{self.seq}", "name": "read_skill", "is_error": text is None,
                             "output": output[:EVENT_CHARS]})
        if self.on_read is not None:
            self.on_read(name, text is not None)
        return text


class Marks:
    """ctx.mark on the host: what a package reports about its own loop, written labelled `source: "package"` so no
    reader mistakes it for the host's record (M0 §2). A kind outside MARK_KINDS is kept under `package_mark` with the
    kind it asked for — nothing a package says is lost, and nothing it says can take a host kind's name. The record's
    own fields (RESERVED_KEYS: kind, ts, source, mark) are stripped from the package's data before it is written: a
    `kind` or `ts` in the data would otherwise overwrite the host's in the line (_emit spreads the data last), and
    the label is the host's to put."""

    def __init__(self, record, on_mark=None):
        self.record, self.on_mark = record, on_mark
        self.count = 0

    def mark(self, kind, data: dict | None = None) -> None:
        kind, data = str(kind), {k: v for k, v in dict(data or {}).items() if k not in RESERVED_KEYS}
        self.count += 1
        if kind in MARK_KINDS:
            self.record(kind, {**data, "source": "package"})
        else:
            self.record("package_mark", {"mark": kind, **data, "source": "package"})
        if self.on_mark is not None:
            self.on_mark(kind, data)


# ---------------------------------------------------------------------------- the in-process ctx
class _Reply:
    """Protocol data with attributes and the same names as keys (the worker's Record), so the default agent reads
    both ctxs alike."""

    def __getitem__(self, key):
        if key not in vars(self):
            raise KeyError(key)
        return vars(self)[key]


class ModelReply(_Reply):
    def __init__(self, text: str, tool_calls: list, stop):
        self.text, self.tool_calls, self.stop = text, tool_calls, stop


class ToolReply(_Reply):
    def __init__(self, output: str, is_error: bool = False):
        self.output, self.is_error = output, is_error


class HostContext:
    """The five names (M0 §2) over the host's own gateway, broker, skills and marks, for the default agent running
    in-process. Exactly five public names and nothing of the host behind them: the host objects live in the closures,
    so `getattr(ctx, anything_else)` is an AttributeError here as it is in the worker (PR7-09)."""

    __slots__ = ("view", "skills", "limits", "model", "tool", "mark")

    def __init__(self, gateway: Gateway, broker: AttemptBroker, skills: SkillReader, marks: Marks, view: dict):
        self.view = view
        self.skills = skills
        self.limits = gateway.limits()

        def refresh():
            self.limits = gateway.limits()

        def model(messages, tools=(), role: str = "act", effort=None) -> ModelReply:
            try:
                reply = gateway.model(messages, tools, role, effort)
            except BaseException as e:  # noqa: BLE001 - the host's failure is not the agent's to catch
                raise HostFault(e) from None
            refresh()
            if reply.get("is_error"):
                raise ModelCallError(str(reply.get("error") or "the gateway refused the call"))
            return ModelReply(reply["text"], list(reply["tool_calls"]), reply["stop"])

        def tool(name: str, args=None, *, read_only: bool = False) -> ToolReply:
            try:
                output, is_error = broker.call(name, args or {}, read_only=read_only)
            except BaseException as e:  # noqa: BLE001 - as above
                raise HostFault(e) from None
            refresh()
            return ToolReply(output, is_error)

        def mark(kind: str, **data) -> None:
            try:
                marks.mark(kind, data)
            except BaseException as e:  # noqa: BLE001 - as above
                raise HostFault(e) from None
            refresh()

        self.model, self.tool, self.mark = model, tool, mark
