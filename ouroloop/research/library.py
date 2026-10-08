"""v0.5 experience library (docs/design/V0_5_PLAN.md §1.4; V0_5_EVOLVING_SEARCH.md §2.4).

The framework distils verified workflows and lessons from the agent's own train episodes into entries, retrieves
them for a task by text similarity at the `start` site (fixed host code, at most `shown_k` entries appended to the
system prompt), credits each entry with the train reward of the attempts it was shown in, retires the ones that do
not pay, and offers the whole library to the gate as one candidate; after the gate the library is a frozen component
of W, and dev and test only ever see a frozen snapshot.

The package side has no new kind: a snapshot is one file per shelf, `skills/library/<shelf>.md`, listed in the
manifest as a *disabled* skill — so it is in no skill index, `read_skill` cannot read it, and only the host's
retrieval puts any of it in front of the actor. Each entry is a segment `<!-- entry: <id> shelf=<s> kind=<k>
src=<task hashes> -->` followed by its text; the first line of a shelf names the retrieval numbers K froze
(`<!-- library shown_k=3 min_score=0.12 -->`), so retrieval reads its rule from the package and never from the
run's split. The retrieval rule is one rule everywhere: an entry whose source hashes include this task's text hash
is not retrieved — on train that is leave-one-out over the entry's source task and its near-duplicates; dev and test
tasks are never sources, so nothing is excluded there.

Everything here is deterministic: TF-IDF over the entries' own `title + when` (the idf is the library's, not any
split's), cosine scores, ties by utility and then id. No dev or test row is read here; the stream and the credit read
train attempts only (bench.library_phase).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, fields

SHELF_DIR = "skills/library/"
SKILL_PREFIX = "library-"
KINDS = ("workflow", "insight", "api_note")
ORIGINS = ("success", "contrast", "reflexion")
SHELF_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
MARKER = re.compile(r"<!-- entry: (?P<id>[0-9a-f]{12,64}) shelf=(?P<shelf>[a-z0-9_-]+) kind=(?P<kind>[a-z_]+) "
                    r"src=(?P<src>[0-9a-f,]*) -->")
HEADER = re.compile(r"<!-- library shown_k=(?P<k>\d+) min_score=(?P<s>[0-9.]+) -->")
STOP = frozenset("the a an and or of to in on for with from by at as is are be this that it its into your you all "
                 "any each when then than them they their there these those not no yes do does did can will would "
                 "should must may might have has had was were been being if else also only use using via per".split())
TOKEN = re.compile(r"[a-z][a-z0-9_]{2,}")
INTRO = ("Notes from verified earlier work on similar tasks (the framework's experience library). Use one only where "
         "its situation matches this task; the task and its data decide.")


@dataclass(frozen=True)
class LibrarySettings:
    """K's `library` table (preset automationbench_harness_v5; V0_5_PLAN §1.4). Fixed by the preset."""
    snapshot_at: int = 50             # the stream position whose snapshot is offered to the gate (one gate, §2)
    retry: int = 1                    # Reflexion retries of a failed first encounter, train only
    shown_k: int = 3                  # entries retrieval puts in front of the actor
    min_score: float = 0.2            # cosine a retrieved entry needs (uncalibrated: S0 reads it off the train texts)
    shelf_max: int = 6                # entries a shelf holds; the lowest utility goes first
    max_shelves: int = 6
    title_chars: int = 80
    when_chars: int = 300
    body_chars: int = 700
    avoid_chars: int = 200
    sibling_sim: float = 0.6          # task-text cosine at which another train task is a near-duplicate source
    dedupe_cos: float = 0.8           # entry cosine at which a new entry merges into an old one
    retire_harm: int = 2
    retire_utility: float = -0.2
    retire_min_shown: int = 3
    min_other_tasks: int = 2          # train tasks beyond its sources an entry's `when` must retrieve
    max_per_distill: int = 2
    abort_d: float = 0.0              # the snapshot is not gated when D <= abort_d (V0_5_PLAN §3; B is reported only)

    def to_json(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_json(cls, d: dict | None) -> "LibrarySettings":
        known = {f.name for f in fields(cls)}
        out = {}
        for k, v in (d or {}).items():
            if k not in known:
                raise ValueError(f"library.{k}: not a library setting ({sorted(known)})")
            out[k] = float(v) if isinstance(getattr(cls, k), float) else int(v)
        return cls(**out)


# ---------------------------------------------------------------------------- text
def task_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def tokens(text: str) -> list[str]:
    return [t for t in TOKEN.findall((text or "").lower()) if t not in STOP]


def _tf(toks) -> dict:
    out: dict = {}
    for t in toks:
        out[t] = out.get(t, 0) + 1
    return out


def idf_of(docs) -> dict:
    n = len(docs)
    df: dict = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    return {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in df.items()}


def vector(toks, idf: dict) -> dict:
    tf = _tf(toks)
    return {t: c * idf.get(t, math.log(1 + len(idf) + 1) + 1.0) for t, c in tf.items()}


def cosine(u: dict, v: dict) -> float:
    if not u or not v:
        return 0.0
    dot = sum(x * v.get(t, 0.0) for t, x in u.items())
    nu = math.sqrt(sum(x * x for x in u.values()))
    nv = math.sqrt(sum(x * x for x in v.values()))
    return dot / (nu * nv) if nu and nv else 0.0


def query_score(task_text: str, doc_tokens: list, idf: dict) -> float:
    """The retrieval score of one entry for a task: the cosine of their TF-IDF vectors over the library's own
    vocabulary (a task's words the library never uses do not dilute the score, so a long task text and a short one
    are scored alike)."""
    q = vector([t for t in tokens(task_text) if t in idf], idf)
    return cosine(q, vector(doc_tokens, idf))


def text_similarity(a: str, b: str, corpus=()) -> float:
    docs = [tokens(a), tokens(b), *[tokens(c) for c in corpus]]
    idf = idf_of(docs)
    return cosine(vector(docs[0], idf), vector(docs[1], idf))


def near_duplicates(task: str, texts: dict, sim: float) -> set:
    """The train tasks whose text is at least `sim` similar to `task`'s (the task itself included)."""
    docs = {t: tokens(x) for t, x in texts.items()}
    idf = idf_of(list(docs.values()))
    base = vector(docs.get(task) or [], idf)
    return {t for t, d in docs.items() if t == task or cosine(base, vector(d, idf)) >= sim}


# ---------------------------------------------------------------------------- entries
def utility(entry: dict) -> float:
    s = entry.get("stats") or {}
    return float(s.get("adv_sum", 0.0)) / (int(s.get("n_shown", 0)) + 2)


def entry_text(entry: dict) -> str:
    parts = [f"### {entry['title']}", f"When: {entry['when']}", f"Do: {entry['body']}"]
    if entry.get("avoid"):
        parts.append(f"Avoid: {entry['avoid']}")
    return "\n".join(parts)


def entry_id(entry: dict) -> str:
    return hashlib.sha256(json.dumps({k: entry.get(k) for k in ("shelf", "kind", "title", "when", "body", "avoid")},
                                     sort_keys=True).encode("utf-8")).hexdigest()[:16]


def make_entry(raw: dict, *, sources, provenance, origin: str, n: int) -> dict:
    entry = {"shelf": str(raw.get("shelf") or "general").lower(), "kind": raw.get("kind") or "workflow",
             "title": str(raw.get("title") or "").strip(), "when": str(raw.get("when") or "").strip(),
             "body": str(raw.get("body") or raw.get("do") or "").strip(),
             "avoid": str(raw.get("avoid") or "").strip(), "source_hashes": sorted(set(sources)),
             "provenance": list(provenance), "origin": origin, "born_at_n": int(n), "status": "active",
             "stats": {"n_shown": 0, "adv_sum": 0.0, "harm": 0, "fixes": 0}}
    entry["shelf"] = re.sub(r"[^a-z0-9_-]+", "-", entry["shelf"]).strip("-")[:32] or "general"
    entry["entry_id"] = entry_id(entry)
    return entry


def schema_problems(entry: dict, settings: LibrarySettings) -> list[str]:
    out = []
    if entry.get("kind") not in KINDS:
        out.append(f"kind {entry.get('kind')!r}: one of {list(KINDS)}")
    if not SHELF_NAME.fullmatch(str(entry.get("shelf") or "")):
        out.append("shelf is a short lowercase name")
    for key, cap in (("title", settings.title_chars), ("when", settings.when_chars), ("body", settings.body_chars),
                     ("avoid", settings.avoid_chars)):
        value = entry.get(key) or ""
        if key != "avoid" and not value:
            out.append(f"{key} is empty")
        if len(value) > cap:
            out.append(f"{key} has {len(value)} characters; at most {cap}")
        if "-->" in value or "<!--" in value:
            out.append(f"{key} holds a comment marker")
    return out


PLACEHOLDER = "<value>"


def literal_pass(entry: dict, forbidden, allowed=frozenset()) -> tuple[dict | None, list]:
    """The literal check on an entry's text (V0_4_PLAN §1.5: a value of one train task is that task's answer), with
    one automatic placeholder pass: each hit is replaced by `<value>` and the entry is checked again; still a hit ->
    None. -> (entry or None, the hits)."""
    from ..agent import harness_package as hp
    keys = ("title", "when", "body", "avoid")
    text = {f"{k}.md": entry.get(k) or "" for k in keys}
    hits = hp.literal_findings(text, forbidden, allowed=allowed)
    if not hits:
        return entry, []
    fixed = dict(entry)
    for h in hits:
        for k in keys:
            fixed[k] = re.sub(r"(?<![\w@.-])" + re.escape(h["literal"]) + r"(?![\w@-])", PLACEHOLDER,
                              fixed.get(k) or "")
    again = hp.literal_findings({f"{k}.md": fixed.get(k) or "" for k in keys}, forbidden, allowed=allowed)
    if again:
        return None, hits
    fixed["entry_id"] = entry_id(fixed)
    return fixed, hits


def reach_beyond_sources(entry: dict, train_texts: dict, settings: LibrarySettings, library=()) -> int:
    """How many train tasks beyond the entry's sources its `title + when` would retrieve (score >= min_score), the
    idf taken over the library with this entry in it, as retrieval will take it."""
    sources = set(entry.get("source_hashes") or ())
    own = tokens(f"{entry['title']} {entry['when']}")
    idf = idf_of([own] + [tokens(f"{e.get('title', '')} {e.get('when', '')}") for e in library
                          if e.get("status", "active") == "active"])
    return sum(1 for text in train_texts.values() if task_hash(text) not in sources
               and query_score(text, own, idf) >= settings.min_score)


def check_entry(entry: dict, *, forbidden, train_texts: dict, settings: LibrarySettings,
                allowed=frozenset(), library=()) -> tuple[dict | None, str]:
    """The fixed checks (§2.4.2 step 6): schema and lengths; the literal check with one placeholder pass; the `when`
    retrieving at least `min_other_tasks` train tasks beyond its sources. -> (entry or None, why it was dropped)."""
    problems = schema_problems(entry, settings)
    if problems:
        return None, "schema: " + "; ".join(problems)
    checked, hits = literal_pass(entry, forbidden, allowed)
    if checked is None:
        return None, "literal: " + ", ".join(sorted({repr(h["literal"]) for h in hits}))[:200]
    beyond = reach_beyond_sources(checked, train_texts, settings, library)
    if beyond < settings.min_other_tasks:
        return None, (f"retrieves {beyond} train task(s) beyond its sources; an entry must serve at least "
                      f"{settings.min_other_tasks} (one task only is an answer under another name)")
    return checked, ""


def duplicate_of(entry: dict, entries: list, settings: LibrarySettings) -> dict | None:
    """An active entry whose text is at least `dedupe_cos` similar to this one's, or None."""
    active = [e for e in entries if e.get("status") == "active"]
    if not active:
        return None
    docs = [tokens(entry_text(entry))] + [tokens(entry_text(e)) for e in active]
    idf = idf_of(docs)
    q = vector(docs[0], idf)
    best, top = None, 0.0
    for e, d in zip(active, docs[1:]):
        s = cosine(q, vector(d, idf))
        if s >= settings.dedupe_cos and s > top:
            best, top = e, s
    return best


def merged(old: dict, new: dict, merger=None) -> dict:
    """One MERGE (never a whole-library rewrite): the two bodies joined by `merger(old, new) -> raw entry` (a fixed
    prompt's call) or, without one, the old entry kept with the new one's sources and provenance added."""
    raw = merger(old, new) if merger is not None else None
    base = old
    if isinstance(raw, dict) and raw.get("body"):
        base = {**old, **{k: raw[k] for k in ("title", "when", "body", "avoid") if raw.get(k)}}
    out = {**base, "source_hashes": sorted(set(old.get("source_hashes") or ()) | set(new.get("source_hashes") or ())),
           "provenance": list(old.get("provenance") or ()) + list(new.get("provenance") or ()),
           "merged_from": [old["entry_id"], new["entry_id"]]}
    out["entry_id"] = entry_id(out)
    return out


def credit(entry: dict, reward: float, baseline: float | None, *, stable_pass: bool) -> None:
    """One attempt the entry was shown in (§2.4.2 step 7): n_shown + 1; adv_sum += r - b(t); a harm when the task is
    one W_base passes and the attempt failed; a fix when W_base fails it and the attempt passed."""
    s = entry.setdefault("stats", {"n_shown": 0, "adv_sum": 0.0, "harm": 0, "fixes": 0})
    s["n_shown"] = int(s.get("n_shown", 0)) + 1
    b = 0.0 if baseline is None else float(baseline)
    s["adv_sum"] = round(float(s.get("adv_sum", 0.0)) + float(reward) - b, 6)
    if stable_pass and reward == 0:
        s["harm"] = int(s.get("harm", 0)) + 1
    if baseline is not None and baseline == 0 and reward == 1:
        s["fixes"] = int(s.get("fixes", 0)) + 1


def retire_reason(entry: dict, settings: LibrarySettings) -> str | None:
    s = entry.get("stats") or {}
    if int(s.get("harm", 0)) >= settings.retire_harm:
        return f"harm {s.get('harm')}"
    if int(s.get("n_shown", 0)) >= settings.retire_min_shown and utility(entry) <= settings.retire_utility:
        return f"utility {utility(entry):.2f} after {s.get('n_shown')} showings"
    return None


def shelf_overflow(entries: list, settings: LibrarySettings) -> list[dict]:
    """The active entries a full shelf, or too many shelves, push out: the lowest utility first, then the oldest."""
    out = []
    active = [e for e in entries if e.get("status") == "active"]
    shelves: dict = {}
    for e in active:
        shelves.setdefault(e["shelf"], []).append(e)
    for shelf, items in shelves.items():
        items.sort(key=lambda e: (utility(e), -int(e.get("born_at_n", 0)), e["entry_id"]))
        out += items[:max(len(items) - settings.shelf_max, 0)]
    kept = [e for e in active if e not in out]
    names = sorted({e["shelf"] for e in kept}, key=lambda s: -sum(utility(e) + 1 for e in kept if e["shelf"] == s))
    for shelf in names[settings.max_shelves:]:
        out += [e for e in kept if e["shelf"] == shelf]
    return out


# ---------------------------------------------------------------------------- the snapshot in a package
def shelf_path(shelf: str) -> str:
    return f"{SHELF_DIR}{shelf}.md"


def _shelf_text(shelf: str, items: list, settings: LibrarySettings) -> str:
    parts = [f"<!-- library shown_k={settings.shown_k} min_score={settings.min_score} -->"]
    for e in sorted(items, key=lambda e: e["entry_id"]):
        parts.append(f"<!-- entry: {e['entry_id']} shelf={shelf} kind={e['kind']} "
                     f"src={','.join(e.get('source_hashes') or ())} -->\n{entry_text(e)}")
    return "\n\n".join(parts) + "\n"


def compile_shelves(entries: list, settings: LibrarySettings, *, max_chars: int = 8000,
                    max_shelves: int | None = None) -> dict:
    """{path: text} of the active entries, one file per shelf, entries in id order (equal libraries are equal
    bytes), each a segment behind its marker. A shelf is kept within K's skill size (`max_chars`): its lowest-utility
    entries are left out until it fits; at most `max_shelves` shelves (the most useful first)."""
    shelves: dict = {}
    for e in (e for e in entries if e.get("status") == "active"):
        shelves.setdefault(e["shelf"], []).append(e)
    ranked = sorted(shelves, key=lambda s: (-sum(utility(e) + 1 for e in shelves[s]), s))
    keep = ranked[:max_shelves if max_shelves is not None else settings.max_shelves]
    out = {}
    for shelf in sorted(keep):
        items = sorted(shelves[shelf], key=lambda e: (-utility(e), e["entry_id"]))
        while items and len(_shelf_text(shelf, items, settings)) > max_chars:
            items = items[:-1]
        if items:
            out[shelf_path(shelf)] = _shelf_text(shelf, items, settings)
    return out


def with_library(files: dict, entries: list, settings: LibrarySettings, *, max_chars: int = 8000,
                 max_skills: int = 12) -> dict:
    """The package with this library as its snapshot: the old shelves and their manifest entries removed, the new
    shelves written and listed as disabled skills (never in the skill index, never readable with read_skill), within
    K's skill size and skill count (`max_chars`, `max_skills`: the package's own skills keep their places)."""
    out = without_library(files)
    own = len(json.loads(out.get("manifest.json") or "{}").get("skills") or ())
    shelves = compile_shelves(entries, settings, max_chars=max_chars,
                              max_shelves=max(0, min(settings.max_shelves, max_skills - own)))
    if not shelves:
        return out
    manifest = json.loads(out.get("manifest.json") or "{}")
    skills = list(manifest.get("skills") or ())
    for path in sorted(shelves):
        name = SKILL_PREFIX + path[len(SHELF_DIR):-3]
        skills.append({"name": name, "path": path, "enabled": False,
                       "description": "experience library shelf (retrieved by the host; not a readable skill)"})
    manifest["skills"] = skills
    out.update(shelves)
    out["manifest.json"] = json.dumps(manifest, sort_keys=True, indent=1)
    return out


def without_library(files: dict) -> dict:
    out = {p: t for p, t in files.items() if not p.startswith(SHELF_DIR)}
    if "manifest.json" in out:
        manifest = json.loads(out["manifest.json"] or "{}")
        skills = [s for s in manifest.get("skills") or () if not str(s.get("path") or "").startswith(SHELF_DIR)]
        if len(skills) != len(manifest.get("skills") or ()):
            manifest["skills"] = skills
            out["manifest.json"] = json.dumps(manifest, sort_keys=True, indent=1)
    return out


def keep_parents_library(parent: dict, files: dict) -> tuple[dict, list[str]]:
    """An R candidate's files with the parent's library in place (V0_5_PLAN §1.4: the library is written by fixed code
    from train rewards, never by R): shelves R added, changed or deleted and their manifest entries are put back as
    the parent has them, with a note for R. A candidate that left the library alone comes back as it was."""
    mine = {p: t for p, t in files.items() if p.startswith(SHELF_DIR)}
    theirs = {p: t for p, t in parent.items() if p.startswith(SHELF_DIR)}

    def lib_skills(f):
        try:
            return [s for s in json.loads(f.get("manifest.json") or "{}").get("skills") or ()
                    if str(s.get("path") or "").startswith(SHELF_DIR)]
        except ValueError:
            return []
    if mine == theirs and lib_skills(files) == lib_skills(parent):
        return files, []
    out = {p: t for p, t in files.items() if not p.startswith(SHELF_DIR)}
    out.update(theirs)
    try:
        manifest = json.loads(out.get("manifest.json") or "{}")
    except ValueError:
        return out, ["the experience library is written by fixed code; your changes to it were undone"]
    manifest["skills"] = [s for s in manifest.get("skills") or () if not str(s.get("path") or "").startswith(SHELF_DIR)] \
        + lib_skills(parent)
    out["manifest.json"] = json.dumps(manifest, sort_keys=True, indent=1)
    return out, ["the experience library (skills/library/) is written by fixed code from train rewards; your changes "
                 "to it were undone"]


def parse_shelf(text: str) -> tuple[list[dict], dict]:
    """(the entries of one shelf file, its retrieval header)."""
    header = HEADER.search(text or "")
    head = {"shown_k": int(header.group("k")), "min_score": float(header.group("s"))} if header else {}
    entries = []
    marks = list(MARKER.finditer(text or ""))
    for i, m in enumerate(marks):
        body = text[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)].strip()
        fields_ = {"title": "", "when": "", "body": "", "avoid": ""}
        for line in body.splitlines():
            if line.startswith("### "):
                fields_["title"] = line[4:].strip()
            elif line.startswith("When: "):
                fields_["when"] = line[6:].strip()
            elif line.startswith("Do: "):
                fields_["body"] = line[4:].strip()
            elif line.startswith("Avoid: "):
                fields_["avoid"] = line[7:].strip()
            elif fields_["body"] and line.strip():
                fields_["body"] += "\n" + line.strip()
        entries.append({"entry_id": m.group("id"), "shelf": m.group("shelf"), "kind": m.group("kind"),
                        "source_hashes": [s for s in m.group("src").split(",") if s], "text": body, **fields_})
    return entries, head


