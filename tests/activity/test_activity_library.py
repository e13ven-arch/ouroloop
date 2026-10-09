"""The library phase (docs/design/MEMORY.md §7, §11; ACTIVITY §4) end to end on the miniature upstream's rsi domain:
the stirrup seed in a ProcessWorker, the scripted actor that forgets the #ops notice unless a note of the opening block
reminds it (rsi.BusinessWorker(forget_notify=True)), the in-process memory client whose summaries go through the real
bridge to the scripted research model (tests/memory/fake_memory.py), the baseline first.

Covered: D > 0 and the snapshot as a candidate package; the report's memory section and its bytes; the research calls
and the attempts each in their own account; a crash mid-stream, an invalid first encounter, a budget that runs out; the
cycle's library stop; the startup checks; a release carrying a snapshot read on dev; W0's baseline and the memory."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import rsi
from fake_memory import FakeMemoryClient, ScriptedResearch

from ouroloop.activity import phases
from ouroloop.activity.checks import startup_checks
from ouroloop.activity.workspace import open_activity
from ouroloop.contracts import short
from ouroloop.evaluate.attempt import task_hash
from ouroloop.evaluate.release import GateReceipt
from ouroloop.harness.kinds import MEMORY_PATH
from ouroloop.harness.package import Limits, build, load
from ouroloop.memory import snapshot_digest
from ouroloop.memory.retrieval import snapshot_collection
from ouroloop.memory.settings import Settings
from ouroloop.memory.snapshot import library_of
from ouroloop.memory.track import StreamStep
from ouroloop.runtime.records import read, session_file

LIBRARY_MARK = "[lib "
RETRY_NOTE = "A previous attempt at this very task was graded and failed these checks"


def quiet(_line):
    pass


class Crash(BaseException):
    """The process dying mid-stream: nothing in the phase may swallow it."""


def stream(root: Path, upstream: str, *, actor=None, memory: dict | None = None, **sections) -> SimpleNamespace:
    """A fresh activity with the memory on: the forgetful actor, the scripted research model behind the bridge, one
    in-process memory client (its store survives a restart of the service, as the chroma directory would)."""
    s = SimpleNamespace(actor=actor or rsi.BusinessWorker(forget_notify=True), research=ScriptedResearch(),
                        client=FakeMemoryClient(), starts=0)

    def client():
        s.starts += 1
        return s.client
    s.a = rsi.activity(root, upstream, provider=s.actor, research=s.research, memory_client=client,
                       memory={"enabled": True, **(memory or {})}, **sections)
    return s


def calls(s) -> tuple[int, int]:
    return s.actor.calls, len(s.research.calls)


def book(a) -> dict:
    return json.loads((a.root / "rsi" / "budget" / "book.json").read_text(encoding="utf-8"))


def ledger(a) -> dict:
    return json.loads((a.root / "memory" / a.domain / "stream.json").read_text(encoding="utf-8"))


def library_rows(a) -> list:
    runs = a.results.runs()
    return [r for r in a.results.all() if runs[r.run].candidate == "library"]


def hashes(a) -> dict:
    return {t: task_hash(a.suite.tasks[t]) for t in a.splits.ids("train")}


@pytest.fixture(scope="module")
def streamed(tmp_path_factory, fake_upstream):
    """One uninterrupted stream after the baseline: what the other cases are measured against."""
    s = stream(tmp_path_factory.mktemp("streamed"), fake_upstream)
    phases.baseline(s.a, out=quiet)
    s.before = calls(s)
    s.lines = []
    s.result = phases.library(s.a, out=s.lines.append)
    s.after = calls(s)
    return s


# ---------------------------------------------------------------------------- end to end
def test_the_stream_learns_from_train_and_freezes_its_library_into_a_candidate_package(streamed):
    s, a = streamed, streamed.a
    result, base = s.result, a.active_release()
    assert result["status"] == "done" and result["readout"]["tasks"] == len(rsi.TRAIN)
    assert result["readout"]["D"] > 0 and result["readout"]["entries"]["active"] > 0
    # the snapshot is a candidate: W_base with the library frozen in, built and stored, and the event names it
    (event,) = a.events.read("library_snapshot")
    assert {k: event[k] for k in ("base", "outcome", "package_ref", "parent", "snapshot_digest", "readout")} == {
        "base": base.ref(), "outcome": "candidate", "package_ref": result["snapshot"]["package_ref"],
        "parent": base.ref(), "snapshot_digest": result["snapshot"]["snapshot_digest"], "readout": result["readout"]}
    candidate, w_base = load(a.objects, event["package_ref"]), a.package(base)
    text = (a.root / "memory" / a.domain / "live.jsonl").read_text(encoding="utf-8")
    assert candidate.memory_text == text and snapshot_digest(text) == event["snapshot_digest"]
    assert candidate.manifest.memory == {"enabled": True, "snapshot": MEMORY_PATH}
    assert {p: t for p, t in candidate.files.items() if p not in (MEMORY_PATH, "manifest.json")} == \
        {p: t for p, t in w_base.files.items() if p != "manifest.json"}
    assert build(a.objects, dict(candidate.files), limits=Limits.from_core(a.k),
                 seed=a.package(a.seed_release())).ref() == candidate.ref()
    entries = library_of(candidate)
    assert len(entries) == result["readout"]["entries"]["active"]
    train = set(hashes(a).values())
    for e in entries:
        assert e["sources"] and set(e["sources"]) <= train and e["provenance"]["ol_split"] == "train"
        assert e["provenance"]["ol_release_ref"] == base.ref() and e["provenance"]["ol_line"] == a.domain
    assert s.lines[-1] == (f"library    the snapshot is a candidate: {short(event['package_ref'])} (the improver's "
                           f"ladder takes it from V3; stage 3)")


def test_the_stream_runs_w_base_with_memory_on_over_train_and_the_actor_reads_the_live_library(streamed):
    a, base = streamed.a, streamed.a.active_release()
    rows = library_rows(a)
    assert rows and {r.split for r in rows} == {"train"} and {r.system_release_ref for r in rows} == {base.ref()}
    runs = a.results.runs()
    packages = {runs[r.run].package_ref for r in rows}
    (ref,) = packages
    stream_package = load(a.objects, ref)
    assert stream_package.manifest.memory == {"enabled": True} and stream_package.memory_text == ""
    assert {r.attempt_id.rsplit("/", 1)[1].split("#")[0] for r in rows} == {"first", "retry"}
    assert sorted(r.task_id for r in rows if "/first#" in r.attempt_id) == sorted(rsi.TRAIN)
    # a first encounter with entries in front of it: the start line reads the live collection, leave-one-out on
    shown = []
    for r in rows:
        lines = read(session_file(a.root, r.attempt_id))
        start = [line for line in lines if line["kind"] == "library_retrieval" and line["site"] == "start"]
        assert [line["kind"] for line in lines][:1] == ["task"]
        if start:
            assert [line["kind"] for line in lines][1] == "library_retrieval"
            assert start[0]["collection"].startswith("ol-live-") and start[0]["snapshot"] is None
            assert start[0]["leave_one_out"] is True and start[0]["task_hash"] == hashes(a)[r.task_id]
            shown += start[0]["shown"]
    assert shown and any(LIBRARY_MARK in seen["system"] for seen in streamed.actor.seen)
    assert ledger(a)["plan"]["order"] == [hashes(a)[t] for t in phases.stream_order(a)]
    # a failed first encounter is tried once more with its failed checks as a note, after the block
    retried = [seen["system"] for seen in streamed.actor.seen if RETRY_NOTE in seen["system"]]
    assert retried and all(not s.index(LIBRARY_MARK) > s.index(RETRY_NOTE) for s in retried if LIBRARY_MARK in s)
    assert any(LIBRARY_MARK in s for s in retried)
    assert len({r.task_id for r in rows if "/retry#" in r.attempt_id}) == \
        sum(1 for r in ledger(a)["readings"].values() if r["retry"] is not None)


def test_the_research_calls_and_the_attempts_are_booked_in_their_own_accounts_and_sum_to_the_root(streamed):
    s, a = streamed, streamed.a
    actor_calls, research_calls = (x - y for x, y in zip(s.after, s.before))
    assert actor_calls > 0 and research_calls == ledger(a)["readout"]["counters"]["research_calls"] > 0
    data = book(a)
    scopes = data["scopes"]
    assert scopes["rsi-test:research:memory"]["parent"] == scopes["rsi-test:library"]["parent"] == "rsi-test"
    by_scope: dict = {}
    for r in data["reservations"].values():
        by_scope[r["scope_id"]] = by_scope.get(r["scope_id"], 0) + 1
    assert by_scope["rsi-test:research:memory"] == research_calls and by_scope["rsi-test:library"] == actor_calls
    bridge = [json.loads(line) for line in
              (a.root / "memory" / a.domain / "bridge_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(bridge) == research_calls and {b["status"] for b in bridge} == {200}
    spend = phases.report(a, write=False, out=quiet)["spend"]
    assert set(spend["phases"]) == {"baseline", "library", "research:memory"}
    assert spend["phases"]["library"]["model_calls"] == actor_calls
    assert spend["phases"]["research:memory"]["model_calls"] == research_calls
    for dim in ("usd", "model_calls", "input_tokens", "output_tokens"):
        assert sum(p[dim] for p in spend["phases"].values()) == pytest.approx(spend["root"][dim])


def test_the_reports_memory_section_reads_the_stream_and_is_the_same_bytes_twice(streamed):
    a = streamed.a
    first = phases.report(a, out=quiet)
    written = {p: (a.root / p).read_bytes() for p in ("report.json", "report.md")}
    second = phases.report(a, out=quiet)
    assert {p: (a.root / p).read_bytes() for p in ("report.json", "report.md")} == written
    assert first == second == json.loads(written["report.json"])
    m, result = first["memory"], streamed.result
    assert (m["status"], m["processed"], m["planned"], m["lost"], m["imports"]) == ("done", 5, 5, 0, 0)
    assert m["readout"] == json.loads(json.dumps(result["readout"]))
    assert m["snapshot"] == {"outcome": "candidate", "why": None, "package_ref": result["snapshot"]["package_ref"],
                             "snapshot_digest": result["snapshot"]["snapshot_digest"]}
    md = written["report.md"].decode()
    assert f"- stream done: 5/5 train tasks (0 lost), {m['dropped']} memories dropped at the checks" in md
    assert f"- D {m['readout']['D']}, B {m['readout']['B']}" in md
    assert f"- snapshot candidate {short(m['snapshot']['package_ref'])}" in md


def test_the_library_run_again_pays_for_nothing_and_records_the_snapshot_once(streamed):
    s, a = streamed, streamed.a
    before, reservations = calls(s), book(a)["reservations"]
    again = phases.library(a, out=quiet)
    assert again["status"] == "done" and again["snapshot"] == s.result["snapshot"]
    assert again["readout"] == s.result["readout"]
    assert calls(s) == before and book(a)["reservations"] == reservations
    assert len(list(a.events.read("library_snapshot"))) == 1


# ---------------------------------------------------------------------------- a crash, a lost task, the budget
@pytest.mark.parametrize("where", ["before the summary", "after the summary"])
def test_a_stream_cut_by_a_crash_resumes_and_pays_for_nothing_twice(tmp_path, fake_upstream, monkeypatch, streamed,
                                                                    where):
    """Killed at the third task: before its batch is summarised (its attempts paid, its summary not), or while the
    summary is written (the summary paid and on disk, a row stored that the ledger does not hold). The resumed phase
    reuses the attempts and the summary; actor and research calls add up to the uninterrupted stream's."""
    s = stream(tmp_path, fake_upstream)
    phases.baseline(s.a, out=quiet)
    start = calls(s)
    seen = {"n": 0}
    if where == "before the summary":
        real = StreamStep.process_batch

        def crashing(self, items):
            seen["n"] += 1
            if seen["n"] == 3:
                raise Crash("killed before the summary")
            return real(self, items)
        monkeypatch.setattr(StreamStep, "process_batch", crashing)
    else:
        real = StreamStep._commit

        def crashing(self, task, first, retry, baseline, pending):
            real(self, task, first, retry, baseline, pending)
            if pending["tasks"][task["task_hash"]]["memory_list"] and not seen["n"]:
                seen["n"] += 1
                raise Crash("killed while the summary was written")
        monkeypatch.setattr(StreamStep, "_commit", crashing)
    with pytest.raises(Crash):
        phases.library(s.a, out=quiet)
    cut = ledger(s.a)
    assert 0 < len(cut["processed"]) < len(rsi.TRAIN) and cut["status"] == "streaming"
    if where == "after the summary":
        assert cut["pending"] is not None and cut["pending"]["tasks"]
        assert len(s.client.collections[next(iter(s.client.collections))]) > len(
            [e for e in cut["entries"].values() if e["status"] == "active"])          # an orphan row in the store
    monkeypatch.undo()
    paid = {r.attempt_id for r in library_rows(s.a)}
    result = phases.library(s.a, out=quiet)
    assert result["status"] == "done" and result["readout"] == streamed.result["readout"]
    assert tuple(x - y for x, y in zip(calls(s), start)) == \
        tuple(x - y for x, y in zip(streamed.after, streamed.before))                 # nothing paid twice
    ids = [r.attempt_id for r in library_rows(s.a)]
    assert len(ids) == len(set(ids)) and paid <= set(ids)
    done = ledger(s.a)
    assert done["pending"] is None and done["readout"]["counters"]["research_calls"] == len(s.research.calls)
    store = s.client.collections[next(iter(s.client.collections))]
    assert set(store) == {k for k, e in done["entries"].items() if e["status"] == "active"}


