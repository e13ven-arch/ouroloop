"""v0.6 first-version contracts and the capability package (docs/modules/00_CONTRACTS.md §1–2,
docs/modules/01_POLICY_RUNTIME.md §1): CT6-01, CT6-14, PR6-13 (text part), PR6-14, and the package shape."""
import json
from pathlib import Path
from dataclasses import fields

import pytest

from ouroloop import policies as P
from ouroloop.agent import harness_package as hp
from ouroloop.benchmark import Budget, RunManifest
from ouroloop.chat import ChatTurn
from ouroloop.evolve import contracts as ct

SKILL = "notify-after-update"
NOTE = "After you change a record, re-read the request and name the tool result that shows it is done."


def text_files(memory: str = "", skill: str = "") -> dict:
    manifest = hp.empty_manifest()
    files = {}
    if memory:
        files["memory.md"] = memory
        manifest["memory"] = {"enabled": True}
    if skill:
        files[f"skills/{SKILL}.md"] = skill
        manifest["skills"] = [{"name": SKILL, "path": f"skills/{SKILL}.md", "description": "check before finishing"}]
    files["manifest.json"] = json.dumps(manifest)
    return files


def code_files() -> dict:
    manifest = hp.empty_manifest()
    manifest["hooks"] = [{"name": "finish_guard", "site": "on_finish", "entrypoint": "middlewares/guard.py:run"}]
    return {"manifest.json": json.dumps(manifest), "middlewares/guard.py": "def run(ctx, event):\n    return {}\n"}


# ---------------------------------------------------------------------------- CT6-01 / CT6-14
V06_RECORD_FIELDS = {ct.SystemRelease: ("target_harness_ref", "required_capabilities"),
                     ct.ExperimentProposal: ("origin",)}


def test_ct6_14_every_new_field_on_an_existing_record_defaults_to_none():
    for cls, names in V06_RECORD_FIELDS.items():
        defaults = {f.name: f.default for f in fields(cls)}
        for name in names:
            assert defaults[name] is None, f"{cls.__name__}.{name} must default to None"
    rr = {f.name: f.default for f in fields(ct.RoundResult)}
    assert rr["next_action"] is None and rr["action_ref"] is None


def test_ct6_01_a_v2_release_keeps_its_ref_and_payload(tmp_path):
    objects = ct.Objects(tmp_path)
    releases, rel, objects = __import__("tests.rsi", fromlist=["workspace"]).workspace(tmp_path)
    release = objects.load(rel, ct.SystemRelease)
    payload = release.payload()
    assert "target_harness_ref" not in payload and "required_capabilities" not in payload
    assert release.ref() == rel


def test_ct6_01_an_old_reader_refuses_a_record_with_new_fields_and_unknown_versions_are_refused(tmp_path):
    objects = ct.Objects(tmp_path)
    exp = {"id": "x1", "proposal_ref": ct.ref_for("proposal", {"a": 1}), "parent_release": ct.ref_for("release", {}),
           "experience_ref": "", "core_ref": ct.ref_for("core", {}), "eval_template_ref": "", "budget_scope": "s",
           "schema_version": 2}
    ct.Experiment.from_json(exp)
    with pytest.raises(ct.ContractError, match="does not take"):
        ct.Experiment.from_json({**exp, "purpose": "deployment"})        # a field this reader does not know
    with pytest.raises(ct.ContractError, match="schema_version"):
        ct.Experiment.from_json({**exp, "schema_version": 3})


def test_ct6_14_runmanifest_hash_is_unchanged_and_unknown_keys_are_refused():
    m = RunManifest("base", "base", "dev", {"provider": "p", "model": "m"}, budget=Budget(max_steps=3))
    before = m.hash()
    assert all(k not in m.payload() for k in ("mode", "actor_identity_ref", "target_harness_ref", "package_ref"))
    assert RunManifest.from_json(m.to_json()).hash() == before
    with pytest.raises(ValueError, match="does not take"):
        RunManifest.from_json({**m.to_json(), "surprise": 1})
    m.package_ref = ct.ref_for("package", {"x": 1})
    assert m.hash() != before and m.payload()["package_ref"] == m.package_ref


# ---------------------------------------------------------------------------- package shape
@pytest.mark.parametrize("path", ["/abs.md", "../x.md", "skills/../memory.md", "notes.txt", "tools/x.js",
                                  "skills/Upper.md", "skills/a//b.md"])
