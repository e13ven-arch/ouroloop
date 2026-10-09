"""The improver (docs/design/IMPROVER.md; DESIGN §6): one round of improvement over train evidence — the current W's
train reading, diagnosis into causes, the layer each cause is assigned, R's coding session, the verification ladder,
the train screen, the dev gate and the install, conditional credit, the ledger, the estimates and the playbook.

This module imports nothing: K's improver section reads its defaults from `settings` (standard library only), and an
attempt or a W0 phase loads no other part of the improver (CONTRACTS §1).
"""
