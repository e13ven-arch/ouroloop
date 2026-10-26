"""The worker's resources, network and isolation come from K (docs/design/RUNTIME.md §3, §6, §8).

Without Docker: WorkerResources.from_core and its refusals, the command DockerWorker builds from them (every resource
from K, the isolation flags fixed), the network rules (none refuses an egress session; proxy needs one, on an internal
network only, and its pool; R's work tree never needs one), and which workers may run package code at all.
In Docker (skipped without it or the image, failed under REHARNESS_TEST_REQUIRE_DOCKER): what package code can reach in
the real image, K's resources as Docker reads them (and a change of K changing them), the image run by the content id K
froze, a hung package's container removed, swap and pids as K sets them, the proxy network, and the seed's whole
attempt in the image under K's limits.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from reharness.activity import presets
from reharness.contracts import EgressUnavailable, Infrastructure, WorkerUnavailable
from reharness.runtime import worker as W
from reharness.runtime.egress import EgressError
from reharness.runtime.worker import (DEFAULT_IMAGE, EGRESS_ENV, RESOURCE_KEYS, WORKER_ENV, DockerWorker,
                                     PackageCallError, ProcessWorker, WorkerLimits, WorkerResources, check_package,
                                     docker_worker, usable_worker)
from runtime_world import Script, attempt, core, docker, finish, inspect, kinds, of, require, seed, turn

THREAD_ENV = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
TABLE = {"image": "some/image:tag", "image_id": "sha256:" + "c" * 64, "cpus": 2, "memory": "640m",
         "memory_swap": "640m", "pids": 77, "tmpfs_mb": 96, "network": "none", "env": THREAD_ENV,
         "request_seconds": 12.5, "max_output_chars": 777}
ISOLATION = ["--read-only", "--user", "65534:65534", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]


def flags(cmd: list[str]) -> dict:
    return {f: cmd[cmd.index(f) + 1] for f in ("--memory", "--memory-swap", "--pids-limit", "--tmpfs", "--cpus",
                                               "--network") if f in cmd}


def env_tail(cmd: list[str]) -> list[str]:
    """What the runner's environment is: the items after `env -i`, before `python -I`."""
    return cmd[cmd.index("-i", cmd.index("env")) + 1:-3]


def isolated(cmd: list[str]) -> None:
    for flag in ISOLATION:
        assert flag in cmd, flag
    assert cmd[-3:] == ["python", "-I", "/runner/worker_runner.py"] and env_tail(cmd)[:2] == list(WORKER_ENV)


def egress(network="reharness-egress-1a2b3c4d", **env):
    return SimpleNamespace(network=network, env={"HTTP_PROXY": "http://attempt:tok@egress:3128",
                                                 "HTTPS_PROXY": "http://attempt:tok@egress:3128",
                                                 "NO_PROXY": "", "REHARNESS_EGRESS": "1", **env})


# ---------------------------------------------------------------------------- K's worker table
def test_every_resource_is_read_from_ks_worker_table_and_the_preset_names_them_all():
    """from_core reads K's `worker` table whole (cpus and memory as Docker's strings), the output bound falls back to
    K's limits; the preset names every resource, so nothing a worker runs with is the code's default."""
    r = WorkerResources.from_core({"worker": TABLE})
    assert r.to_json() == {**TABLE, "cpus": "2"}
    assert WorkerResources.from_core({"worker": r.to_json()}) == r
    fallback = WorkerResources.from_core({"worker": {"memory": "1g"}, "limits": {"max_output_chars": 900}})
    assert fallback.max_output_chars == 900 and fallback.memory == "1g"
    assert set(presets.AUTOMATIONBENCH_V1["worker"]) == set(RESOURCE_KEYS)
    assert WorkerResources.from_core(presets.AUTOMATIONBENCH_V1).to_json() == presets.AUTOMATIONBENCH_V1["worker"]


