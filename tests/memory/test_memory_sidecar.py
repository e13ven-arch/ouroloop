"""The memory sidecar on the real path (docs/design/MEMORY.md §2, §4, §9, §11 "sidecar").

ReMe 0.3.1.10's own HttpService in the memory venv, chroma on disk, bge-small on onnxruntime; only the LLM is scripted
(tests/memory/fake_openai_server.py in the research scope's place). Here:

- the whole chain: summarize -> the platform's checks -> add -> filtered retrieve -> retire -> dump -> SIGTERM ->
  restart -> load, the retrieval after the reload the one before, and LibraryReader over the snapshot;
- K's pins: a lock, flows, ReMe version or wheel that differs refuses to start (exit 3); health reports the pins and
  the host takes a sidecar only with them;
- one store, one writer (the exclusive lock), a start that fails leaves nothing running, the sidecar exits when its
  host dies, a working dir inside the repository is refused;
- every route needs the token, a flow ReMe fails is HTTP 500, loopback only, no CORS, no inherited secret or .env,
  ReMe's own logs hold no request, no bytecode is written into the package's sidecar dir;
- summarize's LLM calls go through the scope, each recorded (call id, op); a sidecar without a scope summarises
  nothing and still retrieves;
- snapshots: dump and load check row counts, a snapshot is loaded once however many reads start at once, reads never
  see a reload half done, a read naming another count fails (SHORT) and LibraryReader reloads once, collections
  beyond eight go least recently used first;
- the host/sidecar boundary: a host that used the sidecar has none of ReMe's stack loaded.

Every test that starts a sidecar is slow (the full run); the static and host-only checks run in the default suite.
"""
from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from fake_openai_server import FakeOpenAI
from memory_support import (HASH, REPO, TASKS, TESTS, conversation, fake, items, live_of, scope_of,  # noqa: F401
                            settings, sidecar, stamped, stream, venv)
from ouroloop.contracts import MemorySidecarError, TrainOnlyError
from ouroloop.memory import (COLLECTION_NAME, LOCK, MODEL_AUX_SHA256, MODEL_FILE, MODEL_SHA256, SIDECAR_DIR,
                             flows_sha256, lock_sha256)
from ouroloop.memory.client import SHORT, MemoryClient, canonical_rows, parse_jsonl
from ouroloop.memory.retrieval import LibraryReader, snapshot_collection
from ouroloop.memory.rules import snapshot_rows
from ouroloop.memory.settings import Settings
from ouroloop.memory.snapshot import fit_snapshot

FLOWS = sorted(["summary_task_memory_v6", "add_task_memory_v6", "retrieve_task_memory_v6", "get_task_memory_v6",
                "retire_task_memory_v6", "dump_memory_v6", "load_memory_v6"])
WHERE = {"ol_line": "finance", "ol_split": "train"}


def one_success(name="T1"):
    return [{"task_id": HASH[name], "messages": conversation(name, True), "score": 1.0,
             "metadata": {"query": TASKS[name]}}]


def reader(client, *, snapshot_text=None, live=None, line="finance", **over) -> LibraryReader:
    return LibraryReader(client, Settings(**over), business_line=line, snapshot_text=snapshot_text, live=live)


@pytest.fixture(scope="module")
def shared(venv, tmp_path_factory):
    """One sidecar and one scripted bridge for the tests that need no special start."""
    root = tmp_path_factory.mktemp("sidecar")
    with FakeOpenAI() as f:
        c = sidecar(root, venv, llm=scope_of(f))
        try:
            yield c, f
        finally:
            c.stop()


# ---------------------------------------------------------------------------------------------- the whole chain
@pytest.mark.slow
def test_the_whole_chain_survives_a_sigterm_restart(venv, fake, tmp_path):
    client = sidecar(tmp_path, venv, llm=scope_of(fake))
    try:
        step = stream(client, fake, tmp_path)
        done = step.process_batch(items(("T1", "T3")))
        added = done[0]["added"] + done[1]["added"]
        assert len(added) >= 3 and all(d["validated"] >= 3 for d in done)
        assert {c["op"] for c in fake.calls()} == {"success", "failure", "comparative", "validation"}
        # filtered retrieval: this line's train entries only, the similarity kept beside ReMe's score
        got = client.retrieve(TASKS["T5"], step.collection, where=WHERE, top_k=10)
        assert got and all(m["metadata"]["ol_line"] == "finance" for m in got)
        assert [m["metadata"]["retrieval_score"] for m in got] == sorted(
            (m["metadata"]["retrieval_score"] for m in got), reverse=True)
        assert client.retrieve(TASKS["T5"], step.collection, where={"ol_line": "sales"}, top_k=10) == []
        # retire one by id: it is gone from the store and from retrieval, the others stay
        gone = got[-1]["memory_id"]
        assert client.retire([gone], step.collection) == [gone]
        before = client.retrieve(TASKS["T5"], step.collection, where=WHERE, top_k=10)
        assert gone not in [m["memory_id"] for m in before] and len(before) == len(got) - 1
        dump = client.dump(step.collection)
        assert len(parse_jsonl(dump)) == len(added) - 1 and "vector" not in dump
        # SIGTERM: uvicorn's lifespan closes ReMe's application; a new process on the same chroma
        client.stop()
        assert "Application shutdown complete" in (client.workdir / "sidecar.log").read_text()
        client.restart()
        assert client.count(step.collection) == len(added) - 1
        after_live = client.retrieve(TASKS["T5"], step.collection, where=WHERE, top_k=10)
        # load the dump into a snapshot collection: the same retrieval, row for row
        snap = snapshot_collection(dump, step.s.retriever)
        assert client.load(dump, snap) == len(added) - 1
        after_snap = client.retrieve(TASKS["T5"], snap, where=WHERE, top_k=10)
        key = [(m["memory_id"], round(m["metadata"]["retrieval_score"], 5)) for m in before]
        assert [(m["memory_id"], round(m["metadata"]["retrieval_score"], 5)) for m in after_live] == key
        assert [(m["memory_id"], round(m["metadata"]["retrieval_score"], 5)) for m in after_snap] == key
        # and the platform's read path over the snapshot finds the same entries, with the opening block
        tm = LibraryReader(client, step.s, business_line="finance", snapshot_text=dump).for_task(TASKS["T5"],
                                                                                               HASH["T5"])
        ids = {k for k, _ in key}
        assert tm.event["collection"] == snap and tm.event["shown"] and set(tm.event["shown"]) <= ids
        assert all(f"[lib {m[:8]}]" in tm.opening for m in tm.event["shown"])
        assert {r["id"] for r in tm.recall(TASKS["T5"])} <= {m[:8] for m in ids}
    finally:
        client.stop()


