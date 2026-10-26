"""The OpenAI-compatible bridge (docs/design/MEMORY.md §2, §4, §9; memory/bridge.py) on its real path: the stdlib HTTP
server on 127.0.0.1, an httpx client, the evaluate.budget.MeteredProvider the bridge requires, and a real budget book
(<tmp>/rsi/budget/book.json). Only the model is scripted: it answers by which of ReMe's prompt templates it is sent, as
the v6 smoke's fake server did. The activity's own builder (Activity.research_provider(step, retries=False)) is tested
through tests/rsi.py's activity, once over a scripted research model and once over the real claude_code provider in
front of a fake `claude` program. Ported from v6-gateway's tests/test_bridge.py and tests/test_bridge_claude_code.py.

v6's fixture of ReMe's 51 smoke requests (tests/fixtures/reme_requests.jsonl) is not in this repository; REME_REQUESTS
below are requests of the same shape (model, messages: one user message, stream: true, tools: null), one per ReMe
prompt template, plus the template-less one ReMe's context step sends."""
from __future__ import annotations

import hashlib
import json
import socket
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

import reharness.memory.bridge as B
from reharness.evaluate.budget import BudgetAccount, Budgets, MeteredProvider, Pricing
from reharness.memory.bridge import (MAX_BODY, RESEARCH_SECONDS, SDK_READ_SECONDS, Bridge, BridgeError, map_messages,
                                    usage_of)
from reharness.providers import ProviderAccountError, ProviderError
from reharness.runtime.chat import ChatTurn, ToolCall

PRICING = Pricing(0.003, 0.015, 4000, 1000, source="test", cache_read_per_1k=0.0003, cache_write_per_1k=0.00375)
SCOPE = "act-mem"
USAGE = {"input_tokens": 11, "output_tokens": 40, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 60}


def fenced(obj) -> str:
    return "```json\n" + json.dumps(obj, ensure_ascii=False) + "\n```"


# ReMe's prompt templates, by a phrase each one contains, the op a flow names its LLM config after, and the reply the
# scripted model gives.
ROUTES = [
    ("reviewing successful step sequences", "success",
     fenced([{"when_to_use": "When a task asks to email every customer whose order shipped late",
              "experience": "List orders first, then send one email per distinct customer.",
              "tags": ["email"], "confidence": 0.9, "step_type": "action", "tools_used": ["orders.list"]}])),
    ("reviewing failed step sequences", "failure",
     fenced([{"when_to_use": "When a task says 'last week' in a sandbox world",
              "experience": "Derive today from the world's latest record, not the system clock.",
              "tags": ["dates"], "confidence": 0.8, "step_type": "reasoning", "tools_used": []}])),
    ("comparing higher-scoring and lower-scoring", "comparative",
     fenced([{"when_to_use": "When some entities must be left untouched", "experience": "Read the exclusions first.",
              "tags": ["exclusions"], "confidence": 0.85, "step_type": "decision", "tools_used": []}])),
    ("comparing successful and failed step sequences", "comparative",
     fenced([{"when_to_use": "When one run succeeded and one failed", "experience": "Verify recipients.",
              "tags": [], "confidence": 0.8, "step_type": "decision"}])),
    ("validating the quality and usefulness", "validation", fenced({"is_valid": True, "score": 0.8,
                                                                    "feedback": "actionable"})),
    ("reranking retrieved experiences", "rerank", fenced({"ranked_indices": [0], "reasoning": "scripted"})),
    ("rewriting and reorganizing context content", "rewrite", fenced({"rewritten_context": "check exclusions"})),
]
MODELS = ["research/reme"] + sorted({f"research/reme:{op}" for _, op, _ in ROUTES})
QUERY = "Email every customer whose order shipped late last week and do not contact accounts on the exclusion list."
STEPS = ("### step.0 role=user content=\n" + QUERY + "\n\n### step.1 role=assistant content=\nI will list the orders "
         "first.\n - tool call=orders_list\n   params={\"since\": \"2026-01-05\"}\n\n### step.2 role=tool tool call "
         "result=\n[{\"id\": 7, \"customer\": \"a@x.com\", \"late\": true}]\n")
TEMPLATES = {
    "success": "You are an expert AI analyst reviewing successful step sequences from an AI agent execution.",
    "failure": "You are an expert AI analyst reviewing failed step sequences from an AI agent execution.",
    "comparative": "You are an expert AI analyst comparing higher-scoring and lower-scoring step sequences.",
    "comparative2": "You are an expert AI analyst comparing successful and failed step sequences to extract lessons.",
    "validation": "You are an expert AI analyst tasked with validating the quality and usefulness of task memories.",
    "rerank": "You are an expert AI analyst tasked with reranking retrieved experiences based on relevance.",
    "rewrite": "You are an expert AI assistant tasked with rewriting and reorganizing context content.",
}
# what ReMe's openai backend sends: model, messages (one user message), stream: true, tools: null; the model name is
# the flow's own ("fake-model" in the smoke), replaced per op by the test
REME_REQUESTS = [{"model": "fake-model", "stream": True, "tools": None,
                  "messages": [{"role": "user", "content": f"{head}\n\nEXTRACTION PRINCIPLES:\n● Focus on "
                                                           f"TRANSFERABLE TECHNIQUES\n\n# Original Query\n{QUERY}\n\n"
                                                           f"# Step Sequence Analysis\n{STEPS}"}]}
                 for head in TEMPLATES.values()] + \
                [{"model": "fake-model", "stream": True, "tools": None,
                  "messages": [{"role": "user", "content": "# Execution Process\n" + STEPS}]}]


def route(prompt: str) -> tuple[str | None, str]:
    for phrase, op, reply in ROUTES:
        if phrase in prompt:
            return op, reply
    return None, "ok"


class ScriptedReMeModel:
    """The research model, scripted: ReMe's reply format for the template it is sent. Records what it got."""
    name, model = "scripted-research", "scripted-research-1"

    def __init__(self, *, gate: threading.Event | None = None, fail=None, usage=None, tool_calls=False):
        self.gate, self.fail, self.usage, self.tool_calls = gate, fail, usage or USAGE, tool_calls
        self.seen: list[dict] = []
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def chat(self, system, messages, specs, effort=None):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.seen.append({"system": system, "messages": [dict(m) for m in messages],
                              "specs": list(specs), "effort": effort})
        try:
            if self.gate is not None:
                assert self.gate.wait(10)
            if self.fail is not None:
                raise self.fail
            prompt = "\n".join(str(m.get("content") or m.get("text") or "") for m in messages)
            _, text = route(prompt)
            calls = [ToolCall("t1", "x", {})] if self.tool_calls else []
            return ChatTurn(text, calls, "tool_use" if calls else "end", usage=dict(self.usage))
        finally:
            with self._lock:
                self.active -= 1


class Rows:
    """The scope's record sink (record(kind, data)), kept in memory."""

    def __init__(self):
        self.rows: list[dict] = []
        self.kinds: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, kind, data):
        with self._lock:
            self.kinds.append(kind)
            self.rows.append(json.loads(json.dumps(data)))


@pytest.fixture
def bridge():
    b = Bridge().start()
    yield b
    b.stop()


def account(tmp_path, **limits):
    return Budgets(tmp_path).account(SCOPE, limits or {"usd": 50.0, "model_calls": 1000})


def scope_on(bridge, tmp_path, model=None, *, limits=None, max_concurrency=4, effort_cap="medium", step="s0",
             record=None):
    model = model or ScriptedReMeModel()
    meter = MeteredProvider(model, account(tmp_path, **(limits or {})), PRICING, operation=f"{SCOPE}:research:reme",
                            max_retries=0)
    rows = record if record is not None else Rows()
    scope = bridge.open_scope("research", scope_id=SCOPE, step=step, provider=meter, record=rows, models=MODELS,
                              effort_cap=effort_cap, max_concurrency=max_concurrency)
    return scope, model, rows


def post(scope, body, token=None, headers=None, **kw):
    return httpx.post(f"{scope.base_url}/chat/completions", json=body, timeout=30, trust_env=False,
                      headers={"Authorization": f"Bearer {scope.token if token is None else token}", **(headers or {})},
                      **kw)


def sse(response) -> list:
    """The SSE body as a list of decoded `data:` payloads, [DONE] as the string."""
    assert response.headers["content-type"].startswith("text/event-stream")
    out = []
    for event in response.text.split("\n\n"):
        if not event.strip():
            continue
        assert event.startswith("data: "), event
        payload = event[len("data: "):]
        out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


