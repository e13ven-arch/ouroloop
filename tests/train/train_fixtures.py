"""Fixtures of the training stack's tests (formerly in tests/rsi.py, P1 step 4): a decision backend whose quality is
defined here, a trainer that returns it as its artifact, and one whose remote job cannot be read."""
from __future__ import annotations

import re
from pathlib import Path


class PhraseDecider:
    """A decision backend that reads the view: the task is complete when the phrase the request asked for appears
    in a tool result, and the record has been updated if the request asked for that.

    A fixture, not a training result. Its quality is defined here, so a test that shows the model changing what
    the agent does shows the wiring; whether a real small model can learn this is what a real experiment answers.
    """
    name = "phrase"

    def __init__(self, model: str = "trained-1", confidence: float = 0.9):
        self.model, self.confidence = model, confidence
        self.views: list[str] = []

    def decide(self, requests):
        from ouroloop.train.types import Answer
        out = []
        for r in requests:
            self.views.append(r.state)
            head, _, tail = r.state.partition("tool calls and what they returned")
            phrase = re.search(r"phrase ([a-z ]+?)(?:,|\.|$)", head)
            needs_update = "opportunity stage" in head and "change nothing" not in head
            done = (phrase is None or phrase.group(1).strip() in tail) and (
                not needs_update or "PATCH" in tail)
            p = self.confidence if done else 1 - self.confidence
            out.append(Answer({"yes": p, "no": 1 - p}, self.model))
        return out


class PhraseTrainer:
    """Trains nothing and says so: it reads the dataset the framework built (so a missing file is still an error)
    and returns a PhraseDecider as the artifact."""
    supports_weights = True
    name = "phrase"

    def __init__(self, model: str = "trained-1", fail: bool = False, confidence: float = 0.9):
        self.model, self.fail, self.confidence = model, fail, confidence
        self.jobs: list = []
        self.attached: list = []

    def train(self, job):
        from ouroloop.train.train import TrainError, TrainResult
        if self.fail:
            raise TrainError("CUDA out of memory")
        rows = sum(1 for _ in open(Path(job.data_dir) / "train.jsonl", encoding="utf-8"))
        self.jobs.append({"version": job.version, "rows": rows, "recipe": dict(job.recipe)})
        return TrainResult(self.model, {"type": "train_fixtures:PhraseDecider", "model": self.model,
                                       "confidence": self.confidence},
                           {"id": f"job-{job.version}", "host": "local", "result": {"rows": rows}})


class UnknownJobTrainer(PhraseTrainer):
    """A trainer whose remote job state cannot be read: `attach` returns nothing, and nothing is resubmitted."""

    def attach(self, job_id):
        self.attached.append(job_id)
        return None
