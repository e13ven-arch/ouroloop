# The ceiling experiment (v0.3 step 4)

> Ran on 2026-10-05, during v0.3. On 2026-10-08 the runner, its config, both packages and the offline tests were
> removed from HEAD; they are unchanged at tag `v0.3.0` and can also be read at `v0.4.0` and `archive-2026-10-08`.
> This README and `tasks.json` stay as the record of the rule and the task set; the result is in docs/RESULTS.md
> (v0.3).

One question, answered once before v0.3's multi-domain activity (docs/ROADMAP.md §3, v0.3 step 4): can mechanisms at
the level of the agent loop reach the failures v0.2's W\* still has? If a hand-written agent package fixes most of
them, the loop layer can reach them and v0.3's weight belongs on search (R writing such packages). If it cannot, what
is left is the model's capability and the weight moves to preparing the second version's training.

The result is a decision record only. It enters no claim (no C1–C4), the package is not a candidate and is never
installed, and no test task is run.

## What is compared

Two arms, the same frozen tasks, the same number of attempts, one workspace (`workspace-claude-ceiling`), the v3
runner shell (`automationbench_harness_v3`: gateway, broker, the five hard limits), the same pinned actor (Claude Opus
5.5 through `claude -p`) at the same effort, **high** — K's `max_effort`:

| Arm | Package | Loop |
|---|---|---|
| `wstar` | `wstar_package/`: v0.2's installed W\* package, byte for byte (its v1 manifest, `memory.md` — segment-0's pre-write protocol — and the `finish_audit` on_finish hook — segment-1's end-of-task audit). The runner checks its files against W\*'s recorded bundle refs. | the default agent (K's), as every v1 package runs under v0.3 |
| `ceiling` | `package/`: the same `memory.md`, hook and hook test, plus `agent/ceiling_agent.py` and its tests, under a v2 manifest declaring roles `act` and `aux` | the ceiling agent: the default agent's loop plus (a)–(d) below |

W\*'s two prompt policies are W0's (an empty `worker.system_prompt`, the default finish check), so both arms' system
prompt is the same text: the task's system message, the finish check, and the memory. v0.2's W\* ran at effort
medium; here both arms run at high, which is why the W\* arm is read again in the same run instead of reusing v0.2's
readings.

## The four mechanisms, and how each stays within K

| | What it does | Where | Within K |
|---|---|---|---|
| (a) full-text reading | `read_full`: up to 20 GET requests at once, their whole joined text returned in pages of at most `max_output_chars`; a later page is answered from the attempt's cache. After any base result the agent adds a note when the host clipped it (`[... N characters omitted ...]`), when it says more pages exist (`nextPageToken`, `has_more`, `next_cursor`, ...; a nested signal counts only when it holds a value, so HubSpot's `{"next": {"after": null}}` is no next page), or when it lists items by id or snippet only. | `ceiling_agent.py`: `_read_full`, `_reading_notes`, `READ_FULL_SPEC` | every GET goes through `ctx.tool(..., read_only=True)` — one broker event and one of the 200 tool calls each; the broker refuses a writing call; 20 tool calls are kept free for writes; a page is never longer than a base tool's output may be. The host's clip itself (K's `max_output_chars`, 20,000) is not raised and cannot be seen past. |
| (b) evidence table before the first write | the first `api_fetch` with a method other than GET/HEAD is not executed — a POST to a search or query route (`/search`, `:search`, `:searchStream`, `/query`, `:find` at the end of the path: Google Ads `googleAds:search`, HubSpot `contacts/search`, QuickBooks `query`, monday `items:find`) is a read and runs; `:findOrCreate…` stays a write (the turn's earlier calls are, its later ones are not — the default agent's before_tool convention, marked); one `aux` call lists every requirement with its evidence, source and status (VERIFIED/ASSUMED/MISSING), the reads still needed and the plan of writes; the table goes into the history and the model re-issues the writes against it | `run` (the blocked-write branch), `EVIDENCE_PROMPT`, `TABLE_INTRO` | role `aux` (declared; counts toward `max_model_calls` 200, not toward `max_steps` 50); same messages and tools as the act call plus one request, so the provider's cached prefix is reused; only while act calls and room for an aux call remain |
| (c) check before finishing | after W\*'s hook has had its say, one `aux` call compares each requirement (the table and the request) with the writes and sends the attempt actually executed (searches and queries are reads and are not listed) — the agent's own record of its calls and their results — and ends with a JSON verdict; `continue` with gaps sends the model back with the gaps; at most 2 checks an attempt; an unreadable verdict finishes | `run` (the finish branch), `CHECK_PROMPT`, `_writes_text`, `_verdict` | role `aux`; only while act calls remain and the attempt has room (2 model calls, 20 s, 8,000 output tokens) |
| (d) effort at the cap | every model call, act and aux, asks for `high` | `_model` | `high` is K's `max_effort`; the gateway would cap a higher request and record it, never refuse |

An undeclared role is not caught by the agent: the draft smoke fails it (a mechanism cannot switch itself off and
leave the arm reading as W\*). The package is standard library only, 6 files, about 45 KB (K: 24 files, 262,144
bytes); it passes the draft check an R candidate gets — shape and K's kinds, the train-literal check, the isolated
probe, the agent smoke, its own 9 tests — in the Docker worker, offline (2026-10-05, again on the image rebuilt
from the Dockerfile, `c7005dd5c421`), and the runner runs that check
again before anything is paid.

**What (a) can reach here.** Replaying, offline, every tool call of W\*'s 25 recorded attempts on these 13 tasks (576
calls) gives a largest output of 7,689 characters: the host's 20,000-character clip never fired on them, so the
actor already read every result in full. In this world (a) acts through `read_full` and the notes on paged or
id-only lists, not through clipped results.

