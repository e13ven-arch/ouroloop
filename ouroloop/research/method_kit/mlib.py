"""tools/_mlib.py: the method library's shared helpers (kit template; docs/design/V0_5_METHOD_LIBRARY.md §2.6).

Standard library only, so the same bytes run in the worker. Everything here reads: a method returns evidence with
provenance and an `unclassified` part, and never writes — in a v5 attempt the host refuses a writing call from any
`m_` tool anyway. R may extend its copy of this file; the router does not depend on anything here.

The attempt's journal (§2.2 rule 6) is a file in the loaded bundle's own directory: the worker loads every attempt
into a fresh directory, so the journal lives exactly as long as the attempt, and every module that loads this file
(the actor's tool call and the router's own run of a method load it separately) reads the same journal.
"""
import base64
import hashlib
import json
import os
import re

MAX_OUTPUT = 20000
JOURNAL = ".m_journal.json"
# what analysis methods decided (excluded rows, superseded values), what verification must find in an outbound body
# (required tokens: computed totals, '<N> <noun>' counts, full names), what was written (the router adds each write)
SECTIONS = ("excluded", "superseded", "required", "writes", "notes")
JOURNAL_KEEP = 200
NUMBER = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?%?")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9'_-]+")


# ---------------------------------------------------------------------------- the attempt's journal
def bundle_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def journal_path() -> str:
    return os.path.join(bundle_root(), JOURNAL)


def journal() -> dict:
    """The journal of this attempt: {section: [item, ...]}; empty sections when nothing was written yet."""
    try:
        with open(journal_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {s: [x for x in data.get(s) or () if x is not None] for s in SECTIONS}


def journal_add(section: str, items) -> int:
    """Append items to a section, each once; -> the section's size. The journal never holds more than JOURNAL_KEEP
    items a section (the oldest go first)."""
    if section not in SECTIONS:
        raise ValueError(f"journal section {section!r}: one of {list(SECTIONS)}")
    data = journal()
    seen = {json.dumps(x, sort_keys=True, default=str) for x in data[section]}
    for item in items or ():
        key = json.dumps(item, sort_keys=True, default=str)
        if key not in seen:
            data[section].append(item)
            seen.add(key)
    data[section] = data[section][-JOURNAL_KEEP:]
    tmp = journal_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, default=str)
    os.replace(tmp, journal_path())
    return len(data[section])


def journal_size() -> int:
    """How many decisions the journal holds that a verification method can check against."""
    j = journal()
    return sum(len(j[s]) for s in ("excluded", "superseded", "required"))


