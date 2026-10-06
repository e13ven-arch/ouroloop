# Synthesis: why three RSI rounds gained only on marketing, and what to change

All five analysts' numbers were cross-checked against the receipts, `events.jsonl` and the config. Nothing under `/Users/leon/workspace/ouroloop` was modified, and no test split was read. My one new computation is a gate simulation at `/private/tmp/claude-502/-Users-leon-workspace-ouroloop/c9591706-bc88-4f6c-9900-8380bdb28477/scratchpad/diag/synth/claimgate.py`. It reuses analyst-e's calibrated noise model.

## (1) Diagnosis: root causes, strongest evidence first

### RC1. No candidate moved the domains the claim reads. Every net gain came from marketing, which is spent.

**Data.**
- Per-domain net tasks from receipts `0bd2e67f…`, `97072c5c…` and `b973b6fd…`:

  | Round | Marketing | Claim domains (sales+support+hr+finance, 100 dev tasks) |
  |---|---|---|
  | r0 | +6 | −2 |
  | r1 | +4 | −1 |
  | r2 | +3 | −2 |

- `claude-v3.toml:59` has `spent = ["marketing"]`. Its header says the claim reads only "test 100 (the new domains' only…)".
- The marketing gains for the two memory candidates are partly real:
  - 5 of rulesmem's 6 marketing fixes are on tasks W0 passed 0 of 5 times historically.
  - Mailbox reads per marketing task rose from 3.0 (W0) to 5.12 (r0) and 4.52 (r1).
  - The tasks fixed are ones whose binding amendments live in email.
- The same "read and act on more" text over-applies in the claim domains. Four r0 sales/support regressions trace to specific rules: create_important_draft, slack_deal_notification, update_contact_phone and hiver_workload_forecast.
- About half of the sales/hr regression events fail negative ("must not") assertions.
- Notes R did see contained the opposite kind of failure: about 5 of 30 over-action notes in each of rounds 1 and 2, mostly sales/hr/support. No candidate addressed them.

**Interpretation.** R's single mechanism ("gather more evidence, act more") fits marketing's task template, the v0.1/v0.2 marketing wins and the pilot (+26.9 on operations+marketing). It is neutral to harmful where W0's restraint is correct. Even a perfect gate would have installed nothing useful for the claim: a claim-domain gate rejects all three on the first reading.

### RC2. The noise in one control reading was read as per-domain signal.

**Data.**
- All three receipts have `control_readings = 1` and share the same W0 reading (run 630a9c7d).
- Three independent estimates of W0's re-run noise agree at about 12% discordance:
  - Marketing's five W0 readings: 15.6% pass→fail, 9.7% fail→pass.
  - The withhold hook stayed quiet in 94 attempts, and its system prompt is byte-identical to W0's, so those attempts are a W0 re-run: 11 of 94 discordant.
  - W0 against r1 and against r2: 15 of 125 discordant each.
- That puts the null standard deviation of the net at about 3.9 tasks (3.1 pp) on 125 tasks, and about ±7–9 pp per 25-task domain. The gains of 4, 3 and 1 tasks are about 1.0, 0.8 and 0.3 sd.
- withhold fired in 31 of 125 attempts. 11 of its 15 flips came from attempts where it never fired, and its net where it did fire was 0.
- Three tasks regressed in all three arms (2 sales, 1 hr). Each is a lucky control pass.
- Marketing's control read 7/25, against a historical W0 mean of 9.6.

**Interpretation.** The ROADMAP conclusion "a change must work in ≥2 domains" and the pattern "sales always regresses" are about half artifact. R was shown these per-domain numbers with no noise band (ledger projection).

Two analysts disagreed here. Analyst A attributed the marketing-up/sales-down pattern mostly to the control. Analysts B, C and E disagreed for the memory candidates, and they are right there. Reconciled view:
- rulesmem: real marketing gain plus real sales harm, with about −8 pp of sales control luck on top.
- writemem: mostly noise plus some real marketing gain.
- withhold: noise.