def test_a_first_encounter_that_stays_invalid_is_lost_not_summarised_and_not_in_the_readout(tmp_path, fake_upstream):
    """The actor's provider fails at one train task (Lea Fontaine's) once the stream starts, on the first encounter
    and on its refill: the task is recorded lost, nothing of it is summarised or credited, the stream goes on."""
    class Failing(rsi.BusinessWorker):
        failing = False

        def chat(self, system, messages, specs, **kw):
            task = next((m["content"] for m in messages if m.get("role") == "user"), "")
            if self.failing and "Lea Fontaine booked a demo" in task:
                self.calls += 1
                raise RuntimeError("the provider failed")
            return super().chat(system, messages, specs, **kw)
    s = stream(tmp_path, fake_upstream, actor=Failing(forget_notify=True))
    phases.baseline(s.a, out=quiet)
    s.actor.failing = True
    result = phases.library(s.a, out=quiet)
    lost = hashes(s.a)["rsi.notify_after_update_variant"]
    led = ledger(s.a)
    assert result["status"] == "done" and led["lost"] == {lost: "the first encounter is infrastructure"}
    assert led["processed"][lost] == {"n": led["processed"][lost]["n"], "lost": "the first encounter is infrastructure"}
    assert len(led["processed"]) == len(rsi.TRAIN) and lost not in led["readings"] and lost not in led["order"]
    assert result["readout"]["tasks"] == len(rsi.TRAIN) - 1 and s.client.summaries == len(rsi.TRAIN) - 1
    assert all(lost not in e["sources"] for e in led["entries"].values())
    tried = [r for r in library_rows(s.a) if r.task_id == "rsi.notify_after_update_variant"]
    assert [(r.attempt_id.rsplit("#", 1)[1], r.failure_class) for r in tried] == \
        [("0", "infrastructure"), ("1", "infrastructure")]                           # the refill rule, then lost
    assert phases.report(s.a, write=False, out=quiet)["memory"]["lost"] == 1
    before = calls(s)
    assert phases.library(s.a, out=quiet)["readout"] == result["readout"] and calls(s) == before


