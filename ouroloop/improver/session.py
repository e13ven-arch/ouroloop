"""R's coding session (docs/design/IMPROVER.md §7; DESIGN §6.5): one bounded session per round.

The model loop runs here, on the host, through the metered research provider (each turn reserved and settled); the
files live in the isolated worker (`/work`: `package/` read-only, `inputs/` read-only, `candidates/<dir>/` writable,
a dir created by its first write as a copy of the package). What R can do is the tool list and nothing else: read and
write the tree, read train trajectories (byte-capped) and search them, run a candidate's package tests or its whole
free ladder (V0-V2) before it finishes, and finish with one declaration per candidate. There is no shell, no network,
no host file, no dev or test content, and no tool that raises a cap.

The session ends at `finish` with readable declarations, or at a cap (turns, edits): then the declarations already
accepted stand and nothing else does. A candidate that fails the ladder after the session comes back once (`revise`):
the verdict is appended and the session continues under the same caps; a revision changes files, not declarations.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..runtime.chat import ToolCall, ToolSpec
from .declaration import Declaration, read_declaration

NUDGE = ("Reply with tool calls: list_files, read_file, write_file, edit_file, delete_file, read_trajectory, "
         "search_trace, run_tests, run_check, or finish with your declarations. Text alone does nothing.")
EDIT_TOOLS = ("write_file", "edit_file", "delete_file")
ITEM_CHARS = 8000                      # of one message in the stored transcript
DECLARATION_SCHEMA = {"type": "object", "properties": {
    "dir": {"type": "string"}, "cause_id": {"type": "string"},
    "kind": {"type": "string"}, "site": {"type": "string"},
    "predicted_affected": {"type": "array", "items": {"type": "string"}},
    "trigger": {"type": "string"},
    "differs_from": {"type": "array", "items": {"type": "object"}},
    "scripts": {"type": "object"}}, "required": ["dir", "cause_id", "kind", "predicted_affected", "trigger"]}

TOOLS = (
    ToolSpec("list_files", "List the files under a directory of the work tree (default: all of it): package/ is the "
             "current package, inputs/ this round's inputs, candidates/<dir>/ your candidates.",
             {"type": "object", "properties": {"path": {"type": "string"}}}),
    ToolSpec("read_file", "Read a file of the work tree in pages: `offset` and `limit` in characters; when more "
             "follows, the answer says the offset to read next.",
             {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"},
                                               "limit": {"type": "integer"}}, "required": ["path"]}),
    ToolSpec("write_file", "Write a whole file under candidates/<dir>/; the first write under a new <dir> creates that "
             "candidate as a copy of the current package. Counts as one edit.",
             {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
              "required": ["path", "text"]}),
    ToolSpec("edit_file", "Replace one exact occurrence of `old` with `new` in a file under candidates/<dir>/. Counts "
             "as one edit.", {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"},
                                                               "new": {"type": "string"}},
                              "required": ["path", "old", "new"]}),
    ToolSpec("delete_file", "Delete a file under candidates/<dir>/. Counts as one edit.",
             {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
    ToolSpec("read_trajectory", "Read a train attempt as the agent saw it (its messages, tool calls and results, each "
             "with a trace reference), in pages of bytes. `session` is one listed in inputs/causes.json or "
             "inputs/successes.json.",
             {"type": "object", "properties": {"session": {"type": "string"}, "offset": {"type": "integer"},
                                               "limit": {"type": "integer"}}, "required": ["session"]}),
    ToolSpec("search_trace", "Full-text search over the train attempts' messages, tool calls and results; every word "
             "must match. Answers trace references with an excerpt.",
             {"type": "object", "properties": {"query": {"type": "string"}, "kind": {"type": "string"},
                                               "tool": {"type": "string"}}, "required": ["query"]}),
    ToolSpec("run_tests", "Run the package tests of candidates/<dir> in the isolated worker.",
             {"type": "object", "properties": {"dir": {"type": "string"}}, "required": ["dir"]}),
    ToolSpec("run_check", "Run the free part of the verification ladder (V0 static, V1 unit, V2 toolcheck / replay / "
             "reach, by kind) on candidates/<dir> under a declaration, before you finish. Nothing is paid.",
             {"type": "object", "properties": {"dir": {"type": "string"}, "declaration": DECLARATION_SCHEMA},
              "required": ["dir", "declaration"]}),
    ToolSpec("finish", "End the session with one declaration per candidate directory you want verified. A declaration "
             "the host cannot read is answered as this call's error; fix it and finish again.",
             {"type": "object", "properties": {"candidates": {"type": "array", "items": DECLARATION_SCHEMA}},
              "required": ["candidates"]}),
)


class WorkTree:
    """The host's handle on the session's `/work` in a worker session (worker_runner.py `work`). A refused action is
    the tool's error, handed to R."""

    def __init__(self, session):
        self.session = session

    def init(self, package: dict, inputs: dict, *, max_bytes: int) -> None:
        answer = self.session.work("init", package=dict(package), inputs=dict(inputs), max_bytes=int(max_bytes),
                                   max_file_bytes=int(max_bytes))
        if not answer.get("ok"):
            raise RuntimeError(f"the work tree could not be set up: {answer.get('error')}")

    def do(self, action: str, **fields) -> tuple[object, bool]:
        answer = self.session.work(action, **fields)
        return (answer.get("value"), False) if answer.get("ok") else (str(answer.get("error")), True)

    def fetch(self, name: str) -> tuple[dict | None, str]:
        value, bad = self.do("export", dir=name)
        return (None, str(value)[:300]) if bad or not isinstance(value, dict) else (dict(value), "")


