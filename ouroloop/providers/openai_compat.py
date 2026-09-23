"""Any OpenAI-compatible chat endpoint (vLLM, Ollama, OpenRouter, ...), for non-Claude models."""
from __future__ import annotations

import json
import os
import time

import httpx

from ..chat import ChatTurn, ToolCall, ToolSpec
from . import ProviderError

_STOP = {"stop": "end", "tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}
_RETRY = {429, 500, 502, 503, 504, 529}


def to_openai(system: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg = {"role": "assistant", "content": m.get("text") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                                     for c in m["tool_calls"]]
            out.append(msg)
        else:
            out += [{"role": "tool", "tool_call_id": r["id"], "content": r["output"]} for r in m["results"]]
    return out


class OpenAICompatProvider:
    name = "openai_compat"
    supports_history_edits = True

    def __init__(self, model: str, base_url: str = "http://localhost:11434/v1", api_key_env: str | None = None,
                 source: str = "frontier_llm", max_tokens: int = 4096, timeout: float = 120.0,
                 extra: dict | None = None, retries: int = 4, client: httpx.Client | None = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.source = source       # set "open_llm" for open-weight models whose answers may be training labels
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.extra = extra or {}   # provider-specific request fields, e.g. {"thinking": {"type": "disabled"}} for GLM
        self.retries = retries     # attempts after a rate limit, a server error or a dropped connection
        self.client = client or httpx.Client()

    def _post(self, body: dict) -> dict:
        headers = {}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ProviderError(f"{self.api_key_env} is not set")
            headers["Authorization"] = f"Bearer {key}"
        payload = {"model": self.model, "max_tokens": self.max_tokens, **self.extra, **body}
        for attempt in range(self.retries + 1):
            try:
                resp = self.client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers,
                                        timeout=self.timeout)
            except httpx.TransportError as e:
                if attempt == self.retries:
                    raise ProviderError(f"{type(e).__name__}: {e}") from e
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code not in _RETRY or attempt == self.retries:
                    raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            time.sleep(min(30.0, 2.0 ** attempt))
        raise AssertionError("unreachable")

    def complete(self, system: str, prompt: str) -> str:
        data = self._post({"messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
        return data["choices"][0]["message"]["content"] or ""

    def chat(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> ChatTurn:
        data = self._post({"messages": to_openai(system, messages),
                           "tools": [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                        "parameters": t.schema}} for t in tools]})
        choice = data["choices"][0]
        msg = choice["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_unparsed": c["function"].get("arguments")}
            calls.append(ToolCall(c["id"], c["function"]["name"], args))
        return ChatTurn(msg.get("content") or "", calls, _STOP.get(choice.get("finish_reason"), "other"),
                        usage=data.get("usage") or {})
