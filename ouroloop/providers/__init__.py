"""LLM providers used by the agent, the research agent and `llm` backends (a default answerer or a judge).

A provider has `complete(system, prompt) -> str` and a `source` that says where its answers come from
("frontier_llm" or "open_llm"). Either way they are for evaluation only, never training targets (docs/DESIGN.md
invariant 6). Besides the API providers (anthropic, openai_compat) there is `manual` (manual.py): the same requests and
replies, as files that a person or another process answers.
"""
from __future__ import annotations

from typing import Callable, Protocol


class Provider(Protocol):
    name: str
    model: str
    source: str

    def complete(self, system: str, prompt: str) -> str: ...


class ProviderError(RuntimeError):
    pass


class MockProvider:
    """Answers with a function of the prompt. For tests and the offline demo."""

    name = "mock"

    def __init__(self, fn: Callable[[str, str], str], model: str = "scripted", source: str = "frontier_llm"):
        self.fn = fn
        self.model = model
        self.source = source

    def complete(self, system: str, prompt: str) -> str:
        return self.fn(system, prompt)
