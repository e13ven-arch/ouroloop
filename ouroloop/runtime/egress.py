"""Egress for workers whose K says `network = {mode: "proxy", allow: [...]}` (docs/design/RUNTIME.md §2, §6).

A worker on the proxy network has no route anywhere but the proxy: each session (one attempt, or one read-only
smoke, check, script run or replay) gets its own Docker network, created `--internal` with a /29 of its own, on
which the only other member is the egress proxy (mitmproxy, unmodified, with ouroloop/runtime/egress_addon.py), reachable by
the name `egress`. So two workers never share a network: concurrent attempts, and a read-only session beside a
read-write one, cannot reach each other directly — every byte a worker sends leaves through the proxy, which
decides it by an allowlist of exact host:port entries with their methods and records it. (A network shared by an
activity's workers would let them talk to each other, unrecorded and around the allowlist and the per-attempt
credentials; this is the change to §4.4's "one network per activity".) The proxy is also on the uplink network (the
default bridge), so it is the one way out. Each session's credential is registered when it opens and revoked when
it closes; a read-only credential may only GET and HEAD and never tunnel. What went out comes back to the host as
rows, one per request or tunnel, for the attempt's transcript.

    pool = EgressPool([{"host": "api.example.com", "port": 443, "methods": ["CONNECT"]}], scope_id).start()
    egress = pool.session(attempt_id, max_requests=200)   # .network (the worker's --network) and .env (its proxy
    ...                                          # variables); the worker runs with them (DockerWorker.start(egress=))
    rows = egress.poll(max_requests=150)         # the rows since the last poll; egress.used is what went out so far
    events = egress.close()                      # the rows not polled yet; the credential and network gone
    pool.stop()

A session may carry a request cap (`max_requests`, protocol 2 of the addon): every request or tunnel the policy lets
out counts one, one past the cap is refused 429 `request_cap`, and poll() moves the cap — the runner shell sets it to
the attempt's tool calls left, so a proxied call counts in max_tool_calls and the limit holds inside the proxy, between
the host's polls (runtime/runner.py). A pool refuses a proxy whose addon does not speak this protocol.

`start()` runs the proxy container on the uplink network with its control port published to 127.0.0.1 only and
waits until the addon answers on the control port with the digest of the policy it was given — so a proxy whose
addon did not load (mitmproxy would then forward everything) is never handed to a worker. The addon binds its
control listener to the uplink address only: a worker, on a session network, cannot reach it at all. The container
runs read-only, as nobody, without capabilities, with CPU, memory and PID limits. `session()` creates the session's
network (a free /29 of `subnets`, so the number of sessions is not bound by Docker's default address pools, about 30
networks), attaches the proxy to it under the alias `egress`, and registers the credential; `close()` revokes the
credential, drains its rows, then disconnects whatever is left on the network and removes it.

One proxy per scope, and the container name (`ouroloop-egress-<scope8>`) is the lock: the container carries its
owner (host, pid, a nonce) in its labels, and `start()` never removes a live owner's proxy — a second process on the
same scope (a smoke run during a cycle, a re-run while one is live) is refused; a stale one (not running, or its
owner process gone on this host) is removed with its session networks and started again. Credentials are 256-bit
random tokens; the proxy holds their sha256 only, and a row names a credential by `token_id` (the digest's first 12
hex digits). Nothing here imports mitmproxy: the host speaks to the proxy over its control port.

Rows come back without the transcript line's own keys (records.RESERVED_KEYS: kind, ts, seq, source), so the
attempt's `_emit("egress", **row)` writes them as they are: the proxy's clock is `proxy_ts`, and rows the proxy had
to drop (over `max_rows` for one credential) are one row `{"dropped": n}`.
"""
from __future__ import annotations

import atexit
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path

from .records import RESERVED_KEYS

