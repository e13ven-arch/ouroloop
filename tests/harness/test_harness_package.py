"""The package model (docs/design/HARNESS.md §3, §4, §6, §10): paths, limits, manifest v3, the stored package, patches
and diffs. Built on the stirrup seed under a K with the actor `claude-opus-5-5` at effort medium."""
from __future__ import annotations

import json

import pytest

from ouroloop.contracts import Objects
from ouroloop.harness import package as pk
from ouroloop.harness import seed as sd
from ouroloop.harness.kinds import KIND_TABLE, KINDS, MEMORY_PATH, PATTERNS

CORE = {"model": {"identity": "claude-opus-5-5", "effort": "medium", "max_effort": "medium", "max_tokens": 32000},
        "memory": {"enabled": True}, "harness": {}}
LIMITS = pk.Limits.from_core(CORE)


@pytest.fixture(scope="module")
def seed(tmp_path_factory):
    return sd.build_seed("stirrup", core=CORE, objects=Objects(tmp_path_factory.mktemp("objects")))


def with_manifest(files: dict, change) -> dict:
    manifest = json.loads(files["manifest.json"])
    change(manifest)
    return {**files, "manifest.json": json.dumps(manifest, indent=1)}


def problems(files, seed, parent=None, limits=LIMITS):
    return pk.check_shape(files, limits=limits, seed=seed, parent=parent)


# ---------------------------------------------------------------------------- paths
VALID = {"manifest": "manifest.json", "agent": "agent/x_agent.py", "vendor": "vendor/stirrup/core/agent.py",
         "tool": "tools/table_diff.py", "hook": "middlewares/date_guard.py", "prompt": "prompt/finish.md",
         "skill": "skills/reconcile/tables.md", "memory": MEMORY_PATH, "test": "tests/test_x.py"}
INVALID = {"manifest": "manifest.yaml", "agent": "agent/X.py", "vendor": "vendor/Stirrup/x.py", "tool": "tools/x.js",
           "hook": "middlewares/sub/x.py", "prompt": "prompt/other.md", "skill": "skills/X.md",
           "memory": "memory/other.jsonl", "test": "tests/sub/test_x.py"}


@pytest.mark.parametrize("kind", [k for k, _ in PATTERNS])
def test_every_pattern_has_a_path_it_takes_and_one_it_refuses(kind):
    assert pk.classify(VALID[kind]) == kind
    with pytest.raises(pk.PackageError):
        pk.classify(INVALID[kind])


@pytest.mark.parametrize("path", ["/abs/x.py", "tools/../x.py", "tools//x.py", "tools\\x.py", "./manifest.json", ""])
def test_paths_are_relative_posix_without_dot_segments(path):
    with pytest.raises(pk.PackageError):
        pk.classify(path)


def test_the_kind_table_covers_every_kind():
    assert tuple(KIND_TABLE) == KINDS
    assert KIND_TABLE["tool"].credit == "invoked" and KIND_TABLE["hook"].credit == "triggered"
    assert KIND_TABLE["memory"].host_only and not KIND_TABLE["skill"].code
    assert KIND_TABLE["tool"].ladder == ("V0", "V1", "V2a") and KIND_TABLE["hook"].ladder == ("V0", "V1", "V2b")


# ---------------------------------------------------------------------------- the seed passes, its variants do not
def test_the_seed_passes_its_own_shape(seed):
    assert problems(seed.files, seed, parent=seed) == []
    assert seed.manifest.model == {"base": "claude-opus-5-5", "effort": "medium", "max_tokens": 32000,
                                   "artifact_ref": None}
    assert seed.manifest.memory["enabled"] is True and seed.manifest.config["max_turns"] == 50
    assert {"agent", "vendor", "tool", "hook", "prompt", "test", "config", "memory"} <= seed.kinds


@pytest.mark.parametrize("limit,value", [("max_files", 10), ("max_package_bytes", 100_000),
                                         ("max_vendor_bytes", 100_000), ("max_tools", 0), ("max_hooks", 3),
                                         ("max_tests", 1), ("max_prompt_chars", 10)])
def test_each_limit_refuses_a_package_over_it(seed, limit, value):
    limits = pk.Limits.from_json({**LIMITS.to_json(), limit: value})
    assert problems(seed.files, seed, limits=limits), limit


def test_the_skill_count_and_size_and_memory_bytes_limits(seed):
    files = {**seed.files, "skills/a.md": "x" * 50}
    files = with_manifest(files, lambda m: m["skills"].append({"name": "a", "path": "skills/a.md"}))
    assert problems(files, seed) == []
    assert problems(files, seed, limits=pk.Limits.from_json({**LIMITS.to_json(), "max_skill_chars": 10}))
    assert problems(files, seed, limits=pk.Limits.from_json({**LIMITS.to_json(), "max_skills": 0}))
    memory = with_manifest({**seed.files, MEMORY_PATH: "{}\n" * 100},
                           lambda m: m["memory"].update(snapshot=MEMORY_PATH))
    assert problems(memory, seed) == []
    assert problems(memory, seed, limits=pk.Limits.from_json({**LIMITS.to_json(), "max_memory_bytes": 10}))


