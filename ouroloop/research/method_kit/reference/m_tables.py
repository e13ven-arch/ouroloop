"""tools/m_tables.py (reference method, analysis family): the row ledger (V0_5_METHOD_LIBRARY §2.4, pattern LEDGER).

Before rows of a table are counted, totalled or acted on, every row is classified include | exclude with its reason:
a note that tells the reader to skip it, a filler or test row, an empty row — by the variant — and what it cannot
classify is listed apart. Counts and totals come from the included rows only; the excluded rows and the computed
totals go to the attempt's journal, so a verification method can check an outbound body against them.
"""
import importlib.util
import os
import re


def _lib():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_mlib.py")
    spec = importlib.util.spec_from_file_location("mlib_" + str(abs(hash(path))), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


L = _lib()

METHODS = {"m_row_ledger": {"family": "analysis", "trigger": "row_ledger_trigger", "kinds": ["table_read"],
                            "on": "recommend", "footprint": ["value", "not_contains"], "split": "noise_rows",
                            "variants": ["notes_only", "notes_and_noise", "notes_noise_empty"],
                            "default_variant": "notes_and_noise"}}
MIN_ROWS = 8
DIRECTIVES = re.compile(r"\b(skip|exclude|excluded|ignore|void|voided|duplicate|cancel+ed|withdrawn|remove|"
                        r"do not (?:count|include|pay|use|send)|not applicable|n/a)\b", re.IGNORECASE)
NOISE = re.compile(r"\b(test|testing|dummy|sample|placeholder|noise|filler|example|lorem)\b", re.IGNORECASE)
NOTE_KEYS = ("note", "notes", "comment", "comments", "remark", "remarks", "memo", "status note")
NOTE_WORDS = re.compile(r"[A-Za-z]{3,}")


def _notes(row: dict) -> str:
    return " ".join(str(v) for k, v in row.items() if str(k).strip().lower() in NOTE_KEYS and v not in (None, ""))


def _key(row: dict, key):
    if key and key in row:
        return str(row[key])
    for k, v in row.items():
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _vocabulary(rows) -> dict:
    """The categorical columns of a table and their values: few distinct short strings over many rows."""
    out = {}
    columns = {k for r in rows for k in r}
    for col in sorted(columns, key=str):
        values = [r.get(col) for r in rows if isinstance(r.get(col), str) and r.get(col).strip()]
        distinct = sorted(set(values))
        if len(values) >= 4 and 1 < len(distinct) <= 12 and all(len(v) <= 30 for v in distinct) \
                and len(distinct) < len(values):
            out[str(col)] = distinct
    return out


def _empty(row: dict) -> bool:
    return all(v in (None, "", [], {}) for v in row.values())


def classify(rows, *, key=None, variant="notes_and_noise"):
    """-> (decisions, unclassified): one decision per row {row, key, decision, reason}."""
    decisions, unclassified = [], []
    for i, row in enumerate(rows, start=1):
        k = _key(row, key)
        note = _notes(row)
        if variant == "notes_noise_empty" and _empty(row):
            decisions.append({"row": i, "key": k, "decision": "exclude", "reason": "empty row"})
            continue
        if note and DIRECTIVES.search(note):
            decisions.append({"row": i, "key": k, "decision": "exclude",
                              "reason": f"note: {DIRECTIVES.search(note).group(0)}"})
            continue
        if variant in ("notes_and_noise", "notes_noise_empty") and NOISE.search(k):
            decisions.append({"row": i, "key": k, "decision": "exclude", "reason": "filler or test row"})
            continue
        if note and NOTE_WORDS.search(note):
            unclassified.append({"row": i, "key": k, "note": note[:120]})
        decisions.append({"row": i, "key": k, "decision": "include", "reason": ""})
    return decisions, unclassified


def row_ledger(ctx, args):
    url = (args or {}).get("url")
    if not isinstance(url, str) or not url:
        return L.usage("m_row_ledger {url: the table's GET url, params?: {...}, key?: column, "
                       "amount_columns?: [column], variant?: notes_only|notes_and_noise|notes_noise_empty}")
    variant = args.get("variant") if args.get("variant") in METHODS["m_row_ledger"]["variants"] \
        else METHODS["m_row_ledger"]["default_variant"]
    data, err, raw = L.get(ctx, url, args.get("params") if isinstance(args.get("params"), dict) else None)
    if err:
        return {"output": L.result(source=url, status=L.status_of(data, err), error=raw[:300]), "is_error": True}
    rows = L.records_of(data)
    decisions, unclassified = classify(rows, key=args.get("key"), variant=variant)
    amounts = [c for c in args.get("amount_columns") or () if isinstance(c, str)]
    included = [rows[d["row"] - 1] for d in decisions if d["decision"] == "include"]
    totals = {}
    for col in amounts:
        values = [L.norm_number(r.get(col)) for r in included if r.get(col) not in (None, "")]
        totals[col] = round(sum(v for v in values if v is not None), 2)
    excluded = [d for d in decisions if d["decision"] == "exclude"]
    L.journal_add("excluded", [{"key": d["key"], "reason": d["reason"], "source": url} for d in excluded])
    vocab = _vocabulary(rows)
    if vocab:
        L.journal_add("notes", [{"vocab": vocab, "source": url}])
    L.journal_add("required", [{"token": f"{len(included)}", "what": "included row count", "source": url}]
                  + [{"token": f"{v:,.2f}", "what": f"total of {c}", "source": url} for c, v in totals.items()])
    return L.result(source=url, variant=variant, rows=len(rows), included=len(included), excluded=excluded,
                    totals=totals, decisions=decisions[:200], unclassified=unclassified,
                    summary=(f"{len(rows)} rows: {len(included)} included, {len(excluded)} excluded "
                             + "; ".join(f"{d['key'][:40]} ({d['reason']})" for d in excluded[:8])
                             + (f"; totals {totals}" if totals else "")))


def _rows_seen(text: str):
    """(rows, noise rows) of an answer the hook saw; a hook sees at most 4,000 characters of a result, so a table
    cut short is counted by its row separators."""
    rows = L.records_of(L.parse(text))
    if rows:
        return len(rows), sum(1 for r in rows if NOISE.search(_key(r, None)))
    approx = max(text.count("], ["), text.count("],["), text.count("}, {"), text.count("},{"))
    return (approx + 1 if approx else 0), len(NOISE.findall(text))


def row_ledger_trigger(event, view):
    """A GET that answered a table of at least MIN_ROWS rows."""
    if (event or {}).get("tool") != "api_fetch" or event.get("is_error"):
        return None
    args = event.get("args") or {}
    if L.http_method(args) != "GET":
        return None
    n, noise = _rows_seen(str(event.get("result") or ""))
    if n < MIN_ROWS:
        return None
    params = L.parse(args.get("params")) if isinstance(args.get("params"), str) else args.get("params")
    return {"kind": "table_read", "args": {"url": args.get("url"), **({"params": params} if params else {})},
            "context": {"noise_rows": noise, "rows": n}}