def book(tmp_path) -> dict:
    return json.loads((tmp_path / "rsi" / "budget" / "book.json").read_text())


def reservations(tmp_path) -> list[dict]:
    return sorted(book(tmp_path)["reservations"].values(), key=lambda r: r["ts"])


BODY = {"model": "research/reme", "messages": [{"role": "user", "content": "x"}]}


# ---------------------------------------------------------------------------- ReMe's requests
def test_every_reme_shaped_request_is_served_streamed_booked_and_recorded(bridge, tmp_path):
    scope, model, rows = scope_on(bridge, tmp_path)
    wanted = []
    for body in REME_REQUESTS:
        op, text = route(body["messages"][0]["content"])
        name = f"research/reme:{op}" if op else "research/reme"
        response = post(scope, {**body, "model": name})
        assert response.status_code == 200, response.text
        assert "x-should-retry" not in response.headers
        events = sse(response)
        assert events[-1] == "[DONE]"
        assert "".join(c["choices"][0]["delta"].get("content") or "" for c in events[:-1] if c["choices"]) == text
        assert events[-2]["choices"][0]["finish_reason"] == "stop"
        wanted.append((op, name, text))
    # the model got each prompt exactly as ReMe wrote it (UTF-8 intact): one user message, no system, no tools
    assert [s["messages"] for s in model.seen] == [[{"role": "user", "content": b["messages"][0]["content"]}]
                                                   for b in REME_REQUESTS]
    assert all(s["system"] == "" and s["specs"] == [] for s in model.seen)
    # one record per call, all 200, with the op it was asked under and the reply it got; the messages whole
    assert rows.kinds == ["bridge_call"] * len(REME_REQUESTS)
    assert [(r["op"], r["model"], r["response"]["text"], r["status"]) for r in rows.rows] == \
        [(op, name, text, 200) for op, name, text in wanted]
    assert all(r["messages"] == b["messages"] and r["stream"] is True and r["error"] is None
               for r, b in zip(rows.rows, REME_REQUESTS))
    assert all(r["usage"] == USAGE and r["cost"] == PRICING.measure(USAGE) for r in rows.rows)
    assert [c["status"] for c in scope.calls()] == [200] * len(REME_REQUESTS)
    # every call reserved and settled on the real book at what it measured, each under its op's label
    held = reservations(tmp_path)
    assert len(held) == len(REME_REQUESTS) and {r["status"] for r in held} == {"settled"}
    assert all(r["measured"] == PRICING.measure(USAGE) for r in held)
    for (op, _, _), r in zip(wanted, held):
        label = f"{SCOPE}:research:reme" + (f":{op}" if op else "")
        assert r["operation_id"].startswith(label + "#"), (op, r["operation_id"])
    assert {"success", "failure", "comparative", "validation", "rerank", "rewrite", None} == {o for o, _, _ in wanted}
    # the meter's own label is back once the calls are done
    assert scope.provider.operation == f"{SCOPE}:research:reme"


def test_the_stream_is_role_content_finish_usage_done_and_the_usage_adds_the_cached_input(bridge, tmp_path):
    scope, _, rows = scope_on(bridge, tmp_path)
    body = {"model": "research/reme:success", "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "reviewing successful step sequences: ..."}], "tools": None}
    response = post(scope, body)
    assert response.headers["connection"] == "close" and response.headers["cache-control"] == "no-cache"
    events = sse(response)
    role, content, finish, usage, done = events
    assert done == "[DONE]"
    assert role["object"] == content["object"] == finish["object"] == usage["object"] == "chat.completion.chunk"
    assert len({e["id"] for e in events[:-1]}) == 1 and role["id"] == rows.rows[0]["call_id"]
    assert role["id"].startswith("chatcmpl-") and role["model"] == "research/reme:success"
    assert role["choices"][0]["delta"]["role"] == "assistant" and role["choices"][0]["delta"]["content"] == ""
    assert content["choices"][0]["delta"] == {"content": ROUTES[0][2]}
    assert finish["choices"][0]["delta"] == {} and finish["choices"][0]["finish_reason"] == "stop"
    assert role["usage"] is content["usage"] is finish["usage"] is None
    assert usage["choices"] == []
    prompt = USAGE["input_tokens"] + USAGE["cache_read_input_tokens"] + USAGE["cache_creation_input_tokens"]
    assert usage["usage"] == {"prompt_tokens": prompt, "completion_tokens": USAGE["output_tokens"],
                              "total_tokens": prompt + USAGE["output_tokens"],
                              "prompt_tokens_details": {"cached_tokens": USAGE["cache_read_input_tokens"]}}
    # without include_usage there is no usage chunk and no usage key on the chunks
    plain = sse(post(scope, {**body, "stream_options": None}))
    assert len(plain) == 4 and all("usage" not in e for e in plain[:-1])
    assert len(sse(post(scope, {**body, "stream_options": {"include_usage": False}}))) == 4
    # not streamed (stream false, or absent, or null): one chat.completion with the same text and usage
    for stream in ({"stream": False}, {"stream": None}):
        whole = post(scope, {**body, **stream})
        assert whole.headers["content-type"] == "application/json"
        whole = whole.json()
        assert whole["object"] == "chat.completion" and whole["id"].startswith("chatcmpl-")
        assert whole["model"] == "research/reme:success" and isinstance(whole["created"], int)
        assert whole["choices"] == [{"index": 0, "message": {"role": "assistant", "content": ROUTES[0][2],
                                                             "refusal": None},
                                     "finish_reason": "stop", "logprobs": None}]
        assert whole["usage"] == usage["usage"]
    absent = post(scope, {k: v for k, v in body.items() if k != "stream"}).json()
    assert absent["object"] == "chat.completion"
    assert rows.rows[0]["usage"] == USAGE and rows.rows[0]["cost"] == PRICING.measure(USAGE)
    assert [r["stream"] for r in rows.rows] == [True, True, True, False, False, False]


@pytest.mark.parametrize("stop, want", [("max_tokens", "length"), ("refusal", "content_filter"), ("end", "stop"),
                                        ("other", "stop")])
def test_finish_reasons_follow_the_providers_stop(bridge, tmp_path, stop, want):
    class Stopping(ScriptedReMeModel):
        def chat(self, system, messages, specs, effort=None):
            return ChatTurn("partial" if stop != "refusal" else None, [], stop, usage=dict(USAGE))
    scope, _, rows = scope_on(bridge, tmp_path, Stopping())
    reply = post(scope, BODY).json()
    assert reply["choices"][0]["finish_reason"] == want
    assert reply["choices"][0]["message"]["content"] == ("partial" if stop != "refusal" else "")
    streamed = sse(post(scope, {**BODY, "stream": True}))
    assert streamed[2]["choices"][0]["finish_reason"] == want
    assert rows.rows[0]["response"] == {"text": reply["choices"][0]["message"]["content"], "stop": stop,
                                        "finish_reason": want}


def test_usage_of_reports_openais_vocabulary_from_either_providers_usage():
    assert usage_of(USAGE) == {"prompt_tokens": 971, "completion_tokens": 40, "total_tokens": 1011,
                               "prompt_tokens_details": {"cached_tokens": 900}}
    # an OpenAI-shaped usage keeps its prompt_tokens (the cached part is inside it already)
    assert usage_of({"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 30}}) \
        == {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
            "prompt_tokens_details": {"cached_tokens": 30}}
    assert usage_of(None) == usage_of({}) == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                                              "prompt_tokens_details": {"cached_tokens": 0}}


# ---------------------------------------------------------------------------- messages and fields
def test_messages_map_to_the_gateway_shapes_and_unread_fields_are_recorded_not_forwarded(bridge, tmp_path):
    scope, model, rows = scope_on(bridge, tmp_path)
    body = {"model": "research/reme", "temperature": 0.2, "top_p": 0.9, "seed": 7, "max_tokens": 900,
            "reasoning_effort": "max", "tool_choice": "auto", "tools": [], "functions": [], "n": 1,
            "messages": [{"role": "system", "content": "You extract memories."},
                         {"role": "user", "content": [{"type": "text", "text": "part one"},
                                                      {"type": "text", "text": "part two"}]},
                         {"role": "assistant", "content": "an earlier answer"},
                         {"role": "developer", "content": "keep it short"},
                         {"role": "assistant", "content": None},
                         {"role": "system", "content": [{"type": "text", "text": "a sub-agent's prompt"}]},
                         {"role": "user", "content": "go on"}]}
    assert post(scope, body).status_code == 200
    (seen,) = model.seen
    assert seen["system"] == "You extract memories."
    assert seen["messages"] == [{"role": "user", "content": "part one\npart two"},
                                {"role": "assistant", "text": "an earlier answer", "tool_calls": []},
                                {"role": "user", "content": "[instructions]\nkeep it short"},
                                {"role": "assistant", "text": "", "tool_calls": []},
                                {"role": "user", "content": "[instructions]\na sub-agent's prompt"},
                                {"role": "user", "content": "go on"}]
    # reasoning_effort is capped at the scope's ceiling and given to a provider that takes it; nothing else is
    assert seen["effort"] == "medium" and seen["specs"] == []
    (row,) = rows.rows
    assert row["extra_fields"] == {"temperature": 0.2, "top_p": 0.9, "seed": 7}
    assert row["effort"] == {"requested": "max", "used": "medium"} and row["max_tokens"] == 900
    assert row["messages"] == body["messages"]
    assert row["request_digest"] == hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False)
                                                   .encode("utf-8")).hexdigest()
    # max_completion_tokens is read before max_tokens; a developer message first is the system
    post(scope, {"model": "research/reme", "max_completion_tokens": 300, "max_tokens": 900,
                 "messages": [{"role": "developer", "content": "be brief"}, {"role": "user", "content": "x"}]})
    assert rows.rows[1]["max_tokens"] == 300
    assert model.seen[1]["system"] == "be brief" and model.seen[1]["messages"] == [{"role": "user", "content": "x"}]
    # the same body twice has one digest, another body another
    post(scope, body)
    assert rows.rows[2]["request_digest"] == row["request_digest"] != rows.rows[1]["request_digest"]