def test_a_budget_that_runs_out_stops_the_phase_and_a_later_run_continues(tmp_path, fake_upstream, streamed):
    """Another holder sets aside all but $0.10 of the root, less than one call's reservation: the stream's first
    encounter is refused and the phase stops. Once the hold is released, the phase runs again and continues — the
    refused attempt run again, superseding it — and pays for nothing twice."""
    s = stream(tmp_path, fake_upstream)
    phases.baseline(s.a, out=quiet)
    start = calls(s)
    left = s.a.account("library").remaining()["effective_remaining"]["usd"]
    hold = s.a.account("other").hold("someone else's reservation", {"usd": round(left - 0.1, 6)})
    lines = []
    stopped = phases.library(s.a, out=lines.append)
    assert stopped == {"status": "budget_exhausted", "processed": 0}
    assert lines[-1] == "library    stopped: the budget refused a first encounter; run the phase again to continue"
    (refused,) = library_rows(s.a)
    assert refused.failure_class == "budget_exhausted" and calls(s) == start
    assert ledger(s.a)["processed"] == {} and list(s.a.events.read("library_snapshot")) == []
    s.a.budgets.settle(hold, None, "released")
    result = phases.library(s.a, out=quiet)
    assert result["status"] == "done" and result["readout"] == streamed.result["readout"]
    assert tuple(x - y for x, y in zip(calls(s), start)) == \
        tuple(x - y for x, y in zip(streamed.after, streamed.before))
    rows = library_rows(s.a)
    assert len({r.attempt_id for r in rows}) == len(rows)
    again = [r for r in rows if r.supersedes == refused.attempt_id]
    assert len(again) == 1 and again[0].task_id == refused.task_id and again[0].validity == "valid"
    before = calls(s)
    assert phases.library(s.a, out=quiet)["status"] == "done" and calls(s) == before


