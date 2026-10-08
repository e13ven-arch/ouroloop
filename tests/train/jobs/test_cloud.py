"""Cloud GPU workers (ouroloop/train/jobs/cloud.py): a RunPod or Vast.ai instance per training job, against fakes of both APIs
(httpx.MockTransport) and a stand-in for ssh and rsync. Nothing reaches a provider, a host or docker."""
import json
import re
import subprocess
from pathlib import Path

import httpx
import pytest

from jobs_fixtures import (COMMIT, GPU_RP, GPU_VAST, HF_TOKEN, PREFIX, RUNPOD_KEY, STORE, VAST_KEY, RunPodApi, Ssh,
                           VastApi, a_job, dataset, keys, options)  # noqa: F401 - keys is a fixture
from ouroloop.train import config as train_config
from ouroloop.train.jobs import cloud
from ouroloop.train.train import train_artifact
from ouroloop.train.jobs.trainer import JobTrainer
from ouroloop.train.train import TrainError
from ouroloop.workspace import Workspace

def trainer(cloud_options, api, ssh, **kw) -> JobTrainer:
    return JobTrainer("signature", cloud=cloud_options, api=api.transport(), runner=ssh, store=STORE,
                      poll_s=0, sleep=lambda s: None, **kw)


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
    # the instance was stopped after the job, the last thing done but a look for others of the job
    stop = max(i for i, r in enumerate(api.requests) if r[0] == "DELETE")
    assert all(r[0] == "GET" and r[1] in ("/v2/pods", "/api/v1/instances/") for r in api.requests[stop + 1:])
    assert "stopped instance" in capsys.readouterr().err
    # its life is bounded without this process too: the boot, a host init, the deadline and a margin
    assert int(body["env"]["OUROLOOP_TTL_S"]) == 900 + 3600 + 28800 + 0 + 1800


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
        if args[0] == "ssh" and ".ouroloop/started" in args[-1]:
            raise subprocess.CalledProcessError(255, args, stderr="Connection refused")
        return ssh(args, input, timeout)
    runner = refuse if provider == "vast" else ssh
    tr = JobTrainer("signature", cloud=options(provider, keys, boot_timeout_s=60), api=api.transport(),
                    runner=runner, store=STORE, poll_s=0,
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

    tr = JobTrainer("signature", cloud=options("runpod", keys), api=api.transport(), runner=Ssh(), store=STORE,
                    sleep=interrupt)
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
    ws, other = tmp_path / "ws", cloud.tag(str(tmp_path / "else"))
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


@pytest.mark.parametrize("provider", ["runpod", "vast"])
@pytest.mark.parametrize("made", [True, False])
def test_a_create_whose_answer_was_lost_is_never_sent_twice_blindly(tmp_path, keys, provider, made):
    """A 5xx or a broken connection on a create: the instance is looked up by its name and adopted if the create made
    it; only if none is there is the create sent again. One instance, stopped after the job."""
    api = RunPodApi() if provider == "runpod" else VastApi()
    create = ("POST", "/v2/pods") if provider == "runpod" else ("PUT", "/api/v0/asks/11/")
    handle, state = api.handle, {"n": 0}

    def flaky(method, path, params, body):
        if (method, path) == create:
            state["n"] += 1
            if state["n"] == 1:
                if made:
                    handle(method, path, params, body)
                    return httpx.Response(502, json={"title": "Bad Gateway"})
                raise httpx.ReadTimeout("lost")
        return handle(method, path, params, body)
    api.handle = flaky
    result = trainer(options(provider, keys), api, Ssh()).train(a_job(tmp_path / "ws"))
    assert state["n"] == (1 if made else 2)
    if provider == "runpod":
        assert [(i, p["status"]) for i, p in api.pods.items()] == [("pod1", "TERMINATED")]
        assert result.job["cloud"]["instance"] == "pod1"
    else:
        assert api.instances == {} and result.job["cloud"]["instance"] == "7001" and api.n == 7001


def test_a_second_instance_made_for_the_job_is_stopped_before_the_boot(tmp_path, keys, capsys):
    """A lost create whose instance the listing did not show yet: the create is sent again, and once one is started
    the other of the same job is stopped at once, not after the job."""
    api = RunPodApi()
    handle, state = api.handle, {"posts": 0, "lists": 0}

    def late(method, path, params, body):
        if (method, path) == ("POST", "/v2/pods"):
            state["posts"] += 1
            r = handle(method, path, params, body)
            return httpx.Response(502, json={"title": "Bad Gateway"}) if state["posts"] == 1 else r
        if (method, path) == ("GET", "/v2/pods"):
            state["lists"] += 1
            if state["lists"] == 2:                 # the lookup after the lost create: not listed yet
                return httpx.Response(200, json={"pods": [], "pagination": {"hasNextPage": False}})
        return handle(method, path, params, body)
    api.handle = late
    trainer(options("runpod", keys), api, Ssh()).train(a_job(tmp_path / "ws"))
    deletes = [r[1] for r in api.calls("DELETE")]
    assert deletes == ["/v2/pods/pod1", "/v2/pods/pod2"]
    first_get = next(i for i, r in enumerate(api.requests) if r[0] == "GET" and r[1] == "/v2/pods/pod2")
    assert api.requests.index(next(r for r in api.requests if r[1] == "/v2/pods/pod1" and r[0] == "DELETE")) < first_get
    assert "a second one made for job" in capsys.readouterr().err


@pytest.mark.parametrize("provider", ["runpod", "vast"])
def test_an_instance_that_dies_during_the_job_fails_it_at_once(tmp_path, keys, provider):
    api = RunPodApi() if provider == "runpod" else VastApi()
    ssh = Ssh(job="hang")
    handle = api.handle

    def dies(method, path, params, body):
        r = handle(method, path, params, body)
        if ssh.id and method == "GET" and path in ("/v2/pods/pod1", "/api/v0/instances/7001/"):
            if provider == "runpod":
                api.pods["pod1"]["status"] = "EXITED"
                return httpx.Response(200, json=api.pods["pod1"])
            return httpx.Response(200, json={"instances": {**api.instances["7001"], "actual_status": "offline"}})
        return r
    api.handle = dies
    tr = trainer(options(provider, keys), api, ssh)            # deadline_s: 8 h
    with pytest.raises(TrainError, match="EXITED|offline"):
        tr.train(a_job(tmp_path / "ws"))
    polls = [a for a in ssh.commands if a[0] == "ssh" and "ps -ww -o args=" in a[-1]]
    assert len(polls) == 1


def test_a_failed_stop_is_on_the_card_and_left_to_the_next_job(tmp_path, keys, capsys):
    api = RunPodApi()
    api.fail[("DELETE", "/v2/pods/pod1")] = (400, {"title": "Bad Request"})
    tr = trainer(options("runpod", keys), api, Ssh())
    result = tr.train(a_job(tmp_path / "ws"))
    assert result.job["cloud"]["stop_failed"] == ["pod1"] and "COULD NOT STOP instance pod1" in capsys.readouterr().err
    tag = ws_tag(tmp_path / "ws")
    assert ("runpod", tag) not in JobTrainer._stopped              # the next job sweeps the workspace again
    del api.fail[("DELETE", "/v2/pods/pod1")]
    tr.train(a_job(tmp_path / "ws", "m_0002"))
    assert api.pods["pod1"]["status"] == "TERMINATED"


def test_a_listing_that_fails_after_the_job_is_left_to_the_next_job(tmp_path, keys):
    api = RunPodApi()
    handle, state = api.handle, {"deleted": False}

    def down(method, path, params, body):
        if method == "DELETE":
            state["deleted"] = True
        if (method, path) == ("GET", "/v2/pods") and state["deleted"]:
            return httpx.Response(503, json={"title": "Service Unavailable"})
        return handle(method, path, params, body)
    api.handle = down
    tr = trainer(options("runpod", keys), api, Ssh())
    tr.train(a_job(tmp_path / "ws"))
    assert api.pods["pod1"]["status"] == "TERMINATED"
    assert ("runpod", ws_tag(tmp_path / "ws")) not in JobTrainer._stopped


@pytest.mark.parametrize("how", ["interrupt", "sigterm"])
def test_an_interrupt_during_the_stop_still_stops_the_known_instance(tmp_path, keys, how):
    """^C again while the instance is being stopped, or SIGTERM while the job runs: the known instance is stopped
    first, before any listing, and the interrupt comes out after."""
    import os
    import signal
    api = RunPodApi()
    handle, state = api.handle, {"lists": 0}
    ssh = Ssh(job="hang")
    polled = ssh.__call__

    def poll(args, input=None, timeout=None):
        if how == "sigterm" and args[0] == "ssh" and "ps -ww -o args=" in args[-1]:
            os.kill(os.getpid(), signal.SIGTERM)
        return polled(args, input, timeout)

    def interrupted(method, path, params, body):
        if (method, path) == ("GET", "/v2/pods"):
            state["lists"] += 1
            if how == "interrupt" and state["lists"] == 2:      # the listing after the known pod's stop
                raise KeyboardInterrupt
        return handle(method, path, params, body)
    api.handle = interrupted
    before = signal.getsignal(signal.SIGTERM)
    tr = trainer(options("runpod", keys), api, poll if how == "sigterm" else ssh, deadline_s=0)
    with pytest.raises(KeyboardInterrupt if how == "interrupt" else SystemExit):
        tr.train(a_job(tmp_path / "ws"))
    assert api.pods["pod1"]["status"] == "TERMINATED" and signal.getsignal(signal.SIGTERM) is before
    assert not any("kill -s TERM" in a[-1] for a in ssh.commands if a[0] == "ssh")      # no ssh cancel to wait on


def test_the_workspace_tag_is_a_random_id_kept_in_the_workspace(tmp_path):
    ws = tmp_path / "ws"
    first = cloud.tag(str(ws))
    assert re.fullmatch(r"ouroloop-[0-9a-f]{12}", first) and cloud.tag(str(ws)) == first
    assert (ws / "models" / ".cloud_id").read_text().strip() == first.removeprefix("ouroloop-")
    (ws / "models" / ".cloud_id").unlink()              # another control plane at the same path: another tag
    assert cloud.tag(str(ws)) != first


def test_start_sh_marks_when_it_is_done(tmp_path):
    """The trainer's boot waits for this marker: on Vast.ai sshd answers before start.sh (onstart) is done."""
    home = tmp_path / "home"
    (home / ".ouroloop").mkdir(parents=True)
    (home / ".ouroloop" / "started").write_text("")        # a marker from an earlier boot is removed first
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "OUROLOOP_SSHD": "0", "PUBLIC_KEY": "ssh-ed25519 AAAAk",
           "HF_TOKEN": "hf_x"}
    out = subprocess.run(["sh", str(cloud.START)], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert (home / ".ouroloop" / "started").exists()
    assert (home / ".ssh" / "authorized_keys").read_text() == "ssh-ed25519 AAAAk\n"
    assert (home / ".cache" / "huggingface" / "token").read_text() == "hf_x"
    script = cloud.START.read_text()
    assert script.index('rm -f "$ouro/started"') < script.index("authorized_keys") < script.index(': > "$ouro/started"')


# ---------------------------------------------------------------- refused before anything is used
def test_a_cloud_trainer_refuses_a_dir_store_and_bad_options(keys):
    base = {"serving": {"url": "http://gpu:8765"}}
    with pytest.raises(ValueError, match='needs a \\[store\\] that outlives the instance'):
        JobTrainer("signature", cloud=options("runpod", keys), **base)
    with pytest.raises(ValueError, match="one or the other"):
        JobTrainer("signature", cloud=options("runpod", keys), host="u@gpu", store=STORE, **base)
    with pytest.raises(ValueError, match="needs max_price"):
        JobTrainer("signature", cloud={**options("vast", keys), "max_price": None}, store=STORE, **base)
    with pytest.raises(ValueError, match="may only set"):
        JobTrainer("signature", cloud={**options("runpod", keys), "api_key": "x"}, store=STORE, **base)
    with pytest.raises(ValueError, match="provider"):
        JobTrainer("signature", cloud={"provider": "aws"}, store=STORE, **base)
    # built from the config: [trainer.cloud], and [store]; an old config's transport "hf" is that store
    for cfg in ({"trainer": {"type": "signature", "cloud": options("vast", keys)}, "store": STORE},
                {"trainer": {"type": "signature", "transport": "hf", "hf_prefix": PREFIX,
                             "cloud": options("vast", keys)}, "models": {"jobs_root": "~/.ouroloop/jobs"}}):
        tr = train_config.build_trainer(cfg)
        assert tr.cloud["provider"] == "vast" and tr.python == "~/.ouroloop/venv/bin/python"
        assert tr.store.table == STORE and not tr.serving.url


@pytest.mark.parametrize("provider", ["runpod", "vast"])
def test_no_offer_under_the_price_fails_the_build_before_anything_is_used(tmp_path, keys, provider):
    api = RunPodApi(price=2.5) if provider == "runpod" else VastApi()
    ssh = Ssh()
    tr = trainer(options(provider, keys, max_price=0.3), api, ssh)
    rt = Workspace(tmp_path / "ws", serving=lambda: tr.serving)
    built = train_artifact(rt, tr, dataset(rt.root), version="m_0001")
    assert built.status == "failed" and built.model_ref is None and built.artifact_ref is None
    assert "no offer under the price" in built.errors[0]
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
