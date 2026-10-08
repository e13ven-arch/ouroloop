"""What an attempt under the runner shell needs of the workspace: its root, and a session record per attempt
(docs/PLAN.md P1 step 2).

The runner shell asks no decision point (docs/modules/01_POLICY_RUNTIME.md §1), so an attempt needs none of a
decision runtime. It writes what the v1 runtime (removed in P1 step 5, tag archive-2026-10-08) wrote for it: one
`session` row in the ledger and `<session>.release.json` beside the session's contexts, with the same keys, so
Ledger.sessions() and the v2 data pipeline read old and new sessions alike. That runtime's release was the triple
(model, decision spec, harness): before any promotion no model, the hash of its five built-in points' specs, and the
harness hash, and its champion None; this is that record, with the spec hash frozen as a constant.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .ledger import Ledger, SessionStore, write_json
from .release import harness_hash

# The spec hash of the v1 runtime's five built-in points at their defaults, as it recorded it for every session of a
# bench activity (the configs declare no [points.X] table). Recorded, never recomputed: the points are gone.
BUILTIN_SPEC = "0b0ad2d0005b"


class Workspace:
    """The workspace root, the ledger the session rows go to, and the session store. `serving` builds the activity's
    serving.Serving when something first asks for it (a model policy to bind, a trainer); a release of rule policies
    never does, so a bench run of one constructs none."""

    def __init__(self, root: str | Path, *, serving: Callable[[], Any] | None = None):
        self.root = Path(root)
        self.ledger = Ledger(self.root / "ledger")
        self.sessions = SessionStore(self.root / "sessions")
        self._make_serving, self._serving = serving, None

    @property
    def serving(self):
        if self._serving is None and self._make_serving is not None:
            self._serving = self._make_serving()
        return self._serving

    def session(self, session: str, meta: dict | None = None, harness: dict | None = None) -> dict:
        """Open a session: its release record ({model, spec, harness}) is fixed when it first opens and kept on disk,
        so a session opened again keeps it; every open appends a `session` row with what the session runs with (task,
        split, held-out, arm). -> the release record."""
        path = self.sessions.path(session, ".release.json")
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            release, champion = dict(saved["release"]), saved.get("champion")
        else:
            release, champion = {"model": None, "spec": BUILTIN_SPEC, "harness": harness_hash(harness)}, None
            write_json(path, {"release": release, "champion": champion})
        self.ledger.append({**(meta or {}), "type": "session", "session": session, "release": release,
                            "champion": champion})
        return release
