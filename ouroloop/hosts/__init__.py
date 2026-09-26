"""Training hosts (docs/TRAINING_HOSTS.md): a machine that runs ouroloop's training jobs, prepared by bootstrap.sh
and described by ~/.ouroloop/host.json.

    ouroloop host init <target> --trainer NAME     pack this checkout's ouroloop (an sdist) and the plugins' local
                                                   sources, send them, run bootstrap.sh there, print host.json
    ouroloop host check <target>                   print host.json, and what keeps it from running this ouroloop

A target is "local" (this machine) or user@host over ssh. Before each job the trainer reads host.json and, when the
host runs another ouroloop, lacks a plugin or has it from another source, runs init itself (`ensure`): bootstrap.sh
is idempotent. Nothing but fixed paths and a generated id reaches a shell line: what the host installs travels as
files (requirements.txt, plan.json).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = "~/.ouroloop"
VENV = "~/.ouroloop/venv"
PYTHON = "~/.ouroloop/venv/bin/python"
MANIFEST = "~/.ouroloop/host.json"
BOOTSTRAP = Path(__file__).with_name("bootstrap.sh")
PACKAGE = Path(__file__).resolve().parents[1]
CHECKOUT = PACKAGE.parent                   # the ouroloop checkout, when the package is not an installed copy
SDIST = ("pyproject.toml", "README.md", "LICENSE")
SKIP = {".git", "__pycache__", ".venv", ".pytest_cache", "node_modules"}
INIT_TIMEOUT_S = 3600                       # installing torch and its CUDA libraries takes minutes
TAIL = 40


class HostError(RuntimeError):
    pass


# ---------------------------------------------------------------------- on the host
def gpus() -> list[str]:
    """The NVIDIA GPUs nvidia-smi lists (name, memory), none without it."""
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _hf() -> dict:
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


def manifest(plan: dict | None = None) -> dict:
    """This host as host.json has it: ouroloop (its code version, and the control plane's git state it was sent
    from), Python, GPUs, Hugging Face, and each plugin of `plan` (plan.json) with its version, source and check."""
    import platform

    from .. import __version__
    from ..ledger import now
    from ..release import code_version
    from ..trainers import load, version
    plan = plan or {}
    plugins = {}
    for name, info in (plan.get("plugins") or {}).items():
        plugin = load(name)
        check = getattr(plugin, "check", None)
        plugins[name] = {"version": version(plugin), "source": info.get("source"),
                         **({"check": check()} if callable(check) else {})}
    return {"ouroloop": {"version": __version__, "code": code_version(), "git": plan.get("git")},
            "python": platform.python_version(), "platform": platform.platform(), "gpu": gpus(),
            "plugins": plugins, "hf": _hf(), "ts": now()}


# ---------------------------------------------------------------------- what init sends
def files(source: Path) -> list[str]:
    """The files of a plugin's source directory: those git tracks in a checkout (no runs, data or venvs), else all
    of them but caches."""
    try:
        out = subprocess.run(["git", "-C", str(source), "ls-files", "-z"], capture_output=True, check=True).stdout
        names = [n for n in out.decode().split("\0") if n and (source / n).is_file()]
        if names:
            return sorted(names)
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for base, dirs, names in os.walk(source):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        found += [os.path.relpath(os.path.join(base, n), source) for n in names if not n.endswith(".pyc")]
    return sorted(found)


def fingerprint(source: Path) -> str:
    h = hashlib.sha256()
    for name in files(source):
        h.update(name.encode() + b"\0" + (source / name).read_bytes() + b"\0")
    return f"sha256:{h.hexdigest()[:16]}"


def _tar(path: Path, prefix: str, root: Path, names: list[str]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name in names:
            tar.add(root / name, arcname=f"{prefix}/{name}", recursive=False)
    return path


def pack_ouroloop(out: Path) -> Path:
    """This ouroloop as an sdist: pyproject.toml, README.md, LICENSE and every file of the package, exactly the files
    code_version hashes, so the host runs this code."""
    from .. import __version__
    if not (CHECKOUT / "pyproject.toml").exists():
        raise HostError(f"{PACKAGE} is an installed copy: run host init from an ouroloop checkout")
    names = [n for n in SDIST if (CHECKOUT / n).exists()]
    names += sorted(p.relative_to(CHECKOUT).as_posix() for p in PACKAGE.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    return _tar(out / "ouroloop.tar.gz", f"ouroloop-{__version__}", CHECKOUT, names)


def source(options: dict) -> tuple[str | None, str | Path | None]:
    """A plugin's `source` option: ("dir", a local directory) for a path (absolute, from ~, or ./ ../), ("pip", a
    requirement) otherwise, (None, None) without one."""
    src = (options or {}).get("source")
    if not src:
        return None, None
    if not str(src).startswith(("/", "~", "./", "../")):
        return "pip", str(src)
    path = Path(os.path.expanduser(src)).resolve()
    if not path.is_dir():
        raise HostError(f"source {src!r}: no such directory")
    return "dir", path


def source_id(options: dict) -> str | None:
    """What host.json records of a plugin's source: a local directory's fingerprint, or the requirement."""
    kind, value = source(options)
    return fingerprint(value) if kind == "dir" else value


def plan(trainers: dict, out: str | Path, hf: bool = False) -> Path:
    """Write what init sends into `out`: the ouroloop sdist, each local plugin source as <name>.tar.gz,
    requirements.txt (those, and the plugins' requirements), plan.json and bootstrap.sh."""
    from ..trainers import load
    from ..worker import git_state
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    reqs, plugins = [f"./{pack_ouroloop(out).name}"], {}
    for name, options in trainers.items():
        plugin = load(name)
        kind, value = source(options)
        if kind == "dir":
            archive = _tar(out / f"{name.replace('.', '_')}.tar.gz", name, value, files(value))
            reqs.append(f"./{archive.name}")
        elif kind == "pip":
            reqs.append(value)
        reqs += list(getattr(plugin, "REQUIREMENTS", []))
        plugins[name] = {"source": source_id(options)}
    if hf:
        reqs.append("huggingface_hub")
    (out / "requirements.txt").write_text("".join(f"{r}\n" for r in reqs), encoding="utf-8")
    (out / "plan.json").write_text(json.dumps({"git": git_state(CHECKOUT), "plugins": plugins}, indent=2),
                                   encoding="utf-8")
    shutil.copyfile(BOOTSTRAP, out / "bootstrap.sh")
    return out


# ---------------------------------------------------------------------- from the control plane
def _remote(target: str, ssh_opts: str, runner):
    from ..evolve.compute import Remote
    return Remote(target, ssh_opts, runner)


def read(target: str, ssh_opts: str = "-o BatchMode=yes", runner=None) -> dict | None:
    """host.json on `target`, None if it has none (or an unreadable one)."""
    if target == "local":
        path = Path(os.path.expanduser(MANIFEST))
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    else:
        text = _remote(target, ssh_opts, runner).ssh(f'cat "$HOME"/{MANIFEST[2:]} 2>/dev/null || true')
    try:
        m = json.loads(text) if text.strip() else None
    except ValueError:
        return None
    return m if isinstance(m, dict) else None


def stale(m: dict | None, trainers: dict, hf: bool = False) -> list[str]:
    """Why a host with manifest `m` cannot run this ouroloop's jobs of `trainers` ({name: options}); none if it can."""
    from ..release import code_version
    if not m:
        return [f"no {MANIFEST}"]
    why, mine = [], code_version()
    theirs = (m.get("ouroloop") or {}).get("code")
    if theirs != mine:
        why.append(f"it runs ouroloop {theirs}, this is {mine}")
    for name, options in trainers.items():
        p = (m.get("plugins") or {}).get(name)
        if p is None:
            why.append(f"no trainer plugin {name}")
        elif p.get("source") != source_id(options):
            why.append(f"plugin {name} is from another source")
    if hf and not (m.get("hf") or {}).get("hub"):
        why.append('no huggingface_hub for transport "hf"')
    if hf and not (m.get("hf") or {}).get("token"):
        why.append('no Hugging Face token for transport "hf": set HF_TOKEN there, or run `hf auth login` there')
    return why


def _tail(text: str) -> str:
    return "".join((text or "").splitlines(True)[-TAIL:])


def init(target: str, trainers: dict, ssh_opts: str = "-o BatchMode=yes", hf: bool = False,
         runner=None) -> tuple[dict | None, str]:
    """Run bootstrap.sh on `target` with this ouroloop and `trainers` ({name: options}). -> (host.json, the log)."""
    with tempfile.TemporaryDirectory() as tmp:
        d = plan(trainers, Path(tmp) / "init", hf)
        if target == "local":
            try:
                proc = subprocess.run(["sh", str(d / "bootstrap.sh"), str(d)], capture_output=True, text=True,
                                      timeout=INIT_TIMEOUT_S)
            except (OSError, subprocess.SubprocessError) as e:
                raise HostError(f"host init on this machine failed: {e}") from e
            log = proc.stdout + proc.stderr
            if proc.returncode:
                raise HostError(f"host init on this machine failed (exit {proc.returncode}):\n{_tail(log)}")
        else:
            from ..ledger import new_id
            remote, name = _remote(target, ssh_opts, runner), new_id("init")
            where = f'"$HOME"/.ouroloop/incoming/{name}'
            try:
                remote.ssh('mkdir -p "$HOME"/.ouroloop/incoming')
                remote.push(f"{d}/", f".ouroloop/incoming/{name}/")         # rsync: relative to the remote home
                log = remote.ssh(f"sh {where}/bootstrap.sh {where} 2>&1; code=$?; rm -rf {where}; exit $code",
                                 timeout=INIT_TIMEOUT_S)
            except subprocess.CalledProcessError as e:
                raise HostError(f"host init on {target} failed (exit {e.returncode}):\n"
                                f"{_tail((e.stdout or '') + (e.stderr or ''))}") from e
            except (OSError, subprocess.SubprocessError) as e:
                raise HostError(f"host init on {target} failed: {e}") from e
    return read(target, ssh_opts, runner), log


def ensure(target: str, trainers: dict, ssh_opts: str = "-o BatchMode=yes", hf: bool = False, runner=None,
           say=print) -> dict:
    """host.json of a host ready for this ouroloop's jobs of `trainers`: as it is, or after host init. HostError if
    init fails or leaves it not ready."""
    m = read(target, ssh_opts, runner)
    why = stale(m, trainers, hf)
    if not why:
        return m
    say(f"host {target}: {'; '.join(why)}: running host init")
    m, _ = init(target, trainers, ssh_opts, hf, runner)
    why = stale(m, trainers, hf)
    if why:
        raise HostError(f"host {target} after host init: {'; '.join(why)}")
    return m