def test_map_messages_refuses_what_this_scope_does_not_serve():
    assert map_messages([{"role": "user", "content": None}]) == [{"role": "user", "content": ""}]
    for messages, code, words in (
            ([{"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}], "invalid_request",
             "'input_audio' part"),
            ([{"role": "user", "content": [{"type": "image_url", "text": "a caption", "image_url": {"url": "x"}}]}],
             "invalid_request", "'image_url' part"),
            ([{"role": "user", "content": ["plain string part"]}], "invalid_request", "'str' part"),
            ([{"role": "user", "content": [{"type": "text", "text": 3}]}], "invalid_request", "'text' part"),
            ([{"role": "user", "content": 5}], "invalid_request", "not int"),
            ([{"role": "user", "content": "x"}, {"role": "assistant", "function_call": {"name": "f"}}],
             "tools_not_served", "tool calls"),
            ([{"role": "user", "content": "x"}, {"role": "function", "name": "f", "content": "r"}], "tools_not_served",
             "'function'"),
            (["x"], "invalid_request", "is an object"),
            ({"role": "user"}, "invalid_request", "non-empty list"),
            ([{"role": "developer", "content": "only instructions"}], "invalid_request", "no user or assistant")):
        with pytest.raises(BridgeError) as caught:
            map_messages(messages)
        assert caught.value.status == 400 and caught.value.code == code, messages
        assert words in str(caught.value), (words, str(caught.value))


@pytest.mark.parametrize("change, status, code", [
    ({"tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]}, 400, "tools_not_served"),
    ({"functions": [{"name": "f"}]}, 400, "tools_not_served"),
    ({"tool_choice": "required"}, 400, "tools_not_served"),
    ({"tool_choice": {"type": "function", "function": {"name": "f"}}}, 400, "tools_not_served"),
    ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]}, 400,
     "invalid_request"),
    ({"messages": [{"role": "user", "content": "x"},
                   {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}]},
     400, "tools_not_served"),
    ({"messages": [{"role": "user", "content": "x"}, {"role": "tool", "tool_call_id": "1", "content": "r"}]}, 400,
     "tools_not_served"),
    ({"messages": "x"}, 400, "invalid_request"),
    ({"messages": []}, 400, "invalid_request"),
    ({"messages": [{"role": "system", "content": "only a system"}]}, 400, "invalid_request"),
    ({"messages": [{"role": "critic", "content": "x"}]}, 400, "invalid_request"),
    ({"stream": "yes"}, 400, "invalid_request"),
    ({"stream_options": "usage"}, 400, "invalid_request"),
    ({"stream_options": {"include_usage": "yes"}}, 400, "invalid_request"),
    ({"n": 2}, 400, "invalid_request"),
    ({"reasoning_effort": "extreme"}, 400, "invalid_request"),
    ({"max_tokens": 0}, 400, "invalid_request"),
    ({"max_tokens": True}, 400, "invalid_request"),
    ({"max_completion_tokens": 1.5}, 400, "invalid_request"),
    ({"model": "research/other"}, 404, "model_not_found"),
    ({"model": "gpt-4o"}, 404, "model_not_found"),
    ({"model": None}, 404, "model_not_found"),
])
def test_requests_this_scope_does_not_serve_are_refused_and_recorded_and_the_model_is_never_called(
        bridge, tmp_path, change, status, code):
    scope, model, rows = scope_on(bridge, tmp_path)
    body = {"model": "research/reme", "messages": [{"role": "user", "content": "x"}], "stream": True, **change}
    response = post(scope, body)
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    error = response.json()["error"]
    assert error["code"] == code and error["type"] == "invalid_request_error" and error["message"]
    assert error["param"] is None
    if code == "tools_not_served" and "tools" in change:
        assert error["message"] == "tools are not served on this scope"
    assert model.seen == []
    (row,) = rows.rows
    assert row["status"] == status and row["error"]["code"] == code and row["usage"] is None
    assert row["error"]["message"] == error["message"] and row["response"] is None and row["cost"] is None
    assert row["messages"] == body["messages"]
    assert [c["status"] for c in scope.calls()] == [status]
    assert book(tmp_path)["reservations"] == {}


def test_every_mapped_request_passes_the_gateways_malformed_check_before_the_provider(bridge, tmp_path,
                                                                                     monkeypatch):
    """map_messages already refuses every shape it does not serve; gateway.malformed is checked on what it produced
    all the same, and a problem it finds is a 400 before the provider is called."""
    scope, model, rows = scope_on(bridge, tmp_path)
    real, checked = B.malformed, []

    def spy(messages, tools):
        checked.append((messages, tools))
        return "a scripted problem" if any("broken" in str(m.get("content")) for m in messages) else \
            real(messages, tools)
    monkeypatch.setattr(B, "malformed", spy)
    assert post(scope, BODY).status_code == 200
    assert checked == [([{"role": "user", "content": "x"}], [])]
    refused = post(scope, {**BODY, "messages": [{"role": "user", "content": "broken"}]})
    assert refused.status_code == 400
    assert refused.json()["error"]["message"] == "malformed model call: a scripted problem"
    assert len(model.seen) == 1 and [r["status"] for r in rows.rows] == [200, 400]
    assert len(book(tmp_path)["reservations"]) == 1


def test_a_body_that_is_not_json_or_not_an_object_an_unknown_path_and_another_method_are_answered(bridge, tmp_path):
    scope, model, rows = scope_on(bridge, tmp_path)
    headers = {"Authorization": f"Bearer {scope.token}", "Content-Type": "application/json"}
    url = f"{scope.base_url}/chat/completions"
    bad = httpx.post(url, content=b"{not json", headers=headers, trust_env=False)
    assert bad.status_code == 400 and "not JSON" in bad.json()["error"]["message"]
    assert rows.rows[-1]["status"] == 400 and rows.rows[-1]["messages"] is None
    latin = httpx.post(url, content=b"\xff\xfe", headers=headers, trust_env=False)
    assert latin.status_code == 400 and "not JSON" in latin.json()["error"]["message"]
    listed = httpx.post(url, content=b"[1, 2]", headers=headers, trust_env=False)
    assert listed.status_code == 400 and "JSON object, not list" in listed.json()["error"]["message"]
    assert rows.rows[-1]["extra_fields"] == {}
    empty = httpx.post(url, content=b"", headers=headers, trust_env=False)
    assert empty.status_code == 400
    assert [r["status"] for r in rows.rows] == [400, 400, 400, 400]
    # paths and methods not served: answered, not recorded (nothing was served)
    assert httpx.post(f"{scope.base_url}/embeddings", json={}, headers=headers, trust_env=False).status_code == 404
    assert httpx.post(f"{scope.base_url}/models", json={}, headers=headers, trust_env=False).status_code == 404
    assert httpx.post(f"{url}?x=1", json=BODY, headers=headers, trust_env=False).status_code == 200
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        answer = httpx.request(method, url, headers=headers, trust_env=False)
        assert answer.status_code == 405 and answer.json()["error"]["code"] == "method_not_allowed", method
        other = httpx.request(method, f"{scope.base_url}/models", headers=headers, trust_env=False)
        assert other.status_code == 404 and other.json()["error"]["code"] == "not_found", method
    assert len(model.seen) == 1 and len(rows.rows) == 5


# ---------------------------------------------------------------------------- tokens, budget, provider failures
def test_an_unknown_or_closed_token_is_401_and_nothing_is_recorded_or_called(bridge, tmp_path):
    scope, model, rows = scope_on(bridge, tmp_path)
    for token in ("not-a-token", scope.token + "x", scope.token[:-1], scope.token.upper()):
        response = post(scope, BODY, token=token)
        assert response.status_code == 401, token
        error = response.json()["error"]
        assert error["type"] == "authentication_error" and error["code"] == "invalid_api_key"
    url = f"{scope.base_url}/chat/completions"
    assert httpx.post(url, json=BODY, trust_env=False).status_code == 401
    basic = httpx.post(url, json=BODY, headers={"Authorization": f"Basic {scope.token}"}, trust_env=False)
    assert basic.status_code == 401
    bare = httpx.post(url, json=BODY, headers={"Authorization": scope.token}, trust_env=False)
    assert bare.status_code == 401
    assert rows.rows == [] and model.seen == []
    # the scheme is case-insensitive, as HTTP's are
    lower = httpx.post(url, json=BODY, headers={"Authorization": f"bearer {scope.token}"}, trust_env=False)
    assert lower.status_code == 200
    assert post(scope, BODY).status_code == 200
    scope.close()
    assert scope.closed and scope.closed_reason == "closed"
    assert post(scope, BODY).status_code == 401
    assert len(model.seen) == 2 and len(rows.rows) == 2


def test_two_scopes_on_one_bridge_have_their_own_tokens_models_and_records(bridge, tmp_path):
    first, first_model, first_rows = scope_on(bridge, tmp_path / "a")
    meter = MeteredProvider(ScriptedReMeModel(), account(tmp_path / "b"), PRICING, max_retries=0)
    other_rows = Rows()
    second = bridge.open_scope("research", scope_id="other", step="t", provider=meter, record=other_rows,
                               models=["research/other"], effort_cap=None, max_concurrency=1)
    assert first.token != second.token and first.base_url == second.base_url
    assert post(first, {**BODY, "model": "research/other"}).status_code == 404
    assert post(second, {**BODY, "model": "research/other"}).status_code == 200
    second.close()
    assert post(first, BODY).status_code == 200                # closing one scope leaves the other's token valid
    assert [r["scope_id"] for r in first_rows.rows] == [SCOPE, SCOPE]
    assert [r["scope_id"] for r in other_rows.rows] == ["other"]


def test_a_refused_reservation_is_429_budget_exhausted_and_nothing_is_booked(bridge, tmp_path):
    # one call's bound is $0.03 (4000 input at the dearest input rate + 1000 output tokens): $0.01 fits none
    scope, model, rows = scope_on(bridge, tmp_path, limits={"usd": 0.01, "model_calls": 10})
    before = book(tmp_path)
    response = post(scope, {"model": "research/reme:success", "messages": [{"role": "user", "content": "x"}],
                            "stream": True})
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["type"] == "budget_exhausted" and error["code"] == "budget_exhausted"
    assert model.seen == []
    after = book(tmp_path)
    assert after["reservations"] == before["reservations"] == {}
    assert rows.rows[0]["status"] == 429 and rows.rows[0]["cost"] is None and rows.rows[0]["op"] == "success"
    assert not scope.closed                      # a spent budget is not the account: the scope stays open
    assert scope.provider.operation == f"{SCOPE}:research:reme"


def test_a_reservation_the_book_cannot_make_is_429_budget_mismatch(bridge, tmp_path):
    model = ScriptedReMeModel()
    Budgets(tmp_path).account(SCOPE, {"usd": 1.0})
    ghost = MeteredProvider(model, BudgetAccount(Budgets(tmp_path), "no-such-scope"), PRICING, max_retries=0)
    rows = Rows()
    scope = bridge.open_scope("research", scope_id=SCOPE, step=None, provider=ghost, record=rows, models=MODELS,
                              effort_cap="medium", max_concurrency=1)
    response = post(scope, BODY)
    assert response.status_code == 429
    assert response.json()["error"] == {**response.json()["error"], "type": "budget_exhausted",
                                        "code": "budget_mismatch"}
    assert model.seen == [] and book(tmp_path)["reservations"] == {} and rows.rows[0]["status"] == 429


def test_an_account_class_failure_is_502_account_and_closes_the_scope(bridge, tmp_path):
    model = ScriptedReMeModel(fail=ProviderAccountError("claude CLI: You've hit your session limit · resets 3pm"))
    scope, _, rows = scope_on(bridge, tmp_path, model)
    body = {"model": "research/reme:validation", "messages": [{"role": "user", "content": "x"}], "stream": True}
    response = post(scope, body)
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["type"] == "provider_error" and error["code"] == "account" and "session limit" in error["message"]
    assert scope.closed and scope.closed_reason == "account"
    assert post(scope, body).status_code == 401              # ReMe's retry meets a closed scope, not the account
    assert len(model.seen) == 1
    assert [c["status"] for c in scope.calls()] == [502] and rows.rows[0]["error"]["code"] == "account"
    # the meter released the reservation: the account refused before any work
    assert [r["status"] for r in book(tmp_path)["reservations"].values()] == ["released"]
    # an SDK error carrying a 402 is the account's too
    billing = Exception("payment required")
    billing.status_code = 402
    other, _, other_rows = scope_on(bridge, tmp_path / "402", ScriptedReMeModel(fail=billing))
    assert post(other, BODY).json()["error"]["code"] == "account" and other.closed_reason == "account"


def test_another_provider_failure_or_tool_calls_are_502_provider_error_and_the_scope_stays_open(bridge, tmp_path):
    for fail in (ProviderError("claude CLI: exit 1", retryable=True), RuntimeError("the socket broke")):
        root = tmp_path / type(fail).__name__
        scope, model, rows = scope_on(bridge, root, ScriptedReMeModel(fail=fail))
        response = post(scope, BODY)
        assert response.status_code == 502
        error = response.json()["error"]
        assert error["type"] == "provider_error" and error["code"] == "provider_error"
        assert str(fail) in error["message"]
        assert not scope.closed and len(rows.rows) == 1 and rows.rows[0]["error"]["code"] == "provider_error"
        # the bridge does not retry and neither does its meter: one provider call for one request, booked as a
        # call that may have been charged
        assert len(model.seen) == 1
        assert [r["status"] for r in reservations(root)] == ["unknown"]
        assert post(scope, BODY).status_code == 502 and len(model.seen) == 2      # the next request is served
    scope, model, rows = scope_on(bridge, tmp_path / "tools", ScriptedReMeModel(tool_calls=True))
    response = post(scope, BODY)
    assert response.status_code == 502 and "tool calls" in response.json()["error"]["message"]
    assert response.json()["error"]["code"] == "provider_error" and not scope.closed
    assert rows.rows[0]["usage"] == USAGE and rows.rows[0]["cost"] == PRICING.measure(USAGE)  # the model did answer
    assert [r["status"] for r in reservations(tmp_path / "tools")] == ["settled"]


def test_open_scope_refuses_a_provider_that_is_not_metered_or_that_retries(bridge, tmp_path):
    model = ScriptedReMeModel()
    base = dict(scope_id=SCOPE, step=None, record=Rows(), models=MODELS, effort_cap="medium", max_concurrency=2)

    def metered(**kw):
        return MeteredProvider(model, account(tmp_path), PRICING, **kw)
    with pytest.raises(TypeError, match="MeteredProvider"):
        bridge.open_scope("research", provider=model, **base)

    class Unbooked:                                   # a meter's shape with no account to book on
        chat, pricing, operation, max_retries = model.chat, PRICING, "research", 0
    with pytest.raises(TypeError, match="MeteredProvider"):
        bridge.open_scope("research", provider=Unbooked(), **base)
    with pytest.raises(ValueError, match="never retries"):
        bridge.open_scope("research", provider=metered(max_retries=1), **base)
    with pytest.raises(ValueError, match="never retries"):
        bridge.open_scope("research", provider=metered(), **base)            # the meter's default retries once

    class Retrying(ScriptedReMeModel):
        retries = 2                                   # a provider that retries makes its meter retry
    with pytest.raises(ValueError, match="never retries"):
        bridge.open_scope("research", provider=MeteredProvider(Retrying(), account(tmp_path), PRICING,
                                                                max_retries=0), **base)
    ok = metered(max_retries=0)
    with pytest.raises(ValueError, match="research"):
        bridge.open_scope("attempt", provider=ok, **base)
    with pytest.raises(ValueError, match="research/<library>"):
        bridge.open_scope("research", provider=ok, **{**base, "models": ["reme"]})
    with pytest.raises(ValueError, match="research/<library>"):
        bridge.open_scope("research", provider=ok, **{**base, "models": ["attempt/reme"]})
    with pytest.raises(ValueError, match="research/<library>"):
        bridge.open_scope("research", provider=ok, **{**base, "models": [7]})
    with pytest.raises(ValueError, match="at least one model"):
        bridge.open_scope("research", provider=ok, **{**base, "models": []})
    with pytest.raises(ValueError, match="effort_cap"):
        bridge.open_scope("research", provider=ok, **{**base, "effort_cap": "huge"})
    for bad in (0, True, 1.5):
        with pytest.raises(ValueError, match="max_concurrency"):
            bridge.open_scope("research", provider=ok, **{**base, "max_concurrency": bad})
    with pytest.raises(TypeError, match="record"):
        bridge.open_scope("research", provider=ok, **{**base, "record": 5})
    with pytest.raises(ValueError, match="scope_id"):
        bridge.open_scope("research", provider=ok, **{**base, "scope_id": ""})
    with pytest.raises(RuntimeError, match="start the bridge"):
        Bridge().open_scope("research", provider=ok, **base)
    # what is refused is never registered: only the accepted scope's token is served
    accepted = bridge.open_scope("research", provider=ok, **base)
    assert list(bridge._scopes.values()) == [accepted]


def test_a_call_is_booked_under_its_ops_label_and_the_callers_label_is_restored_after_it(bridge, tmp_path):
    """Scope.serve on the calling thread: during the provider call the meter's label is the op's; after it, success
    or failure, the label that thread had before is back (the meter keeps labels per thread)."""
    labels = []

    class Labelled(ScriptedReMeModel):
        def chat(self, system, messages, specs, effort=None):
            labels.append(scope.provider.operation)
            return super().chat(system, messages, specs, effort)
    model = Labelled()
    scope, _, rows = scope_on(bridge, tmp_path, model)
    meter = scope.provider
    meter.operation = "host:label"                     # what the host's own thread books under
    status, answer, view = scope.serve({"model": "research/reme:validation", "stream": True,
                                        "messages": [{"role": "user", "content": TEMPLATES["validation"]}]})
    assert status == 200 and answer["object"] == "chat.completion"
    assert view == {"stream": True, "include_usage": False, "model": "research/reme:validation",
                    "call_id": answer["id"], "created": answer["created"], "text": ROUTES[4][2], "finish": "stop",
                    "usage": usage_of(USAGE)}
    assert labels == [f"{SCOPE}:research:reme:validation"] and meter.operation == "host:label"
    model.fail = RuntimeError("down")
    status, answer, view = scope.serve(BODY)
    assert (status, view) == (502, None) and answer["error"]["code"] == "provider_error"
    assert labels[-1] == f"{SCOPE}:research:reme" and meter.operation == "host:label"
    held = reservations(tmp_path)
    assert [r["operation_id"].split("#")[0] for r in held] == [f"{SCOPE}:research:reme:validation",
                                                               f"{SCOPE}:research:reme"]
    # an unreadable body and a client resend are answered by serve too, never raised
    status, answer, view = scope.serve({"_unparsed": "{x"}, unreadable="Expecting property name")
    assert (status, view) == (400, None) and "not JSON: Expecting property name" in answer["error"]["message"]
    assert scope.serve(BODY, client_retry=3)[1]["error"]["code"] == "client_retry_refused"
    assert [r["status"] for r in rows.rows] == [200, 502, 400, 400] and len(model.seen) == 2


# ---------------------------------------------------------------------------- the activity's research provider
class Timed(ScriptedReMeModel):
    """A research model as the claude_code provider is by default: two retries of its own, a 600 s timeout, a pinned
    effort."""
    effort = "medium"

    def __init__(self, timeout=600.0):
        super().__init__()
        self.retries = 2
        self.timeout = timeout


def test_the_activitys_research_provider_without_retries_is_what_the_bridge_serves(tmp_path, fake_upstream):
    import rsi
    research = Timed()
    act = rsi.activity(tmp_path / "act", fake_upstream, research=research)
    base = dict(scope_id=f"{act.scope_id}:memory", step="stream-0", record=Rows(), models=MODELS,
                effort_cap=act.k["model"]["max_effort"], max_concurrency=act.concurrency)
    with Bridge() as bridge:
        # the default builder keeps the provider's retries (the meter takes them over): the bridge refuses it
        retrying = act.research_provider("improve")
        assert isinstance(retrying, MeteredProvider) and retrying.max_retries == 2
        with pytest.raises(ValueError, match="never retries"):
            bridge.open_scope("research", provider=retrying, **base)
        assert research.timeout == 600.0
        # without retries: neither the provider nor the meter retries, the provider's timeout is under the SDK's
        provider = act.research_provider("memory", retries=False)
        assert isinstance(provider, MeteredProvider) and provider.provider is research
        assert provider.max_retries == 0 and research.retries == 0
        assert research.timeout == RESEARCH_SECONDS < SDK_READ_SECONDS
        assert provider.account.scope_id == f"{act.scope_id}:research:memory"
        assert provider.pricing == act.pricing()
        scope = bridge.open_scope("research", provider=provider, **base)
        response = post(scope, {"model": "research/reme:success", "stream": True,
                                "messages": [{"role": "user", "content": TEMPLATES["success"]}]})
        assert response.status_code == 200
        assert "".join(c["choices"][0]["delta"].get("content") or "" for c in sse(response)[:-1]
                       if c["choices"]) == ROUTES[0][2]
    # booked on the activity's own book, under the memory's research account and the op's label
    held = [r for r in act.budgets._read()["reservations"].values()]
    assert len(held) == 1 and held[0]["scope_id"] == f"{act.scope_id}:research:memory"
    assert held[0]["operation_id"] == f"{act.scope_id}:memory:research:reme:success#0.0"
    assert held[0]["status"] == "settled" and held[0]["measured"] == act.pricing().measure(USAGE)
    assert act.budgets._read()["scopes"][f"{act.scope_id}:research:memory"]["parent"] == act.scope_id
    # a timeout K already sets under the SDK's is kept
    quick = Timed(timeout=90)
    assert rsi.activity(tmp_path / "quick", fake_upstream, research=quick).research_provider(
        "memory", retries=False).provider.timeout == 90


FAKE_CLI = """#!{python}
import json, sys
from pathlib import Path
here = Path(__file__).resolve().parent
if sys.argv[1:3] == ["auth", "status"]:
    sys.stdout.write(json.dumps({{"loggedIn": True, "authMethod": "claude.ai"}}))
    sys.exit(0)
replies = json.loads((here / "replies.json").read_text())
reply = replies.pop(0)
(here / "replies.json").write_text(json.dumps(replies))
with open(here / "calls.jsonl", "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}) + "\\n")
sys.stdout.write(json.dumps(reply))
"""
CLI_MODEL = "claude-opus-5-5"
CLI_USAGE = {"input_tokens": 12, "output_tokens": 34, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 6}
FENCED = ('Here are the memories.\n\n```json\n[\n  {\n    "when_to_use": "When an inverse task names entities to leave '
          'untouched",\n    "experience": "Read the exclusion list before any write; skip every excluded account.",\n'
          '    "tags": ["exclusions", "writes"],\n    "confidence": 0.9\n  }\n]\n```\n')


class Cli:
    """A fake `claude` program: answers the replies it is given in order and writes down each call."""

    def __init__(self, root: Path):
        root.mkdir(parents=True)
        self.root, self.binary = root, root / "claude"
        self.binary.write_text(FAKE_CLI.format(python=sys.executable))
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IEXEC)
        self.reply()

    def reply(self, *replies):
        (self.root / "replies.json").write_text(json.dumps(list(replies)))

    def calls(self) -> list[dict]:
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def ok(result: str) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "result": result, "usage": dict(CLI_USAGE),
            "total_cost_usd": 0.0123, "modelUsage": {CLI_MODEL: {"outputTokens": 34}}}


