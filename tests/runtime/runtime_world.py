"""Shared pieces of the runtime tests (docs/design/RUNTIME.md §8): a small tool surface, scripted providers, worker
sessions over the host's ProcessWorker, the stirrup seed under a test K, one attempt under the runner shell, and the
Docker gate (a skip without Docker or the image, a failure under REHARNESS_TEST_REQUIRE_DOCKER)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from reharness.contracts import Objects
from reharness.evaluate.attempt import Budgeted
from reharness.evaluate.limits import Budget
from reharness.harness.package import Limits, build
from reharness.harness.seed import build_seed
from reharness.runtime.chat import ChatTurn, ToolCall, ToolSpec
from reharness.runtime.records import Transcript, read, session_file
from reharness.runtime.runner import PackageAgent
from reharness.runtime.worker import ProcessWorker, WorkerLimits

LIMITS = {"max_steps": 8, "max_model_calls": 20, "max_tool_calls": 10, "max_seconds": 60, "max_tokens": 10000,
          "max_output_chars": 20000}
CORE = {"model": {"identity": "scripted-1", "effort": "medium", "max_effort": "medium"},
        "memory": {"enabled": False}, "limits": LIMITS, "worker": {"network": "none"}}
ATTEMPT = "run1:t1:0:subject"


def core(**sections) -> dict:
    """CORE with some sections replaced (a section given as a dict is merged into CORE's)."""
    out = json.loads(json.dumps(CORE))
    for name, table in sections.items():
        out[name] = {**out.get(name, {}), **table} if isinstance(table, dict) else table
    return out


class World:
    """A tiny business surface: api_search reads, api_fetch with a method other than GET writes."""

    def __init__(self):
        self.calls = []

    def specs(self):
        return [ToolSpec("api_search", "search", {"type": "object", "properties": {"q": {"type": "string"}}}),
                ToolSpec("api_fetch", "fetch", {"type": "object", "properties": {"url": {"type": "string"},
                                                                                  "method": {"type": "string"}}})]

    def run(self, name, args):
        self.calls.append((name, dict(args)))
        return SimpleNamespace(output=f"{name} ok", is_error=False)

    def mutates(self, name, args):
        return name == "api_fetch" and str(args.get("method", "GET")).upper() != "GET"

    @property
    def tool_calls(self):
        return len(self.calls)


def turn(text: str = "", *calls, stop: str | None = None, usage: dict | None = None) -> ChatTurn:
    """A provider's turn: `calls` are (name, args) pairs, given ids c<i>."""
    tool_calls = [ToolCall(f"c{i}", name, dict(args)) for i, (name, args) in enumerate(calls, 1)]
    return ChatTurn(text, tool_calls, stop or ("tool_use" if tool_calls else "end"), usage=dict(usage or {}))


def finish(reason: str = "Done.", **kw) -> ChatTurn:
    return turn("", ("finish", {"reason": reason}), **kw)


class Script:
    """A provider that answers from a list of turns (the last one repeats) and keeps what it was sent. Its chat takes
    no `effort`, so the gateway never hands it one."""
    name = "scripted"
    model = "scripted-1"

    def __init__(self, turns, *, fail: Exception | None = None):
        self.turns, self.fail = list(turns), fail
        self.n = 0
        self.seen: list[dict] = []

    def _next(self, system, messages, specs, effort=None) -> ChatTurn:
        self.seen.append({"system": system, "messages": messages, "tools": [s.name for s in specs or ()],
                          "effort": effort})
        if self.fail is not None:
            raise self.fail
        t = self.turns[min(self.n, len(self.turns) - 1)]
        self.n += 1
        # each call's tool calls get ids of their own, as a model's do (a repeated turn would repeat them)
        return ChatTurn(t.text, [ToolCall(f"r{self.n}.{c.id}", c.name, dict(c.args)) for c in t.tool_calls], t.stop,
                        usage=dict(t.usage or {}))

    def chat(self, system, messages, specs):
        return self._next(system, messages, specs)


class EffortAware(Script):
    """A provider whose chat takes `effort`, pinned at its own tier (what a call without a request runs at)."""

    def __init__(self, turns, *, effort: str | None = None):
        super().__init__(turns)
        self.effort = effort

    def chat(self, system, messages, specs, effort=None):
        return self._next(system, messages, specs, effort)

    @property
    def efforts(self):
        return [s["effort"] for s in self.seen]


def session_with(files: dict, **limits):
    """A ProcessWorker session with `files` loaded; `limits` are WorkerLimits fields."""
    worker = ProcessWorker(WorkerLimits(**limits), for_tests=True)
    session = worker.start()
    session.load(files)
    return session


def runner_module():
    """runtime/worker_runner.py imported for its names. On import it points fd 1 at stderr (the protocol keeps its own
    descriptor), so fd 1 and sys.stdout are put back afterwards."""
    if "reharness.runtime.worker_runner" in sys.modules:
        return sys.modules["reharness.runtime.worker_runner"]
    saved_fd, saved_out = os.dup(1), sys.stdout
    try:
        import reharness.runtime.worker_runner as wr
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
        sys.stdout = saved_out
    return wr


# ---------------------------------------------------------------------------- the seed and one attempt
def seed(root: Path, k: dict = CORE):
    return build_seed("stirrup", core=k, objects=Objects(root))


def variant(root: Path, base, changes: dict, *, manifest=None, k: dict = CORE):
    """`base`'s files with `changes` ({path: text, or None to delete}) and the manifest edited by `manifest(m)`,
    built as a package under K."""
    files = {**base.files}
    for path, text in changes.items():
        if text is None:
            files.pop(path, None)
        else:
            files[path] = text
    if manifest is not None:
        m = json.loads(files["manifest.json"])
        manifest(m)
        files["manifest.json"] = json.dumps(m)
    return build(Objects(root), files, limits=Limits.from_core(k), seed=base)


def minimal(root: Path, agent: str, *, roles=("act",), k: dict = CORE):
    """A package of one agent file and nothing else, under K."""
    manifest = {"api_version": "harness_package.v3", "agent": {"entrypoint": "agent/main.py:run", "roles": list(roles)},
                "model": {"base": k["model"]["identity"], "effort": k["model"]["effort"]}}
    return build(Objects(root), {"manifest.json": json.dumps(manifest), "agent/main.py": agent},
                 limits=Limits.from_core(k), seed=None)


def attempt(root: Path, package, provider, *, world=None, k: dict = CORE, worker=None, task: str = "search for x",
            system: str = "You are a business agent.", attempt_id: str = ATTEMPT, raises=None, **agent_kw):
    """One attempt of `package` under the runner shell over World (or `world`), in a ProcessWorker unless another
    worker is given. -> result, the transcript's lines, the world, the surface and the agent."""
    world = world if world is not None else World()
    surface = Budgeted(world, Budget.from_core(k["limits"]))
    transcript = Transcript(session_file(root, attempt_id))
    agent = PackageAgent(provider=provider, package=package, core=k, surface=surface,
                         worker=worker if worker is not None else ProcessWorker(for_tests=True),
                         transcript=transcript, system=system, attempt_id=attempt_id, **agent_kw)
    if raises is not None:
        with pytest.raises(raises) as info:
            agent.run(task)
        result = info.value
    else:
        result = agent.run(task)
    return SimpleNamespace(result=result, lines=read(transcript.path), world=world, surface=surface, agent=agent)


def kinds(lines, *only) -> list[str]:
    return [line["kind"] for line in lines if not only or line["kind"] in only]


def of(lines, kind: str) -> list[dict]:
    return [line for line in lines if line["kind"] == kind]


# ---------------------------------------------------------------------------- Docker
def require(available: bool, why: str) -> None:
    """Skip without Docker or an image; fail instead where the full run demands them."""
    if available:
        return
    if os.environ.get("REHARNESS_TEST_REQUIRE_DOCKER"):
        pytest.fail(f"REHARNESS_TEST_REQUIRE_DOCKER is set and {why}")
    pytest.skip(why)


def docker_running() -> bool:
    try:
        return subprocess.run(["docker", "version"], capture_output=True, timeout=20).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def image_present(image: str) -> bool:
    return docker_running() and subprocess.run(["docker", "image", "inspect", image], capture_output=True,
                                               timeout=30).returncode == 0


def docker(*args, check: bool = True, env: dict | None = None, timeout: float = 120) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(env or {})})
    if check and done.returncode != 0:
        raise AssertionError(f"docker {' '.join(args[:3])}: {done.stderr.strip()[-800:]}")
    return done.stdout


def inspect(name: str, fmt: str) -> str:
    return docker("inspect", "--format", fmt, name, check=False).strip()
