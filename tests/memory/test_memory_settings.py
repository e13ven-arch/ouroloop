"""K's memory section (docs/design/MEMORY.md §3, §4 settings.py): the defaults are the design's table, the JSON round
trip is exact, an unknown key, a bad origin or a bad import is refused, the retriever and the backend are pinned to
this checkout, and the preset's memory section is the Settings' own, merged by §3's rules (presets.RULES through
config.merge, the held-out lines by the "superset" rule)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import fields

import pytest

from ouroloop import memory as M
from ouroloop.activity import config as cf
from ouroloop.activity import presets
from ouroloop.activity.config import ConfigError
from ouroloop.memory.settings import ORIGINS, Settings, preset_table, settings_of

PRESET = presets.preset("automationbench_v1")

# MEMORY.md §3, the table as written (retriever and backend are the pins, checked on their own below)
DESIGN_DEFAULTS = {
    "enabled": True, "shown_k": 2, "recall_k": 3, "fetch_factor": 5, "min_score": 0.62, "sibling_sim": 0.95,
    "batch_dedup_threshold": 0.85, "store_dedup_threshold": 0.9, "store_dedup_content_threshold": 0.85,
    "validation_threshold": 0.6, "success_threshold": 1.0, "origins": ("success", "failure", "comparative"),
    "max_active": 500, "probation_shown": 3, "retire_harm_tasks": 2, "retire_harm_min_shown": 5,
    "retire_min_shown": 3, "retire_mean_credit": -0.2, "min_other_tasks": 2, "when_chars": 400,
    "content_chars": 1600, "traj_tool_chars": 4000, "traj_total_chars": 60000, "stream_batch": 4, "cite": True,
    "retry": True, "abort_d": 0.0, "heldout_domains": ("operations",), "imports": (), "llm_timeout_s": 1800.0,
    "promote_fixes": 2, "promote_sources": 2,
}
PINS = ("retriever", "backend")

# MEMORY.md §3's merge rules: free, cap, floor, subset, superset; every other field fixed
DESIGN_RULES = {
    "free": {"enabled", "retry", "imports"},
    "cap": {"shown_k", "recall_k", "max_active", "when_chars", "content_chars", "traj_tool_chars", "traj_total_chars",
            "llm_timeout_s", "sibling_sim"},
    "floor": {"min_score", "validation_threshold", "min_other_tasks", "abort_d", "promote_fixes", "promote_sources"},
    "subset": {"origins"},
    "superset": {"heldout_domains"},
}
IMPORT = {"release_ref": "rel-0001", "snapshot_digest": "0123456789abcdef"}


def names() -> list[str]:
    return [f.name for f in fields(Settings)]


# ---------------------------------------------------------------------------------------------- defaults
def test_the_defaults_are_the_designs_table():
    s = Settings()
    assert set(names()) == set(DESIGN_DEFAULTS) | set(PINS)
    got = {k: getattr(s, k) for k in DESIGN_DEFAULTS}
    assert got == DESIGN_DEFAULTS
    # the type too: K is hashed as JSON, where 600 and 600.0 are different bytes
    assert {k: type(v) for k, v in got.items()} == {k: type(v) for k, v in DESIGN_DEFAULTS.items()}
    assert s.origins == ORIGINS


def test_the_retriever_is_pinned_to_bge_small_at_its_revision():
    r = Settings().retriever
    assert r == M.default_retriever()
    assert (r["model"], r["revision"], r["file"], r["sha256"]) == (
        "BAAI/bge-small-en-v1.5", "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a", "onnx/model.onnx",
        "828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35")
    assert set(r["aux_sha256"]) == {"tokenizer.json", "config.json", "1_Pooling/config.json"}
    assert all(len(v) == 64 for v in r["aux_sha256"].values())
    assert (r["dims"], r["pooling"], r["normalize"], r["max_tokens"], r["query_prefix"]) == (384, "cls", True, 512, "")


def test_the_backend_pins_are_this_checkouts_lock_and_flows():
    b = Settings().backend
    assert b == M.default_backend() and M.backend_problems(b) == []
    assert b["kind"] == "reme" and b["version"] == "0.3.1.10" and b["wheel_sha256"] == M.REME_WHEEL_SHA256
    assert b["lock_sha256"] == hashlib.sha256(M.LOCK.read_bytes()).hexdigest()
    assert b["flows_sha256"] == hashlib.sha256(M.FLOWS.read_bytes()).hexdigest()
    assert M.LOCK.parent.name == "sidecar" and M.FLOWS.parent == M.LOCK.parent


def test_a_backend_pin_that_differs_from_this_checkout_is_named():
    b = Settings().backend
    stale = M.backend_problems({**b, "lock_sha256": "0" * 64})
    assert len(stale) == 1 and stale[0].startswith("memory.backend.lock_sha256 is '0000")
    assert len(M.backend_problems({**b, "version": "0.3.1.9", "flows_sha256": "f" * 64})) == 2
    assert len(M.backend_problems({})) == len(b) == 5


# ---------------------------------------------------------------------------------------------- the retriever table
def test_a_retriever_table_is_completed_from_the_pins():
    assert Settings(retriever={}).retriever == M.default_retriever()
    assert Settings(retriever={"model": M.MODEL_REPO, "dims": 384}).retriever == M.default_retriever()
    s = Settings.from_json({"retriever": {"query_prefix": "query: "}})
    assert s.retriever == {**M.default_retriever(), "query_prefix": "query: "}


def test_another_model_must_pin_every_file_the_embedder_reads():
    other = {"model": "someone/other-embedder", "revision": "abc", "sha256": "1" * 64}
    with pytest.raises(ValueError, match="aux_sha256"):
        Settings(retriever=other)
    aux = {"tokenizer.json": "2" * 64, "config.json": "3" * 64, "1_Pooling/config.json": "4" * 64}
    s = Settings(retriever={**other, "aux_sha256": aux})
    assert s.retriever["aux_sha256"] == aux and s.retriever["model"] == "someone/other-embedder"
    with pytest.raises(ValueError, match=r"\['1_Pooling/config.json', 'config.json'\]"):
        Settings(retriever={"aux_sha256": {"tokenizer.json": "2" * 64}})     # a partial table is not completed


# ---------------------------------------------------------------------------------------------- JSON
def test_to_json_and_from_json_round_trip():
    s = Settings()
    doc = s.to_json()
    assert set(doc) == set(names())
    assert json.loads(json.dumps(doc)) == doc                             # plain JSON: tuples became lists
    assert doc["origins"] == list(ORIGINS) and doc["heldout_domains"] == ["operations"] and doc["imports"] == []
    assert Settings.from_json(doc) == s and Settings.from_json(json.loads(json.dumps(doc))) == s
    tight = Settings.from_json({"enabled": False, "shown_k": 1, "min_score": 0.7, "origins": ["failure", "success"],
                                "heldout_domains": ["operations", "finance"], "imports": [IMPORT],
                                "llm_timeout_s": 900})
    again = Settings.from_json(json.loads(json.dumps(tight.to_json())))
    assert again == tight and again.to_json() == tight.to_json()
    assert tight.origins == ("failure", "success") and tight.heldout_domains == ("operations", "finance")
    assert tight.imports == (IMPORT,) and tight.to_json()["imports"] == [IMPORT]
    assert isinstance(tight.llm_timeout_s, float) and tight.llm_timeout_s == 900.0


def test_from_json_of_nothing_and_from_core_are_the_defaults():
    assert Settings.from_json(None) == Settings.from_json({}) == Settings()
    assert Settings.from_core({}) == Settings.from_core({"memory": None}) == Settings()
    assert Settings.from_core({"memory": {"shown_k": 1}, "model": {}}).shown_k == 1
    assert settings_of({"recall_k": 2}).recall_k == 2
    s = Settings(shown_k=1)
    assert settings_of(s) is s and settings_of(None) == Settings()


def test_to_json_does_not_hand_out_the_settings_own_tables():
    s = Settings(imports=(IMPORT,))
    doc = s.to_json()
    doc["retriever"]["dims"] = 1
    doc["backend"]["version"] = "x"
    doc["imports"][0]["release_ref"] = "changed"
    assert s.retriever["dims"] == 384 and s.backend["version"] == M.REME_VERSION
    assert s.imports[0]["release_ref"] == "rel-0001"


@pytest.mark.parametrize("key", ["rerank", "title_chars", "collection_prefix", "reme_version", "lock_sha256",
                                 "embed_model", "concurrency"])
def test_an_unknown_key_is_refused(key):
    with pytest.raises(ValueError, match=f"memory.{key}: not a memory setting"):
        Settings.from_json({key: 1})


@pytest.mark.parametrize("origins", [[], ["success", "validation"], ["personal"]])
def test_origins_are_a_non_empty_subset_of_remes_extraction_ops(origins):
    with pytest.raises(ValueError, match="memory.origins"):
        Settings.from_json({"origins": origins})


def test_any_non_empty_subset_of_the_origins_is_taken():
    for one in ORIGINS:
        assert Settings.from_json({"origins": [one]}).origins == (one,)


@pytest.mark.parametrize("imp", [{"release_ref": "rel-0001"}, {**IMPORT, "line": "sales"},
                                 {**IMPORT, "snapshot_digest": ""}, {**IMPORT, "release_ref": 7}, "rel-0001",
                                 ["rel-0001", "0123456789abcdef"]])
def test_an_import_is_exactly_a_release_ref_and_a_snapshot_digest(imp):
    with pytest.raises(ValueError, match="memory.imports"):
        Settings.from_json({"imports": [imp]})


@pytest.mark.parametrize("key", ["shown_k", "recall_k", "fetch_factor", "max_active", "stream_batch"])
def test_counts_are_at_least_one(key):
    with pytest.raises(ValueError, match=f"memory.{key} is at least 1"):
        Settings.from_json({key: 0})
    assert getattr(Settings.from_json({key: 1}), key) == 1


# ---------------------------------------------------------------------------------------------- the preset and its rules
def test_the_presets_memory_section_is_the_settings_own():
    assert PRESET["memory"] == Settings().to_json() == preset_table()
    assert Settings.from_json(PRESET["memory"]) == Settings()


def test_every_memory_field_has_the_designs_merge_rule():
    rules = presets.RULES["memory"]
    assert set(rules) == set(names())
    want = {k: next((rule for rule, ks in DESIGN_RULES.items() if k in ks), "fixed") for k in names()}
    assert rules == want


def merged(memory: dict) -> dict:
    return cf.merge({"memory": memory}, PRESET)["memory"]


def test_an_empty_memory_table_merges_to_the_preset():
    assert merged({}) == PRESET["memory"]
    assert Settings.from_json(merged({})) == Settings()


@pytest.mark.parametrize("key,loose,tight", [
    ("shown_k", 3, 1), ("recall_k", 4, 2), ("max_active", 501, 100), ("when_chars", 401, 200),
    ("content_chars", 1601, 800), ("traj_tool_chars", 4001, 2000), ("traj_total_chars", 60001, 30000),
    ("llm_timeout_s", 1801.0, 900.0), ("sibling_sim", 0.97, 0.9),
    ("min_score", 0.6, 0.7), ("validation_threshold", 0.5, 0.7), ("min_other_tasks", 1, 3), ("abort_d", -1.0, 0.5),
    ("promote_fixes", 1, 3), ("promote_sources", 1, 3),
])
def test_a_config_may_only_tighten_a_capped_or_floored_memory_field(key, loose, tight):
    with pytest.raises(ConfigError, match=f"memory\\].{key}"):
        merged({key: loose})
    got = merged({key: tight})
    assert got[key] == tight and {k: v for k, v in got.items() if k != key} == \
        {k: v for k, v in PRESET["memory"].items() if k != key}
    assert getattr(Settings.from_json(got), key) == tight


@pytest.mark.parametrize("key,other", [
    ("fetch_factor", 10), ("batch_dedup_threshold", 0.8), ("store_dedup_threshold", 0.95),
    ("store_dedup_content_threshold", 0.9), ("success_threshold", 0.5), ("probation_shown", 1),
    ("retire_harm_tasks", 3), ("retire_harm_min_shown", 4), ("retire_min_shown", 2), ("retire_mean_credit", -0.5),
    ("stream_batch", 8), ("cite", False),
])
def test_a_fixed_memory_field_is_refused_unless_equal(key, other):
    with pytest.raises(ConfigError, match="the preset fixes"):
        merged({key: other})
    assert merged({key: PRESET["memory"][key]})[key] == PRESET["memory"][key]


def test_the_retriever_and_backend_pins_are_fixed():
    with pytest.raises(ConfigError, match=r"memory\]\.retriever"):
        merged({"retriever": {**PRESET["memory"]["retriever"], "model": "someone/other-embedder"}})
    with pytest.raises(ConfigError, match=r"memory\]\.backend"):
        merged({"backend": {**PRESET["memory"]["backend"], "lock_sha256": "0" * 64}})
    assert merged({"backend": dict(PRESET["memory"]["backend"])}) == PRESET["memory"]


def test_the_free_memory_fields_take_the_configs_value():
    got = merged({"enabled": False, "retry": False, "imports": [IMPORT]})
    assert (got["enabled"], got["retry"], got["imports"]) == (False, False, [IMPORT])
    s = Settings.from_json(got)
    assert s.enabled is False and s.retry is False and s.imports == (IMPORT,)


def test_origins_merge_as_a_subset_in_the_presets_order():
    assert merged({"origins": ["comparative", "success"]})["origins"] == ["success", "comparative"]
    with pytest.raises(ConfigError, match="a subset of the preset's"):
        merged({"origins": ["success", "personal"]})


def test_held_out_lines_merge_as_a_superset():
    """A config may hold out more business lines, never fewer: the preset's come first, the added ones sorted once."""
    got = merged({"heldout_domains": ["sales", "operations", "finance", "sales"]})
    assert got["heldout_domains"] == ["operations", "finance", "sales"]
    assert Settings.from_json(got).heldout_domains == ("operations", "finance", "sales")
    assert merged({"heldout_domains": ["operations"]})["heldout_domains"] == ["operations"]
    for fewer in (["finance"], [], "operations", None):
        with pytest.raises(ConfigError, match="keeps every item of the preset's"):
            merged({"heldout_domains": fewer})