def test_paths_outside_the_fixed_shape_are_structure_errors(path):
    with pytest.raises(hp.PackageError):
        hp.classify(path)


def test_manifest_entries_must_point_inside_the_package_and_names_are_unique():
    m = hp.empty_manifest()
    m["skills"] = [{"name": "a", "path": "skills/a.md"}, {"name": "a", "path": "skills/a.md"}]
    with pytest.raises(hp.PackageError, match="used twice"):
        hp.check_shape({"manifest.json": json.dumps(m), "skills/a.md": "x"}, hp.Limits())
    m["skills"] = [{"name": "a", "path": "skills/missing.md"}]
    with pytest.raises(hp.PackageError, match="not a skills"):
        hp.check_shape({"manifest.json": json.dumps(m)}, hp.Limits())
    with pytest.raises(hp.PackageError, match="not listed"):
        hp.check_shape({**text_files(), "skills/orphan.md": "x"}, hp.Limits())


def test_component_kinds_outside_k_and_tests_without_code_kinds_are_refused():
    with pytest.raises(hp.PackageError, match="not allowed by this core"):
        hp.check_shape(code_files(), hp.Limits())                     # the text-only preset of stage A
    hp.check_shape(code_files(), hp.Limits(components=(*hp.TEXT_KINDS, "on_finish")))
    with pytest.raises(hp.PackageError, match="tests"):
        hp.check_shape({**text_files(memory=NOTE), "tests/test_x.py": "def test(): pass\n"}, hp.Limits())


def test_limits_bound_files_bytes_and_text_lengths():
    with pytest.raises(hp.PackageError, match="characters"):
        hp.check_shape(text_files(memory="x" * 9000), hp.Limits())
    with pytest.raises(hp.PackageError, match="bytes"):
        hp.check_shape(text_files(memory="x" * 100), hp.Limits(max_package_bytes=50))


def tool_files(**entry) -> dict:
    """A package with one tool, its entry overridden by `entry`."""
    manifest = hp.empty_manifest()
    manifest["tools"] = [{"name": "lookup_contact", "description": "find a contact by email",
                          "input_schema": {"type": "object", "properties": {"email": {"type": "string"}}},
                          "entrypoint": "tools/t.py:run", **entry}]
    return {"manifest.json": json.dumps(manifest), "tools/t.py": "def run(ctx, args):\n    return ''\n"}


CODE = hp.Limits(components=hp.KINDS)


@pytest.mark.parametrize("name", ["crm.lookup", "lookup contact", "x" * 65, "查找联系人", "ok\n"])
def test_a_tool_name_the_provider_refuses_fails_the_check(tmp_path, name):
    """A name the model providers refuse is a 400 on every call of every attempt, which a run can only book as
    infrastructure: the draft check refuses it as a structure error that goes back to R, before any worker."""
    from ouroloop.research.drafts import Drafts
    with pytest.raises(hp.PackageError, match="letters, digits"):
        hp.check_shape(tool_files(name=name), CODE)
    for fine in ("x" * 64, "Lookup-contact_2"):
        hp.check_shape(tool_files(name=fine), CODE)
    objects = ct.Objects(tmp_path)
    drafts = Drafts(objects, parent_ref=hp.empty_package(objects), limits=CODE, worker=None)
    errors, ref = drafts.check(tool_files(name=name))          # no WorkerUnavailable: refused before the worker
    assert ref is None and "letters, digits" in errors[0]


@pytest.mark.parametrize("schema,message", [
    ({"type": "object", "properties": {"ids": {"type": "array"}}}, "gives no items"),
    ({"type": "object", "properties": [{"name": "x"}]}, "object of schemas"),
    ({"type": "object", "required": "email"}, "list of property names"),
    ({"type": "object", "properties": {"x": {"type": "text"}}}, "type 'text'"),
    ({"type": "object", "anyOf": [{"type": "object"}]}, "anyOf/oneOf/allOf"),
    ({"type": "object", "properties": {"x": json.loads('{"type": "object", "properties": {"x": ' * 16
                                                       + '{"type": "string"}' + '}}' * 16)}}, "nests deeper")])
def test_a_tool_schema_the_provider_refuses_fails_the_check(schema, message):
    with pytest.raises(hp.PackageError, match=message):
        hp.check_shape(tool_files(input_schema=schema), CODE)
    hp.check_shape(tool_files(input_schema={
        "type": "object", "required": ["ids"],
        "properties": {"ids": {"type": "array", "items": {"type": "string"}},
                       "note": {"type": ["string", "null"]}, "mode": {"enum": ["a", "b"]}}}), CODE)
    with pytest.raises(hp.PackageError, match="1024"):
        hp.check_shape(tool_files(description="x" * 1025), CODE)