### RC3. The screen measures in-sample repair and cannot see harm, and it rewards reach.

**Data.**
- All 63 of 63 failing-set tasks were tasks R had notes on.
- In-sample fix rates were 27–44%. On dev, fix rates among W0-failing tasks were 18.9%, 12.2% and 10.8%, against a null of about 10%.
- The passing sample was the same 5 tasks in all 7 screens, with no hr task.
- rulesmem showed 0 of 5 sample regressions on train, then 10 regressions on dev (6 in sales).
- Memory acted in 100% of screen attempts. Hooks acted in 38–60%.
- A null candidate clears `min_net_flips = 1` with probability 0.43–0.57.
- Screens cost $48.59 in total. The three rejected gates cost $182.48.

**Interpretation.** Best-of-k by raw net flips selects broad, always-on text, and the domain trade-off reaches the dev gate unseen.

### RC4. R's inputs and guards push toward bundled, always-on, one-direction text.

**Data.**
- R's system prompt is the legacy `research.propose` (change `d0148797…`). It says "Propose at most one change… most common general mechanism. Prefer… memory notes". The session text asks for k candidates on different causes.
- R read 2–4 failed trajectories per round and 0 passing ones. It read no support or hr trajectory in any round.
- All 9 candidates push "do more". Skills, tools and agent changes: 0 (12 of 78 notes suggested skills).
- The train-literal check failed 14 of 20 drafts. All 14 were vocabulary or constant collisions, such as 'Not Sent', 'Event Types' and '3000' from `[:3000]`. The check reports counts only (`harness_package.py:448–459`, `re.IGNORECASE`).
- Cost of those 14 failures:
  - 14 of 25 R turns went to blind revisions.
  - 2 of 6 hooks died (finishgate, sendcheck).
  - finishgate's trigger was stripped to always-on ("The hook now always pushes back once at finish").
  - rulesmem's examples were removed.

**Interpretation.** These are the upstream causes of RC1's mechanism type. The causal link to outcomes is inferential, which is why this ranks last on evidence. It ranks high on actionability.

## (2) Ranked plan of framework iterations (all generic; test untouched)

| # | Change (module and rule) | Root cause | Expected effect | Extra cost per round | Statistical risk | Frozen plan? |
|---|---|---|---|---|---|---|
| **P1 KEEP** | **Point R at the claim population and give it counter-evidence.** See the detail list below. | RC1, RC4 | Candidates aimed at claim domains, each with a stated scope. The "act more everywhere" bundles that traded sales for marketing should become rarer. | About $0 (direction field in the existing note call) | None on false accepts; the gate is untouched | Changes R's release (R0 prompt) and note selection: record as a deviation like the stratification fix. No A/A needed; the control pool is unaffected because R is not W. |
| **P2 KEEP** | **Make the leak check locatable and precise** (`harness_package.literal_hits` / `describe_hits`). Return file, line, span and literal kind. Match case-sensitively. Exempt pure numbers and values found in ≥2 train tasks or in headers. Keep values unique to a single task forbidden. Regression test: replay the 20 recorded drafts (expect 0 of the 14 false refusals; a planted single-task value must still be refused). | RC4 | Turns go to analysis, not guessing (14 of 25 turns were wasted). Conditional hooks and skills with concrete triggers survive. | $0 | Weaker leak guard. Only train literals are involved, and dev and test are disjoint. | Code change to K's draft guard: record as a deviation. Not a gate rule. |
| **P3 KEEP** | **Grow the pooled control to 3 W0 dev readings, and make the records noise-aware.** See the detail list below. | RC2 | Halves or better the control's share of gain variance. R stops chasing ±2–3-task domain swings. Gives a five-domain A/A set to validate P5. Current-rule null false accepts fall from 1.0–1.6% to 0.2–0.4%. | $117 once (250 × $0.466); $0 per round | Lowers false accepts | The pool rule is unchanged (it already pools valid readings). The extension note "不补测" (no top-up of the control) is reversed: record the decision. Readings are taken unconditionally, before the next gate. |
| **P4 KEEP** | **Rebuild the screen** (`session.passing_sample`, `pick_best`). See the detail list below. | RC3 | At N=15 (analyst C), P(reaching the gate) is 0.20 for rulesmem, 0.47 for writemem, 0.43 for withhold, 0.18 for a no-op and 0.92 for a good candidate. That avoids about 1.9 of 3 null gates (~$115 per 3 rounds). | +$18–28 per round (a screened-out round ≈ $41–51; a gated round ≈ $103–113) | Can reject a good candidate about 8% of the time (13–34% if its true regression rate is 3–6%). Gate false accepts are unaffected because the screen precedes the gate. | `screen.passing_sample` is a K cap that config "may only lower" (`bench.py:1735`), so this changes the preset/K: record as a deviation. |
| **P5 KEEP, conditional** | **Gate on the claim population** (`task_gate.judge`, GatePlan `population`). See the detail list below. | RC1 (validity), power | See the simulation table and retrospective check below. | $0 | Null false accepts in the simulation: 2.9–3.7% (1 control reading), 1.0–1.4% (2), 0.6–0.8% (3). Over 3 rounds at 3 readings, about 2%. | **Yes, a frozen GatePlan change.** Use only if the A/A replay accepts 0 of 6 directed pairs over the 3 W0 readings (dev only; extend `aa_replay.replay`, not its main) and the calibrated simulation is ≤ 0.05. Record as a deviation in 03 §2.1 before the round. Never apply it to past receipts. |

