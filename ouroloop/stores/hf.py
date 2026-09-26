"""The "hf" store: one private Hugging Face repository per model, <prefix><job id>, committed once; the ref is
<repo>@<commit>. It is reached from anywhere with a token (HF_TOKEN, or `hf auth login`), so it outlives a cloud
instance and serves on any host."""
from __future__ import annotations

import os
import re

from . import JOB_ID, check_keys

REF = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40})")


class HfStore:
    TYPE = "hf"
    ON_TRAINING_HOST = False
    ENV = ("HF_TOKEN",)
    REQUIREMENTS = ("huggingface_hub",)

    def __init__(self, table: dict):
        check_keys(table, {"prefix"})
        prefix = table.get("prefix")
        if not isinstance(prefix, str) or "/" not in prefix:
            raise ValueError('[store] of type "hf" needs prefix, e.g. "your-org/ouroloop-"')
        self.prefix = prefix
        self.table = {"type": self.TYPE, "prefix": prefix}

    def here(self) -> HfStore:
        return self

    def in_place(self, jobs_root: str) -> bool:
        return False

    @staticmethod
    def probe() -> dict:
        """{"hub": huggingface_hub is installed, "token": a token is (HF_TOKEN, or `hf auth login`)}"""
        import importlib.util
        hub = importlib.util.find_spec("huggingface_hub") is not None
        token = bool(os.environ.get("HF_TOKEN"))
        if hub and not token:
            try:
                from huggingface_hub import get_token
                token = bool(get_token())
            except ImportError:
                pass
        token = token or os.path.exists(os.path.expanduser("~/.cache/huggingface/token"))
        return {"hub": hub, "token": token}

    @staticmethod
    def ready(record: dict | None) -> list[str]:
        why = []
        if not (record or {}).get("hub"):
            why.append('no huggingface_hub for a [store] of type "hf"')
        if not (record or {}).get("token"):
            why.append('no Hugging Face token for a [store] of type "hf": set HF_TOKEN there, or run `hf auth login` '
                       'there')
        return why

    def job(self, ref) -> str | None:
        m = REF.fullmatch(ref) if isinstance(ref, str) else None
        if m and m.group(1).startswith(self.prefix) and JOB_ID.fullmatch(m.group(1)[len(self.prefix):]):
            return m.group(1)[len(self.prefix):]
        return None

    def publish(self, run: str, job_dir: str, job_id: str) -> str:
        """One commit to a new private repository: the run at its root, the job's spec and data under job/."""
        from huggingface_hub import CommitOperationAdd, HfApi
        repo = f"{self.prefix}{job_id}"
        api = HfApi()
        api.create_repo(repo, private=True, exist_ok=False)
        ops = []
        for base, _, names in os.walk(run):
            for name in sorted(names):
                path = os.path.join(base, name)
                ops.append(CommitOperationAdd(path_in_repo=os.path.relpath(path, run).replace(os.sep, "/"),
                                              path_or_fileobj=path))
        for name in ("spec.json", "train.jsonl", "val.jsonl"):
            ops.append(CommitOperationAdd(path_in_repo=f"job/{name}", path_or_fileobj=os.path.join(job_dir, name)))
        info = api.create_commit(repo, operations=ops, commit_message=f"ouroloop {job_id}")
        return f"{repo}@{info.oid}"

    def fetch(self, ref) -> str:
        if not self.job(ref):
            raise ValueError(f"not a model ref: {ref!r} (<{self.prefix}job id>@<commit>)")
        from huggingface_hub import snapshot_download
        repo, _, commit = ref.partition("@")
        return snapshot_download(repo_id=repo, revision=commit)


STORE = HfStore
