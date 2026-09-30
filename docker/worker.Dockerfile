# The isolated worker for capability-package code (docs/modules/01_POLICY_RUNTIME.md §1).
# Build: docker build -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .
# Only the runner goes in: no project code, no workspace, no credentials. At run time the host adds
# --network none, --read-only, a small /tmp tmpfs, a non-root user, dropped capabilities and CPU/memory/PID limits
# (ouroloop/agent/worker.py DockerWorker); the package files arrive over stdin.
FROM python:3.12-slim
COPY ouroloop/agent/worker_runner.py /runner/worker_runner.py
USER 65534:65534
ENTRYPOINT []
