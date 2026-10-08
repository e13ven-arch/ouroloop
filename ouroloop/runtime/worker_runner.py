"""The runner inside the isolated worker (docs/design/RUNTIME.md §4). Standard library only.

It is the only program the worker image runs. The host sends the package's run files over stdin; the runner writes them
to a scratch directory, imports the entrypoints the host names, and answers one request at a time. Package code reaches
the world only through ctx.tool, which asks the host's broker over the same pipe: the worker has no host files, no
credentials and no network unless K gives it the egress proxy, so the broker is the way out.

Protocol: one JSON object per line. Host -> worker ops (CONTRACT["ops"]):

  load       files                          -> {runner_sha256, ops}
  run        entry, task, roles, view, limits, skills
                                            -> the package's agent `run(ctx, task) -> str` for the whole attempt
  smoke      entry, roles, task, deadline, view
                                            -> the same agent against a worker-local model (one tool_use turn calling
                                               each offered tool once, then end turns) under small limits
  hook       entry, event, view             -> one hook's raw answer (the host checks it against the site's shape)
  call_tool  entry, args, view, read_only   -> one package tool's answer
  probe      entries                        -> imports each entrypoint
  test       -                              -> {passed, failed} of tests/*.py
  check      script, tools, view            -> runs a toolcheck script check(ctx, tools) (the V2a check)
  work       action, ...                    -> R's coding session's file tree (init/list/read/write/edit/delete/export)

While a request runs, the worker calls back (CONTRACT["callbacks"]): model_call {role, effort, messages, tools} ->
model_result {text, tool_calls, stop} or {is_error, error}; base_call {name, args, read_only?} -> base_result {output,
is_error}; skill_read {name} -> skill_result {text}; memory_recall {query, k} -> memory_result {entries}; mark {kind,
data} -> mark_result. Every answer may carry "limits" (the attempt's remaining hard limits) and "view" (the keys of the
projection that changed, merged into ctx.view in place). Ids are monotonic per kind for the worker's life; an answer
with another op or id is a protocol error, reported as kind "protocol" and not catchable by package code. Then exactly
one {"op": "result", "value"} or {"op": "error", "kind", "message"}: kind "defect" when the exception says it is the
package's defect (`ouroloop_defect`: a hook outside its protocol, a package tool that raised), "package" for any other
exception of package code, "protocol" for the host's.

The answer to `load` names the runner (the sha256 of this file); the host refuses a worker image built from another.
`CONTRACT` is harness/contract.py's as_json(), embedded because this file imports nothing of the host; a host test
checks the two are equal.
"""
import asyncio
import hashlib
import importlib.util
import inspect
import json
import os
import re
import sys
import tempfile
import time
import traceback

