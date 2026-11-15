#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8 TZ=UTC
# Identical absolute paths in every job are essential for GN/Ninja and vpython.
export CR_ROOT=/mnt/chromium-relay
export CR_TRANSFER=/mnt/chromium-transfer
export CR_LOGS="${RUNNER_TEMP:?}/chromium-logs"
export CR_PYTHON=/usr/bin/python3
export TMPDIR="${RUNNER_TEMP:?}/chromium-tmp"
export HOME="$CR_ROOT/home"
export XDG_CACHE_HOME="$CR_ROOT/cache/xdg"
# Keep CIPD's download instance cache outside $CR_ROOT so already-deployed
# packages are not archived twice into every stage checkpoint.
export CIPD_CACHE_DIR="$CR_TRANSFER/cipd-cache"
export VPYTHON_VIRTUALENV_ROOT="$CR_ROOT/cache/vpython"
export CHROME_HEADLESS=1
export DEPOT_TOOLS_UPDATE=0
export DEPOT_TOOLS_METRICS=0
export DEPOT_TOOLS_COLLECT_METRICS=0
export GIT_TERMINAL_PROMPT=0
# Do NOT set PYTHONDONTWRITEBYTECODE: thousands of Python build actions would
# recompile their imports on every run; the .pyc caches survive checkpoints.
export PYTHONUNBUFFERED=1
export PATH="$CR_ROOT/depot_tools:/usr/bin:/bin:/usr/sbin:/sbin"
unset GIT_CACHE_PATH CCACHE_DIR CCACHE_PREFIX GOOGLE_APPLICATION_CREDENTIALS \
  ANDROID_HOME ANDROID_SDK_ROOT ANDROID_NDK ANDROID_NDK_HOME \
  ANDROID_NDK_ROOT ANDROID_NDK_LATEST_HOME JAVA_HOME \
  JAVA_TOOL_OPTIONS _JAVA_OPTIONS JDK_JAVA_OPTIONS CLASSPATH \
  PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV \
  CONDA_PREFIX PIP_CONFIG_FILE \
  CC CXX CFLAGS CXXFLAGS CPPFLAGS LDFLAGS \
  GIT_CONFIG_GLOBAL GIT_CONFIG_SYSTEM GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT
mkdir -p "$CR_LOGS" "$TMPDIR"
fail() { echo "::error::$*" >&2; exit 1; }
# Re-run an idempotent network step against the SAME official source and
# revision; never falls back to another mirror, branch, or version.
retry() {
  (( $# > 0 )) || fail 'retry requires a command'
  local delay="${CR_RETRY_DELAY:-30}"
  [[ "$delay" =~ ^[0-9]+$ ]] || fail "Invalid CR_RETRY_DELAY: $delay"
  local attempt rc
  for attempt in 1 2 3; do
    rc=0
    "$@" && return 0 || rc=$?
    if (( rc == 130 || rc == 143 )); then
      fail "Interrupted (exit $rc), aborting without retry: $*"
    fi
    (( attempt < 3 )) || fail "Failed after 3 attempts: $* (exit $rc)"
    echo "::warning::Attempt $attempt failed (exit $rc), retrying: $*" >&2
    sleep $(( attempt * delay ))
  done
}
