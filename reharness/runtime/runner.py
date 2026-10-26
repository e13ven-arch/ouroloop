"""One attempt under the runner shell (docs/design/RUNTIME.md §5): the package's agent in the isolated worker, with
the gateway, the broker, skills, memory recall and marks on the host.

  1. the `task` line; the system prompt composed (the task's own system, prompt/system.md, prompt/finish.md, the skill
     index, the memory's opening block);
  2. under a proxy K, the attempt's egress session; the worker started (on that session's network) and loaded with
     the package's run files;
  3. one `run` for the whole attempt: model_call -> the gateway, base_call -> the broker, skill_read -> the skills,
     memory_recall -> the memory reader, mark -> the marks; every answer carries the remaining limits;
  4. the end: the agent returns (termination `finished`); a hard limit (LimitHit, or the worker outrunning max_seconds)
     ends it as `budget` with the dimension; the package's own defect (a hook outside its protocol, a package tool that
     raised) as `harness_error`, scored as the world stands; anything else that ends it early is `infrastructure`;
  5. the egress rows, the `worker_session` line and the `end` line; the worker closed.

A money error (the budget book refuses a reservation) and a provider's account error are not the attempt's: they are
written on the `end` line and raised to the caller, which stops what it is running.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..contracts import (BudgetError, CandidateDefect, EgressUnavailable, Infrastructure, LimitHit, StaleWorker,
                         WorkerCrashed, WorkerUnavailable)
from ..harness.package import HOST_TOOLS, Package, run_files
from .broker import AttemptBroker, Marks, MemoryRecall, SkillReader
from .gateway import Gateway
from .records import Transcript, digest16
from .worker import AgentRunError, PackageCallError, RunDefect, network_mode, package_view, usable_worker

SKILL_INDEX = "Skills you can read with the read_skill tool before you act:"
DEFAULT_SECONDS = 24 * 3600            # a run's deadline when K sets no max_seconds


@dataclass
class AgentResult:
    """What an attempt's agent hands to evaluate: the final text and how the attempt ended, its usage and cost, and
    what the package did (hooks that acted, marks by kind, tools called, skills read)."""
    text: str
    termination: str                     # finished | budget | harness_error | infrastructure
    limit: str | None = None
    usage: dict = field(default_factory=dict)
    cost: float | None = None
    hooks: list = field(default_factory=list)
    marks: dict = field(default_factory=dict)
    turns: int = 0
    stop: str = ""
    session: str = ""
    error: str = ""
    tools_called: list = field(default_factory=list)
    skills_read: list = field(default_factory=list)
    tool_calls: int = 0


def compose_system(base: str, package: Package, opening: str = "") -> str:
    """The system prompt: the task's own system (the environment's), then the package's prompt/system.md and
    prompt/finish.md, then the index of its enabled skills, then the memory's opening block. By appending only: the
    host never rewrites inside the environment's text."""
    parts = [base or ""]
    for key in ("system", "finish"):
        path = package.manifest.prompt.get(key)
        if path and package.files.get(path, "").strip():
            parts.append(package.files[path].strip())
    skills = [s for s in package.manifest.skills if s.get("enabled", True)]
    if skills:
        parts.append(SKILL_INDEX + "\n" + "\n".join(f"- {s['name']}: {s['description']}" if s.get("description")
                                                    else f"- {s['name']}" for s in skills))
    if opening:
        parts.append(opening)
    return "\n\n".join(p for p in parts if p)


def attempt_view(system: str, surface, package: Package, max_output_chars: int, hook_aux_calls: int = 0) -> dict:
    """ctx.view (harness/contract.py VIEW_KEYS): the composed system prompt, the base tools, the package's components as
    the adapter runs them, the last call's usage, the broker's recent events (refreshed in place), the output bound
    and the aux calls an on_finish or after_turn hook may make (K's improver.hook_aux_calls). Nothing of the scorer,
    the provider or the stores."""
    base_tools = [{"name": s.name, "description": s.description, "schema": s.schema} for s in surface.specs()]
    return {"system": system, "base_tools": base_tools, "package": package_view(package.manifest.to_json()),
            "last_usage": None, "events": [], "max_output_chars": int(max_output_chars),
            "hook_aux_calls": int(hook_aux_calls)}