def test_a_skill_name_with_a_trailing_newline_is_refused():
    manifest = {**hp.empty_manifest(), "skills": [{"name": "ok\n", "path": "skills/ok.md"}]}
    with pytest.raises(hp.PackageError, match="skill name"):
        hp.check_shape({"manifest.json": json.dumps(manifest), "skills/ok.md": "Check."}, CODE)
    manifest["skills"][0]["name"] = "ok"
    hp.check_shape({"manifest.json": json.dumps(manifest), "skills/ok.md": "Check."}, CODE)


def test_enabled_is_a_boolean_and_a_disabled_tool_is_not_offered(tmp_path):
    from ouroloop.chat import ToolSpec
    with pytest.raises(hp.PackageError, match="enabled is true or false"):
        hp.check_shape(tool_files(enabled="false"), CODE)
    manifest = json.loads(code_files()["manifest.json"])
    manifest["hooks"][0]["enabled"] = "false"
    with pytest.raises(hp.PackageError, match="enabled is true or false"):
        hp.check_shape({**code_files(), "manifest.json": json.dumps(manifest)}, CODE)
    with pytest.raises(hp.PackageError, match="memory is"):
        hp.check_shape({**text_files(memory=NOTE), "manifest.json": json.dumps(
            {**hp.empty_manifest(), "memory": {"enabled": "yes"}})}, CODE)
    files = tool_files()
    manifest = json.loads(files["manifest.json"])
    manifest["tools"].append({**manifest["tools"][0], "name": "retired_tool", "enabled": False})
    objects = ct.Objects(tmp_path)
    ref = hp.build_package(objects, {**files, "manifest.json": json.dumps(manifest)}, CODE)

    class Surface:
        def specs(self):
            return [ToolSpec("api_search", "search", {"type": "object"})]
    offered = set(hp.PackageTools(Surface(), hp.load_package(objects, ref)).tools)
    assert offered == {"lookup_contact"}


# ---------------------------------------------------------------------------- the two worker predicates (PR6-14)
def test_check_and_runtime_predicates_differ_on_tests_and_follow_inherited_code(tmp_path):
    objects = ct.Objects(tmp_path)
    limits = hp.Limits(components=hp.KINDS)
    text = hp.build_package(objects, text_files(memory=NOTE), limits)
    view = hp.load_package(objects, text)
    assert not hp.needs_worker_for_checks({**view.run_files, **view.test_files}, view.manifest)
    assert not view.runs_code
    only_tests = hp.build_package(objects, {**text_files(memory=NOTE), "tests/test_x.py": "def test(): pass\n"},
                                  limits)
    v = hp.load_package(objects, only_tests)
    assert hp.needs_worker_for_checks({**v.run_files, **v.test_files}, v.manifest)       # the check needs it
    assert not v.runs_code                                                               # running does not
    coded = hp.build_package(objects, code_files(), limits)
    manifest = json.loads(code_files()["manifest.json"])
    manifest["memory"] = {"enabled": True}
    draft = hp.apply_patch(objects, coded, {"write": {"memory.md": NOTE,                 # a text-only patch
                                                      "manifest.json": json.dumps(manifest)}})
    manifest = hp.check_shape(draft, limits)
    assert hp.needs_worker_for_checks(draft, manifest)                                  # inherited code counts


def test_patch_application_is_checked_and_complete(tmp_path):
    objects = ct.Objects(tmp_path)
    base = hp.build_package(objects, text_files(memory=NOTE), hp.Limits())
    files = hp.apply_patch(objects, base, {"write": {"memory.md": "new"}})
    assert files["memory.md"] == "new" and "manifest.json" in files
    with pytest.raises(hp.PackageError):
        hp.apply_patch(objects, base, {"delete": ["skills/none.md"]})
    with pytest.raises(hp.PackageError):
        hp.apply_patch(objects, base, {"rename": {}})


def test_literal_check_finds_task_specific_values():
    literals = hp.literals_of({"to": "dana.reyes@example.com", "list_id": "L2024", "stage": "open"})
    assert "dana.reyes@example.com" in literals and "L2024" in literals and "open" not in literals
    files = text_files(memory="Always e-mail dana.reyes@example.com after updates.")
    assert hp.literal_hits(files, literals) == ["dana.reyes@example.com"]
    assert hp.literal_hits(text_files(memory=NOTE), literals) == []


