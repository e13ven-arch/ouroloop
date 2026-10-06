"""tools/m_search.py (reference method, search family): fan a search out over the document stores
(V0_5_METHOD_LIBRARY §2.4, patterns DOC and SRC).

When a policy, precedent or fact search came back empty — or a service answered 401 — the actor tends to stop after
one store. This asks api_search for a listing endpoint of each document store (files, wiki pages, mail, chat, sheets),
calls each with the query, and returns the hits with the store and record they came from, the stores that were not
reachable, and the hits it could not tie to the query apart. One call does what takes the actor 3–6 turns by hand.
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

METHODS = {"m_doc_search": {"family": "search", "trigger": "doc_search_trigger", "kinds": ["doc_miss", "http_401"],
                            "on": "recommend", "footprint": ["value", "body_contains"]}}
STORES = (("files", "files list search query name contains"), ("wiki", "pages search content query"),
          ("mail", "messages list search query"), ("chat", "messages search query"),
          ("sheets", "spreadsheets values get"))
PARAM_NAMES = ("q", "query", "cql", "search")
DOC_HINTS = ("drive", "files", "notion", "confluence", "wiki", "gmail", "messages", "search", "docs", "pages")
MAX_HITS = 8


def _terms(query: str) -> list:
    return [w for w in L.words(query) if len(w) > 2][:8]


def _hits(records, terms, source):
    hits, loose = [], []
    for i, rec in enumerate(records):
        text = L.text_of(rec).lower()
        matched = [t for t in terms if t in text]
        title = next((str(rec[k]) for k in ("name", "title", "subject", "snippet") if isinstance(rec, dict)
                      and rec.get(k)), "")
        rid = next((str(rec[k]) for k in ("id", "fileId", "messageId", "pageId") if isinstance(rec, dict)
                    and rec.get(k)), str(i))
        item = L.evidence(title[:120], source, record=rid)
        if len(matched) == len(terms) and terms:
            hits.append({**item, "matched": matched})
        elif matched:
            loose.append({**item, "matched": matched})
    return hits[:MAX_HITS], loose[:MAX_HITS]


def doc_search(ctx, args):
    query = (args or {}).get("query")
    if not isinstance(query, str) or not query.strip():
        return L.usage("m_doc_search {query: words to look for, stores?: [files|wiki|mail|chat|sheets]}")
    wanted = [s for s in args.get("stores") or () if s in dict(STORES)] or [s for s, _ in STORES]
    terms = _terms(query)
    stores, unclassified, unreachable = [], [], []
    for store, words in STORES:
        if store not in wanted:
            continue
        found = L.endpoints(ctx, words, top_k=3)
        if not found:
            unreachable.append({"store": store, "reason": "no listing endpoint"})
            continue
        ep = found[0]
        data, err, raw = None, True, ""
        for name in PARAM_NAMES[:2]:
            data, err, raw = L.get(ctx, ep["url"], {name: query})
            if not err:
                break
        status = L.status_of(data, err)
        if err:
            unreachable.append({"store": store, "endpoint": ep["url"], "status": status})
            continue
        hits, loose = _hits(L.records_of(data), terms, f"{store}: {ep['url']}")
        stores.append({"store": store, "endpoint": ep["url"], "status": status, "hits": hits})
        unclassified += loose
    total = sum(len(s["hits"]) for s in stores)
    return L.result(query=query, stores=stores, unreachable=unreachable, unclassified=unclassified[:20],
                    summary=(f"{total} hits for {query!r} across {len(stores)} stores"
                             + "".join(f"; {s['store']}: " + ", ".join(h['value'][:40] for h in s['hits'][:3])
                                       for s in stores if s["hits"])
                             + (f"; unreachable: {', '.join(u['store'] for u in unreachable)}" if unreachable
                                else "")))


def doc_search_trigger(event, view):
    """A document or message search that came back empty, or a store that answered 401."""
    if (event or {}).get("tool") != "api_fetch":
        return None
    args = event.get("args") or {}
    url = str(args.get("url") or "").lower()
    text = str(event.get("result") or "")
    data = L.parse(text)
    query = ""
    params = L.parse(args.get("params")) if isinstance(args.get("params"), str) else args.get("params")
    if isinstance(params, dict):
        query = next((str(params[k]) for k in PARAM_NAMES if params.get(k)), "")
    if L.status_of(data, bool(event.get("is_error")) or L.is_error_payload(data)) == "unauthorized":
        return {"kind": "http_401", "args": {"query": query or " ".join(L.words(view.get("task", ""))[:6])},
                "context": {}}
    if L.http_method(args) == "GET" and any(h in url for h in DOC_HINTS) and data is not None \
            and not L.is_error_payload(data) and not L.records_of(data):
        return {"kind": "doc_miss", "args": {"query": query or " ".join(L.words(view.get("task", ""))[:6])},
                "context": {}}
    return None