# v6.1: the addon with the request cap and `poll` (control protocol 2); `ouroloop-egress:v6` holds the first addon
DEFAULT_IMAGE = "ouroloop-egress:v6.1"
PROTOCOL = 2                           # the addon's control protocol this host needs
ALIAS = "egress"                       # the proxy's name on each session network
PROXY_PORT = 3128
CONTROL_PORT = 3129
USER = "attempt"
NO_PROXY = "localhost,127.0.0.1,::1"
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT")
HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$|^\[?[0-9A-Fa-f:.]+\]?$")
DRAIN_SECONDS = 5.0                    # how long close() waits for the credential's requests in flight to end
CONTROL_LINE = 64 * 1024 * 1024
SUBNETS = "10.213.0.0/16"              # where session networks take their /29 from (8,192 of them)
SESSION_PREFIX = 29                    # a /29: the bridge's gateway, the proxy, the worker, and room to spare
MAX_ROWS = 10_000                      # rows the proxy keeps per credential until drained; beyond, counted dropped
LABEL = "ouroloop.egress"


class EgressError(RuntimeError):
    """The egress proxy cannot be started or asked: no docker, no image, a proxy whose addon is not enforcing the
    policy it was given, a scope whose proxy another live process owns, a control request refused."""


# ---------------------------------------------------------------------------- the policy (the addon reads the same)
def parse_policy(policy) -> dict:
    """[{host, port, methods}] or K's {"mode": "proxy", "allow": [...]} -> {(host, port): {METHODS}}. Hosts are
    matched exactly (lower-cased), never by wildcard or suffix. The addon parses the policy by the same rules; the
    pool checks that both arrive at the same digest before it hands out a session."""
    if isinstance(policy, dict):
        if policy.get("mode", "proxy") != "proxy":
            raise ValueError(f"an egress pool serves network mode 'proxy', not {policy.get('mode')!r}")
        policy = policy.get("allow")
    if not isinstance(policy, list):
        raise ValueError("the egress policy is a list of {host, port, methods}")
    out: dict = {}
    for i, entry in enumerate(policy):
        if not isinstance(entry, dict) or set(entry) - {"host", "port", "methods"}:
            raise ValueError(f"policy[{i}] is an object with host, port and methods only")
        host, port, methods = entry.get("host"), entry.get("port"), entry.get("methods")
        if not isinstance(host, str) or not HOST.match(host):
            raise ValueError(f"policy[{i}].host {host!r} is not a host name or address")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            raise ValueError(f"policy[{i}].port {port!r} is not a port")
        if not isinstance(methods, list) or not methods or not all(isinstance(m, str) for m in methods):
            raise ValueError(f"policy[{i}].methods is a non-empty list of method names")
        upper = {m.upper() for m in methods}
        unknown = sorted(upper - set(METHODS))
        if unknown:
            raise ValueError(f"policy[{i}].methods {unknown} are not among {list(METHODS)}")
        out.setdefault((host.strip("[]").lower(), port), set()).update(upper)
    return out


def policy_entries(policy) -> list[dict]:
    """The policy in its canonical form: one entry per host:port, methods sorted."""
    return [{"host": host, "port": port, "methods": sorted(methods)}
            for (host, port), methods in sorted(parse_policy(policy).items())]


def policy_sha256(policy) -> str:
    canonical = sorted([host, port, sorted(methods)] for (host, port), methods in parse_policy(policy).items())
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode("utf-8")).hexdigest()


def token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def transcript_row(row: dict) -> dict:
    """A proxy row as an `egress` event's data: the record's own keys (kind, ts, source, mark) are not the row's to
    set — the proxy's clock goes under `proxy_ts`."""
    out = {k: v for k, v in row.items() if k not in RESERVED_KEYS}
    if "ts" in row:
        out["proxy_ts"] = row["ts"]
    return out


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                      # another user's process: alive
    return True


