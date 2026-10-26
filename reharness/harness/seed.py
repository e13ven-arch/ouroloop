"""Building the seed W a K names (docs/design/HARNESS.md §7).

`harness/seeds/<name>/` is a source tree: the vendored upstream under vendor/, the adapter, the seed's hooks, tools,
prompts and tests, a manifest, and `patches/` (the patch stack, not package files). Building it:

  1. read the tree (bytecode caches and Finder files are not the seed's);
  2. check every vendored package against its PROVENANCE.json (SeedUnavailable when the bytes are not the release);
  3. replay the patch stack (SeedUnavailable naming the patches that fail);
  4. fill the manifest from K: model.base and effort are K's actor, config takes the defaults the seed does not set,
     memory.enabled is K's;
  5. check the shape under K's limits and store it.

The seed release itself is written by the activity (evaluate.release.Releases.seed).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..contracts import Objects
from .package import DEFAULT_CONFIG, Limits, Package, PackageError, build
from .vendor import apply_patches, verify

SEEDS = Path(__file__).with_name("seeds")
SKIP = ("__pycache__", ".DS_Store")


class SeedUnavailable(RuntimeError):
    """The seed's source is missing, is not the upstream release its provenance names, or its patch stack does not
    apply. An installation fault, never a candidate's: no seed is built until it is fixed."""


def seed_dir(name: str) -> Path:
    path = SEEDS / name
    if not name or "/" in name or not path.is_dir():
        raise SeedUnavailable(f"no seed named {name!r} under {SEEDS}")
    return path


def read_tree(root: Path) -> dict[str, str]:
    """{path: text} of every package file under a seed directory, patches/ excluded, bytes decoded as UTF-8."""
    files: dict[str, str] = {}
    for f in sorted(Path(root).rglob("*")):
        rel = f.relative_to(root).as_posix()
        parts = rel.split("/")
        if f.is_dir() or parts[0] == "patches" or any(p in SKIP or p.endswith(".pyc") for p in parts):
            continue
        try:
            files[rel] = f.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise SeedUnavailable(f"seed file {rel}: not UTF-8 text") from None
    return files


def seed_files(name: str, *, core: dict) -> dict[str, str]:
    """The seed's files as K builds them: provenance checked, patches replayed, manifest filled from K."""
    root = seed_dir(name)
    files = read_tree(root)
    vendored = sorted({p.split("/")[1] for p in files if p.startswith("vendor/")})
    for pkg in vendored:
        try:
            problems = verify(files, pkg)
        except PackageError as e:
            problems = [str(e)]
        if problems:
            raise SeedUnavailable(f"seed {name}: vendor/{pkg} is not the release its provenance names: "
                                  + "; ".join(problems[:5]))
    files, failed = apply_patches(files, root / "patches")
    if failed:
        raise SeedUnavailable(f"seed {name}: patches that do not apply: {failed}")
    try:
        manifest = json.loads(files["manifest.json"])
    except (KeyError, json.JSONDecodeError) as e:
        raise SeedUnavailable(f"seed {name}: manifest.json: {e}") from None
    model = core.get("model") or {}
    manifest["model"] = {"base": model.get("identity") or "", "effort": model.get("effort") or "medium",
                         **({"max_tokens": model["max_tokens"]} if model.get("max_tokens") else {}),
                         "artifact_ref": None}
    manifest["config"] = {**DEFAULT_CONFIG, **(manifest.get("config") or {})}
    manifest["memory"] = {**(manifest.get("memory") or {}),
                          "enabled": bool((core.get("memory") or {}).get("enabled", False))}
    files["manifest.json"] = json.dumps(manifest, indent=1) + "\n"
    return files


def build_seed(name: str, *, core: dict, objects: Objects) -> Package:
    """The seed W of K, checked under K's limits and stored. Same source and same K, same ref."""
    files = seed_files(name, core=core)
    try:
        return build(objects, files, limits=Limits.from_core(core), seed=None)
    except PackageError as e:
        raise SeedUnavailable(f"seed {name} does not fit K: {e}") from None
