"""The memory's platform rules (docs/design/MEMORY.md §4 rules.py, §5-§8, §11 "规则"): train-only, provenance and the
ReMe metadata kept, the literal check with one placeholder pass, citation credit, retirement, capacity, snapshot rows,
the stream's readout and abort, promotion. Pure functions over ledger-shaped dicts: no client, no sidecar, no venv."""
from __future__ import annotations

import hashlib
import json
import math

import pytest

from ouroloop.contracts import TrainOnlyError
from ouroloop.harness import literals as L
from ouroloop.memory import rules as R
from ouroloop.memory.settings import ORIGINS, Settings

S = Settings()
A, B, C, D = "aaaaaaaa00000001", "bbbbbbbb00000002", "cccccccc00000003", "dddddddd00000004"


def entry(mid: str, *, status: str = "active", born_at: int = 0, origin: str = "success", content: str = "",
          events=(), **stats) -> dict:
    """A ledger entry as track.StreamStep keeps it, with `stats` laid over new_stats()."""
    s = R.new_stats()
    s.update(stats)
    return {"memory_id": mid, "origin": origin, "born_at": born_at, "status": status, "sources": [], "stats": s,
            "row": {"memory_id": mid, "content": content}, "events": list(events)}


def shown_entry(mid: str, n: int, credit_sum: float = 0.0, **kw) -> dict:
    return entry(mid, n_shown=n, credit_sum=credit_sum, **kw)


# ---------------------------------------------------------------------------------------------- train-only
@pytest.mark.parametrize("split", ["dev", "test", "", "Train"])
def test_only_train_experience_enters_a_library(split):
    with pytest.raises(TrainOnlyError, match="only train"):
        R.require_train(split, "sales", ("operations",))


def test_a_held_out_line_is_refused_even_from_its_train_split():
    with pytest.raises(TrainOnlyError, match="held out"):
        R.require_train("train", "operations", ("operations",))
    with pytest.raises(TrainOnlyError, match="held out"):
        R.require_train("train", "finance", ["operations", "finance"])
    assert R.require_train("train", "sales", ("operations",)) is None
    assert R.require_train("train", "operations", ()) is None and R.require_train("train", "operations", None) is None


# ---------------------------------------------------------------------------------------------- provenance
MEMORY = {
    "workspace_id": "ol-live-0123abcd", "memory_id": "0123456789abcdef", "memory_type": "task",
    "when_to_use": "When a request asks to update a contact and post in a channel",
    "content": "1. Update the contact's stage first.\n2. Then post the phrase.", "score": 0.82,
    "author": "research/reme:success", "time_created": "2026-10-09 10:00:00", "time_modified": "2026-10-09 10:00:01",
    "vector": [0.1, 0.2, 0.3],
    # ReMe's extraction op stores the model's whole JSON item here: the lesson again, plus whatever the model wrote,
    # some of it named like MemoryNode's own fields or like our provenance
    "metadata": {"step_type": "  action  ", "confidence": 0.912345, "tags": ["crm", "slack", "crm"],
                 "tools_used": "update_contact,post_message", "experience": "1. Update the contact's stage first.",
                 "when_to_use": "a second trigger", "content": "a second lesson", "memory_type": "personal",
                 "author": "the model", "score": 9.9, "memory_id": "ffffffffffffffff", "workspace_id": "elsewhere",
                 "time_created": "yesterday", "vector": [1.0], "ol_split": "test", "ol_line": "operations"}}
STAMP = dict(line="sales", source_line="sales", split="train", task_hash="th1", activity="act1", release_ref="rel1",
             sessions=["s2", "s1", "s2"], trace_refs=["tr:9", "tr:1"], origin="success", born_at=7, source_score=1,
             heldout_domains=("operations",))
MEMORY_NODE_FIELDS = ("workspace_id", "memory_id", "memory_type", "when_to_use", "content", "score", "time_created",
                      "time_modified", "author", "metadata", "vector")


def test_stamp_adds_the_provenance_and_keeps_only_reme_meta_keys_of_the_models_metadata():
    before = json.dumps(MEMORY, sort_keys=True)
    row = R.stamp(MEMORY, **STAMP)
    meta = row["metadata"]
    assert set(meta) == set(R.PROVENANCE_KEYS) | set(R.REME_META_KEYS)
    assert {k: meta[k] for k in R.PROVENANCE_KEYS} == {
        "ol_line": "sales", "ol_source_line": "sales", "ol_split": "train", "ol_task_hash": "th1",
        "ol_activity": "act1", "ol_release_ref": "rel1", "ol_sessions": '["s1", "s2"]',
        "ol_trace_refs": '["tr:1", "tr:9"]', "ol_origin": "success", "ol_reme_step_type": "action", "ol_born_at": 7,
        "ol_source_score": 1.0, "ol_validation_score": 0.82, "ol_imported_from": ""}
    assert isinstance(meta["ol_born_at"], int) and isinstance(meta["ol_source_score"], float)
    assert all(isinstance(v, (str, int, float)) for v in meta.values())      # scalars only: ReMe joins a list
    # none of the model's extra keys reaches a MemoryNode field when the row is stored: the row's own fields stand
    assert not set(meta) & set(MEMORY_NODE_FIELDS)
    for k in ("workspace_id", "memory_id", "memory_type", "when_to_use", "content", "score", "author"):
        assert row[k] == MEMORY[k]
    assert "vector" not in row
    assert json.dumps(MEMORY, sort_keys=True) == before                  # the memory itself is not changed


