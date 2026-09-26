# A training host as an image (docs/TRAINING_HOSTS.md): the same bootstrap.sh as `ouroloop host init`, run at build
# time, and an sshd that the control plane reaches like any host. Build from the ouroloop checkout:
#
#   docker build -f docker/worker/Dockerfile --build-context tde=../tde -t ouroloop-worker .
#
# TRAINERS names the plugins to install; a plugin's local source is a build context of the same name (a clean
# checkout: git's tracked files are what is installed), or a pip requirement in SOURCES ("name=requirement ...").
# Run it with a GPU and your public key, e.g.
#
#   docker run -d --gpus all -p 2222:22 -e PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)" ouroloop-worker
#
# and set [trainer] host = "root@<box>" with ssh_opts = "-p 2222 ...". RunPod and Vast.ai run it as it is (PUBLIC_KEY,
# HF_TOKEN from their environment settings). No secret is in the image: the key, the token and the ssh host keys
# arrive when the container starts. A host whose ouroloop differs from the control plane's is initialized again by
# the trainer, over ssh, before its first job.

ARG BASE=ubuntu:24.04

# A plugin's local source; empty unless given as --build-context <name>=<dir>.
FROM scratch AS tde

FROM ${BASE}
ARG TRAINERS="tde"
ARG SOURCES=""
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends openssh-server python3 python3-venv rsync git procps ca-certificates \
 && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
 && mkdir -p /run/sshd \
 && printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\n' > /etc/ssh/sshd_config.d/ouroloop.conf

COPY . /opt/ouroloop/checkout
COPY --from=tde . /opt/ouroloop/sources/tde

# What host init would send, written from the checkout, then bootstrap.sh on it: ~/.ouroloop/venv and host.json.
RUN set -eu; cd /opt/ouroloop/checkout; args=""; \
    for name in $TRAINERS; do \
        src=""; \
        if [ -n "$(ls -A "/opt/ouroloop/sources/$name" 2>/dev/null)" ]; then src="/opt/ouroloop/sources/$name"; fi; \
        for pair in $SOURCES; do if [ "${pair%%=*}" = "$name" ]; then src="${pair#*=}"; fi; done; \
        args="$args $name${src:+=$src}"; \
    done; \
    python3 -m ouroloop.hosts pack /opt/ouroloop/init $args; \
    sh /opt/ouroloop/init/bootstrap.sh /opt/ouroloop/init; \
    rm -rf /opt/ouroloop/init /opt/ouroloop/sources /root/.cache/pip

COPY docker/worker/start.sh /usr/local/bin/ouroloop-start
RUN chmod +x /usr/local/bin/ouroloop-start
EXPOSE 22
CMD ["/usr/local/bin/ouroloop-start"]