@pytest.mark.parametrize("table, match", [
    ({"memroy": "512m"}, "memroy"),
    ({"pids": "128"}, "positive integer"),
    ({"pids": 0}, "positive integer"),
    ({"tmpfs_mb": True}, "positive integer"),
    ({"network": "bridge"}, "network"),
    ({"network": {"mode": "open"}}, "network"),
    ({"env": {"PATH": "/tmp"}}, "runner's own"),
    ({"env": {"1BAD": "x"}}, "NAME"),
    ({"env": ["NOVALUE"]}, "NAME=value"),
    ({"env": {"X": "a\nb"}}, "one line"),
    ({"env": {"HTTP_PROXY": "http://elsewhere:8080"}}, "egress session's"),
    ({"env": {"https_proxy": "http://elsewhere:8080"}}, "egress session's"),
    ({"env": {"REHARNESS_EGRESS": "1"}}, "egress session's"),
    ({"env": ["NO_PROXY=*"]}, "egress session's"),
])
def test_a_k_worker_table_that_is_refused(table, match):
    with pytest.raises(ValueError, match=match):
        WorkerResources.from_core({"worker": table})


def test_the_docker_command_carries_every_resource_from_k_and_changes_when_k_does(monkeypatch):
    """Memory, swap, pids, /tmp, cpus, the network, the environment after the runner's own and the image by the
    content id K froze all come from K; the isolation flags are literals whatever K says. A K with another memory
    gives a command that differs in that value and nowhere else; without memory_swap there is no swap flag."""
    worker = DockerWorker(WorkerResources.from_core({"worker": TABLE}))
    cmd = worker.command("w")
    assert flags(cmd) == {"--memory": "640m", "--memory-swap": "640m", "--pids-limit": "77",
                          "--tmpfs": "/tmp:rw,size=96m", "--cpus": "2", "--network": "none"}
    assert cmd[cmd.index("--cpus") + 2] == TABLE["image_id"] and "some/image:tag" not in cmd
    assert env_tail(cmd) == [*WORKER_ENV, *(f"{k}={v}" for k, v in THREAD_ENV.items())]
    isolated(cmd)
    assert worker.limits == WorkerLimits(request_seconds=12.5, max_output_chars=777)
    monkeypatch.setattr(DockerWorker, "image_id", lambda self: self.pinned_id)
    described = worker.describe()
    assert {k: described[k] for k in ("image", "image_id", "cpus", "memory", "memory_swap", "pids", "tmpfs_mb",
                                      "network")} == {"image": "some/image:tag", "image_id": TABLE["image_id"],
                                                      "cpus": "2", "memory": "640m", "memory_swap": "640m", "pids": 77,
                                                      "tmpfs_mb": 96, "network": "none"}
    other = DockerWorker(WorkerResources.from_core({"worker": {**TABLE, "memory": "1g"}})).command("w")
    assert [i for i, (a, b) in enumerate(zip(cmd, other)) if a != b] == [cmd.index("--memory") + 1]
    assert other[other.index("--memory") + 1] == "1g"
    bare = DockerWorker(WorkerResources.from_core({"worker": {k: v for k, v in TABLE.items()
                                                              if k not in ("memory_swap", "image_id")}})).command()
    assert "--memory-swap" not in bare and bare[bare.index("--cpus") + 2] == "some/image:tag"


# ---------------------------------------------------------------------------- the network
PROXY = {"mode": "proxy", "allow": [{"host": "api.example.com", "port": 443, "methods": ["CONNECT"]}]}


