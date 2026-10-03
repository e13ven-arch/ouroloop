The rehearsal's task world: a miniature AutomationBench in the shape `tests/fake_upstream` has, with one domain,
`marketing`, built so the pre-paid rehearsal can exercise the production path (docs/REHEARSAL.md §2).

Every task moves a HubSpot contact to a stage; most end with one closing step of four kinds — a Slack post, a tag,
a follow-up task, an owner. Do not edit it once a rehearsal workspace has frozen its splits: any change to a task
is upstream drift, and the version in pyproject.toml is part of every run's identity.