def test_a_candidate_may_not_change_the_host_written_memory(seed):
    memory = with_manifest({**seed.files, MEMORY_PATH: '{"id": "e1"}\n'},
                           lambda m: m["memory"].update(snapshot=MEMORY_PATH))
    assert any("memory" in p for p in problems(memory, seed, parent=seed))
    assert problems(memory, seed, parent=None) == []                # the memory package builds without a parent
    with pytest.raises(pk.PackageError, match="memory package"):
        pk.apply_patch(seed, {MEMORY_PATH: "{}\n"})


def test_a_vendored_packages_license_and_provenance_are_the_seeds(seed):
    for leaf in ("LICENSE", "PROVENANCE.json"):
        path = f"vendor/stirrup/{leaf}"
        changed = {**seed.files, path: seed.files[path] + "\n"}
        assert any("differs from the seed" in p for p in problems(changed, seed)), leaf
    edited = {**seed.files, "vendor/stirrup/constants.py": seed.files["vendor/stirrup/constants.py"] + "\n# R\n"}
    assert problems(edited, seed) == []                              # the code itself is editable


def test_kinds_outside_k_are_refused(seed):
    narrow = pk.Limits.from_json({**LIMITS.to_json(), "kinds": [k for k in LIMITS.kinds if k != "hook"]})
    assert any("not allowed by K" in p for p in problems(seed.files, seed, limits=narrow))


# ---------------------------------------------------------------------------- manifest
@pytest.mark.parametrize("version", ["harness_package.v1", "harness_package.v2", "harness_package.v4"])
def test_old_and_unknown_api_versions_are_refused(seed, version):
    files = with_manifest(seed.files, lambda m: m.update(api_version=version))
    assert any("harness_package.v3 only" in p for p in problems(files, seed))


def test_the_model_must_be_ks_and_its_effort_within_ks_cap(seed):
    other = with_manifest(seed.files, lambda m: m["model"].update(base="another-model"))
    assert any("is not K's model" in p for p in problems(other, seed))
    high = with_manifest(seed.files, lambda m: m["model"].update(effort="high"))
    assert any("at most K's 'medium'" in p for p in problems(high, seed))
    low = with_manifest(seed.files, lambda m: m["model"].update(effort="low"))
    assert problems(low, seed) == []
    bad_ref = with_manifest(seed.files, lambda m: m["model"].update(artifact_ref="package:sha256:" + "0" * 64))
    assert any("train_artifact" in p for p in problems(bad_ref, seed))
    bound = with_manifest(seed.files, lambda m: m["model"].update(artifact_ref="train_artifact:sha256:" + "0" * 64))
    assert problems(bound, seed) == [] and "model" in pk.kinds_of(pk.parse_manifest(
        bound["manifest.json"], bound, limits=LIMITS), bound)


@pytest.mark.parametrize("key,value", [("max_turns", 5), ("max_turns", 81), ("summarize_at", 0.95),
                                       ("subagent_depth", 3), ("shown_k", 1.5), ("unknown_knob", 1)])
def test_config_outside_ks_ranges_is_refused(seed, key, value):
    files = with_manifest(seed.files, lambda m: m["config"].update({key: value}))
    assert problems(files, seed), (key, value)


def test_a_hook_site_k_does_not_allow_is_refused(seed):
    narrow = pk.Limits.from_json({**LIMITS.to_json(), "sites": ["before_tool", "after_tool", "on_finish"]})
    assert any("site 'before_model'" in p for p in problems(seed.files, seed, limits=narrow))
    files = with_manifest(seed.files, lambda m: m["hooks"].append(
        {"name": "x", "site": "after_everything", "entrypoint": "middlewares/write_guard.py:ledger"}))
    assert problems(files, seed)


def test_writes_defaults_to_false_and_is_kept_when_declared(seed):
    assert seed.manifest.tools[0]["writes"] is False
    files = {**seed.files, "tools/post.py": "def run(ctx, args):\n    return 'ok'\n"}
    files = with_manifest(files, lambda m: m["tools"].append(
        {"name": "post", "description": "Post.", "input_schema": {"type": "object"},
         "entrypoint": "tools/post.py:run", "writes": True}))
    manifest = pk.parse_manifest(files["manifest.json"], files, limits=LIMITS)
    assert [t["writes"] for t in manifest.tools] == [False, True]