def err(text: str) -> dict:
    return {"type": "result", "subtype": "success", "is_error": True, "result": text,
            "usage": {k: 0 for k in CLI_USAGE}, "total_cost_usd": 0}


def arg(call: dict, flag: str):
    return call["argv"][call["argv"].index(flag) + 1]


def test_the_claude_code_research_model_behind_the_bridge(tmp_path, fake_upstream):
    """The real claude_code provider, as the activity builds it for the memory, in front of a fake `claude`: a fenced
    JSON reply reaches the stream byte for byte, each op is booked under its own label, a requested effort is capped
    on the CLI, and a subscription limit is a 502 `account` that closes the scope after one CLI call."""
    import rsi
    from reharness.providers.claude_code import ClaudeCodeProvider
    cli = Cli(tmp_path / "cli")
    research = ClaudeCodeProvider(CLI_MODEL, effort="medium", binary=str(cli.binary), workdir=str(tmp_path / "empty"),
                                  sleep=lambda _s: None)
    assert (research.retries, research.timeout) == (2, 600.0)      # the provider's defaults
    act = rsi.activity(tmp_path / "act", fake_upstream, research=research)
    provider = act.research_provider("memory", retries=False)
    assert (research.retries, research.timeout, provider.max_retries) == (0, RESEARCH_SECONDS, 0)
    rows = Rows()
    with Bridge() as bridge:
        scope = bridge.open_scope("research", scope_id=f"{act.scope_id}:memory", step="batch-0", provider=provider,
                                  record=rows, models=["research/reme:success", "research/reme:validation"],
                                  effort_cap=act.k["model"]["max_effort"], max_concurrency=2)
        cli.reply(ok(FENCED), ok('```json\n{"is_valid": true, "score": 0.8}\n```'), ok("plain"),
                  err("You've hit your session limit · resets 3pm"))
        prompt = TEMPLATES["success"] + "\n\n# Original Query\n" + QUERY
        response = post(scope, {"model": "research/reme:success", "messages": [{"role": "user", "content": prompt}],
                                "stream": True, "tools": None, "reasoning_effort": "max"})
        assert response.status_code == 200, response.text
        assert "".join(c["choices"][0]["delta"].get("content") or "" for c in sse(response)[:-1]
                       if c["choices"]) == FENCED
        validation = post(scope, {"model": "research/reme:validation",
                                  "messages": [{"role": "user", "content": TEMPLATES["validation"]}]})
        assert validation.json()["choices"][0]["message"]["content"] == '```json\n{"is_valid": true, "score": 0.8}\n```'
        assert post(scope, {"model": "research/reme:success", "reasoning_effort": "low",
                            "messages": [{"role": "user", "content": "x"}]}).status_code == 200
        limited = post(scope, {"model": "research/reme:validation", "stream": True,
                               "messages": [{"role": "user", "content": "x"}]})
        assert limited.status_code == 502 and limited.json()["error"]["code"] == "account"
        assert scope.closed and post(scope, {"model": "research/reme:validation",
                                             "messages": [{"role": "user", "content": "x"}]}).status_code == 401
    first, second, third, fourth = cli.calls()
    # one plain research request: the prompt as it is, no tools, no schema; max capped at K's medium, none asked runs
    # at the provider's pinned medium, low passes as low
    assert first["stdin"] == prompt and "--json-schema" not in first["argv"] and arg(first, "--tools") == ""
    assert [arg(c, "--effort") for c in (first, second, third)] == ["medium", "medium", "low"]
    assert [r["effort"] for r in rows.rows[:3]] == [{"requested": "max", "used": "medium"},
                                                    {"requested": None, "used": "medium"},
                                                    {"requested": "low", "used": "low"}]
    assert rows.rows[0]["op"] == "success" and rows.rows[0]["response"]["text"] == FENCED
    assert {k: rows.rows[0]["usage"][k] for k in CLI_USAGE} == CLI_USAGE and rows.rows[0]["usage"]["cli_cost_usd"] \
        == 0.0123
    held = sorted(act.budgets._read()["reservations"].values(), key=lambda r: r["ts"])
    assert [r["operation_id"].split("#")[0] for r in held] == [f"{act.scope_id}:memory:research:reme:{op}" for op in
                                                               ("success", "validation", "success", "validation")]
    assert [r["status"] for r in held] == ["settled", "settled", "settled", "released"]
    assert held[0]["measured"]["input_tokens"] == CLI_USAGE["input_tokens"] + CLI_USAGE["cache_read_input_tokens"] + \
        CLI_USAGE["cache_creation_input_tokens"]
    assert [r["status"] for r in rows.rows] == [200, 200, 200, 502]


