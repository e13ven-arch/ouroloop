"""The runner inside the isolated worker (docs/modules/01_POLICY_RUNTIME.md §1). Standard library only.

It is the only program the worker image runs. The host sends the package files over stdin; the runner writes them
to a scratch directory, imports the entrypoints the host names, and answers one request at a time. Package code
reaches the business world only through `ctx.call_tool`, which asks the host's broker over the same pipe: the
worker has no network, no host files and no credentials, so the broker is the only way out.

Protocol: one JSON object per line. Host -> worker: {"op": "load", "files": {...}}, {"op": "call_tool", "entry",
"args", "view"}, {"op": "hook", "entry", "event", "view"}, {"op": "probe", "entries"}, {"op": "test"},
{"op": "base_result", "id", "output", "is_error"}. Worker -> host: {"op": "base_call", "id", "name", "args"} while
a request runs, then exactly one {"op": "result", "value"} or {"op": "error", "kind", "message"}.

v0.3 (M1 §1 宿主作为运行壳): {"op": "run", "entry", "task", "roles", "view", "limits", "skills"} runs the package's
agent, `run(ctx, task) -> str`, for the whole attempt. While it runs the worker asks the host for the model with
{"op": "model_call", "id", "role", "effort", "messages", "tools"} (answered by {"op": "model_result", "id", "text",
"tool_calls", "stop"} or {"op": "model_result", "id", "is_error": true, "error"}), for a skill with {"op":
"skill_read", "id", "name"} (answered by {"op": "skill_result", "id", "text"}), and for tools with base_call as
before. Every answer may carry "limits" (the attempt's remaining hard limits) and "view" (a fresh projection), and
the ctx shows the latest. Ids are monotonic per kind (b/m/k) for the worker's life; an answer with another op or id
is a protocol error, reported as {"op": "error", "kind": "protocol"} and not catchable by package code. {"op":
"smoke", "entry", "roles", "task", "deadline"} runs the same agent against a worker-local ctx whose model answers one
canned end turn (M0 §3): the draft check's way of finding a deterministic crash or an undeclared role before any paid
call. The smoke has small hard limits of its own (SMOKE_LIMITS), so an agent that never returns is found too.
"""
import asyncio
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import time
import traceback

ROOT = None
MODULES = {}
# The protocol gets its own descriptor, and fd 1 is pointed at stderr: neither print() nor os.write(1, ...) in
# package code can corrupt it.
_STDOUT = os.fdopen(os.dup(1), "w", encoding="utf-8")
os.dup2(2, 1)
sys.stdout = sys.stderr


def send(obj) -> None:
    _STDOUT.write(json.dumps(obj) + "\n")
    _STDOUT.flush()


_closed = [False]


def receive() -> dict:
    line = sys.stdin.readline()
    if not line:
        _closed[0] = True
        raise SystemExit(0)
    return json.loads(line)


class ProtocolError(BaseException):
    """The host answered outside the protocol: another op, or another id than the one in flight. Not an Exception,
    so package code cannot catch it and carry on as if the host had answered; main() reports it as kind "protocol",
    which the host keeps apart from the package's own errors."""


class SmokeLimit(BaseException):
    """The agent asked for more than the smoke's limits allow (M0 §2: a hard limit is the host's to enforce and
    not the package's to catch, so `except Exception` in the agent does not swallow it). Reported as the package's
    error, kind "package": an agent that does not finish within the smoke is the draft's defect."""

    def __init__(self, dimension: str):
        super().__init__(f"the agent did not finish within the smoke's limits ({dimension})")
        self.dimension = dimension


_counters: dict[str, int] = {}


def exchange(kind: str, request: dict, reply_op: str) -> dict:
    """One round trip to the host: `request` goes out with a fresh id of `kind` (b/m/k, monotonic for the worker's
    life, so a stale answer can never match a live request), and the one answer of `reply_op` with that id comes
    back. The pipe carries one exchange at a time, so anything else is a protocol error."""
    _counters[kind] = _counters.get(kind, 0) + 1
    call_id = f"{kind}{_counters[kind]}"
    send({**request, "id": call_id})
    reply = receive()
    if reply.get("op") != reply_op or reply.get("id") != call_id:
        raise ProtocolError(f"the host answered {reply.get('op')!r} {reply.get('id')!r} out of order "
                            f"to {request['op']} {call_id}")
    return reply


