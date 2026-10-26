"""Shared fixtures: the miniature AutomationBench upstream. (The v1 gate and champion fixtures — accepted, accept,
gated, frozen, loose_risk, SMALL_GATE — went with the champion pointer in P1 step 5.)"""
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fake_upstream():
    """The path of the miniature AutomationBench in tests/fake_upstream, importable as `automationbench`.

    A real checkout installed in the same environment must not win: anything already imported under that name is
    dropped first, so the adapter's tests always run against the miniature.
    """
    import sys
    root = str((Path(__file__).parent / "fake_upstream").resolve())
    for name in [m for m in sys.modules if m == "automationbench" or m.startswith("automationbench.")]:
        del sys.modules[name]
    sys.path.insert(0, root)
    try:
        yield root
    finally:
        sys.path.remove(root)
        for name in [m for m in sys.modules if m == "automationbench" or m.startswith("automationbench.")]:
            del sys.modules[name]


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    """The meter waits RETRY_SECONDS before retrying a failed call; the tests' providers fail on purpose and at once."""
    from reharness.evaluate import budget
    monkeypatch.setattr(budget, "RETRY_SECONDS", (0.0,))