# ---------------------------------------------------------------------------- concurrency, steps, records
def wait_for(scope, in_flight: int, waiting: int) -> None:
    deadline = time.monotonic() + 10
    while (scope.in_flight, scope.waiting) != (in_flight, waiting) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (scope.in_flight, scope.waiting) == (in_flight, waiting)


def test_over_max_concurrency_a_request_waits_for_a_slot_and_is_never_refused(bridge, tmp_path):
    gate = threading.Event()
    scope, model, rows = scope_on(bridge, tmp_path, ScriptedReMeModel(gate=gate), max_concurrency=2)
    body = {"model": "research/reme:success", "messages": [{"role": "user", "content": "x"}], "stream": True}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(post, scope, body) for _ in range(3)]
        wait_for(scope, 2, 1)                                       # two at the model, the third queued
        assert model.active == 2 and not any(f.done() for f in futures)
        gate.set()
        statuses = [f.result(timeout=20).status_code for f in futures]
    assert statuses == [200, 200, 200] and model.peak == 2 and len(rows.rows) == 3
    assert (scope.in_flight, scope.waiting) == (0, 0)
    # a slot is given back after a failure too
    failing, failing_model, _ = scope_on(bridge, tmp_path / "fail", ScriptedReMeModel(fail=RuntimeError("x")),
                                         max_concurrency=1)
    assert [post(failing, BODY).status_code for _ in range(3)] == [502, 502, 502]
    assert len(failing_model.seen) == 3 and (failing.in_flight, failing.waiting) == (0, 0)


