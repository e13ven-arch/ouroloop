"""Task suites: small scripted tasks for acceptance runs and for harness prompt candidates (PLAN §4.8).

One TOML file per task:

    prompt = "The median function in stats.py is wrong for some inputs. Fix it."
    checks = ["python3 -m unittest -q test_hidden.T.test_even"]  # each command exits 0 when met
    held_out = false        # held-out tasks are never shown to the research agent
    max_turns = 15
    verify = ""             # optional: a command the agent's stop point runs before finishing

    [files]                 # written into a fresh working directory before the run
    "stats.py" = '''...'''

    [hidden]                # written after the run, just before the checks, so the agent cannot change them
    "test_hidden.py" = '''...'''

    [solution]              # optional: reference files, only used to validate the task itself
    "stats.py" = '''...'''

A run scores the share of checks that pass.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .tools import Tools, clip


@dataclass
class Task:
    name: str
    prompt: str
    checks: list[str]
    files: dict[str, str] = field(default_factory=dict)
    hidden: dict[str, str] = field(default_factory=dict)
    solution: dict[str, str] = field(default_factory=dict)
    held_out: bool = False
    max_turns: int = 15
    verify: str | None = None
    check_timeout: int = 60

    def hash(self) -> str:
        """Changes when anything that affects a run changes; cached runs of an edited task are not reused."""
        data = {k: v for k, v in asdict(self).items() if k not in ("name", "solution", "held_out")}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]


@dataclass
class TaskRun:
    task: str
    score: float | None       # share of checks passed; None when the run broke for reasons outside the task
    passed: list[bool] = field(default_factory=list)
    turns: int = 0
    stop: str = ""
    session: str = ""
    text: str = ""            # the agent's final message
    check_output: str = ""    # output of the failed checks
    error: str = ""


def load_task(path: str | Path) -> Task:
    path = Path(path)
    with open(path, "rb") as f:
        data = tomllib.load(f)
    unknown = set(data) - {"prompt", "checks", "files", "hidden", "solution", "held_out", "max_turns", "verify",
                           "check_timeout"}
    if unknown or not data.get("prompt") or not data.get("checks"):
        raise ValueError(f"{path}: a task needs prompt and checks (unknown keys: {sorted(unknown)})")
    return Task(path.stem, data["prompt"], list(data["checks"]), dict(data.get("files", {})),
                dict(data.get("hidden", {})), dict(data.get("solution", {})), bool(data.get("held_out", False)),
                int(data.get("max_turns", 15)), data.get("verify") or None, int(data.get("check_timeout", 60)))


def load_suite(path: str | Path) -> list[Task]:
    tasks = [load_task(p) for p in sorted(Path(path).glob("*.toml"))]
    if not tasks:
        raise ValueError(f"no task files (*.toml) in {path}")
    return tasks


def write_files(workdir: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = (workdir / rel).resolve()
        if workdir not in p.parents:
            raise ValueError(f"{rel} is outside the task directory")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def check(task: Task, workdir: Path) -> tuple[list[bool], str]:
    """Write the hidden files over whatever is there, then run every check."""
    write_files(workdir, task.hidden)
    tools = Tools(workdir)
    passed, failed = [], []
    for command in task.checks:
        r = tools.run("bash", {"command": command, "timeout": task.check_timeout})
        passed.append(r.meta.get("exit_code") == 0)
        if not passed[-1]:
            failed.append(f"$ {command}\n{clip(r.output, 600)}")
    return passed, "\n".join(failed)[:2000]


def validate(task: Task) -> tuple[float, float | None]:
    """Scores of the untouched files and of the reference solution: a sound task is below 1, then exactly 1."""
    scores: list[float | None] = []
    for files in (task.files, {**task.files, **task.solution} if task.solution else None):
        if files is None:
            scores.append(None)
            continue
        workdir = Path(tempfile.mkdtemp(prefix=f"ouroloop-{task.name}-")).resolve()
        try:
            write_files(workdir, files)
            passed, _ = check(task, workdir)
            scores.append(sum(passed) / len(passed))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    return scores[0], scores[1]


def run_task(task: Task, make_agent: Callable, *, keep: bool = False) -> TaskRun:
    """Run a fresh agent (make_agent(workdir, task) -> Agent) on the task in a temporary directory."""
    workdir = Path(tempfile.mkdtemp(prefix=f"ouroloop-{task.name}-")).resolve()
    result: dict = {}

    def confirm(_task: str, _text: str) -> bool:
        # Called once the agent has finished: the checks are the outcome of its stop and route decisions.
        result["passed"], result["output"] = check(task, workdir)
        return all(result["passed"])

    try:
        write_files(workdir, task.files)
        try:
            agent = make_agent(workdir, task)
            agent.confirm_fn, agent.confirm_source = confirm, "env"
            res = agent.run(task.prompt)
        except Exception as e:  # noqa: BLE001 - an API failure is no evidence about the prompt
            return TaskRun(task.name, None, error=f"{type(e).__name__}: {e}"[:500])
        if "passed" not in result:
            confirm(task.prompt, res.text)
        passed = result["passed"]
        return TaskRun(task.name, sum(passed) / len(passed), passed, res.turns, res.stop, res.session,
                       res.text[:1500], result["output"])
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)
