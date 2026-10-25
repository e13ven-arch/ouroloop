"""Presets (docs/design/ACTIVITY.md §2; CONTRACTS §4): every field of K, its default, and the direction a config may
move it. A config may only tighten a preset: lower a cap, narrow a set, raise a floor, lower the effort. A preset is
code; a new preset name goes with a new version.

Rules, by field (RULES): "cap" (the config's number is at most the preset's), "floor" (at least the preset's), "subset"
(the config's list is inside the preset's), "superset" (the config's list keeps every item of the preset's and may
add more: the held-out business lines), "range" (the config's [lo, hi] is inside the preset's), "effort" (at most
the preset's by the effort order), "bytes" (a size like "512m", at most the preset's), "fixed" (equal to the preset's),
"free" (the config chooses; the preset's value is only a default). A field that has no rule is not a field of K.

The numbers of `automationbench_v1` come from evidence (docs/design/HARNESS.md §3; the v6 preset's measurements):
the seed is 53 files and 601 KB, 499 KB of it Stirrup; the worker's peak RSS was 80 MB; R's sessions used at most 13
turns and 15 edits; the gate is v0.4's (pooled control, one confirmation, a regression is a drop of 0.5, at least 25
tasks) without a cost term (PLAN P6). `automationbench_v2` is v1.2's (the library off; the improver's v1.2 section).
"""
from __future__ import annotations

import json

from ..harness.contract import SITES
from ..harness.kinds import KINDS
from ..harness.package import DEFAULT_CONFIG_RANGES
from ..improver.settings import RULES as IMPROVER_RULES, preset_table as improver_preset
from ..memory.settings import preset_table as memory_preset

THREAD_ENV = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}

AUTOMATIONBENCH_V1 = {
    "bench": {"domain": None, "domains": None, "upstream_path": None, "toolset": "api",
              "ratios": {"train": 0.5, "dev": 0.25, "test": 0.25},
              "seed": 0, "split_rule": "stratified", "frozen_splits": {}, "families": {},
              "near_duplicate_threshold": 0.6, "smoke_tasks": 1, "concurrency": 8},
    "limits": {"max_steps": 50, "max_model_calls": 200, "max_tool_calls": 200, "max_seconds": 900,
               "max_tokens": 400000, "max_output_chars": 20000},
    "model": {"type": "claude_code", "model": None, "effort": "medium", "max_tokens": 32000, "timeout": 600},
    "research_model": {"type": "claude_code", "model": None, "effort": "medium", "max_tokens": 32000, "timeout": 900},
    "pricing": {"input_per_1k": None, "output_per_1k": None, "cache_read_per_1k": None, "cache_write_per_1k": None,
                "max_input_tokens": None, "max_output_tokens": None, "hidden_fallback": False, "source": ""},
    "harness": {"seed": "stirrup", "kinds": list(KINDS), "sites": list(SITES),
                "config_ranges": {k: list(v) for k, v in DEFAULT_CONFIG_RANGES.items()},
                "limits": {"max_files": 96, "max_package_bytes": 786432, "max_vendor_bytes": 655360,
                           "max_memory_bytes": 262144, "max_prompt_chars": 8000, "max_skill_chars": 8000,
                           "max_skills": 32, "max_tools": 16, "max_hooks": 12, "max_tool_description": 1024,
                           "max_schema_depth": 16, "max_tests": 24}},
    "worker": {"image": "ouroloop-worker:py3.12", "image_id": None, "cpus": "1", "memory": "512m",
               "memory_swap": "512m", "pids": 128, "tmpfs_mb": 256, "network": "none", "env": dict(THREAD_ENV),
               "request_seconds": 30.0, "max_output_chars": 20000},
    "memory": memory_preset(),             # memory/settings.py: v6's calibrated values, the sidecar's pins
    "improver": improver_preset(),         # improver/settings.py: the priors from what was measured
    "gate": {"min_gain": 0.05, "min_groups": 5, "min_tasks": 25, "max_regression_rate": 0.1, "regression_drop": 0.5,
             "confirmations": 1, "control_pooling": "pooled", "control_readings": 2, "repeats": 1, "order_seed": 0,
             "missing_cost_policy": "refuse", "max_infrastructure_retries": 1,
             # EVALUATE §5.1: the partial-credit rule beside the strict one (finance-v2: 25 dev tasks resolve the
             # strict rate in steps of 4 points; the partial credit is continuous)
             "partial_min_gain": 0.03},
    # IMPROVER §3 (v1.2): up to 12 rounds; the patience counts rounds in a row without a held-out acceptance
    # v1.3 (DESIGN §6.13): the stream's batch (a round every this many scored tasks) and the splits it runs, in order
    "schedule": {"rounds": 12, "patience": 3, "max_infrastructure_failures": 2, "stream_batch": 30,
                 "stream_splits": ["train"]},
    "budget": {"usd": None, "final_usd": None},
    # v1.4 (DESIGN §6.14): a cross-domain activity's final runs on these domains' test tasks (None: every domain)
    "final": {"tasks": "test", "repeats": 3, "claims": ["C1"], "domains": None},
}

