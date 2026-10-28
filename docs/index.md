---
title: "reharness — a self-improving agent harness (bounded RSI)"
description: "A self-improving agent rewrites its own harness, not its model, and proves each change live. Open source, measured on AutomationBench."
---

# reharness

**A self-improving agent rewrites its own harness, not its model, and proves each change live.**

reharness is an open-source **self-improving agent harness**: a bounded form of **recursive self-improvement (RSI)** in which an LLM agent's own model revises the **harness** the agent runs in — its tools, hooks, skills and prompts — from the agent's own returned runs. Every change is proved by a **Live Probe** (the candidate runs the same request for real) and adopted only for that task. The session is never shown an answer.

- Claude Opus 5.5 scores 42.47% on the AutomationBench leaderboard; with reharness, on the benchmark's held-out finance tasks, it scores 68% at steady state. Across sealed finals the loop's harness gained up to +13 points over the same model without it, and no passing task was lost.
- What the agent learns is harness engineering — a guard, a tool, a prompt — not an answer.

[Source and README on GitHub](https://github.com/e13ven-arch/reharness) · [Results record](https://github.com/e13ven-arch/reharness/blob/main/docs/RESULTS.md) · [中文 README](https://github.com/e13ven-arch/reharness/blob/main/README.zh-CN.md)

![Components and architecture](assets/architecture.svg)

![How one improvement happens](assets/one-improvement.svg)

## Cite

```bibtex
@software{reharness2026,
  title  = {reharness: a self-improving agent rewrites its own harness, not its model, and proves each change live},
  author = {The reharness Authors},
  year   = {2026},
  url    = {https://github.com/e13ven-arch/reharness}
}
```
