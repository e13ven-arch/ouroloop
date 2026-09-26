# ouroloop

English | [中文](README.zh-CN.md)

A lightweight RSI framework. Instead of improving the base model, an LLM improves a small decision model, and the decision model improves the agent harness.

An agent makes many small judgment calls: retry this command? keep this output in context? run this without asking? is the task done? ouroloop sends each of them to a calibrated decision model as a typed question (yes/no, a choice, or a score) and gets probabilities back in one forward pass. When the model is not confident, or its point is not switched on yet, the point takes its declared default: a fixed action, a rule, an LLM or a person. Every call, and what actually happened afterwards, goes into a local ledger.

Those outcomes are training labels. `ouroloop evolve` trains a new decision model from them, and a research agent (an LLM that reasons, with a decision model as the judge) reads the evaluation results and proposes changes to decision-point specs, training recipes and the harness prompt. You can swap in your own research agent. Every change has to pass held-out evaluation and the same promotion gate before it replaces the current version.

The decision backend can be the Jev API or a small model you train yourself, such as TDE: start with Jev, and let your own model take over decision points one by one as data accumulates. Answers from Jev and from closed LLMs are never training labels: labels come from observed outcomes and from people.

The premise: commercial LLMs already reason well, and in real work the decisions are what matter most. ouroloop improves decisions, not reasoning.

## How the loop improves itself

`ouroloop demo <scenario> --generations N` runs this loop generation by generation ([ouroloop/generations.py](ouroloop/generations.py)); a harness of your own runs the online half on every session. Only the demo asks the schedule whether to train: each `ouroloop evolve` call runs one round (the training job, calibration, the gate, promotion) whatever the schedule would say.

<p align="center"><img src="docs/assets/architecture.gif" alt="ouroloop architecture: a loop whose top half runs every session and whose bottom half runs every round" width="100%"></p>

Sessions run along the top: the business agent asks the runtime, the environment answers, the ledger records, the monitor watches. When the schedule fires, the research agent tries changes on dev data, the best one trains, and the frozen gate judges it once on fresh sessions: rejected in the first round, accepted in the second, and the lineage grows a v2 that the next sessions run.

Why this is recursive: a promoted model changes the sessions it runs in. Where it acts, the agent reaches states the default would never have reached; their outcomes go into the ledger, and the next round trains on them. Better policy, new states, data from those states, better policy.

What keeps it safe: the model acts only where its calibrated confidence clears a threshold and its answer differs from the default, so everywhere else the default runs as before. The threshold and the gate bound the risk per session (the share of sessions harmed by any wrong act, 5% at most), and the gate, frozen by a person, judges each candidate on fresh sessions it never trained on, by its gain over the default. Because acting changes the states that follow, a model safe on logged states can still harm live ones: the runtime's monitor checks the champion as each session opens and revokes it once the 95% lower bound on its harmed share is above the budget, and sessions already running it take their defaults from their next decision on. The bound is checked again as each session opens, so a model near its budget is also revoked by chance more often than 5%.

Not built yet: the research agent inside this loop. `ouroloop research` proposes spec, recipe and prompt candidates in rounds of its own; the loop above trains with the config's recipe.

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
ouroloop evolve --config examples/ouroloop.toml                # train a new decision model; promote only if it passes the gate
ouroloop calibrate --config examples/ouroloop.toml             # report each point's temperature and thresholds
ouroloop models --dir examples/workspace                       # model lineage; --rollback to go back (and revoke)
ouroloop ledger --dir examples/workspace                       # decisions, label coverage and agreement per point
```

The gate template can tighten, never loosen, the 5% risk, the 30 fresh sessions each gate needs and the bootstrap. Each round's gate record, its use and its verdict are events in the ledger; `gates/` holds copies. Only what a gate record accepted can be promoted, answer or act.

About TDE: TDE is a separate project that will be released on its own. The `tde` trainer records how ouroloop trains it: as a job (`ouroloop/evolve/worker.py`) on this machine, or on a GPU host over ssh with `host = "user@box"`, in a new directory `<jobs_root>/<job id>/` there. The model it yields is a ref: the job id, whose run stays in that directory, or `<repo>@<commit>` with `transport = "hf"`, which commits the run once to a private Hugging Face repository. A `[models]` table says how refs are served: `url` (and `token_env`) for `ouroloop serve` on the GPU host, or `jobs_root` (and `hf_prefix`) to load them in-process there. Without TDE, the whole pipeline runs with the built-in signature model (`type = "signature"`). To bring your own model, write a trainer with two methods, `train(job)` returning a result and `backend(result)` returning a backend that answers decisions, and name it in the config as `type = "your.module:YourTrainer"`.

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
| `ouroloop/adapters/` | Claude Code hooks (an optional data source) |

## Status

v0.1. Done: the decision layer and an end-to-end skeleton (M0), the agent harness (M1), the training pipeline and research agent (M2), integrations (M3), five built-in decision points, and prompt evolution (M5). The agent, the 10-task suite and two rounds of prompt evolution have run with real models (Zhipu GLM, with Jev as the decision backend).

Not done yet: M4, a long unattended run showing that the share of automatic decisions rises from one generation to the next. This release provides the mechanism; the evidence so far is the TDE experiment record and the flow tests above.

The design docs are in Chinese for now: [docs/PLAN.md](docs/PLAN.md) (design and milestones) and [docs/RESEARCH_LOOP.md](docs/RESEARCH_LOOP.md) (how the research loop grew out of the TDE experiments).

## License

MIT, see [LICENSE](LICENSE).
