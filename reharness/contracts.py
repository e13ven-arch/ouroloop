"""The shapes every package shares (docs/design/CONTRACTS.md): identity, the content-addressed object store, the event
log, the JSONL and file helpers, and the error classes.

Nothing here runs a model, reads a split or decides whether a candidate is any good. What it decides is what two records
mean when they claim to be the same thing:

    identity    an immutable object is named `kind:sha256:<64 hex>` over its canonical JSON. Reordering keys is the same
                object; changing one word is another. A timestamp or a note is not part of what an object is, so it never
                enters the hash (`VOLATILE`).
    closure     an object refers to others by ref; `copy_closure` moves an object with everything it refers to, and the
                refs stay the same in the other store.

Each package registers its record types (`register`); `Objects.load(ref)` finds the class by the ref's kind.
"""
from __future__ import annotations

import fcntl
import importlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator

REF = re.compile(r"[a-z_]+:sha256:[0-9a-f]{64}")


# ---------------------------------------------------------------------------- errors (CONTRACTS §8)
class ContractError(ValueError):
    """A shape, schema or reference that is not legal. A configuration or code error: the loop stops."""


class TrainOnlyError(ContractError):
    """Something asked for dev or test content where only train may be read. The leakage boundary: stop and record."""


class CandidateDefect(RuntimeError):
    """The package's own fault: a hook raised or answered outside its protocol, a tool raised, a smoke run crashed.

    Kept apart from infrastructure on purpose: an infrastructure failure may be retried under a fixed rule, a defect may
    not, because retrying a candidate until the broken task happens to pass is how a defective candidate gets promoted.
    """


class Infrastructure(RuntimeError):
    """A host-side failure. The attempt is marked `infrastructure`, counted against the schedule's
    `max_infrastructure_failures`, and never enters a gate reading."""


class WorkerUnavailable(Infrastructure):
    """No qualified worker: never a reason to run package code in-process."""


class StaleWorker(WorkerUnavailable):
    """The worker runs another worker_runner.py than this host's (an image built before the runner changed). A startup
    error: rebuild the image."""


class WorkerCrashed(Infrastructure):
    """The worker died, timed out or broke the protocol."""


class EgressUnavailable(Infrastructure):
    """K asks for the egress proxy and it cannot be started on an internal network."""


class MemorySidecarError(Infrastructure):
    """The memory sidecar is down, refused its lock file, or answered outside its protocol."""


class LimitHit(RuntimeError):
    """One of the five hard limits of an attempt was reached. Neither a defect nor infrastructure: the attempt ends as
    termination `budget` with `limit` naming the dimension, and the world is scored as it is."""

    def __init__(self, limit: str, message: str | None = None):
        super().__init__(message or f"the attempt reached its hard limit on {limit}")
        self.limit = limit


class BudgetError(RuntimeError):
    """The budget book cannot do what was asked. A paid step that gets one does not start."""


class BudgetMismatch(BudgetError):
    """The budget asked for is not the one the activity froze: a scope reopened with other limits, an unknown dimension,
    an allocation that does not fit. A person looks at it; the activity pauses."""


class BudgetExhausted(BudgetError):
    """The step would exceed a limit of its scope or of an ancestor. Not a fault: whatever runs on it ends."""


# ---------------------------------------------------------------------------- identity
def canonical(obj: Any) -> bytes:
    """The bytes an identity is taken over: UTF-8, sorted keys, compact separators. NaN and Infinity are refused."""
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except ValueError as e:
        raise ContractError(f"not canonical JSON: {e}") from None


def digest(obj: Any) -> str:
    return sha256(canonical(obj)).hexdigest()


def ref_for(kind: str, payload: Any) -> str:
    if not re.fullmatch(r"[a-z_]+", kind):
        raise ContractError(f"object kind {kind!r}: lowercase letters and underscores")
    return f"{kind}:sha256:{digest(payload)}"


def parse_ref(ref: str) -> tuple[str, str]:
    if not isinstance(ref, str) or not REF.fullmatch(ref):
        raise ContractError(f"{ref!r} is not a content reference (kind:sha256:<64 hex>)")
    kind, _, rest = ref.partition(":")
    return kind, rest.partition(":")[2]


