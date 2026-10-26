"""The OpenAI-compatible bridge (docs/design/MEMORY.md §2, §9): a host-side open-source library that speaks the
OpenAI chat API reaches the research model by setting one `base_url`, and every call it makes is still metered,
recorded and booked on the activity's budget like any other research call.

It serves one scope kind, `research`, for one user: ReMe's task-memory pipeline in the memory sidecar, whose
`openai` LLM backend always streams and sends `tools: null` (the shape of the 51 chat requests
recorded in v6's ReMe smoke). Only `POST /v1/chat/completions` is served; `/v1/embeddings`, `/v1/models`, an
embed scope, an attempt scope and the tool-call mapping have no user and are not built.

    bridge = Bridge().start()
    scope = bridge.open_scope("research", scope_id=..., step="stream-0", provider=<the activity's research provider,
                              every retry off>, record=<workspace>/memory/<line>/bridge_calls.jsonl,
                              models=["research/reme:success", ...], effort_cap="medium", max_concurrency=4)
    # the sidecar's ReMe gets scope.base_url and scope.token; the host relabels each batch with scope.set_step()
    # and checks scope.calls(step) for anything that was not a 200 (MEMORY §5 step 3)
    scope.close(); bridge.stop()

Requests. `Authorization: Bearer <scope token>`. `model` is `<kind>/<library>` or
`<kind>/<library>:<op>` and must be one of the scope's `models`; the call is booked as
`<scope_id>:<kind>:<library>[:<op>]` (the metered provider's thread-local operation label), so
ReMe's per-op LLM configs keep their spend apart. Messages: the first message, when it is `system` (or
`developer`), is the provider's system; a later one becomes a user message prefixed `[instructions]\\n` (the adapter's
rule); user content is a string or text parts; an assistant message carries text only; a `tool` message, assistant
`tool_calls`, a non-text part and non-empty `tools` are refused. `stream`, `stream_options.include_usage`,
`max_tokens` / `max_completion_tokens` (validated and recorded; the research provider's own output limit binds) and
`reasoning_effort` (capped at the scope's `effort_cap` with gateway.cap_effort) are read; any other field is recorded
under `extra_fields` and not forwarded. Every request passes gateway.malformed before the provider sees it.

Replies. The provider does not stream: the bridge gets the whole turn, then answers `chat.completion`, or for a
stream the SSE chunks role, content, finish, a usage chunk when `include_usage` was asked, then `data: [DONE]`.
`finish_reason` is `length` for `max_tokens`, `content_filter` for `refusal`, `stop` otherwise;
`usage.prompt_tokens` is input + cache reads + cache writes (claude_code reports the cached part apart). A turn with
tool calls on a scope that offered none is the provider's error.

Errors, in OpenAI's shape {"error": {"message", "type", "code", "param"}}: 401 an unknown or closed token; 404 an
unknown model or path; 400 a bad request (the gateway's or this bridge's own reason as the message); 429
`budget_exhausted` when the meter refuses the reservation (code `budget_exhausted`, or `budget_mismatch` for a book that
does not hold the scope; nothing is booked); 503 `slots_busy` (see Concurrency); 502 `provider_error`, and
for an account-class failure (providers.is_account_error: a subscription limit, a login) `code: account` with the
scope closed at once, so ReMe's retries cannot swallow it; 500 `record_failed` when the record could not be written;
500 `internal_error` for the bridge's own fault. The length checks come before authentication: 411 a chunked body,
413 a body over MAX_BODY.

Retries. The bridge never retries and the meter under it must not either (open_scope refuses one that would). The
client must not retry on its own either: the openai SDK ReMe builds its client with (`AsyncOpenAI(api_key,
base_url)`, so the SDK's default `max_retries=2`) resends a 408, 409, 429 or 5xx twice by itself, and each resend
would be another paid provider call. So every answer that is not a 200 carries `x-should-retry: false`, which the
SDK obeys (openai/_base_client.py `_should_retry`); a request the SDK resends anyway (after a timeout or a dropped
connection: `x-stainless-retry-count` above 0) is refused with 400 `client_retry_refused` before the provider is
called; and the activity keeps the research provider's own timeout under the SDK's 600 s read timeout
(RESEARCH_SECONDS), so the SDK never times a slow call out and resends it. ReMe's own `max_retries` (a new request, retry count 0) is then the only retry
there is, and each of its requests is booked on its own.

Concurrency: one semaphore per scope, `max_concurrency` (the activity's concurrency, a machine setting); a request over
it waits. The wait counts against the client's read timeout (ReMe's `memory.llm_timeout_s`), so it is bounded by
`max_wait`, which the host sets to what that timeout leaves after the provider's own: a request still waiting then is
answered 503 `slots_busy` before the provider is called (nothing booked), rather than timed out by the client and sent
again while the first one is still served. The server is a ThreadingHTTPServer on 127.0.0.1:0, one thread per connection,
HTTP/1.1 keep-alive.

Records: every authenticated request writes one `bridge_call` row — {call_id, scope_id, step, op, model,
request_digest, messages, extra_fields, effort, max_tokens, stream, response, usage, cost, elapsed, status, error} —
before its answer goes out; the research input is train trajectories, so the messages are kept whole. The token is
in no row, no answer and no log line (the request log is off; only the token's digest is held to look a scope up).
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from ..contracts import BudgetError, BudgetExhausted, append_jsonl, now
from ..harness.contract import EFFORTS
from ..runtime.gateway import cap_effort, malformed, normalize_usage, own_effort, split_system, takes_effort
from . import RESEARCH_SECONDS  # noqa: F401 - the research provider's own timeout (the activity applies it)

KINDS = ("research",)                 # the scope kinds served: research only
SDK_READ_SECONDS = 600.0              # the openai SDK's default read timeout (openai/_constants.py DEFAULT_TIMEOUT)
RETRY_HEADER = "x-stainless-retry-count"   # the openai SDK's count of its own resends of one request
PATH = "/v1/chat/completions"
MAX_BODY = 16 * 1024 * 1024           # one request body; ReMe's largest smoke request is under 10 KiB
READ_SECONDS = 300                    # an idle keep-alive connection is closed after this
MODEL = re.compile(r"^(?P<kind>[a-z][a-z0-9_]*)/(?P<library>[a-z][a-z0-9_-]*)(?::(?P<op>[A-Za-z0-9_.-]{1,64}))?$")
# What a request may carry and the bridge reads; everything else is recorded under extra_fields, not forwarded.
READ_FIELDS = ("model", "messages", "stream", "stream_options", "tools", "tool_choice", "functions", "max_tokens",
               "max_completion_tokens", "reasoning_effort", "n")
TOOL_CHOICES = (None, "none", "auto")
FINISH = {"max_tokens": "length", "refusal": "content_filter"}


class BridgeError(Exception):
    """A request the bridge answers with an error: the HTTP status and OpenAI's {type, code, message}."""

    def __init__(self, status: int, message: str, *, type: str = "invalid_request_error", code: str = "invalid_request"):
        super().__init__(message)
        self.status, self.type, self.code = status, type, code

    def body(self) -> dict:
        return {"error": {"message": str(self), "type": self.type, "code": self.code, "param": None}}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _text_of(content, where: str) -> str:
    """A message's content as text: a string, or a list of text parts (joined by newlines, as the Stirrup adapter
    joins blocks); None is empty. Anything else — an image, audio or file part — is not served on this scope."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for i, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
                kind = part.get("type") if isinstance(part, dict) else type(part).__name__
                raise BridgeError(400, f"{where}.content[{i}] is a {kind!r} part: only text parts are served on "
                                       f"this scope")
            out.append(part["text"])
        return "\n".join(out)
    raise BridgeError(400, f"{where}.content is a string or a list of text parts, not {type(content).__name__}")


def map_messages(messages) -> list[dict]:
    """OpenAI chat messages -> the gateway's shapes: [{role: system, content}?, {role: user, content},
    {role: assistant, text, tool_calls: []}]. Raises BridgeError(400) for what this scope does not serve."""
    if not isinstance(messages, list) or not messages:
        raise BridgeError(400, "messages is a non-empty list")
    out: list[dict] = []
    for i, m in enumerate(messages):
        where = f"messages[{i}]"
        if not isinstance(m, dict):
            raise BridgeError(400, f"{where} is an object, not {type(m).__name__}")
        role = m.get("role")
        if role in ("system", "developer"):
            text = _text_of(m.get("content"), where)
            # The first message, when it is a system one, is the provider's system argument; a later one (a
            # sub-agent's own prompt) is shown as a user message rather than dropped — the adapter's rule.
            out.append({"role": "system", "content": text} if i == 0 else
                       {"role": "user", "content": f"[instructions]\n{text}"})
        elif role == "user":
            out.append({"role": "user", "content": _text_of(m.get("content"), where)})
        elif role == "assistant":
            if m.get("tool_calls") or m.get("function_call"):
                raise BridgeError(400, f"{where} carries tool calls: tools are not served on this scope",
                                  code="tools_not_served")
            out.append({"role": "assistant", "text": _text_of(m.get("content"), where), "tool_calls": []})
        elif role in ("tool", "function"):
            raise BridgeError(400, f"{where} has role {role!r}: tools are not served on this scope",
                              code="tools_not_served")
        else:
            raise BridgeError(400, f"{where} has role {role!r}, not one of system, developer, user, assistant")
    if not any(m["role"] != "system" for m in out):
        raise BridgeError(400, "messages has no user or assistant message")
    return out


def usage_of(usage: dict | None) -> dict:
    """The provider's usage in OpenAI's vocabulary. claude_code (like the Anthropic API) reports `input_tokens`
    without the cached part and the cache reads and writes beside it; OpenAI's `prompt_tokens` includes them, so
    they are added back, and the reads are also given as `prompt_tokens_details.cached_tokens`."""
    u = normalize_usage(usage or {})
    prompt = u["input_tokens"] + u["cache_read_tokens"] + u["cache_write_tokens"]
    return {"prompt_tokens": prompt, "completion_tokens": u["output_tokens"],
            "total_tokens": prompt + u["output_tokens"],
            "prompt_tokens_details": {"cached_tokens": u["cache_read_tokens"]}}


# ---------------------------------------------------------------------------- a scope
class Scope:
    """One caller's handle on the bridge: a token, the models it may name, the metered provider its calls are made
    on, the record sink they are written to, the step label the host sets, and a semaphore. Made by
    Bridge.open_scope; the token is valid until close()."""

    def __init__(self, bridge: "Bridge", kind: str, *, scope_id: str, step, provider, record,
                 models, effort_cap, max_concurrency: int, max_wait: float | None = None):
        self._bridge, self.kind, self.scope_id = bridge, kind, scope_id
        self.provider = provider
        self.models = tuple(models)
        self.effort_cap = effort_cap
        self.max_concurrency = int(max_concurrency)
        self.max_wait = None if max_wait is None else max(float(max_wait), 0.0)
        self.token = secrets.token_urlsafe(32)
        self._digest = _digest(self.token)
        self._record = record
        self._effort_kw = takes_effort(provider)
        self._own_effort = own_effort(provider)  # what a call runs at when it is handed no effort (gateway's rule)
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(self.max_concurrency)
        self._step = step
        self._calls: list[dict] = []
        self._closed = False
        self.closed_reason: str | None = None
        self.in_flight = 0                   # requests holding a slot now (read by tests and the host's monitor)
        self.waiting = 0                     # requests queued for a slot

    def __repr__(self) -> str:               # never the token
        return f"Scope({self.kind!r}, scope_id={self.scope_id!r}, closed={self._closed})"

    # ------------------------------------------------------------------ the host's side
    @property
    def base_url(self) -> str:
        return f"{self._bridge.base_url}/v1"

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def step(self):
        with self._lock:
            return self._step

    def set_step(self, step) -> None:
        """The label the next calls are recorded under. The sidecar's token is fixed at its start, so each batch's
        step can only be set here, by the host; ReMe cannot change it."""
        with self._lock:
            self._step = step

    def calls(self, step=None) -> list[dict]:
        """[{call_id, step, op, status, ms}] of every authenticated request, in arrival order; with `step`, only
        that step's. The platform checks that a step's calls are all 200 (ReMe's validation op
        swallows an LLM error on its own side)."""
        with self._lock:
            return [dict(c) for c in self._calls if step is None or c["step"] == step]

    def close(self, reason: str = "closed") -> None:
        """Revoke the token now: the next request with it is a 401. A request already at the provider finishes; one
        still waiting for a slot is refused without calling it (502 `account` after an account failure, else 401)."""
        with self._lock:
            if self._closed:
                return
            self._closed, self.closed_reason = True, reason
        self._bridge._forget(self)

    # ------------------------------------------------------------------ one request
    def _write(self, row: dict) -> None:
        sink = self._record
        if callable(sink):
            sink("bridge_call", row)
        else:
            append_jsonl(Path(sink), {"ts": now(), "kind": "bridge_call", **row})

    def serve(self, body, *, unreadable: str | None = None, client_retry: int = 0) -> tuple[int, dict, dict | None]:
        """One chat completion. -> (status, answer, reply) where `reply` carries what the response writer needs
        ({stream, include_usage, model, call_id, created, text, finish, usage}) for a 200, None otherwise.
        `unreadable` is why the body could not be read as JSON (then `body` holds its first bytes as text);
        `client_retry` the client's own resend count (RETRY_HEADER), refused above 0. Writes the call's record
        before returning; raises nothing a request can cause."""
        call_id = f"chatcmpl-{uuid.uuid4().hex}"
        started = time.monotonic()
        step = self.step
        row = {"call_id": call_id, "scope_id": self.scope_id, "step": step, "op": None, "model": None,
               "request_digest": hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
                                                .encode("utf-8")).hexdigest(),
               "messages": body.get("messages") if isinstance(body, dict) else None,
               "extra_fields": {k: v for k, v in body.items() if k not in READ_FIELDS} if isinstance(body, dict)
               else {}, "effort": None, "max_tokens": None, "stream": None, "response": None, "usage": None,
               "cost": None, "elapsed": None, "status": None, "error": None}
        view = None
        try:
            if unreadable is not None:
                raise BridgeError(400, f"the request body is not JSON: {unreadable}")
            request = self._parse(body, row)
            if client_retry > 0:
                raise BridgeError(400, f"this request is the client's own resend #{client_retry} of an earlier one "
                                       f"({RETRY_HEADER}): the bridge serves each request once, and only the "
                                       f"library's own retry (a new request) is served", code="client_retry_refused")
            turn = self._call(request, row)
            text = turn.text or ""
            finish = FINISH.get(turn.stop, "stop")
            row["response"] = {"text": text, "stop": turn.stop, "finish_reason": finish}
            row["usage"] = dict(turn.usage or {})
            row["cost"] = self.provider.pricing.measure(turn.usage or {})
            created = int(time.time())
            usage = usage_of(turn.usage)
            answer = {"id": call_id, "object": "chat.completion", "created": created, "model": request["model"],
                      "choices": [{"index": 0, "message": {"role": "assistant", "content": text, "refusal": None},
                                   "finish_reason": finish, "logprobs": None}],
                      "usage": usage, "system_fingerprint": None}
            status = 200
            view = {"stream": request["stream"], "include_usage": request["include_usage"],
                    "model": request["model"], "call_id": call_id, "created": created, "text": text,
                    "finish": finish, "usage": usage}
        except BridgeError as e:
            status, answer = e.status, e.body()
            row["error"] = {"type": e.type, "code": e.code, "message": str(e)}
        except Exception as e:  # noqa: BLE001 - the bridge's own fault is answered and recorded, never dropped
            error = BridgeError(500, f"the bridge failed on this request: {type(e).__name__}: {str(e)[:300]}",
                                type="server_error", code="internal_error")
            status, answer = 500, error.body()
            row["error"] = {"type": error.type, "code": error.code, "message": str(error)}
        row["status"] = status
        row["elapsed"] = round(time.monotonic() - started, 3)
        try:
            self._write(row)
        except Exception as e:  # noqa: BLE001 - an unrecorded call must not reach the caller as a success
            status, view = 500, None
            answer = BridgeError(500, f"the call could not be recorded ({type(e).__name__}: {str(e)[:200]})",
                                 type="server_error", code="record_failed").body()
        with self._lock:
            self._calls.append({"call_id": call_id, "step": step, "op": row["op"], "status": status,
                                "ms": int(round(row["elapsed"] * 1000))})
        return status, answer, view

    def _parse(self, body, row: dict) -> dict:
        if not isinstance(body, dict):
            raise BridgeError(400, f"the request body is a JSON object, not {type(body).__name__}")
        model = body.get("model")
        row["model"] = model
        if not isinstance(model, str) or model not in self.models:
            raise BridgeError(404, f"the model {model!r} does not exist on this scope: {list(self.models)}",
                              code="model_not_found")
        row["op"] = MODEL.match(model).group("op")
        stream = body.get("stream", False)
        if stream is None:
            stream = False
        if not isinstance(stream, bool):
            raise BridgeError(400, f"stream is a boolean, not {type(stream).__name__}")
        row["stream"] = stream
        options = body.get("stream_options")
        if options is not None and not isinstance(options, dict):
            raise BridgeError(400, "stream_options is an object")
        include_usage = (options or {}).get("include_usage", False)
        if not isinstance(include_usage, bool):
            raise BridgeError(400, "stream_options.include_usage is a boolean")
        tools = body.get("tools")
        if tools not in (None, []):
            raise BridgeError(400, "tools are not served on this scope", code="tools_not_served")
        if body.get("functions") not in (None, []):
            raise BridgeError(400, "tools are not served on this scope", code="tools_not_served")
        choice = body.get("tool_choice")
        if choice not in TOOL_CHOICES:
            raise BridgeError(400, f"tool_choice {choice!r} is not served on this scope (absent, none or auto)",
                              code="tools_not_served")
        if body.get("n") not in (None, 1):
            raise BridgeError(400, f"n={body.get('n')!r}: this scope answers one choice")
        limit = body.get("max_completion_tokens", body.get("max_tokens"))
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
            raise BridgeError(400, "max_tokens / max_completion_tokens is a positive integer")
        row["max_tokens"] = limit
        requested = body.get("reasoning_effort")
        if requested is not None and requested not in EFFORTS:
            raise BridgeError(400, f"reasoning_effort {requested!r} is not one of {list(EFFORTS)}")
        used = cap_effort(requested, self.effort_cap)
        # what the call runs at: the capped request when the provider takes one, else the provider's own pinned
        # effort (ReMe never asks for one) — recorded as the gateway records it, so one effort level is auditable
        row["effort"] = {"requested": requested,
                         "used": used if used is not None and self._effort_kw else self._own_effort}
        mapped = map_messages(body.get("messages"))
        problem = malformed(mapped, [])
        if problem:
            raise BridgeError(400, f"malformed model call: {problem}")
        system, history = split_system(mapped)
        return {"model": model, "op": row["op"], "stream": stream, "include_usage": include_usage,
                "system": system, "history": history, "effort": used}

    def _call(self, request: dict, row: dict):
        """The provider call, in a slot, booked under the op's label. -> ChatTurn; raises BridgeError."""
        library = MODEL.match(request["model"]).group("library")
        label = f"{self.scope_id}:{self.kind}:{library}" + (f":{request['op']}" if request["op"] else "")
        kwargs = {"effort": request["effort"]} if request["effort"] is not None and self._effort_kw else {}
        with self._lock:
            self.waiting += 1
        got = self._slots.acquire(timeout=self.max_wait) if self.max_wait is not None else self._slots.acquire()
        if not got:
            with self._lock:
                self.waiting -= 1
            raise BridgeError(503, f"every one of the scope's {self.max_concurrency} slots stayed busy for "
                                   f"{self.max_wait:.0f} s: the client would time this request out and send it "
                                   f"again; nothing was booked", type="server_error", code="slots_busy")
        with self._lock:
            self.waiting -= 1
            self.in_flight += 1
            closed, reason = self._closed, self.closed_reason
        provider = self.provider
        try:
            if closed:
                # closed while this request waited (an account failure of another call, the host's close): the
                # provider is not called, nothing is booked
                if reason == "account":
                    raise BridgeError(502, "the research provider refused the account on an earlier call of this "
                                           "scope; the scope is closed", type="provider_error", code="account")
                raise BridgeError(401, "the scope was closed while this request waited for a slot",
                                  type="authentication_error", code="invalid_api_key")
            before = provider.operation
            provider.operation = label       # this thread's label (the meter keeps it thread-local)
            try:
                turn = provider.chat(request["system"], request["history"], [], **kwargs)
            finally:
                provider.operation = before  # the label restored
        except BridgeError:
            raise
        except BudgetError as e:
            # The meter refused the reservation with nothing of this scope in flight: nothing was booked.
            raise BridgeError(429, f"the research budget refused this call: {str(e)[:300]}", type="budget_exhausted",
                              code="budget_exhausted" if isinstance(e, BudgetExhausted) else "budget_mismatch")
        except Exception as e:  # noqa: BLE001 - every provider failure is answered, none raised on the server
            from ..providers import is_account_error
            if is_account_error(e):
                # A person has to act before anything sent again can succeed: the scope is closed now, so ReMe's
                # retries meet a 401 instead of spending, and the host sees the 502 in calls().
                self.close("account")
                raise BridgeError(502, f"the research provider refused the account: {str(e)[:300]}",
                                  type="provider_error", code="account")
            raise BridgeError(502, f"the research provider failed: {type(e).__name__}: {str(e)[:300]}",
                              type="provider_error", code="provider_error")
        finally:
            with self._lock:
                self.in_flight -= 1
            self._slots.release()
        if turn.tool_calls:
            row["usage"] = dict(turn.usage or {})
            row["cost"] = provider.pricing.measure(turn.usage or {})
            raise BridgeError(502, "the research provider returned tool calls on a scope that offered none",
                              type="provider_error", code="provider_error")
        return turn


