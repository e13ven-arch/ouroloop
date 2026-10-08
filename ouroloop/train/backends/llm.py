"""An LLM as a decision backend: a phase's default answerer ("llm:<backend name>") or a judge. It picks one
candidate; a reply it cannot parse abstains. Its answers carry the provider's `source` as their lineage
(docs/DESIGN.md §8, invariant 6, data lineage and selection); until v2.0 replaces the ledger's source gate with the
data plan's selection (docs/design/V2_TRAINING_PIPELINE.md §2.5, §3.8), they are kept for evaluation only."""
from __future__ import annotations

import json
import re

from ..types import Answer, Request

SYSTEM = "You make one decision at a time. Read the state, pick exactly one option, and reply with JSON only."


def parse_label(text: str, labels: list[str]) -> str | None:
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            label = str(json.loads(m.group(0)).get("label", "")).strip()
            if label in labels:
                return label
        except (json.JSONDecodeError, AttributeError):
            pass
    hits = [lab for lab in labels if re.search(rf"\b{re.escape(lab)}\b", text)]
    return hits[0] if len(hits) == 1 else None


class LlmBackend:
    name = "llm"

    def __init__(self, provider):
        self.provider = provider
        self.source = getattr(provider, "source", "frontier_llm")

    def decide(self, requests: list[Request]) -> list[Answer]:
        out = []
        for r in requests:
            q, labels = r.question, r.question.labels()
            if q.type == "noul":
                crit = q.criteria or {}
                options = [f'- yes: {crit.get("true") or "the statement holds"}',
                           f'- no: {crit.get("false") or "it does not"}']
            elif q.type == "choice":
                options = [f"- {k}: {v or ''}" for k, v in q.criteria.items()]
            else:
                options = [f"- {i}: {v}" for i, v in enumerate(q.criteria)]
            prompt = (f"State:\n{r.state}\n\nQuestion: {q.instructions}\nOptions:\n" + "\n".join(options)
                      + f'\n\nReply with JSON only: {{"label": "<one of {", ".join(labels)}>"}}')
            label = parse_label(self.provider.complete(SYSTEM, prompt), labels)
            out.append(Answer.of(labels, {label: 1.0} if label is not None else {},
                                 f"{self.provider.name}:{getattr(self.provider, 'model', '')}"))
        return out
