# ouroloop

English | [中文](README.zh-CN.md)

A bounded self-improvement loop for business agents, built and measured on [AutomationBench](https://github.com/zapier/AutomationBench). An improver reads an agent's own run of a task, writes changes to the agent's harness package (tools, hooks, skills, prompts), tries them for real, and the platform decides what is installed — by evidence, never by a person. Everything the improver cannot touch is frozen: the scorer, the data splits, the gates, the budget. The system is S = (W, R, E; K):

- **W**, the worker: an evolvable agent program (a versioned harness package that holds the loop itself, its tools, hooks, prompts, skills and memory), run by a fixed runner shell against a pinned model.
- **R**, the improver: a bounded coding session that reads one run, builds a package, and tries it on the same request before the host decides.
- **E**, the evidence: the permission-filtered record of attempts, tries and adoptions, written by the host, never by the package.
- **K**, the fixed core: scorer, splits, gates, permissions, budgets and per-attempt limits. R cannot change any of it.

Two loops exist. The first (stages 4b–4n) searched for packages that generalise across different workflows: a candidate climbed a verification ladder and was installed only when a held-out screen read more fixes than regressions, twice. The second (stages 4o–4p, the main line since 2026-10-22) follows how real work repeats: **the same request comes back**, and the agent should get it right the second time. Each task is met three times; after a first encounter that was sent back, R corrects the harness for that task from that run alone, and the later encounters run the task's own harness. What R learns is never a label: at most the one bit "accepted or sent back" per attempt, and the reviewer's rejected items (which message, which row — never the expected value).

**Where it stands (2026-10-26).** The repetitive stream works and its limits are known. On 50 finance test tasks with the one-bit signal, the steady-state pass rate rose from 64% to 68% with zero regressions ($184, not significant). With the rejected items as the signal, R learned mechanisms by itself on train tasks — a hold that opens every source the run never read before the first write, a tool that derives a figure from the inputs read — in four independent sessions, with no word of them in its prompt; one task went 0.5 → 1.0 → 1.0, another reached its correct figure and held once the model ran at effort max. The remaining failures are documented: corrections in sources the agent never opened (now handled), ambiguities the thin signal cannot resolve, the world's clock not given to the agent, and a 10–17% noise floor of the benchmark itself. The numbers and what each shows are below.

## Results

The actor and R are Claude Opus 5.5 through headless Claude Code, at one effort level for every arm (medium unless stated). Costs are nominal API-equivalent dollars on a subscription, not a bill. Every installed change came from the agent's own runs; no person chose it.

**The repetitive stream (stages 4o–4p).** Each task is met three times. "First pass" is the first encounter on the base harness; "steady state" is the third encounter, on whatever the task adopted. Feedback: *one bit* = accepted or sent back, per attempt; *items* = the bit plus the rejected checks' kinds and objects, never a value.

| Activity | Tasks | Feedback | First pass → steady state | What happened | Cost |
|---|---|---|---|---|---|
| finance-v16 (rehearsal) | 20 train | one bit | 15/20 → 18/20 | 5 sent back, 3 corrected and holding; the 15 accepted tasks untouched | $51 |
| finance-v18 (**the number**) | 50 test | one bit | **32/50 (64%) → 34/50 (68%)**, +4.0 [−4.0, +12.0] | 18 sent back, 3 corrected and holding, no passing task lost to learning; the 15 unfixed: 7 wrong figure or tier, 6 wrong item scope, 2 wrong channel — R's candidates aimed elsewhere on 12 because one bit never says which item | $184 |
| finance-v19 | 2 train | items | — | R wrote a tool for a tax estimate and the agent called it on every try; the arithmetic was right, the input lacked a rate change posted in a chat channel the agent never opened | $8 |
| finance-v20 | 2 train | items | 0/2 → 2/2 | two independent sessions each built the same "unread sources" hold; the agent then read the channel and derived 13,162.5 and 105,792 from the corrected inputs; one task's third encounter flipped on mental arithmetic | $11 |
| finance-v21, v22 | 1 train | items | 0/1 → 0/1 | the hold held, the tool came, the session chose the wrong reading of an ambiguous procedure; two of four tries lost to host defects since fixed | $10 |
| finance-v24 (**effort max**) | 3 train | items | 1/3 → 2/3 | one session built the hold, the routing hold and the tool together and the task's steady state held at 105,792; another's accepted candidate was lost to the output cap (fixed since); the base passed a task it never passed at medium | $61 |
| finance-v25 (effort max) | 10 test | items | 2/4 → 3/4 on the 4 tasks completed | stopped by the budget guard after 4 tasks: one improver call summed 400,793 input tokens against a 400,000 bound (the CLI's continuations resend the context) | $68 |

What these runs show, in order: the one-bit rule installs only what was accepted, so nothing regresses; the bit alone cannot aim R at the wrong item; with the rejected items and an inventory of the world's sources, R finds the generic mechanisms on its own; at effort max it finds them in one session at five to seven times the cost. Per-task detail: [docs/JOURNAL.md](docs/JOURNAL.md) (entries of 2026-10-22 to 2026-10-25).

**The held-out screen loop (stages 4b–4n): four sealed finals.** Each compared the evolved version W\* with the starting version W0 on held-out test tasks, under a plan sealed and committed before the first test attempt.

| Version | Activity | W0 | W\* | Paired difference, 95% interval | Reading |
|---|---|---|---|---|---|
| v0.1 | Marketing, 23 test tasks × 2 | 41.3% | 54.3% | **+13.0** [+2.2, +26.1] | supported |
| v0.2 | Marketing with the research slot, 23 × 2 | 47.8% | 52.2% | +4.3 [−10.9, +19.6] | inconclusive |
| v0.4 | finance, 25 × 3 | 54.7% | 62.7% | +8.0 [−4.0, +22.7] | inconclusive; cost per attempt −22%, significant |
| finance-v5 | finance, 50 × 3 on the two-way split | 52.7% | 56.7% | +4.0 [−4.0, +12.7] | inconclusive |

After finance-v5, eight activities (finance-v6 to v13, about $1,150) measured the benchmark's noise floor (10–17% of tasks flip between two runs of one package), rebuilt acceptance on confirmed flips and footprints, and installed nothing that beat the floor. The diagnosis that changed the main line: AutomationBench's 150 train tasks are 150 distinct workflows — there is no repetition to learn from across them — while real work repeats the same pattern with other parameters. [docs/RESULTS.md](docs/RESULTS.md) has every activity with its diagnosis and limits.

## Status

- **Released:** tag `v0.4.0`, the last evaluated version merged into `main`; earlier tags `v0.1.0` to `v0.3.0`. Everything after it is on `m1-automationbench`.
- **Design:** [docs/DESIGN.md](docs/DESIGN.md) v2.1 — the forward design of 2026-10-09 with the amendments each measured run forced (§6.12 to §6.24). The module designs and interfaces are in [docs/design/](docs/design/README.md).
- **The platform:** `harness/` the typed package (ten component kinds, manifest v3, the literal check, the vendored-upstream patch stack, the Stirrup seed with six hook sites); `runtime/` the gateway, broker, isolated worker, egress and runner shell; `evaluate/` splits, attempts, the paired gate, the sealed final, the sealed repeat plan, the budget book and releases; `activity/` config and presets to K, the phases, the startup checks, the report and the CLI; `improver/` R's session and prompts, the verification ladder, the held-out screen, the per-task loop with its library; `memory/` ReMe's task memory in a sidecar.
- **Open items ([docs/PLAN.md](docs/PLAN.md) §3):** the world's clock (`meta.current_time`) as environment information for the agent; the per-call input bound and the session's context budget at effort max; the improver's prompts into the frozen code hash; then a fuller max-effort sample on test.

## Quick start

Python 3.11 or newer, macOS or Linux (on Windows, WSL).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,automationbench]"        # add ,train,train-jobs for the training stack; installs the `ouroloop` command
pytest -q                                      # offline: no network, no model; live and slow tests excluded
OUROLOOP_TEST_REQUIRE_DOCKER=1 OUROLOOP_TEST_REQUIRE_MEMORY=1 pytest -q -m "not live"   # the full run: Docker worker, upstream checkout, memory sidecar
```

The first activity to run is the repetitive stream on a few train tasks, [examples/automationbench/quickstart-repeat.toml](examples/automationbench/quickstart-repeat.toml): a new workspace, nothing frozen yet, no test task read. It needs the benchmark checkout and the worker image (next section). The first two commands call no model:

```bash
ouroloop bench --config examples/automationbench/quickstart-repeat.toml --phase smoke --checks-only   # $0: splits, scorer contract, input audit, the seed's literals
ouroloop freeze --config examples/automationbench/quickstart-repeat.toml                              # $0: K, the budget root, the sealed repeat plan; prints the [rsi] lines to add
ouroloop bench --config examples/automationbench/quickstart-repeat.toml --phase repeat --arm learn    # 5 train tasks x 3 encounters, R's session on each sent-back one: ~$15-25 nominal
ouroloop bench --config examples/automationbench/quickstart-repeat.toml --phase report                # $0
```

[quickstart.toml](examples/automationbench/quickstart.toml) is the older entry point: the batch activity with the memory library and the improver's rounds under the held-out screen.

## Run on AutomationBench

AutomationBench simulates business workflows across 47 SaaS tools and scores the world state the agent's tools leave behind. The agent receives the task prompt and the tool schemas; the assertions and the seeded state stay in an object it never receives.

ouroloop imports the benchmark lazily. Clone it and point `[bench] upstream_path` at the checkout; [examples/automationbench/README.md](examples/automationbench/README.md) lists what to install. A local score on the public task set is not a leaderboard score.

**Model.** The actor (`[model]`) and R (`[research_model]`) use `type = "claude_code"`: each call is a fresh headless `claude -p` on this machine's Claude subscription login (`claude auth login`), in an empty directory, with no tools and no local customization; tool calls come back as structured JSON. The startup check refuses a login that is not a subscription and a table without `effort`. The measured configs pin `claude-opus-5-5`; the preset caps the effort at medium, and `automationbench_v2_max` lifts both arms to max for the experiment above. The `openai_compat` and `anthropic` providers also exist; no reported result uses them.

**Package code** runs only in the isolated worker image, pinned by id in an activity's K:

```bash
docker build --provenance=false -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .
```

**Commands.** An activity runs in phases; you stop after each one and read it. Every paid phase is resumable and pays for nothing on record twice; every one stops at the activity's budget root.

| Command | What it does |
|---|---|
| `ouroloop bench --config <cfg> --phase smoke --checks-only` | upstream identity, the frozen split, scorer contract, input audit, the seed's literal scan; no model call |
| `ouroloop bench --config <cfg> --phase smoke` | the same checks, then one attempt end to end |
| `ouroloop freeze --config <cfg>` | K, the budget root, the gate template, and the sealed plans (`[final]`, `[repeat]`); prints the `[rsi]` lines to add |
| `ouroloop bench --config <cfg> --phase repeat --arm learn\|control [--until task-K]` | the repetitive stream: every task of the sealed plan met `encounters` times; the learn arm adopts a package per sent-back task, the control arm runs the base every time |
| `ouroloop bench --config <cfg> --phase baseline` / `--phase round --round K` / `--phase stream` | the held-out screen loop: W0's readings, one improver round, or the streaming loop |
| `ouroloop bench --config <cfg> --phase gate` / `--phase release` / `--phase evaluate` | the release gate, the release by decision, the sealed final on the test split |
| `ouroloop bench --config <cfg> --phase report` | the report from the records on disk; no model call |
| `ouroloop export --config <cfg> --to exported-app` | an installed release as an app |

**Configs.** A config names a preset (`[harness] preset`) and may only tighten it; the tables are listed in [docs/design/CONTRACTS.md](docs/design/CONTRACTS.md) §9 and the `[repeat]` table in [docs/design/EVALUATE.md](docs/design/EVALUATE.md) §6b. `finance-v14.toml` to `finance-v25.toml` are the measured activities of stages 4o–4p, each header listing its steps, costs and what it was for; `finance-v5.toml` to `finance-v13.toml` the held-out screen loop. Their workspaces hold local, git-ignored run records.

For another domain, copy a quickstart. Change `[bench] domain`, give the copy a new `[workspace] root` and `[rsi] scope_id`, and keep `[repeat] split = "train"` until a plan for the test split is sealed and committed.

## Code layout

| Path | What it holds |
|---|---|
| `ouroloop/contracts.py` | identity and the content-addressed object store, events, the file helpers, the error classes |
| `ouroloop/harness/` | the harness package: component kinds, manifest v3, shape and literal checks, the vendored-upstream patch stack, the seeds (`seeds/stirrup/`: Stirrup 0.2.0, its adapter, the write guard, `plan_changes`) |
| `ouroloop/runtime/` | one attempt: the gateway, the broker, the worker and its runner protocol, egress, the runner shell, the trace |
| `ouroloop/evaluate/` | splits, hard limits, attempts and results, the paired gate, the sealed final, the sealed repeat plan (`repeat.py`), the budget book and metering, releases |
| `ouroloop/improver/` | R's session and prompts (`prompts/repeat.md` for the per-task loop, `propose.md` for rounds), the per-task loop and its library (`repeat.py`), the verification ladder (`verify/`), the held-out screen, the trial, the frontier, the ledger |
| `ouroloop/memory/` | the memory component: the ReMe sidecar, its bridge and rules, retrieval, snapshots |
| `ouroloop/activity/` | config and presets to K, the workspace, the phases, the startup checks, the report, the `ouroloop` CLI |
| `ouroloop/adapters/automationbench.py` | tasks, business tools, the official scorer, the rejected-items and world-sources views (imports nothing of the framework) |
| `ouroloop/providers/` | `claude_code`, `openai_compat`, `anthropic` |
| `ouroloop/train/` (extra `[train]`) | the training and decision stack, off the benchmark path |

## Documents

The design documents are in Chinese.

- [docs/DESIGN.md](docs/DESIGN.md): the architecture (v2.1) and the amendments each measured run forced; §6.23 the repetitive stream, §6.24 the rejected items, the world's sources and R building tools.
- [docs/PLAN.md](docs/PLAN.md): where things stand, the rules, the stages, and what waits for the user. Start here.
- [docs/RESULTS.md](docs/RESULTS.md) and [docs/JOURNAL.md](docs/JOURNAL.md): the results, and a log of every change and run with its numbers, newest first.
- [docs/design/](docs/design/README.md): the module designs and interfaces; [docs/history/](docs/history/): pre-registered plans, analyses, the framework survey and announcement drafts.

Files removed in the cleanups of 2026-10-08 and 2026-10-09 are readable at tags `archive-2026-10-08` and `archive-2026-10-09`.

## License

MIT, see [LICENSE](LICENSE). The Stirrup seed is vendored under its own license in `ouroloop/harness/seeds/stirrup/vendor/stirrup/LICENSE`.