def test_stamp_blanks_the_clock_so_equal_streams_give_equal_bytes():
    row = R.stamp(MEMORY, **STAMP)
    assert row["time_created"] == "" and row["time_modified"] == "" and row["message_time"] == ""
    later = {**MEMORY, "time_created": "2027-01-01 00:00:00", "time_modified": "2027-01-01 00:00:09",
             "vector": [0.9, 0.9, 0.9]}
    assert json.dumps(R.stamp(later, **STAMP), sort_keys=True) == json.dumps(row, sort_keys=True)
    assert R.stamp({**MEMORY, "message_time": "m1"}, **STAMP)["message_time"] == "m1"


def test_stamp_refuses_a_non_train_or_held_out_source_and_records_an_import():
    with pytest.raises(TrainOnlyError):
        R.stamp(MEMORY, **{**STAMP, "split": "dev"})
    with pytest.raises(TrainOnlyError):
        R.stamp(MEMORY, **{**STAMP, "source_line": "operations"})
    row = R.stamp({**MEMORY, "metadata": None, "score": None}, **{**STAMP, "release_ref": None, "sessions": None,
                                                                  "trace_refs": None, "imported_from": "rel0:sales"})
    meta = row["metadata"]
    assert meta["ol_imported_from"] == "rel0:sales" and meta["ol_release_ref"] == ""
    assert meta["ol_sessions"] == "[]" and meta["ol_trace_refs"] == "[]" and meta["ol_validation_score"] == 0.0
    assert meta["ol_reme_step_type"] == "" and set(meta) == set(R.PROVENANCE_KEYS)


def test_reme_metadata_keeps_bounded_scalars_of_its_four_keys():
    tags = [f"tag{i:02d}" for i in range(20)] + ["x" * 100, "  ", None, True, {"k": 1}, 7, 1.5, "tag00"]
    got = R.reme_metadata({"step_type": " " + "s" * 60 + " ", "confidence": 0.123456789, "tags": tags,
                           "tools_used": "b_tool,a_tool, a_tool", "experience": "the lesson", "other": 1})
    assert set(got) == {"step_type", "confidence", "tags", "tools_used"}
    assert got["step_type"] == "s" * 40 and got["confidence"] == 0.1235
    kept = json.loads(got["tags"])
    assert len(kept) == 12 and kept == sorted(kept) and kept[:2] == ["1.5", "7"]       # sorted, at most 12
    assert all(len(t) <= 64 for t in kept) and "True" not in kept
    assert json.loads(got["tools_used"]) == ["a_tool", "b_tool"]          # ReMe's comma-joined form read back
    assert json.loads(R.reme_metadata({"tags": '["b", "a"]'})["tags"]) == ["a", "b"]   # our JSON form read back
    assert R.reme_metadata(got) == got                                   # a stored row reads back unchanged
    long = R.reme_metadata({"tags": ["y" * 100]})
    assert json.loads(long["tags"]) == ["y" * 64]


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), "0.9", None])
def test_reme_metadata_drops_a_confidence_that_is_not_a_finite_number(bad):
    assert "confidence" not in R.reme_metadata({"confidence": bad})


def test_reme_metadata_drops_a_blank_or_non_string_step_type_and_a_non_list_tag_value():
    assert R.reme_metadata({"step_type": "   ", "tags": {"a": 1}, "tools_used": 5}) == {}
    assert R.reme_metadata({"step_type": 3}) == {} and R.reme_metadata(None) == {}
    assert R.reme_metadata({"confidence": 2}) == {"confidence": 2.0}


def test_origin_of_is_the_extraction_op_in_the_author():
    assert [R.origin_of({"author": f"research/reme:{o}"}) for o in ORIGINS] == list(ORIGINS)
    for author in ("research/reme:validation", "success", "", None):
        with pytest.raises(ValueError, match="names no extraction op"):
            R.origin_of({"author": author, "memory_id": "m"})


def test_content_id_is_remes_16_hex_of_the_contents_sha256():
    assert R.content_id("a lesson") == hashlib.sha256(b"a lesson").hexdigest()[:16]
    assert R.content_id(None) == R.content_id("") == hashlib.sha256(b"").hexdigest()[:16]


