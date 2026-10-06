"""M4 §1 (v0.3): R as one bounded coding session per round (docs/modules/04_RESEARCH_LOOP.md §1 R 作为有界编码会话,
§2 train 侧筛选, §5 引擎自有的实验台账).

The summary call that answered with one JSON patch is replaced, under a core whose package API is
`harness_package.v2`, by a coding session: R works with file tools on a copy of the current package that lives in
the isolated worker (`/work`: `package/` read-only, `inputs/` read-only, `candidates/<dir>/` where it writes), reads
raw train trajectories through a byte-capped read-only tool, and ends with `finish` and a declaration — up to k
candidate directories, each with {mechanism, site, carrier, predicted_affected, trigger_condition, differs_from?,
notes}, or a next_action. Fixed code then does what fixed code did before: diffs each candidate against the package
and checks the declaration against the diff, runs the unchanged draft check chain (shape -> isolated probe and
interface smoke -> package tests) with bounded revisions that continue the same session, admits each candidate
(core checks, duplicate_experiment, repeated_mechanism), screens the admitted ones on their own failing train tasks
plus a fixed passing sample, and pays the dev gate for the best one only. Every stage writes the engine ledger.

What the session cannot do, by construction rather than instruction: run a shell, reach the network or the host's
files (the tree is the worker's), read anything of dev or final (the trajectory tool is split-filtered by the
results store; the only dev input is a histogram of failed-assertion types with counts), write the ledger or the
inputs (no tool writes outside `candidates/`), or raise its own caps (K's). Where the design is silent the choices
are these, and the docs now say so: the model loop runs on the host and the worker holds the files; the tool set
includes `delete_file` (removing a component is a candidate like any other); a candidate directory is created by
its first write and starts as the current package; W's prompt texts appear in a candidate as `worker/system_prompt.md`
and `worker/finish.md` so a `prompt` carrier is a file change like the others; `differs_from` cites a ledger row's
declaration ref with one sentence; `thin_screen` is a failing set (the notes' tasks ∪ predicted_affected, ∩ the
tasks that still fail in the latest train measurement) of fewer than 3 tasks, the passing sample not counted; ties
on net flips go to the lower mean cost, then to declaration order.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from ..agent import harness_package as hp
from ..chat import ToolCall, ToolSpec
from ..evolve import contracts as ct
from ..evolve import experience as xp
from .diagnosis import TRAIN, clip, experience_projection
from .experiment import NEXT_ACTIONS, normalize_mechanism

# ---------------------------------------------------------------------------- what K fixes (preset, M7 §2)
@dataclass(frozen=True)
class SessionLimits:
    """The session's caps, frozen into K from the preset (research_session.*); R cannot raise them. `max_turns`
    counts model calls, `finish` included; `max_edits` counts write_file/edit_file/delete_file across every candidate
    directory and every revision of the round; the trajectory bytes are per call and per session.

    v0.4 (docs/design/V0_4_PLAN.md §1.3): two floors admission checks against the host's read records —
    `min_failing_reads` failed trajectories among a candidate's predicted_affected tasks, `min_passing_reads` passing
    trajectories in the session. 0 is no check: a K frozen before them (v0.3) has neither key and keeps v0.3's
    admission, and to_json leaves a 0 out so its caps read as they did."""
    max_turns: int = 40
    max_edits: int = 24
    max_trajectory_bytes: int = 65536
    max_trajectory_bytes_total: int = 524288
    min_failing_reads: int = 0
    min_passing_reads: int = 0

    def to_json(self) -> dict:
        out = {"max_turns": self.max_turns, "max_edits": self.max_edits,
               "max_trajectory_bytes": self.max_trajectory_bytes,
               "max_trajectory_bytes_total": self.max_trajectory_bytes_total}
        out.update({k: v for k, v in (("min_failing_reads", self.min_failing_reads),
                                      ("min_passing_reads", self.min_passing_reads)) if v})
        return out

    @classmethod
    def from_json(cls, d: dict | None) -> "SessionLimits":
        return cls(**{k: int(v) for k, v in (d or {}).items()})


@dataclass(frozen=True)
class ScreenSettings:
    """The train-side screen's numbers (M4 §2 train 侧筛选): how many passing train tasks join every candidate's
    screen, and the least net flips that pay the dev gate.

    v0.4 (docs/design/V0_4_PLAN.md §1.6, M4 §2 v0.4): `max_sample_regressions` (a cap) and `min_fixes` (a floor). With
    the cap set, the screen is v0.4's — the passing sample redrawn every round with must-not tasks first
    (passing_sample_v4), and a candidate eligible only with at least `min_fixes` fixes and at most
    `max_sample_regressions` regressions on the sample — so the harm a broad change does on passing tasks is seen
    before the gate is paid. A K frozen before them has neither key: the fixed sample and net flips alone, as in v0.3."""
    passing_sample: int = 5
    min_net_flips: int = 1
    min_fixes: int = 0
    max_sample_regressions: int | None = None

    @property
    def v04(self) -> bool:
        return self.max_sample_regressions is not None

    def to_json(self) -> dict:
        out = {"passing_sample": self.passing_sample, "min_net_flips": self.min_net_flips}
        if self.min_fixes:
            out["min_fixes"] = self.min_fixes
        if self.max_sample_regressions is not None:
            out["max_sample_regressions"] = self.max_sample_regressions
        return out

    @classmethod
    def from_json(cls, d: dict | None) -> "ScreenSettings":
        return cls(**{k: int(v) for k, v in (d or {}).items()})


# ---------------------------------------------------------------------------- the declaration's vocabulary
CARRIERS = ("memory", "skill", "prompt", "hook", "tool", "agent")
AGENT_SITES = ("plan", "act", "context", "verify", "finish")
# W's prompt texts as the session sees them: files of a candidate directory outside the package's shape, read back
# into prompt changes (kind "prompt" on the target) by the fixed code. The design names the carrier and its sites and
# not the file; this is the file.
PROMPT_FILES = {"worker/system_prompt.md": ("system_prompt", "worker.system_prompt"),
                "worker/finish.md": ("finish", "worker.finish")}
MAX_MECHANISM_WORDS = 12
THIN_SCREEN_TASKS = 3                   # fewer note-derived failing tasks than this: `thin_screen`
PASSING_LISTED = 6                      # v0.4: passing train attempts inputs/trajectories.json lists at most
DECLARATION_FIELDS = ("dir", "mechanism", "site", "carrier", "predicted_affected", "trigger_condition", "notes")
NUDGE = ("Reply with tool calls: list_files, read_file, write_file, edit_file, delete_file, read_trajectory, or "
         "finish with your declaration. Text alone does nothing.")

TOOLS = (
    ToolSpec("list_files", "List the files under a directory of the work tree (default: the whole tree): package/ "
             "is the current package, inputs/ this round's inputs, candidates/<dir>/ your candidates.",
             {"type": "object", "properties": {"path": {"type": "string"}}}),
    ToolSpec("read_file", "Read one file of the work tree, at most 131072 characters a call: `offset` and `limit` "
             "are in characters; when more follows, the answer ends with how many and the offset to read next.",
             {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"},
                                               "limit": {"type": "integer"}}, "required": ["path"]}),
    ToolSpec("write_file", "Write a whole file under candidates/<dir>/...; the first write under a new <dir> creates "
             "that candidate as a copy of the current package. Counts as one edit.",
             {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
              "required": ["path", "text"]}),
    ToolSpec("edit_file", "Replace one exact occurrence of `old` with `new` in a file under candidates/<dir>/. "
             "Counts as one edit.",
             {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"},
                                               "new": {"type": "string"}}, "required": ["path", "old", "new"]}),
    ToolSpec("delete_file", "Delete a file under candidates/<dir>/ (remove a skill, a hook, a tool). Counts as one "
             "edit.", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
    ToolSpec("read_trajectory", "Read a byte range of the host-written record of one train attempt, failed or "
             "passing (every model call with its messages, tools, response and usage; every tool call the broker "
             "saw). Train attempts only; inputs/trajectories.json lists the refs, sizes and outcomes.",
             {"type": "object", "properties": {"attempt_ref": {"type": "string"}, "offset": {"type": "integer"},
                                               "limit": {"type": "integer"}}, "required": ["attempt_ref"]}),
    ToolSpec("finish", "End the session with your declaration: {\"candidates\": [{\"dir\", \"mechanism\", \"site\", "
             "\"carrier\", \"predicted_affected\", \"trigger_condition\", \"differs_from\"?, \"notes\"}], "
             "\"next_action\"?: \"none|stop|collect|repair_runtime\", \"reason\"?}. Without candidates, next_action "
             "is required.",
             {"type": "object", "properties": {"candidates": {"type": "array", "items": {"type": "object"}},
                                               "next_action": {"type": "string"}, "reason": {"type": "string"}}}),
)
EDIT_TOOLS = ("write_file", "edit_file", "delete_file")

SESSION_FORMAT = (
    "This call is one turn of a coding session over the current capability package. The tree: package/ holds the "
    "current package (read-only), inputs/ this round's inputs (read-only: notes.json, coverage.json, histogram.json "
    "— failed-assertion types with counts over the installed version's dev readings, nothing else of dev —, "
    "ledger.json — every earlier candidate's declaration, screen result and verdict —, tools.json — the host's base "
    "tools —, experience.json, trajectories.json — the train attempts you may read with read_trajectory, failed and "
    "passing, each with its outcome —, api_errors.json — per endpoint and tool over the train attempts: calls, "
    "error responses by kind, retries after an error, next pages left unread, empty results —, "
    "package_api.md). You make up to {k} candidates: the first write under candidates/<dir>/ creates <dir> as a copy "
    "of the package; change, add or delete its files; W's instruction texts are candidates/<dir>/worker/"
    "system_prompt.md and worker/finish.md, so a prompt change is a change to those files. Each candidate must change "
    "a different mechanism at a different site: a second candidate on the same (mechanism, site) is dropped. "
    "Limits: {max_turns} model calls including finish, {max_edits} edits in all (write, edit, delete, every "
    "candidate and every revision together), {max_trajectory_bytes} bytes per read_trajectory and "
    "{max_trajectory_bytes_total} in all; reaching a limit before finish ends the session and the round. "
    "read_file answers at most 131072 characters a call; read a longer file in pages with offset. "
    "A candidate is screened on the train tasks that currently fail among its notes' tasks and predicted_affected, "
    "plus a sample of passing train tasks: name what it should fix. "
    "End with finish and one declaration per candidates/<dir> (a dir declared twice in one finish is refused): "
    "mechanism (at most 12 words, what the change does), carrier "
    "(memory|skill|prompt|hook|tool|agent), site (hook: before_tool|after_tool|on_finish; tool: the tool's name; "
    "agent: plan|act|context|verify|finish; prompt: system_prompt|finish; memory or skill: the file's path), "
    "predicted_affected (the train task ids of the notes this candidate should fix), trigger_condition (when the "
    "change acts: always, or a condition), notes (the note refs you read), and differs_from "
    "([{{\"ref\": <a ledger row's declaration_ref>, \"how\": \"one sentence\"}}]) when the ledger shows the same "
    "mechanism at the same site rejected or screened out — without it that candidate is refused at admission. The "
    "carrier and site must match what the candidate's files change, or the candidate is invalid. A candidate that "
    "fails its draft check comes back to you here with the check output: edit and finish again. Checks are run by "
    "fixed code, not by you; there is no shell. Never write a task's specific answers (names, e-mails, ids, values) "
    "into a candidate. ")
# v0.4 (docs/design/V0_4_PLAN.md §1.3; M4 §1 v0.4): the admission rule a K with read floors adds to the protocol. The
# method (why read, which ones) is research.propose's text; this is only what admission checks.
SESSION_READS = (
    "Admission refuses a candidate unless this session read with read_trajectory (bytes returned) at least "
    "{min_failing_reads} failed attempt(s) of tasks in its predicted_affected and, when trajectories.json lists a "
    "passing attempt, at least {min_passing_reads} passing attempt(s); the refusal names what is missing. ")


def session_format(k: int, limits: "SessionLimits") -> str:
    """The session's protocol text for this round: SESSION_FORMAT with K's caps, and the admission's read rule when
    K sets read floors (SESSION_READS)."""
    caps = limits.to_json()
    text = SESSION_FORMAT.format(k=k, **caps)
    if limits.min_failing_reads or limits.min_passing_reads:
        text += SESSION_READS.format(min_failing_reads=limits.min_failing_reads,
                                     min_passing_reads=limits.min_passing_reads)
    return text


# ---------------------------------------------------------------------------- the worker-side tree, driven here
class WorkTree:
    """The host's handle on the session's `/work` in a worker session (worker_runner.py `work`). Every action goes
    over the protocol; a refused action is the tool's error, which the session hands to R."""

    def __init__(self, session):
        self.session = session

    def init(self, package: dict, inputs: dict, *, max_bytes: int = 262144, max_file_bytes: int | None = None) -> None:
        """Lay out the tree with K's byte caps: a candidate directory may hold at most `max_bytes` (the package's
        `max_package_bytes`) and a file at most `max_file_bytes` (the same cap), so a legal candidate always fits a
        protocol line and an oversized write is the tool's error, never a dead worker."""
        answer = self.session.work("init", package=dict(package), inputs=dict(inputs), max_bytes=int(max_bytes),
                                   max_file_bytes=int(max_file_bytes or max_bytes))
        if not answer.get("ok"):
            raise RuntimeError(f"the work tree could not be set up: {answer.get('error')}")

    def do(self, action: str, **fields) -> tuple[object, bool]:
        """-> (value or error text, is_error)."""
        answer = self.session.work(action, **fields)
        return (answer.get("value"), False) if answer.get("ok") else (str(answer.get("error")), True)

    def export(self, name: str) -> dict:
        value, bad = self.do("export", dir=name)
        if bad:
            raise KeyError(value)
        return dict(value)

    def fetch(self, name: str) -> tuple[dict | None, str]:
        """-> (one candidate directory's files, "") or (None, the worker's error): a directory the worker cannot hand
        back (it refused the export) ends that candidate, never the round."""
        value, bad = self.do("export", dir=name)
        return (None, str(value)[:300]) if bad or not isinstance(value, dict) else (dict(value), "")


