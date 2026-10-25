"""Per-task adoption on the repetitive stream (DESIGN v2.0 §6.23; IMPROVER §15): Harness²'s Propose-Probe-Compose,
without labels, on one task the agent has just done and will do again.

After a task's first encounter the host opens that run's transcript to R (never its grading), gives it the request,
a label-free digest of the run, the library of what earlier tasks adopted (with the lessons they left), and a session
with try_task bound to this task: each candidate package R hands in is run once more on the same request (a probe,
role `probe`, an extra test attempt the report never counts as an encounter) and the probe's digest comes back beside
the reference. A composer (one aux call, prompts/compose.md) reads the reference and the probes and adopts one package
for this task or keeps the base; a reflector (one aux call, prompts/reflect.md) gives each changed component a verdict
and, when one supports it, writes a transferable lesson into the library (the whole adopted tree beside an
EXPERIENCE.md, as Harness²'s library does). The task's later encounters run its adopted package; the working line
never moves here. Leakage: no score, no grading row and no assertion is ever in R's or the composer's inputs; the
transcript is the agent's own experience, which real work has too.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

from ..contracts import write_json
from ..harness.kinds import MEMORY_PATH
from ..harness.literals import forbidden as forbidden_of, literal_index
from ..harness.package import Limits, PackageError, build
from .components import components
from .declaration import DEFAULT_DIR, Declaration, read_declaration
from .evidence import Evidence
from .screen import condition, key
from .session import CodingSession, WorkTree
from .settings import Settings
from .verify import VerifyContext, run_ladder
from .verify.unit import unit_check

PROMPTS = Path(__file__).parent / "prompts"
FEEDBACK_OUTCOME = ("This request was NOT accepted (inputs/task.json `outcome`): something the agent did, or left undone, "
                    "was wrong, and you are not told what. That is the one thing you know beyond the run itself. Read the "
                    "run against the request and every document it read until you find what a careful practitioner "
                    "would send back: a requirement it did not carry out, a value it got wrong, an item it should have "
                    "left alone or reported, a recipient it never told, a term it replaced. There is one; build against "
                    "it.")
FEEDBACK_NONE = ("You are told nothing about whether the run was accepted: read it as a practitioner reads a colleague's "
                 "work, and hand in nothing when you find nothing wrong.")
FEEDBACK_ATTEMPTS = (FEEDBACK_OUTCOME + " After each try_task you are told one more thing: whether that run was ACCEPTED. "
                     "A candidate whose run was accepted is adopted for this task; one whose run was not accepted is "
                     "not — use the tries you have left to change what it does, then try again. Hand in every "
                     "candidate you tried; of several accepted, the last declared is adopted.")
FEEDBACK_ITEMS = ("This request was NOT accepted (inputs/task.json `outcome`), and you are told what the request was sent "
                  "back for: `rejected_items`, one entry per check that failed, each naming the kind of check and what it "
                  "is about — which message, channel, row or record — never what it should have contained. After each "
                  "try_task you are told whether that run was accepted and, when it was not, the items of that run. A "
                  "candidate whose run was accepted is adopted for this task; one whose run was not is not — change what "
                  "it does and try again while tries are left. Hand in every candidate you tried; of several accepted, "
                  "the last declared is adopted.")
REPEAT_TOOL_NOTES = {       # v2.1: the session tools worded for one task (the shared descriptions speak of rounds)
    "try_task": "Run a candidate package (candidates/package, or `dir`) once on this task (`task` = task.json's "
                "task_id), for real. The answer puts "
                "that run beside the first one: the hooks that acted and what they said, your tool's calls with their "
                "inputs and outputs, the writes by route, the sources read, the final text, and whether the run was "
                "accepted (and what it was sent back for, when that is told). {max_probes} runs for this task in all; "
                "a candidate unchanged since its last try is refused, a changed one may be tried again. The package "
                "must build and pass V1 first.",
    "read_trajectory": "Read a run of this task as the agent saw it (its messages, tool calls and results), in pages "
                       "of bytes: `session` is inputs/task.json's reference_run.session, or a try's session as its "
                       "answer names it.",
    "run_check": "Run the free rungs on a candidate package (candidates/package, or `dir`) under the declaration you "
                 "would finish with: V0 (shape, limits, literals, the declaration against the diff both ways) and V1 "
                 "(the package's tests, an import probe, a smoke run in the worker). Nothing is paid; nothing else "
                 "runs here.",
    "write_file": "Write a whole file under candidates/<dir>/ (candidates/package/ or candidates/package-2/); the "
                  "first write under a directory creates that candidate as a copy of the current package. Counts "
                  "as one edit.",
    "finish": "End the session with one declaration per candidate package (candidates/package and "
              "candidates/package-2 at most): {dir, mechanism, scope, components: [{kind, site, trigger, cause_ids: "
              "[], predicted_affected: [], uses_model?}]}. A candidate changed after its last try is tried once more "
              "while tries are left and that run decides; otherwise the last tried version is what an adoption "
              "installs. A declaration the host cannot read is answered as this call's error; fix it and finish "
              "again. An empty list keeps the base for this task.",
}
ACCEPTED_LINE = {True: "  THIS RUN WAS ACCEPTED: this candidate is adopted for the task when you hand it in.",
                 False: "  This run was NOT accepted: what it did is still wrong somewhere; change the candidate and "
                        "try again while tries are left."}
HARDEN_LINE = ("  No tool of yours was called in this run: whatever the agent derived, it derived itself, and that "
               "is one rollout. With {left} tries left, harden before you finish (step 5): candidates/package-2 = "
               "this package plus the tool that derives the value from the inputs the run read, try it once, then "
               "finish with this package first and package-2 last.")


def accepted_line(success: bool, *, acted, tries_left: int) -> str:
    """The bit after a try (v2.1, fourth cut): accepted or not, and — when the accepted run called no tool of R's and
    tries are left — the hardening step, so the try's answer does not contradict the prompt's workflow."""
    line = ACCEPTED_LINE[bool(success)]
    if success and tries_left > 0 and not any(str(a).startswith("tool:") for a in acted or ()):
        line += "\n" + HARDEN_LINE.format(left=tries_left)
    return line
EXCERPT = 160                    # of a call's output or error in a digest
ARGS = 200                       # of a package tool's arguments in a digest (v2.1: R reads its tool's calls)
TOOL_SAID = 300                  # of a package tool's output in a digest
SAID = 600                       # of the final text
DETAIL = 240                     # of what a hook said
LIBRARY_ENTRIES_SHOWN = 12       # the newest entries whose files go into a session's inputs
WHY_CHARS = 600


# ---------------------------------------------------------------------------- label-free digests
def footprint_of(trace, session: str) -> dict:
    """{hooks, tools, skills}: the hooks that acted, the tools the model called, the skills read in one run."""
    hooks, tools, skills = set(), set(), set()
    for item in trace.events(session):
        e = item["event"]
        kind = e.get("kind")
        if kind == "hook" and e.get("action") not in (None, "none"):
            hooks.add(str(e.get("name")))
        elif kind == "gateway_call" and e.get("role") == "act":
            for c in (e.get("response") or {}).get("tool_calls") or ():
                if isinstance(c, dict) and c.get("name"):
                    tools.add(str(c["name"]))
        elif kind == "skill_read":
            skills.add(str(e.get("name")))
    return {"hooks": hooks, "tools": tools, "skills": skills}


def acted_components(conditions: list[dict], foot: dict) -> list[str]:
    out = []
    for c in conditions:
        name, kind = c.get("name"), c.get("kind")
        hit = name == "*" or (kind == "hook" and name in foot["hooks"]) or (kind == "tool" and name in foot["tools"]) \
            or (kind == "skill" and name in foot["skills"])
        if hit:
            out.append(key(c))
    return out


def digest(trace, session: str, *, detail_tools=()) -> dict:
    """One run as R and the composer read it: no score. The calls in order (a base tool's name, method, route and
    whether it errored, with an excerpt of an error; a tool named in `detail_tools` — the package's own — with its
    arguments and output), the hooks that acted and what they said, the writes by route, the skills read, the final
    text, the steps."""
    calls, hooks, writes, skills, errors = [], [], {}, [], 0
    steps = 0
    pending: dict = {}                  # a package tool's call id -> its row, until its result comes back
    for item in trace.events(session):
        e = item["event"]
        kind = e.get("kind")
        if kind == "gateway_call" and e.get("role") == "act":
            steps += 1
            if detail_tools:            # the package's own tools are not broker calls: the model's call and its
                for m in e.get("messages_tail") or ():       # result live in the conversation (v2.1)
                    if m.get("role") == "tool":
                        for res in m.get("results") or ():
                            row = pending.pop(str(res.get("id")), None)
                            if row is not None:
                                row["error"] = bool(res.get("is_error"))
                                row["said"] = str(res.get("output") or "")[:TOOL_SAID]
                                errors += int(row["error"])
                for c in (e.get("response") or {}).get("tool_calls") or ():
                    if str(c.get("name")) in detail_tools:
                        row = {"tool": str(c.get("name")), "method": None, "route": None, "error": False,
                               "args": json.dumps(c.get("args") or {}, ensure_ascii=False)[:ARGS], "said": ""}
                        calls.append(row)
                        pending[str(c.get("id"))] = row
        elif kind == "tool":
            args = e.get("args") if isinstance(e.get("args"), dict) else {}
            name = str(e.get("name") or "")
            method = str(args.get("method") or ("GET" if name == "api_fetch" else "")).upper()
            route = str(args.get("url") or "").split("?")[0].split("//")[-1]
            is_error = bool(e.get("is_error"))
            errors += int(is_error)
            row = {"tool": name, "method": method or None, "route": route or None, "error": is_error}
            if e.get("read_only"):          # a hook's own read-only call (ctx.tool(read_only=True)), not the agent's
                row["by"] = "hook"
            if is_error:
                row["said"] = str(e.get("output") or "")[:EXCERPT]
            calls.append(row)
            if name == "api_fetch" and method not in ("GET", "HEAD", ""):
                k = f"{method} {route}"
                writes[k] = writes.get(k, 0) + 1
        elif kind == "hook" and e.get("action") not in (None, "none"):
            hooks.append({"site": e.get("site"), "name": e.get("name"), "action": e.get("action"),
                          "said": str(e.get("detail") or "")[:DETAIL]})
        elif kind == "skill_read":
            skills.append(str(e.get("name")))
    said = trace.finish(session)
    return {"session": session, "steps": steps, "calls": calls[:80], "errors": errors, "hooks": hooks,
            "writes": writes, "skills_read": skills, "termination": said.get("termination"),
            "final_text": str(said.get("reason") or said.get("text") or "")[:SAID]}


def contrast_text(reference: dict, probe: dict, *, task: str, dir_name: str, acted: list, probes_left: int) -> str:
    """What R reads of a try_task here: the probe beside the reference, no score."""
    def line(d):
        return (f"steps {d['steps']}, errors {d['errors']}; hooks that acted: "
                f"{[(h['site'], h['name'], h['action'], h['said'][:120]) for h in d['hooks']] or 'none'}; writes: "
                f"{d['writes'] or 'none'}; skills read: {d['skills_read'] or 'none'}; said: {d['final_text'][:300]!r}")
    own = [c for c in probe["calls"] if "args" in c]
    tools_line = ("\n  your tool's calls in this run: "
                  + "; ".join(f"{c['tool']}({c['args']}) -> {c.get('said', '')!r}" for c in own[:6])) if own else ""
    return (f"try_task candidates/{dir_name} on {task}: the package ran the request again. No score is read here: "
            f"what is certain is what acted and what changed. Your components that acted: "
            f"{', '.join(acted) or 'none (no footprint)'}.\n"
            f"  the first run ({reference['session']}): {line(reference)}\n"
            f"  this run ({probe['session']}, read_trajectory may open it): {line(probe)}{tools_line}\n"
            f"  {probes_left} try_task run(s) left for this task.")


def sources_of(holds: dict, calls, service_of_route) -> dict:
    """The world's sources beside one run: per service, what it holds (`holds`, from the adapter: names and counts) and
    whether the agent read it (a GET of its own), only a hook of R's read it (a read-only call made by a hook — the
    agent saw nothing of it), or the agent wrote to it (any other method). v2.1, fifth cut: three sessions read
    "slack: read" off their own hook's listing and spent a try on a hold that had fired for nothing."""
    read, hook_read, written = set(), set(), set()
    for c in calls or ():
        service = service_of_route(c.get("route") or "")
        if not service:
            continue
        if str(c.get("method") or "GET").upper() in ("GET", "HEAD"):
            (hook_read if c.get("by") == "hook" else read).add(service)
        else:
            written.add(service)
    return {svc: {"holds": what, "read": svc in read, "hook_read": svc in hook_read and svc not in read,
                  "written": svc in written} for svc, what in (holds or {}).items()}


