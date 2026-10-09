"""A config, its preset and K (docs/design/ACTIVITY.md §3, §9; CONTRACTS §4, §9): a config may only tighten its preset,
the old table names are refused with their new ones, and K is frozen reproducibly from the config, the split and the
code."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from ouroloop.activity import config as cf
from ouroloop.activity import presets
from ouroloop.activity.config import ConfigError
from ouroloop.harness.contract import SITES
from ouroloop.providers.claude_code import REQUESTS_PER_CALL

PRESET = presets.preset("automationbench_v1")
PROXY = {"mode": "proxy", "allow": [{"host": "api.example.com", "port": 443, "methods": ["GET"]},
                                    {"host": "hooks.example.com", "port": 443, "methods": ["POST"]}]}


def config(**sections) -> dict:
    """The smallest config K resolves from, with `sections` laid over it."""
    cfg = {"workspace": {"root": "ws"}, "bench": {"domain": "rsi"},
           "model": {"type": "claude_code", "model": "claude-opus-5-5", "effort": "medium"},
           "harness": {"preset": "automationbench_v1"}, "budget": {"usd": 100.0, "final_usd": 30.0}}
    for name, table in sections.items():
        cfg[name] = {**cfg.get(name, {}), **table}
    return cfg


# ---------------------------------------------------------------------------- only tighten
# One case per rule: (section, the config's table that loosens, what the error names, the table that tightens, the
# merged value the tightening gives, read by `get`).
RULE_CASES = {
    "cap": ("limits", {"max_steps": 51}, "max_steps", {"max_steps": 10}, lambda k: k["limits"]["max_steps"], 10),
    "floor": ("gate", {"min_gain": 0.01}, "min_gain", {"min_gain": 0.1}, lambda k: k["gate"]["min_gain"], 0.1),
    "subset": ("harness", {"sites": [*SITES, "on_everything"]}, "sites", {"sites": ["on_finish", "before_tool"]},
               lambda k: k["harness"]["sites"], ["before_tool", "on_finish"]),
    "ranges": ("harness", {"config_ranges": {"max_turns": [5, 80]}}, "max_turns",
               {"config_ranges": {"max_turns": [20, 40]}}, lambda k: k["harness"]["config_ranges"]["max_turns"],
               [20, 40]),
    "effort": ("model", {"effort": "high"}, "effort", {"effort": "low"}, lambda k: k["model"]["effort"], "low"),
    "bytes": ("worker", {"memory": "1g"}, "memory", {"memory": "256m"}, lambda k: k["worker"]["memory"], "256m"),
    "fixed": ("worker", {"image": "someone-elses-image:latest"}, "image", {"image": "ouroloop-worker:py3.12"},
              lambda k: k["worker"]["image"], "ouroloop-worker:py3.12"),
    "network": ("worker", {"network": PROXY}, "network", {"network": "none"}, lambda k: k["worker"]["network"],
                "none"),
    "superset": ("memory", {"heldout_domains": ["finance"]}, "heldout_domains",
                 {"heldout_domains": ["finance", "operations"]}, lambda k: k["memory"]["heldout_domains"],
                 ["operations", "finance"]),
}


def rule_kinds(rules) -> set:
    out = set()
    for rule in rules.values():
        out |= rule_kinds(rule) if isinstance(rule, dict) else {rule}
    return out


def test_every_rule_of_the_preset_table_has_a_case():
    assert rule_kinds(presets.RULES) - {"free"} == set(RULE_CASES)


@pytest.mark.parametrize("rule", sorted(RULE_CASES))
def test_a_rule_refuses_loosening_and_accepts_tightening(rule):
    section, loose, field, tight, get, merged = RULE_CASES[rule]
    with pytest.raises(ConfigError, match=rf"\[{section}\]\.[a-z_.]*{field}"):
        cf.merge(config(**{section: loose}), PRESET)
    assert get(cf.merge(config(**{section: tight}), PRESET)) == merged


def test_an_untouched_field_keeps_the_presets_value():
    k = cf.merge(config(), PRESET)
    assert k["limits"] == PRESET["limits"] and k["worker"]["memory"] == "512m" and k["gate"] == PRESET["gate"]


def test_a_proxy_network_may_be_narrowed_but_not_widened():
    with pytest.raises(ConfigError, match="the preset's network is 'none', and a config may only narrow it"):
        cf.merge(config(worker={"network": {"mode": "proxy", "allow": []}}), PRESET)      # a proxy, even to nowhere
    table = {**PRESET, "worker": {**PRESET["worker"], "network": PROXY}}
    narrow = {"mode": "proxy", "allow": PROXY["allow"][:1]}
    assert cf.merge(config(worker={"network": narrow}), table)["worker"]["network"] == narrow
    assert cf.merge(config(worker={"network": "none"}), table)["worker"]["network"] == "none"
    wider = {"mode": "proxy", "allow": [*PROXY["allow"], {"host": "evil.example.com", "port": 443, "methods": ["GET"]}]}
    with pytest.raises(ConfigError, match="allowlist names hosts the preset does not allow"):
        cf.merge(config(worker={"network": wider}), table)


def test_a_range_outside_or_inverted_and_an_unknown_knob_are_refused():
    for ranges, match in (({"max_turns": [20, 90]}, "inside the preset's"), ({"max_turns": [40, 20]}, "inside"),
                          ({"temperature": [0, 1]}, "no such knob")):
        with pytest.raises(ConfigError, match=match):
            cf.merge(config(harness={"config_ranges": ranges}), PRESET)


def test_an_effort_outside_the_scale_is_refused():
    with pytest.raises(ConfigError, match="an effort at most"):
        cf.merge(config(model={"effort": "turbo"}), PRESET)


# ---------------------------------------------------------------------------- tables and keys
@pytest.mark.parametrize("old,new", [
    ("llm", "[model]"), ("research", "[research_model]"), ("improvement", "[harness] preset and [improver]"),
    ("harness_evolution", "[harness]"), ("freeze", "[gate], [schedule], [budget] and [final]"),
    ("estimate", "the final's estimate reads the costs on record"), ("trainer", "the training stack's config")])
def test_an_old_table_is_refused_naming_its_new_one(old, new):
    with pytest.raises(ConfigError, match=rf"\[{old}\] is not read any more: it is .*") as refused:
        cf.resolve(config(**{old: {"type": "claude_code"}}))
    assert new in str(refused.value)


def test_an_unknown_table_an_unknown_key_and_an_unknown_rsi_key_are_refused():
    with pytest.raises(ConfigError, match=r"unknown tables \['bogus'\]"):
        cf.resolve(config(bogus={"x": 1}))
    with pytest.raises(ConfigError, match=r"\[limits\] does not take \['max_turns'\]"):
        cf.resolve(config(limits={"max_turns": 3}))
    with pytest.raises(ConfigError, match=r"\[harness\]\.limits does not take \['max_lines'\]"):
        cf.resolve(config(harness={"limits": {"max_lines": 3}}))
    with pytest.raises(ConfigError, match=r"\[rsi\] does not take \['budget_ref'\]"):
        cf.resolve(config(rsi={"scope_id": "s", "budget_ref": "change:sha256:" + "0" * 64}))


@pytest.mark.parametrize("value", ["<printed by ouroloop freeze>", "TODO", "changeme"])
def test_a_placeholder_ref_is_refused(value):
    with pytest.raises(ConfigError, match="placeholder: run `ouroloop freeze`"):
        cf.resolve(config(rsi={"scope_id": "s", "core_ref": value}))


def test_a_config_names_its_preset_domain_and_actor():
    for cfg, match in ((config(harness={"preset": ""}), r"\[harness\] preset is required"),
                       (config(harness={"preset": "automationbench_v0"}), "no preset 'automationbench_v0'"),
                       (config(bench={"domain": ""}), r"\[bench\] domain is required"),
                       (config(model={"model": ""}), r"\[model\] model names the actor")):
        with pytest.raises(ConfigError, match=match):
            cf.resolve(cfg)


# ---------------------------------------------------------------------------- resolve and K
def test_resolve_fills_the_derived_fields():
    k = cf.resolve(config(research_model={"effort": "low"}))
    assert {key: k["model"][key] for key in ("provider", "identity", "max_effort")} == \
        {"provider": "claude_code", "identity": "claude-opus-5-5", "max_effort": "medium"}
    # R defaults to the actor's model, at its own (lower) effort
    assert k["research_model"]["model"] == k["research_model"]["identity"] == "claude-opus-5-5"
    assert k["research_model"]["effort"] == "low"
    assert k["harness"]["api_version"] == "harness_package.v3" and "preset" not in k["harness"]
    assert set(k) == set(cf.SECTIONS)
    named = cf.resolve(config(research_model={"model": "claude-other-1"}))["research_model"]
    assert named["model"] == named["identity"] == "claude-other-1"


@pytest.mark.parametrize("final_usd", [100.0, 150.0])
def test_the_final_share_must_be_below_the_root(final_usd):
    with pytest.raises(ConfigError, match="final_usd is a share of usd"):
        cf.resolve(config(budget={"usd": 100.0, "final_usd": final_usd}))
    assert cf.resolve(config(budget={"usd": 100.0, "final_usd": 99.0}))["budget"] == {"usd": 100.0, "final_usd": 99.0}


def test_freeze_core_is_reproducible_and_covers_the_split_and_the_code():
    k = cf.freeze_core(config(), split_hash="aaaa", code_version="c1")
    reordered = json.loads(json.dumps(config(), sort_keys=True))
    assert cf.freeze_core(reordered, split_hash="aaaa", code_version="c1").ref() == k.ref()
    others = {cf.freeze_core(config(), split_hash="bbbb", code_version="c1").ref(),
              cf.freeze_core(config(), split_hash="aaaa", code_version="c2").ref(),
              cf.freeze_core(config(limits={"max_steps": 49}), split_hash="aaaa", code_version="c1").ref()}
    assert k.ref() not in others and len(others) == 3
    doc = k.json()
    assert doc["split_hash"] == "aaaa" and doc["code_version"] == "c1" and doc["preset"] == "automationbench_v1"
    assert doc["model"]["identity"] == "claude-opus-5-5" and doc["limits"] == PRESET["limits"]
    # the frozen document reads back as the same K
    assert cf.Core.from_json(json.loads(json.dumps(k.to_json()))).ref() == k.ref()


def test_relative_paths_are_read_against_the_configs_directory(tmp_path):
    path = tmp_path / "examples" / "a.toml"
    path.parent.mkdir()
    path.write_text('[workspace]\nroot = "workspace-a"\n\n[bench]\ndomain = "rsi"\nupstream_path = "../upstream"\n',
                    encoding="utf-8")
    cfg = cf.load_config(path)
    assert cf.workspace_root(cfg) == (tmp_path / "examples" / "workspace-a").resolve()
    assert cf.upstream_path(cfg) == str((tmp_path / "upstream").resolve())


def test_the_quickstart_config_is_a_new_finance_activity_with_nothing_frozen():
    """README's first activity: it resolves under the new tables, pins finance's v0.4 split, runs in a workspace no
    other config names (so it touches no run record), names no frozen ref and has no [final], so no final can read
    finance's test from it; the memory waits for stage 2, and one CLI call's output fits a reservation."""
    examples = Path(__file__).resolve().parents[2] / "examples" / "automationbench"
    cfg = cf.load_config(examples / "quickstart.toml")
    k = cf.resolve(cfg)
    assert (k["bench"]["domain"], k["bench"]["frozen_splits"]) == ("finance", {"finance": "4fd91eaf9fb8daf5"})
    assert cf.preset_name(cfg) == "automationbench_v1" and k["harness"]["seed"] == "stirrup"
    assert cf.workspace_root(cfg) == examples / "workspace-quickstart"
    others = {tomllib.loads(p.read_text(encoding="utf-8")).get("workspace", {}).get("root")
              for p in examples.glob("*.toml") if p.name != "quickstart.toml"}
    assert "workspace-quickstart" not in others
    assert cfg["rsi"] == {"scope_id": "finance-quickstart"} and "final" not in cfg
    assert k["memory"]["enabled"] is False and k["worker"]["network"] == "none"
    assert (k["model"]["type"], k["model"]["effort"]) == (k["research_model"]["type"], k["research_model"]["effort"]) \
        == ("claude_code", "medium")
    for section in ("model", "research_model"):
        assert k[section]["max_tokens"] * REQUESTS_PER_CALL <= k["pricing"]["max_output_tokens"]