def test_network_none_refuses_an_egress_session_and_proxy_needs_one_on_a_named_network():
    with pytest.raises(ValueError, match="network is none"):
        DockerWorker(WorkerResources()).start(egress=egress())
    worker = DockerWorker(WorkerResources(network=PROXY, env=THREAD_ENV))
    assert worker.network == "proxy"
    with pytest.raises(EgressUnavailable, match="needs the attempt's egress session"):
        worker.start()
    assert issubclass(EgressUnavailable, Infrastructure)
    with pytest.raises(ValueError, match="a session sets the proxy variables"):
        worker.start(egress=egress(PYTHONPATH="/tmp"))
    assert set(egress().env) <= set(EGRESS_ENV)
    for bad in ("host", "bridge", "default", "none", "container:abc", ""):
        with pytest.raises(ValueError, match="egress network"):
            worker.start(egress=egress(network=bad))
    with pytest.raises(ValueError, match="mapping"):
        worker.start(egress=SimpleNamespace(network="reharness-egress-1", env=["HTTP_PROXY=x"]))
    session_egress = egress()
    cmd = worker.command("w", session_egress)
    assert cmd[cmd.index("--network") + 1] == "reharness-egress-1a2b3c4d"
    assert env_tail(cmd) == [*WORKER_ENV, *(f"{k}={v}" for k, v in THREAD_ENV.items()),
                             *(f"{k}={v}" for k, v in session_egress.env.items())]
    isolated(cmd)


def test_a_proxy_worker_starts_only_on_a_network_docker_says_is_internal(monkeypatch):
    """start() asks Docker whether the session's network is --internal before any container starts: a network with a
    route out would give package code the internet past the proxy, so it is EgressUnavailable, as is one that does
    not exist."""
    answers, spawned = [], []

    def run(args, **_kw):
        assert args[:3] == ["docker", "network", "inspect"]
        return answers.pop(0)
    monkeypatch.setattr(W.subprocess, "run", run)
    monkeypatch.setattr(DockerWorker, "_spawn", lambda self, e: spawned.append(e) or "session")
    worker = DockerWorker(WorkerResources(network=PROXY))
    answers.append(SimpleNamespace(returncode=0, stdout="false\n", stderr=""))
    with pytest.raises(EgressUnavailable, match="is not internal"):
        worker.start(egress=egress())
    answers.append(SimpleNamespace(returncode=1, stdout="", stderr="Error: No such network"))
    with pytest.raises(EgressUnavailable, match="does not exist"):
        worker.start(egress=egress())
    assert spawned == []
    answers.append(SimpleNamespace(returncode=0, stdout="true\n", stderr=""))
    session_egress = egress()
    assert worker.start(egress=session_egress) == "session" and spawned == [session_egress]
    # R's work tree and the health probe run no package code: no network, whatever K's is
    assert worker.start_offline() == "session" and worker._health_session() == "session" and spawned[1:] == [None, None]


def test_the_egress_session_comes_from_the_pool_and_without_one_is_infrastructure():
    assert DockerWorker(WorkerResources()).egress_session("a1") is None
    with pytest.raises(EgressUnavailable, match="no egress pool"):
        DockerWorker(WorkerResources(network=PROXY)).egress_session("a1")
    asked = []

    class Pool:
        def session(self, attempt_id, *, read_only=False, max_requests=None):
            asked.append((attempt_id, read_only, max_requests))
            if attempt_id == "broken":
                raise EgressError("the proxy is not running")
            return "the session"
    worker = DockerWorker(WorkerResources(network=PROXY), egress_pool=Pool())
    assert worker.egress_session("a1", read_only=True, max_requests=7) == "the session"
    with pytest.raises(EgressUnavailable, match="could not open a session: the proxy is not running"):
        worker.egress_session("broken")
    assert asked == [("a1", True, 7), ("broken", False, None)]


def test_package_code_runs_only_in_an_isolating_worker_or_one_a_test_names(tmp_path):
    """usable_worker: a qualified worker, or the host's ProcessWorker when a test names it as such; nothing else runs
    package code — the draft check refuses it, and an attempt over it is infrastructure before any model call."""
    assert not usable_worker(None) and not usable_worker(ProcessWorker())
    assert usable_worker(ProcessWorker(for_tests=True)) and usable_worker(DockerWorker(WorkerResources()))
    with pytest.raises(WorkerUnavailable, match="does not isolate"):
        check_package(ProcessWorker(), {}, {})
    provider = Script([finish()])
    a = attempt(tmp_path, seed(tmp_path / "seed"), provider, worker=ProcessWorker())
    assert a.result.termination == "infrastructure" and "no qualified isolated worker" in a.result.error
    assert provider.n == 0 and kinds(a.lines) == ["task", "end"]