@pytest.mark.slow
def test_summarize_calls_go_through_the_scope_and_each_is_recorded(shared):
    """Each LLM call ReMe makes for a summary goes to the research scope the sidecar was started with, and the
    summary reports it: the op, the scope's call id (the completion's id), finished, the reply's JSON state."""
    client, f = shared
    f.set_step("through-the-scope")
    got = client.summarize([one_success()[0], {**one_success()[0], "messages": conversation("T1", False),
                                               "score": 0.3}])
    calls = f.calls("through-the-scope")
    assert calls and all(c["status"] == 200 for c in calls)
    assert sorted((r["call_id"], r["op"]) for r in got["replies"]) == sorted((c["call_id"], c["op"]) for c in calls)
    assert {r["op"] for r in got["replies"]} == {"success", "failure", "comparative", "validation"}
    assert all(r["done"] and r["json"] == "block" for r in got["replies"]) and got["unparsed"] == []
    assert got["validated"] == sum(1 for r in got["replies"] if r["op"] == "validation")
    assert got["memory_list"] and all(m["author"].startswith("research/reme:") for m in got["memory_list"])


@pytest.mark.slow
def test_every_route_needs_the_token_and_a_failed_flow_is_http_500(shared):
    client, _ = shared
    base = client.base_url
    for method, path in (("GET", "/ol/health"), ("GET", "/health"), ("GET", "/docs"), ("GET", "/openapi.json"),
                         ("POST", "/retrieve_task_memory_v6"), ("POST", "/v1/embeddings")):
        r = httpx.request(method, base + path, json={}, trust_env=False)
        assert r.status_code == 401, (path, r.status_code)
        r = httpx.request(method, base + path, json={}, headers={"Authorization": "Bearer wrong"}, trust_env=False)
        assert r.status_code == 401, (path, r.status_code)
    auth = {"Authorization": f"Bearer {client._token}"}
    assert httpx.get(base + "/health", headers=auth, trust_env=False).json() == {"status": "healthy"}
    # a summary ReMe fails inside (its preprocess rejects the trajectory, the root op swallows it, the flow answers
    # success=false): HTTP 500, not 200
    bad = {"trajectories": [{"task_id": "t", "messages": [{"role": "assistant", "content": None}], "score": 1}]}
    r = httpx.post(base + "/summary_task_memory_v6", json=bad, headers=auth, trust_env=False, timeout=60)
    assert r.status_code == 500 and r.json()["success"] is False
    assert "TrajectoryPreprocess" in r.json()["answer"]
    # an op that raises (no collection, a bad one) is a 500 too, with the reason
    r = httpx.post(base + "/get_task_memory_v6", json={"ids": ["x"]}, headers=auth, trust_env=False)
    assert r.status_code in (422, 500)
    r = httpx.post(base + "/get_task_memory_v6", json={"ids": ["x"], "collection": "-bad-"}, headers=auth,
                   trust_env=False)
    assert r.status_code == 500 and "collection" in r.json()["answer"]
    # a request may not override what K pins for the summary
    r = httpx.post(base + "/summary_task_memory_v6", headers=auth, trust_env=False,
                   json={"trajectories": [], "validation_threshold": 0.1})
    assert r.status_code == 500 and "fixed by K" in r.json()["answer"]
    # the client reads any of those as an infrastructure failure, never as an empty result
    with pytest.raises(MemorySidecarError, match="HTTP 500"):
        client._call("POST", "/get_task_memory_v6", {"ids": ["x"], "collection": "-bad-"})
    with pytest.raises(ValueError, match="not ol-sys"):
        client.get(["x"], "ol-sys-default")


# ---------------------------------------------------------------------------------------------- K's pins
@pytest.mark.slow
def test_health_reports_ks_pins(shared):
    client, f = shared
    h = client.health()
    assert h["reme_version"] == "0.3.1.10" and h["lock_sha256"] == lock_sha256() == client.settings["backend"][
        "lock_sha256"]
    assert h["flows_sha256"] == flows_sha256() == client.settings["backend"]["flows_sha256"]
    assert h["model_sha256"] == MODEL_SHA256 == client.settings["retriever"]["sha256"] and h["llm"] == "research"
    assert h["model_files"] == {MODEL_FILE: MODEL_SHA256, **MODEL_AUX_SHA256}
    assert h["flows"] == FLOWS and h["pid"] == client.proc.pid
    cfg = json.loads((client.workdir / "reme_config.yaml").read_text())
    assert "api_key" not in json.dumps(cfg["llms"]) and f.token not in (client.workdir / "reme_config.yaml").read_text()
    assert cfg["http"]["host"] == "127.0.0.1"
    assert client._token not in (client.workdir / "sidecar.json").read_text()   # the config file holds no secret


