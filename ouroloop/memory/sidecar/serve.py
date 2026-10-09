"""The memory sidecar: ReMe 0.3.1.10's own HttpService with our ops, chroma and bge-small (docs/design/MEMORY.md).

    <memory venv>/bin/python serve.py --config <workdir>/sidecar.json

Started by ouroloop.memory.client.MemoryClient with a clean environment: OL_MEMORY_TOKEN (the bearer token every
route requires), OL_PARENT_PID (the host that owns it) and, when the stream has a research scope, OL_LLM_TOKEN (the
bridge scope's token); no proxy, no HF token, HF_HUB_OFFLINE=1. The config file holds no secret. Before anything is
served it refuses to start (exit 3) unless the venv is exactly the lock, the lock, the flows file and ReMe's version
and wheel are K's (library.backend), the model and every file the embedder reads are K's pins (library.retriever),
and no other sidecar holds the chroma directory (an exclusive lock on <chroma>/.ol-sidecar.lock for the process's
life: one store, one writer). It exits by itself when its host dies. Then it builds ReMe's Application (no .env
loading, the run's working dir, ReMe's own log files at WARNING: at INFO they hold every request's trajectories),
ReMe's HttpService on it, and on its FastAPI app: no CORS, 127.0.0.1 only, bearer auth on every route,
`success: false` turned into HTTP 500, `POST /v1/embeddings` and `GET /ol/health`. ReMe's flows are ours
(flows.yaml); its stock flows are not exposed.

Each LLM call ReMe makes is recorded for the flow call it serves (`ol_replies` in the response's metadata): the op,
the bridge's call id (the id of the completion it streamed), whether it finished, and whether the reply holds the JSON
ReMe parses. The host matches these against the bridge's own record of the step (ouroloop/memory/track.py), so a
reply is attributed by its call id, not by the step label the bridge was showing when it arrived.
"""
import argparse
import contextvars
import fcntl
import hashlib
import hmac
import json
import os
import re
import signal
import string
import sys
import threading
import time
from importlib import metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

REFUSED = 3
CLOSED_LLM = "http://127.0.0.1:9/v1"   # nothing listens on port 9: every LLM call without a research scope fails
LLM_OPS = ("success", "failure", "comparative", "validation")
ORIGIN_OPS = {"success": "SuccessExtraction", "failure": "FailureExtraction", "comparative": "ComparativeExtraction"}
NOT_FROZEN = {"pip", "setuptools", "wheel", "distribute"}   # what `pip freeze` leaves out without --all
STORE_LOCK = ".ol-sidecar.lock"
JSON_BLOCK = re.compile(r"```json\s*([\s\S]*?)\s*```")    # ReMe's own pattern (procedural_memory/utils.py, validation)
# ERROR records ReMe writes when a reply it received cannot be read (a model's format, not a failed call): ReMe then
# calls the memory invalid. Counted per flow call (ol_unparsed), not failures of it.
REPLY_PARSERS = {("reme.extension.procedural_memory.summary.memory_validation", "parse_validation"): "validation"}


def refuse(msg: str):
    sys.stderr.write(f"memory sidecar refused to start: {msg}\n")
    sys.stderr.flush()
    raise SystemExit(REFUSED)


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_pins(lock: Path) -> dict:
    pins = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        req = line.split()[0]
        name, _, version = req.partition("==")
        pins[canonical(name)] = version
    return pins


def installed() -> dict:
    """{canonical name: version} of the venv's distributions, as `pip freeze` lists them."""
    site = [p for p in sys.path if p.endswith("site-packages")]
    out = {}
    for dist in metadata.distributions(path=site):
        name = dist.metadata["Name"]
        if name and canonical(name) not in NOT_FROZEN:
            out[canonical(name)] = dist.version
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def lock_hashes(lock: Path) -> dict:
    """{canonical name: [the sha256 values the lock allows for it]}."""
    out = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name = canonical(line.split()[0].partition("==")[0])
            out[name] = re.findall(r"--hash=sha256:([0-9a-f]{64})", line)
    return out


