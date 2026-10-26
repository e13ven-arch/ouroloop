"""ctx.tool, ctx.skills, ctx.memory and ctx.mark on the host (docs/design/RUNTIME.md §3, §6).

AttemptBroker is the only way from package code to the world: every call goes through the deny table, the base-tool
and read-only checks (a call the surface says may write is refused for a read-only caller: a hook, or a package tool
the manifest says does not write), and the episode's own permission and budget checks (the attempt's surface); then a
`tool` line and a `broker_event` line, and ctx.view["events"] refreshed with the latest calls. A surface that reports
its budget spent ends the attempt at that dimension (LimitHit) once the refusal is on record.

SkillReader answers ctx.skills from the package's own text; MemoryRecall answers ctx.memory.recall through the memory
reader the activity gives (none before the memory package is in place); Marks writes what the package says about its
own loop, labelled `source: "package"`, a hook's mark as the `hook` line the improver credits by. HostContext is the
same names in this process, for the toolcheck and for tests.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from ..contracts import LimitHit
from ..harness.contract import VIEW_EVENTS
from .records import EVENT_CHARS, RESERVED_KEYS, digest16

DENY = [re.compile(p) for p in (
    r"\brm\s+-\w*r\w*\s+(/|~|\$HOME)(/?\*)?(\s|;|&|$)",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b[^\n]*\bof=/dev/",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\bgit\s+push\b[^\n]*\s(--force|-f)(\s|$)",
    r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(ba|z)?sh\b",
    r"\bchmod\s+-R\s+777\s+/",
)]


def denied(command: str) -> str | None:
    """The deny-table pattern a shell command matches, or None. Rules first, model second: never run, whatever a
    model says."""
    for rx in DENY:
        if rx.search(command):
            return rx.pattern
    return None


@dataclass
class Broker:
    """The episode's base tools behind a permission check: a name outside them is refused; a call the surface says may
    write is refused when `read_only`. The bounded worker ops (call_tool, hook, test, smoke) answer their base calls
    through one of these over a fixture or train world."""
    surface: object
    read_only: bool = False
    calls: list = field(default_factory=list)

    def __call__(self, name: str, args: dict, *, read_only: bool | None = None) -> tuple[str, bool]:
        read_only = self.read_only if read_only is None else read_only
        base = {s.name for s in self.surface.specs()}
        if name not in base:
            self.calls.append({"tool": name, "refused": "not a base tool"})
            return f"{name!r} is not an authorised base tool: {sorted(base)}", True
        if read_only and self.surface.mutates(name, args):
            self.calls.append({"tool": name, "refused": "writes"})
            return "a read-only caller may only make read-only calls, and this call may change the world", True
        res = self.surface.run(name, args)
        self.calls.append({"tool": name, "is_error": res.is_error})
        return res.output, res.is_error


class AttemptBroker:
    """ctx.tool for one attempt. `surface` is the episode's tool surface with the attempt's limits on it; `gateway`
    is read for the act calls so far and the last model call id; `view` is the attempt's ctx.view, whose `events`
    this refreshes."""

    def __init__(self, surface, *, record, gateway, view: dict | None = None, call_prefix: str = "",
                 max_output_chars: int = 20000):
        self.surface, self.record, self.gateway, self.view = surface, record, gateway, view
        self.call_prefix, self.max_output_chars = call_prefix, max_output_chars
        self.seq = 0
        self.events: list[dict] = []
        self.called: list[str] = []           # base tools called, in order

    def tool_calls(self) -> int:
        return self.seq

    def call(self, name: str, args: dict | None, *, read_only: bool = False) -> tuple[str, bool]:
        args = dict(args or {})
        self.seq += 1
        call_id = f"{self.call_prefix}b{self.seq}"
        t0 = time.monotonic()
        veto = denied(str(args.get("command", ""))) if name == "bash" else None
        refused, reason, mutates = False, "", None
        if veto:
            output, is_error, refused, reason = f"This command is on the deny table and was not run ({veto}).", \
                True, True, f"deny: {veto}"
        else:
            try:
                mutates = bool(self.surface.mutates(name, args))
            except Exception:  # noqa: BLE001 - an unknown tool: the broker below refuses it by name
                mutates = None
            broker = Broker(self.surface, read_only=read_only)
            output, is_error = broker(name, args)
            if broker.calls and broker.calls[-1].get("refused"):
                refused, reason = True, broker.calls[-1]["refused"]
            elif getattr(self.surface, "exhausted", ""):
                refused, reason = True, f"budget: {getattr(self.surface, 'limit', '') or 'tool_calls'}"
        output = str(output)[:self.max_output_chars]
        self.called.append(name)
        self.record("tool", {"call_id": call_id, "name": name, "args": args, "output": output[:EVENT_CHARS],
                             "is_error": bool(is_error), "read_only": bool(read_only),
                             "ms": int((time.monotonic() - t0) * 1000), "after": self.gateway.last_call_id})
        self.record("broker_event", {"call_id": call_id, "name": name, "mutates": mutates, "refused": refused,
                                     "reason": reason, "output_digest": digest16(output), "output_chars": len(output),
                                     "read_only": bool(read_only)})
        self.events.append({"call_id": call_id, "name": name, "args": args,
                            "result_excerpt": "" if is_error else output[:EVENT_CHARS],
                            "error": output[:EVENT_CHARS] if is_error else "", "truncated": len(output) > EVENT_CHARS,
                            "step": self.gateway.act_calls})
        if self.view is not None:
            self.view["events"] = list(self.events[-VIEW_EVENTS:])
        if getattr(self.surface, "exhausted", ""):
            raise LimitHit(getattr(self.surface, "limit", "") or "tool_calls")
        return output, bool(is_error)


class SkillReader:
    """ctx.skills: the package's enabled skill index and read(name), recorded as `skill_read`."""

    def __init__(self, package, *, record):
        self.package, self.record = package, record
        self.index = [{"name": s["name"], "description": s.get("description", "")}
                      for s in (package.manifest.skills if package is not None else ()) if s.get("enabled", True)]
        self.reads: list[str] = []

    def read(self, name) -> str | None:
        name = str(name)
        path = next((s["path"] for s in (self.package.manifest.skills if self.package is not None else ())
                     if s["name"] == name and s.get("enabled", True)), None)
        text = self.package.files.get(path) if path else None
        self.reads.append(name)
        self.record("skill_read", {"name": name, "found": text is not None, "chars": len(text or "")})
        return text