def library_of(files: dict) -> tuple[list[dict], dict]:
    """(every entry of a package's library, the retrieval header) — from the shelves its manifest lists."""
    entries, head = [], {}
    try:
        manifest = json.loads(files.get("manifest.json") or "{}")
    except ValueError:
        return [], {}
    for skill in manifest.get("skills") or ():
        path = str(skill.get("path") or "")
        if path.startswith(SHELF_DIR) and path in files:
            items, h = parse_shelf(files[path])
            entries += items
            head = head or h
    return entries, head


# ---------------------------------------------------------------------------- retrieval (the `start` site)
def retrieve(task_text: str, entries: list, *, k: int, min_score: float) -> tuple[list, int]:
    """-> ([(entry, score)], how many entries the leave-one-out rule excluded). TF-IDF over the entries' title + when
    (the library's own idf), cosine against the task text; the entries whose source hashes hold this task's hash are
    excluded; above min_score; by score, then utility, then id; at most k."""
    if not entries:
        return [], 0
    th = task_hash(task_text)
    docs = [tokens(f"{e.get('title', '')} {e.get('when', '')}") for e in entries]
    idf = idf_of(docs)
    scored, excluded = [], 0
    for e, d in zip(entries, docs):
        if th in set(e.get("source_hashes") or ()):
            excluded += 1
            continue
        s = query_score(task_text, d, idf)
        if s >= min_score:
            scored.append((e, round(s, 4)))
    scored.sort(key=lambda x: (-x[1], -utility(x[0]), x[0]["entry_id"]))
    return scored[:k], excluded


