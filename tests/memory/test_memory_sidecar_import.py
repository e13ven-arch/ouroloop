"""Cross-business-line import on the real sidecar (docs/design/MEMORY.md §4 importing, §9).

Line A (finance) streams two train tasks and releases its snapshot; line B (sales) imports it at its stream's start.
B's retrieval returns the imported entries under B's line with A's provenance kept, B's ledger starts them in
probation, A's collection is untouched; an import K does not declare, a held-out source line and a non-train row are
refused, and imported rows pass B's own literal check. All slow: the full run.
"""
from __future__ import annotations

import json

import pytest

from memory_support import (HASH, TASKS, attempt, fake, items, scope_of, sidecar, stamped, stream, task,  # noqa: F401
                            venv)
from ouroloop.contracts import TrainOnlyError
from ouroloop.memory import snapshot_digest
from ouroloop.memory.client import canonical_rows, parse_jsonl
from ouroloop.memory.importing import import_entries
from ouroloop.memory.retrieval import LibraryReader
from ouroloop.memory.settings import Settings
from ouroloop.memory.track import MemoryStepError

pytestmark = pytest.mark.slow


def shown(client, line, *, snapshot_text=None, live=None, name="T5") -> dict:
    return LibraryReader(client, Settings(), business_line=line, snapshot_text=snapshot_text,
                         live=live).for_task(TASKS[name], HASH[name]).event


def test_a_released_library_of_one_line_seeds_another(venv, fake, tmp_path):
    client = sidecar(tmp_path, venv, llm=scope_of(fake))
    try:
        a = stream(client, fake, tmp_path / "a", collection="ol-live-linea001", line="finance")
        a.process_batch(items(("T1", "T3")))
        snap = a.finish()["snapshot"]
        rows = parse_jsonl(snap)
        assert rows and all(json.loads(r["metadata"]["ol_sources"]) for r in rows)
        a_before = client.dump(a.collection)

        dig = snapshot_digest(snap)
        assert a.ledger["snapshot_digest"] == dig                    # one digest: the ledger's, K's, retrieval's
        assert shown(client, "finance", snapshot_text=snap)["snapshot"] == dig
        # an import K does not declare is refused: it is an input of B's identity, not a side command
        undeclared = stream(client, fake, tmp_path / "u", collection="ol-live-lineu001", line="sales")
        with pytest.raises(MemoryStepError, match="not one of K's memory.imports"):
            undeclared.import_snapshot(snap, source_release_ref="rel-A1", source_line="finance")
        wrong = stream(client, fake, tmp_path / "w", collection="ol-live-linew001", line="sales",
                       imports=({"release_ref": "rel-A1", "snapshot_digest": "0" * 16},))
        with pytest.raises(MemoryStepError, match="not one of K's memory.imports"):
            wrong.import_snapshot(snap, source_release_ref="rel-A1", source_line="finance")
        assert client.count(undeclared.collection) == client.count(wrong.collection) == 0

        b = stream(client, fake, tmp_path / "b", collection="ol-live-lineb001", line="sales",
                   imports=({"release_ref": "rel-A1", "snapshot_digest": dig},))
        got = b.import_snapshot(snap, source_release_ref="rel-A1", source_line="finance")
        assert sorted(got["added"]) == sorted(r["memory_id"] for r in rows) and got["skipped"] == []
        assert b.import_snapshot(snap, source_release_ref="rel-A1", source_line="finance")["again"] is True
        assert client.count(b.collection) == len(rows) and client.dump(a.collection) == a_before
        stored = {r["memory_id"]: r["metadata"] for r in parse_jsonl(client.dump(b.collection))}
        for r in rows:
            m = stored[r["memory_id"]]
            assert m["ol_line"] == "sales" and m["ol_source_line"] == "finance" and m["ol_origin"] == "imported"
            assert m["ol_imported_from"] == f"rel-A1:{r['memory_id']}"
            assert m["ol_task_hash"] == r["metadata"]["ol_task_hash"] and m["ol_split"] == "train"
            assert m["ol_trace_refs"] == r["metadata"]["ol_trace_refs"]          # back to A's trajectories
            assert "ol_sources" not in m and "ol_stats" not in m
            e = b.entries[r["memory_id"]]
            assert e["origin"] == "imported" and e["sources"] == [] and e["stats"]["n_shown"] == 0   # probation anew
        assert b.ledger["imports"][0]["release_ref"] == "rel-A1" and b.ledger["imports"][0]["snapshot_digest"] == dig
        assert {r["memory_id"] for r in parse_jsonl(b.live_path.read_text())} == set(stored)
        # B's retrieval finds them under B's line; A's line filter does not see them in B
        ev = shown(client, "sales", live=b.live_view())
        assert ev["shown"] and set(ev["shown"]) <= set(stored)
        assert shown(client, "finance", live=b.live_view())["shown"] == []
        # imported entries are credited on B's train like any other, from zero
        first = sorted(stored)[:1]
        b.process(task("T2"), attempt("T2", "first", True, shown=first, reason=f"library-used: {first[0][:8]}"),
                  None, attempt("T2", "baseline", True))
        assert b.entries[first[0]]["stats"]["n_shown"] == 1 and b.entries[first[0]]["stats"]["n_cited"] == 1
        assert "imported" in b.finish()["readout"]["D_by_origin"]
    finally:
        client.stop()


