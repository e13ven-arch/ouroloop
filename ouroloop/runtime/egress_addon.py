"""The egress proxy's addon (docs/design/V0_6_DESIGN.md §4.4): mitmproxy runs unmodified in its own container
(docker/egress.Dockerfile) and this script is the only code of ours in it. It decides every request and tunnel,
attributes it to an attempt's credential, and records it. Self-contained: it imports mitmproxy and the stdlib only,
never ouroloop (the host never imports mitmproxy; §1.3 rule 1).

    mitmdump --mode regular --listen-port 3128 --set ignore_hosts='.*' --set show_ignored_hosts=true -s addon.py

TLS is never intercepted: `ignore_hosts='.*'` passes every CONNECT tunnel through as raw TCP, and
`show_ignored_hosts` makes mitmproxy keep a flow for it, so the tunnel's bytes reach `tcp_message` (the addon drops
each relayed message once counted, so nothing piles up in memory). What a tunnel shows is host:port and bytes;
inspecting HTTPS content would mean removing ignore_hosts and giving the worker a CA (§12).

Decisions. A credential is `Proxy-Authorization: Basic base64("attempt:<token>")`, registered by the host per
attempt; only its sha256 is held, and `token_id` (its first 12 hex digits) is what a record carries.
- `http_connect` (a tunnel): the credential, then the allowlist entry for exactly host:port with CONNECT among its
  methods. A read-only credential never tunnels.
- `requestheaders` (plain HTTP through the proxy), decided on the headers, before any of the body is read: the
  credential, then the entry for host:port with the request's method. A read-only credential may GET and HEAD only.
  The credential header is removed before anything goes upstream. An allowed request's body and its response's are
  streamed through (counted, never buffered here), so no body of any size piles up in the proxy. A refused one is
  answered when its body is at most REFUSED_BODY bytes (read and dropped); a longer or unsized (chunked) body is
  never read: the connection is closed without an answer and the row says `cut: true`.
- No credential, an unknown one or a revoked one: 407 with a Proxy-Authenticate challenge. Anything else refused:
  403, and for a tunnel the connection is not opened. A fault in this addon refuses too (500, or the connection
  closed for a body it would have to read): it fails closed.

Records: one JSON row per request or tunnel — {ts, token_id, method, host, port, path (plain HTTP only), status,
bytes_up, bytes_down, ms, allowed, reason} (+ error, cut) — written to stdout (the container's log is the full
audit trail) and kept per token_id until the host drains it, at most OUROLOOP_EGRESS_MAX_ROWS per token (the rest
counted as dropped). bytes are what crossed to and from the target: body bytes for plain HTTP (0 for a refused
request: nothing went out), every relayed byte for a tunnel.

Control: a JSON-lines listener on OUROLOOP_EGRESS_CONTROL_PORT, bound to the container's uplink address only (the
one network it is on when it starts; the session networks are attached later), published to the host's 127.0.0.1
only: a worker, on a session network, cannot reach it. Every request also carries the key the pool started the
container with (OUROLOOP_EGRESS_CONTROL_KEY). Ops: ping -> {mitmproxy, policy_sha256, control_bind, protocol};
register {digest, read_only, max_requests}; revoke {token_id}; poll {token_id, max_requests} -> {rows, used,
dropped}, the rows of requests and tunnels that ended since the last poll or drain, the cap moved to max_requests;
drain {token_id, wait} -> {rows, active, dropped, used}, waiting up to `wait` seconds for the token's requests and
tunnels in flight to end (a tunnel still open is reported as a row with `open: true`).

The request cap (protocol 2; V0_6_DESIGN §4.4, integration part 1): a credential may be registered with
`max_requests`, the attempt's tool calls left, which the host moves with every poll as its base tool calls use the
same budget. Every request or tunnel the policy lets out counts one (`used`); one past the cap is refused 429
`request_cap` and goes nothing out — so a proxied call counts in the attempt's max_tool_calls and the limit holds
between two of the host's polls, not only at them. A refused request (403, 407) counts nothing, as a base call the
broker refuses counts nothing.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import functools
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import sys
import time

try:                                     # imported by mitmdump; the policy helpers below also run without it
    from mitmproxy import ctx, http
    from mitmproxy import version as _mitmproxy_version
except ImportError:                      # pragma: no cover - the host's tests read the policy helpers only
    ctx = http = _mitmproxy_version = None

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT")
READ_ONLY_METHODS = ("GET", "HEAD")
USER = "attempt"
HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$|^\[?[0-9A-Fa-f:.]+\]?$")
MAX_ROWS = 10_000                        # kept per token until drained (OUROLOOP_EGRESS_MAX_ROWS); then dropped
PROTOCOL = 2                             # the control protocol: 2 adds the request cap and `poll`
MAX_STRAY = 1_000                        # rows of no registered token (no or an unknown credential)
PATH_CHARS = 2_000
CONTROL_LINE = 1_048_576
REFUSED_BODY = 1_048_576                 # a refused request's body read before its answer; a longer one is cut off
META = "ouroloop_egress"                 # this addon's state on a plain HTTP flow


# ---------------------------------------------------------------------------- the policy
def parse_policy(value) -> dict:
    """[{host, port, methods}] (or {"mode": "proxy", "allow": [...]}, K's worker.network) -> {(host, port): {methods}}.
    Hosts are compared lower-cased and exactly: no wildcard, no suffix match."""
    if isinstance(value, dict):
        value = value.get("allow")
    if not isinstance(value, list):
        raise ValueError("the egress policy is a list of {host, port, methods}")
    out: dict = {}
    for i, entry in enumerate(value):
        if not isinstance(entry, dict) or set(entry) - {"host", "port", "methods"}:
            raise ValueError(f"policy[{i}] is an object with host, port and methods only")
        host, port, methods = entry.get("host"), entry.get("port"), entry.get("methods")
        if not isinstance(host, str) or not HOST.match(host):
            raise ValueError(f"policy[{i}].host {host!r} is not a host name or address")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            raise ValueError(f"policy[{i}].port {port!r} is not a port")
        if not isinstance(methods, list) or not methods or not all(isinstance(m, str) for m in methods):
            raise ValueError(f"policy[{i}].methods is a non-empty list of method names")
        upper = {m.upper() for m in methods}
        unknown = sorted(upper - set(METHODS))
        if unknown:
            raise ValueError(f"policy[{i}].methods {unknown} are not among {list(METHODS)}")
        out.setdefault((host.strip("[]").lower(), port), set()).update(upper)
    return out


def policy_sha256(value) -> str:
    """The digest the pool and the proxy both compute, so the pool knows the proxy enforces the policy it sent."""
    parsed = parse_policy(value)
    canonical = sorted([host, port, sorted(methods)] for (host, port), methods in parsed.items())
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode("utf-8")).hexdigest()


def credential(header: str | None) -> str | None:
    """The token of a `Basic attempt:<token>` Proxy-Authorization header, or None."""
    scheme, _, data = (header or "").strip().partition(" ")
    if scheme.lower() != "basic" or not data:
        return None
    try:
        user, sep, token = base64.b64decode(data.strip(), validate=True).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    return token if sep and user == USER and token else None


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def body_size(request) -> int | None:
    """A request body's declared size from its headers: Content-Length, 0 without one, None when it is unsized
    (chunked) or the header is not a size."""
    if "transfer-encoding" in request.headers:
        return None
    value = request.headers.get("content-length")
    if value is None:
        return 0
    try:
        size = int(value)
    except ValueError:
        return None
    return size if size >= 0 else None


def too_long_to_read(request) -> bool:
    size = body_size(request)
    return size is None or size > REFUSED_BODY


def uplink_address() -> str:
    """The container's address on the network it started on (Docker writes the hostname's address into
    /etc/hosts): where the control listener binds, so the session networks attached later never see it."""
    address = socket.gethostbyname(socket.gethostname())
    if ipaddress.ip_address(address).is_loopback:
        raise ValueError(f"the hostname resolves to {address}, not the uplink address")
    return address


# ---------------------------------------------------------------------------- the addon
def fail_closed(hook):
    """A request or tunnel hook that raises refuses the flow: mitmproxy would otherwise log the error and forward.
    A plain request whose body would have to be read for an answer is cut off instead (never buffered)."""
    @functools.wraps(hook)
    def guarded(self, flow):
        try:
            return hook(self, flow)
        except Exception as e:  # noqa: BLE001 - whatever went wrong, the flow does not pass
            meta = flow.metadata.pop(META, None) if hasattr(flow, "metadata") else None
            if meta is not None and meta.get("counted"):
                self.active[meta["token_id"]] = max(self.active[meta["token_id"]] - 1, 0)
            request = getattr(flow, "request", None)
            if http is not None and getattr(flow, "response", None) is None:
                if request is not None and request.method != "CONNECT" and too_long_to_read(request):
                    if flow.killable:
                        flow.kill()
                else:
                    if request is not None:
                        request.stream = False
                    flow.response = http.Response.make(500, b"egress: refused (addon error)\n",
                                                       {"Content-Type": "text/plain"})
            self._emit(None, {"method": getattr(flow.request, "method", "?"), "host": getattr(flow.request, "host", "?"),
                              "port": getattr(flow.request, "port", 0), "status": 500, "bytes_up": 0, "bytes_down": 0,
                              "ms": 0, "allowed": False, "reason": "addon_error",
                              "error": f"{type(e).__name__}: {str(e)[:300]}"})
    return guarded


class Egress:
    def __init__(self):
        self.policy: dict = {}
        self.policy_digest = ""
        self.key = ""
        self.control_port = 3129
        self.control_bind = ""
        self.max_rows = MAX_ROWS
        self.broken = ""                         # why this addon refuses everything (a bad policy, no key)
        self.tokens: dict[str, dict] = {}        # sha256 of a token -> {token_id, read_only}
        self.revoked: set[str] = set()           # token ids
        self.rows: dict[str, list] = {}          # token id -> rows not yet drained
        self.dropped: dict[str, int] = collections.Counter()
        self.stray: collections.deque = collections.deque(maxlen=MAX_STRAY)
        self.active: dict[str, int] = collections.Counter()      # token id -> requests and tunnels in flight
        self.used: dict[str, int] = collections.Counter()        # token id -> requests and tunnels let out
        self.caps: dict[str, int] = {}           # token id -> the most it may let out (no entry: no cap)
        self.tunnels: dict[str, dict] = {}       # client connection id -> the open tunnel's state
        self.server = None
        self._task = None
        self._configure_from_env(os.environ)

    # ------------------------------------------------------------------ configuration
    def _configure_from_env(self, env) -> None:
        try:
            # the key first: a pool asking over the control port learns why a misconfigured proxy refuses to serve
            self.key = env.get("OUROLOOP_EGRESS_CONTROL_KEY") or ""
            self.control_port = int(env.get("OUROLOOP_EGRESS_CONTROL_PORT") or 3129)
            self.max_rows = int(env.get("OUROLOOP_EGRESS_MAX_ROWS") or MAX_ROWS)
            if self.max_rows < 1:
                raise ValueError("OUROLOOP_EGRESS_MAX_ROWS is a positive integer")
            if len(self.key) < 16:
                raise ValueError("OUROLOOP_EGRESS_CONTROL_KEY is not set (16 characters at least)")
            raw = env.get("OUROLOOP_EGRESS_POLICY")
            if raw is None:
                raise ValueError("OUROLOOP_EGRESS_POLICY is not set")
            self.policy = parse_policy(json.loads(raw))
            self.policy_digest = policy_sha256(json.loads(raw))
        except (ValueError, TypeError) as e:
            self.broken = f"egress addon misconfigured: {e}"

    def running(self):
        """The proxy is up: refuse to serve on a broken configuration or on options that would intercept TLS or
        hide tunnels from this addon; otherwise open the control listener."""
        problem = self.broken
        options = ctx.options
        if not problem and list(options.ignore_hosts or ()) != [".*"]:
            problem = "ignore_hosts must be ['.*']: TLS is passed through, never intercepted"
        if not problem and not options.show_ignored_hosts:
            problem = "show_ignored_hosts must be on: without it a tunnel's bytes never reach the addon"
        if not problem and list(options.mode) != ["regular"]:
            problem = f"mode must be regular, not {list(options.mode)}"
        if not problem and (options.stream_large_bodies or options.body_size_limit or options.store_streamed_bodies):
            problem = ("stream_large_bodies, body_size_limit and store_streamed_bodies must be unset: this addon "
                       "streams every allowed body itself and never reads a long refused one")
        if not problem:
            try:
                self.control_bind = uplink_address()
            except (OSError, ValueError) as e:
                problem = f"no uplink address to bind the control listener to: {e}"
        if problem:
            self.broken = problem
            sys.stderr.write(problem + "\n")
            sys.stderr.flush()
            ctx.master.shutdown()
            return
        self._task = asyncio.get_running_loop().create_task(self._serve())

    def done(self):
        if self.server is not None:
            self.server.close()

    # ------------------------------------------------------------------ decisions
    def decide(self, token: str | None, method: str, host: str, port: int, *, connect: bool):
        """-> (status, reason, token_id): status 0 lets the flow through."""
        if self.broken:
            return 503, "egress_unavailable", "-"
        if token is None:
            return 407, "no_credential", "-"
        d = digest(token)
        token_id = d[:12]
        if token_id in self.revoked:
            return 407, "revoked", token_id
        entry = self.tokens.get(d)
        if entry is None:
            return 407, "unknown_credential", token_id
        method = method.upper()
        if entry["read_only"] and (connect or method not in READ_ONLY_METHODS):
            return 403, "read_only", token_id
        methods = self.policy.get((str(host).strip("[]").lower(), int(port)))
        if methods is None:
            return 403, "host_not_allowed", token_id
        if ("CONNECT" if connect else method) not in methods:
            return 403, "method_not_allowed", token_id
        cap = self.caps.get(token_id)
        if cap is not None and self.used[token_id] >= cap:
            return 429, "request_cap", token_id
        self.used[token_id] += 1
        return 0, None, token_id

    @staticmethod
    def _refusal(status: int, reason: str):
        headers = {"Content-Type": "text/plain", "Connection": "close"}
        if status == 407:
            headers["Proxy-Authenticate"] = 'Basic realm="ouroloop-egress"'
        return http.Response.make(status, f"egress: refused ({reason})\n".encode(), headers)

    def _emit(self, token_id: str | None, row: dict) -> None:
        row = {"ts": round(time.time(), 3), "token_id": token_id or "-", **row}
        sys.stdout.write(json.dumps(row, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        if token_id and token_id != "-" and (token_id in self.rows or token_id in self.revoked):
            kept = self.rows.setdefault(token_id, [])
            if len(kept) < self.max_rows:
                kept.append(row)
            else:
                self.dropped[token_id] += 1
        else:
            self.stray.append(row)

    # ------------------------------------------------------------------ tunnels (CONNECT)
    @fail_closed
    def http_connect(self, flow):
        started = time.time()
        token = credential(flow.request.headers.get("Proxy-Authorization"))
        host, port = flow.request.host, int(flow.request.port)
        status, reason, token_id = self.decide(token, "CONNECT", host, port, connect=True)
        if status:
            flow.response = self._refusal(status, reason)
            self._emit(token_id, {"method": "CONNECT", "host": host, "port": port, "status": status, "bytes_up": 0,
                                  "bytes_down": 0, "ms": 0, "allowed": False, "reason": reason})
            return
        self.tunnels[flow.client_conn.id] = {"token_id": token_id, "host": host, "port": port, "started": started,
                                             "up": 0, "down": 0}
        self.active[token_id] += 1

    def http_connect_error(self, flow):
        """An allowed tunnel whose upstream could not be reached (the proxy answered 502)."""
        state = self.tunnels.pop(flow.client_conn.id, None)
        if state is None:
            return                               # one of this addon's own refusals: already recorded
        self._tunnel_row(state, status=flow.response.status_code if flow.response else 502,
                         error=str(flow.error) if flow.error else "upstream unreachable", reason="upstream_error")

    def tcp_message(self, flow):
        state = self.tunnels.get(flow.client_conn.id)
        message = flow.messages[-1]
        if state is not None:
            state["up" if message.from_client else "down"] += len(message.content)
        del flow.messages[:-1]                   # relayed already: counted, not kept

    def tcp_end(self, flow):
        state = self.tunnels.pop(flow.client_conn.id, None)
        if state is not None:
            self._tunnel_row(state, status=200)

    def tcp_error(self, flow):
        state = self.tunnels.pop(flow.client_conn.id, None)
        if state is not None:
            self._tunnel_row(state, status=200, error=str(flow.error) if flow.error else "tcp error")

    def client_disconnected(self, client):
        state = self.tunnels.pop(client.id, None)
        if state is not None:                    # a tunnel that never reached its TCP layer
            self._tunnel_row(state, status=200, error="client disconnected")

    def _tunnel_row(self, state: dict, *, status: int, error: str | None = None, reason: str | None = None) -> None:
        token_id = state["token_id"]
        self.active[token_id] = max(self.active[token_id] - 1, 0)
        row = {"method": "CONNECT", "host": state["host"], "port": state["port"], "status": status,
               "bytes_up": state["up"], "bytes_down": state["down"],
               "ms": int(round((time.time() - state["started"]) * 1000)), "allowed": True, "reason": reason}
        if error:
            row["error"] = error[:300]
        self._emit(token_id, row)

    # ------------------------------------------------------------------ plain HTTP
    @fail_closed
    def requestheaders(self, flow):
        """Decided on the headers, before mitmproxy reads any of the body (it would otherwise buffer it whole)."""
        started = time.time()
        request = flow.request
        token = credential(request.headers.get("Proxy-Authorization"))
        request.headers.pop("Proxy-Authorization", None)           # the credential never goes upstream
        method, host, port = request.method.upper(), request.host, int(request.port)
        status, reason, token_id = self.decide(token, method, host, port, connect=False)
        meta = {"token_id": token_id, "allowed": not status, "reason": reason, "status": status or None,
                "started": started, "up": 0, "down": 0, "cut": False, "counted": False}
        flow.metadata[META] = meta
        if status:
            if too_long_to_read(request):
                meta["cut"] = True
                flow.kill()                  # the body is never read: the connection closes without an answer
            else:
                flow.response = self._refusal(status, reason)
            return
        self.active[token_id] += 1
        meta["counted"] = True

        def up(chunk: bytes) -> bytes:       # streamed to the target as it arrives, counted, not kept
            meta["up"] += len(chunk)
            return chunk
        request.stream = up

    def responseheaders(self, flow):
        meta = flow.metadata.get(META)
        if meta is None or not meta["allowed"] or flow.response is None:
            return                           # this addon's own refusal (mitmproxy emulates the hook for it)

        def down(chunk: bytes) -> bytes:     # streamed back to the worker, counted, not kept
            meta["down"] += len(chunk)
            return chunk
        flow.response.stream = down

    def response(self, flow):
        self._http_row(flow, flow.response.status_code if flow.response else 0)

    def error(self, flow):
        self._http_row(flow, 502, error=str(flow.error) if flow.error else "error")

    def _http_row(self, flow, status: int, error: str | None = None) -> None:
        meta = flow.metadata.pop(META, None)
        if meta is None:
            return
        token_id = meta["token_id"]
        if meta["counted"]:
            self.active[token_id] = max(self.active[token_id] - 1, 0)
        if not meta["allowed"]:
            status = meta["status"]          # what this addon answered (or would have, for a body it cut off)
        row = {"method": flow.request.method.upper(), "host": flow.request.host, "port": int(flow.request.port),
               "path": flow.request.path[:PATH_CHARS], "status": status, "bytes_up": meta["up"],
               "bytes_down": meta["down"], "ms": int(round((time.time() - meta["started"]) * 1000)),
               "allowed": meta["allowed"],
               "reason": meta["reason"] if not meta["allowed"] else (None if not error else "upstream_error")}
        if meta["cut"]:
            row["cut"] = True                # the body was not read; the worker got no answer, the connection closed
        elif error:
            row["error"] = error[:300]
        self._emit(token_id, row)

    # ------------------------------------------------------------------ control
    async def _serve(self) -> None:
        self.server = await asyncio.start_server(self._client, host=self.control_bind, port=self.control_port,
                                                 limit=CONTROL_LINE)

    async def _client(self, reader, writer) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    answer = await self._answer(json.loads(line))
                except Exception as e:  # noqa: BLE001 - a bad request is answered, the listener stays up
                    answer = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
                writer.write((json.dumps(answer, separators=(",", ":")) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            writer.close()

    async def _answer(self, request: dict) -> dict:
        if not isinstance(request, dict) or not hmac.compare_digest(str(request.get("key") or ""), self.key):
            return {"ok": False, "error": "not authorized"}
        op = request.get("op")
        if op == "ping":
            return {"ok": True, "mitmproxy": getattr(_mitmproxy_version, "VERSION", "?"),
                    "policy_sha256": self.policy_digest, "broken": self.broken or None,
                    "control_bind": self.control_bind, "max_rows": self.max_rows, "protocol": PROTOCOL}
        if op == "register":
            d = str(request.get("digest") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", d):
                return {"ok": False, "error": "digest is a sha256 hex digest"}
            token_id = d[:12]
            if token_id in self.revoked or d in self.tokens:
                return {"ok": False, "error": "this credential was registered before"}
            cap = request.get("max_requests")
            if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
                return {"ok": False, "error": "max_requests is a count of requests, 0 or more, or null"}
            self.tokens[d] = {"token_id": token_id, "read_only": bool(request.get("read_only"))}
            self.rows.setdefault(token_id, [])
            if cap is not None:
                self.caps[token_id] = cap
            return {"ok": True, "token_id": token_id}
        token_id = str(request.get("token_id") or "")
        if op == "poll":
            if token_id not in self.rows and token_id not in self.revoked:
                return {"ok": False, "error": "no such credential"}
            if "max_requests" in request:
                cap = request["max_requests"]
                if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
                    return {"ok": False, "error": "max_requests is a count of requests, 0 or more, or null"}
                if cap is None:
                    self.caps.pop(token_id, None)
                else:
                    self.caps[token_id] = cap
            rows = self.rows.get(token_id, [])
            self.rows[token_id] = []
            return {"ok": True, "rows": rows, "used": self.used[token_id], "dropped": self.dropped.pop(token_id, 0)}
        if op == "revoke":
            self.tokens = {d: e for d, e in self.tokens.items() if e["token_id"] != token_id}
            self.revoked.add(token_id)
            return {"ok": True, "active": self.active[token_id]}
        if op == "drain":
            deadline = time.monotonic() + min(max(float(request.get("wait") or 0), 0.0), 60.0)
            while self.active[token_id] > 0 and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            known = token_id in self.rows or token_id in self.revoked
            rows = self.rows.pop(token_id, [])
            if known:
                self.rows[token_id] = []         # rows after this drain (a late 407) are kept for the next, bounded
            open_now = [{"ts": round(time.time(), 3), "token_id": token_id, "method": "CONNECT", "host": s["host"],
                         "port": s["port"], "status": 200, "bytes_up": s["up"], "bytes_down": s["down"],
                         "ms": int(round((time.time() - s["started"]) * 1000)), "allowed": True, "reason": None,
                         "open": True} for s in self.tunnels.values() if s["token_id"] == token_id]
            return {"ok": True, "rows": rows + open_now, "active": self.active[token_id],
                    "dropped": self.dropped.pop(token_id, 0), "used": self.used[token_id]}
        return {"ok": False, "error": f"unknown op {op!r}"}


addons = [Egress()]
