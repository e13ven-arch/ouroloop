"""A backend served by `ouroloop serve` on another machine (for example TDE kept loaded on the GPU host)."""
from __future__ import annotations

import os
import time
from dataclasses import replace

import httpx

from ..types import Answer, Request

CONNECT_S = 10.0


class HttpBackend:
    name = "http"

    def __init__(self, url: str, backend: str | None = None, token_env: str | None = "OUROLOOP_TOKEN",
                 timeout: float = 30.0, client: httpx.Client | None = None, model: str | None = None,
                 chunk: int = 256, retries: int = 3, sleep=time.sleep):
        """`model`: a trained model's ref, which the server loads (in place of one of its named backends). Requests
        go in chunks of `chunk`, each with its own `timeout` (connecting: at most CONNECT_S). In a call of more than
        one request (an evaluation), a chunk that fails to arrive or gets a server error is sent again, up to
        `retries` times with a growing pause: the same model and requests give the same answers. A single decision
        fails at once, and its caller takes the default."""
        self.url = url.rstrip("/")
        self.backend = backend          # which backend on the server; None means the server's default
        self.model = model
        self.token_env = token_env
        self.timeout = httpx.Timeout(timeout, connect=min(timeout, CONNECT_S))
        self.client = client or httpx.Client()
        self.chunk, self.retries, self.sleep = max(1, chunk), retries, sleep

    def _headers(self) -> dict:
        if self.token_env and os.environ.get(self.token_env):
            return {"Authorization": f"Bearer {os.environ[self.token_env]}"}
        return {}

    def _post(self, path: str, body: dict, retries: int = 0) -> httpx.Response:
        attempt = 0
        while True:
            try:
                resp = self.client.post(f"{self.url}{path}", headers=self._headers(), timeout=self.timeout, json=body)
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500 or attempt >= retries:     # a bad request stays bad
                    raise
            except httpx.TransportError:
                if attempt >= retries:
                    raise
            self.sleep(2.0 ** attempt)
            attempt += 1

    def decide(self, requests: list[Request]) -> list[Answer]:
        out = []
        for i in range(0, len(requests), self.chunk):
            part = requests[i:i + self.chunk]
            body = {"backend": self.backend, **({"model": self.model} if self.model else {}),
                    "requests": [{"state": r.state, "question": r.question.to_json()} for r in part]}
            answers = self._post("/decide", body, self.retries if len(requests) > 1 else 0).json()["answers"]
            if len(answers) != len(part):
                raise httpx.DecodingError(f"{len(answers)} answers to {len(part)} requests")
            for r, a in zip(part, answers):
                ans = Answer.of(r.question.labels(), a.get("probs") or {}, a.get("model", ""), a.get("usage"))
                out.append(replace(ans, abstained=True) if a.get("abstained") else ans)   # abstention holds
        return out

    def release(self) -> None:
        """Ask the server to let go of the models it keeps loaded, e.g. before a training job on its GPU."""
        self._post("/release", {})

    def code_version(self) -> str | None:
        """The version of the code the server runs, for gate records."""
        resp = self.client.get(f"{self.url}/health", headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get("code")
