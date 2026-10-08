"""What an attempt under the runner shell needs of the workspace: its root, and a session record per attempt
(docs/PLAN.md P1 step 2).

The runner shell asks no decision point (docs/modules/01_POLICY_RUNTIME.md §1), so an attempt needs none of the
decision runtime (runtime.Runtime: backends, built-in points, the champion pointer and its monitor). It writes what
runtime.Runtime.session wrote for it: one `session` row in the ledger and `<session>.release.json` beside the session's
contexts, with the same keys. Before any promotion that runtime's release was (no model, the hash of the five built-in
points' specs, the harness hash) and its champion None; this is that release, with the spec hash frozen as a constant.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .ledger import Ledger, SessionStore, write_json
from .release import Release, harness_hash

# release.spec_hash of the built-in points (points.BUILTIN) at their defaults, as runtime.Runtime recorded it for every
# session of a bench activity (the configs declare no [points.X] table). Recorded, never recomputed: nothing here
# imports the points.
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

    def session(self, session: str, meta: dict | None = None, harness: dict | None = None) -> Release:
        """Open a session: its release is fixed when it first opens and kept on disk, so a session opened again keeps
        it; every open appends a `session` row with what the session runs with (task, split, held-out, arm)."""
        path = self.sessions.path(session, ".release.json")
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            release, champion = Release(**saved["release"]), saved.get("champion")
        else:
            release, champion = Release(None, BUILTIN_SPEC, harness_hash(harness)), None
            write_json(path, {"release": release.to_json(), "champion": champion})
        self.ledger.append({**(meta or {}), "type": "session", "session": session, "release": release.to_json(),
                            "champion": champion})
        return release