def test_the_host_takes_a_sidecar_only_with_ks_pins():
    s = Settings().to_json()
    good = {"lock_sha256": s["backend"]["lock_sha256"], "flows_sha256": s["backend"]["flows_sha256"],
            "reme_version": s["backend"]["version"], "model_sha256": s["retriever"]["sha256"]}
    MemoryClient._check_health(dict(good), s)
    for key in good:
        with pytest.raises(MemorySidecarError, match=f"the sidecar is not K's: {key} 'x', K says"):
            MemoryClient._check_health({**good, key: "x"}, s)


@pytest.mark.slow
@pytest.mark.parametrize("key,value,why", [
    ("lock_sha256", "0" * 64, "library.backend.lock_sha256 is '" + "0" * 64),
    ("flows_sha256", "1" * 64, "library.backend.flows_sha256 is '" + "1" * 64),
    ("version", "0.3.1.9", "library.backend.version is '0.3.1.9'; the venv has reme-ai 0.3.1.10"),
    ("wheel_sha256", "2" * 64, "is not the lock's reme-ai wheel"),
    ("kind", "mem0", "library.backend.kind 'mem0', not 'reme'"),
])
def test_a_sidecar_that_is_not_ks_backend_refuses_to_start(venv, tmp_path, key, value, why):
    s = Settings().to_json()
    s["backend"] = {**s["backend"], key: value}
    c = MemoryClient()
    with pytest.raises(MemorySidecarError, match=r"exited with 3(.|\n)*K's library.backend is not this sidecar: "
                                                 r"(.|\n)*" + why.replace("(", r"\(").replace(".", r"\.")):
        c.start(venv=venv, workdir=tmp_path / "run", chroma_path=tmp_path / "chroma", settings=s, timeout=60)
    assert c.proc.returncode == 3 and c._http is None


@pytest.mark.slow
def test_a_lock_file_that_is_not_ks_refuses_to_start(venv, tmp_path):
    """The lock the sidecar runs against is K's by its sha256: one changed byte (a comment, every pin the same) is
    another lock; one changed pin is a venv that is not the lock."""
    s = Settings().to_json()
    comment = tmp_path / "comment.lock"
    comment.write_text(LOCK.read_text() + "# one more line\n")
    c = MemoryClient()
    with pytest.raises(MemorySidecarError, match=r"exited with 3(.|\n)*library\.backend\.lock_sha256 is '"
                                                 + s["backend"]["lock_sha256"] + r"'; comment\.lock here is "):
        c.start(venv=venv, workdir=tmp_path / "run", chroma_path=tmp_path / "chroma", settings=s, lock=comment,
                timeout=60)
    assert c.proc.returncode == 3
    pinned = tmp_path / "pinned.lock"
    text = LOCK.read_text()
    assert "\nidna==3." in text
    pinned.write_text(text.replace("\nidna==3.", "\nidna==2.", 1))
    with pytest.raises(MemorySidecarError, match=r"exited with 3(.|\n)*the venv .* is not the lock pinned\.lock: "
                                                 r"idna: 3\.\S+ installed, 2\.\S+ pinned"):
        c.start(venv=venv, workdir=tmp_path / "run", chroma_path=tmp_path / "chroma", settings=s, lock=pinned,
                timeout=60)
    assert c.proc.returncode == 3


# ---------------------------------------------------------------------------------------------- one store, one owner
@pytest.mark.slow
def test_a_second_sidecar_on_one_store_is_refused_and_leaves_nothing_running(venv, tmp_path):
    a = sidecar(tmp_path, venv, name="a")
    try:
        held = json.loads((tmp_path / "a" / "chroma" / ".ol-sidecar.lock").read_text())
        assert held["pid"] == a.proc.pid
        b = MemoryClient()
        with pytest.raises(MemorySidecarError, match=r"exited with 3(.|\n)*refused to start: .*chroma is held by "
                                                     r"another memory sidecar \(\{\"pid\": " + str(a.proc.pid)):
            b.start(venv=venv, workdir=tmp_path / "b-run", chroma_path=tmp_path / "a" / "chroma",
                    settings=Settings().to_json(), timeout=60)
        assert b.proc.poll() == 3
        assert a.health()["status"] == "ok"            # the holder is untouched
    finally:
        a.stop()
    b.start(venv=venv, workdir=tmp_path / "b-run", chroma_path=tmp_path / "a" / "chroma",
            settings=Settings().to_json(), timeout=120)     # the lock went with the process
    b.stop()


@pytest.mark.slow
def test_a_start_that_fails_after_the_process_is_up_stops_the_process(venv, tmp_path):
    class Picky(MemoryClient):
        @staticmethod
        def _check_health(h, settings):
            raise MemorySidecarError("pins differ (scripted)")
    c = Picky()
    with pytest.raises(MemorySidecarError, match="scripted"):
        c.start(venv=venv, workdir=tmp_path / "run", chroma_path=tmp_path / "chroma", settings=Settings().to_json())
    assert c.proc.poll() is not None and c._http is None
    ok = sidecar(tmp_path, venv, name="")         # the same chroma (tmp/chroma): free again
    ok.stop()


def test_a_workdir_inside_the_repository_is_refused(tmp_path):
    """Checked before anything is started or written (ReMe reads .env files above its working dir when allowed)."""
    fake_venv = tmp_path / "venv"
    (fake_venv / "bin").mkdir(parents=True)
    (fake_venv / "bin" / "python").write_text("")
    inside = REPO / "examples" / "memory-run-never"
    c = MemoryClient()
    with pytest.raises(MemorySidecarError, match="outside the repository"):
        c.start(venv=fake_venv, workdir=inside, chroma_path=tmp_path / "chroma", settings=Settings().to_json())
    assert not inside.exists() and c.proc is None
    with pytest.raises(MemorySidecarError, match="no memory venv there"):
        c.start(venv=tmp_path / "nothing", workdir=tmp_path / "run", chroma_path=tmp_path / "chroma",
                settings=Settings().to_json())