# ---------------------------------------------------------------------------------------------- literals
# what a literal is comes from harness.literals: a value of exactly one train task, schema words and shared values out
ROWS = [
    {"task_id": "t1", "assertions": [{"params": {"order": "ORD-4471", "to": "dana.reyes@acme.io",
                                                 "cc": "ops@acme.io"}}]},
    {"task_id": "t2", "assertions": [{"params": {"order": "ORD-9902", "cc": "ops@acme.io", "owner": "Sam Okafor"}}]},
    {"task_id": "t3", "assertions": [{"params": {"owner": "Sam Okafor Jr", "ref": "ref_9:<value>"}}]},
]
FORBIDDEN = L.forbidden(L.literal_index(ROWS))


def test_the_forbidden_set_is_harness_literals_one_task_values():
    assert FORBIDDEN == {"ORD-4471", "ORD-9902", "dana.reyes@acme.io", "Sam Okafor", "Sam Okafor Jr", "ref_9:<value>"}


def test_a_memory_without_a_literal_passes_unchanged():
    fields = {"when": "When an order is late", "content": "Copy ops@acme.io; ORD-44710 is not a train value."}
    got, hits = R.literal_pass(fields, FORBIDDEN)
    assert (got, hits) == (fields, []) and got is not fields


def test_one_placeholder_pass_fixes_a_single_task_literal_in_every_field():
    fields = {"when": "When order ORD-4471 is late",
              "content": "Mail DANA.REYES@ACME.IO and copy ops@acme.io; ORD-44710 is another order."}
    got, hits = R.literal_pass(fields, FORBIDDEN)
    assert got == {"when": "When order <value> is late",
                   "content": "Mail <value> and copy ops@acme.io; ORD-44710 is another order."}
    assert sorted(h["literal"] for h in hits) == ["ORD-4471", "dana.reyes@acme.io"]
    assert {h["path"] for h in hits} == {"when.md", "content.md"}
    assert L.findings({"x.md": got["when"] + "\n" + got["content"]}, None, FORBIDDEN) == []


def test_the_pass_replaces_the_longest_literal_first():
    got, hits = R.literal_pass({"content": "Ask Sam Okafor Jr, not Sam Okafor."}, FORBIDDEN)
    assert got == {"content": "Ask <value>, not <value>."}
    assert sorted(h["literal"] for h in hits) == ["Sam Okafor", "Sam Okafor Jr"]


def test_a_literal_that_survives_the_pass_drops_the_memory():
    # replacing ORD-4471 writes another train task's value, ref_9:<value>: the second check finds it
    got, hits = R.literal_pass({"when": "When a ref is asked", "content": "Look up ref_9:ORD-4471 first."}, FORBIDDEN)
    assert got is None and [h["literal"] for h in hits] == ["ORD-4471"]


def test_an_allowed_literal_passes():
    fields = {"content": "Mail dana.reyes@acme.io about ORD-4471."}
    got, hits = R.literal_pass(fields, FORBIDDEN, allowed={"dana.reyes@acme.io"})
    assert got == {"content": "Mail dana.reyes@acme.io about <value>."} and [h["literal"] for h in hits] == ["ORD-4471"]
    assert R.literal_pass(fields, FORBIDDEN, allowed={"dana.reyes@acme.io", "ORD-4471"}) == (fields, [])
    assert R.literal_pass(fields, ()) == (fields, [])


# ---------------------------------------------------------------------------------------------- citation
def test_the_finish_reason_is_read_first_then_the_last_text():
    assert R.parse_citation("done\nlibrary-used: aaaaaaaa", "library-used: bbbbbbbb") == ["aaaaaaaa"]
    assert R.parse_citation("done, no line", "text\nlibrary-used: bbbbbbbb00000002\n") == ["bbbbbbbb"]
    assert R.parse_citation(None, "Library-Used: bbbbbbbb") == ["bbbbbbbb"]
    # a line in the finish reason decides even when it names nothing
    assert R.parse_citation("library-used: none", "library-used: bbbbbbbb") == []


def test_the_last_citation_line_wins_and_ids_are_8_hex_sorted_once():
    reason = "library-used: aaaaaaaa\nthen I changed my mind\nlibrary-used: CCCCCCCC, bbbbbbbb, cccccccc00000003"
    assert R.parse_citation(reason, None) == ["bbbbbbbb", "cccccccc"]
    assert R.parse_citation("library-used: aaaaaaaa\nlibrary-used: none", None) == []


@pytest.mark.parametrize("reason,text", [(None, None), ("", ""), ("I used the library", "aaaaaaaa"),
                                         ("library used aaaaaaaa", None)])
def test_no_citation_line_gives_none(reason, text):
    assert R.parse_citation(reason, text) is None


@pytest.mark.parametrize("line", ["library-used: none", "LIBRARY-USED:NONE", "library-used: nothing helped",
                                  "library-used:"])
def test_a_line_naming_no_id_gives_an_empty_list(line):
    assert R.parse_citation(line, None) == []


