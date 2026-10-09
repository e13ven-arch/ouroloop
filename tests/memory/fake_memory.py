"""An in-process stand-in for the memory sidecar's client, for the default suite (docs/design/MEMORY.md §11).

The real path (ReMe in its venv, chroma, bge-small) runs in the memory suite. Here the same contract runs without the
venv:

- FakeMemoryClient has MemoryClient's calls. Its store is a dict per collection; its vectors are a deterministic bag
  of words (hashed into 512 dimensions, L2-normalised), so similar wordings score high and unrelated ones low; a row's
  vector is its when_to_use, as ReMe's task memory stores it. A read that names a count the collection does not hold
  fails the way the sidecar's does.
- Its `summarize` does what ReMe's flow does with the LLM: one call per extraction op the trajectories need, then one
  validation call, each a real HTTP request to the research scope it was started with — the bridge, which meters it on
  the activity's book — and builds the memories from the replies. Nothing bypasses the bridge unless a test says so.
- ScriptedResearch is the research model behind the bridge: it reads the op from the system line the fake client
  sends and answers each op as a model following ReMe's prompt would, from LESSONS keyed by words of the task.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading

import httpx

from ouroloop.contracts import MemorySidecarError
from ouroloop.memory.client import SHORT, canonical_rows, parse_jsonl
from ouroloop.memory.rules import content_id
from ouroloop.memory.settings import settings_of
from ouroloop.runtime.chat import ChatTurn

DIMS = 512
WORD = re.compile(r"[a-z0-9#]+")

# the rsi domain's lessons (tests/rsi.py): keyword of the task text -> {op: [(when_to_use, content, step_type)]}
LESSONS = {
    "#ops": {
        "success": [("When a request asks to update a contact and then post a phrase in #ops",
                     "1. Update the contact's stage first.\n2. Then post the requested phrase in #ops with the Slack "
                     "API; the request is not done until the message is posted.", "action")],
        "failure": [("When a request asks to update a contact and then post a phrase in #ops",
                     "Do not finish after the update alone: post the phrase in #ops before you finish.", "decision")],
        "comparative": [("When a request asks to update a contact and then post a phrase in #ops",
                         "The higher-scoring run posted the phrase in #ops after the update; the lower-scoring run "
                         "stopped after the update.", "decision")],
    },
    "opportunity stage": {
        "success": [("When a request asks to move a contact to the opportunity stage",
                     "Read the contact, then send one PATCH that sets only the stage field to opportunity.",
                     "action")],
    },
}
VALIDATION_REJECTS = ("Be careful.",)


def tokens(text: str) -> list[str]:
    return WORD.findall(str(text or "").lower())


def bow(text: str) -> list[float]:
    v = [0.0] * DIMS
    for w in tokens(text):
        v[int(hashlib.sha256(w.encode()).hexdigest()[:8], 16) % DIMS] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(u, v) -> float:
    return float(math.fsum(a * b for a, b in zip(u, v)))


class FakeMemoryClient:
    """MemoryClient's calls over an in-process store (see the module docstring)."""

    def __init__(self, *, bypass_bridge: bool = False):
        self.collections: dict[str, dict[str, dict]] = {}
        self.llm = None
        self.settings = None
        self.started = 0
        self.stopped = 0
        self.bypass_bridge = bypass_bridge
        self.summaries = 0
        self._lock = threading.Lock()
        self._snapshots: dict = {}

    # ------------------------------------------------------------------ process
    def start(self, *, venv=None, chroma_path=None, settings=None, llm=None, workdir=None, lock=None,
              timeout: float = 0, scope=None):
        self.settings, self.llm = settings_of(settings), llm
        self.started += 1
        return self

    def stop(self, timeout: float = 0):
        self.stopped += 1

    kill = stop

    def connection(self) -> dict:
        return {}

    def health(self) -> dict:
        return {"status": "ok", "collections": {k: len(v) for k, v in self.collections.items()}}

    # ------------------------------------------------------------------ the store
    def _coll(self, name: str) -> dict:
        return self.collections.setdefault(name, {})

    def count(self, collection: str) -> int:
        return len(self.collections.get(collection) or {})

    def add(self, memories: list[dict], collection: str) -> int:
        with self._lock:
            coll = self._coll(collection)
            ids = [m["memory_id"] for m in memories]
            if len(set(ids)) != len(ids) or any(i in coll for i in ids):
                raise MemorySidecarError(f"add: an id was already there or twice in one call: {ids}")
            for m in memories:
                coll[m["memory_id"]] = json.loads(json.dumps({k: v for k, v in m.items() if k != "vector"}))
            return len(coll)

    def get(self, ids: list[str], collection: str) -> list[dict]:
        coll = self.collections.get(collection) or {}
        return [dict(coll[i]) for i in ids if i in coll]

    def retire(self, ids: list[str], collection: str) -> list[str]:
        with self._lock:
            coll = self.collections.get(collection) or {}
            present = sorted(i for i in ids if i in coll)
            for i in present:
                del coll[i]
            return present

    def dump(self, collection: str) -> str:
        return canonical_rows(list((self.collections.get(collection) or {}).values()))

    def load(self, jsonl: str, collection: str, *, clear: bool = True) -> int:
        with self._lock:
            if clear:
                self.collections[collection] = {}
            coll = self._coll(collection)
            for row in parse_jsonl(jsonl):
                coll[row["memory_id"]] = row
            return len(coll)

    def ensure_snapshot(self, snapshot_text: str, collection: str, *, rows: int | None = None) -> int:
        n = len(parse_jsonl(snapshot_text)) if rows is None else int(rows)
        if self._snapshots.get(collection) != n or self.count(collection) != n:
            self.load(snapshot_text, collection, clear=True)
            self._snapshots[collection] = n
        return n

    def forget_snapshot(self, collection: str) -> None:
        self._snapshots.pop(collection, None)

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        return [bow(t) for t in texts]

    def retrieve(self, query: str, collection: str, *, where: dict | None = None, top_k: int = 5,
                 query_mode: bool = True, expected_count: int | None = None) -> list[dict]:
        if expected_count is not None and (collection not in self.collections
                                           or self.count(collection) != int(expected_count)):
            raise MemorySidecarError(f"{SHORT}: collection {collection} holds {self.count(collection)}; the caller "
                                     f"expects {expected_count}")
        q = bow(query)
        rows = [r for r in (self.collections.get(collection) or {}).values()
                if all((r.get("metadata") or {}).get(k) == v for k, v in (where or {}).items())]
        scored = sorted(((cosine(q, bow(r.get("when_to_use") or "")), r["memory_id"], r) for r in rows),
                        key=lambda x: (-x[0], x[1]))
        out = []
        for score, _id, r in scored[:int(top_k)]:
            m = json.loads(json.dumps(r))
            m.setdefault("metadata", {})["retrieval_score"] = round(score, 6)
            out.append(m)
        return out

    # ------------------------------------------------------------------ the summary, through the research scope
    def _ask(self, op: str, payload: dict) -> tuple[dict, str]:
        """One chat completion of ReMe's `op` on the research scope -> (the reply record, its text)."""
        if self.llm is None:
            raise MemorySidecarError("summary: the sidecar has no research scope")
        body = {"model": f"research/reme:{op}", "stream": False,
                "messages": [{"role": "system", "content": f"[reme:{op}]"},
                             {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)}]}
        if self.bypass_bridge:
            return {"op": op, "call_id": f"chatcmpl-bypass-{op}", "done": True, "json": "block"}, \
                ScriptedResearch.answer(op, payload)
        r = httpx.post(f"{self.llm.base_url}/chat/completions", json=body, timeout=60, trust_env=False,
                       headers={"Authorization": f"Bearer {self.llm.token}"})
        if r.status_code != 200:
            raise MemorySidecarError(f"summary: the {op} call answered HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        text = data["choices"][0]["message"]["content"]
        return {"op": op, "call_id": data["id"], "done": True, "json": "block"}, text

    def summarize(self, trajectories: list[dict]) -> dict:
        self.summaries += 1
        s = self.settings
        scores = [float(t.get("score") or 0.0) for t in trajectories]
        query = next((str((t.get("metadata") or {}).get("query") or "") for t in trajectories), "")
        ops = []
        if "success" in s.origins and any(x >= s.success_threshold for x in scores):
            ops.append("success")
        if "failure" in s.origins and any(x < s.success_threshold for x in scores):
            ops.append("failure")
        if "comparative" in s.origins and len(scores) >= 2 and max(scores) > min(scores):
            ops.append("comparative")
        replies, memories = [], []
        for op in ops:
            reply, text = self._ask(op, {"query": query, "scores": scores})
            replies.append(reply)
            for item in json.loads(text.split("```json", 1)[-1].rsplit("```", 1)[0]):
                content = item["experience"]
                memories.append({"memory_id": content_id(content), "when_to_use": item["when_to_use"],
                                 "content": content, "author": f"research/reme:{op}", "score": 0.0,
                                 "metadata": {"step_type": item.get("step_type"), "when_to_use": item["when_to_use"],
                                              "experience": content}})
        valid = []
        for m in memories:                               # ReMe's MemoryValidation: one call per memory
            reply, text = self._ask("validation", {"contents": [m["content"]]})
            replies.append(reply)
            (v,) = json.loads(text.split("```json", 1)[-1].rsplit("```", 1)[0])
            if v["is_valid"] and float(v["score"]) >= s.validation_threshold:
                valid.append({**m, "score": float(v["score"])})
        seen, kept = set(), []
        for m in valid:                                  # the batch's own dedup: one copy of one content
            if m["memory_id"] not in seen:
                seen.add(m["memory_id"])
                kept.append(m)
        return {"memory_list": kept, "validated": len(valid), "replies": replies, "unparsed": []}


def fenced(obj) -> str:
    return "```json\n" + json.dumps(obj, ensure_ascii=False, indent=1) + "\n```"


class ScriptedResearch:
    """The research model behind the bridge (see the module docstring). `fail` makes the next calls of an op raise."""
    name = "scripted-research"
    model = "scripted-1"
    effort = "medium"
    retries = 0
    timeout = 60

    def __init__(self, lessons=None, usage: dict | None = None):
        self.lessons = LESSONS if lessons is None else lessons
        self.usage = usage or {"input_tokens": 300, "output_tokens": 60}
        self.calls: list[str] = []
        self.fail: dict[str, int] = {}

    @staticmethod
    def answer(op: str, payload: dict, lessons=None) -> str:
        lessons = LESSONS if lessons is None else lessons
        if op == "validation":
            return fenced([{"is_valid": c not in VALIDATION_REJECTS, "score": 0.9 if c not in VALIDATION_REJECTS
                            else 0.1} for c in payload.get("contents") or ()])
        query = str(payload.get("query") or "")
        items = [{"when_to_use": w, "experience": c, "step_type": t}
                 for key, by_op in sorted(lessons.items()) if key in query for w, c, t in by_op.get(op, ())]
        return fenced(items)

    def chat(self, system: str, messages: list[dict], specs, **_kw) -> ChatTurn:
        op = re.search(r"\[reme:(\w+)\]", system or "").group(1)
        self.calls.append(op)
        if self.fail.get(op):
            self.fail[op] -= 1
            raise RuntimeError(f"scripted failure of {op}")
        payload = json.loads(next(m["content"] for m in messages if m.get("role") == "user"))
        return ChatTurn(self.answer(op, payload, self.lessons), [], "end", usage=dict(self.usage))


# ---------------------------------------------------------------------------- behind the bridge, for the real sidecar
# The markers of ReMe 0.3.1.10's own prompts (procedural_memory/summary/*), by op: a prompt routed to the wrong op's
# LLM is an error here, not a silent answer.
MARKERS = {
    "success": "reviewing successful step sequences",
    "failure": "reviewing failed step sequences",
    "comparative": "comparing higher-scoring and lower-scoring",
    "validation": "validating the quality and usefulness",
}


def query_of(prompt: str) -> str:
    m = re.search(r"# Original Query\n(.*?)\n\n#", prompt, re.S)
    if m:
        return m.group(1)
    m = re.search(r"# Task Query\n(.*?)\n\n", prompt, re.S)
    return m.group(1) if m else prompt


class ReMeResearch:
    """The research model behind the bridge when the real sidecar's ReMe calls it: each op answered from ReMe's own
    prompt as a model following it would (fenced JSON of the shape the op parses), the lessons by words of the task.
    `fail` makes the next calls of an op raise (the bridge answers them 502)."""
    name = "reme-research"
    model = "scripted-1"
    effort = "medium"
    retries = 0
    timeout = 60

    def __init__(self, lessons=None, reject=VALIDATION_REJECTS, usage: dict | None = None):
        self.lessons = LESSONS if lessons is None else lessons
        self.reject = tuple(reject)
        self.usage = usage or {"input_tokens": 400, "output_tokens": 80}
        self.calls: list[str] = []
        self.fail: dict[str, int] = {}

    def chat(self, system: str, messages: list[dict], specs, **_kw) -> ChatTurn:
        prompt = "\n".join([system or ""] + [str(m.get("content") or m.get("text") or "") for m in messages])
        op = next((o for o, marker in MARKERS.items() if marker in prompt), None)
        if op is None:
            raise RuntimeError("a prompt of no ReMe op this script knows")
        self.calls.append(op)
        if self.fail.get(op):
            self.fail[op] -= 1
            raise RuntimeError(f"scripted failure of {op}")
        if op == "validation":
            bad = any(r in prompt for r in self.reject)
            text = fenced({"is_valid": not bad, "score": 0.1 if bad else 0.8,
                           "feedback": "too generic" if bad else "specific and actionable", "recommendations": ""})
        else:
            q = query_of(prompt)
            text = fenced([{"when_to_use": w, "experience": c, "tags": [op], "confidence": 0.8, "step_type": t,
                            "tools_used": []}
                           for key, by_op in sorted(self.lessons.items()) if key in q for w, c, t in by_op.get(op, ())])
        return ChatTurn(text, [], "end", usage=dict(self.usage))
