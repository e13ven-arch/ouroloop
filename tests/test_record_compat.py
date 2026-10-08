"""HEAD's readers still read the existing run records (docs/PLAN.md P1; decision of 2026-10-08).

No existing activity is resumed at HEAD, so hash identity with these workspaces is not required (old workspaces are
re-rendered or resumed at the tags v0.4.0 and archive-2026-10-08). What is required is that the records stay
readable, because the v2 data pipeline (docs/design/V2_TRAINING_PIPELINE.md §2.1) and PLAN P4-S1 read them: the
train/dev results with their run manifests, the grading rows, the transcripts, the ledger's session rows and the
sessions' release files, the frozen objects as plain JSON, and library_stream.json.

The records are git-ignored and live only in the main checkout, under OUROLOOP_RECORDS_ROOT (default: this
checkout's examples/automationbench); a workspace that is not there is skipped. They cannot be regenerated, so:

- nothing here writes under a workspace. Every file a test reads is hashed before its first read and again at the
  end, and every file under the workspace keeps its size and mtime, with none added or removed;
- no test split content is touched. A multi-split file (results.jsonl, grading.jsonl, the ledger) is filtered on
  each row's `split` before anything else of the row is looked at, and HEAD's readers of those files run on a copy
  in tmp_path holding the train and dev rows only. Per-attempt files (transcripts, release files, run manifests) are
  opened only for train and dev attempts. No test-split transcript is ever opened.

The counts below were read from the records on 2026-10-08; the activities are finished, so they do not move.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from ouroloop.agent import harness_package as hp
from ouroloop.benchmark import Results, RunManifest, summarise
from ouroloop.evolve import contracts as ct
from ouroloop.ledger import Ledger, _read_jsonl
from ouroloop.report import gateway_summary, m_tool_writes
from ouroloop.research import library as L
from ouroloop.research.diagnosis import DiagnosisLimits, failure_summary, gateway_results, read_transcript

RECORDS = Path(os.environ.get("OUROLOOP_RECORDS_ROOT")
               or Path(__file__).resolve().parents[1] / "examples" / "automationbench")
TRAIN_DEV = ("train", "dev")


@dataclass(frozen=True)
class Expected:
    domain: str                     # the results directory under bench/
    rows: dict                      # split -> (attempts, successes, distinct tasks)
    sessions: dict                  # split -> session rows in the ledger
    runs: int                       # distinct run manifests of the train/dev attempts
    releases: int                   # distinct system releases of the train/dev attempts
    library: dict | None = None     # rsi/library_stream.json, where the activity ran the experience library


EXPECTED = {
    "workspace-claude-v4-finance": Expected("finance", {"train": (152, 97, 50), "dev": (175, 68, 25)},
                                            {"train": 152, "dev": 175}, runs=17, releases=13),
    "workspace-claude-v3": Expected("marketing+sales+support+hr+finance",
                                    {"train": (350, 153, 252), "dev": (500, 212, 125)},
                                    {"train": 350, "dev": 509}, runs=12, releases=11),
    "workspace-claude-v5-support": Expected("support", {"train": (165, 70, 50), "dev": (125, 53, 25)},
                                            {"train": 165, "dev": 134}, runs=57, releases=52,
                                            library={"order": 50, "active": 7, "retired": 35, "D": 3.0, "B": 1}),
    "workspace-claude-v4-support": Expected("support", {"train": (50, 18, 50), "dev": (75, 26, 25)},
                                            {"train": 50, "dev": 75}, runs=4, releases=1),
}


# ---------------------------------------------------------------------------- read-only access
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReadOnly:
    """The workspace as the test found it: every file under it by size and mtime, and the sha256 of each file the
    test reads, taken before its first read. `check()` proves nothing was written, created or removed."""

    def __init__(self, root: Path):
        self.root = root
        self.tree = self._tree()
        self.digests: dict[Path, str] = {}

    def _tree(self) -> dict:
        return {str(p.relative_to(self.root)): (p.stat().st_size, p.stat().st_mtime_ns)
                for p in sorted(self.root.rglob("*")) if p.is_file()}

    def read(self, path: Path) -> Path:
        path = Path(path)
        assert self.root in path.parents, f"{path} is outside {self.root}"
        if path not in self.digests and path.exists():
            self.digests[path] = _sha256(path)
        return path

    def check(self) -> None:
        assert self.digests, "the test read nothing"
        changed = [str(p) for p, d in self.digests.items() if _sha256(p) != d]
        assert changed == [], f"files the test read changed: {changed}"
        now = self._tree()
        assert sorted(set(now) ^ set(self.tree)) == [], "files appeared or disappeared under the workspace"
        assert {k: v for k, v in now.items() if self.tree[k] != v} == {}, "files under the workspace were touched"


class GuardedObjects(ct.Objects):
    """The object store, read only through ReadOnly: every object file is hashed before it is read."""

    def __init__(self, root: Path, guard: ReadOnly):
        super().__init__(root)
        self.guard = guard

    def get(self, ref: str) -> dict:
        self.guard.read(self.path(ref))
        return super().get(ref)


def train_dev_lines(path: Path, guard: ReadOnly) -> list[str]:
    """The lines of a multi-split JSONL file whose row is a train or dev row; the split is the only field read
    before this filter."""
    out = []
    with open(guard.read(path), encoding="utf-8") as f:
        for line in f:
            if line.strip() and json.loads(line).get("split") in TRAIN_DEV:
                out.append(line if line.endswith("\n") else line + "\n")
    return out


@dataclass
class Workspace:
    name: str
    root: Path                      # the real workspace: per-attempt files are read here
    copy: Path                      # tmp_path: the train/dev projection of the multi-split files
    guard: ReadOnly
    expected: Expected

    @property
    def results(self) -> Results:
        """HEAD's results reader over the train/dev projection of results.jsonl and grading.jsonl."""
        return Results(self.copy, self.expected.domain)

    @property
    def objects(self) -> GuardedObjects:
        return GuardedObjects(self.root, self.guard)

    def rows(self) -> list:
        return list(self.results.all())


