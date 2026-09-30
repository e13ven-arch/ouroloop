"""The runner inside the isolated worker (docs/modules/01_POLICY_RUNTIME.md §1). Standard library only.

It is the only program the worker image runs. The host sends the package files over stdin; the runner writes them
to a scratch directory, imports the entrypoints the host names, and answers one request at a time. Package code
reaches the business world only through `ctx.call_tool`, which asks the host's broker over the same pipe: the
worker has no network, no host files and no credentials, so the broker is the only way out.

Protocol: one JSON object per line. Host -> worker: {"op": "load", "files": {...}}, {"op": "call_tool", "entry",
"args", "view"}, {"op": "hook", "entry", "event", "view"}, {"op": "probe", "entries"}, {"op": "test"},
{"op": "base_result", "id", "output", "is_error"}. Worker -> host: {"op": "base_call", "id", "name", "args"} while
a request runs, then exactly one {"op": "result", "value"} or {"op": "error", "kind", "message"}.
"""
import asyncio
import importlib.util
import inspect
import json
import os
import sys
import tempfile
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


class Result:
    def __init__(self, output: str, is_error: bool = False):
        self.output, self.is_error = output, is_error


class Context:
    """What package code holds: a read-only view of the episode and the broker. Nothing else."""

    def __init__(self, view):
        self.view = view
        self._n = 0

    def call_tool(self, name: str, args: dict | None = None) -> Result:
        self._n += 1
        call_id = f"b{self._n}"
        send({"op": "base_call", "id": call_id, "name": name, "args": args or {}})
        reply = receive()
        if reply.get("op") != "base_result" or reply.get("id") != call_id:
            raise RuntimeError("the broker answered out of order")
        return Result(str(reply.get("output", "")), bool(reply.get("is_error")))


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
            else:
                send({"op": "error", "kind": "protocol", "message": f"unknown op {op!r}"})
        except BaseException as e:  # noqa: BLE001 - package code failed, sys.exit included: the package's error
            if isinstance(e, SystemExit) and _closed[0]:
                raise                      # the host closed the pipe: nothing is waiting for an answer
            send({"op": "error", "kind": "package", "message": f"{type(e).__name__}: {str(e)[:300]}",
                  "trace": traceback.format_exc()[-1500:]})


if __name__ == "__main__":
    main()
