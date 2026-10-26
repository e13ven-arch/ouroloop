#!/bin/sh
# Prepare this machine to run reharness's training jobs (docs/TRAINING_HOSTS.md). Idempotent: run it again at will.
#
#     sh bootstrap.sh <dir>
#
# <dir> is what `python -m reharness.train host init` sends (or `python -m reharness.train.jobs.hosts pack` writes):
# reharness.tar.gz, the control plane's own code; the plugins' local sources, <name>.tar.gz; requirements.txt, all of
# them and the plugins' requirements; plan.json. It installs what is missing of rsync, git, ps and a Python >= 3.11
# with venv (as root, or with sudo if it is there and needs no password), makes ~/.reharness/venv, installs everything
# into it, checks the GPU, and writes ~/.reharness/host.json.
#
# Environment:
#   REHARNESS_PYTHON     the Python to make the venv from (default: the newest python3.x >= 3.11 on PATH)
#   REHARNESS_TEST_PIP   tests only: a command run in place of the venv's pip, with the same arguments and
#                       VIRTUAL_ENV set; the venv is then made without pip, and nothing is downloaded
set -eu

DIR=$(cd "${1:?usage: sh bootstrap.sh <dir>}" && pwd)
OURO="$HOME/.reharness"
VENV="$OURO/venv"
unset PYTHONPATH || true            # the venv runs what is installed in it, nothing else

say() { echo "bootstrap: $*"; }
die() { echo "bootstrap: $*" >&2; exit 1; }

as_root() {
    if [ "$(id -u)" = 0 ]; then "$@"
    elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then sudo -n "$@"
    else return 1
    fi
}

install_packages() {                # the distribution's names for them
    if command -v apt-get >/dev/null 2>&1; then
        as_root apt-get update -qq && as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
    elif command -v dnf >/dev/null 2>&1; then as_root dnf install -y -q "$@"
    elif command -v yum >/dev/null 2>&1; then as_root yum install -y -q "$@"
    elif command -v apk >/dev/null 2>&1; then as_root apk add --no-cache -q "$@"
    else return 1
    fi
}

need() {                            # need <command> <package>
    command -v "$1" >/dev/null 2>&1 && return 0
    say "installing $2"
    install_packages "$2" || die "$1 is missing and could not be installed (not root, no sudo without a password, or \
no known package manager): install $2, then run this again"
}

need rsync rsync
need git git
need ps procps

usable() {                          # a Python >= 3.11 that can make a venv (with pip, unless pip is stood in for)
    "$1" -c 'import sys, venv; sys.exit(sys.version_info < (3, 11))' 2>/dev/null || return 1
    [ -n "${REHARNESS_TEST_PIP:-}" ] || "$1" -c 'import ensurepip' 2>/dev/null
}

why_unusable() {                    # what is wrong with the python3 that is here, for an error a person can act on
    p=$(command -v python3 2>/dev/null) || { echo "no python3 on PATH"; return; }
    v=$("$p" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || { echo "$p does not run"; return; }
    "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null ||
        { echo "$p is $v, older than 3.11"; return; }
    "$p" -c 'import venv' 2>/dev/null || { echo "$p is $v but has no venv (the python3-venv package)"; return; }
    "$p" -c 'import ensurepip' 2>/dev/null || { echo "$p is $v with venv but no pip (the python3-venv package)"; return; }
    echo "$p is $v and looks usable"
}

find_python() {
    for p in ${REHARNESS_PYTHON:-} python3.13 python3.12 python3.11 python3; do
        if command -v "$p" >/dev/null 2>&1 && usable "$(command -v "$p")"; then command -v "$p"; return 0; fi
    done
    return 1
}

PY=$(find_python) || {
    say "installing python3 with venv"
    if command -v apt-get >/dev/null 2>&1; then
        # the default python3 may be older than 3.11 (3.10 on Ubuntu 22.04): then a versioned one, each on its own,
        # since apt-get refuses a whole list for one unknown name
        install_packages python3 python3-venv || true
        find_python >/dev/null || install_packages python3.12 python3.12-venv || \
            install_packages python3.11 python3.11-venv || true
    else install_packages python3 || true
    fi
    PY=$(find_python) || die "no usable Python >= 3.11 here: $(why_unusable). Install one with venv and pip, or set \
REHARNESS_PYTHON to one that has both (it need not be python3.x on PATH: a conda or pyenv interpreter will do)"
}

if [ -x "$VENV/bin/python" ] && ! "$VENV/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    say "the venv's Python is too old: making it again"
    rm -rf "$VENV"
fi
if [ ! -x "$VENV/bin/python" ]; then
    say "making $VENV with $PY"
    mkdir -p "$OURO"
    if [ -n "${REHARNESS_TEST_PIP:-}" ]; then "$PY" -m venv --without-pip "$VENV"; else "$PY" -m venv "$VENV"; fi
fi

pip() {
    if [ -n "${REHARNESS_TEST_PIP:-}" ]; then VIRTUAL_ENV="$VENV" "$REHARNESS_TEST_PIP" "$@"
    else "$VENV/bin/python" -m pip --disable-pip-version-check "$@"
    fi
}

cd "$DIR"
say "installing reharness and the plugins into $VENV"
pip install -q -r requirements.txt
for archive in ./*.tar.gz; do       # exactly the code sent, even when its version number did not change
    if [ -e "$archive" ]; then pip install -q --no-deps --force-reinstall "$archive"; fi
done

if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || say "nvidia-smi failed: check the GPU driver"
else
    say "no nvidia-smi: no NVIDIA GPU, or no driver"
fi

cp plan.json "$OURO/plan.json"
"$VENV/bin/python" -m reharness.train.jobs.hosts record "$OURO/plan.json" > "$OURO/host.json.tmp"
mv "$OURO/host.json.tmp" "$OURO/host.json"
cat "$OURO/host.json"
