"""What the [train-jobs] tests share (docs/PLAN.md P1 step 4): stand-ins for the Hugging Face client, the RunPod and
Vast.ai APIs and ssh to a cloud instance, a cloud job, and a built dataset as train_artifact takes it. Imported by
test_jobs, test_cloud, test_hosts and test_roles; nothing here reaches a provider, a host or the network."""
import json
import re
import subprocess
from pathlib import Path

import httpx
import pytest

from ouroloop.evolve.build import DatasetBuild
from ouroloop.evolve.compute import SEP
from ouroloop.evolve.train import DEFAULT_RECIPE, TrainJob
from ouroloop.release import code_version

# huggingface_hub, stood in for: repositories and commits under $FAKE_HF; a job's Python imports it from PYTHONPATH.
FAKE_HF = """
import hashlib, json, os, shutil
ROOT = os.environ["FAKE_HF"]
class CommitOperationAdd:
    def __init__(self, path_in_repo, path_or_fileobj):
        self.path_in_repo, self.path_or_fileobj = path_in_repo, path_or_fileobj
class HfApi:
    def create_repo(self, repo_id, private=False, exist_ok=False):
        os.makedirs(os.path.join(ROOT, repo_id), exist_ok=exist_ok)
        with open(os.path.join(ROOT, repo_id, "repo.json"), "w") as f:
            json.dump({"private": private}, f)
    def create_commit(self, repo_id, operations, commit_message):
        h = hashlib.sha1(commit_message.encode())
        for op in operations:
            h.update(op.path_in_repo.encode() + open(op.path_or_fileobj, "rb").read())
        sha = h.hexdigest()
        for op in operations:
            dst = os.path.join(ROOT, repo_id, sha, op.path_in_repo)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(op.path_or_fileobj, dst)
        return type("CommitInfo", (), {"oid": sha})()
def snapshot_download(repo_id, revision):
    path = os.path.join(ROOT, repo_id, revision)
    if not os.path.isdir(path):
        raise FileNotFoundError(path)
    return path
"""


# ---------------------------------------------------------------- cloud providers and their instances
RUNPOD_KEY, VAST_KEY, HF_TOKEN = "rpa_SECRET_RUNPOD_KEY", "vast_SECRET_VAST_KEY", "hf_SECRET_HF_TOKEN"
GPU_RP, GPU_VAST = "NVIDIA GeForce RTX 4090", "RTX 4090"
PREFIX = "org/ouro-"
COMMIT = "a" * 40
STORE = {"type": "hf", "prefix": PREFIX}      # the models outlive the instance, on the Hub


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
                "stores": {"hf": {"hub": True, "token": True}}}

    def __call__(self, args, input=None, timeout=None):
        self.commands.append(args)
        out = ""
        if args[0] == "rsync":
            local = Path(args[-2])
            if local.is_file():
                self.pushed[local.name] = local.read_text()
            return subprocess.CompletedProcess(args, 0, "", "")
        command = args[-1]
        if command == 'test -f "$HOME"/.ouroloop/started':      # start.sh is done
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


# ---------------------------------------------------------------- a built dataset
QUESTION = "The task is complete"


def example(i: int, label: str, split: str = "train") -> dict:
    """One example as build_task_dataset writes it: the stop point's question over a view that says its label."""
    return {"id": f"d_{i:04d}", "source_id": f"s_{i:04d}", "dataset": "stop", "split": split, "primitive": "noul",
            "state": f"task: t{i % 7}\nsay {label}", "question": QUESTION,
            "candidates": [{"name": "yes", "description": "done"}, {"name": "no", "description": "not done"}],
            "target": [1.0, 0.0] if label == "yes" else [0.0, 1.0],
            "meta": {"weight": 1.0, "truncated": False, "task": f"t{i % 7}", "family": f"t{i % 7}"}}


def dataset(ws, n: int = 150, val: int = 15) -> DatasetBuild:
    """A ready dataset as build_task_dataset leaves it for train_artifact: train.jsonl (n examples, odd ones "yes")
    and val.jsonl in <workspace>/rsi/datasets/worker.stop, where bench._train builds it, and its label counts."""
    out = Path(ws) / "rsi" / "datasets" / "worker.stop"
    out.mkdir(parents=True, exist_ok=True)
    rows = {"train": [example(i, "yes" if i % 2 else "no") for i in range(n)],
            "val": [example(n + i, "yes" if i % 2 else "no", "val") for i in range(val)]}
    for name, examples in rows.items():
        (out / f"{name}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in examples), encoding="utf-8")
    labels = {part: {lab: sum(e["target"][k] > 0 for e in rows[name]) for k, lab in enumerate(("yes", "no"))}
              for part, name in (("fit", "train"), ("validation", "val"))}
    return DatasetBuild(status="ready", dataset_ref=f"dataset-{n}", dir=out, stats={"labels": labels})
