"""The write path of the train stream: StreamStep (docs/design/MEMORY.md §5, §7).

The library phase (activity/phases.py) schedules the train attempts; after each batch of `stream_batch` tasks it
hands StreamStep the tasks and their attempts, and StreamStep does the rest:

 1. rebuilds the attempts' trajectories (baseline, first encounter, retry) with `trajectory_of(session)`
    (runtime.trace.TraceStore, train only) and cuts them to K's sizes (every tool output to traj_tool_chars, every
    trajectory to traj_total_chars), counting cuts;
 2. labels the bridge step (`set_step`) and has ReMe summarise each task's trajectories, a batch's tasks
    concurrently;
 3. checks each summary against the bridge's record of the step (`bridge_calls_of(step)`, the bridge's
    [{call_id, step, op, status, ms}]): every LLM reply the sidecar reports for it must be a 200 call of that op on
    the bridge under this step (matched by the bridge's call id, which is the id of the completion ReMe received, so a
    call still in flight from an earlier step is never taken for this one's), and the ops its trajectories need must
    be among them (a call that bypassed the bridge is not metered). Any non-200 call stops the step; the summaries
    that passed are kept in the ledger and only the others are summarised again. The replies ReMe read by a fallback
    are counted (a validation reply without a ```json block, which ReMe takes as valid with score 0.5; an
    extraction reply without JSON, from which ReMe extracts nothing; a validation reply ReMe could not read);
 4. checks every memory: lengths, the literal check with one placeholder pass (rules.literal_pass) over its text
    and the ReMe metadata kept with it (rules.reme_metadata), and reach (its when_to_use must retrieve at least
    min_other_tasks train tasks beyond its sources at min_score);
 5. deduplicates against the live library: a stored entry whose when_to_use is within store_dedup_threshold of this
    one's (when_to_use against stored when_to_use, one space) and whose content is within
    store_dedup_content_threshold of this one's is the same lesson; and by id (chroma ignores a second add of an id).
    A duplicate gains a source, no row; a matching trigger with a different lesson is stored;
 6. stamps provenance and adds the rest, serially in the batch's order (the frozen stream order), so the library's
    content is deterministic;
 7. credits the first encounter's shown entries by its citation line, then retires;
 8. checkpoints: the canonical live library first (`live_path`), then the ledger (`ledger_path`).

The ledger (<workspace>/memory/<line>/stream.json) is the truth; the store is reconciled to it (`resume`). It holds
the stream's planned order (`plan`), fixed once. A batch's summaries are written to the ledger as `pending` before
anything is stored, so a crash anywhere after a paid summary re-commits it without calling ReMe again; a crash
between a store write and the ledger write leaves orphans `resume` removes. A task whose first encounter could not
run is recorded as `lost` (no summary, no credit) and is not in the readout.
"""
from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..contracts import MemorySidecarError
from . import digest, snapshot_digest
from .client import canonical_rows, parse_jsonl
from .rules import (IMPORTED, ORIGINS, REME_META_KEYS, REPLY_FORMAT_COUNTERS, content_id, cosine, credit, evictable,
                    literal_pass, new_stats, origin_of, parse_citation, readout, reme_metadata, reply_format_problems,
                    require_train, resolve_ids, retire_reason, snapshot_abort, snapshot_rows, stamp)
from .settings import settings_of

SCHEMA = "memory_stream.v1"
CUT_KEYS = ("tool_outputs_cut", "messages_cut", "chars_cut", "no_trajectory")
DEDUP_CANDIDATES = 5       # stored entries compared by content once their when_to_use is within the threshold


class MemoryStepError(RuntimeError):
    """A stream step that cannot be trusted: a research call failed on the bridge, the sidecar failed the summary,
    or calls are missing from the bridge. Nothing of the step is stored; the ledger is as it was."""