class EgressMeter:
    """An attempt's egress session as the runner keeps its books: under a proxy K every request or tunnel the proxy lets
    out is a tool call of the attempt, against the same max_tool_calls as the base calls. The proxy holds the cap (the
    tool calls left) and refuses one past it; the host moves the cap and takes the rows at every callback (`poll`),
    writes each row as an `egress` line at once, and counts the proxy's `used` into the surface. A refusal at the cap
    ends the attempt at tool_calls, as a base call refused for the budget does."""

    def __init__(self, surface, max_tool_calls, record):
        self.surface, self.max_tool_calls, self.record = surface, max_tool_calls, record
        self.egress = None
        self.rows = 0

    def open(self, worker, attempt_id: str):
        self.egress = worker.egress_session(attempt_id, max_requests=self.cap())
        return self.egress

    def cap(self) -> int | None:
        if self.max_tool_calls is None:
            return None
        base = getattr(self.surface, "base_tool_calls", getattr(self.surface, "tool_calls", 0)) or 0
        return max(int(self.max_tool_calls) - int(base), 0)

    def _take(self, rows: list, *, final: bool = False) -> None:
        refused = False
        for row in rows:
            self.record("egress", dict(row))
            refused = refused or row.get("reason") == "request_cap"
        self.rows += len(rows)
        proxied = getattr(self.surface, "proxied", None)
        if callable(proxied):
            proxied(self.egress.used, refused=refused)
        if not final and getattr(self.surface, "exhausted", ""):
            raise LimitHit("tool_calls")

    def poll(self) -> None:
        if self.egress is not None:
            self._take(self.egress.poll(max_requests=self.cap()))

    def close(self) -> dict:
        self._take(self.egress.close(), final=True)
        return {"token_id": self.egress.token_id, "requests": self.egress.used, "rows": self.rows}


