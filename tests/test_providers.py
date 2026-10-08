import json
from types import SimpleNamespace

import httpx
import pytest

from ouroloop.chat import ToolCall, ToolSpec
from ouroloop.providers import ProviderError
from ouroloop.providers.anthropic import AnthropicProvider, to_anthropic
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


# ---------------------------------------------------------------- provider formats (formerly test_agent)
def test_anthropic_history_and_tool_calls():
    call = ToolCall("tu_1", "bash", {"command": "ls"})
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "text": "", "tool_calls": [call], "native": {"anthropic": ["<native blocks>"]}},
               {"role": "tool", "results": [{"id": "tu_1", "name": "bash", "output": "a.txt", "is_error": False}]},
               {"role": "assistant", "text": "from elsewhere", "tool_calls": [], "native": {"other": None}}]
    msgs = to_anthropic(history)
    assert msgs[1] == {"role": "assistant", "content": ["<native blocks>"]}
    assert msgs[2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu_1", "content": "a.txt", "is_error": False}
    assert msgs[3]["content"] == [{"type": "text", "text": "from elsewhere"}]
    blocks = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text="let me look"),
              SimpleNamespace(type="tool_use", id="tu_2", name="read", input={"path": "a.txt"})]
    resp = SimpleNamespace(stop_reason="tool_use", content=blocks,
                           usage=SimpleNamespace(input_tokens=5, output_tokens=2, cache_read_input_tokens=90))
    seen = {}
    create = SimpleNamespace(create=lambda **kw: seen.update(kw) or resp)
    turn = AnthropicProvider(client=SimpleNamespace(beta=SimpleNamespace(messages=create))).chat(
        "sys", history, [ToolSpec("read", "r", {"type": "object"})])
    assert turn.stop == "tool_use" and turn.native is blocks and turn.tool_calls[0].args == {"path": "a.txt"}
    assert seen["cache_control"] == {"type": "ephemeral"} and seen["extra_body"] == {"fallbacks": "default"}
    assert turn.usage == {"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 90,
                          "cache_creation_input_tokens": 0}


def test_openai_compatible_extra_fields_and_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    bodies, codes = [], [429, 503, 200]

    def handler(request):
        bodies.append(json.loads(request.content))
        code = codes.pop(0)
        return httpx.Response(code, json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
                              if code == 200 else {"error": "busy"})

    p = OpenAICompatProvider("glm-5.3", extra={"thinking": {"type": "disabled"}},
                             client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert p.complete("sys", "hi") == "ok" and len(bodies) == 3
    assert bodies[0]["thinking"] == {"type": "disabled"} and bodies[0]["model"] == "glm-5.3"
    p = OpenAICompatProvider("m", retries=1, client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": "bad request"}))))
    with pytest.raises(ProviderError, match="HTTP 400"):
        p.complete("sys", "hi")


def test_openai_compatible_tool_calls():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {"content": None,
                              "tool_calls": [{"id": "c1", "type": "function",
                                              "function": {"name": "bash", "arguments": "{\"command\": \"ls\"}"}}]}}]})

    p = OpenAICompatProvider("qwen3", client=httpx.Client(transport=httpx.MockTransport(handler)))
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "text": "", "tool_calls": [ToolCall("c0", "read", {"path": "x"})]},
               {"role": "tool", "results": [{"id": "c0", "name": "read", "output": "text", "is_error": False}]}]
    turn = p.chat("sys", history, [ToolSpec("bash", "b", {"type": "object"})])
    assert turn.stop == "tool_use" and turn.tool_calls[0].args == {"command": "ls"}
    msgs = seen["body"]["messages"]
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "read", "arguments": "{\"path\": \"x\"}"}
    assert msgs[3] == {"role": "tool", "tool_call_id": "c0", "content": "text"}
    assert seen["body"]["tools"][0]["function"]["name"] == "bash"
