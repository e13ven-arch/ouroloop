"""The "dir" store: a directory, <root>/<job id>/run per model, on this machine or, with a training host, on that host.
The ref is the job id. When root is the jobs' directory (the default of both), a run is published where it was
trained; else it is copied there once."""
from __future__ import annotations

import os
import shutil

from . import JOB_ID, check_keys

ROOT = "~/.reharness/jobs"


class DirStore:
    TYPE = "dir"
    ON_TRAINING_HOST = True
    ENV: tuple = ()
    REQUIREMENTS: tuple = ()

    def __init__(self, table: dict):
        check_keys(table, {"root"})
        root = table.get("root", ROOT)
        if not isinstance(root, str) or not root:
            raise ValueError(f"[store] root {root!r}: a directory")
        self.root = root
        self.table = {"type": self.TYPE, "root": root}

    def here(self) -> DirStore:
        return DirStore({"root": os.path.abspath(os.path.expanduser(self.root))})

    def in_place(self, jobs_root: str) -> bool:
        return os.path.normpath(os.path.expanduser(self.root)) == os.path.normpath(os.path.expanduser(jobs_root))

    @staticmethod
    def probe() -> dict:
        return {}

    @staticmethod
    def ready(record: dict | None) -> list[str]:
        return []

    def job(self, ref) -> str | None:
        return ref if isinstance(ref, str) and JOB_ID.fullmatch(ref) else None

    def _run(self, job_id: str) -> str:
        return os.path.join(os.path.expanduser(self.root), job_id, "run")

    def publish(self, run: str, job_dir: str, job_id: str) -> str:
        """The run is the job's, <job>/run (the plugin contract), and stays there when the job is in root."""
        if os.path.realpath(run) != os.path.realpath(os.path.join(job_dir, "run")):
            raise ValueError(f"the trainer left its run in {run}, not in {os.path.join(job_dir, 'run')}: in a "
                             f"\"dir\" store its run is <job>/run")
        dest = self._run(job_id)
        if os.path.realpath(run) != os.path.realpath(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copytree(run, dest)          # a ref names one run: an existing one is an error, never replaced
        return job_id

    def fetch(self, ref) -> str:
        if not self.job(ref):
            raise ValueError(f"not a model ref: {ref!r} (a job id)")
        run = self._run(ref)
        if not os.path.isdir(run):
            raise ValueError(f"no run {run}: no model {ref!r} in this store")
        return run


STORE = DirStore
