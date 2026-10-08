"""The agent host. Its names are imported on first use: an attempt under the runner shell (agent.runner) loads the
gateway, the broker and the worker, never the v1 loop (agent.loop) or the decision points it asks."""
from __future__ import annotations

from importlib import import_module

_LAZY = {"Agent": ".loop", "AgentConfig": ".loop", "AgentResult": ".runner", "ToolResult": ".tools", "Tools": ".tools",
         "ToolSurface": ".tools"}

__all__ = ["Agent", "AgentConfig", "AgentResult", "ToolResult", "Tools", "ToolSurface"]


def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_LAZY[name], __name__), name)
    globals()[name] = value
    return value
