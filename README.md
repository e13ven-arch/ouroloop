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
- **Next** ([docs/PLAN.md](docs/PLAN.md)): slimming in three batches. Memory work, open source first: no existing cross-task memory fits as it is, so the experience library stays the base and borrows designs from ACE and ReMe. R0 changes so that R also proposes tools and loop patches; K already allows writable package tools and agent-program edits, but R has never proposed a tool, while StarHarness reports +26.1 on finance (on a different metric) from such patches. New single-domain activities with sealed finals: any domain may run one under conditional reuse, with its read count disclosed. Phase 1 of the v2 training chain: Tinker LoRA for training, self-hosted vLLM on RunPod for evaluation inference, accepted on chain receipts only.
- **Legacy code:** the pre-benchmark decision stack (Jev and TDE backends, local trainers, GPU jobs, decision points, and commands such as `run`, `demo`, `suite`, `research`, `evolve` and `serve`) is still in the tree, off the benchmark path, until slimming batch 1 moves its reusable part into the optional subpackage `ouroloop/train/` (extras `[train]`, `[train-jobs]`) and deletes the rest.

## Quick start

Python 3.11 or newer, on macOS or Linux (on Windows, use WSL).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,automationbench]"
pytest -q                                      # offline: no network, no model; live and slow tests excluded
OUROLOOP_TEST_REQUIRE_DOCKER=1 pytest -q -m "not live"   # the full run: adds the Docker worker (image below) and the upstream checkout
```

## Run on AutomationBench

AutomationBench simulates business workflows across 47 SaaS tools and scores the world state the agent's tools leave behind. The agent receives the task prompt and the tool schemas; the assertions and the seeded state stay in an object it never receives. ouroloop imports the benchmark lazily: clone it and point `[bench] upstream_path` at the checkout ([examples/automationbench/README.md](examples/automationbench/README.md) lists what to install). A local score on the public task set is not a leaderboard score.

**Model.** The actor (`[llm]`) and R (`[research.llm]`) use `type = "claude_code"`. Each call is a fresh headless `claude -p` on this machine's Claude subscription login (`claude auth login`), with no tools and no local customization, in an empty directory. Tool calls come back as structured JSON output, not native tool use. The startup check refuses a login that is not a subscription and a table without `effort`. The measured configs pin `claude-opus-5-5` at effort medium, one level for every arm. The `openai_compat` and `anthropic` providers also exist; no reported result uses them.

**Package code** runs only in the isolated worker image:

```bash
docker build -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .
```

**An activity** runs in stages, and you stop after each one to read it:

```bash
ouroloop bench --config <cfg> --phase smoke --checks-only    # upstream identity, frozen splits, contract and input audit; no model call
ouroloop freeze --config <cfg>                               # freezes the task plan, schedule and budget; prints the [rsi] lines to add
ouroloop bench --config <cfg> --phase cycle --until baseline # W0 on train and dev
ouroloop bench --config <cfg> --phase library                # v5 preset only: the experience library's stream and snapshot round
ouroloop bench --config <cfg> --phase cycle                  # R's rounds: notes, coding session, screen, dev gate, confirmation
# only after a confirmed install: fill [freeze.final] with W*, freeze again and commit the plan hash, then
ouroloop bench --config <cfg> --phase evaluate               # the sealed final on the test split
ouroloop bench --config <cfg> --phase report                 # recomputes everything from the records on disk; no model call
ouroloop export --config <cfg> --to exported-app             # an installed release as a minimal app
```

An interrupted `cycle` or `library` run continues where it stopped and pays nothing twice. Every paid phase stops at the activity's budget root, in notional dollars. The phases `baseline`, `improve`, `collect`, `research` and `learn` also exist at HEAD. The slimming plan removes the in-activity research slot and, unless the user vetoes, the method library's `learn`.

**Configs.** The measured configs are `examples/automationbench/claude-v4-finance.toml` (v0.4's final) and `claude-v4-support.toml` and `claude-v5-support.toml` (the two support arms). Each header lists its stages and their costs. Their `[workspace]` roots hold local, git-ignored run records, and their `[rsi]` refs name plans frozen there. For a new activity, copy one and give it a new `[workspace] root` and `[rsi] scope_id`. Replace the `[rsi]` plan refs with the lines your `freeze` prints, and leave out `[freeze.final]` until there is a W\*.

## Code layout (the benchmark path)

| Path | What it holds |
|---|---|
| `ouroloop/bench.py` | the phases of `ouroloop bench`, presets, startup checks, freezing and the cycle |
| `ouroloop/adapters/automationbench.py` | tasks, business tools, the official scorer |
| `ouroloop/agent/` | the runner shell (`runner.py`, `gateway.py`), the default agent program, capability packages (`harness_package.py`) and the isolated worker (`worker.py`, `worker_runner.py`) |
| `ouroloop/research/` | R: failure diagnosis, the coding session (`session.py`), draft checks, the experience library (`library.py`), the method library (`methods.py`, `method_kit/`) |
| `ouroloop/evolve/` | contracts and the object store (`contracts.py`), the paired task gate (`task_gate.py`), execution and the budget book (`execution.py`); the legacy trainers and promotion gate live here too |
| `ouroloop/release.py`, `ouroloop/policies.py` | W/R releases, installs and recovery; registered policy targets |
| `ouroloop/report.py` | the offline report: identity, task effect, costs, C1–C4 states |
| `ouroloop/providers/` | `claude_code`, `openai_compat`, `anthropic` and `manual` |
| `ouroloop/app.py` | the minimal app an exported release runs as |

## Documents

The design documents are in Chinese.

- [docs/PLAN.md](docs/PLAN.md): where things stand, the rules, the next stages, and what waits for the user. Start here.
- [docs/RESULTS.md](docs/RESULTS.md) and [docs/JOURNAL.md](docs/JOURNAL.md): the results, and a log of every change and run, newest first.
- [docs/DESIGN.md](docs/DESIGN.md), [docs/ROADMAP.md](docs/ROADMAP.md) and [docs/modules/](docs/modules/README.md): the design, the activity history and the module rules.
- [docs/design/](docs/design/): the slimming inventory, the open-source components plan, the v2 training pipeline, and the v0.4 and v0.5 plans.

Every file deleted in the 2026-10-08 cleanup is still readable at tag `archive-2026-10-08`.

## License

MIT, see [LICENSE](LICENSE).