@dataclass
class SessionResult:
    declarations: list = field(default_factory=list)     # [Declaration], in finish order, one per dir
    ended: str = ""                                       # why the session ended without a finish, or ""


class CodingSession:
    """One round's session. `researcher.chat(system, messages, tools) -> ChatTurn` is the metered research model;
    `check(dir, raw declaration) -> str` runs V0-V2 and answers the verdict's text; `tests(dir) -> str` the package
    tests; `readable` the train sessions read_trajectory may open; `kinds` the kinds this round assigned."""

    def __init__(self, *, researcher, work: WorkTree, trace, check, tests, settings, k: int, system: str,
                 inputs: dict, readable, kinds):
        self.researcher, self.work, self.trace = researcher, work, trace
        self.check, self.tests, self.s = check, tests, settings
        self.k, self.system, self.inputs = max(int(k), 1), system, inputs
        self.readable, self.kinds = set(readable), tuple(kinds)
        self.turns = self.edits = self.read_bytes = 0
        self.messages: list[dict] = []
        self.calls: list[dict] = []
        self.dirs: list[str] = []
        self.declarations: dict[str, Declaration] = {}
        self.ended = ""
        self.revising = False

    # ------------------------------------------------------------------ driving
    def start(self) -> SessionResult:
        self.messages = [{"role": "user", "content": json.dumps(self.inputs, ensure_ascii=False, default=str)}]
        return self._run()

    def revise(self, text: str) -> SessionResult:
        if self.ended:
            return SessionResult(list(self.declarations.values()), self.ended)
        self.revising = True
        self.messages.append({"role": "user", "content": text})
        return self._run()

    def _end(self, why: str) -> SessionResult:
        self.ended = why
        return SessionResult(list(self.declarations.values()), why)

    def _run(self) -> SessionResult:
        while True:
            if self.turns >= self.s.session_max_turns:
                return self._end(f"the session used its {self.s.session_max_turns} turns")
            self.turns += 1
            reply = self.researcher.chat(self.system, self.messages, list(TOOLS))
            calls = [c for c in getattr(reply, "tool_calls", None) or () if isinstance(c, ToolCall)]
            self.messages.append({"role": "assistant", "text": reply.text or "", "tool_calls": calls})
            if not calls:
                self.messages.append({"role": "user", "content": NUDGE})
                continue
            results, finished = [], False
            for call in calls:
                if finished:
                    results.append({"id": call.id, "name": call.name, "output": "not performed: the session "
                                    "finished", "is_error": True})
                    continue
                if call.name in EDIT_TOOLS and self.edits >= self.s.session_max_edits:
                    self.messages.append({"role": "tool", "results": results})
                    return self._end(f"the session used its {self.s.session_max_edits} edits")
                output, bad, finished = self._tool(call)
                results.append({"id": call.id, "name": call.name, "output": output, "is_error": bad})
            self.messages.append({"role": "tool", "results": results})
            if finished:
                return SessionResult(list(self.declarations.values()), "")

    # ------------------------------------------------------------------ the tools
    def _tool(self, call: ToolCall) -> tuple[str, bool, bool]:
        """-> (output, is_error, finished)."""
        args = call.args if isinstance(call.args, dict) else None
        record = {"turn": self.turns, "tool": call.name}
        self.calls.append(record)
        if args is None:
            record["ok"] = False
            return f"{call.name}: the arguments are a JSON object", True, False
        name = call.name
        if name in EDIT_TOOLS:
            self.edits += 1
            action = {"write_file": "write", "edit_file": "edit", "delete_file": "delete"}[name]
            fields = {"path": args.get("path")}
            if action == "write":
                fields["text"] = args.get("text")
            elif action == "edit":
                fields.update(old=args.get("old"), new=args.get("new"))
            value, bad = self.work.do(action, **fields)
            path = args.get("path")
            if not bad and isinstance(path, str) and path.count("/") >= 2 and path.split("/")[1] not in self.dirs:
                self.dirs.append(path.split("/")[1])
            record.update(path=path, ok=not bad)
            return (value if isinstance(value, str) else json.dumps(value)), bad, False
        if name == "list_files":
            value, bad = self.work.do("list", path=args.get("path") or ".")
            return (json.dumps(value) if not bad else value), bad, False
        if name == "read_file":
            value, bad = self.work.do("read", path=args.get("path"), offset=args.get("offset"),
                                      limit=args.get("limit"))
            return str(value), bad, False
        if name == "read_trajectory":
            return (*self._read_trajectory(args), False)
        if name == "search_trace":
            try:
                hits = self.trace.search(str(args.get("query") or ""), kind=args.get("kind"), tool=args.get("tool"),
                                         limit=20)
            except (ValueError, RuntimeError) as e:
                return f"search_trace: {e}", True, False
            return json.dumps([h for h in hits if h["session"] in self.readable], ensure_ascii=False), False, False
        if name == "run_tests":
            if args.get("dir") not in self.dirs:
                return f"no candidate directory {args.get('dir')!r} ({self.dirs or 'none yet'})", True, False
            return self.tests(str(args["dir"])), False, False
        if name == "run_check":
            if args.get("dir") not in self.dirs:
                return f"no candidate directory {args.get('dir')!r} ({self.dirs or 'none yet'})", True, False
            return self.check(str(args["dir"]), dict(args.get("declaration") or {})), False, False
        if name == "finish":
            problem = self._finish_problems(args)
            if problem:
                return problem, True, False
            return self._finish(args)[0], False, True
        return f"unknown tool {name!r}", True, False

    def _read_trajectory(self, args: dict) -> tuple[str, bool]:
        session = args.get("session")
        if session not in self.readable:
            return f"AccessDenied: {session!r} is not a train attempt of this round's inputs", True
        try:
            offset = max(int(args.get("offset") or 0), 0)
            limit = min(int(args.get("limit") or self.s.session_max_read_bytes), self.s.session_max_read_bytes)
        except (TypeError, ValueError):
            return "offset and limit are integers", True
        if limit < 1:
            return "limit is a positive number of bytes", True
        if self.read_bytes + limit > self.s.session_max_read_total:
            return (f"refused: this read would pass the session's {self.s.session_max_read_total} bytes "
                    f"({self.read_bytes} read so far)"), True
        data = json.dumps(self.trace.trajectory(session), ensure_ascii=False, indent=1).encode("utf-8")
        chunk = data[offset:offset + limit]
        self.read_bytes += len(chunk)
        rest = len(data) - offset - len(chunk)
        return chunk.decode("utf-8", errors="replace") + (f"\n[... {rest} bytes follow; read again with offset "
                                                          f"{offset + len(chunk)}]" if rest > 0 else ""), False

    def _finish_problems(self, args: dict) -> str:
        raw = args.get("candidates")
        if not isinstance(raw, list):
            return "finish takes candidates: a list of declarations (an empty list ends the session with none)"
        if len(raw) > self.k:
            return f"at most {self.k} candidates this round"
        problems, seen = [], set()
        for i, item in enumerate(raw):
            decl, why = read_declaration(item, dirs=self.dirs, kinds_allowed=self.kinds)
            if why:
                problems.append(f"candidates[{i}]: {why}")
            elif decl.dir in seen:
                problems.append(f"candidates[{i}]: dir {decl.dir!r} is declared twice")
            elif self.revising and decl.dir not in self.declarations:
                problems.append(f"candidates[{i}]: a revision changes files of {sorted(self.declarations)}; it "
                                f"declares no new candidate")
            else:
                seen.add(decl.dir)
        return ("the declarations are not readable: " + "; ".join(problems))[:2000] if problems else ""

    def _finish(self, args: dict) -> tuple[str, bool]:
        for item in args["candidates"]:
            decl, _ = read_declaration(item, dirs=self.dirs, kinds_allowed=self.kinds)
            if decl.dir not in self.declarations:          # a revision changes files, never a declaration
                self.declarations[decl.dir] = decl
        return "declared: " + (", ".join(self.declarations) or "nothing"), False

    # ------------------------------------------------------------------ records
    def record(self) -> dict:
        return {"turns": self.turns, "edits": self.edits, "read_bytes": self.read_bytes, "dirs": list(self.dirs),
                "declared": list(self.declarations), "ended": self.ended, "calls": list(self.calls)}

    def transcript(self) -> dict:
        items = []
        for m in self.messages:
            if m.get("role") == "assistant":
                items.append({"role": "assistant", "text": _cut(m.get("text") or ""),
                              "tool_calls": [{"id": c.id, "name": c.name, "args": _cut_all(c.args)}
                                             for c in m.get("tool_calls") or ()]})
            elif m.get("role") == "tool":
                items.append({"role": "tool", "results": [{**r, "output": _cut(str(r.get("output") or ""))}
                                                          for r in m.get("results") or ()]})
            else:
                items.append({"role": m.get("role"), "content": _cut(str(m.get("content") or ""))})
        return {"schema": "coding_session.v1", "turns": self.turns, "ended": self.ended, "messages": items}


def _cut(text: str, n: int = ITEM_CHARS) -> str:
    text = text.encode("utf-8", "backslashreplace").decode("utf-8")
    return text if len(text) <= n else text[:n] + f"... [{len(text) - n} more characters]"


def _cut_all(value):
    if isinstance(value, str):
        return _cut(value)
    if isinstance(value, dict):
        return {k: _cut_all(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_cut_all(v) for v in value]
    return value