def test_resolve_ids_maps_8_hex_to_the_known_full_ids_in_order():
    assert R.resolve_ids([B[:8], A, "deadbeef", A[:8], B], {A, B, C}) == [B, A]
    assert R.resolve_ids(None, {A}) == [] and R.resolve_ids([A], set()) == []
    # two known ids share a prefix: the first of them in sorted order, every time
    twins = {"aaaaaaaa00000009", "aaaaaaaa00000002"}
    assert R.resolve_ids(["aaaaaaaa"], twins) == ["aaaaaaaa00000002"]
    assert R.resolve_ids(["aaaaaaaa00000009"], twins) == ["aaaaaaaa00000009"]


def test_used_entries_without_a_line_shares_among_all_shown():
    assert R.used_entries([A, B, A], None) == ([A, B], 0.5, [])
    assert R.used_entries([A, B, C, D], None) == ([A, B, C, D], 0.25, [])
    assert R.used_entries([], None) == ([], 0.0, [])


def test_used_entries_with_a_line_is_the_cited_shown_ones_each_whole():
    assert R.used_entries([A, B], [A[:8], "deadbeef"]) == ([A], 1.0, ["deadbeef"])
    assert R.used_entries([A, B], [B[:8], A[:8]]) == ([B, A], 1.0, [])
    assert R.used_entries([A, B], []) == ([], 1.0, [])


# ---------------------------------------------------------------------------------------------- credit
def test_without_a_line_every_shown_entry_shares_the_advantage():
    entries = {A: entry(A), B: entry(B)}
    got = R.credit(entries, task_hash="t1", shown=[A, B], cited=None, r=1, b=0.0, stable_pass=False)
    assert got == {"used": [A, B], "share": 0.5, "cited_unshown": [], "adv": 1.0}
    for mid in (A, B):
        assert entries[mid]["stats"] == {"n_shown": 1, "n_cited": 0, "credit_sum": 0.5, "harm_tasks": [],
                                         "fixes": 1, "fix_tasks": ["t1"]}


def test_with_a_line_only_the_cited_entries_are_credited_and_counted_cited():
    entries = {A: entry(A), B: entry(B)}
    got = R.credit(entries, task_hash="t1", shown=[A, B], cited=[A[:8], "deadbeef"], r=1, b=0.25,
                   stable_pass=False)
    assert got == {"used": [A], "share": 1.0, "cited_unshown": ["deadbeef"], "adv": 0.75}
    assert entries[A]["stats"] == {**R.new_stats(), "n_shown": 1, "n_cited": 1, "credit_sum": 0.75}
    assert entries[B]["stats"] == {**R.new_stats(), "n_shown": 1}
    R.credit(entries, task_hash="t2", shown=[A, B], cited=[], r=0, b=1.0, stable_pass=True)    # library-used: none
    assert entries[A]["stats"]["n_shown"] == 2 and entries[A]["stats"]["credit_sum"] == 0.75
    assert entries[A]["stats"]["harm_tasks"] == [] and entries[B]["stats"] == {**R.new_stats(), "n_shown": 2}


def test_harm_is_counted_in_distinct_tasks_and_only_on_stable_passes():
    entries = {A: entry(A), B: entry(B)}
    R.credit(entries, task_hash="t5", shown=[A], cited=None, r=0, b=1.0, stable_pass=True)
    R.credit(entries, task_hash="t5", shown=[A], cited=None, r=0, b=1.0, stable_pass=True)       # the same task
    assert entries[A]["stats"]["harm_tasks"] == ["t5"] and entries[A]["stats"]["credit_sum"] == -2.0
    R.credit(entries, task_hash="t6", shown=[A], cited=None, r=0, b=0.5, stable_pass=False)       # not stable
    R.credit(entries, task_hash="t7", shown=[A], cited=None, r=1, b=1.0, stable_pass=True)        # passed
    assert entries[A]["stats"]["harm_tasks"] == ["t5"] and entries[A]["stats"]["credit_sum"] == -2.5
    R.credit(entries, task_hash="t1", shown=[A, B], cited=[B[:8]], r=0, b=1.0, stable_pass=True)  # B cited only
    assert entries[A]["stats"]["harm_tasks"] == ["t5"] and entries[B]["stats"]["harm_tasks"] == ["t1"]
    R.credit(entries, task_hash="t0", shown=[A], cited=None, r=0, b=1.0, stable_pass=True)
    assert entries[A]["stats"]["harm_tasks"] == ["t0", "t5"] and entries[A]["stats"]["n_shown"] == 6


def test_a_fix_is_a_pass_where_the_base_never_passes():
    entries = {A: entry(A)}
    R.credit(entries, task_hash="t2", shown=[A], cited=None, r=1, b=0.0, stable_pass=False)
    R.credit(entries, task_hash="t3", shown=[A], cited=None, r=1, b=0.5, stable_pass=False)       # b > 0
    R.credit(entries, task_hash="t4", shown=[A], cited=None, r=0, b=0.0, stable_pass=False)       # failed
    R.credit(entries, task_hash="t1", shown=[A], cited=None, r=1, b=0, stable_pass=False)
    s = entries[A]["stats"]
    assert s["fixes"] == 2 and s["fix_tasks"] == ["t1", "t2"] and s["credit_sum"] == 2.5


