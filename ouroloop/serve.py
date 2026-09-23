"""A small HTTP decision service (standard library only), so a model can stay loaded on one machine (for example
TDE on the GPU host) and answer single decisions quickly for harnesses elsewhere.

    GET  /health                                   -> {"ok", "points", "backends"}
    POST /decide  {"backend"?, "requests": [{"state", "question"}]}  -> {"answers": [{"probs", "label", "model"}]}
    POST /ask     {"point", "ctx", "session"?}      -> the recorded Decision
    POST /outcome {"decision", "label", "source"?, "weight"?, ...}   -> {"ok": true}

Set OUROLOOP_TOKEN to require "Authorization: Bearer <token>". It binds to 127.0.0.1 unless told otherwise, and
refuses any other address without a token: /outcome writes training labels into the ledger.
"""
from __future__ import annotations

import hmac
import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .types import Question, Request


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def make_server(rt, host: str = "127.0.0.1", port: int = 8765, token: str | None = None) -> ThreadingHTTPServer:
    if host not in LOOPBACK and not token:
        raise ValueError(f"serving on {host} needs a token: set OUROLOOP_TOKEN, or bind to 127.0.0.1")
    lock = threading.Lock()   # one model call and one ledger write at a time
    expected = f"Bearer {token}".encode() if token else b""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, {"ok": True, "points": sorted(rt.points), "backends": sorted(rt.backends)})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if token and not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected):
                self._send(401, {"error": "unauthorized"})
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                with lock:
                    if self.path == "/decide":
                        backend = rt.backends[body.get("backend") or rt.default_backend]
                        answers = backend.decide([Request(r["state"], Question.from_json(r["question"]))
                                                  for r in body["requests"]])
                        self._send(200, {"answers": [{"probs": a.probs, "label": a.label, "model": a.model,
                                                      "usage": a.usage} for a in answers]})
                    elif self.path == "/ask":
                        d = rt.ask(body["point"], body["ctx"], session=body.get("session", "s_http"))
                        self._send(200, asdict(d))
                    elif self.path == "/outcome":
                        extra = {k: v for k, v in body.items() if k not in ("decision", "label", "source", "weight")}
                        rt.outcome(body["decision"], body.get("label"), body.get("source", "env"),
                                   float(body.get("weight", 1.0)), **extra)
                        self._send(200, {"ok": True})
                    else:
                        self._send(404, {"error": "not found"})
            except (KeyError, ValueError, TypeError) as e:
                self._send(400, {"error": f"{type(e).__name__}: {e}"})

        def log_message(self, fmt, *args) -> None:   # keep the terminal quiet
            pass

    return ThreadingHTTPServer((host, port), Handler)
