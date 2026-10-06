"""The ceiling agent: W*'s loop plus four loop-level mechanisms, hand-written for the ceiling experiment
(docs/ROADMAP.md §3 v0.3 step 4; examples/automationbench/ceiling/README.md). Not a candidate, never installed.

It is the default agent (ouroloop/agent/default_agent.py, the seed every evolved loop descends from) with the same
loop — the task under the system prompt the host composed (W's prompt policies and the package's memory), one `act`
call a turn, a turn without tool calls is a finish request, the package's on_finish hooks may push back at most
MAX_PUSHBACKS times and never on the last turn — and four additions, each within K:

(a) Full-text reading. The host clips one tool result at K's `max_output_chars` (in ctx.view; 200 * 100 characters
    under the v3 preset), cutting the middle and leaving "[... N characters omitted ...]"; a package can neither see
    past that nor raise it. What it can do:
    say so, and read in pages. `read_full` takes up to MAX_READ_REQUESTS GET requests, runs each through ctx.tool with
    read_only=True (the broker refuses anything that writes; every call is one broker event and one of the 200 tool
    calls, and it keeps READ_RESERVE of them free for writes), and returns their whole joined text in pages of at most
    `max_output_chars` — so a package tool's output never exceeds a base tool's bound, and nothing is cut: the next
    page is the same requests with page+1, answered from this attempt's cache with no new tool call. After any base
    result the agent adds a reading note when the host clipped it, when it says more pages exist, or when it is a
    list of ids or snippets only — once per kind and endpoint.
(b) An evidence table before the first write. The first call that would change the world (api_fetch with a method
    other than GET/HEAD, a POST to a search or query route excepted) is not executed; the calls of that turn before
    it are, the rest are not (the default agent's before_tool convention, recorded with ctx.mark). One `aux` call —
    same messages and tools, so the provider's
    cached prefix is reused, plus one request — lists every requirement with its evidence, source and status and the
    plan of writes; the table goes into the history and the model re-issues the writes against it.
(c) A check before finishing. After W*'s on_finish hook has had its say, one `aux` call compares each requirement
    (the table, the request) with the writes and sends this attempt actually executed — the agent's own record of its
    calls and their results, not the model's memory — and ends with a JSON verdict. "continue" with gaps sends the
    model back once with the gaps; at most MAX_CHECKS checks an attempt; anything unreadable finishes.
(d) Effort at the cap. Every model call asks for EFFORT ("high", K's `max_effort` in the v3 preset); the gateway caps a
    request above K's ceiling and records the request, so asking is never an error.

Aux calls are made only while the attempt has room for them (AUX_MIN_*); a hard limit is the host's and ends the
attempt whatever this code does. Standard library only, no ouroloop import, the five names of ctx plus ctx.mark.
"""
import asyncio
import importlib.util
import inspect
import json
import os
import re

EFFORT = "high"
MAX_PUSHBACKS = 2                     # on_finish hooks (W*'s finish_audit), the default agent's cap
MAX_CHECKS = 2                        # (c): check calls per attempt; the second only after a first "continue"
MAX_TOOL_OUTPUT = 200 * 100           # K's max_output_chars when the view lacks it
READ_FULL = "read_full"
READ_SKILL = "read_skill"
MAX_READ_REQUESTS = 20                # (a): GETs per read_full call
READ_RESERVE = 20                     # (a): tool calls read_full leaves for writes
PAGE_HEAD = 300                       # room for a page's header inside max_output_chars
AUX_MIN_MODEL_CALLS = 2
AUX_MIN_SECONDS = 20
AUX_MIN_TOKENS = 80 * 100
TABLE_CHARS = 120 * 100               # of the evidence table, in the history and in the check
WRITES_CHARS = 120 * 100              # of the executed writes, in the check
NOTE_CHARS = 40 * 100                 # of the reading notes, and of the final message a hook sees
READ_METHODS = ("GET", "HEAD")
READ_POST = re.compile(r"[/:](search|searchStream|query|find)$")     # POST routes that only read
CLIPPED = re.compile(r"\[\.\.\. (\d+) characters omitted \.\.\.\]")
PAGE_KEYS = ("nextPageToken", "next_page_token", "next_cursor", "next_page", "nextPage", "has_more", "hasMore",
             "next")
