"""v0.5 method library (docs/design/V0_5_PLAN.md §1.1–§1.3; V0_5_METHOD_LIBRARY.md §2–§4): the method contract in the
default agent, the kit and its check, the router, the reach check, the applicability learner and its receipts, R's
inputs and the playbook's citations — offline, with the reference methods as realistic fixtures.
"""
import importlib.util
import json

from ouroloop.agent import harness_package as hp
from ouroloop.agent.tools import ToolResult
from ouroloop.agent.worker import ProcessWorker
from ouroloop.chat import ChatTurn, ToolCall, ToolSpec
from ouroloop.research import methods as M

V2 = hp.Limits(components=("agent", "memory", "skills", "on_finish", "before_tool", "after_tool", "tools"),
               package_api=hp.AGENT_API_VERSION)


# ---------------------------------------------------------------------------- fixtures
def lay_out(tmp_path, files):
    """The files as a loaded bundle (what the worker writes), so a method module finds its _mlib.py beside it."""
    for path, text in files.items():
        full = tmp_path / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)
    return tmp_path


def load(root, path):
    spec = importlib.util.spec_from_file_location("t_" + path.replace("/", "_").replace(".py", "") + str(id(root)),
                                                  root / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Res:
    def __init__(self, output, is_error=False):
        self.output, self.is_error = output, is_error


class FakeCtx:
    """call_tool answers from `answers`: {(tool, url or query-prefix): payload}; a GET to an unknown url is a 404."""

    def __init__(self, answers, task="synthetic task", searches=None):
        self.answers, self.searches = answers, searches or {}
        self.view, self.calls = {"task": task, "events": []}, []

    def call_tool(self, name, args=None):
        args = dict(args or {})
        self.calls.append((name, args))
        if name == "api_search":
            for key, results in self.searches.items():
                if key in args.get("query", ""):
                    return Res(json.dumps({"results": results, "count": len(results)}))
            return Res(json.dumps({"results": [], "count": 0}))
        if str(args.get("method", "GET")).upper() != "GET":
            return Res(json.dumps({"ok": True}))
        payload = self.answers.get(args.get("url"))
        if payload is None:
            return Res(json.dumps({"error": {"code": 404, "message": "not found"}}), True)
        return Res(json.dumps(payload))


SHEET = "https://sheets.example.invalid/v4/spreadsheets/s1/values/Budget"
ROWS = [["channel", "amount", "notes"], ["Search", "1200", ""], ["Social", "800", ""], ["Email", "400", "skip this row"],
        ["Display", "600", ""], ["Filler test row", "999", ""], ["Video", "300", ""], ["Radio", "200", ""],
        ["Print", "100", "see the memo"]]


def reference_bundle(tmp_path, names=None):
    files, _ = M.kit_complete({}, M.reference_files(names))
    return lay_out(tmp_path, files), files


# ---------------------------------------------------------------------------- the reference methods
def test_the_row_ledger_classifies_every_row_and_journals_what_it_excluded(tmp_path):
    root, _ = reference_bundle(tmp_path, ["m_row_ledger"])
    mod = load(root, "tools/m_tables.py")
    ctx = FakeCtx({SHEET: {"values": ROWS}})
    out = json.loads(mod.row_ledger(ctx, {"url": SHEET, "amount_columns": ["amount"]}))
    excluded = {d["key"]: d["reason"] for d in out["excluded"]}
    assert set(excluded) == {"Email", "Filler test row"}                    # a skip note, a filler row
    assert out["included"] == 6 and out["totals"] == {"amount": 3200.0}
    assert [u["key"] for u in out["unclassified"]] == ["Print"]            # a note it could not read, said apart
    journal = json.loads((root / ".m_journal.json").read_text())
    assert {e["key"] for e in journal["excluded"]} == {"Email", "Filler test row"}
    assert {"token": "3,200.00", "what": "total of amount", "source": SHEET} in journal["required"]
    # the variant without noise filtering keeps the filler row; a wrong argument is an answer, never an exception
    keep = json.loads(mod.row_ledger(FakeCtx({SHEET: {"values": ROWS}}), {"url": SHEET, "variant": "notes_only"}))
    assert {d["key"] for d in keep["excluded"]} == {"Email"}
    bad = mod.row_ledger(ctx, {})
    assert bad["is_error"] and bad["output"].startswith("usage:")


def test_the_row_ledger_trigger_fires_on_a_table_and_counts_a_clipped_one():
    import tempfile
    root = tempfile.mkdtemp()
    from pathlib import Path
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    mod = load(lay_out(Path(root), files), "tools/m_tables.py")
    event = {"tool": "api_fetch", "args": {"method": "GET", "url": SHEET}, "result": json.dumps({"values": ROWS}),
             "is_error": False}
    fired = mod.row_ledger_trigger(event, {"task": "t"})
    assert fired["kind"] == "table_read" and fired["context"] == {"noise_rows": 1, "rows": 8}
    clipped = {**event, "result": json.dumps({"values": ROWS * 3})[:300]}   # what a hook sees of a big table
    assert mod.row_ledger_trigger(clipped, {}) is not None
    assert mod.row_ledger_trigger({**event, "args": {"method": "POST", "url": SHEET}}, {}) is None
    assert mod.row_ledger_trigger({**event, "result": json.dumps({"values": ROWS[:3]})}, {}) is None


def test_the_doc_search_fans_out_over_the_stores_and_says_which_were_unreachable(tmp_path):
    root, _ = reference_bundle(tmp_path, ["m_doc_search"])
    mod = load(root, "tools/m_search.py")
    drive = "https://drive.example.invalid/files"
    wiki = "https://wiki.example.invalid/search"
    searches = {"files list": [{"method": "GET", "url": drive}], "pages search": [{"method": "GET", "url": wiki}],
                "messages list": [{"method": "POST", "url": "https://mail.example.invalid/send"}]}
    ctx = FakeCtx({drive: {"files": [{"id": "f1", "name": "Expense guideline 2026"}, {"id": "f2", "name": "Menu"}]}},
                  searches=searches)
    out = json.loads(mod.doc_search(ctx, {"query": "expense guideline"}))
    stores = {s["store"]: s for s in out["stores"]}
    assert [h["record"] for h in stores["files"]["hits"]] == ["f1"]
    unreachable = {u["store"]: u for u in out["unreachable"]}
    assert unreachable["wiki"]["status"] == "not_found" and unreachable["mail"]["reason"] == "no listing endpoint"
    assert all(c[0] == "api_search" or c[1]["method"] == "GET" for c in ctx.calls)     # it only reads
    assert len(ctx.calls) <= 25
    event = {"tool": "api_fetch", "args": {"method": "GET", "url": drive, "params": json.dumps({"q": "policy"})},
             "result": json.dumps({"files": []}), "is_error": False}
    assert mod.doc_search_trigger(event, {"task": "x"}) == {"kind": "doc_miss", "args": {"query": "policy"},
                                                            "context": {}}
    unauthorized = {**event, "result": json.dumps({"error": {"code": 401, "message": "not connected"}}),
                    "is_error": True}
    assert mod.doc_search_trigger(unauthorized, {"task": "x"})["kind"] == "http_401"


def test_the_policy_timeline_orders_versions_and_lists_later_overrides(tmp_path):
    root, _ = reference_bundle(tmp_path, ["m_policy_timeline"])
    mod = load(root, "tools/m_policy.py")
    records = [{"id": "a", "name": "Refund policy", "modifiedTime": "2026-01-10"},
               {"id": "b", "name": "Refund policy v2", "modifiedTime": "2026-05-02"},
               {"id": "c", "name": "Refund policy supplement", "modifiedTime": "2026-06-01"},
               {"id": "d", "name": "Refund policy hold lifted", "modifiedTime": "2026-07-01", "body": "the hold is lifted"}]
    entries, overrides, undated = mod.timeline(records, variant="replace")
    assert [e["status"] for e in entries] == ["superseded", "superseded", "superseded", "current"]
    merged, _, _ = mod.timeline(records, variant="merge_supplement")
    assert [e["status"] for e in merged] == ["superseded", "superseded", "supplement", "current"]
    assert overrides and overrides[0]["says"] == "lifted"
    drive = "https://drive.example.invalid/files"
    ctx = FakeCtx({drive: {"files": records}}, searches={"files list": [{"method": "GET", "url": drive}]})
    out = json.loads(mod.policy_timeline(ctx, {"topic": "refund policy"}))
    assert out["timeline"][-1]["record"] == "d"
    journal = json.loads((root / ".m_journal.json").read_text())
    assert {e["value"] for e in journal["superseded"]} == {"Refund policy", "Refund policy v2",
                                                           "Refund policy supplement"}


def test_the_outbound_lint_checks_a_body_against_the_journal_only(tmp_path):
    root, _ = reference_bundle(tmp_path, ["m_row_ledger", "m_outbound_lint"])
    lint = load(root, "tools/m_verify.py")
    send = {"method": "POST", "url": "https://slack.example.invalid/api/chat.postMessage",
            "body": json.dumps({"channel": "#ops", "text": "Totals: Email 400, sum 3,099.00"})}
    empty = json.loads(lint.outbound_lint(FakeCtx({}), {"tool": "api_fetch", "args": send}))
    assert empty["violations"] == [] and empty["grounded"] is False          # nothing decided, nothing to check
    ledger = load(root, "tools/m_tables.py")
    ledger.row_ledger(FakeCtx({SHEET: {"values": ROWS}}), {"url": SHEET, "amount_columns": ["amount"]})
    out = json.loads(lint.outbound_lint(FakeCtx({}), {"tool": "api_fetch", "args": send}))
    kinds = sorted(v["kind"] for v in out["violations"])
    assert kinds == ["a computed value is missing", "a computed value is missing", "excluded value in the body"]
    fixed = {**send, "body": json.dumps({"channel": "#ops", "text": "6 channels, total 3,200.00"})}
    assert json.loads(lint.outbound_lint(FakeCtx({}), {"tool": "api_fetch", "args": fixed}))["violations"] == []
    assert lint.lint_trigger({"tool": "api_fetch", "args": send, "turn": 3}, {})["kind"] == "outbound_send"
    assert lint.lint_trigger({"tool": "api_fetch", "args": send, "result": "", "turn": 3}, {}) is None


def test_the_effect_audit_reads_back_every_non_message_write(tmp_path):
    root, _ = reference_bundle(tmp_path, ["m_effect_audit"])
    audit = load(root, "tools/m_verify.py")
    mlib = load(root, "tools/_mlib.py")
    rec = "https://crm.example.invalid/contacts/c1"
    mlib.journal_add("writes", [{"target": f"PATCH {rec}", "fields": ["stage"]},
                                {"target": "PATCH https://crm.example.invalid/contacts/c9", "fields": ["stage"]},
                                {"target": "POST https://chat.example.invalid/api/chat.postMessage", "fields": ["text"]}])
    fired = audit.audit_trigger({"tool": None, "final_message": "done"}, {"events": []})
    assert fired["kind"] == "write_no_readback" and fired["context"]["unread"] == 2
    out = json.loads(audit.effect_audit(FakeCtx({rec: {"id": "c1", "stage": "won"}}), {}))
    assert [d["target"] for d in out["discrepancies"]] == ["https://crm.example.invalid/contacts/c9"]
    read = {"events": [{"name": "api_fetch", "args": {"method": "GET", "url": rec}},
                       {"name": "api_fetch", "args": {"method": "GET",
                                                      "url": "https://crm.example.invalid/contacts/c9"}}]}
    assert audit.audit_trigger({"tool": None}, read) is None


def test_the_journal_does_not_survive_an_attempt(tmp_path):
    """§2.2 rule 6: the journal lives in the loaded bundle's own directory, and every attempt is a new one."""
    first, _ = reference_bundle(tmp_path / "a", ["m_row_ledger"])
    load(first, "tools/m_tables.py").row_ledger(FakeCtx({SHEET: {"values": ROWS}}), {"url": SHEET})
    second, _ = reference_bundle(tmp_path / "b", ["m_row_ledger"])
    assert (first / ".m_journal.json").exists()
    assert load(second, "tools/_mlib.py").journal()["excluded"] == []


# ---------------------------------------------------------------------------- the kit
def test_kit_completion_gives_a_method_candidate_the_router_the_table_and_the_hooks(tmp_path):
    files, notes = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    assert notes == []
    assert files[M.ROUTER_PATH] == M.router_source() and M.MLIB_PATH in files
    manifest = json.loads(files["manifest.json"])
    assert [h["name"] for h in manifest["hooks"]][-3:] == ["m_router_after", "m_router_before", "m_router_finish"]
    assert M.table_of(files)["cells"] == {"m_row_ledger|table_read|*": {"action": "recommend",
                                                                        "variant": "notes_and_noise"}}
    hp.check_shape(files, V2)
    assert M.kit_check(files) == []


def test_r_edits_to_the_router_or_table_are_replaced_and_reported():
    parent, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    edited = {**parent, M.ROUTER_PATH: parent[M.ROUTER_PATH] + "\n# always recommend\n",
              M.TABLE_PATH: M.render_table({"mode": "greedy", "cells": {"m_row_ledger|table_read|*":
                                                                        {"action": "silent"}}})}
    files, notes = M.kit_complete(parent, edited)
    assert files[M.ROUTER_PATH] == M.router_source()
    assert M.table_of(files) == M.table_of(parent)              # the parent's cells, not R's
    assert len(notes) == 2 and "replaced" in notes[0] and "fixed code" in notes[1]
    # a learned cell of the parent survives a candidate that adds another method
    learned = {**parent, M.TABLE_PATH: M.render_table({"mode": "greedy", "cells": {
        "m_row_ledger|table_read|*": {"action": "silent", "variant": "notes_only"}}, "splits": {}})}
    grown, _ = M.kit_complete(learned, M.reference_files(["m_row_ledger", "m_doc_search"], base=learned))
    cells = M.table_of(grown)["cells"]
    assert cells["m_row_ledger|table_read|*"] == {"action": "silent", "variant": "notes_only"}
    assert cells["m_doc_search|doc_miss|*"] == {"action": "recommend"}


def test_a_candidate_without_methods_is_untouched_and_one_that_drops_them_loses_the_kit():
    plain = {"manifest.json": json.dumps(hp.empty_manifest()), "memory.md": "x"}
    assert M.kit_complete({}, plain) == (plain, [])
    parent, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    manifest = json.loads(parent["manifest.json"])
    manifest["tools"] = []
    stripped = {p: t for p, t in parent.items() if not p.startswith("tools/m_")}
    stripped["manifest.json"] = json.dumps(manifest)
    files, notes = M.kit_complete(parent, stripped)
    assert M.ROUTER_PATH not in files and M.TABLE_PATH not in files
    assert json.loads(files["manifest.json"])["hooks"] == [] and notes


def test_the_kit_check_refuses_a_table_or_declaration_outside_the_contract():
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    explore = {**files, M.TABLE_PATH: M.render_table({**M.table_of(files), "mode": "explore", "salt": "s"})}
    assert any("explore" in e for e in M.kit_check(explore))
    bad_cell = {**files, M.TABLE_PATH: M.render_table({"mode": "greedy", "cells": {
        "m_row_ledger|other_kind|*": {"action": "inject"}, "m_ghost|x|*": {"action": "silent"}}})}
    errors = M.kit_check(bad_cell)
    assert any("not one of m_row_ledger's kinds" in e for e in errors)
    assert any("neither silent nor" in e for e in errors) and any("no declared method" in e for e in errors)
    module = files["tools/m_tables.py"].replace('"default_variant": "notes_and_noise"}}',
                                                '"default_variant": "notes_and_noise", '
                                                '"variants": ["a", "b", "c", "d", "e"]}}')
    assert module != files["tools/m_tables.py"]
    too_many = {**files, "tools/m_tables.py": module}
    assert any("at most 4" in e for e in M.kit_check(too_many))
    undeclared = {**files, "tools/m_tables.py": files["tools/m_tables.py"].replace("METHODS = ", "OTHER = ")}
    assert any("declares no METHODS entry" in e for e in M.kit_check(undeclared))
    assert any("not the kit's router" in e for e in M.kit_check({**files, M.ROUTER_PATH: "x = 1\n"}))


def test_the_literal_check_covers_method_code_and_the_kit_check_runs_in_the_draft_chain(tmp_path):
    """V0_5_PLAN §1.1: a method module, its METHODS literal and its tests are package files like any other — a value
    of one train task in them is refused — and under a methods K the kit check runs before the worker."""
    from ouroloop.evolve import contracts as ct
    from ouroloop.research.drafts import Drafts
    objects = ct.Objects(tmp_path)
    parent = hp.empty_package(objects)
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    leaky = {**files, "tools/m_tables.py": files["tools/m_tables.py"].replace(
        'NOTE_KEYS = ("note",', 'NOTE_KEYS = ("INV-ZETA-0077", "note",')}
    drafts = Drafts(objects, parent_ref=parent, limits=V2, literals={"INV-ZETA-0077": {"t1"}},
                    worker=ProcessWorker(for_tests=True), methods=M.MethodSettings())
    errors, ref = drafts.check(leaky)
    assert ref is None and "INV-ZETA-0077" in errors[0] and "tools/m_tables.py" in errors[0]
    explore = {**files, M.TABLE_PATH: M.render_table({**M.table_of(files), "mode": "explore", "salt": "s"})}
    errors, ref = drafts.check(explore)
    assert ref is None and errors[0].startswith("kit_check:")
    errors, ref = drafts.check(files)
    assert errors == [] and ref


def test_the_router_is_the_kits_bytes_and_the_table_is_parsed_not_imported(tmp_path):
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    poisoned = {**files, M.TABLE_PATH: "import os\nos.environ['TOUCHED'] = '1'\nTABLE = {'mode': 'greedy', "
                                       "'cells': {}}\n"}
    import os
    os.environ.pop("TOUCHED", None)
    assert M.table_of(poisoned) == {"mode": "greedy", "cells": {}}
    assert "TOUCHED" not in os.environ                                      # read with ast, never executed
    assert M.methods_of(files)["m_row_ledger"]["kinds"] == ["table_read"]


# ---------------------------------------------------------------------------- the method contract in an attempt
class World:
    """A business surface with a sheet to read and records to write."""

    def __init__(self):
        self.calls = []

    def specs(self):
        return [ToolSpec("api_search", "search", {"type": "object", "properties": {"query": {"type": "string"}}}),
                ToolSpec("api_fetch", "fetch", {"type": "object", "properties": {
                    "url": {"type": "string"}, "method": {"type": "string"}, "body": {"type": "string"},
                    "params": {"type": "string"}}})]

    def run(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "api_fetch" and str(args.get("method", "GET")).upper() == "GET" and args.get("url") == SHEET:
            return ToolResult(json.dumps({"values": ROWS}))
        if name == "api_search":
            return ToolResult(json.dumps({"results": [], "count": 0}))
        return ToolResult(json.dumps({"ok": True}))

    def mutates(self, name, args):
        return name == "api_fetch" and str(args.get("method", "GET")).upper() != "GET"

    @property
    def tool_calls(self):
        return len(self.calls)


WRITER = '''METHODS = {"m_writer": {"family": "analysis", "trigger": "never", "kinds": ["table_read"], "on": "recommend",
                        "footprint": ["value"]}}


def never(event, view):
    return None


def run(ctx, args):
    out = []
    for i in range(int(args.get("n") or 1)):
        r = ctx.call_tool("api_fetch", {"method": args.get("method") or "GET", "url": "%s"})
        out.append(r.output[:40])
    return "\\n".join(out)
''' % SHEET


def writer_files():
    files = {"manifest.json": json.dumps({**hp.empty_manifest(hp.AGENT_API_VERSION), "tools": [{
        "name": "m_writer", "description": "a method that tries to write", "entrypoint": "tools/m_writer.py:run",
        "input_schema": {"type": "object", "properties": {"method": {"type": "string"}, "n": {"type": "integer"}}}}]}),
        "tools/m_writer.py": WRITER}
    return M.kit_complete({}, files)[0]


def shell(tmp_path, files, turns, methods):
    from test_package_worker import Scripted, bound_with, shell_agent
    manifest = json.loads(files["manifest.json"])
    bound, objects = bound_with(tmp_path, {p: t for p, t in files.items() if p != "manifest.json"},
                                {k: v for k, v in manifest.items() if k != "api_version"} | {"api_version":
                                                                                            manifest["api_version"]})
    world = World()
    agent = shell_agent(tmp_path, bound, world, Scripted(turns))
    agent.methods = methods
    return agent, world


def transcript_of(tmp_path, agent):
    path = tmp_path / "transcripts" / f"{agent.session}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_an_m_tool_gets_a_read_only_broker_and_a_post_is_refused(tmp_path):
    from ouroloop.report import m_tool_writes
    turns = [ChatTurn("", [ToolCall("c1", "m_writer", {"method": "POST"})], "tool_use"), ChatTurn("done", [], "end")]
    agent, world = shell(tmp_path / "v5", writer_files(), turns, {"prefix": "m_", "max_calls": 25})
    agent.run("Write the record.")
    assert not any(world.mutates(n, a) for n, a in world.calls)               # the write never reached the world
    events = transcript_of(tmp_path / "v5", agent)
    marks = [e for e in events if e["kind"] == "package_tool"]
    assert marks and "may only make read-only calls" in marks[0]["output"]
    # the host's own record says what the worker asked for, and the report's check reads it
    assert [b["read_only"] for b in events if b["kind"] == "broker_event"] == [True]
    assert m_tool_writes(events) == {"calls": 1, "executed": 0, "refused": 1}
    # the same package under a K without methods: the tool writes, as a v0.4 package tool always could
    agent, world = shell(tmp_path / "v4", writer_files(), turns, None)
    agent.run("Write the record.")
    assert any(world.mutates(n, a) for n, a in world.calls)
    events = transcript_of(tmp_path / "v4", agent)
    assert [b["read_only"] for b in events if b["kind"] == "broker_event"] == [False]
    assert m_tool_writes(events) == {"calls": 1, "executed": 1, "refused": 0}


def test_an_m_tool_past_its_call_cap_gets_an_error_not_a_call(tmp_path):
    turns = [ChatTurn("", [ToolCall("c1", "m_writer", {"n": 5})], "tool_use"), ChatTurn("done", [], "end")]
    agent, world = shell(tmp_path, writer_files(), turns, {"prefix": "m_", "max_calls": 3})
    agent.run("Read the sheet five times.")
    assert len([c for c in world.calls if c[0] == "api_fetch"]) == 3
    mark = next(e for e in transcript_of(tmp_path, agent) if e["kind"] == "package_tool")
    assert mark["output"].count("refused: a method makes at most 3") == 2


def test_the_router_decides_once_marks_the_decision_and_recommends_within_its_caps(tmp_path):
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    read = ToolCall("c1", "api_fetch", {"method": "GET", "url": SHEET})
    turns = [ChatTurn("", [read], "tool_use"), ChatTurn("", [ToolCall("c2", "api_fetch", {"method": "GET",
                                                                                          "url": SHEET})], "tool_use"),
             ChatTurn("done", [], "end")]
    agent, world = shell(tmp_path, files, turns, {"prefix": "m_", "max_calls": 25})
    agent.run("Total the budget by channel.")
    events = transcript_of(tmp_path, agent)
    decisions = [e for e in events if e["kind"] == "method_decision"]
    assert len(decisions) == 1 and decisions[0]["source"] == "package"           # decided once, at the first firing
    d = decisions[0]
    assert (d["method"], d["trigger_kind"], d["action"], d["propensity"], d["mode"]) == \
        ("m_row_ledger", "table_read", "recommend", 1.0, "greedy")
    assert d["cell"] == "m_row_ledger|table_read|*" and d["variant"] == "notes_and_noise"
    feedback = [e for e in events if e["kind"] == "hook" and e["hook"] == "m_router_after" and e["action"] == "feedback"]
    assert len(feedback) == 1 and "m_row_ledger may help here" in feedback[0]["message"]   # one recommendation


def test_a_silent_cell_marks_its_decision_and_says_nothing(tmp_path):
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    files[M.TABLE_PATH] = M.render_table({"mode": "greedy", "cells": {"m_row_ledger|table_read|*":
                                                                      {"action": "silent"}}})
    turns = [ChatTurn("", [ToolCall("c1", "api_fetch", {"method": "GET", "url": SHEET})], "tool_use"),
             ChatTurn("done", [], "end")]
    agent, _ = shell(tmp_path, files, turns, {"prefix": "m_", "max_calls": 25})
    agent.run("Total the budget by channel.")
    events = transcript_of(tmp_path, agent)
    assert [e["action"] for e in events if e["kind"] == "method_decision"] == ["silent"]
    assert not [e for e in events if e["kind"] == "hook" and e["action"] == "feedback"]


def test_the_router_blocks_a_body_once_and_lets_the_unchanged_resend_through(tmp_path):
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger", "m_outbound_lint"]))
    files[M.TABLE_PATH] = M.render_table({"mode": "greedy", "cells": {
        "m_row_ledger|table_read|*": {"action": "inject", "variant": "notes_and_noise"},
        "m_outbound_lint|outbound_send|*": {"action": "block_once"},
        "m_outbound_lint|categorical_write|*": {"action": "block_once"}}})
    files["tools/m_tables.py"] = files["tools/m_tables.py"].replace('"on": "recommend"', '"on": "inject"')
    send = ToolCall("c2", "api_fetch", {"method": "POST", "url": "https://chat.example.invalid/api/chat.postMessage",
                                        "body": json.dumps({"text": "Email channel spent 400"})})
    turns = [ChatTurn("", [ToolCall("c1", "api_fetch", {"method": "GET", "url": SHEET})], "tool_use"),
             ChatTurn("", [send], "tool_use"), ChatTurn("", [ToolCall("c3", send.name, send.args)], "tool_use"),
             ChatTurn("done", [], "end")]
    agent, world = shell(tmp_path, files, turns, {"prefix": "m_", "max_calls": 25})
    agent.run("Report the budget to the channel.")
    events = transcript_of(tmp_path, agent)
    blocked = [e for e in events if e["kind"] == "tool_blocked"]
    assert len(blocked) == 1 and "excluded value in the body" in blocked[0]["reason"]
    posts = [a for n, a in world.calls if world.mutates(n, a)]
    assert len(posts) == 1                                     # the unchanged resend went through
    injected = [e for e in events if e["kind"] == "hook" and e["hook"] == "m_router_after" and e["action"] == "feedback"]
    assert injected and "found:" in injected[0]["message"]


def test_a_hook_context_has_mark_only_under_a_methods_k(tmp_path):
    hook = "def run(ctx, event):\n    return {'action': 'feedback', 'message': 'mark:' + str(hasattr(ctx, 'mark'))}\n"
    files = {"manifest.json": json.dumps({**hp.empty_manifest(hp.AGENT_API_VERSION), "hooks": [
        {"name": "probe", "site": "after_tool", "entrypoint": "middlewares/probe.py:run"}]}),
        "middlewares/probe.py": hook}
    turns = [ChatTurn("", [ToolCall("c1", "api_search", {"query": "x"})], "tool_use"), ChatTurn("done", [], "end")]
    seen = {}
    for name, methods in (("v4", None), ("v5", {"prefix": "m_", "max_calls": 25})):
        agent, _ = shell(tmp_path / name, files, turns, methods)
        agent.run("Search.")
        seen[name] = next(e["message"] for e in transcript_of(tmp_path / name, agent) if e["kind"] == "hook")
    assert seen == {"v4": "mark:False", "v5": "mark:True"}


def test_a_package_without_m_tools_runs_and_records_as_before(tmp_path):
    """V0_5_PLAN §6: under a K with methods, a package without a method gets nothing new — no decision, no router,
    and the same calls as without the methods view."""
    files = {"manifest.json": json.dumps(hp.empty_manifest(hp.AGENT_API_VERSION)), "memory.md": "Read first.\n"}
    turns = [ChatTurn("", [ToolCall("c1", "api_fetch", {"method": "GET", "url": SHEET})], "tool_use"),
             ChatTurn("done", [], "end")]
    a5, w5 = shell(tmp_path / "a", {**files, "manifest.json": json.dumps({**hp.empty_manifest(hp.AGENT_API_VERSION),
                                                                          "memory": {"enabled": True}})},
                   turns, {"prefix": "m_", "max_calls": 25})
    a4, w4 = shell(tmp_path / "b", {**files, "manifest.json": json.dumps({**hp.empty_manifest(hp.AGENT_API_VERSION),
                                                                          "memory": {"enabled": True}})}, turns, None)
    assert a5.run("Read the sheet.").text == a4.run("Read the sheet.").text == "done"
    assert w5.calls == w4.calls
    kinds5 = [e["kind"] for e in transcript_of(tmp_path / "a", a5) if e["kind"] not in ("task",)]
    assert "method_decision" not in kinds5 and "library_retrieval" not in kinds5


# ---------------------------------------------------------------------------- the reach check
class TrainWorld(World):
    def close(self):
        pass


def recorded(task, *calls):
    return {"task": task, "events": [{"tool": n, "args": a, "output": o, "is_error": False} for n, a, o in calls],
            "final": "done"}


def sheet_read(rows=ROWS):
    return ("api_fetch", {"method": "GET", "url": SHEET}, json.dumps({"values": rows}))


def test_the_reach_check_replays_recorded_train_events_through_the_trigger_and_runs_the_method_on_a_fresh_train_world():
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    attempts = [recorded("t1", sheet_read()), recorded("t2", sheet_read()), recorded("t3", ("api_search",
                                                                                           {"query": "x"}, "[]"))]
    worlds = []

    def world_of(task):
        worlds.append(task)
        return TrainWorld()
    errors, report = M.reach_check(ProcessWorker(for_tests=True), files, ["m_row_ledger"], attempts=attempts,
                                   predicted=["t1", "t3"], world_of=world_of,
                                   values_of=lambda t: ["Search", "3,200.00", "absent value"],
                                   task_text_of=lambda t: f"task {t}")
    assert errors == []
    r = report["m_row_ledger"]
    assert sorted(r["fired_tasks"]) == ["t1", "t2"] and r["reached"] == 1
    t1 = r["claimed"]["t1"]
    assert t1["fired"] and t1["ran"] and t1["error"] is None and t1["calls"] == 1
    assert t1["assertion_values_in_output"] == 1                    # counted on the host: only the count goes back
    assert r["claimed"]["t3"]["fired"] is False
    assert worlds == ["t1", "t3"]                                   # a fresh train world per claimed task


def test_a_method_that_never_fires_on_its_predicted_tasks_is_refused_with_counts():
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    attempts = [recorded("t1", sheet_read()), recorded("t2", sheet_read()), recorded("t3", sheet_read(ROWS[:2]))]
    errors, report = M.reach_check(ProcessWorker(for_tests=True), files, ["m_row_ledger"], attempts=attempts,
                                   predicted=["t3"], world_of=lambda t: TrainWorld(), values_of=lambda t: [],
                                   task_text_of=str)
    assert len(errors) == 1 and "fired and ran without error on 0" in errors[0] and "t3: fired False" in errors[0]
    assert report["m_row_ledger"]["claimed"]["t3"]["fired"] is False


def test_a_trigger_that_fires_on_one_train_task_only_is_refused():
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    attempts = [recorded("t1", sheet_read()), recorded("t2", sheet_read(ROWS[:2]))]
    errors, _ = M.reach_check(ProcessWorker(for_tests=True), files, ["m_row_ledger"], attempts=attempts,
                              predicted=["t1"], world_of=lambda t: TrainWorld(), values_of=lambda t: [],
                              task_text_of=str)
    assert len(errors) == 1 and "fires on 1 recorded train task" in errors[0]


def test_a_method_that_raises_in_the_reach_check_is_reported_with_its_error():
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    files["tools/m_tables.py"] = files["tools/m_tables.py"].replace("def row_ledger(ctx, args):\n",
                                                                    "def row_ledger(ctx, args):\n    raise "
                                                                    "RuntimeError('broken parser')\n")
    attempts = [recorded("t1", sheet_read()), recorded("t2", sheet_read())]
    errors, report = M.reach_check(ProcessWorker(for_tests=True), files, ["m_row_ledger"], attempts=attempts,
                                   predicted=["t1"], world_of=lambda t: TrainWorld(), values_of=lambda t: [],
                                   task_text_of=str)
    assert "RuntimeError: broken parser" in report["m_row_ledger"]["claimed"]["t1"]["error"]
    assert errors and "error RuntimeError: broken parser" in errors[0]


# ---------------------------------------------------------------------------- the learner
def test_the_explore_twins_flip_every_gate_between_a_and_b_with_exact_propensities():
    router = load_router()
    tasks = [f"task text {i}" for i in range(40)]
    bits_a = [router.assign("s", t, "m_row_ledger", "table_read", "A") for t in tasks]
    bits_b = [router.assign("s", t, "m_row_ledger", "table_read", "B") for t in tasks]
    assert all(a + b == 1 for a, b in zip(bits_a, bits_b))                  # foldover: each task on once, off once
    assert 10 < sum(bits_a) < 30
    assert [M.assign("s", t, "m_row_ledger", "table_read", "A") for t in tasks] == bits_a   # the host mirrors it
    assert [M.assign("s", t, "m_row_ledger", "table_read", "B") for t in tasks] == bits_b
    other = [router.assign("s", t, "m_doc_search", "doc_miss", "A") for t in tasks]
    assert other != bits_a                                                  # decisions are independent
    variants = ["a", "b", "c"]
    for t in tasks[:10]:
        va = router.assign_variant("s", t, "m", "k", variants, "A")
        vb = router.assign_variant("s", t, "m", "k", variants, "B")
        assert va != vb and {va, vb} <= set(variants)
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    twin = M.twin_files(files, salt="s", twin="B")
    assert M.table_of(twin)["mode"] == "explore" and M.table_of(twin)["twin"] == "B"
    assert {p: t for p, t in twin.items() if p != M.TABLE_PATH} == {p: t for p, t in files.items()
                                                                    if p != M.TABLE_PATH}


def load_router():
    spec = importlib.util.spec_from_file_location("router_under_test", M.KIT_DIR / "m_router.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_dev_or_test_reading_refuses_an_explore_table_and_a_propensity_below_one_is_invalid():
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger"]))
    assert M.frozen_table_problems(files) == []
    assert M.frozen_table_problems(M.twin_files(files, salt="s", twin="A"))
    assert M.frozen_table_problems({"manifest.json": "{}"}) == []          # a package without methods
    greedy = [{"kind": "method_decision", "method": "m", "trigger_kind": "k", "propensity": 1.0, "mode": "greedy"}]
    explore = [{"kind": "method_decision", "method": "m", "trigger_kind": "k", "propensity": 0.5, "mode": "explore"}]
    assert M.propensity_problems(greedy) == [] and M.propensity_problems(explore) == ["m|k: propensity 0.5"]


DECLS = {"m_x": {"family": "analysis", "kinds": ["table_read"], "on": "recommend", "footprint": ["value"],
                 "split": "noise_rows"}}


def obs(task, up=True, *, on_success=None, silent_success=0, must=0, noise=0, cost=(1.0, 1.0)):
    on_fp, off_fp = (1.0, 0.5) if up else (0.5, 1.0)
    on_success = (1 if up else 0) if on_success is None else on_success
    return {"task": task, "method": "m_x", "kind": "table_read", "cell": "m_x|table_read|*", "bucket": "*",
            "context": {"noise_rows": noise}, "r_task": (on_success, silent_success), "r_fp": (on_fp, off_fp),
            "must_not": must, "cost": cost}


def test_fit_table_recovers_a_planted_cell_and_keeps_the_default_below_five_tasks():
    silent = {"mode": "greedy", "cells": {"m_x|table_read|*": {"action": "silent"}}}
    table, record = M.fit_table([obs(f"t{i}") for i in range(7)], DECLS, silent)
    assert table["cells"]["m_x|table_read|*"]["action"] == "recommend"
    assert record["m_x|table_read|*"]["p_helps"] >= 0.8 and record["m_x|table_read|*"]["n"] == 7
    table, record = M.fit_table([obs(f"t{i}") for i in range(4)], DECLS, silent)
    assert table["cells"]["m_x|table_read|*"]["action"] == "silent"           # 4 < 5 tasks: the default stays
    harmful, _ = M.fit_table([obs(f"t{i}", up=False) for i in range(7)], DECLS, None)
    assert harmful["cells"]["m_x|table_read|*"]["action"] == "silent"         # turned off from the declared on
    musts, rec = M.fit_table([obs("t1", must=1), obs("t2", must=1)], DECLS, None)
    assert musts["cells"]["m_x|table_read|*"]["action"] == "silent" and "must-not" in rec["m_x|table_read|*"]["why"]
    dear, _ = M.fit_table([obs(f"t{i}", cost=(2.0, 1.0)) for i in range(7)], DECLS, silent)
    assert dear["cells"]["m_x|table_read|*"]["action"] == "silent"            # the cost guard


def test_a_split_needs_five_tasks_on_each_side():
    mixed = [obs(f"lo{i}", up=False, noise=0) for i in range(6)] + [obs(f"hi{i}", up=True, noise=3) for i in range(6)]
    table, record = M.fit_table(mixed, DECLS, None)
    assert table["splits"]["m_x"] == {"feature": "noise_rows", "cut": 3}
    assert table["cells"]["m_x|table_read|noise_rows=0"]["action"] == "silent"
    assert table["cells"]["m_x|table_read|noise_rows=1"]["action"] == "recommend"
    few = [obs(f"lo{i}", up=False, noise=0) for i in range(4)] + [obs(f"hi{i}", up=True, noise=3) for i in range(6)]
    table, _ = M.fit_table(few, DECLS, None)
    assert "m_x" not in table["splits"]
    same = [obs(f"lo{i}", up=True, noise=0) for i in range(6)] + [obs(f"hi{i}", up=True, noise=3) for i in range(6)]
    table, _ = M.fit_table(same, DECLS, None)
    assert "m_x" not in table["splits"]                  # both sides would act alike: no split is kept


def test_other_domains_and_pre_install_counts_enter_at_half_weight():
    silent = {"mode": "greedy", "cells": {"m_x|table_read|*": {"action": "silent"}}}
    weak = [obs(f"t{i}") for i in range(3)] + [obs(f"u{i}", up=False) for i in range(2)]
    table, record = M.fit_table(weak, DECLS, silent)
    assert table["cells"]["m_x|table_read|*"]["action"] == "silent"
    table, record = M.fit_table(weak, DECLS, silent, prior={"m_x|table_read|*": {"k": 8, "m": 0}})
    assert record["m_x|table_read|*"]["alpha"] == 1 + 3 + 4.0 and record["m_x|table_read|*"]["beta"] == 1 + 2
    assert table["cells"]["m_x|table_read|*"]["action"] == "recommend"


def test_observations_pair_the_twins_by_decision_and_skip_a_task_where_neither_fired():
    decls = {"m_x": DECLS["m_x"]}

    def attempt(success, decided, fp_pass):
        return {"success": success, "cost": 0.5, "decisions": decided,
                "assertions": [{"type": "sheet_field_equals", "passed": fp_pass},
                               {"type": "gmail_email_not_sent_to", "passed": True}]}
    on = [{"method": "m_x", "kind": "table_read", "action": "recommend", "cell": "m_x|table_read|*", "bucket": "*"}]
    off = [{"method": "m_x", "kind": "table_read", "action": "silent", "cell": "m_x|table_read|*", "bucket": "*"}]
    pairs = [{"task": "t1", "A": attempt(1, on, True), "B": attempt(0, off, False)},
             {"task": "t2", "A": attempt(1, [], True), "B": attempt(1, [], True)},
             {"task": "t3", "A": attempt(0, [], False), "B": attempt(1, on, True)}]
    seen = M.observations(pairs, decls)
    assert [o["task"] for o in seen] == ["t1", "t3"]
    assert seen[0]["r_task"] == (1, 0) and seen[0]["r_fp"] == (1.0, 0.0)
    assert seen[1]["r_task"] == (1, 0)                       # B acted (on), A never fired: A is the silent side


def test_the_refit_candidate_changes_only_the_table_and_the_enabled_flags():
    files, _ = M.kit_complete({}, M.reference_files(["m_row_ledger", "m_doc_search"]))
    table = M.table_of(files)
    table["cells"] = {k: {**v, "action": "silent"} if k.startswith("m_doc_search") else v
                      for k, v in table["cells"].items()}
    out, disabled = M.refit_files(files, table, call_rates={"m_doc_search": 0.01})
    assert disabled == ["m_doc_search"]
    changed = {p for p in out if out[p] != files.get(p)}
    assert changed == {M.TABLE_PATH, "manifest.json"}
    tools = {t["name"]: t for t in json.loads(out["manifest.json"])["tools"]}
    assert tools["m_doc_search"]["enabled"] is False and tools["m_row_ledger"].get("enabled", True)
    kept, none = M.refit_files(files, table, call_rates={"m_doc_search": 0.2})
    assert none == [] and {p for p in kept if kept[p] != files.get(p)} == {M.TABLE_PATH}
    on = M.all_on_files(out)
    assert {c["action"] for c in M.table_of(on)["cells"].values()} == {"recommend"}


def test_a_playbook_lesson_without_a_valid_ledger_citation_is_dropped():
    ledger = {"sup/d1/m1": {"screen": {"fixes": 3, "sample_regressions": 0}, "gate": {"outcome": "rejected"}}}
    text = ("Verification methods broader than an outbound send regress passing tasks [sup/d1/m1: fixes 3]\n"
            "Ledgers help [sup/d1/m1: fixes 5]\n"
            "No citation here\n"
            "Ghost [sup/d9/x: fixes 1]\n"
            "Gate said no [sup/d1/m1: outcome rejected]")
    kept, dropped = M.playbook_check(text, ledger)
    assert kept.splitlines() == ["Verification methods broader than an outbound send regress passing tasks "
                                 "[sup/d1/m1: fixes 3]", "Gate said no [sup/d1/m1: outcome rejected]"]
    assert len(dropped) == 3 and any("not '5'" in d for d in dropped)


def test_assertion_classes_cover_the_five_footprints():
    assert M.assertion_class("gmail_email_not_sent_to") == "not_contains"
    assert M.assertion_class("gmail_message_body_contains") == "body_contains"
    assert M.assertion_class("freshdesk_ticket_has_tag") == "categorical"
    assert M.assertion_class("calendly_event_field_equals") == "value"
    assert M.assertion_class("airtable_record_exists") == "exists"
    from ouroloop.adapters import automationbench as ab
    assert ab.assertion_class("docusign_envelope_status_equals") == "categorical"