# ---------------------------------------------------------------------------- the cycle
def test_the_cycle_with_the_memory_on_stops_at_the_library_and_a_second_cycle_pays_nothing(tmp_path, fake_upstream):
    s = stream(tmp_path, fake_upstream)
    first = phases.cycle(s.a, until="library", out=quiet)
    assert (first["status"], first["at"]) == ("stopped", "library")
    assert first["steps"]["baseline"]["train"]["tasks"] == len(rsi.TRAIN)
    assert first["steps"]["library"]["status"] == "done" and first["steps"]["library"]["readout"]["D"] > 0
    assert json.loads((s.a.root / "rsi" / "cycle.json").read_text(encoding="utf-8"))["steps"] == \
        json.loads(json.dumps(first["steps"]))
    paid, reservations, starts = calls(s), book(s.a)["reservations"], s.starts
    second = phases.cycle(s.a, until="library", out=quiet)
    assert json.loads(json.dumps(second)) == json.loads(json.dumps(first))
    with pytest.raises(NotImplementedError, match="stage 3"):
        phases.cycle(s.a, out=quiet)
    assert calls(s) == paid and book(s.a)["reservations"] == reservations and s.starts == starts


# ---------------------------------------------------------------------------- startup checks
def test_the_library_refuses_before_paying_with_the_memory_off_or_on_a_held_out_line(tmp_path, fake_upstream):
    for name, memory, why in (("off", {"enabled": False}, "[memory] enabled = false: this activity runs no memory "
                                                          "stream"),
                              ("held", {"heldout_domains": ["operations", "rsi"]},
                               "rsi is a held-out business line ([memory] heldout_domains)")):
        s = stream(tmp_path / name, fake_upstream, memory=memory)
        with pytest.raises(RuntimeError, match=r"the library phase cannot start: .*" + why.replace("[", r"\[")
                           .replace("]", r"\]").replace("(", r"\(").replace(")", r"\)")):
            phases.library(s.a, out=quiet)
        assert calls(s) == (0, 0) and s.starts == 0 and list(s.a.results.all()) == []
        assert not (s.a.root / "memory").exists()