# ---------------------------------------------------------------------------- one session's credential and network
class EgressSession:
    """What a worker needs to go out through the proxy: `network` (its own internal network, the worker's only
    one) and `env` (HTTP_PROXY / HTTPS_PROXY with this session's credential, NO_PROXY, OUROLOOP_EGRESS=1). poll()
    takes the rows of the requests that ended since the last poll and moves the request cap; close() revokes the
    credential, returns the rows not polled yet and removes the network — poll and close between them return every
    row once — and a request with it afterwards is answered 407. `used` is how many requests and tunnels the policy
    let out with this credential, as the proxy last said."""

    def __init__(self, pool: "EgressPool", attempt_id: str, token: str, *, network: str, subnet: str,
                 read_only: bool):
        self._pool = pool
        self.attempt_id = attempt_id
        self.read_only = bool(read_only)
        self.token_id = token_id(token)
        self.network = network
        self.subnet = subnet
        url = f"http://{USER}:{token}@{ALIAS}:{PROXY_PORT}"
        # both spellings: Python reads either, curl and many libraries only the lower-case ones
        self.env = {"HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url,
                    "NO_PROXY": NO_PROXY, "no_proxy": NO_PROXY, "OUROLOOP_EGRESS": "1"}
        self._lock = threading.Lock()
        self._events: list[dict] | None = None
        self.used = 0

    def __repr__(self) -> str:                   # never the credential
        return (f"EgressSession(attempt_id={self.attempt_id!r}, token_id={self.token_id!r}, "
                f"read_only={self.read_only}, network={self.network!r})")

    @property
    def closed(self) -> bool:
        return self._events is not None

    def _rows(self, reply: dict) -> list[dict]:
        events = [{**transcript_row(row), "attempt_id": self.attempt_id, "read_only": self.read_only}
                  for row in reply.get("rows") or ()]
        if reply.get("dropped"):
            events.append({"token_id": self.token_id, "attempt_id": self.attempt_id,
                           "read_only": self.read_only, "dropped": int(reply["dropped"])})
        if isinstance(reply.get("used"), int):
            self.used = int(reply["used"])
        return events

    def poll(self, **cap) -> list[dict]:
        """The rows of this credential's requests and tunnels that ended since the last poll (a tunnel still open is
        not among them: its row comes when it ends, or with close()), each as close() returns it; `used` updated.
        With `max_requests=n` (None: no cap) the proxy's cap for this credential moves to n. After close(), []."""
        if set(cap) - {"max_requests"}:
            raise TypeError(f"poll() takes max_requests only, not {sorted(set(cap) - {'max_requests'})}")
        with self._lock:
            if self._events is not None:
                return []
            return self._rows(self._pool._control({"op": "poll", "token_id": self.token_id, **cap}))

    def close(self, wait: float = DRAIN_SECONDS) -> list[dict]:
        """Revoke the credential (no new request passes), wait up to `wait` seconds for its requests in flight,
        take all its rows — each with the attempt id and the credential's read-only flag, without the record's
        own keys (transcript_row) — then cut and remove the network. Idempotent. The rows are kept even when the
        network cannot be removed now (stop() removes it then)."""
        with self._lock:
            if self._events is not None:
                return list(self._events)
            self._pool._control({"op": "revoke", "token_id": self.token_id})
            reply = self._pool._control({"op": "drain", "token_id": self.token_id, "wait": float(wait)},
                                        timeout=float(wait) + 30)
            events = self._rows(reply)
            self._events = events
            self._pool._forget(self)
            try:
                self._pool._remove_network(self.network)
            except EgressError:
                pass                             # left for stop(): every network of this pool carries its owner
            return list(events)


