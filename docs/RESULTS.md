# Results

## Summary

Three sealed finals so far. In each, an automatically evolved version W\* was measured against the same starting version W0 on a held-out test split, under a plan sealed and committed before any test task was run: v0.1 and v0.2 on Marketing (23 tasks × 2 runs), v0.4 on finance (25 tasks × 3 runs). v0.3 and v0.5 have no final.

| Activity | W0 on test | W\* on test | Paired difference, 95% interval | Verdict |
|---|---|---|---|---|
| v0.1 (segment-0 only) | 41.3% | 54.3% | **+13.0** [+2.2, +26.1] | C1 supported |
| v0.2 (full activity with the research slot) | 47.8% | 52.2% | **+4.3** [−10.9, +19.6] | C1 inconclusive |
| v0.1 and v0.2, per task averaged over the two activities (computed after the fact, not a sealed plan) | 44.6% | 53.3% | +8.7 [−2.2, +20.7] | — |
| v0.3 (five domains, one shared package) | — | — | — | No final: no change was installed, so by the rule fixed before the run none was run; the new domains' test split is unread |
| v0.4 (finance, one single-domain activity) | 54.7% | 62.7% | **+8.0** [−4.0, +22.7] | C1 inconclusive; cost per attempt −22%, significant |
| v0.5 (support, the v0.5 framework against v0.4's in two arms) | — | — | — | No final: the user stopped the comparison on 2026-10-08 after both baselines and the experience-library phase; H1 and C1 are untested and support's test split is unread |

What this says: in all three finals the loop found changes from training failures alone that raised the held-out score, by +13, +4 and +8 points. Only the first was large enough for 23–25 test tasks to call significant; the three estimates agree with each other within the noise. v0.4's change also made each attempt 22% cheaper, which is significant on its own. The recursive part, R improving its own research prompts (C4), has not been shown: in v0.2's research slot neither R0 nor the R1 it proposed added anything to W\*, so there was nothing to compare. v0.3 and v0.5 add no held-out number and change none of the rows above. Over five domains with one shared package, v0.3 passed no change through the dev gate in three rounds. v0.5's two-arm comparison on support was stopped before any final, so its numbers are train and dev process evidence only.

## v0.5: support, stopped after the experience-library phase (2026-10-08)

v0.5 adds two parts to v0.4's single-domain loop. Both appear only under the preset `automationbench_harness_v5`; the rules are in [design/V0_5_PLAN.md](history/V0_5_PLAN.md).

- **A method library.** R writes read-only, deterministic `m_` package tools. Fixed code learns from paired train rewards where each one should be used and freezes that as a table.
- **An experience library.** Entries are distilled from train attempts the grader verified and retrieved per task for the actor, leaving out the task's own entries. They are credited and retired by reward. The whole library goes through the gate as one candidate and is frozen before a final.

v0.5 was merged into `m1-automationbench` (`7e7732e`). It is not released: there is no v0.5 final and no `v0.5.0` tag.

**The planned test.** Two arms on support were frozen on 2026-10-07, before the first payment (`e414f7c`). One ran the v0.4 framework (`claude-v4-support.toml`, plan `c978e0a1…`) and the other the v0.5 framework (`claude-v5-support.toml`, plan `98874475…`). Both had the same split (`3da11232f5d5ff38`), the same schedule and the same budget: a $600 root with a $160 final share. The claim, H1, was a per-task comparison of W\*_v5 with W\*_v4 on support's 25 test tasks × 3; it would hold if the lower end of the 95% interval was above 0.

**Baselines (W0, both arms).** The two arms start from the same place.

| Arm | Train | Dev, three readings | Cost |
|---|---|---|---|
| v0.4 | 18/50 (36%) | 8, 9, 9 = 26/75 (34.7%) | $56.19 |
| v0.5 | 19/50 (38%) | 10, 9, 8 = 27/75 (36.0%) | $56.97 |

**The experience-library phase (v0.5 arm, `--phase library`, $81.86).**

- **The stream.** Each of the 50 train tasks ran once, on its first encounter with the library:
  - D = 3.0: the library fixed 4 tasks and broke 1, counted as paired effects. B = 1.
  - The two halves moved 0.00 → 0.12; the probability that the second half is better is 0.89 (bootstrap).
  - The rank correlation of the credit was 0.2.
  - Entries: 7 active, 35 retired, 45 dropped at the checks; 1.24 entries were shown per attempt.
- **The snapshot round at n = 50.** The screen gave 2 fixes and 1 sample regression. The paired dev gate on 25 tasks then read:
  - first reading +0.24;
  - confirmation +0.08, which clears `min_gain` 0.05 on its own;
  - pooled +0.16, interval [+0.047, +0.287];
  - no regressions;
  - cost per attempt $0.407 against $0.435 (−6.6%, interval excludes 0).

  The snapshot was installed as release `9f07d95032b4`, six library-\* skills.

**Stopped.** On 2026-10-08, after this phase, the user stopped the comparison: "没有可用基线没关系，能够改进的地方都改进掉，看的是最终结果" (it does not matter that there is no usable baseline; improve everything that can be improved; what counts is the final result).

- **Not run:**
  - the v0.4 arm's three R rounds and its final;
  - the v0.5 arm's three R rounds with the method library, the method fit (`--phase learn`) and its final.
- **Not sealed:** neither final plan.
- **Not tested:** H1 and each arm's C1. Support's test split has not been read.

### What it shows, and what it does not

- **Process evidence only, not a claim.** All three pre-registered process readings for the experience library held (V0_5_PLAN §3): it is effective on train (D ≥ +2), it has a learning curve (P ≥ 0.8), and its credit carries information (rank correlation > 0). They are noisy, with one reading per train task.
- **The dev gain is selection-biased.** The gate used it to select, and it fell from +0.24 on the first reading to +0.08 on the confirmation. The true effect is probably below +0.16; only a test final could tell, and none ran.
- **The method library has no readings.** Its two readings ("R can build a usable method", "it learned where to use it") need the R rounds and the fit, which did not run.
- **What follows from this.** The slimming plan's third batch had a rule fixed before the stop: without an H1 reading, each library is judged on its own process readings (design/SLIMMING_INVENTORY.md（tag archive-2026-10-09） §3).
  - The experience library passed both of its readings. It is kept, behind a preset switch and off by default, as the memory base for later work.
  - The method library has no readings, so under the same rule it is cut. This is a proposal, and the user may veto it.

### Cost and records

- **Cost.** The v0.4 arm spent $56.19 and the v0.5 arm $138.83 ($56.97 + $81.86), $195.02 together. These are notional API-equivalent dollars at list prices, as above.
- **Records.** The two workspaces, `workspace-claude-v4-support` and `workspace-claude-v5-support`, are not committed; they stay as local records.
- **Code.** No `.py` or `.toml` file changed between the freeze (`e414f7c`) and tag `archive-2026-10-08`, so that tag holds the code both arms ran. Each arm's report re-renders there with `ouroloop bench --config examples/automationbench/claude-v5-support.toml --phase report`, or the same with `claude-v4-support.toml`.
- **H1 script.** `support_h1.py` was never run; it is at that tag only.

What comes next is in [PLAN.md](PLAN.md).

## v0.4: finance, one single-domain activity (2026-10-07)

v0.4 went back to v0.1's shape, one activity per new domain, and fixed the framework defects the v0.3 diagnosis found:

- R's inputs and its legacy prompt: an endpoint error table from the train trajectories, passing trajectories beside the failing ones, a failure direction in each note, and a method text that asks for k candidates in place of the old one-change prompt;
- literal false rejections;
- a screen blind to harm: a 6-task passing sample redrawn each round, with at most 1 sample regression allowed;
- a single control reading, replaced by three W0 dev readings.

The final also reports W0's pass@k as an equal-cost reference. The rules are in [design/V0_4_PLAN.md](history/V0_4_PLAN.md).

- **Config:** [`examples/automationbench/claude-v4-finance.toml`](../examples/automationbench/claude-v4-finance.toml).
- **Workspace:** `workspace-claude-v4-finance`, not committed.
- **Finance split:** 50 train, 25 dev, 25 test, the same split v0.3 froze (`4fd91eaf9fb8daf5`). Its test was unread until this final.
- **Tag:** `v0.4.0` (`7f65947`). It is not yet merged into main.

| Version | Test pass rate (25 tasks × 3 runs) | Partial credit | Cost per attempt | Steps per attempt |
|---|---|---|---|---|
| W0 | 54.7% (41/75) | 0.843 | $0.482 | 11.1 |
| W\* | **62.7%** (47/75) | **0.915** | $0.378 | 9.4 |

- **Paired difference.** +0.080 with a 95% interval of [−0.040, +0.227] (bootstrap over tasks, 2000 replicates, seed 1). That is *inconclusive* by the plan sealed before the run (`68c9efd`, `finalization:sha256:2af18001…`).
- **Per task.** 5 tasks improved, 3 got worse and 17 were unchanged.
  - Three fixes trace to rules in the memory: subscription_billing 0/3 → 3/3, wave_freelance_invoice 0/3 → 3/3 and monthend_journal_entries 1/3 → 3/3.
  - One break also traces to it: wave_expense_categorization 3/3 → 1/3.
- **Cost per attempt.** The paired difference per task is −$0.105, interval [−$0.145, −$0.070]. That is significant, the only significant result; W\* was cheaper on 22 of the 25 tasks.
- **Equal-cost reference.** W0's pass@1/2/3 is 0.547 / 0.613 / 0.640. Pass@2 and pass@3 are oracle bounds, since a grader picks the best attempt. W\* in one attempt (0.627) matches W0's oracle best-of-2 at 39% of its cost.

### What the loop did

| Step | Outcome |
|---|---|
| Baseline | W0 train 25/50 (50%). Dev three readings 9, 8, 9 (36%, 32%, 36%); 16 of the 25 dev tasks failed all three and 8 passed all three. $57.92 |
| Round 1 | **Accepted:** `source_memory`, a memory. It says to list every sheet and source, infer dates from the data and handle each policy branch. Screen: 9 fixes, 1 sample regression. Gate against the three W0 readings: dev 34.7% → 46.0%, pooled +11.3 (first reading +9.3, confirmation +13.3), 1 regression (xero_po_matching), cheaper per attempt ($0.37 against $0.44) |
| Round 2 | Rejected: deliverable_scope, dev 36% against W1's 46% (−10 points, 2 regressions) |
| Round 3 | Rejected: send_scope, dev 40% against 46% (−6 points) |

W\* is therefore W0 plus one memory file, with no hook, skill or tool.

### What it shows, and what it does not

- **The change worked as written.** W\* used the memory's sheet range in all 110 of its sheet reads. The share of attempts with a Slack search rose from 7/75 to 49/75.
  - The gain comes from 2–3 of the memory's roughly 15 rules: search Gmail and Slack by topic for instructions, and send each person an explicit e-mail.
  - The cost drop comes from about half as many endpoint-catalogue searches (10.3 → 5.7 per attempt).
- **Not significant, and not expected to be.**
  - At +8 points, 25 tasks × 3 has a power of about 0.20.
  - 76.5% of the variance of the paired difference lies between tasks, so more repeats would not help; 80% power needs about 160 tasks.
- **Dev transferred to test better than in v0.1 and v0.2.** The gain was +11.3 on dev and +8.0 on test. Normalized by the headroom left above W0, that is 0.173 on dev and 0.176 on test, with no visible winner's curse.
- **A pooled reading, computed after the fact and not a sealed plan.** v0.1/v0.2's per-task average on Marketing (23 tasks) pooled with finance (25 tasks) gives +8.3 [0.0, +18.1] on 48 tasks.
- **Not shown:**
  - C4: no research slot ran.
  - Any tool: R proposed none.

The full post-hoc analysis is in [history/FINANCE_V04_ANALYSIS_2026-10-07.md](history/FINANCE_V04_ANALYSIS_2026-10-07.md).

### Cost

| Phase | Cost |
|---|---|
| Baseline (train 50 + dev 25 × 3) | $57.92 |
| Three rounds: screens $36.61, gate candidate arms $28.28, confirmation $9.19, R's sessions $8.89, re-measurement after the install $4.35 | $87.32 |
| Final (150 attempts; $61.10 estimated when sealed) | $64.49 |
| **Total** | **$209.73** |

### Reproduce

The config and the sealed final plan are committed; the workspace is not. To rerun the same code:

1. Check out tag `v0.4.0`.
2. Copy the config. Give the copy its own `[workspace] root` and `[rsi] scope_id`, as in ROADMAP §3, step 2a（tag archive-2026-10-09）; the committed config points at the original workspace.
3. Run `ouroloop freeze` on the copy, and add the `[rsi]` lines it prints.
4. Run `--phase cycle`.
5. Fill `[freeze.final]` with W\*, freeze and commit the plan, then run `--phase evaluate`.

A new workspace makes its own choices.

## v0.3: five domains, one shared package (2026-10-06)

v0.3 rebuilt the framework (the agent program as part of the package, new gate statistics, R as a coding session, multi-domain activities; see [What v0.3 adds](#what-v03-adds)) and ran three experiments with Claude Opus 5.5 through headless Claude Code on the subscription. None of them read a test split. There is no new held-out claim.

| Experiment | Tasks | Question | Outcome | Cost |
|---|---|---|---|---|
| Ceiling (step 4) | 13 Marketing tasks v0.2's W\* still failed (6 dev, 7 train), 2 attempts per arm, effort high | Can loop-level mechanisms reach W\*'s remaining failures? | Verdict **training**: a hand-written package fixed 1 of the 8 tasks W\* failed in every attempt and broke 2 | $45.28 |
| Pilot (stage 0) | operations + Marketing, 12 train / 26 dev | Does the new R path work with a real model? | Yes, end to end; one change installed | $47.11 |
| Five-domain activity (stages 1–2 and one extension round) | Marketing + sales, support, HR, finance: 252 train / 125 dev | Does one evolved package raise the score across five domains? | Three rounds, all rejected; nothing installed, so no final | $424.61 |

All costs are nominal API-equivalent dollars at list prices; the calls ran on a Claude subscription.

### The ceiling experiment

Before the five-domain run, a fixed question about v0.2's result: are the Marketing failures W\* still has within reach of the loop at all? A hand-written package (W\*'s two changes plus four loop-level mechanisms: full paged reads, an evidence table before the first write, a check of every executed write before finishing, and effort high on every call) ran against W\* itself on the 13 tasks W\* had failed. The decision rule was committed before the run (`882fed5`): "search" if the hand-written package fixed more than half of the tasks W\* failed in every attempt, and more than it broke; otherwise "training".

| Arm | Pass rate (13 tasks × 2) | Cost per attempt | Cost |
|---|---|---|---|
| W\* | 6/26 (23.1%) | $0.685 | $17.82 |
| Hand-written package | 6/26 (23.1%) | $1.056 | $27.46 |

- Of the 8 tasks W\* failed in every attempt, the hand-written package fixed 1 (`featured_snippet`, 0/2 → 1/2); "search" needed 5. It broke 2 (`voice_of_customer`, `google_ads_pause_low_ctr`).
- The 4 dev tasks W\* failed on every v0.2 reading stayed at 0/2 in both arms.
- The mechanisms did run: the hand-written arm made 53 auxiliary calls (the evidence table and the check before finishing). All 52 attempts were valid, and none hit a hard limit.
- **Verdict: training.** On Marketing, what W\* still fails is out of reach of these loop-level mechanisms; the remaining weight is on the second release's training work. The limits: one hand-written package, 13 tasks, 2 attempts per task and arm, and only Marketing after evolution. It says nothing about the gain available on a new domain starting from W0.

Records: `examples/automationbench/workspace-claude-ceiling/ceiling/result.md` (the workspace is not committed). Rule and task set: [ceiling/README](../examples/automationbench/ceiling/README.md) and `ceiling/tasks.json`. The hand-written package, the copy of W\*'s package, the run script `ceiling_run.py` and its config `claude-ceiling.toml` were removed from HEAD on 2026-10-08; they are at tag `v0.3.0`.

### The pilot

A small activity on two domains (operations, which the main activity does not use, and Marketing) ran one real round of the new R path: R's coding session, a declared candidate, the train-side screen, a confirmed dev gate and the install.

- Baseline W0: train 5/12, dev 11/26 (42.3%).
- R's session made 1 of the 3 candidates allowed and read no trajectory: a memory checklist (gather the full evidence before any write). The screen gave it 4 net flips.
- The gate's first reading and its confirmation re-run were both +26.9: dev **42.3% → 69.2%**, no regressions (Marketing +30.8, operations +23.1). It was installed.
- Cost $47.11: baseline $15.27, the round $31.84.

The +26.9 on 26 dev tasks is a process figure, not a claim. Because R used 1 of 3 candidates and read no trajectory, its session text was changed before stage 2 to ask for k candidates on different causes and a trajectory read for each.

### The five-domain activity

Config: [`examples/automationbench/claude-v3.toml`](../examples/automationbench/claude-v3.toml); workspace `workspace-claude-v3`.

- **Domains.** Marketing keeps v0.2's split (52/25/23, pinned by hash); its test has been read three times and is marked spent, so no new claim reads it. Sales, support, HR and finance (100 tasks each) are split 50/25/25, stratified by the kinds of scored assertion and the tool family, read from the task definitions only. Pooled: train 252, dev 125; the claim's test is the four new domains' 100 tasks.
- **Plan.** One worker segment of 6 rounds with patience 2, up to k = 3 candidates a round, the dev gate on all 125 tasks (`min_gain` 0.05, confirmation re-run before an install). The research slot (C4) was deferred to a separate workspace and did not run. K's effort ceiling is medium, W0's pin, so a candidate cannot buy a gain with effort. Budget root $1,500 with a $390 final share; 8 attempts in flight.
- **Stop rule, fixed before the run.** No install, no final: the final would compare W0 with W0.

**Baseline (W0):**

| Domain | Train | Dev |
|---|---|---|
| Marketing (test spent) | 48.1% | 28.0% |
| Sales | 34.0% | 48.0% |
| Support | 46.0% | 36.0% |
| HR | 24.0% | 60.0% |
| Finance | 54.0% | 32.0% |
| **All** | **41.3%** (104/252) | **40.8%** (51/125) |

377 attempts, all valid, $175.97 ($0.466 per attempt). Each domain has only 25 dev tasks, so the gaps between a domain's train and dev rates are small-sample noise, not differences in difficulty.

**Rounds.** Per-domain columns are points on that domain's 25 dev tasks.

| Round | R's session | Gated candidate | Dev gain (125 tasks) | Marketing | Sales | Support | HR | Finance | Regressed tasks | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 13 turns, 4 trajectory reads, 3 candidates | rulesmem (memory) | +3.2 | +24 | −16 | −4 | +8 | +4 | 10 | Rejected |
| 2 | 5 turns, 2 trajectory reads, 3 candidates | writemem (memory) | +2.4 | +16 | −8 | 0 | 0 | +4 | 6 | Rejected |
| 3 (extension) | 7 turns, 3 trajectory reads, 3 candidates | withhold (hook) | +0.8 | +12 | −4 | 0 | −4 | 0 | 7 | Rejected |

In tasks, Marketing gained +6, +4 and +3; the four claim domains together moved −2, −1 and −2 of 100. No first reading reached `min_gain`, so no confirmation re-run was paid.

- **Rounds 1–2** ran as stage 2. Patience 2 ended the segment after round 2. The cause found then: R's notes took the first 30 failures in the pooled frozen order, so round 1's notes were 27 Marketing + 3 sales and round 2's were all sales; R saw none of the support, HR or finance failures.
- **The fix, offline:** notes stratified by domain (6 per domain), R's ledger showing each domain's gate outcome (counts and means, no dev task ids), and a recorded extension of a finished segment (`--phase improve --rounds N --reason`, logged as `schedule_extended`). Round 3 is the one extension round run under it. It was rejected too.
- A subscription session limit paused stage 2 once; the same command resumed it without paying twice. Three calls in the baseline settled as `unknown` and hold $21.83 of the root as reservations.

### What it shows, and what it does not

- **No new held-out claim.** Nothing was installed, so by the pre-registered rule no final was run. The four new domains' 100 test tasks remain unread, and the $390 final share is unspent.
- **Five domains with one shared package did not produce an accepted change.** All three gated candidates gained on Marketing and were flat or negative on the domains the claim reads.
- **Marketing gains reproduce on dev,** as in v0.1, v0.2 and the pilot. The diagnosis finds the two memory candidates' Marketing gains partly real (5 of rulesmem's 6 Marketing fixes are on tasks W0 had passed 0 of 5 times) and partly the control: Marketing's control reading was 7/25 against a historical W0 mean of 9.6. These are single dev readings on a domain whose test is spent.
- **The ceiling verdict stands for Marketing:** training.
- **Not shown:** that the loop cannot improve the new domains. Three rounds ran, each read against one control reading, and the diagnosis below finds causes in the framework. C4 was not run.

### Diagnosis

A diagnosis of the three rounds, from the train and dev records only, is in [history/DIAGNOSIS_FIVE_DOMAIN_2026-10-06.md](history/DIAGNOSIS_FIVE_DOMAIN_2026-10-06.md). Its findings, strongest evidence first:

1. **One shared package across heterogeneous domains.** Each candidate's single mechanism ("read more, act more") fits Marketing's tasks and hurts where W0's restraint is right; about half of the sales and HR regressions fail "must not" assertions.
2. **One-reading control noise.** All three gates read the same single W0 reading. Its noise is about ±3.9 tasks on 125 (about ±7–9 points on a 25-task domain), so the gains of 4, 3 and 1 tasks are 1.0, 0.8 and 0.3 sd. R was shown per-domain numbers with no noise band.
3. **A screen blind to harm.** The screen re-ran only tasks R had notes on and a fixed 5-task passing sample with no HR task: rulesmem showed 0 regressions there and 10 on dev.
4. **R's inputs and guards.** R's system prompt was still the legacy "propose at most one change" text, contradicting the session's k candidates. The train-literal leak check refused 14 of 20 drafts, all on ordinary words or constants (such as 'Not Sent' or '3000'), and 14 of R's 25 turns went to blind revisions.

The next version was first to shard the search by domain, with routing. That design was superseded before it was built: v0.4 instead ran one single-domain activity per domain (see the v0.4 section above).

### Cost

| Phase | Cost |
|---|---|
| Ceiling experiment (52 attempts) | $45.28 |
| Pilot (baseline $15.27, one round $31.84) | $47.11 |
| Five-domain baseline (377 attempts) | $175.97 |
| Five-domain rounds 1–2 | $170.30 |
| Five-domain extension round | $78.34 |
| **Total** | **$517.00** |

The five-domain activity's own total is $424.61. In rounds 1–2, notes and R's sessions cost $10.70, the train screens $35.94 and the dev gates' candidate arms (250 attempts) $123.67: the gate is most of a round.

### What v0.3 adds

- **The agent program is part of the package** (package API v2), run by a fixed runner shell: a metered model gateway that caps effort, a tool broker, records written by the host, and five hard limits per attempt.
- **Gate statistics and speed:** a pooled control topped up per task; a confirmation re-run that must clear `min_gain` on its own (an A/A replay of W0's recorded readings accepts 0/20 on dev and 0/12 on test; the first version of the rule accepted 4/20); a magnitude rule for regressions; a cost term; `min_tasks`; 8 attempts in flight.
- **R as a bounded coding session:** up to k candidates, each with a declaration (mechanism, site, carrier, predicted tasks, trigger); an engine ledger of every candidate's fate; a train-side screen before the dev gate; raw train trajectories and a dev failure-type histogram as inputs.
- **Multi-domain activities:** per-domain stratified splits, Marketing's split pinned and its spent test kept out of claims, per-domain reports and gate receipts, and final-share checks before a plan is sealed and before the final pays.
- **Staged runs** (`--until`), notes stratified by domain, per-domain gate outcomes in R's ledger, and recorded extensions of a finished segment.
- The offline suite: 1140 tests pass.

## v0.2: the full activity (2026-10-04)

The same model, prompt, preset and limits as v0.1, in a workspace of its own with a budget large enough for the research slot. Config: [`examples/automationbench/claude-full.toml`](../examples/automationbench/claude-full.toml). The final ran from `claude-full-final.toml`, which was removed from HEAD on 2026-10-08 and is at tag `v0.2.0`.

| Version | Test pass rate (23 tasks × 2 runs) | Partial credit | Cost per attempt | Steps per attempt |
|---|---|---|---|---|
| W0 | 47.8% | 0.866 | $0.452 | 9.8 |
| W\* | **52.2%** | **0.880** | $0.570 | 9.9 |

- **Paired difference:** +0.043, bootstrap over task families, 2000 replicates, 95% interval [−0.109, +0.196]: *inconclusive*.
- **Per task:** 4 improved, 2 got worse, 17 unchanged.
- **The same W0 scored 41.3% in v0.1's final and 47.8% here.** Two runs of 46 attempts each differ by 6 points with nothing changed, which is the noise floor these finals have.

### What the loop did

| Step | Outcome |
|---|---|
| Baseline | train 25/52 (48%), dev 9/25 (36%, the same as v0.1's W0) |
| segment-0, round 1 | **Accepted:** a pre-write protocol (sources, directives, per-row dispositions, literal values, loop closing), dev 48% → 80% |
| segment-0, rounds 2–3 | Rejected: a longer pre-send audit (76% vs 76%); a code hook comparing each outgoing message with the sheets read (−8 points, 3 regressions) |
| Research slot | R0 read its own experiment log and proposed R1: say what each rejection rules out, prefer narrow triggers, never re-propose a refuted mechanism with more items. Two arms from the same W\*, two rounds each on 12 inner dev tasks: R0 rejected twice (0, −8); R1 rejected twice (0, 0), its proposals at a different site as instructed (a note attached to each sheet read). Both arms sealed the same W\*: **inconclusive**, no outer comparison |
| segment-1, round 1 | **Accepted:** an on_finish hook that fires once per task and makes the actor audit inputs against actions, dev 68% → 84%, no regressions |
| segment-1, rounds 2–3 | Rejected (−8 with 3 regressions; −4) |

W\* is therefore W0 plus the pre-write protocol and the end-of-task audit hook.

**Dev against test.** The gates read W\* at 80–84% on dev and W0 at 36%; on test the same versions scored 52% and 48%. Gate numbers select the winner and are read on 25 tasks, half of them at a time inside the slot; they are process figures, not the claim.

**The research slot.** It ran as designed and compared nothing: with W\* already at 67–80% on the inner dev half, two rounds of 12 tasks did not let either research policy add a change. One task is 8 points on 12, so the inner gate cannot see the small effects that remained. Testing C4 needs more attempts per gate than this activity's budget gave it.

### Two defects the run exposed, both fixed before the release

- **Concurrency.** Attempts of one arm now run four at a time (`[bench] concurrency`, added mid-run and used from the research arms on): 88 s → 27 s of wall time per attempt, the same cost per attempt, no invalid attempts. Research steps stay serial.
- **Reservation headroom.** The first final stopped short: its $50 share held the actual spend ($23 by then) but not the four $7.28 reservation bounds concurrent calls hold at once, and 51 attempts were recorded as `budget_exhausted` in 78 seconds. A refused reservation now waits for a call in flight to settle; only with nothing in flight is a budget spent. A root's limits are fixed once it opens, so the same sealed comparison was run again as `final-2` in a copy of the workspace's object store with a $120 share. The first final's attempts (W\* 38 valid, 10/19 tasks; W0 2 valid) stay on record there. The test tasks had been read once by then; the plan did not change.

### Cost and time

Notional API-equivalent dollars at list prices; the book matched the CLI's reported cost per call.

| Phase | Cost |
|---|---|
| Baseline, segment-0, research slot, segment-1 (5,255 calls) | $303 |
| final-1, stopped short | $23 |
| final-2 (910 calls) | $47 |
| **Total** | **$373** |

The cycle ran from 04:41 to 14:48 with one restart; the finals took about 25 and 45 minutes.

### What v0.2 adds to v0.1

- The gain is real but smaller than v0.1's and not significant on its own; across the two activities the per-task average is +8.7 points with an interval that just reaches zero.
- The same recipe found a different pair of changes, which is what a search is expected to do; both times the accepted changes were end-of-task or pre-write checks, which is where the baseline's failures were.
- C4 is untested, not refuted. The next version needs a research slot whose inner gates can resolve small effects: more attempts per gate, which with concurrency is now affordable in time.

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

The commands below run from the repository root. The config, its sealed plans and the final plan are committed in [`examples/automationbench/claude.toml`](../examples/automationbench/claude.toml). The step-by-step protocol is in ROADMAP §3, step 2a（tag archive-2026-10-09）. To run the code that produced this result, check out tag `v0.1.0` first: the slimming plan (design/SLIMMING_INVENTORY.md（tag archive-2026-10-09）) removes this config and its preset from later code.

```text
claude auth login                       # a Claude subscription; turn extra usage off
ouroloop bench --config examples/automationbench/claude.toml --phase cycle --until segment-0
ouroloop bench --config examples/automationbench/claude.toml --phase evaluate
ouroloop bench --config examples/automationbench/claude.toml --phase report
```

A new workspace makes its own choices. R's proposals will differ, and a reproduction measures the loop, not this particular change.