def backend_problems(backend: dict, lock: Path, flows: Path) -> list[str]:
    """What differs between K's library.backend and what this sidecar would run: the lock and flows files, ReMe's
    installed version, and ReMe's wheel (the lock's hash for reme-ai must be K's wheel_sha256)."""
    backend = dict(backend or {})
    out = []
    if backend.get("kind") != "reme":
        out.append(f"library.backend.kind {backend.get('kind')!r}, not 'reme'")
    for key, path in (("lock_sha256", lock), ("flows_sha256", flows)):
        got = sha256_file(path)
        if backend.get(key) != got:
            out.append(f"library.backend.{key} is {backend.get(key)!r}; {path.name} here is {got}")
    try:
        version = metadata.version("reme-ai")
    except metadata.PackageNotFoundError:
        version = None
    if backend.get("version") != version:
        out.append(f"library.backend.version is {backend.get('version')!r}; the venv has reme-ai {version}")
    if backend.get("wheel_sha256") not in lock_hashes(lock).get("reme-ai", []):
        out.append(f"library.backend.wheel_sha256 {backend.get('wheel_sha256')!r} is not the lock's reme-ai wheel")
    return out


_HELD: dict = {}


def hold_store(chroma_path: Path, workdir: Path) -> None:
    """An exclusive lock on the chroma directory for this process's life (released by the OS when it exits, however
    it exits), or refuse: two sidecars on one directory are two writers on one store."""
    chroma_path.mkdir(parents=True, exist_ok=True)
    f = open(chroma_path / STORE_LOCK, "a+", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        holder = f.read().strip() or "a process that wrote no record"
        f.close()
        refuse(f"{chroma_path} is held by another memory sidecar ({holder}): one store, one writer")
    f.seek(0)
    f.truncate()
    f.write(json.dumps({"pid": os.getpid(), "workdir": str(workdir), "since": time.time()}))
    f.flush()
    _HELD["store"] = f


def watch_parent(ppid: int) -> None:
    """Stop (SIGTERM, so uvicorn's lifespan closes ReMe) when the host that started this sidecar is gone. The
    sidecar runs in its own session, so nothing else would end it, and it would keep its store's lock."""
    def run():
        while True:
            time.sleep(1.0)
            if os.getppid() != ppid:
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=run, name="ol-parent-watch", daemon=True).start()


def json_state(text: str) -> str:
    """How ReMe can read a reply: 'block' (a ```json block that parses), 'broken' (a block that does not), 'bare'
    (no block; the whole text parses, which the extraction ops accept and validation does not), 'none'."""
    blocks = JSON_BLOCK.findall(text or "")
    try:
        json.loads(blocks[0] if blocks else text or "")
        return "block" if blocks else "bare"
    except ValueError:
        return "broken" if blocks else "none"


def freeze_problems(lock: Path) -> list[str]:
    want, got = lock_pins(lock), installed()
    out = [f"{n}: {got[n]} installed, {v} pinned" for n, v in sorted(want.items()) if n in got and got[n] != v]
    out += [f"{n}=={v}: pinned, not installed" for n, v in sorted(want.items()) if n not in got]
    out += [f"{n}=={got[n]}: installed, not in the lock" for n in sorted(set(got) - set(want))]
    return out


def render_flows(template: Path, settings: dict) -> dict:
    import yaml

    origins = list(settings["origins"])
    unknown = sorted(set(origins) - set(ORIGIN_OPS))
    if unknown or not origins:
        refuse(f"library.origins {origins}: a non-empty subset of {sorted(ORIGIN_OPS)}")
    extractors = "|".join(f'{ORIGIN_OPS[o]}(llm="reme_{o}", raise_exception=True, max_retries=1)'
                          for o in ("success", "failure", "comparative") if o in origins)
    text = string.Template(template.read_text(encoding="utf-8")).substitute(
        extractors=extractors, success_threshold=float(settings["success_threshold"]),
        validation_threshold=float(settings["validation_threshold"]),
        batch_dedup_threshold=float(settings["batch_dedup_threshold"]))
    return yaml.safe_load(text)["flows"]


