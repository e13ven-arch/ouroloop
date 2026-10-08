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
- **Implemented, not released:** v0.5's method library and experience library, merged into `m1-automationbench` (`7e7732e`). Under the batch-3 rule agreed before the run, the experience library stays as the memory base behind a preset switch, because both of its process readings held (train D ≥ +2; learning curve P ≥ 0.8). The method library's readings were never taken, so the rule cuts it. That cut is a proposal the user may veto.
- **Slimming batch 1 (done on branch `slim/1`, 2026-10-08):** the dead code and legacy paths are gone, and every attempt runs under the runner shell. The typed decision contracts (compatible with Jev and TDE), the decision records and the controller training chain are kept in the optional subpackage `ouroloop/train/` (extras `[train]` and `[train-jobs]`, entry point `python -m ouroloop.train`), off the benchmark path. The v1 agent loop's decision points and champion releases are removed (read them at tag `archive-2026-10-08`). Hash identity with the existing workspaces was not kept: no existing activity resumes at HEAD, and old workspaces are re-rendered or resumed at their tags. HEAD still reads their train/dev records.
- **Forward design (2026-10-09, awaiting review):** [docs/DESIGN.md](docs/DESIGN.md) v1.1. It first fixes which layer of self-improvement changes: tools and flow code are the main search surface, text is auxiliary, the loop may change but rarely, weights come second. The deliverable is a lightweight per-business harness package plus the runner shell and a model descriptor, with distillation as a second lever. The framework has two jobs, improving the harness automatically and training models. The harness becomes a typed component graph (agent, vendor, tool, hook, prompt, skill, memory, model, config, test), each kind with its own edit path, $0 verification, credit condition and limit; Stirrup is vendored as the agent base behind one adapter file, ReMe's procedural-memory ops run in a sidecar behind our platform rules; the improver is the framework's own eight-step loop with a per-kind verification ladder; the platform keeps only what protects money, prevents leakage and keeps results correct. Stages and costs: [docs/PLAN.md](docs/PLAN.md) §2 (stages 1 to 3 cost nothing; the first paid run is a finance activity under the new preset).

## Quick start

