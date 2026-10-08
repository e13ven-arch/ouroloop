"""ouroloop: a lightweight RSI framework. An LLM improves a small decision model, and the decision model
improves the agent harness.

The decision-model names below are the training stack's (ouroloop/train/), imported on first use, so
`import ouroloop.bench` (and every attempt) loads none of it."""
from __future__ import annotations

from importlib import import_module

__version__ = "0.1.0"
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
