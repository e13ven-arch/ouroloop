"""Provider-neutral chat types: what a provider's `chat(system, messages, tools) -> ChatTurn` takes and returns, as the
gateway calls it for the agent (agent/gateway.py) and R's session calls it (research/session.py).

History entries:
    {"role": "user", "content": str}
    {"role": "assistant", "text": str, "tool_calls": [ToolCall], "native": {provider_name: native_content}}
    {"role": "tool", "results": [{"id", "name", "output", "is_error"}]}
Providers convert these to their own format on every call. `native` keeps the provider's exact assistant content
(for Claude, including thinking blocks), which must be sent back unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ChatTurn:
    text: str
    tool_calls: list[ToolCall]
    stop: str                 # "end", "tool_use", "max_tokens", "refusal" or "other"
    native: Any = None
    usage: dict = field(default_factory=dict)


class ScriptedChat:
    """A chat provider driven by a function of the history, or a list of turns. For tests and demos."""

    name = "scripted"
    model = "scripted"
    supports_history_edits = True

    def __init__(self, script: Callable[[list[dict]], ChatTurn] | list[ChatTurn]):
        self.script = script
        self.calls = 0

    def chat(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> ChatTurn:
        self.calls += 1
        if callable(self.script):
            return self.script(messages)
        return self.script[self.calls - 1]