def test_the_library_refuses_a_k_whose_backend_pins_are_not_this_checkouts_sidecar(tmp_path, fake_upstream,
                                                                                  monkeypatch):
    s = stream(tmp_path, fake_upstream)
    assert startup_checks(s.a, phase="library") == []
    import ouroloop.memory as memory
    pinned = Settings.from_core(s.a.k).backend["flows_sha256"]
    monkeypatch.setattr(memory, "flows_sha256", lambda: "0" * 64)
    (problem,) = startup_checks(s.a, phase="library")
    assert problem == f"K's memory backend: memory.backend.flows_sha256 is {pinned!r}; this checkout's is {'0' * 64!r}"
    with pytest.raises(RuntimeError, match="the library phase cannot start: K's memory backend"):
        phases.library(s.a, out=quiet)
    assert calls(s) == (0, 0) and s.starts == 0


def test_without_an_in_process_client_the_library_needs_the_sidecars_venv(tmp_path, fake_upstream, monkeypatch):
    monkeypatch.setenv("OUROLOOP_MEMORY_VENV", str(tmp_path / "no-venv"))
    s = stream(tmp_path, fake_upstream)
    assert not [p for p in startup_checks(s.a, phase="library") if "venv" in p or "model" in p]
    real = open_activity(s.a.config, out=quiet, worker_factory=s.a.worker_factory,
                         provider_factory=s.a.provider_factory)                     # the sidecar itself
    venv = [p for p in startup_checks(real, phase="library") if p.startswith("the memory sidecar's venv: ")]
    assert len(venv) == 1 and str(tmp_path / "no-venv") in venv[0]
    with pytest.raises(RuntimeError, match="the memory sidecar's venv: "):
        phases.library(real, out=quiet)
    assert calls(s) == (0, 0) and real._memory is None