HOOK_MESSAGE_CHARS = 20 * 100         # of a hook's message, as the default agent
HOOK_ACTIONS = {"before_tool": ("continue", "block", "feedback"), "after_tool": ("continue", "feedback"),
                "on_finish": ("continue", "block", "feedback")}
PUSHBACK = "{reason}\nKeep working on the task, or explain why it is already done."

READ_FULL_SPEC = {
    "name": READ_FULL,
    "description": ("Read the whole text of up to %d GET requests at once, in pages; nothing is cut. Give the same "
                    "requests with page 1, 2, ... to read the rest (no new call is made for a later page). "
                    "Read-only: it cannot write." % MAX_READ_REQUESTS),
    "schema": {"type": "object",
               "properties": {"requests": {"type": "array", "description": "the GET requests, in order",
                                           "items": {"type": "object",
                                                     "properties": {"url": {"type": "string"},
                                                                    "params": {"type": "string",
                                                                               "description": "query parameters "
                                                                                              "as a JSON string"}},
                                                     "required": ["url"]}},
                              "page": {"type": "integer", "description": "0 for the first page"}},
               "required": ["requests"]},
}

EVIDENCE_PROMPT = (
    "Before the first write. Your next step changes the world and nothing is undoable, so first build an evidence "
    "table. Reply in text only: do not call any tool in this reply.\n"
    "1. Requirements. Number every requirement the request states and every directive of the documents, messages "
    "and records you have read that governs it: each recipient and channel; each record to create, change or leave "
    "alone; each value, code, date, name or wording to include; each count or total; each condition, exclusion or "
    "exception.\n"
    "2. Table. One row per requirement: | # | requirement | evidence (the exact value) | source (the tool call and "
    "the document, row or field it came from) | status |. Status is VERIFIED (read in a tool result), ASSUMED "
    "(inferred, not read) or MISSING (not found yet).\n"
    "3. Reads still needed. For each ASSUMED or MISSING row, the read that would settle it.\n"
    "4. Plan. The writes and sends in order, each citing the rows it satisfies. A write may rest only on VERIFIED "
    "rows.")
TABLE_INTRO = (
    "Evidence table, built before the first write from what you had read (that write was not executed):\n\n"
    "{table}\n\n"
    "Carry out the plan against this table: first make the reads it lists for ASSUMED or MISSING rows and correct "
    "the rows they change; then make the writes and sends, each one matching its rows exactly.")
CHECK_PROMPT = (
    "Check before finishing. Reply in text only: do not call any tool in this reply.\n"
    "Compare what the request and the documents that govern it require with what was actually done. The writes and "
    "sends this attempt executed, from their tool results, in order (searches and queries are reads and are not "
    "listed):\n{writes}\n{table}"
    "For each requirement: the executed write or send that satisfies it (by its number), or GAP with what is missing "
    "or wrong: a missing or extra recipient, channel or record; a value, code, name, count or wording that is "
    "missing or differs from its source; a write that should not have been made; a failed call never retried.\n"
    'End the reply with exactly one line of JSON: {{"verdict": "finish"}} when every requirement is met and nothing '
    'wrong was written, otherwise {{"verdict": "continue", "gaps": ["<gap>", ...]}}.')
GAPS_PUSHBACK = (
    "A check before finishing compared each requirement with the writes and sends you made and found these gaps:\n"
    "{gaps}\nClose each one with tools now (a corrective or additional write or send where one is needed), or say "
    "why it does not apply; then finish.")
CLIP_NOTE = ("The result of {what} was clipped by the host at {limit} characters: {omitted} characters in its middle "
             "are not shown. Read the omitted part in pages: repeat the request with a narrower range, filter, "
             "limit/offset or page parameter, or GET its items one at a time (read_full returns up to "
             + str(MAX_READ_REQUESTS) + " GETs in full, in pages).")
