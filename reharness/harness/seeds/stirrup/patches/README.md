The patch stack of the stirrup seed (docs/design/HARNESS.md §7, docs/DESIGN.md §3.3): `NNN-<slug>.patch`, unified
diffs against the package root (`a/vendor/stirrup/...`), replayed in name order when the seed is built. Each patch
starts with its reason and the evidence ref in the diff header. Empty today: the seven fixes over the prototype live
in the adapter (agent/stirrup_agent.py).
