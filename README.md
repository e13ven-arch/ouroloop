<div align="center">

# reharness

**A self-improving agent rewrites its own harness, not its model, and proves each change live.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Benchmark: AutomationBench](https://img.shields.io/badge/benchmark-AutomationBench-555.svg)](https://github.com/zapier/AutomationBench)
[![AutomationBench finance: 68% with reharness](https://img.shields.io/badge/AutomationBench%20finance-68%25%20with%20reharness-2e7d32.svg)](#results)
[![中文](https://img.shields.io/badge/README-%E4%B8%AD%E6%96%87-lightgrey.svg)](README.zh-CN.md)

</div>

reharness is a self-improving agent harness: bounded recursive self-improvement in which the agent's own model revises the harness the agent runs in, never the model. A business agent runs requests that repeat. When a request comes back returned, a **Revision Session** reads the agent's own run of it, changes the agent's **Harness Package** — its tools, hooks, skills, prompts — and proves the change with a **Live Probe**: the candidate runs the same request for real. Only a probe that comes back accepted is adopted, and only for that request (**Per-Task Adoption**). No person picks the change, no model judges it, and the session is never shown an answer: at most *accepted or returned* per attempt, and *which check failed*. Everything the session cannot touch is a **Frozen Core** — the scorer, what the session may be told, the package's contract, the bounds — and package code runs in an isolated worker. What the agent learns from its own runs is harness engineering — a guard, a tool, a prompt — not an answer.

Built and measured on [AutomationBench](https://github.com/zapier/AutomationBench) (47 simulated SaaS tools, scored on the world state the agent leaves behind) with Claude Opus 5.5 as both the agent and the session, on a subscription budget. As of October 2026, Claude Opus 5.5 at effort medium scores 29.53% on the AutomationBench leaderboard; the same model at the same effort, with reharness, scores 68% at steady state on the benchmark's held-out finance tasks — above the top of the leaderboard at any effort — with no passing task lost; on train tasks a session learned, with no hint in its prompt, to open every unread source before writing and to derive figures by code.

<p align="center"><img src="docs/assets/architecture.svg" alt="Components and architecture: the Frozen Core K (Package Contract, Host-Side Scorer, Feedback Tier, Bounds) over the Harness Package W as W₀ and one W_t per task, the Isolated Worker as a boundary around the Host Transcript E, the Revision Session R with its admissibility gate and Live Probe, and Per-Task Adoption applied by the host" width="100%"></p>

## Contents

- [Key ideas](#key-ideas)
- [Architecture](#architecture)
- [How one improvement happens](#how-one-improvement-happens)
- [Feedback tiers](#feedback-tiers)
- [Experiment design](#experiment-design)
- [Results](#results)
- [Open problems](#open-problems--where-to-contribute)
- [Install](#install)
- [Run](#run)
- [Repository layout](#repository-layout)
- [FAQ](#faq)
- [Contributing](#contributing)
- [Citation](#citation)
- [License](#license)

## Key ideas

- **Per-Task Adoption by an accepted Live Probe.** The session's candidate runs the request for real and the verdict that comes back decides; the host applies the rule, no judge model, no person. An accepted task opens no session and its package is never revised; adoption binds to one task, so a change proved on one request reaches no other. Tasks influence each other only through an Experience Library of earlier adoptions.
- **A typed, bounded learning signal.** What a session may learn is a field of the Sealed Protocol, the feedback tier: the Acceptance Signal alone (accepted or returned, once per attempt), or the signal plus the Rejected Checks' kinds and objects (which message, which row — never a value), plus a Source Inventory of the world's data sources as counts. The grading rows never leave the host.
- **The session learns habits, not answers.** Given that signal it built the same two mechanisms in independent sessions, with none of the words in its prompt: a guard that holds the first write until every unread source is opened, and a tool that derives a figure from the inputs read. Every calculation tool in an adopted package was written by a session; the seed has none.
- **A Frozen Core of four invariants.** The Host-Side Scorer, whose grading rows never leave the host; the Feedback Tier, which fixes what crosses to the session; the Package Contract — a fixed shape, byte, count and effort limits, and no answer in the package: a candidate's changed lines are scanned against the train grading rows and a value that belongs to one task is refused; and the Bounds on probes, steps, tokens, time and money, which no session can raise. Around the loop sits the measurement, not the mechanism: a Family Split by task family, protocols sealed and committed before the first test attempt, Integrity Pins on the worker image and the code, a Budget Guard that reserves every model call at its worst case before it is made.

## Architecture

The system is S = (W, R, E; K): the Harness Package **W** a session may change, the Revision Session **R** that changes it, the Host Transcript **E** it is judged from, and the Frozen Core **K** that bounds all three.

| Component | What it is | Where |
|---|---|---|
| **K** Frozen Core | four invariants — the Host-Side Scorer and its grading rows, which never leave the host; the Feedback Tier; the Package Contract, with the one-task literal scan; the Bounds — held in a preset a config may only tighten, frozen with the split hash, the worker image id, two code hashes and the CLI version (the Integrity Pins), the Sealed Protocol and the Budget Guard | `reharness/activity/presets.py`, `checks.py`; `reharness/evaluate/repeat.py`, `budget.py` |
| **W** Harness Package | a directory of fixed shape, every file content-addressed: the agent loop (Stirrup, vendored under a provenance file behind one adapter), tools, hooks at six sites, prompts, skills, config, tests, one manifest; shape, byte and count limits and the effort ceiling come from K | `reharness/harness/` |
| **R** Revision Session | one bounded coding session per returned task: reads the Run Digest, the Source Inventory, the Rejected Checks and the Experience Library; edits a copy of the package; the host admits a candidate deterministically before each probe; the Live Probe (`try_task`) runs it for real; at most four probes | `reharness/improver/repeat.py`, `prompts/repeat.md` |
| **E** Host Transcript | the host-written record of every attempt (calls, hooks, writes, marks), the budget book, the events; the package can only add lines marked as its own | `reharness/runtime/trace.py`, `records.py` |
| Isolated Worker | one attempt: a Docker container pinned by image id (no network, read-only root, unprivileged user, a standard-library runner whose sha256 the host checks on every load); a Metered Gateway to the model; a World Broker with a deny table to the business tools; hooks get it read-only | `reharness/runtime/` |
| Adapter | the benchmark's tasks, tools and official scorer; the views the loop needs — the Rejected Checks as kinds and identifiers, the Source Inventory as counts | `reharness/adapters/automationbench.py` |

The seed carries four generic hooks (a ledger, a triage before the first write, a date anchor, a verify on finish) and one tool that writes nothing, `plan_changes`. The hook sites are before and after the model call, before and after a tool call, after a turn, and on finish; a hook answers in a fixed shape (block, modify, feedback, pushback), and a *guard* — a before-tool hook that blocks one call once with its reasons and lets the same call through when sent again — is the cheapest component a session has.

## How one improvement happens

<p align="center"><img src="docs/assets/one-improvement.svg" alt="How one improvement happens: every run passes through the Host-Side Scorer; the first pass is accepted (finished, never revised) or returned to a Revision Session, which edits a candidate the host admits before a Live Probe; an accepted probe is adopted for this task by the host, none accepted leaves W_t as it was; later encounters run W_t; the lesson enters the Experience Library, the only channel between tasks" width="100%"></p>

A Sealed Protocol names the tasks and how many times each is met (three). The figure follows one task's package through the steps, which are the same for every task; the worked example beneath it is a quarterly tax estimate, drawn from one real trace (finance-v24, effort max).

1. **First Pass.** Encounter 1 runs the base package. An accepted request is finished: no session, and its package is never revised.
2. **Revision Session.** A returned request gives R one coding session bound to that task. It reads the Run Digest — each call with its route and error flag, the hooks that acted and what they said, the writes by route, the final text; no score — the Source Inventory (each source read by the agent, read only by the session's own hook, or never read), the Rejected Checks the protocol allows, and the Experience Library.
3. **Candidate and admissibility.** R edits a copy of the package; the candidate is the task's package plus the diff, taken as the diff rather than as the session's claim. Before any probe the host decides admissibility deterministically, no model asked: within the Package Contract (shape and limits, the manifest against the diff both ways), no literal of one task (the changed lines scanned against the train grading rows), and changed, since the same tree is never probed twice. The package's own tests and a smoke run in the worker sit at the same point as a fail-fast; they decide nothing.
4. **Live Probe.** The candidate executes the same request once for real. The answer is the new run beside the first, plus the verdict — and, at the items tier, which check that run failed. An unchanged candidate is refused; a changed one may be probed again; four probes per task.
5. **Per-Task Adoption.** The last candidate whose probe was accepted is adopted for this task; none accepted, the base stays. An accepted probe the session never handed in is declared by the host from the diff and stands. The adopted tree and its lesson go to the Experience Library.
6. **Steady State.** Encounters 2 and 3 run the task's own package. The report reads encounter 1 (First Pass) against encounter 3 (Steady State), paired by task family with a bootstrap interval.

Tasks run in parallel, a task's encounters in order, and every encounter is on record, so a rerun pays for nothing twice.

## Feedback tiers

<p align="center"><img src="docs/assets/feedback-levels.svg" alt="the four feedback tiers: none, outcome, attempts, items" width="100%"></p>

`feedback` is a field of the Sealed Protocol, so the number reported with an activity says what the session had. The two tiers measured worse — a judge model deciding from the run alone — are kept for comparison.

## Experiment design

- **Benchmark.** AutomationBench's finance domain: 100 public tasks, each a business workflow over simulated SaaS tools (sheets, mail, chat, accounting systems), scored by assertions on the world state after the run; a task passes when all its assertions hold. A local score on the public set is not a leaderboard score.
- **Splits.** Tasks are assigned to train and test per task family by hash (50 / 50) and frozen; the split hash is part of K.
- **Sealing.** A protocol — the tasks, the encounters, the arms, the feedback tier, the base release — is sealed to one file and its reference committed before the first test attempt. A session opens train sessions and the task's own runs, nothing else; the trace store refuses the rest.
- **Arms.** The learn arm adopts per task; the control arm runs the base every time. The reported number is First Pass against Steady State on the learn arm, paired per task and resampled over families; the base's own flip rate stands beside it (10–17% of tasks flip between two runs of one package).
- **Model and cost.** The agent and the session are Claude Opus 5.5 through headless Claude Code, every arm at one effort level (medium unless stated; `automationbench_v2_max` lifts both arms to max). Costs are nominal API-equivalent dollars on a subscription; the Budget Guard reserves every model call at its worst case before it is made.
- **What is disclosed with a number.** The split, the feedback tier, the effort, the sample size, the paired difference with its interval, and the cost.

## Results

**Same model, same effort: Claude Opus 5.5 at medium scores 29.53% on the AutomationBench leaderboard; with reharness it scores 68% at steady state on the benchmark's held-out finance tasks, above the top of the leaderboard at any effort (51.29%).** Every reharness number is a sealed final, the protocol committed before the first test attempt; across the finals the harness the loop built lifted the same model by up to 31% relative, every adopted change held on every later encounter, and not one passing task was lost.

| | Pass rate | Cost |
|---|---|---|
| Claude Opus 5.5 on the AutomationBench leaderboard, effort medium | 29.53% | $0.65 / task |
| Claude Opus 5.5 on the AutomationBench leaderboard, effort Max | 42.47% | $1.44 / task |
| Best entry on the AutomationBench leaderboard, any model, any effort | 51.29% | — |
| Best finance score on the AutomationBench leaderboard | 50.00% | — |
| **Claude Opus 5.5 with reharness — finance, requests that repeat, steady state** | **68%** (first pass 64% → steady state 68%; every adopted change held; zero regressions across 150 encounters) | $184 |
| Claude Opus 5.5 with reharness — finance, one evolved package for the domain | 62.7% (base 54.7% → 62.7%, +15% relative; each attempt 22% cheaper) | $210 |
| Claude Opus 5.5 with reharness — Marketing, one evolved package for the domain | 54.3% (base 41.3% → 54.3%, +31% relative, significant) | $153 |

Leaderboard figures are the board's own (v1.0.6, its private set); every reharness number was produced at effort medium on the public set's held-out split, under a protocol sealed before the first test attempt, with every run's cost on record. Costs are for the whole activity, in API-equivalent dollars.

What the loop learned: with the Acceptance Signal alone it corrected a third of the returned tasks and never touched a passing one; with the Rejected Checks and the Source Inventory it built generic mechanisms by itself — open every unread source before writing, derive figures by code, route a derived value through a tool — in independent sessions with none of those words in its prompt. Every calculation tool in an adopted package was written by a session; the seed has none. Full records, every run: [docs/RESULTS.md](docs/RESULTS.md), [docs/JOURNAL.md](docs/JOURNAL.md).

## Open problems — where to contribute

The loop is deliberately narrow, and the records show exactly where the next gains are. Each of these is a well-scoped contribution with the protocol, the split and the scorer already in place:

- **The items tier on the full held-out set.** The Rejected Checks let a session aim at the failing item; so far they were measured on named train tasks, where sessions built the unread-sources guard and a calculation tool by themselves. Running the tier on the 50 held-out finance tasks is one sealed activity.
- **A world clock.** Some tasks turn on today's date, which the agent is never told. Handing the environment's clock to the agent through the adapter is a small design change with a measurable effect.
- **A richer signal for document ambiguity.** When a task's own documents allow two readings, accepted-or-returned cannot pick one. What signal resolves it without carrying a value is an open design question.
- **A second domain and a second model.** The adapter boundary and the Sealed Protocol are benchmark- and model-agnostic; nothing has been run outside finance and Marketing, or with a session other than Claude Opus 5.5.
- **The noise floor.** Two runs of one package flip 10–17% of tasks; a cheaper confirmation rule than the current probe bound would shorten every session.

## Install

Python 3.11 or newer, macOS or Linux (on Windows, WSL); Docker for the worker.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,automationbench]"        # installs the `reharness` command
pytest -q                                      # offline: no network, no model
docker build --provenance=false -f docker/worker.Dockerfile -t reharness-worker:py3.12 .
```

Clone AutomationBench and point `[bench] upstream_path` at it; [examples/automationbench/README.md](examples/automationbench/README.md) lists what the adapter imports. The agent and the session run through headless Claude Code on this machine's subscription login (`claude auth login`); the startup check refuses a login that is not a subscription.

## Run

The quick start runs the loop on five train tasks in a new workspace; the first two commands call no model.

```bash
reharness bench --config examples/automationbench/quickstart-repeat.toml --phase smoke --checks-only   # $0: splits, scorer contract, input audit, the seed's literals
reharness freeze --config examples/automationbench/quickstart-repeat.toml                              # $0: K, the budget root, the Sealed Protocol; prints the [rsi] lines to add
reharness bench --config examples/automationbench/quickstart-repeat.toml --phase repeat --arm learn    # 5 tasks x 3 encounters, a Revision Session on each returned one: ~$15-25
reharness bench --config examples/automationbench/quickstart-repeat.toml --phase report                # $0
```

| Command | What it does |
|---|---|
| `reharness bench --config <cfg> --phase smoke --checks-only` | upstream identity, the frozen split, scorer contract, input audit, the seed's literal scan |
| `reharness freeze --config <cfg>` | K, the budget root and the Sealed Protocol; prints the `[rsi]` lines to add |
| `reharness bench --config <cfg> --phase repeat --arm learn\|control [--until task-K]` | the stream; the learn arm adopts per returned task, the control arm runs the base every time |
| `reharness bench --config <cfg> --phase report` | the report from the records on disk |

| `[repeat]` option | Behaviour |
|---|---|
| `split = "train" \| "test"` | which split the protocol names; a test protocol is sealed and committed before the first test attempt |
| `tasks = [...]` or `max_tasks = N` | the named tasks, or the first N in the stream's order |
| `encounters = 3` | how many times each task is met |
| `feedback = "attempts" \| "items" \| "outcome" \| "none"` | the feedback tier: what the session is told (see above) |
| `max_probes = 4` | Live Probes per task |
| `arms = ["learn", "control"]`, `concurrency = 4` | the arms to run; tasks in parallel (a machine setting, not in the protocol) |

A config names a preset (`[harness] preset`) and may only tighten it ([docs/design/CONTRACTS.md](docs/design/CONTRACTS.md) §9, [docs/design/EVALUATE.md](docs/design/EVALUATE.md) §6b). `finance-v14.toml` to `finance-v25.toml` are the measured activities; their workspaces hold local, git-ignored run records. For another domain, copy the quick start, change `[bench] domain`, give the copy a new `[workspace] root` and `[rsi] scope_id`, and keep `[repeat] split = "train"` until a test protocol is sealed and committed.

## Repository layout

| Path | What it holds |
|---|---|
| `reharness/activity/` | config and presets to K, the workspace, the phases (`phases.py`: `repeat`), the startup checks, the report, the CLI |
| `reharness/improver/repeat.py`, `prompts/repeat.md` | the per-task loop: the label-free Run Digest, the Live Probe (`try_task`), the Acceptance Signal, the Rejected Checks, the Source Inventory, the Experience Library |
| `reharness/evaluate/` | frozen splits, attempts and results, the sealed protocols (`repeat.py`, `final.py`), the paired bootstrap, the budget book, releases |
| `reharness/harness/` | the typed package: component kinds, manifest, shape and literal checks, the vendored seed (`seeds/stirrup/`) |
| `reharness/runtime/` | one attempt: gateway, broker, the Docker worker and its runner, egress, the trace |
| `reharness/adapters/automationbench.py` | tasks, business tools, the official scorer, the Rejected Checks and Source Inventory views |
| `docs/` | the design (`DESIGN.md`, `design/`), the plan, the results, the journal, the announcement drafts (in Chinese, except this README and the announcement) |
| `reharness/improver/` (the rest), `reharness/memory/`, `reharness/train/` | the earlier held-out screen loop, the memory sidecar (off in every measured preset) and the training stack (never run); kept for the record |

## FAQ

**What is reharness?** reharness is an open-source self-improving agent harness: a bounded form of recursive self-improvement (RSI) in which an LLM agent's own model revises the harness the agent runs in — its tools, hooks, skills and prompts — from the agent's own returned runs, and proves every change with a live run before it is adopted.

**Is this recursive self-improvement?** It is the bounded kind. The agent's model improves the agent's harness; the model's weights, the scorer, the feedback tier, the package contract and the bounds are frozen, and the Revision Session that makes the change is not itself improved by the loop. That is what "bounded RSI" means here.

**What is an agent harness?** Everything around the model that makes it an agent: the agent loop, the tools it may call, the hooks that run before and after model and tool calls, its skills, prompts and config. reharness treats that as one versioned Harness Package with a fixed shape, and the loop changes only that package.

**How is this different from fine-tuning or prompt optimization?** No weights change and no prompt is optimized against a score. A change is a piece of harness engineering — a guard, a tool, a skill — written by a coding session that has read the failing run and is never shown the answer; it is adopted only after a Live Probe runs the same request for real and comes back accepted, and only for that task.

**What does the session learn, then?** Habits, not answers: open every unread source before writing, derive figures by code instead of in the model's head, route a derived value through a tool. Those mechanisms appeared in independent sessions with none of the words in the prompt.

**Can I run it on my own agent and tools?** Yes, through an adapter: the benchmark's tasks, tools and scorer sit behind one adapter, and a real business tool set fits behind the same Package Contract. See Repository layout and CONTRIBUTING.md.

## Contributing

Three kinds of contribution are useful: another adapter — a benchmark or a real business tool set — behind the same Package Contract; a feedback-tier experiment with its Sealed Protocol committed before the first test attempt; a reproduction report with config, split hash, effort and cost. Every measured number comes with the disclosures listed under Experiment design. See [CONTRIBUTING.md](CONTRIBUTING.md); questions go to [Issues](https://github.com/e13ven-arch/reharness/issues).

## Citation

```bibtex
@software{reharness2026,
  title   = {reharness: a self-improving agent rewrites its own harness, not its model, and proves each change live},
  author  = {The reharness Authors},
  year    = {2026},
  url     = {https://github.com/e13ven-arch/reharness},
  note    = {Bounded self-improvement of an agent's harness package, measured on AutomationBench}
}
```

## License

MIT, see [LICENSE](LICENSE). The Stirrup seed is vendored under its own license in `reharness/harness/seeds/stirrup/vendor/stirrup/LICENSE`; AutomationBench keeps its upstream license.
