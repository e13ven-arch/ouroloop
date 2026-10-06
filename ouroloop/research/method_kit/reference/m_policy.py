"""tools/m_policy.py (reference method, version family): the policy timeline (V0_5_METHOD_LIBRARY §2.4, pattern VER).

When two or more documents or messages on one policy topic were read, the actor tends to follow the first one found,
treat a supplement as the whole rule, or miss a later message that lifts a hold. This lists every document and
message on the topic it can reach in date order, marks the newest as current and the earlier ones superseded (variant
`replace`) or, for a supplement, amendment or addendum, as adding to the current one (variant `merge_supplement`), and
lists the later messages that lift, rescind or override something. Superseded documents go to the journal.
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

METHODS = {"m_policy_timeline": {"family": "version", "trigger": "policy_trigger", "kinds": ["policy_hits"],
                                 "on": "recommend", "footprint": ["value", "categorical"],
                                 "variants": ["replace", "merge_supplement"], "default_variant": "replace"}}
TOPIC = re.compile(r"\b(policy|policies|guideline|guidelines|procedure|procedures|rule|rules|playbook|memo|"
                   r"handbook|standard|threshold|approval|approvals)\b", re.IGNORECASE)
SUPPLEMENT = re.compile(r"\b(supplement|supplemental|amendment|addendum|appendix|update to)\b", re.IGNORECASE)
OVERRIDE = re.compile(r"\b(lift|lifted|rescind|rescinded|revoke|revoked|cancel|cancelled|no longer|supersede|"
                      r"supersedes|superseded|replaces|override|overrides|effective immediately)\b", re.IGNORECASE)
DATE_KEYS = ("modifiedTime", "modified", "updated", "updated_at", "lastModified", "createdTime", "created",
             "created_at", "date", "internalDate", "sent", "timestamp", "ts", "effective_date", "version")
SOURCES = (("files", "files list search query name contains"), ("wiki", "pages search content query"),
           ("mail", "messages list search query"))


def _date(rec) -> str:
    for k in DATE_KEYS:
        if isinstance(rec, dict) and rec.get(k) not in (None, ""):
            return str(rec[k])
    return ""


def _title(rec) -> str:
    for k in ("name", "title", "subject", "snippet"):
        if isinstance(rec, dict) and rec.get(k):
            return str(rec[k])
    return L.text_of(rec)[:80]


def timeline(records, *, variant="replace"):
    """-> (entries in date order with a status each, overrides, undated)."""
    dated = [(_date(r), r) for r in records]
    undated = [{"title": _title(r)[:120]} for d, r in dated if not d]
    ordered = sorted([(d, r) for d, r in dated if d], key=lambda x: x[0])
    entries, overrides = [], []
    current = None
    for d, r in reversed(ordered):
        title = _title(r)
        if variant == "merge_supplement" and SUPPLEMENT.search(title):
            status = "supplement"
        elif current is None:
            status, current = "current", title
        else:
            status = "superseded"
        entries.append({"title": title[:120], "date": d, "status": status,
                        "record": next((str(r[k]) for k in ("id", "fileId", "messageId") if r.get(k)), "")})
        if OVERRIDE.search(L.text_of(r)):
            overrides.append({"title": title[:120], "date": d, "says": OVERRIDE.search(L.text_of(r)).group(0)})
    return list(reversed(entries)), overrides, undated


def policy_timeline(ctx, args):
    topic = (args or {}).get("topic") or (args or {}).get("query")
    if not isinstance(topic, str) or not topic.strip():
        return L.usage("m_policy_timeline {topic: the policy's subject words, variant?: replace|merge_supplement}")
    variant = args.get("variant") if args.get("variant") in METHODS["m_policy_timeline"]["variants"] else "replace"
    records, unreachable = [], []
    for source, words in SOURCES:
        found = L.endpoints(ctx, words, top_k=2)
        if not found:
            continue
        data, err, raw = L.get(ctx, found[0]["url"], {"q": topic})
        if err:
            unreachable.append({"source": source, "status": L.status_of(data, err)})
            continue
        terms = [w for w in L.words(topic) if len(w) > 2]
        records += [r for r in L.records_of(data) if all(t in L.text_of(r).lower() for t in terms)]
    entries, overrides, undated = timeline(records, variant=variant)
    L.journal_add("superseded", [{"value": e["title"], "date": e["date"], "reason": "an earlier version"}
                                 for e in entries if e["status"] == "superseded"])
    current = next((e for e in reversed(entries) if e["status"] == "current"), None)
    return L.result(topic=topic, variant=variant, timeline=entries, overrides=overrides, unreachable=unreachable,
                    unclassified=undated,
                    summary=(f"{len(entries)} dated versions of {topic!r}; current: "
                             f"{current['title'] if current else 'none'}"
                             + (f"; later overrides: " + "; ".join(f"{o['date']} {o['says']}" for o in overrides[:4])
                                if overrides else "")))


def policy_trigger(event, view):
    """A read that returned two or more documents or messages on a policy topic."""
    if (event or {}).get("tool") != "api_fetch" or event.get("is_error"):
        return None
    if L.http_method(event.get("args") or {}) != "GET":
        return None
    data = L.parse(str(event.get("result") or ""))
    records = L.records_of(data)
    hits = [r for r in records if TOPIC.search(_title(r))]
    if len(hits) < 2:
        return None
    word = TOPIC.search(_title(hits[0])).group(0).lower()
    return {"kind": "policy_hits", "args": {"topic": word}, "context": {"documents": len(hits)}}
