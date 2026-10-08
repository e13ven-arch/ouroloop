"""LLM providers used by the agent, the research agent and `llm` backends (a default answerer or a judge).

A provider has `complete(system, prompt) -> str` and a `source` that says where its answers come from
("frontier_llm" or "open_llm"). Either way they are for evaluation only, never training targets (docs/DESIGN.md
invariant 6). Besides the API providers (anthropic, openai_compat) there is `claude_code` (claude_code.py): Claude
through headless Claude Code on this machine's login, one blind request per call.
"""
from __future__ import annotations

from typing import Protocol


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


class ProviderAccountError(ProviderError):
    """The provider refuses this account: no balance or quota left, or the key is not accepted. Nothing sent
    again will succeed until a person acts, so it is never retried and never recorded as a task's failure."""

    def __init__(self, message: str = ""):
        super().__init__(message, retryable=False, unbilled=True)


# What providers say when the account, not the request, is the problem (Zhipu's code 1113 is "余额不足或无可用资源包").
_ACCOUNT_MARKERS = ("insufficient_quota", "insufficient balance", "credit balance", "billing", "quota exceeded",
                    '"code":"1113"', '"code": "1113"', "余额不足", "资源包", "请充值", "欠费")


def is_account_error(e: BaseException) -> bool:
    """Whether a provider failure is about the account (balance, quota, key) rather than one request. Only a model
    provider's failure is read this way — a ProviderError, or an SDK error that carries an HTTP status: an internal
    error whose text happens to say "billing" (a CRM record's billing address, say) is not a refused account."""
    if isinstance(e, ProviderAccountError):
        return True
    status = getattr(e, "status_code", None)
    if not isinstance(e, ProviderError) and not isinstance(status, int):
        return False
    if status in (401, 402, 403):
        return True
    text = str(e).lower()
    return any(marker.lower() in text for marker in _ACCOUNT_MARKERS)


class ReplyTruncated(ProviderError):
    """The model stopped at its output limit: the reply is incomplete, and asking again with the same limit would
    stop at the same place. A configuration problem (the limit is too small for this model's replies), not a
    transient failure and not the model's answer."""

    def __init__(self, message: str = ""):
        super().__init__(message, retryable=False)


class ReplyRefused(ProviderError):
    """The model declined the request. Not an answer: a research step reads it as an unreadable reply, never as a
    decision (docs/modules/04_RESEARCH_LOOP.md §1)."""

    def __init__(self, message: str = ""):
        super().__init__(message, retryable=False)