# ---------------------------------------------------------------------------- a release that carries a snapshot
def test_a_release_carrying_a_snapshot_reads_it_on_dev_and_leave_one_out_excludes_nothing(tmp_path, fake_upstream):
    s = stream(tmp_path, fake_upstream)
    a = s.a
    phases.baseline(a, out=quiet)
    w0_dev = {r.task_id: r for r in a.results.all() if r.split == "dev"}
    result = phases.library(a, out=quiet)
    candidate = load(a.objects, result["snapshot"]["package_ref"])
    text = candidate.memory_text
    parent = a.active_release()
    receipt = GateReceipt(plan_ref=phases.freeze(a, out=quiet)["gate_template"], subject_ref=candidate.ref(),
                          control_ref=parent.package_ref, parent_release=parent.ref(), verdict="accepted",
                          reasons=("scripted",), metrics={"gain": 0.3})
    receipt_ref = a.objects.put(receipt)
    release = a.releases.candidate(candidate.ref(), receipt_ref)
    paid = calls(s)
    live = {name: dict(rows) for name, rows in s.client.collections.items()}
    rows = phases.run_arm(a, release=release, split="dev", candidate="gate", role="candidate",
                          pairs=[(t, 0) for t in a.splits.ids("dev")], account=a.account("gate"))
    a.releases.register_receipt(receipt_ref, plan=receipt.plan_ref)
    a.releases.install(release, receipt_ref, expected_parent=parent.ref(), operation_id="gate-1")
    assert a.package().memory_text == text                                  # the active W carries the snapshot
    assert calls(s)[0] > paid[0] and calls(s)[1] == paid[1]                 # no research call on dev
    assert s.client.llm is None                                             # a reading sidecar: no research scope
    collection = snapshot_collection(text, Settings.from_core(a.k).retriever)
    assert {n: dict(r) for n, r in s.client.collections.items() if n != collection} == live   # the live store as was
    starts = []
    for r in rows:
        lines = read(session_file(a.root, r.attempt_id))
        assert [line["kind"] for line in lines][:2] == ["task", "library_retrieval"]
        start = lines[1]
        assert start["site"] == "start" and start["snapshot"] == snapshot_digest(text)
        assert start["collection"] == collection and start["library_size"] == len(library_of(candidate))
        assert start["excluded_as_source"] == 0 and start["leave_one_out"] is True
        starts.append(start)
    notify = next(r for r in rows if r.task_id == "rsi.dev_notify_after_update")
    assert not w0_dev["rsi.dev_notify_after_update"].success and notify.success      # the snapshot's #ops note
    assert any(st["shown"] for st in starts)


# ---------------------------------------------------------------------------- the hot path
def test_a_w0_baseline_without_memory_starts_no_memory_client(tmp_path, fake_upstream, monkeypatch):
    """W0 of a K with the memory off reads none: the baseline never makes the memory's client, needs no sidecar venv,
    and its attempts carry no block and no retrieval line."""
    monkeypatch.setenv("OUROLOOP_MEMORY_VENV", str(tmp_path / "no-venv"))
    s = stream(tmp_path, fake_upstream, memory={"enabled": False})
    assert not s.a.package().manifest.memory.get("enabled") and startup_checks(s.a, phase="baseline") == []
    phases.baseline(s.a, out=quiet)
    assert s.starts == 0 and s.a._memory is None and s.client.started == 0 and s.research.calls == []
    assert not (s.a.root / "memory").exists()
    for r in s.a.results.all():
        assert "library_retrieval" not in {line["kind"] for line in read(session_file(s.a.root, r.attempt_id))}
    assert all(LIBRARY_MARK not in seen["system"] for seen in s.actor.seen)