ROOT = None
MODULES = {}
CONTRACT = json.loads("""
{
 "actions": [
  "none",
  "block",
  "modify",
  "feedback",
  "pushback"
 ],
 "api_version": "harness_package.v3",
 "callback_ids": {
  "base_call": "b",
  "mark": "n",
  "memory_recall": "r",
  "model_call": "m",
  "skill_read": "k"
 },
 "callbacks": {
  "base_call": "base_result",
  "mark": "mark_result",
  "memory_recall": "memory_result",
  "model_call": "model_result",
  "skill_read": "skill_result"
 },
 "ctx": [
  "model",
  "tool",
  "skills",
  "memory",
  "limits",
  "view",
  "mark"
 ],
 "efforts": [
  "low",
  "medium",
  "high",
  "xhigh",
  "max"
 ],
 "events": {
  "after_tool": [
   "site",
   "turn",
   "call_id",
   "name",
   "args",
   "output",
   "is_error"
  ],
  "after_turn": [
   "site",
   "turn",
   "tool_results"
  ],
  "before_model": [
   "site",
   "turn",
   "role",
   "messages",
   "tools"
  ],
  "before_run": [
   "site",
   "task",
   "system",
   "messages"
  ],
  "before_tool": [
   "site",
   "turn",
   "call_id",
   "name",
   "args"
  ],
  "on_finish": [
   "site",
   "turn",
   "reason",
   "final_text",
   "pushbacks"
  ]
 },
 "limits": [
  "act_calls",
  "model_calls",
  "tool_calls",
  "seconds",
  "tokens"
 ],
 "ops": [
  "load",
  "run",
  "smoke",
  "hook",
  "call_tool",
  "probe",
  "test",
  "check",
  "work"
 ],
 "returns": {
  "after_tool": [
   "output",
   "feedback"
  ],
  "after_turn": [
   "message"
  ],
  "before_model": [
   "messages"
  ],
  "before_run": [
   "system",
   "messages"
  ],
  "before_tool": [
   "block",
   "args"
  ],
  "on_finish": [
   "pushback"
  ]
 },
 "roles": [
  "act",
  "aux"
 ],
 "sites": [
  "before_run",
  "before_model",
  "before_tool",
  "after_tool",
  "after_turn",
  "on_finish"
 ],
 "view": [
  "system",
  "base_tools",
  "package",
  "last_usage",
  "events",
  "max_output_chars"
 ],
 "work_actions": [
  "init",
  "list",
  "read",
  "write",
  "edit",
  "delete",
  "export"
 ]
}
""")
OPS = tuple(CONTRACT["ops"])
WORK = {"root": None, "max_bytes": 262144, "max_file_bytes": 262144}   # the coding session's /work tree
WORK_ACTIONS = tuple(CONTRACT["work_actions"])
WORK_DIR_NAME = "[A-Za-z0-9_-]{1,32}"  # a candidate directory's name under candidates/
# The protocol line a work answer may take, measured as it goes out (json.dumps escapes to ASCII: a control character
# is \u00XX, a non-ASCII one \uXXXX, one outside the basic plane a pair — up to 12 characters for one), safely under
# the host's MAX_LINE (agent/worker.py, 1_000_000) so the envelope always fits.
LINE_BUDGET = 900_000
LINE_OVERHEAD = 4                      # per file of an export: the ": " and ", " around its path and text
# The reply around an export's files, reserved by every write: '{"op": "result", "value": {"ok": true, "value": {}}}'
EXPORT_WRAPPER = len(json.dumps({"op": "result", "value": {"ok": True, "value": {}}}))
READ_CHARS = 131072                    # characters one read answers at most; the rest is read with offset
MAX_PATH_PARTS, MAX_PATH_CHARS = 32, 1024


