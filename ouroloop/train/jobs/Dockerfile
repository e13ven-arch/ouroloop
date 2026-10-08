# syntax=docker/dockerfile:1
# A training host as an image (docs/TRAINING_HOSTS.md): the same bootstrap.sh as `python -m ouroloop.train host init`,
# run at build time, and an sshd that the control plane reaches like any host. Build from the ouroloop checkout:
#
#   docker build -f ouroloop/train/jobs/Dockerfile -t ouroloop-worker .             # ouroloop alone
#   docker build -f ouroloop/train/jobs/Dockerfile --build-arg TRAINERS="tde" \
#       --build-context sources=../plugin-sources -t ouroloop-worker .              # with plugins
#
# TRAINERS names the plugins to install (none by default: a host lacking one gets it by host init before its first
# job). A plugin's local source is <name>/ in the `sources` build context, a directory of checkouts, one per plugin
# (e.g. ../plugin-sources/tde, a clone of TDE; not a symlink, which a context does not follow), of which only git's
# tracked files are installed: the context is
# mounted for the install step only and never becomes a layer of the image. Or it is a pip requirement in SOURCES
# ("name=requirement ...").
# Run it with a GPU and your public key, e.g.
#
#   docker run -d --gpus all -p 2222:22 -e PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)" ouroloop-worker
#
# and set [trainer] host = "root@<box>" with ssh_opts = "-p 2222 ...". [trainer.cloud] (RunPod, Vast.ai) runs it per
# job with the same start script, ouroloop/train/jobs/hosts/start.sh. No secret is in the image: the key, the token and the ssh
# host keys arrive when the container starts. A host whose ouroloop differs from the control plane's is initialized again by
# the trainer, over ssh, before its first job.

ARG BASE=ubuntu:24.04

# The plugins' local sources, <name>/ each; empty unless given as --build-context sources=<dir>.
FROM scratch AS sources

FROM ${BASE}
ARG TRAINERS=""
ARG SOURCES=""
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends openssh-server python3 python3-venv rsync git procps ca-certificates \
 && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
 && mkdir -p /run/sshd \
 && printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\n' > /etc/ssh/sshd_config.d/ouroloop.conf

COPY . /opt/ouroloop/checkout

# What host init would send, written from the checkout and the mounted sources (git's tracked files, packed), then
# bootstrap.sh on it: ~/.ouroloop/venv and host.json.
RUN --mount=type=bind,from=sources,target=/opt/ouroloop/sources \
    set -eu; cd /opt/ouroloop/checkout; args=""; \
    for name in $TRAINERS; do \
        src=""; \
        if [ -n "$(ls -A "/opt/ouroloop/sources/$name" 2>/dev/null)" ]; then src="/opt/ouroloop/sources/$name"; fi; \
        for pair in $SOURCES; do if [ "${pair%%=*}" = "$name" ]; then src="${pair#*=}"; fi; done; \
        args="$args $name${src:+=$src}"; \
    done; \
    python3 -m ouroloop.train.jobs.hosts pack /opt/ouroloop/init $args; \
    sh /opt/ouroloop/init/bootstrap.sh /opt/ouroloop/init; \
    rm -rf /opt/ouroloop/init /root/.cache/pip

COPY ouroloop/train/jobs/hosts/start.sh /usr/local/bin/ouroloop-start
RUN chmod +x /usr/local/bin/ouroloop-start
EXPOSE 22
CMD ["/usr/local/bin/ouroloop-start"]
