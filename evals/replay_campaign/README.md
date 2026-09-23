# Campaign decision set

38 typed decisions taken from a three-day model-improvement campaign, in the same schema as
`evals/replay_tde`. Each item is one judgment that was actually made, restated as a question a decision model
can answer.

74 items were extracted; 36 are withheld and 38 are published here. The withheld ones fall in three classes,
and the reason is disclosure rather than difficulty: judgments that describe a third party's process (a public
benchmark's submission handling), judgments that turn on a vendor's terms or on credentials, and judgments
about this project's own external claims and the editorial decisions behind its write-ups. They are not
cherry-picked for score: on the withheld 36 the same backend scores 80.6%, against 84.2% here.

    ouroloop replay evals/replay_campaign/decisions.jsonl --backend jev

## How the items were built

- **Prospective states.** A state contains only what was knowable *before* the decision. It never contains the
  outcome, and never phrases the situation so that the answer follows. Every item records `state_basis`.
- **Live candidates.** The options are the ones actually available at the time, described honestly. No straw
  alternatives.
- **Shuffled at presentation.** In the raw record the reference was the last-listed option in only 3 of 53
  choices, because the option written last is usually "abandon this". The harness shuffles candidate order at
  presentation time, seeded per item, so that order carries no signal. The file keeps its written order.
- **Reserved classes kept in.** 30 of 74 items are judgments that should stay with a person — goals,
  permissions, external-facing statements, spending. They are marked `reserved: true` rather than dropped. A
  model should not be rewarded for agreeing with these.
- **Redacted.** No paths, hosts, usernames, key names or personal file names.

## What a score here means, and does not

`ref_kind` says where the reference came from: `outcome` (24 items) is what actually happened, so those items
measure whether a judgment was right. `human` (15) and `llm` (35) record what was decided, so those items
measure agreement with the project's own choices — not correctness.

Four items whose recorded decision looks wrong in hindsight are excluded, because their references would mark
a correct model as disagreeing. Excluding them also removes the only items that could test whether a model
spots a bad call.

One consequence of withholding is worth stating: 30 of the 74 items are judgments that should stay with a
person, and most of them are in the withheld set, because that is exactly the material that describes third
parties and external claims. Only 3 remain here. The observation that the model is weaker on reserved
judgments (76.7% against 86.4% across all 74) therefore cannot be checked against this file.

The states were reconstructed by a project that already knew how things turned out, so they are tidier and
more pointed than what was in view at the time. Treat any agreement rate here as an upper bound, keep it
separate from what a harness records live, and report it beside the majority-label and random baselines — a
figure without them is not readable.
