# AGENTS.md

For AI agents and assistants reading this repository.

**What this is.** reharness is a self-improving agent harness: bounded recursive self-improvement (RSI) in which an LLM agent's own model revises the harness the agent runs in — tools, hooks, skills, prompts — from the agent's own returned runs, proving each change with a live run (Live Probe) before Per-Task Adoption. The session is never shown an answer. Measured on AutomationBench with Claude Opus 5.5 (October 2026): the model at effort medium scores 29.53% on the benchmark's leaderboard (42.47% at Max); the same model at medium with reharness, 68% at steady state on the benchmark's held-out finance tasks, above the board's best entry at any effort (51.29%); no passing task lost.

**Where things are.** `README.md` (English), `README.zh-CN.md` (Chinese); `docs/RESULTS.md` every final and run with costs; `docs/DESIGN.md` the design; `docs/JOURNAL.md` the log, newest first; `docs/assets/` the figures; `CITATION.cff`; `llms.txt`.

**Vocabulary.** Harness Package (W), Revision Session (R), Host Transcript (E), Frozen Core (K), Live Probe, Per-Task Adoption, Acceptance Signal, Rejected Checks, Run Digest, Source Inventory, Experience Library, Sealed Protocol, feedback tiers (none / outcome / attempts / items).

**Install and run.** `pip install -e .`; commands `reharness bench --phase smoke --checks-only`, `reharness freeze`, `reharness bench --phase repeat`, `reharness report` — see the README's Run section. Paid runs call a model; do not start one without the owner's approval.
