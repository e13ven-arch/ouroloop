"""The improver (docs/design/IMPROVER.md; DESIGN §6, v1.2): one round of improvement over train evidence — the working
line's train reading on the studied part, diagnosis into causes, the focus list, R's coding session for one candidate
package, the verification ladder, the held-out screen and the install on the working line, conditional credit, the
ledger, the estimates and the playbook. The release gate at the end of the activity is evaluate's.

This module imports nothing: K's improver section reads its defaults from `settings` (standard library only), and an
attempt or a W0 phase loads no other part of the improver (CONTRACTS §1).
"""
