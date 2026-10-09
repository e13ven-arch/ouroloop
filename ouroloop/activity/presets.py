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
tasks) without a cost term (PLAN P6).
"""
from __future__ import annotations

import json

from ..harness.contract import SITES
from ..harness.kinds import KINDS
from ..harness.package import DEFAULT_CONFIG_RANGES
from ..memory.settings import preset_table as memory_preset

THREAD_ENV = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}

AUTOMATIONBENCH_V1 = {
    "bench": {"domain": None, "upstream_path": None, "toolset": "api", "ratios": {"train": 0.5, "dev": 0.25,
                                                                                    "test": 0.25},
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
    "improver": {"k": 3, "session_max_turns": 40, "session_max_edits": 32, "session_max_read_bytes": 65536,
                 "session_max_read_total": 1048576, "diagnose_max_cases": 60, "explore_slots": 1, "l5_slots": 1,
                 "screen_passing_sample": 6, "screen_min_net_flips": 0, "screen_min_fixes": 1,
                 "screen_max_sample_regressions": 1, "round_share": 0.25},
    "gate": {"min_gain": 0.05, "min_groups": 5, "min_tasks": 25, "max_regression_rate": 0.1, "regression_drop": 0.5,
             "confirmations": 1, "control_pooling": "pooled", "control_readings": 2, "repeats": 1, "order_seed": 0,
             "missing_cost_policy": "refuse", "max_infrastructure_retries": 1},
    "schedule": {"rounds": 3, "patience": 2, "max_infrastructure_failures": 2},
    "budget": {"usd": None, "final_usd": None},
    "final": {"tasks": "test", "repeats": 3, "claims": ["C1"]},
}

PRESETS = {"automationbench_v1": AUTOMATIONBENCH_V1}

RULES = {
    "bench": {"domain": "free", "upstream_path": "free", "toolset": "fixed", "ratios": "free", "seed": "free",
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
    "improver": {"k": "cap", "session_max_turns": "cap", "session_max_edits": "cap", "session_max_read_bytes": "cap",
                 "session_max_read_total": "cap", "diagnose_max_cases": "cap", "explore_slots": "cap",
                 "l5_slots": "cap", "screen_passing_sample": "cap", "screen_min_net_flips": "floor",
                 "screen_min_fixes": "floor", "screen_max_sample_regressions": "cap", "round_share": "cap"},
    "gate": {"min_gain": "floor", "min_groups": "floor", "min_tasks": "floor", "max_regression_rate": "cap",
             "regression_drop": "cap", "confirmations": "floor", "control_pooling": "fixed",
             "control_readings": "floor", "repeats": "floor", "order_seed": "free", "missing_cost_policy": "fixed",
             "max_infrastructure_retries": "cap"},
    "schedule": {"rounds": "cap", "patience": "cap", "max_infrastructure_failures": "cap"},
    "budget": {"usd": "free", "final_usd": "free"},
    "final": {"tasks": "fixed", "repeats": "floor", "claims": "free"},
}


def preset(name: str) -> dict:
    if name not in PRESETS:
        raise KeyError(f"no preset {name!r}: {sorted(PRESETS)}")
    return json.loads(json.dumps(PRESETS[name]))
