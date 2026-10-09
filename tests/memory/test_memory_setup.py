"""`python -m ouroloop.memory check` (docs/design/MEMORY.md §2 `__main__.py`, §11).

Check only: no test runs `setup` (it installs the lock's 161 wheels and fetches the model). The memory venv passes
check; a venv whose freeze differs from the lock in one version, or holds one package more, fails check and the
sidecar refuses to start on it (exit 3); a lock that differs fails check; a model missing from the Hugging Face cache,
or one of its files changed, fails check. All slow: they read the memory venv.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from memory_support import REPO, venv  # noqa: F401
from ouroloop.contracts import MemorySidecarError
from ouroloop.memory import LOCK, MODEL_AUX_SHA256, MODEL_FILE, MODEL_REPO, MODEL_REVISION, lock_sha256, model_snapshot
from ouroloop.memory.client import MemoryClient
from ouroloop.memory.settings import Settings

pytestmark = pytest.mark.slow


def memory(*args, env=None):
    run = subprocess.run([sys.executable, "-m", "ouroloop.memory", *map(str, args)], cwd=REPO,
                         env={**os.environ, "PYTHONPATH": str(REPO), **(env or {})}, capture_output=True, text=True,
                         timeout=300)
    try:
        report = json.loads(run.stdout)
    except ValueError:
        report = {"stdout": run.stdout, "stderr": run.stderr}
    return run.returncode, report


def site_packages(v: Path) -> Path:
    return next((v / "lib").glob("python3.*/site-packages"))


@pytest.fixture(scope="module")
def metadata_venv(venv, tmp_path_factory):
    """A python3.12 venv holding the memory venv's distribution metadata (every *.dist-info, no package code but
    pip's, copied, not installed): what `pip freeze` and the sidecar's own freeze check read, so a tampered copy is
    cheap."""
    made = tmp_path_factory.mktemp("venvs") / "meta"
    subprocess.run([str(venv / "bin" / "python"), "-m", "venv", "--without-pip", str(made)], check=True,
                   capture_output=True)
    src, dst = site_packages(venv), site_packages(made)
    for d in src.glob("*.dist-info"):
        shutil.copytree(d, dst / d.name)
    shutil.copytree(src / "pip", dst / "pip")              # `python -m pip freeze` needs pip's code
    return made


def tampered(src: Path, dst: Path, *, package: str = "", version: str | None = None, extra: str | None = None) -> Path:
    shutil.copytree(src, dst, symlinks=True)
    sp = site_packages(dst)
    if version:
        info = next(sp.glob(f"{package}-*.dist-info"))
        meta = info / "METADATA"
        meta.write_text(re.sub(r"(?m)^Version: .*$", f"Version: {version}", meta.read_text()))
        info.rename(sp / f"{package}-{version}.dist-info")
    if extra:
        (sp / f"{extra}-1.0.dist-info").mkdir()
        (sp / f"{extra}-1.0.dist-info" / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {extra}\n"
                                                                 f"Version: 1.0\n")
    return dst


def refused(bad: Path, tmp_path: Path, match: str) -> None:
    c = MemoryClient()
    with pytest.raises(MemorySidecarError, match=r"exited with 3 before it served(.|\n)*refused to start: the venv "
                                                 r"\(.*\) is not the lock requirements-macos-arm64\.lock: " + match):
        c.start(venv=bad, workdir=tmp_path / "run", chroma_path=tmp_path / "chroma", settings=Settings().to_json(),
                timeout=60)
    assert c.proc.returncode == 3


def test_the_memory_venv_passes_check(venv):
    code, report = memory("check", "--venv", venv)
    assert code == 0 and report["ok"] is True and report["problems"] == [], report
    assert report["cmd"] == "check" and report["venv"] == str(venv.resolve()) and report["packages"] == 161
    assert report["lock_sha256"] == lock_sha256() and report["lock"] == str(LOCK)


def test_the_metadata_copy_passes_and_one_changed_version_fails_check_and_the_sidecar(metadata_venv, tmp_path):
    assert memory("check", "--venv", metadata_venv)[0] == 0          # the copy is the lock, as pip freeze sees it
    bad = tampered(metadata_venv, tmp_path / "bad", package="idna", version="9.9")
    code, report = memory("check", "--venv", bad)
    assert code == 1 and report["ok"] is False and any(p.startswith("idna: 9.9 installed") for p in
                                                       report["problems"]), report
    refused(bad, tmp_path, r"idna: 9\.9 installed")


def test_one_package_more_fails_check_and_the_sidecar(metadata_venv, tmp_path):
    bad = tampered(metadata_venv, tmp_path / "extra", extra="evilpkg")
    code, report = memory("check", "--venv", bad)
    assert code == 1 and report["problems"] == ["evilpkg (1.0): installed, not in the lock"], report
    refused(bad, tmp_path, r"evilpkg==1\.0: installed, not in the lock")


def test_one_package_missing_fails_check_and_the_sidecar(metadata_venv, tmp_path):
    bad = tmp_path / "missing"
    shutil.copytree(metadata_venv, bad, symlinks=True)
    shutil.rmtree(next(site_packages(bad).glob("idna-*.dist-info")))
    code, report = memory("check", "--venv", bad)
    assert code == 1 and len(report["problems"]) == 1 and report["problems"][0].startswith("idna==")
    assert report["problems"][0].endswith(": pinned, not installed")
    refused(bad, tmp_path, r"idna==\S+: pinned, not installed")


def test_a_lock_that_differs_fails_check(venv, tmp_path):
    lock = tmp_path / "lock.txt"
    lock.write_text(re.sub(r"(?m)^chromadb==1\.5\.9 ", "chromadb==1.5.8 ", LOCK.read_text()))
    code, report = memory("check", "--venv", venv, "--lock", lock)
    assert code == 1 and report["problems"] == ["chromadb: 1.5.9 installed, 1.5.8 pinned"], report
    assert report["lock_sha256"] != lock_sha256()


def test_no_venv_fails_check(tmp_path):
    code, report = memory("check", "--venv", tmp_path / "none")
    assert code == 1 and report["problems"][0].startswith(f"{(tmp_path / 'none').resolve()}: no venv"), report


def test_a_model_missing_from_the_cache_fails_check(venv, tmp_path):
    code, report = memory("check", "--venv", venv, env={"HF_HUB_CACHE": str(tmp_path / "empty-cache")})
    assert code == 1 and any(f"{MODEL_FILE}: missing" in p for p in report["problems"]), report
    assert len(report["problems"]) == 1 + len(MODEL_AUX_SHA256)


def test_a_model_file_that_is_not_the_pinned_one_fails_check(venv, tmp_path):
    """A cache holding the pinned revision's path with one file changed: every file the embedder reads is pinned."""
    real = model_snapshot(MODEL_REPO, MODEL_REVISION)
    cache = tmp_path / "hub"
    snap = cache / ("models--" + MODEL_REPO.replace("/", "--")) / "snapshots" / MODEL_REVISION
    for rel in [MODEL_FILE, *MODEL_AUX_SHA256]:
        (snap / rel).parent.mkdir(parents=True, exist_ok=True)
        if rel == MODEL_FILE:
            (snap / rel).symlink_to((real / rel).resolve())       # 130 MB: linked, not copied
        else:
            shutil.copyfile(real / rel, snap / rel)
    assert memory("check", "--venv", venv, env={"HF_HUB_CACHE": str(cache)})[0] == 0
    (snap / "config.json").write_text((snap / "config.json").read_text().replace("384", "385", 1))
    code, report = memory("check", "--venv", venv, env={"HF_HUB_CACHE": str(cache)})
    assert code == 1 and len(report["problems"]) == 1, report
    assert report["problems"][0].startswith(f"{snap / 'config.json'}: sha256 ")
    assert report["problems"][0].endswith(f", pinned {MODEL_AUX_SHA256['config.json']}")