def test_an_unknown_memory_key_in_a_config_is_refused():
    with pytest.raises(ConfigError, match=r"\[memory\] does not take \['rerank'\]"):
        merged({"rerank": True})


def test_a_resolved_k_carries_the_tightened_memory_section():
    cfg = {"workspace": {"root": "ws"}, "bench": {"domain": "sales"},
           "model": {"type": "claude_code", "model": "claude-opus-5-5", "effort": "medium"},
           "harness": {"preset": "automationbench_v1"}, "budget": {"usd": 100.0, "final_usd": 30.0},
           "memory": {"shown_k": 1, "heldout_domains": ["operations", "finance"], "min_score": 0.7}}
    k = cf.freeze_core(cfg, split_hash="s" * 16, code_version="c" * 16).json()
    s = Settings.from_core(k)
    assert (s.shown_k, s.heldout_domains, s.min_score) == (1, ("operations", "finance"), 0.7)
    assert {f: getattr(s, f) for f in ("recall_k", "origins", "retriever", "backend")} == \
        {f: getattr(Settings(), f) for f in ("recall_k", "origins", "retriever", "backend")}


# ---------------------------------------------------------------------------- the review's cases (2026-10-09)
def test_a_held_out_line_given_as_one_string_is_one_line():
    assert Settings(heldout_domains="operations").heldout_domains == ("operations",)
    assert Settings.from_json({"heldout_domains": "finance"}).heldout_domains == ("finance",)


def test_the_clients_timeout_leaves_the_research_provider_its_time_and_a_minute_for_a_slot():
    from ouroloop.memory import RESEARCH_SECONDS
    assert Settings().llm_timeout_s >= RESEARCH_SECONDS + 60
    with pytest.raises(ValueError, match="at least 600 s"):
        Settings(llm_timeout_s=RESEARCH_SECONDS + 59)
