"""Fixtures of the v1 decision tests, formerly ouroloop/demo.py and providers.MockProvider (P1 step 3 removed both from
the package): a scripted LLM, a synthetic `retry` ledger, and one research round over it through a frozen gate.

The ledger has `retry` decisions with the outcomes of rerunning the command; the round goes report -> the largest
error group -> the scripted LLM proposes two spec changes (and a recipe, when one may be proposed) -> offline replay
-> the frozen gate, once per round -> experience record. No network and no model.
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Callable

from ouroloop.backends import KeywordBackend
from ouroloop.evolve.gate import freeze_template, template_path
from ouroloop.evolve.pipeline import recipe_evaluator, research_context
from ouroloop.points import retry_point
from ouroloop.research import BasicResearcher, run_round
from ouroloop.runtime import Runtime
from ouroloop.trainers.signature import SignatureTrainer


class MockProvider:
    """Answers with a function of the prompt."""

    name = "mock"

    def __init__(self, fn: Callable[[str, str], str], model: str = "scripted", source: str = "frontier_llm"):
        self.fn = fn
        self.model = model
        self.source = source

    def complete(self, system: str, prompt: str) -> str:
        return self.fn(system, prompt)


TRANSIENT = [
    ("pip install -r requirements.txt", "ConnectionResetError: [Errno 54] Connection reset by peer"),
    ("apt-get install -y jq", "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 4121"),
    ("gh api repos/acme/app/pulls", "HTTP 429: rate limit exceeded, retry after 60 seconds"),
    ("docker pull python:3.12", "Error response from daemon: 503 Service Unavailable"),
    ("curl -sSf https://api.example.com/health", "curl: (28) Operation timed out after 30000 milliseconds"),
]
PERMANENT = [
    ("python -m app", "ModuleNotFoundError: No module named 'yaml'"),
    ("pytest -x", "bash: pytest: command not found"),
    ("python -m app", "SyntaxError: invalid syntax"),
    ("cat config.yml", "cat: config.yml: No such file or directory"),
    ("pytest tests/test_math.py", "AssertionError: assert 3 == 4"),
    ("python train.py --lr", "train.py: error: argument --lr: expected one argument"),
]
FRAMES = [
    '  File "/srv/app/main.py", line 12, in <module>',
    '  File "/usr/lib/python3.12/site-packages/urllib3/connection.py", line 174, in _new_conn',
    '  File "/usr/lib/python3.12/importlib/__init__.py", line 90, in import_module',
    '  File "/srv/app/net/retry_session.py", line 40, in request',
]


def make_context(rng: random.Random) -> tuple[dict, str]:
    transient = rng.random() < 0.4
    cmd, err = rng.choice(TRANSIENT if transient else PERMANENT)
    frames = []
    if re.match(r"[A-Z]\w+Error:", err) and cmd.startswith(("python", "pytest", "pip")):
        frames = ["Traceback (most recent call last):", *rng.sample(FRAMES, rng.randint(1, 3))]
    return {"command": cmd, "stderr": "\n".join(frames + [err]), "exit_code": 1}, ("yes" if transient else "no")


def seed_ledger(rt: Runtime, sessions: int = 120, per_session: int = 3, seed: int = 0, start: int = 0) -> None:
    """`sessions` sessions, numbered from `start`: new sessions for a later round start past the earlier ones."""
    rng = random.Random(seed)
    for s in range(start, start + sessions):
        for _ in range(per_session):
            ctx, label = make_context(rng)
            d = rt.ask("retry", ctx, session=f"s{s:03d}")
            rt.outcome(d.id, label, source="env", note="result of rerunning the command")


def _propose(system: str, prompt: str) -> str:
    m = re.search(r"Error signature: (.+?)\.\n", prompt)
    sig = m.group(1) if m else "the observed error"
    return json.dumps({"candidates": [
        {"kind": "spec", "cost": "low",
         "change": {"criteria": {"false": "a permanent failure that needs a change: missing module "
                                          "(ModuleNotFoundError), command not found, syntax error (SyntaxError), "
                                          "missing file (No such file or directory), failing assertion "
                                          "(AssertionError), invalid argument"}},
         "hypothesis": f"Naming common permanent errors such as {sig} in the 'false' description separates them "
                       f"from transient ones."},
        {"kind": "spec", "cost": "low", "change": {"view_params": {"tail_lines": 1}},
         "hypothesis": "Only the last stderr line carries the error; stack frames add misleading words."},
    ] + ([{"kind": "recipe", "cost": "medium", "change": {"min_weight": 1.0},
           "hypothesis": "A model trained on this point's own outcomes (environment labels only) predicts reruns "
                         "better than keyword overlap."}] if "- recipe:" in prompt else [])})


def round_llm() -> MockProvider:
    return MockProvider(_propose, model="scripted-round")


# A gate a single synthetic point can pass: the checks scaled to the synthetic ledger, and half the training sessions
# kept for calibration, so that a model can earn a threshold at the 5% risk (59 sessions it acts in, none harmed).
ROUND_GATE = {"checks": {"min_train": 100, "max_mde": 0.2}, "min_cal_sessions": 10, "cal_frac": {"*": 0.5}}


def research_round(root: str | Path, seed: int = 0, sessions: int = 400, train: bool = True):
    """One round, through one gate record: the first candidate to reach the gate's verdict ends it. With train=True
    the LLM may also propose a training recipe, trained in-process by a small signature model."""
    root = Path(root)
    rt = Runtime(root, {"mock": KeywordBackend()}, default_backend="mock", seed=seed)
    rt.register(retry_point())
    seed_ledger(rt, sessions, seed=seed)
    if not template_path(root).exists():
        freeze_template(root, ROUND_GATE)
    kw = {}
    if train:
        trainer = SignatureTrainer()
        kw = {"evaluators": {"recipe": recipe_evaluator(trainer)}, "context": research_context(trainer, None)}
    return root, run_round(rt, BasicResearcher(budget=3), round_llm(), KeywordBackend(), seed=seed, **kw)