def runner_sha256() -> str:
    """The digest of this file as it runs: what the host checks its own worker_runner.py against."""
    with open(__file__, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()
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
    """The agent asked for more than the smoke's limits allow (a hard limit is the host's to enforce and
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
    dict shapes of the contract ({text, tool_calls} / {output, is_error}) reads it too."""

    def __getitem__(self, key):
        if key not in vars(self):
            raise KeyError(key)
        return vars(self)[key]


class Result(Record):
    def __init__(self, output: str, is_error: bool = False):
        self.output, self.is_error = output, is_error


def base_call(name: str, args: dict | None, read_only: bool = False) -> Result:
    request = {"op": "base_call", "name": name, "args": args or {}}
    if read_only:
        request["read_only"] = True
    reply = exchange(CONTRACT["callback_ids"]["base_call"], request, "base_result")
    return Result(str(reply.get("output", "")), bool(reply.get("is_error")))


class ModelCallError(RuntimeError):
    """The gateway refused or failed a ctx.model call (an undeclared role, a provider failure). The agent may catch
    it; a hard limit is not reported this way — the host ends the run itself."""


class ModelReply(Record):
    """What ctx.model returns: the model's text, its tool calls as [{id, name, args}] and the stop reason."""

    def __init__(self, text: str, tool_calls: list, stop):
        self.text, self.tool_calls, self.stop = text, tool_calls, stop


class Skills:
    """ctx.skills: the package's enabled index ([{name, description}]) and read(name) -> its text or None."""

    def __init__(self, index, read):
        self.index, self._read = list(index or ()), read

    def read(self, name: str):
        return self._read(name)


class Memory:
    """ctx.memory: recall(query, k=None) -> [{id, when, content, score}] from the host's memory reader."""

    def __init__(self, recall):
        self._recall = recall

    def recall(self, query: str, k=None) -> list:
        return self._recall(query, k)


def _no_skill(name):
    return None


def _no_memory(query, k=None):
    return []


class Context:
    """What a hook, a package tool, a test or a toolcheck script holds: the view, the broker as `tool` (read-only for
    a hook, and for a tool that does not write), empty skills and memory, and `mark`, which records nothing here.
    No `model`: hooks and tools are deterministic code."""

    def __init__(self, view, *, read_only: bool = False):
        self.view = dict(view or {})
        self.limits = {}
        self.skills = Skills((), _no_skill)
        self.memory = Memory(_no_memory)
        self._read_only = read_only

    def tool(self, name: str, args: dict | None = None, *, read_only: bool = False) -> Result:
        return base_call(name, args, self._read_only or read_only)

    def mark(self, kind: str, **data) -> None:
        return None


class AgentContext:
    """The names an agent program gets (harness/contract.py), each backed by the host over the pipe: model (the
    gateway), tool (the broker), skills, memory, limits (the latest snapshot the host attached to an answer), view
    (refreshed in place whenever an answer carries one) and mark. Nothing else: no provider, no scorer, no files of the
    host."""

    def __init__(self, view: dict, limits: dict, skills: dict):
        self.view = dict(view or {})
        self.limits = dict(limits or {})
        self.skills = Skills((skills or {}).get("index"), self._read_skill)
        self.memory = Memory(self._recall)

    def _ask(self, op: str, request: dict, reply_op: str) -> dict:
        reply = exchange(CONTRACT["callback_ids"][op], {"op": op, **request}, reply_op)
        if isinstance(reply.get("limits"), dict):
            self.limits = dict(reply["limits"])
        if isinstance(reply.get("view"), dict):
            # The keys that changed (the broker's `events`, the call's `last_usage`), merged in place: a reference
            # the agent keeps stays current.
            self.view.update(reply["view"])
        return reply

    def model(self, messages, tools=(), role: str = "act", effort=None) -> ModelReply:
        reply = self._ask("model_call", {"role": role, "effort": effort, "messages": list(messages),
                                         "tools": list(tools or ())}, "model_result")
        if reply.get("is_error"):
            raise ModelCallError(str(reply.get("error") or "the gateway refused the call"))
        calls = [{"id": c.get("id"), "name": c.get("name"), "args": c.get("args") or {}}
                 for c in reply.get("tool_calls") or () if isinstance(c, dict)]
        return ModelReply(str(reply.get("text", "")), calls, reply.get("stop"))

    def tool(self, name: str, args: dict | None = None, *, read_only: bool = False) -> Result:
        """A base tool through the host's broker; `read_only` asks the broker to refuse a call that may change the
        world (the flag travels only when set)."""
        request = {"name": name, "args": args or {}}
        if read_only:
            request["read_only"] = True
        reply = self._ask("base_call", request, "base_result")
        return Result(str(reply.get("output", "")), bool(reply.get("is_error")))

    def mark(self, kind: str, **data) -> None:
        """Report an event of the agent's own loop; the host records it labelled `source: "package"`."""
        self._ask("mark", {"kind": str(kind), "data": dict(data)}, "mark_result")

    def _read_skill(self, name: str):
        return self._ask("skill_read", {"name": name}, "skill_result").get("text")

    def _recall(self, query: str, k=None) -> list:
        entries = self._ask("memory_recall", {"query": str(query), "k": k}, "memory_result").get("entries")
        return [dict(e) for e in entries or () if isinstance(e, dict)]


SMOKE_TURN = {"text": "ok", "tool_calls": [], "stop": "end"}
SMOKE_TURN_TOKENS = 1                   # what the one-word canned answer costs the smoke's token count
# The smoke's hard limits (the contract names the five dimensions; the values are the smoke's own, small because the
# canned model ends every turn after the one tool_use turn, so an agent that needs more than this is looping). Seconds
# is the request's deadline. tool_calls covers the one call per offered tool of the tool_use turn, hook reads included.
SMOKE_LIMITS = {"act_calls": 5, "model_calls": 10, "tool_calls": 40, "tokens": 20000}
SMOKE_SECONDS = 30
# Placeholder values for a tool schema's required properties, by JSON type, so the smoke's one call of each tool is
# a well-formed call: what it does with the placeholder is the fixture's business, an error result is a result.
PLACEHOLDERS = {"string": "smoke", "integer": 0, "number": 0, "boolean": False, "array": [], "object": {}, "null": None}


def minimal_args(schema) -> dict:
    """The smallest arguments a tool's schema accepts: a placeholder for each required property, {} otherwise."""
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    out = {}
    for name in schema.get("required") or ():
        spec = properties.get(name) if isinstance(properties.get(name), dict) else {}
        kind = spec.get("type")
        kind = kind[0] if isinstance(kind, list) and kind else kind
        if isinstance(spec.get("enum"), list) and spec["enum"]:
            out[name] = spec["enum"][0]
        else:
            out[name] = PLACEHOLDERS.get(kind, "smoke")
    return out


class SmokeContext(AgentContext):
    """The draft check's ctx (V1, docs/design/IMPROVER.md §8): the model answers without leaving the worker — the
    first call that offers tools gets one tool_use turn calling each offered tool once with the smallest arguments its
    schema allows, every other call the canned end turn — so the agent's own tool path (its hooks at every site and
    the package's tools) runs once against the fixture, and a hook or tool that raises or answers outside its protocol
    is found here as the draft's failure, not later at run time. It refuses a role the manifest does not declare; tool
    goes to the host's fixture broker; skills and memory are empty; mark records nothing. Limits are the remaining
    counts of SMOKE_LIMITS, and a call past a count ends the run (SmokeLimit), which the agent cannot catch."""

    def __init__(self, roles, seconds: float | None = None, view: dict | None = None):
        self._end = time.monotonic() + (seconds if seconds and seconds > 0 else SMOKE_SECONDS)
        super().__init__({**(view or {}), "smoke": True}, {**SMOKE_LIMITS, "seconds": self._seconds_left()}, {})
        self.memory = Memory(_no_memory)
        self.roles = tuple(roles or ())
        self._toured = False                 # the one tool_use turn has been answered

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
        offered = [t for t in (tools or ()) if isinstance(t, dict) and isinstance(t.get("name"), str)]
        if offered and not self._toured:
            self._toured = True
            calls = [{"id": f"smoke{i + 1}", "name": t["name"],
                      "args": minimal_args(t.get("schema", t.get("input_schema")))} for i, t in enumerate(offered)]
            return ModelReply("", calls, "tool_use")
        return ModelReply(SMOKE_TURN["text"], list(SMOKE_TURN["tool_calls"]), SMOKE_TURN["stop"])

    def tool(self, name: str, args: dict | None = None, *, read_only: bool = False) -> Result:
        self._charge(tool_calls=1)
        return super().tool(name, args, read_only=read_only)

    def mark(self, kind: str, **data) -> None:
        return None                    # the smoke writes no record: nothing it sees is evidence

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
    """The agent's entrypoint: the contract allows only a callable named run, checked before the module is imported."""
    if spec.partition(":")[2] != "run":
        raise ValueError(f"{spec}: the agent entrypoint must be a function named run")
    return entry(spec)


def run_agent(fn, ctx, task) -> str:
    """run(ctx, task) to completion. Returning is finishing, and what is returned is the final
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


# ---------------------------------------------------------------------------- the coding session's /work tree
class WorkError(Exception):
    """A file operation of R's session that the tree refuses: a path outside it, a write outside candidates/, a
    read of a file that is not there. Answered to the host as the tool's error (the session hands it to R as the
    tool result and counts the turn), never as a worker failure."""


def _work_root() -> str:
    if WORK["root"] is None:
        raise WorkError("the work tree is not initialised")
    return WORK["root"]


def _work_path(path, *, write: bool = False) -> str:
    """The absolute path of a tree path. Relative POSIX paths only, inside the tree, strict UTF-8, at most
    MAX_PATH_PARTS segments and MAX_PATH_CHARS characters; a write stays under candidates/<dir>/ and names a file:
    not the directory itself, not a path whose parent segment is a file.

    Strict UTF-8 because a lone surrogate (the \\udc80-\\udcff of surrogateescape) would make a raw-byte filename
    whose list/export key then breaks the host's ledger rows and canonical JSON; the bounds because a path of
    thousands of segments recurses in os.makedirs (2026-10-05)."""
    root = _work_root()
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise WorkError(f"{path!r}: a relative POSIX path inside the work tree"[:300])
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        raise WorkError("the path is not strict UTF-8 (it holds a lone surrogate)") from None
    if not write and path.endswith("/") and path.strip("/"):
        # a directory named with a trailing slash ("package/") on a read or a listing is the same directory; the
        # 2026-10-05 pilot's R lost a turn to this refusal. A write names a file, so it keeps the strict form.
        path = path.rstrip("/")
    parts = path.split("/")
    if len(parts) > MAX_PATH_PARTS or len(path) > MAX_PATH_CHARS:
        raise WorkError(f"a path of {len(parts)} segments and {len(path)} characters: at most {MAX_PATH_PARTS} "
                        f"segments and {MAX_PATH_CHARS} characters")
    if any(p in ("", ".", "..") for p in parts):
        raise WorkError(f"{path!r}: no '.', '..' or empty segments")
    full = os.path.realpath(os.path.join(root, path))
    if not (full == os.path.realpath(root) or full.startswith(os.path.realpath(root) + os.sep)):
        raise WorkError(f"{path!r}: outside the work tree")
    if write:
        if len(parts) < 3 or parts[0] != "candidates" or not re.fullmatch(WORK_DIR_NAME, parts[1]):
            raise WorkError(f"{path!r}: a session writes under candidates/<dir>/<file> only, where <dir> matches "
                            f"{WORK_DIR_NAME}; package/ and inputs/ are read-only")
        if os.path.isdir(full):
            raise WorkError(f"{path!r} is a directory, not a file")
        parent = os.path.dirname(full)
        while len(parent) > len(os.path.join(root, "candidates", parts[1])):
            if os.path.isfile(parent):
                raise WorkError(f"{path!r}: {os.path.relpath(parent, root)} is a file, not a directory")
            parent = os.path.dirname(parent)
    return full


def _line_size(rel: str, text: str) -> int:
    """What one file of an export takes on the protocol line: its path and text as json.dumps escapes them."""
    return len(json.dumps(rel)) + len(json.dumps(text)) + LINE_OVERHEAD


def _within_caps(full: str, text: str, dir_name: str) -> None:
    """A write or edit that would put the file over the per-file cap, the candidate directory over K's package byte
    cap, or the directory's export over the protocol line (LINE_BUDGET less the reply's fixed wrapper, measured
    escaped: disk bytes are not line characters — a control character is six of them, an emoji twelve) is refused
    before anything touches the disk, so an export of a legal candidate always fits one line."""
    base = os.path.realpath(os.path.join(_work_root(), "candidates", dir_name))
    rel = os.path.relpath(full, base).replace(os.sep, "/")
    new_size = len(text.encode("utf-8"))
    if new_size > WORK["max_file_bytes"]:
        raise WorkError(f"{new_size} bytes: a file of a candidate may hold at most {WORK['max_file_bytes']} bytes")
    total, line = new_size, _line_size(rel, text)
    for dirpath, _dirs, files in os.walk(base):
        for f in files:
            path = os.path.join(dirpath, f)
            if os.path.realpath(path) != os.path.realpath(full):
                other = os.path.relpath(path, base).replace(os.sep, "/")
                total += os.path.getsize(path)
                line += _line_size(other, _read_text(path))
    if total > WORK["max_bytes"]:
        raise WorkError(f"{total} bytes: candidates/{dir_name} may hold at most {WORK['max_bytes']} bytes in all "
                        f"(K's max_package_bytes)")
    if line > LINE_BUDGET - EXPORT_WRAPPER:
        raise WorkError(f"candidates/{dir_name} would take {line} characters on the protocol line once escaped "
                        f"(at most {LINE_BUDGET - EXPORT_WRAPPER}): control and non-ASCII characters count six to "
                        f"twelve each")


def _seed_candidate(name: str) -> bool:
    """candidates/<name>/ as a copy of package/ when it does not exist yet. -> whether it was seeded now."""
    root = _work_root()
    target = os.path.join(root, "candidates", name)
    if os.path.isdir(target):
        return False
    for dirpath, _dirs, files in os.walk(os.path.join(root, "package")):
        rel = os.path.relpath(dirpath, os.path.join(root, "package"))
        os.makedirs(os.path.join(target, rel) if rel != "." else target, exist_ok=True)
        for f in files:
            src = os.path.join(dirpath, f)
            dst = os.path.join(target, rel, f) if rel != "." else os.path.join(target, f)
            with open(src, "rb") as a, open(dst, "wb") as b:
                b.write(a.read())
    return True


def _write_tree(base: str, files: dict) -> None:
    for path, text in (files or {}).items():
        if not isinstance(path, str) or not isinstance(text, str):
            raise WorkError("files are a mapping of path to text")
        full = os.path.join(base, path)
        if not os.path.realpath(full).startswith(os.path.realpath(base) + os.sep):
            raise WorkError(f"{path}: outside its directory")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        _write_text(full, text)


def _read_text(full: str) -> str:
    # newline="" both ways: a CRLF file round-trips as it is, so a file R never touched is never a changed file
    with open(full, "r", encoding="utf-8", newline="") as h:
        return h.read()


def _write_text(full: str, text: str) -> None:
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _files_under(base: str) -> dict:
    out = {}
    for dirpath, _dirs, files in os.walk(base):
        for f in files:
            full = os.path.join(dirpath, f)
            out[os.path.relpath(full, base).replace(os.sep, "/")] = _read_text(full)
    return dict(sorted(out.items()))


def work(req: dict):
    """One action on the session's tree. -> the action's value, or raises WorkError (the tool's error)."""
    action = req.get("action")
    if action == "init":
        WORK["root"] = tempfile.mkdtemp(prefix="work-")
        WORK["max_bytes"] = int(req.get("max_bytes") or 262144)
        WORK["max_file_bytes"] = int(req.get("max_file_bytes") or WORK["max_bytes"])
        for name in ("package", "inputs", "candidates"):
            os.makedirs(os.path.join(WORK["root"], name), exist_ok=True)
        _write_tree(os.path.join(WORK["root"], "package"), req.get("package") or {})
        _write_tree(os.path.join(WORK["root"], "inputs"), req.get("inputs") or {})
        return {"ok": True}
    if action == "list":
        full = _work_path(req.get("path") or ".") if (req.get("path") or ".") != "." else _work_root()
        if not os.path.isdir(full):
            raise WorkError(f"{req.get('path')!r}: no such directory")
        return sorted(os.path.relpath(os.path.join(dp, f), full).replace(os.sep, "/")
                      for dp, _d, fs in os.walk(full) for f in fs)
    if action == "read":
        # One page of at most READ_CHARS characters from `offset`, halved until it fits the protocol line escaped;
        # what follows is named, so a file of any size (inputs/ledger.json grows with the activity) is read in pages.
        full = _work_path(req.get("path"))
        if not os.path.isfile(full):
            raise WorkError(f"{req.get('path')!r}: no such file")
        try:
            offset = max(int(req.get("offset") or 0), 0)
            limit = READ_CHARS if req.get("limit") is None else min(max(int(req["limit"]), 0), READ_CHARS)
        except (TypeError, ValueError, OverflowError):
            raise WorkError("offset and limit are integers (characters)") from None
        text = _read_text(full)
        page = text[offset:offset + limit]
        while page and len(json.dumps(page)) > LINE_BUDGET - 200:
            page = page[:len(page) // 2]
        rest = len(text) - offset - len(page)
        return page + (f"\n[... {rest} characters follow; read again with offset {offset + len(page)}]"
                       if rest > 0 else "")
    if action in ("write", "edit", "delete"):
        path = req.get("path")
        # the directory is seeded first (a candidate starts as the package), then the path is checked against what
        # is now there — a write onto a directory or under a file is refused before anything is written
        parts = path.split("/") if isinstance(path, str) else []
        seeded = False
        if len(parts) >= 3 and parts[0] == "candidates" and re.fullmatch(WORK_DIR_NAME, parts[1]):
            seeded = _seed_candidate(parts[1])
        full = _work_path(path, write=True)
        dir_name = parts[1]
        if action == "write":
            text = req.get("text")
            if not isinstance(text, str):
                raise WorkError("write_file takes the file's whole text")
            _within_caps(full, text, dir_name)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            _write_text(full, text)
            return {"written": len(text.encode("utf-8")), "seeded": seeded}
        if action == "edit":
            old, new = req.get("old"), req.get("new")
            if not isinstance(old, str) or not old or not isinstance(new, str):
                raise WorkError("edit_file takes the exact text to replace (non-empty) and its replacement")
            if not os.path.isfile(full):
                raise WorkError(f"{path!r}: no such file to edit; write_file creates one")
            text = _read_text(full)
            if text.count(old) != 1:
                raise WorkError(f"{path!r}: the text to replace occurs {text.count(old)} times, not once")
            edited = text.replace(old, new, 1)
            _within_caps(full, edited, dir_name)
            _write_text(full, edited)
            return {"edited": True, "seeded": seeded}
        if not os.path.isfile(full):
            raise WorkError(f"{path!r}: no such file to delete")
        os.remove(full)
        return {"deleted": True, "seeded": seeded}
    if action == "export":
        name = req.get("dir")
        if not isinstance(name, str) or not re.fullmatch(WORK_DIR_NAME, name):
            raise WorkError(f"{name!r}: not a candidate directory name")
        base = os.path.join(_work_root(), "candidates", name)
        if not os.path.isdir(base):
            raise WorkError(f"candidates/{name}: no such candidate directory")
        return _files_under(base)
    raise WorkError(f"unknown work action {action!r}: one of {list(WORK_ACTIONS)}")


def _json_value(value):
    """A hook's answer as protocol data, or the package's defect when it is not JSON."""
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as e:
        raise HookAnswerError(f"the hook answered something that is not JSON: {type(value).__name__}") from e


class HookAnswerError(Exception):
    ouroloop_defect = True


class CheckError(Exception):
    """A toolcheck script that does not define check(ctx, tools)."""
    ouroloop_defect = True


def run_check(script: str, tools: list, view: dict, limit: int) -> dict:
    """Run a toolcheck script (the V2a check, docs/design/IMPROVER.md §8): `check(ctx, tools)` where tools[name](args)
    runs the candidate's tool entrypoint with a context whose broker writes (the world is the task's fresh episode on
    the host). -> {"ok": True, "called": [tool names, in order]}; a failed assertion or an exception is the error."""
    namespace: dict = {"__name__": "toolcheck"}
    exec(compile(script, "toolcheck.py", "exec"), namespace)          # noqa: S102 - R's script, in the worker
    check = namespace.get("check")
    if not callable(check):
        raise CheckError("the toolcheck script defines no check(ctx, tools)")
    called: list = []
    ctx = Context(view)

    def runner(name: str, spec: str, writes: bool):
        def run(args=None):
            called.append(name)
            return plain(call(entry(spec), Context(view, read_only=not writes), dict(args or {})), limit)
        return run
    table = {t["name"]: runner(t["name"], t["entrypoint"], bool(t.get("writes"))) for t in tools
             if isinstance(t, dict) and isinstance(t.get("name"), str) and isinstance(t.get("entrypoint"), str)}
    call(check, ctx, table)
    return {"ok": True, "called": called}


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
                send({"op": "result", "value": {"runner_sha256": runner_sha256(), "ops": list(OPS)}})
            elif op == "probe":
                for spec in req.get("entries") or ():
                    entry(spec)
                send({"op": "result", "value": "ok"})
            elif op == "call_tool":
                ctx = Context(req.get("view") or {}, read_only=bool(req.get("read_only")))
                send({"op": "result", "value": plain(call(entry(req["entry"]), ctx, req.get("args") or {}),
                                                     int(req.get("limit") or 20000))})
            elif op == "hook":
                # the raw answer: the host checks it against the site's shape (harness/contract.py action_of)
                value = call(entry(req["entry"]), Context(req.get("view") or {}, read_only=True),
                             req.get("event") or {})
                send({"op": "result", "value": _json_value(value)})
            elif op == "test":
                send({"op": "result", "value": run_tests()})
            elif op == "check":
                send({"op": "result", "value": run_check(req.get("script") or "", req.get("tools") or [],
                                                         req.get("view") or {}, int(req.get("limit") or 20000))})
            elif op == "run":
                ctx = AgentContext(req.get("view") or {}, req.get("limits") or {}, req.get("skills") or {})
                send({"op": "result", "value": run_agent(agent_entry(req["entry"]), ctx, req.get("task", ""))})
            elif op == "smoke":
                ctx = SmokeContext(req.get("roles"), req.get("deadline"), req.get("view") or {})
                send({"op": "result", "value": run_agent(agent_entry(req["entry"]), ctx, req.get("task", ""))})
            elif op == "work":
                # The session's file tree: a refused action — and an OS or encoding failure under it — is the tool's
                # error, answered as a value the host hands to R; no package code runs here, so nothing in it is
                # kind "package" (that would be read as the candidate's defect and end the round).
                # Every Exception is the tool's (a RecursionError of a deep path included); SystemExit and
                # KeyboardInterrupt are not Exceptions and fall through. The answer is serialized once and measured
                # as it goes out: one over the line budget is never sent (2026-10-05).
                try:
                    answer = {"ok": True, "value": work(req)}
                except WorkError as e:
                    answer = {"ok": False, "error": str(e)[:300]}
                except Exception as e:  # noqa: BLE001 - see above: the tool's error, never kind "package"
                    answer = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:260]}"}
                line = json.dumps({"op": "result", "value": answer})
                if len(line) > LINE_BUDGET:
                    line = json.dumps({"op": "result", "value": {
                        "ok": False, "error": "the answer would exceed the protocol line; read it in pieces"}})
                _STDOUT.write(line + "\n")
                _STDOUT.flush()
            else:
                send({"op": "error", "kind": "protocol", "message": f"unknown op {op!r}"})
        except ProtocolError as e:         # the host's answer, not the package's code: never kind "package"
            send({"op": "error", "kind": "protocol", "message": str(e)[:300]})
        except SmokeLimit as e:            # the agent did not finish: the draft's defect, named by its dimension
            send({"op": "error", "kind": "package", "message": str(e)})
        except BaseException as e:  # noqa: BLE001 - package code failed, sys.exit included: the package's error
            if isinstance(e, SystemExit) and _closed[0]:
                raise                      # the host closed the pipe: nothing is waiting for an answer
            kind = "defect" if getattr(e, "ouroloop_defect", False) else "package"
            send({"op": "error", "kind": kind, "message": f"{type(e).__name__}: {str(e)[:300]}",
                  "trace": traceback.format_exc()[-1500:]})


if __name__ == "__main__":
    main()
