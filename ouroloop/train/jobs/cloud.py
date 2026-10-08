"""Cloud GPU workers (docs/TRAINING_HOSTS.md): an instance per training job, on RunPod or Vast.ai, behind one small
interface:

    start(tag, job, options, env) -> id     an instance named <tag>-<job>, running start.sh with `env`
    ready(id) -> (user@host, port) | None   where its ssh listens, once the provider says it can; None while booting;
                                            Gone when it is gone, or stopped and will not run again
    stop(id)                                terminated: nothing of it bills any more
    running(tag) -> [id]                    the instances named `tag` or <tag>-..., not terminated: a workspace's,
                                            or with <tag>-<job>, one job's

`placed[id]` holds what the card records of an instance: its GPU and its price per hour. The trainer (trainer.py) runs
the job's lifecycle: boot() waits for ready() and a real ssh answer, and the instance is always stopped.

The API keys come from the environment only (RUNPOD_API_KEY, VAST_API_KEY), are read when a request is made, and go
nowhere but that request's Authorization header: never into an error, a log, spec.json or a card. Every endpoint,
field and header below is from the providers' docs (RunPod REST v2, https://api.runpod.io/v2/openapi.json; Vast.ai
https://docs.vast.ai/api-reference and vast-cli's vast.py); where the docs are unclear, a comment says so.
"""
from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path

import httpx

START = Path(__file__).resolve().parent / "hosts" / "start.sh"
TIMEOUT_S = 30.0                # one API request
RETRIES = 4                     # tries of a request that met a 429, a 5xx or a broken connection
BACKOFF_S = 5.0                 # the wait before a retry when the provider does not say (Vast.ai never does)
# The keys of [trainer.cloud]; `provider` picks the class.
COMMON = {"provider", "image", "gpu", "gpu_count", "disk_gb", "max_price", "ssh_key", "boot_timeout_s",
          "boot_poll_s"}


class CloudError(RuntimeError):
    pass


class Gone(CloudError):
    """The instance is gone, or stopped for good: whatever runs on it is lost."""


class Unanswered(CloudError):
    """A request whose answer was lost (a broken connection, a 5xx): it may or may not have been carried out."""


def tag(workspace: str) -> str:
    """A workspace's tag on its instances: a random id made once and kept in <workspace>/models/.cloud_id, so two
    control planes on one account never share a tag, even at the same path. Short, fixed in length (so <tag>- is a
    prefix no other tag has), and no path of this machine."""
    path = Path(workspace) / "models" / ".cloud_id"
    valid = re.compile(r"[0-9a-f]{12}")
    try:
        id = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        id = ""
    if not valid.fullmatch(id):
        path.parent.mkdir(parents=True, exist_ok=True)
        new = secrets.token_hex(6)
        try:
            with open(path, "x", encoding="utf-8") as f:     # two processes of a workspace keep the first one's
                f.write(new + "\n")
            id = new
        except FileExistsError:
            id = path.read_text(encoding="utf-8").strip()
            if not valid.fullmatch(id):
                path.write_text(new + "\n", encoding="utf-8")
                id = new
    return "ouroloop-" + id


def _named(name, tag: str) -> bool:
    name = str(name or "")
    return name == tag or name.startswith(f"{tag}-")


def startup() -> str:
    """start.sh, which every instance runs when it starts: a fixed script with no value in it."""
    return START.read_text(encoding="utf-8")


def _problem(r: httpx.Response) -> str:
    """What went wrong, from the response body only (never the request, which holds the key)."""
    try:
        body = r.json()
    except ValueError:
        return (r.text or "").strip()[:300]
    if isinstance(body, dict):
        parts = [str(body[k]) for k in ("title", "detail", "error", "msg", "message") if body.get(k)]
        if parts:
            return ": ".join(dict.fromkeys(parts))[:300]
    return str(body)[:300]