## The task set

`tasks.json`, frozen on 2026-10-05 from v0.2's records (`workspace-claude-full`, read with `Results`, `Releases`,
`Objects`) before any run; the runner reads it and never chooses tasks. Its `source` names W\*, the runs, the attempt
ids and the rule:

- **W\*** = `release:sha256:6cd37b8e49a4…`: `rsi/active.json` and `rsi/cycle.json` (segment-1's release; the cycle
  is done), installed by `install-cb19d2b77cb2` from the accepted receipt of segment-1 round 1.
- **dev, 6 tasks**: failed on at least 2 of W\*'s 3 dev readings (84%, 76%, 76% — the candidate arm that installed
  it and the control arms of segment-1 rounds 2 and 3), which here is exactly the set the latest reading failed.
  Four failed every reading (the "4" the earlier docs name; marked `persistent`): `ad_performance_review`,
  `event_registration_sync`, `industry_event_tracking`, `seo_competitor_gap`. Two failed the two later readings:
  `campaign_calendar`, `featured_snippet`.
- **train, 7 tasks**: failed in W\*'s only train measurement (run `130aa1d0657a8f35`; after an install the engine
  reruns only the previous version's failed train tasks, so W\* was run on 11): `budget_reallocation`,
  `voice_of_customer`, `social_mention_response`, `social_comment_response_queue`, `linkedin_company_update`,
  `linkedin_speaker_outreach`, `google_ads_pause_low_ctr`. The other 41 train tasks last passed under an earlier
  version and were never run under W\*.
- **failure kinds**, from each failed scored assertion's type name: `missing_write` (a record, message or post that
  should exist and does not), `forbidden_write` (`_not_`: something written that must not be), `content`
  (`contains`: a message to the named recipient with the required text), `count`. Types and counts only — no
  assertion params (the answer key) are in the file. The package was written from the mechanisms alone: no dev
  trajectory's content and no assertion param was read for it (the replay above measured output lengths only).

## How to run

On a checkout of tag `v0.3.0` (the runner and its config are not at HEAD):

```text
claude auth login                       # a Claude subscription; extra usage turned off in claude.ai's settings
.venv/bin/python examples/automationbench/ceiling_run.py --config examples/automationbench/claude-ceiling.toml --checks-only
.venv/bin/python examples/automationbench/ceiling_run.py --config examples/automationbench/claude-ceiling.toml
```

