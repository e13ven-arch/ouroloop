"""The five hard limits of one attempt (docs/design/EVALUATE.md §3; CONTRACTS §4 `limits`): Budget field -> the
dimension a `budget` termination records in TaskResult.limit. `max_steps` keeps its name: a step is one `act` model
call. `max_model_calls` and `max_tokens` default to None (no limit), so a plan frozen before them reads as it was.
"""
from __future__ import annotations

from dataclasses import dataclass

HARD_LIMITS = {"max_steps": "act_calls", "max_model_calls": "model_calls", "max_tool_calls": "tool_calls",
               "max_seconds": "seconds", "max_tokens": "tokens"}
LIMIT_DIMENSIONS = tuple(HARD_LIMITS.values())
V03_BUDGET_FIELDS = ("max_model_calls", "max_tokens")


@dataclass(frozen=True)
class Budget:
    """Per-attempt limits. `max_usd` is a limit, not a measurement: an attempt whose cost cannot be measured is
    not stopped by it, and its result says the cost is unknown. `max_model_calls` counts every model call, act and
    aux together; `max_tokens` is the attempt's output tokens over all of them, thinking included — not the
    `[llm] max_tokens` of one reply. None on either means no such limit."""
    max_steps: int = 50
    max_tool_calls: int = 200
    max_seconds: float = 900.0
    max_usd: float | None = None
    max_model_calls: int | None = None
    max_tokens: int | None = None

    def limits(self) -> dict:
        """The hard limits that are set, by field name: what a frozen plan and a TargetHarness carry."""
        return {k: getattr(self, k) for k in HARD_LIMITS if getattr(self, k) is not None}

    def limit_hit(self, *, steps: int = 0, model_calls: int = 0, tool_calls: int = 0, seconds: float = 0.0,
                  tokens: int = 0) -> str | None:
        """The dimension of the first hard limit these counts exceed, in HARD_LIMITS order, or None."""
        counts = {"max_steps": steps, "max_model_calls": model_calls, "max_tool_calls": tool_calls,
                  "max_seconds": seconds, "max_tokens": tokens}
        for key, cap in self.limits().items():
            if counts[key] > cap:
                return HARD_LIMITS[key]
        return None

    def exceeded(self, *, steps: int = 0, tool_calls: int = 0, seconds: float = 0.0,
                 usd: float | None = None, model_calls: int = 0, tokens: int = 0) -> str:
        if steps > self.max_steps:
            return f"steps {steps} > {self.max_steps}"
        if self.max_model_calls is not None and model_calls > self.max_model_calls:
            return f"model calls {model_calls} > {self.max_model_calls}"
        if tool_calls > self.max_tool_calls:
            return f"tool calls {tool_calls} > {self.max_tool_calls}"
        if seconds > self.max_seconds:
            return f"seconds {seconds:.0f} > {self.max_seconds:.0f}"
        if self.max_tokens is not None and tokens > self.max_tokens:
            return f"tokens {tokens} > {self.max_tokens}"
        if self.max_usd is not None and usd is not None and usd > self.max_usd:
            return f"cost {usd:.4f} > {self.max_usd:.4f}"
        return ""

    def to_core(self, *, max_output_chars: int = 20000) -> dict:
        """K's `limits` section: the five hard limits (None where unset) and the output bound."""
        return {**{k: getattr(self, k) for k in HARD_LIMITS}, "max_output_chars": max_output_chars}

    @classmethod
    def from_core(cls, limits: dict) -> "Budget":
        known = {k: limits[k] for k in ("max_steps", "max_tool_calls", "max_seconds", "max_usd", "max_model_calls",
                                        "max_tokens") if k in (limits or {})}
        return cls(**known)
