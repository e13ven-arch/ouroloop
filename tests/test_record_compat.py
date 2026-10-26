"""HEAD's readers still read the existing run records (docs/design/EVALUATE.md §9; CONTRACTS.md §10; PLAN rule 15).

No existing activity is resumed at HEAD, so neither hash identity nor the old typed records are required (old
workspaces are re-rendered or resumed at their tags). What is required is that the train/dev records stay readable,
because the v2 data pipeline reads them (docs/design/V2_TRAINING_PIPELINE.md §2.1: the runner-shell records'
`gateway_call` and `broker_event` lines, results and grading): the results with their run manifests through
evaluate.results, the grading rows, the transcripts as JSONL through runtime.records, and the releases the attempts ran
with everything they refer to as plain JSON through contracts.Objects and refs_in.

The records are git-ignored and live only in the main checkout, under REHARNESS_RECORDS_ROOT (default: this
checkout's examples/automationbench); a workspace that is not there is skipped, unless REHARNESS_RECORDS_ROOT is set,
where a missing workspace fails (a misdirected root would otherwise skip every test here and pass). They cannot be
regenerated, so:

- nothing here writes under a workspace. Every file a test reads is hashed before its first read and again at the
  end, and every file under the workspace keeps its size and mtime, with none added or removed;
- no test split content is touched. A multi-split file (results.jsonl, grading.jsonl) is filtered on each row's
  `split` before anything else of the row is looked at, and HEAD's readers of those files run on a copy in tmp_path
  holding the train and dev rows only. Per-attempt files (transcripts, run manifests) are opened only for train and
  dev attempts. No test-split transcript is ever opened. The closure walk does not open a `finalization` object.

The counts below were read from the records on 2026-10-08 and 2026-10-09; the activities are finished, so they do not
move.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, fields
from pathlib import Path

import pytest

from reharness.contracts import Objects, parse_ref, read_jsonl, refs_in, short
from reharness.evaluate.results import Results, RunManifest, TaskResult, summarise
from reharness.runtime import records

RECORDS = Path(os.environ.get("REHARNESS_RECORDS_ROOT")
               or Path(__file__).resolve().parents[1] / "examples" / "automationbench")
TRAIN_DEV = ("train", "dev")
# The line kinds the v3-v5 runner shell wrote (read over every train/dev transcript of the four workspaces).
OLD_KINDS = {"task", "agent", "assistant", "tool_specs", "gateway_call", "tool", "broker_event", "end", "hook",
             "tool_blocked", "tool_skipped", "pushback", "worker_session", "library_retrieval"}
DIGEST16 = re.compile(r"[0-9a-f]{16}")


@dataclass(frozen=True)
class Expected:
    domain: str                     # the results directory under bench/
    rows: dict                      # split -> (attempts, successes, distinct tasks)
    runs: int                       # distinct run manifests of the train/dev attempts
    releases: int                   # distinct releases of the train/dev attempts
    closure: int                    # objects reachable from those releases


EXPECTED = {
    "workspace-claude-v4-finance": Expected(
        "finance", {"train": (152, 97, 50), "dev": (175, 68, 25)}, runs=17, releases=13, closure=207),
    "workspace-claude-v3": Expected(
        "marketing+sales+support+hr+finance", {"train": (350, 153, 252), "dev": (500, 212, 125)}, runs=12,
        releases=11, closure=207),
    "workspace-claude-v5-support": Expected(
        "support", {"train": (165, 70, 50), "dev": (125, 53, 25)}, runs=57, releases=52, closure=504),
    "workspace-claude-v4-support": Expected(
        "support", {"train": (50, 18, 50), "dev": (75, 26, 25)}, runs=4, releases=1, closure=41),
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


class GuardedObjects(Objects):
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

    def rows(self) -> list[TaskResult]:
        return list(self.results.all())

    def run_file(self, run: str) -> Path:
        return self.guard.read(Results(self.root, self.expected.domain).root / "runs" / f"{run}.json")


@pytest.fixture(params=sorted(EXPECTED))
def ws(request, tmp_path) -> Workspace:
    name = request.param
    root = RECORDS / name
    if not root.is_dir():
        if os.environ.get("REHARNESS_RECORDS_ROOT"):
            pytest.fail(f"{name} is not under REHARNESS_RECORDS_ROOT={RECORDS}")
        pytest.skip(f"{name} is not under {RECORDS} (git-ignored run records; set REHARNESS_RECORDS_ROOT)")
    expected = EXPECTED[name]
    guard = ReadOnly(root)
    copy = tmp_path / name
    source = Results(root, expected.domain)
    target = Results(copy, expected.domain)
    target.root.mkdir(parents=True)
    target.path.write_text("".join(train_dev_lines(source.path, guard)), encoding="utf-8")
    target.grading_path().write_text("".join(train_dev_lines(source.grading_path(), guard)), encoding="utf-8")
    return Workspace(name, root, copy, guard, expected)


def sample(rows: list, per_split: int = 12) -> list:
    """A fixed sample of train and dev attempts: evenly spaced over each split in attempt-id order, and the first
    failed train attempts."""
    picked: dict = {}
    for split in TRAIN_DEV:
        of = sorted((r for r in rows if r.split == split), key=lambda r: r.attempt_id)
        for r in of[::max(1, len(of) // per_split)]:
            picked[r.attempt_id] = r
    train = sorted((r for r in rows if r.split == "train"), key=lambda r: r.attempt_id)
    for r in [r for r in train if not r.success][:3]:
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
    # TaskResult keeps every field a recorded row carries: the reader drops only the line's timestamp
    declared = {f.name for f in fields(TaskResult)}
    assert set().union(*(json.loads(line) for line in ws.results.path.read_text().splitlines())) - declared == {"ts"}
    # the run manifest beside the results, under the name the attempts carry; it parses with HEAD's RunManifest, which
    # refuses an unknown key (so every field an old manifest carries is still declared) and hashes it as it was written
    runs = sorted({r.run for r in rows})
    assert len(runs) == ws.expected.runs
    copied = ws.results.root / "runs"
    copied.mkdir()
    for run in runs:
        text = ws.run_file(run).read_text(encoding="utf-8")
        recorded = json.loads(text)
        assert recorded["hash"] == run
        manifest = RunManifest.from_json(recorded)
        assert manifest.hash() == run and manifest.to_json() == recorded
        assert {r.split for r in rows if r.run == run} == {manifest.split}
        assert {r.candidate for r in rows if r.run == run} == {manifest.candidate}
        with pytest.raises(ValueError, match="does not take"):
            RunManifest.from_json({**recorded, "unknown_field": 1})
        (copied / f"{run}.json").write_text(text, encoding="utf-8")
    # HEAD's reader of the runs directory leaves out a manifest it cannot parse: it leaves out none of these
    assert sorted(ws.results.runs()) == runs
    assert sum(len(ws.results.of_run(run)) for run in runs) == len(rows)
    ws.guard.check()


# ---------------------------------------------------------------------------- grading
def test_every_train_and_dev_attempt_has_one_grading_row_that_matches_its_result(ws):
    rows = ws.rows()
    store = ws.results
    grading: dict = {}
    for row in read_jsonl(store.grading_path()):
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
def test_a_sample_of_train_and_dev_transcripts_reads_as_jsonl_with_their_line_kinds(ws):
    """Each transcript under HEAD's session path, read by HEAD's JSONL reader: the old runner shell's line kinds, one
    gateway_call per assistant turn with the attempt's id and a prefix chain that holds, one broker_event per tool
    line with the 16-hex output digest the v2 pipeline checks a backfilled output against."""
    rows = ws.rows()
    picked = sample(rows)
    assert {r.split for r in picked} == set(TRAIN_DEV)
    kinds_seen: set = set()
    for r in picked:
        path = ws.guard.read(records.session_file(ws.root, r.evidence))
        assert path.parent == ws.root / "transcripts"
        events = records.read(path)
        kinds = [e.get("kind") for e in events]
        kinds_seen |= set(kinds)
        assert set(kinds) <= OLD_KINDS, sorted(set(kinds) - OLD_KINDS)
        assert kinds[0] == "task" and kinds[-1] == "end" and "agent" in kinds and "tool_specs" in kinds
        assert events[0]["text"]
        acts = [e for e in events if e["kind"] == "assistant" and e.get("role") == "act"]
        calls = [e for e in events if e["kind"] == "gateway_call"]
        assert len(acts) == r.steps and len(calls) == kinds.count("assistant")
        assert {e["attempt_id"] for e in calls} == {r.attempt_id}
        assert len({e["call_id"] for e in calls}) == len(calls)
        history: list = []
        for e in calls:
            assert isinstance(e["messages_prefix"], int) and 0 <= e["messages_prefix"] <= len(history)
            history = history[:e["messages_prefix"]] + list(e["messages_tail"])
            assert all(isinstance(m, dict) and m.get("role") for m in history)
            assert all(c["id"] and c["name"] for c in e["response"]["tool_calls"])
        brokered = [e for e in events if e["kind"] == "broker_event"]
        assert len(brokered) == kinds.count("tool")
        for b in brokered:
            assert b["attempt_id"] == r.attempt_id and b["tool"] and isinstance(b["args"], dict) and b["gate"]
            assert DIGEST16.fullmatch(b["output_digest"])
    assert {"task", "assistant", "gateway_call", "tool", "broker_event", "end"} <= kinds_seen
    ws.guard.check()


# ---------------------------------------------------------------------------- frozen objects
def test_the_releases_the_attempts_ran_and_their_closure_load_as_plain_json(ws):
    rows = ws.rows()
    objects = ws.objects
    releases = sorted({r.system_release_ref for r in rows})
    assert len(releases) == ws.expected.releases
    for ref in releases:
        release = objects.get(ref)
        assert parse_ref(release["core_ref"])[0] == "core" and parse_ref(release["worker_ref"])[0] == "policyset"
    # the closure: every object a release refers to, at any depth, is there and hashes to its contents (get checks)
    seen, todo = set(), list(releases)
    while todo:
        ref = todo.pop()
        if ref in seen or ref.startswith("finalization:"):
            continue
        seen.add(ref)
        assert objects.has(ref), f"{short(ref)} is referenced and missing"
        todo.extend(sorted(refs_in(objects.get(ref)) - seen))
    assert len(seen) == ws.expected.closure
    assert {parse_ref(ref)[0] for ref in seen} >= {"release", "policyset", "policy", "core", "package"}
    # the package each run mounted is in that closure, and its files read back as plain JSON (the v2 pipeline's S1'
    # groups attempts by it)
    for run in sorted({r.run for r in rows}):
        package_ref = json.loads(ws.run_file(run).read_text(encoding="utf-8")).get("package_ref")
        if not package_ref:
            continue
        assert package_ref in seen
        bundle = objects.get(objects.get(package_ref)["bundle_ref"])
        files = {path: objects.get(ref)["text"] for path, ref in bundle["files"].items()}
        assert isinstance(json.loads(files["manifest.json"])["api_version"], str)
        assert all(isinstance(text, str) for text in files.values())
    ws.guard.check()
