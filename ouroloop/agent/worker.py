"""M1: the isolated worker and the host's broker (docs/modules/01_POLICY_RUNTIME.md §1).

The control process never imports package code. A package's tools and hooks run in a worker process; the host
talks to it over stdin/stdout with the bounded protocol of worker_runner.py and answers its `ctx.call_tool`
requests through the broker, which runs the authorised base tools of the episode with their own permission and
budget checks and records every call. A hook's broker is read-only: a call the surface says may write is refused.
Calls made through the broker are internal and never trigger package hooks.

Two workers exist. DockerWorker is the qualified one: a fixed image, no network, no host mounts, a read-only root,
dropped capabilities, and CPU/memory/PID limits. ProcessWorker runs the same runner as a plain subprocess; it has
no isolation, reports `qualified = False`, and is for tests of the protocol only — production paths refuse it.

Failure kinds are kept apart: the package's own errors (an exception, an illegal hook answer) are a candidate
defect; a worker that dies, times out or breaks the protocol is infrastructure — unless a fresh worker then answers
normally, in which case it was the package's code that killed or hung it, and that is the candidate's defect too.

The worker's runner is this host's: every session's first `load` answers with the sha256 of the worker_runner.py it
runs, compared with the host's copy (RUNNER), and an image built from another runner is refused as StaleWorker with
the command that rebuilds it — at startup (bench.harness_checks, App) and at every load, so a stale image never serves
a run whose failures would look like the package's.

v0.3 (M1 §1 宿主作为运行壳): one `run` request spans a whole attempt. The worker runs the package's agent and calls
back for the model (model_call), skills (skill_read) and tools (base_call); WorkerSession.serve answers each
through a handler the host supplies — the gateway and broker live in the host, never in the worker. An agent that
raises at run time is AgentRunError, infrastructure rather than a defect (the draft smoke catches deterministic
crashes before any paid run, M0 §3); a handler that finds a hard limit reached raises LimitHit, and the session
ends there with the dimension named (termination `budget`, M5 §2).
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..evolve import contracts as ct
from .default_agent import DEFAULT_ENTRY, DEFAULT_PATH, seed_files

RUNNER = Path(__file__).with_name("worker_runner.py")
DEFAULT_IMAGE = "ouroloop-worker:py3.12"
REBUILD = f"rebuild the image: docker build -f docker/worker.Dockerfile -t {DEFAULT_IMAGE} ."
MAX_LINE = 1_000_000                      # one protocol line of the v1 ops; anything longer is not a protocol message
MAX_MODEL_LINE = 8_000_000                # a model_call carries the agent's whole history; the reader allows this much
STDERR_TAIL = 4000                        # what is kept of the worker's stderr, for a failure message
# The requests that run package code in a bounded way, so a hang or death in them with a healthy fresh worker is
# the package's defect. `run` is not here: an attempt-long run is ended by the host's limits, and whatever ends it
# early is infrastructure (M1 §1 失败语义), never reclassified.
PACKAGE_OPS = ("probe", "call_tool", "hook", "test", "smoke")
WORKER_ENV = ("PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8")   # the whole environment package code sees


class WorkerUnavailable(RuntimeError):
    """No qualified worker: an infrastructure condition, never a reason to run package code in-process."""


class StaleWorker(WorkerUnavailable):
    """The worker runs another worker_runner.py than this host's (an image built before the runner changed): its
    ops and flags are not the host's, so a run in it would fail in ways that look like the package's. Found at the
    first `load` of every session and at startup, and the operator rebuilds the image (M1 §1 默认 agent 即种子包:
    the host and the image carry the same bytes; different bytes are a startup error)."""


def runner_digest() -> str:
    """sha256 of the host's worker_runner.py: what a worker's `load` answer must name."""
    return hashlib.sha256(RUNNER.read_bytes()).hexdigest()


def stale_runner(identity) -> str | None:
    """Why a worker's `load` answer is not this host's runner, or None when it is: a runner that does not name
    itself is older than the check, a different digest is other bytes."""
    digest = identity.get("runner_sha256") if isinstance(identity, dict) else None
    host = runner_digest()
    if digest == host:
        return None
    seen = f"runs worker_runner.py {digest[:12]}" if isinstance(digest, str) else "runs a worker_runner.py that does not name itself"
    return (f"the worker {seen}, and this host's is {host[:12]}: the image was built from another runner; "
            f"{REBUILD}")


class WorkerCrashed(RuntimeError):
    """The worker died, timed out or broke the protocol: infrastructure, not evidence about the package."""


class PackageCallError(ct.CandidateDefect):
    """The package's code raised, or a hook answered outside its protocol: the candidate's defect. Only for the
    bounded ops (call_tool, hook, probe, test, smoke)."""


class AgentRunError(RuntimeError):
    """The package's agent raised during a `run` (or returned something other than a string). Infrastructure, not a
    CandidateDefect: the attempt is invalid and refilled once (M1 §1 失败语义), because a deterministic crash is the
    draft smoke's to catch and what is left at run time is the world's — a model answer, a tool failure."""


class LimitHit(RuntimeError):
    """A hard limit of the attempt was reached while a `run` was being served (M5 §2): the handler raises it, serve
    ends the worker session and re-raises it, and the attempt ends as termination `budget` with `limit` naming the
    dimension (act_calls, model_calls, tool_calls, seconds, tokens). Neither a defect nor infrastructure."""

    def __init__(self, limit: str, message: str | None = None):
        super().__init__(message or f"the attempt reached its hard limit on {limit}")
        self.limit = limit


@dataclass(frozen=True)
class WorkerLimits:
    request_seconds: float = 30.0          # one tool or hook request, broker calls included
    max_base_calls: int = 50               # broker calls within one request
    max_output_chars: int = 20000


class Worker:
    """A way to start worker sessions. `qualified` says whether it isolates package code."""
    qualified = False
    limits = WorkerLimits()

    def command(self) -> list[str]:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"kind": type(self).__name__, "qualified": self.qualified}

    def start(self) -> "WorkerSession":
        try:
            proc = subprocess.Popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1, env=self.environment())
        except OSError as e:
            raise WorkerUnavailable(f"the worker could not start: {e}") from None
        return WorkerSession(proc, self.limits, worker=self)

    def environment(self) -> dict:
        # Nothing of the host's environment: no keys, no paths beyond what the runner needs to start.
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1"}

    def kill(self, session: "WorkerSession") -> None:
        """Stop what a session started, whatever state it is in."""
        if session.proc.poll() is None:
            session.proc.kill()

    def healthy(self) -> bool:
        """Whether a fresh session loads nothing (as this host's runner) and answers: a failure that this passes
        was the package's."""
        return self.runner_problem() is None

    def runner_problem(self) -> str | None:
        """Why a fresh session of this worker cannot serve this host, or None: it does not start, dies or breaks the
        protocol on an empty load, or runs another worker_runner.py than the host's (StaleWorker, with the rebuild
        command). Read at startup (bench.harness_checks, App) so a stale image refuses before anything is paid."""
        try:
            session = self.start()
        except WorkerUnavailable as e:
            return str(e)
        try:
            session.load({})
            session.request({"op": "probe", "entries": []})
            return None
        except (WorkerCrashed, PackageCallError, WorkerUnavailable) as e:
            return str(e)
        finally:
            session.close(force=True)


class ProcessWorker(Worker):
    """The runner as a plain subprocess. Not isolated: for protocol tests only (qualified = False)."""
    qualified = False

    def __init__(self, limits: WorkerLimits | None = None, *, for_tests: bool = False):
        self.limits = limits or WorkerLimits()
        # Not isolated, so nothing runs package code in it unless a test says so by name (usable_worker).
        self.for_tests = for_tests

    def command(self) -> list[str]:
        return [sys.executable, "-I", str(RUNNER)]


class DockerWorker(Worker):
    """The qualified worker: the runner in a fixed image, with nothing of the host mounted."""
    qualified = True

    def __init__(self, image: str = DEFAULT_IMAGE, *, cpus: str = "1", memory: str = "256m", pids: int = 64,
                 limits: WorkerLimits | None = None, pinned_id: str | None = None):
        self.image, self.cpus, self.memory, self.pids = image, cpus, memory, pids
        self.limits = limits or WorkerLimits()
        # Run by the content id K froze, not by the tag: a re-tagged image is other bytes under the same name.
        self.pinned_id = pinned_id
        self._names: dict[int, str] = {}

    def command(self, name: str | None = None) -> list[str]:
        return ["docker", "run", "--rm", "-i", *(["--name", name] if name else []), "--network", "none",
                "--read-only", "--tmpfs", "/tmp:rw,size=64m", "--user", "65534:65534", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", str(self.pids), "--memory", self.memory,
                "--cpus", self.cpus, self.pinned_id or self.image,
                # The runner starts with exactly this environment: nothing of the host (docker run forwards
                # none) and nothing the base image declares either (its ENV would otherwise reach package code).
                "env", "-i", *WORKER_ENV, "python", "-I", "/runner/worker_runner.py"]

    def start(self) -> "WorkerSession":
        import uuid
        name = f"ouroloop-worker-{uuid.uuid4().hex[:12]}"
        try:
            proc = subprocess.Popen(self.command(name), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1, env=self.environment())
        except OSError as e:
            raise WorkerUnavailable(f"the worker could not start: {e}") from None
        session = WorkerSession(proc, self.limits, worker=self)
        self._names[id(session)] = name
        return session

    def kill(self, session: "WorkerSession") -> None:
        """Killing the docker client leaves the container running; remove the container itself."""
        super().kill(session)
        name = self._names.pop(id(session), None)
        if name:
            try:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30,
                               env=self.environment())
            except (OSError, subprocess.TimeoutExpired):
                pass

    def environment(self) -> dict:
        # The docker client's own settings (daemon address, context, config dir) and nothing else: the container
        # gets no environment from here, since docker run forwards none it is not told to.
        env = super().environment()
        env.update({k: v for k, v in os.environ.items() if k.startswith("DOCKER_") or k in ("HOME", "XDG_CONFIG_HOME")})
        return env

    def image_id(self) -> str | None:
        """The image's content id, or None when docker or the image is not there."""
        if shutil.which("docker") is None:
            return None
        try:
            out = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", self.image],
                                 capture_output=True, text=True, timeout=20, env=self.environment())
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout.strip() or None if out.returncode == 0 else None

    def describe(self) -> dict:
        return {**super().describe(), "image": self.image, "image_id": self.image_id(), "cpus": self.cpus,
                "memory": self.memory, "pids": self.pids}


