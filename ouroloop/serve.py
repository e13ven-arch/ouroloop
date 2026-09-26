"""A small HTTP decision service (standard library only), so a model can stay loaded on one machine (for example
the GPU host) and answer single decisions quickly for harnesses elsewhere.

    GET  /health                                   -> {"ok", "points", "backends", "code"}
    POST /decide  {"backend"? | "model"?, "trainer"?, "requests": [{"state", "question"}]}
                                        -> {"answers": [{"probs", "label", "model", "usage", "abstained"}]}
    POST /release                                  -> {"ok": true}; loaded models let go of the GPU
    POST /ask     {"point", "ctx", "session"?, "phase"?, "veto"?}  -> the recorded Decision; a session runs the
                                                                     release that was current at its first ask
    POST /outcome {"decision", "label", "source"?, "weight"?, ...}   -> {"ok": true}; "label" is the right answer, a
                                                                     list of answers that are all right, or null

"model" is a trained model's ref, loaded in-process by the trainer plugin that trained it ("trainer": one of this
package's, or one the server config names; default: the only one it names) as the server config's [models] table
says (jobs_root, and hf_prefix for <repo>@<commit>): a job id (letters, digits, _ and -) or
<hf_prefix><job id>@<40 hex>; any other is refused (400), so a request never names a path or a module.

Set OUROLOOP_TOKEN to require "Authorization: Bearer <token>". It binds to 127.0.0.1 unless told otherwise, and
refuses any other address without a token: /outcome writes training labels into the ledger.
"""
from __future__ import annotations

import hmac
import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .release import code_version
from .types import Question, Request

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def make_server(rt, host: str = "127.0.0.1", port: int = 8765, token: str | None = None,
                trainers: dict | None = None) -> ThreadingHTTPServer:
    """`trainers`: {plugin: options} of the config (config.plugin_options), which load the models it serves."""
    if host not in LOOPBACK and not token:
        raise ValueError(f"serving on {host} needs a token: set OUROLOOP_TOKEN, or bind to 127.0.0.1")
    lock = threading.Lock()   # one model call and one ledger write at a time
    expected = f"Bearer {token}".encode() if token else b""
    code = code_version()     # the inference code's version, for the gate records (M8)
    models = getattr(rt, "models", None) or {}
    trainers = dict(trainers or {})
    loaded: set = set()         # the plugins that loaded a model: /release asks them, and the config's, to let go

    def model(ref, trainer) -> object:
        from .trainers import builtin, load
        if trainer is None and len(trainers) == 1:
            trainer = next(iter(trainers))
        if trainer not in trainers and not builtin(trainer):
            raise ValueError(f"no trainer plugin {trainer!r} here: name one of this package's, or of the config")
        if not models.get("jobs_root"):
            raise ValueError("this server has no [models] jobs_root to load models from")
        backend = load(trainer).backend(ref, {**trainers.get(trainer, {}), **models})
        loaded.add(trainer)
        return backend

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
                self._send(200, {"ok": True, "points": sorted(rt.points), "backends": sorted(rt.backends),
                                 "code": code})
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
                        backend = (model(body["model"], body.get("trainer")) if body.get("model") is not None
                                   else rt.backends[body.get("backend") or rt.default_backend])
                        answers = backend.decide([Request(r["state"], Question.from_json(r["question"]))
                                                  for r in body["requests"]])
                        self._send(200, {"answers": [{"probs": a.probs, "label": a.label, "model": a.model,
                                                      "usage": a.usage, "abstained": a.abstained} for a in answers]})
                    elif self.path == "/release":
                        from .trainers import load
                        for name in sorted(loaded | set(trainers)):
                            release = getattr(load(name), "release", None)
                            if callable(release):
                                release()
                        self._send(200, {"ok": True})
                    elif self.path == "/ask":
                        d = rt.ask(body["point"], body["ctx"], session=body.get("session", "s_http"),
                                   phase=body.get("phase"), veto=body.get("veto"))
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
            except Exception as e:  # noqa: BLE001 - an answer, not a dropped connection: the client may retry
                self._send(500, {"error": f"{type(e).__name__}: {e}"[:500]})

        def log_message(self, fmt, *args) -> None:   # keep the terminal quiet
            pass

    return ThreadingHTTPServer((host, port), Handler)
