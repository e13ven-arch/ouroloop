"""The write guard: four hooks of the stirrup seed (docs/DESIGN.md §4.2), standard library only, no model call.

  ledger  (after_tool)    keeps this attempt's record of what it changed, by call id, from the hook event and from
                          ctx.view["events"] (the broker's recent calls, a package tool's own included): every write —
                          a fetch whose method is neither GET nor HEAD and whose route is not a search (READ_POST) —
                          with its method, URL, status and the first LEDGER_BODY_CHARS of its body; the latest
                          plan_changes arguments; and the newest date in what was read (never a write's own result).
  triage  (before_tool)   the first write of the attempt, when plan_changes has not been called, is blocked once with
                          TRIAGE_MESSAGE.
  anchor  (before_model)  once a date has been read, every act call is sent with DATE_MESSAGE as a note at the place
                          it was first given — the end of the first act call that knew the date — and kept there
                          (non-destructive: the agent's history is not changed). A newer date gets its own note where
                          it is first given. Notes never move, so an act call's earlier messages are the previous
                          call's and the provider's prompt cache holds; a summary that shortens the history starts the
                          notes over at its end. A hook's aux call (an audit) gets the note as its last message: it is
                          a conversation of its own, and its model is told the real date too (HARNESS §7.1 rule 4).
  verify  (on_finish)     the first finish, while act calls remain, is pushed back once with the plan, the ledger and
                          the excluded items side by side and a check in both directions; an empty ledger is named.

The date anchor (failure cause #8: the claude_code CLI tells the model the real date, the sandbox's data is months
earlier, and 76 of 103 attempts that wrote the real date failed). DATE_MESSAGE names the newest date the agent read as
the earliest the task's "today" can be. A date is a value whose field name has a word of DATE_WORDS and whose field —
or any object it sits in (`start` of `{"start": {"dateTime": ...}}`) — has no word of NOT_TODAY (a planned, due or
scheduled time is not "today") and no word of SYSTEM_WORDS (a record system's audit stamp — QuickBooks' MetaData,
Xero's UpdatedDateUTC — is the clock's, not the data's: finance-v2's three anchored real_date failures all read one
of those). A date on or after the worker's own day is the clock's too and is never taken: the data cannot hold today
(docs/design/HARNESS.md §7.1). Dates are read from every read result, before and after the first write; a write's
own result is never read (the world stamps what the agent creates with the real clock).

Ported from StarHarness's triage before the first write and SWE-agent's review_on_submit. The ledger is accumulated at
every after_tool because the view keeps only the latest 20 broker events and a hook sees 4,000 characters of a result.
The state is this module's: the agent loads each hook module once per attempt, so it starts empty in every attempt.
"""
import datetime
import email.utils
import json
import re

TRIAGE = True
VERIFY = True
DATE_ANCHOR = True
LEDGER_BODY_CHARS = 200
READ_POST = re.compile(r"[/:](search|searchStream|query|find)$")     # write-method routes that only read
READ_METHODS = ("GET", "HEAD")
FETCH_TOOLS = {"api_fetch": "", "http_fetch": "GET"}                  # tool -> the method a call without one has
PLAN_TOOL = "plan_changes"
MESSAGE_CHARS = 2000                  # the verify message at most
LINE_CHARS = 200                      # of one plan, ledger or excluded line in the verify message

TRIAGE_MESSAGE = ("Before your first change, call plan_changes: list every change the request requires (each record "
                  "to create or update, each message and every recipient, values copied verbatim from the source), "
                  "and in `excluded` every entity or item that must not be changed, contacted or mentioned. Then "
                  "issue the writes.")
DATE_MESSAGE = ("The newest date in the data you have read is {date} (field {field}); the task's today is on or "
                "after it. A date attached outside the conversation is not the task's date.")
VERIFY_MESSAGE = ("Before you finish, check in both directions what the request requires against what you did: a "
                  "required change that is missing or wrong — make it now; a change you made that the request, the "
                  "data or a policy says not to make, or that touches an excluded item — say so; do not add changes "
                  "beyond the request.")
EMPTY_LEDGER = "no change was made; if the request asks for one, make it"

