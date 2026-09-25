"""A backend served by `ouroloop serve` on another machine (for example TDE kept loaded on the GPU host)."""
from __future__ import annotations

import os
from dataclasses import replace

import httpx

from ..types import Answer, Request


class HttpBackend:
    name = "http"

    def __init__(self, url: str, backend: str | None = None, token_env: str | None = "OUROLOOP_TOKEN",
                 timeout: float = 30.0, client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.backend = backend          # which backend on the server; None means the server's default
        self.token_env = token_env
        self.timeout = timeout
        self.client = client or httpx.Client()

    def _headers(self) -> dict:
        if self.token_env and os.environ.get(self.token_env):
            return {"Authorization": f"Bearer {os.environ[self.token_env]}"}
        return {}

    def decide(self, requests: list[Request]) -> list[Answer]:
        resp = self.client.post(f"{self.url}/decide", headers=self._headers(), timeout=self.timeout, json={
            "backend": self.backend,
            "requests": [{"state": r.state, "question": r.question.to_json()} for r in requests]})
        resp.raise_for_status()
        out = []
        for r, a in zip(requests, resp.json()["answers"]):
            ans = Answer.of(r.question.labels(), a.get("probs") or {}, a.get("model", ""), a.get("usage"))
            out.append(replace(ans, abstained=True) if a.get("abstained") else ans)   # the server's abstention holds
        return out

    def code_version(self) -> str | None:
        """The version of the code the server runs, for gate records."""
        resp = self.client.get(f"{self.url}/health", headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get("code")