# ---------------------------------------------------------------------------- the proxy
class EgressPool:
    """One egress proxy for one activity scope, and a network per session. `policy` is K's allowlist; `uplink` the
    network the proxy reaches the outside through (Docker's default bridge); `subnets` the IPv4 range session
    networks take their /29 from; `max_rows` the rows the proxy keeps per credential between drains; `log_path`,
    when given, receives the proxy's full audit log (its JSON rows) at stop()."""

    def __init__(self, policy, scope_id: str, *, image: str = DEFAULT_IMAGE, uplink: str = "bridge",
                 docker: str = "docker", log_path=None, cpus: str = "1", memory: str = "512m", pids: int = 256,
                 start_seconds: float = 60.0, subnets: str = SUBNETS, max_rows: int = MAX_ROWS):
        if not isinstance(scope_id, str) or not scope_id:
            raise ValueError("scope_id is a non-empty string")
        self.policy = policy_entries(policy)
        if not self.policy:
            raise ValueError("an egress policy allows at least one host:port (network mode 'none' needs no pool)")
        self.policy_digest = policy_sha256(self.policy)
        self.scope_id = scope_id
        self.scope8 = hashlib.sha256(scope_id.encode("utf-8")).hexdigest()[:8]
        self.container = f"ouroloop-egress-{self.scope8}"
        self.image, self.uplink, self.docker = image, uplink, docker
        self.log_path = Path(log_path) if log_path else None
        self.cpus, self.memory, self.pids = str(cpus), str(memory), int(pids)
        self.start_seconds = float(start_seconds)
        self.subnets = ipaddress.ip_network(subnets)
        if self.subnets.version != 4 or self.subnets.prefixlen > SESSION_PREFIX:
            raise ValueError(f"subnets {subnets!r} is an IPv4 range of a /{SESSION_PREFIX} at least")
        if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1:
            raise ValueError("max_rows is a positive integer")
        self.max_rows = max_rows
        self.owner = {"host": socket.gethostname(), "pid": os.getpid(), "nonce": secrets.token_hex(8)}
        self.image_id: str | None = None
        self.mitmproxy: str | None = None
        self.control_bind: str | None = None
        self._key = secrets.token_urlsafe(32)
        self._port: int | None = None
        self._lock = threading.Lock()
        self._sessions: dict[str, EgressSession] = {}

    def __repr__(self) -> str:
        return f"EgressPool(scope_id={self.scope_id!r}, container={self.container!r}, started={self._port is not None})"

    @property
    def started(self) -> bool:
        return self._port is not None

    # ------------------------------------------------------------------ docker
    def _run_docker(self, *args, env: dict | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self.docker, *args], capture_output=True, text=True, timeout=timeout,
                                  env={**os.environ, **(env or {})})
        except FileNotFoundError:
            raise EgressError(f"the docker CLI {self.docker!r} is not installed") from None
        except subprocess.TimeoutExpired:
            raise EgressError(f"docker {args[0]} did not finish within {timeout:.0f}s") from None

    def _docker(self, *args, check: bool = True, env: dict | None = None, timeout: float = 120) -> str:
        done = self._run_docker(*args, env=env, timeout=timeout)
        if check and done.returncode != 0:
            raise EgressError(f"docker {' '.join(args[:2])} failed: {(done.stderr or done.stdout).strip()[-500:]}")
        return done.stdout

    def _labels(self) -> list[str]:
        return [x for k, v in (("scope", self.scope8), ("host", self.owner["host"]), ("pid", str(self.owner["pid"])),
                               ("owner", self.owner["nonce"])) for x in ("--label", f"{LABEL}.{k}={v}")]

    def _remove_network(self, network: str) -> None:
        """A session network: whatever is still attached is cut off (never left on a live network), then it goes."""
        members = self._run_docker("network", "inspect", network, "--format",
                                   "{{range .Containers}}{{.Name}} {{end}}")
        if members.returncode != 0:
            return                               # already gone
        for member in members.stdout.split():
            self._docker("network", "disconnect", "-f", network, member, check=False)
        self._docker("network", "rm", network)

    def _owned_networks(self, nonce: str) -> list[str]:
        return self._docker("network", "ls", "--filter", f"label={LABEL}.owner={nonce}", "--format", "{{.Name}}",
                            check=False).split()

    def _remove_own(self) -> None:
        """This pool's proxy (only when the container under the scope's name carries this pool's owner label: the
        name may be a live owner's) and every session network it made (by the same label)."""
        holder = self._holder()
        if holder is not None and holder["labels"].get(f"{LABEL}.owner") == self.owner["nonce"]:
            self._docker("rm", "-f", self.container, check=False)
        for network in self._owned_networks(self.owner["nonce"]):
            try:
                self._remove_network(network)
            except EgressError:
                pass

    # ------------------------------------------------------------------ the scope's lock: the container name
    def _holder(self) -> dict | None:
        """The container holding this scope's name now: {running, labels}, or None."""
        done = self._run_docker("inspect", "--type", "container", "--format",
                                "{{json .State.Running}} {{json .Config.Labels}}", self.container)
        if done.returncode != 0:
            return None
        running, _, labels = done.stdout.strip().partition(" ")
        return {"running": running == "true", "labels": json.loads(labels or "null") or {}}

    def _reclaim(self) -> None:
        """The scope's name is taken: remove the holder if it is stale, else refuse."""
        holder = self._holder()
        if holder is None:
            return                               # gone in the meantime
        labels = holder["labels"]
        host, pid, nonce = labels.get(f"{LABEL}.host"), labels.get(f"{LABEL}.pid"), labels.get(f"{LABEL}.owner")
        stale = not holder["running"]
        if not stale and host == self.owner["host"] and pid and pid.isdigit():
            stale = not _pid_alive(int(pid))
        if not stale:
            who = f"pid {pid} on {host}" if pid else "an owner it does not name"
            raise EgressError(f"the egress proxy of this scope ({self.container}) is in use by {who}: one proxy "
                              f"per scope. Stop that run first; if it is gone, `docker rm -f {self.container}`")
        self._docker("rm", "-f", self.container, check=False)
        for network in self._owned_networks(nonce) if nonce else ():
            try:
                self._remove_network(network)
            except EgressError:
                pass

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "EgressPool":
        if self._port is not None:
            return self
        self.image_id = self._docker("image", "inspect", "--format", "{{.Id}}", self.image, check=False).strip() or None
        if not self.image_id:
            raise EgressError(f"the egress image {self.image} is not built "
                              f"(docker build -f docker/egress.Dockerfile -t {self.image} .)")
        env = {"OUROLOOP_EGRESS_POLICY": json.dumps(self.policy), "OUROLOOP_EGRESS_CONTROL_KEY": self._key,
               "OUROLOOP_EGRESS_CONTROL_PORT": str(CONTROL_PORT), "OUROLOOP_EGRESS_MAX_ROWS": str(self.max_rows)}
        # The values travel in docker's own environment (-e NAME), never on its command line.
        run = ("run", "-d", "--name", self.container, "--network", self.uplink,
               "-p", f"127.0.0.1::{CONTROL_PORT}", "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534",
               "--cpus", self.cpus, "--memory", self.memory, "--memory-swap", self.memory,
               "--pids-limit", str(self.pids), *self._labels(), *[x for name in env for x in ("-e", name)],
               self.image_id)
        try:
            for attempt in range(3):
                done = self._run_docker(*run, env=env)
                if done.returncode == 0:
                    break
                if "already in use" not in (done.stderr or "") or attempt == 2:
                    raise EgressError(f"docker run failed: {(done.stderr or done.stdout).strip()[-500:]}")
                self._reclaim()                  # raises when a live owner holds the scope
            published = self._docker("port", self.container, f"{CONTROL_PORT}/tcp").split()
            address = next((p for p in published if p.startswith("127.0.0.1:")), None)
            if address is None:
                raise EgressError(f"the control port is not published on 127.0.0.1 only: {published}")
            self._port = int(address.rsplit(":", 1)[1])
            self._await_ready()
        except BaseException:
            self._port = None
            self._remove_own()                   # ours only: never a live owner's proxy
            raise
        return self

    def _await_ready(self) -> None:
        deadline = time.monotonic() + self.start_seconds
        last = "no answer"
        while time.monotonic() < deadline:
            state = self._docker("inspect", "--format", "{{.State.Running}}", self.container, check=False).strip()
            if state != "true":
                logs = self._docker("logs", "--tail", "20", self.container, check=False)
                raise EgressError(f"the egress proxy exited at start: {logs.strip()[-800:]}")
            try:
                reply = self._control({"op": "ping"}, timeout=5)
            except (OSError, EgressError, ValueError) as e:
                last = str(e)
                time.sleep(0.2)
                continue
            if reply.get("broken"):
                raise EgressError(f"the egress addon refuses to serve: {reply['broken']}")
            if reply.get("policy_sha256") != self.policy_digest:
                raise EgressError("the egress proxy enforces another policy than the one it was given "
                                  f"({reply.get('policy_sha256')} != {self.policy_digest})")
            if not isinstance(reply.get("protocol"), int) or reply["protocol"] < PROTOCOL:
                # an addon without the request cap would let an attempt's proxied calls past max_tool_calls
                raise EgressError(f"the egress proxy's addon speaks control protocol {reply.get('protocol')}, and "
                                  f"this host needs {PROTOCOL} (the request cap): rebuild the image "
                                  f"(docker build -f docker/egress.Dockerfile -t {self.image} .)")
            self.mitmproxy = reply.get("mitmproxy")
            self.control_bind = reply.get("control_bind")
            return
        raise EgressError(f"the egress proxy did not answer on its control port within {self.start_seconds:.0f}s "
                          f"({last})")

    def stop(self) -> list[dict]:
        """Close the sessions still open (their rows are returned, so none is lost), keep the audit log if asked,
        remove the proxy and every session network. Idempotent."""
        events: list[dict] = []
        with self._lock:
            open_sessions = list(self._sessions.values())
        if self._port is not None:
            for session in open_sessions:
                try:
                    events += session.close(wait=1.0)
                except EgressError:
                    pass
            if self.log_path is not None:
                done = subprocess.run([self.docker, "logs", self.container], capture_output=True, text=True,
                                      timeout=60)
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                rows = [line for line in done.stdout.splitlines() if line.startswith("{")]
                self.log_path.write_text("".join(row + "\n" for row in rows), encoding="utf-8")
            self._port = None
            self._remove_own()
        return events

    def __enter__(self) -> "EgressPool":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def describe(self) -> dict:
        return {"image": self.image, "image_id": self.image_id, "mitmproxy": self.mitmproxy,
                "container": self.container, "alias": ALIAS, "uplink": self.uplink, "subnets": str(self.subnets),
                "session_prefix": SESSION_PREFIX, "max_rows": self.max_rows,
                "policy": self.policy, "policy_sha256": self.policy_digest,
                "control": f"127.0.0.1:{self._port}" if self._port is not None else None,
                "owner": {"host": self.owner["host"], "pid": self.owner["pid"]}}

    # ------------------------------------------------------------------ sessions
    def _create_network(self, name: str) -> str:
        """An internal network with a free /29 of `subnets`: tried from a random start, the next one on an overlap
        (another session's, another pool's, anything else on the host). -> the subnet."""
        size = 1 << (SESSION_PREFIX - self.subnets.prefixlen)
        start = secrets.randbelow(size)
        step = 1 << (32 - SESSION_PREFIX)
        tried = 0
        for i in range(size):
            subnet = ipaddress.ip_network((int(self.subnets.network_address) + ((start + i) % size) * step,
                                           SESSION_PREFIX))
            done = self._run_docker("network", "create", "--internal", "--subnet", str(subnet), *self._labels(),
                                    name)
            if done.returncode == 0:
                return str(subnet)
            if "overlaps" not in (done.stderr or ""):
                raise EgressError(f"docker network create failed: {(done.stderr or done.stdout).strip()[-500:]}")
            tried += 1
            if tried >= 256:
                break
        raise EgressError(f"no free /{SESSION_PREFIX} in {self.subnets} after {tried} tries (pass another `subnets`)")

    def session(self, attempt_id: str, *, read_only: bool = False, max_requests: int | None = None) -> EgressSession:
        """A fresh credential and network for one session (read-only for a smoke, a check, a script run, a
        replay), capped at `max_requests` requests and tunnels let out (None: no cap)."""
        if self._port is None:
            raise EgressError("the egress pool is not started")
        if max_requests is not None and (isinstance(max_requests, bool) or not isinstance(max_requests, int)
                                         or max_requests < 0):
            raise ValueError("max_requests is a count of requests, 0 or more, or None")
        token = secrets.token_urlsafe(32)
        network = f"{self.container}-{token_id(token)}"
        subnet = self._create_network(network)
        try:
            self._docker("network", "connect", "--alias", ALIAS, network, self.container)
            self._control({"op": "register", "digest": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                           "read_only": bool(read_only), "max_requests": max_requests})
        except BaseException:
            try:
                self._remove_network(network)
            except EgressError:
                pass
            raise
        session = EgressSession(self, str(attempt_id), token, network=network, subnet=subnet, read_only=read_only)
        with self._lock:
            self._sessions[session.token_id] = session
        return session

    def _forget(self, session: EgressSession) -> None:
        with self._lock:
            self._sessions.pop(session.token_id, None)

    def _control(self, request: dict, timeout: float = 30) -> dict:
        if self._port is None:
            raise EgressError("the egress pool is not started")
        try:
            with socket.create_connection(("127.0.0.1", self._port), timeout=timeout) as s:
                s.sendall((json.dumps({"key": self._key, **request}) + "\n").encode("utf-8"))
                line = s.makefile("rb").readline(CONTROL_LINE)
        except OSError as e:
            raise EgressError(f"the egress control port did not answer {request.get('op')}: {e}") from None
        if not line:
            raise EgressError(f"the egress proxy closed the control connection on {request.get('op')}")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise EgressError(f"egress control {request.get('op')}: {reply.get('error')}")
        return reply