@pytest.mark.parametrize("change,match", [
    (lambda m: m.pop("agent"), "no agent entry"),
    (lambda m: m["agent"].update(entrypoint="agent/stirrup_agent.py:main"), "named run"),
    (lambda m: m["agent"].update(roles=["act", "judge"]), "roles"),
    (lambda m: m["tools"].append(dict(m["tools"][0])), "used twice"),
    (lambda m: m["tools"][0].update(name="read_skill"), "host's own tool"),
    (lambda m: m["tools"][0].update(input_schema={"type": "array"}), "object schema"),
    (lambda m: m["tools"][0].update(description="x" * 2000), "description"),
    (lambda m: m.update(tests=["tests/test_write_guard.py"]), "not listed"),
    (lambda m: m.update(vendor=[]), "not declared"),
    (lambda m: m.update(prompt={"system": "prompt/system.md"}), "not named"),
    (lambda m: m.update(extra=1), "does not take"),
])
def test_manifest_shape_errors(seed, change, match):
    files = with_manifest(seed.files, change)
    assert any(match in p for p in problems(files, seed)), problems(files, seed)


# ---------------------------------------------------------------------------- storage, patch, diff
def test_build_is_content_addressed_and_load_round_trips(seed, tmp_path):
    objects = Objects(tmp_path)
    a = pk.build(objects, seed.files, limits=LIMITS, seed=seed)
    b = pk.build(objects, dict(reversed(list(seed.files.items()))), limits=LIMITS, seed=seed)
    assert a.ref() == b.ref() == seed.ref()
    assert pk.load(objects, a.ref()).files == seed.files
    changed = pk.build(objects, {**seed.files, "prompt/system.md": "Be careful."}, limits=LIMITS, seed=seed)
    assert changed.ref() != a.ref()
    # one file object per distinct text: the vendored files are stored once for both packages
    assert len(list((objects.dir / "file").glob("*.json"))) == len(set(seed.files.values()) | {"Be careful."})
    with pytest.raises(pk.PackageError):
        pk.build(objects, with_manifest(seed.files, lambda m: m.update(api_version="harness_package.v2")),
                 limits=LIMITS, seed=seed)


def test_run_files_leave_out_tests_and_memory(seed):
    files = with_manifest({**seed.files, MEMORY_PATH: "{}\n"}, lambda m: m["memory"].update(snapshot=MEMORY_PATH))
    package = pk.Package(manifest=pk.parse_manifest(files["manifest.json"], files, limits=LIMITS), files=files)
    run = pk.run_files(package)
    assert MEMORY_PATH not in run and not any(p.startswith("tests/") for p in run)
    assert "agent/stirrup_agent.py" in run and "vendor/stirrup/core/agent.py" in run


def test_apply_patch_writes_deletes_and_refuses_a_no_op(seed):
    files = pk.apply_patch(seed, {"prompt/system.md": "Plan first.", "tests/test_write_guard.py": None})
    assert files["prompt/system.md"] == "Plan first." and "tests/test_write_guard.py" not in files
    with pytest.raises(pk.PackageError, match="exactly as it was"):
        pk.apply_patch(seed, {"prompt/system.md": seed.files["prompt/system.md"]})
    with pytest.raises(pk.PackageError, match="no such file"):
        pk.apply_patch(seed, {"tools/none.py": None})


def test_diff_names_kinds_sites_and_vendor(seed):
    tool = with_manifest({**seed.files, "tools/table_diff.py": "def run(ctx, args):\n    return 'x'\n"},
                         lambda m: m["tools"].append({"name": "table_diff", "description": "Diff.",
                                                      "input_schema": {"type": "object"},
                                                      "entrypoint": "tools/table_diff.py:run"}))
    d = pk.diff(seed, tool)
    assert d.kinds == {"tool"} and d.sites == set() and not d.vendor_touched
    assert d.changed == {"manifest.json": "modified", "tools/table_diff.py": "added"}
    guard = {**seed.files, "middlewares/write_guard.py": seed.files["middlewares/write_guard.py"] + "\n# R\n"}
    d = pk.diff(seed, guard)
    assert d.kinds == {"hook"} and d.sites == {"after_tool", "before_tool", "before_model", "on_finish"}
    off = with_manifest(seed.files, lambda m: m["hooks"][1].update(enabled=False))
    assert pk.diff(seed, off).sites == {"before_tool"} and pk.diff(seed, off).kinds == {"hook"}
    vendor = {**seed.files, "vendor/stirrup/constants.py": seed.files["vendor/stirrup/constants.py"] + "\n"}
    assert pk.diff(seed, vendor).vendor_touched and pk.diff(seed, vendor).kinds == {"vendor"}
    knob = with_manifest(seed.files, lambda m: m["config"].update(max_turns=40))
    assert pk.diff(seed, knob).kinds == {"config"}