def test_a_w0_baseline_under_a_k_with_the_memory_on_reads_an_empty_library_and_writes_nothing(tmp_path,
                                                                                              fake_upstream):
    """K's memory on makes the seed's manifest say memory (HARNESS §7): W0 has no snapshot, so its attempts get no
    block, no start line, no research scope, and nothing is loaded or stored."""
    s = stream(tmp_path, fake_upstream)
    assert s.a.package().manifest.memory == {"enabled": True} and s.a.package().memory_text == ""
    phases.baseline(s.a, out=quiet)
    assert s.research.calls == [] and s.client.llm is None and s.client.collections == {}
    assert s.client.summaries == 0 and not (s.a.root / "memory" / s.a.domain / "stream.json").exists()
    for r in s.a.results.all():
        lines = read(session_file(s.a.root, r.attempt_id))
        assert not [line for line in lines if line["kind"] == "library_retrieval" and line["site"] == "start"]
    assert all(LIBRARY_MARK not in seen["system"] for seen in s.actor.seen)


# ---------------------------------------------------------------------------- the review's cases (2026-10-09)
class FailingAt(rsi.BusinessWorker):
    """The actor's provider fails on one task's text while `bad` names it."""
    bad = None

    def chat(self, system, messages, specs, **kw):
        task = next((m["content"] for m in messages if m.get("role") == "user"), "")
        if self.bad and task == self.bad:
            self.calls += 1
            raise RuntimeError("the provider failed")
        return super().chat(system, messages, specs, **kw)


def test_a_failed_batch_holding_a_lost_task_is_cut_the_same_on_the_next_run(tmp_path, fake_upstream, monkeypatch):
    """A batch of four whose first task is lost and whose summary then fails on the bridge: the lost task is recorded
    only once the batch is written, so the next run cuts the same batch, commits the pending summaries and goes on."""
    monkeypatch.setitem(rsi._preset["memory"], "stream_batch", 4)      # the real preset's value
    s = stream(tmp_path, fake_upstream, actor=FailingAt(forget_notify=True))
    assert s.a.k["memory"]["stream_batch"] == 4
    phases.baseline(s.a, out=quiet)
    first = phases.stream_order(s.a)[0]
    s.actor.bad = s.a.suite.tasks[first].trigger
    s.research.fail = {"validation": 1}
    with pytest.raises(Exception, match="research calls failed on the bridge"):
        phases.library(s.a, out=quiet)
    led = ledger(s.a)
    assert led["pending"] is not None and led["processed"] == {} and led["lost"] == {}
    s.actor.bad = None
    result = phases.library(s.a, out=quiet)
    led = ledger(s.a)
    assert result["status"] == "done" and led["pending"] is None and len(led["processed"]) == len(rsi.TRAIN)
    assert led["lost"] == {hashes(s.a)[first]: "the first encounter is infrastructure"}
    assert result["readout"]["tasks"] == len(rsi.TRAIN) - 1


def test_a_retry_the_budget_refused_stops_the_phase_and_runs_on_the_next(tmp_path, fake_upstream, monkeypatch):
    """A retry refused by the budget is no evidence: the phase stops before the batch is written, and the next run
    runs the retry again (superseding it) and goes on."""
    from ouroloop.contracts import BudgetExhausted
    from ouroloop.evaluate import budget as bg
    from ouroloop.memory import retrieval as rt
    state = {"refuse": True, "retrying": False}
    for_task, reserve = rt.LibraryReader.for_task, bg.BudgetAccount.reserve

    def noting(self, text, th, **kw):
        state["retrying"] = bool(kw.get("note"))
        return for_task(self, text, th, **kw)

    def refusing(self, oid, bound, **kw):
        if state["refuse"] and state["retrying"] and ":research:" not in self.scope_id:
            raise BudgetExhausted("no further paid operations: the root is spent")
        return reserve(self, oid, bound, **kw)
    monkeypatch.setattr(rt.LibraryReader, "for_task", noting)
    monkeypatch.setattr(bg.BudgetAccount, "reserve", refusing)
    s = stream(tmp_path, fake_upstream)
    phases.baseline(s.a, out=quiet)
    lines = []
    stopped = phases.library(s.a, out=lines.append)
    assert stopped["status"] == "budget_exhausted" and lines[-1].endswith("the budget refused a retry; run the phase "
                                                                          "again to continue")
    refused = [r for r in library_rows(s.a) if "/retry#" in r.attempt_id]
    assert [r.failure_class for r in refused] == ["budget_exhausted"]
    assert hashes(s.a)[refused[0].task_id] not in ledger(s.a)["processed"]
    state["refuse"] = False
    result = phases.library(s.a, out=quiet)
    again = [r for r in library_rows(s.a) if r.supersedes == refused[0].attempt_id]
    assert result["status"] == "done" and len(again) == 1 and again[0].validity == "valid"
    assert ledger(s.a)["readings"][hashes(s.a)[refused[0].task_id]]["retry"] is not None


