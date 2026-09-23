import json
from types import SimpleNamespace

import httpx
import pytest

from ouroloop.providers import ProviderError
from ouroloop.providers.anthropic import AnthropicProvider
from ouroloop.providers.openai_compat import OpenAICompatProvider


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_client(stop_reason="end_turn", text="hello", category=None):
    resp = SimpleNamespace(stop_reason=stop_reason, stop_details=SimpleNamespace(category=category),
                           content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)])
    return SimpleNamespace(messages=FakeMessages(resp), beta=SimpleNamespace(messages=FakeMessages(resp)))


def test_anthropic_opus5_uses_default_fallbacks_and_returns_text():
    client = fake_client()
    p = AnthropicProvider(client=client, effort="low")
    assert p.model == "claude-opus-5" and p.source == "frontier_llm"
    assert p.complete("sys", "hi") == "hello"
    kw = client.beta.messages.kwargs
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["extra_body"] == {"fallbacks": "default"}
    assert kw["output_config"] == {"effort": "low"} and kw["system"] == "sys"


def test_anthropic_other_models_skip_fallbacks_and_refusals_raise():
    client = fake_client()
    AnthropicProvider(model="claude-sonnet-5", client=client).complete("s", "p")
    assert client.messages.kwargs["model"] == "claude-sonnet-5" and client.beta.messages.kwargs is None
    with pytest.raises(ProviderError, match="cyber"):
        AnthropicProvider(client=fake_client(stop_reason="refusal", category="cyber")).complete("s", "p")


def test_openai_compatible_endpoint(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setenv("OR_KEY", "k")
    p = OpenAICompatProvider("qwen3", base_url="http://x/v1/", api_key_env="OR_KEY", source="open_llm",
                             client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert p.complete("sys", "hi") == "ok"
    assert seen["auth"] == "Bearer k" and seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