class Provider:
    """One provider's REST API over httpx. `transport` is an httpx transport (tests: httpx.MockTransport)."""
    NAME = ""
    BASE = ""
    KEY_ENV = ""
    OPTIONS: set = set()
    REQUIRED: set = {"image", "gpu"}

    def __init__(self, transport=None, sleep=time.sleep):
        self.sleep = sleep
        self.placed: dict[str, dict] = {}
        self.unsure: set[str] = set()      # names of instances whose create lost an answer: there may be two
        self._client = httpx.Client(base_url=self.BASE, transport=transport, timeout=TIMEOUT_S)

    @classmethod
    def check(cls, options: dict) -> dict:
        unknown = set(options) - COMMON - cls.OPTIONS
        if unknown:
            raise ValueError(f"[trainer.cloud] for {cls.NAME} may only set {sorted(COMMON | cls.OPTIONS)}, "
                             f"not {sorted(unknown)}")
        missing = [k for k in sorted(cls.REQUIRED) if not options.get(k)]
        if missing:
            raise ValueError(f"[trainer.cloud] for {cls.NAME} needs {', '.join(missing)}")
        return options

    def _key(self) -> str:
        key = os.environ.get(self.KEY_ENV, "").strip()
        if not key:
            raise CloudError(f"{self.KEY_ENV} is not set: the {self.NAME} API key is read from the environment only")
        return key

    def _call(self, method: str, path: str, ok=(), retry: bool = True, **kw) -> httpx.Response:
        """A request with the key; 429, 5xx and broken connections are tried again, anything else not 2xx (or in
        `ok`) is a CloudError that names the status and the provider's message. Without `retry` (a create, which
        must not be sent twice blindly) only a 429 is tried again: a broken connection or a 5xx is Unanswered."""
        headers = {"Authorization": f"Bearer {self._key()}", "Accept": "application/json"}
        for attempt in range(RETRIES):
            last = attempt == RETRIES - 1
            try:
                r = self._client.request(method, path, headers=headers, **kw)
            except httpx.TransportError as e:
                if not retry:
                    raise Unanswered(f"{self.NAME} {method} {path}: {type(e).__name__}") from None
                if last:
                    raise CloudError(f"{self.NAME} {method} {path}: {type(e).__name__}") from None
                self.sleep(BACKOFF_S * (attempt + 1))
                continue
            if r.status_code >= 500 and not retry:
                raise Unanswered(f"{self.NAME} {method} {path}: {r.status_code} {_problem(r)}")
            if r.status_code == 429 or r.status_code >= 500:
                if last:
                    raise CloudError(f"{self.NAME} {method} {path}: {r.status_code} {_problem(r)}")
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = BACKOFF_S * (attempt + 1)
                self.sleep(wait)
                continue
            if r.is_success or r.status_code in ok:
                return r
            raise CloudError(f"{self.NAME} {method} {path}: {r.status_code} {_problem(r)}")
        raise AssertionError("unreachable")

    def _create(self, name: str, method: str, path: str, **kw) -> tuple[httpx.Response | None, str | None]:
        """A create of the instance `name`, never sent twice blindly: when its answer is lost, the instance is looked
        up by its name and adopted, and the create is sent again only if there is none. -> (the answer, None), or
        (None, the id of the instance it made)."""
        for attempt in range(RETRIES):
            try:
                return self._call(method, path, retry=False, **kw), None
            except Unanswered as e:
                self.unsure.add(name)
                found = self.running(name)      # a CloudError here: no second create; the job's stop sweeps it
                if found:
                    return None, found[0]
                if attempt == RETRIES - 1:
                    raise CloudError(str(e)) from None
                self.sleep(BACKOFF_S * (attempt + 1))
        raise AssertionError("unreachable")

    def start(self, tag: str, job: str, options: dict, env: dict) -> str: ...

    def ready(self, id: str) -> tuple[str, int] | None: ...

    def stop(self, id: str) -> None: ...

    def running(self, tag: str) -> list[str]: ...


