"""M1 §1 宿主作为运行壳: one attempt as the runner shell runs it — episode, worker session (or the host process), the
package's `agent.run`, with the gateway and the broker on the host (docs/modules/01_POLICY_RUNTIME.md §1,
docs/DESIGN.md §5).

PackageAgent is what task_eval drives for every attempt (the v1 loop, agent.loop.Agent, which ran a K frozen before
v0.3, went in P1 step 5): `run(task) -> AgentResult`, the session, the transcript channel and the attributes the
attempt runner reads. Where the loop runs is the package's: a package with an `agent` entry or with code (tools, hooks) runs in the
isolated worker — a v1 package with code gets the default agent added to its loaded bundle, which changes nothing of
its identity — and a package with text only (or no package) runs the default agent in this process over a HostContext.
Both placements use the same Gateway, AttemptBroker, SkillReader and Marks, so their records are the same records
(PR7-07).

An attempt ends in two ways only: `run` returns, and the string it returns is the final message (termination
`finished`, `stop` the last act call's); or a hard limit ends it (LimitHit, termination `budget` with the dimension,
an empty final message, the world scored as it stands). Anything else — the agent raising (AgentRunError), the worker
dying or outrunning the attempt's deadline (WorkerCrashed) — propagates to the attempt runner as infrastructure
(M1 §1 失败语义), except that a worker crash at or past `max_seconds` is the seconds limit, since the host's deadline
is what ended it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..evolve import contracts as ct
from ..ledger import _append_jsonl, new_id, now, redact
from .default_agent import DEFAULT_ENTRY, DEFAULT_PATH, seed_files
from .gateway import AttemptBroker, Gateway, HostContext, HostFault, Marks, SkillReader
from .harness_package import HOST_TOOLS, CapabilityError, usable_worker
from .worker import AgentRunError, LimitHit, PackageCallError, WorkerCrashed, component_view

MAX_PUSHBACKS = 2                     # the default agent's cap, recorded with the session's harness as before


@dataclass
class AgentResult:
    """What an attempt's agent returns to the attempt runner (evolve.task_eval.run_attempt): the final message, the
    act calls (turns), how it stopped and the session it recorded."""
    text: str
    turns: int
    stop: str
    session: str
    verified: bool | None = None
    # v0.3: the hard limit that ended the attempt (act_calls, model_calls, tool_calls, seconds, tokens), None when
    # the agent finished.
    limit: str | None = None


def attempt_view(task: str, system: str, surface, bound, package, max_output_chars: int = 20000,
                 methods: dict | None = None) -> dict:
    """ctx.view (M0 §2): the task, the composed system prompt, the base tools (`base_tools`, the contract's name),
    the package's enabled components (what the default agent needs to run a v1 package), W's prompt policies, the
    recent broker events — refreshed in place by the broker, so a reference the agent keeps stays current — and
    `max_output_chars`, K's bound on any tool's output (WorkerLimits), so a package tool's output is clipped where a
    base tool's is. Nothing of the scorer, the provider or the stores.

    v0.5 (docs/design/V0_5_PLAN.md §1.1): `methods` — {prefix, max_calls}, only under a K with a methods table — tells
    the default agent which package tools are methods (read-only, a call cap a run). Without it the view has no
    such key and the agent runs every package tool as before."""
    base_tools = [{"name": s.name, "description": s.description, "schema": s.schema} for s in surface.specs()]
    pkg = None
    if package is not None:
        pkg = {"api_version": package.api_version, **component_view(package.manifest),
               "skills": [{"name": s["name"], "description": s.get("description", "")} for s in package.skills],
               "memory": bool(package.memory)}
    policies = {}
    if bound is not None:
        policies = {"system_prompt": bound.prompts.get("worker.system_prompt"), "finish": bound.prompts.get("worker.finish")}
    view = {"task": task[:4000], "system": system, "base_tools": base_tools, "package": pkg, "policies": policies,
            "events": [], "max_output_chars": int(max_output_chars)}
    if methods:
        view["methods"] = {"prefix": str(methods.get("prefix") or "m_"),
                           "max_calls": int(methods.get("max_calls") or methods.get("method_calls") or 25)}
    return view


class PackageAgent:
    """One attempt under the runner shell. `surface` is the Budgeted tool surface of the episode; `bound` the release
    (policies.BoundRuntime) whose package and prompts run; `system` the composed system prompt; `budget` the
    attempt's hard limits; `max_effort` K's ceiling; `worker` the isolated worker for a package with code (refused at
    assembly when missing or unqualified, never an in-process fallback); `placement` overrides where the default
    agent runs ("worker" or "host"), for the equivalence tests."""

    def __init__(self, provider, rt, surface, *, bound=None, system: str = "", budget, max_effort=None, worker=None,
                 session: str | None = None, session_meta: dict | None = None, on_event=None,
                 placement: str | None = None, methods: dict | None = None, extra_system: str = ""):
        self.provider, self.rt, self.surface, self.bound = provider, rt, surface, bound
        # v0.5 (docs/design/V0_5_PLAN.md §1.1, §1.4): K's methods ({prefix, max_calls}) for the view, and a text the
        # host appends to this attempt's system prompt (the library stream's retry note on train); neither is set
        # for a v0.4 K, so its attempts are composed as before.
        self.methods, self.extra_system = methods, extra_system
        self.package = getattr(bound, "package", None) if bound is not None else None
        self.system, self.budget, self.max_effort, self.worker = system, budget, max_effort, worker
        self.session = session or new_id("s")
        self.session_meta = dict(session_meta or {})
        self.on_event = on_event
        self.probe = None                  # set by the attempt runner; nothing here makes a stop decision to label
        self._observation_refs: list = []
        self.placement = placement or ("worker" if self.package is not None and self.package.runs_code else "host")
        if self.placement == "worker" and not usable_worker(worker):
            raise CapabilityError(f"package {ct.short(self.package.ref) if self.package else '-'} runs code and no "
                                  f"qualified isolated worker is available")
        if self.package is not None:
            base = {s.name for s in surface.specs()} | set(HOST_TOOLS)
            taken = sorted({t["name"] for t in self.package.manifest["tools"] if t.get("enabled", True)} & base)
            if taken:
                # A package tool that shadows a base tool would replace it silently, or send the provider two tools
                # of one name: the candidate's defect, refused before the episode starts (M1 §1).
                raise PackageCallError(f"package tools {taken} take the names of this host's tools")
        self.gateway: Gateway | None = None
        self.broker: AttemptBroker | None = None
        self.skills: SkillReader | None = None

    # ------------------------------------------------------------------ what the attempt runner reads
    @property
    def entry(self) -> str:
        return self.package.agent["entrypoint"] if self.package is not None and self.package.agent else DEFAULT_ENTRY

    @property
    def roles(self) -> tuple[str, ...]:
        return self.package.roles if self.package is not None else ("act",)

    def harness(self) -> dict:
        """The harness component of the session's release: prompt, tools, the agent entry and where it ran."""
        return {"system": self.system, "tools": {s.name: s.description for s in self.surface.specs()},
                "agent": {"entry": self.entry, "roles": list(self.roles), "placement": self.placement},
                "stop": {"point": "return_or_limit", "max_pushbacks": MAX_PUSHBACKS}}

    def decision_ids(self) -> list[str]:
        return []                          # the host asks no decision point (M1 §1)

    def _emit(self, kind: str, **data) -> None:
        _append_jsonl(self.rt.root / "transcripts" / f"{self.session}.jsonl",
                      redact({"ts": now(), "kind": kind, **data}))
        if self.on_event:
            self.on_event(kind, data)

    def _record(self, kind: str, data: dict) -> None:
        self._emit(kind, **data)

    # ------------------------------------------------------------------ the run
    def run(self, task: str) -> AgentResult:
        self.rt.session(self.session, self.session_meta, harness=self.harness())
        self._emit("task", text=task)
        # Where the loop ran, host-written (M1 §4): the agent entry, its placement (the isolated worker or this
        # process) and the roles it may call the model as. The session's harness goes to the ledger as a hash only,
        # and the gateway log alone does not say which program sent the calls; the report reads this line.
        self._emit("agent", entry=self.entry, placement=self.placement, roles=list(self.roles))
        limits = getattr(self.worker, "limits", None)
        max_output_chars = getattr(limits, "max_output_chars", 20000)
        system = self.system
        if self.package is not None:
            # v0.5 (V0_5_PLAN §1.4, the `start` site): the experience library's entries for this task, retrieved by
            # fixed host code and appended to the system prompt; a package without a library adds nothing, so the
            # prompt is the same bytes as before.
            from ..research.library import retrieval_block
            block, retrieval = retrieval_block(self.package, task)
            if block:
                system = f"{system}\n\n{block}" if system else block
            if retrieval is not None:
                self._emit("library_retrieval", **retrieval)
        if self.extra_system:
            system = f"{system}\n\n{self.extra_system}" if system else self.extra_system
        view = attempt_view(task, system, self.surface, self.bound, self.package, max_output_chars,
                            methods=self.methods)
        activity = getattr(self.bound, "activity", None)
        prefix = f"{self.session}:"
        attempt_id = self.session_meta.get("attempt_id")
        identity = getattr(self.bound, "actor_identity", None) if self.bound is not None else None
        self.gateway = Gateway(self.provider, roles=self.roles, max_effort=self.max_effort, budget=self.budget,
                               record=self._record, call_prefix=prefix, surface=self.surface, attempt_id=attempt_id,
                               model_identity_ref=identity.ref() if identity is not None else None)
        self.broker = AttemptBroker(self.surface, record=self._record, gateway=self.gateway, view=view,
                                    event_prefix=prefix, max_output_chars=max_output_chars, attempt_id=attempt_id)

        def on_read(name: str, found: bool) -> None:
            if activity is not None:
                activity.execute("worker.harness", f"read_skill:{name}" if found else "read_skill:missing")

        def on_mark(kind: str, data: dict) -> None:
            if activity is not None and kind in ("hook", "package_tool"):
                activity.execute("worker.harness", f"{kind}:{data.get('hook') or data.get('tool')}")
        self.skills = SkillReader(self.package, record=self._record, on_read=on_read, event_prefix=prefix)
        marks = Marks(self._record, on_mark=on_mark)
        try:
            if self.placement == "worker":
                value = self._run_in_worker(task, view, marks)
            else:
                value = self._run_in_process(task, view, marks)
        except LimitHit as e:
            # The attempt is over at this dimension: an empty final message, the world scored as it stands (M1 §1).
            self._emit("end", stop=e.limit, turns=self.gateway.act_calls, verified=None, limit=e.limit)
            return AgentResult("", self.gateway.act_calls, e.limit, self.session, limit=e.limit)
        stop = self.gateway.last_act_stop or "end"
        self._emit("end", stop=stop, turns=self.gateway.act_calls, verified=None)
        return AgentResult(value, self.gateway.act_calls, stop, self.session)

    def _run_in_worker(self, task: str, view: dict, marks: Marks) -> str:
        """The package's agent in the isolated worker: load the run bundle (the default agent added for a v1 package,
        its identity untouched), serve one `run` for the whole attempt, and record the session's cost."""
        started, served = time.monotonic(), [0]
        session = self.worker.start()
        try:
            files = dict(self.package.run_files)
            if not self.package.agent:
                files[DEFAULT_PATH] = seed_files()[DEFAULT_PATH].decode("utf-8")
            session.load(files)
            base = self.broker.handler(session)

            def counted(handler):
                def handle(reply):
                    served[0] += 1
                    return handler(reply)
                return handle

            def model_call(reply: dict) -> dict:
                answer = self.gateway.model(reply.get("messages") or [], reply.get("tools") or [],
                                            reply.get("role") or "act", reply.get("effort"))
                return {"op": "model_result", "id": reply.get("id"), **answer}

            def skill_read(reply: dict) -> dict:
                return {"op": "skill_result", "id": reply.get("id"), "text": self.skills.read(reply.get("name")),
                        "limits": self.gateway.limits()}

            def mark(reply: dict) -> dict:
                data = reply.get("data") if isinstance(reply.get("data"), dict) else {}
                marks.mark(str(reply.get("kind")), data)
                return {"op": "mark_result", "id": reply.get("id"), "limits": self.gateway.limits()}
            handlers = {"model_call": counted(model_call), "base_call": counted(base), "skill_read": counted(skill_read),
                        "mark": counted(mark)}
            message = {"op": "run", "entry": self.entry, "task": task, "roles": list(self.roles), "view": view,
                       "limits": self.gateway.limits(),
                       "skills": {"index": self.skills.index, "memory": self.skills.memory}}
            deadline = self.budget.max_seconds if self.budget.max_seconds is not None else 24 * 3600
            try:
                value = session.serve(message, handlers=handlers, deadline=deadline)
            except WorkerCrashed as e:
                if self.budget.max_seconds is not None and self.gateway.elapsed() >= self.budget.max_seconds:
                    raise LimitHit("seconds", str(e)) from None      # the host's deadline ended it: a hard limit
                raise
        finally:
            session.close()
            # Worker time is part of what a candidate costs (docs/modules/05_EXECUTION_BUDGET.md §2).
            self._emit("worker_session", seconds=round(time.monotonic() - started, 3), requests=served[0],
                       worker=self.worker.describe().get("kind"))
        if not isinstance(value, str):
            raise AgentRunError(f"the agent returned {type(value).__name__}, not a string")
        return value

    def _run_in_process(self, task: str, view: dict, marks: Marks) -> str:
        """The default agent in this process over a HostContext: the same five names, the same records. The host's
        own failures cross the agent as HostFault and leave as themselves; the agent's own are AgentRunError."""
        from . import default_agent
        ctx = HostContext(self.gateway, self.broker, self.skills, marks, view)
        try:
            value = default_agent.run(ctx, task)
        except HostFault as fault:
            raise fault.error
        except Exception as e:  # noqa: BLE001 - the agent's code raised: infrastructure, not a defect (M1 §1)
            raise AgentRunError(f"{type(e).__name__}: {str(e)[:300]}") from e
        if not isinstance(value, str):
            raise AgentRunError(f"the agent returned {type(value).__name__}, not a string")
        return value


def release_max_effort(objects, release_ref: str) -> str | None:
    """K's `max_effort` when the release's core was frozen under v0.3 — its harness capabilities fix one, the same
    key bench._five_limits reads — else None: an older K (a v0.1/v0.2 workspace, a v2 release) fixed no ceiling, and
    its attempts run uncapped."""
    try:
        rel = objects.load(release_ref, ct.SystemRelease)
        core = objects.load(rel.core_ref, ct.CoreManifest)
    except (KeyError, ct.ContractError):
        return None
    harness = (core.capabilities or {}).get("harness")
    return harness.get("max_effort") if isinstance(harness, dict) else None


def k_max_effort(bound) -> str | None:
    """release_max_effort of the bound release; None for a runtime bound to no release or no object store."""
    services = getattr(bound, "services", None)
    objects = getattr(services, "objects", None)
    if objects is None or not getattr(bound, "release_ref", ""):
        return None
    return release_max_effort(objects, bound.release_ref)


def release_harness_table(objects, release_ref: str, key: str) -> dict | None:
    """One table of K's harness capabilities (v0.5: `methods`, `library`) for the release's core, or None — a K
    frozen before v0.5, or under a preset without the table, has none."""
    try:
        rel = objects.load(release_ref, ct.SystemRelease)
        core = objects.load(rel.core_ref, ct.CoreManifest)
    except (KeyError, ct.ContractError):
        return None
    harness = (core.capabilities or {}).get("harness")
    table = harness.get(key) if isinstance(harness, dict) else None
    return dict(table) if isinstance(table, dict) else None


def k_methods(bound) -> dict | None:
    """The view's `methods` for a bound release whose K carries a methods table (V0_5_PLAN §1.1): the prefix and the
    call cap; None otherwise, and the default agent then treats no package tool as a method."""
    services = getattr(bound, "services", None)
    objects = getattr(services, "objects", None)
    if objects is None or not getattr(bound, "release_ref", ""):
        return None
    table = release_harness_table(objects, bound.release_ref, "methods")
    if table is None:
        return None
    return {"prefix": "m_", "max_calls": int(table.get("method_calls") or 25)}