**P1 detail.**
- Replace the legacy `research.propose` (`session.py:906`) with a k-candidate text.
- Each candidate declares:
  - one cause;
  - the trigger, and when it does not apply;
  - target domains, derived mechanically from `predicted_affected`, which must be claim domains;
  - one contrast case, read as a passing trajectory.
- `select_failures` draws notes only from domains not in `spent`. Spent domains act as guards only.
- `TrajectoryReader.listing` adds passing train attempts per domain, preferring tasks with negative assertions.
- R's coverage view gets per-domain W0 train pass rates.
- The note schema gets `direction` (omission or over-action).
- Admission check: at least one trajectory read among the candidate's `predicted_affected` tasks.

**P3 detail.**
- Pool 3 W0 dev readings.
- In the receipt and `ledger_projection`:
  - split flips into activated and quiet attempts (hook action ≠ continue, `read_skill`, package tool call; memory and prompt count as always active);
  - add a per-domain noise band from the pool's own pairwise discordance;
  - label recurrent flips across arms (counts only, no dev ids to R);
  - add a per-arm behaviour summary (reads, writes and sends per domain).

**P4 detail.**
- Passing sample: 3 per claim domain plus 2 from marketing (14 tasks), redrawn each round with a seed derived from (`order_seed`, round).
- Eligibility: ≥1 fix AND ≤1 sample regression. Then rank by net flips.
- Holdout slice: 2 failing train tasks per claim domain that R never sees. Record-only for now.

**P5 detail.**
- `min_gain` 0.05 applies over the 100 claim-domain dev tasks, on both readings (5 tasks).
- Spent domains are a no-harm guard: ≥ −3 tasks on the mean of both readings.
- The regression cap is unchanged.

**P5 simulation results** (f = 0.36–0.42; acceptance rates):

| Candidate's true effect | Current rule | Claim-population rule |
|---|---|---|
| Marketing-only, +6 tasks | 0.27–0.28 | 0.006–0.04 |
| Claim domains, +6 of 100 | 0.27–0.29 | 0.44–0.50 |
| Claim domains, +8 of 100 | 0.48–0.53 | 0.70–0.75 |

The current gate is equally likely to install a change that cannot move the claim as one that can. On the past receipts, the claim-domain nets (−2, −1, −2) clear nothing under P5, so it is not retro-fitted to any past candidate.

