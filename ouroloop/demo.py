"""A self-contained run of one research round (PLAN §5.6), with no network and no model by default.

It seeds a workspace with synthetic `retry` decisions and their outcomes, then: report -> the judge picks an
error group -> a scripted LLM proposes two spec changes -> the judge rates them -> offline replay ->
promotion gate -> experience record. With judge="jev" the two research judgments go to the real Jev API.
"""
from __future__ import annotations

import json
import random
import re
import tempfile
from pathlib import Path

from .backends import JevBackend, KeywordBackend
from .evolve.pipeline import recipe_evaluator
from .evolve.train import DEFAULT_RECIPE, RECIPE_KEYS, SignatureTrainer
from .points import retry_point  # noqa: F401 - also exposed as ouroloop.demo:retry_point
from .providers import MockProvider
from .research import BasicResearcher, run_round
from .runtime import Runtime

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


def seed_ledger(rt: Runtime, sessions: int = 120, per_session: int = 3, seed: int = 0) -> None:
    rng = random.Random(seed)
    for s in range(sessions):
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


def demo_llm() -> MockProvider:
    return MockProvider(_propose, model="scripted-demo")


def run(root: str | Path | None = None, judge: str = "mock", seed: int = 0, sessions: int = 120, train: bool = True):
    """One round. With train=True the LLM may also propose a training recipe, trained in-process by a small
    signature model (the stand-in for TDE on a GPU host), so the demo shows spec and model evolution."""
    root = Path(root) if root else Path(tempfile.mkdtemp(prefix="ouroloop-demo-"))
    rt = Runtime(root, {"mock": KeywordBackend()}, default_backend="mock", seed=seed)
    rt.register(retry_point())
    seed_ledger(rt, sessions, seed=seed)
    judge_backend = JevBackend() if judge == "jev" else KeywordBackend()
    kw = {}
    if train:
        kw = {"evaluators": {"recipe": recipe_evaluator(SignatureTrainer(),
                                                        check_kwargs={"min_train": 100, "max_mde": 0.2})},
              "context": {"recipe": dict(DEFAULT_RECIPE), "recipe_keys": sorted(RECIPE_KEYS)}}
    return root, run_round(rt, BasicResearcher(budget=3, seed=seed), demo_llm(), judge_backend, seed=seed, **kw)
