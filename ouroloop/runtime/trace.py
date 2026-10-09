"""Trace memory (docs/design/MEMORY.md §2; IMPROVER §4; DESIGN §6.2): stable references into the attempt transcripts,
the trajectory of an attempt rebuilt as the actor saw it (across Stirrup's context summaries), and a full-text index —
all of it train only, enforced here.

The host already writes every intermediate output (runtime/records.py): each model call as a `gateway_call` line
(the messages as a prefix count against the previous call plus the tail, the reply, the usage), each base tool call as
a `tool` line (its output cut at EVENT_CHARS) and a `broker_event`, the package's marks (`mark` lines, the package's
kind under `mark` and its fields under `data`) and the hooks that acted (`hook` lines). Nothing here writes a
transcript: the records keep their bytes, and everything below is derived at read time.

- A session is an attempt id; its transcript is `transcripts/<session with / as __>.jsonl` (records.session_file).
- A reference is `<session>:L<line>`: the 1-based line of the session's transcript, which is append-only, so a
  reference never moves. `get(ref)` and `events(session)` read them.
- `trajectory(session)` -> OpenAI chat messages: the system and the task, then for each act call the user messages
  it added, its reply (text and tool calls) and the results of those calls. Each result is taken from the first
  later call (act or aux) whose messages carry its call id — the full text the actor saw — and only when no call
  carries it (the last turn, an attempt cut by a limit) from the `tool` line or the package's mark, marked as an
  excerpt. An act call that follows an aux summary call whose history was replaced (Stirrup's summarize_messages:
  task context + the summary bridge + an acknowledgement) contributes neither the bridge nor the acknowledgement;
  they are kept only as an annotation, and the aux calls contribute no messages, so the rebuilt trajectory still
  holds everything before the summary. Each message carries a `metadata` annotation (its trace reference, and for a
  result where it came from); `annotations=False` drops them.
- `search(query, ...)` reads an SQLite FTS5 index at <workspace>/index/trace.sqlite: derived, rebuildable, updated
  incrementally per (session, last line indexed). It holds the assistant texts, the tool-call arguments, the full
  tool results from the gateway_call tails (and, once a session has ended, the excerpts of results no later call
  carried), the marks, the hooks and the `egress` lines. Without FTS5 it raises; there is no other search.

Train only: `split_of(task_id)` is the frozen split's ("train" | "dev" | "test"), the task id is the session's `task`
line's. For a session that is not train only that first line is read, to learn its split; it is never indexed, and
get, events and trajectory raise TraceAccessError for it.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable, Iterator

from .gateway import common_prefix
from .records import EVENT_CHARS, SESSION_ID, session_file

SCHEMA = 2
REF = re.compile(r"^(?P<session>.+):L(?P<line>[1-9][0-9]{0,8})$")
# Stirrup's acknowledgement after its summary bridge (vendor/stirrup/core/agent.py, summarize_messages). Only a user
# message with exactly this text is dropped with the bridge: a message R's edits put there instead is kept.
ACKNOWLEDGEMENTS = ("Got it, thanks!",)
INDEXED_KINDS = ("assistant", "tool_call", "tool_result", "hook", "mark", "egress")
CONTENT_KINDS = ("gateway_call", "tool", "broker_event", "tool_specs", "hook", "mark")


class TraceError(RuntimeError):
    """A trace that cannot be read as asked: a malformed reference, a session with no transcript, a broken
    gateway_call chain."""


class TraceAccessError(TraceError):
    """The session is not a train session (or its split is unknown): trace memory reads train only."""


class TraceIndexError(TraceError):
    """The full-text index cannot be built here (no SQLite FTS5)."""


def _roles(messages) -> list:
    return [m.get("role") if isinstance(m, dict) else None for m in messages]


def _content(message) -> str:
    value = message.get("content") if isinstance(message, dict) else None
    if isinstance(value, str):
        return value
    if isinstance(value, list):            # text parts, as an OpenAI-shaped agent may send them
        return "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in value)
    return "" if value is None else str(value)


def to_openai_call(call: dict) -> dict:
    return {"id": str(call.get("id") or ""), "type": "function",
            "function": {"name": str(call.get("name") or ""),
                         "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False, sort_keys=True)}}


def plain(messages: list[dict]) -> list[dict]:
    """Messages without their annotations: the OpenAI chat format alone."""
    return [{k: v for k, v in m.items() if k != "metadata"} for m in messages]


def _normal(event: dict) -> dict:
    """A transcript line as the readers below take it: a mark's own fields beside its kind (`mark` names the
    package's kind), a gateway_call's reply as `response`."""
    kind = event.get("kind")
    if kind == "mark":
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        return {**{k: v for k, v in data.items() if k not in ("kind", "mark")}, **event}
    if kind == "gateway_call" and "response" not in event:
        return {**event, "response": event.get("reply") or {}}
    return event


def parse_ref(ref: str) -> tuple[str, int]:
    match = REF.match(ref) if isinstance(ref, str) else None
    if match is None or not SESSION_ID.fullmatch(match.group("session")):
        raise TraceError(f"not a trace reference (<session>:L<line>): {ref!r}")
    return match.group("session"), int(match.group("line"))


class TraceStore:
    """Trace memory over one workspace's transcripts (<workspace>/transcripts/<session>.jsonl). `split_of(task_id)`
    is the frozen split's assignment (KeyError for a task it does not know)."""

    def __init__(self, workspace_root, split_of: Callable[[str], str]):
        self.root = Path(workspace_root)
        self.transcripts = self.root / "transcripts"
        self.index_path = self.root / "index" / "trace.sqlite"
        self.split_of = split_of
        self._splits: dict[str, tuple[str | None, str | None]] = {}     # session -> (task_hash, split)

    # ------------------------------------------------------------------ sessions and splits
    def _path(self, session: str) -> Path:
        try:
            path = session_file(self.root, session)
        except ValueError as e:
            raise TraceError(f"not a session id: {session!r}") from e
        if not path.is_file():
            raise TraceError(f"no transcript for session {session!r}")
        return path

    def _first_task(self, path: Path) -> tuple[dict | None, bool]:
        """(the session's `task` line, whether that answer is final). A transcript with no complete `task` line yet
        (a live attempt just started) is read again next time; content before any task is final (None)."""
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                if not raw.endswith("\n"):
                    return None, False         # an append in progress: not read yet
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    return None, False
                kind = event.get("kind") if isinstance(event, dict) else None
                if kind == "task":
                    return event, True
                if kind in CONTENT_KINDS:
                    return None, True          # content before any task: not an attempt this store can place, ever
        return None, False

    def _classify(self, session: str) -> tuple[str | None, str | None]:
        """(task_hash, split) of a session, read from its `task` line and nothing after it. Only an answer that
        cannot change is kept for the store's lifetime: a split the split table gave, or content before any task."""
        if session in self._splits:
            return self._splits[session]
        task, final = self._first_task(self._path(session))
        found: tuple[str | None, str | None] = (None, None)
        if task is not None:
            try:
                split = self.split_of(str(task.get("task_id") or ""))
            except (KeyError, LookupError, ValueError):
                split = None
            found = (str(task.get("task_hash") or "") or None, split if isinstance(split, str) else None)
            final = found[1] is not None
        if final:
            self._splits[session] = found
        return found

    def split(self, session: str) -> str | None:
        return self._classify(session)[1]

    def task_hash(self, session: str) -> str:
        digest, split = self._classify(session)
        if split != "train":
            raise TraceAccessError(f"session {session} is not a train session ({split or 'split unknown'}): trace "
                                   f"memory reads train only")
        return digest

    def _train(self, session: str) -> Path:
        self.task_hash(session)
        return self._path(session)

    def sessions(self) -> list[dict]:
        """The train sessions on disk: [{session, task_hash}], by name. Only the first line of the others is read."""
        out = []
        if not self.transcripts.is_dir():
            return out
        for path in sorted(self.transcripts.glob("*.jsonl")):
            task, _ = self._first_task(path)
            session = str((task or {}).get("attempt_id") or "")
            if not session or not SESSION_ID.fullmatch(session):
                continue
            try:
                if session_file(self.root, session) != path:
                    continue
            except ValueError:
                continue
            digest, split = self._classify(session)
            if split == "train":
                out.append({"session": session, "task_hash": digest})
        return sorted(out, key=lambda r: r["session"])

    # ------------------------------------------------------------------ events and references
    @staticmethod
    def ref(session: str, line: int) -> str:
        return f"{session}:L{int(line)}"

    @staticmethod
    def _read(path: Path, offset: int = 0, line: int = 0) -> Iterator[tuple[int, dict, int]]:
        """(line number, event, byte offset after the line) for every complete line from byte `offset`, which starts
        line `line` + 1. A torn last line (an append in progress) is not read yet."""
        with path.open("rb") as f:
            f.seek(offset)
            n = line
            for raw in f:
                if not raw.endswith(b"\n"):
                    return
                n += 1
                offset += len(raw)
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    raise TraceError(f"{path.name}:L{n} is not a JSON line: {e}") from None
                if isinstance(event, dict):
                    yield n, _normal(event), offset

    @classmethod
    def _lines(cls, path: Path) -> Iterator[tuple[int, dict]]:
        """(line number, event) for every complete line of a transcript's last run: an attempt that died without a
        result runs again under its id and appends to the same file, and its trajectory is the last run's (from the
        last `task` line), not the two spliced."""
        rows = [(n, event) for n, event, _ in cls._read(path)]
        start = max((i for i, (_n, e) in enumerate(rows) if e.get("kind") == "task"), default=0)
        yield from rows[start:]

    def events(self, session: str) -> list[dict]:
        """[{ref, line, event}] of a train session, in transcript order."""
        path = self._train(session)
        return [{"ref": self.ref(session, n), "line": n, "event": e} for n, e in self._lines(path)]

    def get(self, ref: str) -> dict:
        """{ref, line, event} of one reference into a train session."""
        session, line = parse_ref(ref)
        path = self._train(session)
        for n, event in self._lines(path):
            if n == line:
                return {"ref": ref, "line": n, "event": event}
            if n > line:
                break
        raise TraceError(f"{ref}: no event on that line")

    def finish(self, session: str) -> dict:
        """What a train attempt said at its end: {reason (the last finish call's), text (the last act reply with any text),
        termination, shown (ids of the entries the memory gave it, start then recall, in order)}."""
        reason, text, termination, shown = None, "", None, []
        for _n, e in self._lines(self._train(session)):
            kind = e.get("kind")
            if kind == "gateway_call" and e.get("role") == "act":
                response = e.get("response") or {}
                text = str(response.get("text") or "") or text
                for call in response.get("tool_calls") or ():
                    if isinstance(call, dict) and call.get("name") == "finish":
                        reason = str((call.get("args") or {}).get("reason") or "")
            elif kind == "library_retrieval":
                shown += [str(i) for i in e.get("shown") or () if i and str(i) not in shown]
            elif kind == "end":
                termination = e.get("termination")
        return {"reason": reason, "text": text, "termination": termination, "shown": shown}

    # ------------------------------------------------------------------ the trajectory
    def trajectory(self, session: str, *, annotations: bool = True) -> list[dict]:
        rows = list(self._lines(self._train(session)))
        built = _Rebuild(session, rows).messages()
        return built if annotations else plain(built)

    def calls(self, session: str) -> list[dict]:
        """Every model call of a train session with the full messages it sent (the prefix chain resolved):
        [{ref, call_id, role, messages, response}]."""
        rows = list(self._lines(self._train(session)))
        rebuild = _Rebuild(session, rows)
        return [{"ref": self.ref(session, line), "call_id": c.get("call_id"), "role": c.get("role"),
                 "messages": msgs, "response": c.get("response")}
                for (line, c), msgs in zip(rebuild.calls, rebuild.full)]

    # ------------------------------------------------------------------ the index
    def _drop_index(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.index_path) + suffix)
            if path.exists():
                path.unlink()

    def _open(self) -> tuple[sqlite3.Connection, int | None]:
        db = sqlite3.connect(self.index_path, timeout=30)
        try:
            _wal(db)
            row = db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
            return db, (int(row[0]) if row else None)
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return db, None
            db.close()
            raise
        except sqlite3.DatabaseError:
            db.close()
            raise

    def _connect(self) -> sqlite3.Connection:
        _require_fts5()
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            db, version = self._open()
        except sqlite3.DatabaseError as e:
            if isinstance(e, sqlite3.OperationalError) and "locked" in str(e):
                raise
            # not a database (a torn or foreign file): the index is derived data, so it is built again
            self._drop_index()
            db, version = self._open()
        if version != SCHEMA:
            # under the write lock, and checked again there: two stores opening a new index at once build it once
            db.execute("BEGIN IMMEDIATE")
            try:
                row = None
                if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone():
                    row = db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
                if not row or int(row[0]) != SCHEMA:
                    for statement in SCHEMA_SQL:
                        db.execute(statement)
                    db.execute("INSERT INTO meta (key, value) VALUES ('schema', ?)", (str(SCHEMA),))
                db.commit()
            except BaseException:
                db.rollback()
                db.close()
                raise
        return db

    def index(self) -> dict:
        """Bring the index up to date: the new lines of every train session since the last call. -> counts."""
        stats = {"sessions": 0, "lines": 0, "docs": 0}
        db = self._connect()
        try:
            for entry in self.sessions():
                added = self._index_session(db, entry["session"], entry["task_hash"])
                if added is not None:
                    stats["sessions"] += 1
                    stats["lines"] += added[0]
                    stats["docs"] += added[1]
        finally:
            db.close()
        return stats

    def rebuild(self) -> dict:
        """Drop the index and build it again from the transcripts (it is derived data)."""
        self._drop_index()
        return self.index()

    def _index_session(self, db: sqlite3.Connection, session: str, digest: str):
        """One session's new lines, read and inserted under the index's write lock (BEGIN IMMEDIATE before the
        session's row is read): two stores indexing the same session at once insert its documents once."""
        path = self._path(session)
        db.execute("BEGIN IMMEDIATE")
        try:
            added = self._index_locked(db, session, digest, path)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return added

    def _index_locked(self, db: sqlite3.Connection, session: str, digest: str, path: Path):
        row = db.execute("SELECT lines, offset, ended FROM sessions WHERE session = ?", (session,)).fetchone()
        done, offset, ended = (int(row[0]), int(row[1]), bool(row[2])) if row else (0, 0, False)
        if path.stat().st_size == offset:
            return None                              # not grown since: not opened
        docs: list[tuple] = []
        seen = {r[0] for r in db.execute("SELECT call_id FROM results WHERE session = ?", (session,))}
        new_results: list[str] = []
        last, end_offset = done, offset
        finished = ended
        for n, event, after in self._read(path, offset, done):
            last, end_offset = n, after
            ref = self.ref(session, n)
            kind = event.get("kind")
            if kind == "gateway_call":
                response = event.get("response") or {}
                if response.get("text"):
                    docs.append((str(response["text"]), ref, "assistant", None))
                for call in response.get("tool_calls") or ():
                    docs.append((json.dumps(call.get("args") or {}, ensure_ascii=False, sort_keys=True), ref,
                                 "tool_call", call.get("name")))
                for message in event.get("messages_tail") or ():
                    if not isinstance(message, dict) or message.get("role") != "tool":
                        continue
                    for result in message.get("results") or ():
                        if not isinstance(result, dict):
                            continue
                        call_id = str(result.get("id") or "")
                        if call_id in seen:
                            continue                 # re-sent (an agent's compaction, a rewritten history)
                        if call_id:
                            seen.add(call_id)
                            new_results.append(call_id)
                        docs.append((str(result.get("output") or ""), ref, "tool_result", result.get("name")))
            elif kind in ("hook", "mark", "egress"):
                data = {k: v for k, v in event.items() if k not in ("ts", "seq", "kind", "source", "data")}
                tool = event.get("tool") if isinstance(event.get("tool"), str) else None
                docs.append((json.dumps(data, ensure_ascii=False, sort_keys=True, default=str), ref, kind, tool))
            elif kind == "end":
                finished = True
        if end_offset == offset:
            return None                              # only a torn line so far
        if finished and not ended:
            # the attempt is over: a result no later call carried (its last turn) is indexed from its excerpt
            try:
                rebuilt = _Rebuild(session, list(self._lines(path))).messages()
            except TraceError:
                rebuilt = []                        # an attempt that ended before its first model call
            for message in rebuilt:
                meta = message.get("metadata") or {}
                if message.get("role") == "tool" and meta.get("source") != "gateway_call" and \
                        message.get("tool_call_id") not in seen and message.get("tool_call_id"):
                    seen.add(message["tool_call_id"])
                    new_results.append(message["tool_call_id"])
                    docs.append((message.get("content") or "", meta.get("ref"), "tool_result", meta.get("tool")))
        db.executemany("INSERT INTO docs (text, ref, session, task_hash, kind, tool) VALUES (?, ?, ?, ?, ?, ?)",
                       [(text, ref, session, digest, kind, tool) for text, ref, kind, tool in docs])
        db.executemany("INSERT OR IGNORE INTO results (session, call_id) VALUES (?, ?)",
                       [(session, c) for c in new_results])
        db.execute("INSERT INTO sessions (session, task_hash, lines, offset, ended) VALUES (?, ?, ?, ?, ?) "
                   "ON CONFLICT(session) DO UPDATE SET lines = excluded.lines, offset = excluded.offset, "
                   "ended = excluded.ended", (session, digest, last, end_offset, int(finished)))
        return last - done, len(docs)

    def search(self, query: str, *, kind: str | None = None, tool: str | None = None, task_hash: str | None = None,
               limit: int = 20) -> list[dict]:
        """[{ref, session, task_hash, kind, tool, excerpt}] best first (FTS5 bm25), train sessions only. Every
        word of `query` must match (FTS5 tokens, quoted: no query syntax is passed through)."""
        words = re.findall(r"\w+", str(query or ""), flags=re.UNICODE)
        if not words:
            return []
        if kind is not None and kind not in INDEXED_KINDS:
            raise ValueError(f"kind {kind!r} is not one of {list(INDEXED_KINDS)}")
        self.index()
        expression = " ".join('"' + w.replace('"', '""') + '"' for w in words)
        sql = ("SELECT ref, session, task_hash, kind, tool, snippet(docs, 0, '[', ']', '…', 24) FROM docs "
               "WHERE docs MATCH ?")
        params: list = [expression]
        for column, value in (("kind", kind), ("tool", tool), ("task_hash", task_hash)):
            if value is not None:
                sql += f" AND {column} = ?"
                params.append(value)
        sql += " ORDER BY bm25(docs) LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        db = self._connect()
        try:
            rows = db.execute(sql, params).fetchall()
        finally:
            db.close()
        out = []
        for ref, session, digest, k, t, excerpt in rows:
            if self.split(session) != "train":         # the index holds train only; a changed split is refused
                continue
            out.append({"ref": ref, "session": session, "task_hash": digest, "kind": k, "tool": t,
                        "excerpt": excerpt})
        return out