def test_eight_threads_book_exactly_what_the_calls_cost(bridge, tmp_path):
    usages = [{"input_tokens": 10 + i, "output_tokens": 20 + 3 * i, "cache_read_input_tokens": 100 * i,
               "cache_creation_input_tokens": 7 * i} for i in range(8)]

    class Varied(ScriptedReMeModel):
        def chat(self, system, messages, specs, effort=None):
            i = int(messages[-1]["content"])
            time.sleep(0.05)
            return ChatTurn(f"reply {i}", [], "end", usage=dict(usages[i]))
    scope, _, rows = scope_on(bridge, tmp_path, Varied(), max_concurrency=4)
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(lambda i: post(scope, {"model": "research/reme", "messages": [
            {"role": "user", "content": str(i)}]}), range(8)))
    assert [r.json()["choices"][0]["message"]["content"] for r in replies] == [f"reply {i}" for i in range(8)]
    want = round(sum(PRICING.cost({"input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"],
                                   "cache_read_tokens": u["cache_read_input_tokens"],
                                   "cache_write_tokens": u["cache_creation_input_tokens"]}) for u in usages), 8)
    used = Budgets(tmp_path).usage(SCOPE, book(tmp_path))
    assert round(used["usd"], 8) == want and used["model_calls"] == 8
    assert round(sum(r["cost"]["usd"] for r in rows.rows), 8) == want
    assert used["output_tokens"] == sum(u["output_tokens"] for u in usages)
    assert {r["status"] for r in book(tmp_path)["reservations"].values()} == {"settled"}


def test_set_step_labels_the_next_calls_and_calls_lists_them_by_step(bridge, tmp_path):
    scope, _, rows = scope_on(bridge, tmp_path, step="batch-0")
    assert scope.step == "batch-0"
    post(scope, BODY)
    post(scope, {**BODY, "model": "research/missing"})
    post(scope, BODY, token="wrong")                              # not authenticated: not a call of the scope
    scope.set_step("batch-1")
    assert scope.step == "batch-1"
    post(scope, {**BODY, "model": "research/reme:validation"})
    assert [(c["step"], c["status"], c["op"]) for c in scope.calls()] == [("batch-0", 200, None),
                                                                          ("batch-0", 404, None),
                                                                          ("batch-1", 200, "validation")]
    assert [c["status"] for c in scope.calls("batch-0")] == [200, 404]
    assert [c["status"] for c in scope.calls("batch-1")] == [200]
    assert scope.calls("batch-9") == []
    assert [r["step"] for r in rows.rows] == ["batch-0", "batch-0", "batch-1"]
    assert all(set(c) == {"call_id", "step", "op", "status", "ms"} for c in scope.calls())
    assert all(isinstance(c["ms"], int) and c["ms"] >= 0 for c in scope.calls())
    assert [c["call_id"] for c in scope.calls()] == [r["call_id"] for r in rows.rows]
    assert len({c["call_id"] for c in scope.calls()}) == 3
    # what calls() returns is a copy: the scope's own list is not changed through it
    scope.calls()[0]["status"] = 999
    assert scope.calls()[0]["status"] == 200


