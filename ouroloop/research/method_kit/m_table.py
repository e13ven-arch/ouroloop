"""middlewares/m_table.py: the method table (docs/design/V0_5_METHOD_LIBRARY.md §2.5). Written by fixed code only —
a new method's cells take R's declared `on`, a refit candidate writes what the paired train rewards learned — and read
with ast.literal_eval by the host and the router, never imported. Keys of `cells` are "<method>|<kind>|<bucket>",
the bucket `*` or "<feature>=0|1" for the cut in `splits`; an action is `silent` or the method's declared `on`."""
TABLE = {"mode": "greedy", "cells": {}, "splits": {}}
