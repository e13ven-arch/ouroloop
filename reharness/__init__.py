"""reharness: bounded recursive self-improvement for agent applications. An improver R evolves the harness package W —
the agent loop, tools, hooks, prompts, skills, memory, model binding and config of one business line — run by a fixed
runner shell under a fixed core K (scorer, splits, gate, budgets); a change is installed only after a train screen, a
paired dev gate and a confirmation reading (README.md, docs/DESIGN.md).

The decision-model names below are the optional training stack's (reharness/train/, `python -m reharness.train`),
imported on first use, so an activity (and every attempt) loads none of it."""
from __future__ import annotations

from importlib import import_module

__version__ = "0.5.0"                 # pyproject.toml's version (tests/test_entry_points.py)
_LAZY = {"Choice": ".train.decision", "Decision": ".train.decision", "DecisionPoint": ".train.decision",
         "Noul": ".train.decision", "Score": ".train.decision", "Spec": ".train.decision", "Policy": ".train.policy",
         "Runtime": ".train.runtime", "Answer": ".train.types", "Question": ".train.types", "Request": ".train.types"}
__all__ = ["Answer", "Choice", "Decision", "DecisionPoint", "Noul", "Policy", "Question", "Request", "Runtime",
           "Score", "Spec"]


def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_LAZY[name], __name__), name)
    globals()[name] = value
    return value