def test_a_jsonl_record_path_gets_one_row_per_call_and_the_token_is_in_no_record_answer_book_or_log(
        bridge, tmp_path, capfd):
    sink = tmp_path / "rsi" / "bridge_calls.jsonl"
    scope, _, _ = scope_on(bridge, tmp_path, record=str(sink))
    answers = []
    for body in REME_REQUESTS[:6]:
        answers.append(post(scope, {**body, "model": "research/reme"}).text)
    answers.append(post(scope, {"model": "research/reme", "messages": [{"role": "tool"}]}).text)
    answers.append(post(scope, {"model": "nope", "messages": []}).text)
    answers.append(post(scope, BODY, token=scope.token[:-2]).text)
    answers.append(post(scope, BODY, headers={"x-stainless-retry-count": "1"}).text)
    lines = [json.loads(line) for line in sink.read_text().splitlines()]
    assert len(lines) == 9 and {r["kind"] for r in lines} == {"bridge_call"} and all(r["ts"] for r in lines)
    keys = {"call_id", "scope_id", "step", "op", "model", "request_digest", "messages", "extra_fields", "effort",
            "max_tokens", "stream", "response", "usage", "cost", "elapsed", "status", "error"}
    assert all(set(r) == keys | {"ts", "kind"} for r in lines)
    assert [r["status"] for r in lines] == [200] * 6 + [400, 404, 400]
    assert [r["call_id"] for r in lines] == [c["call_id"] for c in scope.calls()]
    token = scope.token
    for text in (sink.read_text(), json.dumps(scope.calls()), *answers,
                 (tmp_path / "rsi" / "budget" / "book.json").read_text(), repr(scope), repr(bridge.__dict__)):
        assert token not in text
    assert token[:12] not in sink.read_text()
    # no request log: nothing of a request reaches stdout or stderr
    out, errors = capfd.readouterr()
    assert token not in out + errors and "/v1/chat/completions" not in out + errors and "HTTP/1.1" not in errors


# ---------------------------------------------------------------------------- the client is told never to resend
def raw_request(scope, head: str, body: bytes = b"") -> bytes:
    """One request over a plain socket (what httpx will not send: a chunked body, a lying Content-Length)."""
    with socket.create_connection(("127.0.0.1", scope._bridge.port), timeout=10) as s:
        s.sendall(head.encode("ascii") + b"\r\n" + body)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return out
            out += chunk


def test_every_answer_but_a_200_tells_the_client_not_to_resend(bridge, tmp_path, monkeypatch):
    """The openai SDK resends a 408, 409, 429 or 5xx by itself (max_retries=2 by default, which ReMe keeps) unless
    the server says `x-should-retry: false`; every resend would be another paid provider call."""
    scope, _, _ = scope_on(bridge, tmp_path / "ok")
    ok_ = post(scope, BODY)
    assert ok_.status_code == 200 and "x-should-retry" not in ok_.headers
    streamed = post(scope, {**BODY, "stream": True})
    assert streamed.status_code == 200 and "x-should-retry" not in streamed.headers
    answers = {401: post(scope, BODY, token="nope"), 404: post(scope, {**BODY, "model": "research/nope"}),
               400: post(scope, {**BODY, "n": 3}),
               405: httpx.get(f"{scope.base_url}/chat/completions", trust_env=False)}
    budget, _, _ = scope_on(bridge, tmp_path / "budget", limits={"usd": 0.001, "model_calls": 10})
    answers[429] = post(budget, BODY)
    failing, _, _ = scope_on(bridge, tmp_path / "fail", ScriptedReMeModel(fail=ProviderError("exit 1", retryable=True)))
    answers[502] = post(failing, BODY)
    broken, _, _ = scope_on(bridge, tmp_path / "record", record=lambda kind, data: 1 / 0)
    answers[500] = post(broken, BODY)
    for status, response in answers.items():
        assert response.status_code == status, (status, response.text)
        assert response.headers["x-should-retry"] == "false", status
    monkeypatch.setattr(B, "MAX_BODY", 10)
    assert post(scope, BODY).status_code == 413 and post(scope, BODY).headers["x-should-retry"] == "false"
    chunked = raw_request(scope, "POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n",
                          b"0\r\n\r\n")
    assert chunked.startswith(b"HTTP/1.1 411 ") and b"x-should-retry: false" in chunked.lower()


def test_a_request_the_client_resends_by_itself_is_refused_before_the_provider_is_called(bridge, tmp_path):
    scope, model, rows = scope_on(bridge, tmp_path)
    body = {"model": "research/reme:success", "messages": [{"role": "user", "content": "x"}], "stream": True}
    sent = post(scope, body, headers={"x-stainless-retry-count": "0"})
    assert sent.status_code == 200                       # the SDK's first send (and ReMe's own retries) say 0
    for count in ("1", "2", "lots"):
        resent = post(scope, body, headers={"x-stainless-retry-count": count})
        assert resent.status_code == 400, count
        assert resent.json()["error"]["code"] == "client_retry_refused"
        assert resent.headers["x-should-retry"] == "false"
    assert len(model.seen) == 1                          # the resends never reached the model
    assert [r["status"] for r in rows.rows] == [200, 400, 400, 400]
    assert rows.rows[1]["error"]["code"] == "client_retry_refused" and rows.rows[1]["op"] == "success"
    assert len(book(tmp_path)["reservations"]) == 1
    assert post(scope, body).status_code == 200          # no header at all is a first send


# ---------------------------------------------------------------------------- a scope closed while requests wait
def test_requests_waiting_for_a_slot_when_an_account_failure_closes_the_scope_never_reach_the_provider(bridge,
                                                                                                    tmp_path):
    gate = threading.Event()
    model = ScriptedReMeModel(gate=gate, fail=ProviderAccountError("claude CLI: You've hit your session limit"))
    scope, _, rows = scope_on(bridge, tmp_path, model, max_concurrency=1)
    body = {"model": "research/reme:validation", "messages": [{"role": "user", "content": "x"}], "stream": True}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(post, scope, body) for _ in range(3)]
        wait_for(scope, 1, 2)
        gate.set()
        answers = [f.result(timeout=20) for f in futures]
    assert [a.status_code for a in answers] == [502] * 3
    assert {a.json()["error"]["code"] for a in answers} == {"account"}
    assert {a.json()["error"]["type"] for a in answers} == {"provider_error"}
    assert len(model.seen) == 1                       # only the call that held the slot reached the provider
    assert scope.closed and scope.closed_reason == "account"
    assert [r["status"] for r in rows.rows] == [502] * 3
    assert [r["status"] for r in book(tmp_path)["reservations"].values()] == ["released"]
    assert (scope.in_flight, scope.waiting) == (0, 0)


def test_a_request_waiting_when_the_host_closes_the_scope_is_refused_without_a_provider_call(bridge, tmp_path):
    gate = threading.Event()
    model = ScriptedReMeModel(gate=gate)
    scope, _, rows = scope_on(bridge, tmp_path, model, max_concurrency=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(post, scope, BODY) for _ in range(2)]
        wait_for(scope, 1, 1)
        scope.close()
        gate.set()
        answers = sorted((f.result(timeout=20) for f in futures), key=lambda a: a.status_code)
    assert [a.status_code for a in answers] == [200, 401]   # the one at the provider finished; the waiting one did not
    assert answers[1].json()["error"]["code"] == "invalid_api_key"
    assert len(model.seen) == 1 and sorted(r["status"] for r in rows.rows) == [200, 401]
    assert len(book(tmp_path)["reservations"]) == 1


