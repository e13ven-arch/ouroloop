"""ouroloop: a lightweight RSI framework. An LLM improves a small decision model, and the decision model
improves the agent harness.

The decision-model names below are imported on first use, so `import ouroloop.bench` (and every attempt under the
runner shell) loads none of the decision stack."""
from __future__ import annotations

from importlib import import_module

__version__ = "0.1.0"
_LAZY = {"Choice": ".decision", "Decision": ".decision", "DecisionPoint": ".decision", "Noul": ".decision",
         "Score": ".decision", "Spec": ".decision", "Policy": ".policy", "Runtime": ".runtime", "Answer": ".types",
         "Question": ".types", "Request": ".types"}
__all__ = ["Answer", "Choice", "Decision", "DecisionPoint", "Noul", "Policy", "Question", "Request", "Runtime",
           "Score", "Spec"]


def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_LAZY[name], __name__), name)
    globals()[name] = value
    return value