def docker_worker(image: str = DEFAULT_IMAGE) -> DockerWorker | None:
    """The qualified worker when docker runs and the image is built; None otherwise."""
    worker = DockerWorker(image)
    return worker if worker.image_id() else None


# ---------------------------------------------------------------------------- one session
class WorkerSession:
    """One worker process: load a package once, then answer requests in order."""

    def __init__(self, proc: subprocess.Popen, limits: WorkerLimits, worker: Worker | None = None):
        self.proc, self.limits, self.worker = proc, limits, worker
        self._lines: queue.Queue = queue.Queue()
        self._stderr: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        # stderr is drained as it comes: a pipe nobody reads fills up and blocks the package mid-request.
        self._drain = threading.Thread(target=self._read_stderr, daemon=True)
        self._drain.start()

    def _read(self) -> None:
        # The reader does not know which op is in flight, so it allows the larger model_call bound; serve() holds
        # every other op to MAX_LINE once it has parsed the line.
        while True:
            line = self.proc.stdout.readline(MAX_MODEL_LINE)
            if not line:
                break
            if len(line) >= MAX_MODEL_LINE and not line.endswith("\n"):
                while True:                   # an oversized line: skip the rest of it, report it as such
                    rest = self.proc.stdout.readline(MAX_MODEL_LINE)
                    if not rest or rest.endswith("\n"):
                        break
                self._lines.put('{"op": "oversized"}')
                continue
            self._lines.put(line)
        self._lines.put(None)

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:
            self._stderr.append(line)
            while sum(len(s) for s in self._stderr) > STDERR_TAIL and len(self._stderr) > 1:
                self._stderr.pop(0)

    def stderr_tail(self) -> str:
        return "".join(self._stderr)[-STDERR_TAIL:]

    def _failed(self, message: dict, what: str) -> Exception:
        """A worker that died, hung or broke the protocol while running package code. When a fresh worker is
        healthy, the package did it: a candidate defect. Otherwise infrastructure."""
        self.close(force=True)
        tail = self.stderr_tail().strip()[-300:]
        detail = f"{what}" + (f"; stderr: {tail}" if tail else "")
        if message.get("op") in PACKAGE_OPS and self.worker is not None and self.worker.healthy():
            return PackageCallError(f"the package's code {detail} (a fresh worker answers normally)")
        return WorkerCrashed(f"the worker {detail}")

    def _send(self, obj: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise WorkerCrashed(f"the worker is gone: {e}") from None

    def base_call_handler(self, broker=None, max_calls: int | None = None):
        """The handler for the worker's base_call: the broker's answer as a base_result, bounded. Without a broker
        every call is answered as an error; past `max_calls` (the request's bound; None for no bound beyond the
        broker's own, as in a run, where the broker counts against the attempt's limits) likewise. A malformed call
        is the package's and is answered as an error, never raised on the host."""
        calls = [0]

        def handle(reply: dict) -> dict:
            calls[0] += 1
            name, args = reply.get("name"), reply.get("args") or {}
            if broker is None:
                output, is_error = "no tool calls are available here", True
            elif max_calls is not None and calls[0] > max_calls:
                output, is_error = f"more than {max_calls} tool calls in one request", True
            elif not isinstance(name, str) or not isinstance(args, dict):
                output, is_error = "call_tool takes a tool name and an object of arguments", True
            elif reply.get("read_only") and isinstance(broker, Broker):
                # The agent's hooks ask for a read-only broker (M1 §1): honoured here too, so the smoke refuses
                # what an attempt's broker would refuse, and a hook that writes is found in the draft check.
                output, is_error = broker(name, args, read_only=True)
            else:
                output, is_error = broker(name, args)
            return {"op": "base_result", "id": reply.get("id"), "output": str(output)[:self.limits.max_output_chars],
                    "is_error": bool(is_error)}
        return handle

    def serve(self, message: dict, *, handlers: dict, deadline: float) -> object:
        """Send one request and answer the worker's callbacks until its result. -> the result value.

        `handlers` maps a callback op (base_call, model_call, skill_read) to handler(reply) -> the dict to send
        back; a handler that raises LimitHit ends the session at once (the attempt is over, nothing the worker
        could still say matters) and LimitHit propagates with its dimension; any other exception of a handler ends
        the session too and propagates as it is. `deadline` is seconds from now for the
        whole exchange: a request's request_seconds, or the attempt's max_seconds for a run. The worker's error of
        kind "package" is AgentRunError for a run and PackageCallError for the bounded ops (M1 §1 失败语义); a
        callback with no handler, a protocol-kind error, a line past the op's bound, the deadline or an exit are
        _failed (infrastructure, or the package's when a fresh worker is healthy and the op is a bounded one).
        """
        self._send(message)
        op_sent = message.get("op")
        end = time.monotonic() + deadline
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise self._failed(message, f"did not answer {op_sent} within {deadline}s")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise self._failed(message, f"exited during {op_sent}")
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                raise self._failed(message, f"broke the protocol: {line[:120]!r}") from None
            op = reply.get("op")
            if op == "oversized":
                raise self._failed(message, f"sent a line longer than {MAX_MODEL_LINE} characters")
            if op != "model_call" and len(line) > MAX_LINE:
                raise self._failed(message, f"sent a {op} line longer than {MAX_LINE} characters")
            if op in handlers:
                try:
                    answer = handlers[op](reply)
                except LimitHit:
                    self.close(force=True)        # the attempt is over: nothing the worker could still say matters
                    raise
                except Exception:
                    # The host's own handler failed (a gateway or broker fault), so this is the caller's error, not
                    # the worker's; but the worker is blocked inside exchange() for an answer that never comes and
                    # the session has no consistent state to go on with, so it is closed before the error leaves.
                    self.close(force=True)
                    raise
                self._send(answer)
                continue
            if op == "result":
                return reply.get("value")
            if op == "error" and reply.get("kind") == "package":
                text = reply.get("message", "the package raised")
                raise AgentRunError(text) if op_sent == "run" else PackageCallError(text)
            raise self._failed(message, f"answered {op!r}: {str(reply.get('message', ''))[:200]}")

    def request(self, message: dict, broker=None) -> object:
        """Send one bounded request (a v1 op or smoke) and serve its broker calls until the result, within
        request_seconds. -> the result value."""
        if message.get("op") in ("call_tool", "hook"):
            message = {**message, "limit": self.limits.max_output_chars}
        elif message.get("op") == "smoke":
            # The smoke's own seconds limit is this request's deadline, so ctx.limits["seconds"] says what is left.
            message = {**message, "deadline": self.limits.request_seconds}
        return self.serve(message, handlers={"base_call": self.base_call_handler(broker, self.limits.max_base_calls)},
                          deadline=self.limits.request_seconds)

    def load(self, files: dict[str, str]) -> dict:
        """Load the package files and read the runner's identity back. -> {runner_sha256, ops}. A runner that is not
        this host's worker_runner.py ends the session as StaleWorker before any package code runs: whichever path
        reached here (an attempt, a draft check, a health probe), a stale image never serves."""
        identity = self.request({"op": "load", "files": files})
        problem = stale_runner(identity)
        if problem:
            self.close(force=True)
            raise StaleWorker(problem)
        return identity

    def close(self, force: bool = False) -> None:
        """End the session. A worker still running after its stdin closes — or any, with `force` — is killed
        through the worker that started it, which for Docker removes the container too."""
        if self.proc.poll() is None and not force:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                force = True
        if force:
            if self.worker is not None:
                self.worker.kill(self)
            elif self.proc.poll() is None:
                self.proc.kill()
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:                              # reaped before returning: a caller's poll() then says it is gone
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


# ---------------------------------------------------------------------------- the broker
@dataclass
class Broker:
    """The only way from package code to the business world: the episode's own base tools, each call through the
    surface's permission and budget checks, recorded. `read_only` (hooks) refuses any call the surface says may
    write. Package tools are not reachable from here, so a composite tool cannot call itself or another."""
    surface: object
    read_only: bool = False
    calls: list = field(default_factory=list)

    def __call__(self, name: str, args: dict, *, read_only: bool | None = None) -> tuple[str, bool]:
        """One call; `read_only` overrides the broker's own flag for this call alone (a hook's call through a
        broker the host built for the whole request)."""
        read_only = self.read_only if read_only is None else read_only
        base = {s.name for s in self.surface.specs()}
        if name not in base:
            self.calls.append({"tool": name, "refused": "not a base tool"})
            return f"{name!r} is not an authorised base tool: {sorted(base)}", True
        if read_only and self.surface.mutates(name, args):
            self.calls.append({"tool": name, "refused": "writes"})
            return "a hook may only make read-only calls, and this call may change the world", True
        res = self.surface.run(name, args)
        self.calls.append({"tool": name, "is_error": res.is_error})
        return res.output, res.is_error


# ---------------------------------------------------------------------------- draft checks (M5 §1)
def component_view(manifest: dict) -> dict:
    """The manifest's enabled hooks and tools as ctx.view["package"] carries them: what the default agent runs
    (M1 §1 hook 作为约定), the same projection in an attempt (runner.attempt_view) and in the smoke."""
    return {"hooks": [dict(h) for h in manifest.get("hooks") or () if h.get("enabled", True)],
            "tools": [dict(t) for t in manifest.get("tools") or () if t.get("enabled", True)]}


def smoke_view(manifest: dict, fixture, task: str, max_output_chars: int) -> dict:
    """ctx.view for the smoke, in the attempt view's shape: the fixture's base tools, the draft's enabled components
    (skills by name, no memory — the smoke reads no skill), no policies, no system prompt. The default agent reads
    its hooks, tools and base tools from here, so the smoke runs the draft's code as an attempt would."""
    base = [{"name": s.name, "description": s.description, "schema": s.schema} for s in fixture.specs()] \
        if fixture is not None else []
    pkg = {"api_version": manifest.get("api_version"), **component_view(manifest),
           "skills": [{"name": s["name"], "description": s.get("description", "")}
                      for s in manifest.get("skills") or () if s.get("enabled", True)],
           "memory": False}
    return {"task": task, "system": "", "base_tools": base, "package": pkg, "policies": {}, "events": [],
            "max_output_chars": max_output_chars}


def check_package(worker: Worker, files: dict[str, str], manifest: dict, *, fixture=None,
                  max_chars: int = 2000) -> list[str]:
    """isolated_probe, agent_smoke and package_test inside the worker. -> check errors (empty when all pass).

    The probe imports every entrypoint the manifest names. Every package that runs code (M0 §2 运行判定: a tool,
    hook or agent entry, or Python in the run bundle) is then smoked: its own agent through its `agent` entry, a
    package without one through the default agent — added to the loaded files as the host adds it at run time
    (runner.PackageAgent) — against the runner's SmokeContext, whose model calls each offered tool once and then
    ends, with the fixture broker for the tools and a view carrying the fixture's base tools and the draft's
    components. So an undeclared role, a deterministic crash in `run`, a hook that raises or answers outside its
    protocol and a package tool that does are all found here, as the draft's failure, before a paid run — which is
    what lets a run-time crash count as infrastructure (M0 §3, M1 §1 失败语义). The tests run each tests/*.py test_*
    function with a context whose broker is bound to `fixture` — a synthetic or train-only world, never a dev or
    final task. The output returned is capped and names failures, not tool outputs. WorkerUnavailable/WorkerCrashed
    propagate: they are infrastructure, not the draft's defect.
    """
    from .harness_package import needs_worker_at_runtime, usable_worker
    if not usable_worker(worker):
        raise WorkerUnavailable("the draft holds code and the worker given does not isolate it")
    session = worker.start()
    try:
        agent = manifest.get("agent") if isinstance(manifest.get("agent"), dict) else {}
        agent_entry = agent.get("entrypoint") if isinstance(agent.get("entrypoint"), str) else None
        run_files = {p: t for p, t in files.items() if not p.startswith("tests/")}
        loaded = dict(files)
        if not agent_entry and needs_worker_at_runtime(run_files, manifest):
            loaded[DEFAULT_PATH] = seed_files()[DEFAULT_PATH].decode("utf-8")
        session.load(loaded)
        entries = [t["entrypoint"] for t in manifest.get("tools") or ()] + \
                  [h["entrypoint"] for h in manifest.get("hooks") or ()] + ([agent_entry] if agent_entry else [])
        try:
            session.request({"op": "probe", "entries": entries})
        except PackageCallError as e:
            return [f"isolated_probe: {e}"[:max_chars]]
        if agent_entry or needs_worker_at_runtime(run_files, manifest):
            entry = agent_entry or DEFAULT_ENTRY
            roles = list(agent.get("roles") or ()) if agent_entry else ["act"]
            try:
                session.request({"op": "smoke", "entry": entry, "roles": roles, "task": "smoke",
                                 "view": smoke_view(manifest, fixture, "smoke", session.limits.max_output_chars)},
                                broker=Broker(fixture, read_only=False) if fixture else None)
            except PackageCallError as e:
                return [f"agent_smoke: {e}"[:max_chars]]
        if not any(p.startswith("tests/") for p in files):
            return []
        try:
            outcome = session.request({"op": "test"},
                                      broker=Broker(fixture, read_only=False) if fixture else None)
        except PackageCallError as e:            # a test that killed or hung the worker
            return [f"package_test: {e}"[:max_chars]]
        failed = (outcome or {}).get("failed") or []
        return [f"package_test: {f['test']}: {f['error']}"[:300] for f in failed][:10]
    finally:
        session.close()
