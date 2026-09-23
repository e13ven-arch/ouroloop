import json

import httpx
import pytest

from ouroloop.backends import JevBackend, JevError, KeywordBackend, LlmBackend, TdeBackend
from ouroloop.providers import MockProvider
from ouroloop.types import Question, Request

NOUL = Question("noul", "urgent?")
CHOICE = Question("choice", "team?", {"billing": "money", "technical": "bugs"})
SCORE = Question("score", "mood?", ["calm", "annoyed", "angry"])


def jev_with(handler, **kw):
    return JevBackend(client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None, **kw)


def test_jev_batches_questions_that_share_a_state(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "testkey")
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request, body))
        return httpx.Response(200, json={"model": "jev-1.13.0", "usage": {"input_tokens": 10, "output_tokens": 3},
                                         "answers": {
            "q0": {"type": "noul", "noul": 0.8},
            "q1": {"type": "choice", "choice": "technical", "probabilities": {"technical": 0.7, "billing": 0.3}},
            "q2": {"type": "score", "score": 1.0, "probabilities": {"0": 0.1, "1": 0.8, "2": 0.1}}}})

    out = jev_with(handler).decide([Request("s", NOUL), Request("s", CHOICE), Request("s", SCORE)])
    assert len(calls) == 1
    request, body = calls[0]
    assert request.headers["authorization"] == "Bearer testkey"
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert body["model"] == "jev-1.13.0" and set(body["questions"]) == {"q0", "q1", "q2"}
    assert body["questions"]["q1"]["criteria"] == {"billing": "money", "technical": "bugs"}
    assert out[0].probs == {"yes": 0.8, "no": pytest.approx(0.2)}
    assert out[1].label == "technical" and out[2].label == "1"
    assert out[0].usage == {"input_tokens": 10, "output_tokens": 3} and out[1].usage == {}


def test_jev_retries_on_rate_limit_and_reports_errors_without_the_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "testkey")
    responses = [httpx.Response(429, text="slow down"),
                 httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"q0": {"type": "noul", "noul": 0.3}}})]
    assert jev_with(lambda r: responses.pop(0)).decide([Request("s", NOUL)])[0].label == "no"
    with pytest.raises(JevError) as err:
        jev_with(lambda r: httpx.Response(401, text="bad key")).decide([Request("s", NOUL)])
    assert "401" in str(err.value) and "testkey" not in str(err.value)
    monkeypatch.delenv("TYPESAFE_API_KEY")
    with pytest.raises(JevError, match="TYPESAFE_API_KEY"):
        jev_with(lambda r: httpx.Response(200)).decide([Request("s", NOUL)])


class FakeDecider:
    def __init__(self):
        self.calls = []

    def decide_batch(self, state, questions):
        self.calls.append((state, questions))
        return {k: {"probabilities": {"yes": 0.6, "no": 0.4} if q["type"] == "noul" else {"billing": 2, "technical": 2}}
                for k, q in questions.items()}


def test_tde_backend_groups_by_state_and_normalizes():
    fake = FakeDecider()
    out = TdeBackend("runs/exp010", decider=fake).decide([Request("a", NOUL), Request("a", CHOICE), Request("b", NOUL)])
    assert [s for s, _ in fake.calls] == ["a", "b"]
    assert out[0].probs == {"yes": 0.6, "no": 0.4} and out[1].probs == {"billing": 0.5, "technical": 0.5}
    assert out[0].model == "tde:exp010"


def test_llm_backend_parses_a_label_or_abstains():
    backend = LlmBackend(MockProvider(lambda s, p: '{"label": "technical"}', source="open_llm"))
    assert backend.source == "open_llm"
    assert backend.decide([Request("s", CHOICE)])[0].probs == {"billing": 0.0, "technical": 1.0}
    unsure = LlmBackend(MockProvider(lambda s, p: "no idea")).decide([Request("s", CHOICE)])[0]
    assert unsure.probs == {"billing": 0.5, "technical": 0.5}


def test_keyword_backend_follows_word_overlap():
    q = Question("noul", "retry?", {"true": "network timeout", "false": "syntax error"})
    assert KeywordBackend().decide([Request("network timeout while fetching", q)])[0].label == "yes"
    assert KeywordBackend().decide([Request("syntax error on line 3", q)])[0].label == "no"
