"""A scripted OpenAI-compatible chat server for the real-sidecar tests (docs/design/MEMORY.md §11): the stand-in for
the bridge's research scope (memory/bridge.py Scope) where a test needs to script the model's replies call by call.

Standard library only. It serves `POST /v1/chat/completions`, streamed (SSE, as ReMe's openai backend always asks)
and not, behind a bearer token, and answers each ReMe op the way a model following ReMe's prompt would: fenced JSON
of the shape the op parses. No model is called.

- The op is read from the model name (`research/reme:<op>`, the per-op LLMs of the sidecar's flows) and checked
  against the prompt's own marker, so a prompt routed to the wrong LLM is an error, not a silent answer.
- Every request is logged (in memory, and as JSONL when a log dir is given) with its call id (`chatcmpl-<hex>`, the
  id of the completion it answers, as the bridge's), the step the host set (`set_step`, as the bridge's
  Scope.set_step) when it arrived, the op, the status returned and the reply text. `calls(step)` gives exactly what
  the bridge's Scope.calls gives ({call_id, step, op, status, ms}: no reply), so the memory tests run on the
  bridge's contract; `requests(step)` gives the whole log rows (prompt and reply) for the tests' own checks.
- `fail(op, status, times)` makes the next calls of an op answer an error; `no_json(op, times)` makes them answer
  prose without a ```json block; `reply_with(op, text, times)` makes them answer that text; `slow(op, seconds,
  times)` delays them.
- The replies are scripted per task: `lessons` maps a keyword of the task text to the lessons a success, a failure and
  a comparison of it teach, each (when_to_use, experience, step_type) or with a dict of extra keys the model's JSON
  item carries as a fourth element; `reject` lists contents validation marks invalid.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MARKERS = {
    "success": "reviewing successful step sequences",
    "failure": "reviewing failed step sequences",
    "comparative": "comparing higher-scoring and lower-scoring",
    "validation": "validating the quality and usefulness",
}

# keyword in the task text -> {origin: [lesson]}; a lesson is (when_to_use, experience, step_type)
LESSONS = {
    "invoice": {
        "success": [("When a task asks to email customers about overdue or unpaid invoices",
                     "List the invoices first, filter them by days overdue, then send exactly one email per distinct "
                     "customer and check the recipient list before sending.", "action")],
        "failure": [("When a task gives a relative date such as 'older than 30 days' in a sandbox world",
                     "Derive today's date from the world's own records (the newest timestamps), never from the "
                     "system clock: the sandbox calendar is not the real one.", "reasoning")],
        "comparative": [("When a task asks to contact customers and copy someone on the message",
                         "The higher-scoring run added the account manager as cc on every message; the lower-scoring "
                         "run sent the emails without the cc.", "decision")],
    },
    "crm": {
        "success": [("When a task asks to update CRM records for contacts who replied to a campaign",
                     "Search the replies first, map each reply to its CRM record by email address, then update the "
                     "status field of only those records.", "action")],
        "failure": [("When a task says some contacts or accounts must be skipped or excluded",
                     "Read the exclusion or do-not-contact list before acting and skip every entity on it; updating "
                     "every record fails the task.", "decision")],
        "comparative": [("When a task asks to change a status field for a filtered set of CRM records",
                         "The higher-scoring run filtered the records before the first write; the lower-scoring run "
                         "wrote first and tried to undo the extra changes.", "decision")],
    },
}
GENERIC = ("When working on any task", "Be careful.", "reasoning")


def fenced(obj) -> str:
    return "```json\n" + json.dumps(obj, ensure_ascii=False, indent=1) + "\n```"


def query_of(prompt: str) -> str:
    m = re.search(r"# Original Query\n(.*?)\n\n#", prompt, re.S)
    if m:
        return m.group(1)
    m = re.search(r"# Task Query\n(.*?)\n\n", prompt, re.S)
    return m.group(1) if m else prompt


class FakeOpenAI:
    def __init__(self, log_dir: str | Path | None = None, *, token: str = "fake-scope-token", lessons=None,
                 generic: bool = False, reject=("Be careful.",), host: str = "127.0.0.1"):
        self.token = token
        self.lessons = dict(LESSONS if lessons is None else lessons)
        self.generic = generic           # add a generic lesson to every success extraction (validation rejects it)
        self.reject = tuple(reject)
        self.log: list[dict] = []
        self.step = ""
        self._fail: dict = {}            # op -> [status, ...] for the next calls
        self._no_json: dict = {}         # op -> remaining count
        self._replies: dict = {}         # op -> [text, ...] for the next calls
        self._slow: dict = {}            # op -> [seconds, ...] for the next calls
        self._lock = threading.Lock()
        self.log_path = None
        if log_dir is not None:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            self.log_path = Path(log_dir) / "requests.jsonl"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/_control/step":
                    return outer._control(self, body)
                outer._handle(self, body)

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/_control/calls"):
                    return outer._control(self, None)
                outer._send_json(self, 404, {"error": {"message": "not served"}})

        self.httpd = ThreadingHTTPServer((host, 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://{host}:{self.port}/v1"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    # ------------------------------------------------------------------------------------------- control
    def start(self) -> "FakeOpenAI":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def set_step(self, step: str) -> None:
        with self._lock:
            self.step = step

    def fail(self, op: str, status: int = 500, times: int = 1, message: str = "scripted failure") -> None:
        with self._lock:
            self._fail.setdefault(op, []).extend([(status, message)] * times)

    def no_json(self, op: str, times: int = 1) -> None:
        with self._lock:
            self._no_json[op] = self._no_json.get(op, 0) + times

    def reply_with(self, op: str, text: str, times: int = 1) -> None:
        with self._lock:
            self._replies.setdefault(op, []).extend([text] * times)

    def slow(self, op: str, seconds: float, times: int = 1) -> None:
        with self._lock:
            self._slow.setdefault(op, []).extend([float(seconds)] * times)

    def requests(self, step: str | None = None) -> list[dict]:
        """The whole log rows (prompt, reply, keys...) of every request, or of one step's."""
        with self._lock:
            return [dict(c) for c in self.log if step is None or c["step"] == step]

    def calls(self, step: str | None = None) -> list[dict]:
        """The bridge's Scope.calls(step): [{call_id, step, op, status, ms}] in arrival order."""
        return [{k: c[k] for k in ("call_id", "step", "op", "status", "ms")} for c in self.requests(step)]

    def chat_calls(self) -> list[dict]:
        return [c for c in self.requests() if c["path"].endswith("/chat/completions")]

    # ------------------------------------------------------------------------------------------- replies
    def _lessons_for(self, prompt: str) -> dict:
        q = query_of(prompt).lower()
        for key, lessons in self.lessons.items():
            if key in q:
                return lessons
        return {}

    def reply(self, op: str, prompt: str) -> str:
        if op in ("success", "failure", "comparative"):
            items = [{"when_to_use": w, "experience": e, "tags": [op, "fake"], "confidence": 0.8, "step_type": t,
                      "tools_used": [], **(extra[0] if extra else {})}
                     for w, e, t, *extra in self._lessons_for(prompt).get(op, ())]
            if self.generic and op == "success":
                w, e, t = GENERIC
                items.append({"when_to_use": w, "experience": e, "tags": [], "confidence": 0.2, "step_type": t,
                              "tools_used": []})
            return fenced(items)
        if op == "validation":
            if any(r in prompt for r in self.reject):
                return fenced({"is_valid": False, "score": 0.1, "feedback": "too generic", "recommendations": ""})
            return fenced({"is_valid": True, "score": 0.8, "feedback": "specific and actionable",
                           "recommendations": ""})
        return "ok"

    # ------------------------------------------------------------------------------------------- HTTP
    def _control(self, h, body) -> None:
        """Another process's handle on the scripted bridge: set the step, read a step's calls (token required)."""
        if h.headers.get("Authorization") != f"Bearer {self.token}":
            return self._send_json(h, 401, {"error": {"message": "unknown token"}})
        if body is not None:
            self.set_step(str(body.get("step") or ""))
            return self._send_json(h, 200, {"step": self.step})
        from urllib.parse import parse_qs, urlparse
        step = (parse_qs(urlparse(h.path).query).get("step") or [None])[0]
        return self._send_json(h, 200, {"calls": self.calls(step)})

    def _send_json(self, h, status: int, obj) -> None:
        data = json.dumps(obj).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def _record(self, row: dict) -> None:
        with self._lock:
            self.log.append(row)
            if self.log_path is not None:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _handle(self, h, body: dict) -> None:
        started = time.monotonic()
        call_id = "chatcmpl-" + uuid.uuid4().hex
        model = str(body.get("model") or "")
        prompt = "\n".join(m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
                           for m in body.get("messages") or ())
        op = model.rsplit(":", 1)[-1] if model.startswith("research/reme:") else ""
        with self._lock:
            step = self.step
        row = {"call_id": call_id, "path": h.path, "step": step, "op": op, "model": model,
               "stream": bool(body.get("stream")), "keys": sorted(body), "tools": body.get("tools", "absent"),
               "status": 200, "response": "", "prompt": prompt, "t": time.time(), "ms": 0}

        def record():
            row["ms"] = int(round((time.monotonic() - started) * 1000))
            self._record(row)
        if h.headers.get("Authorization") != f"Bearer {self.token}":
            row["status"] = 401
            record()
            return self._send_json(h, 401, {"error": {"message": "unknown token", "type": "auth", "code": "auth"}})
        if not h.path.endswith("/chat/completions"):
            row["status"] = 404
            record()
            return self._send_json(h, 404, {"error": {"message": "not served", "type": "invalid", "code": None}})
        marker = MARKERS.get(op)
        if marker is None or marker not in prompt:
            row["status"] = 404
            record()
            return self._send_json(h, 404, {"error": {"message": f"model {model!r} with a prompt for another op",
                                                      "type": "invalid", "code": "model"}})
        with self._lock:
            queued = self._fail.get(op) or []
            status, message = queued.pop(0) if queued else (None, "")
            prose = self._no_json.get(op, 0) > 0
            if prose:
                self._no_json[op] -= 1
            scripted = self._replies.get(op) or []
            given = scripted.pop(0) if scripted else None
            delays = self._slow.get(op) or []
            delay = delays.pop(0) if delays else 0.0
        if delay:
            time.sleep(delay)
        if status is not None:
            row["status"] = status
            record()
            return self._send_json(h, status, {"error": {"message": message, "type": "provider_error",
                                                         "code": "provider_error"}})
        text = (given if given is not None else
                "This memory looks reasonable and I would keep it." if prose else self.reply(op, prompt))
        row["response"] = text
        record()
        cid = call_id
        usage = {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(text) // 4,
                 "total_tokens": (len(prompt) + len(text)) // 4}
        if not body.get("stream"):
            return self._send_json(h, 200, {"id": cid, "object": "chat.completion", "created": int(time.time()),
                                            "model": model, "usage": usage,
                                            "choices": [{"index": 0, "finish_reason": "stop",
                                                         "message": {"role": "assistant", "content": text}}]})
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("Connection", "close")
        h.end_headers()

        def chunk(delta, finish=None, extra=None):
            obj = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if extra:
                obj.update(extra)
            h.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())

        chunk({"role": "assistant", "content": ""})
        for i in range(0, len(text), 64):
            chunk({"content": text[i:i + 64]})
        chunk({}, finish="stop")
        if (body.get("stream_options") or {}).get("include_usage"):
            tail = {"id": cid, "object": "chat.completion.chunk", "model": model, "choices": [], "usage": usage}
            h.wfile.write(f"data: {json.dumps(tail)}\n\n".encode())
        h.wfile.write(b"data: [DONE]\n\n")
        h.wfile.flush()
        h.close_connection = True


class Control:
    """The scripted bridge seen from another process (set_step, calls), over its control routes."""

    def __init__(self, base_url: str, token: str):
        self.root = base_url.rsplit("/v1", 1)[0]
        self.token = token

    def _req(self, path: str, body=None):
        import urllib.request

        req = urllib.request.Request(self.root + path, method="POST" if body is not None else "GET",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def set_step(self, step: str) -> None:
        self._req("/_control/step", {"step": step})

    def calls(self, step: str | None = None) -> list[dict]:
        from urllib.parse import quote
        return self._req("/_control/calls" + ("" if step is None else f"?step={quote(step)}"))["calls"]
