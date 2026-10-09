"""Vendored open-source programs and their patch stack (docs/design/HARNESS.md §6, §7; docs/DESIGN.md §3.3).

Upstream sits under `vendor/<name>/` of the seed byte for byte, with its LICENSE and a PROVENANCE.json that names the
release and the sha256 of every file. Secondary development goes adapter -> patch stack -> R's own edits:

  patches   `seeds/<name>/patches/NNN-<slug>.patch`, unified diffs against the package root (a/vendor/<name>/...), each
            with its reason in the header; replayed in order when the seed is built.
  R's edits a candidate may edit vendored code; once it is installed, `extract_patch` turns the difference from the
            seed into the next patch of the stack.
  upgrade   replace the upstream files, write the new provenance, replay the stack; a patch that no longer applies is
            reported, and becomes a cause for R to repair.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from .package import PackageError

NO_NEWLINE = "\\ No newline at end of file"
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(PackageError):
    """A patch that does not apply to the files it names."""


@dataclass(frozen=True)
class Provenance:
    name: str
    version: str
    url: str
    tag: str
    sha256: str                                # of the upstream release archive
    license: str                               # SPDX id
    files: int
    hashes: dict = field(default_factory=dict)   # path under vendor/<name>/ -> sha256 of its bytes
    license_sha256: str = ""


def _hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def read_provenance(files: dict[str, str], name: str) -> Provenance:
    path = f"vendor/{name}/PROVENANCE.json"
    if path not in files:
        raise PackageError(f"{path} is missing")
    try:
        p = json.loads(files[path])
        return Provenance(name=p["name"], version=p["version"], url=p["upstream"]["repository"],
                          tag=p["upstream"]["tag"], sha256=p["pypi"]["sdist"]["sha256"], license=p["license"]["spdx"],
                          files=int(p["file_count"]), hashes=dict(p["files"]),
                          license_sha256=p["license"].get("sha256", ""))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise PackageError(f"{path} is not a provenance record: {type(e).__name__}: {e}") from None


def verify(files: dict[str, str], name: str) -> list[str]:
    """Why the vendored files are not the release the provenance names ([] when they are): a file whose sha256
    differs, a listed file that is missing, an unlisted file, a license that is not the recorded one."""
    p = read_provenance(files, name)
    prefix = f"vendor/{name}/"
    have = {path[len(prefix):]: text for path, text in files.items() if path.startswith(prefix)}
    problems = []
    for rel, digest in sorted(p.hashes.items()):
        if rel not in have:
            problems.append(f"{prefix}{rel} is missing")
        elif _hash(have[rel]) != digest:
            problems.append(f"{prefix}{rel} is not the upstream file (sha256 differs)")
    extra = sorted(set(have) - set(p.hashes) - {"LICENSE", "PROVENANCE.json"})
    problems += [f"{prefix}{rel} is not in the provenance's file list" for rel in extra]
    if p.license_sha256 and _hash(have.get("LICENSE", "")) != p.license_sha256:
        problems.append(f"{prefix}LICENSE is not the recorded license")
    if len(p.hashes) != p.files:
        problems.append(f"{prefix}PROVENANCE.json lists {len(p.hashes)} files and says {p.files}")
    return problems


# ---------------------------------------------------------------------------- unified diffs
def _sections(patch: str) -> list[tuple[str | None, str | None, list[str]]]:
    """(old path, new path, hunk lines) per file of a unified diff; /dev/null is None."""
    out, lines, i = [], patch.splitlines(keepends=True), 0
    while i < len(lines):
        if lines[i].startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old, new = _path(lines[i][4:]), _path(lines[i + 1][4:])
            i += 2
            body = []
            while i < len(lines) and not (lines[i].startswith("--- ") and i + 1 < len(lines)
                                          and lines[i + 1].startswith("+++ ")):
                body.append(lines[i])
                i += 1
            out.append((old, new, body))
        else:
            i += 1
    return out


def _path(header: str) -> str | None:
    p = header.rstrip("\n").split("\t", 1)[0].strip()
    if p == "/dev/null":
        return None
    return p[2:] if p.startswith(("a/", "b/")) else p


def _apply_file(text: str, body: list[str], where: str) -> str:
    old = text.splitlines(keepends=True)
    out, cursor, i = [], 0, 0
    while i < len(body):
        m = HUNK.match(body[i])
        if not m:
            i += 1
            continue
        start = int(m.group(1)) - (0 if m.group(2) == "0" else 1)
        i += 1
        want, put, prev = [], [], " "
        while i < len(body) and not body[i].startswith("@@"):
            line = body[i]
            if line.startswith(NO_NEWLINE):
                for side in ((want, put) if prev == " " else (want,) if prev == "-" else (put,)):
                    if side:
                        side[-1] = side[-1].rstrip("\n")
            elif line[:1] in (" ", "-", "+"):
                prev = line[:1]
                if prev in (" ", "-"):
                    want.append(line[1:])
                if prev in (" ", "+"):
                    put.append(line[1:])
            i += 1
        at = start if old[start:start + len(want)] == want else None
        if at is None or at < cursor:
            found = [k for k in range(cursor, len(old) - len(want) + 1) if old[k:k + len(want)] == want]
            if not found:
                raise PatchError(f"{where}: a hunk at line {start + 1} does not match the file")
            at = min(found, key=lambda k: abs(k - start))
        out += old[cursor:at] + put
        cursor = at + len(want)
    return "".join(out + old[cursor:])


def apply_unified(files: dict[str, str], patch: str, *, name: str = "patch") -> dict[str, str]:
    """`files` with a unified diff applied (paths relative to the package root). Raises PatchError."""
    out = dict(files)
    sections = _sections(patch)
    if not sections:
        raise PatchError(f"{name}: no file in the patch")
    for old, new, body in sections:
        path = new or old
        if old is None:
            if path in out:
                raise PatchError(f"{name}: {path} is added and already exists")
            out[path] = _apply_file("", body, f"{name}: {path}")
        elif new is None:
            if old not in out:
                raise PatchError(f"{name}: {old} is deleted and does not exist")
            del out[old]
        else:
            if old not in out:
                raise PatchError(f"{name}: {old} does not exist")
            text = _apply_file(out.pop(old), body, f"{name}: {old}")
            out[new] = text
    return out


def unified(before: dict[str, str], after: dict[str, str], paths) -> str:
    """A unified diff of `paths` from `before` to `after`, with no-newline markers."""
    chunks = []
    for path in sorted(paths):
        a, b = before.get(path), after.get(path)
        if a == b:
            continue
        lines = difflib.unified_diff((a or "").splitlines(keepends=True), (b or "").splitlines(keepends=True),
                                     fromfile=f"a/{path}" if a is not None else "/dev/null",
                                     tofile=f"b/{path}" if b is not None else "/dev/null", n=3)
        for line in lines:
            chunks.append(line if line.endswith("\n") else line + "\n" + NO_NEWLINE + "\n")
    return "".join(chunks)


def apply_patches(vendor_files: dict[str, str], patches_dir: Path) -> tuple[dict[str, str], list[str]]:
    """Replay the patch stack in name order. -> (the patched files, the names of the patches that failed). A failed
    patch is skipped, so the later ones are still tried."""
    files, failed = dict(vendor_files), []
    for patch in sorted(Path(patches_dir).glob("*.patch")) if Path(patches_dir).is_dir() else ():
        try:
            files = apply_unified(files, patch.read_text(encoding="utf-8"), name=patch.name)
        except PatchError:
            failed.append(patch.name)
    return files, failed


def extract_patch(seed, installed, name: str) -> str | None:
    """The installed package's edits of vendor/<name>/ against the seed, as the next patch's text; None when it has
    none. The protected LICENSE and PROVENANCE.json never differ (check_shape)."""
    prefix = f"vendor/{name}/"
    paths = {p for p in set(seed.files) | set(installed.files) if p.startswith(prefix)}
    text = unified(seed.files, installed.files, paths)
    return text or None


def upgrade(seed_dir: Path, new_source: Path, provenance: Provenance) -> list[str]:
    """Replace vendor/<name>/ of a seed directory with a new upstream release and replay the patch stack against it.
    `new_source` holds the release's files and its LICENSE; the provenance's hashes are recomputed from them.
    -> the patches that no longer apply. The seed directory is a source tree: this is a developer's step, whose
    result is reviewed and committed like any code."""
    seed_dir, new_source = Path(seed_dir), Path(new_source)
    target = seed_dir / "vendor" / provenance.name
    fresh = {f.relative_to(new_source).as_posix(): f.read_bytes().decode("utf-8")
             for f in sorted(new_source.rglob("*")) if f.is_file() and "__pycache__" not in f.parts}
    if "LICENSE" not in fresh:
        raise PackageError(f"{new_source} has no LICENSE")
    hashes = {rel: _hash(text) for rel, text in fresh.items() if rel != "LICENSE"}
    record = {"name": provenance.name, "version": provenance.version,
              "license": {"spdx": provenance.license, "file": "LICENSE", "sha256": _hash(fresh["LICENSE"])},
              "upstream": {"repository": provenance.url, "tag": provenance.tag},
              "pypi": {"sdist": {"sha256": provenance.sha256}},
              "files": dict(sorted(hashes.items())), "file_count": len(hashes),
              "total_bytes": sum(len(t.encode("utf-8")) for r, t in fresh.items() if r != "LICENSE")}
    if target.exists():
        for f in sorted(target.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
    for rel, text in {**fresh, "PROVENANCE.json": json.dumps(record, indent=1) + "\n"}.items():
        out = target / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(text.encode("utf-8"))
    files = {f"vendor/{provenance.name}/{rel}": text for rel, text in fresh.items()}
    _, failed = apply_patches(files, seed_dir / "patches")
    return failed