def test_without_a_baseline_an_entry_is_shown_and_cited_but_not_credited():
    entries = {A: entry(A), B: entry(B)}
    got = R.credit(entries, task_hash="t1", shown=[A, B], cited=[A[:8]], r=0, b=None, stable_pass=True)
    assert got == {"used": [A], "share": 1.0, "cited_unshown": [], "adv": None}
    assert entries[A]["stats"] == {**R.new_stats(), "n_shown": 1, "n_cited": 1}
    R.credit(entries, task_hash="t2", shown=[A], cited=None, r=1, b=None, stable_pass=False)
    assert entries[A]["stats"] == {**R.new_stats(), "n_shown": 2, "n_cited": 1}


def test_credit_skips_unknown_ids_counts_a_repeat_once_and_starts_missing_stats():
    entries = {A: {"memory_id": A, "status": "active"}}
    R.credit(entries, task_hash="t1", shown=[A, A, "eeeeeeee00000005"], cited=None, r=1, b=0.0, stable_pass=False)
    assert entries[A]["stats"]["n_shown"] == 1 and entries[A]["stats"]["credit_sum"] == 0.5    # 1 / two shown
    assert set(entries) == {A}


def test_utility_and_mean_credit():
    assert R.utility(shown_entry(A, 2, 1.0)) == 0.25 and R.utility({}) == 0.0
    assert R.utility(shown_entry(A, 0, -1.0)) == -0.5                   # a prior of two showings
    assert R.mean_credit({}) is None and R.mean_credit(shown_entry(A, 0)) is None
    assert R.mean_credit(shown_entry(A, 3, -0.6)) == -0.2 and R.mean_credit(shown_entry(A, 3, 1.0)) == 0.333333


# ---------------------------------------------------------------------------------------------- retirement
@pytest.mark.parametrize("n,credit_sum,harm,why", [
    (2, -2.0, ["t1", "t2"], None),                                  # probation: nothing before three showings
    (3, -0.6, [], "mean credit -0.200 after 3 showings"),           # at the bound retires
    (3, -0.57, [], None),                                           # mean -0.19: above it
    (4, 0.0, ["t1", "t2"], None),                                   # harm in two tasks needs five showings
    (5, 0.0, ["t1", "t2"], "harm in 2 tasks after 5 showings"),
    (5, 0.0, ["t1"], None),                                         # one task's harm is not enough
    (6, -6.0, ["t1", "t2", "t3"], "harm in 3 tasks after 6 showings"),     # harm is named first
])
def test_retire_reason_by_the_default_settings(n, credit_sum, harm, why):
    assert R.retire_reason(shown_entry(A, n, credit_sum, harm_tasks=harm), S) == why


def test_retire_reason_reads_each_bound_from_the_settings():
    bad = shown_entry(A, 3, -3.0)
    assert R.retire_reason(bad, Settings(probation_shown=4)) is None
    assert R.retire_reason(bad, Settings(retire_min_shown=4)) is None       # out of probation, still too few
    assert R.retire_reason(bad, Settings(retire_mean_credit=-1.5)) is None
    assert R.retire_reason(shown_entry(A, 1, -1.0), Settings(probation_shown=1, retire_min_shown=1)) \
        == "mean credit -1.000 after 1 showings"
    harmed = shown_entry(A, 3, 0.0, harm_tasks=["t1"])
    assert R.retire_reason(harmed, Settings(retire_harm_tasks=1, retire_harm_min_shown=3)) \
        == "harm in 1 tasks after 3 showings"
    assert R.retire_reason(harmed, Settings(retire_harm_tasks=1, retire_harm_min_shown=4)) is None
    assert R.retire_reason({}, S) is None


# ---------------------------------------------------------------------------------------------- capacity
def test_nothing_is_evicted_at_or_below_max_active():
    two = Settings(max_active=2)
    entries = {A: shown_entry(A, 9, -9.0), B: shown_entry(B, 9, -9.0),
               C: shown_entry(C, 9, -9.0, status="retired"), D: shown_entry(D, 9, -9.0, status="superseded")}
    assert R.evictable(entries, two) == []
    assert R.evictable({}, two) == []


def test_eviction_takes_the_lowest_utility_out_of_probation_only():
    two = Settings(max_active=2)
    entries = {A: shown_entry(A, 3, 1.0), B: shown_entry(B, 3, -1.0), C: shown_entry(C, 2, -5.0),
               D: shown_entry(D, 3, 0.0)}
    assert R.evictable(entries, two) == [B, D]               # C is the worst, and still in probation
    assert R.evictable(entries, Settings(max_active=3)) == [B]
    young = {A: shown_entry(A, 2, -1.0), B: shown_entry(B, 0), C: shown_entry(C, 1, -2.0)}
    assert R.evictable(young, two) == []                     # over, but none out of probation


