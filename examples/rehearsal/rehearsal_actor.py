"""The rehearsal's deterministic fake actor (docs/REHEARSAL.md §3).

It reads the request, moves the contact to the stage asked for, and does the request's closing step only when
something it was given says to: a rule in its instructions or in a skill it read, for three kinds of closing step,
and for the fourth (a follow-up task) only a check that sends it back before it finishes — an on_finish hook's
push-back. When it skips a closing step, its final message says which, so the failure summary carries the clue
to R. It never raises, and its output depends only on what it is called with.
"""
from __future__ import annotations

import json
import re

from ouroloop.chat import ChatTurn, ToolCall

KEEP_WORKING = "Keep working on the task, or explain why it is already done."
SKILLS_HEADER = "Skills you can read with the read_skill tool before you act:"
BASE_TOOLS = {"api_search", "api_fetch", "base64_encode", "read_skill"}

CONTACTS = "https://api.hubapi.com/crm/v3/objects/contacts"
CLOSING = {
    # kind: (how the request states it, endpoint, body, what it is, the rule that makes the actor do it,
    #        whether the rule counts in its instructions and skills or only in a push-back)
    "A": (re.compile(r'Then post "([^"]+)" in #([\w-]+)\.'), ("POST", "https://slack.com/api/chat.postMessage"),
          lambda m, cid: {"channel": m.group(2), "text": m.group(1)},
          "posting the update in the Slack channel it names",
          re.compile(r"(post|message)[^.\n]{0,80}\b(slack|channel)\b|\b(slack|channel)\b[^.\n]{0,80}(post|message)",
                     re.I), True),
    "B": (re.compile(r"Then tag the contact ([\w-]+)\."), ("POST", f"{CONTACTS}/tags"),
          lambda m, cid: {"id": cid, "tag": m.group(1)}, "tagging the contact",
          re.compile(r"\btag(s|ging|ged)?\b", re.I), True),
    "C": (re.compile(r'Then log a follow-up task titled "([^"]+)" for the contact\.'),
          ("POST", "https://api.hubapi.com/crm/v3/objects/tasks"),
          lambda m, cid: {"contact_id": cid, "title": m.group(1)}, "logging the follow-up task",
          re.compile(r"follow[- ]?up tasks?", re.I), False),
    "D": (re.compile(r"Then make (.+?) the contact's owner\."), ("PATCH", f"{CONTACTS}/owner"),
          lambda m, cid: {"id": cid, "owner": m.group(1)}, "assigning the contact's owner",
          re.compile(r"\bowners?\b", re.I), True),
}


class RehearsalActor:
    """`[llm] type = "rehearsal_actor:RehearsalActor"`. Every other key of the table is accepted and ignored but
    `model`, which is the name it reports."""
    name = "rehearsal-actor"
    supports_history_edits = True

    def __init__(self, model: str = "rehearsal-actor-1", **_config):
        self.model = model

    def chat(self, system, messages, tools) -> ChatTurn:
        try:
            return self._turn(system or "", list(messages or ()), {t.name for t in tools or ()})
        except Exception as e:  # noqa: BLE001 - a fake actor that raised would be an infrastructure failure
            return self._say(system, messages, f"I could not work out the request ({type(e).__name__}).")

    # ------------------------------------------------------------------------ one turn
    def _turn(self, system: str, messages: list, offered: set) -> ChatTurn:
        request = str(messages[0].get("content", "")) if messages else ""
        contact = re.search(r"\(contact (\w+)\)", request)
        stage = re.search(r"Move them to the ([\w-]+) stage", request)
        if not contact or not stage:
            return self._say(system, messages, "Done: there was nothing I could act on.")
        cid, stage = contact.group(1), stage.group(1)
        called = [c for m in messages if m.get("role") == "assistant" for c in m.get("tool_calls") or ()]
        names = [(c.name if hasattr(c, "name") else c.get("name")) for c in called]
        args = [(c.args if hasattr(c, "args") else c.get("args")) or {} for c in called]
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        # the fault the protocol builds in (docs/REHEARSAL.md §6): a tool that is not offered, once
        if request.startswith("Look the contact up first.") and "crm_lookup" not in names:
            return self._call(system, messages, turn, [("crm_lookup", {"contact_id": cid})])
        read = {a.get("name") for n, a in zip(names, args) if n == "read_skill"}
        listed = self._skills(system) if "read_skill" in offered else []
        unread = [s for s in listed if s not in read]
        if unread:
            return self._call(system, messages, turn, [("read_skill", {"name": s}) for s in unread])
        bodies = [str(r.get("output", "")) for m in messages if m.get("role") == "tool"
                  for r in m.get("results") or () if r.get("name") == "read_skill" and not r.get("is_error")]
        urls = [str(a.get("url", "")) for n, a in zip(names, args) if n == "api_fetch"]
        if CONTACTS not in urls:
            return self._call(system, messages, turn, [("api_fetch", {
                "method": "PATCH", "url": CONTACTS, "body": json.dumps({"id": cid, "stage": stage})})])
        kind, match = next(((k, c[0].search(request)) for k, c in CLOSING.items() if c[0].search(request)),
                           (None, None))
        name = request.split(" (contact")[0].removeprefix("Look the contact up first. ")
        done = f"Done: {name} is in the {stage} stage"
        if kind is None:
            return self._say(system, messages, done + ".")
        _pattern, (method, url), body, what, rule, in_text = CLOSING[kind]
        if url in urls:
            return self._say(system, messages, f"{done}, and I finished the closing step, {what}.")
        pushbacks = [str(m.get("content", "")) for m in messages[1:]
                     if m.get("role") == "user" and str(m.get("content", "")).endswith(KEEP_WORKING)]
        texts = pushbacks + ([system, *bodies] if in_text else [])
        if any(rule.search(t) for t in texts):
            return self._call(system, messages, turn, [("api_fetch", {
                "method": method, "url": url, "body": json.dumps(body(match, cid))})])
        return self._say(system, messages, f"{done}. Not done: the closing step at the end of the request, {what}; "
                                           f"nothing in my instructions covers closing steps of this kind.")

    @staticmethod
    def _skills(system: str) -> list[str]:
        if SKILLS_HEADER not in system:
            return []
        block = system.split(SKILLS_HEADER, 1)[1]
        return re.findall(r"^- ([A-Za-z0-9_-]+)", block, re.M)

    # ------------------------------------------------------------------------ replies
    @staticmethod
    def _usage(system, messages) -> dict:
        size = len(system or "") + sum(len(json.dumps(m, default=str)) for m in messages or ())
        return {"input_tokens": max(1, size // 4), "output_tokens": 30}

    def _call(self, system, messages, turn: int, calls: list) -> ChatTurn:
        return ChatTurn("", [ToolCall(f"call_{turn}_{i}", n, a) for i, (n, a) in enumerate(calls)], "tool_use",
                        usage=self._usage(system, messages))

    def _say(self, system, messages, text: str) -> ChatTurn:
        return ChatTurn(text, [], "end", usage=self._usage(system, messages))
