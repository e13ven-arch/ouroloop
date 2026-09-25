"""A person, or any other process, as the LLM ([llm] type = "manual", dir = "..."). The same interface and data protocol
as the API providers: each call writes the request, (system, messages, tools), to <dir>/requests/<id>.json and waits
for the reply, (text, tool_calls, stop, usage), in <dir>/responses/<id>.json:

    request   {"id", "model", "system", "messages": [history entries, see chat.py, without "native"],
               "tools": [{"name", "description", "schema"}]}
    response  {"text": str, "tool_calls": [{"id", "name", "args": {}}], "stop": "end" | "tool_use" | "max_tokens" |
               "refusal" | "other", "usage": {name: number}}      (tool_calls and usage may be left out)

A reply is checked against what an API provider can return: tool calls only to the tools offered, each with an id and
an argument object, exactly when the stop reason is "tool_use". `complete(system, prompt)` is one user message and no
tools. Write the response file in one step (write it elsewhere, then rename it): a file that does not parse yet is
read again at the next poll.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..chat import ChatTurn, ToolCall, ToolSpec
from ..ledger import new_id, write_json
from . import ProviderError

STOPS = ("end", "tool_use", "max_tokens", "refusal", "other")


def request_json(system: str, messages: list[dict], tools: list[ToolSpec]) -> dict:
    """The request as JSON: history entries as chat.py defines them, tool calls as objects, no provider's native
    content."""
    history = []
    for m in messages:
        m = {k: v for k, v in m.items() if k != "native"}
        if m.get("tool_calls"):
            m["tool_calls"] = [{"id": c.id, "name": c.name, "args": c.args} for c in m["tool_calls"]]
        history.append(m)
    return {"system": system, "messages": history,
            "tools": [{"name": t.name, "description": t.description, "schema": t.schema} for t in tools]}


def parse_turn(reply, tools: list[ToolSpec]) -> ChatTurn:
    """A reply as an API provider would have returned it, or ProviderError."""
    def bad(why: str):
        raise ProviderError(f"manual reply: {why}")

    if not isinstance(reply, dict):
        bad("not a JSON object")
    unknown = set(reply) - {"id", "text", "tool_calls", "stop", "usage"}
    if unknown:
        bad(f"unknown fields {sorted(unknown)}")
    text, stop, calls, usage = reply.get("text"), reply.get("stop"), reply.get("tool_calls", []), reply.get("usage", {})
    if not isinstance(text, str):
        bad('"text" must be a string')
    if stop not in STOPS:
        bad(f'"stop" must be one of {STOPS}, not {stop!r}')
    if not isinstance(usage, dict) or not all(isinstance(k, str) and isinstance(v, (int, float))
                                              and not isinstance(v, bool) for k, v in usage.items()):
        bad('"usage" must map names to numbers')
    if not isinstance(calls, list):
        bad('"tool_calls" must be a list')
    offered, out = {t.name for t in tools}, []
    for c in calls:
        if not isinstance(c, dict) or set(c) != {"id", "name", "args"}:
            bad('each tool call is {"id", "name", "args"}')
        if not isinstance(c["id"], str) or not c["id"]:
            bad("a tool call needs an id")
        if c["name"] not in offered:
            bad(f"tool {c['name']!r} was not offered (tools: {sorted(offered)})")
        if not isinstance(c["args"], dict):
            bad(f"the args of {c['name']} must be an object")
        out.append(ToolCall(c["id"], c["name"], c["args"]))
    if len({c.id for c in out}) != len(out):
        bad("tool call ids must be unique")
    if bool(out) != (stop == "tool_use"):
        bad('the stop reason is "tool_use" exactly when there are tool calls')
    return ChatTurn(text, out, stop, usage=dict(usage))


class ManualProvider:
    name = "manual"
    supports_history_edits = True

    def __init__(self, dir: str, model: str = "manual", source: str = "frontier_llm", poll: float = 1.0,
                 timeout: float = 24 * 3600.0, sleep=time.sleep):
        self.dir = Path(dir).expanduser()
        self.model = model
        self.source = source        # whoever answers, the answers are for evaluation only (PLAN_v0.2 §1 rule 4)
        self.poll, self.timeout, self.sleep = poll, timeout, sleep

    def complete(self, system: str, prompt: str) -> str:
        return self.chat(system, [{"role": "user", "content": prompt}], []).text

    def chat(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> ChatTurn:
        rid = new_id("req")
        write_json(self.dir / "requests" / f"{rid}.json",
                   {"id": rid, "model": self.model, **request_json(system, messages, tools)})
        path = self.dir / "responses" / f"{rid}.json"
        deadline = time.monotonic() + self.timeout
        while True:
            if path.exists():
                try:
                    reply = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    reply = None            # still being written
                if reply is not None:
                    if isinstance(reply, dict) and reply.get("id", rid) != rid:
                        raise ProviderError(f"manual reply {path} answers {reply['id']!r}, not {rid!r}")
                    return parse_turn(reply, tools)
            if time.monotonic() > deadline:
                raise ProviderError(f"no reply in {path} within {self.timeout:.0f}s")
            self.sleep(self.poll)
