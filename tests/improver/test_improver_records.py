"""The improver's records (docs/design/IMPROVER.md §4, §10, §11, §13): the evidence everything R reads is built from,
the ledger and its projection, the archive, conditional credit and the anti-bloat rule, R's playbook and policy.

Most tests are pure. A hand-built workspace holds results, run manifests, grading rows and transcripts written the way
the host writes them, under a small split table (three train tasks, one dev, one test). Ledgers, archives and credit
histories are built in a temporary directory. One round on the fake upstream's rsi domain is shared by the tests that
read what a real round recorded: tests/improver/rounds.py's activity, with the forgetful actor and the scripted R in
tool mode, whose post_channel_message tool is screened, gated, installed and credited.

Covered:
- evidence (§4): a dev, test or unknown-split session raises TraceAccessError, for a row and for a train-labelled
  row that points at a dev transcript; AttemptEvidence and `features` are exact and deterministic; `footprint` per
  kind (a tool called in an act call, a hook that acted, a skill read and found, memory entries given; prompt, agent,
  config, vendor and model on every attempt); `reading` is the package's latest valid train attempt and never a
  library row; `dev_histogram` keeps types and counts with no task id and no params; on the real round, the dev
  sessions are refused and the histogram names no dev task;
- ledger (§10, §11): ids L1, L2... in write order (an unknown stage writes nothing); rows by stage and round; the
  projection keeps only the projected fields and the line; it has no dev task id in a real round; the scoreboard;
  `last_round_of`;
- archive (§10): put/rows (the latest per round and dir, a line's rows), duplicates (same files in an earlier round,
  not in the same round), `for_prompt`; on the real round, the archived candidate keeps its screen and receipt;
- credit and anti-bloat (§10; DESIGN §6.8): a delta is conditioned on the footprint, and test and deleted components
  get no credit; `summary`; a component that shows on no attempt for `removal_zero_credit_rounds` credited rounds,
  or that has had no conditioned credit since its install, is a removal candidate, and not one round earlier; a
  re-install restarts the count; rounds before a component existed do not count; `without` drops the
  manifest entry, and its file only when no other entry names it;
  the round's credited row checked against a recomputation;
- policy (§10): only lessons whose citations are all ledger ids survive (`check_lessons`, `update_playbook` with the
  scripted R's cited and uncited lesson); the real round's playbook; export_policy/import_policy round trip, and
  an import refused once the ledger has rows.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import rsi
from rounds import quiet, round_activity, round_state
from scripted_r import ScriptedR

from ouroloop.activity import phases
from ouroloop.contracts import Objects
from ouroloop.evaluate.results import Results, RunManifest, TaskResult
from ouroloop.harness.contract import API_VERSION
from ouroloop.harness.package import Manifest, Package
from ouroloop.improver.archive import Archive, load_files
from ouroloop.improver.components import components, present, without
from ouroloop.improver.credit import Credit, credit, removal_candidates, summary
from ouroloop.improver.declaration import Declaration
from ouroloop.improver.diagnose import PROMPTS
from ouroloop.improver.evidence import AttemptEvidence, Evidence, dev_histogram
from ouroloop.improver.ledger import PROJECTED, Ledger
from ouroloop.improver.policy import (MAX_LESSONS, ImproverPolicy, check_lessons, export_policy, import_policy,
                                      playbook_path, read_playbook, update_playbook)
from ouroloop.improver.screen import ScreenResult
from ouroloop.improver.verify import Verdict
from ouroloop.runtime.records import session_file
from ouroloop.runtime.trace import TraceAccessError, TraceStore

# ---------------------------------------------------------------------------- a hand-built workspace
SPLIT = {"t.a": "train", "t.b": "train", "t.c": "train", "t.dev": "dev", "t.test": "test"}
P = "package:sha256:" + "1" * 64          # the parent W's package
Q = "package:sha256:" + "2" * 64          # a child's package
SECRET = "dev-phrase-not-for-R"            # a dev assertion's parameter: never in what R reads


class Splits:
    """The frozen split table's two readers the evidence uses."""

    @staticmethod
    def split_of(task_id: str) -> str:
        return SPLIT[task_id]               # KeyError for a task the table does not know

    @staticmethod
    def ids(split: str) -> list[str]:
        return [t for t, s in SPLIT.items() if s == split]


def act(*tools: str, tail: tuple = ()) -> dict:
    """An act model call whose reply calls `tools`; `tail` is what it added to the messages."""
    return {"kind": "gateway_call", "role": "act", "messages_tail": list(tail) or [{"role": "user", "content": "go"}],
            "reply": {"text": "", "tool_calls": [{"id": f"c{i}", "name": n, "args": {}} for i, n in enumerate(tools)]}}


def results_of(*pairs: tuple) -> dict:
    """A tool message: (name, is_error) per result."""
    return {"role": "tool", "results": [{"id": f"r{i}", "name": n, "output": "x", "is_error": e}
                                        for i, (n, e) in enumerate(pairs)]}