def reme_config(cfg: dict, flows: dict) -> dict:
    settings, retriever = cfg["settings"], cfg["settings"]["retriever"]
    base_url = cfg.get("llm_base_url") or CLOSED_LLM
    llm = {"backend": "ol_openai", "max_retries": 1, "raise_exception": True, "request_interval": 0,
           "client_timeout": float(settings.get("llm_timeout_s", 600))}
    llms = {f"reme_{op}": {**llm, "model_name": f"research/reme:{op}", "base_url": base_url} for op in LLM_OPS}
    # an op that names no LLM gets the closed port: an unlabeled call is never sent to the bridge
    llms["default"] = {**llm, "model_name": "research/reme:default", "base_url": CLOSED_LLM}
    return {
        "backend": "http", "app_name": "ouroloop-memory", "language": "", "thread_pool_max_workers": 4,
        "flows": flows, "llms": llms,   # only these flows exist: ReMe's service.yaml is not loaded
        "http": {"host": "127.0.0.1", "port": int(cfg["port"]), "timeout_keep_alive": 600,
                 "limit_concurrency": 64},
        "embedding_models": {"default": {"backend": "local_onnx", "model_name": retriever["model"],
                                         "model_sha256": retriever["sha256"], "dimensions": int(retriever["dims"]),
                                         "max_batch_size": 32, "max_retries": 1, "raise_exception": True,
                                         "max_input_length": 100000, "enable_cache": False}},
        "vector_stores": {"default": {"backend": "ol_chroma", "collection_name": "ol-sys-default",
                                      "embedding_model": "default"}},
        "token_counters": {"default": {"backend": "base"}},
    }


# The record of the flow call being served: {failures, replies, unparsed}. ReMe swallows failures in several places
# and reports none of them in the response: the flow's root SequentialOp catches what a raise_exception=True op raises
# (core/op/base_op.py _handle_failure: the root is built with raise_exception=False), ParallelOp gathers its
# extraction ops with return_exceptions=True and drops what raised (join_async_tasks), the validation op turns an LLM
# error into "invalid" (memory_validation.py:132-139). Each of those paths logs at ERROR, and only those do in a
# working run, so a loguru sink collects the ERROR records of the call in progress and the flow wrapper fails the call
# on any of them (success=false, which leaves the sidecar as HTTP 500; §1.3 rule 4). The one exception is a reply ReMe
# could not read (REPLY_PARSERS): a model's format, counted as unparsed. Every LLM call is recorded as well (the
# ol_openai backend below), and one that failed fails the call even if ReMe logged nothing.
FLOW: contextvars.ContextVar = contextvars.ContextVar("ol_flow_record", default=None)
_SINK = {"installed": False}


def _flow_sink(message) -> None:
    flow = FLOW.get()
    if flow is not None:
        r = message.record
        text = f"{r['name']}.{r['function']}: {str(r['message']).splitlines()[0][:300]}"
        op = REPLY_PARSERS.get((r["name"], r["function"]))
        if op is not None:
            flow["unparsed"].append({"op": op, "error": text})
        else:
            flow["failures"].append(text)


def _install_flow_sink() -> None:
    """After ReMe's init_logger (Application.start removes every loguru handler), once."""
    if not _SINK["installed"]:
        from loguru import logger

        logger.add(_flow_sink, level="ERROR", format="{message}", catch=False)
        _SINK["installed"] = True


def quiet_reme_logs() -> None:
    """ReMe's own log files (<workdir>/logs) at WARNING: Application.start calls init_logger with level INFO, which
    writes every request's parameters (the trajectories, train data) and every validation reply."""
    import reme.core.application as application

    init = application.init_logger

    def init_logger(*args, **kwargs):
        kwargs["level"] = "WARNING"
        return init(*args, **kwargs)

    application.init_logger = init_logger


def op_of(model: str | None) -> str:
    return str(model or "").rsplit(":", 1)[-1]


class RecordedStream:
    """ReMe's completion stream, passed through as it is; the call's record gets the completion's id (the bridge's
    call id), whether the stream finished, and the reply's JSON state."""

    def __init__(self, stream, record: dict):
        self._stream, self._record, self._text = stream, record, []

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        try:
            async for chunk in self._stream:
                if self._record["call_id"] is None and getattr(chunk, "id", None):
                    self._record["call_id"] = chunk.id
                for choice in getattr(chunk, "choices", None) or ():
                    delta = getattr(choice, "delta", None)
                    if delta is not None and getattr(delta, "content", None):
                        self._text.append(delta.content)
                yield chunk
            self._record["done"] = True
        except BaseException as e:
            self._record["error"] = f"{type(e).__name__}: {str(e)[:300]}"
            raise
        finally:
            text = "".join(self._text)
            self._record.update(chars=len(text), json=json_state(text))