class PackageAgent:
    """One attempt. `provider` is the actor (metered in an activity); `package` the W that runs; `core` K (its
    `limits`, `model.max_effort` and `worker.network` are read here); `surface` the episode's tool surface with the
    attempt's limits on it; `worker` the isolated worker; `transcript` the session's writer; `system` the task's own
    system prompt; `task_meta` the `task` line's fields (task_id, split, task_hash, release_ref, core_ref, arm);
    `memory` the attempt's memory (memory.retrieval.TaskMemory: `opening`, the block appended to the system prompt;
    `event`, the start's `library_retrieval` line; `recall(query, k)`), None for a package that reads none;
    `pricing` turns each call's usage into a cost."""

    def __init__(self, *, provider, package: Package, core: dict, surface, worker, transcript: Transcript,
                 system: str = "", attempt_id: str, task_meta: dict | None = None, memory=None, pricing=None):
        self.provider, self.package, self.core, self.surface, self.worker = provider, package, core, surface, worker
        self.transcript, self.base_system, self.attempt_id = transcript, system, attempt_id
        self.task_meta, self.memory, self.pricing = dict(task_meta or {}), memory, pricing
        self.limits = dict(core.get("limits") or {})
        self.gateway: Gateway | None = None
        self.broker: AttemptBroker | None = None

    @property
    def entry(self) -> str:
        return self.package.manifest.agent["entrypoint"]

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(self.package.manifest.agent["roles"])

    def _record(self, kind: str, data: dict) -> None:
        self.transcript.write(kind, **data)

    def run(self, task: str) -> AgentResult:
        self._record("task", {**self.task_meta, "attempt_id": self.attempt_id, "package_ref": self.package.ref(),
                              "text": task})
        opening = str(getattr(self.memory, "opening", "") or "")
        event = getattr(self.memory, "event", None)
        if event:
            self._record("library_retrieval", {**{k: v for k, v in dict(event).items() if k != "site"},
                                               "site": "start"})
        max_output_chars = int(self.limits.get("max_output_chars") or 20000)
        system = compose_system(self.base_system, self.package, opening)
        view = attempt_view(system, self.surface, self.package, max_output_chars,
                            int((self.core.get("improver") or {}).get("hook_aux_calls") or 0))
        prefix = f"{self.attempt_id}:"
        self.gateway = Gateway(self.provider, roles=self.roles, max_effort=(self.core.get("model") or {}).get(
            "max_effort"), limits=self.limits, record=self._record, surface=self.surface, call_prefix=prefix,
            attempt_id=self.attempt_id, pricing=self.pricing)
        self.broker = AttemptBroker(self.surface, record=self._record, gateway=self.gateway, view=view,
                                    call_prefix=prefix, max_output_chars=max_output_chars)
        skills = SkillReader(self.package, record=self._record)
        memory = MemoryRecall(self.memory.recall if self.memory is not None else None, record=self._record,
                              max_k=int((self.core.get("memory") or {}).get("recall_k") or 5))
        marks = Marks(self._record)
        text, termination, limit, error, raised = "", "finished", None, "", None
        try:
            text = self._serve(task, view, skills, memory, marks)
        except LimitHit as e:
            termination, limit = "budget", e.limit
        except CandidateDefect as e:
            termination, error = "harness_error", str(e)[:500]
        except (BudgetError,) as e:
            termination, error, raised = "infrastructure", f"budget: {e}"[:500], e
        except (AgentRunError, WorkerCrashed, WorkerUnavailable, EgressUnavailable, Infrastructure) as e:
            termination, error = "infrastructure", f"{type(e).__name__}: {e}"[:500]
            if isinstance(e, StaleWorker):
                raised = e
        except Exception as e:  # noqa: BLE001 - a provider's failure or the host's: never evidence about the package
            termination, error = "infrastructure", f"{type(e).__name__}: {e}"[:500]
            if _account_error(e):
                raised = e
        cost = self.gateway.cost()
        self._record("end", {"termination": termination, "limit": limit, "error": error,
                             "final_text_digest": digest16(text), "usage": self.gateway.last_usage(), "cost": cost,
                             "turns": self.gateway.act_calls, "stop": self.gateway.last_act_stop or ""})
        if raised is not None:
            raise raised
        return AgentResult(text=text if termination == "finished" else "", termination=termination, limit=limit,
                           usage=self.gateway.last_usage(), cost=cost, hooks=list(marks.hooks),
                           marks=dict(marks.kinds), turns=self.gateway.act_calls,
                           stop=limit or self.gateway.last_act_stop or "", session=self.attempt_id, error=error,
                           tools_called=list(self.broker.called), skills_read=list(skills.reads),
                           tool_calls=int(getattr(self.surface, "tool_calls", self.broker.tool_calls()) or 0))

    def _serve(self, task: str, view: dict, skills: SkillReader, memory: MemoryRecall, marks: Marks) -> str:
        if not usable_worker(self.worker):
            raise WorkerUnavailable("the package runs code and no qualified isolated worker is available")
        base = {s.name for s in self.surface.specs()} | set(HOST_TOOLS)
        taken = sorted({t["name"] for t in self.package.manifest.tools if t.get("enabled", True)} & base)
        if taken:
            raise PackageCallError(f"package tools {taken} take the names of this host's tools")
        network = network_mode((self.core.get("worker") or {}).get("network", "none"))
        meter = EgressMeter(self.surface, self.limits.get("max_tool_calls"), self._record) \
            if network == "proxy" else None
        egress = meter.open(self.worker, self.attempt_id) if meter is not None else None
        started, served, summary = time.monotonic(), [0], None
        session = None
        try:
            session = self.worker.start(egress=egress) if egress is not None else self.worker.start()
            session.load(run_files(self.package))
            handlers = self._handlers(view, skills, memory, marks, meter, served)
            message = {"op": "run", "entry": self.entry, "task": task, "roles": list(self.roles), "view": view,
                       "limits": self.gateway.limits(), "skills": {"index": skills.index}}
            deadline = float(self.limits.get("max_seconds") or DEFAULT_SECONDS)
            try:
                value = session.serve(message, handlers=handlers, deadline=deadline)
            except WorkerCrashed as e:
                if self.limits.get("max_seconds") is not None and self.gateway.elapsed() >= self.limits["max_seconds"]:
                    raise LimitHit("seconds", str(e)) from None
                raise
            except PackageCallError as e:
                raise RunDefect(str(e)) from None
        finally:
            if session is not None:
                session.close()
            if meter is not None and meter.egress is not None:
                summary = meter.close()
            self._record("worker_session", {"seconds": round(time.monotonic() - started, 3), "requests": served[0],
                                            "worker": self.worker.describe(),
                                            **({"egress": summary} if summary else {})})
        if not isinstance(value, str):
            raise AgentRunError(f"the agent returned {type(value).__name__}, not a string")
        return value

    def _handlers(self, view, skills, memory, marks, meter, served) -> dict:
        gateway, broker = self.gateway, self.broker

        def step(handler):
            def handle(reply):
                served[0] += 1
                if meter is not None:
                    meter.poll()               # what went out since the last callback, before anything else
                answer = handler(reply)
                if meter is not None and reply.get("op") == "base_call":
                    meter.poll()               # the cap follows the base call just made: package code may go out
                return answer                  # again before its next callback
            return handle

        def model_call(reply: dict) -> dict:
            answer = gateway.model(reply.get("messages") or [], reply.get("tools") or [], reply.get("role") or "act",
                                   reply.get("effort"))
            usage = answer.pop("usage", None)
            return {"op": "model_result", "id": reply.get("id"), **answer,
                    **({"view": {"last_usage": usage}} if usage is not None else {})}

        def base_call(reply: dict) -> dict:
            name, args = reply.get("name"), reply.get("args") or {}
            if not isinstance(name, str) or not isinstance(args, dict):
                output, is_error = "a tool call takes a tool name and an object of arguments", True
            else:
                output, is_error = broker.call(name, args, read_only=bool(reply.get("read_only")))
            return {"op": "base_result", "id": reply.get("id"), "output": output, "is_error": is_error,
                    "limits": gateway.limits(), "view": {"events": list(view.get("events") or ())}}

        def skill_read(reply: dict) -> dict:
            return {"op": "skill_result", "id": reply.get("id"), "text": skills.read(reply.get("name")),
                    "limits": gateway.limits()}

        def memory_recall(reply: dict) -> dict:
            return {"op": "memory_result", "id": reply.get("id"),
                    "entries": memory.recall(reply.get("query"), reply.get("k")), "limits": gateway.limits()}

        def mark(reply: dict) -> dict:
            data = reply.get("data") if isinstance(reply.get("data"), dict) else {}
            marks.mark(str(reply.get("kind")), data)
            return {"op": "mark_result", "id": reply.get("id"), "limits": gateway.limits()}

        return {"model_call": step(model_call), "base_call": step(base_call), "skill_read": step(skill_read),
                "memory_recall": step(memory_recall), "mark": step(mark)}


def _account_error(e: BaseException) -> bool:
    """A provider that refuses the account (no balance, a revoked key): every further attempt would fail the same way."""
    try:
        from ..providers import is_account_error
    except ImportError:                                         # pragma: no cover - providers ship with the package
        return False
    return bool(is_account_error(e))
