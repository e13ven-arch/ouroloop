"""Real Jev API smoke test. Run with: pytest -m live (needs TYPESAFE_API_KEY, e.g. in .env)."""
import os
from pathlib import Path

import pytest

from ouroloop.backends import JevBackend
from ouroloop.cli import SMOKE_QUESTIONS, SMOKE_STATE
from ouroloop.config import load_dotenv
from ouroloop.types import Request

pytestmark = pytest.mark.live


def test_three_primitives_in_one_call():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    if not os.environ.get("TYPESAFE_API_KEY"):
        pytest.skip("TYPESAFE_API_KEY not set")
    answers = JevBackend().decide([Request(SMOKE_STATE, q) for q in SMOKE_QUESTIONS.values()])
    for q, ans in zip(SMOKE_QUESTIONS.values(), answers):
        assert set(ans.probs) == set(q.labels())
        assert abs(sum(ans.probs.values()) - 1.0) < 1e-6
        assert ans.model.startswith("jev-")
    assert answers[0].usage.get("input_tokens", 0) > 0