# Every kind of line the evidence reads, and lines it must not count.
RICH = [
    act("api_fetch", "post_channel_message"),
    {"kind": "broker_event", "name": "api_fetch", "mutates": True, "refused": False},
    {"kind": "broker_event", "name": "api_fetch", "mutates": True, "refused": True},     # refused: no write
    {"kind": "broker_event", "name": "api_search", "mutates": False, "refused": False},  # a read: no write
    act("api_fetch", tail=(results_of(("api_fetch", True), ("post_channel_message", False)),)),
    {"kind": "gateway_call", "role": "aux", "messages_tail": [{"role": "user", "content": "summarize"}],
     "reply": {"text": "a summary", "tool_calls": [{"id": "s", "name": "aux_only_tool", "args": {}}]}},
    act("finish", tail=(results_of(("api_fetch", True)),)),
    {"kind": "hook", "site": "before_tool", "name": "triage", "action": "none"},          # did not act
    {"kind": "hook", "site": "before_model", "name": "anchor"},                           # no action at all
    {"kind": "hook", "site": "on_finish", "name": "verify", "action": "pushback"},
    {"kind": "skill_read", "name": "ops-notes", "found": True, "chars": 10},
    {"kind": "skill_read", "name": "missing-skill", "found": False, "chars": 0},
    {"kind": "library_retrieval", "site": "start", "query": "q", "shown": ["e1aaaaaaaa", "e2bbbbbbbb"]},
    {"kind": "library_retrieval", "site": "recall", "query": "q", "shown": ["e1aaaaaaaa", ""]},
    {"kind": "end", "termination": "finished"},
]
PLAIN = [act("finish"), {"kind": "end", "termination": "finished"}]
SHOWS_TOOL = [act("post_channel_message"), act("finish"), {"kind": "end", "termination": "finished"}]
NO_TOOL = [act("api_fetch"), act("finish"), {"kind": "end", "termination": "finished"}]
TRAIN_GRADING = [
    {"type": "slack_message_contains", "params": {"channel": "C1"}, "passed": False, "excluded": False},
    {"type": "slack_message_contains", "params": {"channel": "C2"}, "passed": False, "excluded": False},
    {"type": "hubspot_contact_stage", "params": {"id": "c1"}, "passed": True, "excluded": False},
    {"type": "ignored_type", "params": {}, "passed": False, "excluded": True},
]
DEV_GRADING = [
    {"type": "slack_message_contains", "params": {"text": SECRET}, "passed": False, "excluded": False},
    {"type": "hubspot_contact_stage", "params": {"id": "c9"}, "passed": False, "excluded": False},
    {"type": "slack_message_contains", "params": {"text": SECRET}, "passed": True, "excluded": False},
    {"type": "ignored_type", "params": {}, "passed": False, "excluded": True},
]


class Hand:
    def __init__(self, root: Path):
        self.root = root
        self.results = Results(root, "d")
        self.act = SimpleNamespace(root=root, results=self.results, splits=Splits())
        self.runs: dict[tuple, str] = {}
        self.n = 0

    def run(self, role: str, package_ref: str, split: str) -> str:
        key = (role, package_ref, split)
        if key not in self.runs:
            run = RunManifest(candidate=role, role=role, split=split, model={"model": "m"}, package_ref=package_ref)
            self.results.put_run(run)
            self.runs[key] = run.hash()
        return self.runs[key]

    def attempt(self, role: str, package_ref: str, task: str, *, success: bool, events=PLAIN, grading=(),
                valid: bool = True, steps: int = 3) -> TaskResult:
        split = SPLIT[task]
        run = self.run(role, package_ref, split)
        self.n += 1
        aid = f"{run}/{task}/r0/subject#{self.n}"
        row = TaskResult(task, task, split, 0, role, run, "m", success=success,
                         partial_credit=1.0 if success else 0.5, steps=steps, cost_usd=0.01,
                         termination="finished" if valid else "infrastructure",
                         validity="valid" if valid else "invalid", invalid_reason="" if valid else "worker crashed",
                         attempt_id=aid, evidence=aid)
        self.results.append(row)
        self.results.put_grading(row, list(grading))
        path = session_file(self.root, aid)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [{"kind": "task", "task_id": task, "split": split, "task_hash": f"h-{task}", "attempt_id": aid},
                 *events]
        path.write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
        return row


@pytest.fixture(scope="module")
def hand(tmp_path_factory) -> SimpleNamespace:
    """P: t.a failed (every kind of line), t.b passed then measured again, t.c only invalid; a library run of P that
    passed t.a later; P's valid and invalid dev attempts and a test attempt. Q: t.a fixed where the tool showed, t.b
    broken where it did not, t.c passed (P has no reading of it), and a dev attempt of its own."""
    h = Hand(tmp_path_factory.mktemp("hand"))
    rows = SimpleNamespace(
        a=h.attempt("base", P, "t.a", success=False, events=RICH, grading=TRAIN_GRADING),
        b0=h.attempt("base", P, "t.b", success=True, events=PLAIN),
        c_invalid=h.attempt("base", P, "t.c", success=True, valid=False),
        dev=h.attempt("base", P, "t.dev", success=False, grading=DEV_GRADING),
        dev_invalid=h.attempt("base", P, "t.dev", success=False, valid=False, grading=DEV_GRADING),
        test=h.attempt("base", P, "t.test", success=False, grading=DEV_GRADING),
        library=h.attempt("library", P, "t.a", success=True, events=PLAIN),
        b=h.attempt("measure", P, "t.b", success=True, events=PLAIN, steps=2),
        qa=h.attempt("screen", Q, "t.a", success=True, events=SHOWS_TOOL),
        qb=h.attempt("screen", Q, "t.b", success=False, events=NO_TOOL),
        qc=h.attempt("screen", Q, "t.c", success=True, events=SHOWS_TOOL),
        qdev=h.attempt("subject", Q, "t.dev", success=False,
                       grading=[{"type": "q_only_type", "params": {}, "passed": False, "excluded": False}]),
    )
    return SimpleNamespace(h=h, act=h.act, rows=rows)


