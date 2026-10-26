"""The isolated worker (docs/design/RUNTIME.md §2, §3, §4, §6).

The control process never imports package code. The package's agent, tools and hooks run in a worker process; the host
talks to it over stdin/stdout with the protocol of worker_runner.py and answers its callbacks through handlers it
supplies (the gateway for model calls, the broker for tool calls, skills, memory and marks), which live in the host and
never in the worker.

Two workers exist. DockerWorker is the qualified one: a fixed image run by the content id K froze, no host mounts, a
read-only root, user 65534, dropped capabilities, and every resource and the network from K's worker table
(WorkerResources.from_core: cpus, memory, memory_swap, pids, tmpfs_mb, env, and the network "none" or the egress proxy).
ProcessWorker runs the same runner as a plain subprocess; it has no isolation, reports `qualified = False`, and is for
protocol tests only — production paths refuse it unless a test names it (`for_tests`).

Failure kinds are kept apart: the package's own errors in a bounded op (call_tool, hook, probe, test, smoke, check) are
a candidate defect (PackageCallError); a worker that dies, times out or breaks the protocol is infrastructure
(WorkerCrashed) — unless a fresh worker then answers normally, in which case the package's code killed or hung it, and
that is the defect too. In a `run`, an exception the package itself marks as its defect (a hook outside its protocol,
a package tool that raised) is RunDefect (termination harness_error); any other is AgentRunError (infrastructure);
a handler that finds a hard limit reached raises LimitHit and the session ends there.

Every session's first `load` answers with the sha256 of the worker_runner.py it runs, compared with the host's copy:
an image built from another runner is StaleWorker, at startup and at every load.

Under a proxy K, a session starts with the attempt's egress session (an internal Docker network whose only way out is
the egress proxy) after Docker says the network is internal; anything else is EgressUnavailable. R's work tree
(`start_offline`) never gets a network.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..contracts import (CandidateDefect, EgressUnavailable, Infrastructure, LimitHit, StaleWorker,
                         WorkerCrashed, WorkerUnavailable)
from .broker import Broker

RUNNER = Path(__file__).with_name("worker_runner.py")
DEFAULT_IMAGE = "reharness-worker:py3.12"
REBUILD = f"rebuild the image: docker build --provenance=false -f docker/worker.Dockerfile -t {DEFAULT_IMAGE} ."
MAX_LINE = 1_000_000                      # one protocol line of the v1 ops; anything longer is not a protocol message
MAX_MODEL_LINE = 8_000_000                # a model_call carries the agent's whole history; the reader allows this much
STDERR_TAIL = 4000                        # what is kept of the worker's stderr, for a failure message
# The requests that run package code in a bounded way, so a hang or death in them with a healthy fresh worker is
# the package's defect. `run` is not here: an attempt-long run is ended by the host's limits, and whatever ends it
# early is infrastructure, never reclassified.
PACKAGE_OPS = ("probe", "call_tool", "hook", "test", "smoke", "check")
WORKER_ENV = ("PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8")   # the whole environment package code sees
# The worker's network is K's — "none" (the benchmark default: the world is
# simulated, and a network would only widen what can leak, the real date first), or "proxy": the attempt's egress
# session (an internal Docker network whose only way out is the host-side egress proxy, and the proxy variables) is
# attached at start. The names a K may add to the environment, and the networks a worker may never join.
NETWORKS = ("none", "proxy")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DOCKER_NETWORK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
OPEN_NETWORKS = ("host", "bridge", "default", "none")
# The variables an egress session sets (runtime/egress.py: the proxy URL with the attempt's credential, upper and lower
# case, and the flag that offers http_fetch). They are the session's alone: K's `env` may not set them (under
# "none" they would offer a way out that does not exist, under "proxy" they would replace the attempt's credential),
# and a session's env holds nothing else.
EGRESS_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy",
              "all_proxy", "REHARNESS_EGRESS")
# K's worker table (§4.1) as docker_worker reads it: the resources, the network, the environment and the bounds.
RESOURCE_KEYS = ("image", "image_id", "cpus", "memory", "memory_swap", "pids", "tmpfs_mb", "network", "env",
                 "request_seconds", "max_output_chars")





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
    seen = f"runs worker_runner.py {digest[:12]}" if isinstance(digest, str) else \
        "runs a worker_runner.py that does not name itself"
    return (f"the worker {seen}, and this host's is {host[:12]}: the image was built from another runner; "
            f"{REBUILD}")



class PackageCallError(CandidateDefect):
    """The package's code raised, or a hook answered outside its protocol: the candidate's defect. Only for the
    bounded ops (call_tool, hook, probe, test, smoke)."""


class AgentRunError(Infrastructure):
    """The package's agent raised during a `run` (or returned something other than a string). Infrastructure: the
    attempt is invalid, because a deterministic crash is the draft smoke's to catch and what is left at run time is the
    world's — a model answer, a tool failure."""


