"""tools/m_verify.py (reference methods, verification family): the pre-send content check and the effect audit
(V0_5_METHOD_LIBRARY §2.4, patterns LIT, SCRUB, VOCAB and EFFECT).

`m_outbound_lint` reads an outbound body (a mail's decoded raw text, a chat message, a note) or a categorical write
before it goes out and checks it against what this attempt's analysis methods decided — the journal: a value they
excluded or superseded must not be in it (SCRUB), a token they computed (a total, a count) should be (LIT), and a
categorical value should be one the attempt saw in its records (VOCAB). It only checks against the journal; with an
empty journal it has nothing grounded to check and says so, which is why the router fires it on a send only when the
journal holds a decision (the lesson of a broad pre-send checklist that broke passing tasks).

`m_effect_audit` reads back every non-message write the attempt made (the router records each in the journal) and
reports a target that cannot be read back or a field that is not there (EFFECT).
"""
import importlib.util
import os


def _lib():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_mlib.py")
    spec = importlib.util.spec_from_file_location("mlib_" + str(abs(hash(path))), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


L = _lib()

METHODS = {
    "m_outbound_lint": {"family": "verification", "trigger": "lint_trigger",
                        "kinds": ["outbound_send", "categorical_write"], "on": "block_once",
                        "footprint": ["body_contains", "not_contains", "categorical"]},
    "m_effect_audit": {"family": "verification", "trigger": "audit_trigger", "kinds": ["write_no_readback"],
                       "on": "pushback", "footprint": ["exists", "value"]},
}
MIN_TOKEN = 3


def _present(token: str, text: str) -> bool:
    token = str(token).strip()
    if not token:
        return False
    if token.lower() in text.lower():
        return True
    num = L.norm_number(token)
    return num is not None and any(L.norm_number(n) == num for n in L.numbers_in(text))


def outbound_lint(ctx, args):
    tool, call = (args or {}).get("tool"), (args or {}).get("args")
    if not isinstance(call, dict):
        return L.usage("m_outbound_lint {tool: the tool about to be called, args: its arguments}")
    j = L.journal()
    text = L.outbound_text(call)
    violations, checked = [], {"excluded": 0, "superseded": 0, "required": 0, "categorical": 0}
    for item in j["excluded"]:
        key = str(item.get("key") or item.get("value") or "")
        if len(key) >= MIN_TOKEN and text and key.lower() in text.lower():
            violations.append({"kind": "excluded value in the body", "value": key, "why": item.get("reason")})
        checked["excluded"] += 1
    for item in j["superseded"]:
        value = str(item.get("value") or "")
        if len(value) >= MIN_TOKEN and text and value.lower() in text.lower():
            violations.append({"kind": "superseded value in the body", "value": value})
        checked["superseded"] += 1
    if text and L.is_outbound(tool, call):
        for item in j["required"]:
            token = str(item.get("token") or "")
            if token and not _present(token, text):
                violations.append({"kind": "a computed value is missing", "value": token, "what": item.get("what")})
            checked["required"] += 1
    vocab = {}
    for note in j["notes"]:
        if isinstance(note, dict) and isinstance(note.get("vocab"), dict):
            for field, values in note["vocab"].items():
                vocab.setdefault(str(field).lower(), set()).update(str(v) for v in values or ())
    for field, value in L.categorical_fields(call).items():
        seen = vocab.get(field.lower())
        values = value if isinstance(value, list) else [value]
        for v in values:
            if seen and str(v) not in seen:
                violations.append({"kind": "a categorical value the records do not use", "field": field,
                                   "value": v, "seen": sorted(seen)[:8]})
        checked["categorical"] += 1
    return L.result(tool=tool, violations=violations, checked=checked,
                    grounded=bool(sum(checked.values())),
                    unclassified=[] if text or L.categorical_fields(call) else [{"why": "no readable body"}])


def lint_trigger(event, view):
    """Before an outbound send (mail, chat, note) or a write that sets a categorical field."""
    tool, args = (event or {}).get("tool"), (event or {}).get("args") or {}
    if "result" in (event or {}):
        return None                                   # before the call only
    if L.is_outbound(tool, args):
        return {"kind": "outbound_send", "args": {}, "context": {"journal": L.journal_size()}}
    if L.is_write(tool, args) and L.categorical_fields(args):
        return {"kind": "categorical_write", "args": {}, "context": {"journal": L.journal_size()}}
    return None


def effect_audit(ctx, args):
    writes = [w for w in L.journal()["writes"] if isinstance(w, dict)]
    targets = [w for w in writes if not any(h in str(w.get("target")) for h in L.OUTBOUND_HINTS)]
    discrepancies, checked = [], []
    for w in targets[:12]:
        method, _, url = str(w.get("target") or "").partition(" ")
        if method == "DELETE" or not url:
            continue
        data, err, raw = L.get(ctx, url)
        status = L.status_of(data, err)
        checked.append({"target": url, "status": status})
        if err:
            discrepancies.append({"target": url, "problem": f"cannot be read back ({status})"})
            continue
        text = L.text_of(data)
        missing = [f for f in w.get("fields") or () if f and f not in text]
        if missing:
            discrepancies.append({"target": url, "problem": "fields written are not in the read-back",
                                  "fields": missing[:8]})
    return L.result(discrepancies=discrepancies, checked=checked, writes=len(writes),
                    unclassified=[w for w in targets[12:]])


def audit_trigger(event, view):
    """At finish: a non-message write this attempt made whose target was never read back after it."""
    if (event or {}).get("tool") is not None:
        return None
    writes = [w for w in L.journal()["writes"] if isinstance(w, dict)
              and not any(h in str(w.get("target")) for h in L.OUTBOUND_HINTS)]
    if not writes:
        return None
    reads = [e for e in (view or {}).get("events") or () if e.get("name") == "api_fetch"
             and L.http_method(e.get("args") or {}) == "GET"]
    read_urls = {str((e.get("args") or {}).get("url") or "").split("?")[0] for e in reads}
    unread = [w for w in writes if str(w.get("target")).partition(" ")[2] not in read_urls]
    if not unread:
        return None
    return {"kind": "write_no_readback", "args": {}, "context": {"writes": len(writes), "unread": len(unread)}}
