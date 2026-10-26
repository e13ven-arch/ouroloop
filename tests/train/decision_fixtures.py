"""A scripted LLM for the training stack's tests (formerly providers.MockProvider; P1 step 3 removed it from the
package): it answers with a function of the prompt. The synthetic `retry` ledger and the research round over it that
lived here went with the champion pointer in P1 step 5 (tag archive-2026-10-08)."""
from __future__ import annotations

from typing import Callable


class MockProvider:
    """Answers with a function of the prompt."""

    name = "mock"

    def __init__(self, fn: Callable[[str, str], str], model: str = "scripted", source: str = "frontier_llm"):
        self.fn = fn
        self.model = model
        self.source = source

    def complete(self, system: str, prompt: str) -> str:
        return self.fn(system, prompt)