def write_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def text_of(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") if isinstance(p, dict) else str(p) for p in content)
    return str(content)


def cut_trajectory(messages: list[dict], *, tool_chars: int, total_chars: int) -> tuple[list[dict], dict]:
    """The trajectory ReMe reads: system messages left out (ReMe's step formatting skips them), content made text,
    each tool output cut to `tool_chars`, and the whole cut to `total_chars` by dropping middle messages (the task
    and the first steps, and the last steps, are kept). -> (messages, {tool_outputs_cut, messages_cut, chars_cut})."""
    counts = {"tool_outputs_cut": 0, "messages_cut": 0, "chars_cut": 0}
    out = []
    for m in messages or ():
        role = m.get("role")
        if role == "system":
            continue
        msg = {k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "tool_call_id", "name")}
        msg["content"] = text_of(m.get("content"))
        if not msg.get("tool_calls"):
            msg.pop("tool_calls", None)
        if role == "tool" and len(msg["content"]) > tool_chars:
            cut = len(msg["content"]) - tool_chars
            msg["content"] = msg["content"][:tool_chars] + f"\n[... {cut} characters cut by the platform]"
            counts["tool_outputs_cut"] += 1
            counts["chars_cut"] += cut
        out.append(msg)

    def size(m):
        return len(m["content"]) + sum(len(json.dumps(c)) for c in m.get("tool_calls") or ())
    total = sum(size(m) for m in out)
    if total <= total_chars:
        return out, counts
    head, tail, used = [], [], 0
    budget = total_chars // 2
    i, j = 0, len(out) - 1
    while i <= j and used + size(out[i]) <= budget:
        used += size(out[i])
        head.append(out[i])
        i += 1
    budget = total_chars - used
    used = 0
    while j >= i and used + size(out[j]) <= budget:
        used += size(out[j])
        tail.insert(0, out[j])
        j -= 1
    dropped = out[i:j + 1]
    counts["messages_cut"] = len(dropped)
    counts["chars_cut"] += sum(size(m) for m in dropped)
    marker = {"role": "user", "content": f"[... {len(dropped)} messages ({sum(size(m) for m in dropped)} "
                                          f"characters) cut by the platform]"}
    return head + [marker] + tail, counts


