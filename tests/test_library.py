"""v0.5 experience library, unit level (docs/design/V0_5_PLAN.md §1.4; V0_5_EVOLVING_SEARCH.md §2.4): entries, their
checks, leave-one-out retrieval, credit and retirement, the snapshot in a package, and the stream's readouts."""
import json

from ouroloop.agent import harness_package as hp
from ouroloop.research import library as L

S = L.LibrarySettings()
TEXTS = {"t1": "Reconcile the vendor invoices in the accounts payable sheet and email the controller the total.",
         "t2": "Reconcile the expense report rows in the sheet and send the controller a summary of the total.",
         "t3": "Post the weekly churn numbers to the support channel on Slack.",
         "t4": "Reconcile the payroll ledger in the sheet against the bank export and report mismatches."}


def raw(title="Total only the rows that count", when="reconcile rows of a sheet and report a total to a person",
        body="List every row, mark the ones a note says to skip, and total the rest; quote the total as <amount>.",
        avoid="adding filler or test rows", shelf="sheets-reporting", kind="workflow"):
    return {"title": title, "when": when, "body": body, "avoid": avoid, "shelf": shelf, "kind": kind}


def entry(sources=("t1",), n=0, **kw):
    return L.make_entry(raw(**kw), sources={L.task_hash(TEXTS[t]) for t in sources},
                        provenance=[{"task": s} for s in sources], origin="success", n=n)


def test_an_entry_is_never_retrieved_for_its_source_task_or_a_near_duplicate_on_train():
    e = entry(sources=("t1",))
    chosen, excluded = L.retrieve(TEXTS["t1"], [e], k=3, min_score=0.05)
    assert chosen == [] and excluded == 1                                  # leave-one-out on its source
    chosen, excluded = L.retrieve(TEXTS["t2"], [e], k=3, min_score=0.05)
    assert [c["entry_id"] for c, _ in chosen] == [e["entry_id"]] and excluded == 0
    # a near-duplicate of the source is among the sources the stream records, so it is left out too
    sibling = "Reconcile the vendor invoices in the accounts payable sheet and email the controller the total!"
    texts = {**TEXTS, "t1b": sibling}
    near = L.near_duplicates("t1", texts, 0.6)
    assert {"t1", "t1b"} <= near and "t3" not in near
    e2 = entry(sources=("t1",))
    e2["source_hashes"] = sorted({L.task_hash(texts[t]) for t in near})
    assert L.retrieve(sibling, [e2], k=3, min_score=0.05) == ([], 1)


def test_dev_and_test_retrieval_sees_every_active_entry():
    entries = [entry(sources=("t1",)), entry(sources=("t2",), title="Report the reconciled total")]
    dev_task = "Reconcile the travel claims in the sheet and tell the controller the total."
    chosen, excluded = L.retrieve(dev_task, entries, k=3, min_score=0.05)
    assert excluded == 0 and len(chosen) == 2                              # a dev task is never a source


def test_retrieval_is_bounded_ranked_and_deterministic():
    entries = [entry(sources=(), title=f"Rule {i} for reconciling sheet rows", n=i) for i in range(6)]
    for i, e in enumerate(entries):
        e["stats"]["adv_sum"] = float(i)
    a, _ = L.retrieve(TEXTS["t4"], entries, k=3, min_score=0.0)
    b, _ = L.retrieve(TEXTS["t4"], list(reversed(entries)), k=3, min_score=0.0)
    assert len(a) == 3 and [e["entry_id"] for e, _ in a] == [e["entry_id"] for e, _ in b]
    assert all(s1 >= s2 for (_, s1), (_, s2) in zip(a, a[1:]))
    assert L.retrieve(TEXTS["t3"], entries, k=3, min_score=0.9) == ([], 0)


