"""R's coding session (docs/design/IMPROVER.md §7; DESIGN §6.5, v1.2): one bounded session per round, one package.

The model loop runs here, on the host, through the metered research provider (each turn reserved and settled); the
files live in the isolated worker (`/work`: `package/` read-only, `inputs/` read-only, `candidates/package/` writable,
created by its first write as a copy of the current package; `candidates/package-2/` and on when K allows more than
one package a round). What R can do is the tool list and nothing else: read and write the tree, read train
trajectories (byte-capped) and search them, run the package's tests or its whole free ladder (V0-V2) before it
finishes, and finish with one declaration per package: the mechanism in a sentence and every component it changes.
There is no shell, no network, no host file, no dev or test content, and no tool that raises a cap.

The session ends at `finish` with readable declarations, or at a cap (turns, edits): then the declarations already
accepted stand and nothing else does. A package that fails the ladder after the session comes back once (`revise`):
the verdict is appended and the session continues under the same caps; a revision changes files, not declarations.
The in-sample trial's results come back the same way (round.py, v1.9). At EDIT_REMINDERS of the edit cap the tool's
result carries a line saying how many edits are left (finance-v8's s1 used its 48 edits and never finished).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..runtime.chat import ToolCall, ToolSpec
from .declaration import DEFAULT_DIR, Declaration, read_declaration

NUDGE = ("Reply with tool calls: list_files, read_file, write_file, edit_file, delete_file, read_trajectory, "
         "search_trace, run_tests, run_check, or finish with your package's declaration. Text alone does nothing.")
CUT = ("Your last reply was cut at the output limit and none of its calls ran. Write one file per reply, change "
       "existing files with edit_file, and keep a run_check or finish with many scripts small.")
REFUSED = "Your last reply was refused and none of its calls ran. Reply with tool calls for the assigned work."
KEEP_RECENT = 4                         # tool messages (and assistant turns) kept whole when the session is compacted
COMPACT_TO = 0.6                        # of session_compact_tokens: where a compaction brings the context down to
CHARS_PER_TOKEN = 3                     # the estimate of a call's tokens from its text (JSON with code: on the low side)
READ_TOOLS = ("read_file", "read_trajectory")
EDIT_TOOLS = ("write_file", "edit_file", "delete_file")
ITEM_CHARS = 8000                      # of one message in the stored transcript
EDIT_REMINDERS = (0.75, 0.9)           # v1.9: shares of session_max_edits at which a tool result reminds R to finish
COMPONENT_SCHEMA = {"type": "object", "properties": {
    "kind": {"type": "string"}, "site": {"type": "string"},
    "cause_ids": {"type": "array", "items": {"type": "string"}},
    "predicted_affected": {"type": "array", "items": {"type": "string"}},
    "trigger": {"type": "string"}, "scripts": {"type": "object"}, "probes": {"type": "object"},
    "uses_model": {"type": "boolean"}},
    "required": ["kind", "cause_ids", "trigger"]}
DECLARATION_SCHEMA = {"type": "object", "properties": {
    "dir": {"type": "string"}, "mechanism": {"type": "string"},
    "components": {"type": "array", "items": COMPONENT_SCHEMA},
    "differs_from": {"type": "array", "items": {"type": "object"}},
    "scope": {"type": "string"}},
    "required": ["mechanism", "components"]}

TOOLS = (
    ToolSpec("list_files", "List the files under a directory of the work tree (default: all of it): package/ is the "
             "current package, inputs/ this round's inputs, candidates/package/ your candidate package.",
             {"type": "object", "properties": {"path": {"type": "string"}}}),
    ToolSpec("read_file", "Read a file of the work tree in pages: `offset` and `limit` in characters; when more "
             "follows, the answer says the offset to read next.",
             {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"},
                                               "limit": {"type": "integer"}}, "required": ["path"]}),
    ToolSpec("write_file", "Write a whole file under candidates/package/; the first write there creates the candidate "
             "package as a copy of the current one. Counts as one edit.",
             {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
              "required": ["path", "text"]}),
    ToolSpec("edit_file", "Replace one exact occurrence of `old` with `new` in a file under candidates/package/. "
             "Counts as one edit.", {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"},
                                                               "new": {"type": "string"}},
                              "required": ["path", "old", "new"]}),
    ToolSpec("delete_file", "Delete a file under candidates/package/. Counts as one edit.",
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
    ToolSpec("run_tests", "Run the package tests of your candidate package (candidates/package, or `dir`) in the "
             "isolated worker.", {"type": "object", "properties": {"dir": {"type": "string"}}}),
    ToolSpec("run_check", "Run the free part of the verification ladder on your candidate package under the "
             "declaration you would finish with: V0 (shape, limits, literals, the declaration against the diff both "
             "ways), V1 (tests, probe, smoke), then each component's rung — V2a toolcheck for a tool, V2b replay for a "
             "hook, V2c reach for a skill. Nothing is paid.",
             {"type": "object", "properties": {"declaration": DECLARATION_SCHEMA, "dir": {"type": "string"}},
              "required": ["declaration"]}),
    ToolSpec("try_task", "Run your candidate package (candidates/package, or `dir`) once on one studied failing task "
             "(a task of inputs/failures.json) in the real world, paid from the round's trial attempts. The answer "
             "puts this run beside the working line's reading of the task: pass or fail (one rollout, not a "
             "confirmation), the checks that failed, the hooks that acted and what they said, the writes by route, "
             "the final text, which of your components acted, and the session for read_trajectory. At most "
             "try_per_task tries per task; the package must build and pass V1 first.",
             {"type": "object", "properties": {"task": {"type": "string"}, "dir": {"type": "string"}},
              "required": ["task"]}),
    ToolSpec("finish", "End the session with the declaration of each candidate package you want verified (one, unless "
             "the round allows more): {mechanism, components: [{kind, site, cause_ids, predicted_affected, trigger, "
             "scripts?, probes?, uses_model?}], differs_from, scope?}. A declaration the host cannot read is answered as this "
             "call's error; fix it and finish again. An empty list ends the session with no candidate.",
             {"type": "object", "properties": {"packages": {"type": "array", "items": DECLARATION_SCHEMA}},
              "required": ["packages"]}),
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
    tests; `readable` the train sessions read_trajectory may open; `kinds` the component kinds K allows."""

    def __init__(self, *, researcher, work: WorkTree, trace, check, tests, settings, system: str, inputs: dict,
                 readable, kinds, merge_ids=(), tryer=None, require_causes: bool = True):
        self.researcher, self.work, self.trace = researcher, work, trace
        self.check, self.tests, self.s = check, tests, settings
        self.tryer = tryer                          # v1.11: try_task -> (text, is_error); None: the tool is refused
        self.require_causes = require_causes        # v2.0: the repeat loop has no causes (DESIGN §6.23)
        self.n, self.system, self.inputs = max(int(settings.packages_per_round), 1), system, inputs
        self.readable, self.kinds = set(readable), tuple(kinds)
        self.merge_ids = tuple(merge_ids)          # v1.8: the frontier entries a declaration's merge_of may name
        self.turns = self.edits = self.read_bytes = 0
        self.messages: list[dict] = []
        self.calls: list[dict] = []
        self.dirs: list[str] = []
        self.declarations: dict[str, Declaration] = {}
        self.ended = ""
        self.revising = False
        self.cut = 0                    # replies in a row cut at the output limit
        self.compactions = 0
        self.reminded: list = []        # v1.9: the EDIT_REMINDERS shares already said

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

    def resume(self) -> SessionResult:
        """A session restored from a checkpoint goes on where it stopped, under the same caps."""
        if self.ended:
            return SessionResult(list(self.declarations.values()), self.ended)
        return self._run()

    def _run(self) -> SessionResult:
        while True:
            if self.turns >= self.s.session_max_turns:
                return self._end(f"the session used its {self.s.session_max_turns} turns")
            self.turns += 1
            self._compact()
            reply = self.researcher.chat(self.system, self.messages, list(TOOLS))
            calls = [c for c in getattr(reply, "tool_calls", None) or () if isinstance(c, ToolCall)]
            self.messages.append({"role": "assistant", "text": reply.text or "", "tool_calls": calls})
            if not calls:
                stop = getattr(reply, "stop", "")
                self.cut = self.cut + 1 if stop == "max_tokens" else 0
                if self.cut >= 2:
                    return self._end("two replies in a row were cut at the output limit")
                self.messages.append({"role": "user", "content": CUT if stop == "max_tokens" else
                                      REFUSED if stop == "refusal" else NUDGE})
                continue
            self.cut = 0
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
            return (value if isinstance(value, str) else json.dumps(value)) + self._edit_reminder(), bad, False
        if name == "list_files":
            value, bad = self.work.do("list", path=args.get("path") or ".")
            return (json.dumps(value) if not bad else value), bad, False
        if name == "read_file":
            left = self.s.session_max_read_total - self.read_bytes
            try:
                want = int(args.get("limit") or self.s.session_max_read_bytes)
            except (TypeError, ValueError):
                return "offset and limit are integers", True, False
            if want < 1:
                return "limit is a positive number of characters", True, False
            if left <= 0:
                return (f"refused: the session has read its {self.s.session_max_read_total} bytes (files and "
                        f"trajectories together)"), True, False
            value, bad = self.work.do("read", path=args.get("path"), offset=args.get("offset"),
                                      limit=min(want, self.s.session_max_read_bytes, left))
            if not bad:
                self.read_bytes += len(str(value).encode("utf-8"))
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
            d = args.get("dir") or DEFAULT_DIR
            if d not in self.dirs:
                return f"no candidate directory {d!r} ({self.dirs or 'none yet'})", True, False
            return self.tests(str(d)), False, False
        if name == "run_check":
            raw = dict(args.get("declaration") or {})
            d = args.get("dir") or raw.get("dir") or DEFAULT_DIR
            if d not in self.dirs:
                return f"no candidate directory {d!r} ({self.dirs or 'none yet'})", True, False
            return self.check(str(d), raw), False, False
        if name == "try_task":
            d = args.get("dir") or DEFAULT_DIR
            if d not in self.dirs:
                return f"no candidate directory {d!r} ({self.dirs or 'none yet'})", True, False
            task = args.get("task")
            if not isinstance(task, str) or not task:
                return "task is a studied task id (inputs/failures.json)", True, False
            if self.tryer is None:
                return "try_task is not available in this session", True, False
            text, bad = self.tryer(str(d), task)
            record.update(task=task, ok=not bad)
            return text, bad, False
        if name == "finish":
            problem = self._finish_problems(args)
            if problem:
                return problem, True, False
            return self._finish(args)[0], False, True
        return f"unknown tool {name!r}", True, False

    def _edit_reminder(self) -> str:
        """A line for the tool result when the edits just reached one of EDIT_REMINDERS of the cap (v1.9)."""
        cap = int(self.s.session_max_edits)
        for share in EDIT_REMINDERS:
            if share not in self.reminded and self.edits >= max(1, int(round(share * cap))):
                self.reminded.append(share)
                return (f"\n[note: {self.edits} of the session's {cap} edits are used; a session that runs out of "
                        f"edits without finish hands in nothing — call finish with your declaration while edits are "
                        f"left]")
        return ""

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
        raw = args.get("packages")
        if not isinstance(raw, list):
            return "finish takes packages: a list of declarations (an empty list ends the session with none)"
        if len(raw) > self.n:
            return f"at most {self.n} candidate package{'s' if self.n > 1 else ''} this round"
        problems, seen = [], set()
        for i, item in enumerate(raw):
            decl, why = read_declaration(item, dirs=self.dirs, kinds_allowed=self.kinds, merge_ids=self.merge_ids,
                                         require_causes=self.require_causes)
            if why:
                problems.append(f"packages[{i}]: {why}")
            elif decl.dir in seen:
                problems.append(f"packages[{i}]: dir {decl.dir!r} is declared twice")
            elif self.revising and decl.dir not in self.declarations:
                problems.append(f"packages[{i}]: a revision changes files of {sorted(self.declarations)}; it "
                                f"declares no new package")
            else:
                seen.add(decl.dir)
        return ("the declarations are not readable: " + "; ".join(problems))[:2000] if problems else ""

    def _finish(self, args: dict) -> tuple[str, bool]:
        for item in args["packages"]:
            decl, _ = read_declaration(item, dirs=self.dirs, kinds_allowed=self.kinds, merge_ids=self.merge_ids,
                                       require_causes=self.require_causes)
            if decl.dir not in self.declarations:          # a revision changes files, never a declaration
                self.declarations[decl.dir] = decl
        return "declared: " + (", ".join(self.declarations) or "nothing"), False

    # ------------------------------------------------------------------ the context
    def _context_tokens(self) -> int:
        """What the next call sends, in tokens, estimated from its text (CHARS_PER_TOKEN characters a token, on the
        low side): the billed usage of a call sums the CLI's continuations and retries, and says nothing exact."""
        sent = json.dumps([_plain(m) for m in self.messages], ensure_ascii=False, default=str)
        return (len(self.system or "") + len(sent)) // CHARS_PER_TOKEN

    def _compact(self) -> None:
        """Before a call whose context passes session_compact_tokens: the outputs of the older tool messages and the
        texts the older assistant turns wrote become a line that says how to see them again, keeping the last
        KEEP_RECENT (fewer if that is not enough) until the context is under COMPACT_TO of the threshold. Between two
        compactions each call extends the one before (the cache). An elided read gives its bytes back to the
        session's read total, so reading it again is not refused."""
        limit = int(self.s.session_compact_tokens)
        if self._context_tokens() <= limit:
            return
        changed = False
        for keep in (KEEP_RECENT, 2, 1):
            tools = [i for i, m in enumerate(self.messages) if m.get("role") == "tool"][:-keep]
            turns = [i for i, m in enumerate(self.messages) if m.get("role") == "assistant"][:-keep]
            for i in tools:
                results = []
                for r in self.messages[i].get("results") or ():
                    out = str(r.get("output") or "")
                    if len(out) > 300 and not out.startswith("[elided"):
                        if r.get("name") in READ_TOOLS and not r.get("is_error"):
                            self.read_bytes = max(self.read_bytes - len(out.encode("utf-8")), 0)
                        r = {**r, "output": f"[elided to keep the session within its context: {len(out)} "
                                            f"characters; call {r.get('name')} again to see it]"}
                        changed = True
                    results.append(r)
                self.messages[i] = {**self.messages[i], "results": results}
            for i in turns:
                calls = []
                for c in self.messages[i].get("tool_calls") or ():
                    args = dict(c.args) if isinstance(c.args, dict) else c.args
                    for key in ("text", "old", "new"):
                        if isinstance(args, dict) and isinstance(args.get(key), str) and len(args[key]) > 300:
                            args[key] = f"[elided: {len(args[key])} characters; read the file to see it]"
                            changed = True
                    calls.append(ToolCall(c.id, c.name, args))
                self.messages[i] = {**self.messages[i], "tool_calls": calls}
            if self._context_tokens() <= limit * COMPACT_TO:
                break
        self.compactions += changed

    # ------------------------------------------------------------------ checkpoints
    def state(self) -> dict:
        """What a resumed session needs (the candidate directories' files are the host's to save)."""
        return {"messages": [_plain(m) for m in self.messages], "turns": self.turns, "edits": self.edits,
                "read_bytes": self.read_bytes, "calls": list(self.calls), "dirs": list(self.dirs),
                "declarations": {d: decl.to_json() for d, decl in self.declarations.items()}, "ended": self.ended,
                "revising": self.revising, "cut": self.cut, "compactions": self.compactions,
                "reminded": list(self.reminded)}

    def restore(self, state: dict) -> None:
        self.messages = [_unplain(m) for m in state.get("messages") or ()]
        self.turns, self.edits = int(state.get("turns") or 0), int(state.get("edits") or 0)
        self.read_bytes, self.calls = int(state.get("read_bytes") or 0), list(state.get("calls") or ())
        self.dirs = list(state.get("dirs") or ())
        self.declarations = {d: Declaration.from_json(v) for d, v in (state.get("declarations") or {}).items()}
        self.ended, self.revising = str(state.get("ended") or ""), bool(state.get("revising"))
        self.cut, self.compactions = int(state.get("cut") or 0), int(state.get("compactions") or 0)
        self.reminded = list(state.get("reminded") or ())

    # ------------------------------------------------------------------ records
    def record(self) -> dict:
        return {"turns": self.turns, "edits": self.edits, "read_bytes": self.read_bytes, "dirs": list(self.dirs),
                "declared": list(self.declarations), "ended": self.ended, "calls": list(self.calls),
                "compactions": self.compactions}

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


def _plain(m: dict) -> dict:
    if m.get("role") != "assistant":
        return dict(m)
    return {**m, "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in m.get("tool_calls") or ()]}


def _unplain(m: dict) -> dict:
    if m.get("role") != "assistant":
        return dict(m)
    return {**m, "tool_calls": [ToolCall(c["id"], c["name"], c["args"]) for c in m.get("tool_calls") or ()]}