@pytest.fixture(params=sorted(EXPECTED))
def ws(request, tmp_path) -> Workspace:
    name = request.param
    root = RECORDS / name
    if not root.is_dir():
        pytest.skip(f"{name} is not under {RECORDS} (git-ignored run records; set OUROLOOP_RECORDS_ROOT)")
    expected = EXPECTED[name]
    guard = ReadOnly(root)
    copy = tmp_path / name
    source = Results(root, expected.domain)
    target = Results(copy, expected.domain)
    target.root.mkdir(parents=True)
    target.path.write_text("".join(train_dev_lines(source.path, guard)), encoding="utf-8")
    target.grading_path().write_text("".join(train_dev_lines(source.grading_path(), guard)), encoding="utf-8")
    (copy / "ledger").mkdir()
    for day in sorted((root / "ledger").glob("*.jsonl")):
        (copy / "ledger" / day.name).write_text("".join(train_dev_lines(day, guard)), encoding="utf-8")
    return Workspace(name, root, copy, guard, expected)


def sample(rows: list, per_split: int = 12) -> list:
    """A fixed sample of train and dev attempts: evenly spaced over each split in attempt-id order, the first
    failed train attempts (a failure summary needs them) and, where the experience library ran, its first attempts."""
    picked: dict = {}
    for split in TRAIN_DEV:
        of = sorted((r for r in rows if r.split == split), key=lambda r: r.attempt_id)
        for r in of[::max(1, len(of) // per_split)]:
            picked[r.attempt_id] = r
    train = sorted((r for r in rows if r.split == "train"), key=lambda r: r.attempt_id)
    for r in [r for r in train if not r.success][:3] + [r for r in train if "/library_" in r.attempt_id][:6]:
        picked[r.attempt_id] = r
    assert all(r.split in TRAIN_DEV for r in picked.values())
    return [picked[k] for k in sorted(picked)]


# ---------------------------------------------------------------------------- results and run manifests
def test_the_train_and_dev_results_and_their_run_manifests_load(ws):
    rows = ws.rows()
    for split in TRAIN_DEV:
        of = [r for r in rows if r.split == split]
        attempts, successes, tasks = ws.expected.rows[split]
        assert (len(of), sum(r.success for r in of), len({r.task_id for r in of})) == (attempts, successes, tasks)
        total = summarise(of)
        assert total["attempts"] == attempts and total["invalid"] == 0 and total["tasks"] == tasks
        assert total["cost_missing"] == 0 and total["cost_usd"] > 0
    assert {r.split for r in rows} == set(TRAIN_DEV)
    for r in rows:
        assert r.attempt_id and r.evidence.startswith("s_") and r.system_release_ref.startswith("release:sha256:")
        assert 0.0 <= r.partial_credit <= 1.0 and 0 <= r.assertions_passed <= r.assertions_scored
        assert r.termination == "finished" and r.validity == "valid" and r.usage
    # the run manifest beside the results, under the name the attempts carry; it parses with HEAD's RunManifest
    # (from_json refuses an unknown key, so a field removed from RunManifest must still be accepted on read)
    runs = sorted({r.run for r in rows})
    assert len(runs) == ws.expected.runs
    for run in runs:
        path = ws.guard.read(Results(ws.root, ws.expected.domain).root / "runs" / f"{run}.json")
        recorded = json.loads(path.read_text(encoding="utf-8"))
        assert recorded["hash"] == run
        manifest = RunManifest.from_json(recorded)
        assert {r.split for r in rows if r.run == run} == {manifest.split}
        assert {r.candidate for r in rows if r.run == run} == {manifest.candidate}
    ws.guard.check()


# ---------------------------------------------------------------------------- grading
def test_every_train_and_dev_attempt_has_one_grading_row_that_matches_its_result(ws):
    rows = ws.rows()
    store = ws.results
    grading: dict = {}
    for row in _read_jsonl(store.grading_path()):
        grading.setdefault(row["attempt_id"], []).append(row)
    for split in TRAIN_DEV:
        assert sum(1 for g in grading.values() if g[0]["split"] == split) == ws.expected.rows[split][0]
    for r in rows:
        assert len(grading[r.attempt_id]) == 1
        row = grading[r.attempt_id][0]
        assert (row["task_id"], row["split"], row["run"]) == (r.task_id, r.split, r.run)
        scored = [a for a in row["assertions"] if not a["excluded"]]
        assert all(set(a) == {"type", "params", "passed", "excluded"} for a in row["assertions"])
        assert (len(scored), sum(a["passed"] for a in scored)) == (r.assertions_scored, r.assertions_passed)
    # HEAD's reader: the recorded assertions of an attempt, train only unless asked for more
    for r in sample(rows):
        assert store.grading(r.attempt_id, splits=TRAIN_DEV) == grading[r.attempt_id][0]["assertions"]
        assert store.grading(r.attempt_id) == (grading[r.attempt_id][0]["assertions"] if r.split == "train" else [])
    ws.guard.check()


# ---------------------------------------------------------------------------- transcripts
def test_a_sample_of_train_and_dev_transcripts_reads_with_heads_projections(ws):
    rows = ws.rows()
    store = ws.results
    picked = sample(rows)
    assert {r.split for r in picked} == set(TRAIN_DEV)
    transcripts = ws.root / "transcripts"
    stream = _library_state(ws)
    read = []
    for r in picked:
        ws.guard.read(transcripts / f"{r.evidence}.jsonl")
        events = read_transcript(transcripts, r.evidence)
        read.append(events)
        kinds = [e.get("kind") for e in events]
        assert kinds[0] == "task" and kinds[-1] == "end" and "agent" in kinds and "tool_specs" in kinds
        acts = [e for e in events if e.get("kind") == "assistant" and e.get("role") == "act"]
        calls = [e for e in events if e.get("kind") == "gateway_call"]
        assert len(acts) == r.steps and len(calls) == kinds.count("assistant")
        assert {e["attempt_id"] for e in calls} == {r.attempt_id}
        # the model-visible tool results, rebuilt from the gateway log (the v2 pipeline's `gateway_call` source)
        results = gateway_results(events)
        assert set(results) == {e["call_id"] for e in calls}
        for e in calls:
            wanted = [c["id"] for c in e["response"]["tool_calls"]]
            got = [x["id"] for x in results[e["call_id"]]]
            assert got == [w for w in wanted if w in set(got)]
        for b in (e for e in events if e.get("kind") == "broker_event"):
            assert b["attempt_id"] == r.attempt_id and b["tool"] and isinstance(b["args"], dict) and b["gate"]
        assert m_tool_writes(events)["executed"] == 0
        if r.split == "train" and not r.success:
            summary = failure_summary(r, events, store.grading(r.attempt_id), DiagnosisLimits())
            assert summary["attempt_id"] == r.attempt_id and summary["turns_total"] == kinds.count("assistant")
            assert len(summary["checks"]) == r.assertions_scored and summary["task_text"]
        elif r.split == "dev":
            with pytest.raises(ct.ContractError, match="only train attempts are summarised"):
                failure_summary(r, events, [], DiagnosisLimits())
        for e in (e for e in events if e.get("kind") == "library_retrieval"):
            assert stream is not None and set(e["entries"]) <= set(stream["entries"])
            assert len(e["scores"]) == len(e["entries"]) and e["task_hash"]
    tally = gateway_summary(read)
    assert tally["attempts"] == len(picked) and tally["by_role"]["act"]["calls"] == sum(r.steps for r in picked)
    assert tally["placements"] and tally["entries"]
    ws.guard.check()


# ---------------------------------------------------------------------------- sessions
def test_the_ledgers_session_rows_and_the_sessions_release_files_load(ws):
    rows = ws.rows()
    events = list(Ledger(ws.copy / "ledger").events())
    assert {e["type"] for e in events} == {"session"}
    sessions, n = Ledger(ws.copy / "ledger").sessions()
    assert n == len(events) == len(sessions)
    for split in TRAIN_DEV:
        of = [s for s in sessions.values() if s["split"] == split]
        assert len(of) == ws.expected.sessions[split]
        # held out for good: every dev session, and no train session (docs/DESIGN.md invariant 3)
        assert all(s["held_out"] is (split == "dev") for s in of)
    for r in rows:
        s = sessions[r.evidence]
        assert (s["task"], s["split"], s["attempt_id"], s["run"]) == (r.task_id, r.split, r.attempt_id, r.run)
    for r in sample(rows):
        path = ws.guard.read(ws.root / "sessions" / f"{r.evidence}.release.json")
        pin = json.loads(path.read_text(encoding="utf-8"))
        assert set(pin) == {"release", "champion"}
        assert pin["release"] == sessions[r.evidence]["release"] and pin["champion"] == sessions[r.evidence]["champion"]
    ws.guard.check()


# ---------------------------------------------------------------------------- frozen objects
def test_the_releases_the_attempts_ran_and_everything_they_reference_load_as_plain_json(ws):
    rows = ws.rows()
    objects = ws.objects
    releases = sorted({r.system_release_ref for r in rows})
    assert len(releases) == ws.expected.releases
    for ref in releases:
        release = objects.load(ref, ct.SystemRelease)
        assert objects.load(release.worker_ref, ct.PolicySet).policies and release.core_ref
    # the closure: every object a release refers to, at any depth, is there and hashes to its contents (get checks)
    seen, todo = set(), list(releases)
    while todo:
        ref = todo.pop()
        if ref in seen or ref.startswith("finalization:"):
            continue
        seen.add(ref)
        assert objects.has(ref), f"{ct.short(ref)} is referenced and missing"
        todo.extend(sorted(ct.refs_in(objects.get(ref)) - seen))
    assert {ref.partition(":")[0] for ref in seen} >= {"release", "policyset", "policy", "core", "package"}
    # the package each run mounted, as files (the v2 pipeline's S1' groups attempts by it)
    for run in sorted({r.run for r in rows}):
        recorded = json.loads(ws.guard.read(Results(ws.root, ws.expected.domain).root / "runs" / f"{run}.json")
                              .read_text(encoding="utf-8"))
        if recorded.get("package_ref"):
            run_files, _ = hp.files_of(objects, recorded["package_ref"])
            assert "manifest.json" in run_files and all(isinstance(v, str) for v in run_files.values())
    ws.guard.check()


# ---------------------------------------------------------------------------- the experience library's stream
def _library_state(ws: Workspace) -> dict | None:
    path = ws.root / "rsi" / "library_stream.json"
    if not path.exists():
        return None
    return json.loads(ws.guard.read(path).read_text(encoding="utf-8"))


def test_the_library_stream_loads_and_its_readout_recomputes(ws):
    state = _library_state(ws)
    if ws.expected.library is None:
        assert state is None, f"{ws.name} ran no experience library and has a library_stream.json"
        return
    want = ws.expected.library
    rows = ws.rows()
    train_tasks = {r.task_id for r in rows if r.split == "train"}
    assert state["schema"] == "library_stream.v1" and state["status"] == "done"
    # train only: the stream's order is the train split, and every reading is of a task in it
    assert len(state["order"]) == state["cursor"] == want["order"] and set(state["order"]) <= train_tasks
    assert set(state["readings"]) <= set(state["order"])
    entries = list(state["entries"].values())
    by_status = {s: sum(e["status"] == s for e in entries) for s in ("active", "retired")}
    assert by_status == {"active": want["active"], "retired": want["retired"]}
    assert all(e["retired"] for e in entries if e["status"] == "retired")
    readout = L.stream_readout(state["readings"], state["order"])
    assert (readout["D"], readout["B"]) == (want["D"], want["B"])
    assert readout == {k: state["readout"][k] for k in readout}
    assert L.bootstrap_halves(state["readings"], state["order"]) == state["readout"]["p_second_half_better"]
    # HEAD's retrieval runs over the recorded entries (P4-S1 replays it on recorded train task texts)
    active = [e for e in entries if e["status"] == "active"]
    first = next(r for r in sorted(rows, key=lambda r: r.attempt_id) if r.split == "train")
    ws.guard.read(ws.root / "transcripts" / f"{first.evidence}.jsonl")
    text = next(e["text"] for e in read_transcript(ws.root / "transcripts", first.evidence) if e.get("kind") == "task")
    chosen, _ = L.retrieve(text, active, k=3, min_score=0.0)
    assert chosen and all(e["entry_id"] in state["entries"] and 0.0 <= s for e, s in chosen)
    assert L.telemetry(active)["entries"] == want["active"]
    ws.guard.check()