def retrieval_block(package, task_text: str) -> tuple[str, dict | None]:
    """The library block for one attempt and the `library_retrieval` record, from a mounted package (its run files);
    ("", None) for a package without a library — the system prompt is then exactly what it was."""
    files = getattr(package, "run_files", None) or {}
    entries, head = library_of(files)
    if not entries:
        return "", None
    chosen, excluded = retrieve(task_text, entries, k=int(head.get("shown_k", 3)),
                                min_score=float(head.get("min_score", 0.12)))
    text = ""
    if chosen:
        text = INTRO + "\n\n" + "\n\n".join(e["text"] for e, _ in chosen)
    info = {"entries": [e["entry_id"] for e, _ in chosen], "scores": [s for _, s in chosen], "chars": len(text),
            "library_size": len(entries), "excluded_as_source": excluded, "task_hash": task_hash(task_text)}
    return text, info


# ---------------------------------------------------------------------------- distillation
DISTILL = (
    "You distil verified experience for an agent that works business tasks through APIs. You are shown a train "
    "task's attempt that the grader verified as successful, and sometimes a failed attempt at the same task. Write at "
    "most {max} entries that would help on OTHER, similar tasks: what to do and in what situation; for a "
    "success-and-failure pair, what the success did that the failure did not (its advantage), and what to avoid. "
    "Write general rules: never a specific name, e-mail, id, amount or date of this task (write <amount>, <vendor>, "
    "<date> instead). Answer with JSON only: {{\"entries\": [{{\"shelf\": a short tool or intent family such as "
    "sheets-reporting or email-slack, \"kind\": \"workflow|insight|api_note\", \"title\": at most 80 characters, "
    "\"when\": the situation, at most 300, \"body\": what to do, at most 700, \"avoid\": what not to do, at most "
    "200}}]}}.")