# ---------------------------------------------------------------------------- raw train trajectories, read-only
class TrajectoryReader:
    """`read_trajectory` (M4 §1 R 的输入): a byte range of one train attempt's host-written transcript. The results
    store decides what an attempt ref names and which split it is in; anything but a valid train attempt is
    AccessDenied, recorded like every call. Per-call and per-session byte caps are K's."""

    def __init__(self, results, transcripts: Path, limits: SessionLimits):
        self.results, self.transcripts, self.limits = results, Path(transcripts), limits
        self.total = 0
        self.calls: list[dict] = []

    def listing(self, attempt_ids, passing=(), must_not=()) -> list[dict]:
        """What inputs/trajectories.json lists: the readable train attempts among `attempt_ids` (the round's failures,
        in the results' order) with their sizes, then (v0.4, docs/design/V0_4_PLAN.md §1.3) up to PASSING_LISTED of the
        `passing` attempts in the order given — the caller puts tasks with must-not checks first. Each entry says its
        outcome; a task in `must_not` is marked so."""
        out = []
        rows = list(self.results.all()) if self.results is not None else []

        def entry(r) -> dict:
            path = self.transcripts / f"{r.evidence}.jsonl"
            return {"attempt_ref": r.attempt_id, "task_id": r.task_id,
                    "bytes": path.stat().st_size if path.exists() else 0,
                    "outcome": "passing" if r.success else "failing",
                    **({"must_not": True} if r.task_id in set(must_not) else {})}

        def readable(r) -> bool:
            return r.split in TRAIN and r.validity == "valid" and bool(r.evidence)
        wanted = set(attempt_ids)
        for r in rows:
            if r.attempt_id in wanted and readable(r):
                out.append(entry(r))
        by_id = {r.attempt_id: r for r in rows}
        listed = 0
        for ref in passing:
            r = by_id.get(ref)
            if listed >= PASSING_LISTED or r is None or ref in wanted or not readable(r) or not r.success:
                continue
            out.append(entry(r))
            listed += 1
        return out

    def read_tasks(self) -> dict:
        """(v0.4, V0_4_PLAN §1.3) What the session actually read, from the host's record of its calls: the train
        attempts read with bytes returned and no refusal, as the task of each, split by the attempt's outcome —
        {"failing": [task, ...], "passing": [task, ...]}, one entry per attempt. Admission's read floors are checked
        against this, never against what R declares it read."""
        rows = {r.attempt_id: r for r in self.results.all()} if self.results is not None else {}
        seen, out = set(), {"failing": [], "passing": []}
        for call in self.calls:
            ref = call.get("ref")
            if call.get("denied") or not call.get("bytes") or ref in seen or ref not in rows:
                continue
            seen.add(ref)
            r = rows[ref]
            out["passing" if r.success else "failing"].append(r.task_id)
        return out

    def read(self, attempt_ref, offset=0, limit=None) -> tuple[str, bool]:
        """-> (text, is_error). The record's bytes [offset, offset+limit), decoded, limit capped per call; a read
        past the session's total is refused and says so; a ref that is not a valid train attempt is AccessDenied."""
        try:
            offset = max(int(offset or 0), 0)
            limit = self.limits.max_trajectory_bytes if limit is None else min(int(limit), self.limits.max_trajectory_bytes)
        except (TypeError, ValueError, OverflowError):
            # recorded like any read (0 bytes, the reason), so the session record's per-call bytes and its
            # trajectory_reads count this call too (M4 §1, 2026-10-05)
            self.calls.append({"ref": _safe(attempt_ref), "offset": None, "bytes": 0,
                               "denied": "offset and limit are integers"})
            return "offset and limit are integers", True
        limit = max(limit, 0)
        call = {"ref": attempt_ref, "offset": offset, "bytes": 0}
        self.calls.append(call)
        row = next((r for r in (self.results.all() if self.results is not None else ())
                    if r.attempt_id == attempt_ref), None)
        if row is None or row.split not in TRAIN or row.validity != "valid":
            call["denied"] = "not a valid train attempt"
            return f"AccessDenied: {attempt_ref!r} is not a valid train attempt of this activity", True
        if self.total + limit > self.limits.max_trajectory_bytes_total:
            call["denied"] = "session byte cap"
            return (f"refused: this read of {limit} bytes would pass the session's cumulative cap of "
                    f"{self.limits.max_trajectory_bytes_total} bytes ({self.total} read so far)"), True
        path = self.transcripts / f"{row.evidence}.jsonl"
        data = path.read_bytes() if path.exists() else b""
        chunk = data[offset:offset + limit]
        self.total += len(chunk)
        call["bytes"] = len(chunk)
        return (chunk.decode("utf-8", errors="replace")
                + ("" if offset + len(chunk) >= len(data) else f"\n[... {len(data) - offset - len(chunk)} bytes follow]")), False


# ---------------------------------------------------------------------------- the session
class SessionEnded(Exception):
    """The session reached a cap before `finish`: the round is invalid (RL6-29)."""


@dataclass
class SessionResult:
    declaration: dict | None = None
    unreadable: str = ""                 # why the session decided nothing, or ""

    @property
    def candidates(self) -> list[dict]:
        return list((self.declaration or {}).get("candidates") or ())

    @property
    def next_action(self) -> str | None:
        return (self.declaration or {}).get("next_action")