# ---------------------------------------------------------------------------- evidence: the split guard
def test_a_session_that_is_not_train_is_refused(hand):
    trace = TraceStore(hand.h.root, Splits.split_of)
    ev = Evidence(hand.act, trace)
    for row in (hand.rows.dev, hand.rows.test):
        with pytest.raises(TraceAccessError):
            ev.of(row)
        for read in (trace.events, trace.trajectory, trace.task_hash, trace.finish):
            with pytest.raises(TraceAccessError):
                read(row.evidence)
        with pytest.raises(TraceAccessError):
            trace.get(f"{row.evidence}:L1")
        # a row that says train but names a dev transcript is refused by the trace itself
        with pytest.raises(TraceAccessError):
            ev.of(dataclasses.replace(hand.rows.a, attempt_id=row.attempt_id + "-forged", evidence=row.evidence))
    with pytest.raises(TraceAccessError):
        ev.footprint(hand.rows.dev, {"kind": "tool", "name": "api_fetch"})
    # and the evidence refuses a row that says dev by itself, even for a train transcript it has already read
    assert ev.of(hand.rows.a).task_id == "t.a"
    for split in ("dev", "test"):
        with pytest.raises(TraceAccessError):
            ev.of(dataclasses.replace(hand.rows.a, split=split))
    # a task the split table does not know is no train task either
    unknown = TraceStore(hand.h.root, lambda t: {"t.a": "train"}[t])
    with pytest.raises(TraceAccessError):
        unknown.events(hand.rows.b.evidence)
    assert unknown.events(hand.rows.a.evidence)
    # the train sessions on disk are all train
    listed = {s["session"] for s in trace.sessions()}
    assert hand.rows.a.evidence in listed and hand.rows.qa.evidence in listed
    assert not listed & {hand.rows.dev.evidence, hand.rows.dev_invalid.evidence, hand.rows.test.evidence,
                         hand.rows.qdev.evidence}


# ---------------------------------------------------------------------------- evidence: one attempt
def test_the_evidence_of_a_train_attempt(hand):
    ev = Evidence(hand.act).of(hand.rows.a)
    assert ev == AttemptEvidence(
        attempt_id=hand.rows.a.attempt_id, task_id="t.a", task_hash="h-t.a", session=hand.rows.a.evidence,
        result=hand.rows.a,
        failed_checks=({"type": "slack_message_contains", "params": {"channel": "C1"}},
                       {"type": "slack_message_contains", "params": {"channel": "C2"}}),
        tool_errors=("api_fetch", "api_fetch"),
        hooks=({"site": "on_finish", "name": "verify", "action": "pushback"},),
        tools_called=("api_fetch", "post_channel_message", "api_fetch", "finish"),
        skills_read=("ops-notes",), entries_shown=("e1aaaaaaaa", "e2bbbbbbbb"), writes=1)
    assert not ev.passed
    plain = Evidence(hand.act).of(hand.rows.b)
    assert (plain.failed_checks, plain.tool_errors, plain.hooks, plain.skills_read, plain.entries_shown,
            plain.writes, plain.tools_called) == ((), (), (), (), (), 0, ("finish",))
    assert plain.passed


def test_features_are_deterministic(hand):
    expected = {"task_id": "t.a", "termination": "finished", "limit": None, "success": False, "partial_credit": 0.5,
                "steps": 3, "failed_assertion_types": {"slack_message_contains": 2},
                "tool_errors": {"api_fetch": 2}, "writes": 1,
                "tools_called": ["api_fetch", "finish", "post_channel_message"],
                "hooks": ["on_finish:verify:pushback"], "skills_read": ["ops-notes"],
                "entries_shown": ["e1aaaaaaaa", "e2bbbbbbbb"]}
    first = Evidence.features(Evidence(hand.act).of(hand.rows.a))
    again = Evidence.features(Evidence(hand.act).of(hand.rows.a))      # a fresh reader: no cache in common
    assert first == expected
    assert json.dumps(first) == json.dumps(again)                         # the same keys in the same order too
    ev = Evidence(hand.act)
    assert ev.of(hand.rows.a) is ev.of(hand.rows.a)


def test_the_footprint_of_each_kind(hand):
    ev = Evidence(hand.act)
    rich, plain = hand.rows.a, hand.rows.b
    shown = {
        ("tool", "post_channel_message"): (True, False),
        ("tool", "api_fetch"): (True, False),
        ("tool", "aux_only_tool"): (False, False),         # called by an aux call, not the model acting
        ("tool", "never_called"): (False, False),
        ("hook", "verify"): (True, False),
        ("hook", "triage"): (False, False),                # action none
        ("hook", "anchor"): (False, False),                # no action
        ("skill", "ops-notes"): (True, False),
        ("skill", "missing-skill"): (False, False),        # read, not found
        ("memory", "snapshot"): (True, False),
        ("memory", None): (True, False),
        ("memory", "e2bbbbbbXXXXXX"): (True, False),       # an entry by its id's first 8 characters
        ("memory", "e3cccccc"): (False, False),
        ("removal", "x"): (False, False),
        ("test", "tests"): (False, False),
    }
    for kind in ("prompt", "agent", "config", "vendor", "model"):
        shown[(kind, "any")] = (True, True)
    got = {(k, n): (ev.footprint(rich, {"kind": k, "name": n}), ev.footprint(plain, {"kind": k, "name": n}))
           for k, n in shown}
    assert got == shown


# ---------------------------------------------------------------------------- evidence: readings and dev
def test_a_reading_is_the_packages_latest_valid_train_attempt_and_never_a_library_one(hand):
    ev = Evidence(hand.act)
    r = hand.rows
    assert ev.reading(P) == {"t.a": r.a, "t.b": r.b}          # not the library's t.a, not the invalid t.c
    assert ev.reading(Q) == {"t.a": r.qa, "t.b": r.qb, "t.c": r.qc}
    assert ev.missing(P) == ["t.c"]
    assert [e.attempt_id for e in ev.failed(P)] == [r.a.attempt_id]
    assert [e.attempt_id for e in ev.passed(P)] == [r.b.attempt_id]
    assert ev.reading("package:sha256:" + "9" * 64) == {}


