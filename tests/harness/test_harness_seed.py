"""The seed and its vendored upstream (docs/design/HARNESS.md §7, §10): reproducible builds, provenance, the patch
stack, the seed's own tests, and the adapter's agreement with the contract."""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from ouroloop.contracts import Objects
from ouroloop.harness import contract as hc
from ouroloop.harness import seed as sd
from ouroloop.harness import vendor as vd

CORE = {"model": {"identity": "claude-opus-5-5", "effort": "medium", "max_effort": "medium"},
        "memory": {"enabled": False}}


@pytest.fixture
def seed_copy(tmp_path, monkeypatch):
    """A copy of harness/seeds/ to break, with SEEDS pointed at it."""
    root = tmp_path / "seeds"
    shutil.copytree(sd.SEEDS, root, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(sd, "SEEDS", root)
    return root / "stirrup"


def test_the_build_is_reproducible_and_follows_k(tmp_path):
    a = sd.build_seed("stirrup", core=CORE, objects=Objects(tmp_path / "a"))
    b = sd.build_seed("stirrup", core=CORE, objects=Objects(tmp_path / "b"))
    assert a.ref() == b.ref()
    other = sd.build_seed("stirrup", core={**CORE, "model": {**CORE["model"], "identity": "claude-sonnet-5-5"}},
                          objects=Objects(tmp_path / "c"))
    assert other.ref() != a.ref() and other.manifest.model["base"] == "claude-sonnet-5-5"
    on = sd.build_seed("stirrup", core={**CORE, "memory": {"enabled": True}}, objects=Objects(tmp_path / "d"))
    assert on.manifest.memory["enabled"] and not a.manifest.memory["enabled"]
    assert "patches/README.md" not in a.files and "tests/test_write_guard.py" in a.files


def test_a_vendored_file_that_is_not_the_release_is_seed_unavailable(seed_copy, tmp_path):
    target = seed_copy / "vendor" / "stirrup" / "constants.py"
    target.write_text(target.read_text() + "\n# changed\n")
    with pytest.raises(sd.SeedUnavailable, match="sha256 differs"):
        sd.build_seed("stirrup", core=CORE, objects=Objects(tmp_path))


def test_the_patch_stack_replays_and_a_failing_patch_is_named(seed_copy, tmp_path):
    base = sd.build_seed("stirrup", core=CORE, objects=Objects(tmp_path / "a"))
    path = "vendor/stirrup/constants.py"
    edited = {**base.files, path: base.files[path].replace("AGENT_MAX_TURNS = 30", "AGENT_MAX_TURNS = 40")}
    assert edited[path] != base.files[path]
    patch = vd.extract_patch(base, type(base)(manifest=base.manifest, files=edited), "stirrup")
    assert patch.startswith(f"--- a/{path}") and "+AGENT_MAX_TURNS = 40" in patch
    (seed_copy / "patches" / "001-more-turns.patch").write_text(patch)
    patched = sd.build_seed("stirrup", core=CORE, objects=Objects(tmp_path / "b"))
    assert patched.files[path] == edited[path]
    (seed_copy / "patches" / "002-stale.patch").write_text(patch)          # applies to the old text only
    with pytest.raises(sd.SeedUnavailable, match="002-stale.patch"):
        sd.build_seed("stirrup", core=CORE, objects=Objects(tmp_path / "c"))


def test_unified_diffs_round_trip_adds_deletes_and_missing_newlines():
    before = {"vendor/x/a.py": "one\ntwo\nthree", "vendor/x/b.py": "gone\n"}
    after = {"vendor/x/a.py": "one\n2\nthree\nfour", "vendor/x/c.py": "new\n"}
    text = vd.unified(before, after, set(before) | set(after))
    assert vd.apply_unified(before, text) == after
    with pytest.raises(vd.PatchError):
        vd.apply_unified({"vendor/x/a.py": "other\n"}, text)


def test_upgrade_writes_the_new_release_and_reports_patches_that_no_longer_apply(seed_copy, tmp_path):
    source = tmp_path / "stirrup-0.3"
    source.mkdir()
    (source / "LICENSE").write_text("MIT\n")
    (source / "__init__.py").write_text("VERSION = '0.3'\n")
    (seed_copy / "patches" / "001-x.patch").write_text(
        "--- a/vendor/stirrup/__init__.py\n+++ b/vendor/stirrup/__init__.py\n@@ -1 +1 @@\n-VERSION = '0.2'\n"
        "+VERSION = '0.2-patched'\n")
    p = vd.Provenance(name="stirrup", version="0.3.0", url="https://example.invalid/stirrup", tag="v0.3.0",
                      sha256="0" * 64, license="MIT", files=1)
    assert vd.upgrade(seed_copy, source, p) == ["001-x.patch"]
    record = json.loads((seed_copy / "vendor/stirrup/PROVENANCE.json").read_text())
    assert record["version"] == "0.3.0" and list(record["files"]) == ["__init__.py"]
    files = sd.read_tree(seed_copy)
    assert vd.verify(files, "stirrup") == []


def test_the_seeds_own_tests_pass_on_the_host():
    """tests/*.py of the seed, as the worker's test op runs them: every test_* function."""
    root = sd.seed_dir("stirrup")
    failed, ran = [], 0
    for path in sorted((root / "tests").glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(f"seedtest_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in sorted(n for n in dir(module) if n.startswith("test_")):
            ran += 1
            try:
                getattr(module, name)()
            except Exception as e:  # noqa: BLE001 - collected and reported together
                failed.append(f"{path.name}:{name}: {type(e).__name__}: {e}")
    assert ran >= 15 and failed == []


ANSWERS = {
    "before_run": [None, {}, {"system": "s"}, {"messages": [{"role": "user", "content": "x"}]}, {"system": 1},
                   {"messages": "x"}, {"block": {"reason": "x"}}],
    "before_model": [None, {"messages": []}, {"messages": [{"role": "user"}]}, {"messages": [{"x": 1}]}, "x"],
    "before_tool": [None, {"block": {"reason": "r"}}, {"args": {}}, {"block": {"reason": "r"}, "args": {}},
                    {"block": "r"}, {"args": []}, {"output": "x"}],
    "after_tool": [None, {"output": "o"}, {"feedback": "f"}, {"output": "o", "feedback": "f"}, {"output": 1}],
    "after_turn": [None, {"message": "m"}, {"message": None}, {"pushback": "p"}, {"missing": ["a", "b"]},
                   {"missing": []}, {"missing": ["a", 1]}, {"missing": ["a"], "message": "m"}],
    "on_finish": [None, {"pushback": "p"}, {"pushback": 3}, {"message": "m"}, ["p"], {"missing": ["x"]},
                  {"missing": [" "]}, {"missing": "x"}, {"missing": ["x"], "pushback": "p"}],
}


def _adapter_action():
    """The adapter's own check, without importing Stirrup: its source has _action and _messages self-contained."""
    source = (sd.seed_dir("stirrup") / "agent" / "stirrup_agent.py").read_text()
    start, end = source.index("def _messages("), source.index("class Hooks:")
    namespace = {"HookError": type("HookError", (Exception,), {}), "SITES": hc.SITES, "RETURNS": hc.RETURNS}
    exec(source[start:end], namespace)                       # noqa: S102 - the seed's own code, read from the repo
    return namespace["_action"], namespace["HookError"]


@pytest.mark.parametrize("site", hc.SITES)
def test_the_adapter_checks_hook_answers_as_the_contract_does(site):
    action, error = _adapter_action()
    for value in ANSWERS[site]:
        try:
            want = hc.action_of(site, value)
        except hc.HookAnswerError:
            want = "error"
        try:
            got = action(site, value)
        except error:
            got = "error"
        assert got == want, (site, value)


def test_the_adapter_carries_the_contracts_sites_and_returns():
    source = (sd.seed_dir("stirrup") / "agent" / "stirrup_agent.py").read_text()
    namespace: dict = {}
    start = source.index("SITES = (")
    exec(source[start:source.index("\n\n", start)], namespace)   # noqa: S102 - the two literals, read from the repo
    assert namespace["SITES"] == hc.SITES and namespace["RETURNS"] == hc.RETURNS