# ---------------------------------------------------------------------------- binding (PR6-13 text part)
def v06_workspace(tmp_path, files=None, limits=None):
    from tests import rsi
    objects = ct.Objects(tmp_path)
    package = hp.build_package(objects, files or text_files(memory=NOTE), limits or hp.Limits())
    worker = P.default_worker_set(objects, finish_check=rsi.W0_FINISH, package_ref=package)
    targets = (*P.targets_of("worker"), "worker.harness", *P.targets_of("research"))
    releases, rel, objects = rsi.workspace(tmp_path, worker_ref=worker, targets=targets)
    return releases, rel, objects, package


def test_a_v2_core_and_set_still_bind_without_the_new_targets(tmp_path):
    from tests import rsi
    releases, rel, objects = rsi.workspace(tmp_path)
    bound = P.bind_release(rel, profile="task", services=P.Services(objects))
    assert bound.package is None and bound.actor_identity is None


def test_the_package_text_reaches_the_prompt_and_is_recorded(tmp_path):
    releases, rel, objects, package = v06_workspace(tmp_path, text_files(memory=NOTE, skill=NOTE))
    bound = P.bind_release(rel, profile="task", services=P.Services(objects))
    prompt, parts = bound.system_prompt("base")
    assert NOTE in prompt and f"- {SKILL}: check before finishing" in prompt
    assert parts["parts"][-1] == "worker.harness" and parts["package_ref"] == package


def test_read_skill_is_a_host_read_and_is_recorded(tmp_path):
    releases, rel, objects, package = v06_workspace(tmp_path, text_files(skill=NOTE))
    bound = P.bind_release(rel, profile="task", services=P.Services(objects))

    class Business:
        def specs(self):
            return []

        def run(self, name, args):
            raise AssertionError("read_skill must not reach the business world")

        def mutates(self, name, args):
            return True

    reads = []
    surface = hp.mount(Business(), bound, on_read=lambda n, found: reads.append((n, found)))
    assert [s.name for s in surface.specs()] == ["read_skill"]
    assert surface.run("read_skill", {"name": SKILL}).output == NOTE
    assert surface.run("read_skill", {"name": "other"}).is_error
    assert reads == [(SKILL, True), ("other", False)] and not surface.mutates("read_skill", {})


def test_a_package_that_runs_code_is_refused_without_a_worker(tmp_path):
    limits = hp.Limits(components=hp.KINDS)
    releases, rel, objects, package = v06_workspace(tmp_path, code_files(), limits)
    bound = P.bind_release(rel, profile="task", services=P.Services(objects))
    with pytest.raises(hp.CapabilityError, match="runs code"):
        hp.mount(object(), bound)


def test_the_scripted_worker_follows_a_skill_it_reads(tmp_path):
    """IT6-22 (text part) in miniature: the same request, and a package skill changes what gets done — under the
    runner shell, the default agent offering read_skill beside the base tools."""
    from types import SimpleNamespace
    from ouroloop.benchmark import Budget
    from ouroloop.evolve.task_eval import Budgeted, make_agent_factory
    from tests import rsi
    releases, rel, objects, package = v06_workspace(tmp_path, text_files(skill=rsi.FINISH_CHECK_MARK))
    bound = P.bind_release(rel, profile="task", services=P.Services(objects))
    calls = []

    class World:
        tool_calls = 0

        def specs(self):
            from ouroloop.chat import ToolSpec
            return [ToolSpec("api_fetch", "fetch", {"type": "object", "properties": {}})]

        def run(self, name, args):
            from ouroloop.agent.tools import ToolResult
            calls.append(args.get("url"))
            return ToolResult("{}")

        def mutates(self, name, args):
            return True

    budget = Budget(max_steps=6, max_tool_calls=12)
    make = make_agent_factory(rsi.ws(tmp_path), rsi.BusinessWorker(), budget=budget, bound=bound, max_effort="high")
    agent = make(Budgeted(World(), budget), SimpleNamespace(task_id="t1"), "You are a business agent.")
    agent.run("Move Dana Reyes to opportunity stage, then post to #ops with the phrase deal moved.")
    assert calls == [rsi.HUBSPOT, rsi.SLACK]


