"""The agent host: the runner shell (runner), the gateway and the broker (gateway), the isolated worker (worker), the
default agent and the capability package. Its names are imported on first use."""
from __future__ import annotations

from importlib import import_module

_LAZY = {"AgentResult": ".runner", "ToolResult": ".tools"}

__all__ = ["AgentResult", "ToolResult"]


def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_LAZY[name], __name__), name)
    globals()[name] = value
    return value