def test_a_sidecar_that_dies_mid_stream_pauses_the_phase_and_loses_no_task(tmp_path, fake_upstream):
    """The sidecar stops answering once the library holds an entry: the phase pauses (nothing is recorded as lost
    for it), and the next run with a working sidecar streams every remaining task."""
    from ouroloop.contracts import MemorySidecarError

    class Dying(FakeMemoryClient):
        dead = False

        def _down(self):
            raise MemorySidecarError("POST /retrieve_task_memory_v6: ConnectError: connection refused")

        def retrieve(self, query, collection, **kw):
            if self.dead or kw.get("expected_count") and self.count(collection) >= 1:
                self.dead = True
                self._down()
            return super().retrieve(query, collection, **kw)

        def health(self):
            if self.dead:
                self._down()
            return super().health()
    s = stream(tmp_path, fake_upstream)
    s.client = Dying()
    s.a.memory_client_factory = lambda: s.client
    phases.baseline(s.a, out=quiet)
    lines = []
    paused = phases.library(s.a, out=lines.append)
    led = ledger(s.a)
    assert paused["status"] == "paused" and "ConnectError" in paused["why"] and led["lost"] == {}
    assert 0 < len(led["processed"]) < len(rsi.TRAIN) and lines[-1].startswith("library    paused: ")
    s.client.dead = False
    s.client.retrieve = lambda query, collection, **kw: FakeMemoryClient.retrieve(s.client, query, collection, **kw)
    result = phases.library(s.a, out=quiet)
    assert result["status"] == "done" and ledger(s.a)["lost"] == {}
    assert result["readout"]["tasks"] == len(rsi.TRAIN)


def test_the_library_refuses_before_paying_without_w_bases_train_readings(tmp_path, fake_upstream):
    s = stream(tmp_path, fake_upstream)
    with pytest.raises(ValueError, match="has none: run the baseline first"):
        phases.library(s.a, out=quiet)
    assert calls(s) == (0, 0) and not (s.a.root / "memory" / s.a.domain / "stream.json").exists()


def test_the_streams_own_attempts_never_count_as_w_bases_readings(streamed):
    """Under a K with the memory on the stream's package is W_base's own (HARNESS §7): b(t) still reads only the
    baseline's runs."""
    a = streamed.a
    readings = phases.base_readings(a, a.active_release())
    runs = a.results.runs()
    assert readings and all(runs[r.run].role != "library" for rows in readings.values() for r in rows)
    assert sum(len(rows) for rows in readings.values()) == len(rsi.TRAIN)


def test_a_w0_baseline_under_the_memory_on_starts_no_sidecar_and_needs_no_venv(tmp_path, fake_upstream, monkeypatch):
    """W0 holds no snapshot: its empty library starts nothing, so its baseline needs neither the client nor the venv."""
    monkeypatch.setenv("OUROLOOP_MEMORY_VENV", str(tmp_path / "no-venv"))
    s = stream(tmp_path, fake_upstream)
    s.a.memory_client_factory = None
    assert s.a.package().manifest.memory == {"enabled": True}
    assert startup_checks(s.a, phase="baseline") == []
    phases.baseline(s.a, out=quiet)
    assert s.a._memory is None and s.starts == 0
