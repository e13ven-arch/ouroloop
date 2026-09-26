"""Cloud GPU workers (evolve/cloud.py): a RunPod or Vast.ai instance per training job, against fakes of both APIs
(httpx.MockTransport) and a stand-in for ssh and rsync. Nothing reaches a provider, a host or docker."""
import json
import re
import subprocess
from pathlib import Path

import httpx
import pytest

from ouroloop import config
from ouroloop.evolve import cloud
from ouroloop.evolve.compute import SEP
from ouroloop.evolve.gate import freeze_template, records
from ouroloop.evolve.pipeline import evolve_model
from ouroloop.evolve.train import DEFAULT_RECIPE, JobTrainer, TrainError, TrainJob
from ouroloop.release import code_version
from test_gate_record import ACT_GATE, CFG, collect

pytestmark = pytest.mark.usefixtures("loose_risk")

RUNPOD_KEY, VAST_KEY, HF_TOKEN = "rpa_SECRET_RUNPOD_KEY", "vast_SECRET_VAST_KEY", "hf_SECRET_HF_TOKEN"
GPU_RP, GPU_VAST = "NVIDIA GeForce RTX 4090", "RTX 4090"
PREFIX = "org/ouro-"
COMMIT = "a" * 40
MODELS = {"jobs_root": "~/.ouroloop/jobs", "hf_prefix": PREFIX}   # models load from the Hub here


@pytest.fixture
def keys(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNPOD_API_KEY", RUNPOD_KEY)
    monkeypatch.setenv("VAST_API_KEY", VAST_KEY)
    monkeypatch.setenv("HF_TOKEN", HF_TOKEN)
    key = tmp_path / "sshkey"
    key.write_text("PRIVATE")
    (tmp_path / "sshkey.pub").write_text("ssh-ed25519 AAAAtest ouroloop@test\n")
    return str(key)


class Api:
    """A provider's API, faked: every request recorded (method, path, query, Authorization, body)."""

    def __init__(self):
        self.requests, self.fail = [], {}          # fail: (method, path prefix) -> (status, body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, dict(request.url.params),
                              request.headers.get("authorization"), body))
        for (method, prefix), (status, out) in self.fail.items():
            if request.method == method and request.url.path.startswith(prefix):
                return httpx.Response(status, json=out)
        return self.handle(request.method, request.url.path, dict(request.url.params), body)

    def transport(self):
        return httpx.MockTransport(self)

    def calls(self, method, prefix=""):
        return [r for r in self.requests if r[0] == method and r[1].startswith(prefix)]


class RunPodApi(Api):
    """Pods boot PROVISIONING -> STARTING -> RUNNING, one GET at a time (never, with `boots` False); a RUNNING pod
    has ssh.direct."""

    def __init__(self, pods=(), boots=True, price=0.69):
        super().__init__()
        self.pods = {p["id"]: dict(p) for p in pods}
        self.boots, self.price, self.n = boots, price, 0

    def handle(self, method, path, params, body):
        if method == "GET" and path == "/v2/catalog/gpus":
            return httpx.Response(200, json=[{"id": GPU_RP, "name": "RTX 4090", "memory": 24,
                                              "price": {"secure": self.price, "community": 0.34}, "maxCount": 8,
                                              "availability": "HIGH"}])
        if method == "POST" and path == "/v2/pods":
            self.n += 1
            pod = {**body, "id": f"pod{self.n}", "status": "PROVISIONING", "cost": 0, "ssh": None}
            self.pods[pod["id"]] = pod
            return httpx.Response(201, json=pod)
        if method == "GET" and path == "/v2/pods":
            live = [p for p in self.pods.values() if p["status"] != "TERMINATED"]
            return httpx.Response(200, json={"pods": live, "pagination": {"nextCursor": None, "hasNextPage": False}})
        m = re.fullmatch(r"/v2/pods/(\w+)", path)
        if m and m.group(1) in self.pods:
            pod = self.pods[m.group(1)]
            if method == "DELETE":
                pod["status"] = "TERMINATED"
                return httpx.Response(204)
            if self.boots and pod["status"] != "TERMINATED":
                nxt = {"PROVISIONING": "STARTING", "STARTING": "RUNNING"}.get(pod["status"], pod["status"])
                pod["status"] = nxt
                if nxt == "RUNNING":
                    pod.update(cost=self.price * pod["gpu"]["count"],
                               ssh={"proxy": None, "direct": {"host": "203.0.113.7", "port": 34446,
                                                              "username": "root"}})
            return httpx.Response(200, json=pod)
        return httpx.Response(404, json={"title": "Not Found", "status": 404})