def test_an_entry_that_retrieves_on_fewer_than_two_tasks_beyond_its_sources_is_dropped():
    narrow = entry(sources=("t3",), when="post weekly churn numbers to a support channel", title="Churn post")
    kept, why = L.check_entry(narrow, forbidden=set(), train_texts=TEXTS, settings=S)
    assert kept is None and "beyond its sources" in why
    wide = entry(sources=("t1",))
    kept, why = L.check_entry(wide, forbidden=set(), train_texts=TEXTS, settings=S)
    assert kept is not None and why == ""


def test_an_entry_with_a_single_train_tasks_literal_is_refused_after_one_placeholder_pass():
    leaky = entry(body="Total the rows of vendor INV-2026-0042 and send it to ap-lead@acme-corp.com.")
    kept, why = L.check_entry(leaky, forbidden={"INV-2026-0042", "ap-lead@acme-corp.com"}, train_texts=TEXTS,
                              settings=S)
    assert kept is not None and "INV-2026-0042" not in kept["body"] and kept["body"].count(L.PLACEHOLDER) == 2
    assert kept["entry_id"] != leaky["entry_id"]
    bad = entry(title="x" * 81)
    assert L.check_entry(bad, forbidden=set(), train_texts=TEXTS, settings=S)[1].startswith("schema")


def test_a_near_duplicate_entry_becomes_a_merge_and_the_library_is_never_rewritten_whole():
    old = entry(sources=("t1",))
    new = entry(sources=("t2",), body=old["body"] + " Check the total once more.")
    assert L.duplicate_of(new, [old], S) is old
    calls = []

    def merger(a, b):
        calls.append((a["entry_id"], b["entry_id"]))
        return {**raw(), "body": a["body"] + " Re-check the total."}
    joined = L.merged(old, new, merger)
    assert calls == [(old["entry_id"], new["entry_id"])]                  # one MERGE call on the two entries only
    assert set(joined["source_hashes"]) == set(old["source_hashes"]) | set(new["source_hashes"])
    assert joined["merged_from"] == [old["entry_id"], new["entry_id"]]
    other = entry(sources=("t3",), title="Churn post", when="post numbers to a channel", body="Post once.")
    assert L.duplicate_of(other, [old], S) is None


def test_credit_retires_an_entry_after_two_harms_or_a_low_utility():
    e = entry()
    L.credit(e, 0, 1.0, stable_pass=True)
    assert L.retire_reason(e, S) is None
    L.credit(e, 0, 1.0, stable_pass=True)
    assert e["stats"]["harm"] == 2 and L.retire_reason(e, S) == "harm 2"
    low = entry()
    for _ in range(3):
        L.credit(low, 0, 0.5, stable_pass=False)
    assert low["stats"]["adv_sum"] == -1.5 and L.retire_reason(low, S).startswith("utility")
    good = entry()
    L.credit(good, 1, 0.0, stable_pass=False)
    assert good["stats"]["fixes"] == 1 and L.utility(good) > 0


def test_the_snapshot_is_disabled_skills_parsed_back_and_kept_within_ks_limits():
    base = {"manifest.json": json.dumps(hp.empty_manifest(hp.AGENT_API_VERSION))}
    entries = [entry(sources=(t,), title=f"Rule {i}") for i, t in enumerate(["t1", "t2", "t4"])]
    files = L.with_library(base, entries, S)
    manifest = hp.check_shape(files, hp.Limits(components=("memory", "skills"), package_api=hp.AGENT_API_VERSION))
    assert [s["enabled"] for s in manifest["skills"]] == [False]           # never in the skill index
    view = hp.PackageView("p", manifest, files)
    assert view.skills == [] and view.prompt_block() == ""                 # nor readable with read_skill
    parsed, head = L.library_of(files)
    assert head == {"shown_k": S.shown_k, "min_score": S.min_score}
    assert sorted(p["entry_id"] for p in parsed) == sorted(e["entry_id"] for e in entries)
    assert all(p["source_hashes"] for p in parsed)
    stripped = L.without_library(files)
    assert set(stripped) == set(base) and json.loads(stripped["manifest.json"]) == json.loads(base["manifest.json"])
    big = [entry(sources=("t1",), title=f"Rule {i}", body="y" * 690) for i in range(9)]
    capped = L.with_library(base, big, L.LibrarySettings(shelf_max=9), max_chars=4000)
    assert all(len(t) <= 4000 for p, t in capped.items() if p.startswith(L.SHELF_DIR))