# ---------------------------------------------------------------------- RunPod (REST v2)
class RunPod(Provider):
    """RunPod pods over REST v2 (v1 retires on 2026-11-15). v2 has no price cap, labels, name filter or spot flag, so
    the price is checked against the GPU catalog before a create (and against the pod's `cost` after), and a
    workspace's pods are found by their name. A create places exactly one GPU type and never falls back."""
    NAME = "runpod"
    BASE = "https://api.runpod.io"
    KEY_ENV = "RUNPOD_API_KEY"
    # cloud_type: "SECURE" (default) or "COMMUNITY". The v2 docs do not say whether a COMMUNITY pod without a public
    # IP ever gets ssh.direct; SECURE always has a public IP (v1 docs). data_centers: dataCenterIds.
    OPTIONS = {"cloud_type", "data_centers"}
    GONE = {"EXITED", "ERROR", "TERMINATED"}

    def _price(self, options: dict) -> float | None:
        """The catalog's $/hr for gpu_count of the GPU in the cloud type; CloudError if it is over max_price or
        has no availability."""
        count, cloud = int(options.get("gpu_count", 1)), options.get("cloud_type", "SECURE")
        r = self._call("GET", "/v2/catalog/gpus", params={"include": "AVAILABILITY", "product": "POD",
                                                           "count": count, "cloud": cloud})
        body = r.json()
        # the docs give each item's fields, not the envelope around them: a list, or a list under one key
        items = body if isinstance(body, list) else body.get("gpus") or body.get("items") or body.get("data") or []
        item = next((g for g in items if isinstance(g, dict) and g.get("id") == options["gpu"]), None)
        if item is None:
            raise CloudError(f"runpod: no GPU type {options['gpu']!r} in the {cloud} catalog")
        if str(item.get("availability", "")).upper() == "NONE":
            raise CloudError(f"runpod: no {options['gpu']} available in {cloud} now")
        one = (item.get("price") or {}).get(cloud.lower())
        price = float(one) * count if one is not None else None
        cap = options.get("max_price")
        if cap is not None and (price is None or price > float(cap)):
            raise CloudError(f"runpod: no offer under the price: {count} x {options['gpu']} costs "
                             f"{'an unknown price' if price is None else f'${price:.3f}/hr'} in {cloud}, "
                             f"max_price is ${float(cap):.3f}/hr")
        return price

    def start(self, tag: str, job: str, options: dict, env: dict) -> str:
        price = self._price(options)
        body = {
            "name": f"{tag}-{job}",
            "image": options["image"],
            "gpu": {"id": options["gpu"], "count": int(options.get("gpu_count", 1))},
            "cloud": options.get("cloud_type", "SECURE"),
            "env": dict(env),               # PUBLIC_KEY set here keeps startSsh from injecting the account's keys
            "ports": ["22/tcp"],            # ssh.direct needs it: only direct ssh carries rsync
            "disk": int(options.get("disk_gb", 50)),    # ephemeral container disk; no persistent mount bills later
            "startSsh": True,
            # start.sh as the container's command, on any image: it starts sshd itself. The docs do not say whether
            # an entrypoint override keeps the image's CMD as arguments; `sh -c` would take them as $0.. and ignore
            # them, and cmd is cleared as well.
            "entrypoint": ["sh", "-c", startup()],
            "cmd": [],
        }
        if options.get("data_centers"):
            body["dataCenterIds"] = list(options["data_centers"])
        # 400 is a rule violation or no capacity (the API does not tell them apart), 402 no balance, 403 a pool this
        # key cannot use, 422 an invalid body: none is worth another try with the same body.
        r, adopted = self._create(body["name"], "POST", "/v2/pods", json=body)
        id = adopted or str(r.json().get("id") or "")
        if not id:
            raise CloudError("runpod: the create answered without a pod id")
        self.placed[id] = {"gpu": f"{body['gpu']['count']} x {options['gpu']}", "price_per_hour": price,
                           "max_price": options.get("max_price")}
        return id

    def ready(self, id: str) -> tuple[str, int] | None:
        """RUNNING with ssh.direct (a public port for 22/tcp); RUNNING only means the container is healthy, so the
        trainer still probes ssh."""
        r = self._call("GET", f"/v2/pods/{id}", ok=(404,))
        if r.status_code == 404:
            raise Gone(f"runpod: pod {id} is gone")
        pod = r.json()
        status = str(pod.get("status") or "")
        if status in self.GONE:
            raise Gone(f"runpod: pod {id} is {status} and will not start")
        info = self.placed.setdefault(id, {})
        if pod.get("cost"):
            info["price_per_hour"] = float(pod["cost"])
            cap = info.get("max_price")
            if cap is not None and float(pod["cost"]) > float(cap):
                raise CloudError(f"runpod: pod {id} costs ${float(pod['cost']):.3f}/hr, over max_price "
                                 f"${float(cap):.3f}/hr")
        direct = (pod.get("ssh") or {}).get("direct")
        if status != "RUNNING" or not direct or not direct.get("host") or not direct.get("port"):
            return None
        return f"{direct.get('username') or 'root'}@{direct['host']}", int(direct["port"])

    def stop(self, id: str) -> None:
        """DELETE: terminated, which ends the billing (a "stop" would keep charging for the volume); 404 is gone."""
        self._call("DELETE", f"/v2/pods/{id}", ok=(404,))

    def running(self, tag: str) -> list[str]:
        """v2 lists pods without a name filter: all of them, a page at a time, filtered here on the name."""
        found, cursor = [], None
        for _ in range(100):
            params = {"limit": 1000, **({"cursor": cursor} if cursor else {})}
            body = self._call("GET", "/v2/pods", params=params).json()
            for pod in body.get("pods") or []:
                if _named(pod.get("name"), tag) and pod.get("status") != "TERMINATED":
                    found.append(str(pod["id"]))
            page = body.get("pagination") or {}
            cursor = page.get("nextCursor")
            if not page.get("hasNextPage") or not cursor:
                break
        return found


