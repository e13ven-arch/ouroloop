"""The platform rules over ReMe's task memories (docs/design/MEMORY.md §5-§8; DESIGN §5.2-§5.6).

ReMe distils, stores and retrieves; the rules that make the library an honest, train-only component of W are ours:

- provenance: every stored memory carries `ol_*` metadata (scalars only; lists as JSON strings, since ReMe joins a
  list value with commas when it stores one); the source list and the stats live in the stream's ledger and are merged
  into a snapshot as `ol_sources` and `ol_stats`. Of ReMe's own metadata (the extraction model's raw JSON item) only
  REME_META_KEYS are kept, bounded: the rest repeats the lesson's text past the platform's checks, and any key of it
  would override a MemoryNode field when stored;
- train-only: a memory whose split is not train, or whose source line is held out, is refused with TrainOnlyError;
- literals: a value that occurs in exactly one train task is that task's answer; a memory naming one gets one
  placeholder pass (each hit becomes `<value>`), and is dropped when a hit remains;
- citation credit: the first encounter's finish reason (then its last text) names the entries it used in its last
  `library-used:` line; each cited entry is credited r - b(t); without such a line each shown entry gets
  (r - b(t)) / len(shown); harm is counted in distinct tasks;
- retirement after probation, capacity eviction only above max_active, the stream's readout (D, D per origin, the
  halves and the learning curve, the credit's rank correlation) and the abort rule;
- promotion: an entry that fixed tasks from several sources becomes a skill candidate, or a cause for the improver
  when the diagnosis can mechanise it.

No dev or test row is read here.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re

from ..contracts import TrainOnlyError
from ..harness.literals import findings, literal_pattern
from .settings import ORIGINS, Settings

IMPORTED = "imported"
PROVENANCE_KEYS = ("ol_line", "ol_source_line", "ol_split", "ol_task_hash", "ol_activity", "ol_release_ref",
                   "ol_sessions", "ol_trace_refs", "ol_origin", "ol_reme_step_type", "ol_born_at", "ol_source_score",
                   "ol_validation_score", "ol_imported_from")
SNAPSHOT_KEYS = ("ol_sources", "ol_stats")   # merged from the ledger into a snapshot row, never stored live
# what is kept of ReMe's metadata: the extraction op stores the model's whole JSON item there, i.e. `experience` and
# `when_to_use` again plus whatever else the model wrote; MemoryNode's to_vector_node spreads metadata over
# memory_type, author, score and content, so an unchecked key overrides them
REME_META_KEYS = ("step_type", "confidence", "tags", "tools_used")
CITATION = re.compile(r"library-used[ \t]*:[ \t]*(?P<ids>[^\n]*)", re.IGNORECASE)   # the ids on the line itself
ID8 = re.compile(r"\b[0-9a-f]{8,16}\b")
PLACEHOLDER = "<value>"
STEP_LINE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+\S", re.MULTILINE)
SENTENCE = re.compile(r"[^.!?;\n]{12,}[.!?;](?:\s|$)")
STATUSES = ("active", "retired", "superseded")


# ---------------------------------------------------------------------------------------------- provenance
def require_train(split: str, source_line: str, heldout_domains) -> None:
    if split != "train":
        raise TrainOnlyError(f"split {split!r}: only train experience enters a library")
    if source_line in set(heldout_domains or ()):
        raise TrainOnlyError(f"business line {source_line!r} is held out ({sorted(heldout_domains)}): none of its "
                             f"experience enters a library")


def origin_of(memory: dict) -> str:
    """ReMe's extraction op of a memory, from its author (the op's LLM model name, research/reme:<op>)."""
    author = str(memory.get("author") or "")
    op = author.rsplit(":", 1)[-1] if ":" in author else ""
    if op not in ORIGINS:
        raise ValueError(f"memory {memory.get('memory_id')}: author {author!r} names no extraction op")
    return op


def reme_metadata(meta) -> dict:
    """ReMe's metadata of a memory reduced to REME_META_KEYS, each a bounded scalar: step_type (a short string),
    confidence (a finite number), tags and tools_used (a JSON list of at most 12 short strings, sorted; ReMe would
    store a list joined by commas, so a stored row may hold that form, which is read back too)."""
    meta = dict(meta or {})
    out = {}
    step_type = meta.get("step_type")
    if isinstance(step_type, str) and step_type.strip():
        out["step_type"] = step_type.strip()[:40]
    conf = meta.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool) and math.isfinite(conf):
        out["confidence"] = round(float(conf), 4)
    for key in ("tags", "tools_used"):
        v = meta.get(key)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                v = v.split(",")
        if isinstance(v, (list, tuple)):
            items = sorted({str(x).strip()[:64] for x in v
                            if isinstance(x, (str, int, float)) and not isinstance(x, bool) and str(x).strip()})
            out[key] = json.dumps(items[:12], ensure_ascii=False)
    return out


def stamp(memory: dict, *, line: str, source_line: str, split: str, task_hash: str, activity: str, release_ref: str,
          sessions, trace_refs, origin: str, born_at: int, source_score: float, heldout_domains,
          imported_from: str = "") -> dict:
    """The memory as it is stored: ReMe's fields kept, its metadata reduced to REME_META_KEYS, the `ol_*` provenance
    added, the clock fields blanked (the snapshot is content, so equal streams give equal bytes) and the vector left
    out."""
    require_train(split, source_line, heldout_domains)
    meta = reme_metadata(memory.get("metadata"))
    meta.update({
        "ol_line": line, "ol_source_line": source_line, "ol_split": split, "ol_task_hash": task_hash,
        "ol_activity": activity, "ol_release_ref": release_ref or "",
        "ol_sessions": json.dumps(sorted(set(sessions or ()))),
        "ol_trace_refs": json.dumps(sorted(set(trace_refs or ()))),
        "ol_origin": origin, "ol_reme_step_type": str(meta.get("step_type") or ""), "ol_born_at": int(born_at),
        "ol_source_score": float(source_score), "ol_validation_score": float(memory.get("score") or 0.0),
        "ol_imported_from": imported_from or ""})
    out = {k: v for k, v in memory.items() if k not in ("vector", "metadata")}
    out.update(metadata=meta, time_created="", time_modified="", message_time="")
    return out


def content_id(content: str) -> str:
    """ReMe's memory id: the first 16 hex of the content's sha256 (memory_node.py _update_memory_id)."""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------------------------- literals
def literal_pass(fields: dict, forbidden, allowed=frozenset()) -> tuple[dict | None, list]:
    """The literal check on a memory's text fields ({name: text}), with one placeholder pass: each hit is replaced by
    PLACEHOLDER and the fields are checked again; a hit that remains drops the memory. -> (fields or None, the hits:
    those found, or for a dropped memory those that remained)."""
    files = {f"{k}.md": str(v or "") for k, v in fields.items()}
    hits = findings(files, None, set(forbidden or ()), allowed=set(allowed or ()))
    if not hits:
        return dict(fields), []
    fixed = dict(fields)
    for lit in sorted({h["literal"] for h in hits}, key=lambda x: (-len(x), x)):
        pattern = literal_pattern(lit)
        for k in fixed:
            fixed[k] = pattern.sub(PLACEHOLDER, str(fixed[k] or ""))
    again = findings({f"{k}.md": str(v or "") for k, v in fixed.items()}, None, set(forbidden or ()),
                     allowed=set(allowed or ()))
    return (None, again) if again else (fixed, hits)       # dropped: the hits the placeholder pass left


# ---------------------------------------------------------------------------------------------- citation
def parse_citation(finish_reason: str | None, last_text: str | None) -> list[str] | None:
    """The 8-hex ids an attempt says it used: the last `library-used:` line of the finish call's reason, else of the
    last reply's text; [] for `library-used: none`; None when neither holds such a line."""
    for text in (finish_reason, last_text):
        hits = list(CITATION.finditer(text or ""))
        if hits:
            ids = hits[-1].group("ids")
            return sorted({m.group(0)[:8] for m in ID8.finditer(ids.lower())})
    return None