def test_eviction_order_breaks_ties_by_born_at_then_id():
    one = Settings(max_active=1)
    entries = {D: shown_entry(D, 3, 0.0, born_at=0), C: shown_entry(C, 3, 0.0, born_at=1),
               B: shown_entry(B, 3, 0.0, born_at=1), A: shown_entry(A, 3, 0.5, born_at=0)}
    assert R.evictable(entries, one) == [D, B, C]            # the older first, then by id
    assert R.evictable(dict(reversed(list(entries.items()))), one) == [D, B, C]


# ---------------------------------------------------------------------------------------------- snapshot rows
def test_snapshot_rows_merge_the_ledgers_sources_and_stats():
    stats = {**R.new_stats(), "n_shown": 4, "credit_sum": 1.5, "fixes": 2, "fix_tasks": ["t1", "t3"]}
    rows = [{"memory_id": A, "content": "x", "metadata": {"ol_line": "sales", "ol_sources": "stale",
                                                             "ol_stats": "stale"}},
            {"memory_id": B, "content": "y", "metadata": {}},
            {"memory_id": C, "content": "z"}]
    before = json.dumps(rows, sort_keys=True)
    out = R.snapshot_rows(rows, {A: {"sources": ["t3", "t1", "t3"], "stats": stats}, B: {}})
    assert [r["memory_id"] for r in out] == [A, B, C] and [r["content"] for r in out] == ["x", "y", "z"]
    assert out[0]["metadata"] == {"ol_line": "sales", "ol_sources": '["t1", "t3"]',
                                  "ol_stats": json.dumps(stats, sort_keys=True)}
    fresh = json.dumps(R.new_stats(), sort_keys=True)
    assert out[1]["metadata"] == {"ol_sources": "[]", "ol_stats": fresh} == out[2]["metadata"]
    assert json.dumps(rows, sort_keys=True) == before
    assert R.sources_of_row(out[0]) == ["t1", "t3"] and R.stats_of_row(out[0]) == stats
    assert R.sources_of_row(out[1]) == [] and R.stats_of_row(out[1]) == R.new_stats()


def test_sources_and_stats_of_a_row_without_them_or_with_bad_json_are_empty():
    for row in ({}, {"metadata": None}, {"metadata": {}}, {"metadata": {"ol_sources": "", "ol_stats": ""}},
                {"metadata": {"ol_sources": "[t1", "ol_stats": "{n"}}):
        assert R.sources_of_row(row) == [] and R.stats_of_row(row) == {}


# ---------------------------------------------------------------------------------------------- readout
def reading(r, b, *, stable=False, shown=(), cited=None, used=(), credit_each=0.0, origins=()) -> dict:
    return {"r": r, "b": b, "stable_pass": stable, "shown": list(shown), "cited": cited, "used": list(used),
            "credit_each": credit_each, "origins_used": list(origins)}


def test_readout_d_b_per_origin_and_citation_rates():
    order = ["t0", "t1", "t2", "t3", "t4"]
    readings = {
        "t0": reading(1, 0.0, shown=[A], used=[A], origins=["success", "failure", "success"]),
        "t1": reading(0, 1.0, stable=True, shown=[A, B], cited=["aaaaaaaa"], used=[A], origins=["success"]),
        "t2": reading(1, None, shown=[B], cited=[], origins=["comparative"]),        # no baseline: not in D
        "t3": reading(1, 0.5),
        "t4": reading(0, 0.0, shown=[B], cited=[]),
        "tx": reading(1, 0.0, shown=[A], used=[A], origins=["success"]),              # not in the order
    }
    entries = {A: entry(A), B: entry(B, origin="failure"), C: entry(C, status="retired", origin="comparative"),
               D: entry(D, status="superseded")}
    ro = R.readout(readings, order, entries)
    assert ro["tasks"] == 4 and ro["D"] == 0.5 and ro["B"] == 1
    assert ro["D_by_origin"] == {"failure": {"D": 1.0, "n": 1}, "success": {"D": 0.0, "n": 2}}
    assert ro["citation_rate"] == 0.6667 and ro["cited_any_rate"] == 0.3333     # of the shown rows: t0, t1, t4
    assert ro["shown_per_attempt"] == 1.0
    assert ro["first_half"] == 0.0 and ro["second_half"] == 0.25 and ro["halves_difference"] == 0.25
    assert ro["windows"] == [{"from": 0, "to": 10, "n": 4, "effect": 0.125}]
    assert ro["credit_rank_correlation"] is None                                  # one entry in both halves
    assert ro["entries"] == {"active": 2, "retired": 1, "superseded": 1, "by_origin": {"failure": 1, "success": 1}}


