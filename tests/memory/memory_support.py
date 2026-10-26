"""Shared pieces of the real-sidecar tests (docs/design/MEMORY.md §11, the memory suite).

These tests run the real path: ReMe 0.3.1.10's own HTTP service in the memory venv, chroma on disk and bge-small on
onnxruntime; only the LLM is scripted (tests/memory/fake_openai_server.py, or the real bridge in front of a scripted
research model). They need the venv: REHARNESS_MEMORY_VENV=<path>, else reharness.memory.default_venv() (what
`python -m reharness.memory setup` builds from the lock). Without one they are skipped; with
REHARNESS_TEST_REQUIRE_MEMORY=1 a missing venv fails them instead. Every test that starts a sidecar is marked slow: the
default run leaves them out, the full run (-m "not live") takes them.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from fake_openai_server import FakeOpenAI
from reharness.memory import default_venv
from reharness.memory.client import MemoryClient
from reharness.memory.settings import Settings

TESTS = Path(__file__).resolve().parent          # tests/memory: a child process puts it on sys.path
REPO = TESTS.parents[1]


def memory_venv() -> Path:
    """The memory venv these tests run the sidecar in, or a skip (a failure under REHARNESS_TEST_REQUIRE_MEMORY)."""
    path = os.environ.get("REHARNESS_MEMORY_VENV")
    venv = Path(path).expanduser() if path else default_venv()
    if (venv / "bin" / "python").exists():
        return venv
    why = (f"REHARNESS_MEMORY_VENV={path!r} has no bin/python" if path else
           f"{venv} has no bin/python (python -m reharness.memory setup, or point REHARNESS_MEMORY_VENV at a venv)")
    if os.environ.get("REHARNESS_TEST_REQUIRE_MEMORY"):
        pytest.fail(f"REHARNESS_TEST_REQUIRE_MEMORY is set and the memory venv is missing: {why}")
    pytest.skip(f"the memory sidecar tests need the memory venv: {why}")


@pytest.fixture(scope="module")
def venv():
    return memory_venv()


@pytest.fixture
def fake():
    with FakeOpenAI() as f:
        yield f


@dataclass
class Scope:
    """The bridge's research Scope as MemoryClient sees it (base_url and token)."""
    base_url: str
    token: str


def scope_of(fake: FakeOpenAI) -> Scope:
    return Scope(fake.base_url, fake.token)


def settings(**over) -> dict:
    return Settings(**over).to_json()


def sidecar(root: Path, venv: Path, *, llm=None, name: str = "sc", **over) -> MemoryClient:
    """A started sidecar on <root>/<name>/chroma with its working dir <root>/<name>/run (tmp: outside the repo)."""
    root = Path(root) / name
    return MemoryClient().start(venv=venv, workdir=root / "run", chroma_path=root / "chroma",
                                settings=settings(**over), llm=llm)


# ---------------------------------------------------------------------------------------------- the train split
TASKS = {
    "T1": "Email every customer whose invoice is overdue by more than 30 days and copy their account manager on "
          "each message.",
    "T2": "Send a payment reminder email to each customer with an unpaid invoice older than 30 days.",
    "T3": "Update the CRM status to 'engaged' for every lead that replied to last week's campaign, but skip leads on "
          "the exclusion list.",
    "T4": "In the CRM, mark each contact who answered the spring newsletter as 'responded', except contacts flagged "
          "do-not-contact.",
    "T5": "Notify customers by email about overdue invoice payments and log each notice in the collections sheet.",
    "T6": "Change the CRM stage of prospects who responded to the outreach sequence, leaving excluded accounts "
          "untouched.",
    "T7": "Compute the quarterly revenue per region from the sales sheet and post the totals to the finance Slack "
          "channel.",
}
# a task's identity is a plain string (evaluate.attempt.task_hash's role); here 16 hex of the text
HASH = {k: hashlib.sha256(v.encode("utf-8")).hexdigest()[:16] for k, v in TASKS.items()}
TRAIN_TEXTS = {HASH[k]: v for k, v in TASKS.items()}
FORBIDDEN = {"ana@example.com", "INV-20417"}       # train literals: a value of exactly one train task


