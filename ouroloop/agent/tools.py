"""What a tool call returns to the agent (ToolResult), and the clipping of long outputs. The tools themselves are the
episode's business tools (adapters/automationbench.py), offered through the attempt's broker (agent/gateway.py). The
v1 loop's working-directory tools (read, write, edit, bash) went with it in P1 step 5 (tag archive-2026-10-08).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    meta: dict = field(default_factory=dict)


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - limit} characters omitted ...]\n{text[-half:]}"