# v1.2 (DESIGN §6.12, MEMORY §9): the search loop redone — the library off by default, one package a round, held-out
# acceptance; otherwise automationbench_v1's numbers. A config may turn the memory on ([memory] enabled is free).
AUTOMATIONBENCH_V2 = json.loads(json.dumps(AUTOMATIONBENCH_V1))
AUTOMATIONBENCH_V2["memory"]["enabled"] = False

# 2026-10-24: the same preset with the effort ceiling at "max" for both the actor and the improver — the user's
# experiment (finance-v24) on whether the mechanism reads better at max; every arm of an activity still runs one
# effort. The default presets keep "medium" as the ceiling.
AUTOMATIONBENCH_V2_MAX = json.loads(json.dumps(AUTOMATIONBENCH_V2))
AUTOMATIONBENCH_V2_MAX["model"]["effort"] = "max"
AUTOMATIONBENCH_V2_MAX["research_model"]["effort"] = "max"
# at max the model thinks longer: finance-v24's improver lost an accepted candidate when two replies in a row hit the
# 32,000-token output cap; the ceiling doubles here ([pricing] max_output_tokens must cover 5 x max_tokens)
AUTOMATIONBENCH_V2_MAX["model"]["max_tokens"] = 64000
AUTOMATIONBENCH_V2_MAX["research_model"]["max_tokens"] = 64000
PRESETS = {"automationbench_v1": AUTOMATIONBENCH_V1, "automationbench_v2": AUTOMATIONBENCH_V2,
           "automationbench_v2_max": AUTOMATIONBENCH_V2_MAX}

RULES = {
    "bench": {"domain": "free", "domains": "free", "upstream_path": "free", "toolset": "fixed", "ratios": "free",
              "seed": "free",
              "split_rule": "free", "frozen_splits": "free", "families": "free", "near_duplicate_threshold": "free",
              "smoke_tasks": "free", "concurrency": "free"},
    "limits": {k: "cap" for k in AUTOMATIONBENCH_V1["limits"]},
    "model": {"type": "fixed", "model": "free", "effort": "effort", "max_tokens": "cap", "timeout": "cap"},
    "research_model": {"type": "fixed", "model": "free", "effort": "effort", "max_tokens": "cap", "timeout": "cap"},
    "pricing": {k: "free" for k in AUTOMATIONBENCH_V1["pricing"]},
    "harness": {"seed": "fixed", "kinds": "subset", "sites": "subset", "config_ranges": "ranges",
                "limits": {k: "cap" for k in AUTOMATIONBENCH_V1["harness"]["limits"]}},
    "worker": {"image": "fixed", "image_id": "free", "cpus": "cap", "memory": "bytes", "memory_swap": "bytes",
               "pids": "cap", "tmpfs_mb": "cap", "network": "network", "env": "free", "request_seconds": "cap",
               "max_output_chars": "cap"},
    "memory": {"enabled": "free", "shown_k": "cap", "recall_k": "cap", "fetch_factor": "fixed", "min_score": "floor",
               "sibling_sim": "cap", "batch_dedup_threshold": "fixed", "store_dedup_threshold": "fixed",
               "store_dedup_content_threshold": "fixed", "validation_threshold": "floor", "success_threshold": "fixed",
               "origins": "subset", "max_active": "cap", "probation_shown": "fixed", "retire_harm_tasks": "fixed",
               "retire_harm_min_shown": "fixed", "retire_min_shown": "fixed", "retire_mean_credit": "fixed",
               "min_other_tasks": "floor", "when_chars": "cap", "content_chars": "cap", "traj_tool_chars": "cap",
               "traj_total_chars": "cap", "stream_batch": "fixed", "cite": "fixed", "retry": "free", "abort_d": "floor",
               "heldout_domains": "superset", "imports": "free", "llm_timeout_s": "cap", "promote_fixes": "floor",
               "promote_sources": "floor", "retriever": "fixed", "backend": "fixed"},
    "improver": dict(IMPROVER_RULES),
    "gate": {"min_gain": "floor", "min_groups": "floor", "min_tasks": "floor", "max_regression_rate": "cap",
             "regression_drop": "cap", "confirmations": "floor", "control_pooling": "fixed",
             "control_readings": "floor", "repeats": "floor", "order_seed": "free", "missing_cost_policy": "fixed",
             "max_infrastructure_retries": "cap", "partial_min_gain": "floor"},
    "schedule": {"rounds": "cap", "patience": "cap", "max_infrastructure_failures": "cap", "stream_batch": "cap",
                 "stream_splits": "free"},
    "budget": {"usd": "free", "final_usd": "free"},
    "final": {"tasks": "fixed", "repeats": "floor", "claims": "free", "domains": "free"},
}


def preset(name: str) -> dict:
    if name not in PRESETS:
        raise KeyError(f"no preset {name!r}: {sorted(PRESETS)}")
    return json.loads(json.dumps(PRESETS[name]))
