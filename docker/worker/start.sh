#!/bin/sh
# The worker image's command: authorize $PUBLIC_KEY for root, hand $HF_TOKEN to the ssh sessions (which do not see
# the container's environment), make this container's own ssh host keys, record host.json with the GPU it sees now,
# and run sshd until the container stops. Nothing here is baked into the image.
set -eu

mkdir -p /root/.ssh /run/sshd
chmod 700 /root/.ssh
if [ -n "${PUBLIC_KEY:-}" ]; then
    touch /root/.ssh/authorized_keys
    grep -qxF "$PUBLIC_KEY" /root/.ssh/authorized_keys || printf '%s\n' "$PUBLIC_KEY" >> /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
fi
if [ -n "${HF_TOKEN:-}" ]; then
    mkdir -p /root/.cache/huggingface
    (umask 077 && printf '%s' "$HF_TOKEN" > /root/.cache/huggingface/token)
fi
ssh-keygen -A >/dev/null

ouro=/root/.ouroloop
if "$ouro/venv/bin/python" -m ouroloop.hosts record "$ouro/plan.json" > "$ouro/host.json.tmp"; then
    mv "$ouro/host.json.tmp" "$ouro/host.json"
else
    echo "start: could not record host.json; the trainer will run host init" >&2
    rm -f "$ouro/host.json.tmp"
fi

exec /usr/sbin/sshd -D -e