class MemoryRecall:
    """ctx.memory.recall: the memory reader's entries for a query, at most `max_k`, recorded as `library_retrieval`
    with site "recall". Without a reader (memory off, or before the memory package is in place) every recall is []."""

    def __init__(self, reader, *, record, max_k: int = 5):
        self.reader, self.record, self.max_k = reader, record, int(max_k)
        self.queries: list[str] = []

    def recall(self, query, k=None) -> list[dict]:
        query = str(query or "")
        k = self.max_k if not isinstance(k, int) or k <= 0 else min(k, self.max_k)
        entries = [dict(e) for e in (self.reader(query, k) if self.reader is not None else [])][:k]
        self.queries.append(query)
        self.record("library_retrieval", {"site": "recall", "query": query[:500],
                                          "shown": [e.get("id") for e in entries],
                                          "scores": [e.get("score") for e in entries]})
        return [{key: e.get(key) for key in ("id", "when", "content", "score")} for e in entries]


class Marks:
    """ctx.mark: what a package reports about its own loop, labelled `source: "package"` — never the host's evidence.
    A hook's mark (kind "hook": site, name, action, detail) becomes the `hook` line; anything else a `mark` line with
    the package's kind. The line's own fields are stripped from the package's data first."""

    HOOK_FIELDS = ("site", "name", "action", "detail", "turn")

    def __init__(self, record):
        self.record = record
        self.count = 0
        self.hooks: list[dict] = []
        self.kinds: dict[str, int] = {}

    def mark(self, kind, data: dict | None = None) -> None:
        kind, data = str(kind), {k: v for k, v in dict(data or {}).items() if k not in RESERVED_KEYS}
        self.count += 1
        self.kinds[kind] = self.kinds.get(kind, 0) + 1
        if kind == "hook":
            line = {k: data.get(k) for k in self.HOOK_FIELDS}
            line["detail"] = str(line.get("detail") or "")[:500]
            self.hooks.append(line)
            self.record("hook", {**line, "source": "package"})
        else:
            self.record("mark", {"mark": kind, "data": data, "source": "package"})


# ---------------------------------------------------------------------------- the same names in this process
class _Reply:
    """Protocol data with attributes and the same names as keys, as the worker's records are."""

    def __getitem__(self, key):
        if key not in vars(self):
            raise KeyError(key)
        return vars(self)[key]

    def get(self, key, default=None):
        return vars(self).get(key, default)


class ModelReply(_Reply):
    def __init__(self, text: str, tool_calls: list, stop):
        self.text, self.tool_calls, self.stop = text, tool_calls, stop


class ToolReply(_Reply):
    def __init__(self, output: str, is_error: bool = False):
        self.output, self.is_error = output, is_error


class ModelCallError(RuntimeError):
    """The gateway refused a ctx.model call (an undeclared role, a malformed call). The agent may catch it."""


class HostContext:
    """The contract's names over the host's gateway, broker, skills, memory and marks, in this process: for the
    toolcheck's scripts (IMPROVER §8) and for tests. Nothing of the host behind the names."""

    __slots__ = ("view", "skills", "memory", "limits", "model", "tool", "mark")

    def __init__(self, gateway, broker: AttemptBroker, skills: SkillReader, memory: MemoryRecall, marks: Marks,
                 view: dict):
        self.view, self.skills, self.memory = view, skills, memory
        self.limits = gateway.limits() if gateway is not None else {}

        def refresh():
            if gateway is not None:
                self.limits = gateway.limits()

        def model(messages, tools=(), role: str = "act", effort=None) -> ModelReply:
            if gateway is None:
                raise ModelCallError("no model here")
            reply = gateway.model(messages, tools, role, effort)
            refresh()
            if reply.get("is_error"):
                raise ModelCallError(str(reply.get("error") or "the gateway refused the call"))
            self.view["last_usage"] = reply.get("usage")
            return ModelReply(reply["text"], list(reply["tool_calls"]), reply["stop"])

        def tool(name: str, args=None, *, read_only: bool = False) -> ToolReply:
            output, is_error = broker.call(name, args or {}, read_only=read_only)
            refresh()
            return ToolReply(output, is_error)

        def mark(kind: str, **data) -> None:
            marks.mark(kind, data)

        self.model, self.tool, self.mark = model, tool, mark