def test_code_files_under_a_text_only_core_are_structure_errors_even_without_a_hook_entry():
    files = {**text_files(memory=NOTE), "middlewares/guard.py": "def run(ctx, e):\n    return {}\n"}
    with pytest.raises(hp.PackageError, match="code files"):
        hp.check_shape(files, hp.Limits())


def test_memory_without_the_manifest_enabling_it_is_refused():
    files = {"manifest.json": json.dumps(hp.empty_manifest()), "memory.md": NOTE}
    with pytest.raises(hp.PackageError, match="does not enable memory"):
        hp.check_shape(files, hp.Limits())


@pytest.mark.parametrize("patch", [{}, {"write": [{"memory.md": "x"}]}, {"delete": 5}, {"write": {}},
                                   {"write": {"memory.md": 3}}])
def test_malformed_or_empty_patches_are_structure_errors(tmp_path, patch):
    objects = ct.Objects(tmp_path)
    base = hp.build_package(objects, text_files(memory=NOTE), hp.Limits())
    with pytest.raises(hp.PackageError):
        hp.apply_patch(objects, base, patch)


def test_a_patch_that_changes_nothing_is_refused(tmp_path):
    objects = ct.Objects(tmp_path)
    base = hp.build_package(objects, text_files(memory=NOTE), hp.Limits())
    with pytest.raises(hp.PackageError, match="exactly as it was"):
        hp.apply_patch(objects, base, {"write": {"memory.md": NOTE}})


def test_ordinary_words_are_not_literals_and_hits_match_whole_tokens():
    literals = hp.literals_of({"channel_name": "general", "subject_contains": "update",
                               "to": "jane.doe@acme.com", "record": "Meridian Corp - Platform Deal",
                               "id": "INV-20240042"})
    assert literals == {"jane.doe@acme.com", "Meridian Corp - Platform Deal", "INV-20240042"}
    assert hp.literal_hits({"memory.md": "Check every update, in general."}, literals) == []
    name = "Jos\u00e9 Garc\u00eda"
    escaped = {"manifest.json": json.dumps({**hp.empty_manifest(), "skills": [
        {"name": "x", "path": "skills/x.md", "description": f"always write to {name}"}]}),
        "skills/x.md": "general rule"}
    assert name not in escaped["manifest.json"]                             # escaped in the raw file
    assert hp.literal_hits(escaped, {name}) == [name]                       # found in the decoded text
    found = hp.literal_findings(escaped, {name})
    assert found == [{"file": "manifest.json", "line": None, "literal": name, "kind": "record name"}]


# ---------------------------------------------------------------------------- v0.4: the literal check's precision
def test_the_check_output_names_the_file_the_line_and_the_literal():
    """V0_4_PLAN §1.5, M0 §3 (v0.4): a finding is located — file, line, the literal and its kind — so R revises
    the line it wrote; v0.3 gave counts by kind and 14 of R's 25 turns went to blind revisions. The literal is train
    data R already reads; the output only goes back to R."""
    files = {"manifest.json": json.dumps(hp.empty_manifest()), "memory.md": "First line.\nWrite to Dana Reyes.\n",
             "skills/L2024.md": "x"}
    found = hp.literal_findings(files, {"Dana Reyes", "L2024"})
    assert {"file": "memory.md", "line": 2, "literal": "Dana Reyes", "kind": "record name"} in found
    assert {"file": "skills/L2024.md", "line": 0, "literal": "L2024", "kind": "identifier"} in found
    text = hp.describe_hits(found)
    assert "memory.md:2: 'Dana Reyes' (record name)" in text and "skills/L2024.md:0" in text


def test_matching_is_case_sensitive_except_for_an_e_mail_address():
    """V0_4_PLAN §1.5: 'not sent' in a hook's regex is not the sheet value 'Not Sent' (the finishgate refusals of
    v0.3); an e-mail address is the same address in any case, so it is still found."""
    hook = {"middlewares/gate.py": 'DEFER = re.compile(r"(not sent|did not send)")\n'}
    assert hp.literal_hits(hook, {"Not Sent"}) == []
    assert hp.literal_hits({"memory.md": "Status Not Sent means wait."}, {"Not Sent"}) == ["Not Sent"]
    assert hp.literal_hits({"memory.md": "write to Dana.Reyes@Example.com"}, {"dana.reyes@example.com"}) == \
        ["dana.reyes@example.com"]