PAGES_NOTE = "The result of {what} says more pages exist ({key}). Read every page before relying on the list."
SUMMARY_NOTE = ("The result of {what} lists {count} items by id or snippet only. Open in full every item that could "
                "matter (read_full can GET several at once) before relying on what it says.")


# ---------------------------------------------------------------------------- the loaded bundle (as default_agent)
class _Bundle:
    def __init__(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.modules = {}

    def entry(self, spec):
        module_path, _, func = spec.partition(":")
        if module_path not in self.modules:
            full = os.path.join(self.root, module_path)
            name = "ceilingpkg_" + module_path.replace("/", "_").replace(".py", "")
            loader = importlib.util.spec_from_file_location(name, full)
            if loader is None or loader.loader is None:
                raise ImportError("cannot load " + module_path)
            module = importlib.util.module_from_spec(loader)
            loader.loader.exec_module(module)
            self.modules[module_path] = module
        fn = getattr(self.modules[module_path], func, None)
        if not callable(fn):
            raise AttributeError(spec + ": no callable " + func)
        return fn


async def _await(value):
    return await value


def _call(fn, *args):
    value = fn(*args)
    if inspect.isawaitable(value):
        value = asyncio.run(value if asyncio.iscoroutine(value) else _await(value))
    return value


class _PackageContext:
    """What a v1 hook holds: the view with this turn added, and a read-only broker through ctx.tool."""

    def __init__(self, ctx, turn, read_only):
        self.view = dict(ctx.view or {}, turn=turn)
        self._ctx, self._read_only = ctx, read_only
        self.calls = []

    def call_tool(self, name, args=None):
        res = self._ctx.tool(name, dict(args or {}), read_only=self._read_only)
        self.calls.append({"tool": name, "is_error": bool(res.is_error)})
        return res


def _run_hooks(ctx, bundle, hooks, site, event, turn):
    """-> (action, messages), as the default agent: the first block or feedback of an on_finish hook ends the chain;
    an answer outside the site's protocol raises."""
    messages = []
    for hook in hooks:
        if hook.get("site") != site:
            continue
        hctx = _PackageContext(ctx, turn, read_only=True)
        value = _call(bundle.entry(hook["entrypoint"]), hctx, event)
        if not isinstance(value, dict) or value.get("action") not in HOOK_ACTIONS[site] or \
                sorted(set(value) - {"action", "message"}):
            raise ValueError("hook %s at %s answered %s" % (hook.get("name"), site, str(value)[:120]))
        action, message = value["action"], str(value.get("message") or "")[:HOOK_MESSAGE_CHARS]
        ctx.mark("hook", hook=hook.get("name"), site=site, action=action, message=message[:300],
                 broker_calls=hctx.calls)
        if action == "continue":
            continue
        messages.append(message or "%s asks you to check before going on" % hook.get("name"))
        if site != "after_tool":
            return action, messages
    return ("feedback" if messages else "continue"), messages


# ---------------------------------------------------------------------------- limits
def _left(ctx, key):
    return (ctx.limits or {}).get(key)


def _turns_left(ctx):
    left = _left(ctx, "act_calls")
    return left is None or left > 0


def _aux_room(ctx):
    """Whether an aux call fits what is left of the attempt, with room to act on its answer."""
    for key, least in (("model_calls", AUX_MIN_MODEL_CALLS), ("seconds", AUX_MIN_SECONDS),
                       ("tokens", AUX_MIN_TOKENS)):
        left = _left(ctx, key)
        if left is not None and left < least:
            return False
    return True


def _model(ctx, messages, specs, role):
    return ctx.model(messages, specs, role=role, effort=EFFORT)


def _aux(ctx, messages, specs, prompt):
    """One aux call over the act history plus one request -> its text. The tools are the act call's, so the request
    shares the history's cached prefix; any tool call in the answer is ignored. A gateway refusal (an undeclared role)
    is not caught: the draft smoke finds it, and a mechanism that silently switched itself off would make the ceiling
    read as W*."""
    reply = _model(ctx, list(messages) + [{"role": "user", "content": prompt}], specs, "aux")
    return str(reply.text or "")


# ---------------------------------------------------------------------------- (a) reading
def _is_write(name, args):
    """A call that may change the world: api_fetch with a method other than GET/HEAD, except a POST to a search or
    query route (`.../search`, `:search`, `:searchStream`, `/query`, `:find` at the end of the path), which reads.
    Anchored at the end, so `:findOrCreate...` stays a write."""
    args = args or {}
    if name != "api_fetch" or str(args.get("method", "")).upper() in READ_METHODS:
        return False
    path = str(args.get("url", "")).split("?", 1)[0].rstrip("/")
    return not READ_POST.search(path)


def _what(name, args):
    args = args or {}
    if name == "api_fetch":
        return ("%s %s" % (str(args.get("method", "")).upper(), args.get("url", "")))[:160]
    return name


def _endpoint(name, args):
    url = str((args or {}).get("url", "")) if name == "api_fetch" else name
    return url.split("?", 1)[0]


def _nonempty(value):
    """A page signal holds something: a dict only when one of its values does ({"after": null} is no next page)."""
    if isinstance(value, dict):
        return any(_nonempty(v) for v in value.values())
    return bool(value)


def _page_key(value, depth=0):
    if depth > 3:
        return None
    if isinstance(value, dict):
        for key in PAGE_KEYS:
            v = value.get(key)
            if not isinstance(v, list) and _nonempty(v):
                return "%s=%s" % (key, json.dumps(v)[:60] if isinstance(v, dict) else str(v)[:60])
        for v in value.values():
            if isinstance(v, dict):
                found = _page_key(v, depth + 1)
                if found:
                    return found
    return None


def _summary_count(value):
    """How many items a list of ids or snippets holds, or 0: the first list of objects in the top level whose every
    item has an id and either a snippet or at most three keys."""
    if not isinstance(value, dict):
        return 0
    for v in value.values():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            if all("id" in x and ("snippet" in x or len(x) <= 3) for x in v):
                return len(v)
            return 0
    return 0


def _reading_notes(name, args, output, max_out, seen):
    """The reading notes for one result, each once per (kind, endpoint) an attempt."""
    notes, where = [], _endpoint(name, args)
    what = _what(name, args)
    clipped = CLIPPED.search(output or "")
    if clipped and ("clip", where) not in seen:
        seen.add(("clip", where))
        notes.append(CLIP_NOTE.format(what=what, limit=max_out, omitted=clipped.group(1)))
    if name != "api_fetch":
        return notes
    try:
        value = json.loads(output)
    except (ValueError, TypeError):
        return notes
    key = _page_key(value)
    if key and ("pages", where) not in seen:
        seen.add(("pages", where))
        notes.append(PAGES_NOTE.format(what=what, key=key))
    count = _summary_count(value)
    if count and ("summary", where) not in seen:
        seen.add(("summary", where))
        notes.append(SUMMARY_NOTE.format(what=what, count=count))
    return notes


def _read_full(ctx, args, cache, max_out):
    """(a) -> (output, is_error, broker calls)."""
    requests = (args or {}).get("requests")
    try:
        page = int((args or {}).get("page") or 0)
    except (TypeError, ValueError):
        page = -1
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_READ_REQUESTS or page < 0:
        return ("read_full takes 1 to %d requests ({url, params}) and a page of 0 or more" % MAX_READ_REQUESTS,
                True, [])
    clean = []
    for r in requests:
        if not isinstance(r, dict) or not isinstance(r.get("url"), str) or not r["url"]:
            return "each request is an object with a url (and params as a JSON string)", True, []
        params = r.get("params")
        if params is not None and not isinstance(params, str):
            params = json.dumps(params)
        clean.append({"url": r["url"], **({"params": params} if params else {})})
    key = json.dumps(clean, sort_keys=True)
    calls = []
    if key not in cache:
        left = _left(ctx, "tool_calls")
        if left is not None and left - len(clean) < READ_RESERVE:
            return ("read_full would leave fewer than %d tool calls for writes (%s left): read fewer items"
                    % (READ_RESERVE, left), True, [])
        parts = []
        for i, r in enumerate(clean):
            res = ctx.tool("api_fetch", {"method": "GET", **r}, read_only=True)
            out = str(res.output)
            calls.append({"tool": "api_fetch", "is_error": bool(res.is_error)})
            head = "=== [%d] GET %s%s (%d characters%s) ===" % (
                i + 1, r["url"], (" params=" + r["params"]) if r.get("params") else "", len(out),
                ", error" if res.is_error else "")
            clipped = CLIPPED.search(out)
            tail = ("\n[the host clipped this item: %s characters are missing in its middle; read it with a "
                    "narrower request]" % clipped.group(1)) if clipped else ""
            parts.append(head + "\n" + out + tail)
        cache[key] = "\n\n".join(parts)
    text = cache[key]
    size = max(max_out - PAGE_HEAD, 10 * 100)
    pages = max((len(text) + size - 1) // size, 1)
    if page >= pages:
        return "there are %d pages (0 to %d)" % (pages, pages - 1), True, calls
    a, b = page * size, min((page + 1) * size, len(text))
    more = "\n[page %d of %d: call read_full with the same requests and page=%d for the rest]" % (
        page + 1, pages, page + 1) if b < len(text) else ""
    return ("page %d of %d (characters %d-%d of %d)\n" % (page + 1, pages, a, b, len(text))) + text[a:b] + more, \
        False, calls


# ---------------------------------------------------------------------------- (b), (c)
def _writes_text(writes):
    if not writes:
        return "(none: this attempt made no write or send)"
    lines = []
    for i, w in enumerate(writes, 1):
        args = w["args"]
        parts = ["%d. %s" % (i, _what("api_fetch", args))]
        if args.get("params"):
            parts.append("params=" + str(args["params"])[:300])
        if args.get("body"):
            parts.append("body=" + str(args["body"])[:800])
        parts.append("-> %s: %s" % ("ERROR" if w["is_error"] else "ok", str(w["output"])[:200]))
        lines.append(" ".join(parts))
    text = "\n".join(lines)
    return text if len(text) <= WRITES_CHARS else text[:WRITES_CHARS] + "\n[... more writes not shown]"


def _verdict(text):
    """The last JSON object with a verdict in the text: ("finish" | "continue", gaps) or (None, [])."""
    decoder = json.JSONDecoder()
    text = text or ""
    for i in range(len(text) - 1, -1, -1):
        if text[i] != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("verdict") in ("finish", "continue"):
            gaps = value.get("gaps") if isinstance(value.get("gaps"), list) else []
            return value["verdict"], [str(g)[:400] for g in gaps if str(g).strip()][:20]
    return None, []


# ---------------------------------------------------------------------------- the loop
def run(ctx, task):
    """One attempt. Returns the final message; returning is finishing."""
    view = ctx.view or {}
    package = view.get("package") or {}
    bundle = _Bundle()
    hooks = [h for h in package.get("hooks") or () if h.get("enabled", True) and h.get("site") == "on_finish"]
    skills = [s.get("name") for s in (getattr(ctx.skills, "index", None) or ())]
    max_out = int(view.get("max_output_chars") or MAX_TOOL_OUTPUT)
    specs = [dict(t) for t in view.get("base_tools") or ()] + [dict(READ_FULL_SPEC)]
    if skills:
        specs.append({"name": READ_SKILL, "description": "Read one of the skills listed in your instructions. "
                                                         "Returns its text.",
                      "schema": {"type": "object", "properties": {"name": {"type": "string", "enum": skills}},
                                 "required": ["name"], "additionalProperties": False}})
    messages = [{"role": "system", "content": view.get("system") or ""}, {"role": "user", "content": task}]
    table = None                      # (b): None until the first write; the table text after
    writes = []                       # (c): the executed writes, the agent's own record
    seen, cache = set(), {}           # (a): notes given; read_full's pages
    pushbacks, checks, turn = 0, 0, 0
    while True:
        turn += 1
        reply = _model(ctx, messages, specs, "act")
        calls = [{"id": c["id"], "name": c["name"], "args": dict(c.get("args") or {})} for c in reply.tool_calls]
        messages.append({"role": "assistant", "text": reply.text, "tool_calls": calls})
        text = reply.text
        if reply.stop != "tool_use" or not calls:
            if reply.stop not in ("end", "other") or not _turns_left(ctx):
                return text
            if hooks and pushbacks < MAX_PUSHBACKS:
                action, notes = _run_hooks(ctx, bundle, hooks, "on_finish",
                                           {"final_message": text[:NOTE_CHARS], "turn": turn}, turn)
                if action != "continue":
                    pushbacks += 1
                    reason = " ".join(notes)
                    messages.append({"role": "user", "content": PUSHBACK.format(reason=reason)})
                    ctx.mark("pushback", reason=reason[:300])
                    continue
            if checks < MAX_CHECKS and _aux_room(ctx):
                checks += 1
                answer = _aux(ctx, messages, specs, CHECK_PROMPT.format(
                    writes=_writes_text(writes),
                    table=("The evidence table built before the first write:\n%s\n" % table[:TABLE_CHARS])
                    if table else ""))
                verdict, gaps = _verdict(answer)
                ctx.mark("check", verdict=verdict or "unreadable", gaps=len(gaps), writes=len(writes))
                if verdict == "continue" and gaps:
                    messages.append({"role": "user", "content": GAPS_PUSHBACK.format(
                        gaps="\n".join("- " + g for g in gaps))})
                    continue
            return text
        results, notes, blocked, build_table = [], [], None, False
        for call in calls:
            if blocked is not None:
                results.append({"id": call["id"], "name": call["name"], "is_error": True,
                                "output": "Not executed: an earlier call in this turn was stopped (%s)" % blocked})
                ctx.mark("tool_skipped", tool=call["name"], call_id=call["id"], turn=turn)
                continue
            if table is None and _is_write(call["name"], call["args"]) and _turns_left(ctx) and _aux_room(ctx):
                blocked, build_table = "an evidence table is built before the first write", True
                results.append({"id": call["id"], "name": call["name"], "is_error": True,
                                "output": "Not executed yet: before the first write the agent builds an evidence "
                                          "table (next message). Re-issue this write if the table supports it."})
                ctx.mark("tool_blocked", tool=call["name"], call_id=call["id"], turn=turn, reason=blocked)
                continue
            output, is_error = _execute(ctx, call, cache, max_out, skills)
            results.append({"id": call["id"], "name": call["name"], "output": output, "is_error": is_error})
            if _is_write(call["name"], call["args"]):
                writes.append({"args": call["args"], "is_error": is_error, "output": output})
            if call["name"] != READ_FULL:
                notes += _reading_notes(call["name"], call["args"], output, max_out, seen)
        messages.append({"role": "tool", "results": results})
        follow = []
        if build_table:
            made = _aux(ctx, messages, specs, EVIDENCE_PROMPT)
            table = made.strip()[:TABLE_CHARS] or "(the evidence table call returned no text)"
            ctx.mark("evidence_table", chars=len(made), turn=turn)
            follow.append(TABLE_INTRO.format(table=table))
        if notes:
            follow.append("\n".join(notes)[:NOTE_CHARS])
        if follow:
            messages.append({"role": "user", "content": "\n\n".join(follow)})


def _execute(ctx, call, cache, max_out, skills):
    name, args = call["name"], call["args"]
    if name == READ_FULL:
        output, is_error, broker_calls = _read_full(ctx, args, cache, max_out)
        ctx.mark("package_tool", tool=READ_FULL, is_error=is_error, output=output[:300], broker_calls=broker_calls)
        return output, is_error
    if name == READ_SKILL and skills:
        text = ctx.skills.read(str(args.get("name", "")))
        if text is None:
            return "no skill named %r; the skills are %s" % (args.get("name"), skills), True
        return text, False
    res = ctx.tool(name, args)
    return str(res.output), bool(res.is_error)