# ---------------------------------------------------------------------------- reading the world
def parse(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def call(ctx, name: str, args: dict):
    """-> (output text, is_error) of one base call through the broker."""
    res = ctx.call_tool(name, args)
    return str(getattr(res, "output", "") or ""), bool(getattr(res, "is_error", False))


def is_error_payload(data) -> bool:
    return isinstance(data, dict) and ("error" in data and data.get("error") not in (None, False, "", {}))


def get(ctx, url: str, params=None):
    """A GET through api_fetch -> (parsed JSON or None, is_error, raw text)."""
    args = {"method": "GET", "url": url}
    if params:
        args["params"] = json.dumps(params)
    out, err = call(ctx, "api_fetch", args)
    data = parse(out)
    return data, bool(err or is_error_payload(data)), out


def status_of(data, err: bool) -> str:
    """ok | unauthorized | not_found | error, from a fetch's answer."""
    if not err:
        return "ok"
    code = None
    if isinstance(data, dict):
        e = data.get("error")
        code = e.get("code") if isinstance(e, dict) else data.get("status")
    text = json.dumps(data) if data is not None else ""
    if code == 401 or "401" in text or "not connected" in text.lower() or "no account" in text.lower():
        return "unauthorized"
    if code == 404 or "404" in text:
        return "not_found"
    return "error"


def endpoints(ctx, query: str, top_k: int = 8, method: str = "GET") -> list:
    """The endpoints api_search ranks for a query, of one HTTP method: [{method, url, description, id}]."""
    out, err = call(ctx, "api_search", {"query": query, "top_k": top_k})
    data = parse(out)
    items = data.get("results") if isinstance(data, dict) else data
    found = []
    for e in items if isinstance(items, list) else ():
        if isinstance(e, dict) and str(e.get("method", "")).upper() == method and isinstance(e.get("url"), str):
            found.append({"method": method, "url": e["url"], "id": e.get("id"),
                          "description": str(e.get("description") or "")[:200]})
    return found


def records_of(data) -> list:
    """The records inside an API answer, as dicts: a sheet's `values` (the first row is the header), or the first
    list of objects found under the usual keys (or anywhere, breadth first)."""
    if isinstance(data, dict) and isinstance(data.get("values"), list) and data["values"] \
            and all(isinstance(r, list) for r in data["values"]):
        header = [str(h) for h in data["values"][0]]
        return [{header[i] if i < len(header) else f"col{i}": v for i, v in enumerate(row)}
                for row in data["values"][1:]]
    queue = [data]
    while queue:
        item = queue.pop(0)
        if isinstance(item, list) and item and all(isinstance(x, dict) for x in item):
            return list(item)
        if isinstance(item, dict):
            for key in ("results", "items", "records", "rows", "data", "files", "messages", "entries", "pages",
                        "value", "elements", "invoices", "bills", "tickets", "contacts"):
                if key in item:
                    queue.append(item[key])
            queue.extend(v for k, v in item.items() if isinstance(v, (list, dict)))
    return []


def text_of(value) -> str:
    """A value as searchable text."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def decode_b64(text: str) -> str:
    """base64 or base64url text decoded as UTF-8, or '' when it is not."""
    if not isinstance(text, str) or not text:
        return ""
    s = text.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


# ---------------------------------------------------------------------------- what a call does
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
OUTBOUND_HINTS = ("messages/send", "chat.postMessage", "/send", "drafts/send", "/notes", "/comments", "/replies")
CATEGORICAL_KEYS = ("status", "stage", "label", "labels", "category", "state", "priority", "type", "tags", "tag")


def http_method(args) -> str:
    return str((args or {}).get("method") or "GET").upper()


def is_write(tool: str, args) -> bool:
    return tool == "api_fetch" and http_method(args) in WRITE_METHODS


def body_of(args):
    body = (args or {}).get("body")
    data = parse(body) if isinstance(body, str) else body
    return data if isinstance(data, dict) else {}


def is_outbound(tool: str, args) -> bool:
    """A write that sends text to a person: a mail send, a chat post, a note or comment."""
    if not is_write(tool, args):
        return False
    url = str((args or {}).get("url") or "")
    return any(h in url for h in OUTBOUND_HINTS)


def outbound_text(args) -> str:
    """The human-readable text an outbound write carries: a mail's decoded raw body or its fields, a chat message's
    text, a note's body."""
    body = body_of(args)
    parts = []
    raw = body.get("raw")
    if isinstance(raw, str):
        parts.append(decode_b64(raw))
    for key in ("text", "body", "content", "message", "html", "subject", "note", "comment"):
        v = body.get(key)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            parts.append(text_of(v))
    return "\n".join(p for p in parts if p)


def categorical_fields(args) -> dict:
    """The categorical fields a write sets: {key: value} for keys like status, stage, label."""
    body = body_of(args)
    out = {}

    def walk(d, depth=0):
        if depth > 3 or not isinstance(d, dict):
            return
        for k, v in d.items():
            if str(k).lower() in CATEGORICAL_KEYS and isinstance(v, (str, list)):
                out[str(k)] = v
            elif isinstance(v, dict):
                walk(v, depth + 1)
    walk(body)
    return out


def target_of(args) -> str:
    """The record a write targets, as METHOD + URL path (no query)."""
    url = str((args or {}).get("url") or "").split("?")[0]
    return f"{http_method(args)} {url}"


# ---------------------------------------------------------------------------- text
def numbers_in(text: str) -> list:
    return [m.group(0) for m in NUMBER.finditer(text or "")]


def norm_number(token: str):
    s = str(token).replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def words(text: str) -> list:
    return [w.lower() for w in WORD.findall(text or "")]


def digest(value) -> str:
    return hashlib.sha256(text_of(value).encode("utf-8")).hexdigest()[:16]


def evidence(value, source: str, record=None, field=None) -> dict:
    """One value with where it came from."""
    out = {"value": value, "source": source}
    if record is not None:
        out["record"] = record
    if field is not None:
        out["field"] = field
    return out


def result(**fields) -> str:
    """A method's answer: JSON, clipped to MAX_OUTPUT characters (an `unclassified` list is always present)."""
    fields.setdefault("unclassified", [])
    text = json.dumps(fields, ensure_ascii=False, default=str)
    if len(text) <= MAX_OUTPUT:
        return text
    for key in sorted(fields, key=lambda k: -len(text_of(fields[k]))):
        if isinstance(fields[key], list) and len(fields[key]) > 5:
            fields[key] = fields[key][:max(5, len(fields[key]) // 2)] + [{"clipped": True}]
            text = json.dumps(fields, ensure_ascii=False, default=str)
            if len(text) <= MAX_OUTPUT:
                return text
    return text[:MAX_OUTPUT]


def usage(message: str) -> dict:
    """A wrong-argument answer: never an exception (§2.2 rule 5)."""
    return {"output": "usage: " + message, "is_error": True}