# ---------------------------------------------------------------------- Vast.ai
class VastAI(Provider):
    """Vast.ai instances: the cheapest offer under max_price (required: a market's price has no other bound),
    created with the ssh_direct runtype. That runtype replaces the image's entrypoint with Vast's own, which runs sshd
    with the account's keys; start.sh runs as `onstart` with OUROLOOP_SSHD=0, and the job's public key is attached to
    the instance as well."""
    NAME = "vast"
    BASE = "https://console.vast.ai"
    KEY_ENV = "VAST_API_KEY"
    OPTIONS = {"min_reliability", "offers"}      # offers: how many of the cheapest to try when one is taken
    REQUIRED = {"image", "gpu", "max_price"}
    GONE = {"exited", "unknown", "offline"}
    CREATE_GAP_S = 4.5                           # the create endpoint allows one call per 4.5 s

    def _offers(self, options: dict) -> list[dict]:
        disk, cap = int(options.get("disk_gb", 50)), float(options["max_price"])
        query = {
            # gpu_name has spaces ("RTX 4090"); the CLI turns underscores into them, so this does too
            "gpu_name": {"in": [str(options["gpu"]).replace("_", " ")]},
            "num_gpus": {"eq": int(options.get("gpu_count", 1))},
            "dph_total": {"lte": cap},           # total $/hr, storage included at allocated_storage GB
            "allocated_storage": disk,
            "reliability": {"gte": float(options.get("min_reliability", 0.98))},  # reliability2: undocumented
            "direct_port_count": {"gte": 1},     # direct ssh
            "verified": {"eq": True}, "rentable": {"eq": True}, "rented": {"eq": False}, "external": {"eq": False},
            # the enum says "ondemand"; the examples and the CLI send "on-demand"
            "type": "on-demand",
            "order": [["dph_total", "asc"]],
            "limit": 20,
        }
        offers = self._call("POST", "/api/v0/bundles/", json=query).json().get("offers") or []
        # the filter again, here: the price bound is the one thing that must hold
        return [o for o in offers if o.get("id") is not None and float(o.get("dph_total", cap + 1)) <= cap]

    def start(self, tag: str, job: str, options: dict, env: dict) -> str:
        offers = self._offers(options)
        if not offers:
            raise CloudError(f"vast: no offer under the price: no {int(options.get('gpu_count', 1))} x "
                             f"{options['gpu']} at or under ${float(options['max_price']):.3f}/hr")
        body = {
            "client_id": "me",
            "image": options["image"],
            "label": f"{tag}-{job}",
            "disk": max(8, int(options.get("disk_gb", 50))),
            # a JSON object, as the guide and the CLI send it (one YAML spec says a docker-flag string)
            "env": {**env, "OUROLOOP_SSHD": "0"},
            # the docs' enum; the CLI still sends the older "ssh_direc ssh_proxy"
            "runtype": "ssh_direct",
            "onstart": startup(),
            "cancel_unavail": True,              # an error, not a stopped instance, when the machine is taken
            "target_state": "running",
        }
        tried = []
        for i, offer in enumerate(offers[:int(options.get("offers", 3))]):
            if i:
                self.sleep(self.CREATE_GAP_S)
            r, adopted = self._create(body["label"], "PUT", f"/api/v0/asks/{offer['id']}/", json=body,
                                      ok=(404, 410))
            if r is not None and r.status_code in (404, 410):      # no_such_ask: the offer went to someone else
                tried.append(str(offer["id"]))
                continue
            id = adopted or str(r.json().get("new_contract") or "")
            if not id:
                raise CloudError(f"vast: the create of offer {offer['id']} answered without an instance id")
            self.placed[id] = {"gpu": f"{offer.get('num_gpus', options.get('gpu_count', 1))} x "
                                      f"{offer.get('gpu_name', options['gpu'])}",
                               "price_per_hour": float(offer["dph_total"])}
            # The docs disagree on whether an account key reaches instances made after it; this one does not rely
            # on it. PUBLIC_KEY in env is only for images whose own start honors it.
            if env.get("PUBLIC_KEY"):
                self._call("POST", f"/api/v0/instances/{id}/ssh/", json={"ssh_key": env["PUBLIC_KEY"]})
            return id
        raise CloudError(f"vast: every offer under the price was taken before it could be rented ({', '.join(tried)})")

    def ready(self, id: str) -> tuple[str, int] | None:
        """actual_status "running", then direct ssh (public_ipaddr and the host port of 22/tcp, a string) or the
        proxy (ssh_host, ssh_port, plus one for a jupyter runtype), as vast-cli's _ssh_url; user root."""
        inst = self._call("GET", f"/api/v0/instances/{id}/").json().get("instances")
        if not inst:
            raise Gone(f"vast: instance {id} is gone")
        status = inst.get("actual_status")
        if status in self.GONE:
            raise Gone(f"vast: instance {id} is {status} and will not start")
        if status != "running":
            return None
        ports = inst.get("ports")
        # show-instance's YAML says ports is an int array; the list endpoint and the CLI use this dict
        direct = ports.get("22/tcp") if isinstance(ports, dict) else None
        if direct and inst.get("public_ipaddr"):
            return f"root@{str(inst['public_ipaddr']).strip()}", int(direct[0]["HostPort"])
        if inst.get("ssh_host") and inst.get("ssh_port"):
            bump = 1 if "jupyter" in str(inst.get("image_runtype") or "") else 0
            return f"root@{inst['ssh_host']}", int(inst["ssh_port"]) + bump
        return None

    def stop(self, id: str) -> None:
        """Destroy: irreversible, and the end of all billing ("stop" keeps charging for storage); 404 is gone."""
        self._call("DELETE", f"/api/v0/instances/{id}/", ok=(404,))

    def running(self, tag: str) -> list[str]:
        """The v1 list (v0's is deprecated), filtered here: its filters match a label exactly, and each label holds
        the job id after the tag. At most 25 a page."""
        found, token = [], None
        for _ in range(400):
            params = {"limit": 25, "order_by": '[{"col":"id","dir":"asc"}]'}
            if token:
                params["after_token"] = token
            body = self._call("GET", "/api/v1/instances/", params=params).json()
            for inst in body.get("instances") or []:
                if _named(inst.get("label"), tag):
                    found.append(str(inst["id"]))
            token = body.get("next_token")
            if not token:
                break
        return found


PROVIDERS = {p.NAME: p for p in (RunPod, VastAI)}


def check(options: dict) -> dict:
    """[trainer.cloud], checked before any job: its provider and that provider's options."""
    options = dict(options or {})
    name = options.get("provider")
    if name not in PROVIDERS:
        raise ValueError(f"[trainer.cloud] provider {name!r}: one of {sorted(PROVIDERS)}")
    return PROVIDERS[name].check(options)


def make(options: dict, transport=None, sleep=time.sleep) -> Provider:
    return PROVIDERS[check(options)["provider"]](transport, sleep)