def resolve_ids(ids, known) -> list[str]:
    """Shown ids as the agent saw them (8 hex, or whole) mapped to the full ids `known`, in order, unknown ones
    dropped."""
    by8: dict[str, str] = {}
    for k in sorted(known):
        by8.setdefault(k[:8], k)
    out = []
    for i in ids or ():
        full = i if i in known else by8.get(str(i)[:8])
        if full and full not in out:
            out.append(full)
    return out


def used_entries(shown: list[str], cited: list[str] | None) -> tuple[list[str], float, list[str]]:
    """(the shown ids an attempt used, the share of r - b each gets, cited ids that were never shown)."""
    shown = list(dict.fromkeys(shown or ()))
    if cited is None:
        return shown, (1.0 / len(shown) if shown else 0.0), []
    by8 = {s[:8]: s for s in shown}
    used = [by8[c] for c in cited if c in by8]
    return used, 1.0, sorted(c for c in cited if c not in by8)


def new_stats() -> dict:
    return {"n_shown": 0, "n_cited": 0, "credit_sum": 0.0, "harm_tasks": [], "fixes": 0, "fix_tasks": []}


def credit(entries: dict, *, task_hash: str, shown: list[str], cited: list[str] | None, r: float, b: float | None,
           stable_pass: bool) -> dict:
    """Credit one first encounter on the ledger's entries {id: entry}: every shown entry is shown once more; the used
    ones (cited, or all shown without a citation line) get their share of r - b(t), a harm when W_base passes the task
    stably and the attempt failed (distinct tasks), a fix when W_base fails it and the attempt passed. -> the reading's
    credit part."""
    used, share, unshown = used_entries(shown, cited)
    adv = None if b is None else float(r) - float(b)
    for mid in dict.fromkeys(shown or ()):
        e = entries.get(mid)
        if e is None:
            continue
        s = e.setdefault("stats", new_stats())
        s["n_shown"] += 1
        if mid in used:
            if cited is not None:
                s["n_cited"] += 1
            if adv is None:          # no baseline for the task: shown (and cited), credited nothing, as D reads it
                continue
            s["credit_sum"] = round(s["credit_sum"] + adv * share, 6)
            if stable_pass and r == 0 and task_hash not in s["harm_tasks"]:
                s["harm_tasks"] = sorted(s["harm_tasks"] + [task_hash])
            if float(b) == 0 and r == 1:
                s["fixes"] += 1
                s["fix_tasks"] = sorted(set(s.get("fix_tasks") or ()) | {task_hash})
    return {"used": used, "share": share, "cited_unshown": unshown, "adv": None if adv is None else round(adv, 6)}


