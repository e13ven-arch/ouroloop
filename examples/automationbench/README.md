# AutomationBench runs

The upstream benchmark ([zapier/AutomationBench](https://github.com/zapier/AutomationBench)) is a separate
runtime. ouroloop imports it lazily, so nothing here changes ouroloop's own dependencies; the adapter is
`ouroloop/adapters/automationbench.py` and the phases are `ouroloop/bench.py`.

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

`ouroloop bench --phase smoke --checks-only` calls no model and prints exactly what is missing.

## What the smoke phase establishes

1. **Which upstream.** Checkout path, git commit, package version, Python, toolset, and whether upstream's own
   runner was importable.
2. **Frozen task groups.** `<workspace>/bench/marketing/splits.json` holds one family per task (upstream ships no
   variant field, so a family keeps a task's own attempts, branches and repeats on one side of the split) and the
   train/dev/test assignment. Re-running reads the frozen file; a task whose upstream fingerprint changed is
   reported as drift, never silently regrouped.
3. **The same score as upstream.** The official `partial_credit` and `task_completed_correctly` are called on the
   state shape upstream's runner builds. Where `verifiers` is installed, the rubric entry point and the service
   gate are compared with upstream's. A write to an unrelated service is shown not to move the score, so a run
   that only narrates success scores what it actually wrote.
4. **Nothing grading-related reaches the agent.** The agent is given the upstream prompt and the tool schemas,
   and the audit fails on an assertion, an expected value or the seeded state appearing in either. Assertion
   values that also occur in the prompt are listed for review: a task may legitimately say what to write.
5. **Simulated actions stay simulated.** A task's world is subscribed only to the services it seeds, asserts on or
   grants tools for, so upstream refuses anything else with a credentials error. On top of that an episode hides
   `OPENAI_API_KEY` while a tool runs, so a simulated ChatGPT action takes upstream's canned path instead of
   calling a live API.
6. **One attempt end to end.** A `TaskResult` with the official metrics, the tool trace, usage, the termination
   reason and the session id its transcript is under.

Cost is not measured: `cost_usd` is `null` and the summary says on how many attempts it is missing. It is not
reported as zero.

## The later phases

`baseline`, `collect`, `improve`, `research`, `evaluate` and `report` are implemented and wired to the same driver.
Three of them run against frozen plans rather than config settings — a development task plan, a campaign plan and a
sealed finalization plan — which are objects in the workspace, created through the Python API
(`bench.freeze_task_plan`, `bench.seal`) and named under `[rsi]`. A phase whose plan, model table or price snapshot
is missing says which piece is missing before it calls a model.

## The first release's automatic loop

`improvement.toml` is the complete, key-free config for the first release: GLM-5.3 as the pinned actor, the
`marketing_harness_v1` preset, the package limits, the isolated worker and a USD budget. One command runs the
whole loop and resumes it after an interruption:

```bash
docker build -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .     # once, from the repository root
ouroloop bench --config examples/automationbench/improvement.toml --phase cycle
```

`cycle` freezes the plans `[rsi]` does not name, runs the starting version's baseline (train and dev), a worker
segment, the planned research slot, and one more segment that shows whether an installed R is used, then writes the
report. It never runs the final split. Without Docker, narrow `[harness_evolution] components` to
`["memory", "skills"]`; with code components and no worker image, `freeze` and every paid phase refuse to start.

## Not established here

The smoke phase makes no claim about capability, and neither does a phase that runs. Nothing here has been run with
a real model: there are no baselines, no frontier measurements and no C1 to C4 results in this repository. Local
public-task scores are also not official leaderboard scores: the leaderboard runs a private task set.
