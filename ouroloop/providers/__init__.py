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
    """A failed provider call. `retryable` says whether sending the same request again may succeed (a dropped
    connection, a timeout, a rate limit, a server error) — the caller that books each request decides whether to."""

    def __init__(self, message: str = "", *, retryable: bool = False, unbilled: bool = False):
        super().__init__(message)
        self.retryable = retryable
        # The request never reached the model: it could not be sent, or the provider turned it away before any
        # work (rate limit, overload). A metered caller releases its reservation instead of keeping it as unknown.
        self.unbilled = unbilled


class ReplyTruncated(ProviderError):
    """The model stopped at its output limit: the reply is incomplete, and asking again with the same limit would
    stop at the same place. A configuration problem (the limit is too small for this model's replies), not a
    transient failure and not the model's answer."""

    def __init__(self, message: str = ""):
        super().__init__(message, retryable=False)


class MockProvider:
    """Answers with a function of the prompt. For tests and the offline demo."""

    name = "mock"

    def __init__(self, fn: Callable[[str, str], str], model: str = "scripted", source: str = "frontier_llm"):
        self.fn = fn
        self.model = model
        self.source = source

    def complete(self, system: str, prompt: str) -> str:
        return self.fn(system, prompt)
