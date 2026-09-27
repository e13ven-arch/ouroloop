# ouroloop

English | [中文](README.zh-CN.md)

A lightweight RSI framework built around learnable decision policies. It is designed to improve both how an agent performs tasks and how the system diagnoses failures, designs experiments and develops its next version.

The core research direction is to learn small, reusable decision models from execution feedback. The v0.5 design gives prompts, rules, workflows and models a common policy interface, with training used when it adds value. Task and research policies will share evidence, evaluation and version management. A learned model must justify its added task-quality or cost benefit.

AutomationBench Marketing is the first proving ground, evaluated against leading models and the same base agent before improvement. The complete v0.5 target also includes evaluating revised research policies and using them in subsequent improvement rounds. This recursive path is planned, not yet implemented.

**Status:** the decision and training infrastructure exists, and so does the AutomationBench run contract: task groups frozen before the first result, the official scorer over the world the agent's tools left, and the checks that say the runner measures what upstream measures (`ouroloop bench --phase smoke`, roadmap M1). It has been verified against a real checkout (upstream `4a8e106`, package 1.0.6): all 25 development tasks pass the contract checks, and upstream's own rubric entry point, service gate and tool schemas agree with this adapter's. It has **not** been run with a real model, so **there are no benchmark results here**. The first feedback path is implemented: a completion probe labels each stop decision, offline, with whether the task was actually finished at that moment. The task-level promotion loop is not. See the [v0.5 design](docs/DESIGN.md) and [implementation roadmap](docs/ROADMAP.md). The runnable examples below exercise the current mechanisms; they do not demonstrate business benchmark gains.

An agent makes many small judgment calls: retry this command? keep this output in context? run this without asking? is the task done? ouroloop sends each of them to a calibrated decision model as a typed question (yes/no, a choice, or a score) and gets probabilities back in one forward pass. When the model is not confident, or its point is not switched on yet, the point takes its declared default: a fixed action, a rule, an LLM or a person. Every call, and what actually happened afterwards, goes into a local ledger.

Outcomes with a justified action label can become supervised training examples. A final task reward alone does not label every intermediate decision. `ouroloop evolve` trains from labelled data; the separate research loop can propose decision-spec, recipe and harness-prompt candidates. Connecting research, business feedback, training and complete task evaluation is the next implementation step.

The decision backend can be the Jev API or a small model you train yourself, such as TDE: start with Jev, and let your own model take over decision points one by one as data accumulates. Answers from Jev and from closed LLMs are never training labels: labels come from observed outcomes and from people.

Small models are an empirical choice for recurring decisions with useful feedback. Clear rules and effective prompts remain valid alternatives; a small answer space does not imply an easy reasoning problem.

## How the loop improves itself

`ouroloop demo <scenario> --generations N` runs this loop generation by generation ([ouroloop/generations.py](ouroloop/generations.py)); a harness of your own runs the online half on every session. Only the demo plans each round (`ouroloop plan` shows the same reading): a round trains only what a plan's admission passed, so a round with nothing new to learn from spends no GPU. Each `ouroloop evolve` call runs one round (the training job, calibration, the gate, promotion) whatever the plan would say.

<p align="center"><img src="docs/assets/architecture.gif" alt="ouroloop architecture: a loop whose top half runs every session and whose bottom half runs every round" width="100%"></p>

The diagram illustrates the feedback-loop design, not a measured business result. Current scenario runs collect decisions and labels, apply the built-in planner, train and evaluate candidates, and use the selected release in later sessions. The automatic researcher and complete business-task evaluator still need to be connected.

An updated task policy changes the states encountered in later sessions; evidence from those sessions informs the next proposal. The complete design adds a second path: update a research policy, evaluate its ability to produce useful task improvements under the same budget, and use the accepted version for later research. Updating a prompt, rule or research tool can close this path without training a separate researcher model. Neither path guarantees continual improvement.