def short(ref: str) -> str:
    """For reports and logs only. Nothing resolves a short id: two objects may share one."""
    kind, d = parse_ref(ref)
    return f"{kind}:{d[:12]}"


def check_refs(refs, *, kind: str | None = None, what: str = "reference") -> tuple[str, ...]:
    out = []
    for r in refs or ():
        k, _ = parse_ref(r)
        if kind is not None and k != kind:
            raise ContractError(f"{what} {short(r)} is a {k}, not a {kind}")
        out.append(r)
    return tuple(out)


# ---------------------------------------------------------------------------- records
# Fields that describe a record rather than identify it: two records that differ only in these are one object.
VOLATILE = ("created", "ts", "note", "notes", "display", "path_hint", "observed_at", "sealed_at")

TYPES: dict[str, type] = {}


def register(cls: type) -> type:
    """Class decorator: `Objects.load(ref)` finds this class by its TYPE."""
    if TYPES.get(cls.TYPE, cls) is not cls:
        raise ContractError(f"record kind {cls.TYPE!r} is registered twice")
    TYPES[cls.TYPE] = cls
    return cls


class Record:
    """A content-addressed object. Subclasses are dataclasses; `TYPE` is the kind in the ref, `payload()` is what the
    digest is taken over (absent and None are the same thing, volatile fields are left out)."""
    TYPE = "object"

    def payload(self) -> dict:
        out = {}
        for f in fields(self):
            if f.name in VOLATILE:
                continue
            value = getattr(self, f.name)
            if value is None:
                continue
            out[f.name] = plain(value)
        return out

    def ref(self) -> str:
        return ref_for(self.TYPE, self.payload())

    def to_json(self) -> dict:
        return {**{f.name: plain(getattr(self, f.name)) for f in fields(self)}, "ref": self.ref()}

    @classmethod
    def from_json(cls, d: dict):
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(d) - known - {"ref"})
        if unknown:
            raise ContractError(f"{cls.__name__} does not take {unknown}")
        return cls(**{k: v for k, v in d.items() if k in known})


def plain(value: Any) -> Any:
    """A value as JSON: records as their payload, tuples as lists."""
    if isinstance(value, Record):
        return value.payload()
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    return value


def as_dict(record) -> dict:
    """A dataclass as plain JSON, every field included (for files that are not objects)."""
    return plain(asdict(record))


# ---------------------------------------------------------------------------- files
_SECRETS = [re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"(?i)\b\w*(api[_-]?key|access[_-]?key|token|secret|passw(or)?d|credential)\w*[\"']?\s*[=:]\s*"
    r"(?![\d.,]+(\s|$))\S+",                                 # a number (max_tokens: 4096) is no secret
    r"sk-[A-Za-z0-9_\-]{16,}",
    r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}",
    r"AKIA[0-9A-Z]{16}",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}",
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    r"\b[0-9a-f]{32}\.[A-Za-z0-9]{16}\b",                  # Zhipu API keys
)]


def redact(obj: Any) -> Any:
    """Applied to everything reharness writes: events, transcripts, run records."""
    if isinstance(obj, str):
        for rx in _SECRETS:
            obj = rx.sub("[REDACTED]", obj)
        return obj
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000):x}{uuid.uuid4().hex[:6]}"


_APPEND = threading.Lock()     # concurrent attempts append from several threads; a line is written whole


