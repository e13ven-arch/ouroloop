# Results

## v0.1: Claude Opus 5.5 on AutomationBench Marketing (2026-10-04)

The framework's automatic loop found one change from training failures. On held-out test tasks, that change raised the same model's pass rate from **41.3% to 54.3%**:

- the difference is **+13.0 points**, with a 95% interval of **[+2.2, +26.1]**;
- partial credit rose from 0.844 to 0.881;
- the cost per attempt stayed the same.

The comparison plan was sealed and committed (`663f930`) before any test task was run. No human wrote or chose the change.

| Version | Test pass rate (23 tasks × 2 runs) | Partial credit | Cost per attempt | Steps per attempt |
|---|---|---|---|---|
| W0, the starting version | 41.3% | 0.844 | $0.437 | 9.5 |
| W\*, after automatic evolution | **54.3%** | **0.881** | $0.425 | 8.8 |

- **Paired difference:** +0.130, bootstrap over task families, 2000 replicates, 95% interval [+0.022, +0.261]. The claim C1, same-model gain, is *supported*.
- **Per task:** 6 tasks improved, 1 got worse (vendor_management, from 2/2 to 1/2), and 16 stayed the same.

### What was measured

- **Benchmark.** AutomationBench Marketing has 100 business-workflow tasks. Each task is scored by its own assertions against the final state of simulated SaaS APIs: Gmail, Slack, HubSpot, Google Sheets and others.
- **Splits.** A fixed hash, seed 1, splits the tasks into 52 train, 25 dev and 23 test. No two tasks are close enough to count as variants, so each task is its own family.
  - The research agent R reads train failures in detail.
  - Dev decides installs, and R sees only aggregate dev scores.
  - Test was run only for this final comparison.
- **Model.** Claude Opus 5.5 (`claude-opus-5-5`, effort medium) is both the actor and the research agent R.
  - It is called through headless Claude Code (`claude -p`), one fresh process per call, with no tools and no local customization, on a Claude subscription login. See the [`claude_code` provider](modules/01_POLICY_RUNTIME.md).
  - Tool calls are structured JSON output, not native tool use.
  - The CLI also attaches the account's e-mail address and the date. The actor's format note says these are not part of the task.
  - These scores are therefore not directly comparable with published native tool-use results.
- **Limits per attempt.** 50 steps, 200 tool calls and 900 seconds, the same for every version.

### What the loop did

These are segment-0 of the cycle: three worker rounds. In each round, R wrote one note per failed train task, summarized the notes into one proposal, and the gate compared the proposal with the current version on all 25 dev tasks.

| Round | R's proposal | Dev gate |
|---|---|---|
| 1 | An after-tool hook telling the actor to re-read results it had "not seen" | Rejected: 36% → 40%, below the 5-point minimum |
| 2 | A rewritten end-of-task check, as a five-item checklist (below) | **Accepted:** 36% → 64%, interval [+12, +44], no regressions |
| 3 | A before-tool hook that pauses the first write of each kind for a reconciliation check | Rejected: 60% → 60%, two regressions |

**Round 2: the accepted change.** It is a new text for the actor's end-of-task check. Before finishing, the actor checks five things:

1. **Instruction sources.** List every source of instructions, and treat a later amendment as adding to the rules unless it says it replaces them.
2. **Truncated reads.** Confirm any truncated read was re-read.
3. **Requirements.** Name the successful tool call behind every requirement: recipients, channel, tracking codes, counts, totals and dates. A chat reply is never a sent message.
4. **Rows.** Account for every in-scope row, either included or excluded by a quoted rule.
5. **No unfinished work.** Never end on a question with work left undone.

The baseline's failures were mostly of these kinds: a report e-mail that was never sent or went to Slack instead, the wrong one of two policy versions or tracking codes, and wrong counts or rows.

**Round 1 exposed a defect in the framework.** R misread the clipping marks in its own failure summaries as cuts the actor had seen. The actor always received whole tool results. So R wrote a hook that falsely told the actor it had missed text, after every long result. The gate rejected it.

The summaries and the package API now say whose cuts these are (`29fbc34`). The process was restarted with the fix before round 3. Rounds 1 and 2 ran with the old summaries. The fix took no part in choosing W\*.

**Other measurements:**
- W0 was measured on dev three times and scored 36% each time.
- W\* was measured on dev twice and scored 64% and 60%.
- After the install, 13 of the 30 failed train tasks passed when they were run again.
- The dev gains are selection-biased: the gate picks the winner. The test result above is the claim.

### Cost and time

All costs are notional API-equivalent dollars at claude-opus-5-5 list prices. The book matched the cost the CLI reported for every call. The calls ran on a subscription, not a metered key.

| Phase | Attempts | Model calls | Cost |
|---|---|---|---|
| Baseline, W0 on train and dev | 77 | 670 | $32.35 |
| Evolution, segment-0 (3 rounds, gates, reruns, R's notes and proposals) | 180 | 1,599 | $81.31 |
| Final, on test | 92 | 845 | $39.64 |
| **Total** | **349** | **3,114** | **$153.30** |

The runs took about 8.6 hours of serial attempts.

### Limits of this result

- **Scope.** One model, one domain (Marketing) and one evolved change. The test set is small (23 tasks), so the interval is wide. The lower bound, +2 points, is the conservative reading.
- **Recursion not yet tested.** The recursive part of the framework, R improving its own research prompts (claim C4), was not run here. This activity's budget did not leave room for the research slot. That is the next version's run, together with an independent repeat of this gain.
- **No frontier comparison.** No frontier model was run (C2). Leaderboard figures are cited separately, never mixed with local runs.

### Reproduce

The commands below run from the repository root. The config, its sealed plans and the final plan are committed in [`examples/automationbench/claude.toml`](../examples/automationbench/claude.toml). The step-by-step protocol is in [ROADMAP §3, step 2a](ROADMAP.md).

```text
claude auth login                       # a Claude subscription; turn extra usage off
ouroloop bench --config examples/automationbench/claude.toml --phase cycle --until segment-0
ouroloop bench --config examples/automationbench/claude.toml --phase evaluate
ouroloop bench --config examples/automationbench/claude.toml --phase report
```

A new workspace makes its own choices. R's proposals will differ, and a reproduction measures the loop, not this particular change.