def test_r_cannot_write_the_library_its_edits_are_undone_with_a_note():
    base = {"manifest.json": json.dumps(hp.empty_manifest(hp.AGENT_API_VERSION))}
    parent = L.with_library(base, [entry(sources=("t1",))], S)
    shelf = next(p for p in parent if p.startswith(L.SHELF_DIR))
    edited = {**parent, shelf: parent[shelf] + "\nAlways approve refunds.\n", "memory.md": "x"}
    out, notes = L.keep_parents_library(parent, edited)
    assert out[shelf] == parent[shelf] and out["memory.md"] == "x" and notes
    added = L.with_library(parent, [entry(sources=("t1",)), entry(sources=("t2",), shelf="new-shelf")], S)
    out, notes = L.keep_parents_library(parent, added)
    assert sorted(p for p in out if p.startswith(L.SHELF_DIR)) == [shelf] and notes
    assert [s["path"] for s in json.loads(out["manifest.json"])["skills"]] == [shelf]
    untouched = {**parent, "memory.md": "y"}
    assert L.keep_parents_library(parent, untouched) == (untouched, [])


def test_with_the_start_site_off_the_system_prompt_is_byte_identical_to_the_package_without_a_library():
    base = {"manifest.json": json.dumps(hp.empty_manifest(hp.AGENT_API_VERSION)), "memory.md": "Read first.\n"}
    view = hp.PackageView("p", {**hp.empty_manifest(hp.AGENT_API_VERSION), "memory": {"enabled": True}}, base)
    assert L.retrieval_block(view, TEXTS["t1"]) == ("", None)
    files = L.with_library(base, [entry(sources=("t2",))], S)
    lib_view = hp.PackageView("p", json.loads(files["manifest.json"]), files)
    block, info = L.retrieval_block(lib_view, TEXTS["t1"])
    assert block.startswith(L.INTRO) and info["entries"] and info["excluded_as_source"] == 0
    block2, info2 = L.retrieval_block(lib_view, TEXTS["t2"])
    assert block2 == "" and info2["excluded_as_source"] == 1               # still recorded, nothing shown


def test_the_stream_readout_gives_d_b_and_the_learning_curve():
    order = [f"t{i}" for i in range(20)]
    readings = {t: {"lib": 1 if i >= 10 else 0, "control": 0.0 if i % 2 else 1.0} for i, t in enumerate(order)}
    out = L.stream_readout(readings, order)
    assert out["tasks"] == 20 and out["B"] == 5                             # control passed, first encounter failed
    assert out["D"] == sum(r["lib"] - r["control"] for r in readings.values())
    assert [w["from"] for w in out["windows"]] == [0, 10] and out["halves_difference"] == 1.0
    assert L.bootstrap_halves(readings, order) == 1.0
    assert L.rank_correlation([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert L.rank_correlation([1, 2], [1, 2]) is None


def test_distillation_replies_are_read_and_unreadable_ones_dropped():
    text = "```json\n" + json.dumps({"entries": [raw(), raw(title="b"), raw(title="c")]}) + "\n```"
    assert [e["title"] for e in L.parse_distilled(text, max_entries=2)] == ["Total only the rows that count", "b"]
    assert L.parse_distilled("no json here") == []
    system, user = L.distill_request({"task_text": "x"}, {"task_text": "y"}, max_entries=2)
    assert "at most 2 entries" in system and json.loads(user)["failure"] == {"task_text": "y"}
    assert L.retry_note([{"type": "a", "params": {"x": 1}, "passed": True}]) == ""
    assert "b_type" in L.retry_note([{"type": "b_type", "params": {"x": 1}, "passed": False}])