class VastApi(Api):
    """Offers searched by price; instances load, then run with a direct ssh port. Listing pages 25 at a time."""

    def __init__(self, offers=None, instances=(), taken=()):
        super().__init__()
        self.offers = offers if offers is not None else [
            {"id": 11, "gpu_name": GPU_VAST, "num_gpus": 1, "dph_total": 0.42, "reliability": 0.99},
            {"id": 12, "gpu_name": GPU_VAST, "num_gpus": 1, "dph_total": 0.55, "reliability": 0.99}]
        self.instances = {str(i["id"]): dict(i) for i in instances}
        self.taken, self.n = set(taken), 7000

    def handle(self, method, path, params, body):
        if method == "POST" and path == "/api/v0/bundles/":
            cap = body["dph_total"]["lte"]
            return httpx.Response(200, json={"offers": [o for o in self.offers if o["dph_total"] <= cap]})
        m = re.fullmatch(r"/api/v0/asks/(\d+)/", path)
        if method == "PUT" and m:
            if int(m.group(1)) in self.taken:
                return httpx.Response(404, json={"success": False, "error": "no_such_ask"})
            self.n += 1
            self.instances[str(self.n)] = {"id": self.n, "label": body["label"], "actual_status": None,
                                           "public_ipaddr": "198.51.100.9 ", "ssh_host": "ssh5.vast.ai",
                                           "ssh_port": 20022, "ports": {}}
            return httpx.Response(200, json={"success": True, "new_contract": self.n})
        if method == "GET" and path == "/api/v1/instances/":
            rows = sorted(self.instances.values(), key=lambda i: i["id"])
            start = int(params.get("after_token") or 0)
            page = rows[start:start + int(params["limit"])]
            token = str(start + len(page)) if start + len(page) < len(rows) else None
            return httpx.Response(200, json={"instances": page, "next_token": token, "total_instances": len(rows)})
        m = re.fullmatch(r"/api/v0/instances/(\d+)/(ssh/)?", path)
        if m:
            inst = self.instances.get(m.group(1))
            if method == "POST" and m.group(2):
                return httpx.Response(200, json={"success": True})
            if method == "DELETE":
                self.instances.pop(m.group(1), None)
                return httpx.Response(200, json={"success": True})
            if method == "GET":
                if inst and inst["actual_status"] != "running":
                    inst["actual_status"] = "loading" if inst["actual_status"] is None else "running"
                    if inst["actual_status"] == "running":
                        inst["ports"] = {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40022"}]}
                return httpx.Response(200, json={"instances": inst})
        return httpx.Response(404, json={"success": False, "error": "not_found"})


class Ssh:
    """ssh and rsync to a new instance whose home is /root. The first probe fails (sshd still starting). host.json is
    this ouroloop's (the worker image) unless `image` is "plain": then only after bootstrap.sh ran. `job` says how the
    job goes: "ok", "fail" (exit 1), "hang" (never exits), "interrupt" (^C while it is polled)."""

    def __init__(self, job="ok", image="worker"):
        self.commands, self.pushed, self.job, self.image = [], {}, job, image
        self.probes, self.bootstrapped, self.id = 0, False, None

    def manifest(self):
        return {"ouroloop": {"code": code_version()}, "plugins": {"signature": {"source": None}},
                "hf": {"hub": True, "token": True}}

    def __call__(self, args, input=None, timeout=None):
        self.commands.append(args)
        out = ""
        if args[0] == "rsync":
            local = Path(args[-2])
            if local.is_file():
                self.pushed[local.name] = local.read_text()
            return subprocess.CompletedProcess(args, 0, "", "")
        command = args[-1]
        if command == "true":
            self.probes += 1
            if self.probes == 1:
                raise subprocess.CalledProcessError(255, args, stderr="ssh: connect to host: Connection refused")
        elif "/.ouroloop/host.json" in command:
            out = json.dumps(self.manifest()) if self.image == "worker" or self.bootstrapped else ""
        elif "bootstrap.sh" in command:
            self.bootstrapped = True
            out = "bootstrap: done\n"
        elif 'echo "$HOME"' in command:
            out = "/root\n"
        elif command.startswith("mkdir") and "jobs" in command:
            self.id = re.search(r"(m_[0-9]{4}_[0-9a-f]+)", command).group(1)
        elif "ps -ww -o args=" in command and "echo exited" in command:
            if self.job == "interrupt":
                raise KeyboardInterrupt
            out = "running\n" if self.job == "hang" else "exited\n"
        elif command.startswith("cd") and SEP in command:
            code = 1 if self.job == "fail" else 0
            result = {"model": f"{PREFIX}{self.id}@{COMMIT}", "plugin": {"name": "signature", "version": None},
                      "hf_commit": COMMIT}
            out = f"{code}\n{SEP}\n{json.dumps(result) if not code else ''}\n\n{SEP}\nworker: the end\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    def shell(self) -> str:
        return " ".join(" ".join(a) for a in self.commands)


def a_job(root, version="m_0001") -> TrainJob:
    data = Path(root) / "models" / version / "data"
    data.mkdir(parents=True)
    example = {"id": "e", "state": "say yes", "question": "done?", "candidates": [{"name": "yes"}, {"name": "no"}],
               "target": [1.0, 0.0], "meta": {"weight": 1.0}}
    (data / "train.jsonl").write_text(json.dumps(example) + "\n")
    (data / "val.jsonl").write_text(json.dumps(example) + "\n")
    return TrainJob(version, data, dict(DEFAULT_RECIPE), None)


def options(provider, key, **kw):
    gpu = GPU_RP if provider == "runpod" else GPU_VAST
    return {"provider": provider, "image": "ghcr.io/org/ouroloop-worker:1", "gpu": gpu, "max_price": 1.0,
            "ssh_key": key, "boot_poll_s": 10, **kw}


def trainer(cloud_options, api, ssh, **kw) -> JobTrainer:
    return JobTrainer("signature", cloud=cloud_options, api=api.transport(), runner=ssh, transport="hf",
                      hf_prefix=PREFIX, models=MODELS, poll_s=0, sleep=lambda s: None, **kw)


def ws_tag(ws) -> str:
    return cloud.tag(str(Path(ws).resolve()))


# ---------------------------------------------------------------- the lifecycle
@pytest.mark.parametrize("provider", ["runpod", "vast"])
def test_a_job_runs_on_a_new_instance_which_is_stopped_after_it(tmp_path, keys, capsys, provider):
    api = RunPodApi() if provider == "runpod" else VastApi()
    ssh = Ssh()
    tr = trainer(options(provider, keys), api, ssh)
    result = tr.train(a_job(tmp_path / "ws"))
    job = result.job
    assert result.model_id == f"{PREFIX}{job['id']}@{COMMIT}"
    rec = job["cloud"]
    if provider == "runpod":
        (create,) = api.calls("POST", "/v2/pods")
        body = create[4]
        assert body["name"] == f"{ws_tag(tmp_path / 'ws')}-{job['id']}" and body["ports"] == ["22/tcp"]
        assert body["gpu"] == {"id": GPU_RP, "count": 1} and body["cloud"] == "SECURE" and body["image"].endswith(":1")
        assert body["entrypoint"][:2] == ["sh", "-c"] and "ouroloop-start" in body["entrypoint"][2]
        assert rec == {"provider": "runpod", "instance": "pod1", "gpu": f"1 x {GPU_RP}", "price_per_hour": 0.69,
                       "uptime_s": rec["uptime_s"]}
        assert api.pods["pod1"]["status"] == "TERMINATED" and api.calls("DELETE", "/v2/pods/pod1")
        assert job["host"] == "root@203.0.113.7"
        port = "34446"
    else:
        (search,) = api.calls("POST", "/api/v0/bundles/")
        assert search[4]["dph_total"] == {"lte": 1.0} and search[4]["gpu_name"] == {"in": [GPU_VAST]}
        assert search[4]["allocated_storage"] == 50 and search[4]["direct_port_count"] == {"gte": 1}
        (create,) = api.calls("PUT", "/api/v0/asks/")
        body = create[4]
        assert create[1] == "/api/v0/asks/11/" and body["label"] == f"{ws_tag(tmp_path / 'ws')}-{job['id']}"
        assert body["runtype"] == "ssh_direct" and body["env"]["OUROLOOP_SSHD"] == "0"
        assert "ouroloop-start" in body["onstart"]
        (attach,) = api.calls("POST", "/api/v0/instances/7001/ssh/")
        assert attach[4] == {"ssh_key": "ssh-ed25519 AAAAtest ouroloop@test"}
        assert rec == {"provider": "vast", "instance": "7001", "gpu": f"1 x {GPU_VAST}", "price_per_hour": 0.42,
                       "uptime_s": rec["uptime_s"]}
        assert "7001" not in api.instances and api.calls("DELETE", "/api/v0/instances/7001/")
        assert job["host"] == "root@198.51.100.9"
        port = "40022"
    assert isinstance(rec["uptime_s"], float)
    # HF_TOKEN reaches the instance as its env var, with the job's public key, and nothing else of this machine
    assert body["env"]["HF_TOKEN"] == HF_TOKEN and body["env"]["PUBLIC_KEY"].startswith("ssh-ed25519 AAAAtest")
    # every request carried the key in its header, and only there
    assert {r[3] for r in api.requests} == {f"Bearer {RUNPOD_KEY if provider == 'runpod' else VAST_KEY}"}
    # ssh waited for sshd, then reached the instance on its port, with the key, pinning its host key for this job
    assert ssh.probes == 2
    ssh_args = [a for a in ssh.commands if a[0] == "ssh"]
    for a in ssh_args:
        assert a[a.index("-p") + 1] == port and a[a.index("-i") + 1] == keys
        assert "StrictHostKeyChecking=accept-new" in a
    assert tr.host is None and tr.compute.host is None          # the next job gets an instance of its own
    # the instance was stopped after the job, the last thing done
    last = api.requests[-1]
    assert last[0] == "DELETE"
    assert "stopped instance" in capsys.readouterr().err


def test_an_image_without_the_worker_is_initialized_before_the_job(tmp_path, keys):
    api, ssh = RunPodApi(), Ssh(image="plain")
    tr = trainer(options("runpod", keys), api, ssh)
    tr.train(a_job(tmp_path / "ws"))
    assert ssh.bootstrapped
    order = [a[-1] for a in ssh.commands if a[0] == "ssh"]
    boot = next(i for i, c in enumerate(order) if "bootstrap.sh" in c)
    launch = next(i for i, c in enumerate(order) if "setsid" in c)
    assert boot < launch and api.pods["pod1"]["status"] == "TERMINATED"


# ---------------------------------------------------------------- always stopped
@pytest.mark.parametrize("provider", ["runpod", "vast"])
@pytest.mark.parametrize("how", ["fail", "hang", "interrupt"])
def test_the_instance_is_stopped_however_the_job_ends(tmp_path, keys, provider, how):
    api = RunPodApi() if provider == "runpod" else VastApi()
    tr = trainer(options(provider, keys), api, Ssh(job=how), deadline_s=0)
    error = KeyboardInterrupt if how == "interrupt" else TrainError
    with pytest.raises(error, match={"fail": "exit 1", "hang": "deadline", "interrupt": None}[how]):
        tr.train(a_job(tmp_path / "ws"))
    if provider == "runpod":
        assert api.pods["pod1"]["status"] == "TERMINATED"
    else:
        assert api.instances == {} and api.calls("DELETE", "/api/v0/instances/7001/")
    assert tr.host is None


@pytest.mark.parametrize("provider", ["runpod", "vast"])
def test_an_instance_that_never_answers_is_stopped_at_the_boot_timeout(tmp_path, keys, provider):
    api = RunPodApi(boots=False) if provider == "runpod" else VastApi()
    ssh = Ssh()

    def refuse(args, input=None, timeout=None):     # on Vast.ai the API says running; ssh never answers
        if args[0] == "ssh" and args[-1] == "true":
            raise subprocess.CalledProcessError(255, args, stderr="Connection refused")
        return ssh(args, input, timeout)
    runner = refuse if provider == "vast" else ssh
    tr = JobTrainer("signature", cloud=options(provider, keys, boot_timeout_s=60), api=api.transport(),
                    runner=runner, transport="hf", hf_prefix=PREFIX, models=MODELS, poll_s=0,
                    sleep=lambda s: None)
    with pytest.raises(TrainError, match="did not answer ssh within 60s"):
        tr.train(a_job(tmp_path / "ws"))
    gets = [r for r in api.requests if r[0] == "GET" and re.search(r"/(pod1|7001)/?$", r[1])]
    assert len(gets) == 7                       # at 0, 10, ..., 60 s of boot_poll_s
    if provider == "runpod":
        assert api.pods["pod1"]["status"] == "TERMINATED"
    else:
        assert api.instances == {}


def test_an_interrupted_boot_stops_the_instance(tmp_path, keys):
    api = RunPodApi(boots=False)

    def interrupt(s):
        raise KeyboardInterrupt

    tr = JobTrainer("signature", cloud=options("runpod", keys), api=api.transport(), runner=Ssh(), transport="hf",
                    hf_prefix=PREFIX, models=MODELS, sleep=interrupt)
    with pytest.raises(KeyboardInterrupt):
        tr.train(a_job(tmp_path / "ws"))
    assert api.pods["pod1"]["status"] == "TERMINATED"


def test_an_instance_that_fails_to_start_is_a_train_error_and_stopped(tmp_path, keys):
    api = RunPodApi()
    tr = trainer(options("runpod", keys), api, Ssh())
    handle = api.handle

    def exits(method, path, params, body):
        r = handle(method, path, params, body)
        if method == "GET" and path == "/v2/pods/pod1":
            api.pods["pod1"]["status"] = "EXITED"
            return httpx.Response(200, json=api.pods["pod1"])
        return r
    api.handle = exits
    with pytest.raises(TrainError, match="EXITED and will not start"):
        tr.train(a_job(tmp_path / "ws"))
    assert api.calls("DELETE", "/v2/pods/pod1")


# ---------------------------------------------------------------- leftovers
@pytest.mark.parametrize("provider", ["runpod", "vast"])
def test_the_first_job_stops_only_this_workspaces_leftover_instances(tmp_path, keys, provider, capsys):
    ws, other = tmp_path / "ws", cloud.tag("/somewhere/else")
    mine = ws_tag(ws)
    if provider == "runpod":
        api = RunPodApi(pods=[
            {"id": "old1", "name": f"{mine}-m_0000_dead", "status": "RUNNING"},
            {"id": "old2", "name": f"{other}-m_0000_live", "status": "RUNNING"},
            {"id": "old3", "name": "someone-elses-pod", "status": "RUNNING"},
            {"id": "old4", "name": f"{mine}x-m_0000_near", "status": "RUNNING"}])
    else:
        filler = [{"id": 100 + i, "label": f"{other}-m_{i:04d}", "actual_status": "running"} for i in range(30)]
        api = VastApi(instances=[*filler, {"id": 900, "label": f"{mine}-m_0000_dead", "actual_status": "running"},
                                 {"id": 901, "label": None, "actual_status": "running"}])
    tr = trainer(options(provider, keys), api, Ssh())
    tr.train(a_job(ws))
    tr.train(a_job(ws, "m_0002"))           # once per process: no second sweep
    deleted = sorted(r[1] for r in api.calls("DELETE"))
    if provider == "runpod":
        assert deleted == ["/v2/pods/old1", "/v2/pods/pod1", "/v2/pods/pod2"]
        assert all(api.pods[i]["status"] == "RUNNING" for i in ("old2", "old3", "old4"))
        assert len([r for r in api.calls("GET", "/v2/pods") if r[1] == "/v2/pods"]) == 3   # a sweep, 2 job checks
    else:
        assert deleted == ["/api/v0/instances/7001/", "/api/v0/instances/7002/", "/api/v0/instances/900/"]
        assert len(api.instances) == 31 and "900" not in api.instances
        pages = [r for r in api.calls("GET", "/api/v1/instances/")]
        assert pages[1][2]["after_token"] == "25"                    # the list was read to its end
    assert "left running by an earlier run of this workspace" in capsys.readouterr().err


def test_a_create_whose_answer_was_lost_is_stopped_too(tmp_path, keys):
    """A 5xx after the pod was made: the create is retried (the docs' advice), and both pods are stopped."""
    api = RunPodApi()
    handle, state = api.handle, {"n": 0}

    def flaky(method, path, params, body):
        r = handle(method, path, params, body)
        if method == "POST" and path == "/v2/pods":
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(502, json={"title": "Bad Gateway"})
        return r
    api.handle = flaky
    trainer(options("runpod", keys), api, Ssh()).train(a_job(tmp_path / "ws"))
    assert sorted(p["status"] for p in api.pods.values()) == ["TERMINATED", "TERMINATED"]


# ---------------------------------------------------------------- refused before anything is used
def test_a_cloud_trainer_refuses_transport_direct_and_bad_options(keys):
    base = {"models": {"url": "http://gpu:8765"}}
    with pytest.raises(ValueError, match='needs transport "hf"'):
        JobTrainer("signature", cloud=options("runpod", keys), **base)
    with pytest.raises(ValueError, match="one or the other"):
        JobTrainer("signature", cloud=options("runpod", keys), host="u@gpu", transport="hf", hf_prefix=PREFIX, **base)
    with pytest.raises(ValueError, match="needs max_price"):
        JobTrainer("signature", cloud={**options("vast", keys), "max_price": None}, transport="hf", hf_prefix=PREFIX,
                   **base)
    with pytest.raises(ValueError, match="may only set"):
        JobTrainer("signature", cloud={**options("runpod", keys), "api_key": "x"}, transport="hf", hf_prefix=PREFIX,
                   **base)
    with pytest.raises(ValueError, match="provider"):
        JobTrainer("signature", cloud={"provider": "aws"}, transport="hf", hf_prefix=PREFIX, **base)
    # built from the config: [trainer.cloud]
    tr = config.build_trainer({"type": "signature", "transport": "hf", "hf_prefix": PREFIX,
                               "cloud": options("vast", keys)}, MODELS)
    assert tr.cloud["provider"] == "vast" and tr.python == "~/.ouroloop/venv/bin/python"


@pytest.mark.parametrize("provider", ["runpod", "vast"])
def test_no_offer_under_the_price_leaves_the_round_inconclusive_before_anything_is_used(tmp_path, keys, provider):
    api = RunPodApi(price=2.5) if provider == "runpod" else VastApi()
    rt = config.build_runtime({**CFG, "models": dict(MODELS)}, tmp_path)
    collect(rt, 150)
    freeze_template(rt.root, ACT_GATE)
    ssh = Ssh()
    tr = trainer(options(provider, keys, max_price=0.3), api, ssh)
    card = evolve_model(rt, tr)
    (record,) = records(rt.root)
    assert card["gate"] == "inconclusive" and not record.used and "model_id" not in card
    assert "no offer under the price" in card["reasons"][0]
    assert not api.calls("POST", "/v2/pods") and not api.calls("PUT", "/api/v0/asks/") and ssh.commands == []


def test_a_missing_api_key_or_hf_token_is_a_train_error_before_any_instance(tmp_path, keys, monkeypatch):
    api = VastApi()
    monkeypatch.delenv("VAST_API_KEY")
    with pytest.raises(TrainError, match="VAST_API_KEY is not set"):
        trainer(options("vast", keys), api, Ssh()).train(a_job(tmp_path / "ws"))
    monkeypatch.setenv("VAST_API_KEY", VAST_KEY)
    monkeypatch.delenv("HF_TOKEN")
    with pytest.raises(TrainError, match="HF_TOKEN is not set"):
        trainer(options("vast", keys), api, Ssh()).train(a_job(tmp_path / "ws2"))
    assert not api.calls("PUT")


def test_a_taken_offer_is_passed_over_for_the_next(tmp_path, keys):
    api = VastApi(taken={11})
    result = trainer(options("vast", keys), api, Ssh()).train(a_job(tmp_path / "ws"))
    assert [r[1] for r in api.calls("PUT")] == ["/api/v0/asks/11/", "/api/v0/asks/12/"]
    assert result.job["cloud"]["price_per_hour"] == 0.55


def test_a_provider_error_names_the_status_and_never_the_key(tmp_path, keys):
    api = RunPodApi()
    api.fail[("POST", "/v2/pods")] = (402, {"title": "Payment Required", "status": 402,
                                           "detail": "insufficient balance"})
    with pytest.raises(TrainError, match="402 Payment Required: insufficient balance") as e:
        trainer(options("runpod", keys), api, Ssh()).train(a_job(tmp_path / "ws"))
    assert RUNPOD_KEY not in str(e.value)


# ---------------------------------------------------------------- no key anywhere
@pytest.mark.parametrize("provider", ["runpod", "vast"])
def test_no_key_reaches_spec_json_the_logs_the_shell_or_the_card(tmp_path, keys, capsys, provider):
    api = RunPodApi() if provider == "runpod" else VastApi()
    ssh = Ssh()
    tr = trainer(options(provider, keys), api, ssh)
    result = tr.train(a_job(tmp_path / "ws"))
    card = json.dumps({"model_id": result.model_id, "backend": result.backend, "job": result.job})  # the card's part
    assert result.job["cloud"]["provider"] == provider and set(ssh.pushed) == {"spec.json", "train.jsonl", "val.jsonl"}
    out = capsys.readouterr()
    for secret in (RUNPOD_KEY, VAST_KEY, HF_TOKEN):
        assert secret not in ssh.pushed["spec.json"] and secret not in card
        assert secret not in out.out + out.err and secret not in ssh.shell()
    assert "cloud" not in json.loads(ssh.pushed["spec.json"]).get("options", {})
