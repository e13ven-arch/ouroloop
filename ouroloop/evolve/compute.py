"""Where training jobs run (PLAN_v0.2 M9): this machine, or one host over ssh, the same way. A job is a new directory
<jobs_root>/<job id>/ holding exactly the files it was given, and one detached process group:

    nohup sh -c '<python> worker.py spec.json "$0" > log 2>&1; echo $? > exit' <job id>

whose leader's pid is in `pid`; the leader and the worker both carry the job id as their last argument, so a pid that
now belongs to another process, or to another job, is never taken for this one. The shell line holds only paths a
person set (python, jobs_root) and the job id; everything else reaches the job through its files. `poll` reads the
exit code, result.json and the end of the log; `cancel` stops the process group; `unfinished` lists the jobs of a
workspace that never exited.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .worker import JOB_ID

MARK = "worker.py spec.json"        # what a job's processes run, followed by the job id as their last argument
TAIL = 40                           # log lines a finished job reports
SEP = "----ouroloop-log----"
SSH_TIMEOUT_S = 120                 # the longest one ssh command may take (a status read, a launch, a cancel)
# Always given to ssh (and rsync's ssh), after the person's own options, which win: a dead peer is noticed in about
# a minute, not after TCP's two hours.
SSH_ALIVE = ["-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]


@dataclass
class Status:
    state: str                      # "running" | "exited" | "lost"
    code: int | None = None
    result: dict | None = None      # result.json, None if missing or malformed
    log: str = ""                   # the end of the log


class Remote:
    """ssh and rsync to one host. `runner(args, input=None)` returns a CompletedProcess (injectable for tests)."""

    def __init__(self, host: str, ssh_opts: str = "-o BatchMode=yes", runner=None):
        self.host = host
        self.ssh_opts = [os.path.expanduser(t) for t in shlex.split(ssh_opts)] + SSH_ALIVE
        self.runner = runner or _run

    def ssh(self, command: str, input: str | None = None) -> str:
        return self.runner(["ssh", *self.ssh_opts, self.host, command], input=input).stdout

    def push(self, local: str, remote: str) -> None:
        self.runner(["rsync", "-az", "--timeout=120", "-e", shlex.join(["ssh", *self.ssh_opts]), local,
                     f"{self.host}:{remote}"])

    def pull(self, remote: str, local: str) -> None:
        self.runner(["rsync", "-az", "--timeout=120", "-e", shlex.join(["ssh", *self.ssh_opts]),
                     f"{self.host}:{remote}", local])


def _run(args: list, input: str | None = None) -> subprocess.CompletedProcess:
    """ssh within SSH_TIMEOUT_S (a TimeoutExpired is a SubprocessError: a status read then counts as running); rsync
    stops by itself once no data moves for its --timeout."""
    return subprocess.run(args, input=input, capture_output=True, text=True, check=True,
                          timeout=SSH_TIMEOUT_S if args[0] == "ssh" else None)


def _q(path: str) -> str:
    """A path for the remote shell: quoted, with a leading ~ still the remote home."""
    if path == "~" or path.startswith("~/"):
        return '"$HOME"' + (shlex.quote(path[1:]) if len(path) > 1 else "")
    return shlex.quote(path)


def _job(args: str, job_id: str) -> bool:
    """Whether a process's command line is one of the job's: the leader or the worker."""
    return MARK in args and args.rstrip().endswith(f" {job_id}")


def _grep(job_id: str) -> str:
    """_job as a shell filter of `ps` lines (job ids need no quoting: letters, digits, _ and -)."""
    return f"grep -F '{MARK}' | grep -q ' {job_id}$'"


def _mine(pid: int, job_id: str) -> tuple[bool, bool]:
    """(the leader `pid` still runs the job, any process of its group still does) on this machine. A pid that is still
    some group's id is never given to a new process, so a group with a process of the job is the job's."""
    rows = subprocess.run(["ps", "-ww", "-A", "-o", "pid=,pgid=,args="], capture_output=True, text=True).stdout
    leader = group = False
    for row in rows.splitlines():
        p, g, args = (row.split(None, 2) + ["", "", ""])[:3]
        if _job(args, job_id):
            leader = leader or p == str(pid)
            group = group or g == str(pid)
    return leader, group


def _group(pgid: int, leader: subprocess.Popen | None) -> bool:
    """Whether any process of the group is left (a leader of ours that exited is reaped first)."""
    if leader is not None:
        leader.poll()
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse(exit_text: str, result_text: str | None, log: str) -> Status:
    try:
        code = int(exit_text.strip())
    except ValueError:
        code = -1
    try:
        result = json.loads(result_text) if result_text else None
    except ValueError:
        result = None
    return Status("exited", code, result if isinstance(result, dict) else None, log)


class Compute:
    def __init__(self, host: str | None = None, jobs_root: str = "~/.ouroloop/jobs",
                 ssh_opts: str = "-o BatchMode=yes", runner=None, sleep=time.sleep):
        """host: None for this machine, or "user@box" over ssh (ssh_opts, e.g. "-i ~/.ssh/key -o BatchMode=yes")."""
        self.host = host
        self.jobs_root = jobs_root
        self.remote = Remote(host, ssh_opts, runner) if host else None
        self.sleep = sleep
        self._procs: dict[str, subprocess.Popen] = {}     # this process's local jobs, reaped when polled

    def where(self) -> str:
        return self.host or "this machine"

    def dir(self, job_id: str) -> str:
        return f"{self.jobs_root.rstrip('/')}/{job_id}"

    @staticmethod
    def line(python: str, job_id: str, quote=shlex.quote) -> str:
        """The job's shell line: the only command a job runs."""
        return ("nohup sh -c " + shlex.quote(f'{quote(python)} {MARK} "$0" > log 2>&1; echo $? > exit') + " "
                + shlex.quote(job_id))

    # ------------------------------------------------------------------ this machine
    def _local(self, job_id: str = "") -> Path:
        return Path(self.jobs_root).expanduser() / job_id

    def _alive(self, job_id: str, d: Path, group: bool = False) -> bool:
        """Whether the job's leader (with `group`: any of its processes) still runs."""
        proc = self._procs.get(job_id)
        if proc is not None and not group:
            return proc.poll() is None
        try:
            pid = int((d / "pid").read_text().strip())
        except (OSError, ValueError):
            return False
        return _mine(pid, job_id)[1 if group else 0]

    # ------------------------------------------------------------------ operations
    def launch(self, job_id: str, files: list, python: str) -> str:
        """Make the job's directory (never one that exists), copy exactly `files` into it under their names, and
        start the job detached in its own process group. -> the shell line it runs."""
        names = [Path(f).name for f in files]
        if len(set(names)) != len(names):
            raise ValueError(f"two files with one name: {names}")
        if not JOB_ID.fullmatch(job_id):
            raise ValueError(f"not a job id: {job_id!r}")
        if not self.remote:
            line = self.line(os.path.expanduser(python), job_id)
            root = self._local()
            root.mkdir(parents=True, exist_ok=True)
            d = root / job_id
            d.mkdir()
            for f, name in zip(files, names):
                shutil.copyfile(f, d / name)
            proc = subprocess.Popen(["sh", "-c", line], cwd=d, start_new_session=True, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._procs[job_id] = proc
            (d / "pid").write_text(f"{proc.pid}\n")
            return line
        line = self.line(python, job_id, _q)
        d = _q(self.dir(job_id))
        self.remote.ssh(f"mkdir -p {_q(self.jobs_root)} && mkdir {d}")
        for f, name in zip(files, names):
            self.remote.push(str(f), f"{self._remote_root()}/{job_id}/{name}")
        self.remote.ssh(f"cd {d} && {{ setsid {line} > /dev/null 2>&1 < /dev/null & echo $! > pid; }}")
        return line

    def _remote_root(self) -> str:
        """jobs_root for rsync, which does not expand ~: the remote home, asked once."""
        if not (self.jobs_root == "~" or self.jobs_root.startswith("~/")):
            return self.jobs_root.rstrip("/")
        if not hasattr(self, "_home"):
            self._home = self.remote.ssh("echo \"$HOME\"").strip()
        return (self._home + self.jobs_root[1:]).rstrip("/")

    def poll(self, job_id: str) -> Status:
        """running, exited (code, result.json, the end of the log) or lost (no exit code and the job's process is
        gone: a reboot, kill -9). A status that cannot be read (ssh fails, the laptop slept) is running."""
        try:
            return self._poll(job_id)
        except (OSError, subprocess.SubprocessError):
            return Status("running")

    def _poll(self, job_id: str) -> Status:
        if not self.remote:
            d = self._local(job_id)
            alive = self._alive(job_id, d)          # before the exit file: a job exits after writing it
            text = (d / "exit").read_text() if (d / "exit").exists() else ""
            if text.strip():
                result = (d / "result.json").read_text() if (d / "result.json").exists() else None
                log = (d / "log").read_text(errors="replace") if (d / "log").exists() else ""
                return _parse(text, result, "".join(log.splitlines(True)[-TAIL:]))
            return Status("running" if alive else "lost")
        d = _q(self.dir(job_id))
        state = self.remote.ssh(
            f"cd {d} 2>/dev/null || {{ echo lost; exit 0; }}; pid=$(cat pid 2>/dev/null); "
            f"if [ -n \"$pid\" ] && ps -ww -o args= -p \"$pid\" 2>/dev/null | {_grep(job_id)}; then a=1; "
            f"else a=0; fi; "
            f"if [ -s exit ]; then echo exited; elif [ $a = 1 ]; then echo running; else echo lost; fi").strip()
        if state != "exited":
            return Status("running" if state == "running" else "lost")
        out = self.remote.ssh(f"cd {d} && cat exit && echo {SEP} && cat result.json 2>/dev/null; echo; echo {SEP}; "
                              f"tail -n {TAIL} log 2>/dev/null")
        exit_text, _, rest = out.partition(f"{SEP}\n")
        result, _, log = rest.partition(f"{SEP}\n")
        return _parse(exit_text, result.strip(), log)

    def cancel(self, job_id: str, grace: float = 5.0) -> None:
        """Stop the job's process group: TERM, then KILL after `grace` seconds. Its exit file then says 143, so it is
        never listed as unfinished, nor its pid signalled again."""
        if not self.remote:
            d = self._local(job_id)
            # a job this process started is its own group, even with its leader gone; any other only while a process
            # of its group still runs the job (after a reboot the pid may be another process's, or another job's)
            proc = self._procs.pop(job_id, None)
            if proc is not None or self._alive(job_id, d, group=True):
                pid = proc.pid if proc is not None else int((d / "pid").read_text().strip())
                try:
                    os.killpg(pid, signal.SIGTERM)
                    waited = 0.0
                    while waited < grace and _group(pid, proc):
                        self.sleep(0.1)
                        waited += 0.1
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            if proc is not None:
                proc.wait()
            if d.is_dir() and not (d / "exit").exists():
                (d / "exit").write_text("143\n")
            return
        d = _q(self.dir(job_id))
        self.remote.ssh(
            f"cd {d} || exit 0; pid=$(cat pid 2>/dev/null); "
            f"if [ -n \"$pid\" ] && ps -ww -A -o pgid=,args= | awk -v g=\"$pid\" '$1==g' | {_grep(job_id)}; "
            f"then kill -s TERM -- -\"$pid\"; "
            f"sleep {grace:g}; kill -s KILL -- -\"$pid\" 2>/dev/null; fi; [ -s exit ] || echo 143 > exit")

    def unfinished(self, tag: str) -> list[str]:
        """The jobs under jobs_root with no exit code whose spec.json carries workspace `tag`."""
        found: dict[str, str] = {}
        if not self.remote:
            root = self._local()
            for d in sorted(root.iterdir()) if root.is_dir() else ():
                if (d / "spec.json").exists() and not (d / "exit").exists():
                    found[d.name] = (d / "spec.json").read_text(encoding="utf-8")
        else:
            out = self.remote.ssh(
                f"cd {_q(self.jobs_root)} 2>/dev/null || exit 0; for d in *; do "
                f"if [ -f \"$d/spec.json\" ] && [ ! -s \"$d/exit\" ]; then "
                f"printf '%s\\t' \"$d\"; tr -d '\\n' < \"$d/spec.json\"; echo; fi; done")
            for row in out.splitlines():
                name, _, spec = row.partition("\t")
                found[name] = spec
        out = []
        for name, text in found.items():
            try:
                if json.loads(text).get("workspace") == tag:
                    out.append(name)
            except (ValueError, AttributeError):
                continue
        return out