def utility(entry: dict) -> float:
    s = entry.get("stats") or {}
    return float(s.get("credit_sum", 0.0)) / (int(s.get("n_shown", 0)) + 2)


def mean_credit(entry: dict) -> float | None:
    s = entry.get("stats") or {}
    n = int(s.get("n_shown", 0))
    return round(float(s.get("credit_sum", 0.0)) / n, 6) if n else None   # credit sums are kept to 6 places


def retire_reason(entry: dict, settings: Settings) -> str | None:
    """Why an active entry is retired now, or None: nothing during probation (fewer than probation_shown showings);
    harm in retire_harm_tasks distinct tasks once shown retire_harm_min_shown times; or a mean credit at or below
    retire_mean_credit once shown retire_min_shown times."""
    s = entry.get("stats") or {}
    n = int(s.get("n_shown", 0))
    if n < settings.probation_shown:
        return None
    harm = len(s.get("harm_tasks") or ())
    if harm >= settings.retire_harm_tasks and n >= settings.retire_harm_min_shown:
        return f"harm in {harm} tasks after {n} showings"
    m = mean_credit(entry)
    if n >= settings.retire_min_shown and m is not None and m <= settings.retire_mean_credit:
        return f"mean credit {m:.3f} after {n} showings"
    return None


def evictable(entries: dict, settings: Settings) -> list[str]:
    """The ids capacity pushes out, lowest first: only while more than max_active are active, and only among entries
    out of probation (n_shown >= probation_shown), by (utility, born_at, id)."""
    active = {k: e for k, e in entries.items() if e.get("status") == "active"}
    over = len(active) - settings.max_active
    if over <= 0:
        return []
    proven = [k for k, e in active.items() if int((e.get("stats") or {}).get("n_shown", 0)) >= settings.probation_shown]
    proven.sort(key=lambda k: (utility(active[k]), int(active[k].get("born_at", 0)), k))
    return proven[:over]