def test_the_dev_histogram_has_types_and_counts_and_no_task(hand):
    hist = dev_histogram(hand.act, P)
    # the valid dev attempt's failed, scored assertions; not the invalid one's, the test's, the train's or Q's
    assert hist == {"split": "dev", "types": {"hubspot_contact_stage": 1, "slack_message_contains": 1},
                    "attempts": 1, "tasks": 1}
    assert dev_histogram(hand.act, Q) == {"split": "dev", "types": {"q_only_type": 1}, "attempts": 1, "tasks": 1}
    text = json.dumps(hist)
    assert "t.dev" not in text and SECRET not in text and "c9" not in text


# ---------------------------------------------------------------------------- ledger
def test_ledger_ids_follow_write_order(tmp_path):
    ledger = Ledger(tmp_path)
    one = ledger.row("measured", round_id="s/r0", line="d", passed=1, steps=3)
    two = ledger.row("diagnosed", round_id="s/r0", line="d", index=0, tasks=["t.a"])
    with pytest.raises(ValueError):
        ledger.row("not_a_stage", round_id="s/r0")
    three = ledger.row("stopped", round_id="s/r1", line="d", reason="nothing_to_fix")
    assert [one["id"], two["id"], three["id"]] == ["L1", "L2", "L3"]
    assert Ledger(tmp_path).row("playbook", round_id="s/r1")["id"] == "L4"      # a reopened ledger continues
    assert ledger.ids() == {"L1", "L2", "L3", "L4"}
    assert [r["id"] for r in ledger.rows()] == ["L1", "L2", "L3", "L4"]
    assert [r["id"] for r in ledger.rows(stage="diagnosed")] == ["L2"]
    assert [r["id"] for r in ledger.rows(round_id="s/r0")] == ["L1", "L2"]
    assert ledger.rows(stage="stopped", round_id="s/r0") == []
    assert ledger.rows()[1] == {**two, "tasks": ["t.a"]}