class Record:
    """Protocol data handed to package code: attributes, and the same names as keys, so code written against the
    dict shapes of M0 §2 ({text, tool_calls} / {output, is_error}) reads it too."""

    def __getitem__(self, key):
        if key not in vars(self):
            raise KeyError(key)
        return vars(self)[key]


class Result(Record):
    def __init__(self, output: str, is_error: bool = False):
        self.output, self.is_error = output, is_error


def base_call(name: str, args: dict | None) -> Result:
    reply = exchange("b", {"op": "base_call", "name": name, "args": args or {}}, "base_result")
    return Result(str(reply.get("output", "")), bool(reply.get("is_error")))


class Context:
    """What v1 package code (tools, hooks, tests) holds: a read-only view of the episode and the broker. Nothing
    else."""

    def __init__(self, view):
        self.view = view

    def call_tool(self, name: str, args: dict | None = None) -> Result:
        return base_call(name, args)


# ---------------------------------------------------------------------------- the agent's ctx (M0 §2 包 API v2)
class ModelCallError(RuntimeError):
    """The gateway refused or failed a ctx.model call (an undeclared role, a provider failure). The agent may catch
    it and decide what to do; a hard limit is not reported this way — the host ends the run itself."""


class ModelReply(Record):
    """What ctx.model returns: the model's text, its tool calls as [{id, name, args}] and the stop reason."""

    def __init__(self, text: str, tool_calls: list, stop):
        self.text, self.tool_calls, self.stop = text, tool_calls, stop


class Skills:
    """ctx.skills: the manifest's index ([{name, description}]), the memory text (None when the package has none)
    and read(name) -> the skill's text, None when there is no such skill."""

    def __init__(self, index, memory, read):
        self.index, self.memory, self._read = list(index or ()), memory, read

    def read(self, name: str):
        return self._read(name)


class AgentContext:
    """The five names an agent program gets (M0 §2), each backed by the host over the pipe: model (the gateway),
    tool (the broker), skills, limits (the latest snapshot the host attached to an answer) and view (the projection
    given at run, refreshed in place whenever an answer carries one, so a reference the agent keeps stays current).
    Nothing else: no provider, no scorer, no files of the host."""

    def __init__(self, view: dict, limits: dict, skills: dict):
        self.view = dict(view or {})
        self.limits = dict(limits or {})
        self.skills = Skills((skills or {}).get("index"), (skills or {}).get("memory"), self._read_skill)

    def _ask(self, kind: str, request: dict, reply_op: str) -> dict:
        reply = exchange(kind, request, reply_op)
        if isinstance(reply.get("limits"), dict):
            self.limits = dict(reply["limits"])
        if isinstance(reply.get("view"), dict):
            self.view.clear()
            self.view.update(reply["view"])
        return reply

    def model(self, messages, tools=(), role: str = "act", effort=None) -> ModelReply:
        reply = self._ask("m", {"op": "model_call", "role": role, "effort": effort, "messages": list(messages),
                                "tools": list(tools or ())}, "model_result")
        if reply.get("is_error"):
            raise ModelCallError(str(reply.get("error") or "the gateway refused the call"))
        calls = [{"id": c.get("id"), "name": c.get("name"), "args": c.get("args") or {}}
                 for c in reply.get("tool_calls") or () if isinstance(c, dict)]
        return ModelReply(str(reply.get("text", "")), calls, reply.get("stop"))

    def tool(self, name: str, args: dict | None = None) -> Result:
        reply = self._ask("b", {"op": "base_call", "name": name, "args": args or {}}, "base_result")
        return Result(str(reply.get("output", "")), bool(reply.get("is_error")))

    def _read_skill(self, name: str):
        return self._ask("k", {"op": "skill_read", "name": name}, "skill_result").get("text")


