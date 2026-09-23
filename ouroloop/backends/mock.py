"""A deterministic stand-in for a decision model, for tests and the offline demo.

It scores each candidate by word overlap between the candidate's description and the state, so rewording a
question or trimming a view really changes its answers. It knows nothing else.
"""
from __future__ import annotations

import math
import re

from ..types import Answer, Request

_STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "not", "but", "can", "has", "have",
         "will", "its", "into", "when", "than", "then", "there", "such", "other"}


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", str(text).lower()) if len(t) >= 3 and t not in _STOP}


class KeywordBackend:
    name = "mock"

    def __init__(self, sharpness: float = 1.5):
        self.sharpness = sharpness

    def decide(self, requests: list[Request]) -> list[Answer]:
        out = []
        for r in requests:
            q, state = r.question, tokens(r.state)
            if q.type == "noul":
                crit = q.criteria or {}
                descs = {"yes": crit.get("true") or q.instructions, "no": crit.get("false") or ""}
            elif q.type == "choice":
                descs = {str(k): f"{k} {v or ''}" for k, v in q.criteria.items()}
            else:
                descs = {str(i): str(v) for i, v in enumerate(q.criteria)}
            z = {k: math.exp(self.sharpness * len(tokens(d) & state)) for k, d in descs.items()}
            total = sum(z.values())
            out.append(Answer({k: v / total for k, v in z.items()}, model="mock-keyword"))
        return out
