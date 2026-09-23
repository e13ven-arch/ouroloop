"""Question conversion (PLAN §5.3): check and complete a typed question before a decision model sees it."""
from __future__ import annotations

from ..types import TYPES, Question

NONE_FIT = "none_fit"
NONE_FIT_DESCRIPTION = "None of the other options fits"
MAX_CHOICE, MIN_SCORE, MAX_SCORE = 255, 2, 10


class QuestionError(ValueError):
    pass


def normalize(question: Question | dict, *, escape: bool = True) -> Question:
    """Validate a typed question. Choice questions get a "none fits" option, since Jev cannot abstain otherwise.
    Only exact duplicates are caught here; making the options mutually exclusive is the asker's job."""
    q = question if isinstance(question, Question) else Question.from_json(question)
    if q.type not in TYPES:
        raise QuestionError(f"unknown question type {q.type!r}")
    if not str(q.instructions).strip():
        raise QuestionError("instructions are empty")
    if q.type == "noul":
        if q.criteria is not None and (not isinstance(q.criteria, dict) or set(q.criteria) - {"true", "false"}):
            raise QuestionError('noul criteria must be {"true": ..., "false": ...}')
        return q
    if q.type == "score":
        levels = list(q.criteria or [])
        if not MIN_SCORE <= len(levels) <= MAX_SCORE:
            raise QuestionError(f"a score needs {MIN_SCORE} to {MAX_SCORE} levels, got {len(levels)}")
        return Question("score", q.instructions, levels)
    if not isinstance(q.criteria, dict):
        raise QuestionError("choice criteria must map labels to descriptions")
    criteria = {str(k).strip(): v for k, v in q.criteria.items()}
    if any(not k for k in criteria) or len({k.lower() for k in criteria}) != len(q.criteria):
        raise QuestionError("choice labels must be non-empty and distinct")
    descriptions = [str(v).strip().lower() for v in criteria.values() if v]
    if len(set(descriptions)) != len(descriptions):
        raise QuestionError("two options share the same description")
    if len(criteria) < 2:
        raise QuestionError("a choice needs at least two real options")
    if escape and NONE_FIT not in criteria:
        criteria[NONE_FIT] = NONE_FIT_DESCRIPTION
    if len(criteria) > MAX_CHOICE:
        raise QuestionError(f"a choice allows at most {MAX_CHOICE} options")
    return Question("choice", q.instructions, criteria)


def fit_state(state: str, max_chars: int) -> tuple[str, bool]:
    """Cut an over-long state. Summaries written for a judge put the essentials first, so keep the head."""
    return (state, False) if len(state) <= max_chars else (state[:max_chars], True)