# ---------------------------------------------------------------------------- one pool per activity in a process
class LazyPool:
    """The pool an activity's workers share in one process: started at its first session (a phase that runs no
    proxied attempt or check never starts a proxy), stopped by stop_shared() at the end of the phase and at exit —
    so no proxy container or session network outlives the process that started it (a killed process leaves one,
    which the next start on the scope reclaims as stale)."""

    def __init__(self, policy, scope_id: str, *, image: str = DEFAULT_IMAGE):
        self.pool = EgressPool(policy, scope_id, image=image)
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"LazyPool({self.pool!r})"

    def session(self, attempt_id: str, *, read_only: bool = False, max_requests: int | None = None) -> EgressSession:
        with self._lock:
            if not self.pool.started:
                self.pool.start()
        return self.pool.session(attempt_id, read_only=read_only, max_requests=max_requests)

    def stop(self) -> list[dict]:
        with self._lock:
            return self.pool.stop() if self.pool.started else []

    def describe(self) -> dict:
        return self.pool.describe()


_SHARED: dict = {}
_SHARED_LOCK = threading.Lock()


def shared_pool(policy, scope_id: str, *, image: str = DEFAULT_IMAGE) -> LazyPool:
    """The process's one pool for a scope, policy and image: every worker of an activity (its attempts, its draft
    checks) opens its sessions from the same proxy, as one proxy per scope requires."""
    key = (scope_id, policy_sha256(policy), image)
    with _SHARED_LOCK:
        pool = _SHARED.get(key)
        if pool is None:
            pool = _SHARED[key] = LazyPool(policy, scope_id, image=image)
        return pool


def stop_shared() -> list[dict]:
    """Stop every shared pool of this process (bench.run_phase calls it when a phase ends): the rows of sessions still
    open are returned, the proxies and session networks removed."""
    with _SHARED_LOCK:
        pools = list(_SHARED.values())
        _SHARED.clear()
    rows: list[dict] = []
    for pool in pools:
        try:
            rows += pool.stop()
        except EgressError:
            pass                                 # a proxy that cannot be stopped now is reclaimed as stale later
    return rows


atexit.register(stop_shared)
