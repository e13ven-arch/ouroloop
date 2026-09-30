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
"""
from __future__ import annotations

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

RUNNER = Path(__file__).with_name("worker_runner.py")
DEFAULT_IMAGE = "ouroloop-worker:py3.12"
MAX_LINE = 1_000_000                      # one protocol line; anything longer is not a protocol message
STDERR_TAIL = 4000                        # what is kept of the worker's stderr, for a failure message
PACKAGE_OPS = ("probe", "call_tool", "hook", "test")       # the requests that run package code


class WorkerUnavailable(RuntimeError):
    """No qualified worker: an infrastructure condition, never a reason to run package code in-process."""


class WorkerCrashed(RuntimeError):
    """The worker died, timed out or broke the protocol: infrastructure, not evidence about the package."""


class PackageCallError(ct.CandidateDefect):
    """The package's code raised, or a hook answered outside its protocol: the candidate's defect."""


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
        """Whether a fresh session loads nothing and answers: a failure that this passes was the package's."""
        try:
            session = self.start()
        except WorkerUnavailable:
            return False
        try:
            session.request({"op": "load", "files": {}})
            session.request({"op": "probe", "entries": []})
            return True
        except (WorkerCrashed, PackageCallError):
            return False
        finally:
            session.close(force=True)


class ProcessWorker(Worker):
    """The runner as a plain subprocess. Not isolated: for protocol tests only (qualified = False)."""
    qualified = False

    def __init__(self, limits: WorkerLimits | None = None):
        self.limits = limits or WorkerLimits()

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
                "--cpus", self.cpus, self.pinned_id or self.image, "python", "-I", "/runner/worker_runner.py"]

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
        while True:
            line = self.proc.stdout.readline(MAX_LINE)
            if not line:
                break
            if len(line) >= MAX_LINE and not line.endswith("\n"):
                while True:                   # an oversized line: skip the rest of it, report it as such
                    rest = self.proc.stdout.readline(MAX_LINE)
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

    def request(self, message: dict, broker=None) -> object:
        """Send one request and serve its broker calls until the result. -> the result value."""
        if message.get("op") in ("call_tool", "hook"):
            message = {**message, "limit": self.limits.max_output_chars}
        self._send(message)
        deadline = time.monotonic() + self.limits.request_seconds
        calls = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._failed(message, f"did not answer {message.get('op')} within "
                                            f"{self.limits.request_seconds}s")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise self._failed(message, f"exited during {message.get('op')}")
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                raise self._failed(message, f"broke the protocol: {line[:120]!r}") from None
            op = reply.get("op")
            if op == "oversized":
                raise self._failed(message, f"sent a line longer than {MAX_LINE} characters")
            if op == "base_call":
                calls += 1
                name, args = reply.get("name"), reply.get("args") or {}
                if broker is None or calls > self.limits.max_base_calls:
                    output, is_error = ("no tool calls are available here" if broker is None
                                        else f"more than {self.limits.max_base_calls} tool calls in one request"), True
                elif not isinstance(name, str) or not isinstance(args, dict):
                    # The package's malformed call: answered as an error, never raised on the host.
                    output, is_error = "call_tool takes a tool name and an object of arguments", True
                else:
                    output, is_error = broker(name, args)
                self._send({"op": "base_result", "id": reply.get("id"),
                            "output": str(output)[:self.limits.max_output_chars], "is_error": bool(is_error)})
                continue
            if op == "result":
                return reply.get("value")
            if op == "error" and reply.get("kind") == "package":
                raise PackageCallError(reply.get("message", "the package raised"))
            raise self._failed(message, f"answered {op!r}: {str(reply.get('message', ''))[:200]}")

    def load(self, files: dict[str, str]) -> None:
        self.request({"op": "load", "files": files})

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


# ---------------------------------------------------------------------------- the broker
@dataclass
class Broker:
    """The only way from package code to the business world: the episode's own base tools, each call through the
    surface's permission and budget checks, recorded. `read_only` (hooks) refuses any call the surface says may
    write. Package tools are not reachable from here, so a composite tool cannot call itself or another."""
    surface: object
    read_only: bool = False
    calls: list = field(default_factory=list)

    def __call__(self, name: str, args: dict) -> tuple[str, bool]:
        base = {s.name for s in self.surface.specs()}
        if name not in base:
            self.calls.append({"tool": name, "refused": "not a base tool"})
            return f"{name!r} is not an authorised base tool: {sorted(base)}", True
        if self.read_only and self.surface.mutates(name, args):
            self.calls.append({"tool": name, "refused": "writes"})
            return "a hook may only make read-only calls, and this call may change the world", True
        res = self.surface.run(name, args)
        self.calls.append({"tool": name, "is_error": res.is_error})
        return res.output, res.is_error


# ---------------------------------------------------------------------------- draft checks (M5 §1)
def check_package(worker: Worker, files: dict[str, str], manifest: dict, *, fixture=None,
                  max_chars: int = 2000) -> list[str]:
    """isolated_probe and package_test inside the worker. -> check errors (empty when both pass).

    The probe imports every entrypoint the manifest names; the tests run each tests/*.py test_* function with a
    context whose broker is bound to `fixture` — a synthetic or train-only world, never a dev or final task. The
    output returned is capped and names failures, not tool outputs. WorkerUnavailable/WorkerCrashed propagate: they
    are infrastructure, not the draft's defect.
    """
    session = worker.start()
    try:
        session.load(files)
        entries = [t["entrypoint"] for t in manifest.get("tools") or ()] + \
                  [h["entrypoint"] for h in manifest.get("hooks") or ()]
        try:
            session.request({"op": "probe", "entries": entries})
        except PackageCallError as e:
            return [f"isolated_probe: {e}"[:max_chars]]
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