# Date fields, by the words of their name (internalDate -> internal, date; created_at -> created, at). The _42 lists
# are MEMORY_AGENT_ITERATION §4.2's; the others add past-tense record times (received, updated, modified, time) and
# names of planned times, and are what the guard uses.
DATE_WORDS_42 = {"date", "sent", "created", "ts", "timestamp"}
NOT_TODAY_42 = {"due", "expires", "end"}
DATE_WORDS = DATE_WORDS_42 | {"time", "datetime", "received"}
# A record system's own stamps: set by the system's clock when a record is made or touched, so in a simulated world
# they carry the real date (HARNESS §7.1). `created` is kept as a business date on its own (a ticket's created date),
# but any such field under a `MetaData`-like object is the stamp's.
SYSTEM_WORDS = {"meta", "metadata", "updated", "modified", "edited", "lastupdated", "lastmodified", "lastedited",
                "sync", "synced", "indexed", "audit"}
NOT_TODAY = NOT_TODAY_42 | {"expire", "expiry", "expiration", "ends", "until", "deadline", "scheduled", "start",
                            "starts", "next", "renewal", "renews", "close", "closing", "publish", "planned", "target",
                            "expected", "estimated", "eta", "effective", "remind", "reminder", "follow", "followup",
                            "warn", "disable", "send", "event", "meeting", "appointment", "launch", "release"}
EPOCH_WORDS = {"ts", "timestamp", "internaldate", "time", "date"}    # names whose numbers may be epoch seconds or ms
# JSON read leniently, token by token, so that a result cut anywhere (a hook sees 4,000 characters) yields what it shows
TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\]:,]|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?')
NUMBER = re.compile(r"\d{9,13}(?:\.\d+)?")
ISO = re.compile(r"^(\d{4})[-/](\d{2})[-/](\d{2})")
WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")

STATE = {}


def reset() -> None:
    """A fresh attempt's state (the module is loaded once per attempt; tests call this between cases)."""
    STATE.clear()
    STATE.update({"writes": {}, "order": [], "seen": set(), "plan": None, "date": None, "triaged": False,
                  "verified": False, "own": 0, "anchored": None, "notes": {}, "today": None})


reset()


