"""The bridge read by the client that really calls it (docs/design/MEMORY.md §2, §9; memory/bridge.py "Retries"): the
openai SDK, whose AsyncOpenAI ReMe's openai backend builds as `AsyncOpenAI(api_key, base_url)`. Ported from
v6-gateway's tests/test_bridge_openai_sdk.py.

v6 ran the SDK and ReMe's own OpenAILLM in a subprocess of the memory venv. The default suite does not need that venv:
here the SDK is the one installed in this environment (the `automationbench` extra), run in this process; the tests are
skipped when it is not installed. ReMe's own backend reading the bridge is the memory suite's (MEMORY §11)."""
from __future__ import annotations

import asyncio
import time

import pytest

openai = pytest.importorskip("openai", reason="the openai SDK is not installed here (the automationbench extra)")

from ouroloop.evaluate.budget import Budgets, MeteredProvider  # noqa: E402
from ouroloop.memory.bridge import RESEARCH_SECONDS, SDK_READ_SECONDS, Bridge  # noqa: E402
from ouroloop.providers import ProviderAccountError, ProviderError  # noqa: E402

from test_memory_bridge import MODELS, PRICING, ROUTES, SCOPE, USAGE, Rows, ScriptedReMeModel  # noqa: E402

PROMPT = ("You are an expert AI analyst reviewing successful step sequences from an AI agent execution.\n\n"
          "# Original Query\nEmail every customer whose order shipped late last week.")
PROXIES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    """The SDK reads proxies from the environment; the bridge is on this host."""
    for name in PROXIES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def scope_with(tmp_path):
    """A scope on a given model, budget and record sink; the bridges stopped at the end."""
    bridges = []

    def make(model, limits=None, record=None):
        meter = MeteredProvider(model, Budgets(tmp_path / f"b{len(bridges)}").account(
            SCOPE, limits or {"usd": 10.0, "model_calls": 100}), PRICING, operation=f"{SCOPE}:research:reme",
            max_retries=0)
        rows = Rows() if record is None else record
        bridge = Bridge().start()
        bridges.append(bridge)
        s = bridge.open_scope("research", scope_id=SCOPE, step="sdk", provider=meter, record=rows, models=MODELS,
                              effort_cap="medium", max_concurrency=4)
        return s, rows
    yield make
    for bridge in bridges:
        bridge.stop()


def error_of(e) -> list:
    return [type(e).__name__, e.status_code, (e.body or {}).get("code") if isinstance(e.body, dict) else None,
            e.response.headers.get("x-should-retry")]


def collect(stream) -> dict:
    text, usage, finish, ids = [], None, None, set()
    for chunk in stream:
        ids.add(chunk.id)
        if chunk.usage is not None:
            usage = chunk.usage.model_dump(exclude_none=True)
        for choice in chunk.choices:
            text.append(choice.delta.content or "")
            finish = choice.finish_reason or finish
    return {"text": "".join(text), "usage": usage, "finish": finish, "ids": len(ids)}


def test_the_openai_sdk_streams_with_tools_none_and_gets_the_text_and_the_usage(scope_with):
    model = ScriptedReMeModel()
    s, rows = scope_with(model)
    client = openai.OpenAI(base_url=s.base_url, api_key=s.token, max_retries=0)
    # as ReMe's openai backend calls it: stream=True and tools=None, plus include_usage
    streamed = collect(client.chat.completions.create(
        model="research/reme:success", messages=[{"role": "user", "content": PROMPT}], stream=True, tools=None,
        stream_options={"include_usage": True}))
    plain = collect(client.chat.completions.create(
        model="research/reme:success", messages=[{"role": "user", "content": PROMPT}], stream=True, tools=None))
    whole = client.chat.completions.create(model="research/reme", messages=[{"role": "system", "content": "s"},
                                                                            {"role": "user", "content": PROMPT}])

    async def concurrent():
        aclient = openai.AsyncOpenAI(base_url=s.base_url, api_key=s.token, max_retries=0)

        async def one(i):
            stream = await aclient.chat.completions.create(
                model="research/reme:validation", stream=True, tools=None,
                messages=[{"role": "user", "content": "validating the quality and usefulness " + str(i)}])
            return "".join([c.choices[0].delta.content or "" async for c in stream if c.choices])
        try:
            return await asyncio.gather(*(one(i) for i in range(4)))
        finally:
            await aclient.close()
    together = asyncio.run(concurrent())

    errors = {}
    for label, kwargs, key in (
            ("token", {"model": "research/reme"}, "bad"),
            ("model", {"model": "research/unknown"}, None),
            ("tools", {"model": "research/reme", "tools": [{"type": "function", "function": {"name": "f",
                                                                                          "parameters": {}}}]}, None)):
        c = openai.OpenAI(base_url=s.base_url, api_key=key or s.token, max_retries=0)
        with pytest.raises(openai.APIStatusError) as caught:
            c.chat.completions.create(messages=[{"role": "user", "content": "x"}], **kwargs)
        errors[label] = error_of(caught.value)

    text = ROUTES[0][2]
    prompt_tokens = USAGE["input_tokens"] + USAGE["cache_read_input_tokens"] + USAGE["cache_creation_input_tokens"]
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": USAGE["output_tokens"],
             "total_tokens": prompt_tokens + USAGE["output_tokens"],
             "prompt_tokens_details": {"cached_tokens": USAGE["cache_read_input_tokens"]}}
    assert streamed == {"text": text, "usage": usage, "finish": "stop", "ids": 1}
    assert plain == {"text": text, "usage": None, "finish": "stop", "ids": 1}
    assert (whole.object, whole.choices[0].message.content, whole.choices[0].finish_reason) == \
        ("chat.completion", text, "stop")
    assert whole.usage.model_dump(exclude_none=True) == usage
    assert together == [ROUTES[4][2]] * 4
    assert errors == {"token": ["AuthenticationError", 401, "invalid_api_key", "false"],
                      "model": ["NotFoundError", 404, "model_not_found", "false"],
                      "tools": ["BadRequestError", 400, "tools_not_served", "false"]}
    # what the SDK sent is what the bridge recorded and the model saw: tools never reached it
    assert all(seen["specs"] == [] for seen in model.seen) and len(model.seen) == 7
    assert model.seen[2]["system"] == "s"
    assert [r["status"] for r in rows.rows] == [200] * 7 + [404, 400]
    assert all(s.token not in str(r) for r in rows.rows)