def sources_line(sources: dict) -> str:
    """One line of the world's sources beside a run: what each holds; read by the agent, read only by a hook of
    R's, or never read; written or not."""
    def state(v):
        if v.get("read"):
            return "read by the agent"
        if v.get("hook_read"):
            return "listed only by your hook (the agent read nothing of it)"
        return "never read"
    return "; ".join(f"{svc} ({v.get('holds')}): " + state(v) + (", written" if v.get("written") else "")
                     for svc, v in (sources or {}).items()) or "none"


def tool_names(files: dict) -> set:
    """The tools a package's manifest offers."""
    try:
        return {str(t.get("name")) for t in (json.loads(files.get("manifest.json") or "{}").get("tools") or [])}
    except (ValueError, AttributeError):
        return set()


def files_hash(files: dict) -> str:
    """What a candidate's files are, for 'changed since its try' (v2.1)."""
    return hashlib.sha256(json.dumps(sorted(files.items()), ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------- the library
class Library:
    """<root>/library/: entries.json, INDEX.md and one directory per entry holding EXPERIENCE.md and the files the
    adopted package changed against the base. The whole adopted tree stays in the object store (files_ref)."""

    _lock = threading.Lock()            # tasks adopt in parallel; one writer at a time

    def __init__(self, root):
        self.dir = Path(root) / "library"
        self.path = self.dir / "entries.json"

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return list(json.loads(self.path.read_text(encoding="utf-8")).get("entries") or ())

    def add(self, *, task: str, dir_name: str, mechanism: str, files_ref: str, changed: dict, experience: dict,
            why: str, scope: str = "") -> dict:
        with self._lock:
            return self._add(task=task, dir_name=dir_name, mechanism=mechanism, files_ref=files_ref, changed=changed,
                             experience=experience, why=why, scope=scope)

    def _add(self, *, task, dir_name, mechanism, files_ref, changed, experience, why, scope) -> dict:
        entries = self.entries()
        entry_id = f"lib{len(entries) + 1}"
        entry = {"id": entry_id, "task": task, "dir": dir_name, "scope": scope, "mechanism": mechanism,
                 "files_ref": files_ref, "changed_files": sorted(changed), "experience": dict(experience),
                 "why": why[:WHY_CHARS]}
        folder = self.dir / entry_id
        folder.mkdir(parents=True, exist_ok=True)
        for p, text in changed.items():
            target = folder / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        (folder / "EXPERIENCE.md").write_text(self.experience_text(entry), encoding="utf-8")
        entries.append(entry)
        write_json(self.path, {"entries": entries})
        (self.dir / "INDEX.md").write_text(self.index_text(entries), encoding="utf-8")
        return entry

    @staticmethod
    def experience_text(entry: dict) -> str:
        x = entry.get("experience") or {}
        return (f"# {entry['id']} (adopted for one task; scope {entry.get('scope') or 'unscoped'})\n\n"
                f"mechanism: {entry['mechanism']}\nchanged files: {', '.join(entry['changed_files'])}\n\n"
                f"## Why it was adopted\n{entry.get('why') or ''}\n\n## Transferable lesson\n"
                f"- **cue**: {x.get('cue', '')}\n- **lesson**: {x.get('lesson', '')}\n- **evidence**: "
                f"{x.get('evidence', '')}\n- **confidence**: {x.get('confidence', '')}\n")

    @staticmethod
    def index_text(entries: list[dict]) -> str:
        if not entries:
            return "# Library\n\n_(empty: nothing adopted yet)_\n"
        lines = ["# Library", "", "Browse this index, then open the entries whose cue matches the run in front of you. "
                 "Their files are examples to copy and re-check, not part of the harness.", ""]
        for e in entries:
            x = e.get("experience") or {}
            lines += [f"## `library/{e['id']}/`", f"mechanism: {e['mechanism']}",
                      f"files: {', '.join(e['changed_files'])}", f"cue: {x.get('cue', '')}",
                      f"lesson: {x.get('lesson', '')}", ""]
        return "\n".join(lines)

    def inputs(self) -> dict[str, str]:
        """{inputs path: text}: INDEX.md and, for the newest LIBRARY_ENTRIES_SHOWN entries, EXPERIENCE.md and the
        changed files."""
        entries = self.entries()
        out = {"library/INDEX.md": self.index_text(entries)}
        for e in entries[-LIBRARY_ENTRIES_SHOWN:]:
            folder = self.dir / e["id"]
            for p in folder.rglob("*"):
                if p.is_file():
                    out[f"library/{e['id']}/{p.relative_to(folder).as_posix()}"] = p.read_text(encoding="utf-8")
        return out


# ---------------------------------------------------------------------------- the adoption of one task
def _json_reply(text: str) -> dict:
    text = str(text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        value = json.loads(m.group(0) if m else text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def verify_context(activity, settings: Settings, *, package) -> VerifyContext:
    train = tuple(activity.splits.ids("train"))
    rows = [{"task_id": t, "assertions": [dict(x) for x in activity.suite.grading(t).assertions],
             "state": activity.suite.grading(t).initial_state, "prompt": activity.suite.tasks[t].trigger}
            for t in train]
    ev = Evidence(activity)
    return VerifyContext(parent=package, seed=activity.package(activity.seed_release()), core=activity.k,
                         objects=activity.objects, worker=activity.worker(), suite=activity.suite, evidence=ev,
                         forbidden=forbidden_of(literal_index(rows)), train_tasks=train,
                         train_texts={t: activity.suite.tasks[t].trigger for t in train}, embed=None,
                         settings=settings, fixture_task=train[0] if train else None, study_tasks=(), cause_ids=())


def session_spend(account, prefix: str) -> float:
    """What the research calls of one operation family measured on `account`, in usd: those whose operation id is
    `<prefix>#...` (a task's session is `repeat:<task>`). The stream's workers share the account, so its total says
    nothing about one task."""
    book = account.budgets._read()
    return round(sum(float((r.get("measured") or {}).get("usd") or 0.0)
                     for r in (book.get("reservations") or {}).values()
                     if r.get("status") == "settled" and r.get("scope_id") == account.scope_id
                     and str(r.get("operation_id") or "").startswith(prefix + "#")), 6)


def adopt(activity, *, task_id: str, reference, base_release, run_arm, account, plan, library: Library,
          out=print, outcome: str | None = None, feedback: str = "attempts", rejected=None, sources=None) -> dict:
    """One task's Propose-Probe-Compose after its first encounter (`reference`: that attempt's TaskResult). `rejected`
    (feedback "items", v2.1): attempt_id -> the items the reviewer sent that attempt back for, kinds and objects only.
    `sources` (v2.1, second cut): (task_id, digest) -> the world's sources with what each holds (counts) and whether
    that run read or wrote it.
    -> {choice: "base" | dir, why, package_ref | None, files_ref | None, probes, candidates, entry | None,
    session, usd (what this task's session cost: its research calls and its probes, not the arm's total)}."""
    s = Settings.from_core(activity.k)
    package = activity.package(base_release)
    ev = Evidence(activity)
    trace = ev.trace
    ref_session = reference.evidence or reference.attempt_id
    trace.allow({ref_session})
    reference_digest = digest(trace, ref_session)
    request = activity.suite.tasks[task_id].trigger
    task_json = {"task_id": task_id, "request": request, "reference_run": reference_digest,
                 "note": "the agent's own run of this request: every call with its error flag, the hooks that acted, "
                         "the writes by route, the final text; no score is given or exists here"}
    if outcome:                     # the one bit of feedback (DESIGN §6.23, the second cut): sent back, not what for
        task_json["outcome"] = outcome
        task_json["note"] += ("; outcome: the request was NOT accepted — something the agent did or left undone was "
                              "wrong, and you are not told what")
    if sources is not None:         # v2.1, second cut: the world's sources beside the run (an inventory, no contents)
        task_json["sources"] = sources(task_id, reference_digest)
        task_json["note"] += ("; sources: every data source this request's world holds, what it holds (counts) and "
                              "whether this run read or wrote it")
    if rejected is not None:        # v2.1 (DESIGN §6.24): what it was sent back for — the checks' kinds and objects
        task_json["rejected_items"] = list(rejected(reference.attempt_id))
        task_json["note"] += ("; rejected_items: what the reviewer sent it back for — the kind of each check and what "
                              "it is about, never what it should have contained")
    from ..improver.round import EVENTS, RETURNS  # the package contract's site table, as the rounds render it
    sites = "\n".join(f"    {site}: event {list(EVENTS[site])} -> may answer {list(RETURNS[site])}" for site in EVENTS)
    api = (PROMPTS / "package_api.md").read_text(encoding="utf-8").replace("{sites}", sites)
    inputs = {"task.json": json.dumps(task_json, ensure_ascii=False, indent=1), "package_api.md": api,
              **library.inputs()}
    kinds = [k for k in activity.k["harness"]["kinds"] if k not in ("memory", "model", "test")]
    system = (PROMPTS / "repeat.md").read_text(encoding="utf-8")
    notes = {k: v.replace("{max_probes}", str(int(plan.max_probes))) for k, v in REPEAT_TOOL_NOTES.items()}
    for name, value in {"max_turns": s.session_max_turns, "max_edits": s.session_max_edits,
                        "max_read": s.session_max_read_bytes, "max_read_total": s.session_max_read_total,
                        "max_tokens": int(activity.k["research_model"]["max_tokens"]),
                        "max_probes": int(plan.max_probes),
                        "feedback": (FEEDBACK_ITEMS if feedback == "items" else FEEDBACK_ATTEMPTS
                                     if feedback == "attempts" else FEEDBACK_OUTCOME if outcome
                                     else FEEDBACK_NONE)}.items():
        system = system.replace("{" + name + "}", str(value))
    vctx = verify_context(activity, s, package=package)
    worker = activity.worker()
    wsession = worker.start()
    tree = WorkTree(wsession)
    probes: dict[str, dict] = {}            # dir -> {session, digest, acted, package_ref, files_ref}
    readable = {ref_session}                # grows with each try's session: read_trajectory may open them
    spent = {"probes": 0, "usd": 0.0}
    researcher = activity.research_provider(f"repeat:{task_id}", account=account)

    def probe(dir_name: str, files: dict) -> tuple[str, bool]:
        if dir_name in probes and probes[dir_name]["files_hash"] == files_hash(files):
            return (f"candidates/{dir_name} is unchanged since its try on this task: change it before trying it again "
                    f"(a changed candidate may be tried again while tries are left), or finish", True)
        if spent["probes"] >= int(plan.max_probes):
            return f"refused: the {plan.max_probes} try_task runs of this task are used", True
        try:
            cand = build(activity.objects, files, limits=Limits.from_core(activity.k),
                         seed=activity.package(activity.seed_release()), parent=package)
        except PackageError as e:
            return f"the package does not build: {e}"[:600], True
        v1 = unit_check(files, None, vctx)
        if not v1.get("passed", not v1.get("detail")):
            return f"V1 failed, nothing run: {v1.get('detail')}"[:1200], True
        rows = run_arm(activity, release=base_release, split=plan.split, candidate=f"probe:{dir_name}", role="probe",
                       pairs=[(task_id, 0)], account=account, arm="probe", package=cand,
                       notes=f"repeat probe of candidates/{dir_name} on {task_id}")
        spent["probes"] += 1
        row = next((r for r in rows if r.task_id == task_id), None)
        if row is not None and row.cost_usd is not None:
            spent["usd"] += float(row.cost_usd)
        if row is None or row.validity != "valid":
            return f"the probe did not read the task ({getattr(row, 'validity', 'no attempt')})", True
        session_id = row.evidence or row.attempt_id
        trace.allow({session_id})
        readable.add(session_id)
        conds = condition(components(package, files))
        acted = acted_components(conds, footprint_of(trace, session_id))
        d = digest(trace, session_id, detail_tools=tool_names(files) - tool_names(package.files))
        probes[dir_name] = {"session": session_id, "digest": d, "acted": acted, "package_ref": cand.ref(),
                            "files_ref": activity.objects.put_json("candidate_files", files), "attempt": row.attempt_id,
                            "accepted": bool(row.success), "files_hash": files_hash(files)}
        out(f"repeat     {task_id}: probe of {dir_name}: acted {acted or 'nothing'}, {d['steps']} steps, "
            f"writes {sum(d['writes'].values())}" + (f", {'ACCEPTED' if row.success else 'not accepted'}"
                                                     if feedback in ("attempts", "items") else ""))
        text = contrast_text(reference_digest, d, task=task_id, dir_name=dir_name, acted=acted,
                             probes_left=int(plan.max_probes) - spent["probes"])
        if sources is not None:
            probes[dir_name]["sources"] = sources(task_id, d)
            text += "\n  sources in this run: " + sources_line(probes[dir_name]["sources"])
        if feedback in ("attempts", "items"):   # the one bit per attempt: the request was accepted, or sent back
            text += "\n" + accepted_line(bool(row.success), acted=acted,
                                          tries_left=int(plan.max_probes) - spent["probes"])
        if rejected is not None and not row.success:    # v2.1: and what this run was sent back for
            items = list(rejected(row.attempt_id))
            probes[dir_name]["rejected"] = items
            text += "\n  sent back for: " + ("; ".join(items) or "(no item named)")
        return text, False

    session = None
    try:
        wsession.load({})
        tree.init({p: t for p, t in package.files.items() if p != MEMORY_PATH}, inputs,
                  max_bytes=Limits.from_core(activity.k).max_package_bytes)

        def check(dir_name: str, raw: dict) -> str:
            files, error = tree.fetch(dir_name)
            if files is None:
                return f"candidates/{dir_name} cannot be read back: {error}"
            decl, why = read_declaration({**raw, "dir": dir_name}, dirs=[dir_name], kinds_allowed=kinds,
                                         require_causes=False)
            if why:
                return f"the declaration is not readable: {why}"
            return run_ladder(with_memory(package, files), decl, vctx, rungs=("V0", "V1")).text()

        def tests(dir_name: str) -> str:
            files, error = tree.fetch(dir_name)
            if files is None:
                return f"candidates/{dir_name} cannot be read back: {error}"
            return unit_check(with_memory(package, files), None, vctx)["detail"] or "the package tests pass"

        def tryer(dir_name: str, _task: str) -> tuple[str, bool]:
            files, error = tree.fetch(dir_name)
            if files is None:
                return f"candidates/{dir_name} cannot be read back: {error}", True
            return probe(dir_name, with_memory(package, files))

        session = CodingSession(researcher=researcher, work=tree, trace=trace, check=check, tests=tests, settings=s,
                                system=system, inputs={"task": task_id, "inputs": sorted(f"inputs/{n}" for n in inputs)},
                                readable=readable, kinds=kinds, tryer=tryer, require_causes=False,
                                tool_notes=notes)
        result = session.start()
        candidates = []
        for decl in result.declarations:
            files, error = tree.fetch(decl.dir)
            if files is None:
                continue
            files = with_memory(package, files)
            verdict = run_ladder(files, decl, vctx, rungs=("V0", "V1"))
            if not verdict.passed():
                out(f"repeat     {task_id}: candidates/{decl.dir} failed its free rungs: {verdict.text()[:200]}")
                continue
            if decl.dir not in probes or probes[decl.dir]["files_hash"] != files_hash(files):
                # R never tried it, or changed it after its last try (v2.1): the host probes what is handed in; with
                # no try left, the last tried version stands — it is what an adoption installs
                text, bad = probe(decl.dir, files)
                if bad and decl.dir in probes and text.startswith("refused"):
                    out(f"repeat     {task_id}: candidates/{decl.dir} changed after its last try and no try is left: "
                        f"the tried version stands")
                elif bad:
                    out(f"repeat     {task_id}: candidates/{decl.dir} not probed: {text[:160]}")
                    continue
            candidates.append(decl)
        # v2.1, sixth cut: an accepted try the session never handed in (finance-v24: R's replies were cut at the
        # output limit twice after its accepted try, and the session ended without a finish). The decision was the
        # acceptance; the declaration is the host's, read off the diff, and V0 + V1 run on the tried files as on
        # any hand-in. What an adoption installs is the tried version in every case.
        declared = {d.dir for d in result.declarations}
        for dir_name, p in probes.items():
            if not p.get("accepted") or dir_name in declared:
                continue
            tried = activity.objects.get(p["files_ref"])
            manifest = json.loads(tried.get("manifest.json") or "{}")
            sites = {h.get("name"): h.get("site") for h in manifest.get("hooks") or []}
            comps = [{"kind": c["kind"], "site": (sites.get(c["name"]) if c["kind"] == "hook" else c["name"]),
                      "cause_ids": [], "predicted_affected": [], "trigger": "undeclared: the accepted try stands"}
                     for c in components(package, tried) if c["kind"] in kinds]
            raw = {"dir": dir_name, "scope": "invariant", "components": comps,
                   "mechanism": (f"undeclared: the session ended ({result.ended or 'without a finish'}) after this "
                                 f"candidate's try was accepted; the tried version stands")}
            decl, why = read_declaration(raw, dirs=[dir_name], kinds_allowed=kinds, require_causes=False)
            if decl is None:
                out(f"repeat     {task_id}: candidates/{dir_name} was accepted and never handed in; the host cannot "
                    f"declare it: {why}")
                continue
            verdict = run_ladder(tried, decl, vctx, rungs=("V0", "V1"))
            if not verdict.passed():
                out(f"repeat     {task_id}: candidates/{dir_name} was accepted and never handed in; it fails its free "
                    f"rungs: {verdict.text()[:200]}")
                continue
            out(f"repeat     {task_id}: candidates/{dir_name} was accepted and never handed in: the host declares it "
                f"from the diff and it stands")
            candidates.append(decl)
    finally:
        wsession.close(force=True)
        transcript = activity.objects.put_json("session_transcript", session.transcript()) if session else None
    # ---- compose: with the per-attempt bit, the accepted candidate is adopted (the latest, R's most refined);
    # without it, one aux call over the reference and the probes, no score
    choice, why = "base", "no candidate reached the composer"
    if candidates and feedback in ("attempts", "items"):
        accepted = [d for d in candidates if probes[d.dir]["accepted"]]
        if accepted:
            choice, why = accepted[-1].dir, f"its run on this request was accepted (candidates/{accepted[-1].dir})"
        else:
            why = f"no candidate's run was accepted ({[d.dir for d in candidates]}): the base stays"
    elif candidates:
        compose_in = {"request": request, "reference_run": reference_digest,
                      "candidates": [{"dir": d.dir, "scope": d.scope, "mechanism": d.mechanism,
                                      "components": [c.key for c in d.components],
                                      "acted": probes[d.dir]["acted"], "run": probes[d.dir]["digest"]}
                                     for d in candidates]}
        turn = researcher.chat((PROMPTS / "compose.md").read_text(encoding="utf-8"),
                               [{"role": "user", "content": json.dumps(compose_in, ensure_ascii=False)}], [])
        reply = _json_reply(getattr(turn, "text", ""))
        picked = str(reply.get("choice") or "base")
        why = str(reply.get("why") or "")[:WHY_CHARS]
        choice = picked if picked in {d.dir for d in candidates} else "base"
    chosen = next((d for d in candidates if d.dir == choice), None)
    entry = None
    if chosen is not None:
        # ---- reflect: verdicts per component, at most one lesson
        reflect_in = {"request": request, "mechanism": chosen.mechanism, "scope": chosen.scope,
                      "components": [c.key for c in chosen.components], "reference_run": reference_digest,
                      "adopted_run": probes[chosen.dir]["digest"], "acted": probes[chosen.dir]["acted"],
                      "composer_why": why}
        turn = researcher.chat((PROMPTS / "reflect.md").read_text(encoding="utf-8"),
                               [{"role": "user", "content": json.dumps(reflect_in, ensure_ascii=False)}], [])
        reply = _json_reply(getattr(turn, "text", ""))
        x = reply.get("entry")
        experience = x if isinstance(x, dict) and all(str(x.get(k) or "").strip() for k in ("cue", "lesson", "evidence")) \
            else {}
        files = activity.objects.get(probes[chosen.dir]["files_ref"])
        changed = {p: t for p, t in files.items() if package.files.get(p) != t and p != MEMORY_PATH}
        if experience:
            entry = library.add(task=task_id, dir_name=chosen.dir, mechanism=chosen.mechanism,
                                files_ref=probes[chosen.dir]["files_ref"], changed=changed, experience=experience,
                                why=why, scope=chosen.scope)
    usd = round(session_spend(account, f"repeat:{task_id}") + spent["usd"], 6)
    return {"choice": choice, "why": why, "package_ref": probes[choice]["package_ref"] if chosen else None,
            "accepted": {d: bool(p.get("accepted")) for d, p in probes.items()} if feedback in ("attempts", "items")
            else None,
            "files_ref": probes[choice]["files_ref"] if chosen else None, "probes": spent["probes"],
            "candidates": [d.dir for d in candidates], "entry": entry["id"] if entry else None,
            "session": transcript, "usd": usd,
            "verdicts": reply.get("edits") if chosen is not None else None}


def with_memory(package, files: dict) -> dict:
    out = {p: t for p, t in files.items() if p != MEMORY_PATH}
    if MEMORY_PATH in package.files:
        out[MEMORY_PATH] = package.files[MEMORY_PATH]
    return out