def append_jsonl(path: Path, obj: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _APPEND, open(path, "a", encoding="utf-8") as f:
        f.write(line)


def read_jsonl(path: Path) -> Iterator[dict]:
    path = Path(path)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


@contextmanager
def locked(path: Path):
    """An exclusive lock on `path` (through a .lock file beside it), shared by every process that writes it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f"{path.name}.lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def write_json(path: Path, obj: Any) -> None:
    """Replace a JSON file in one step: a reader sees the old content or the new, never half of it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def load_dotenv(path: str | Path = ".env") -> None:
    """Read KEY=VALUE lines into the environment without overriding variables that are already set. The CLIs call it
    for the operator's .env; nothing else reads that file."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def import_object(ref: str):
    """"package.module:name" -> the object."""
    module, _, attr = ref.partition(":")
    return getattr(importlib.import_module(module), attr)


def file_key(path: Path) -> tuple | None:
    """Changes whenever the file is replaced or rewritten; None when there is none."""
    try:
        st = Path(path).stat()
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


def durable_write(path: Path, data: bytes) -> None:
    """Write through a temporary file in the same directory, fsync it, rename, then fsync the directory: a reader sees
    the whole file or no file, and a crash after the rename cannot lose it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:                          # not every filesystem allows it; the rename is still atomic
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------- the stores
class Objects:
    """The immutable object store: <workspace>/rsi/objects/<kind>/<digest>.json. Writing the same object twice is a
    no-op, so a retry after a crash produces the same ref and no second copy."""

    def __init__(self, root: str | Path):
        self.dir = Path(root) / "rsi" / "objects"

    def path(self, ref: str) -> Path:
        kind, d = parse_ref(ref)
        return self.dir / kind / f"{d}.json"

    def put(self, record_or_kind, payload: Any = None) -> str:
        """put(record) or put(kind, payload)."""
        if isinstance(record_or_kind, Record):
            return self.put_json(record_or_kind.TYPE, record_or_kind.payload())
        return self.put_json(record_or_kind, payload)

    def put_json(self, kind: str, payload: Any) -> str:
        ref = ref_for(kind, payload)
        path = self.path(ref)
        if not path.exists():
            durable_write(path, canonical(payload))
        return ref

    def has(self, ref: str) -> bool:
        return self.path(ref).exists()

    def get(self, ref: str) -> dict:
        path = self.path(ref)
        if not path.exists():
            raise KeyError(f"no object {short(ref)} in {self.dir}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if ref_for(parse_ref(ref)[0], payload) != ref:
            raise ContractError(f"{short(ref)} does not hash to its contents")
        return payload

    def load(self, ref: str, cls: type | None = None):
        kind, _ = parse_ref(ref)
        cls = cls or TYPES.get(kind)
        if cls is None:
            raise ContractError(f"no record type for {kind!r}")
        if cls.TYPE != kind:
            raise ContractError(f"{short(ref)} is a {kind}, not a {cls.TYPE}")
        return cls.from_json(self.get(ref))

    def refs(self, kind: str) -> list[str]:
        return sorted(f"{kind}:sha256:{p.stem}" for p in (self.dir / kind).glob("*.json"))


class Events:
    """<workspace>/rsi/events.jsonl: what happened, in order, referring to objects rather than repeating them."""

    def __init__(self, root: str | Path):
        self.path = Path(root) / "rsi" / "events.jsonl"

    def append(self, kind: str, **fields) -> dict:
        event = redact({"ts": now(), "event": kind, **fields})
        append_jsonl(self.path, event)
        return event

    def read(self, kind: str | None = None) -> Iterator[dict]:
        for e in read_jsonl(self.path):
            if kind is None or e.get("event") == kind:
                yield e

    def all(self) -> list[dict]:
        return list(self.read())


def refs_in(payload: Any) -> set[str]:
    """Every content reference inside a payload, at any depth. A record embedded whole by its to_json() carries its own
    identity under "ref"; that names the embedded copy itself, not another object, so it is not a reference."""
    out: set[str] = set()
    if isinstance(payload, str):
        if REF.fullmatch(payload):
            out.add(payload)
    elif isinstance(payload, dict):
        own = _own_ref(payload)
        for key, value in payload.items():
            if key != "ref" or own is None:
                out |= refs_in(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            out |= refs_in(value)
    return out


def _own_ref(d: dict) -> str | None:
    ref = d.get("ref")
    if not isinstance(ref, str) or not REF.fullmatch(ref):
        return None
    body = {k: v for k, v in d.items() if k != "ref" and k not in VOLATILE and v is not None}
    try:
        return ref if ref_for(parse_ref(ref)[0], body) == ref else None
    except ContractError:
        return None


def copy_closure(source: Objects, target: Objects, refs) -> set[str]:
    """Copy these objects and everything they refer to into another store. -> the refs copied. The same object has the
    same ref in both stores."""
    seen: set[str] = set()
    frontier = [r for r in refs if r]
    while frontier:
        ref = frontier.pop()
        if ref in seen:
            continue
        seen.add(ref)
        payload = source.get(ref)
        copied = target.put_json(parse_ref(ref)[0], payload)
        if copied != ref:
            raise ContractError(f"{short(ref)} changed identity when copied: {short(copied)}")
        frontier.extend(r for r in refs_in(payload) if r not in seen)
    return seen