# ---------------------------------------------------------------------------- what is a write, what is a date
def _route(url) -> str:
    return str(url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")


def is_write(tool, args) -> bool:
    """A fetch whose method is not GET or HEAD, unless its route only reads (READ_POST)."""
    if tool not in FETCH_TOOLS or not isinstance(args, dict):
        return False
    method = str(args.get("method") or FETCH_TOOLS[tool]).upper()
    return method not in READ_METHODS and not READ_POST.search(_route(args.get("url")))


def _words(key: str) -> list[str]:
    return [w.lower() for w in WORD.findall(key)]


def _date_of(key: str, text, number, *, within=(), words_of=DATE_WORDS, not_today=NOT_TODAY) -> str | None:
    """The day a field holds, when its name says it is a record's time: `key` the field, `within` the keys of the
    objects it sits in (outermost first)."""
    words = _words(key)
    if not words or not_today & set(words) or SYSTEM_WORDS & set(words):
        return None
    if any((not_today | SYSTEM_WORDS) & set(_words(k)) for k in within):
        return None
    joined = "".join(words)
    if not (words_of & set(words) or joined in words_of or (words[-1] == "at" and len(words) > 1)):
        return None
    day = None
    if text is not None:
        m = ISO.match(text.strip())
        if m:
            try:
                day = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        elif re.fullmatch(r"\d{9,13}(\.\d+)?", text.strip()):
            number = text.strip()
        else:
            try:
                day = email.utils.parsedate_to_datetime(text).date()
            except (TypeError, ValueError, IndexError):
                return None
    if day is None and number is not None:
        if not (EPOCH_WORDS & set(words) or joined in EPOCH_WORDS):
            return None
        seconds = float(number)
        if seconds > 1e11:                                   # milliseconds (Gmail's internalDate)
            seconds /= 1000
        try:
            day = datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    if day is None or not 2000 <= day.year <= 2100:
        return None
    if day >= _today():                 # the clock's, not the data's (HARNESS §7.1)
        return None
    return day.isoformat()


def _today() -> datetime.date:
    return STATE.get("today") or datetime.date.today()


def _string(tok: str):
    try:
        value = json.loads(tok)
    except ValueError:
        return None
    return value if isinstance(value, str) else None


def fields(text):
    """(within, key, text, number) for every string or number value of an object member in JSON text, `within` the
    keys of the objects and arrays it sits in, outermost first (an array's items sit under the array's key).
    Lenient: a cut text, or one that starts inside an object, yields the members it shows (the top level reads as an
    object); a string value is 4 to 64 characters, a number 9 to 13 digits, as dates are."""
    stack = []                      # [kind, the key it sits under] per open container
    key = candidate = None          # the member's key once its ':' is read; a string where a key may be
    for m in TOKEN.finditer(str(text or "")):
        tok = m.group()
        top = stack[-1] if stack else ["{", None]
        if tok in ("{", "["):
            stack.append([tok, key if top[0] == "{" else top[1]])
            key = candidate = None
        elif tok in ("}", "]"):
            if stack:
                stack.pop()
            key = candidate = None
        elif tok == ":":
            key, candidate = (_string(candidate) if candidate else None), None
        elif tok == ",":
            key = candidate = None
        elif top[0] == "{" and key is None:
            candidate = tok if tok.startswith('"') else None
        else:
            name = key if top[0] == "{" else top[1]
            key = None
            if not name:
                continue
            within = tuple(k for _, k in (stack if top[0] == "{" else stack[:-1]) if k)
            if tok.startswith('"'):
                value = _string(tok)
                if value is not None and 4 <= len(value) <= 64:
                    yield within, name, value, None
            elif NUMBER.fullmatch(tok):
                yield within, name, None, tok


def newest_date(text, *, words=DATE_WORDS, not_today=NOT_TODAY, nested=True) -> tuple[str, str] | None:
    """(the newest date, its field) among the date fields of a tool result. `nested=False` reads each field by its
    own name alone (MEMORY_AGENT_ITERATION §4.2's rule; with DATE_WORDS_42 and NOT_TODAY_42 it is §4.2's reading),
    for E0(a)'s comparison."""
    best = None
    for within, key, value, number in fields(text):
        day = _date_of(key, value, number, within=within if nested else (), words_of=words, not_today=not_today)
        if day is not None and (best is None or day > best[0]):
            best = (day, key)
    return best


def _note_date(text) -> None:
    """The newest date in a read result (a write's own result never comes here; see the module's docstring)."""
    found = newest_date(text)
    if found is not None and (STATE["date"] is None or found[0] > STATE["date"][0]):
        STATE["date"] = found


def anchor_message() -> str:
    return DATE_MESSAGE.format(date=STATE["date"][0], field=STATE["date"][1])


# ---------------------------------------------------------------------------- the ledger
def _status(error) -> str:
    return "ok" if not error else "error: " + " ".join(str(error).split())[:80]


def _record(call_id: str, tool, args: dict, error) -> None:
    if call_id in STATE["writes"]:
        return
    STATE["writes"][call_id] = {"tool": tool, "method": str(args.get("method") or FETCH_TOOLS.get(tool, "")).upper(),
                                "url": str(args.get("url") or "")[:300], "status": _status(error),
                                "body": str(args.get("body") if args.get("body") is not None else "")[:LEDGER_BODY_CHARS]}
    STATE["order"].append(call_id)


def _plan(args: dict) -> dict:
    changes = [c for c in args.get("changes") or () if isinstance(c, dict)]
    return {"changes": [{"content": str(c.get("content") or ""), "status": str(c.get("status") or "pending")}
                        for c in changes],
            "excluded": [str(x) for x in args.get("excluded") or () if isinstance(x, (str, int, float))]}


def ledger(ctx, event):
    view = ctx.view
    base = {t.get("name") for t in view.get("base_tools") or () if isinstance(t, dict)}
    tool, args = event.get("name"), event.get("args") if isinstance(event.get("args"), dict) else {}
    for e in view.get("events") or ():
        # the broker's calls, the agent's own and those a package tool made through it, by the broker's call id
        call_id = e.get("call_id") if isinstance(e, dict) else None
        if not call_id or call_id in STATE["seen"]:
            continue
        STATE["seen"].add(call_id)
        args_e = e.get("args") if isinstance(e.get("args"), dict) else {}
        if is_write(e.get("name"), args_e):
            _record(call_id, e.get("name"), args_e, e.get("error"))
        else:
            _note_date(e.get("result_excerpt"))
    if tool == PLAN_TOOL and not event.get("is_error"):
        STATE["plan"] = _plan(args)
    elif tool not in base and is_write(tool, args):
        # a call the broker never saw (http_fetch goes through the egress proxy): the event is its record
        STATE["own"] += 1
        _record(f"{tool}:{event.get('turn')}:{STATE['own']}", tool, args,
                str(event.get("output") or "")[:80] if event.get("is_error") else "")
    if not is_write(tool, args):
        # this call's result (a write's own result is never read: the world stamps what the agent makes)
        _note_date(event.get("output"))
    return None


# ---------------------------------------------------------------------------- triage and verify
def triage(ctx, event):
    if not TRIAGE or STATE["triaged"] or STATE["plan"] is not None:
        return None
    if not is_write(event.get("name"), event.get("args")):
        return None
    STATE["triaged"] = True
    return {"block": {"reason": TRIAGE_MESSAGE}}


def _conversation(messages: list) -> str:
    """Which conversation a call belongs to (the attempt's own or a sub-agent's): its first message."""
    first = messages[0] if messages else {}
    return f"{first.get('role')}:{str(first.get('content') or '')[:200]}"


def anchor(ctx, event):
    """The dates read so far, each a note at the place it was first given, in every act call; at the end of a hook's aux
    call (never a summary's: the adapter does not pass those here)."""
    if not DATE_ANCHOR or STATE["date"] is None or event.get("role") not in ("act", "aux"):
        return None
    messages = [dict(m) for m in event.get("messages") or ()]
    if event.get("role") == "aux":          # a hook's audit: its own conversation, the newest date's note at its end
        return {"messages": messages + [{"role": "user", "content": anchor_message()}]}
    notes = STATE["notes"].setdefault(_conversation(messages), [])
    if notes and notes[-1][0] > len(messages):         # a summary shortened the history: start over at its end
        notes.clear()
    text = anchor_message()
    if not notes or notes[-1][1] != text:
        notes.append((len(messages), text))
    STATE["anchored"] = STATE["anchored"] or STATE["date"]
    for index, note in reversed(notes):
        messages.insert(index, {"role": "user", "content": note})
    return {"messages": messages}


def _line(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= LINE_CHARS else text[:LINE_CHARS - 3] + "..."


def _section(title: str, lines: list[str], room: int) -> str:
    """A titled list within `room` characters: the lines that do not fit are counted at its end."""
    out, used = [title], len(title)
    for i, line in enumerate(lines):
        rest = f"  ... and {len(lines) - i} more"
        last = i == len(lines) - 1
        if used + 1 + len(line) + (0 if last else 1 + len(rest)) > room:
            out.append(rest)
            break
        out.append(line)
        used += 1 + len(line)
    return "\n".join(out)


def verify_message() -> str:
    plan = STATE["plan"]
    writes = [STATE["writes"][c] for c in STATE["order"]]
    planned = [_line(f"{i}. [{c['status']}] {c['content']}") for i, c in enumerate((plan or {}).get("changes") or (), 1)]
    excluded = [_line(f"- {x}") for x in (plan or {}).get("excluded") or ()]
    made = [_line(f"{i}. {w['method']} {w['url']} -> {w['status']}" + (f"; body: {w['body']}" if w["body"] else ""))
            for i, w in enumerate(writes, 1)]
    head = VERIFY_MESSAGE + (f" ({EMPTY_LEDGER}.)" if not writes else "")
    room = MESSAGE_CHARS - len(head) - 8
    parts = [head,
             _section("Changes you planned (plan_changes):", planned or ["none recorded"], room * 2 // 5),
             _section("Items you excluded:", excluded or ["none recorded"], room // 5),
             _section("Changes you made (from your tool calls):", made or ["none"], room * 2 // 5)]
    return "\n\n".join(parts)[:MESSAGE_CHARS]


def verify(ctx, event):
    if not VERIFY or STATE["verified"]:
        return None
    STATE["verified"] = True
    return {"pushback": verify_message()}


def snapshot() -> dict:
    """The guard's state as data (for tests and for R reading what the guard saw)."""
    return {"writes": [dict(STATE["writes"][c], call_id=c) for c in STATE["order"]], "plan": STATE["plan"],
            "date": STATE["date"], "anchored": STATE["anchored"], "triaged": STATE["triaged"],
            "verified": STATE["verified"]}