def test_only_a_value_of_one_train_task_is_forbidden():
    """V0_4_PLAN §1.5: the index keeps each literal's train tasks; a value of two or more tasks is the domain's
    vocabulary, a pure number a constant as often as a value ('3000' of `[:3000]`), a field name or a column header
    the schema — none is forbidden; a value of exactly one task still is."""
    index: dict = {}
    hp.add_literals(index, "t1", {"rows": [{"cells": {"Rule ID": "R-17", "Rule": "Legal Hold"}}],
                                  "headers": ["Rule ID", "Exclusion Rule"], "limit": "3000", "owner": "Dana Reyes"})
    hp.add_literals(index, "t2", {"note": "Legal Hold", "limit": "3000"})
    headers = hp.header_names({"rows": [{"cells": {"Rule ID": "R-17"}}], "headers": ["Rule ID", "Exclusion Rule"]})
    assert headers >= {"Rule ID", "Exclusion Rule", "cells", "rows"}
    forbidden = hp.forbidden_literals(index)
    assert "Legal Hold" not in forbidden                     # in two tasks
    assert "3000" not in forbidden                           # a pure number
    assert {"R-17", "Dana Reyes"} <= forbidden               # one task's values
    package = {"memory.md": "Rule ID and Exclusion Rule columns; Legal Hold rows; cut at 3000; Dana Reyes."}
    assert hp.literal_hits(package, forbidden, allowed=headers) == ["Dana Reyes"]
    assert hp.forbidden_literals({"Dana Reyes"}) == {"Dana Reyes"}       # a v0.3 set is forbidden whole


def test_a_single_task_amount_the_grader_checks_stays_forbidden(tmp_path):
    """V0_4_PLAN §1.5 (review 2026-10-06): the number exemption covers constants, not answers — a number that is a
    train assertion's value in one task ('13,650', a finance amount) stays forbidden, in the index rule and through
    Drafts; a number only in one task's state or in code ('3000' of `[:3000]`) is exempt, and an amount shared by
    two tasks is vocabulary like any shared value."""
    from ouroloop.research.drafts import Drafts
    index: dict = {}
    hp.add_literals(index, "fin.a", [{"type": "sheet_cell_equals", "value": "13,650"}, {"total": "13,162.5"}])
    answers = set(index)                                        # the assertion params' literals, before the states
    hp.add_literals(index, "fin.a", {"limit": "3000", "rows": [{"cells": {"Amount": "13,650"}}]})
    hp.add_literals(index, "fin.b", {"total": "13,162.5"})
    forbidden = hp.forbidden_literals(index, answers=answers)
    assert "13,650" in forbidden                                 # one task's checked amount
    assert "3000" not in forbidden                               # a number of one task's state, not an answer
    assert "13,162.5" not in forbidden                           # in two tasks
    assert "13,650" not in hp.forbidden_literals(index)          # without the answers, the exemption let it through
    objects = ct.Objects(tmp_path)
    drafts = Drafts(objects, parent_ref=hp.empty_package(objects), limits=hp.Limits(), literals=index,
                    answers=answers)
    errors, ref = drafts.check(text_files(memory="When the books close, the total is 13,650; cut text at 3000."))
    assert ref is None and "memory.md:1: '13,650'" in errors[0]
    assert drafts.check(text_files(memory="Cut long text at 3000 characters."))[0] == []


def test_drafts_built_from_an_index_refuse_only_single_task_values(tmp_path):
    """V0_4_PLAN §1.5, through the draft check itself (research.drafts.Drafts): an index refuses a planted value of
    one task with its location, and passes the shared value, the number and the header the v0.3 set refused."""
    from ouroloop.research.drafts import Drafts
    objects = ct.Objects(tmp_path)
    index = {"Legal Hold": {"t1", "t2"}, "3000": {"t1"}, "Dana Reyes": {"t1"}, "Exclusion Rule": {"t3"}}
    drafts = Drafts(objects, parent_ref=hp.empty_package(objects), limits=hp.Limits(), literals=index,
                    allowed_literals={"Exclusion Rule"})
    clean = text_files(memory="Legal Hold rows are kept; cut at 3000; the Exclusion Rule column decides.")
    assert drafts.check(clean)[0] == []
    errors, ref = drafts.check(text_files(memory="Legal Hold rows.\nAsk Dana Reyes first."))
    assert ref is None and "memory.md:2: 'Dana Reyes'" in errors[0]
    old = Drafts(objects, parent_ref=hp.empty_package(objects), limits=hp.Limits(), literals=set(index))
    assert old.check(clean)[0]                                # the v0.3 form refuses them


