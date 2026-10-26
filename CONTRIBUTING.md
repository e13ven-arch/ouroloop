# Contributing

ouroloop is a research prototype with a measured record. Three things keep that record honest, and a change must keep them:

1. **Money.** No paid run starts without a person's approval in chat, and every paid phase stops at the activity's budget root. Tests never call a model: the default suite (`pytest -q`) is offline, and `live` tests run only when asked for.
2. **Leakage.** The test split of a benchmark is read only under a plan sealed and committed before the first test attempt. Labels never reach the improver: it sees at most "accepted or sent back" per attempt and the rejected checks' kinds and objects. Anything that would hand it an expected value is a bug.
3. **Correctness of results.** Run records (`examples/**/workspace*/`, git-ignored) are never regenerated, deleted or edited; old workspaces are resumed at their tags. A result is reported with what it does and does not show.

## Working on the code

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,automationbench,seed]"
pytest -q                                                   # the default suite: offline, a few minutes
OUROLOOP_TEST_REQUIRE_DOCKER=1 OUROLOOP_TEST_REQUIRE_MEMORY=1 pytest -q -m "not live"   # the full run before a merge
```

- Design first: the design documents in `docs/` (Chinese) are the source of truth; a change to the improver, the evaluation protocol or the platform's constraints is written there before it is coded (`docs/PLAN.md` §1 lists the rules, §4 which document owns what).
- The framework stays benchmark-agnostic: benchmark specifics live in the adapter (`ouroloop/adapters/`) or are learned by a run from train.
- One pytest run at a time: concurrent runs make the isolated worker's tests fail spuriously.
- Mutation or review checks run only in an isolated copy of the checkout.
- Every change to the record (`docs/JOURNAL.md`, newest first) says what was done, what was measured and what changed in the design because of it.

## Reporting a result

A number is reported with its activity, its split, the feedback the improver had, the effort level, the sample size, the paired difference with its interval, and the cost. A reading on a held-out screen or a dev split is a process record, never a result.
