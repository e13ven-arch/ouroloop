"""Egress through the proxy (docs/design/RUNTIME.md §2, §6, §8; runtime/egress.py, v6-gateway's unchanged), all of it
real: Docker's internal networks (one per session), the `ouroloop-egress:v6.1` proxy container (mitmproxy 12.2.3
unmodified + ouroloop/runtime/egress_addon.py), a client container on its session's network only that goes out with
Python's urllib (or a raw socket) and nothing but the session's environment, and two target containers on the default
bridge only — a plain HTTP server and a TLS server, both the stdlib's. Then the platform's side: the addon's request
cap, the process's shared pools, and the stirrup seed's attempt going out through its own capped credential.

The Docker tests are skipped without docker or the images, and fail instead under OUROLOOP_TEST_REQUIRE_DOCKER=1.
Build the proxy first: docker build --provenance=false -f docker/egress.Dockerfile -t ouroloop-egress:v6.1 .
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import textwrap
import uuid

import pytest

from ouroloop.runtime import egress as E
from ouroloop.runtime.egress import DEFAULT_IMAGE, EgressError, EgressPool, parse_policy, policy_sha256
from ouroloop.runtime.records import RESERVED_KEYS, Transcript, read
from runtime_world import docker, docker_running, image_present, require

BASE = "python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f"

TARGET = r'''
import http.server, json, os, ssl, sys

class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, body: bytes, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/hello":
            return self._send(b'{"hello": "world"}')
        if self.path == "/slow":
            import time
            time.sleep(2.5)
            return self._send(b'{"slow": true}')
        # what reached the target: the path and every header (the proxy credential must not be among them)
        self._send(json.dumps({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}).encode())

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        import time
        headers_at = time.time()             # when the request's head reached the target (before its body)
        size = int(self.headers.get("Content-Length") or 0)
        got = 0
        while got < size:
            chunk = self.rfile.read(min(size - got, 1 << 20))
            if not chunk:
                break
            got += len(chunk)
        self._send(json.dumps({"posted": got, "headers_at": headers_at}).encode())

    def log_message(self, *args):
        pass

server = http.server.ThreadingHTTPServer(("0.0.0.0", int(sys.argv[2])), Handler)
if sys.argv[1] == "tls":
    for name in ("TLS_CERT", "TLS_KEY"):
        with open(f"/tmp/{name}.pem", "w") as f:
            f.write(os.environ[name])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/tmp/TLS_CERT.pem", "/tmp/TLS_KEY.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
print("ready", flush=True)
server.serve_forever()
'''

CLIENT = r'''
import base64, json, os, re, socket, ssl, sys, time, urllib.error, urllib.request

plain, tls, other = os.environ["PLAIN"], os.environ["TLS"], os.environ["OTHER"]
with open("/tmp/ca.pem", "w") as f:
    f.write(os.environ["TLS_CERT"])
context = ssl.create_default_context(cafile="/tmp/ca.pem")
context.check_hostname = False             # the target is addressed by IP; the chain is still verified

def call(url, method="GET", data=None, direct=False):
    handlers = [urllib.request.ProxyHandler({})] if direct else []    # direct: no proxy at all
    opener = urllib.request.build_opener(*handlers, urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(url, data=data, method=method)
    try:
        with opener.open(request, timeout=8) as r:
            return {"status": r.status, "body": r.read().decode()}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode(errors="replace")}
    except (urllib.error.URLError, OSError) as e:
        text = str(getattr(e, "reason", e))
        tunnel = re.search(r"Tunnel connection failed: (\d{3})", text)
        return {"status": int(tunnel.group(1)) if tunnel else None, "error": text}

def proxy_auth():
    creds = os.environ["HTTP_PROXY"].split("//", 1)[1].split("@", 1)[0]
    return "Basic " + base64.b64encode(creds.encode()).decode()

def raw_post(target, size=None, send=65536):
    """A POST through the proxy whose body is far longer than what is sent (or chunked, size None): a proxy that
    reads the body before it decides waits; one that decides on the headers closes the connection at once."""
    s = socket.create_connection(("egress", 3128), timeout=10)
    head = f"POST http://{target}/x HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: {proxy_auth()}\r\n"
    head += "Transfer-Encoding: chunked\r\n" if size is None else f"Content-Length: {size}\r\n"
    started = time.time()
    try:
        s.sendall((head + "\r\n").encode())
        body = b"x" * send
        s.sendall((f"{len(body):x}\r\n".encode() + body + b"\r\n") if size is None else body)
        s.settimeout(8)
        data = s.recv(65536)
        result = "closed" if not data else "answered:" + data.split(b"\r\n", 1)[0].decode()
    except socket.timeout:
        result = "waiting"
    except (ConnectionResetError, BrokenPipeError):
        result = "closed"
    finally:
        s.close()
    return {"result": result, "seconds": round(time.time() - started, 2)}

def stream_upload(target, half):
    """Half the body, a pause, the other half: the target's clock says whether it had the request's head before
    the second half was sent (streamed through) or only after (buffered at the proxy)."""
    s = socket.create_connection(("egress", 3128), timeout=30)
    s.sendall((f"POST http://{target}/upload HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: {proxy_auth()}"
               f"\r\nContent-Length: {2 * half}\r\nConnection: close\r\n\r\n").encode() + b"y" * half)
    time.sleep(2.0)
    second = time.time()
    s.sendall(b"y" * half)
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    answer = json.loads(body)
    return {"status": int(head.split(b" ", 2)[1]), "posted": answer["posted"],
            "head_before_second_half": answer["headers_at"] < second}

out = {}
for name in sys.argv[1:]:
    if name == "get":
        out[name] = call(f"http://{plain}/hello")
    elif name == "echo":
        out[name] = call(f"http://{plain}/echo?x=1")
    elif name == "head":
        out[name] = call(f"http://{plain}/hello", method="HEAD")
    elif name == "other_host":
        out[name] = call(f"http://{other}/hello")
    elif name == "post":
        out[name] = call(f"http://{plain}/echo", method="POST", data=b"x" * 300)
    elif name == "tls":
        out[name] = call(f"https://{tls}/hello")
    elif name == "tls_on_plain_port":
        out[name] = call(f"https://{plain}/hello")
    elif name == "direct":
        out[name] = call(f"http://{plain}/hello", direct=True)
    elif name == "direct_tls":
        out[name] = call(f"https://{tls}/hello", direct=True)
    elif name == "direct_internet":
        import socket
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=3).close()
            out[name] = {"status": "connected"}
        except OSError as e:
            out[name] = {"status": None, "error": str(e)}
    elif name == "slow":
        out[name] = call(f"http://{plain}/slow")
    elif name == "peer":                   # another container, directly (a worker of another attempt)
        out[name] = call(f"http://{os.environ['PEER']}/", direct=True)
    elif name == "control":                # the proxy's control port, from the worker's side
        try:
            socket.create_connection(("egress", 3129), timeout=3).close()
            out[name] = {"status": "connected"}
        except OSError as e:
            out[name] = {"status": None, "error": str(e)}
    elif name == "big_refused":
        out[name] = raw_post(other, size=200 * 1024 * 1024)
    elif name == "chunked_refused":
        out[name] = raw_post(other)
    elif name == "stream_upload":
        out[name] = stream_upload(plain, 1_000_000)
    elif name == "dead_port":
        out[name] = call(f"https://{os.environ['DEAD']}/")
print(json.dumps(out))
'''

HOLD = r'''
import base64, os, socket, time
creds = os.environ["HTTP_PROXY"].split("//", 1)[1].split("@", 1)[0]
target = os.environ["TLS"]
s = socket.create_connection(("egress", 3128), timeout=10)
s.sendall((f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: Basic "
           f"{base64.b64encode(creds.encode()).decode()}\r\n\r\n").encode())
print("tunnel:" + s.recv(4096).split(b"\r\n", 1)[0].decode(), flush=True)
time.sleep(120)                            # the tunnel stays open
'''


@pytest.fixture(scope="module")
def images():
    require(docker_running(), "docker is not running")
    for image in (DEFAULT_IMAGE, BASE):
        require(image_present(image), f"the image {image} is not here (docker build -f docker/egress.Dockerfile -t "
                                      f"{DEFAULT_IMAGE} . ; docker pull {BASE})")


@pytest.fixture(scope="module")
def targets(images):
    """A self-signed certificate (made by the base image's openssl), and the two targets on the default bridge."""
    out = docker("run", "--rm", BASE, "sh", "-c",
                 "openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=ouroloop-egress-target "
                 "-keyout /tmp/k.pem -out /tmp/c.pem 2>/dev/null && cat /tmp/c.pem /tmp/k.pem")
    cert = out[:out.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")] + "\n"
    key = out[out.index("-----BEGIN PRIVATE KEY-----"):]
    run = uuid.uuid4().hex[:8]
    names = {"plain": f"ouroloop-egress-test-plain-{run}", "tls": f"ouroloop-egress-test-tls-{run}"}
    env = {"TLS_CERT": cert, "TLS_KEY": key}
    try:
        for kind, port in (("plain", 8080), ("tls", 8443)):
            docker("run", "-d", "--rm", "--name", names[kind], "--network", "bridge", "--user", "65534:65534",
                   "-e", "TLS_CERT", "-e", "TLS_KEY", BASE, "python", "-c", TARGET, kind, str(port), env=env)
        ips = {kind: docker("inspect", "--format", "{{.NetworkSettings.Networks.bridge.IPAddress}}", name).strip()
               for kind, name in names.items()}
        for name in names.values():
            for _ in range(100):
                if "ready" in docker("logs", name, check=False):
                    break
                import time
                time.sleep(0.1)
        yield {"plain": f"{ips['plain']}:8080", "tls": f"{ips['tls']}:8443", "ips": ips, "cert": cert}
    finally:
        for name in names.values():
            docker("rm", "-f", name, check=False)


def client(session, targets, *checks, **extra) -> dict:
    """The client container: on the session's internal network only, its environment the session's and nothing
    else of the host's; the values are handed to docker through its environment (-e NAME), not its command line."""
    env = {**session.env, "PLAIN": targets["plain"], "TLS": targets["tls"], "OTHER": f"{targets['ips']['tls']}:8080",
           "TLS_CERT": targets["cert"], **extra}
    args = [x for name in env for x in ("-e", name)]
    out = docker("run", "--rm", "--network", session.network, "--user", "65534:65534", *args, BASE, "python", "-c",
                 CLIENT, *checks, env=env, timeout=180)
    return json.loads(out.strip().splitlines()[-1])


@pytest.fixture
def pool(targets, tmp_path):
    plain_ip, tls_ip = targets["ips"]["plain"], targets["ips"]["tls"]
    policy = {"mode": "proxy", "allow": [{"host": plain_ip, "port": 8080, "methods": ["GET", "HEAD"]},
                                          {"host": tls_ip, "port": 8443, "methods": ["CONNECT"]}]}
    p = EgressPool(policy, f"test-egress-{uuid.uuid4().hex}", log_path=tmp_path / "egress.jsonl").start()
    yield p
    p.stop()


def by_path(rows, method, host_port, path=None):
    host, port = host_port.rsplit(":", 1)
    return [r for r in rows if r["method"] == method and r["host"] == host and r["port"] == int(port)
            and (path is None or r.get("path") == path)]


def test_the_proxy_lets_out_only_what_the_allowlist_names_and_records_every_request(pool, targets, tmp_path):
    rw = pool.session("run/t1/0")
    ro = pool.session("smoke/t1", read_only=True)
    assert rw.network != ro.network and rw.env["OUROLOOP_EGRESS"] == "1"
    token = rw.env["HTTP_PROXY"].split("attempt:", 1)[1].split("@", 1)[0]
    assert rw.token_id == hashlib.sha256(token.encode()).hexdigest()[:12]
    got = client(rw, targets, "get", "echo", "head", "other_host", "post", "tls", "tls_on_plain_port", "direct",
                 "direct_tls", "direct_internet")
    assert got["get"] == {"status": 200, "body": '{"hello": "world"}'}
    echoed = json.loads(got["echo"]["body"])
    assert got["echo"]["status"] == 200 and echoed["path"] == "/echo?x=1"
    assert "proxy-authorization" not in echoed["headers"]                      # the credential stayed at the proxy
    assert got["head"]["status"] == 200
    assert got["other_host"]["status"] == 403                                   # a host:port not on the list
    assert got["post"]["status"] == 403 and "method_not_allowed" in got["post"]["body"]
    assert got["tls"] == {"status": 200, "body": '{"hello": "world"}'}          # tunnelled; the chain verified
    assert got["tls_on_plain_port"]["status"] == 403                            # CONNECT not among its methods
    assert got["direct"]["status"] is None and got["direct_tls"]["status"] is None    # no route without the proxy
    assert got["direct_internet"]["status"] is None
    read_only = client(ro, targets, "get", "post", "tls")
    assert read_only["get"]["status"] == 200
    assert read_only["post"]["status"] == 403 and "read_only" in read_only["post"]["body"]
    assert read_only["tls"]["status"] == 403

    events = rw.close()
    assert {e["token_id"] for e in events} == {rw.token_id} and {e["attempt_id"] for e in events} == {"run/t1/0"}
    keys = {"proxy_ts", "token_id", "method", "host", "port", "status", "bytes_up", "bytes_down", "ms", "allowed",
            "reason", "attempt_id", "read_only"}
    assert all(keys <= set(e) for e in events)
    assert not any(set(RESERVED_KEYS) & set(e) for e in events)                    # the transcript line's own keys
    (get,) = by_path(events, "GET", targets["plain"], "/hello")
    assert get["status"] == 200 and get["allowed"] is True and get["bytes_down"] == len('{"hello": "world"}')
    (other,) = by_path(events, "GET", f"{targets['ips']['tls']}:8080")
    assert other["status"] == 403 and other["allowed"] is False and other["reason"] == "host_not_allowed"
    assert other["bytes_up"] == other["bytes_down"] == 0                        # nothing crossed to a target
    (post,) = by_path(events, "POST", targets["plain"])
    assert post["status"] == 403 and post["reason"] == "method_not_allowed" and post["path"] == "/echo"
    (tunnel,) = by_path(events, "CONNECT", targets["tls"])
    assert tunnel["status"] == 200 and tunnel["allowed"] is True and "path" not in tunnel
    assert tunnel["bytes_up"] > 200 and tunnel["bytes_down"] > 500              # a TLS handshake each way at least
    assert "error" not in tunnel and tunnel["reason"] is None                     # ended by the TCP layer, cleanly
    (wrong_port,) = by_path(events, "CONNECT", targets["plain"])
    assert wrong_port["status"] == 403 and wrong_port["reason"] == "method_not_allowed"
    assert len(events) == 7                                                       # direct attempts never reached it
    ro_events = ro.close()
    assert [(e["method"], e["status"], e.get("reason")) for e in ro_events] == [
        ("GET", 200, None), ("POST", 403, "read_only"), ("CONNECT", 403, "read_only")]
    assert all(e["read_only"] is True for e in ro_events)

    # after close the credential is revoked and its network is gone; the credential used from elsewhere (another
    # session's network) is 407, recorded under it but not handed out again
    assert docker("network", "ls", "--filter", f"name=^{rw.network}$", "--format", "{{.Name}}").strip() == ""
    elsewhere = pool.session("run/t1/elsewhere")
    after = client(Forged(elsewhere, rw.env["HTTP_PROXY"].split("attempt:", 1)[1].split("@", 1)[0]), targets,
                   "get", "tls")
    assert after["get"]["status"] == 407 and after["tls"]["status"] == 407
    assert "revoked" in after["get"]["body"]
    assert rw.close() == events
    assert elsewhere.close() == []
    # no credential anywhere in what comes back or what the proxy logs
    stopped = pool.stop()
    assert stopped == []
    text = json.dumps(events) + json.dumps(ro_events) + (tmp_path / "egress.jsonl").read_text()
    for session in (rw, ro):
        secret = session.env["HTTPS_PROXY"].split("attempt:", 1)[1].split("@", 1)[0]
        assert secret not in text and secret not in repr(session) and secret not in repr(pool)
    audit = [json.loads(line) for line in (tmp_path / "egress.jsonl").read_text().splitlines()]
    assert len(audit) == len(events) + len(ro_events) + 2                         # the two 407s are in the audit log
    assert [(r["status"], r["reason"]) for r in audit if r["token_id"] == rw.token_id][-2:] == [(407, "revoked")] * 2


def test_stop_returns_the_rows_of_sessions_left_open_and_removes_the_proxy_and_its_network(targets, tmp_path):
    p = EgressPool([{"host": targets["ips"]["plain"], "port": 8080, "methods": ["GET"]}],
                   f"test-egress-{uuid.uuid4().hex}").start()
    try:
        left_open = p.session("run/t2/0")
        assert client(left_open, targets, "get")["get"]["status"] == 200
        assert p.describe()["policy_sha256"] == policy_sha256(p.policy) and p.describe()["mitmproxy"] == "12.2.3"
    finally:
        rows = p.stop()
    assert [(r["method"], r["status"], r["attempt_id"]) for r in rows] == [("GET", 200, "run/t2/0")]
    assert docker("ps", "-a", "--filter", f"name=^{p.container}$", "--format", "{{.Names}}").strip() == ""
    assert docker("network", "ls", "--filter", f"name=^{left_open.network}$", "--format", "{{.Name}}").strip() == ""
    assert docker("network", "ls", "--filter", f"label=ouroloop.egress.owner={p.owner['nonce']}", "--format",
                  "{{.Name}}").strip() == ""
    with pytest.raises(EgressError):
        p.session("late")


def test_the_proxy_container_is_locked_down_and_its_control_port_is_only_on_the_hosts_loopback(pool):
    session = pool.session("run/lock/0")
    info = json.loads(docker("inspect", pool.container))[0]
    host = info["HostConfig"]
    assert host["ReadonlyRootfs"] is True and host["CapDrop"] == ["ALL"]
    assert "no-new-privileges" in host["SecurityOpt"] and info["Config"]["User"] == "65534:65534"
    assert host["PidsLimit"] == pool.pids and host["Memory"] == host["MemorySwap"] > 0
    bindings = host["PortBindings"]
    assert list(bindings) == ["3129/tcp"] and all(b["HostIp"] == "127.0.0.1" for b in bindings["3129/tcp"])
    networks = info["NetworkSettings"]["Networks"]
    assert set(networks) == {"bridge", session.network} and "egress" in (networks[session.network]["Aliases"] or [])
    internal = json.loads(docker("network", "inspect", session.network))[0]
    assert internal["Internal"] is True and internal["IPAM"]["Config"][0]["Subnet"] == session.subnet
    import ipaddress
    assert ipaddress.ip_network(session.subnet).subnet_of(pool.subnets) and session.subnet.endswith("/29")
    # the control listener is bound to the uplink address only (the session networks never see it)
    assert pool.control_bind == networks["bridge"]["IPAddress"]
    labels = info["Config"]["Labels"]
    assert labels["ouroloop.egress.pid"] == str(os.getpid()) and labels["ouroloop.egress.owner"] == pool.owner["nonce"]
    session.close()
    # neither the policy nor the key is on the container's command line
    assert pool._key not in json.dumps(info["Config"]["Cmd"] or []) + json.dumps(info["Args"] or [])


def test_a_policy_is_parsed_strictly_and_the_proxy_and_the_pool_agree_on_its_digest(pool):
    for bad in ([{"host": "*.example.com", "port": 443, "methods": ["CONNECT"]}],
                [{"host": "example.com", "port": 0, "methods": ["GET"]}],
                [{"host": "example.com", "port": 443, "methods": ["TRACE"]}],
                [{"host": "example.com", "port": 443, "methods": []}],
                [{"host": "example.com", "port": 443, "methods": ["GET"], "paths": ["/"]}],
                {"mode": "none"}, "example.com"):
        with pytest.raises(ValueError):
            parse_policy(bad)
    same = [{"host": "Example.COM", "port": 443, "methods": ["connect"]}, {"host": "example.com", "port": 443,
                                                                            "methods": ["CONNECT"]}]
    assert parse_policy(same) == {("example.com", 443): {"CONNECT"}}
    assert pool._control({"op": "ping"})["policy_sha256"] == pool.policy_digest == policy_sha256(pool.policy)
    # the control port refuses a caller without the pool's key (a worker on the internal network can reach it)
    import socket
    with socket.create_connection(("127.0.0.1", pool._port), timeout=10) as s:
        s.sendall(b'{"op": "register", "digest": "' + b"0" * 64 + b'", "key": "guess"}\n')
        assert json.loads(s.makefile().readline()) == {"ok": False, "error": "not authorized"}


def test_a_proxy_given_a_broken_policy_never_serves(targets):
    p = EgressPool([{"host": "example.com", "port": 443, "methods": ["CONNECT"]}], f"test-egress-{uuid.uuid4().hex}")
    p.policy = [{"host": "example.com", "port": 443, "methods": ["SMUGGLE"]}]       # what the addon will refuse
    with pytest.raises(EgressError, match="exited at start|refuses to serve"):
        p.start()
    assert docker("ps", "-a", "--filter", f"name=^{p.container}$", "--format", "{{.Names}}").strip() == ""


def test_close_waits_for_a_request_still_in_flight_and_returns_its_row(pool, targets):
    """A credential is revoked first (nothing new passes), then drained: a request already let through finishes
    and its row is among close()'s, not lost to the race between the worker's end and the session's."""
    import time
    session = pool.session("run/t3/0")
    env = {**session.env, "PLAIN": targets["plain"], "TLS": targets["tls"], "OTHER": targets["plain"],
           "TLS_CERT": targets["cert"]}
    name = f"ouroloop-egress-test-slow-{uuid.uuid4().hex[:8]}"
    docker("run", "-d", "--name", name, "--network", session.network, "--user", "65534:65534",
           *[x for k in env for x in ("-e", k)], BASE, "python", "-c", CLIENT, "slow", env=env)
    try:
        time.sleep(1.2)                               # the request is at the target now (it answers after 2.5 s)
        events = session.close(wait=10)
        assert [(e["method"], e.get("path"), e["status"]) for e in events] == [("GET", "/slow", 200)]
        assert events[0]["ms"] >= 2000
        docker("wait", name, timeout=60)
        assert json.loads(docker("logs", name).strip().splitlines()[-1])["slow"]["status"] == 200
    finally:
        docker("rm", "-f", name, check=False)


class Forged:
    """A session's shape with a credential the proxy never registered (or none at all)."""

    def __init__(self, session, token: str | None):
        self.network = session.network
        url = f"http://attempt:{token}@egress:3128" if token else "http://egress:3128"
        self.env = {**session.env, "HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url}


def test_no_credential_or_one_never_registered_gets_407_and_never_goes_out(pool, targets):
    real = pool.session("run/t4/0")
    for token, reason in (("x" * 43, "unknown_credential"), (None, "no_credential")):
        got = client(Forged(real, token), targets, "get", "tls")
        assert got["get"]["status"] == 407 and reason in got["get"]["body"], (reason, got)
        assert got["tls"]["status"] == 407
    # none of it is the registered attempt's: its rows are empty
    assert real.close() == []


def test_the_pool_refuses_a_proxy_that_reports_another_policy_than_it_was_given(pool):
    """start() hands out nothing until the addon's own digest of the policy it enforces is the pool's: here the pool
    is told to expect another policy, and the real proxy's answer is refused."""
    expected = pool.policy_digest
    try:
        pool.policy_digest = policy_sha256([{"host": "elsewhere.example", "port": 443, "methods": ["CONNECT"]}])
        with pytest.raises(EgressError, match="enforces another policy"):
            pool._await_ready()
    finally:
        pool.policy_digest = expected
    pool._await_ready()


def test_the_host_and_the_addon_read_a_policy_alike_without_importing_mitmproxy():
    """The addon is self-contained (one file copied into the image), so the policy rules exist twice; here both
    copies parse the same policies to the same digest. Loading the addon's file by path imports no mitmproxy on the
    host: there is none (§1.3 rule 1)."""
    import importlib.util
    import sys
    from pathlib import Path
    import ouroloop.runtime.egress as host
    path = Path(host.__file__).with_name("egress_addon.py")
    spec = importlib.util.spec_from_file_location("egress_addon_under_test", path)
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    assert "mitmproxy" not in sys.modules and addon.ctx is None
    policies = [[{"host": "API.example.com", "port": 443, "methods": ["connect"]}],
                {"mode": "proxy", "allow": [{"host": "10.0.0.7", "port": 8080, "methods": ["GET", "HEAD"]},
                                            {"host": "10.0.0.7", "port": 8080, "methods": ["POST"]},
                                            {"host": "[::1]", "port": 9000, "methods": ["DELETE"]}]}]
    for policy in policies:
        assert addon.parse_policy(policy) == host.parse_policy(policy)
        assert addon.policy_sha256(policy) == host.policy_sha256(policy)
    for bad in ([{"host": "a b", "port": 1, "methods": ["GET"]}], [{"host": "x", "port": 70000, "methods": ["GET"]}]):
        with pytest.raises(ValueError):
            addon.parse_policy(bad)
        with pytest.raises(ValueError):
            host.parse_policy(bad)
    import base64
    assert addon.credential("Basic " + base64.b64encode(b"attempt:tok3n").decode()) == "tok3n"
    for header in (None, "", "Bearer x", "Basic " + base64.b64encode(b"admin:tok3n").decode(), "Basic !!",
                   "Basic " + base64.b64encode(b"attempt:").decode()):
        assert addon.credential(header) is None
    with pytest.raises(ValueError, match="at least one"):
        EgressPool([], "scope")


def test_rows_are_drained_once_each_and_a_live_credential_keeps_recording_between_drains(pool, targets):
    session = pool.session("run/t5/0")
    for _ in range(2):
        assert client(session, targets, "get")["get"]["status"] == 200
        rows = pool._control({"op": "drain", "token_id": session.token_id, "wait": 1})["rows"]
        assert [(r["method"], r["path"], r["status"]) for r in rows] == [("GET", "/hello", 200)]
    assert session.close() == []                    # both rows were handed out already, none twice


# ---------------------------------------------------------------------------- the review's cases (2026-10-08)
def test_each_session_has_its_own_network_and_reaches_nothing_but_the_proxy(pool, targets):
    """Concurrent attempts, and a read-only session beside them, never share a network: a worker of one cannot
    reach a worker of another directly (that traffic would be unrecorded and go around the allowlist and the
    credentials), and no worker can reach the proxy's control port."""
    a, b = pool.session("run/iso/0"), pool.session("smoke/iso", read_only=True)
    assert a.network != b.network and a.subnet != b.subnet
    for s in (a, b):
        info = json.loads(docker("network", "inspect", s.network))[0]
        assert info["Internal"] is True and [c["Name"] for c in info["Containers"].values()] == [pool.container]
    peer = f"ouroloop-egress-test-peer-{uuid.uuid4().hex[:8]}"
    docker("run", "-d", "--name", peer, "--network", a.network, "--user", "65534:65534", BASE, "python", "-m",
           "http.server", "8000")
    try:
        ip = docker("inspect", "--format", "{{(index .NetworkSettings.Networks \"%s\").IPAddress}}" % a.network,
                    peer).strip()
        assert ip
        import time
        time.sleep(1.5)
        same = client(a, targets, "peer", "control", PEER=f"{ip}:8000")
        across = client(b, targets, "peer", "control", PEER=f"{ip}:8000")
    finally:
        docker("rm", "-f", peer, check=False)
    assert same["peer"]["status"] == 200                    # the probe works: on one network, a peer is reachable
    assert across["peer"]["status"] is None                 # across sessions: no route
    assert same["control"]["status"] is None and across["control"]["status"] is None
    assert a.close() == [] and b.close() == []              # direct traffic never reached the proxy


def test_one_proxy_per_scope_a_live_owner_is_never_displaced_and_a_dead_ones_proxy_is_reclaimed(targets):
    import sys
    from pathlib import Path
    scope = f"test-egress-{uuid.uuid4().hex}"
    policy = [{"host": targets["ips"]["plain"], "port": 8080, "methods": ["GET"]}]
    first = EgressPool(policy, scope).start()
    second = EgressPool(policy, scope)
    try:
        working = first.session("run/a/0")
        with pytest.raises(EgressError, match=f"in use by pid {os.getpid()}"):
            second.start()
        # the live proxy is untouched: its session still goes out, its control port still answers
        assert client(working, targets, "get")["get"]["status"] == 200
        assert first._control({"op": "ping"})["policy_sha256"] == first.policy_digest
        assert second.stop() == []                          # the refused pool owns nothing and removes nothing
        assert client(working, targets, "get")["get"]["status"] == 200
    finally:
        second.stop()                                       # whatever it started, were start() to displace first
        first.stop()
    # a process that started the scope's proxy and a session, then died without stop()
    repo = Path(__file__).resolve().parents[2]
    code = textwrap.dedent(f"""
        import json, os
        from ouroloop.runtime.egress import EgressPool
        p = EgressPool({policy!r}, {scope!r}).start()
        s = p.session("run/crashed/0")
        print(json.dumps({{"network": s.network}}), flush=True)
        os._exit(0)
    """)
    done = subprocess.run([sys.executable, "-c", code], cwd=repo, capture_output=True, text=True, timeout=180,
                          env={**os.environ, "PYTHONPATH": str(repo)})
    assert done.returncode == 0, done.stderr[-2000:]
    left = json.loads(done.stdout.strip().splitlines()[-1])["network"]
    container = first.container
    assert docker("inspect", "--format", "{{.State.Running}}", container).strip() == "true"
    third = EgressPool(policy, scope).start()               # the dead owner's proxy and network are removed
    try:
        assert docker("network", "ls", "--filter", f"name=^{left}$", "--format", "{{.Name}}").strip() == ""
        assert client(third.session("run/b/0"), targets, "get")["get"]["status"] == 200
    finally:
        third.stop()
    assert docker("ps", "-a", "--filter", f"name=^{container}$", "--format", "{{.Names}}").strip() == ""


def test_a_host_name_on_the_allowlist_is_matched_by_name_and_its_address_is_not(targets, tmp_path):
    """Hosts are matched exactly as named: an entry `target.test:8080` lets a request to that name out (the proxy
    resolves it on its uplink network), and the same target's IP literal is refused."""
    run = uuid.uuid4().hex[:8]
    uplink, target = f"ouroloop-egress-test-up-{run}", f"ouroloop-egress-test-named-{run}"
    docker("network", "create", uplink)
    try:
        docker("run", "-d", "--name", target, "--network", uplink, "--network-alias", "target.test", "--user",
               "65534:65534", BASE, "python", "-c", TARGET, "plain", "8080")
        ip = docker("inspect", "--format", "{{(index .NetworkSettings.Networks \"%s\").IPAddress}}" % uplink,
                    target).strip()
        import time
        for _ in range(100):
            if "ready" in docker("logs", target, check=False):
                break
            time.sleep(0.1)
        p = EgressPool([{"host": "target.test", "port": 8080, "methods": ["GET"]}], f"test-egress-{run}",
                       uplink=uplink).start()
        try:
            session = p.session("run/named/0")
            got = client(session, {"plain": "target.test:8080", "tls": "target.test:1", "ips": {"tls": ip},
                                   "cert": targets["cert"]}, "get", "other_host")
            rows = session.close()
        finally:
            p.stop()
    finally:
        docker("rm", "-f", target, check=False)
        docker("network", "rm", uplink, check=False)
    assert got["get"] == {"status": 200, "body": '{"hello": "world"}'}
    assert got["other_host"]["status"] == 403
    assert [(r["host"], r["status"], r["reason"]) for r in rows] == [("target.test", 200, None),
                                                                    (ip, 403, "host_not_allowed")]


@pytest.fixture
def wide(targets, tmp_path):
    """A pool whose policy also lets POST out to the plain target and tunnels to a port nothing listens on."""
    plain_ip, tls_ip = targets["ips"]["plain"], targets["ips"]["tls"]
    p = EgressPool([{"host": plain_ip, "port": 8080, "methods": ["GET", "POST"]},
                    {"host": tls_ip, "port": 8443, "methods": ["CONNECT"]},
                    {"host": plain_ip, "port": 9, "methods": ["CONNECT"]}], f"test-egress-{uuid.uuid4().hex}").start()
    yield p
    p.stop()


def test_plain_http_is_decided_on_the_headers_bodies_stream_through_and_a_long_refused_body_is_never_read(wide,
                                                                                                        targets):
    session = wide.session("run/body/0")
    got = client(session, targets, "big_refused", "chunked_refused", "stream_upload", "post")
    # refused (a host off the list) with a 200 MiB or an unsized body: the connection is closed at once, unread
    for name in ("big_refused", "chunked_refused"):
        assert got[name]["result"] == "closed" and got[name]["seconds"] < 5, (name, got[name])
    # allowed: the body streams through — the target had the head before the client sent the second half
    assert got["stream_upload"] == {"status": 200, "posted": 2_000_000, "head_before_second_half": True}
    assert got["post"]["status"] == 200 and json.loads(got["post"]["body"])["posted"] == 300
    rows = session.close()
    cut = [r for r in rows if r.get("cut")]
    assert [(r["method"], r["status"], r["reason"], r["bytes_up"]) for r in cut] == \
        [("POST", 403, "host_not_allowed", 0)] * 2 and all("error" not in r for r in cut)
    (upload,) = [r for r in rows if r.get("path") == "/upload"]
    assert upload["status"] == 200 and upload["allowed"] is True and upload["bytes_up"] == 2_000_000
    assert upload["bytes_down"] > len('{"posted": 2000000}')            # the answer, counted as it streamed back
    (small,) = [r for r in rows if r.get("path") == "/echo"]
    assert small["status"] == 200 and small["bytes_up"] == 300


def test_a_tunnel_to_an_unreachable_port_and_a_tunnel_still_open_at_close_are_recorded(wide, targets):
    session = wide.session("run/tunnels/0")
    dead = client(session, targets, "dead_port", DEAD=f"{targets['ips']['plain']}:9")
    assert dead["dead_port"]["status"] == 502
    env = {**session.env, "TLS": targets["tls"]}
    holder = f"ouroloop-egress-test-hold-{uuid.uuid4().hex[:8]}"
    docker("run", "-d", "--name", holder, "--network", session.network, "--user", "65534:65534",
           *[x for k in env for x in ("-e", k)], BASE, "python", "-c", HOLD, env=env)
    try:
        import time
        for _ in range(200):
            if "tunnel:" in docker("logs", holder, check=False):
                break
            time.sleep(0.1)
        assert "tunnel:HTTP/1.1 200" in docker("logs", holder)
        rows = session.close(wait=0.5)
        # close() cut the holder off: its network is gone
        assert session.network not in docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", holder)
    finally:
        docker("rm", "-f", holder, check=False)
    (unreachable,) = [r for r in rows if r["port"] == 9]
    assert unreachable["method"] == "CONNECT" and unreachable["status"] == 502 and unreachable["allowed"] is True
    assert unreachable["reason"] == "upstream_error" and unreachable["error"]
    (still_open,) = [r for r in rows if r.get("open")]
    assert still_open["method"] == "CONNECT" and still_open["port"] == 8443 and still_open["status"] == 200


def test_rows_over_the_cap_come_back_as_one_dropped_row_and_go_into_the_transcript_as_they_are(targets, tmp_path):
    """The rows close() returns go into the attempt's transcript as `egress` lines unchanged: none of their keys is
    the line's own (kind, ts, seq, source), the proxy's clock is `proxy_ts`, and rows the proxy had to drop are one row
    {"dropped": n}."""
    p = EgressPool([{"host": targets["ips"]["plain"], "port": 8080, "methods": ["GET"]}],
                   f"test-egress-{uuid.uuid4().hex}", max_rows=2).start()
    try:
        assert p._control({"op": "ping"})["max_rows"] == 2
        session = p.session("run/cap/0")
        got = client(session, targets, "get", "echo", "other_host", "post")
        assert got["get"]["status"] == 200 and got["other_host"]["status"] == 403
        rows = session.close()
    finally:
        p.stop()
    assert [r.get("dropped") for r in rows] == [None, None, 2]
    assert rows[-1] == {"token_id": session.token_id, "attempt_id": "run/cap/0", "read_only": False, "dropped": 2}
    transcript = Transcript(tmp_path / "transcripts" / "run-cap-0.jsonl")
    transcript.write("task", text="Fetch the hello document.")
    for row in rows:
        transcript.write("egress", **row)
    lines = read(transcript.path)
    assert [e["kind"] for e in lines] == ["task", "egress", "egress", "egress"]
    assert [e["seq"] for e in lines] == [1, 2, 3, 4]
    assert all(isinstance(e["ts"], str) and "T" in e["ts"] for e in lines)       # the transcript's own clock
    assert all(isinstance(e["proxy_ts"], float) for e in lines[1:3])
    assert [e.get("path") for e in lines[1:3]] == ["/hello", "/echo?x=1"] and lines[3]["dropped"] == 2


# ---------------------------------------------------------------------------- the addon's request cap (no mitmproxy)
KEY = "k" * 32


@pytest.fixture
def addon(monkeypatch):
    """The addon loaded by path, as the image runs it, configured from its environment; no mitmproxy on the host."""
    import importlib.util
    from pathlib import Path
    monkeypatch.setenv("OUROLOOP_EGRESS_CONTROL_KEY", KEY)
    monkeypatch.setenv("OUROLOOP_EGRESS_POLICY", json.dumps([{"host": "api.example.com", "port": 443,
                                                              "methods": ["CONNECT", "GET"]}]))
    spec = importlib.util.spec_from_file_location("egress_addon_cap", Path(E.__file__).with_name("egress_addon.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Egress()


def control(addon, **request):
    return asyncio.run(addon._answer({"key": KEY, **request}))


def test_the_addon_lets_out_max_requests_and_refuses_the_next_at_the_cap(addon):
    """Control protocol 2: a credential registered with max_requests lets that many requests and tunnels out and
    refuses the next with 429 request_cap; a policy refusal counts nothing; poll moves the cap and hands out each row
    once; without a cap a credential is uncapped; a bad cap and an unknown credential are refused."""
    token = "t" * 43
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert control(addon, op="ping")["protocol"] == E.PROTOCOL == 2
    assert control(addon, op="register", digest=digest, read_only=False, max_requests=2)["ok"]
    tid = digest[:12]
    assert addon.decide(token, "GET", "other.example.com", 443, connect=False)[:2] == (403, "host_not_allowed")
    assert addon.decide(token, "GET", "api.example.com", 443, connect=False)[0] == 0
    assert addon.decide(token, "CONNECT", "api.example.com", 443, connect=True)[0] == 0
    assert addon.decide(token, "GET", "api.example.com", 443, connect=False)[:2] == (429, "request_cap")
    addon._emit(tid, {"method": "GET", "host": "api.example.com", "port": 443, "status": 200, "allowed": True})
    polled = control(addon, op="poll", token_id=tid, max_requests=3)
    assert polled["used"] == 2 and [r["status"] for r in polled["rows"]] == [200]
    assert control(addon, op="poll", token_id=tid)["rows"] == []
    assert addon.decide(token, "GET", "api.example.com", 443, connect=False)[0] == 0            # the cap moved to 3
    assert addon.decide(token, "GET", "api.example.com", 443, connect=False)[:2] == (429, "request_cap")
    assert control(addon, op="poll", token_id=tid, max_requests=None)["used"] == 3
    assert addon.decide(token, "GET", "api.example.com", 443, connect=False)[0] == 0            # no cap any more
    assert control(addon, op="drain", token_id=tid, wait=0)["used"] == 4
    other = hashlib.sha256(b"u" * 43).hexdigest()
    assert control(addon, op="register", digest=other, read_only=False)["ok"]
    assert all(addon.decide("u" * 43, "GET", "api.example.com", 443, connect=False)[0] == 0 for _ in range(5))
    assert not control(addon, op="register", digest=hashlib.sha256(b"v").hexdigest(), max_requests=-1)["ok"]
    assert not control(addon, op="poll", token_id="0" * 12)["ok"]


def test_the_pool_refuses_an_addon_without_the_request_cap(monkeypatch):
    pool = EgressPool([{"host": "a.example", "port": 80, "methods": ["GET"]}], "scope-old-addon")
    pool._port = 1
    monkeypatch.setattr(pool, "_docker", lambda *a, **k: "true")
    monkeypatch.setattr(pool, "_control", lambda request, timeout=30: {
        "ok": True, "policy_sha256": pool.policy_digest, "mitmproxy": "12.2.3"})          # protocol 1: no `protocol`
    with pytest.raises(EgressError, match="control protocol None, and this host needs 2"):
        pool._await_ready()


def test_one_shared_pool_per_scope_and_policy_and_stop_shared_stops_them_all(monkeypatch):
    """A process's workers of one activity open their sessions from one proxy (shared_pool), which starts at its first
    session only; stop_shared stops every pool, returns the rows of their open sessions, and the next phase gets a new
    pool."""
    policy = {"mode": "proxy", "allow": [{"host": "api.example.com", "port": 443, "methods": ["CONNECT"]}]}
    pool = E.shared_pool(policy, f"scope-{uuid.uuid4().hex}", image="img")
    assert E.shared_pool(policy, pool.pool.scope_id, image="img") is pool
    assert E.shared_pool(policy, pool.pool.scope_id, image="other") is not pool
    assert not pool.pool.started and pool.stop() == []
    stopped = []
    monkeypatch.setattr(pool.pool, "stop", lambda: stopped.append(1) or [{"row": 1}])
    pool.pool._port = 1                                                                  # as if started
    assert {"row": 1} in E.stop_shared() and stopped == [1]
    assert E.shared_pool(policy, pool.pool.scope_id, image="img") is not pool          # a new phase, a new pool
    E.stop_shared()


# ---------------------------------------------------------------------------- an attempt through the proxy, in Docker
THREAD_ENV = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


@pytest.fixture
def proxy_k(targets):
    """K's proxy network allowing the plain target (GET and POST), the worker image's qualified worker under it with
    a pool of its own (started at the first session, stopped after the test)."""
    from ouroloop.runtime.worker import DEFAULT_IMAGE as WORKER_IMAGE, WorkerResources, docker_worker
    from runtime_world import core
    require(image_present(WORKER_IMAGE), f"the image {WORKER_IMAGE} is not here")
    network = {"mode": "proxy", "allow": [{"host": targets["ips"]["plain"], "port": 8080, "methods": ["GET", "POST"]}]}
    pool = E.LazyPool(network, f"test-attempt-{uuid.uuid4().hex}")
    worker = docker_worker(WorkerResources(network=network, env=THREAD_ENV), egress_pool=pool)
    try:
        yield core(worker={"network": network, "env": THREAD_ENV}), worker
    finally:
        pool.stop()


def test_the_seeds_attempt_goes_out_through_its_own_capped_credential(tmp_path, targets, proxy_k):
    """The stirrup seed under a proxy K, in the real image: it is offered http_fetch, its requests go out through the
    attempt's own credential, every row is an `egress` line of its transcript and proxied requests are tool calls.
    With one tool call left the second request is refused at the proxy and the attempt ends at tool_calls; a base call
    and two requests in one turn under two tool calls end the same way."""
    from runtime_world import Script, attempt, core, finish, of, seed, turn
    k, worker = proxy_k
    package = seed(tmp_path / "seed", k)
    plain = targets["plain"]

    def fetch(path):
        return ("http_fetch", {"url": f"http://{plain}{path}", "method": "GET"})
    script = [turn("", fetch("/hello")), turn("", fetch("/echo?n=2")), finish("Fetched twice.")]
    provider = Script(script)
    a = attempt(tmp_path / "ok", package, provider, k=k, worker=worker, attempt_id="run1:ok:0:subject")
    assert (a.result.termination, a.result.text) == ("finished", "Fetched twice.")
    assert "http_fetch" in provider.seen[0]["tools"]
    rows = of(a.lines, "egress")
    assert [(r["method"], r["status"], r["path"], r["read_only"]) for r in rows] == \
        [("GET", 200, "/hello", False), ("GET", 200, "/echo?n=2", False)]
    assert all(r["attempt_id"] == "run1:ok:0:subject" for r in rows)
    (session,) = of(a.lines, "worker_session")
    assert session["egress"]["requests"] == 2 and session["egress"]["rows"] == 2
    assert a.surface.tool_calls == a.surface.base_tool_calls + 2 == 2

    capped = attempt(tmp_path / "cap", package, Script(script), k=core(**{**k, "limits": {"max_tool_calls": 1}}),
                     worker=worker, attempt_id="run1:cap:0:subject")
    assert [(r["status"], r.get("reason")) for r in of(capped.lines, "egress")] == [(200, None), (429, "request_cap")]
    assert (capped.result.termination, capped.result.limit, capped.surface.tool_calls) == ("budget", "tool_calls", 1)

    mixed = attempt(tmp_path / "mixed", package,
                    Script([turn("", ("api_search", {"q": "contacts"}), fetch("/hello"), fetch("/echo?n=2")),
                            finish("Done.")]),
                    k=core(**{**k, "limits": {"max_tool_calls": 2}}), worker=worker, attempt_id="run1:mixed:0:subject")
    assert [(r["status"], r.get("reason")) for r in of(mixed.lines, "egress")] == [(200, None), (429, "request_cap")]
    assert (mixed.surface.base_tool_calls, mixed.surface.tool_calls, mixed.result.limit) == (1, 2, "tool_calls")


def test_the_cap_follows_a_base_call_before_the_agent_goes_out_again(tmp_path, targets, proxy_k):
    """An agent that makes a base call and then two requests with no callback between them (no mark, no model
    call): the host tightens the proxy's cap as the base call is answered, so under two tool calls the second request
    is refused at the proxy and never goes out — the limit holds without the agent's cooperation."""
    from runtime_world import Script, attempt, core, minimal, of
    k, worker = proxy_k
    plain = targets["plain"]
    agent = (f"import json, urllib.request, urllib.error\n"
             f"def _get(path):\n"
             f"    try:\n"
             f"        with urllib.request.urlopen('http://{plain}' + path, timeout=10) as r:\n"
             f"            return r.status\n"
             f"    except urllib.error.HTTPError as e:\n"
             f"        return e.code\n"
             f"def run(ctx, task):\n"
             f"    ctx.tool('api_search', {{'q': 'contacts'}})\n"
             f"    return json.dumps([_get('/hello'), _get('/echo?n=2')])\n")
    k = core(**{**k, "limits": {"max_tool_calls": 2}})
    a = attempt(tmp_path, minimal(tmp_path / "pkg", agent, k=k), Script([]), k=k, worker=worker)
    rows = of(a.lines, "egress")
    assert [(r["path"], r["status"], r.get("reason")) for r in rows] == \
        [("/hello", 200, None), ("/echo?n=2", 429, "request_cap")]
    assert (a.surface.base_tool_calls, a.surface.tool_calls) == (1, 2)