# ---------------------------------------------------------------------------- in Docker
def qualified(resources: WorkerResources | None = None) -> DockerWorker:
    """The Docker worker of DEFAULT_IMAGE with these resources: skipped without Docker or the image (failed where the
    full run demands them)."""
    worker = docker_worker(resources or WorkerResources())
    require(worker is not None, f"docker or the {DEFAULT_IMAGE} image is not available")
    return worker


PROBE = '''import json, os, socket
def run(ctx, args):
    out = {"host_path": os.path.exists(args["path"])}
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2)
        out["net"] = True
    except OSError:
        out["net"] = False
    out["env"] = sorted(os.environ)
    out["proc_env"] = sorted(k.split(b"=")[0].decode() for k in open("/proc/self/environ", "rb").read().split(b"\\0")
                             if k)
    out["uid"] = os.getuid()
    try:
        open("/runner/x", "w").write("x")
        out["root_writable"] = True
    except OSError:
        out["root_writable"] = False
    open("/tmp/scratch", "w").write("ok")
    out["tmp"] = open("/tmp/scratch").read()
    return json.dumps(out)
'''


def test_package_code_in_the_docker_worker_cannot_reach_the_host():
    """What package code sees in the real image: no host file, no network, an environment of exactly the runner's
    and K's variables (the host's is not forwarded and the base image's is dropped), not root, a read-only root
    filesystem and a writable /tmp."""
    session = qualified(WorkerResources(env=THREAD_ENV)).start()
    try:
        session.load({"tools/t.py": PROBE})
        value = session.request({"op": "call_tool", "entry": "tools/t.py:run", "args": {"path": str(Path.cwd())},
                                 "view": {}})
        names = sorted(["LANG", "PATH", *THREAD_ENV])
        assert json.loads(value["output"]) == {"host_path": False, "net": False, "env": names, "proc_env": names,
                                               "uid": 65534, "root_writable": False, "tmp": "ok"}
    finally:
        session.close()


