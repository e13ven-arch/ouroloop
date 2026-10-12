# AutomationBench runs

The upstream benchmark ([zapier/AutomationBench](https://github.com/zapier/AutomationBench)) is a separate
runtime. ouroloop imports it lazily, so nothing here changes ouroloop's own dependencies; the adapter is
`ouroloop/adapters/automationbench.py` (it imports nothing of the framework) and the phases are
`ouroloop/activity/phases.py` (docs/design/ACTIVITY.md).

## What to install

Clone the benchmark and point `[bench] upstream_path` (or `OUROLOOP_AUTOMATIONBENCH`) at the checkout. Upstream
declares `requires-python >= 3.13`; the pieces this adapter uses are plain pydantic code and import on 3.12 too,
which is what `ouroloop bench` reports so a mismatch is visible rather than assumed.

Into the environment ouroloop runs in:

| Package | Needed for |
| --- | --- |
| `pydantic>=2` | the simulated world state, and the deep copy a branch snapshot uses |
| `datasets` | upstream's own task loader (`get_domain_dataset`), including its noise injection |
| `openai` | importing `automationbench.tools` at all: the package eagerly imports the Zapier tool registry, and one of those modules imports the OpenAI client at module level. No call is made to it. |
| `verifiers` | optional. Upstream's `runner` and `create_rubric` live behind it. Without it the adapter uses its own copies of `strip_none_values` and `compute_allowed_services` and says so; with it, `--phase smoke` checks those copies and the rubric entry point against upstream's. |

The worker image runs the package's agent (the stirrup seed: Stirrup 0.2.0 vendored in the package) and needs
Stirrup's own dependencies, pinned by hash in `docker/worker.requirements.txt`:

```bash
docker build --provenance=false -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .
```

The memory (`[memory] enabled = true`, the `library` phase) runs ReMe 0.3.1.10 in a sidecar with its own venv, built
from the hashed lock `ouroloop/memory/sidecar/requirements-macos-arm64.lock` (161 wheels) together with the pinned
bge-small model; the startup checks refuse a venv that is not the lock (docs/design/MEMORY.md):

```bash
python -m ouroloop.memory setup
```

## A config

A config names the preset it tightens (`[harness] preset = "automationbench_v2"`, or `automationbench_v1` with the
library on by default); every other table may only
lower a cap, raise a floor or narrow a set (`ouroloop/activity/presets.py`, the RULES table). The tables are
`[workspace]`, `[bench]`, `[limits]`, `[model]`, `[research_model]`, `[pricing]`, `[harness]`, `[worker]`, `[memory]`,
`[improver]`, `[gate]`, `[schedule]`, `[budget]`, `[final]` and `[rsi]` (docs/design/CONTRACTS.md §9). The configs of
the activities before stage 1 used other tables; they run at their tags (`v0.4.0`, `archive-2026-10-08`) and were
removed from here.

- `quickstart.toml`: a new single-domain activity on finance with nothing frozen yet, so it touches no existing
  record. Its header lists the steps and their nominal cost.
- `finance-v1.toml`: the first measured activity of the forward design (docs/PLAN.md stage 4). It enables the memory
  and runs the improver's rounds.

## The phases

```bash
ouroloop bench --config C --phase smoke --checks-only   # upstream, frozen split, scorer contract, input audit, the seed's literals: $0
ouroloop bench --config C --phase smoke                 # one dev attempt end to end through the runner shell
ouroloop freeze --config C                              # K, the gate template, the budget root (and a final plan); prints [rsi]
ouroloop bench --config C --phase cycle --until baseline   # W0 on train and on dev control_readings times
ouroloop bench --config C --phase cycle --until library    # the memory's train stream; its snapshot as a candidate
ouroloop bench --config C --phase cycle --until round-0    # one improver round (then round-1, round-2: stop after each)
ouroloop bench --config C --phase gate                   # the release gate: the working line against W0 on dev, once
ouroloop bench --config C --phase release                # no dev in the split: release the working line by decision
ouroloop bench --config C --phase report                # the report from what is on disk; no model is called
ouroloop export --config C --to DIR                     # the released W* as an app
```

`smoke --checks-only` calls no model and writes nothing but the frozen split. Every other phase opens the activity:
the first time it writes `core.json` (K, with the frozen split's hash and the two code versions: what an attempt
runs, and the rest) and the seed release {W0, R0, K}; after that, a config, a code change or a split that gives
another K is refused — a changed K is a new activity (docs/PLAN.md rule 13). With `[rsi] baseline_from` naming
another workspace of the same AttemptIdentity, the baseline imports its W0 readings and pays nothing. The library
step (only with `[memory] enabled = true`; the `automationbench_v2` preset has it off) streams the train tasks once
through W0 with the live library, writes what ReMe distils through the platform's checks, and ends with the readout
and, when D > 0, the snapshot as a candidate package. A round (docs/design/IMPROVER.md §3, v1.2) measures the working
line on the studied part of train (the rest is held out from R), diagnoses its failures into causes, writes R a focus
list, runs R's coding session for one candidate package (one mechanism, as many components as it needs), checks it on
the free rungs (V0 static, V1 package tests, each component's V2: toolcheck / hook replay / skill reach), screens it
once on every held-out train task (V3) and installs it on the working line when the accept rule holds (net held-out
flips ≥ 1, regressions ≤ 1, the held-out score within one of the activity's best); dev is not read inside a round. The
`gate` phase, run once when the rounds are over, reads the working line against W0 on dev and, accepted, promotes it to
the release line (W*), which the final and `export` read. A split without dev (`[bench] ratios` with dev 0: the
two-way protocol of stage 4c) has no gate: `freeze` writes no template, the rounds need none, and `--phase release`
promotes the working line by decision after reading its own train tasks; the final's two arms are then the number
to report, W0 beside W*. A round needs the gate plan `freeze` wrote; it is priced
before its screen is paid, and a resumed round pays only for what is missing. `cycle` stops after
`schedule.patience` rounds in a row without an acceptance; `cycle --until gate` runs the gate after them.

## What the smoke phase establishes

1. **Which upstream.** Checkout path, git commit, package version, Python, toolset.
2. **Frozen task groups.** `<workspace>/bench/<domain>/splits.json` holds the families and the train/dev/test
   assignment. Re-running reads the frozen file; a pinned hash (`[bench] frozen_splits`) must be reproduced; a task
   whose upstream fingerprint changed is reported as drift, never silently regrouped.
3. **The same score as upstream.** The official `partial_credit` and `task_completed_correctly` on the state shape
   upstream's runner builds; a write to an unrelated service does not move the score.
4. **Nothing grading-related reaches the agent.** The audit fails on an assertion, an expected value or the seeded
   state appearing in what the agent is given.
5. **No train answer in the seed.** The seed is searched whole for values that occur in exactly one train task
   (candidates are later searched only on the lines they change).
6. **One attempt end to end** (without `--checks-only`). A `TaskResult` with the official metrics, the transcript
   (`transcripts/<attempt id>.jsonl`: the model calls, the tool calls, the hooks that acted, the end), usage and cost.

## Not established here

The smoke phase makes no claim about capability. Local public-task scores are not official leaderboard scores: the
leaderboard runs a private task set. The outside numbers come only from a sealed final (docs/RESULTS.md).

## The A/A replay (v0.3 step 2)

`aa_replay.py` replays the gate rule (pooled control, confirmation reading, regression as a drop of 0.5, min_gain
0.05) over the readings of the same W0 kept in three v0.1/v0.2 workspaces — every directional pair judged through the
production `ouroloop.evaluate.gate.judge` with the rest pooled as the control, the v0.1/v0.2 rule beside it. No model
is called. The rule accepts 0/20 dev pairs and 0/12 test pairs; the v0.1/v0.2 rule accepted 4/20.

## The ceiling experiment (v0.3 step 4)

A decision record for v0.3; the runner and its config are at tag `v0.3.0`. How it ran, what it cost and the decision
rule are in [ceiling/README.md](ceiling/README.md).