class CodingSession:
    """One round's session. `turn(system, messages, tools) -> ChatTurn` is the metered research call (the researcher's
    `session_turn`); `work` the worker-side tree; `trajectories` the read-only reader; `note_refs` the refs of this
    round's notes (a declaration cites only those). `start` sends the inputs and runs to the first accepted
    `finish`; `revise` appends a check output and runs to the next. Both count against the same caps."""

    def __init__(self, *, turn, work: WorkTree, limits: SessionLimits, k: int, system: str, inputs: dict,
                 trajectories: TrajectoryReader | None = None, note_refs=(), provider: str = "llm"):
        self.turn, self.work, self.limits, self.k = turn, work, limits, max(int(k), 1)
        # the name a reply's native content is kept under, as agent/loop.py keys it ({provider.name: content}): the
        # provider replays its own content on the next turn (providers/anthropic.to_anthropic reads it by name)
        self.provider = provider
        self.system, self.inputs, self.trajectories = system, inputs, trajectories
        self.note_refs = set(note_refs)
        self.turns = 0
        self.edits = 0
        self.messages: list[dict] = []
        self.calls: list[dict] = []            # every tool call, in order: the host's record of the session
        self.declarations: list[dict] = []     # every accepted finish, in order
        self.dirs: list[str] = []              # candidate directories, in the order they were first written
        self.ended = ""

    # ------------------------------------------------------------------ driving
    def start(self) -> SessionResult:
        self.messages = [{"role": "user", "content": json.dumps(self.inputs, ensure_ascii=False, default=str)}]
        return self._run()

    def revise(self, text: str) -> SessionResult:
        if self.ended:
            return SessionResult(unreadable=self.ended)
        self.messages.append({"role": "user", "content": text})
        return self._run()

    def _run(self) -> SessionResult:
        from ..providers import ReplyRefused
        while True:
            if self.turns >= self.limits.max_turns:
                return self._end(f"the session used its {self.limits.max_turns} model calls without finishing")
            self.turns += 1
            try:
                reply = self.turn(self.system, self.messages, list(TOOLS))
            except ReplyRefused as e:
                return self._end(f"the research model declined the session: {str(e)[:200]}")
            calls = _calls_of(reply)
            # A call whose name or arguments are not strict UTF-8 (a lone surrogate) is never performed: it is the
            # tool's error, and every record of it — the assistant message kept for the next turn, the session's
            # call row — holds a backslash-escaped copy, so no later write of the session fails to encode.
            unsafe = {i for i, c in enumerate(calls) if not _strict_utf8({"id": c.id, "name": c.name, "args": c.args})}
            kept = [ToolCall(_safe(c.id), _safe(c.name), _safe(c.args)) if i in unsafe else c
                    for i, c in enumerate(calls)]
            # The provider's own content is replayed as it came only when everything in it encodes: with a lone
            # surrogate anywhere, the message is rebuilt from the escaped text and calls instead.
            native = reply.native if reply.native is not None and not unsafe and _strict_utf8(reply.text or "") \
                else None
            self.messages.append({"role": "assistant", "text": _safe(reply.text or ""), "tool_calls": kept,
                                  **({"native": {self.provider: native}} if native is not None else {})})
            if not calls:
                self.messages.append({"role": "user", "content": NUDGE})
                continue
            results, finished = [], None
            for i, call in enumerate(calls):
                if i in unsafe:
                    results.append({"id": _safe(call.id), "name": _safe(call.name), "is_error": True,
                                    "output": "not performed: the call's name or arguments are not strict UTF-8 "
                                              "(a lone surrogate); write them as plain text"})
                    self.calls.append({"turn": self.turns, "tool": _safe(call.name), "ok": False,
                                       "error": "not strict UTF-8"})
                    continue
                if finished is not None:
                    # the reply finished the session before this call: not performed, and the accepted declaration
                    # stands whatever this call would have done (an edit past the cap included)
                    results.append({"id": call.id, "name": call.name, "output": "not performed: the session finished",
                                    "is_error": True})
                    self.calls.append({"turn": self.turns, "tool": call.name, "ok": False, "error": "not performed"})
                    continue
                if call.name == "finish":
                    problem = self._declaration(call.args)
                    if problem:
                        results.append({"id": call.id, "name": "finish", "output": problem, "is_error": True})
                        self.calls.append({"turn": self.turns, "tool": "finish", "ok": False, "error": problem})
                    else:
                        finished = self.declarations[-1]
                        results.append({"id": call.id, "name": "finish", "output": "declared", "is_error": False})
                        self.calls.append({"turn": self.turns, "tool": "finish", "ok": True,
                                           "candidates": [c["dir"] for c in finished.get("candidates") or ()]})
                    continue
                try:
                    output, bad = self._tool(call)
                except SessionEnded as e:
                    return self._end(str(e))
                results.append({"id": call.id, "name": call.name, "output": output, "is_error": bad})
            self.messages.append({"role": "tool", "results": results})
            if finished is not None:
                return SessionResult(declaration=finished)

    def _end(self, why: str) -> SessionResult:
        self.ended = why
        return SessionResult(unreadable=why)

    # ------------------------------------------------------------------ the tools
    def _tool(self, call: ToolCall) -> tuple[str, bool]:
        if call.args is not None and not isinstance(call.args, dict):
            # counted like any call (it was a turn's call), answered as the tool's error, nothing executed
            self.calls.append({"turn": self.turns, "tool": call.name, "ok": False, "error": "arguments not an object"})
            return f"{call.name}: the tool's arguments are a JSON object, not {type(call.args).__name__}", True
        args = dict(call.args or {})
        record = {"turn": self.turns, "tool": call.name, "path": args.get("path")}
        self.calls.append(record)
        if call.name in EDIT_TOOLS:
            if self.edits >= self.limits.max_edits:
                record.update(ok=False, error=f"the session's {self.limits.max_edits} edits are used up")
                raise SessionEnded(f"the session reached its {self.limits.max_edits} edits without finishing")
            self.edits += 1
            action = {"write_file": "write", "edit_file": "edit", "delete_file": "delete"}[call.name]
            fields = {"path": args.get("path")}
            if action == "write":
                fields["text"] = args.get("text")
            elif action == "edit":
                fields.update(old=args.get("old"), new=args.get("new"))
            value, bad = self.work.do(action, **fields)
            if not bad and isinstance(args.get("path"), str):
                name = args["path"].split("/")[1] if args["path"].count("/") >= 2 else None
                if name and name not in self.dirs:
                    self.dirs.append(name)
            out = json.dumps(value) if not isinstance(value, str) else value
            return out, self._done(record, out, bad)
        if call.name == "list_files":
            value, bad = self.work.do("list", path=args.get("path") or ".")
            out = json.dumps(value) if not bad else value
            return out, self._done(record, out, bad)
        if call.name == "read_file":
            value, bad = self.work.do("read", path=args.get("path"), offset=args.get("offset"),
                                      limit=args.get("limit"))
            return str(value), self._done(record, str(value), bad)
        if call.name == "read_trajectory":
            if self.trajectories is None:
                return self._refused(record, "AccessDenied: this session has no trajectory reader")
            text, bad = self.trajectories.read(args.get("attempt_ref"), args.get("offset", 0), args.get("limit"))
            # the bytes are the reader's count of record bytes handed over (not the decoded text's length)
            record.update(ref=args.get("attempt_ref"),
                          bytes=self.trajectories.calls[-1]["bytes"] if self.trajectories.calls else 0)
            return text, self._done(record, text, bad)
        return self._refused(record, f"unknown tool {call.name!r}")

    @staticmethod
    def _done(record: dict, output, bad: bool) -> bool:
        """A performed call's row: ok, and — when the tool refused it — the error text R was answered with
        (2026-10-05 review: a refused write's reason was R's only, and lost with the session)."""
        record["ok"] = not bad
        if bad:
            record["error"] = _cut(_safe(str(output)), ROW_ERROR_CHARS)
        return bad

    def _refused(self, record: dict, why: str) -> tuple[str, bool]:
        return why, self._done(record, why, True)

    def _declaration(self, args: dict) -> str:
        """Read `finish`'s arguments as candidate_declaration.v1, or say what is wrong with them. A readable
        declaration is appended to `declarations`; the shape problems are returned to R as the tool's error so it
        may finish again within its caps."""
        if args is not None and not isinstance(args, dict):
            return f"finish's arguments are a JSON object, not {type(args).__name__}"
        args = dict(args or {})
        candidates, action = args.get("candidates"), args.get("next_action")
        if candidates is None and action is None:
            return "finish takes candidates (a list) or a next_action"
        if candidates is not None and not isinstance(candidates, list):
            return "candidates is a list of candidate objects"
        if not candidates and action not in NEXT_ACTIONS:
            return f"without candidates, next_action must be one of {list(NEXT_ACTIONS)}"
        problems, out, dirs = [], [], set()
        for i, c in enumerate(candidates or ()):
            if not isinstance(c, dict):
                problems.append(f"candidates[{i}] is an object")
                continue
            if isinstance(c.get("dir"), str) and c["dir"] in dirs:
                problems.append(f"candidates[{i}].dir {c['dir']!r} is declared twice: one declaration per "
                                f"candidates/<dir>")
                continue
            dirs.add(c.get("dir")) if isinstance(c.get("dir"), str) else None
            before = len(problems)
            missing = [f for f in DECLARATION_FIELDS if c.get(f) in (None, "")]
            if missing:
                problems.append(f"candidates[{i}] lacks {missing}")
                continue
            blank = [f for f in ("mechanism", "site", "trigger_condition")
                     if not isinstance(c.get(f), str) or not c[f].strip()]
            if blank:
                problems.append(f"candidates[{i}]: {blank} must be non-blank text")
                continue
            if not isinstance(c["dir"], str) or c["dir"] not in self.dirs:
                problems.append(f"candidates[{i}].dir {c.get('dir')!r} is not a candidate directory you wrote "
                                f"({self.dirs or 'none yet'})")
            if len(str(c["mechanism"]).split()) > MAX_MECHANISM_WORDS:
                problems.append(f"candidates[{i}].mechanism has more than {MAX_MECHANISM_WORDS} words")
            if c["carrier"] not in CARRIERS:
                problems.append(f"candidates[{i}].carrier {c['carrier']!r} is not one of {list(CARRIERS)}")
            if not isinstance(c["predicted_affected"], list) or not all(isinstance(t, str) for t in c["predicted_affected"]):
                problems.append(f"candidates[{i}].predicted_affected is a list of train task ids")
            if not isinstance(c["notes"], list) or not all(isinstance(n, str) for n in c["notes"]):
                problems.append(f"candidates[{i}].notes is a list of note refs")
            elif self.note_refs and not set(c["notes"]) <= self.note_refs:
                problems.append(f"candidates[{i}].notes cites notes not of this round: "
                                f"{sorted(set(c['notes']) - self.note_refs)[:3]}")
            differs = c.get("differs_from")
            if differs is not None and (not isinstance(differs, list) or not all(
                    isinstance(d, dict) and isinstance(d.get("ref"), str) and isinstance(d.get("how"), str)
                    for d in differs)):
                problems.append(f"candidates[{i}].differs_from is a list of {{ref, how}}")
            if len(problems) > before:
                continue                  # a candidate with any problem is not built: the call is refused whole
            out.append({"dir": str(c["dir"]), "mechanism": str(c["mechanism"])[:200],
                        "mechanism_normalized": normalize_mechanism(c["mechanism"]), "site": str(c["site"])[:200],
                        "carrier": str(c["carrier"]), "predicted_affected": list(c["predicted_affected"]),
                        "trigger_condition": str(c["trigger_condition"])[:500],
                        "differs_from": [{"ref": d["ref"], "how": d["how"][:500]} for d in differs or ()],
                        "notes": list(c["notes"])})
        if problems:
            return "the declaration is not readable: " + "; ".join(problems)[:1500]
        ungrounded = self._ungrounded(out)
        if ungrounded:
            return ungrounded
        self.declarations.append({"candidates": out, "next_action": action if action in NEXT_ACTIONS else None,
                                  "reason": str(args.get("reason") or "")[:500]})
        return ""

    def _ungrounded(self, candidates: list) -> str:
        """(v0.4, docs/design/V0_4_PLAN.md §1.3; review 2026-10-06) The read floors checked at the first `finish`, so a
        candidate the session's reads do not ground is the finish call's error — R reads and finishes again within its
        caps — rather than a refusal at admission after the session is gone. Only the first finish: a revision changes
        files, never the declaration, and the reads only grow. Admission checks the same record as the backstop.
        "" when K sets no floor, there is no reader, or every candidate is grounded."""
        if self.declarations or self.trajectories is None or not candidates or \
                not (self.limits.min_failing_reads or self.limits.min_passing_reads):
            return ""
        from .experiment import grounding_reasons
        reads = {**self.trajectories.read_tasks(), "min_failing": self.limits.min_failing_reads,
                 "min_passing": self.limits.min_passing_reads,
                 "passing_listed": any(e.get("outcome") == "passing"
                                       for e in (self.inputs or {}).get("trajectories") or () if isinstance(e, dict))}
        missing = [f"candidates/{c['dir']}: {'; '.join(why)}" for c in candidates
                   for why in [grounding_reasons(c, reads)] if why]
        if not missing:
            return ""
        return ("finish refused, the declaration is not grounded in what this session read (read_trajectory, then "
                "finish again): " + " | ".join(missing))[:1500]

    def transcript(self) -> dict:
        """The whole session as R and the tools saw it (2026-10-05 review, stage 0's go rule reads it): every message
        in order — the inputs and any later user message, R's reply text with its tool calls and their arguments,
        each tool result with its output or error text — each string cut to TRANSCRIPT_ITEM_CHARS and every string
        strict-UTF-8 safe, so the host can store it whole as one object. The provider's native content is left out:
        it repeats the text and the calls."""
        items = []
        for m in self.messages:
            role = m.get("role")
            if role == "assistant":
                items.append({"role": "assistant", "text": _cut(_safe(m.get("text") or ""), TRANSCRIPT_ITEM_CHARS),
                              "tool_calls": [{"id": _safe(c.id), "name": _safe(c.name),
                                              "args": _cut_strings(_safe(c.args), TRANSCRIPT_ITEM_CHARS)}
                                             for c in m.get("tool_calls") or ()]})
            elif role == "tool":
                items.append({"role": "tool", "results": [
                    {"id": _safe(r.get("id")), "name": _safe(r.get("name")), "is_error": bool(r.get("is_error")),
                     "output": _cut(_safe(str(r.get("output", ""))), TRANSCRIPT_ITEM_CHARS)}
                    for r in m.get("results") or ()]})
            else:
                items.append({"role": _safe(str(role)),
                              "text": _cut(_safe(str(m.get("content", ""))), TRANSCRIPT_ITEM_CHARS)})
        return {"schema": "research_session_transcript.v1", "turns": self.turns, "ended": _safe(self.ended),
                "messages": items}

    def record(self) -> dict:
        """The host's record of the session (M4 §1: every tool call, the turns and edits, the trajectory reads)."""
        return {"turns": self.turns, "edits": self.edits, "calls": list(self.calls),
                "trajectory_bytes": self.trajectories.total if self.trajectories else 0,
                "trajectory_reads": len(self.trajectories.calls) if self.trajectories else 0,
                "dirs": list(self.dirs), "finishes": len(self.declarations), "ended": self.ended}


