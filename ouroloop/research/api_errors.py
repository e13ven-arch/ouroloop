"""R's interface-error table, `inputs/api_errors.json` (docs/design/V0_4_PLAN.md §1.2; docs/modules/04_RESEARCH_LOOP.md
§1 R 的输入与准入, v0.4): where the agent fights the API, counted by fixed code over the host-written trajectories of
the activity's train attempts — and nothing else.

Per endpoint (`METHOD host/path` for a call that names a URL, the tool's name for any other tool): the calls, how many
tasks made them, how many fell in failed attempts, the error responses and their kinds, the retries (a call right
after an error on the same endpoint), the responses that said a next page exists when the agent never called the
endpoint again, and the empty results. A path segment is kept only when it is the API's vocabulary — no digit (a
version such as `v1` aside) and seen in the calls of at least two tasks — and is `{id}` otherwise; an error's kind is
its status code and its message with numbers, quoted values, e-mail addresses and path ids replaced. No task id and no
argument value is in the table, and only train attempts are read (the caller hands train rows; any other split is
refused here as well).

Why a table and not more notes: StarHarness's held-out gain on AutomationBench Finance (+29.3) came from fixes at the
tool interface, and none of v0.3's nine candidates touched a tool; the notes summarise one failure at a time, so a
cluster of errors on one endpoint across tasks is what they cannot show.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from .diagnosis import TRAIN, read_transcript

MAX_ENDPOINTS = 40               # rows the table keeps, the most troubled first
MAX_KINDS = 5                    # error kinds kept per endpoint, the most frequent first
MIN_TASKS_FOR_VOCABULARY = 2     # a path segment seen in fewer tasks' calls is an id
VERSION = re.compile(r"v\d+(\.\d+)*")
PAGE_TOKENS = ("nextPageToken", "next_page_token", "nextCursor", "next_cursor", "nextUri", "nextLink",
               "@odata.nextLink", "next_page", "next_page_url", "next")
TOTALS = ("total", "totalSize", "total_count", "total_records", "totalResultsCount", "total_items", "total_size",
          "resultSizeEstimate")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
PATHISH = re.compile(r"[\w.:-]*/[\w./{}:-]*")


def _calls(transcript: list[dict]) -> list[dict]:
    """The attempt's tool calls in order: {tool, args, output, is_error}. The args come from the broker's event (the
    runner shell, v0.3) or from the assistant's tool call (the v1 loop), matched to the `tool` event by id."""
    args: dict = {}
    for e in transcript:
        if e.get("kind") == "broker_event" and e.get("event_id"):
            args[e["event_id"]] = (e.get("tool"), e.get("args"))
        elif e.get("kind") == "assistant":
            for c in e.get("tool_calls") or ():
                if isinstance(c, dict) and c.get("id"):
                    args.setdefault(c["id"], (c.get("name"), c.get("args")))
    out = []
    for e in transcript:
        if e.get("kind") != "tool":
            continue
        name, given = args.get(e.get("id"), (e.get("name"), None))
        out.append({"tool": e.get("name") or name, "args": given if isinstance(given, dict) else {},
                    "output": e.get("output") if isinstance(e.get("output"), str) else "",
                    "is_error": bool(e.get("is_error"))})
    return out


def _url_parts(call: dict) -> tuple[str, str, list[str]] | None:
    """(METHOD, host, path segments) of a call that names a URL, else None — also for a URL urlsplit refuses (a
    bracketed placeholder host such as `https://[your-domain].zendesk.com/...`, review 2026-10-06): such a call is
    counted under its tool's name."""
    url = call["args"].get("url")
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        host = parts.netloc.lower()
    except ValueError:
        return None
    method = str(call["args"].get("method") or "GET").upper()
    return method, host, [s for s in parts.path.split("/") if s]


def _segment(seg: str, vocabulary: set) -> str:
    if VERSION.fullmatch(seg):
        return seg
    if any(ch.isdigit() for ch in seg) or seg not in vocabulary:
        return "{id}"
    return seg


def endpoint_of(call: dict, vocabulary: set) -> str:
    """The endpoint a call reached: `METHOD host/path` with ids as `{id}`, or the tool's name."""
    parts = _url_parts(call)
    if parts is None:
        return str(call["tool"] or "unknown")
    method, host, segments = parts
    return f"{method} {host}/" + "/".join(_segment(s, vocabulary) for s in segments)


def _normalise(text: str, vocabulary: set) -> str:
    text = EMAIL.sub("<email>", text)
    text = QUOTED.sub("'…'", text)
    text = PATHISH.sub(lambda m: "/".join(_segment(s, vocabulary) if s else s for s in m.group(0).split("/")), text)
    return re.sub(r"\d+", "#", text).strip()[:120]


