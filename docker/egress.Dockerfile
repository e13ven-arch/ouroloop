# The egress proxy (docs/design/RUNTIME.md §2): mitmproxy, unmodified, with ouroloop's addon as its only script.
# Build: docker build --provenance=false -f docker/egress.Dockerfile -t ouroloop-egress:v6.1 .  (v6.1: the addon with the
# request cap and `poll`, control protocol 2; the pool refuses an older addon)
# The base image is pinned by digest and every Python package by its wheel's sha256 (docker/egress.requirements.txt,
# installed with --require-hashes --no-deps: nothing outside the list can enter). linux/arm64 only (an amd64 lock is not written yet).
# At run time ouroloop.runtime.egress.EgressPool adds --read-only, a /tmp tmpfs, --cap-drop ALL, no-new-privileges, CPU,
# memory and PID limits, the allowlist, the control key and the per-credential row cap; the container starts on the
# uplink network (where the addon binds its control listener, published to the host's 127.0.0.1 only) and is then
# attached to each session's own internal network (the worker's only network, where it is `egress`).
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 HOME=/tmp
COPY docker/egress.requirements.txt /egress/requirements.txt
RUN pip install --require-hashes --no-deps --only-binary=:all: -r /egress/requirements.txt \
    && pip check \
    && python -c "import mitmproxy.version as v; assert v.VERSION == '12.2.3', v.VERSION"
COPY ouroloop/runtime/egress_addon.py /egress/addon.py
RUN python -c "import ast, sys; ast.parse(open('/egress/addon.py').read())" \
    && chmod -R a-w /egress
USER 65534:65534
EXPOSE 3128 3129
# TLS passes through untouched (ignore_hosts '.*'); show_ignored_hosts keeps a flow per tunnel so the addon counts its
# bytes; flow_detail 0 and termlog warn leave stdout to the addon's JSON rows; confdir is on the /tmp tmpfs (mitmproxy
# writes its CA there at start even though nothing is intercepted).
ENTRYPOINT ["mitmdump", "--mode", "regular", "--listen-port", "3128", "--set", "ignore_hosts=.*", \
            "--set", "show_ignored_hosts=true", "--set", "flow_detail=0", "--set", "termlog_verbosity=warn", \
            "--set", "confdir=/tmp/mitmproxy", "-s", "/egress/addon.py"]