# ---------------------------------------------------------------------------- the server
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "reharness-bridge"
    sys_version = ""
    timeout = READ_SECONDS

    def log_message(self, format, *args):    # noqa: A002 - no request log: nothing of a request reaches a log
        return

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if status != 200:
            # the openai SDK resends a 408, 409, 429 or 5xx by itself unless the server says not to; each resend
            # would be another paid provider call (module docstring, "Retries")
            self.send_header("x-should-retry", "false")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, e: BridgeError) -> None:
        self._json(e.status, e.body())

    def _drain(self) -> bytes | None:
        """The request body, read whole; None (answered, and the connection closed) when it cannot be."""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._error(BridgeError(411, "send the body with a Content-Length", code="length_required"))
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            self._error(BridgeError(400, "Content-Length is a non-negative integer"))
            return None
        if length > MAX_BODY:
            self.close_connection = True
            self._error(BridgeError(413, f"the request body is over {MAX_BODY} bytes", code="request_too_large"))
            return None
        return self.rfile.read(length) if length else b""

    def do_POST(self):  # noqa: N802 - http.server's name
        bridge: Bridge = self.server.bridge
        raw = self._drain()
        if raw is None:
            return
        if self.path.split("?", 1)[0] != PATH:
            return self._error(BridgeError(404, f"no such endpoint: POST {self.path.split('?', 1)[0]}",
                                           code="not_found"))
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        scope = bridge._scope_for(token) if token else None
        if scope is None:
            return self._error(BridgeError(401, "unknown or closed token", type="authentication_error",
                                           code="invalid_api_key"))
        try:
            body, unreadable = json.loads(raw.decode("utf-8")) if raw else None, None
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            body, unreadable = {"_unparsed": raw[:2000].decode("utf-8", "replace")}, str(e)
        try:
            client_retry = int(self.headers.get(RETRY_HEADER) or 0)
        except ValueError:
            client_retry = 1                 # an unreadable count is not trusted to be a first send
        status, answer, view = scope.serve(body, unreadable=unreadable, client_retry=client_retry)
        if status != 200 or not view["stream"]:
            return self._json(status, answer)
        self._stream(view)

    def _stream(self, view: dict) -> None:
        """SSE: the role chunk, the content chunk, the finish chunk, the usage chunk when asked, [DONE]."""
        base = {"id": view["call_id"], "object": "chat.completion.chunk", "created": view["created"],
                "model": view["model"], "system_fingerprint": None}
        extra = {"usage": None} if view["include_usage"] else {}
        chunks = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "", "refusal": None},
                                        "finish_reason": None, "logprobs": None}], **extra},
                  {**base, "choices": [{"index": 0, "delta": {"content": view["text"]}, "finish_reason": None,
                                        "logprobs": None}], **extra},
                  {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": view["finish"],
                                        "logprobs": None}], **extra}]
        if view["include_usage"]:
            chunks.append({**base, "choices": [], "usage": view["usage"]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")         # the stream's end is the connection's
        self.end_headers()
        self.close_connection = True
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _not_served(self):
        path = self.path.split("?", 1)[0]
        if self._drain() is None:
            return
        if path == PATH:
            return self._error(BridgeError(405, f"{self.command} {path}: only POST is served",
                                           code="method_not_allowed"))
        self._error(BridgeError(404, f"no such endpoint: {self.command} {path}", code="not_found"))

    do_GET = do_PUT = do_DELETE = do_PATCH = _not_served


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):     # a dropped client is not an incident; nothing is logged
        return


class Bridge:
    """The bridge server: 127.0.0.1 on an ephemeral port, scopes opened and closed by the host. IPv4 only: the
    server is the stdlib's ThreadingHTTPServer (AF_INET) and its clients are on this host."""

    def __init__(self, host: str = "127.0.0.1"):
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("the bridge listens on the IPv4 loopback interface only (127.0.0.1)")
        self.host = "127.0.0.1"
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._scopes: dict[str, Scope] = {}      # token digest -> scope; the token itself is held by the Scope

    def start(self) -> "Bridge":
        if self._server is not None:
            return self
        server = _Server((self.host, 0), _Handler)
        server.bridge = self
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="reharness-bridge", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Close every scope, then the server. Idempotent."""
        with self._lock:
            scopes = list(self._scopes.values())
        for scope in scopes:
            scope.close("bridge stopped")
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def __enter__(self) -> "Bridge":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("the bridge is not started")
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def open_scope(self, kind: str, *, scope_id: str, step, provider, record: Callable | str | Path, models,
                   effort_cap: str | None, max_concurrency: int, max_wait: float | None = None) -> Scope:
        """A new scope with its own token. `provider` is the metered research provider (the activity's, with the
        provider's and the meter's retries off);
        `record` a callable record(kind, data) or a JSONL path; `models` the model names it may be asked for,
        each `<kind>/<library>[:<op>]`; `effort_cap` K's ceiling for `reasoning_effort` (None: requests pass
        as asked); `max_concurrency` the provider calls it runs at once."""
        if kind not in KINDS:
            raise ValueError(f"scope kind {kind!r} is not served: v6 serves {list(KINDS)} only")
        if self._server is None:
            raise RuntimeError("start the bridge before opening a scope")
        if not isinstance(scope_id, str) or not scope_id:
            raise ValueError("scope_id is a non-empty string")
        if not all(hasattr(provider, a) for a in ("chat", "pricing", "operation", "max_retries", "account")):
            raise TypeError("a scope's provider is a metered provider (evaluate.budget.MeteredProvider): every call "
                            "it serves is reserved and settled on the activity's budget")
        if provider.max_retries != 0:
            raise ValueError(f"the scope's meter retries {provider.max_retries} time(s); the bridge never retries "
                             f"(the activity switches the provider's and the meter's retries off)")
        names = list(models or ())
        if not names:
            raise ValueError("a scope serves at least one model name")
        for name in names:
            match = MODEL.match(name) if isinstance(name, str) else None
            if match is None or match.group("kind") != kind:
                raise ValueError(f"model name {name!r} is not {kind}/<library>[:<op>]")
        if effort_cap is not None and effort_cap not in EFFORTS:
            raise ValueError(f"effort_cap {effort_cap!r} is not one of {list(EFFORTS)}")
        if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool) or max_concurrency < 1:
            raise ValueError("max_concurrency is a positive integer")
        if not callable(record) and not isinstance(record, (str, Path)):
            raise TypeError("record is a callable record(kind, data) or a JSONL path")
        scope = Scope(self, kind, scope_id=scope_id, step=step, provider=provider, record=record, models=names,
                      effort_cap=effort_cap, max_concurrency=max_concurrency, max_wait=max_wait)
        with self._lock:
            self._scopes[scope._digest] = scope
        return scope

    def _scope_for(self, token: str) -> Scope | None:
        """The open scope of this token: a closed scope is no longer registered (Scope.close forgets it)."""
        with self._lock:
            return self._scopes.get(_digest(token))

    def _forget(self, scope: Scope) -> None:
        with self._lock:
            self._scopes.pop(scope._digest, None)