def test_b_counts_the_stable_passes_the_first_encounter_failed():
    order = ["t0", "t1", "t2", "t3"]
    readings = {"t0": reading(0, 1.0, stable=True), "t1": reading(1, 1.0, stable=True), "t2": reading(0, 0.5),
                "t3": reading(0, None)}
    ro = R.readout(readings, order, {})
    assert ro["B"] == 1 and ro["D"] == -1.5 and ro["tasks"] == 3


def test_readout_of_nothing_is_empty_and_never_divides_by_zero():
    ro = R.readout({}, [], {})
    assert ro["tasks"] == 0 and ro["D"] == 0 and ro["B"] == 0 and ro["windows"] == []
    assert ro["citation_rate"] is None and ro["shown_per_attempt"] is None and ro["p_second_half_better"] is None
    assert ro["halves_difference"] is None and ro["first_half"] is None


def test_the_learning_curve_windows_of_ten_and_the_halves():
    order = [f"t{i:02d}" for i in range(25)]
    readings = {t: reading(0 if i < 12 else 1, None if i == 3 else 0.0) for i, t in enumerate(order)}
    ro = R.readout(readings, order, {})
    assert ro["windows"] == [{"from": 0, "to": 10, "n": 9, "effect": 0.0}, {"from": 10, "to": 20, "n": 10,
                                                                          "effect": 0.8},
                             {"from": 20, "to": 30, "n": 5, "effect": 1.0}]
    assert ro["first_half"] == 0.0 and ro["second_half"] == 1.0 and ro["halves_difference"] == 1.0
    assert ro["p_second_half_better"] == 1.0 and ro["D"] == 13 and ro["tasks"] == 24
    # a window with no baseline reading is left out
    gap = {t: reading(1, 0.0) for t in order[:10]}
    assert [w["from"] for w in R.readout(gap, order, {})["windows"]] == [0]


def test_p_second_half_better_is_deterministic():
    first, second = [0, 1, -1, 0.5, 0, 0.5], [1, 0, 0.5, 1, -0.5, 0]
    order = [f"t{i}" for i in range(12)]
    readings = {t: reading(1, 1 - e) if e >= 0 else reading(0, -e) for t, e in zip(order, first + second)}
    p = R.readout(readings, order, {})["p_second_half_better"]
    assert 0.0 < p < 1.0 and p == R.readout(readings, order, {})["p_second_half_better"]
    assert p == R.halves_better(first, second)
    for a, b in (([0, 1, 0], [1, 1, -1]), ([0.5, 0, 1, 0], [1, 0, 0, 0.5]), ([-1, 0, 1], [0, 1, 0])):
        assert R.halves_better(a, b) == R.halves_better(a, b)
    assert R.halves_better([1, 1, 1], [0, 0, 0]) == 0.0 and R.halves_better([0, 0], [0, 0]) == 0.0
    assert R.halves_better([], [1]) is None and R.halves_better([1], []) is None


def test_credit_rank_correlation_pairs_the_credit_at_the_half_with_the_later_effect():
    early = {"t0": reading(0, 1.0, shown=[A, D], used=[A], credit_each=-1.0),     # D shown, not used: no credit
             "t1": reading(0, 0.0, shown=[B], used=[B], credit_each=0.0),
             "t2": reading(1, 0.5, shown=[C], used=[C], credit_each=0.5),
             "t3": reading(1, 0.0, shown=[D], used=[D], credit_each=1.0)}
    order = ["t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7"]
    same = {"t4": reading(0, 1.0, shown=[A]), "t5": reading(0, 0.0, shown=[B]), "t6": reading(1, 0.5, shown=[C]),
            "t7": reading(1, 0.0, shown=[D])}
    assert R.readout({**early, **same}, order, {})["credit_rank_correlation"] == 1.0
    reverse = {"t4": reading(1, 0.0, shown=[A]), "t5": reading(1, 0.5, shown=[B]), "t6": reading(0, 0.0, shown=[C]),
               "t7": reading(0, 1.0, shown=[D])}
    assert R.readout({**early, **reverse}, order, {})["credit_rank_correlation"] == -1.0
    # a later reading without a baseline is not an effect
    assert R.readout({**early, **same, "t7": reading(1, None, shown=[D])}, order, {})["credit_rank_correlation"] \
        == 1.0


def test_rank_correlation():
    assert R.rank_correlation([1, 2, 3], [10, 20, 30]) == 1.0
    assert R.rank_correlation([1, 1, 2, 3], [1, 2, 3, 4]) == 0.9487        # ties take their mean rank
    assert R.rank_correlation([1, 2], [1, 2]) is None and R.rank_correlation([1, 2, 3], [1, 2]) is None
    assert R.rank_correlation([1, 1, 1], [1, 2, 3]) is None


