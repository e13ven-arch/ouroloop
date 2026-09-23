"""A backend served by `ouroloop serve` on another machine (for example TDE kept loaded on the GPU host)."""
from __future__ import annotations

import os

import httpx

from ..types import Answer, Request, normalize


class HttpBackend:
    name = "http"

    def __init__(self, url: str, backend: str | None = None, token_env: str | None = "OUROLOOP_TOKEN",
                 timeout: float = 30.0, client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.backend = backend          # which backend on the server; None means the server's default
        self.token_env = token_env
        self.timeout = timeout
        self.client = client or httpx.Client()

    def decide(self, requests: list[Request]) -> list[Answer]:
        headers = {}
        if self.token_env and os.environ.get(self.token_env):
            headers["Authorization"] = f"Bearer {os.environ[self.token_env]}"
        resp = self.client.post(f"{self.url}/decide", headers=headers, timeout=self.timeout, json={
            "backend": self.backend,
            "requests": [{"state": r.state, "question": r.question.to_json()} for r in requests]})
        resp.raise_for_status()
        return [Answer(normalize(r.question.labels(), a["probs"]), a.get("model", ""), a.get("usage") or {})
                for r, a in zip(requests, resp.json()["answers"])]
