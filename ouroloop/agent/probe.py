"""The probe (PLAN_v0.2 M6): an environment label for every stop decision, whatever the agent did next.

At each stop decision it copies the working directory as it is (the hidden tests are never in it during a run).
After the session it writes the hidden files into each copy, runs the task's checks there, and labels the
decisions made on that state. It is not a checker: nothing in the run, neither StopGuard nor the view nor the
agent, sees its result.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable


class Probe:
    def __init__(self, rt, workdir: str | Path, checks: Callable[[Path], list[bool]]):
        """checks(dir) writes the hidden files into dir, runs the task's checks there and returns which passed."""
        self.rt, self.workdir, self.checks = rt, Path(workdir), checks
        self.root: Path | None = None
        self.snaps: list[dict] = []     # {"changes", "dir", "decisions"}

    def snap(self, decision: str, changes: int) -> None:
        """At a stop decision. `changes` is the agent's change count: an unchanged state is not copied again."""
        if not self.snaps or self.snaps[-1]["changes"] != changes:
            self.root = self.root or Path(tempfile.mkdtemp(prefix="ouroloop-probe-")).resolve()
            dest = self.root / str(len(self.snaps))
            shutil.copytree(self.workdir, dest, symlinks=True)
            self.snaps.append({"changes": changes, "dir": dest, "decisions": []})
        self.snaps[-1]["decisions"].append(decision)

    def label(self) -> None:
        """After the session: label every stop decision from its copy, then delete the copies."""
        try:
            for s in self.snaps:
                passed = self.checks(s["dir"])
                for d in s["decisions"]:
                    self.rt.outcome(d, "yes" if all(passed) else "no", source="env", note="probe",
                                    score=sum(passed) / len(passed))
        finally:
            if self.root is not None:
                shutil.rmtree(self.root, ignore_errors=True)
                self.root = None