# ---------------------------------------------------------------------------------------------- snapshot rows
def snapshot_rows(store_rows: list[dict], entries: dict) -> list[dict]:
    """The store's rows with the ledger's source list and stats merged in as JSON strings (ol_sources, ol_stats)."""
    out = []
    for row in store_rows:
        e = entries.get(row["memory_id"]) or {}
        meta = {k: v for k, v in (row.get("metadata") or {}).items() if k not in SNAPSHOT_KEYS}
        meta["ol_sources"] = json.dumps(sorted(set(e.get("sources") or ())))
        meta["ol_stats"] = json.dumps(e.get("stats") or new_stats(), sort_keys=True)
        out.append({**row, "metadata": meta})
    return out


def sources_of_row(row: dict) -> list[str]:
    try:
        return list(json.loads((row.get("metadata") or {}).get("ol_sources") or "[]"))
    except ValueError:
        return []


def stats_of_row(row: dict) -> dict:
    try:
        return dict(json.loads((row.get("metadata") or {}).get("ol_stats") or "{}"))
    except ValueError:
        return {}


# ---------------------------------------------------------------------------------------------- readout
def rank_correlation(xs, ys) -> float | None:
    """Spearman's rho of two equal-length lists (the credit-is-informative reading)."""
    if len(xs) < 3 or len(xs) != len(ys):
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return round(num / den, 4) if den else None


def _mean(xs):
    return round(sum(xs) / len(xs), 4) if xs else None


def halves_better(first: list[float], second: list[float], *, replicates: int = 2000, seed: int = 1) -> float | None:
    """P(second-half effect > first-half effect) by resampling tasks within each half."""
    if not first or not second:
        return None
    rng = random.Random(seed)
    up = 0
    for _ in range(replicates):
        a = [first[rng.randrange(len(first))] for _ in first]
        b = [second[rng.randrange(len(second))] for _ in second]
        up += (sum(b) / len(b)) > (sum(a) / len(a))
    return round(up / replicates, 4)


def readout(readings: dict, order: list[str], entries: dict) -> dict:
    """The stream's readout (DESIGN §5.5): D = Σ (r - b) over first encounters with a baseline; D per origin over the
    encounters that used an entry of that origin; B = stable passes the first encounter failed; the learning curve
    (the effect per window of 10 stream positions, the halves and P(the second half is better)); the citation rate;
    the rank correlation of the credit at the stream's half with the later effect."""
    pos = {t: i for i, t in enumerate(order)}
    rows = [(t, readings[t]) for t in order if t in readings and readings[t].get("b") is not None]
    d = sum(r["r"] - r["b"] for _, r in rows)
    b_count = sum(1 for _, r in rows if r.get("stable_pass") and r["r"] == 0)
    by_origin: dict = {}
    for _, r in rows:
        for o in sorted(set(r.get("origins_used") or ())):
            slot = by_origin.setdefault(o, {"D": 0.0, "n": 0})
            slot["D"] = round(slot["D"] + r["r"] - r["b"], 6)
            slot["n"] += 1
    shown_rows = [r for _, r in rows if r.get("shown")]
    with_line = [r for r in shown_rows if r.get("cited") is not None]
    half = len(order) // 2
    effects = [(pos[t], r["r"] - r["b"]) for t, r in rows]
    windows = []
    for start in range(0, len(order), 10):
        w = [e for i, e in effects if start <= i < start + 10]
        if w:
            windows.append({"from": start, "to": start + 10, "n": len(w), "effect": _mean(w)})
    first = [e for i, e in effects if i < half]
    second = [e for i, e in effects if i >= half]
    early = [t for t in order[:half] if t in readings]
    late = [t for t in order[half:] if t in readings and readings[t].get("b") is not None]
    at_half: dict = {}
    for t in early:
        r = readings[t]
        for mid in r.get("shown") or ():
            slot = at_half.setdefault(mid, [0.0, 0])
            slot[1] += 1
            if mid in (r.get("used") or ()):
                slot[0] += r.get("credit_each", 0.0)
    later: dict = {}
    for t in late:
        r = readings[t]
        for mid in r.get("shown") or ():
            later.setdefault(mid, []).append(r["r"] - r["b"])
    paired = [(at_half[k][0] / (at_half[k][1] + 2), sum(v) / len(v)) for k, v in sorted(later.items())
              if k in at_half]
    active = [e for e in entries.values() if e.get("status") == "active"]
    return {
        "tasks": len(rows), "D": round(d, 4), "B": b_count,
        "D_by_origin": {o: {"D": round(v["D"], 4), "n": v["n"]} for o, v in sorted(by_origin.items())},
        "windows": windows, "first_half": _mean(first), "second_half": _mean(second),
        "halves_difference": None if not first or not second else round(_mean(second) - _mean(first), 4),
        "p_second_half_better": halves_better(first, second),
        "citation_rate": round(len(with_line) / len(shown_rows), 4) if shown_rows else None,
        "cited_any_rate": (round(sum(1 for r in with_line if r.get("used")) / len(shown_rows), 4)
                           if shown_rows else None),
        "credit_rank_correlation": rank_correlation([a for a, _ in paired], [b for _, b in paired]),
        "entries": {"active": len(active),
                    "retired": sum(1 for e in entries.values() if e.get("status") == "retired"),
                    "superseded": sum(1 for e in entries.values() if e.get("status") == "superseded"),
                    "by_origin": {o: sum(1 for e in active if e.get("origin") == o)
                                  for o in sorted({e.get("origin") for e in active})}},
        "shown_per_attempt": round(sum(len(r.get("shown") or ()) for _, r in rows) / len(rows), 3) if rows else None,
    }


