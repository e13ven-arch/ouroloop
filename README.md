# ouroloop

English | [中文](README.zh-CN.md)

An engineering prototype of bounded recursive self-improvement (bounded RSI) for agent applications, built and measured on [AutomationBench](https://github.com/zapier/AutomationBench). The system is S = (W, R, E; K):

- **W**, the worker: an evolvable agent program (a versioned harness package that holds the loop itself, its tools, hooks, prompts, skills and memory), run by a fixed runner shell against a pinned model.
- **R**, the improver: it reads train failures and writes package changes in a bounded coding session; its packages climb a verification ladder before a held-out screen decides whether they are installed.
- **E**, the evidence: the permission-filtered record of attempts, screens and rounds, written by the host, never by the package.
- **K**, the fixed core: scorer, splits, gates, permissions, budgets and per-attempt limits. R cannot change any of it.

A change is installed only after it passes free checks (static, unit, tool check, hook replay), an in-sample trial on the failures it claims, and a screen on held-out train tasks whose flips are read twice. An effect is claimed only from a sealed final on the test split, under a plan committed before the first test attempt. The recursion is bounded: an update to R counts only when a downstream W\* comparison validates it ([design §7](docs/DESIGN.md)); that part has not been shown.

**Where it stands (2026-10-19).** Four sealed finals and a dozen measured activities on a subscription budget. The loop finds changes from train failures alone; the one significant gain is +13 points on Marketing (v0.1), the finance finals are +8 and +4 within noise. The later activities measured the noise floor of the benchmark (10–25% of tasks flip between two runs of the same package), rebuilt acceptance on confirmed flips and footprints, and have not yet produced a package that beats that floor on held-out tasks. The numbers are below, with what each one does and does not show.

## Results

Each final tests claim C1, a same-model gain: it compares the evolved version W\* with the starting version W0 on held-out test tasks, under a plan sealed and committed before the first test attempt. The actor and R are Claude Opus 5.5 at effort medium through headless Claude Code, one level for every arm. Costs are nominal API-equivalent dollars on a subscription, not a bill.

| Version | Activity | W0 | W\* | Paired difference, 95% interval | Reading |
|---|---|---|---|---|---|
| v0.1 | Marketing, 23 test tasks × 2 | 41.3% | 54.3% | **+13.0** [+2.2, +26.1] | C1 supported |
| v0.2 | Marketing with the research slot, 23 × 2 | 47.8% | 52.2% | +4.3 [−10.9, +19.6] | inconclusive |
| v0.3 | five domains, one shared package | — | — | no final: three rounds rejected on 125 dev tasks, nothing installed | — |
| v0.4 | finance, 25 × 3 | 54.7% | 62.7% | +8.0 [−4.0, +22.7] | inconclusive; cost per attempt $0.482 → $0.378 (−22%, significant) |
| v0.5 | support, v0.4 arm against v0.5 arm | — | — | no final: the comparison was stopped after the experience-library step | train/dev process evidence only |
| finance-v2 to v4 | finance, 50 train / 25 dev; the first runs of the redesigned loop with a dev gate | — | — | no final: the dev gate rejected three times (gain 0, 0, −0.02), nothing installed | candidates passed the free rungs; cost per attempt −18%, significant; v4 $225 |
| finance-v5 | finance, 50 × 3 on the two-way split; the v1.2 search loop, two rounds, one hook installed | 52.7% | 56.7% | +4.0 [−4.0, +12.7] | inconclusive; partial credit 0.870 → 0.877; cost per attempt +23% |

Every installed change came from train failures alone; no person chose it. Gains read on dev or on the held-out screen are selected by the gate and are process records, never results.

**The three-domain stream (no finals; finance's test split unread since finance-v5).** From finance-v6 on, the train halves of finance, hr and operations (150 tasks) run as one stream: each task is attempted once on the working line as it stands (its first encounter is its score), and the improver runs a round every 30 tasks.

| Activity | Loop | First encounters | Outcome | Cost |
|---|---|---|---|---|
| finance-v6 | several packages a round, stacking, audit hooks, `seed_from`, the stream (stages 4d/4e) | 89/150 | one package installed in 5 rounds (a system-prompt change) | $199 |
| finance-v7 | failures side by side, vocabulary causes, one-way audits with a rehearsal (4f/4g) | 90/150 | two packages installed in 5 rounds, both within the noise floor the run measured: 10–17% of tasks flip between two runs of one package | $335 |
| finance-v8 | V2b false-trigger gate, flip confirmation, acceptance on confirmed counts, regression evidence and a frontier of partial packages (4h/4i) | 87/150 | no package installed in 5 rounds; one partial package on the frontier, its merge lost the fix | $261 |
| hand-made package | not RSI: a person read the 63 failing train tasks, wrote one evidence-bearing hold package and screened it with the loop's own rule | — | 22 of 55 failing train tasks pass on one reading; held-out +5 fixes −3 regressions, twice, rejected under the zero-regression rule | $121 |
| finance-v9 r0 | in-sample trial before the screen, regressions only where a component acted, fix evidence on the frontier (4j) | 87/150 (imported from v8) | R's hook passed the trial (1 of 3 tasks fixed) and the free rungs; on 60 held-out tasks confirmed 0 fixes, 0 regressions (6 of 7 first-reading flips had no footprint, 1 was noise), rejected | $48 |

What these runs taught, in order: a single reading of a held-out screen installs noise, so a flip counts only when it repeats under the candidate and the working line agrees with itself; a package of tools and hooks is credited or blamed only on attempts where one of its components acted; a hook that passes the recorded-attempt replay can still change nothing live, and the replay itself gives a before_tool hook less than a live run does (the open defect in [docs/PLAN.md](docs/PLAN.md) §3). Per-activity detail, diagnoses and limits: [docs/RESULTS.md](docs/RESULTS.md) and [docs/JOURNAL.md](docs/JOURNAL.md).

Test splits read so far: marketing 3 times, finance twice (v0.4 and finance-v5), support, sales and hr never. Operations has never been read and stays fully held out.

## Status

- **Released:** tag `v0.4.0`, the last evaluated version merged into `main`; earlier tags `v0.1.0` to `v0.3.0`. Everything after it is on `m1-automationbench` and unreleased.
- **Design:** [docs/DESIGN.md](docs/DESIGN.md), v1.1 (2026-10-09) with the amendments §6.12 to §6.19 (v1.9). Tools and flow code are the main search surface, text is auxiliary, the loop may change but rarely, weights come second. The deliverable is a lightweight per-business harness package plus the runner shell and a model descriptor, with distillation as a second lever. The module designs and interfaces are in [docs/design/](docs/design/README.md).
- **Stages 1 to 3 (2026-10-09):** the platform rebuilt to the design. `ouroloop/harness/` is the typed package (ten component kinds, manifest v3, the literal check, the vendored-upstream patch stack, the Stirrup seed with six hook sites); `runtime/` the gateway, broker, isolated worker, egress and runner shell; `evaluate/` the splits, attempts, the paired gate, the sealed final, the budget book and releases {W, R, K}; `activity/` config and presets to K, the phases, the startup checks (money, leakage, correctness only), the report and the CLI; `memory/` ReMe's task memory in a sidecar under the platform's rules; `improver/` the round as resumable steps: measure, diagnose into causes, allocate a component kind per cause, R's coding session in the worker, the ladder V0 static / V1 unit / V2a toolcheck / V2b hook replay / V2c reach / V2d audit rehearsal, V3 the held-out screen, install by compare-and-swap, the ledger and the playbook.
- **Stage 4 (2026-10-10 to 2026-10-19):** the measured runs above and the loop changes they forced — one package per round with held-out acceptance accumulating on the working line (4b/4c), several packages a round, audit hooks and the stream (4d/4e), failures side by side and one-way audits (4f/4g), reproducible acceptance (4h), accumulation without regression (4i), the in-sample trial and regressions by footprint (4j). Each is a section of DESIGN §6 and a JOURNAL entry with its numbers.
- **Next (waiting on the user, [docs/PLAN.md](docs/PLAN.md) §3):** fix V2b's replay so a before_tool hook sees the same view a live run gives it and a package's hooks replay together; then finance-v9's next round, or stage 4k (an evidence store and hold composer in the seed, so R writes evidence rules instead of infrastructure).

## Quick start

You need Python 3.11 or newer, on macOS or Linux (on Windows, use WSL).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,automationbench]"        # add ,train,train-jobs for the training stack; installs the `ouroloop` command
pytest -q                                      # offline: no network, no model; live and slow tests excluded
OUROLOOP_TEST_REQUIRE_DOCKER=1 OUROLOOP_TEST_REQUIRE_MEMORY=1 pytest -q -m "not live"   # the full run: Docker worker, upstream checkout, memory sidecar
```

Then run a first activity with [examples/automationbench/quickstart.toml](examples/automationbench/quickstart.toml): finance on v0.4's split in a new workspace, `examples/automationbench/workspace-quickstart/`, with nothing frozen yet, so it touches no existing record. It needs the benchmark checkout (next section). The first command calls no model:

```bash
ouroloop bench --config examples/automationbench/quickstart.toml --phase smoke --checks-only   # $0: splits, scorer contract, input audit, the seed's literals
ouroloop bench --config examples/automationbench/quickstart.toml --phase smoke                 # one dev attempt, ~$0.5 nominal
ouroloop freeze --config examples/automationbench/quickstart.toml                              # K, gate template, budget root; prints the [rsi] lines to add
ouroloop bench --config examples/automationbench/quickstart.toml --phase cycle --until baseline   # W0 on train and dev, ~$58 nominal
```

The config's header lists the stages that follow and what each costs. The file has no `[final]` on purpose: a final reads finance's test split, which two finals have already read.

## Run on AutomationBench

AutomationBench simulates business workflows across 47 SaaS tools. It scores the world state that the agent's tools leave behind. The agent receives the task prompt and the tool schemas; the assertions and the seeded state stay in an object it never receives.

ouroloop imports the benchmark lazily. Clone it and point `[bench] upstream_path` at the checkout; [examples/automationbench/README.md](examples/automationbench/README.md) lists what to install. A local score on the public task set is not a leaderboard score.

**Model.** The actor (`[model]`) and R (`[research_model]`) use `type = "claude_code"`.
- Each call is a fresh headless `claude -p` on this machine's Claude subscription login (`claude auth login`), in an empty directory, with no tools and no local customization.
- Tool calls come back as structured JSON output, not native tool use.
- The startup check refuses a login that is not a subscription, and a table without `effort`.
- The measured configs pin `claude-opus-5-5` at effort medium, one level for every arm.

The `openai_compat` and `anthropic` providers also exist; no reported result uses them.

**Package code** runs only in the isolated worker image (the runner and the Stirrup seed's dependencies, pinned by hash); an activity's K pins the image by id:

```bash
docker build --provenance=false -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .
```

**Commands.** An activity runs in phases, and you stop after each one to read it. Every paid phase is resumable and pays for nothing on record twice; every one stops at the activity's budget root, in notional dollars.

| Command | What it does |
|---|---|
| `ouroloop bench --config <cfg> --phase smoke --checks-only` | upstream identity, the frozen split, scorer contract, input audit, the seed's literal scan; no model call |
| `ouroloop bench --config <cfg> --phase smoke` | the same checks, then one dev attempt end to end |
| `ouroloop freeze --config <cfg>` | K, the gate template and the budget root (and, after a release, the final plan from `[final]`); prints the `[rsi]` lines to add |
| `ouroloop bench --config <cfg> --phase baseline` | W0 on train and on dev `control_readings` times; `[rsi] baseline_from` imports another workspace's readings of the same attempt identity first, unpaid |
| `ouroloop bench --config <cfg> --phase library` | the memory's train stream and its snapshot as a candidate (needs `python -m ouroloop.memory setup`) |
| `ouroloop bench --config <cfg> --phase round --round K` | one round of the improver on the working line's train reading |
| `ouroloop bench --config <cfg> --phase stream [--until batch-K]` | the streaming loop: first encounters a batch at a time, a round after each batch |
| `ouroloop bench --config <cfg> --phase gate` / `--phase release` | the release gate on dev at the end of the activity, or the release by decision when the split has no dev |
| `ouroloop bench --config <cfg> --phase evaluate` | the sealed final on the test split; only after a release, with the plan hash committed |
| `ouroloop bench --config <cfg> --phase report` | the report from the records on disk; no model call |
| `ouroloop bench --config <cfg> --phase cycle --until baseline\|library\|round-K\|gate` | the batch phases in order, stopping where told |
| `ouroloop export --config <cfg> --to exported-app` | an installed release as an app |
| `python -m ouroloop.train decide \| smoke --jev \| host init \| host check` | the optional training stack: one decision from a backend, a backend smoke, a training host ([docs/TRAINING_HOSTS.md](docs/TRAINING_HOSTS.md)) |

**Configs.** A config names a preset (`[harness] preset`) and may only tighten it; the tables are listed in [docs/design/CONTRACTS.md](docs/design/CONTRACTS.md) §9.
- `quickstart.toml` is the template for a new activity; `finance-v5.toml` to `finance-v9.toml` are the measured activities of stage 4, each header listing its steps and costs.
- Their workspaces hold local, git-ignored run records. Workspaces from before stage 1 are re-rendered or resumed at their tags (`v0.4.0`, `archive-2026-10-08`), not at HEAD.

For another domain, copy `quickstart.toml`. Change `[bench] domain` (or `domains`) and the `[bench.frozen_splits]` pin: use the hash an earlier workspace froze for that domain, or remove the pin for a fresh split. Give the copy a new `[workspace] root` and `[rsi] scope_id`.

## Code layout

| Path | What it holds |
|---|---|
| `ouroloop/contracts.py` | identity and the content-addressed object store, events, the file helpers, the error classes |
| `ouroloop/harness/` | the harness package: component kinds, manifest v3, shape and literal checks, the vendored-upstream patch stack, the seeds (`seeds/stirrup/`: Stirrup 0.2.0, its adapter, the write guard, `plan_changes`) |
| `ouroloop/runtime/` | one attempt: the gateway, the broker, the worker and its runner protocol, egress, the runner shell, the trace, the exported app |
| `ouroloop/evaluate/` | splits, hard limits, attempts and results, the paired gate, the sealed final, the budget book and metering, releases {W, R, K}, the model binding |
| `ouroloop/improver/` | the round: evidence, diagnosis, allocation, R's session and prompts, the verification ladder (`verify/`), the held-out screen, the trial, the frontier, the ledger and the playbook |
| `ouroloop/memory/` | the memory component: the ReMe sidecar, its bridge and rules, the train stream's write path, retrieval, snapshots |
| `ouroloop/activity/` | config and presets to K, the workspace, the phases, the startup checks, the report, the `ouroloop` CLI |
| `ouroloop/adapters/automationbench.py` | tasks, business tools, the official scorer (imports nothing of the framework) |
| `ouroloop/providers/` | `claude_code`, `openai_compat`, `anthropic` |
| `ouroloop/train/` (extra `[train]`) | the training and decision stack, off the benchmark path: decision contracts, labels and datasets, `train_artifact`, backends (Jev, TDE, LLM), model stores and serving, trainer plugins, StopGuard, `python -m ouroloop.train` |
| `ouroloop/train/jobs/` (extra `[train-jobs]`) | training jobs on this machine, on a host over ssh, or on a RunPod or Vast.ai instance; host setup scripts and the training-host image |

## Documents

The design documents are in Chinese.

- [docs/DESIGN.md](docs/DESIGN.md): the architecture (v1.9): responsibilities, which layer changes, the typed harness, the agent seed, memory, the improver, training, the platform, code layout, stages, and the amendments each measured run forced (§6.12 to §6.19).
- [docs/PLAN.md](docs/PLAN.md): where things stand, the rules, the next stages, and what waits for the user. Start here.
- [docs/RESULTS.md](docs/RESULTS.md) and [docs/JOURNAL.md](docs/JOURNAL.md): the results, and a log of every change and run with its numbers, newest first.
- [docs/design/](docs/design/README.md): the module designs and interfaces, and the v2 training chain; [docs/history/](docs/history/): the pre-registered plans and analyses of measured activities, the failure analysis behind the hand-made package, and the framework survey.

Every file deleted on 2026-10-08, in the cleanup and in slimming batch 1, is still readable at tag `archive-2026-10-08`; the code and documents stage 1 replaced are at tag `archive-2026-10-09`.

## License

MIT, see [LICENSE](LICENSE). The Stirrup seed is vendored under its own license in `ouroloop/harness/seeds/stirrup/vendor/stirrup/LICENSE`.