def register_llm_backend():
    """ReMe's `openai` LLM backend with the SDK's own retries off and every call recorded for its flow call.
    AsyncOpenAI retries a 5xx or 429 twice by itself (openai/_constants.py DEFAULT_MAX_RETRIES = 2), so under ReMe's
    max_retries=1 one failed bridge call would be three metered calls; with max_retries=0 here it is one request.
    ReMe's own code (OpenAILLM._stream_chat) is unchanged: the client's create is wrapped, its stream passed through."""
    from openai import AsyncOpenAI
    from reme.core.llm.openai_llm import OpenAILLM
    from reme.core.registry_factory import R

    class NoRetryOpenAILLM(OpenAILLM):
        def __init__(self, client_timeout: float = 600.0, **kwargs):
            self.client_timeout = float(client_timeout)
            super().__init__(**kwargs)

        def _create_client(self):
            client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, max_retries=0,
                                 timeout=self.client_timeout)
            create = client.chat.completions.create
            default_model = self.model_name

            async def recorded(**kwargs):
                record = {"op": op_of(kwargs.get("model") or default_model), "call_id": None, "done": False}
                flow = FLOW.get()
                if flow is not None:
                    flow["replies"].append(record)
                try:
                    stream = await create(**kwargs)
                except BaseException as e:
                    record["error"] = f"{type(e).__name__}: {str(e)[:300]}"
                    raise
                if not kwargs.get("stream"):         # ReMe always streams; a plain completion is recorded whole
                    record.update(call_id=getattr(stream, "id", None), done=True)
                    text = "".join((c.message.content or "") for c in stream.choices or ())
                    record.update(chars=len(text), json=json_state(text))
                    return stream
                return RecordedStream(stream, record)

            client.chat.completions.create = recorded
            return client

    R.llms.register("ol_openai")(NoRetryOpenAILLM)


def guard(flow, name: str):
    """Wrap a flow's call: K's pinned request values (ol_pinned in flows.yaml) are put in and may not be overridden;
    a failure ReMe logged anywhere in the call, or an LLM call that failed, fails the call; the call's LLM replies
    and unreadable replies go out in its metadata (ol_replies, ol_unparsed)."""
    original, pinned = flow.call, dict(flow.flow_params.get("ol_pinned") or {})

    async def call(**kwargs):
        clash = sorted(k for k in pinned if k in kwargs and kwargs[k] != pinned[k])
        if clash:
            raise ValueError(f"{name}: {clash} are fixed by K for this sidecar")
        _install_flow_sink()
        record = {"failures": [], "replies": [], "unparsed": []}
        token = FLOW.set(record)
        try:
            response = await original(**{**kwargs, **pinned})
        finally:
            FLOW.reset(token)
        failures = list(record["failures"])
        failures += [f"LLM call {r['op']} failed: {r['error']}" for r in record["replies"] if r.get("error")]
        if failures:
            response.success = False
            response.answer = f"{name}: {len(failures)} failure(s) inside ReMe: " + " | ".join(failures)[:3000]
        if record["replies"] or record["unparsed"]:
            response.metadata["ol_replies"] = record["replies"]
            response.metadata["ol_unparsed"] = record["unparsed"]
        return response

    flow.call = call