The existing takeover policy uses calibrated thresholds, labelled-session risk checks and runtime revocation. These controls depend on their feedback and statistical assumptions; they do not establish a 5% business-task failure bound. The planned benchmark profile will evaluate complete task outcomes under a separately fixed protocol while preserving the existing deployment profile.

`ouroloop research` currently runs separately from the scenario generation loop. Its integration with task feedback and training is tracked in the roadmap.

## Quick start

Python 3.11 or newer, on macOS or Linux (the agent's tools use bash; on Windows, use WSL).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"      # the only runtime dependency is httpx
pytest -q                    # offline: no network, no models
ouroloop demo                # one research round on synthetic data, offline
ouroloop demo snake --config examples/snake/offline.toml   # Snake, several generations, a tiny model (examples/snake)
```

To use Jev, put `TYPESAFE_API_KEY=...` in `.env` at the repository root (git ignores it).

```bash
ouroloop smoke                   # ask Jev one question of each type
ouroloop demo --judge jev        # let Jev make the research loop's judgments
ouroloop decide --backend jev --type noul --question "The failure is transient" --state "ConnectionResetError"
pytest -m live                   # smoke tests against the real API
```

## Run the agent

The built-in agent is minimal: an LLM with read, write, edit and bash, plus five decision points around it (`route`, `tool_gate`, `retry`, `compact`, `stop`).

```bash
pip install -e ".[anthropic]"    # for Claude; set ANTHROPIC_API_KEY
ouroloop run "fix the failing tests" --llm anthropic --judge jev
ouroloop run "..." --llm openai_compat --model qwen3 --base-url http://localhost:11434/v1
ouroloop run "..." --llm anthropic --verify "pytest -q" --small-model claude-haiku-4-5
```

- Tool calls ask for your approval (y/N) unless you pass `--yes`. Commands on the deny list (`rm -rf /`, `curl ... | sh` and the like) never run.
- `--verify` takes a command that exits 0 when the task is done. When the agent wants to stop, ouroloop runs it first and sends the output back if it fails (at most twice). The result also labels the `stop` decision.
- `--small-model` adds a cheaper tier; the `route` decision point picks the tier for each step once it acts; until then every step uses the large tier. The task outcome is a weak label for small-tier steps.
- In an interactive run, ouroloop asks at the end whether the task was done, and records the answer.
- `ouroloop hook print-settings` prints Claude Code hooks that record tool calls and their results from your normal Claude Code sessions into the ledger, without changing Claude Code's behaviour.

## Models

The research agent and `ouroloop run` work with any model:

- Claude through the official SDK: `[llm] type = "anthropic"` (default `claude-opus-5`, with prompt caching).
- Anything with an OpenAI-compatible API: `type = "openai_compat"` (Zhipu GLM, OpenRouter, DeepSeek, vLLM, Ollama and others), with retries on rate limits and server errors. Provider-specific request fields go in `[llm.extra]`, for example GLM's reasoning effort. See [examples/zhipu.toml](examples/zhipu.toml).
- A config's `env_file` points at a `.env` relative to the config, so keys are found from any directory.

## Research and evolution

```bash
ouroloop gate freeze --config examples/ouroloop.toml           # once, by a person: the gate every promotion goes through
ouroloop research --config examples/ouroloop.toml --rounds 3   # research rounds over the ledger
ouroloop plan --config examples/ouroloop.toml                  # what the data says, and whether a round should train
ouroloop evolve --config examples/ouroloop.toml                # train a new decision model; promote only if it passes the gate
ouroloop calibrate --config examples/ouroloop.toml             # report each point's temperature and thresholds
ouroloop models --dir examples/workspace                       # model lineage; --rollback to go back (and revoke)
ouroloop ledger --dir examples/workspace                       # decisions, label coverage and agreement per point
```

The gate template can tighten, never loosen, the 5% risk, the 30 fresh sessions each gate needs and the bootstrap. Each round's gate record, its use and its verdict are events in the ledger; `gates/` holds copies. Only what a gate record accepted can be promoted, answer or act.

About TDE: TDE is a separate project that will be released on its own. The `tde` trainer records how ouroloop trains it: as a job (`ouroloop/worker.py`) on this machine, or on a GPU host over ssh with `host = "user@box"`, in a new directory `<jobs_root>/<job id>/` there. The job publishes its run to the model store, `[store]`, and the model it yields is a ref: the job id with `type = "dir"` (a directory, on the training host when there is one), or `<repo>@<commit>` with `type = "hf"`, which commits the run once to a private Hugging Face repository. `[serving]` (formerly `[models]`) says how refs are asked: `url` (and `token_env`) for `ouroloop serve` on a host that can read the store, or in-process from the store. A model the gate accepts is deployed before it becomes the champion (`ouroloop deploy <version>` retries a failed deploy). Control plane, training, store and serving are four roles; one GPU host that holds three of them is a layout (docs/TRAINING_HOSTS.md). Without TDE, the whole pipeline runs with the built-in signature model (`type = "signature"`). To bring your own model, write a trainer plugin (`ouroloop/trainers/__init__.py`, docs/TRAINING_HOSTS.md §1): `train` runs as the job on the training machine, `backend(run, ref, options)` loads the model for serving; name it in the config as `type = "your.module"`. A `type = "your.module:YourTrainer"` trainer trains in this process and its backend is built here, outside the store and `[serving]`: for tests and demos.

To serve a model for harnesses on other machines, run the decision service where the model lives and point an `http` backend at it. Binding to anything other than localhost requires a token, because `/outcome` writes training labels into the ledger.

```bash
OUROLOOP_TOKEN=... ouroloop serve --config host.toml --host 0.0.0.0 --port 8765
```

## Task suites and prompt evolution

[evals/tasks](evals/tasks) holds 10 small coding tasks: 7 for development and 3 held out. Each task is one TOML file with a prompt, starting files, check commands, and hidden tests that are written only after the agent finishes, so the agent cannot edit them. A run scores the share of checks that pass.

```bash
ouroloop suite --validate --tasks evals/tasks               # no LLM: every task fails as given and passes with its solution
ouroloop suite --config examples/zhipu.toml                 # run the suite with the champion prompt
ouroloop research --config examples/zhipu.toml --rounds 2   # with a [suite], the research agent also improves the harness prompt
```

A prompt candidate is evaluated in steps:

- a change that only touches whitespace is rejected without running anything;
- it runs on a few development tasks first, and only if it does no worse than the champion do the champion and the candidate both run again on every task, held-out ones included, for the promotion gate;
- the research agent never sees held-out results;
- each run is recorded once per (prompt, model, task, repeat) in `harness/runs.jsonl`; recorded runs serve the first step only, and a round gates once, so re-running a prompt until it gets lucky is not possible.

The checks at the end of a task also label that run's `stop` and `route` decisions. Suite runs execute bash automatically, in a temporary directory with the deny list in force; use a container or a VM if you do not trust the model.

## AutomationBench

[AutomationBench](https://github.com/zapier/AutomationBench) simulates business workflows across 47 SaaS tools and
checks whether the agent left the world in the correct state. It is a separate runtime that ouroloop imports
lazily; clone it and point `[bench] upstream_path` at the checkout
([examples/automationbench/README.md](examples/automationbench/README.md) lists what to install).

```bash
ouroloop bench --config examples/automationbench/marketing.toml --phase smoke --checks-only   # no model called
ouroloop bench --config examples/automationbench/marketing.toml --phase smoke                 # and a few dev tasks
```

The smoke phase reports which upstream this is, freezes the train/dev/test task groups into
`<workspace>/bench/<domain>/splits.json` before any result is looked at, checks this adapter against upstream's own
rubric entry point, service gate and tool schemas, audits that nothing grading-related reaches the agent, and then
attempts a couple of development tasks end to end. It makes no claim about capability, and a local score on the
public task set is not an official leaderboard score — the leaderboard runs a private set.

The boundaries are structural rather than a matter of care: the agent is handed the task prompt and the tool
schemas while the assertions and the seeded state live in an object it never receives, and the score comes from
the world the tools left, so a run that writes the wrong record and then reports success scores what it wrote.
Baseline, frontier-model comparison, automated collection and paired task evaluation are M2 to M6 in the roadmap;
their phase names are refused until they exist. The offline completion probe already exists; M7 adds the planned
research-policy improvement path.

## Code layout

| Path | What it holds |
|---|---|
| `ouroloop/decision.py` | decision points and specs (question, candidates, view parameters) |
| `ouroloop/policy.py` | the execution rule, threshold fitting, exploration and propensities |
| `ouroloop/ledger.py` | the ledger, raw contexts, champion specs, redaction |
| `ouroloop/runtime.py` | ties decision points, backends, default answerers and the ledger together; fixes each session's release |
| `ouroloop/release.py` | the release a session runs with (model, decision spec, harness) and the revocation list |
| `ouroloop/agent/` | the minimal agent: loop, four tools, harness prompt, task suites |
| `ouroloop/points/` | built-in points `retry`, `compact`, `tool_gate`, `stop`, `route`, and the deny list |
| `ouroloop/chat.py` | a provider-neutral chat format |
| `ouroloop/providers/` | Anthropic (official SDK) and OpenAI-compatible APIs |
| `ouroloop/backends/` | decision backends: `mock`, `jev`, `tde`, `llm`, `http` |
| `ouroloop/research/` | question conversion, the research agent interface and default, research rounds |
| `ouroloop/evolve/` | dataset building, data checks, trainers and their training jobs (worker, compute), calibration, the frozen promotion gate and its records, model registry, prompt candidates, experience records |
| `ouroloop/serve.py` | the HTTP decision service |
| `ouroloop/benchmark.py` | the benchmark run contract: frozen task groups, run identity, one attempt's result |
| `ouroloop/bench.py` | the phases of `ouroloop bench` |
| `ouroloop/adapters/` | AutomationBench (tasks, business tools, the official scorer); Claude Code hooks (an optional data source) |

## Status

The current implementation provides decision points, the ledger, supervised training jobs, trainer plugins, model storage and serving, calibration, version management, a scenario generation loop, and a separate research/prompt candidate path.

Design v0.5 prioritizes AutomationBench Marketing. The adapter, the task-group splits and the full-task runner now
exist; the contract checks pass against a real checkout, and the tests run against a miniature stand-in for it
(`tests/fake_upstream`). The train-only completion probe is also implemented, but nothing has been run with a real model.
Missing work includes useful training data from real runs, paired task evaluation and promotion, the integrated
researcher with evidence-based experience, research-policy revision and reuse, and reproducible comparisons with leading models. **No
AutomationBench performance has been measured in this repository.** Existing evidence remains in
[docs/JOURNAL.md](docs/JOURNAL.md).

Implementation handoff: [architecture review](docs/RSI_REVIEW.md), [module specifications and acceptance cases](docs/modules/README.md). These describe planned work, not implemented benchmark gains.

The design docs are in Chinese for now: [docs/DESIGN.md](docs/DESIGN.md) (the authoritative design, its invariants and the settled trade-offs), [docs/ROADMAP.md](docs/ROADMAP.md) (what is built, what is next, and the deferred items), [docs/RESEARCH_AGENT.md](docs/RESEARCH_AGENT.md) (the researcher: what it reads, the experiment it writes, its boundaries), [docs/DEMO.md](docs/DEMO.md) (the interactive demo and the video) and [docs/JOURNAL.md](docs/JOURNAL.md) (what changed, with the evidence).

## License

MIT, see [LICENSE](LICENSE).