`--checks-only` freezes the preset's plans and the budget object, bootstraps W0/R0/K in the workspace on its first
run (the Docker image's id is frozen into K), runs the paid phases' startup checks (`bench.startup_checks`: the
upstream contract, the five limits, the pricing bound, `claude auth status`, the worker image's runner), checks the
task file against the split (same split hash as v0.2, dev tasks in dev, train in train, none in test), puts both
packages through the draft check and builds both candidate W — and calls no model, opens no budget root and runs no
attempt. The real run does the same, opens the root and runs, repeat by repeat: repeat 0 runs `wstar` then
`ceiling`, repeat 1 the other way round, each arm on dev then train, 8 attempts in flight at most, up to three
infrastructure refills per attempt (the plan's retry policy). A rerun of the same command resumes: recorded attempts
are not paid again. A subscription limit pauses it the same way.

Output: `workspace-claude-ceiling/ceiling/result.json` and `result.md`, also printed; the attempts, transcripts
(gateway calls with role and effort, broker events, the agent's marks) and grading are in the workspace as any arm's.

## What it costs

52 attempts (13 tasks × 2 attempts × 2 arms). v0.2's W\* cost $0.50 an attempt on these 13 tasks at effort medium
(25 attempts, 10 steps and 23 tool calls on average); the estimate here is $0.65 for the W\* arm at effort high and
$0.90 for the ceiling arm (its two or three aux calls, the table and the check prompts, more reads): about $40
nominal, $34–47 as a range. Nominal means API-equivalent dollars at list price, paid from the subscription.

The root is $150 nominal with a hard stop (`[freeze.budget]`), sized for the reservations rather than the spend:

- every call reserves its worst case before it is made, `bound` = 1M input × $4/M + 163,840 output × $20/M =
  $7.2768, and an `unknown` settlement (a killed `claude` child, an answer that never came) keeps its bound, so the
  calls that can be in flight at once are **slots = floor((root − spent − unknown × bound) / bound)**;
- a call waiting for a slot waits inside its attempt's 900 s. With a $60 root the last lanes (W\*'s repeat 1 always
  runs last) would have 2–3 slots for 8 attempts: attempts cut by the seconds limit would count as W\* failures — a
  bias toward "search" — and one Ctrl-C could leave the run `incomplete`;
- at $150 the run keeps all 8 slots to the end with room for several unknowns: it needs root − bound × (1 + 8) =
  $84.5 above the ~$40 it spends. The runner's pre-run note compares the estimate with exactly that number.

The money guard is the subscription with extra usage turned off; the expected spend stays about $40. A root's limits
are fixed once it opens. (2026-10-05: the first draft had $30, then $60; both are too small for this reason.)

## The decision rule (written before any run)

Over the frozen set F (13 tasks), both arms, 2 attempts each. Each (task, arm, repeat) is read from its final row
(its valid attempt, else its last refill):

- a valid attempt counts as it scored;
- an attempt invalid because **the arm's own package crashed** — its agent program raised at run time (recorded by
  the runner shell as `harness: AgentRunError`, failure class infrastructure, and refilled like any) or the candidate
  could not be assembled (`candidate_defect`) — counts as a **failed attempt of that arm**: it is the package's
  behaviour, and leaving it unread would hide it;
- an attempt invalid for the **environment** (a provider or worker failure, the budget) has no reading;
- a task is **read** when both arms have a reading for both repeats;
- **R**, still failing: read tasks the W\* arm fails on both attempts;
- **X**, fixed: tasks of R the ceiling arm passes on at least one attempt;
- **B**, broken: read tasks the W\* arm passes at least once and the ceiling arm on neither;
- **E**: read tasks the W\* arm passes at least once (in this run, at effort high).

Verdict:

- `search` (v0.3's weight on search): every task read, |R| ≥ 4, |X| > |R|/2 (more than half; for |R| = 13 that is 7)
  and |X| > |B|;
- `training` (the weight moves to the second version's training preparation): every task read, |R| ≥ 4, not search;
- `inconclusive`: every task read and |R| < 4 — W\* itself passes most of the set in this run;
- `incomplete`: some task not read (the budget's stop, an environment failure that outlasted three refills); the
  numbers are recorded, no verdict.

Why each part:

- *at least one of two for the ceiling*: it is a ceiling — the question is whether the loop layer can reach a
  failure at all, and one pass shows it can;
- *none of two for W\* in the same run*: the set was chosen because W\* failed it, which selects for bad luck; reading
  W\* again under the same shell, effort and time removes the tasks it fails only sometimes, and keeps everything but
  the loop equal;
- *more than half*: the docs' "修掉大半" / "过半";
- *|R| ≥ 4*: with fewer, "more than half" is one or two tasks, which two extra attempts of noise produce;
- *|X| > |B|*: a package that loses as many tasks of the set as it gains has fixes no different from attempt noise;
- *a package's own crash is a failure, an environment failure is no reading*: what the arm's code does is what is
  being compared, and an attempt it crashed is one it did not get right; a provider or worker failure says nothing
  about either arm, so it is refilled (up to three times) and otherwise left out.

## What each number means

- `R`, `X`, `B`, `E`, `needed` — as above; `needed` is |R|/2 rounded down, plus one.
- `by_split` — the same counts for the dev and the train tasks; `persistent` — for the 4 dev tasks W\* failed on every
  v0.2 reading.
- `by_failure_kind` — for each failure kind in `tasks.json`, how many of its tasks are still failing, fixed, broken,
  or passed by W\* here (a task with two kinds counts under both).
- per task and arm: success, partial credit and cost of each attempt, and the failed assertion types.
- per arm: valid and invalid attempts, pass rate, partial credit, `cost_per_attempt` (the gate's name for it),
  total cost, terminations, and the failed assertion types over all attempts.
- per arm `limits` — the attempts a hard limit ended, by dimension (`seconds`, `act_calls`, ...), so a cut in one
  arm is visible; `package_crashes` — the attempts counted as failures because the arm's package crashed;
  `unread_attempts` — the final rows lost to the environment.
- `stopped` — set when the budget root's hard stop ended the run; `budget` — the root's state at the end.

## Files

All but `tasks.json` and this README were removed from HEAD on 2026-10-08; read them at tag `v0.3.0`.

| Path | What |
|---|---|
| `tasks.json` | the frozen task set and its provenance |
| `package/` | the ceiling package (`manifest.json`, `memory.md`, `middlewares/finish_audit.py`, `agent/ceiling_agent.py`, `tests/`) |
| `wstar_package/` | v0.2's W\* package, byte for byte |
| `../ceiling_run.py` | the runner |
| `../claude-ceiling.toml` | the config |
| `tests/test_ceiling.py` (repository root) | the offline tests: the draft checks, the runner with a scripted model, the rule |