def conversation(name: str, passed: bool, *, tool_output: str | None = None, steps: int = 1) -> list[dict]:
    """An attempt's trajectory in OpenAI format, as runtime.trace.TraceStore.trajectory rebuilds it."""
    text = TASKS[name]
    crm = "CRM" in text
    tool = "crm_search" if crm else "invoices_list"
    msgs = [{"role": "system", "content": "You work business tasks through the tools you are given."},
            {"role": "user", "content": text}]
    for i in range(steps):
        call = f"call_{name}_{i}"
        msgs.append({"role": "assistant", "content": None if i else "I will look the records up first.",
                     "tool_calls": [{"id": call, "type": "function",
                                     "function": {"name": tool, "arguments": json.dumps({"page": i + 1})}}]})
        msgs.append({"role": "tool", "tool_call_id": call,
                     "content": tool_output if tool_output is not None else
                     json.dumps([{"id": 10 + i, "email": "ana@example.com", "status": "open"}])})
    if passed:
        msgs.append({"role": "assistant", "content": "Done: the matching records were handled and the excluded ones "
                                                     "were left alone."})
    else:
        msgs.append({"role": "assistant", "content": "Done: I handled every record I found."})
    return msgs


def attempt(name: str, kind: str, passed: bool, *, reward: float | None = None, shown=(), reason=None, text=None,
            release_ref: str = "rel-w0") -> dict:
    session = f"{name}-{kind}"
    return {"session": session, "reward": float(reward if reward is not None else (1.0 if passed else 0.3)),
            "passed": passed, "release_ref": release_ref, "shown": list(shown), "final_reason": reason,
            "final_text": text, "trace_refs": [f"{session}:L4"]}


def task(name: str, *, b: float = 0.5, stable_pass: bool = False) -> dict:
    return {"task_hash": HASH[name], "text": TASKS[name], "b": b, "stable_pass": stable_pass}


class Sessions(dict):
    """session -> OpenAI messages: the `trajectory_of` the library phase wires to TraceStore.trajectory."""

    def __call__(self, session: str) -> list[dict]:
        return self[session]


def standard_sessions(names=("T1", "T2", "T3", "T4")) -> Sessions:
    s = Sessions()
    for n in names:
        s[f"{n}-baseline"] = conversation(n, True)
        s[f"{n}-first"] = conversation(n, False)
        s[f"{n}-retry"] = conversation(n, True)
    return s


def items(names=("T1", "T2", "T3", "T4"), **over) -> list:
    """(task, first, retry, baseline) per task: W_base passed it once, the first encounter failed, the retry passed."""
    return [(task(n), attempt(n, "first", False, **over), attempt(n, "retry", True), attempt(n, "baseline", True))
            for n in names]


def stream(client, fake, root: Path, *, collection: str = "ol-live-test0001", line: str = "finance",
           sessions: Sessions | None = None, bridge_calls_of=None, set_step=None, **over):
    """A StreamStep over the scripted bridge (set_step and calls are the fake's, as the bridge Scope's)."""
    from reharness.memory.track import StreamStep
    return StreamStep(client, settings(**over), ledger_path=Path(root) / "memory" / line / "stream.json",
                      live_path=Path(root) / "memory" / line / "live.jsonl", collection=collection,
                      business_line=line, activity="act-test", heldout_domains=("operations",),
                      train_texts=TRAIN_TEXTS, forbidden_literals=set(FORBIDDEN), allowed_literals=set(),
                      trajectory_of=sessions if sessions is not None else standard_sessions(),
                      bridge_calls_of=bridge_calls_of or fake.calls, set_step=set_step or fake.set_step)


def stamped(content: str, when: str, *, line: str = "finance", task_name: str = "T1", origin: str = "success",
            score: float = 0.8, split: str = "train") -> dict:
    """A stored row as StreamStep writes it (for tests that need rows without a summary)."""
    from reharness.memory.rules import content_id, stamp
    mem = {"memory_id": content_id(content), "memory_type": "procedural", "when_to_use": when,
           "content": content, "author": f"research/reme:{origin}", "score": score,
           "metadata": {"step_type": "action", "tags": ["fake"]}}
    return stamp(mem, line=line, source_line=line, split=split, task_hash=HASH[task_name], activity="act-test",
                 release_ref="rel-w0", sessions=[f"{task_name}-first"], trace_refs=[f"{task_name}-first:L4"],
                 origin=origin, born_at=0, source_score=1.0, heldout_domains=("operations",))


def live_of(rows: list[dict], collection: str, *, sources=None) -> dict:
    """StreamStep.live_view()'s shape for rows added by hand: each row's own task as its source unless given."""
    return {"collection": collection,
            "sources": {r["memory_id"]: list((sources or {}).get(r["memory_id"]) or [r["metadata"]["ol_task_hash"]])
                        for r in rows}}
