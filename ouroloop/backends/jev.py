"""TypeSafe Jev over its REST API (POST /v1/systemone). Questions that share a state go in one request."""
from __future__ import annotations

import os
import time

import httpx

from ..types import Answer, Question, Request, normalize

URL = "https://api.typesafe.ai/v1/systemone"


class JevError(RuntimeError):
    pass


def parse_answer(ans: dict, q: Question) -> dict[str, float]:
    if q.type == "noul":
        p = float(ans["noul"])
        return {"yes": p, "no": 1.0 - p}
    return normalize(q.labels(), ans.get("probabilities") or {})


class JevBackend:
    name = "jev"

    def __init__(self, model: str = "jev-1.13.0", api_key_env: str = "TYPESAFE_API_KEY", url: str = URL,
                 timeout: float = 60.0, max_retries: int = 4, client: httpx.Client | None = None, sleep=time.sleep):
        self.model = model            # pin a version: thresholds are fitted against one model
        self.api_key_env = api_key_env
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self.client = client or httpx.Client()
        self.sleep = sleep

    def _headers(self) -> dict:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise JevError(f"{self.api_key_env} is not set")
        return {"Authorization": f"Bearer {key}"}

    def _post(self, body: dict) -> dict:
        for attempt in range(self.max_retries + 1):
            resp = self.client.post(self.url, json=body, headers=self._headers(), timeout=self.timeout)
            if resp.status_code in (429, 529) and attempt < self.max_retries:
                self.sleep(min(30.0, 2.0 ** attempt))   # rate limited or overloaded: back off and retry
                continue
            if resp.status_code >= 400:
                raise JevError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise JevError("unreachable")

    def decide(self, requests: list[Request]) -> list[Answer]:
        by_state: dict[str, list[int]] = {}
        for i, r in enumerate(requests):
            by_state.setdefault(r.state, []).append(i)
        out: list[Answer | None] = [None] * len(requests)
        for state, idx in by_state.items():
            body = {"state": state, "model": self.model,
                    "questions": {f"q{j}": requests[i].question.to_json() for j, i in enumerate(idx)}}
            data = self._post(body)
            model = data.get("model", self.model)
            for j, i in enumerate(idx):
                usage = data.get("usage", {}) if j == 0 else {}   # a request's usage is counted once
                out[i] = Answer(parse_answer(data["answers"][f"q{j}"], requests[i].question), model, usage)
        return out