SMOKE_TURN = {"text": "ok", "tool_calls": [], "stop": "end"}
SMOKE_TURN_TOKENS = 1                   # what the one-word canned answer costs the smoke's token count
# The smoke's hard limits (M0 §2 names the five dimensions; the values are the smoke's own, small because the canned
# model ends every turn, so an agent that needs more than this is looping). Seconds is the request's deadline.
SMOKE_LIMITS = {"act_calls": 5, "model_calls": 10, "tool_calls": 10, "tokens": 20000}
SMOKE_SECONDS = 30


class SmokeContext(AgentContext):
    """The draft check's ctx (M0 §3): model answers one canned end turn without leaving the worker, and refuses a
    role the manifest does not declare — the structural error the smoke exists to find before a paid run; tool
    goes to the host as in the test op (the fixture broker); skills are empty. Limits are the remaining counts of
    SMOKE_LIMITS, integers as M0 §2 defines ctx.limits, so an agent that reads them to pace itself runs here as it
    would under the host; each model or tool call is checked against them first and takes one off, and a call past
    a count is the end of the run (SmokeLimit), not something the agent can catch."""

    def __init__(self, roles, seconds: float | None = None):
        self._end = time.monotonic() + (seconds if seconds and seconds > 0 else SMOKE_SECONDS)
        super().__init__({"smoke": True}, {**SMOKE_LIMITS, "seconds": self._seconds_left()}, {})
        self.roles = tuple(roles or ())

    def _seconds_left(self) -> int:
        return max(0, int(self._end - time.monotonic()))

    def _charge(self, **amounts: int) -> None:
        """Check every dimension a call touches before it, then take the amounts off: a count already at 0 (or
        the deadline passed) refuses the call whole, as the host's gateway and broker do (M5 §2), and a refused
        call costs nothing."""
        self.limits["seconds"] = self._seconds_left()
        if self.limits["seconds"] <= 0:
            raise SmokeLimit("seconds")
        for dimension in amounts:
            if self.limits[dimension] <= 0:
                raise SmokeLimit(dimension)
        for dimension, amount in amounts.items():
            self.limits[dimension] = max(0, self.limits[dimension] - amount)

    def model(self, messages, tools=(), role: str = "act", effort=None) -> ModelReply:
        if role not in self.roles:
            raise ModelCallError(f"role {role!r} is not declared in the manifest's roles {list(self.roles)}")
        self._charge(**({"act_calls": 1} if role == "act" else {}), model_calls=1, tokens=SMOKE_TURN_TOKENS)
        return ModelReply(SMOKE_TURN["text"], list(SMOKE_TURN["tool_calls"]), SMOKE_TURN["stop"])

    def tool(self, name: str, args: dict | None = None) -> Result:
        self._charge(tool_calls=1)
        return super().tool(name, args)

    def _read_skill(self, name: str):
        return None


def load(files: dict) -> None:
    global ROOT
    ROOT = tempfile.mkdtemp(prefix="pkg-")
    for path, text in files.items():
        full = os.path.join(ROOT, path)
        if not os.path.realpath(full).startswith(os.path.realpath(ROOT) + os.sep):
            raise ValueError(f"{path}: outside the package")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
    MODULES.clear()


def entry(spec: str):
    module_path, _, func = spec.partition(":")
    if module_path not in MODULES:
        full = os.path.join(ROOT, module_path)
        name = "pkg_" + module_path.replace("/", "_").replace(".py", "")
        loader = importlib.util.spec_from_file_location(name, full)
        if loader is None or loader.loader is None:
            raise ImportError(f"cannot load {module_path}")
        module = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(module)
        MODULES[module_path] = module
    fn = getattr(MODULES[module_path], func, None)
    if not callable(fn):
        raise AttributeError(f"{spec}: no callable {func}")
    return fn


async def _await(value):
    return await value


def call(fn, *args):
    """Package entrypoints may be plain or async functions."""
    value = fn(*args)
    if inspect.isawaitable(value):
        value = asyncio.run(value if asyncio.iscoroutine(value) else _await(value))
    return value


def agent_entry(spec: str):
    """The agent's entrypoint: M0 §2 allows only a callable named run, checked before the module is imported."""
    if spec.partition(":")[2] != "run":
        raise ValueError(f"{spec}: the agent entrypoint must be a function named run")
    return entry(spec)