def test_the_twenty_recorded_v03_draft_checks_replay_to_no_false_refusal():
    """V0_4_PLAN §1.5's regression (examples/automationbench/literal_replay.py, read-only over workspace-claude-v3):
    the v0.3 rule reproduces the 14 refusals the activity recorded among its 20 draft checks; the v0.4 rule refuses
    none of them; a value of exactly one train task, planted in its own case into the first refused draft, is still
    refused. Needs the v0.3 workspace and the upstream checkout, which are not in the repository. Run as its own
    process: the upstream package is imported from the real checkout there, never the test fixture's copy."""
    import subprocess
    import sys
    script = Path(__file__).resolve().parent.parent / "examples" / "automationbench" / "literal_replay.py"
    workspace = script.parent / "workspace-claude-v3"
    if not (workspace / "rsi" / "events.jsonl").exists() or not Path("~/workspace/AutomationBench").expanduser().exists():
        pytest.skip("workspace-claude-v3 or the upstream checkout is not here")
    done = subprocess.run([sys.executable, str(script), "--json"], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["checks"] == 20 and out["recorded_refusals"] == 14
    assert out["old_rule_refusals"] == 14 and out["old_rule_reproduces_record"]
    assert out["new_rule_refusals"] == 0
    assert out["planted"]["refused"] and out["planted"]["tasks"] == 1
    # review 2026-10-06: a numeric answer of one train task, planted the same way, is refused too
    assert out["planted_amount"]["refused"] and out["planted_amount"]["assertion_value"]
    assert out["planted_amount"]["tasks"] == 1 and out["numbers"]["forbidden"] == out["numbers"]["of_one_task"] > 0


# ---------------------------------------------------------------------------- package API v2: the agent component
AGENT = "def run(ctx, task):\n    return 'done'\n"
V2 = hp.Limits(components=hp.KINDS, package_api="harness_package.v2")


def agent_files(entry: str = "agent/loop.py:run", roles=("act",), **extra) -> dict:
    """A v2 package with one agent, its entry overridden by the arguments."""
    manifest = hp.empty_manifest("harness_package.v2")
    manifest["agent"] = {"entrypoint": entry, "roles": list(roles), **extra}
    return {"manifest.json": json.dumps(manifest), "agent/loop.py": AGENT}


def test_a_v2_manifest_with_an_agent_entry_parses_and_the_package_runs_in_the_worker(tmp_path):
    """docs/modules/00_CONTRACTS.md §2 (包 API v2): the entry is parsed with its roles, `agent` is a component kind,
    and both worker predicates hold for a package with a loop of its own."""
    files = agent_files(roles=("aux", "act"))
    manifest = hp.check_shape(files, V2)
    assert manifest["api_version"] == "harness_package.v2"
    assert manifest["agent"] == {"entrypoint": "agent/loop.py:run", "roles": ["act", "aux"]}
    assert "agent" in hp.kinds_of(manifest, files)
    assert hp.needs_worker_for_checks(files, manifest) and hp.needs_worker_at_runtime(files, manifest)
    objects = ct.Objects(tmp_path)
    view = hp.load_package(objects, hp.build_package(objects, files, V2))
    assert view.api_version == "harness_package.v2" and view.roles == ("act", "aux") and view.runs_code
    assert view.agent["entrypoint"] == "agent/loop.py:run"
    assert objects.get(objects.load(view.ref, ct.HarnessPackage).package_api_ref)["package_api"] == "harness_package.v2"


def test_a_v1_package_parses_unchanged_and_its_api_ref_differs_from_a_v2_one(tmp_path):
    """docs/modules/00_CONTRACTS.md §2: a v1 package is a v2 package the default agent runs — it is stored as before,
    with no agent key — and `package_api_ref` records the package's own version, so the two are different packages."""
    objects = ct.Objects(tmp_path)
    v1 = hp.build_package(objects, text_files(memory=NOTE), hp.Limits())
    view = hp.load_package(objects, v1)
    assert view.api_version == "harness_package.v1" and view.agent is None and view.roles == ("act",)
    assert "agent" not in view.manifest and not view.runs_code
    assert hp.load_package(objects, hp.empty_package(objects)).manifest["api_version"] == "harness_package.v1"
    files = text_files(memory=NOTE)
    files["manifest.json"] = json.dumps({**json.loads(files["manifest.json"]), "api_version": "harness_package.v2"})
    v2 = hp.build_package(objects, files, hp.Limits(package_api="harness_package.v2"))
    p1, p2 = objects.load(v1, ct.HarnessPackage), objects.load(v2, ct.HarnessPackage)
    assert v1 != v2 and p1.package_api_ref != p2.package_api_ref and p1.bundle_ref != p2.bundle_ref
    assert objects.get(p1.package_api_ref)["package_api"] == "harness_package.v1"
    assert objects.get(p2.package_api_ref)["package_api"] == "harness_package.v2"
    assert not hp.load_package(objects, v2).runs_code                 # a v2 package without an agent: default agent


@pytest.mark.parametrize("files, message", [
    (agent_files(entry="agent/loop.py:main"), "named run"),
    (agent_files(entry="tools/loop.py:run"), "inside the package"),
    (agent_files(entry="agent/missing.py:run"), "inside the package"),
    (agent_files(roles=("act", "judge")), "unknown"),
    (agent_files(roles=()), "non-empty"),
    (agent_files(roles=("act", "act")), "twice"),
    (agent_files(effort="max"), "it has \\['effort', 'entrypoint', 'roles'\\]"),
    ({**agent_files(), "manifest.json": json.dumps({**hp.empty_manifest("harness_package.v2"),
                                                     "agent": [{"entrypoint": "agent/loop.py:run", "roles": ["act"]},
                                                               {"entrypoint": "agent/loop.py:run", "roles": ["act"]}]})},
     "exactly one agent"),
    ({**agent_files(), "manifest.json": json.dumps(hp.empty_manifest("harness_package.v2"))}, "no agent entry"),
    ({**agent_files(), "manifest.json": json.dumps({**json.loads(agent_files()["manifest.json"]),
                                                     "api_version": "harness_package.v1"})}, "needs harness_package.v2"),
    ({**agent_files(), "manifest.json": json.dumps({**json.loads(agent_files()["manifest.json"]),
                                                     "api_version": "harness_package.v9"})}, "this host serves"),
])
def test_a_malformed_agent_entry_is_a_structure_error(files, message):
    """docs/modules/00_CONTRACTS.md §2 (包 API v2): one entry, `agent/<file>.py:run`, known roles, no stray agent file,
    and an agent only under v2; each refused with the rule it broke, as check output R can act on."""
    with pytest.raises(hp.PackageError, match=message):
        hp.check_shape(files, V2)


def test_an_agent_under_a_core_that_does_not_allow_it_is_refused_by_the_rule_it_breaks():
    """docs/modules/00_CONTRACTS.md §2: a text-only core refuses the code, a code core without the `agent` kind
    refuses the kind, a core frozen to v1 refuses the manifest's API, and a core cannot allow agents under v1."""
    with pytest.raises(hp.PackageError, match="code files"):
        hp.check_shape(agent_files(), hp.Limits(package_api="harness_package.v2"))
    with pytest.raises(hp.PackageError, match="component kinds \\['agent'\\]"):
        hp.check_shape(agent_files(), hp.Limits(components=(*hp.TEXT_KINDS, "tools"), package_api="harness_package.v2"))
    # a stray agent/*.py under such a core is refused as the kind it is, not told to name an entrypoint
    with pytest.raises(hp.PackageError, match="component kinds \\['agent'\\]"):
        hp.check_shape({"manifest.json": json.dumps(hp.empty_manifest("harness_package.v2")), "agent/loop.py": AGENT},
                       hp.Limits(components=(*hp.TEXT_KINDS, "tools"), package_api="harness_package.v2"))
    with pytest.raises(hp.PackageError, match="this core allows \\['harness_package.v1'\\]"):
        hp.check_shape(agent_files(), hp.Limits(components=(*hp.TEXT_KINDS, "tools")))
    with pytest.raises(hp.PackageError, match="needs package_api harness_package.v2"):
        hp.Limits(components=hp.KINDS, package_api="harness_package.v1")
    assert hp.Limits(components=hp.KINDS).package_api == "harness_package.v2"   # unnamed: the oldest API with agents
    assert hp.Limits.from_json({"components": ["memory"]}).package_api == "harness_package.v1"   # an older K
    assert hp.Limits.from_json(V2.to_json()) == V2 and V2.to_json()["package_api"] == "harness_package.v2"