def _calls_of(reply) -> list[ToolCall]:
    """The tool calls of a reply: the provider's own, else one written as JSON in the text ({"tool", "args"} or
    {"tool_calls": [...]}) for a provider that answers in text only (the manual provider's hand-written replies).
    A provider whose replies carry their own content (`native`: the Anthropic API) has native tool use, so its
    text is never read as a call: the replayed content would hold no tool_use for the tool_result that followed,
    which the API refuses — such a reply gets the session's nudge instead."""
    # Every provider answers with ToolCall objects (anthropic, openai_compat, claude_code, manual); nothing else is
    # read as a structured call.
    calls = [c for c in getattr(reply, "tool_calls", None) or () if isinstance(c, ToolCall)]
    if calls or getattr(reply, "native", None) is not None:
        return calls
    from .basic import parse_json
    data = parse_json(getattr(reply, "text", "") or "")
    if isinstance(data.get("tool"), str):
        return [ToolCall("t1", data["tool"], _args(data))]
    written = data.get("tool_calls")
    if not isinstance(written, list):
        return []                     # {"tool_calls": 1} is no call: the session answers with its nudge
    return [ToolCall(str(c.get("id") or f"t{i + 1}"), str(c.get("name") or c.get("tool")), _args(c))
            for i, c in enumerate(written) if isinstance(c, dict)]


def _args(call: dict):
    """A written call's arguments as written: absent is {}, anything else goes to the tool unchanged — `_tool` and
    `_declaration` answer arguments that are not an object as the tool's error (a dict() here would raise on a
    string and end the round)."""
    args = call.get("args")
    return {} if args is None else args


def _strict_utf8(value) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


# How much of each item a session's stored transcript and a refused call's row keep (2026-10-05 review): enough to
# read what R wrote and what the tool answered, bounded so a 40-turn session with whole files in its arguments stays a
# record and not a copy of the package.
TRANSCRIPT_ITEM_CHARS = 8000
ROW_ERROR_CHARS = 500


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more characters]"