def run_agent(fn, ctx, task) -> str:
    """run(ctx, task) to completion. Returning is finishing (M1 §1 完成协议), and what is returned is the final
    message, so anything but a string is the package's error — the host must never guess a message from None."""
    value = call(fn, ctx, task)
    if not isinstance(value, str):
        raise TypeError(f"the agent returned {type(value).__name__}, not a string")
    return value


def plain(value, limit: int = 20000):
    """A tool or hook answer as protocol data; a tool's output is clipped to the host's limit here, so an
    oversized answer never reaches the pipe."""
    if isinstance(value, Result):
        return {"output": value.output[:limit], "is_error": value.is_error}
    if isinstance(value, str):
        return {"output": value[:limit], "is_error": False}
    if isinstance(value, dict) and isinstance(value.get("output"), str):
        return {**value, "output": value["output"][:limit]}
    return value


def run_tests() -> dict:
    """Run every test_* function of tests/*.py with a context bound to the host's fixture broker."""
    outcome = {"passed": [], "failed": []}
    tests_dir = os.path.join(ROOT, "tests")
    if not os.path.isdir(tests_dir):
        return outcome
    for name in sorted(os.listdir(tests_dir)):
        if not name.endswith(".py"):
            continue
        spec = importlib.util.spec_from_file_location("pkgtest_" + name[:-3], os.path.join(tests_dir, name))
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except BaseException as e:  # noqa: BLE001 - reported as a failed test file
            if isinstance(e, SystemExit) and _closed[0]:
                raise
            outcome["failed"].append({"test": name, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            continue
        for attr in sorted(dir(module)):
            if not attr.startswith("test_") or not callable(getattr(module, attr)):
                continue
            try:
                call(getattr(module, attr), Context({"fixture": True}))
                outcome["passed"].append(f"{name}:{attr}")
            except BaseException as e:  # noqa: BLE001 - a failing test (sys.exit too), not a runner failure
                if isinstance(e, SystemExit) and _closed[0]:
                    raise
                outcome["failed"].append({"test": f"{name}:{attr}", "error": f"{type(e).__name__}: {str(e)[:200]}"})
    return outcome


def main() -> None:
    while True:
        req = receive()
        op = req.get("op")
        try:
            if op == "load":
                load(req["files"])
                send({"op": "result", "value": "loaded"})
            elif op == "probe":
                for spec in req.get("entries") or ():
                    entry(spec)
                send({"op": "result", "value": "ok"})
            elif op == "call_tool":
                send({"op": "result", "value": plain(call(entry(req["entry"]), Context(req.get("view") or {}),
                                                          req.get("args") or {}), int(req.get("limit") or 20000))})
            elif op == "hook":
                send({"op": "result", "value": plain(call(entry(req["entry"]), Context(req.get("view") or {}),
                                                          req.get("event") or {}), int(req.get("limit") or 20000))})
            elif op == "test":
                send({"op": "result", "value": run_tests()})
            elif op == "run":
                ctx = AgentContext(req.get("view") or {}, req.get("limits") or {}, req.get("skills") or {})
                send({"op": "result", "value": run_agent(agent_entry(req["entry"]), ctx, req.get("task", ""))})
            elif op == "smoke":
                ctx = SmokeContext(req.get("roles"), req.get("deadline"))
                send({"op": "result", "value": run_agent(agent_entry(req["entry"]), ctx, req.get("task", ""))})
            else:
                send({"op": "error", "kind": "protocol", "message": f"unknown op {op!r}"})
        except ProtocolError as e:         # the host's answer, not the package's code: never kind "package"
            send({"op": "error", "kind": "protocol", "message": str(e)[:300]})
        except SmokeLimit as e:            # the agent did not finish: the draft's defect, named by its dimension
            send({"op": "error", "kind": "package", "message": str(e)})
        except BaseException as e:  # noqa: BLE001 - package code failed, sys.exit included: the package's error
            if isinstance(e, SystemExit) and _closed[0]:
                raise                      # the host closed the pipe: nothing is waiting for an answer
            send({"op": "error", "kind": "package", "message": f"{type(e).__name__}: {str(e)[:300]}",
                  "trace": traceback.format_exc()[-1500:]})


if __name__ == "__main__":
    main()
