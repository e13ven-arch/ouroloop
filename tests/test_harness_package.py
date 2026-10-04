"""v0.6 first-version contracts and the capability package (docs/modules/00_CONTRACTS.md §1–2,
docs/modules/01_POLICY_RUNTIME.md §1): CT6-01, CT6-14, PR6-13 (text part), PR6-14, and the package shape."""
import json
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
    """IT6-22 (text part) in miniature: the same request, and a package skill changes what gets done."""
    from ouroloop.agent import Agent, AgentConfig
    from tests import rsi
    releases, rel, objects, package = v06_workspace(tmp_path, text_files(skill=rsi.FINISH_CHECK_MARK))
    bound = P.bind_release(rel, profile="task", services=P.Services(objects))
    worker = rsi.BusinessWorker()
    prompt, _ = bound.system_prompt("You are a business agent.")
    calls = []

    class World:
        def specs(self):
            return []

        def run(self, name, args):
            from ouroloop.agent.tools import ToolResult
            calls.append(args.get("url"))
            return ToolResult("{}")

        def mutates(self, name, args):
            return True

    agent = Agent(worker, rsi.runtime(tmp_path), hp.mount(World(), bound), system=prompt,
                  config=AgentConfig(approve="auto", max_turns=6, gated_tools=()))
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
    assert "jane" not in hp.describe_hits(["jane.doe@acme.com"])            # the output names kinds, not values


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
