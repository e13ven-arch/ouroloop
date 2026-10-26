"""ctx.model on the host (docs/design/RUNTIME.md §3, §6): the gateway every model call of an attempt goes through.

For each call: the shape of what the agent sent is checked (`malformed`: answered as an error the agent may catch,
recorded as a `protocol` line, never raised on the host); the role must be one the manifest declares; the effort is
capped at K's `max_effort` (the requested value is recorded beside the one sent, never refused); the act-call,
model-call and seconds limits are checked before the provider is called and the output tokens after it; the first
`system` message becomes the provider's system argument. The provider is what the host gives — a MeteredProvider in an
activity, which reserves before and settles after every call — and the call is written as one `gateway_call` line (the
messages as a prefix count against the previous call plus the tail; the tools by digest, their full specs on a
`tool_specs` line whenever they change). Each answer carries the call's usage, which the worker merges into the
agent's ctx.view["last_usage"].

A call past a limit raises LimitHit with the dimension; the attempt ends as termination `budget`, and the package
cannot catch it: the worker session is closed under it.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import time
import uuid

from ..contracts import LimitHit
from ..harness.contract import EFFORTS
from .chat import ToolCall, ToolSpec


def normalize_usage(usage: dict | None) -> dict:
    """One provider's usage in one vocabulary: uncached input, cache reads, cache writes, output. Anthropic reports
    input without the cached part and the cache beside it; OpenAI-compatible APIs report prompt_tokens with the cached
    part inside. Reading one as the other prices a call at zero or counts its cache twice."""
    usage = dict(usage or {})
    if "input_tokens" in usage or "output_tokens" in usage:
        return {"input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
                "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0)}
    details = usage.get("prompt_tokens_details") or {}
    cached = int((details.get("cached_tokens") if isinstance(details, dict) else 0) or 0)
    return {"input_tokens": max(int(usage.get("prompt_tokens") or 0) - cached, 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "cache_read_tokens": cached, "cache_write_tokens": 0}


def cap_effort(requested: str | None, max_effort: str | None) -> str | None:
    """The effort a call is made at: the request, capped at K's ceiling by the EFFORTS order; None stays None."""
    if requested is None or max_effort is None:
        return requested
    return requested if EFFORTS.index(requested) <= EFFORTS.index(max_effort) else max_effort


def takes_effort(provider) -> bool:
    """Whether the provider's `chat` takes an `effort` keyword, looking through meters (a `.provider` whose chat passes
    **kwargs on) to the provider that makes the call."""
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
    """The effort the provider itself is pinned at, looking through meters: what a call made with no request runs at."""
    seen: set[int] = set()
    while provider is not None and id(provider) not in seen:
        seen.add(id(provider))
        effort = getattr(provider, "effort", None)
        if isinstance(effort, str) and effort:
            return effort
        provider = getattr(provider, "provider", None)
    return None


def model_name(provider) -> str | None:
    seen: set[int] = set()
    while provider is not None and id(provider) not in seen:
        seen.add(id(provider))
        name = getattr(provider, "model", None)
        if isinstance(name, str) and name:
            return name
        provider = getattr(provider, "provider", None)
    return None


def malformed(messages, tools) -> str | None:
    """Why a ctx.model call cannot be made, or None: messages are objects with a string role; an assistant entry's
    tool_calls are objects with a string name and an object of args; a tool entry carries a list of results; tools are
    objects with a string name and, when given, an object schema."""
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


def tool_spec(tool) -> ToolSpec:
    if isinstance(tool, ToolSpec):
        return tool
    schema = tool.get("schema", tool.get("input_schema"))
    return ToolSpec(str(tool.get("name", "")), str(tool.get("description") or ""),
                    dict(schema) if isinstance(schema, dict) else {"type": "object"})


def split_system(messages: list) -> tuple[str, list[dict]]:
    """(system, history): the first `role: system` message is the provider's system argument; the rest is the history
    in chat.py's shape, an assistant entry's tool calls as ToolCall objects."""
    system, history = "", []
    for m in messages:
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


def common_prefix(previous: list, current: list) -> int:
    n = 0
    for a, b in zip(previous, current):
        if a != b:
            break
        n += 1
    return n


def _left(cap, used) -> int | None:
    return None if cap is None else max(int(cap) - int(used), 0)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


