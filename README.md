# ouroloop

English | [中文](README.zh-CN.md)

An engineering prototype of bounded recursive self-improvement (bounded RSI) for agent applications, built and measured on [AutomationBench](https://github.com/zapier/AutomationBench). The system is S = (W, R, E; K):

- **W**, the worker: an evolvable agent program (a versioned capability package that holds the loop itself, its tools, hooks, skills and memory), run by a fixed runner shell against a pinned model.
- **R**, the improvement policy: it reads train failures and proposes package changes in a bounded coding session.
- **E**, the evidence: the permission-filtered record of runs and experiments, written by the host, never by the package.
- **K**, the fixed core: scorer, splits, gate, permissions, budgets and per-attempt limits. R cannot change any of it.

A change is installed only after a screen on train tasks, a paired dev gate and a confirmation reading. An effect is claimed only from a sealed final on the held-out test split. The recursion is bounded: an update to R counts only when a downstream W\* comparison validates it and the next improvement actually uses it ([design §7](docs/DESIGN.md)). That part has not been shown yet.

## Results

Each final tests claim C1, a same-model gain: it compares the evolved version W\* with the starting version W0 on held-out test tasks, under a plan sealed and committed before the first test attempt. The actor and R are Claude Opus 5.5 through headless Claude Code.

| Version | Activity | W0 | W\* | Paired difference, 95% interval | Reading |
|---|---|---|---|---|---|
| v0.1 | Marketing, 23 test tasks × 2 | 41.3% | 54.3% | **+13.0** [+2.2, +26.1] | C1 supported |
| v0.2 | Marketing with the research slot, 23 × 2 | 47.8% | 52.2% | +4.3 [−10.9, +19.6] | inconclusive |
| v0.3 | five domains, one shared package | — | — | no final: three rounds rejected on 125 dev tasks, nothing installed | — |
| v0.4 | finance, 25 × 3 | 54.7% | 62.7% | +8.0 [−4.0, +22.7] | inconclusive; cost per attempt $0.482 → $0.378 (−22%, interval [−$0.145, −$0.070], significant) |
| v0.5 | support, v0.4 arm against v0.5 arm | — | — | no final: the user stopped the comparison after step 3b | train/dev process evidence only |

Every installed change came from train failures alone; no person chose it. Gains on dev were selected by the gate and are kept as process records, never as results. R improving itself (C4) is not shown: in v0.2's research slot neither research policy added a change, so there was nothing to compare. Costs are nominal API-equivalent dollars on a subscription: about $525 for v0.1 and v0.2, $517 for v0.3, $209.73 for v0.4. Setup, per-task detail, diagnoses and limits: [docs/RESULTS.md](docs/RESULTS.md).

**v0.5 on support (process evidence, not a claim).** The two arms started level (W0 on dev: 26/75 for the v0.4 arm, 27/75 for the v0.5 arm). In step 3b, the experience library's stream over 50 train tasks scored D = 3.0, and the second half did better than the first (0.00 → 0.12, P = 0.89). Its snapshot then passed the paired dev gate: first reading +0.24, confirmation +0.08, pooled +0.16 [+0.047, +0.287], cost per attempt −6.6%. The step cost $81.86. On 2026-10-08 the user stopped the comparison: a missing baseline does not matter, improve everything that can be improved, and judge by the final result. So R's rounds (3c), `learn` (3d) and both finals were not run. H1 (v0.5 beats v0.4) is untested, and support's test split is unread.

Test splits read so far: marketing 3 times, finance once, support, sales and HR never. Operations has never been read and stays fully held out.

## Status

- **Released:** tag `v0.4.0`, the finance final; earlier tags `v0.1.0` to `v0.3.0`.
- **Forward design (2026-10-09):** [docs/DESIGN.md](docs/DESIGN.md) v1.1. Tools and flow code are the main search surface, text is auxiliary, the loop may change but rarely, weights come second. The deliverable is a lightweight per-business harness package plus the runner shell and a model descriptor, with distillation as a second lever. The module designs and interfaces are in [docs/design/](docs/design/README.md).
- **Stage 1 (2026-10-09, branch `s1/harness-platform`):** the harness and the platform rebuilt to the design. `ouroloop/harness/` is the typed package (ten component kinds, manifest v3, the literal check, the vendored-upstream patch stack, the stirrup seed with six hook sites); `runtime/` is the gateway, broker, worker (resources and network from K), egress and runner shell; `evaluate/` is the splits, attempts, the paired gate, the sealed final, the budget book and releases {W, R, K}; `activity/` is config and preset to K, the phases, the startup checks (money, leakage, correctness only), the report and the CLI. The old `bench.py`, the policy sets, the default agent and the old improver loop are gone (readable at tag `archive-2026-10-09`). The memory (stage 2) and the improver (stage 3) come next; [docs/PLAN.md](docs/PLAN.md) §2.

## Quick start

You need Python 3.11 or newer, on macOS or Linux (on Windows, use WSL).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,automationbench]"        # add ,train,train-jobs for the training stack
pytest -q                                      # offline: no network, no model; live and slow tests excluded
OUROLOOP_TEST_REQUIRE_DOCKER=1 pytest -q -m "not live"   # the full run: adds the Docker worker (image below) and the upstream checkout
```

Then run a first activity with [examples/automationbench/quickstart.toml](examples/automationbench/quickstart.toml): finance on v0.4's split in a new workspace, `examples/automationbench/workspace-quickstart/`, with nothing frozen yet, so it touches no existing record. It needs the benchmark checkout (next section). The first command calls no model:

```bash
ouroloop bench --config examples/automationbench/quickstart.toml --phase smoke --checks-only   # $0: splits, scorer contract, input audit, the seed's literals
ouroloop bench --config examples/automationbench/quickstart.toml --phase smoke                 # one dev attempt, ~$0.5 nominal
ouroloop freeze --config examples/automationbench/quickstart.toml                              # K, gate template, budget root; prints the [rsi] lines to add
ouroloop bench --config examples/automationbench/quickstart.toml --phase cycle --until baseline   # W0 on train and dev, ~$58 nominal
```

The config's header lists the stages that follow and what each costs. The file has no `[final]` on purpose: a final reads finance's test split, which v0.4's final has already read once.

## Run on AutomationBench

AutomationBench simulates business workflows across 47 SaaS tools. It scores the world state that the agent's tools leave behind. The agent receives the task prompt and the tool schemas; the assertions and the seeded state stay in an object it never receives.

ouroloop imports the benchmark lazily. Clone it and point `[bench] upstream_path` at the checkout; [examples/automationbench/README.md](examples/automationbench/README.md) lists what to install. A local score on the public task set is not a leaderboard score.

**Model.** The actor (`[model]`) and R (`[research_model]`) use `type = "claude_code"`.
- Each call is a fresh headless `claude -p` on this machine's Claude subscription login (`claude auth login`), in an empty directory, with no tools and no local customization.
- Tool calls come back as structured JSON output, not native tool use.
- The startup check refuses a login that is not a subscription, and a table without `effort`.
- The measured configs pin `claude-opus-5-5` at effort medium, one level for every arm.

The `openai_compat` and `anthropic` providers also exist; no reported result uses them.

**Package code** runs only in the isolated worker image (the runner and the stirrup seed's dependencies, pinned by hash):

```bash
docker build --provenance=false -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .
```

**Commands.** An activity runs in stages, and you stop after each one to read it.

| Command | What it does |
|---|---|
| `ouroloop bench --config <cfg> --phase smoke --checks-only` | upstream identity, the frozen split, scorer contract, input audit, the seed's literal scan; no model call |
| `ouroloop bench --config <cfg> --phase smoke` | the same checks, then one dev attempt end to end |
| `ouroloop freeze --config <cfg>` | K, the gate template and the budget root (and, after an install, the final plan from `[final]`); prints the `[rsi]` lines to add |
| `ouroloop bench --config <cfg> --phase cycle --until baseline` | W0 on train and on dev `control_readings` times |
| `ouroloop bench --config <cfg> --phase cycle --until library` / `round-K` | the memory's stream (stage 2) and the improver's rounds (stage 3) |
| `ouroloop bench --config <cfg> --phase evaluate` | the sealed final on the test split; only after an install, with the plan hash committed |
| `ouroloop bench --config <cfg> --phase report` | the report from the records on disk; no model call |
| `ouroloop export --config <cfg> --to exported-app` | an installed release as an app |
| `python -m ouroloop.train decide \| smoke --jev \| host init \| host check` | the optional training stack: one decision from a backend, a backend smoke, a training host ([docs/TRAINING_HOSTS.md](docs/TRAINING_HOSTS.md)) |

An interrupted `cycle` continues where it stopped and pays nothing twice. Every paid phase stops at the activity's budget root, in notional dollars.

**Configs.** A config names a preset (`[harness] preset`) and may only tighten it; the tables are listed in [docs/design/CONTRACTS.md](docs/design/CONTRACTS.md) §9.
- `quickstart.toml` is the template for a new activity; `finance-v1.toml` is the first measured activity of the forward design (stage 4).
- The configs of the activities before stage 1 used other tables and are on the tags (`v0.4.0`, `archive-2026-10-08`); their workspaces hold local, git-ignored run records, re-rendered or resumed at those tags, not at HEAD.

For another domain, copy `quickstart.toml`. Change `[bench] domain` and the `[bench.frozen_splits]` pin: use the hash `workspace-claude-v3` froze for that domain, or remove the pin for a fresh split. Give the copy a new `[workspace] root` and `[rsi] scope_id`.

## Code layout

| Path | What it holds |
|---|---|
| `ouroloop/contracts.py` | identity and the content-addressed object store, events, the file helpers, the error classes |
| `ouroloop/harness/` | the harness package: component kinds, manifest v3, shape and literal checks, the vendored-upstream patch stack, the seeds (`seeds/stirrup/`: Stirrup 0.2.0, its adapter, the write guard, `plan_changes`) |
| `ouroloop/runtime/` | one attempt: the gateway, the broker, the worker and its runner protocol, egress, the runner shell, the exported app |
| `ouroloop/evaluate/` | splits, hard limits, attempts and results, the paired gate, the sealed final, the budget book and metering, releases {W, R, K}, the model binding |
| `ouroloop/activity/` | config and presets to K, the workspace, the phases, the startup checks, the report, the `ouroloop` CLI |
| `ouroloop/adapters/automationbench.py` | tasks, business tools, the official scorer (imports nothing of the framework) |
| `ouroloop/providers/` | `claude_code`, `openai_compat`, `anthropic` |
| `ouroloop/train/` (extra `[train]`) | the training and decision stack, off the benchmark path: decision contracts, labels and datasets, `train_artifact`, backends (Jev, TDE, LLM), model stores and serving, trainer plugins, StopGuard, `python -m ouroloop.train` |
| `ouroloop/train/jobs/` (extra `[train-jobs]`) | training jobs on this machine, on a host over ssh, or on a RunPod or Vast.ai instance; host setup scripts and the training-host image |

## Documents

The design documents are in Chinese.

- [docs/DESIGN.md](docs/DESIGN.md): the architecture (v1.1): responsibilities, which layer changes, the typed harness, the agent seed, memory, the improver, training, the platform, code layout and stages.
- [docs/PLAN.md](docs/PLAN.md): where things stand, the rules, the next stages, and what waits for the user. Start here.
- [docs/RESULTS.md](docs/RESULTS.md) and [docs/JOURNAL.md](docs/JOURNAL.md): the results, and a log of every change and run, newest first.
- [docs/design/](docs/design/README.md): the module designs and interfaces, and the v2 training chain; [docs/history/](docs/history/): the pre-registered plans and analyses of measured activities, and the framework survey.

Every file deleted on 2026-10-08, in the cleanup and in slimming batch 1, is still readable at tag `archive-2026-10-08`; the code and documents stage 1 replaced are at tag `archive-2026-10-09`.

## License

MIT, see [LICENSE](LICENSE).