SCHEMA_SQL = (
    "DROP TABLE IF EXISTS docs", "DROP TABLE IF EXISTS sessions", "DROP TABLE IF EXISTS results",
    "DROP TABLE IF EXISTS meta",
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE sessions (session TEXT PRIMARY KEY, task_hash TEXT, lines INTEGER NOT NULL, offset INTEGER NOT NULL, "
    "ended INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE results (session TEXT NOT NULL, call_id TEXT NOT NULL, PRIMARY KEY (session, call_id))",
    "CREATE VIRTUAL TABLE docs USING fts5(text, ref UNINDEXED, session UNINDEXED, task_hash UNINDEXED, "
    "kind UNINDEXED, tool UNINDEXED, tokenize = 'unicode61')",
)


def _wal(db: sqlite3.Connection, seconds: float = 30.0) -> None:
    """The index in WAL mode. A switch that races another connection's lock on a new index file is answered
    "database is locked" at once — SQLite does not wait on a lock it could deadlock on — so it is tried again until
    `seconds` pass (the connection's own timeout): two stores opening a new index at once both get it."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            db.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _require_fts5() -> None:
    try:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        finally:
            probe.close()
    except sqlite3.OperationalError as e:
        raise TraceIndexError(f"this Python's SQLite ({sqlite3.sqlite_version}) has no FTS5: trace search needs it "
                              f"and has no other index ({e})") from None


# ---------------------------------------------------------------------------- the rebuild
class _Rebuild:
    """One session's gateway_call chain and the trajectory it gives (see the module docstring)."""

    def __init__(self, session: str, rows: list[tuple[int, dict]]):
        self.session = session
        self.rows = rows
        self.calls = [(n, e) for n, e in rows if e.get("kind") == "gateway_call"]
        if not self.calls:
            raise TraceError(f"session {session} has no gateway_call records")
        self.full: list[list] = []
        previous: list = []
        for n, call in self.calls:
            prefix = int(call.get("messages_prefix") or 0)
            if prefix > len(previous):
                raise TraceError(f"{session}:L{n}: messages_prefix {prefix} is longer than the previous call's "
                                 f"{len(previous)} messages")
            messages = previous[:prefix] + list(call.get("messages_tail") or ())
            self.full.append(messages)
            previous = messages

    def ref(self, line: int) -> str:
        return f"{self.session}:L{line}"

    def _later_result(self, k: int, call_id: str):
        """The first call after k whose new messages carry this call id's result: (line, result)."""
        if not call_id:
            return None
        for j in range(k + 1, len(self.calls)):
            line, call = self.calls[j]
            for message in call.get("messages_tail") or ():
                if isinstance(message, dict) and message.get("role") == "tool":
                    for result in message.get("results") or ():
                        if isinstance(result, dict) and str(result.get("id") or "") == call_id:
                            return line, result
        return None

    def _segment(self, k: int) -> list[tuple[int, dict]]:
        """The lines between call k's gateway_call line and the next one: what its tool calls left on record."""
        start = self.calls[k][0]
        end = self.calls[k + 1][0] if k + 1 < len(self.calls) else float("inf")
        return [(n, e) for n, e in self.rows if start < n < end]

    def _event_results(self, k: int, tool_calls: list[dict]) -> dict:
        """call id -> (line, output, is_error, truncated, source) for the calls of reply k that no later call
        carries: the broker's `tool` line (its excerpt), a package tool's mark, a blocked or skipped call's mark.
        The tool calls are matched in order — a package tool's own base calls (the `tool` lines before its mark)
        and a hook's read-only calls are not the model's."""
        segment = self._segment(k)
        broker = {e.get("call_id"): e for _, e in segment if e.get("kind") == "broker_event"}
        package_tools = {e.get("tool") for _, e in segment if e.get("kind") == "mark"
                         and e.get("mark") == "package_tool"}
        out: dict = {}
        cursor = 0
        for call in tool_calls:
            call_id, name = str(call.get("id") or ""), call.get("name")
            mark = next(((i, n, e) for i, (n, e) in enumerate(segment)
                         if e.get("kind") == "mark" and e.get("mark") in ("tool_blocked", "tool_skipped")
                         and e.get("call_id") == call_id and call_id), None)
            if mark is not None:
                _, n, e = mark
                text = ("Stopped before execution: " + str(e.get("reason") or "")) if e["mark"] == "tool_blocked" \
                    else "Not executed: an earlier call in this turn was stopped"
                out[call_id] = (n, text, True, True, e["mark"])
                continue
            for i in range(cursor, len(segment)):
                n, e = segment[i]
                if name in package_tools:
                    if e.get("kind") == "mark" and e.get("mark") == "package_tool" and e.get("tool") == name:
                        output = str(e.get("output") or "")
                        out[call_id] = (n, output, bool(e.get("is_error")), len(output) >= EVENT_CHARS,
                                        "package_tool")
                        cursor = i + 1
                        break
                elif e.get("kind") == "tool" and e.get("name") == name:
                    b = broker.get(e.get("call_id"))
                    if e.get("read_only") or (b is not None and b.get("read_only")):
                        continue                   # a hook's read, not the model's call
                    output = str(e.get("output") or "")
                    full = b.get("output_chars") if b is not None else None
                    truncated = (full > len(output)) if isinstance(full, int) else len(output) >= EVENT_CHARS
                    out[call_id] = (n, output, bool(e.get("is_error")), truncated, "tool_event")
                    cursor = i + 1
                    break
        return out

    def _drop_bridge(self, new: list, summaries: list[int]) -> tuple[list, dict]:
        """An act call after aux summary calls whose history was replaced: its leading summary bridge — the user
        message that carries the last summary's text — and the acknowledgement after it (a user message whose text
        is one of ACKNOWLEDGEMENTS) are not the trajectory's; they are returned as the annotation. Neither is
        guessed at: a bridge that does not carry the summary drops nothing, and a user message after the bridge
        that is not the acknowledgement is kept; the annotation says which."""
        summary = str((self.calls[summaries[-1]][1].get("response") or {}).get("text") or "")
        note = {"aux_refs": [self.ref(self.calls[j][0]) for j in summaries],
                "aux_call_ids": [self.calls[j][1].get("call_id") for j in summaries], "dropped": []}
        if not (summary and new and new[0].get("role") == "user" and summary in _content(new[0])):
            note["bridge_not_found"] = True
            return new, {"after_summary": note}
        dropped = [new[0]]
        if len(new) > 1 and new[1].get("role") == "user" and _content(new[1]).strip() in ACKNOWLEDGEMENTS:
            dropped.append(new[1])                  # the acknowledgement (Stirrup's "Got it, thanks!")
        else:
            note["acknowledgement_not_found"] = True
        note["dropped"] = [{"role": m.get("role"), "chars": len(_content(m)),
                            "sha256": hashlib.sha256(_content(m).encode("utf-8")).hexdigest()[:16],
                            "excerpt": _content(m)[:200]} for m in dropped]
        return new[len(dropped):], {"after_summary": note}

    def messages(self) -> list[dict]:
        out: list[dict] = []
        previous: list | None = None          # the messages the previous act call sent
        for k, (line, call) in enumerate(self.calls):
            if call.get("role") != "act":
                continue
            sent = self.full[k]
            note = None
            if previous is None:
                i = 0
                if sent and sent[0].get("role") == "system":
                    out.append({"role": "system", "content": _content(sent[0]), "metadata": {"ref": self.ref(line)}})
                    i = 1
                if i < len(sent) and sent[i].get("role") == "user":
                    out.append({"role": "user", "content": _content(sent[i]), "metadata": {"ref": self.ref(line)}})
                    i += 1
                new = sent[i:]
            elif len(sent) > len(previous) and _roles(sent[:len(previous)]) == _roles(previous) and \
                    sent[len(previous)].get("role") == "assistant":
                # appended to: the previous reply's echo, its results, and what came after them (a feedback note,
                # a pushback, a turn warning). Earlier entries may have been edited in place: their full text is
                # already in the trajectory.
                new = sent[len(previous):]
            else:
                # the history was replaced
                new = sent[common_prefix(previous, sent):]
                summaries = []
                j = k - 1
                while j >= 0 and self.calls[j][1].get("role") == "aux":
                    summaries.insert(0, j)
                    j -= 1
                if summaries:
                    new, note = self._drop_bridge(new, summaries)
            for m in new:
                if m.get("role") in ("user", "system"):
                    out.append({"role": m["role"], "content": _content(m), "metadata": {"ref": self.ref(line)}})
            response = call.get("response") or {}
            tool_calls = [c for c in response.get("tool_calls") or () if isinstance(c, dict)]
            meta = {"ref": self.ref(line), "call_id": call.get("call_id"), "stop": response.get("stop")}
            if note:
                meta.update(note)
            reply = {"role": "assistant", "content": str(response.get("text") or ""), "metadata": meta}
            if tool_calls:
                reply["tool_calls"] = [to_openai_call(c) for c in tool_calls]
            out.append(reply)
            unresolved = []
            fallback = None
            for c in tool_calls:
                call_id = str(c.get("id") or "")
                found = self._later_result(k, call_id)
                if found is not None:
                    at, result = found
                    out.append({"role": "tool", "tool_call_id": call_id, "content": str(result.get("output") or ""),
                                "metadata": {"ref": self.ref(at), "tool": c.get("name"),
                                             "is_error": bool(result.get("is_error")), "source": "gateway_call",
                                             "truncated": False}})
                    continue
                if fallback is None:
                    fallback = self._event_results(k, tool_calls)
                if call_id in fallback:
                    at, output, is_error, truncated, source = fallback[call_id]
                    out.append({"role": "tool", "tool_call_id": call_id, "content": output,
                                "metadata": {"ref": self.ref(at), "tool": c.get("name"), "is_error": is_error,
                                             "source": source, "truncated": truncated}})
                else:
                    unresolved.append(call_id)      # never answered (a finish call, the attempt's end)
            if unresolved:
                reply["metadata"]["unanswered"] = unresolved
            previous = sent
        return out
