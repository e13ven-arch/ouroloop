#!/bin/sh
# A training host's startup script (docs/TRAINING_HOSTS.md): the worker image's command, and what a cloud instance runs
# when it starts on any image (reharness/train/jobs/cloud.py: RunPod as the container's command, Vast.ai as its onstart). It holds
# no value: everything arrives in the container's environment.
#
#   PUBLIC_KEY      authorized for root's ssh
#   HF_TOKEN        written to ~/.cache/huggingface/token, since ssh sessions do not see the container's environment
#   REHARNESS_SSHD   0: sshd is someone else's (Vast.ai's ssh runtypes start their own); otherwise it is started here
#   REHARNESS_TTL_S  a cloud instance's longest life (its boot, a host init and its job's deadline, with a margin):
#                   after it the container is ended, should the control plane that started it never stop it
#
# It installs what is missing of sshd and rsync (the control plane needs both before host init can run), makes this
# container's own ssh host keys, records host.json again when ~/.reharness/venv exists (the GPU is only visible now),
# writes ~/.reharness/started (the trainer waits for it: on Vast.ai sshd answers before this script is done), and
# runs sshd until the container stops. A host without the venv is prepared by the trainer (host init) over ssh.
set -u

say() { echo "reharness-start: $*" >&2; }

as_root() {
    if [ "$(id -u)" = 0 ]; then "$@"
    elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then sudo -n "$@"
    else return 1
    fi
}

install_packages() {
    if command -v apt-get >/dev/null 2>&1; then
        as_root apt-get update -qq && as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
    elif command -v dnf >/dev/null 2>&1; then as_root dnf install -y -q "$@"
    elif command -v yum >/dev/null 2>&1; then as_root yum install -y -q "$@"
    elif command -v apk >/dev/null 2>&1; then as_root apk add --no-cache -q "$@"
    else return 1
    fi
}

ouro="$HOME/.reharness"
mkdir -p "$ouro"
rm -f "$ouro/started"               # this boot's marker, written once all below is done

if [ -n "${REHARNESS_TTL_S:-}" ]; then
    # End the container after the TTL: its main process (sshd here; the provider's init on Vast.ai) is asked to stop.
    ( sleep "$REHARNESS_TTL_S" && kill -TERM 1 ) >/dev/null 2>&1 &
fi

sshd_bin() { command -v sshd 2>/dev/null || { [ -x /usr/sbin/sshd ] && echo /usr/sbin/sshd; }; }

command -v rsync >/dev/null 2>&1 || install_packages rsync || say "rsync is missing and could not be installed"
if [ "${REHARNESS_SSHD:-1}" != 0 ] && [ -z "$(sshd_bin)" ]; then
    install_packages openssh-server || say "sshd is missing and could not be installed"
fi

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
if [ -n "${PUBLIC_KEY:-}" ]; then
    touch "$HOME/.ssh/authorized_keys"
    printf '%s\n' "$PUBLIC_KEY" | while IFS= read -r key; do
        [ -z "$key" ] || grep -qxF "$key" "$HOME/.ssh/authorized_keys" || printf '%s\n' "$key" >> "$HOME/.ssh/authorized_keys"
    done
    chmod 600 "$HOME/.ssh/authorized_keys"
fi
if [ -n "${HF_TOKEN:-}" ]; then
    mkdir -p "$HOME/.cache/huggingface"
    (umask 077 && printf '%s' "$HF_TOKEN" > "$HOME/.cache/huggingface/token")
fi

if [ -x "$ouro/venv/bin/python" ] && [ -f "$ouro/plan.json" ]; then
    if "$ouro/venv/bin/python" -m reharness.train.jobs.hosts record "$ouro/plan.json" > "$ouro/host.json.tmp"; then
        mv "$ouro/host.json.tmp" "$ouro/host.json"
    else
        say "could not record host.json; the trainer will run host init"
        rm -f "$ouro/host.json.tmp"
    fi
fi

: > "$ouro/started"
[ "${REHARNESS_SSHD:-1}" != 0 ] || exit 0
sshd=$(sshd_bin) || { say "no sshd: the control plane cannot reach this host"; exec sleep infinity; }
mkdir -p /run/sshd
as_root ssh-keygen -A >/dev/null
if [ -d /etc/ssh/sshd_config.d ] && [ ! -f /etc/ssh/sshd_config.d/reharness.conf ]; then
    printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\n' > /etc/ssh/sshd_config.d/reharness.conf \
        2>/dev/null || true
fi
exec "$sshd" -D -e
