"""http_fetch: one HTTP request through the worker's egress proxy (docs/design/V0_6_DESIGN.md §3.3, §4.4).

The adapter (agent/stirrup_agent.py) offers this tool only when the worker's environment has REHARNESS_EGRESS=1, which
the host sets when K's worker network is the proxy: the worker then sits on an internal Docker network whose only way
out is the host-side egress proxy, named by HTTP_PROXY / HTTPS_PROXY with the attempt's own credential
(http://attempt:<token>@egress:3128). The proxy allows hosts, ports and methods by K's allowlist and records every
request into the attempt's trace; this tool adds nothing to that and decides nothing about it.

Standard library only (urllib). Stirrup's own fetch_web_page cannot be used here: it builds its client with
trust_env=False, so it ignores the proxy variables, and it resolves and pins IP addresses locally and refuses private
ones (vendor/stirrup/tools/web.py), none of which work on an internal network.

GET, HEAD and POST only. The proxy settings come from the environment alone (getproxies_environment), never from a
platform's system configuration; urllib puts the credential of a proxy URL into Proxy-Authorization, on the request
for http and on the CONNECT of an https tunnel. The answer is `HTTP <status> <reason>`, the content type and the body,
cut to ctx.view["max_output_chars"]; a refusal, an HTTP error status or a network failure is an error result the
model reads, never an exception.
"""
import http.client
import os
import urllib.error
import urllib.parse
import urllib.request

NAME = "http_fetch"
ENV_FLAG = "REHARNESS_EGRESS"
METHODS = ("GET", "HEAD", "POST")
TIMEOUT_SECONDS = 30
MAX_OUTPUT = 20000                    # when the view gives no max_output_chars
READ_FACTOR = 4                       # bytes read per output character at most: a body is never held whole
# Headers the model may not set: the proxy's credential is the attempt's, and the connection is urllib's.
BLOCKED_HEADERS = ("proxy-authorization", "proxy-connection", "host", "connection", "content-length",
                   "transfer-encoding", "upgrade", "te", "trailer", "keep-alive")
SPEC = {
    "name": NAME,
    "description": ("Make one HTTP request to an allowed external host through this deployment's egress proxy. "
                    "GET, HEAD or POST. Returns the status line, the content type and the body (cut to the output "
                    "limit). Requests to hosts outside the allowlist are refused by the proxy."),
    "schema": {"type": "object",
               "properties": {"url": {"type": "string", "description": "The full http:// or https:// URL."},
                              "method": {"type": "string", "enum": list(METHODS), "description": "Default GET."},
                              "headers": {"type": "object", "description": "Request headers, name to value.",
                                          "additionalProperties": {"type": "string"}},
                              "body": {"type": "string", "description": "The request body, for POST only."}},
               "required": ["url"], "additionalProperties": False},
}


def available(environ=None) -> bool:
    """Whether the worker has egress: the one condition under which the adapter lists this tool."""
    return (os.environ if environ is None else environ).get(ENV_FLAG) == "1"


def _error(text: str) -> dict:
    return {"output": text, "is_error": True}


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    note = f"\n[... {len(text) - limit} more characters not shown]"
    return text[:max(limit - len(note), 0)] + note


def _opener():
    # Only the environment's proxies (no system configuration), and the default handlers otherwise: redirects are
    # followed, each through the proxy and its allowlist again.
    return urllib.request.build_opener(urllib.request.ProxyHandler(urllib.request.getproxies_environment()))


def run(ctx, args: dict) -> dict:
    """One request. -> {"output", "is_error"}."""
    if not available():
        return _error(f"{NAME} is not available: this worker has no egress ({ENV_FLAG} is not set)")
    args = args if isinstance(args, dict) else {}
    limit = int((getattr(ctx, "view", None) or {}).get("max_output_chars") or MAX_OUTPUT)
    method = str(args.get("method") or "GET").upper()
    if method not in METHODS:
        return _error(f"method {method!r} is not allowed: {NAME} makes {', '.join(METHODS)} requests only")
    url = args.get("url")
    parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.hostname:
        return _error(f"url {str(url)[:200]!r}: an absolute http:// or https:// URL")
    headers = args.get("headers") or {}
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        return _error("headers are an object of header names to string values")
    refused = sorted(k for k in headers if k.lower() in BLOCKED_HEADERS)
    if refused:
        return _error(f"headers {refused} are set by the connection and the proxy, not by the request")
    body = args.get("body")
    if body is not None and (not isinstance(body, str) or method != "POST"):
        return _error("a body is a string, and only a POST takes one")
    data = body.encode("utf-8") if body is not None else (b"" if method == "POST" else None)
    request = urllib.request.Request(url, data=data, method=method, headers=dict(headers))
    try:
        response = _opener().open(request, timeout=TIMEOUT_SECONDS)
        failed = False
    except urllib.error.HTTPError as e:            # an answer with an error status: the model reads it
        response, failed = e, True
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        return _error(f"{NAME} {method} {url[:300]} failed: {type(reason).__name__}: {str(reason)[:300]}")
    try:
        raw = response.read(limit * READ_FACTOR + 1) if method != "HEAD" else b""
        status, why = response.status if hasattr(response, "status") else response.code, response.reason
        content_type = response.headers.get("Content-Type", "") if response.headers else ""
        charset = response.headers.get_content_charset() if response.headers else None
    finally:
        response.close()
    more = len(raw) > limit * READ_FACTOR
    text = raw[:limit * READ_FACTOR].decode(charset or "utf-8", errors="replace")
    head = f"HTTP {status} {why}\nContent-Type: {content_type}\n\n"
    output = _clip(head + text + ("\n[... the body continues]" if more else ""), limit)
    return {"output": output, "is_error": failed or status >= 400}