def test_ks_resources_are_what_docker_runs_the_container_with_and_a_change_of_k_changes_them():
    """docker inspect reads K's memory, swap, pids, cpus and /tmp off the running container, and the isolation;
    the same K with another memory gives a container with that memory."""
    table = {"memory": "512m", "memory_swap": "512m", "pids": 77, "tmpfs_mb": 96, "cpus": "1", "env": THREAD_ENV}
    seen = {}
    for memory in ("512m", "640m"):
        worker = qualified(WorkerResources.from_core({"worker": {**table, "memory": memory, "memory_swap": memory}}))
        session = worker.start()
        try:
            session.load({})
            host = json.loads(docker("inspect", "--format", "{{json .HostConfig}}", session.container))
            seen[memory] = host
        finally:
            session.close(force=True)
    mb = 1 << 20
    for memory, size in (("512m", 512 * mb), ("640m", 640 * mb)):
        host = seen[memory]
        assert (host["Memory"], host["MemorySwap"], host["PidsLimit"], host["NanoCpus"]) == (size, size, 77, 10 ** 9)
        assert host["Tmpfs"] == {"/tmp": "rw,size=96m"} and host["NetworkMode"] == "none"
        assert host["ReadonlyRootfs"] is True and host["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in host["SecurityOpt"]


def test_the_qualified_worker_runs_the_image_k_froze_by_its_content_id():
    """The id, not the tag, is what runs: a worker pinned to the image's id runs it even under a tag that names
    nothing, and names it in describe(); an id that is gone is no worker."""
    image_id = qualified().image_id()
    assert image_id and image_id.startswith("sha256:")
    pinned = docker_worker(WorkerResources(image="reharness-worker:no-such-tag-" + uuid.uuid4().hex[:8],
                                           image_id=image_id, request_seconds=7))
    assert pinned is not None and pinned.pinned_id == image_id
    assert pinned.command("n")[pinned.command("n").index("--cpus") + 2] == image_id
    assert pinned.healthy() and pinned.describe()["image_id"] == image_id and pinned.limits.request_seconds == 7.0
    gone = WorkerResources(image_id="sha256:" + "0" * 64)
    assert docker_worker(gone) is None and DockerWorker(gone).describe()["image_id"] is None


def test_a_hung_package_in_docker_is_its_defect_and_its_container_is_removed():
    worker = qualified(WorkerResources(request_seconds=3.0))
    session = worker.start()
    name = session.container
    session.load({"tools/t.py": "def run(ctx, args):\n    while True:\n        pass\n"})
    with pytest.raises(PackageCallError, match="did not answer call_tool"):
        session.request({"op": "call_tool", "entry": "tools/t.py:run", "args": {}, "view": {}})
    left = docker("ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}").strip()
    assert left == ""                                         # the container itself is gone, not only its client


ALLOC = "def run(ctx, args):\n    data = b'1' * (int(args['mb']) << 20)\n    return str(len(data))\n"


def test_memory_swap_equal_to_memory_turns_swap_off_in_a_real_container():
    """K's memory 512m with memory_swap 512m: a tool that needs 640 MB is killed; with no memory_swap in K the same
    call is answered (Docker's default swap is twice the memory)."""
    off = qualified(WorkerResources(memory="512m", memory_swap="512m", request_seconds=60))
    session = off.start()
    try:
        session.load({"tools/alloc.py": ALLOC})
        with pytest.raises(PackageCallError, match="exited during call_tool"):
            session.request({"op": "call_tool", "entry": "tools/alloc.py:run", "args": {"mb": 640}, "view": {}})
    finally:
        session.close(force=True)
    session = qualified(WorkerResources(memory="512m", request_seconds=60)).start()
    try:
        session.load({"tools/alloc.py": ALLOC})
        assert inspect(session.container, "{{.HostConfig.MemorySwap}}") != str(512 << 20)
        value = session.request({"op": "call_tool", "entry": "tools/alloc.py:run", "args": {"mb": 640}, "view": {}})
        assert value == {"output": str(640 << 20), "is_error": False}
    finally:
        session.close(force=True)


NUMPY = "import numpy\n\ndef run(ctx, args):\n    return str(numpy.ones(3).sum())\n"


def test_under_pids_16_the_thread_variables_in_ks_env_decide_whether_numpy_imports():
    """OpenBLAS starts one thread per CPU the container sees at import, and --cpus does not change that: on a host
    with more CPUs than --pids-limit leaves, the import fails; K's `env` setting the thread variables makes it work."""
    session = qualified(WorkerResources(pids=16, env=THREAD_ENV, request_seconds=60)).start()
    try:
        session.load({"tools/np.py": NUMPY})
        assert session.request({"op": "call_tool", "entry": "tools/np.py:run", "args": {}, "view": {}}) == \
            {"output": "3.0", "is_error": False}
    finally:
        session.close(force=True)
    cpus = int(docker("info", "--format", "{{.NCPU}}").strip() or 0)
    if cpus <= 16:
        pytest.skip(f"the docker host has {cpus} CPUs: OpenBLAS's threads fit under 16 pids, so the failure without "
                    f"the variables cannot be shown here")
    session = qualified(WorkerResources(pids=16, request_seconds=60)).start()
    try:
        session.load({"tools/np.py": NUMPY})
        with pytest.raises(PackageCallError):
            session.request({"op": "probe", "entries": ["tools/np.py:run"]})
    finally:
        session.close(force=True)


ENV_AND_NET = '''import os, socket
def run(ctx, args):
    seen = {k: os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "REHARNESS_EGRESS", "OPENBLAS_NUM_THREADS")}
    try:
        socket.create_connection(("1.1.1.1", 80), timeout=3).close()
        direct = "open"
    except OSError as e:
        direct = type(e).__name__
    return {"output": repr((sorted(os.environ), seen, direct)), "is_error": False}
'''


def _network(internal: bool) -> str:
    name = f"reharness-test-{uuid.uuid4().hex[:8]}"
    docker("network", "create", *(["--internal"] if internal else []), name)
    return name


def test_a_proxy_worker_joins_only_an_internal_network_and_sees_the_egress_variables(monkeypatch):
    """The proxy mode on the container side: an open network and a missing one are refused before any container
    starts; on an internal network the worker sees exactly the runner's, K's and the session's variables, and a
    direct connection out fails — the proxy is the only way out. The health probe of a proxy worker needs no session."""
    qualified()
    worker = DockerWorker(WorkerResources(network=PROXY, env={"OPENBLAS_NUM_THREADS": "1"}))
    spawned, spawn = [], DockerWorker._spawn
    monkeypatch.setattr(DockerWorker, "_spawn", lambda self, e: spawned.append(e) or spawn(self, e))
    open_net = _network(internal=False)
    try:
        with pytest.raises(EgressUnavailable, match="is not internal"):
            worker.start(egress=egress(network=open_net))
    finally:
        docker("network", "rm", open_net, check=False)
    with pytest.raises(EgressUnavailable, match="does not exist"):
        worker.start(egress=egress(network=f"reharness-test-gone-{uuid.uuid4().hex[:8]}"))
    assert spawned == []                                  # no container was started for either
    network = _network(internal=True)
    try:
        session = worker.start(egress=egress(network=network))
        try:
            session.load({"tools/env.py": ENV_AND_NET})
            nets = inspect(session.container, "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}")
            assert nets.split() == [network]
            value = session.request({"op": "call_tool", "entry": "tools/env.py:run", "args": {}, "view": {}})
            names, seen, direct = eval(value["output"])            # noqa: S307 - our own repr of a tuple
            assert seen == {"HTTP_PROXY": "http://attempt:tok@egress:3128",
                            "HTTPS_PROXY": "http://attempt:tok@egress:3128", "REHARNESS_EGRESS": "1",
                            "OPENBLAS_NUM_THREADS": "1"}
            assert set(names) == {"PATH", "LANG", "OPENBLAS_NUM_THREADS", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                                  "REHARNESS_EGRESS"}
            assert direct != "open"
        finally:
            session.close(force=True)
        assert worker.runner_problem() is None
    finally:
        docker("network", "rm", network, check=False)


def test_the_seed_runs_its_attempt_in_the_real_image_under_ks_worker_table(tmp_path):
    """The stirrup seed's whole attempt in the qualified worker built from the preset's worker table (with the image
    K pinned): the image's runner is this host's, the transcript is the one the host's test worker writes, and the
    worker_session line names the image and K's resources."""
    image_id = qualified().image_id()
    k = core(worker={**presets.AUTOMATIONBENCH_V1["worker"], "image_id": image_id})
    worker = qualified(WorkerResources.from_core(k))
    assert worker.runner_problem() is None
    package = seed(tmp_path / "seed", k)
    script = [turn("", ("api_search", {"q": "x"})), finish("Found it.")]
    a = attempt(tmp_path / "docker", package, Script(script), k=k, worker=worker)
    b = attempt(tmp_path / "process", package, Script(script), k=k)
    assert (a.result.termination, a.result.text) == ("finished", "Found it.") and a.world.calls == b.world.calls
    assert kinds(a.lines) == kinds(b.lines)
    (session,) = of(a.lines, "worker_session")
    table = presets.AUTOMATIONBENCH_V1["worker"]
    assert session["worker"] == {**session["worker"], "kind": "DockerWorker", "qualified": True, "image_id": image_id,
                                 "memory": table["memory"], "pids": table["pids"], "tmpfs_mb": table["tmpfs_mb"],
                                 "memory_swap": table["memory_swap"], "network": "none"}