def _cut_strings(value, limit: int):
    if isinstance(value, str):
        return _cut(value, limit)
    if isinstance(value, dict):
        return {k: _cut_strings(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cut_strings(v, limit) for v in value]
    return value


def _safe(value):
    """`value` with every string's lone surrogates backslash-escaped (keys included): what a record may hold."""
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, dict):
        return {_safe(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


# ---------------------------------------------------------------------------- what a candidate directory changed
def split_candidate(files: dict) -> tuple[dict, dict]:
    """-> (package files, prompt texts by target): the worker/ files are W's prompt texts, everything else the
    package's."""
    package = {p: t for p, t in files.items() if p not in PROMPT_FILES}
    prompts = {PROMPT_FILES[p][1]: t for p, t in files.items() if p in PROMPT_FILES}
    return package, prompts


def diff_of(parent: dict, candidate: dict) -> dict:
    """The paths a candidate changed against the parent, with byte counts: added, modified, deleted."""
    added = sorted(p for p in candidate if p not in parent)
    deleted = sorted(p for p in parent if p not in candidate)
    modified = sorted(p for p in candidate if p in parent and candidate[p] != parent[p])
    return {"added": added, "modified": modified, "deleted": deleted,
            "bytes": sum(len(candidate[p].encode("utf-8")) for p in [*added, *modified])}


def patch_of_diff(parent: dict, candidate: dict) -> dict | None:
    """The package patch a candidate's package files amount to, or None when they are the parent's."""
    diff = diff_of(parent, candidate)
    write = {p: candidate[p] for p in [*diff["added"], *diff["modified"]]}
    if not write and not diff["deleted"]:
        return None
    return {k: v for k, v in (("write", write), ("delete", diff["deleted"])) if v}


def _role(path) -> str | None:
    try:
        return hp.classify(path)
    except (hp.PackageError, TypeError):
        return None


def _manifest(files: dict) -> dict:
    try:
        return hp.parse_manifest(files.get("manifest.json", json.dumps(hp.empty_manifest())), files)
    except Exception:  # noqa: BLE001 - a manifest the shape check will refuse; the declaration check reads what it can
        try:
            return json.loads(files.get("manifest.json") or "{}")
        except ValueError:
            return {}


def declaration_mismatch(declaration: dict, parent: dict, candidate: dict) -> str:
    """Why the declared carrier/site do not match what the candidate's files change (M4 §1: carrier=hook and no
    hooks file in the diff, carrier=agent and no agent entry — each is `declaration_mismatch`), or "". `parent` and
    `candidate` are the whole directories, prompt files included."""
    changed = {*diff_of(parent, candidate)["added"], *diff_of(parent, candidate)["modified"],
               *diff_of(parent, candidate)["deleted"]}
    if not changed:
        return "the candidate changes nothing"
    carrier, site = declaration["carrier"], declaration["site"]
    before, after = _manifest(parent), _manifest(candidate)

    def touched(prefix: str) -> bool:
        return any(p.startswith(prefix) for p in changed)
    if carrier == "memory":
        return "" if "memory.md" in changed and site == "memory.md" else \
            "carrier memory needs memory.md in the diff and site memory.md"
    if carrier == "skill":
        return "" if _role(site) == "skill" and site in changed else \
            "carrier skill needs the skill file at `site` in the diff"
    if carrier == "prompt":
        path = next((p for p, (s, _t) in PROMPT_FILES.items() if s == site), None)
        return "" if path and path in changed else \
            f"carrier prompt needs site in {sorted(s for s, _ in PROMPT_FILES.values())} and that worker/ file changed"
    if carrier == "hook":
        sites = {h.get("site") for h in (after.get("hooks") or []) + (before.get("hooks") or [])}
        if site not in hp.HOOK_SITES or site not in sites:
            return f"carrier hook needs site in {list(hp.HOOK_SITES)} with a hook at that site in the manifest"
        return "" if touched("middlewares/") or (after.get("hooks") != before.get("hooks")) else \
            "carrier hook needs a middlewares/ file or the manifest's hooks in the diff"
    if carrier == "tool":
        names = {t.get("name") for t in (after.get("tools") or []) + (before.get("tools") or [])}
        if site not in names:
            return "carrier tool needs site to be a package tool's name in the manifest"
        return "" if touched("tools/") or (after.get("tools") != before.get("tools")) else \
            "carrier tool needs a tools/ file or the manifest's tools in the diff"
    if carrier == "agent":
        if site not in AGENT_SITES:
            return f"carrier agent needs site in {list(AGENT_SITES)}"
        return "" if touched("agent/") or (after.get("agent") != before.get("agent")) else \
            "carrier agent needs an agent/ file or the manifest's agent entry in the diff"
    return f"unknown carrier {carrier!r}"


# ---------------------------------------------------------------------------- R's other inputs
def dev_failure_histogram(results, pool, *, split: str = "dev") -> dict:
    """The dev failure-type histogram (M4 §1 R 的输入): over the installed W's pooled dev readings (`pool`, the
    control pool's rows), the count of failed scored assertions by assertion `type`, with how many readings and
    tasks they come from. Types and counts only — no params, no task ids, no per-task rows; test never."""
    types: dict[str, int] = {}
    runs, tasks, attempts = set(), set(), 0
    for r in pool or ():
        if r.split != split or r.validity != "valid":
            continue
        attempts += 1
        runs.add(r.run)
        tasks.add(r.task_id)
        for a in results.grading(r.attempt_id, splits=(split,)) if results is not None else ():
            if a.get("excluded") or a.get("passed"):
                continue
            key = str(a.get("type") or "unknown")
            types[key] = types.get(key, 0) + 1
    return {"split": split, "types": dict(sorted(types.items())), "readings": len(runs), "tasks": len(tasks),
            "attempts": attempts}


def passing_sample(rows, *, order, size: int, seed: int) -> list[str]:
    """The round's fixed passing sample (M4 §2): the train tasks the latest measurement passed, in the frozen split
    order, drawn with the plan's order_seed; the same list for every candidate of the round."""
    passed = [t for t in order if any(r.task_id == t and r.validity == "valid" and r.success for r in rows)]
    rng = random.Random(seed)
    rng.shuffle(passed)
    return sorted(passed[:max(int(size), 0)], key=lambda t: order.index(t) if t in order else len(order))


def passing_sample_v4(rows, *, order, size: int, seed: int, round_ordinal: int, must_not=()) -> list[str]:
    """The round's passing sample under the v0.4 screen (docs/design/V0_4_PLAN.md §1.6, M4 §2 v0.4): `size` train tasks
    the latest measurement passed, drawn afresh every round — the generator is seeded by the plan's order_seed and the
    scope's diagnosis ordinal, so a rerun of the round draws the same tasks and the next round other ones — taking the
    tasks with must-not checks (`must_not`) first, then the rest; listed in the frozen split order. v0.3 drew one
    sample per activity, the same five tasks in all seven screens, and none held a must-not check of HR, where the
    candidates' harm was."""
    passed = [t for t in order if any(r.task_id == t and r.validity == "valid" and r.success for r in rows)]
    rng = random.Random(f"passing-sample:{int(seed)}:{int(round_ordinal)}")
    first = [t for t in passed if t in set(must_not)]
    rest = [t for t in passed if t not in set(must_not)]
    rng.shuffle(first)
    rng.shuffle(rest)
    chosen = (first + rest)[:max(int(size), 0)]
    return sorted(chosen, key=lambda t: order.index(t) if t in order else len(order))


def screen_counts(entry: dict) -> tuple[int, int]:
    """-> (fixes, sample regressions) of one screen (V0_4_PLAN §1.6): failing-set tasks that turned to pass, and sample
    tasks that turned to fail. The failing set only fails before and the sample only passes, so net flips = fixes −
    sample regressions."""
    before, after = entry.get("before") or {}, entry.get("after") or {}
    fixes = sum(1 for t in entry.get("failing") or () if after.get(t) is True and not before.get(t))
    regressions = sum(1 for t in entry.get("sample") or () if before.get(t) and after.get(t) is False)
    return fixes, regressions


def eligible(entry: dict, screen: ScreenSettings) -> bool:
    """Whether a screened candidate may pay the dev gate: a reading, net flips at or above `min_net_flips`, and under
    the v0.4 screen at least `min_fixes` fixes and at most `max_sample_regressions` regressions on the passing sample."""
    if entry.get("net_flips") is None or entry["net_flips"] < screen.min_net_flips:
        return False
    fixes, regressions = (entry["fixes"], entry["sample_regressions"]) if "fixes" in entry else screen_counts(entry)
    return fixes >= screen.min_fixes and (screen.max_sample_regressions is None
                                          or regressions <= screen.max_sample_regressions)


def net_flips(before: dict, after: dict) -> int:
    """Transitions to pass minus transitions to fail over the tasks read on both sides."""
    shared = set(before) & set(after)
    return sum(1 for t in shared if after[t] and not before[t]) - sum(1 for t in shared if before[t] and not after[t])


def pick_best(screened: list[dict], min_net_flips: int | ScreenSettings) -> dict | None:
    """The one candidate that pays the dev gate: among the eligible (`eligible`: net flips at or above min_net_flips,
    and under the v0.4 screen ≥ min_fixes fixes and ≤ max_sample_regressions sample regressions) the highest net
    flips, ties to the lower mean cost (an unknown cost sorts after a known one), then to declaration order. A bare
    number is the v0.3 call: net flips alone."""
    screen = min_net_flips if isinstance(min_net_flips, ScreenSettings) else ScreenSettings(min_net_flips=min_net_flips)
    ok = [s for s in screened if eligible(s, screen)]
    if not ok:
        return None
    return sorted(ok, key=lambda s: (-s["net_flips"], s.get("cost_per_attempt") is None,
                                     s.get("cost_per_attempt") or 0.0, s["order"]))[0]


def check_output_text(dir_name: str, errors: list[str]) -> str:
    return (f"candidates/{dir_name} failed its draft check:\n" + clip("\n".join(errors), 2000)
            + "\nEdit the candidate's files and call finish again with its declaration.")


def gate_ledger_fields(outcome: str, receipt_ref: str | None, objects: ct.Objects) -> dict:
    """The `gated` ledger row's fields from a finished experiment (M4 §5): the verdict (the receipt's where there is
    one, else the execution's terminal state), the receipt ref and reading kind, the gain, the pooled readings, the
    cost per attempt, the three per-task classes read off the receipt, and whether it installed. One helper for the
    round and for a gate finished on resume, so the ledger never lacks a verdict that was paid for."""
    from ..evolve.task_gate import receipt_differences
    gate = {"verdict": outcome, "receipt": receipt_ref, "installed": outcome == "installed"}
    if receipt_ref and objects.has(receipt_ref):
        receipt = objects.load(receipt_ref, ct.EvaluationReceipt)
        gate.update(verdict=receipt.verdict, reading_kind=receipt.reading_kind, gain=receipt.metrics.get("gain"),
                    control_readings=receipt.metrics.get("control_readings"),
                    cost_per_attempt=receipt.metrics.get("cost_per_attempt"), **receipt_differences(receipt, objects))
    return gate


def ledger_candidate_of(entries, proposal_ref: str, experiment_id: str | None = None) -> dict | None:
    """The folded ledger entry a resumed experiment belongs to, or None: the one whose screen row names
    `experiment_id` (fixed at the screen), else — for an older log whose entries carry no experiment id — the latest
    ungated entry whose admitted proposal is `proposal_ref`. Proposal refs are content-addressed and repeat across
    rounds (the same R output over cached notes), so a first match on them alone would put the gated row on an
    earlier candidate that never reached a gate (M4 §5, 2026-10-05)."""
    rows = xp.ledger_rows(entries)
    if experiment_id:
        hit = next((e for e in rows if e.get("experiment_id") == experiment_id), None)
        if hit is not None:
            return hit
    older = [e for e in rows if not e.get("experiment_id") and not e.get("gate")
             and e.get("proposal_ref") == proposal_ref]
    return older[-1] if older else None


LOST_TO = ("infrastructure", "evaluator")     # failure classes that are not the candidate's: the screen is lost


def _read_screen(entry: dict, rows_after, before: dict) -> bool:
    """One screen's rows read into its entry (M4 §2, 2026-10-05): net flips only when the failing set has a valid
    after-reading; else the reason — a candidate defect or an attempt invalid for another reason of the candidate's
    is `invalid`, attempts lost (refill included) only to infrastructure or the evaluator are `lost` with the
    failure classes in `lost_to`. -> whether an attempt ran out of budget (the round then ends budget_exhausted)."""
    entry["attempts"] = [r.attempt_id for r in rows_after]
    invalid = [r for r in rows_after if r.validity != "valid"]
    valid = [r for r in rows_after if r.validity == "valid"]
    defects = [r for r in invalid if r.failure_class == "candidate_defect"]
    exhausted = [r for r in invalid if r.failure_class == "budget_exhausted"]
    read_on_failing = [r for r in valid if r.task_id in set(entry["failing"])]
    if defects:
        entry["reason"] = f"candidate defect on {defects[0].task_id}: {defects[0].invalid_reason}"[:300]
    elif exhausted:
        entry["reason"] = f"budget_exhausted after {len(exhausted)} screen attempts could not run"
        return True
    elif read_on_failing:
        entry["after"] = {r.task_id: bool(r.success) for r in valid}
        entry["before"] = {t: bool(before[t]) for t in entry["tasks"] if t in before}
        entry["net_flips"] = net_flips(entry["before"], entry["after"])
        entry["fixes"], entry["sample_regressions"] = screen_counts(entry)
        entry["cost_per_attempt"] = _mean_cost(rows_after)
    else:
        lost = [r for r in invalid if r.task_id in set(entry["failing"])]
        entry["lost"] = bool(lost) and all(r.failure_class in LOST_TO for r in lost)
        if entry["lost"]:
            # only a lost screen names what it was lost to: the failing set's attempts, refills included
            entry["lost_to"] = sorted({str(r.failure_class) for r in lost})
        entry["reason"] = (f"no valid screen reading on the failing set {entry['failing']}: "
                           f"{len(rows_after)} attempts, {len(valid)} valid (the sample alone is no reading)"
                           + (f"; lost to {entry['lost_to']}" if entry.get("lost_to") else ""))[:300]
    return False


def _mean_cost(rows) -> float | None:
    valid = [r for r in rows if r.validity == "valid"]
    if not valid or any(r.cost_usd is None for r in valid):
        return None
    return round(sum(r.cost_usd for r in valid) / len(valid), 6)


def run_session_round(ctx, budget) -> ct.RoundResult:
    """One worker round as R's coding session (M4 §1, §2, §5, v0.3; the replacement block of M4 §4 一轮执行).

    notes -> the session over the worker's /work with this round's inputs -> finish -> for each declared candidate:
    the diff against the current package, the declaration against the diff, duplicates on (mechanism, site)
    dropped -> the draft chain (shape, isolated probe and smoke, tests; revisions continue the session) -> the
    proposal with its declaration ref -> admission (core, duplicate_experiment, repeated_mechanism, train-only
    predicted_affected) -> the train-side screen of the admitted -> the best one pays the dev gate through M5 -> the
    gate's per-task classes into the ledger. The ledger is written here at every stage, by this fixed code only.

    Unreadable (no finish within the caps, a refused session, a declaration the session never accepted, candidates
    without a readable next_action): outcome=invalid, the round counts. A cut reply pauses (ReplyTruncated), a spent
    budget ends (BudgetExhausted), a worker that is not there or dies is infrastructure (repair_runtime) — a session
    is not resumed, the round runs again after the repair. Every candidate's screen and the one gate are recorded:
    the screened candidates other than the one that pays the gate end `not_selected` (cleared min_net_flips),
    `screened_out` (below it), `invalid` (no reading of their own making) or `lost` (their attempts lost to
    infrastructure or the evaluator) in the ledger, each with its result. A round in which no candidate read and
    some were lost ends `inconclusive` for infrastructure, as a gate's integrity loss does: it is run again.
    """
    from ..agent.worker import WorkerCrashed, WorkerUnavailable
    from ..evolve import experience as _experience
    from ..evolve.execution import BudgetExhausted
    from ..ledger import new_id
    from ..policies import REGISTRY
    from ..providers import ReplyTruncated
    from .basic import PACKAGE_API
    from .experiment import ProposalResult, normalize, retrieve
    from .loop import _admit, _execute_admitted, _record_research_steps
    inputs: SessionInputs = ctx.session
    root = ctx.root or ctx.releases.root
    before = ctx.releases.active()["release_ref"]
    core = ctx.objects.load(ctx.core_ref, ct.CoreManifest)
    release = ctx.objects.load(before, ct.SystemRelease)
    events = ct.Events(root)
    objects = ctx.objects
    propose_prompt = ctx.bound.prompts.get("research.propose", "")
    sent_before = len(getattr(ctx.researcher, "requests", ()) or ())
    origin = getattr(ctx.researcher, "origin", None) or "llm"
    # The scope's diagnosis ordinal (Diagnosis.round_index, diagnosis_count over the scope): unique across the
    # segments and reruns of one scope, where the per-segment round index repeats, so a candidate id built on it
    # never folds two rounds' candidates into one ledger entry (M4 §5).
    ordinal = int(getattr(ctx.diagnosis, "round_index", 0) or 0)
    tag = {"scope": ctx.scope_id, "release": before, "round": ctx.round_index, "ordinal": ordinal}
    # k is the frozen schedule's candidates_per_round (<= the preset's max_candidates_per_round, bench._beyond_preset)
    k = max(int(getattr(ctx.schedule, "candidates_per_round", 0) or inputs.k), 1)

    def ended(outcome: str, reason: str, next_action: str | None = None, **more) -> ct.RoundResult:
        _experience.append(root, {"kind": "round", "outcome": outcome, "reason": reason[:300], **tag,
                                  **({"next_action": next_action} if next_action else {}), **more})
        return ct.RoundResult(outcome=outcome, release_before=before, release_after=before, reason=reason[:300],
                              next_action=next_action)

    def ledger(stage: str, candidate: str, **fields) -> None:
        _experience.append(root, xp.ledger_row(stage, candidate, **tag, research_ref=release.research_ref, **fields))

    # ------------------------------------------------------------------ the notes (unchanged)
    try:
        notes, coverage = ctx.diagnosis.run(ctx.researcher, ctx.bound.prompts.get("research.diagnose", ""))
    except ReplyTruncated as e:
        return ended("infrastructure_failed", str(e), "repair_runtime")
    except BudgetExhausted as e:
        return ended("budget_exhausted", str(e), "stop")
    _experience.append(root, {**coverage, "kind": "diagnosis", "scope": ctx.scope_id, "release": before})
    if coverage["invalid"] and not notes:
        problems = sorted({i["problem"] for i in coverage["invalid"]})
        return ended("invalid", f"no readable note: all {len(coverage['invalid'])} note replies were unreadable "
                                f"({'; '.join(problems)}); the session was not opened", "none")
    if not any(ok is False for ok in inputs.before.values()):
        # M4 §2: a candidate is screened on train tasks that fail now; with none failing, no candidate could have a
        # failing set and every one would end invalid after a paid session — more train evidence is asked for
        # instead, through the collection path (bounded by collection_batches, not idle).
        return ended("needs_collection", "no train task failed in the latest measurement: no candidate could have a "
                                         "failing set to screen on (M4 §2); the session was not opened", "collect")
    for _ in notes:
        ctx.bound.activity.call("research.diagnose")
    retrieved = retrieve(ctx.experience, ctx.bound.retrieval, targets=tuple(REGISTRY))
    ctx.bound.activity.call("research.retrieve")
    ctx.bound.activity.call("research.propose")

    # ------------------------------------------------------------------ the session's inputs (M4 §1 R 的输入)
    rows = xp.ledger_rows(ctx.experience)
    note_tasks = {n["note_ref"]: n["task_id"] for n in notes if n.get("note_ref")}
    attempt_refs = [s.get("attempt_id") for s in ctx.diagnosis.summaries if s.get("task_id") in coverage["included"]]
    trajectories = inputs.trajectories.listing(attempt_refs, passing=inputs.passing_attempts,
                                               must_not=inputs.must_not) if inputs.trajectories is not None else []
    budget_seen = budget.for_research() if budget is not None and hasattr(budget, "for_research") else \
        getattr(ctx.context, "budget", None)
    package_dir = {**inputs.package_files, **{path: inputs.prompt_texts[target] for path, (_s, target) in
                                              PROMPT_FILES.items() if target in inputs.prompt_texts}}
    state = {"goal": ctx.context.goal, "k": k, "limits": inputs.limits.to_json(), "coverage": coverage,
             "notes": notes, "allowed_components": list(ctx.diagnosis.components),
             "package_files": sorted(package_dir), "tools": list(inputs.base_tools),
             "dev_failure_histogram": inputs.histogram, "ledger": xp.ledger_projection(rows, ctx.objects),
             "experience": experience_projection(list(retrieved)[:10]), "trajectories": trajectories,
             "api_errors": inputs.api_errors, "budget": budget_seen}
    inputs_files = {"notes.json": json.dumps(notes, ensure_ascii=False, indent=1, default=str),
                    "coverage.json": json.dumps(coverage, ensure_ascii=False, indent=1, default=str),
                    "histogram.json": json.dumps(inputs.histogram, ensure_ascii=False, indent=1),
                    "ledger.json": json.dumps(state["ledger"], ensure_ascii=False, indent=1, default=str),
                    "tools.json": json.dumps(state["tools"], ensure_ascii=False, indent=1, default=str),
                    "experience.json": json.dumps(state["experience"], ensure_ascii=False, indent=1, default=str),
                    "trajectories.json": json.dumps(trajectories, ensure_ascii=False, indent=1),
                    # v0.4 (V0_4_PLAN §1.2): where the agent fights the API, from the train trajectories only
                    "api_errors.json": json.dumps(inputs.api_errors, ensure_ascii=False, indent=1),
                    "package_api.md": PACKAGE_API}
    system = (f"{propose_prompt}\n\n" if propose_prompt else "") + session_format(k, inputs.limits) + PACKAGE_API

    # ------------------------------------------------------------------ the session in the worker
    worker = inputs.worker
    session = wsession = None
    proposals_made: list = []
    screened: list = []                   # every candidate's screen, recorded as it is paid (flushed in `finally`)
    best = None

    def screened_row(s: dict) -> None:
        if s.get("recorded"):
            return
        s["recorded"] = True
        ledger("screened", s["candidate"], **{key: s.get(key) for key in (
            "tasks", "failing", "sample", "before", "after", "net_flips", "cost_per_attempt", "thin_screen", "outcome",
            "reason", "lost_to", "experiment_id", "proposal_ref")},
               **{key: s[key] for key in ("fixes", "sample_regressions") if key in s})
    try:
        try:
            if worker is None or not hp.usable_worker(worker):
                raise WorkerUnavailable("R's coding session needs the isolated worker and none is available")
            wsession = worker.start()
            wsession.load({})
            tree = WorkTree(wsession)
            tree.init(package_dir, inputs_files, max_bytes=inputs.max_package_bytes)
            session = CodingSession(turn=ctx.researcher.session_turn, work=tree, limits=inputs.limits, k=k,
                                    system=system, inputs=state, trajectories=inputs.trajectories,
                                    note_refs=list(note_tasks),
                                    provider=getattr(getattr(ctx.researcher, "llm", None), "name", None) or "llm")
            result = session.start()
            if result.unreadable:
                return ended("invalid", f"the coding session decided nothing: {result.unreadable}", "none")
            if not result.candidates:
                action = result.next_action or "none"
                collect = action == "collect"
                outcome = "needs_collection" if collect else "no_proposal"
                reason = result.declaration.get("reason") or action
                return ended(outcome, reason, action, hypothesis="")
            # ------------------------------------------------------------ the declarations against the diffs
            kept, seen, why = [], {}, {}
            for i, decl in enumerate(result.candidates):
                cid = f"{ctx.scope_id}/d{ordinal}/{decl['dir']}"
                files, error = tree.fetch(decl["dir"])
                if files is None:
                    why[decl["dir"]] = f"the candidate directory could not be read back: {error}"
                    ledger("declared", cid, dir=decl["dir"], declaration=decl, status="invalid",
                           reason=why[decl["dir"]][:300])
                    continue
                diff = diff_of(package_dir, files)
                declaration_ref = objects.put_json("declaration", {k: decl[k] for k in (
                    "mechanism", "mechanism_normalized", "site", "carrier", "predicted_affected", "trigger_condition",
                    "differs_from", "notes")})
                base = {"dir": decl["dir"], "declaration": decl, "declaration_ref": declaration_ref, "diff": diff,
                        "carrier": decl["carrier"]}
                if i >= k:
                    why[decl["dir"]] = f"beyond the round's {k} candidates"
                    ledger("declared", cid, status="dropped", reason=why[decl["dir"]], **base)
                    continue
                mismatch = declaration_mismatch(decl, package_dir, files)
                if mismatch:
                    why[decl["dir"]] = mismatch
                    ledger("declared", cid, status="declaration_mismatch", reason=mismatch, **base)
                    continue
                key = (decl["mechanism_normalized"], decl["site"])
                if key in seen:
                    why[decl["dir"]] = f"the same (mechanism, site) as candidate {seen[key]}"
                    ledger("declared", cid, status="dropped", reason=why[decl["dir"]], **base)
                    continue
                seen[key] = cid
                ledger("declared", cid, status="declared", **base)
                kept.append({"id": cid, "decl": decl, "files": files, "declaration_ref": declaration_ref,
                             "order": len(kept)})
            if not kept:
                return ended("invalid", "every declared candidate was invalid or dropped: "
                             + "; ".join(f"{d}: {w}" for d, w in why.items())[:280], "none")
            # ------------------------------------------------------------ the draft chain per candidate
            admitted: list = []
            parent_pkg, parent_prompts = split_candidate(package_dir)
            round_rows = xp.ledger_rows(ctx.experience)       # earlier rounds: what repeated_mechanism compares with
            for cand in kept:
                decl, cid = cand["decl"], cand["id"]
                package_files, prompts = split_candidate(cand["files"])
                patch = patch_of_diff(parent_pkg, package_files)
                draft_id = new_id("d")
                events.append("draft_started", scope=ctx.scope_id, draft_id=draft_id, round=ctx.round_index,
                              candidate=cid, session=True)
                drafts, package_ref, status, reason = [], None, "ready", ""
                revision = 0
                while patch is not None:
                    draft_ref = objects.put_json("bundle", {"files": {
                        p: objects.put_json("file", {"text": package_files[p]}) for p in sorted(package_files)}})
                    errors, package_ref = ctx.drafts.check(package_files)
                    events.append("draft_check", scope=ctx.scope_id, draft_id=draft_id, round=ctx.round_index,
                                  revision=revision, draft=draft_ref, package=package_ref,
                                  patch=objects.put_json("change", {"patch": patch}), ok=not errors,
                                  infrastructure=False, errors=[e[:300] for e in errors], session=True, candidate=cid)
                    drafts.append({"revision": revision, "draft_ref": draft_ref, "package_ref": package_ref,
                                   "ok": not errors, "errors": errors})
                    if not errors:
                        break
                    if revision >= ctx.drafts.max_repairs:
                        status, reason = "invalid", (f"the draft still failed its check after {ctx.drafts.max_repairs} "
                                                     f"revisions: {errors[:2]}")
                        break
                    revision += 1
                    revised = session.revise(check_output_text(decl["dir"], errors))
                    if revised.unreadable:
                        status, reason = "invalid", f"the revision was not made: {revised.unreadable}"
                        break
                    redeclared = next((c for c in revised.candidates if c["dir"] == decl["dir"]), None)
                    if redeclared is not None and (redeclared["mechanism_normalized"], redeclared["site"],
                                                   redeclared["carrier"]) != (decl["mechanism_normalized"],
                                                                              decl["site"], decl["carrier"]):
                        # a revision changes files, never the declaration: the ledger row, the declaration object,
                        # the proposal, the admission and the screen all read the one declared at the first finish
                        status, reason = "invalid", (f"the revision redeclared candidates/{decl['dir']} as "
                                                     f"({redeclared['mechanism']!r}, {redeclared['site']!r}, "
                                                     f"{redeclared['carrier']!r}); a revision changes files, not "
                                                     f"the declaration")
                        break
                    revised_files, error = tree.fetch(decl["dir"])
                    if revised_files is None:
                        status, reason = "invalid", f"the revised candidate could not be read back: {error}"
                        break
                    cand["files"] = revised_files
                    mismatch = declaration_mismatch(decl, package_dir, cand["files"])
                    if mismatch:
                        status, reason = "invalid", f"declaration_mismatch after the revision: {mismatch}"
                        break
                    package_files, prompts = split_candidate(cand["files"])
                    patch = patch_of_diff(parent_pkg, package_files)
                    if patch is None:
                        package_ref = None
                        break
                events.append("draft_finished", scope=ctx.scope_id, draft_id=draft_id, status=status,
                              package=package_ref, session=True, candidate=cid)
                ledger("drafted", cid, drafts=[{k: d[k] for k in ("revision", "package_ref", "ok")} for d in drafts],
                       package_ref=package_ref, status=status, reason=reason)
                if status != "ready":
                    continue
                changes = []
                if package_ref is not None:
                    changes.append({"target": "worker.harness", "kind": "workflow",
                                    "change": {"builtin": hp.load_package(objects, package_ref).api_version,
                                               "package_ref": package_ref}})
                changes += [{"target": t, "kind": "prompt", "change": {"text": text}}
                            for t, text in prompts.items() if text != parent_prompts.get(t)]
                if not changes:
                    ledger("admitted", cid, status="invalid", reasons=["the candidate changes nothing after its revisions"])
                    continue
                proposal = ct.ExperimentProposal(
                    owner=ctx.owner, hypothesis=f"{decl['mechanism']} ({decl['carrier']} at {decl['site']})"[:2000],
                    changes=tuple(changes), expected=f"fixes {decl['predicted_affected']}"[:1000],
                    abandon_if=decl["trigger_condition"][:500] or None, origin=origin,
                    declaration_ref=cand["declaration_ref"], notes="; ".join(decl["notes"])[:500])
                refused = ctx.drafts.check_texts(proposal)
                if refused:
                    ledger("admitted", cid, status="invalid", reasons=refused)
                    continue
                proposal = normalize(proposal, objects)
                proposals_made.append(proposal)
                admission = _admit(ctx, proposal, core, budget, declaration=decl, ledger=round_rows,
                                   train_tasks=inputs.train_tasks, reads=grounding_inputs(inputs, trajectories))
                ledger("admitted", cid, status=admission.status, reasons=admission.reasons,
                       proposal_ref=admission.proposal_ref)
                if admission.ok:
                    # the id its Experiment gets if the screen chooses it: fixed here so the screen's build and
                    # the gate's are one W (bench.screen_build)
                    admitted.append({**cand, "decl": decl, "proposal": proposal, "admission": admission,
                                     "experiment_id": ctx.experiment_id or new_id("x")})
                else:
                    cand["admission"] = admission
            if not admitted:
                statuses = {c.get("admission").status for c in kept if c.get("admission") is not None}
                if statuses == {"collect"}:
                    return ended("needs_collection", "admission asks for more evidence for every candidate", "collect")
                if "repair_runtime" in statuses and statuses <= {"repair_runtime", "collect"}:
                    return ended("repair_runtime", "admission names a missing runtime capability", "repair_runtime")
                return ended("invalid", "no candidate passed its draft check and admission", "none")
            # ------------------------------------------------------------ the train-side screen (M4 §2)
            # The failing set is (the notes' tasks ∪ predicted_affected) ∩ the tasks the latest train measurement
            # FAILED: a predicted task that already passes stays in the declaration and reaches the screen only
            # through the fixed passing sample.
            failed_now = {t for t, ok in inputs.before.items() if not ok}
            for cand in admitted:
                decl = cand["decl"]
                failing = [t for t in dict.fromkeys([*(note_tasks.get(n) for n in decl["notes"]),
                                                     *decl["predicted_affected"]])
                           if t in set(inputs.train_tasks) and t in failed_now]
                tasks = failing + [t for t in inputs.sample if t not in failing]
                cand["screen"] = {"candidate": cand["id"], "order": cand["order"], "tasks": tasks, "failing": failing,
                                  "sample": [t for t in tasks if t not in failing],
                                  "thin_screen": len(failing) < THIN_SCREEN_TASKS, "net_flips": None,
                                  "cost_per_attempt": None, "before": {}, "after": {}, "candidate_ref": None,
                                  "experiment_id": cand["experiment_id"], "proposal_ref": cand["admission"].proposal_ref}
            # once, before the first screen is paid: every admitted candidate's screen attempts plus the gate's own
            # verification bound must fit, or the round ends here without a screen (a candidate with no failing
            # task is not screened, so its sample is not counted)
            attempts = sum(len(c["screen"]["tasks"]) for c in admitted if c["screen"]["failing"])
            verify = dict((ctx.handlers or {}).get("verify_bound") or {})
            need = {"model_calls": attempts + int(verify.get("model_calls") or 0),
                    "tool_calls": attempts + int(verify.get("tool_calls") or 0)}
            if verify.get("usd") is not None:
                # v0.3 step 5 (M5 §2, 2026-10-05): the gate's bound in dollars, and the screens at the same per-attempt
                # price, when the parent W's cost is on record
                need["usd"] = round(float(verify["usd"]) + attempts * float(verify.get("usd_per_attempt") or 0.0), 6)
            if budget is not None and not budget.affordable(need):
                raise BudgetExhausted(f"the round's {len(admitted)} screens ({attempts} attempts) and the gate's "
                                      f"verification bound {verify} do not fit the budget")
            for cand in admitted:
                entry = cand["screen"]
                tasks = entry["tasks"]
                spent = False
                if not entry["failing"]:
                    # nothing the candidate claims to fix fails now: the sample alone could only read <= 0, and a
                    # screened_out from it would count as a repeat of the mechanism — not paid, invalid
                    entry["reason"] = ("no failing train task to screen on: its notes' tasks and predicted_affected "
                                       "include no task that failed in the latest measurement")
                else:
                    try:
                        entry["candidate_ref"] = inputs.build(cand["proposal"], cand["experiment_id"])
                    except Exception as e:  # noqa: BLE001 - a candidate that does not build has no reading: invalid
                        if isinstance(e, (BudgetExhausted, ReplyTruncated, WorkerUnavailable, WorkerCrashed)):
                            raise
                        entry["reason"] = f"build: {type(e).__name__}: {e}"[:300]
                    else:
                        rows_after = inputs.screen_arm(entry["candidate_ref"], tasks)
                        spent = _read_screen(entry, rows_after, inputs.before)
                events.append("train_screen", scope=ctx.scope_id, round=ctx.round_index, ordinal=ordinal,
                              candidate=cand["id"], tasks=tasks, net_flips=entry["net_flips"],
                              **{key: entry[key] for key in ("fixes", "sample_regressions") if key in entry},
                              cost_per_attempt=entry["cost_per_attempt"], thin_screen=entry["thin_screen"],
                              attempts=entry.get("attempts") or [],
                              **({"lost_to": entry["lost_to"]} if entry.get("lost_to") else {}),
                              **({"error": entry["reason"]} if entry.get("reason") else {}))
                entry["outcome"] = "interrupted"            # until the pick; flushed as such if the round dies here
                screened.append(entry)
                if spent:
                    raise BudgetExhausted(f"the screen of {cand['id']} ran out of budget: {entry['reason']}")
            best = pick_best(screened, inputs.screen)
            for s in screened:
                if s["net_flips"] is None:
                    s["outcome"] = "lost" if s.get("lost") else "invalid"
                elif best is not None and s is best:
                    s["outcome"] = "selected"
                elif eligible(s, inputs.screen):
                    s["outcome"] = "not_selected"
                else:
                    s["outcome"] = "screened_out"
                screened_row(s)
        finally:
            for s in screened:
                screened_row(s)                 # a paid screen the round died after is on record as interrupted
            if wsession is not None:
                wsession.close(force=True)
            if session is not None:
                # 2026-10-05 review: the turns themselves — R's replies, the calls with their arguments, every tool
                # answer and refusal — stored as one object the event names, not only the per-call rows
                transcript_ref = ctx.objects.put_json("transcript", session.transcript())
                events.append("research_session", scope=ctx.scope_id, release=before, round=ctx.round_index,
                              research_ref=release.research_ref, prompt_hash=ct.digest({"system": system})[:16],
                              transcript_ref=transcript_ref, **session.record())
            record = ProposalResult(proposals=list(proposals_made))
            record.budget_seen = budget_seen
            _record_research_steps(ctx, root, before, sent_before, record)
    except ReplyTruncated as e:
        return ended("infrastructure_failed", str(e), "repair_runtime")
    except BudgetExhausted as e:
        return ended("budget_exhausted", str(e), "stop")
    except (WorkerUnavailable, WorkerCrashed) as e:
        # M0 §3: the worker K requires is not there or died under the session; nothing of R's is at fault, the
        # round is run again after the repair with a new session.
        return ended("infrastructure_failed", f"the isolated worker failed during R's session: {str(e)[:240]}",
                     "repair_runtime")
    if best is None:
        if all(s["net_flips"] is None for s in screened):
            why = "; ".join(f"{s['candidate'].rpartition('/')[2]}: {s.get('reason')}" for s in screened)
            if any(s["outcome"] == "lost" for s in screened):
                # nobody read, and what was lost was lost to infrastructure or the evaluator: the gate's integrity
                # loss, not R's — run again, counted toward the failure limit, never a completed round
                return ended("inconclusive", ("infrastructure: no candidate had a valid screen reading, the screen "
                                              "attempts were lost (refill included): " + why)[:300])
            return ended("invalid", ("no candidate had a valid screen reading: " + why)[:300], "none")
        if inputs.screen.v04:
            # V0_4_PLAN §1.6: the rule and each candidate's fixes and sample regressions, so the record says why
            return ended("screened_out", (f"no candidate was eligible (≥{inputs.screen.min_fixes} fixes, "
                                          f"≤{inputs.screen.max_sample_regressions} sample regressions, net flips "
                                          f"≥{inputs.screen.min_net_flips}): " + "; ".join(
                f"{s['candidate'].rpartition('/')[2]} fixes {s.get('fixes')} sample regressions "
                f"{s.get('sample_regressions')}" for s in screened))[:300], "none")
        return ended("screened_out", "no candidate reached the screen's min_net_flips "
                                     f"{inputs.screen.min_net_flips}: " + "; ".join(
                         f"{s['candidate'].rpartition('/')[2]} {s['net_flips']}" for s in screened)[:250], "none")
    chosen = next(c for c in admitted if c["id"] == best["candidate"])
    result = _execute_admitted(ctx, chosen["admission"], chosen["proposal"], before, root, budget, requirements=(),
                               experiment_id=chosen["experiment_id"])
    ledger("gated", chosen["id"], **gate_ledger_fields(result.outcome, result.receipt_ref, objects))
    return result


def grounding_inputs(inputs: "SessionInputs", listed) -> dict | None:
    """What admission's read floors are checked against (V0_4_PLAN §1.3, M4 §1 v0.4), or None when K sets none: the
    session's reads by outcome (TrajectoryReader.read_tasks), the floors, and whether a passing attempt was listed at
    all (with none listed the passing floor cannot be met and is not asked)."""
    limits = inputs.limits
    if not (limits.min_failing_reads or limits.min_passing_reads) or inputs.trajectories is None:
        return None
    return {**inputs.trajectories.read_tasks(), "min_failing": limits.min_failing_reads,
            "min_passing": limits.min_passing_reads,
            "passing_listed": any(e.get("outcome") == "passing" for e in listed or ())}


@dataclass
class SessionInputs:
    """What one round's session needs from the activity (built by bench.Improvement.round_inputs): K's caps, the
    worker, the current package and prompt texts, the dev histogram, the trajectory reader, the train split, the
    fixed passing sample, the latest measurement's per-task outcomes, and the callables that build and screen a
    candidate — so this module runs no arm and reads no store of its own."""
    limits: SessionLimits
    screen: ScreenSettings
    k: int
    worker: object
    package_files: dict
    prompt_texts: dict                      # target -> current text (worker.system_prompt, worker.finish)
    histogram: dict
    trajectories: TrajectoryReader
    train_tasks: tuple
    train_order: tuple
    sample: tuple                           # the fixed passing sample of this round
    before: dict                            # task -> success in the latest measurement (the screen's "before")
    build: object                           # (proposal, experiment_id) -> candidate W ref
    screen_arm: object                      # (candidate_ref, tasks) -> list[TaskResult]
    affordable: object = None               # (n_attempts) -> bool
    base_tools: list = field(default_factory=list)
    api_version: str = hp.AGENT_API_VERSION
    max_package_bytes: int = 262144         # K's cap on a candidate directory and on each of its files (the tree)
    # v0.4 (docs/design/V0_4_PLAN.md §1.2, §1.3): the passing train attempts of the latest measurement, tasks with
    # must-not checks first, for the trajectory listing; those tasks; and the interface error table (api_errors.py)
    passing_attempts: tuple = ()
    must_not: tuple = ()
    api_errors: dict = field(default_factory=dict)