def test_the_projection_keeps_projected_fields_of_the_line(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.row("gated", round_id="s/r0", line="d", dir="notify", verdict="accepted", gain=0.25, fixed_count=1,
               regressed_count=0, receipt="receipt:sha256:" + "3" * 64, dev_fixed=["t.dev"], differs_from=[])
    ledger.row("verified", round_id="s/r0", line="d", dir="notify", kind="tool", passed=False, outcome="",
               reason="V2a: FAILED")
    ledger.row("measured", round_id="o/r0", line="other", passed=1, steps=2)
    ledger.row("stopped", round_id="s/r1", reason="budget")
    got = ledger.projection(line="d")
    assert got == [
        {"id": "L1", "stage": "gated", "round_id": "s/r0", "dir": "notify", "verdict": "accepted", "gain": 0.25,
         "fixed_count": 1, "regressed_count": 0},
        {"id": "L2", "stage": "verified", "round_id": "s/r0", "dir": "notify", "kind": "tool", "passed": False,
         "reason": "V2a: FAILED"},
        {"id": "L4", "stage": "stopped", "round_id": "s/r1", "reason": "budget"},
    ]
    assert [r["id"] for r in ledger.projection()] == ["L1", "L2", "L3", "L4"]
    assert all(set(r) <= set(PROJECTED) for r in ledger.projection())


def test_the_scoreboard(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.row("screened", round_id="s/r0", line="d", kind="tool", predicted_affected=["a", "b"], fixed=["a", "c"],
               regressed=["d"])
    ledger.row("screened", round_id="s/r0", line="d", kind="hook", predicted_affected=["a"], fixed=[],
               regressed=["a"])
    ledger.row("screened", round_id="o/r0", line="other", kind="tool", predicted_affected=["x"], fixed=["x"])
    ledger.row("declared", round_id="s/r0", line="d", kind="tool", predicted_affected=["a"], fixed=["a"])
    assert ledger.scoreboard("d") == {
        "all": {"candidates": 2, "hits": 1, "misses": 2, "unpredicted_regressions": 1},
        "hook": {"candidates": 1, "hits": 0, "misses": 1, "unpredicted_regressions": 0},
        "tool": {"candidates": 1, "hits": 1, "misses": 1, "unpredicted_regressions": 1}}
    assert ledger.scoreboard()["tool"] == {"candidates": 2, "hits": 2, "misses": 1, "unpredicted_regressions": 1}
    assert Ledger(tmp_path / "empty").scoreboard() == {}


def test_last_round_of(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.row("diagnosed", round_id="s/r2", index=2, tasks=["a"])
    ledger.row("diagnosed", round_id="s/r0", index=0, tasks=["a", "b"])
    ledger.row("diagnosed", round_id="s/r1", index=1, tasks=["b"])
    ledger.row("declared", round_id="s/r5", index=5, tasks=["c", "a"])
    assert [ledger.last_round_of(t) for t in ("a", "b", "c", "z")] == [2, 1, -1, -1]


# ---------------------------------------------------------------------------- archive
def decl(dir_name: str, cause: str = "missed_action", kind: str = "tool", **kw) -> Declaration:
    return Declaration(dir=dir_name, cause_id=f"s/r0:{cause}", kind=kind, site=kw.pop("site", "post"),
                       predicted_affected=("t.a",), trigger="a request asks for #ops", **kw)


def verdict(*passed: bool) -> Verdict:
    return Verdict(dir="x", steps={rung: {"passed": p, "detail": ""} for rung, p in zip(("V0", "V1", "V2a"), passed)})


FILES = {"manifest.json": "{}\n", "tools/post.py": "def run(ctx, args):\n    return {}\n"}


def test_archive_put_and_rows(tmp_path):
    objects = Objects(tmp_path)
    archive = Archive(tmp_path, objects)
    d = decl("notify")
    row = archive.put(0, "notify", FILES, d, verdict(True, True, False), line="d")
    assert {k: row[k] for k in ("round", "dir", "line", "kind", "cause", "site", "fixed", "screen", "receipt")} == {
        "round": 0, "dir": "notify", "line": "d", "kind": "tool", "cause": "missed_action", "site": "post",
        "fixed": "", "screen": None, "receipt": None}
    assert Declaration.from_json(objects.get(row["declaration"])) == d
    assert load_files(tmp_path, 0, "notify") == FILES
    assert json.loads((tmp_path / "rounds" / "r0" / "candidates" / "notify" / "record.json").read_text()) == row
    assert archive.rows() == [row]
    # the same candidate again, now with its screen and receipt: the latest row stands for it
    screen = ScreenResult(candidate="notify", fixed=("t.a",), regressed=(), net_flips=1, outcome="screened")
    again = archive.put(0, "notify", FILES, d, verdict(True, True, True), screen=screen,
                        receipt_ref="receipt:sha256:" + "4" * 64, line="d")
    assert archive.rows() == [again]
    other = archive.put(1, "elsewhere", {"a.py": "x"}, decl("elsewhere"), verdict(True), line="other")
    shared = archive.put(1, "shared", {"b.py": "x"}, decl("shared"), verdict(True), line="")
    assert archive.rows(line="d") == [again, shared]
    assert archive.rows(line="other") == [other, shared]
    assert archive.rows() == [again, other, shared]


def test_a_duplicate_is_the_same_files_in_an_earlier_round(tmp_path):
    archive = Archive(tmp_path, Objects(tmp_path))
    row = archive.put(1, "notify", FILES, decl("notify"), verdict(True), line="d")
    reordered = dict(reversed(list(FILES.items())))
    assert archive.duplicate(FILES, before_round=2) == row
    assert archive.duplicate(reordered, before_round=5) == row
    assert archive.duplicate(FILES, before_round=1) is None                 # the same round is not a repeat
    assert archive.duplicate(FILES, before_round=0) is None
    assert archive.duplicate({**FILES, "tools/post.py": "changed\n"}, before_round=2) is None
    assert archive.duplicate({"manifest.json": "{}\n"}, before_round=2) is None


def test_for_prompt(tmp_path):
    objects = Objects(tmp_path)
    archive = Archive(tmp_path, objects)
    first = archive.put(0, "notify", FILES, decl("notify"), verdict(True, True, False), line="d")
    screen = ScreenResult(candidate="notify2", fixed=("t.a",), regressed=("t.b",), net_flips=0, outcome="screened")
    second = archive.put(1, "notify2", {"x.py": "1"}, decl("notify2", kind="hook", site="on_finish"),
                         verdict(True, True, True), screen=screen, receipt_ref="receipt:sha256:" + "5" * 64, line="d")
    archive.put(1, "value", {"y.py": "1"}, decl("value", cause="wrong_value"), verdict(True), line="d")
    archive.put(1, "elsewhere", {"z.py": "1"}, decl("elsewhere"), verdict(True), line="other")
    want = [{"round": 0, "dir": "notify", "kind": "tool", "site": "post", "declaration": first["declaration"],
             "rungs": {"V0": True, "V1": True, "V2a": False}, "screen": None, "gated": False},
            {"round": 1, "dir": "notify2", "kind": "hook", "site": "on_finish", "declaration": second["declaration"],
             "rungs": {"V0": True, "V1": True, "V2a": True},
             "screen": {"outcome": "screened", "net_flips": 0, "fixed": ["t.a"], "regressed": ["t.b"]},
             "gated": True}]
    assert archive.for_prompt("d", "missed_action") == want
    assert archive.for_prompt("d", "missed_action", n=1) == want[1:]
    assert [r["dir"] for r in archive.for_prompt("d", "wrong_value")] == ["value"]
    assert [r["dir"] for r in archive.for_prompt("other", "missed_action")] == ["elsewhere"]
    assert archive.for_prompt("d", "unclear_request") == []


# ---------------------------------------------------------------------------- credit
def test_credit_is_conditioned_on_the_footprint(hand):
    ev = Evidence(hand.act)
    parent, child = ev.reading(P), ev.reading(Q)
    comps = [{"kind": "tool", "name": "post_channel_message", "change": "added"},
             {"kind": "prompt", "name": "system", "change": "modified"},
             {"kind": "test", "name": "tests", "change": "modified"},
             {"kind": "hook", "name": "gone", "change": "deleted"}]
    got = credit("release:sha256:" + "6" * 64, parent, child, comps=comps, evidence=ev)
    rel = "release:sha256:" + "6" * 64
    # t.c has no parent reading; the test and the deleted hook credit nothing
    assert got == [Credit(release_ref=rel, component="tool:post_channel_message", kind="tool", task_id="t.a",
                          conditioned=True, delta=1.0),
                   Credit(release_ref=rel, component="tool:post_channel_message", kind="tool", task_id="t.b",
                          conditioned=False, delta=-1.0),
                   Credit(release_ref=rel, component="prompt:system", kind="prompt", task_id="t.a",
                          conditioned=True, delta=1.0),
                   Credit(release_ref=rel, component="prompt:system", kind="prompt", task_id="t.b",
                          conditioned=True, delta=-1.0)]
    assert summary(got) == {
        "tool:post_channel_message": {"kind": "tool", "credit": 1.0, "shown": 1, "fixed": ["t.a"], "broke": []},
        "prompt:system": {"kind": "prompt", "credit": 0.0, "shown": 2, "fixed": ["t.a"], "broke": ["t.b"]}}


# ---------------------------------------------------------------------------- anti-bloat
def package_of(files: dict) -> Package:
    return Package(manifest=Manifest.from_json(json.loads(files["manifest.json"])), files=dict(files))


SMALL = {
    "manifest.json": json.dumps({
        "api_version": API_VERSION,
        "tools": [{"name": "post", "entrypoint": "tools/post.py:run", "enabled": True},
                  {"name": "plan", "entrypoint": "tools/plan.py:run", "enabled": True},
                  {"name": "off", "entrypoint": "tools/off.py:run", "enabled": False}],
        "hooks": [{"name": "ledger", "site": "after_tool", "entrypoint": "middlewares/guard.py:ledger"},
                  {"name": "verify", "site": "on_finish", "entrypoint": "middlewares/guard.py:verify"}],
        "skills": [{"name": "ops", "path": "skills/ops.md"}]}, indent=1) + "\n",
    "tools/post.py": "def run(ctx, args):\n    return {}\n",
    "tools/plan.py": "def run(ctx, args):\n    return {}\n",
    "tools/off.py": "def run(ctx, args):\n    return {}\n",
    "middlewares/guard.py": "def ledger(ctx, e):\n    return None\n\ndef verify(ctx, e):\n    return None\n",
    "skills/ops.md": "# ops\n",
    "prompt/system.md": "You act.\n",
}


def rnd_history(footprints: dict, installed: dict | None = None) -> dict:
    return {"footprints": dict(footprints), "installed": dict(installed or {})}


SHOWING = {"tool:post": 2, "tool:plan": 1, "hook:ledger": 0, "hook:verify": 3, "skill:ops": 1}


def proposed(history, package, n=3) -> list[str]:
    return [f"{c['kind']}:{c['name']}" for c, _ in removal_candidates(history, package, zero_rounds=n)]


def test_a_component_that_never_shows_for_n_rounds_is_proposed_and_not_one_round_earlier():
    pkg = package_of(SMALL)
    assert [f"{c['kind']}:{c['name']}" for c in present(pkg)] == ["tool:post", "tool:plan", "hook:ledger",
                                                                  "hook:verify", "skill:ops"]
    quiet_plan = {**SHOWING, "tool:plan": 0}
    history = [rnd_history(SHOWING), rnd_history(quiet_plan), rnd_history(quiet_plan), rnd_history(quiet_plan)]
    assert proposed(history[:2], pkg) == []
    assert proposed(history[:3], pkg) == ["hook:ledger"]           # tool:plan showed in the first of these three
    got = removal_candidates(history, pkg, zero_rounds=3)
    assert [f"{c['kind']}:{c['name']}" for c, _ in got] == ["tool:plan", "hook:ledger"]
    (plan, plan_files), (ledger, ledger_files) = got
    assert plan == {"kind": "tool", "name": "plan", "why": "never showed"}
    assert ledger == {"kind": "hook", "name": "ledger", "site": "after_tool", "why": "never showed"}
    assert plan_files == without(pkg, {"kind": "tool", "name": "plan"})
    assert ledger_files == without(pkg, {"kind": "hook", "name": "ledger"})
    assert proposed(history, pkg, n=5) == []
    assert proposed(history[:1], pkg, n=1) == ["hook:ledger"]


def test_an_install_with_no_conditioned_credit_is_proposed_after_n_rounds():
    pkg = package_of(SMALL)
    idle = [rnd_history(SHOWING, {"tool:post": 0.0}), rnd_history(SHOWING), rnd_history(SHOWING)]
    assert proposed(idle[:2], pkg) == []
    got = dict((f"{c['kind']}:{c['name']}", c) for c, _ in removal_candidates(idle, pkg, zero_rounds=3))
    assert got["tool:post"]["why"] == "no conditioned credit since its install"
    lost = [rnd_history(SHOWING, {"tool:post": -1.0}), rnd_history(SHOWING), rnd_history(SHOWING)]
    assert "tool:post" in proposed(lost, pkg)
    earned = [rnd_history(SHOWING, {"tool:post": 2.0}), rnd_history(SHOWING), rnd_history(SHOWING)]
    assert "tool:post" not in proposed(earned, pkg)
    # installed in a later round: N credited rounds from that one, and not one earlier
    late = [rnd_history(SHOWING), rnd_history(SHOWING, {"tool:post": 0.0}), rnd_history(SHOWING),
            rnd_history(SHOWING)]
    assert "tool:post" in proposed(late, pkg)
    assert "tool:post" not in proposed([*late[:1], rnd_history(SHOWING), *late[1:3]], pkg)
    # installed again in the last round: the count starts over
    again = [rnd_history(SHOWING, {"tool:post": 0.0}), rnd_history(SHOWING), rnd_history(SHOWING, {"tool:post": 0.0})]
    assert "tool:post" not in proposed(again, pkg)
    # a disabled tool is not present, so it is never proposed
    assert "tool:off" not in proposed(idle, pkg, n=1)


def test_rounds_before_a_component_existed_do_not_count():
    pkg = package_of(SMALL)
    before = {k: v for k, v in SHOWING.items() if k != "skill:ops"}       # the skill came with the last install
    history = [rnd_history(before), rnd_history(before), rnd_history({**SHOWING, "skill:ops": 0}, {"skill:ops": 0.0})]
    assert "skill:ops" not in proposed(history, pkg)


def test_without_drops_the_entry_and_its_file_only_when_nothing_else_names_it():
    pkg = package_of(SMALL)
    before = dict(pkg.files)
    tool = without(pkg, {"kind": "tool", "name": "post"})
    assert "tools/post.py" not in tool
    assert [t["name"] for t in json.loads(tool["manifest.json"])["tools"]] == ["plan", "off"]
    assert {p: t for p, t in tool.items() if p != "manifest.json"} == \
        {p: t for p, t in SMALL.items() if p not in ("manifest.json", "tools/post.py")}
    hook = without(pkg, {"kind": "hook", "name": "ledger"})
    assert "middlewares/guard.py" in hook                                  # verify still names it
    assert [h["name"] for h in json.loads(hook["manifest.json"])["hooks"]] == ["verify"]
    last = without(package_of(hook), {"kind": "hook", "name": "verify"})
    assert "middlewares/guard.py" not in last and json.loads(last["manifest.json"])["hooks"] == []
    skill = without(pkg, {"kind": "skill", "name": "ops"})
    assert "skills/ops.md" not in skill and json.loads(skill["manifest.json"])["skills"] == []
    manifest = json.loads(skill["manifest.json"])
    assert {k: v for k, v in manifest.items() if k != "skills"} == \
        {k: v for k, v in json.loads(SMALL["manifest.json"]).items() if k != "skills"}
    assert pkg.files == before                                             # the package itself is untouched


# ---------------------------------------------------------------------------- policy
def test_check_lessons_keeps_only_lessons_whose_citations_are_all_rows():
    text = "\n".join(["- Tools beat prompts for missed actions [L1]",
                      "-   Spaces are trimmed    [L1,L2]",
                      "- Cites a row that does not exist [L1, L9]",
                      "- Cites nothing",
                      "",
                      "Not a lesson [L1]",
                      "- Empty citation []",
                      "- Trailing comma [L2, ]"])
    kept, dropped = check_lessons(text, {"L1", "L2"})
    assert kept == ["- Tools beat prompts for missed actions [L1]", "- Spaces are trimmed [L1, L2]"]
    assert dropped == ["- Cites a row that does not exist [L1, L9]", "- Cites nothing", "Not a lesson [L1]",
                       "- Empty citation []", "- Trailing comma [L2, ]"]
    many = "\n".join(f"- lesson {i} [L1]" for i in range(MAX_LESSONS + 3))
    assert len(check_lessons(many, {"L1"})[0]) == MAX_LESSONS
    assert check_lessons("", {"L1"}) == ([], [])


class Recording:
    """The scripted R, with what it was asked kept."""

    def __init__(self):
        self.r, self.asked = ScriptedR(), []

    def chat(self, system, messages, specs, **kw):
        self.asked.append(json.loads(messages[0]["content"]))
        return self.r.chat(system, messages, specs, **kw)


def test_update_playbook_keeps_the_cited_lesson(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.row("measured", round_id="s/r0", line="other", passed=1, steps=2)
    ledger.row("screened", round_id="s/r0", line="d", kind="tool", fixed=["t.a"], receipt="hidden")
    ledger.row("stopped", round_id="s/r0", line="d", reason="no_candidate")
    playbook_path(tmp_path).write_text("- an old lesson [L2]\n", encoding="utf-8")
    r = Recording()
    got = update_playbook(tmp_path, ledger, researcher=r, line="d")
    # R saw the line's projected rows and the old playbook; it cited the first row shown (L2) and one lesson not at all
    assert r.asked == [{"playbook": "- an old lesson [L2]\n", "ledger_rows": ledger.projection(line="d")}]
    assert [row["id"] for row in r.asked[0]["ledger_rows"]] == ["L2", "L3"]
    assert got == {"lessons": 1, "dropped": ["- An uncited lesson"]}
    assert read_playbook(tmp_path) == "- A tool that posts the requested message fixed missed_action [L2]\n"


def test_a_lesson_citing_a_row_that_does_not_exist_is_dropped(tmp_path):
    ledger = Ledger(tmp_path)                       # empty: the scripted R cites L1, which is no row
    got = update_playbook(tmp_path, ledger, researcher=ScriptedR(), line="d")
    assert got["lessons"] == 0 and len(got["dropped"]) == 2
    assert read_playbook(tmp_path) == ""


# ---------------------------------------------------------------------------- one real round
@pytest.fixture(scope="module")
def rnd(tmp_path_factory, fake_upstream) -> SimpleNamespace:
    """One round on the rsi domain: the scripted R's tool is screened, gated, installed and credited."""
    s = round_activity(tmp_path_factory.mktemp("round"), fake_upstream, "tool")
    s.base = s.a.active_release()
    s.result = phases.round_(s.a, 0, out=quiet)
    assert s.result.installed, s.result
    s.new = s.a.releases.load(s.result.installed)
    s.ledger = Ledger(s.a.root)
    return s


def test_the_round_refuses_its_dev_sessions_and_the_histogram_names_no_task(rnd):
    a = rnd.a
    dev = [r for r in a.results.all() if r.split == "dev"]
    assert dev and {r.task_id for r in dev} == set(rsi.DEV)
    ev, trace = Evidence(a), TraceStore(a.root, a.splits.split_of)
    for row in dev:
        with pytest.raises(TraceAccessError):
            ev.of(row)
        with pytest.raises(TraceAccessError):
            trace.events(row.evidence)
    assert {trace.split(s["session"]) for s in trace.sessions()} == {"train"}
    hist = dev_histogram(a, rnd.base.package_ref)
    assert hist == {"split": "dev", "types": {"slack_message_contains": 1}, "attempts": 3, "tasks": 3}
    assert dev_histogram(a, rnd.new.package_ref)["types"] == {}
    text = json.dumps([hist, dev_histogram(a, rnd.new.package_ref)])
    assert not any(t in text for t in rsi.DEV + rsi.TRAIN + rsi.FINAL)


def test_the_round_ledger_ids_projection_and_scoreboard(rnd):
    rows = rnd.ledger.rows()
    assert [r["id"] for r in rows] == [f"L{i}" for i in range(1, len(rows) + 1)]
    assert [r["stage"] for r in rows] == ["measured", "diagnosed", "allocated", "declared", "verified", "screened",
                                          "gated", "installed", "credited", "playbook"]
    raw = (rnd.a.root / "ledger" / "experience.jsonl").read_text(encoding="utf-8")
    projection = json.dumps(rnd.ledger.projection(line=rnd.a.domain))
    for dev_task in rsi.DEV + rsi.FINAL:
        assert dev_task not in raw and dev_task not in projection
    assert "rsi.notify_after_update" in projection                      # train task ids are R's to read
    gated = rnd.ledger.rows(stage="gated")[0]
    assert {k: gated[k] for k in ("verdict", "fixed_count", "regressed_count")} == \
        {"verdict": "accepted", "fixed_count": 1, "regressed_count": 0}
    board = {"candidates": 1, "hits": 2, "misses": 0, "unpredicted_regressions": 0}
    assert rnd.ledger.scoreboard(rnd.a.domain) == {"all": board, "tool": board}
    assert rnd.ledger.last_round_of("rsi.notify_after_update") == 0
    assert rnd.ledger.last_round_of("rsi.update_stage") == -1


def test_the_round_archives_the_screen_and_receipt_of_its_candidate(rnd):
    archive = Archive(rnd.a.root, rnd.a.objects)
    row = next(r for r in archive.rows(line=rnd.a.domain) if r["dir"] == "notify")
    assert row["receipt"] == rnd.result.gate["receipt"]
    earlier = archive.for_prompt(rnd.a.domain, "missed_action")
    assert earlier[-1]["gated"] is True
    assert earlier[-1]["screen"]["net_flips"] == 2


def test_the_round_archive_holds_the_verified_candidate(rnd):
    archive = Archive(rnd.a.root, rnd.a.objects)
    (row,) = archive.rows(line=rnd.a.domain)
    assert {k: row[k] for k in ("round", "dir", "kind", "cause", "site")} == \
        {"round": 0, "dir": "notify", "kind": "tool", "cause": "missed_action", "site": "post_channel_message"}
    files = load_files(rnd.a.root, 0, "notify")
    assert archive.duplicate(files, before_round=1) == row and archive.duplicate(files, before_round=0) is None
    (earlier,) = archive.for_prompt(rnd.a.domain, "missed_action")
    assert earlier["rungs"] == {"V0": True, "V1": True, "V2a": True}


def test_the_round_credit_matches_a_recomputation(rnd):
    a = rnd.a
    (row,) = rnd.ledger.rows(stage="credited")
    ev = Evidence(a)
    base_pkg, new_pkg = a.package(rnd.base), a.package(rnd.new)
    comps = [c for c in components(base_pkg, new_pkg.files) if c["kind"] != "test"]
    assert comps == [{"kind": "tool", "name": "post_channel_message", "change": "added"}]
    again = summary(credit(rnd.new.ref(), ev.reading(rnd.base.package_ref), ev.reading(rnd.new.package_ref),
                           comps=comps, evidence=ev))
    assert row["credit"] == {k: v["credit"] for k, v in again.items()} == {"tool:post_channel_message": 2.0}
    assert sorted(again["tool:post_channel_message"]["fixed"]) == ["rsi.notify_after_update",
                                                                   "rsi.notify_after_update_variant"]
    assert round_state(a, 0)["steps"]["credited"]["credit"] == again
    history = row["history"]
    assert set(history["footprints"]) == {f"{c['kind']}:{c['name']}" for c in present(new_pkg)}
    assert history["installed"] == {"tool:post_channel_message": 2.0}
    # the seed's hooks that never acted are what the anti-bloat rule would propose after one credited round
    got = removal_candidates([history], new_pkg, zero_rounds=1)
    never = sorted(k.split(":", 1)[1] for k, n in history["footprints"].items() if n == 0)
    assert got and sorted(c["name"] for c, _ in got) == never
    assert "post_channel_message" not in {c["name"] for c, _ in got}
    for comp, files in got:
        names = [h["name"] for h in json.loads(files["manifest.json"])["hooks"]]
        assert comp["name"] not in names and names
        assert files["middlewares/write_guard.py"] == new_pkg.files["middlewares/write_guard.py"]
    assert removal_candidates([history], new_pkg, zero_rounds=2) == []


def test_the_round_playbook_keeps_only_the_cited_lesson(rnd):
    text = read_playbook(rnd.a.root)
    assert text == "- A tool that posts the requested message fixed missed_action [L1]\n"
    kept, dropped = check_lessons(text, rnd.ledger.ids())
    assert kept == text.splitlines() and dropped == []
    assert rnd.ledger.rows(stage="playbook")[0]["status"] == "1 lessons"
    assert "playbook" in rnd.r.calls


def test_export_and_import_of_the_policy_round_trip(rnd, tmp_path):
    a = rnd.a
    ref = export_policy(a)
    policy = a.objects.load(ref)
    assert isinstance(policy, ImproverPolicy)
    estimates = json.loads((a.root / "ledger" / "estimates.json").read_text(encoding="utf-8"))
    assert estimates and a.objects.get(policy.estimates_ref) == estimates
    assert a.objects.get(policy.playbook_ref) == {"text": read_playbook(a.root)}
    assert a.objects.get(policy.settings_ref) == dict(a.k["improver"])
    assert set(a.objects.get(policy.prompts_ref)) == {p.name for p in PROMPTS.glob("*.md")}
    b = SimpleNamespace(root=tmp_path / "next", objects=Objects(tmp_path / "next"), k=a.k)
    import_policy(b, ref, source_objects=a.objects)
    assert json.loads((b.root / "ledger" / "estimates.json").read_text(encoding="utf-8")) == estimates
    assert read_playbook(b.root) == read_playbook(a.root)
    assert export_policy(b) == ref                    # what was imported exports as the same policy
    Ledger(b.root).row("measured", round_id="next/r0", line="rsi", passed=0, steps=0)
    with pytest.raises(ValueError):
        import_policy(b, ref, source_objects=a.objects)