def test_imported_rows_pass_the_target_lines_literal_check_and_carry_no_foreign_keys(venv, fake, tmp_path):
    """B's train literals are B's answers: an imported entry is checked as B's own summaries are (lengths, one
    placeholder pass), and only provenance and ReMe's kept keys are copied from a row's metadata."""
    client = sidecar(tmp_path, venv, llm=scope_of(fake))
    try:
        rows = [stamped("Look the invoice up by its number (for example INV-20417) first.",
                        "When a task asks to email customers about overdue invoices"),
                # an address in another case is the same literal: fixed the same way, and then the same content as
                # the next row: one row is kept
                stamped("Write to ANA@Example.com before anyone else.", "When a task asks to email a customer",
                        task_name="T3"),
                stamped("Write to ana@example.com before anyone else.", "When a task asks to email a customer",
                        task_name="T6"),
                # longer than B's content cap
                stamped("Check the recipients. " * 100, "When a task asks to email customers", task_name="T2"),
                stamped("Read the exclusion list before any write.",
                        "When a task says some contacts must be skipped or excluded", task_name="T4")]
        rows[4]["metadata"]["experience"] = "a stray key a hand-made snapshot carries"
        snap = canonical_rows(rows)
        b = stream(client, fake, tmp_path / "b", collection="ol-live-linelit1", line="sales",
                   imports=({"release_ref": "rel-A2", "snapshot_digest": snapshot_digest(snap)},))
        got = b.import_snapshot(snap, source_release_ref="rel-A2", source_line="finance")
        stored = parse_jsonl(client.dump(b.collection))
        text = json.dumps(stored)
        assert "INV-20417" not in text and "ana@example.com" not in text.lower()
        assert len(stored) == len(got["added"]) == 3
        assert [d["memory_id"] for d in got["dropped"]] == [rows[3]["memory_id"]]
        assert got["dropped"][0]["why"].startswith("length")
        by_source = {r["metadata"]["ol_imported_from"].split(":")[1]: r for r in stored}
        fixed = by_source[rows[0]["memory_id"]]
        assert "<value>" in fixed["content"] and fixed["memory_id"] != rows[0]["memory_id"]   # id follows content
        assert sum(1 for r in stored if r["content"] == "Write to <value> before anyone else.") == 1
        assert all("experience" not in r["metadata"] for r in stored)
        assert b.ledger["counters"]["placeholder_fixed"] == 3 and b.ledger["imports"][0]["dropped"] == got["dropped"]
        assert set(b.active()) == {r["memory_id"] for r in stored}
    finally:
        client.stop()


def test_held_out_and_non_train_sources_are_refused(venv, tmp_path):
    client = sidecar(tmp_path, venv)
    try:
        rows = [stamped("Lesson one.", "When a task asks to email customers about overdue invoices")]
        snap = canonical_rows(rows)
        kw = dict(client=client, collection="ol-live-refuse01", heldout_domains=("operations",),
                  source_release_ref="rel-X")
        with pytest.raises(TrainOnlyError, match="held out"):
            import_entries(snap, source_line="operations", target_line="sales", **kw)
        ops = [{**rows[0], "metadata": {**rows[0]["metadata"], "ol_source_line": "operations"}}]
        with pytest.raises(TrainOnlyError, match="held out"):     # a finance release that carries ops experience
            import_entries(canonical_rows(ops), source_line="finance", target_line="sales", **kw)
        dev = [{**rows[0], "metadata": {**rows[0]["metadata"], "ol_split": "dev"}}]
        with pytest.raises(TrainOnlyError, match="only train"):
            import_entries(canonical_rows(dev), source_line="finance", target_line="sales", **kw)
        with pytest.raises(TrainOnlyError, match="in a snapshot of"):
            import_entries(snap, source_line="marketing", target_line="sales", **kw)
        with pytest.raises(ValueError, match="target itself"):
            import_entries(snap, source_line="finance", target_line="finance", **kw)
        assert client.count("ol-live-refuse01") == 0             # nothing of a refused import is stored
        assert import_entries(snap, source_line="finance", target_line="sales", **kw)["added"] == [
            rows[0]["memory_id"]]
        again = import_entries(snap, source_line="finance", target_line="sales", **kw)
        assert again["added"] == [] and again["skipped"] == [rows[0]["memory_id"]]   # already there: no second row
        assert client.count("ol-live-refuse01") == 1
    finally:
        client.stop()
