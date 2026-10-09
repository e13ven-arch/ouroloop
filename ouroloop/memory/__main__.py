"""`python -m ouroloop.memory setup|check`: build and verify the memory sidecar's venv and model (docs/design/MEMORY.md §2).

    python -m ouroloop.memory setup [--venv PATH] [--lock PATH] [--python python3.12]
    python -m ouroloop.memory check [--venv PATH] [--lock PATH]

setup creates the venv with `python3.12 -m venv` (default ~/.cache/ouroloop/memory-venv/<lock sha16>/), installs the
lock with `pip install --require-hashes --no-deps --only-binary :all:` (161 hashed wheels, nothing resolved or
built), fetches BAAI/bge-small-en-v1.5 at its pinned revision into the Hugging Face cache when it is not there
(no token is sent), and then runs the check. check changes nothing: `pip freeze` of the venv must equal the lock's
pins exactly (no package missing, none extra, every version the pinned one), the interpreter must be 3.12, and
every model file must have its pinned sha256. Exit status 0 when all of it holds, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import LOCK, MODEL_AUX_SHA256, MODEL_FILE, MODEL_REPO, MODEL_REVISION, default_venv, lock_sha256, model_problems

PY = "3.12"
NOT_FROZEN = {"pip", "setuptools", "wheel", "distribute"}


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_pins(lock: Path) -> dict:
    pins = {}
    for line in Path(lock).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, version = line.split()[0].partition("==")
            pins[canonical(name)] = version
    return pins


def freeze(venv: Path) -> dict:
    out = subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "freeze", "--disable-pip-version-check"],
                         capture_output=True, text=True, env=_clean_env())
    if out.returncode != 0:
        raise RuntimeError(f"pip freeze failed in {venv}: {out.stderr.strip()[:500]}")
    got = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            got[canonical(re.split(r"[ @]", line)[0])] = line   # an editable or a direct URL: never in the lock
            continue
        name, _, version = line.partition("==")
        if canonical(name) not in NOT_FROZEN:
            got[canonical(name)] = version
    return got


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k in ("HOME", "PATH", "LANG", "TMPDIR", "USER", "HF_HOME",
                                                       "HF_HUB_CACHE", "XDG_CACHE_HOME", "PIP_CACHE_DIR",
                                                       "PIP_INDEX_URL", "PIP_CERT", "SSL_CERT_FILE")}
    env["PYTHONNOUSERSITE"] = "1"
    return env


def problems(venv: Path, lock: Path) -> list[str]:
    return venv_problems(venv, lock) + model_problems()


def venv_problems(venv: Path, lock: Path) -> list[str]:
    venv = Path(venv)
    python = venv / "bin" / "python"
    if not python.exists():
        return [f"{venv}: no venv (run `python -m ouroloop.memory setup --venv {venv}`)"]
    out = []
    version = subprocess.run([str(python), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                             capture_output=True, text=True).stdout.strip()
    if version != PY:
        out.append(f"{python} is Python {version}; the lock is for {PY}")
    want, got = lock_pins(lock), freeze(venv)
    out += [f"{n}: {got[n]} installed, {v} pinned" for n, v in sorted(want.items()) if n in got and got[n] != v]
    out += [f"{n}=={v}: pinned, not installed" for n, v in sorted(want.items()) if n not in got]
    out += [f"{n} ({got[n]}): installed, not in the lock" for n in sorted(set(got) - set(want))]
    return out


def fetch_model(venv: Path) -> None:
    """Download the pinned model files into the HF cache with the venv's huggingface_hub (no token sent)."""
    files = [MODEL_FILE, *sorted(MODEL_AUX_SHA256), "special_tokens_map.json", "tokenizer_config.json", "vocab.txt"]
    code = ("import sys; from huggingface_hub import hf_hub_download\n"
            "for f in sys.argv[3:]: print(hf_hub_download(sys.argv[1], f, revision=sys.argv[2]))")
    env = {**_clean_env(), "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
    run = subprocess.run([str(venv / "bin" / "python"), "-c", code, MODEL_REPO, MODEL_REVISION, *files], env=env,
                         capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError(f"downloading {MODEL_REPO}@{MODEL_REVISION} failed: {run.stderr.strip()[-1500:]}")


def setup(venv: Path, lock: Path, python: str) -> list[str]:
    venv = Path(venv)
    if not (venv / "bin" / "python").exists():
        made = subprocess.run([python, "-m", "venv", str(venv)], capture_output=True, text=True)
        if made.returncode != 0:
            raise RuntimeError(f"{python} -m venv {venv}: {made.stderr.strip()[:500]}")
    if venv_problems(venv, lock):
        pip = subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "install", "--require-hashes", "--no-deps",
                              "--only-binary", ":all:", "--disable-pip-version-check", "--no-input", "-r", str(lock)],
                             env=_clean_env(), capture_output=True, text=True)
        if pip.returncode != 0:
            raise RuntimeError(f"pip install -r {lock} failed:\n{pip.stdout[-2000:]}\n{pip.stderr[-2000:]}")
    if any("missing (download" in p for p in model_problems()):
        fetch_model(venv)
    return problems(venv, lock)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ouroloop.memory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("setup", "check"):
        p = sub.add_parser(name)
        p.add_argument("--venv", type=Path, default=None)
        p.add_argument("--lock", type=Path, default=LOCK)
        if name == "setup":
            p.add_argument("--python", default=f"python{PY}")
    args = ap.parse_args(argv)
    venv = (args.venv or default_venv(args.lock)).expanduser().resolve()
    try:
        found = setup(venv, args.lock, args.python) if args.cmd == "setup" else problems(venv, args.lock)
    except RuntimeError as e:
        print(f"memory {args.cmd}: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"cmd": args.cmd, "venv": str(venv), "lock": str(args.lock), "lock_sha256": lock_sha256(args.lock),
                      "packages": len(lock_pins(args.lock)), "ok": not found, "problems": found}, indent=1))
    return 0 if not found else 1


if __name__ == "__main__":
    raise SystemExit(main())
