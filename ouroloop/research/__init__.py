"""The research side. Its package-level names (the old decision research loop) are imported on first use, so a bench
run that reads research.context, inspection or session never loads that loop or what it imports."""
from __future__ import annotations

from importlib import import_module

_LAZY = {"Candidate": ".agent", "Report": ".agent", "ResearchAgent": ".agent", "Workspace": ".agent",
         "BasicResearcher": ".basic", "RoundResult": ".loop", "build_report": ".loop", "run_round": ".loop"}

__all__ = ["BasicResearcher", "Candidate", "Report", "ResearchAgent", "RoundResult", "Workspace", "build_report",
           "run_round"]


def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_LAZY[name], __name__), name)
    globals()[name] = value
    return value