class StreamStep:
    """The stream's write path over one live collection; see the module docstring."""

    def __init__(self, client, settings, *, ledger_path, live_path, collection: str, business_line: str,
                 activity: str, heldout_domains, train_texts: dict, forbidden_literals, allowed_literals=(),
                 trajectory_of, bridge_calls_of, set_step, concurrency: int = 4):
        self.client = client
        self.s = settings_of(settings)
        self.ledger_path, self.live_path = Path(ledger_path), Path(live_path)
        self.collection, self.line, self.activity = collection, business_line, activity
        self.heldout = tuple(heldout_domains or ())
        require_train("train", business_line, self.heldout)       # a held-out line runs no stream at all
        self.train_texts = dict(train_texts)
        self.forbidden, self.allowed = set(forbidden_literals or ()), set(allowed_literals or ())
        self.trajectory_of, self.bridge_calls_of, self.set_step = trajectory_of, bridge_calls_of, set_step
        self.concurrency = max(int(concurrency or 1), 1)    # summaries at once: the machine's, not K's
        self._qvec: dict | None = None
        self.settings_digest = digest(self.s.to_json())[:16]
        self.ledger = self._load()

    # ------------------------------------------------------------------------------------------- ledger
    def _fresh(self) -> dict:
        return {"schema": SCHEMA, "collection": self.collection, "business_line": self.line,
                "activity": self.activity, "settings_digest": self.settings_digest, "status": "streaming", "seq": 0,
                "plan": None, "order": [], "processed": {}, "readings": {}, "entries": {}, "dropped": [], "lost": {},
                "pending": None,
                "imports": [], "counters": {**{k: 0 for k in CUT_KEYS}, **{k: 0 for k in REPLY_FORMAT_COUNTERS},
                                            "research_calls": 0, "research_calls_failed": 0,
                                            "research_calls_unattributed": 0, "steps": 0, "summarized": 0,
                                            "validated": 0, "batch_dedup_dropped": 0, "placeholder_fixed": 0}}

    def _load(self) -> dict:
        if not self.ledger_path.exists():
            return self._fresh()
        led = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        if led.get("schema") != SCHEMA:
            raise ValueError(f"{self.ledger_path}: schema {led.get('schema')!r}, not {SCHEMA}")
        for key, mine in (("collection", self.collection), ("business_line", self.line),
                          ("settings_digest", self.settings_digest)):
            if led.get(key) != mine:
                raise ValueError(f"{self.ledger_path}: the stream's {key} is {led.get(key)!r}, this one's {mine!r}: "
                                 f"a stream is not continued under another K or into another collection")
        return led

    @property
    def entries(self) -> dict:
        return self.ledger["entries"]

    def active(self) -> dict:
        return {k: e for k, e in self.entries.items() if e.get("status") == "active"}

    def _save_ledger(self) -> None:
        write_atomic(self.ledger_path, json.dumps(self.ledger, indent=1, sort_keys=True, ensure_ascii=False))

    def snapshot_text(self) -> str:
        """The live library as canonical JSONL with the ledger's sources and stats merged in."""
        rows = parse_jsonl(self.client.dump(self.collection))
        return canonical_rows(snapshot_rows(rows, self.entries))

    def checkpoint(self) -> None:
        """The library first, then the ledger (MEMORY §5 step 8)."""
        write_atomic(self.live_path, self.snapshot_text())
        self._save_ledger()

    def live_view(self) -> dict:
        """What retrieval.LibraryReader needs to read the live library during the stream (leave-one-out by the
        ledger; the store holds exactly these entries, which the read checks)."""
        return {"collection": self.collection,
                "sources": {k: list(e.get("sources") or ()) for k, e in self.active().items()}}

    def plan(self, order: list[str]) -> list[str]:
        """The stream's order, fixed the first time and checked every time after (a resume continues the same
        stream, never another one). -> the tasks of the plan not processed yet, in order."""
        order = list(order)
        unknown = sorted(set(order) - set(self.train_texts))
        if unknown or len(set(order)) != len(order):
            raise MemoryStepError(f"the stream's order names tasks that are not its train tasks, or one twice: "
                                  f"{unknown[:5]}")
        planned = self.ledger.get("plan")
        if planned is None:
            self.ledger["plan"] = {"order": order, "digest": digest({"order": order})[:16]}
            self._save_ledger()
        elif planned["order"] != order:
            raise MemoryStepError(f"the stream's order {planned['digest']} is fixed; this one is "
                                  f"{digest({'order': order})[:16]}")
        return [t for t in order if t not in self.ledger["processed"]]

    def lose(self, task_hash: str, why: str) -> dict:
        """A task whose first encounter could not run (invalid after the refill rule): no summary, no credit, not in
        the readout; recorded so a resume does not run it again."""
        if task_hash in self.ledger["processed"]:
            return self.ledger["processed"][task_hash]
        if self.ledger.get("pending"):
            raise MemoryStepError(f"step {self.ledger['pending']['step']} is pending: process it first")
        record = {"n": len(self.ledger["order"]), "lost": why}
        self.ledger["lost"][task_hash] = why
        self.ledger["processed"][task_hash] = record
        self._save_ledger()
        return record

    def supersede(self, entry_id: str, by: str) -> None:
        """An entry whose promotion was installed (MEMORY §8): superseded in the ledger, gone from the live store,
        never shown again."""
        from .promote import mark_superseded
        mark_superseded(self.entries, entry_id, by)
        self.client.retire([entry_id], self.collection)
        self.checkpoint()

    # ------------------------------------------------------------------------------------------- resume
    def resume(self) -> dict:
        """Reconcile the store with the ledger: rows the ledger does not hold as active are deleted (a crash between a
        store write and the ledger write), active entries the store lacks are added back from the ledger's copy.
        The live file is rewritten. A pending batch is re-committed by the next process_batch without a new
        summary."""
        store = {r["memory_id"] for r in parse_jsonl(self.client.dump(self.collection))}
        active = set(self.active())
        orphans, missing = sorted(store - active), sorted(active - store)
        if orphans:
            self.client.retire(orphans, self.collection)
        if missing:
            self.client.add([self.entries[k]["row"] for k in missing], self.collection)
        write_atomic(self.live_path, self.snapshot_text())
        return {"orphans_removed": orphans, "restored": missing, "pending": (self.ledger.get("pending") or {})
                .get("step")}

    # ------------------------------------------------------------------------------------------- embeddings
    def _task_vectors(self) -> dict:
        if self._qvec is None:
            hashes = sorted(self.train_texts)
            vecs = []
            for i in range(0, len(hashes), 64):
                vecs += self.client.embed([self.train_texts[h] for h in hashes[i:i + 64]], query=True)
            self._qvec = dict(zip(hashes, vecs))
        return self._qvec

    def siblings(self, task_hash: str) -> set:
        """The train tasks whose text is at least sibling_sim similar to this one's (the task itself included)."""
        q = self._task_vectors()
        mine = q.get(task_hash)
        if mine is None:
            return {task_hash}
        return {t for t, v in q.items() if t == task_hash or cosine(mine, v) >= self.s.sibling_sim}

    def reach(self, when: str, sources: set) -> int:
        """How many train tasks beyond `sources` the when_to_use retrieves at min_score (as retrieval scores it:
        the task as a query, the entry as a document)."""
        doc = self.client.embed([when])[0]
        return sum(1 for t, v in self._task_vectors().items()
                   if t not in sources and cosine(v, doc) >= self.s.min_score)

    # ------------------------------------------------------------------------------------------- the step
    def process(self, task: dict, first: dict, retry: dict | None = None, baseline: dict | None = None) -> dict:
        return self.process_batch([(task, first, retry, baseline)])[0]

    def process_batch(self, items: list) -> list[dict]:
        """One stream step over tasks in the frozen stream order: [(task, first, retry, baseline)] with
        task = {task_hash, text, b, stable_pass} and each attempt {session, reward, passed, release_ref, shown,
        final_reason, final_text, trace_refs} (`shown`: the ids the memory gave the attempt, whole or the 8 hex the
        agent saw; `retry` and `baseline` may be None). -> per task what was done (a task already processed returns
        what was done then and costs nothing)."""
        if self.ledger.get("status") != "streaming":
            raise MemoryStepError(f"the stream is {self.ledger.get('status')}")
        done = self.ledger["processed"]
        todo = [it for it in items if it[0]["task_hash"] not in done]
        hashes = [it[0]["task_hash"] for it in items]
        if len(set(hashes)) != len(hashes):
            twice = sorted(h for h in set(hashes) if hashes.count(h) > 1)
            raise MemoryStepError(f"a batch holds a task twice: {twice}")
        for task, *_ in todo:
            self._check_task(task)
        if todo:
            pending = self.ledger.get("pending")
            hashes = [it[0]["task_hash"] for it in todo]
            if pending is not None and sorted(pending["batch"]) != sorted(hashes):
                raise MemoryStepError(f"step {pending['step']} is pending for {sorted(pending['batch'])}; process "
                                      f"that batch first (resume() after a crash, then the same items)")
            try:
                if pending is None or set(pending["tasks"]) != set(hashes):
                    pending = self._summarize(todo, pending)
                for task, first, retry, baseline in todo:
                    self._commit(task, first, retry, baseline, pending)
                self._count(pending)
                self.ledger["pending"] = None
                self.checkpoint()
            except BaseException:
                # the ledger on disk is the truth: drop what this call changed in memory (a stored row it added is
                # an orphan resume() removes; a pending summary on disk is committed again without a new call)
                self.ledger = self._load()
                raise
        return [done[it[0]["task_hash"]] for it in items]

    def _check_task(self, task: dict) -> None:
        th = task["task_hash"]
        if th not in self.train_texts:
            raise MemoryStepError(f"task {th} is not one of the stream's train tasks")
        require_train(task.get("split", "train"), self.line, self.heldout)

    def _trajectories(self, task: dict, first: dict, retry, baseline) -> tuple[list[dict], dict]:
        counts = {"tool_outputs_cut": 0, "messages_cut": 0, "chars_cut": 0, "no_trajectory": 0}
        out = []
        for kind, att in (("baseline", baseline), ("first", first), ("retry", retry)):
            if att is None:
                continue
            try:
                messages = self.trajectory_of(att["session"])
            except Exception as e:  # noqa: BLE001 - a valid attempt that made no model call has no trajectory
                from ..runtime.trace import TraceAccessError
                if isinstance(e, TraceAccessError):
                    raise                  # a session that is not train is never summarised
                counts["no_trajectory"] += 1
                continue
            msgs, c = cut_trajectory(messages, tool_chars=self.s.traj_tool_chars,
                                     total_chars=self.s.traj_total_chars)
            for k in c:
                counts[k] += c[k]
            out.append({"task_id": task["task_hash"], "messages": msgs, "score": float(att["reward"]),
                        "metadata": {"query": task["text"], "session": att["session"], "kind": kind}})
        return out, counts

    def _expected_ops(self, trajectories: list[dict]) -> set:
        """The extraction ops ReMe calls the LLM for on these trajectories (each at least once)."""
        scores = [t["score"] for t in trajectories]
        ops = set()
        if "success" in self.s.origins and any(s >= self.s.success_threshold for s in scores):
            ops.add("success")
        if "failure" in self.s.origins and any(s < self.s.success_threshold for s in scores):
            ops.add("failure")
        if "comparative" in self.s.origins and len(scores) >= 2 and max(scores) > min(scores):
            ops.add("comparative")
        return ops

    def _summarize(self, todo: list, pending: dict | None) -> dict:
        """Summarise the batch's tasks that have no kept summary yet, under a new step label (step 2), and keep each
        summary that passes the bridge check (step 3). The kept summaries are on disk before anything is stored or
        anything raised: when any call of the step failed, or a summary failed or did not check, MemoryStepError is
        raised and the next call summarises only the tasks still without one. -> the pending batch, complete."""
        if pending is None:
            pending = {"batch": [it[0]["task_hash"] for it in todo], "tasks": {}, "steps": [], "step": None}
        self.ledger["seq"] += 1
        step = f"{self.collection}:{self.ledger['seq']:05d}"   # unique on a bridge shared by several streams
        pending["step"] = step
        self.ledger["pending"] = pending
        self._save_ledger()           # a step label is used once: a failed step's calls never count for a retry
        prepared, cuts = {}, {}
        for task, first, retry, baseline in todo:
            th = task["task_hash"]
            if th not in pending["tasks"]:
                prepared[th], cuts[th] = self._trajectories(task, first, retry, baseline)
        self.set_step(step)
        results, errors = {}, {}

        def one(th):
            if not prepared[th]:          # no attempt of the task left a trajectory: nothing to summarise, no call
                results[th] = {"memory_list": [], "validated": 0, "replies": [], "unparsed": []}
                return
            try:
                results[th] = self.client.summarize(prepared[th])
            except MemorySidecarError as e:
                errors[th] = str(e)
        with ThreadPoolExecutor(max_workers=max(1, min(len(prepared), self.concurrency))) as ex:
            list(ex.map(one, list(prepared)))
        calls = list(self.bridge_calls_of(step) or ())
        by_id = {c.get("call_id"): c for c in calls}
        bad = [c for c in calls if int(c.get("status") or 0) != 200]
        problems, attributed = {}, set()
        for th in prepared:
            if th not in results:
                continue
            res = results[th]
            attributed |= {r.get("call_id") for r in res["replies"]}
            why = self._check_replies(res, prepared[th], by_id)
            if why:
                problems[th] = why
                continue
            pending["tasks"][th] = {"memory_list": res["memory_list"], "validated": res["validated"], "step": step,
                                    "cuts": cuts[th], "replies": self._reply_counts(res)}
        unattributed = sorted(str(c.get("call_id")) for c in calls
                              if int(c.get("status") or 0) == 200 and c.get("call_id") not in attributed)
        kept = sorted(th for th in prepared if th in pending["tasks"])
        pending["steps"].append({"step": step, "calls": len(calls), "failed": len(bad),
                                 "unattributed": unattributed, "kept": kept})
        self._save_ledger()           # the paid summaries that passed are on disk before anything is stored
        still = len(prepared) - len(kept)
        tail = (f"; {len(kept)} summaries that passed are kept and {still} will be summarised again"
                if kept else "")
        if bad:
            raise MemoryStepError(f"step {step}: {len(bad)} of {len(calls)} research calls failed on the bridge ("
                                  + ", ".join(f"{c.get('call_id')} {c.get('op')} HTTP {c.get('status')}"
                                              for c in bad[:8]) + "); nothing of the step is stored" + tail)
        if errors:
            raise MemoryStepError(f"step {step}: the sidecar failed the summary of {sorted(errors)}: "
                                  + " | ".join(v[:500] for v in errors.values()) + tail)
        if problems:
            raise MemoryStepError(f"step {step}: summaries whose LLM calls are not the bridge's 200s of this step "
                                  f"(calls that bypass the bridge are not metered): "
                                  + " | ".join(f"{th}: " + "; ".join(v[:4]) for th, v in sorted(problems.items()))
                                  + tail)
        return pending

    def _check_replies(self, result: dict, trajectories: list[dict], by_id: dict) -> list[str]:
        """Why a summary's LLM replies are not this step's metered calls (empty when they are): each must be a call
        the bridge recorded under the step, with the same op, answered 200 and streamed to its end; the ops its
        trajectories need must be among them; no more memories validated than validation calls made."""
        out, replies = [], list(result.get("replies") or ())
        for r in replies:
            cid, op = r.get("call_id"), r.get("op")
            c = by_id.get(cid) if cid else None
            if c is None:
                out.append(f"{op} reply {cid} is not on the bridge under this step")
            elif c.get("op") != op or int(c.get("status") or 0) != 200:
                out.append(f"{op} reply {cid} is {c.get('op')} HTTP {c.get('status')} on the bridge")
            if not r.get("done"):
                out.append(f"{op} reply {cid} did not finish")
        missing = sorted(self._expected_ops(trajectories) - {r.get("op") for r in replies})
        if missing:
            out.append(f"no {missing} call for trajectories that need one")
        validations = sum(1 for r in replies if r.get("op") == "validation")
        if int(result.get("validated") or 0) > validations:
            out.append(f"{result.get('validated')} validated memories from {validations} validation calls")
        return out

    @staticmethod
    def _reply_counts(result: dict) -> dict:
        replies = list(result.get("replies") or ())
        return {"calls": len(replies),
                "no_json_validation": sum(1 for r in replies if r.get("op") == "validation"
                                          and r.get("json") != "block"),
                "no_json_extraction": sum(1 for r in replies if r.get("op") in ORIGINS
                                          and r.get("json") not in ("block", "bare")),
                "unparsed_validation": sum(1 for u in result.get("unparsed") or () if u.get("op") == "validation")}

    def _count(self, pending: dict) -> None:
        """The batch's counters into the ledger's: cuts and reply formats of the summaries committed, the calls of
        every step the batch took (the failed steps' calls were paid as well)."""
        c = self.ledger["counters"]

        def add(k, v):
            c[k] = c.get(k, 0) + v
        for t in pending["tasks"].values():
            for k in CUT_KEYS:
                add(k, t["cuts"][k])
            for k in REPLY_FORMAT_COUNTERS:
                add(k, t["replies"][k])
            add("validated", int(t["validated"] or 0))
            add("batch_dedup_dropped", int(t["validated"] or 0) - len(t["memory_list"]))
        add("summarized", len(pending["tasks"]))
        for st in pending["steps"]:
            add("research_calls", st["calls"])
            add("research_calls_failed", st["failed"])
            add("research_calls_unattributed", len(st["unattributed"]))
            add("steps", 1)

    def _drop(self, n: int, th: str, memory: dict, why: str) -> None:
        self.ledger["dropped"].append({"n": n, "task_hash": th, "memory_id": memory.get("memory_id"),
                                       "origin": memory.get("author"), "why": why})

    def _add_source(self, mid: str, th: str, sources: set, how: str, n: int) -> None:
        e = self.entries.get(mid)
        if e is None:
            raise MemoryStepError(f"memory {mid} is in the store and not in the ledger: resume() first")
        e["sources"] = sorted(set(e.get("sources") or ()) | sources)
        e.setdefault("events", []).append({"n": n, "task_hash": th, "how": how})

    def _commit(self, task, first, retry, baseline, pending) -> None:
        th = task["task_hash"]
        n = len(self.ledger["order"])
        sessions = [a["session"] for a in (baseline, first, retry) if a is not None]
        trace_refs = [r for a in (baseline, first, retry) if a is not None for r in (a.get("trace_refs") or ())]
        score = max(float(a["reward"]) for a in (baseline, first, retry) if a is not None)
        sources = self.siblings(th)
        record = {"n": n, "step": pending["tasks"][th]["step"], "added": [], "duplicates": [], "dropped": [],
                  "retired": [], "evicted": [], "validated": pending["tasks"][th]["validated"]}
        for mem in pending["tasks"][th]["memory_list"]:
            origin = origin_of(mem)
            when, content = (mem.get("when_to_use") or "").strip(), (mem.get("content") or "").strip()
            meta = reme_metadata(mem.get("metadata"))
            why = self._shape(when, content)
            if not why:
                when, content, meta, why = self._literal(when, content, meta)
            if why:
                self._drop(n, th, mem, why)
                record["dropped"].append(why)
                continue
            mem = {**mem, "when_to_use": when, "content": content, "metadata": meta,
                   "memory_id": content_id(content)}
            mid = mem["memory_id"]
            prior = self.entries.get(mid, {})
            if prior.get("status") in ("retired", "superseded"):    # retired or superseded content does not come back
                why = (f"retired before: {prior.get('retired')}" if prior["status"] == "retired"
                       else f"superseded before: {prior.get('superseded_by')}")
                self._drop(n, th, mem, why)
                record["dropped"].append(why)
                continue
            reached = self.reach(when, sources)
            if reached < self.s.min_other_tasks:
                why = (f"reach: retrieves {reached} train task(s) beyond its sources, fewer than "
                       f"{self.s.min_other_tasks}")
                self._drop(n, th, mem, why)
                record["dropped"].append(why)
                continue
            dup = self._store_duplicate(when, content)
            if dup is not None:
                self._add_source(dup["memory_id"], th, sources, "store_dedup", n)
                record["duplicates"].append({"memory_id": mid, "dup_of": dup["memory_id"], "how": "store_dedup",
                                             "score": dup["score"], "content_score": dup["content_score"]})
                continue
            if self.client.get([mid], self.collection):
                self._add_source(mid, th, sources, "same_id", n)
                record["duplicates"].append({"memory_id": mid, "dup_of": mid, "how": "same_id"})
                continue
            if len(self.active()) >= self.s.max_active:
                over = evictable({**self.entries, "__new__": {"status": "active"}}, self.s)
                if not over:
                    why = f"capacity: {self.s.max_active} active, none out of probation"
                    self._drop(n, th, mem, why)
                    record["dropped"].append(why)
                    continue
                self.client.retire(over, self.collection)
                for k in over:
                    self.entries[k].update(status="retired", retired=f"capacity {self.s.max_active}", retired_at=n)
                record["evicted"] += over
            row = stamp(mem, line=self.line, source_line=self.line, split="train", task_hash=th,
                        activity=self.activity, release_ref=first.get("release_ref") or "", sessions=sessions,
                        trace_refs=trace_refs, origin=origin, born_at=n, source_score=score,
                        heldout_domains=self.heldout)
            self.client.add([row], self.collection)
            self.entries[mid] = {"memory_id": mid, "origin": origin, "born_at": n, "status": "active",
                                 "sources": sorted(sources), "stats": new_stats(), "row": row,
                                 "events": [{"n": n, "task_hash": th, "how": "added"}]}
            record["added"].append(mid)
        # credit: the first encounter, by its citation line (the retry carries a note: never credited)
        cited = parse_citation(first.get("final_reason"), first.get("final_text")) if self.s.cite else None
        shown = resolve_ids(first.get("shown") or (), set(self.entries))
        r = 1 if first.get("passed") else 0
        cr = credit(self.entries, task_hash=th, shown=shown, cited=cited, r=r, b=task.get("b"),
                    stable_pass=bool(task.get("stable_pass")))
        credit_each = 0.0 if cr["adv"] is None else round(cr["adv"] * cr["share"], 6)
        origins = sorted({self.entries[m]["origin"] for m in cr["used"] if m in self.entries})
        for mid, e in sorted(self.active().items()):
            why = retire_reason(e, self.s)
            if why:
                record["retired"].append(mid)
                e.update(status="retired", retired=why, retired_at=n)
        if record["retired"]:
            self.client.retire(record["retired"], self.collection)
        self.ledger["readings"][th] = {
            "n": n, "r": r, "b": task.get("b"), "stable_pass": bool(task.get("stable_pass")), "shown": shown,
            "cited": cited, "used": cr["used"], "credit_each": credit_each,
            "cited_unshown": cr["cited_unshown"], "origins_used": origins,
            "retry": None if retry is None else int(bool(retry.get("passed"))), "sessions": sessions}
        self.ledger["order"].append(th)
        self.ledger["processed"][th] = record

    def _shape(self, when: str, content: str) -> str:
        if not when or not content:
            return "empty when_to_use or content"
        if len(when) > self.s.when_chars or len(content) > self.s.content_chars:
            return (f"length: when_to_use {len(when)} (at most {self.s.when_chars}), content {len(content)} "
                    f"(at most {self.s.content_chars})")
        return ""

    def _store_duplicate(self, when: str, content: str) -> dict | None:
        """The stored entry this memory repeats, or None: among the DEDUP_CANDIDATES stored entries of this line
        nearest by when_to_use (one space: when_to_use against stored when_to_use), the first at
        store_dedup_threshold whose content is also within store_dedup_content_threshold of this one's. A failure or
        comparative lesson with the trigger of a stored success lesson is another lesson, not a source of it."""
        top = self.client.retrieve(when, self.collection, where={"ol_line": self.line}, top_k=DEDUP_CANDIDATES,
                                   query_mode=False)
        close = [m for m in top if float(m["metadata"].get("retrieval_score", 0.0)) >= self.s.store_dedup_threshold]
        if not close:
            return None
        mine, *theirs = self.client.embed([content] + [m["content"] for m in close])
        for m, v in zip(close, theirs):
            sim = cosine(mine, v)
            if sim >= self.s.store_dedup_content_threshold:
                return {"memory_id": m["memory_id"], "score": round(float(m["metadata"]["retrieval_score"]), 4),
                        "content_score": round(sim, 4)}
        return None

    def _literal(self, when: str, content: str, meta: dict) -> tuple[str, str, dict, str]:
        """The literal check with one placeholder pass (rules.literal_pass) over the memory's text and every string
        of the ReMe metadata kept with it. -> (when, content, metadata, why it is dropped or "")."""
        fixed, hits = literal_pass({"when": when, "content": content}, self.forbidden, self.allowed)
        if fixed is None:
            return when, content, meta, "literal: " + ", ".join(sorted({repr(h["literal"]) for h in hits}))[:200]
        out, found = {}, bool(hits)
        for k, v in sorted(meta.items()):
            if isinstance(v, str):
                got, h = literal_pass({"value": v}, self.forbidden, self.allowed)
                if got is None:
                    return when, content, meta, (f"literal in metadata.{k}: "
                                                 + ", ".join(sorted({repr(x["literal"]) for x in h}))[:200])
                found, v = found or bool(h), got["value"]
            out[k] = v
        if found:
            self.ledger["counters"]["placeholder_fixed"] += 1
        return fixed["when"], fixed["content"], out, ""

    def _import_fix(self, row: dict) -> tuple[dict | None, str]:
        """An imported row under this line's checks: lengths and the literal check (this line's train literals); a
        content this stream retired or superseded does not come back through an import either."""
        when, content = row.get("when_to_use") or "", row.get("content") or ""
        why = self._shape(when, content)
        reme = {k: v for k, v in (row.get("metadata") or {}).items() if k in REME_META_KEYS}
        if not why:
            when, content, reme, why = self._literal(when, content, reme)
        if why:
            return None, why
        prior = self.entries.get(content_id(content), {})
        if prior.get("status") in ("retired", "superseded"):
            return None, f"{prior['status']} before in this stream"
        return {**row, "when_to_use": when, "content": content, "memory_id": content_id(content),
                "metadata": {**row["metadata"], **reme}}, ""

    # ------------------------------------------------------------------------------------------- imports
    def import_snapshot(self, snapshot_text: str, *, source_release_ref: str, source_line: str) -> dict:
        """K's memory.imports at the stream's start: the released snapshot of another line copied into this live
        library, recorded in the ledger once; imported entries start their probation from zero and pass this line's
        length and literal checks. Only a snapshot K declares ({release_ref, snapshot_digest} in memory.imports) is
        imported: an import is an input of the activity's identity, never a side command."""
        from .importing import import_entries

        declared = {"release_ref": source_release_ref, "snapshot_digest": snapshot_digest(snapshot_text)}
        if declared not in [dict(i) for i in self.s.imports]:
            raise MemoryStepError(f"import {declared}: not one of K's memory.imports {list(self.s.imports)}")
        key = {**declared, "source_line": source_line}
        if any({k: i.get(k) for k in key} == key for i in self.ledger["imports"]):
            return {"added": [], "skipped": [], "dropped": [], "again": True}
        got = import_entries(snapshot_text, source_release_ref=source_release_ref, source_line=source_line,
                             target_line=self.line, client=self.client, collection=self.collection,
                             heldout_domains=self.heldout, fix=self._import_fix)
        n = len(self.ledger["order"])
        for row in got["rows"]:
            self.entries[row["memory_id"]] = {"memory_id": row["memory_id"], "origin": IMPORTED, "born_at": n,
                                              "status": "active", "sources": [], "stats": new_stats(), "row": row,
                                              "events": [{"n": n, "how": "imported",
                                                          "from": row["metadata"]["ol_imported_from"]}]}
        self.ledger["imports"].append({**key, "added": got["added"], "skipped": got["skipped"],
                                       "dropped": got["dropped"]})
        self.checkpoint()
        return {"added": got["added"], "skipped": got["skipped"], "dropped": got["dropped"], "again": False}

    # ------------------------------------------------------------------------------------------- the end
    def finish(self) -> dict:
        """The stream's end: the readout, the snapshot (canonical, retired entries are not in the store) and why it
        is not gated, if it is not (D <= abort_d, or an empty library)."""
        if self.ledger.get("pending"):
            raise MemoryStepError(f"step {self.ledger['pending']['step']} is pending: process it first")
        text = self.snapshot_text()
        ro = readout(self.ledger["readings"], self.ledger["order"], self.entries)
        ro["counters"] = dict(self.ledger["counters"])
        why: dict = {}
        for d in self.ledger["dropped"]:
            key = d["why"].split(":", 1)[0]
            why[key] = why.get(key, 0) + 1
        ro["dropped"] = why
        ro["imports"] = len(self.ledger["imports"])
        ro["reply_format"] = reply_format_problems(ro["counters"])
        abort = snapshot_abort(ro, self.s, empty=not text)
        self.ledger.update(status="done", readout=ro, abort=abort, snapshot_digest=snapshot_digest(text))
        self.checkpoint()
        return {"snapshot": text, "readout": ro, "abort": abort}
