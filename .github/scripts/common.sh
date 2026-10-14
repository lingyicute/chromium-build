#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8 TZ=UTC
# Identical absolute paths in every job are essential for GN/Ninja and vpython.
export CR_ROOT=/mnt/chromium-relay
export CR_TRANSFER=/mnt/chromium-transfer
export CR_LOGS="${RUNNER_TEMP:?}/chromium-logs"
export HOME="$CR_ROOT/home"
export XDG_CACHE_HOME="$CR_ROOT/cache/xdg"
export CIPD_CACHE_DIR="$CR_ROOT/cache/cipd"
export VPYTHON_VIRTUALENV_ROOT="$CR_ROOT/cache/vpython"
export DEPOT_TOOLS_UPDATE=0
export DEPOT_TOOLS_METRICS=0
export GIT_TERMINAL_PROMPT=0
export PYTHONDONTWRITEBYTECODE=1
export PATH="$CR_ROOT/depot_tools:/usr/bin:/bin:/usr/sbin:/sbin"
unset GIT_CACHE_PATH CCACHE_DIR CCACHE_PREFIX GOOGLE_APPLICATION_CREDENTIALS
mkdir -p "$CR_LOGS"
fail() { echo "::error::$*" >&2; exit 1; }
