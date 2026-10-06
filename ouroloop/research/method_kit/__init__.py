"""The method kit (docs/design/V0_5_PLAN.md §1.1; V0_5_METHOD_LIBRARY.md §2.5–§2.7): files the fixed code copies into
a package or a check session, read as bytes and never imported by the host for a package's sake.

- `mlib.py` is the template of `tools/_mlib.py` (fetch, parse, decode, the attempt's journal); R may extend its copy.
- `m_router.py` is `middlewares/m_router.py`, byte for byte: the router that runs the methods' triggers and acts by the
  table. R cannot change it (kit completion replaces an edited copy).
- `m_table.py` is the stub of `middlewares/m_table.py`, the cell table only fixed code writes.
- `reach.py` is loaded as `middlewares/_m_reach.py` into a draft check's worker session only, never into a package:
  it replays recorded train events through a method's trigger and runs the method once (the reach check).
- `template_test.py` is the test template R is shown.
- `reference/` holds the reference methods (§1.2): test fixtures and the material of the pre-registered seeded-library
  ablation; they are not R's inputs and not W0's.

Every file here is standard library only, so the same bytes run in the worker.
"""