You need Python 3.11 or newer, on macOS or Linux (on Windows, use WSL).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,automationbench]"        # add ,train,train-jobs for the training stack
pytest -q                                      # offline: no network, no model; live and slow tests excluded
OUROLOOP_TEST_REQUIRE_DOCKER=1 pytest -q -m "not live"   # the full run: adds the Docker worker (image below) and the upstream checkout
```

Then run a first activity with [examples/automationbench/quickstart.toml](examples/automationbench/quickstart.toml). It is the v0.4 finance activity in a new workspace, `examples/automationbench/workspace-quickstart/`, with nothing frozen yet, so it touches no existing record. It needs the benchmark checkout (next section). The first command calls no model:

```bash
ouroloop bench --config examples/automationbench/quickstart.toml --phase smoke --checks-only   # $0: splits, scorer contract, input audit
ouroloop bench --config examples/automationbench/quickstart.toml --phase smoke                 # one dev attempt, ~$0.5 nominal
ouroloop freeze --config examples/automationbench/quickstart.toml                              # prints the [rsi] lines to add to the file
ouroloop bench --config examples/automationbench/quickstart.toml --phase cycle --until baseline   # W0 on train and dev, ~$58 nominal
```

The config's header lists the stages that follow and what each costs. The file has no `[freeze.final]` on purpose: a final reads finance's test split, which v0.4's final has already read once.

## Run on AutomationBench

AutomationBench simulates business workflows across 47 SaaS tools. It scores the world state that the agent's tools leave behind. The agent receives the task prompt and the tool schemas; the assertions and the seeded state stay in an object it never receives.

ouroloop imports the benchmark lazily. Clone it and point `[bench] upstream_path` at the checkout; [examples/automationbench/README.md](examples/automationbench/README.md) lists what to install. A local score on the public task set is not a leaderboard score.

**Model.** The actor (`[llm]`) and R (`[research.llm]`) use `type = "claude_code"`.
- Each call is a fresh headless `claude -p` on this machine's Claude subscription login (`claude auth login`), in an empty directory, with no tools and no local customization.
- Tool calls come back as structured JSON output, not native tool use.
- The startup check refuses a login that is not a subscription, and a table without `effort`.
- The measured configs pin `claude-opus-5-5` at effort medium, one level for every arm.

The `openai_compat` and `anthropic` providers also exist; no reported result uses them.

**Package code** runs only in the isolated worker image:

```bash
docker build -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .
```

**Commands.** An activity runs in stages, and you stop after each one to read it.

| Command | What it does |
|---|---|
| `ouroloop bench --config <cfg> --phase smoke --checks-only` | upstream identity, frozen splits, scorer contract and input audit; no model call |
| `ouroloop bench --config <cfg> --phase smoke` | the same checks, then one dev attempt end to end |
| `ouroloop freeze --config <cfg>` | freezes the task plan, schedule and budget (and the final plan, from `[freeze.final]`); prints the `[rsi]` lines to add |
| `ouroloop bench --config <cfg> --phase cycle --until baseline` | W0 on train and dev |
| `ouroloop bench --config <cfg> --phase library` | v5 preset only: the experience library's stream and snapshot round |
| `ouroloop bench --config <cfg> --phase cycle --until segment-0/round-K` | R's rounds, one at a time: notes, coding session, screen, dev gate, confirmation |
| `ouroloop bench --config <cfg> --phase evaluate` | the sealed final on the test split; only after a confirmed install, with W\* in `[freeze.final]`, frozen and its plan hash committed |
| `ouroloop bench --config <cfg> --phase report` | recomputes everything from the records on disk; no model call |
| `ouroloop export --config <cfg> --to exported-app` | an installed release as a minimal app |
| `python -m ouroloop.train decide \| smoke --jev \| host init \| host check` | the optional training stack: one decision from a backend, a backend smoke, a training host ([docs/TRAINING_HOSTS.md](docs/TRAINING_HOSTS.md)) |

An interrupted `cycle` or `library` run continues where it stopped and pays nothing twice. Every paid phase stops at the activity's budget root, in notional dollars. The phases `baseline`, `improve`, `research` and `learn` also exist at HEAD. Slimming batch 2 removes the in-activity research slot, and batch 3 removes the method library's `learn` unless the user vetoes it.

**Configs.**
- `quickstart.toml` is the template for a new activity.
- The measured configs are `examples/automationbench/claude-v4-finance.toml` (v0.4's final) and `claude-v4-support.toml` and `claude-v5-support.toml` (the two support arms). Each header lists its stages and their costs.
- Their `[workspace]` roots hold local, git-ignored run records, and their `[rsi]` refs name plans frozen there. Those workspaces were run on earlier code: re-render or resume them at their tags (`v0.4.0`, `archive-2026-10-08`), not at HEAD.

For another domain, copy `quickstart.toml`. Change `[bench] domains` and the `[bench.frozen_splits]` pin: use the hash `workspace-claude-v3` froze for that domain, or remove the pin for a fresh split. Give the copy a new `[workspace] root` and `[rsi] scope_id`, and leave out `[freeze.final]` until there is a W\*.

## Code layout

| Path | What it holds |
|---|---|
| `ouroloop/cli.py`, `ouroloop/bench.py` | the `ouroloop bench`, `freeze` and `export` commands; the phases, presets, startup checks, freezing and the cycle |
| `ouroloop/adapters/automationbench.py` | tasks, business tools, the official scorer |
| `ouroloop/agent/` | the runner shell (`runner.py`, `gateway.py`, the deny list in `deny.py`), the default agent program, capability packages (`harness_package.py`) and the isolated worker (`worker.py`, `worker_runner.py`) |
| `ouroloop/research/` | R: failure diagnosis, the coding session (`session.py`), draft checks, the experience library (`library.py`), the method library (`methods.py`, `method_kit/`) |
| `ouroloop/evolve/` | contracts and the object store (`contracts.py`), the paired task gate (`task_gate.py`), execution and the budget book (`execution.py`), attempts (`task_eval.py`), the experiment ledger (`experience.py`) |
| `ouroloop/release.py`, `ouroloop/policies.py`, `ouroloop/workspace.py` | W/R releases, installs, recovery and export; registered policy targets; the session record each attempt writes |
| `ouroloop/report.py` | the offline report: identity, task effect, costs, C1–C4 states |
| `ouroloop/providers/` | `claude_code`, `openai_compat`, `anthropic` |
| `ouroloop/app.py` | the minimal app an exported release runs as |
| `ouroloop/train/` (extra `[train]`) | the training and decision stack, off the benchmark path: decision contracts, labels and datasets, `train_artifact`, backends (Jev, TDE, LLM), model stores and serving, trainer plugins, StopGuard, `python -m ouroloop.train` |
| `ouroloop/train/jobs/` (extra `[train-jobs]`) | training jobs on this machine, on a host over ssh, or on a RunPod or Vast.ai instance; host setup scripts and the training-host image |

## Documents

The design documents are in Chinese.

- [docs/DESIGN.md](docs/DESIGN.md): the architecture (v1.1): responsibilities, which layer changes, the typed harness, the agent seed, memory, the improver, training, the platform, code layout and stages.
- [docs/PLAN.md](docs/PLAN.md): where things stand, the rules, the next stages, and what waits for the user. Start here.
- [docs/RESULTS.md](docs/RESULTS.md) and [docs/JOURNAL.md](docs/JOURNAL.md): the results, and a log of every change and run, newest first.
- [docs/design/](docs/design/): the v2 training chain; [docs/history/](docs/history/): the pre-registered plans and analyses of measured activities, and the framework survey.

Every file deleted on 2026-10-08, in the cleanup and in slimming batch 1, is still readable at tag `archive-2026-10-08`.

## License

MIT, see [LICENSE](LICENSE).