class BearerAuth:
    """Every HTTP route needs `Authorization: Bearer <OL_MEMORY_TOKEN>` (ReMe's own /health and /docs included)."""

    def __init__(self, app, token: str):
        self.app, self.want = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or ()).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.want):
                body = b'{"success": false, "answer": "unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class FailureToStatus:
    """A JSON body with `success: false` leaves as HTTP 500, not 200: ReMe reports a failed flow in the body only."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start, chunks = None, []

        async def capture(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(chunks)
                status = start["status"]
                if status == 200 and b"application/json" in dict(start.get("headers") or ()).get(b"content-type",
                                                                                                    b""):
                    try:
                        data = json.loads(body)
                    except ValueError:
                        data = None
                    if isinstance(data, dict) and data.get("success") is False:
                        status = 500
                await send({**start, "status": status})
                await send({"type": "http.response.body", "body": body})
                return
            await send(message)

        await self.app(scope, receive, capture)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    cfg = json.loads(Path(ap.parse_args().config).read_text(encoding="utf-8"))
    token = os.environ.get("OL_MEMORY_TOKEN") or refuse("OL_MEMORY_TOKEN is not set")
    parent = os.environ.get("OL_PARENT_PID")
    if parent and int(parent) != os.getppid():
        refuse(f"the host {parent} that started this sidecar is gone")
    workdir, chroma_path = Path(cfg["workdir"]), Path(cfg["chroma_path"])
    lock, flows_file = Path(cfg["lock"]), Path(cfg["flows"])
    settings = cfg["settings"]
    retriever = settings["retriever"]

    problems = freeze_problems(lock)
    if problems:
        refuse(f"the venv ({sys.prefix}) is not the lock {lock.name}: " + "; ".join(problems[:12]))
    problems = backend_problems(settings.get("backend"), lock, flows_file)
    if problems:
        refuse("K's library.backend is not this sidecar: " + "; ".join(problems))
    hold_store(chroma_path, workdir)
    if parent:
        watch_parent(int(parent))
    import embed

    try:
        engine = embed.engine(retriever)
    except embed.ModelError as e:
        refuse(str(e))

    import chromadb
    from chromadb.config import Settings as ChromaSettings
    import reme
    from reme.config import ReMeConfigParser
    from reme.core import Application
    from reme.core.service.http_service import HttpService
    from fastapi import Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    import ops

    embed.register_reme_backend("local_onnx")
    register_llm_backend()
    quiet_reme_logs()
    ops.Shared.client = chromadb.PersistentClient(path=str(chroma_path),
                                                  settings=ChromaSettings(anonymized_telemetry=False))
    ops.Shared.chroma_path = chroma_path

    flows = render_flows(flows_file, settings)
    config = reme_config(cfg, flows)
    config_path = workdir / "reme_config.yaml"
    config_path.write_text(json.dumps(config, indent=1, sort_keys=True), encoding="utf-8")  # JSON is YAML
    app = Application(config_path=str(config_path), parser=ReMeConfigParser, enable_load_env=False,
                      working_dir=str(workdir), llm_api_key=os.environ.get("OL_LLM_TOKEN") or "no-research-scope",
                      enable_logo=False, log_to_console=False, log_to_file=True)
    for name, flow in app.service_context.flows.items():
        guard(flow, name)

    service = HttpService(app=app)
    api = service.http_service
    api.user_middleware = [m for m in api.user_middleware if m.cls is not CORSMiddleware]
    api.add_middleware(FailureToStatus)
    api.add_middleware(BearerAuth, token=token)   # outermost: nothing is served without the token

    @api.exception_handler(Exception)
    async def failed(_request: Request, exc: Exception):
        return JSONResponse({"success": False, "answer": f"{type(exc).__name__}: {exc}"[:4000], "metadata": {}},
                            status_code=500)

    lock_sha, flows_sha = sha256_file(lock), sha256_file(flows_file)

    @api.post("/v1/embeddings")
    async def embeddings(request: Request):
        body = await request.json()
        inputs = body.get("input")
        texts = [inputs] if isinstance(inputs, str) else list(inputs or ())
        if not texts or not all(isinstance(t, str) for t in texts):
            return JSONResponse({"error": {"message": "input: a string or a list of strings", "type": "invalid"}},
                                status_code=400)
        import asyncio

        vectors, tokens = await asyncio.to_thread(engine.embed, texts, query=bool(body.get("ol_query")))
        return {"object": "list", "model": retriever["model"], "ol_model_sha256": engine.model_sha256,
                "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)],
                "ol_tokens": tokens, "usage": {"prompt_tokens": sum(tokens), "total_tokens": sum(tokens)}}

    @api.get("/ol/health")
    async def health():
        counts = {}
        for name in ops.Shared.names():
            if not name.startswith(ops.SYSTEM_PREFIX):
                counts[name] = ops.Shared.client.get_collection(name).count()
        return {"status": "ok", "reme_version": reme.__version__, "lock_sha256": lock_sha, "flows_sha256": flows_sha,
                "model_sha256": engine.model_sha256, "model_files": engine.files, "query_prefix": engine.query_prefix,
                "chroma_path": str(chroma_path),
                "max_tokens": engine.max_tokens, "flows": sorted(flows), "collections": counts,
                "llm": "research" if cfg.get("llm_base_url") else "closed", "pid": os.getpid(),
                "env_keys": sorted(os.environ), "no_proxy": os.environ.get("NO_PROXY"), "started": STARTED}

    service.run()


STARTED = time.time()

if __name__ == "__main__":
    main()