class Gateway:
    """ctx.model on the host. `provider` is what the host calls (the pinned actor, metered in an activity); `roles` the
    manifest's declared roles; `max_effort` K's ceiling; `limits` K's five hard limits (max_steps, max_model_calls,
    max_tool_calls, max_seconds, max_tokens; None where unset); `record(kind, data)` the transcript; `surface` the
    attempt's tool surface (read for the tool calls so far and the clock); `call_prefix` goes before every call id."""

    def __init__(self, provider, *, roles, max_effort, limits: dict, record, surface=None, call_prefix: str = "",
                 attempt_id: str | None = None, pricing=None):
        self.provider, self.roles, self.max_effort = provider, tuple(roles or ()), max_effort
        self.caps = {k: limits.get(k) for k in ("max_steps", "max_model_calls", "max_tool_calls", "max_seconds",
                                                "max_tokens")}
        self.record, self.surface, self.call_prefix = record, surface, call_prefix
        self.attempt_id, self.pricing = attempt_id, pricing
        self.identity = model_name(provider)
        self.started = getattr(surface, "started", None) or time.monotonic()
        self._effort_kw = takes_effort(provider)
        self._own_effort = own_effort(provider)
        self.act_calls = 0
        self.model_calls = 0
        self.tokens = 0                       # output tokens of every call so far, thinking included
        self.usage: dict[str, int] = {}       # the attempt's usage, normalized, summed
        self.costs: list = []                 # per call: USD, or None when the call cannot be priced
        self.last_call_id: str | None = None
        self.last_act_stop: str | None = None
        self._previous: list = []
        self._specs_digest: str | None = None

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def limits(self) -> dict:
        """The remaining amount of each hard limit (ctx.limits); None where K set none."""
        c = self.caps
        tool_calls = None
        if c["max_tool_calls"] is not None and getattr(self.surface, "tool_calls", None) is not None:
            tool_calls = _left(c["max_tool_calls"], self.surface.tool_calls)
        return {"act_calls": _left(c["max_steps"], self.act_calls),
                "model_calls": _left(c["max_model_calls"], self.model_calls),
                "tool_calls": tool_calls,
                "seconds": None if c["max_seconds"] is None else max(int(c["max_seconds"] - self.elapsed()), 0),
                "tokens": _left(c["max_tokens"], self.tokens)}

    def cost(self) -> float | None:
        """The attempt's model cost so far: None when there are no prices or a call could not be priced."""
        if self.pricing is None or not self.costs or any(c is None for c in self.costs):
            return None
        return round(sum(self.costs), 8)

    def _refuse(self, error: str, role, effort) -> dict:
        self.record("protocol", {"error": error, "role": role, "effort_requested": effort})
        return {"is_error": True, "error": error, "limits": self.limits()}

    def _tools(self, specs: list[ToolSpec]) -> str:
        full = [{"name": s.name, "description": s.description, "schema": s.schema} for s in specs]
        digest = _digest(full)
        if digest != self._specs_digest:
            self._specs_digest = digest
            self.record("tool_specs", {"digest": digest, "specs": full})
        return digest

    def model(self, messages, tools=(), role: str = "act", effort: str | None = None) -> dict:
        """One model call. -> {text, tool_calls: [{id, name, args}], stop, usage, limits}, or {is_error, error,
        limits}. Raises LimitHit before the call at max_steps (act calls), max_model_calls or max_seconds, and after
        it at max_tokens: the call that crossed the token limit is recorded and booked, its answer is not handed on."""
        problem = malformed(messages, tools)
        if problem:
            return self._refuse(f"malformed model call: {problem}", role, effort)
        if role not in self.roles:
            return self._refuse(f"role {role!r} is not declared in the manifest's roles {list(self.roles)}", role,
                                effort)
        if effort is not None and effort not in EFFORTS:
            return self._refuse(f"effort {effort!r} is not one of {list(EFFORTS)}", role, effort)
        used = cap_effort(effort, self.max_effort)
        c = self.caps
        if role == "act" and c["max_steps"] is not None and self.act_calls + 1 > c["max_steps"]:
            raise LimitHit("act_calls")
        if c["max_model_calls"] is not None and self.model_calls + 1 > c["max_model_calls"]:
            raise LimitHit("model_calls")
        if c["max_seconds"] is not None and self.elapsed() > c["max_seconds"]:
            raise LimitHit("seconds")
        sent = json.loads(json.dumps(list(messages), default=str))
        system, history = split_system(sent)
        specs = [tool_spec(t) for t in tools]
        kwargs = {"effort": used} if used is not None and self._effort_kw else {}
        t0 = time.monotonic()
        turn = self.provider.chat(system, history, specs, **kwargs)
        ms = int((time.monotonic() - t0) * 1000)
        self.model_calls += 1
        if role == "act":
            self.act_calls += 1
            self.last_act_stop = turn.stop
        usage = dict(turn.usage or {})
        normal = normalize_usage(usage) if usage else {}
        for key, value in normal.items():
            self.usage[key] = self.usage.get(key, 0) + value
        if self.pricing is not None:
            self.costs.append(self.pricing.cost(normal) if usage else None)
        out_tokens = normal.get("output_tokens", 0)
        self.tokens += out_tokens
        call_id = f"{self.call_prefix}m{self.model_calls}.{uuid.uuid4().hex[:8]}"
        self.last_call_id = call_id
        calls = [{"id": c_.id, "name": c_.name, "args": dict(c_.args or {})} for c_ in turn.tool_calls]
        prefix = common_prefix(self._previous, sent)
        self.record("gateway_call", {"call_id": call_id, "attempt_id": self.attempt_id, "model": self.identity,
                                     "role": role, "effort_requested": effort,
                                     "effort_sent": used if used is not None else self._own_effort,
                                     "messages_prefix": prefix, "messages_tail": sent[prefix:],
                                     "tools_digest": self._tools(specs),
                                     "reply": {"text": turn.text, "tool_calls": calls, "stop": turn.stop},
                                     "usage": usage, "ms": ms})
        self._previous = sent
        if c["max_tokens"] is not None and self.tokens > c["max_tokens"]:
            raise LimitHit("tokens")
        return {"text": turn.text, "tool_calls": calls, "stop": turn.stop, "usage": usage, "limits": self.limits()}

    def last_usage(self) -> dict:
        return dict(self.usage)