# the reply-format counters of a stream (track.StreamStep's ledger) that a canary run requires to be 0
REPLY_FORMAT_COUNTERS = ("no_json_validation", "no_json_extraction", "unparsed_validation")


def reply_format_problems(counters: dict) -> list[str]:
    """The model replies ReMe read by a fallback or not at all: a validation reply without a ```json block (ReMe
    calls it valid at 0.5), an extraction reply with no JSON (ReMe extracts nothing and only warns), a validation
    reply ReMe could not read (it calls the memory invalid). Each costs lessons silently."""
    return [f"{k}: {counters[k]}" for k in REPLY_FORMAT_COUNTERS if counters.get(k)]


def snapshot_abort(result: dict, settings: Settings, *, empty: bool) -> str | None:
    """Why the snapshot is not offered to the ladder: D <= abort_d, or an empty library."""
    if result["D"] <= settings.abort_d:
        return f"D {result['D']} <= {settings.abort_d}"
    if empty:
        return "the library is empty"
    return None


def cosine(u: list[float], v: list[float]) -> float:
    """Dot product of two unit vectors (bge-small's are L2-normalised), computed without numpy on the host."""
    return float(math.fsum(a * b for a, b in zip(u, v)))


# ---------------------------------------------------------------------------------------------- promotion
def multi_step(content: str) -> bool:
    """Whether a lesson reads as a procedure of several steps: at least two numbered or bulleted lines, or at least
    two sentences (a one-line warning is a note, not a procedure)."""
    text = str(content or "")
    return len(STEP_LINE.findall(text)) >= 2 or len(SENTENCE.findall(text)) >= 2


DISTILLED = ("added", "store_dedup", "same_id")      # ledger events of a task whose summary gave this lesson


def distilled_from(entry: dict) -> set[str]:
    """The train tasks whose trajectories gave this lesson: the one it was added from and every later one whose
    summary repeated it (a duplicate adds a source, not a row)."""
    return {ev["task_hash"] for ev in entry.get("events") or () if ev.get("how") in DISTILLED and ev.get("task_hash")}


def promotable(entries: dict, settings: Settings, mechanizable=None) -> list[tuple[dict, str]]:
    """The active entries that grew past a note (DESIGN §5.6), by id: fixes >= promote_fixes and distilled from at
    least promote_sources train tasks; then ("cause") when `mechanizable(entry)` says the diagnosis can turn it into
    code, else ("skill") when its content is a multi-step procedure. An entry that is neither stays a note."""
    out = []
    for mid in sorted(entries):
        e = entries[mid]
        if e.get("status") != "active":
            continue
        s = e.get("stats") or {}
        if int(s.get("fixes") or 0) < settings.promote_fixes or len(distilled_from(e)) < settings.promote_sources:
            continue
        if mechanizable is not None and mechanizable(e):
            out.append((e, "cause"))
        elif multi_step((e.get("row") or {}).get("content") or e.get("content") or ""):
            out.append((e, "skill"))
    return out
