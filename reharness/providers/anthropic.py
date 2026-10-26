"""Claude through the official Anthropic SDK. Optional dependency: pip install "reharness[anthropic]".

Credentials come from the environment (ANTHROPIC_API_KEY, or a profile from `ant auth login`). The agent loop
owns the tool-use cycle itself, because decision points run before and after every tool call.
"""
from __future__ import annotations

from ..runtime.chat import ChatTurn, ToolCall, ToolSpec
from . import ProviderError

# Models that accept server-side refusal fallbacks in their "default" (route-by-category) form.
_FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5", "claude-fable-5-1"}
_STOP = {"end_turn": "end", "stop_sequence": "end", "tool_use": "tool_use", "max_tokens": "max_tokens",
         "refusal": "refusal"}


def to_anthropic(messages: list[dict], provider: str = "anthropic") -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "user":
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                # A host message right after tool results (a package hook's feedback) joins that user turn, so
                # the roles still alternate.
                out[-1]["content"].append({"type": "text", "text": m["content"]})
            else:
                out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            native = (m.get("native") or {}).get(provider)
            if native is not None:
                out.append({"role": "assistant", "content": native})   # replay unchanged, thinking blocks included
            else:
                blocks = [{"type": "text", "text": m["text"]}] if m.get("text") else []
                blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args}
                           for c in m.get("tool_calls", [])]
                out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r["id"], "content": r["output"], "is_error": r["is_error"]}
                for r in m["results"]]})
    return out


class AnthropicProvider:
    name = "anthropic"
    source = "frontier_llm"            # lineage (DESIGN §8, invariant 6); eval-only in the ledger until v2.0
    supports_history_edits = False     # edited history invalidates Claude's thinking blocks; keep it append-only

    def __init__(self, model: str = "claude-opus-5", max_tokens: int = 16000, effort: str | None = None,
                 fallbacks: bool | None = None, client=None, retries: int = 2):
        self.model = model
        self.retries = retries        # the SDK's own retries; a metered caller sets 0 and books each request
        self.max_tokens = max_tokens
        self.effort = effort                  # "low" .. "max"; None keeps the API default ("high")
        self.fallbacks = model in _FALLBACK_MODELS if fallbacks is None else fallbacks
        self._client = client

    def _get(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(max_retries=self.retries)
        return self._client

    def _create(self, **kwargs):
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        client = self._get()
        options = getattr(client, "with_options", None)
        if callable(options):
            client = options(max_retries=self.retries)       # read per call: a meter sets 0 and books each retry
        if self.fallbacks:
            # On a policy decline the API re-runs the request on its recommended fallback model.
            return client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                               extra_body={"fallbacks": "default"}, **kwargs)
        return client.messages.create(**kwargs)

    def complete(self, system: str, prompt: str) -> str:
        resp = self._create(model=self.model, max_tokens=self.max_tokens, system=system,
                            messages=[{"role": "user", "content": prompt}])
        if resp.stop_reason == "refusal":
            category = getattr(getattr(resp, "stop_details", None), "category", None)
            raise ProviderError(f"{self.model} declined the request (category: {category})")
        return "".join(block.text for block in resp.content if block.type == "text")

    def chat(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> ChatTurn:
        # Automatic caching: the breakpoint follows the growing conversation, so each turn re-reads the prefix.
        resp = self._create(model=self.model, max_tokens=self.max_tokens, system=system,
                            messages=to_anthropic(messages, self.name), cache_control={"type": "ephemeral"},
                            tools=[{"name": t.name, "description": t.description, "input_schema": t.schema}
                                   for t in tools])
        usage = getattr(resp, "usage", None)
        return ChatTurn(
            text="".join(b.text for b in resp.content if b.type == "text"),
            tool_calls=[ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"],
            stop=_STOP.get(resp.stop_reason, "other"), native=resp.content,
            usage={k: getattr(usage, k, None) or 0 for k in ("input_tokens", "output_tokens",
                                                              "cache_read_input_tokens", "cache_creation_input_tokens")})