HOST = r"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import memory_support as ms
c = ms.sidecar(Path(sys.argv[2]), Path(sys.argv[3]))
print(json.dumps({"pid": c.proc.pid}), flush=True)
time.sleep(600)
"""


@pytest.mark.slow
def test_the_sidecar_exits_when_its_host_dies(venv, tmp_path):
    host = subprocess.Popen([sys.executable, "-c", HOST, str(TESTS), str(tmp_path), str(venv)], cwd=REPO,
                            env={**os.environ, "PYTHONPATH": str(REPO)}, stdout=subprocess.PIPE, text=True)
    try:
        pid = json.loads(host.stdout.readline())["pid"]
        os.kill(host.pid, signal.SIGKILL)
        host.wait(10)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            os.kill(pid, signal.SIGKILL)
            pytest.fail("the sidecar outlived its host by 30 s")
        assert "Application shutdown complete" in (tmp_path / "sc" / "run" / "sidecar.log").read_text()
    finally:
        if host.poll() is None:
            host.kill()
    again = sidecar(tmp_path, venv)       # its store is free for the next host
    again.stop()


# ---------------------------------------------------------------------------------------------- what it can reach
@pytest.mark.slow
def test_no_dotenv_no_inherited_secret_and_no_proxy_reach_the_sidecar(venv, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OL_SENTINEL_LEAK=from-dotenv\nOPENAI_API_KEY=sk-from-dotenv\n")
    for k, v in (("HTTPS_PROXY", "http://proxy.invalid:1"), ("HF_TOKEN", "hf_parent"), ("OPENAI_API_KEY", "sk-x"),
                 ("PYTHONPATH", "/nonexistent"), ("OL_SENTINEL_PARENT", "1")):
        monkeypatch.setenv(k, v)
    client = sidecar(tmp_path, venv, name="")      # workdir tmp/run: the .env is one level above it
    try:
        h = client.health()
        keys = set(h["env_keys"])
        assert not keys & {"OL_SENTINEL_LEAK", "OL_SENTINEL_PARENT", "HTTPS_PROXY", "HF_TOKEN", "OPENAI_API_KEY",
                           "PYTHONPATH", "http_proxy", "https_proxy", "OL_LLM_TOKEN"}
        assert {"HF_HUB_OFFLINE", "HF_HUB_DISABLE_IMPLICIT_TOKEN", "ANONYMIZED_TELEMETRY", "OL_MEMORY_TOKEN",
                "OL_PARENT_PID", "PYTHONDONTWRITEBYTECODE"} <= keys
        # and no proxy from the OS either: httpx reads macOS's proxy settings through urllib unless NO_PROXY=*
        assert h["no_proxy"] == "*" and h["llm"] == "closed"
        assert client.workdir == (tmp_path / "run").resolve()
    finally:
        client.stop()


@pytest.mark.slow
def test_loopback_only_no_cors_and_a_token_on_every_route(shared):
    client, _ = shared
    cfg = json.loads((client.workdir / "reme_config.yaml").read_text())
    assert cfg["http"]["host"] == "127.0.0.1"
    assert f"Uvicorn running on http://127.0.0.1:{client.base_url.rsplit(':', 1)[1]}" in (
        client.workdir / "sidecar.log").read_text()
    pre = httpx.options(client.base_url + "/ol/health", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}, trust_env=False)
    assert pre.status_code == 401 and "access-control-allow-origin" not in pre.headers
    got = httpx.get(client.base_url + "/ol/health", trust_env=False, headers={
        "Origin": "https://evil.example", "Authorization": f"Bearer {client._token}"})
    assert got.status_code == 200 and "access-control-allow-origin" not in got.headers


@pytest.mark.slow
def test_remes_own_log_files_hold_no_request_parameters(venv, fake, tmp_path):
    """At ReMe's INFO level its log files keep every request's parameters (the trajectories: train data) and every
    validation reply; the sidecar runs them at WARNING."""
    client = sidecar(tmp_path, venv, llm=scope_of(fake))
    try:
        step = stream(client, fake, tmp_path)
        step.process(*items(("T1",))[0])
        logs = sorted((client.workdir / "logs").glob("*.log"))
        text = "".join(p.read_text() for p in logs)
        assert logs and "| INFO |" not in text
        assert TASKS["T1"][:40] not in text and "llm_kwargs" not in text and fake.token not in text
    finally:
        client.stop()


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*")}


@pytest.mark.slow
def test_the_sidecar_writes_no_bytecode_into_the_package(venv, fake, tmp_path):
    """The sidecar runs serve.py from the package's own sidecar/ directory and imports ops and embed from it: with
    PYTHONDONTWRITEBYTECODE nothing is written there (no __pycache__ beside the checkout's sources)."""
    before = _tree(SIDECAR_DIR)
    client = sidecar(tmp_path, venv, llm=scope_of(fake))
    try:
        step = stream(client, fake, tmp_path)
        step.process(*items(("T1",))[0])
        client.retrieve(TASKS["T5"], step.collection, top_k=2)
    finally:
        client.stop()
    assert _tree(SIDECAR_DIR) == before
    assert not list(SIDECAR_DIR.rglob("*.pyc"))


# ---------------------------------------------------------------------------------------------- no research scope
@pytest.mark.slow
def test_without_a_research_scope_retrieval_works_and_a_summary_is_a_500(venv, fake, tmp_path):
    client = sidecar(tmp_path, venv, llm=None)
    try:
        assert client.health()["llm"] == "closed"
        rows = [stamped("List the invoices first, then email each customer once.",
                        "When a task asks to email customers about overdue or unpaid invoices"),
                stamped("Read the exclusion list before any write.",
                        "When a task says some contacts or accounts must be skipped or excluded", task_name="T3")]
        client.add(rows, "ol-live-nollm01")
        tm = reader(client, live=live_of(rows, "ol-live-nollm01")).for_task(TASKS["T5"], HASH["T5"])
        assert tm.event["shown"] == [rows[0]["memory_id"]] and f"[lib {rows[0]['memory_id'][:8]}]" in tm.opening
        with pytest.raises(MemorySidecarError, match=r"HTTP 500.*research/reme:success.*Connection error"):
            client.summarize(one_success())
        assert fake.calls() == []
        assert client.count("ol-live-nollm01") == 2
    finally:
        client.stop()


# ---------------------------------------------------------------------------------------------- LLM failures
@pytest.mark.slow
def test_a_failed_llm_call_fails_the_summary_as_one_request(shared):
    """ReMe's validation op turns an LLM error into "invalid" and logs it: the sidecar fails the flow (HTTP 500),
    and the openai SDK's own retries are off, so one failed bridge call is one request."""
    client, f = shared
    f.set_step("llm-fail-a")
    f.fail("validation", 500, times=1)
    with pytest.raises(MemorySidecarError, match=r"HTTP 500.*research/reme:validation"):
        client.summarize(one_success())
    f.set_step("llm-fail-b")
    f.fail("success", 502, times=1)
    with pytest.raises(MemorySidecarError):
        client.summarize(one_success("T3"))
    assert [c["status"] for c in f.calls("llm-fail-b") if c["op"] == "success"] == [502]   # no SDK retry


@pytest.mark.slow
def test_remes_requests_stream_with_tools_null(shared):
    client, f = shared
    f.set_step("requests-shape")
    client.summarize(one_success("T3"))
    calls = f.requests("requests-shape")
    assert calls and all(c["keys"] == ["messages", "model", "stream", "tools"] for c in calls)
    assert all(c["stream"] is True and c["tools"] is None for c in calls)


@pytest.mark.slow
def test_a_quota_error_remes_llm_only_warns_about_still_fails_the_summary(shared):
    """On a quota or rate-limit message ReMe's chat logs a WARNING, sleeps 60 s and returns nothing
    (base_llm.py _chat_impl), and validation then calls the memory invalid with another WARNING: no ERROR at all.
    The sidecar's own record of the call fails the flow. (ReMe's 60 s sleep.)"""
    client, f = shared
    f.fail("validation", 429, times=1, message="You exceeded your current quota (insufficient_quota)")
    with pytest.raises(MemorySidecarError, match=r"HTTP 500.*LLM call validation failed: RateLimitError"):
        client.summarize(one_success())


# ---------------------------------------------------------------------------------------------- the store's calls
@pytest.mark.slow
def test_a_sigkill_keeps_every_row(venv, tmp_path):
    client = sidecar(tmp_path, venv)
    try:
        rows = [stamped(f"Lesson {i}: check the recipients.", f"When a task asks to email customers, case {i}")
                for i in range(5)]
        client.add(rows, "ol-live-kill0001")
        client.kill()
        client.restart()
        assert client.count("ol-live-kill0001") == 5
        assert len(client.retrieve(TASKS["T1"], "ol-live-kill0001", top_k=10)) == 5
    finally:
        client.stop()


@pytest.mark.slow
def test_concurrent_writes_to_one_collection_are_serialised(shared):
    client, _ = shared
    rows = [[stamped(f"Lesson {i}-{j}: check the records.", f"When a task asks for batch {i}, item {j}")
             for j in range(4)] for i in range(8)]
    with ThreadPoolExecutor(8) as ex:        # each add checks that the collection grew by exactly its rows
        counts = list(ex.map(lambda r: client.add(r, "ol-live-concur01"), rows))
    assert client.count("ol-live-concur01") == 32 and max(counts) == 32


@pytest.mark.slow
def test_the_same_id_twice_is_refused_and_retire_removes_that_id_only(shared):
    client, _ = shared
    coll = "ol-live-sameid01"
    row = stamped("Check the recipient list against the records before the first write.",
                  "When a task asks to email customers about overdue invoices")
    other = stamped("Another lesson entirely.", "When a task asks to post totals to Slack")
    assert client.add([row], coll) == 1
    # chroma would keep the first copy silently; the client refuses a second add of an id outright
    with pytest.raises(MemorySidecarError, match="an id was already there"):
        client.add([row], coll)
    with pytest.raises(MemorySidecarError, match="duplicate ids in one call"):
        client.add([other, other], coll)
    client.add([other], coll)
    assert client.get([row["memory_id"], "0" * 16], coll)[0]["memory_id"] == row["memory_id"]
    assert client.retire([row["memory_id"], "0" * 16], coll) == [row["memory_id"]]
    assert [r["memory_id"] for r in parse_jsonl(client.dump(coll))] == [other["memory_id"]]


@pytest.mark.slow
def test_dump_is_canonical_and_dump_load_failures_are_errors(shared):
    client, _ = shared
    coll = "ol-live-dump0001"
    client.add([stamped(f"Lesson {i}.", f"When a task asks for step {i}") for i in range(4)], coll)
    one, two = client.dump(coll), client.dump(coll)
    assert one == two and one.endswith("\n") and len(one.splitlines()) == 4
    assert [json.loads(x)["memory_id"] for x in one.splitlines()] == sorted(json.loads(x)["memory_id"]
                                                                             for x in one.splitlines())
    assert client.dump("ol-live-never001") == ""                     # an empty library: an empty dump
    auth = {"Authorization": f"Bearer {client._token}"}
    r = httpx.post(client.base_url + "/dump_memory_v6", json={"collection": coll, "dump_file_path": ""},
                   headers=auth, trust_env=False)
    assert "dump_file_path is required" in r.json()["answer"]
    assert r.status_code == 500        # ReMe's DumpMemory logs an empty path at ERROR and answers success: failed
    assert httpx.post(client.base_url + "/dump_memory_v6", json={"collection": coll}, headers=auth,
                      trust_env=False).status_code == 422          # a declared field missing: the flow's own check
    # load: the collection must then hold every row (ReMe skips a line it cannot read and only logs it)
    broken = one + '{"memory_id": "broken", "content": "no memory_type"}\n'
    with pytest.raises(MemorySidecarError, match="5 rows into 0, the collection holds 4"):
        client.load(broken, "ol-snap-broken0000001")
    assert client.load(one, "ol-snap-whole00000001") == 4
    assert client.load(one, "ol-snap-whole00000001", clear=True) == 4      # emptied first: the same 4
    more = canonical_rows([stamped("One more lesson.", "When a task asks for one more step")])
    assert client.load(more, "ol-snap-whole00000001", clear=False) == 5


@pytest.mark.slow
def test_a_row_remes_dump_cannot_convert_fails_the_dump(venv, tmp_path):
    import sqlite3
    client = sidecar(tmp_path, venv)
    try:
        rows = [stamped(f"Lesson {i}.", f"When a task asks for step {i}") for i in range(3)]
        client.add(rows, "ol-live-badrow01")
        client.stop()
        db = sqlite3.connect(tmp_path / "sc" / "chroma" / "chroma.sqlite3")
        with db:
            n = db.execute("UPDATE embedding_metadata SET string_value='bogus' WHERE key='memory_type' AND id IN "
                           "(SELECT id FROM embeddings WHERE embedding_id=?)", (rows[0]["memory_id"],)).rowcount
        db.close()
        assert n == 1
        client.restart()
        assert client.count("ol-live-badrow01") == 3
        with pytest.raises(MemorySidecarError, match=r"2 rows \(2 ids\) for a collection of 3"):
            client.dump("ol-live-badrow01")      # ReMe's DumpMemory skipped the row and logged a warning only
    finally:
        client.stop()


# ---------------------------------------------------------------------------------------------- retrieval
@pytest.mark.slow
def test_retrieval_filters_line_and_split_and_leaves_the_source_out(shared):
    client, _ = shared
    coll = "ol-live-filter01"
    when = "When a task asks to email customers about overdue or unpaid invoices"
    rows = [stamped("Finance lesson.", when), stamped("Sales lesson.", when, line="sales")]
    dev = stamped("Dev lesson.", when)
    dev["metadata"]["ol_split"] = "dev"          # a row that should never exist: retrieval still does not show it
    client.add(rows + [dev], coll)
    live = live_of(rows + [dev], coll, sources={r["memory_id"]: [HASH["T1"]] for r in rows + [dev]})
    tm = reader(client, live=live, min_score=0.0).for_task(TASKS["T5"], HASH["T5"])
    assert tm.event["shown"] == [rows[0]["memory_id"]] and tm.event["filters"] == WHERE
    assert tm.event["leave_one_out"] is True and tm.event["library_size"] == 3
    tm = reader(client, live=live, min_score=0.0).for_task(TASKS["T1"], HASH["T1"])
    assert tm.event["shown"] == [] and tm.event["excluded_as_source"] == 1 and tm.opening == ""
    assert tm.recall(TASKS["T5"]) == []                                   # recall keeps the same rule
    # a dev or test task is never a source: nothing is left out for it
    tm = reader(client, live=live, min_score=0.0).for_task(TASKS["T1"], "a-dev-task")
    assert tm.event["shown"] == [rows[0]["memory_id"]]
    assert [r["id"] for r in tm.recall(when)] == [rows[0]["memory_id"][:8]]
    with pytest.raises(TrainOnlyError):
        stamped("Operations lesson.", when, line="operations")


@pytest.mark.slow
def test_leave_one_out_looks_further_down_when_the_fetch_is_used_up(shared):
    """Twelve entries sourced from the task itself rank above the two that are not: the first fetch (top_k * 5 =
    10) is all left out, so the read widens until top_k are kept."""
    client, _ = shared
    own = [stamped(f"Own lesson {i}: list the invoices and email each overdue customer once.",
                   f"When a task asks to email every customer whose invoice is overdue, case {i}")
           for i in range(12)]
    others = [stamped("Compute the totals from the sheet first and post one message with all of them.",
                      "When a task asks to post computed totals to a team chat channel", task_name="T7"),
              stamped("Read the exclusion list before any write.",
                      "When a task says some CRM contacts must be skipped", task_name="T3")]
    client.add(own + others, "ol-live-loo00001")
    live = live_of(own + others, "ol-live-loo00001", sources={r["memory_id"]: [HASH["T1"]] for r in own})
    tm = reader(client, live=live, min_score=0.0).for_task(TASKS["T1"], HASH["T1"])
    assert sorted(tm.event["shown"]) == sorted(r["memory_id"] for r in others)
    assert tm.event["fetches"] == 2 and tm.event["fetched"] == 14 and tm.event["excluded_as_source"] == 12


@pytest.mark.slow
def test_a_read_naming_its_count_fails_on_a_missing_or_short_collection(shared):
    client, _ = shared
    with pytest.raises(MemorySidecarError, match=f"HTTP 500.*{SHORT}.*does not exist; the caller expects 5"):
        client.retrieve("anything", "ol-snap-missing000001", top_k=3, expected_count=5)
    assert client.retrieve("anything", "ol-snap-missing000001", top_k=3) == []     # no count named: empty
    assert "ol-snap-missing000001" not in client.collections()                        # and a read creates nothing
    client.add([stamped(f"Lesson {i}.", f"When a task asks for step {i}") for i in range(3)], "ol-live-short001")
    with pytest.raises(MemorySidecarError, match=f"{SHORT}.*holds 3 rows; the caller expects 4"):
        client.retrieve("step", "ol-live-short001", top_k=3, expected_count=4)
    assert len(client.retrieve("step", "ol-live-short001", top_k=3, expected_count=3)) == 3
    # a live library is read with the ledger's count: a store that lost a row fails the read, never answers short
    rows = parse_jsonl(client.dump("ol-live-short001"))
    live = live_of(rows + [stamped("A lesson the store lost.", "When a task asks for step 9")], "ol-live-short001")
    with pytest.raises(MemorySidecarError, match=SHORT):
        reader(client, live=live, min_score=0.0).for_task("step 1", "a-dev-task")


@pytest.mark.slow
def test_snapshot_collections_beyond_eight_go_least_recently_used_first_and_a_short_read_reloads_once(venv,
                                                                                                    tmp_path):
    client = sidecar(tmp_path, venv)
    try:
        snaps = {i: canonical_rows([stamped(f"Snapshot lesson {i}.", f"When a task asks for case {i}")])
                 for i in range(1, 10)}
        names = {i: snapshot_collection(t, client.settings["retriever"]) for i, t in snaps.items()}
        for i in range(1, 9):
            client.load(snaps[i], names[i])
        client.retrieve("case 1", names[1], top_k=1)                 # snapshot 1 is used again: now 2 is the oldest
        client.load(snaps[9], names[9])
        held = {n for n in client.collections() if n.startswith("ol-snap-")}
        assert held == {names[i] for i in (1, 3, 4, 5, 6, 7, 8, 9)}
        # a snapshot that was dropped is loaded again on its next use
        loads = []
        real_load = client.load

        def counted(jsonl, collection, **kw):
            loads.append(collection)
            return real_load(jsonl, collection, **kw)
        client.load = counted
        tm = reader(client, snapshot_text=snaps[2], min_score=0.0).for_task("case 2", "a-dev-task")
        assert tm.event["collection"] == names[2] and len(tm.event["shown"]) == 1 and loads == [names[2]]
        assert len([n for n in client.collections() if n.startswith("ol-snap-")]) == 8
        # one this client had loaded itself, dropped behind its back: the read naming its count fails (SHORT), the
        # collection is loaded once again and read once more (never an empty library)
        extra = {i: canonical_rows([stamped(f"Snapshot lesson {i}.", f"When a task asks for case {i}")])
                 for i in range(10, 18)}
        for t in extra.values():
            real_load(t, snapshot_collection(t, client.settings["retriever"]))
        assert names[2] not in client.collections() and client._snapshots.get(names[2]) == 1
        loads.clear()
        tm = reader(client, snapshot_text=snaps[2], min_score=0.0).for_task("case 2", "a-dev-task")
        assert len(tm.event["shown"]) == 1 and client.count(names[2]) == 1 and loads == [names[2]]
        # and while it is there, a read loads nothing
        loads.clear()
        assert len(reader(client, snapshot_text=snaps[2], min_score=0.0).for_task("case 2", "x").event["shown"]) == 1
        assert loads == []
    finally:
        client.stop()


@pytest.mark.slow
def test_a_snapshots_lean_rows_load_into_reme_and_read_what_its_whole_rows_do(shared):
    """ReMe's load validates each row as a MemoryNode: a snapshot row (memory/snapshot.py snapshot_row: no author,
    score or clock, the metadata cut to the readers' keys) loads whole, and the opening, its start line and recall
    over it are those over the live library's whole rows, leave-one-out included."""
    client, _ = shared
    lessons = [("When a task asks to email customers about overdue or unpaid invoices", "List the overdue invoices "
                "and email each customer once.", "T1"),
               ("When a task asks to post computed totals to a team chat channel", "Compute the totals from the "
                "sheet first and post one message with all of them.", "T7"),
               ("When a task says some CRM contacts must be skipped", "Read the exclusion list before any write.",
                "T3"),
               ("When a task asks to reconcile two ledgers before the close", "Match the entries by amount and date, "
                "then report what is left.", "T2")]
    rows = [stamped(content, when, task_name=name) for when, content, name in lessons]
    entries = {r["memory_id"]: {"sources": [HASH[name], HASH["T4"]], "stats": {"fixes": i, "n_cited": i}}
               for r, (_w, _c, name), i in zip(rows, lessons, range(4))}
    whole = canonical_rows(snapshot_rows(rows, entries))
    lean, fit = fit_snapshot(whole, None)
    assert fit["kept"] == 4 and "author" not in lean and len(lean) < len(whole)
    readers = [reader(client, snapshot_text=t, min_score=0.0) for t in (whole, lean)]
    for query, task in ((TASKS["T5"], "a-dev-task"), (TASKS["T1"], HASH["T1"]), (TASKS["T3"], HASH["T4"])):
        got = [r.for_task(query, task) for r in readers]
        assert got[0].opening == got[1].opening and got[1].event["library_size"] == 4
        assert {k: v for k, v in got[0].event.items() if k not in ("snapshot", "collection")} == \
            {k: v for k, v in got[1].event.items() if k not in ("snapshot", "collection")}
        assert got[0].recall("invoices and totals") == got[1].recall("invoices and totals")
    assert readers[1].for_task(TASKS["T3"], HASH["T4"]).event["excluded_as_source"] > 0
    assert client.count(snapshot_collection(lean, client.settings["retriever"])) == 4


@pytest.mark.slow
def test_concurrent_first_reads_of_a_snapshot_all_read_the_whole_library_after_one_load(venv, tmp_path):
    """Eight attempts start at once on one snapshot: each must read the whole library, not an empty or half-loaded
    collection, and the snapshot is loaded once (MemoryClient.ensure_snapshot)."""
    client = sidecar(tmp_path, venv)
    try:
        rows = [stamped(f"Lesson {i}: when emailing customers about invoices, check item {i} first. " * 3,
                        f"When a task asks to email customers about overdue invoices, variant {i}")
                for i in range(300)]
        snap = canonical_rows(rows)
        other = MemoryClient.attach(client.connection())       # a second client of this process: one load still

        def one(i):
            c = client if i % 2 else other
            ev = reader(c, snapshot_text=snap, min_score=0.0).for_task("Email every customer whose invoice is overdue",
                                                                       "a-dev-task").event
            return ev["fetched"], len(ev["shown"]), ev["library_size"]
        with ThreadPoolExecutor(8) as ex:
            got = list(ex.map(one, range(16)))
        assert got == [(10, 2, 300)] * 16
        loads = [line for line in (client.workdir / "sidecar.log").read_text().splitlines()
                 if "POST /load_memory_v6" in line]
        assert len(loads) == 1
        other.stop()
        assert client.health()["status"] == "ok"                # stop() of an attached client leaves the sidecar
    finally:
        client.stop()


@pytest.mark.slow
def test_reads_never_see_a_reload_half_done(venv, tmp_path):
    """Another process (here: another client without this process's lock) reloads the collection while reads naming
    its count run: the sidecar's collection lock lets no read in between the clear and the insert."""
    client = sidecar(tmp_path, venv)
    try:
        rows = [stamped(f"Lesson {i}: check the records of case {i} first.", f"When a task asks for case {i}")
                for i in range(120)]
        snap, coll = canonical_rows(rows), "ol-snap-reloads0000001"
        client.load(snap, coll)
        rogue = MemoryClient.attach(client.connection())
        stop, errors, reads = threading.Event(), [], []

        def reload():
            while not stop.is_set():
                rogue.load(snap, coll, clear=True)

        def read():
            for _ in range(25):
                try:
                    reads.append(len(client.retrieve("case 7", coll, top_k=10, expected_count=120)))
                except Exception as e:  # noqa: BLE001 - collected and asserted below
                    errors.append(str(e))
        t = threading.Thread(target=reload)
        t.start()
        readers = [threading.Thread(target=read) for _ in range(4)]
        for r in readers:
            r.start()
        for r in readers:
            r.join()
        stop.set()
        t.join()
        assert errors == [] and reads == [10] * 100
        rogue.stop()
    finally:
        client.stop()


# ---------------------------------------------------------------------------------------------- the boundary
FORBIDDEN_STACK = ("reme", "chromadb", "onnxruntime", "agentscope", "tokenizers", "fastapi", "uvicorn", "loguru")
USED = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import memory_support as ms
import ouroloop.memory.importing, ouroloop.memory.retrieval, ouroloop.memory.track, ouroloop.memory.rules  # noqa
import ouroloop.memory.service, ouroloop.memory.snapshot, ouroloop.memory.promote, ouroloop.memory.bridge  # noqa
c = ms.sidecar(Path(sys.argv[2]), Path(sys.argv[3]))
try:
    c.embed(["a query"], query=True)
    row = ms.stamped("A lesson.", "When a task asks to email customers")
    c.add([row], "ol-live-boundary")
    assert c.retrieve(ms.TASKS["T1"], "ol-live-boundary", top_k=1)
    assert c.dump("ol-live-boundary")
finally:
    c.stop()
print(json.dumps(sorted(m for m in sys.modules if m.split(".")[0] in sys.argv[4].split(","))))
"""


@pytest.mark.slow
def test_a_host_that_used_the_sidecar_has_none_of_remes_stack_loaded(venv, tmp_path):
    run = subprocess.run([sys.executable, "-c", USED, str(TESTS), str(tmp_path), str(venv),
                          ",".join(FORBIDDEN_STACK)], cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)},
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stderr[-3000:]
    assert json.loads(run.stdout.strip().splitlines()[-1]) == []


def imports_of(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            out.add("." if node.level else (node.module or "").split(".")[0])
    return out


def test_host_modules_import_the_standard_library_httpx_and_ouroloop_only():
    host = sorted((REPO / "ouroloop" / "memory").glob("*.py"))
    allowed = set(sys.stdlib_module_names) | {"httpx", "ouroloop", "."}
    for path in host:
        assert imports_of(path) <= allowed, (path.name, sorted(imports_of(path) - allowed))
    assert {p.name for p in host} >= {"__init__.py", "__main__.py", "client.py", "rules.py", "track.py",
                                      "retrieval.py", "importing.py", "bridge.py", "service.py"}


def test_sidecar_modules_import_nothing_of_ouroloop():
    side = sorted(SIDECAR_DIR.glob("*.py"))
    assert {p.name for p in side} == {"serve.py", "ops.py", "embed.py"}
    for path in side:
        got = imports_of(path)
        assert "ouroloop" not in got and "." not in got, (path.name, sorted(got))


def test_the_collection_name_rule_and_the_short_marker_are_the_same_on_both_sides():
    tree = ast.parse((SIDECAR_DIR / "ops.py").read_text(encoding="utf-8"))
    consts = {t.id: n.value for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets
              if isinstance(t, ast.Name)}
    assert consts["COLLECTION_NAME"].args[0].value == COLLECTION_NAME.pattern
    assert consts["SHORT"].value == SHORT