**Deferred:**
- Domain-scoped path C: needs ≥3 control readings and accepts net-0 candidates about 15% of the time. P5 captures the claim population without per-domain multiplicity.
- Rule-by-rule ablation: P1's one-cause rule mostly covers it; revisit if a multi-rule text is gated.
- Fixes-per-activation ranking: needs P3's activation data first.
- Reserved slot for unused carrier kinds: only if P1 and P2 still produce 0 skills or tools after 2 rounds.
- Candidate pool and merge: needs ≥2 readings per candidate.
- Offline hook-trigger replay: $0 and useful, but second-order.
- Cumulative root guard: only after the first install.

## (3) Recommended next experiment

**Phase 0 (offline, $0).**
- Implement P1–P5 with tests, plus the P2 20-draft replay.
- Write the pre-registration below into ROADMAP/JOURNAL before any paid step.

**Phase 1 ($117).**
- Take 2 W0 dev readings, unconditionally.
- Run the A/A replay (6 directed pairs) and recalibrate per-domain noise.
- The gate for the following rounds is P5 if 0 of 6 pairs are accepted and the simulation is ≤ 0.05. Otherwise it is the current rule.

**Phase 2: extension rounds 2 and 3 of the 4 allowed, one at a time.**

| Outcome | Cost of the experiment |
|---|---|
| Both rounds screened out | ≈ $117 + 2 × $46 ≈ $210 |
| Two gates, no confirmation | ≈ $345 |
| Plus one confirmation | ≈ $405 |
| Plus an install's train rerun | ≈ $471 |

About $605 is usable: $663 minus the 8-lane in-flight reservation of $58. The final share of $390 is separate.

**Pre-registered reading.**
- **Framework works:** a confirmed install under the A/A-validated gate. Then proceed to the stage-4 sealed final from the final share (≈ $287).
- **Use extension round 4 (≈ $113) only if both hold:**
  - A gated candidate's first reading has claim-domain net ≥ +4/100 (≈ 1.1 sd), with ≥2 claim domains positive.
  - For conditional carriers, at least half its claim fixes are in activated attempts, or the holdout shows ≥2 fixes across ≥2 claim domains.
- **Stop:** otherwise. Under the existing rule, no install means no final. Report the negative result (R as a single coding session at k = 3 did not move the claim domains). The redesign then goes to v2.
- **Process checks (free, read from the records), which should hold before any gate money is read:**
  - 0 false leak refusals;
  - every candidate grounded by a trajectory read;
  - at least one conditional or skill candidate reaches the screen per round.

  If they fail, fix offline instead of paying for more rounds.

## (4) What NOT to do

- **Do not re-judge the three rejected receipts.** Not with the new control pool, not with P5, not with a lower `min_gain`, and do not re-run their gates hoping for a better reading.
- **Do not lower pooled `min_gain`, and do not use these rules:**

  | Rule | Null false accepts / power |
  |---|---|
  | `min_gain` 0.024 | 12–14% false accepts with 1 control reading |
  | Per-domain "any domain ≥ +3" with 1 control reading | 7.5–10% false accepts |
  | Sign tests | Power 0.25–0.32 at +5 tasks |
  | Mean-of-both-readings decision | Up to 4.3% false accepts |
  | "Reject on any sample regression" | Rejects 27–47% of good candidates |
- **Do not add readings or repeats conditionally** after a borderline first reading (optional stopping). Every extra reading is declared in advance and taken regardless of outcome.
- **Do not draw conclusions from single-reading per-domain or per-task flips** ("only works in marketing", "sales always regresses"). Correct the ROADMAP sentence.
- **Do not give R dev task ids, dev trajectories or per-task dev flip lists.** Do not encode anything about the recurrent-regression tasks or specific grader conventions; rulesmem's "N noun" rule already leaned that way. R gets counts, noise bands and behaviour summaries only.
- **Do not draw the passing sample from stable-pass tasks,** and do not add more note-selection tweaks or a separate LLM success digest.
- **Do not spend the remaining three extension rounds on the unchanged pipeline** (≈ $85 each). Its claim-domain nets so far are −2, −1 and −2.
- **Do not let marketing target or install anything.** It may only guard.
- **Do not touch any test split,** including marketing's spent test.
