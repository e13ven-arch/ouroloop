# The isolated worker (docs/design/RUNTIME.md §2, §6): the same runner as the host's (ouroloop/runtime/worker_runner.py,
# byte for byte — every load checks its digest) plus the third-party libraries the stirrup seed's vendored Stirrup
# 0.2.0 imports, pinned by hash. Stirrup itself is NOT installed: it arrives with the package under vendor/stirrup, so
# the bytes that run are the package's and R can edit them.
#
# Build (from the repository root):
#   docker build --provenance=false -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .
# K freezes the built image's content id (worker.image_id) and the worker runs by that id; --provenance=false gives an id
# that a rebuild from the same inputs reproduces. linux/arm64 only for now (the hashes are the aarch64 wheels').
#
# At run time DockerWorker adds every resource and the network from K's worker table (--network none or the attempt's
# internal egress network, --read-only, a /tmp tmpfs, user 65534, --cap-drop ALL, no-new-privileges, --pids-limit,
# --memory and --memory-swap, --cpus) and starts `env -i PATH LANG <K's env> python -I` on the runner: nothing of this
# image's environment reaches package code.
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
COPY docker/worker.requirements.txt /tmp/worker.requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes --no-deps \
        -r /tmp/worker.requirements.txt \
 && rm /tmp/worker.requirements.txt \
 && OPENBLAS_NUM_THREADS=1 python -c "import pydantic, anyio, filetype, PIL, moviepy, rich, httpx, trafilatura, tenacity, numpy"
COPY ouroloop/runtime/worker_runner.py /runner/worker_runner.py
USER 65534:65534
ENTRYPOINT []
