"""The transcript of one attempt (docs/design/CONTRACTS.md §6): one JSONL file per session, append-only, written by the
host. Package code cannot write it; what a package says about itself arrives as `mark` (and `hook`) lines labelled
`source: "package"`, never as evidence of the host's.

Every line is {"kind", "ts", "seq", ...}; `seq` is monotonic within the session, so `<session>:L<line>` is a stable
reference to a line (the improver's trace reads it that way).

    task          task_id, split, task_hash, attempt_id, release_ref, core_ref, package_ref, arm, text
    gateway_call  call_id, role, effort_requested, effort_sent, messages_prefix, messages_tail, tools_digest, reply
                  {text, tool_calls, stop}, usage, ms, attempt_id, model
    tool_specs    digest, specs (written when the call's tools change)
    tool          call_id, name, args, output (EVENT_CHARS), is_error, read_only, ms
    broker_event  call_id, name, mutates, refused, reason, output_digest, read_only
    skill_read    name, found, chars
    library_retrieval  query, shown, scores, site ("start" | "recall")
    hook          site, name, action, detail, source "package" (the adapter's mark of a hook that acted)
    mark          mark (the package's kind), data, source "package"
    egress        method, host, port, status, bytes, decision (the proxy's row)
    protocol      error, role, effort_requested (a model call the gateway refused before calling the provider)
    worker_session  seconds, requests, worker, egress
    end           termination ("finished" | "budget" | "harness_error" | "infrastructure"), limit, error,
                  final_text_digest, usage, cost, turns, stop
"""
from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path

from ..contracts import append_jsonl, now, read_jsonl, redact

KINDS = ("task", "gateway_call", "tool_specs", "tool", "broker_event", "skill_read", "library_retrieval", "hook",
         "mark", "egress", "protocol", "worker_session", "end")
TERMINATIONS = ("finished", "budget", "harness_error", "infrastructure")
EVENT_CHARS = 2000                     # of a tool output, in the `tool` line and the view's result excerpt
RESERVED_KEYS = ("kind", "ts", "seq", "source")        # the line's own fields: package data may not set them
SESSION_ID = re.compile(r"\w[\w.@:#+/-]{0,299}")


def digest16(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]


def session_file(root: Path, session: str) -> Path:
    """<workspace>/transcripts/<session>.jsonl. A session id is an attempt id; it may not name a path elsewhere."""
    if not isinstance(session, str) or not SESSION_ID.fullmatch(session) or ".." in session:
        raise ValueError(f"session id {session!r}: letters, digits and _ . @ : # + / -")
    return Path(root) / "transcripts" / (session.replace("/", "__") + ".jsonl")


class Transcript:
    """The writer of one session's file. `on_line(line)` sees every line as written (the runner's tallies)."""

    def __init__(self, path: Path, on_line=None):
        self.path = Path(path)
        self.on_line = on_line
        self.seq = sum(1 for _ in read_jsonl(self.path))     # a reopened session continues its numbering
        self._lock = threading.Lock()

    def write(self, kind: str, **fields) -> dict:
        if kind not in KINDS:
            raise ValueError(f"transcript kind {kind!r}: one of {list(KINDS)}")
        with self._lock:
            self.seq += 1
            line = redact({"kind": kind, "ts": now(), "seq": self.seq, **fields})
            append_jsonl(self.path, line)
        if self.on_line is not None:
            self.on_line(line)
        return line

    def __call__(self, kind: str, data: dict) -> dict:
        return self.write(kind, **data)


def read(path: Path) -> list[dict]:
    return list(read_jsonl(Path(path)))
