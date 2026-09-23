"""LLM providers used by the research agent and the `llm` fallback backend.

A provider has `complete(system, prompt) -> str` and a `source`, which decides whether its answers may ever
become training targets ("open_llm") or stay evaluation-only ("frontier_llm").
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

    def __init__(self, fn: Callable[[str, str], str], model: str = "scripted", source: str = "open_llm"):
        self.fn = fn
        self.model = model
        self.source = source

    def complete(self, system: str, prompt: str) -> str:
        return self.fn(system, prompt)