class RunDefect(CandidateDefect):
    """The package marked the exception that ended its run as its own defect (a hook outside its protocol, a package
    tool that raised): the attempt ends as termination harness_error and is scored as the world stands."""


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

    def start(self, egress=None) -> "WorkerSession":
        if egress is not None:
            raise ValueError(f"{type(self).__name__} has no network of its own to attach an egress session to")
        try:
            proc = subprocess.Popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1, env=self.environment())
        except OSError as e:
            raise WorkerUnavailable(f"the worker could not start: {e}") from None
        return WorkerSession(proc, self.limits, worker=self)

    def _health_session(self) -> "WorkerSession":
        """A session for runner_problem: it loads nothing and runs no package code."""
        return self.start()

    def start_offline(self) -> "WorkerSession":
        """A session that runs no package code — R's coding session's file tree (`work` actions only) — and so needs
        no network whatever K's is: here the same as start()."""
        return self.start()

    def egress_session(self, attempt_id: str, *, read_only: bool = False, max_requests: int | None = None):
        """The egress session a session of this worker starts with: None here (no network of its own)."""
        return None

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
        command). Read at startup (activity/checks.py, App) so a stale image refuses before anything is paid."""
        try:
            session = self._health_session()
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


def env_items(env, *, egress: bool = False) -> tuple[str, ...]:
    """K's extra worker environment (a mapping, or NAME=value strings) as the NAME=value items `env -i` takes after
    WORKER_ENV. A name is a shell identifier, a value one line; PATH and LANG stay the runner's, and the proxy
    variables (EGRESS_ENV) the egress session's. With `egress`, the items are an egress session's own: only
    EGRESS_ENV names."""
    if env is None:
        return ()
    items = [f"{k}={v}" for k, v in env.items()] if isinstance(env, Mapping) else list(env)
    fixed = {e.split("=", 1)[0] for e in WORKER_ENV}
    out = []
    for item in items:
        if not isinstance(item, str) or "=" not in item:
            raise ValueError(f"worker environment {item!r}: NAME=value")
        name, _, value = item.partition("=")
        if not ENV_NAME.fullmatch(name) or any(c in value for c in "\x00\n\r"):
            raise ValueError(f"worker environment {name!r}: a NAME of letters, digits and '_', a value of one line")
        if name in fixed:
            raise ValueError(f"worker environment {name}: the runner's own (WORKER_ENV), not K's to set")
        if egress and name not in EGRESS_ENV:
            raise ValueError(f"egress session environment {name}: a session sets the proxy variables "
                             f"{list(EGRESS_ENV)} and nothing else")
        if not egress and name in EGRESS_ENV:
            raise ValueError(f"worker environment {name}: the egress session's (EGRESS_ENV), not K's to set: K's "
                             f"network decides whether the worker has a proxy")
        out.append(item)
    return tuple(out)


def network_mode(network) -> str:
    """K's `worker.network` as a mode: "none", or "proxy" for {mode: proxy, allow: [...]} (the allowlist is the
    egress pool's, not the worker's)."""
    mode = network.get("mode") if isinstance(network, Mapping) else network
    if mode not in NETWORKS:
        raise ValueError(f"worker network {network!r}: one of {list(NETWORKS)}, or {{mode: proxy, allow: [...]}}")
    return mode


class DockerWorker(Worker):
    """The qualified worker: the runner in a fixed image, with nothing of the host mounted.

    Every resource comes from K's worker table (WorkerResources): `cpus`, `memory`, `memory_swap` (when set,
    `--memory-swap`; equal to `memory` it turns swap off), `pids`, `tmpfs_mb` (the writable /tmp), `env` (after
    WORKER_ENV inside `env -i`) and `network`: "none", or "proxy", where `start(egress=...)` must be handed the
    attempt's egress session — any object with `.network` (the internal Docker network to join) and `.env` (the proxy
    variables). The isolation flags (`--read-only`, user 65534, `--cap-drop ALL`, `no-new-privileges`, `python -I`)
    are not settings."""
    qualified = True

    def __init__(self, resources: "WorkerResources | None" = None, *, egress_pool=None):
        r = resources or WorkerResources()
        self.resources = r
        self.image, self.cpus, self.memory, self.pids = r.image, r.cpus, r.memory, r.pids
        self.limits = WorkerLimits(request_seconds=r.request_seconds, max_output_chars=r.max_output_chars)
        # Run by the content id K froze, not by the tag: a re-tagged image is other bytes under the same name.
        self.pinned_id = r.image_id
        self.memory_swap, self.tmpfs_mb, self.network = r.memory_swap, r.tmpfs_mb, network_mode(r.network)
        self.extra_env = env_items(r.env)
        # for network "proxy": the activity's egress pool (runtime.egress: .session(attempt_id, read_only=,
        # max_requests=) -> an EgressSession), whose sessions this worker's sessions start with
        self.egress_pool = egress_pool
        self._names: dict[int, str] = {}

    def command(self, name: str | None = None, egress=None) -> list[str]:
        return ["docker", "run", "--rm", "-i", *(["--name", name] if name else []),
                "--network", "none" if egress is None else egress.network,
                "--read-only", "--tmpfs", f"/tmp:rw,size={self.tmpfs_mb}m", "--user", "65534:65534",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", str(self.pids),
                "--memory", self.memory, *(["--memory-swap", self.memory_swap] if self.memory_swap is not None else []),
                "--cpus", self.cpus, self.pinned_id or self.image,
                # The runner starts with exactly this environment: nothing of the host (docker run forwards
                # none) and nothing the base image declares either (its ENV would otherwise reach package code).
                "env", "-i", *WORKER_ENV, *self.extra_env, *(env_items(egress.env, egress=True) if egress is not None
                                                             else ()),
                "python", "-I", "/runner/worker_runner.py"]

    def _egress(self, egress):
        """The egress session K's network calls for, checked as data: none for "none", one for "proxy", whose network
        is a named Docker network the worker may join (never the host's, the default bridge or none) and whose env
        holds the proxy variables only. A missing session is EgressUnavailable; a malformed one is the caller's bug
        (ValueError)."""
        if self.network == "none":
            if egress is not None:
                raise ValueError("this worker's network is none (K): an egress session cannot be attached")
            return None
        if egress is None:
            raise EgressUnavailable("this worker's network is proxy (K): start(egress=...) needs the attempt's egress "
                                    "session, and none was given")
        network, env = getattr(egress, "network", None), getattr(egress, "env", None)
        if not isinstance(network, str) or not DOCKER_NETWORK.fullmatch(network) or network in OPEN_NETWORKS:
            raise ValueError(f"egress network {network!r}: the attempt's own internal Docker network, not "
                             f"{list(OPEN_NETWORKS)}")
        if not isinstance(env, Mapping):
            raise ValueError("an egress session's env is a mapping of the proxy variables")
        env_items(env, egress=True)
        return egress

    def internal_network(self, network: str) -> None:
        """The network exists and is `--internal` (Docker's .Internal), or EgressUnavailable: a user network with a
        route out would give package code the internet past the proxy and its allowlist. Asked of Docker at every
        start, so a network removed and re-created without the flag is caught on the next session."""
        try:
            out = subprocess.run(["docker", "network", "inspect", "--format", "{{.Internal}}", network],
                                 capture_output=True, text=True, timeout=30, env=self.environment())
        except (OSError, subprocess.TimeoutExpired) as e:
            raise EgressUnavailable(f"egress network {network!r} could not be inspected: {e}") from None
        if out.returncode != 0:
            raise EgressUnavailable(f"egress network {network!r} does not exist: "
                                    f"{(out.stderr or out.stdout).strip()[:200]}")
        if out.stdout.strip() != "true":
            raise EgressUnavailable(f"egress network {network!r} is not internal (docker network create --internal): "
                                    f"a worker on it could reach the internet past the egress proxy")

    def start(self, egress=None) -> "WorkerSession":
        egress = self._egress(egress)
        if egress is not None:
            self.internal_network(egress.network)
        return self._spawn(egress)

    def _health_session(self) -> "WorkerSession":
        # The health probe loads nothing and runs no package code, so it never needs the network: a proxy worker is
        # probed without an egress session.
        return self._spawn(None)

    def start_offline(self) -> "WorkerSession":
        # R's work tree runs no package code either: `--network none` whatever K's network is (no egress session, so
        # no credential and nothing to record)
        return self._spawn(None)

    def egress_session(self, attempt_id: str, *, read_only: bool = False, max_requests: int | None = None):
        """The session a session of this worker starts with under K's network: None for "none"; for "proxy" a fresh
        credential and network from the activity's pool — read-only (GET and HEAD, never a tunnel) for a draft check,
        a smoke or a replay; capped at `max_requests` requests let out (an attempt's tool calls left). No pool, or a
        proxy that cannot open one, is EgressUnavailable: infrastructure, never a start without the proxy."""
        if self.network == "none":
            return None
        if self.egress_pool is None:
            raise EgressUnavailable("this worker's network is proxy (K) and it has no egress pool to open the "
                                    "session's credential and network from")
        from .egress import EgressError
        try:
            return self.egress_pool.session(attempt_id, read_only=read_only, max_requests=max_requests)
        except EgressError as e:
            raise EgressUnavailable(f"the egress proxy could not open a session: {e}") from None

    def _spawn(self, egress) -> "WorkerSession":
        import uuid
        name = f"reharness-worker-{uuid.uuid4().hex[:12]}"
        try:
            proc = subprocess.Popen(self.command(name, egress), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1, env=self.environment())
        except OSError as e:
            raise WorkerUnavailable(f"the worker could not start: {e}") from None
        session = WorkerSession(proc, self.limits, worker=self)
        self._names[id(session)] = name
        session.container = name
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
        """The content id of the image this worker runs — the one K pinned (pinned_id) when it pinned one, the tag's
        otherwise — or None when docker or that image is not there."""
        if shutil.which("docker") is None:
            return None
        try:
            out = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", self.pinned_id or self.image],
                                 capture_output=True, text=True, timeout=20, env=self.environment())
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout.strip() or None if out.returncode == 0 else None

    def describe(self) -> dict:
        # image_id is the id of what runs (the pinned one when K pinned it), so a worker_session record names the
        # bytes that ran even after the tag moved
        return {**super().describe(), "image": self.image, "image_id": self.image_id(), "cpus": self.cpus,
                "memory": self.memory, "pids": self.pids, "memory_swap": self.memory_swap,
                "tmpfs_mb": self.tmpfs_mb, "network": self.network}


@dataclass(frozen=True)
class WorkerResources:
    """K's worker table (CONTRACTS §4 `worker`): every resource and the network of a DockerWorker. Nothing in the code
    picks a memory size or a pid count; a K that leaves one out gets the preset's value (activity/presets.py)."""
    image: str = DEFAULT_IMAGE
    image_id: str | None = None
    cpus: str = "1"
    memory: str = "512m"
    memory_swap: str | None = None
    pids: int = 128
    tmpfs_mb: int = 64
    network: object = "none"                   # "none" or {"mode": "proxy", "allow": [{host, port, methods}]}
    env: dict = field(default_factory=dict)
    request_seconds: float = 30.0
    max_output_chars: int = 20000

    def __post_init__(self) -> None:
        for key in ("pids", "tmpfs_mb", "max_output_chars"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"worker {key} {value!r}: a positive integer")
        network_mode(self.network)
        env_items(self.env)

    @classmethod
    def from_core(cls, core: dict) -> "WorkerResources":
        table = dict((core or {}).get("worker") or {})
        unknown = sorted(set(table) - set(RESOURCE_KEYS))
        if unknown:
            raise ValueError(f"worker resources {unknown}: K's worker table takes {list(RESOURCE_KEYS)}")
        if "cpus" in table:
            table["cpus"] = str(table["cpus"])
        for key in ("memory", "memory_swap"):
            if table.get(key) is not None:
                table[key] = str(table[key])
        if "max_output_chars" not in table and (core or {}).get("limits", {}).get("max_output_chars"):
            table["max_output_chars"] = int(core["limits"]["max_output_chars"])
        return cls(**table)

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in RESOURCE_KEYS}


def docker_worker(resources: WorkerResources | None = None, *, egress_pool=None) -> "DockerWorker | None":
    """The qualified worker when docker runs and its image is there (the pinned id when K pinned one), else None."""
    worker = DockerWorker(resources or WorkerResources(), egress_pool=egress_pool)
    return worker if worker.image_id() else None


def usable_worker(worker) -> bool:
    """A worker package code may run in: an isolating one, or a protocol-test worker a test named as such."""
    return worker is not None and (bool(getattr(worker, "qualified", False)) or bool(getattr(worker, "for_tests",
                                                                                              False)))


# ---------------------------------------------------------------------------- one session
class WorkerSession:
    """One worker process: load a package once, then answer requests in order."""

    def __init__(self, proc: subprocess.Popen, limits: WorkerLimits, worker: Worker | None = None):
        self.proc, self.limits, self.worker = proc, limits, worker
        self.container: str | None = None     # the Docker container's name, for a DockerWorker's session
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
                # A hook and a non-writing tool ask for a read-only broker: honoured here too, so the smoke refuses
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
        kind "package" is AgentRunError for a run (kind "defect": RunDefect) and PackageCallError for the bounded ops; a
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
            if op == "error" and reply.get("kind") in ("package", "defect"):
                text = reply.get("message", "the package raised")
                if op_sent != "run":
                    raise PackageCallError(text)
                raise RunDefect(text) if reply.get("kind") == "defect" else AgentRunError(text)
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

    def work(self, action: str, **fields) -> dict:
        """One action on R's coding session's file tree in this worker (worker_runner.py `work`): ->
        {"ok": True, "value": ...} or {"ok": False, "error": ...}. A refused action is the session tool's error and
        is handed to R as the tool result; no package code runs in it, so a worker that dies here is infrastructure
        (WorkerCrashed), as for any op outside PACKAGE_OPS."""
        answer = self.request({"op": "work", "action": action, **fields})
        if not isinstance(answer, dict) or "ok" not in answer:
            raise WorkerCrashed(f"the worker answered the work action {action!r} outside its protocol")
        return answer

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


# ---------------------------------------------------------------------------- draft checks
def package_view(manifest: dict) -> dict:
    """ctx.view["package"]: what the adapter reads to run the package — its enabled hooks and tools, the memory
    switch, the config knobs, the effort its model calls ask for (the gateway caps it at K's) — the same projection in
    an attempt (runtime/runner.py) and in the smoke."""
    return {"hooks": [dict(h) for h in manifest.get("hooks") or () if h.get("enabled", True)],
            "tools": [dict(t) for t in manifest.get("tools") or () if t.get("enabled", True)],
            "memory": {"enabled": bool((manifest.get("memory") or {}).get("enabled"))},
            "config": dict(manifest.get("config") or {}),
            "model": {"effort": (manifest.get("model") or {}).get("effort")}}


def smoke_view(manifest: dict, fixture, max_output_chars: int, hook_aux_calls: int = 1) -> dict:
    """ctx.view for the smoke, in the attempt view's shape: the fixture's base tools and the draft's components; a
    hook that calls the aux model gets one call, answered by the smoke's scripted model (IMPROVER §8)."""
    base = [{"name": s.name, "description": s.description, "schema": s.schema} for s in fixture.specs()] \
        if fixture is not None else []
    return {"system": "", "base_tools": base, "package": package_view(manifest), "last_usage": None, "events": [],
            "max_output_chars": max_output_chars, "hook_aux_calls": int(hook_aux_calls)}


def check_package(worker: Worker, files: dict[str, str], manifest: dict, *, fixture=None, max_chars: int = 2000,
                  egress=None) -> list[str]:
    """The V1 checks inside the worker (IMPROVER §8): probe (import every entrypoint the manifest names), smoke (the
    package's agent against the runner's SmokeContext, whose model calls each offered tool once and then ends, with the
    fixture broker for the tools), and the package's tests/*.py. -> check errors (empty when all pass). `files` are the
    package's files without the host-only memory; `manifest` its parsed manifest as JSON; `fixture` a synthetic or
    train-only world, never a dev or test task. WorkerUnavailable and WorkerCrashed propagate: infrastructure, not the
    draft's defect."""
    if not usable_worker(worker):
        raise WorkerUnavailable("the draft holds code and the worker given does not isolate it")
    session = worker.start(egress=egress) if egress is not None else worker.start()
    try:
        session.load(dict(files))
        agent = manifest.get("agent") or {}
        entries = [t["entrypoint"] for t in manifest.get("tools") or ()] + \
                  [h["entrypoint"] for h in manifest.get("hooks") or ()] + [agent["entrypoint"]]
        try:
            session.request({"op": "probe", "entries": entries})
        except PackageCallError as e:
            return [f"probe: {e}"[:max_chars]]
        try:
            session.request({"op": "smoke", "entry": agent["entrypoint"], "roles": list(agent.get("roles") or ()),
                             "task": "smoke", "view": smoke_view(manifest, fixture, session.limits.max_output_chars)},
                            broker=Broker(fixture, read_only=False) if fixture else None)
        except PackageCallError as e:
            return [f"smoke: {e}"[:max_chars]]
        if not any(p.startswith("tests/") for p in files):
            return []
        try:
            outcome = session.request({"op": "test"}, broker=Broker(fixture, read_only=False) if fixture else None)
        except PackageCallError as e:            # a test that killed or hung the worker
            return [f"test: {e}"[:max_chars]]
        failed = (outcome or {}).get("failed") or []
        return [f"test: {f['test']}: {f['error']}"[:300] for f in failed][:10]
    finally:
        session.close()
