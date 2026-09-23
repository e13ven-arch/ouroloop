# ouroloop

English | [中文](README.zh-CN.md)

A lightweight RSI framework. Instead of improving the base model, an LLM improves a small decision model, and the decision model improves the agent harness.

An agent makes many small judgment calls: retry this command? keep this output in context? run this without asking? is the task done? ouroloop sends each of them to a calibrated decision model as a typed question (yes/no, a choice, or a score) and gets probabilities back in one forward pass. When the model is not confident, the call goes to an LLM or a person. Every call, and what actually happened afterwards, goes into a local ledger.

Those outcomes are training labels. `ouroloop evolve` trains a new decision model from them, and a research agent (an LLM that reasons, with a decision model as the judge) reads the evaluation results and proposes changes to decision-point specs, training recipes and the harness prompt. You can swap in your own research agent. Every change has to pass held-out evaluation and the same promotion gate before it replaces the current version.

The decision backend can be the Jev API or a small model you train yourself, such as TDE: start with Jev, and let your own model take over decision points one by one as data accumulates. Answers from Jev and from closed LLMs are never training labels: labels come from observed outcomes and from people (answers from open-weight models can count, at half weight).

The premise: commercial LLMs already reason well, and in real work the decisions are what matter most. ouroloop improves decisions, not reasoning.

## Quick start

Python 3.11 or newer, on macOS or Linux (the agent's tools use bash; on Windows, use WSL).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"      # the only runtime dependency is httpx
pytest -q                    # offline: no network, no models
ouroloop demo                # one research round on synthetic data, offline
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
- `--small-model` adds a cheaper tier; the `route` decision point picks the tier for each step. In shadow mode about 10% of steps try the small tier, and the task outcome is a weak label for them.
- In an interactive run, ouroloop asks at the end whether the task was done, and records the answer.
- `ouroloop hook print-settings` prints Claude Code hooks that record tool calls and their results from your normal Claude Code sessions into the ledger, without changing Claude Code's behaviour.

## Models

The research agent and `ouroloop run` work with any model:

- Claude through the official SDK: `[llm] type = "anthropic"` (default `claude-opus-5`, with prompt caching).
- Anything with an OpenAI-compatible API: `type = "openai_compat"` (Zhipu GLM, OpenRouter, DeepSeek, vLLM, Ollama and others), with retries on rate limits and server errors. Provider-specific request fields go in `[llm.extra]`, for example GLM's reasoning effort. See [examples/zhipu.toml](examples/zhipu.toml).
- A config's `env_file` points at a `.env` relative to the config, so keys are found from any directory.

## Research and evolution

```bash
ouroloop research --config examples/ouroloop.toml --rounds 3   # research rounds over the ledger
ouroloop evolve --config examples/ouroloop.toml                # train a new decision model; promote only if it passes the gate
ouroloop calibrate --config examples/ouroloop.toml             # fit each point's temperature and threshold
ouroloop models --dir examples/workspace                       # model lineage; --rollback to go back
ouroloop ledger --dir examples/workspace                       # decisions, label coverage and agreement per point
```

About TDE: TDE is a separate project that will be released on its own. The trainers in this repository record how ouroloop trains it: `local_tde` on the same machine, `remote_tde` on a GPU host over ssh, where the checkpoint stays. Without TDE, the whole pipeline runs with the built-in signature model (`type = "signature"`). To bring your own model, write a trainer with two methods, `train(job)` returning a result and `backend(result)` returning a backend that answers decisions, and name it in the config as `type = "your.module:YourTrainer"`.

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
- it runs on a few development tasks first, and only if it does no worse than the champion does it run on every task, held-out ones included, before the promotion gate;
- the research agent never sees held-out results;
- each run is recorded once per (prompt, model, task, repeat) in `harness/runs.jsonl` and reused: the champion's runs carry over between rounds, and re-running a prompt until it gets lucky is not possible.

The checks at the end of a task also label that run's `stop` and `route` decisions. Suite runs execute bash automatically, in a temporary directory with the deny list in force; use a container or a VM if you do not trust the model.

## Code layout

| Path | What it holds |
|---|---|
| `ouroloop/decision.py` | decision points and specs (question, candidates, view parameters) |
| `ouroloop/policy.py` | modes, threshold fitting, exploration and propensities |
| `ouroloop/ledger.py` | the ledger, raw contexts, champion specs, redaction |
| `ouroloop/runtime.py` | ties decision points, backends, fallbacks and the ledger together |
| `ouroloop/agent/` | the minimal agent: loop, four tools, harness prompt, task suites |
| `ouroloop/points/` | built-in points `retry`, `compact`, `tool_gate`, `stop`, `route`, and the deny list |
| `ouroloop/chat.py` | a provider-neutral chat format |
| `ouroloop/providers/` | Anthropic (official SDK) and OpenAI-compatible APIs |
| `ouroloop/backends/` | decision backends: `mock`, `jev`, `tde`, `llm`, `http` |
| `ouroloop/research/` | question conversion, the research agent interface and default, research rounds |
| `ouroloop/evolve/` | dataset building, data checks, trainers, calibration, the promotion gate, model registry, prompt candidates, experience records |
| `ouroloop/serve.py` | the HTTP decision service |
| `ouroloop/adapters/` | Claude Code hooks (an optional data source) |

## Status

v0.1. Done: the decision layer and an end-to-end skeleton (M0), the agent harness (M1), the training pipeline and research agent (M2), integrations (M3), five built-in decision points, and prompt evolution (M5). The agent, the 10-task suite and two rounds of prompt evolution have run with real models (Zhipu GLM, with Jev as the decision backend).

Not done yet: M4, a long unattended run showing that the share of automatic decisions rises from one generation to the next. This release provides the mechanism; the evidence so far is the TDE experiment record and the flow tests above.

The design docs are in Chinese for now: [docs/PLAN.md](docs/PLAN.md) (design and milestones) and [docs/RESEARCH_LOOP.md](docs/RESEARCH_LOOP.md) (how the research loop grew out of the TDE experiments).

## License

MIT, see [LICENSE](LICENSE).