def _parsed(output: str):
    try:
        return json.loads(output)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def error_kind(call: dict, vocabulary: set) -> str | None:
    """The kind of an error response — `<code> <message>` with values replaced — or None when the call did not fail."""
    data = _parsed(call["output"])
    err = data.get("error") if isinstance(data, dict) else None
    if err is None and not call["is_error"]:
        return None
    if isinstance(err, dict):
        code = err.get("code") or err.get("status") or "error"
        message = err.get("message") or err.get("detail") or ""
        return f"{code} {_normalise(str(message), vocabulary)}".strip()
    if isinstance(err, str):
        return f"error {_normalise(err, vocabulary)}".strip()
    return f"error {_normalise(call['output'], vocabulary)}".strip()


def _items(data: dict) -> list | None:
    lists = [v for v in data.values() if isinstance(v, list)]
    return max(lists, key=len) if lists else None


def more_pages(call: dict) -> bool:
    """Whether a response says there is more to read: a has_more flag, a next-page token or link, a page number below
    the page count, or a total above the items returned."""
    data = _parsed(call["output"])
    if not isinstance(data, dict):
        return False
    if data.get("has_more") is True or data.get("hasMore") is True:
        return True
    if any(isinstance(data.get(k), (str, int)) and data.get(k) not in ("", 0) for k in PAGE_TOKENS):
        return True
    page, count = data.get("page_number") or data.get("page"), data.get("page_count") or data.get("pageCount")
    if isinstance(page, int) and isinstance(count, int) and page < count:
        return True
    items = _items(data)
    total = next((data[k] for k in TOTALS if isinstance(data.get(k), int) and not isinstance(data.get(k), bool)), None)
    return items is not None and total is not None and total > len(items)


def empty_result(call: dict) -> bool:
    """Whether a response that is not an error came back empty: an empty list, or an object whose lists are all empty,
    or whose total is 0."""
    data = _parsed(call["output"])
    if isinstance(data, list):
        return not data
    if not isinstance(data, dict) or "error" in data:
        return False
    lists = [v for v in data.values() if isinstance(v, list)]
    if lists:
        return all(not v for v in lists)
    return any(data.get(k) == 0 and not isinstance(data.get(k), bool) for k in TOTALS)


def api_error_table(attempts, transcripts) -> dict:
    """The table over `attempts` (TaskResult rows; only valid train attempts with a transcript are read)."""
    rows = [r for r in attempts or () if r.split in TRAIN and r.validity == "valid" and r.evidence]
    per_attempt = [(r, _calls(read_transcript(transcripts, r.evidence))) for r in rows]
    # the API's vocabulary: path segments seen in the calls of at least two tasks
    seen: dict = {}
    for r, calls in per_attempt:
        for c in calls:
            parts = _url_parts(c)
            for s in (parts[2] if parts else ()):
                seen.setdefault(s, set()).add(r.task_id)
    vocabulary = {s for s, tasks in seen.items() if len(tasks) >= MIN_TASKS_FOR_VOCABULARY}
    table: dict = {}
    for r, calls in per_attempt:
        ends = [endpoint_of(c, vocabulary) for c in calls]
        last_error: dict = {}
        for i, (c, end) in enumerate(zip(calls, ends)):
            row = table.setdefault(end, {"endpoint": end, "calls": 0, "tasks": set(), "calls_in_failed_attempts": 0,
                                         "errors": 0, "error_kinds": {}, "retries": 0, "unread_pages": 0,
                                         "empty_results": 0})
            row["calls"] += 1
            row["tasks"].add(r.task_id)
            row["calls_in_failed_attempts"] += int(not r.success)
            if last_error.get(end):
                row["retries"] += 1
            kind = error_kind(c, vocabulary)
            last_error[end] = kind is not None
            if kind is not None:
                row["errors"] += 1
                row["error_kinds"][kind] = row["error_kinds"].get(kind, 0) + 1
                continue
            if more_pages(c) and end not in ends[i + 1:]:
                row["unread_pages"] += 1
            if empty_result(c):
                row["empty_results"] += 1
    ordered = sorted(table.values(), key=lambda x: (-x["errors"], -x["retries"], -x["unread_pages"],
                                                    -x["empty_results"], -x["calls"], x["endpoint"]))
    endpoints = []
    for x in ordered[:MAX_ENDPOINTS]:
        kinds = sorted(x["error_kinds"].items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_KINDS]
        endpoints.append({**x, "tasks": len(x["tasks"]), "error_kinds": dict(kinds)})
    return {"split": "train", "attempts": len(rows), "tasks": len({r.task_id for r in rows}),
            "failed_attempts": sum(1 for r in rows if not r.success), "endpoints_total": len(table),
            "endpoints": endpoints}