def test_stopping_the_bridge_closes_every_scope_and_is_idempotent(tmp_path):
    bridge = Bridge()
    with pytest.raises(RuntimeError, match="not started"):
        bridge.port
    assert bridge.start() is bridge and bridge.start() is bridge
    port = bridge.port
    assert bridge.base_url == f"http://127.0.0.1:{port}"
    first, _, _ = scope_on(bridge, tmp_path / "a")
    second, _, _ = scope_on(bridge, tmp_path / "b", ScriptedReMeModel(fail=ProviderAccountError("not logged in")))
    assert first.base_url == f"http://127.0.0.1:{port}/v1"
    assert post(second, BODY).status_code == 502 and second.closed_reason == "account"
    bridge.stop()
    assert first.closed and first.closed_reason == "bridge stopped"
    assert second.closed_reason == "account"               # closed before: its reason is kept
    first.close("again")
    assert first.closed_reason == "bridge stopped"
    bridge.stop()
    with pytest.raises(httpx.HTTPError):
        httpx.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=BODY, timeout=2, trust_env=False)
    with pytest.raises(RuntimeError, match="start the bridge"):
        scope_on(bridge, tmp_path / "c")


# ---------------------------------------------------------------------------- the bridge's own failures
def test_a_call_that_cannot_be_recorded_never_reaches_the_client_as_a_success(bridge, tmp_path):
    def broken(kind, data):
        raise OSError("disk full")
    scope, model, _ = scope_on(bridge, tmp_path, record=broken)
    for stream in (False, True):
        response = post(scope, {**BODY, "stream": stream})
        assert response.status_code == 500 and response.headers["content-type"] == "application/json"
        error = response.json()["error"]
        assert error["code"] == "record_failed" and error["type"] == "server_error" and "disk full" in error["message"]
    assert len(model.seen) == 2                       # the provider answered; the answer was not handed on
    assert [c["status"] for c in scope.calls()] == [500, 500]
    assert [r["status"] for r in reservations(tmp_path)] == ["settled", "settled"]     # and was paid for
    # a refusal that cannot be recorded is a 500 too, not the refusal
    assert post(scope, {**BODY, "model": "research/nope"}).json()["error"]["code"] == "record_failed"


def test_the_bridges_own_fault_is_a_recorded_500_and_the_server_stays_up(bridge, tmp_path, monkeypatch):
    scope, model, rows = scope_on(bridge, tmp_path)

    def boom(messages):
        raise RuntimeError("mapping broke")
    monkeypatch.setattr(B, "map_messages", boom)
    response = post(scope, BODY)
    assert response.status_code == 500 and response.headers["x-should-retry"] == "false"
    error = response.json()["error"]
    assert error["code"] == "internal_error" and error["type"] == "server_error"
    assert "RuntimeError: mapping broke" in error["message"]
    assert rows.rows[-1]["status"] == 500 and rows.rows[-1]["error"]["code"] == "internal_error"
    assert model.seen == []
    monkeypatch.undo()
    assert post(scope, BODY).status_code == 200


def test_a_chunked_body_is_411_and_a_body_over_the_limit_is_413_before_anything_is_read(bridge, tmp_path,
                                                                                         monkeypatch):
    scope, model, rows = scope_on(bridge, tmp_path)
    auth = f"Authorization: Bearer {scope.token}\r\n"
    chunked = raw_request(scope, "POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + auth +
                          "Transfer-Encoding: chunked\r\nContent-Type: application/json\r\n",
                          b"5\r\n{\"a\":\r\n0\r\n\r\n")
    assert chunked.startswith(b"HTTP/1.1 411 ") and b"length_required" in chunked
    assert b"x-should-retry: false" in chunked.lower()
    started = time.monotonic()
    # the header claims more than MAX_BODY and no body follows: answered at once, the connection closed, not read
    huge = raw_request(scope, "POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + auth +
                       f"Content-Length: {MAX_BODY + 1}\r\nContent-Type: application/json\r\n")
    assert huge.startswith(b"HTTP/1.1 413 ") and b"request_too_large" in huge
    assert time.monotonic() - started < 5
    for length in ("abc", "-5"):
        lying = raw_request(scope, "POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + auth +
                            f"Content-Length: {length}\r\n")
        assert lying.startswith(b"HTTP/1.1 400 ") and b"Content-Length" in lying, length
    assert model.seen == [] and rows.rows == []        # refused before authentication: nothing to record
    # the limit itself: a body of exactly MAX_BODY bytes is read, one byte more is not (a GET is held to it too)
    body = json.dumps(BODY).encode("utf-8")

    def send(method="POST"):
        return httpx.request(method, f"{scope.base_url}/chat/completions", content=body, trust_env=False,
                             headers={"Authorization": f"Bearer {scope.token}", "Content-Type": "application/json"})
    monkeypatch.setattr(B, "MAX_BODY", len(body))
    assert send().status_code == 200
    monkeypatch.setattr(B, "MAX_BODY", len(body) - 1)
    assert send().status_code == 413 and send("GET").status_code == 413
    assert len(model.seen) == 1 and len(rows.rows) == 1


# ---------------------------------------------------------------------------- effort on the record
class Pinned(ScriptedReMeModel):
    effort = "medium"                              # the provider's own pinned effort (claude_code's `effort`)


class PinnedNoKeyword(Pinned):
    def chat(self, system, messages, specs):       # a provider whose chat takes no effort
        return super().chat(system, messages, specs)


def test_the_record_says_what_effort_each_call_ran_at_even_when_none_was_asked(bridge, tmp_path):
    scope, model, rows = scope_on(bridge, tmp_path / "pinned", Pinned())
    post(scope, BODY)                                              # ReMe never sends reasoning_effort
    post(scope, {**BODY, "reasoning_effort": "low"})
    post(scope, {**BODY, "reasoning_effort": "xhigh"})
    assert [r["effort"] for r in rows.rows] == [{"requested": None, "used": "medium"},
                                                {"requested": "low", "used": "low"},
                                                {"requested": "xhigh", "used": "medium"}]
    assert [s["effort"] for s in model.seen] == [None, "low", "medium"]
    scope, model, rows = scope_on(bridge, tmp_path / "nokw", PinnedNoKeyword())
    post(scope, {**BODY, "reasoning_effort": "low"})               # not passed on: it runs at its own
    assert rows.rows[0]["effort"] == {"requested": "low", "used": "medium"}
    assert model.seen[0]["effort"] is None
    # no cap: a request passes as asked
    scope, model, rows = scope_on(bridge, tmp_path / "uncapped", effort_cap=None)
    post(scope, {**BODY, "reasoning_effort": "max"})
    assert rows.rows[0]["effort"] == {"requested": "max", "used": "max"} and model.seen[0]["effort"] == "max"


def test_the_bridge_is_ipv4_loopback_only():
    for host in ("::1", "0.0.0.0", "10.0.0.1"):
        with pytest.raises(ValueError, match="loopback"):
            Bridge(host)
    assert Bridge("localhost").host == "127.0.0.1"
    with Bridge() as bridge:
        assert bridge._server.server_address[0] == "127.0.0.1"


# ---------------------------------------------------------------------------- the review's case (2026-10-09)
def test_a_request_that_waits_past_max_wait_is_503_slots_busy_and_nothing_is_booked(bridge, tmp_path):
    """The wait for a slot counts against the client's read timeout: past `max_wait` the request is answered 503
    before the provider is called, so the client never times out a request the bridge still serves and pays for."""
    gate = threading.Event()
    model = ScriptedReMeModel(gate=gate)
    meter = MeteredProvider(model, account(tmp_path), PRICING, operation=f"{SCOPE}:research:reme", max_retries=0)
    rows = Rows()
    scope = bridge.open_scope("research", scope_id=SCOPE, step="s0", provider=meter, record=rows, models=MODELS,
                              effort_cap="medium", max_concurrency=1, max_wait=0.2)
    body = {"model": "research/reme:success", "messages": [{"role": "user", "content": "x"}], "stream": True}
    with ThreadPoolExecutor(max_workers=1) as pool:
        held = pool.submit(post, scope, body)
        wait_for(scope, 1, 0)
        late = post(scope, body)
        gate.set()
        assert held.result(timeout=20).status_code == 200
    assert late.status_code == 503 and late.headers["x-should-retry"] == "false"
    assert late.json()["error"]["code"] == "slots_busy" and len(model.seen) == 1
    assert [r["status"] for r in rows.rows] == [503, 200] or [r["status"] for r in rows.rows] == [200, 503]
    assert [r["status"] for r in reservations(tmp_path)] == ["settled"]          # one call, one reservation
    assert (scope.in_flight, scope.waiting) == (0, 0)