def test_snapshot_abort():
    assert R.snapshot_abort({"D": 0.0}, S, empty=False) == "D 0.0 <= 0.0"
    assert R.snapshot_abort({"D": -2}, S, empty=True) == "D -2 <= 0.0"
    assert R.snapshot_abort({"D": 0.5}, S, empty=True) == "the library is empty"
    assert R.snapshot_abort({"D": 0.5}, S, empty=False) is None
    assert R.snapshot_abort({"D": 0.5}, Settings(abort_d=1.0), empty=False) == "D 0.5 <= 1.0"


def test_reply_format_problems_name_each_counter_above_zero():
    assert R.reply_format_problems({}) == []
    assert R.reply_format_problems({"no_json_validation": 2, "no_json_extraction": 0, "unparsed_validation": 1,
                                    "calls": 9}) == ["no_json_validation: 2", "unparsed_validation: 1"]
    assert R.reply_format_problems({"no_json_extraction": 3}) == ["no_json_extraction: 3"]


def test_cosine_of_unit_vectors():
    assert R.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert math.isclose(R.cosine([0.6, 0.8], [0.6, 0.8]), 1.0)


# ---------------------------------------------------------------------------------------------- promotion
@pytest.mark.parametrize("content,many", [
    ("Check the record id.", False),
    ("Always confirm the stage. Ok.", False),                      # a short tail is not a second sentence
    ("1. Only one step", False),
    ("", False),
    (None, False),
    ("1. Update the stage.\n2. Post in the channel.", True),
    ("- update the stage\n- post it", True),
    ("2) look up\n3) post", True),
    ("Update the contact stage first. Then post the phrase in the channel.", True),
])
def test_multi_step(content, many):
    assert R.multi_step(content) is many


def test_distilled_from_counts_the_tasks_whose_summary_gave_the_lesson():
    e = entry(A, events=[{"n": 0, "task_hash": "t1", "how": "added"},
                         {"n": 1, "task_hash": "t2", "how": "store_dedup"},
                         {"n": 2, "task_hash": "t2", "how": "store_dedup"},
                         {"n": 3, "task_hash": "t3", "how": "same_id"},
                         {"n": 4, "task_hash": "t4", "how": "imported"},
                         {"n": 5, "task_hash": "t5", "how": "credited"},
                         {"n": 6, "how": "added"}])
    assert R.distilled_from(e) == {"t1", "t2", "t3"}
    assert R.distilled_from({}) == set()


STEPS = "1. Update the contact's stage first.\n2. Then post the phrase in #ops."
NOTE = "Post in #ops"


def from_tasks(*tasks):
    return [{"n": i, "task_hash": t, "how": "added" if i == 0 else "store_dedup"} for i, t in enumerate(tasks)]


def promotion_entries() -> dict:
    return {
        "p0": entry("p0", content=STEPS, events=from_tasks("t1", "t2"), fixes=2),             # a skill
        "p1": entry("p1", content=NOTE, events=from_tasks("t1", "t2"), fixes=2),              # a note
        "p2": entry("p2", content=STEPS, events=from_tasks("t1", "t2"), fixes=1),             # too few fixes
        "p3": entry("p3", content=STEPS, events=from_tasks("t1", "t1", "t1"), fixes=3),       # one source
        "p4": entry("p4", content=STEPS, events=from_tasks("t1", "t2", "t3"), fixes=3, status="retired"),
        "p5": entry("p5", content=STEPS, events=from_tasks("t1", "t2", "t3"), fixes=3, status="superseded"),
        "p6": {**entry("p6", events=from_tasks("t3", "t4", "t5"), fixes=4), "row": None, "content": STEPS},
    }


def test_a_procedure_that_fixed_tasks_from_several_sources_is_a_skill():
    assert [(e["memory_id"], why) for e, why in R.promotable(promotion_entries(), S)] == [("p0", "skill"),
                                                                                         ("p6", "skill")]


def test_a_mechanizable_entry_is_a_cause_and_a_retired_or_superseded_one_is_never_promoted():
    asked = []

    def mech(e):
        asked.append(e["memory_id"])
        return e["memory_id"] in ("p0", "p1", "p4", "p5")

    got = R.promotable(promotion_entries(), S, mech)
    assert [(e["memory_id"], why) for e, why in got] == [("p0", "cause"), ("p1", "cause"), ("p6", "skill")]
    assert asked == ["p0", "p1", "p6"]                       # only entries past the counts are asked
    assert R.promotable(promotion_entries(), S, lambda e: False) == R.promotable(promotion_entries(), S)


def test_promotion_reads_its_counts_from_the_settings():
    entries = promotion_entries()
    assert [e["memory_id"] for e, _ in R.promotable(entries, Settings(promote_fixes=3))] == ["p6"]
    assert [e["memory_id"] for e, _ in R.promotable(entries, Settings(promote_sources=3))] == ["p6"]
    assert [e["memory_id"] for e, _ in R.promotable(entries, Settings(promote_fixes=1, promote_sources=1))] \
        == ["p0", "p2", "p3", "p6"]
    assert R.promotable({}, S) == []