MERGE = (
    "Two entries of an agent's experience library say nearly the same thing. Merge them into one entry that keeps "
    "every distinct instruction of both and nothing task-specific. Answer with JSON only: {\"shelf\", \"kind\", "
    "\"title\", \"when\", \"body\", \"avoid\"} with the same limits (80, 300, 700, 200 characters).")


def distill_request(success: dict, failure: dict | None = None, *, max_entries: int = 2) -> tuple[str, str]:
    """(system, user) of one distillation call: a train success's summary (and a failure's) as the R projection
    gives it — the task text, the turns, the checks it passed by type."""
    system = DISTILL.format(max=max_entries)
    user = json.dumps({"success": success, **({"failure": failure} if failure else {})}, ensure_ascii=False,
                      default=str)
    return system, user


def parse_distilled(text: str, *, max_entries: int = 2) -> list[dict]:
    """The entries of a distillation reply, or [] for an unreadable one (it is dropped, never retried)."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?|```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        try:
            data = json.loads(raw[start:end + 1]) if start >= 0 and end > start else {}
        except ValueError:
            return []
    if isinstance(data, dict) and "entries" not in data and isinstance(data.get("body"), str):
        # one bare entry, as the MERGE prompt asks for (2026-10-07 review: a merge reply was always discarded)
        return [data][:max_entries]
    items = data.get("entries") if isinstance(data, dict) else data if isinstance(data, list) else []
    return [e for e in items if isinstance(e, dict)][:max_entries] if isinstance(items, list) else []


def retry_note(checks) -> str:
    """The Reflexion retry's note (§2.4.2 step 4): the failed checks of the first encounter by type and params, the
    projection R reads — train only."""
    failed = [c for c in checks or () if not c.get("passed")]
    if not failed:
        return ""
    lines = [f"- {c.get('type')}: {json.dumps(c.get('params'), ensure_ascii=False, default=str)[:300]}"
             for c in failed[:12]]
    return ("A previous attempt at this very task was graded and failed these checks. Work the task again from the "
            "start and make sure the result satisfies them:\n" + "\n".join(lines))


def telemetry(entries: list, experience=()) -> dict:
    """inputs/library.json (V0_5_PLAN §1.4), train only: the installed library's entries by shelf with their credit
    as the stream left it, and the entries the stream retired with why — no task id, no provenance."""
    state: dict = {}
    for row in experience or ():
        if row.get("kind") == "library_entry" and row.get("entry_id"):
            state[row["entry_id"]] = row
    shelves: dict = {}
    for e in entries:
        row = state.get(e["entry_id"], {})
        stats = row.get("stats") or {}
        shelves.setdefault(e["shelf"], []).append({"entry_id": e["entry_id"], "kind": e.get("kind"),
                                                   "title": e.get("title"), "n_shown": stats.get("n_shown"),
                                                   "adv_sum": stats.get("adv_sum"), "harm": stats.get("harm"),
                                                   "fixes": stats.get("fixes"), "origin": row.get("origin")})
    retired = [{"entry_id": k, "title": r.get("title"), "why": r.get("retired")} for k, r in sorted(state.items())
               if r.get("status") == "retired"]
    return {"shelves": shelves, "entries": len(entries), "retired": retired}


# ---------------------------------------------------------------------------- the stream's readouts
def stream_readout(readings: dict, order: list) -> dict:
    """D, B and the learning curve of a stream (V0_5_PLAN §3): per task the first-encounter reward r_lib and the
    control b (W_base's train mean). D = Σ (r_lib − b); B = tasks W_base passes (b = 1) that the first encounter
    failed; the curve is the paired effect per window of 10 by stream position, and the halves' difference."""
    rows = [(i, t, readings[t]) for i, t in enumerate(order) if t in readings and readings[t].get("lib") is not None
            and readings[t].get("control") is not None]
    d = sum(r["lib"] - r["control"] for _, _, r in rows)
    b = sum(1 for _, _, r in rows if r["control"] >= 1.0 and r["lib"] == 0)
    windows = []
    for start in range(0, len(order), 10):
        w = [r["lib"] - r["control"] for i, _, r in rows if start <= i < start + 10]
        if w:
            windows.append({"from": start, "to": start + 10, "n": len(w), "effect": round(sum(w) / len(w), 4)})
    half = len(order) // 2
    first = [r["lib"] - r["control"] for i, _, r in rows if i < half]
    second = [r["lib"] - r["control"] for i, _, r in rows if i >= half]
    mean = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
    return {"tasks": len(rows), "D": round(d, 4), "B": b, "windows": windows,
            "first_half": mean(first), "second_half": mean(second),
            "halves_difference": None if not first or not second else round(mean(second) - mean(first), 4)}


def snapshot_abort(readout: dict, settings: LibrarySettings, *, empty: bool = False) -> str | None:
    """Why the stream's snapshot is not offered to the gate, or None (V0_5_PLAN §1.4, 2026-10-07): D <= abort_d, or an
    empty library. B is a process number only — the snapshot round's own screen, gate and confirmation catch harm."""
    if readout["D"] <= settings.abort_d:
        return f"D {readout['D']} <= {settings.abort_d}"
    if empty:
        return "the library is empty"
    return None


def bootstrap_halves(readings: dict, order: list, *, replicates: int = 2000, seed: int = 1) -> float | None:
    """P(second-half effect − first-half effect > 0) by resampling tasks within each half (the pre-registered
    reading: > 0 with P >= 0.8)."""
    import random
    rows = [(i, readings[t]["lib"] - readings[t]["control"]) for i, t in enumerate(order) if t in readings
            and readings[t].get("lib") is not None and readings[t].get("control") is not None]
    half = len(order) // 2
    a = [x for i, x in rows if i < half]
    b = [x for i, x in rows if i >= half]
    if not a or not b:
        return None
    rng = random.Random(seed)
    up = 0
    for _ in range(replicates):
        ra = [a[rng.randrange(len(a))] for _ in a]
        rb = [b[rng.randrange(len(b))] for _ in b]
        up += (sum(rb) / len(rb)) > (sum(ra) / len(ra))
    return round(up / replicates, 4)


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