@pytest.mark.parametrize("label", ["provider_error", "account", "budget", "record_failed"])
def test_a_failed_call_is_one_request_and_one_provider_call_with_the_sdks_default_retries(scope_with, label):
    """ReMe builds `AsyncOpenAI(api_key, base_url)`: the SDK's default max_retries=2. Without `x-should-retry:
    false` one failing research call was sent three times and paid for three times (v6's review probe)."""
    if label == "provider_error":
        model = ScriptedReMeModel(fail=ProviderError("claude CLI: exit 1 after 40s", retryable=True))
        s, rows = scope_with(model)
        want, calls = ["InternalServerError", 502, "provider_error", "false"], 1
    elif label == "account":
        model = ScriptedReMeModel(fail=ProviderAccountError("claude CLI: You've hit your session limit"))
        s, rows = scope_with(model)
        want, calls = ["InternalServerError", 502, "account", "false"], 1
    elif label == "budget":
        model = ScriptedReMeModel()
        s, rows = scope_with(model, {"usd": 0.001, "model_calls": 100})
        want, calls = ["RateLimitError", 429, "budget_exhausted", "false"], 0
    else:
        model = ScriptedReMeModel()

        def broken(kind, data):
            raise OSError("disk full")
        s, rows = scope_with(model, record=broken)
        want, calls = ["InternalServerError", 500, "record_failed", "false"], 1
    statuses = (lambda: [r["status"] for r in rows.rows]) if label != "record_failed" else \
        (lambda: [c["status"] for c in s.calls()])
    client = openai.OpenAI(base_url=s.base_url, api_key=s.token)          # as ReMe builds it
    assert client.max_retries == 2
    with pytest.raises(openai.APIStatusError) as caught:
        client.chat.completions.create(model="research/reme:success", messages=[{"role": "user", "content": "x"}],
                                       stream=True, tools=None)
    assert error_of(caught.value) == want
    assert statuses() == [want[1]] and len(model.seen) == calls
    if label == "account":
        return                                         # the scope is closed: the next request would be a 401

    # and through the async client ReMe really uses: still one request
    async def once():
        aclient = openai.AsyncOpenAI(base_url=s.base_url, api_key=s.token)
        try:
            await aclient.chat.completions.create(model="research/reme:success", stream=True, tools=None,
                                                  messages=[{"role": "user", "content": "x"}])
        finally:
            await aclient.close()
    with pytest.raises(openai.APIStatusError) as caught:
        asyncio.run(once())
    assert error_of(caught.value) == want
    assert statuses() == [want[1]] * 2 and len(model.seen) == 2 * calls


class Slow(ScriptedReMeModel):
    def chat(self, system, messages, specs, effort=None):
        time.sleep(1.5)
        return super().chat(system, messages, specs, effort)


def test_the_sdks_resend_after_its_own_timeout_is_refused_and_never_reaches_the_provider(scope_with):
    """A call slower than the client's read timeout: the SDK times out and resends it (x-stainless-retry-count 1).
    The resend is refused before the provider; the first call still finishes and is recorded: one provider call.
    The activity keeps the provider's timeout under the SDK's 600 s, so in production this does not arise."""
    model = Slow()
    s, rows = scope_with(model)
    client = openai.OpenAI(base_url=s.base_url, api_key=s.token, timeout=0.5, max_retries=1)
    with pytest.raises(openai.BadRequestError) as caught:
        client.chat.completions.create(model="research/reme:success", messages=[{"role": "user", "content": "x"}],
                                       stream=True, tools=None)
    assert error_of(caught.value) == ["BadRequestError", 400, "client_retry_refused", "false"]
    deadline = time.monotonic() + 10
    while len(rows.rows) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sorted(r["status"] for r in rows.rows) == [200, 400]
    assert len(model.seen) == 1


def test_the_research_timeout_is_under_the_sdks_read_timeout():
    """RESEARCH_SECONDS is kept under the SDK's default read timeout, which SDK_READ_SECONDS restates."""
    from openai._constants import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
    assert DEFAULT_TIMEOUT.read == SDK_READ_SECONDS and DEFAULT_MAX_RETRIES == 2
    assert RESEARCH_SECONDS < SDK_READ_SECONDS
