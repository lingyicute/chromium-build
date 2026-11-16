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
export XDG_CONFIG_HOME="$CR_ROOT/cache/xdg-config"
export XDG_DATA_HOME="$CR_ROOT/cache/xdg-data"
export XDG_STATE_HOME="$CR_ROOT/cache/xdg-state"
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
  CUSTOM_CIPD_CLIENT \
  ANDROID_HOME ANDROID_SDK_ROOT ANDROID_NDK ANDROID_NDK_HOME \
  ANDROID_NDK_ROOT ANDROID_NDK_LATEST_HOME JAVA_HOME GRADLE_HOME ANT_HOME \
  JAVA_TOOL_OPTIONS _JAVA_OPTIONS JDK_JAVA_OPTIONS CLASSPATH \
  PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV \
  CONDA_PREFIX PIP_CONFIG_FILE PIP_CACHE_DIR NPM_CONFIG_CACHE \
  CARGO_HOME RUSTUP_HOME \
  CC CXX CFLAGS CXXFLAGS CPPFLAGS LDFLAGS \
  GIT_CONFIG_GLOBAL GIT_CONFIG_SYSTEM GIT_CONFIG_NOSYSTEM \
  GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT \
  GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CEILING_DIRECTORIES
mkdir -p "$CR_LOGS" "$TMPDIR"
trap 'echo "::error::Interrupted (signal INT), aborting" >&2; exit 130' INT
trap 'echo "::error::Terminated (signal TERM), aborting" >&2; exit 143' TERM
fail() { echo "::error::$*" >&2; exit 1; }
# Re-run an idempotent network step against the SAME official source and
# revision; never falls back to another mirror, branch, or version.
retry() {
  (( $# > 0 )) || fail 'retry requires a command'
  local delay="${CR_RETRY_DELAY:-30}"
  [[ "$delay" =~ ^[0-9]+$ ]] || fail "Invalid CR_RETRY_DELAY: $delay"
  local attempt rc interrupted=0 prev_int prev_term
  prev_int=$(trap -p INT)
  prev_term=$(trap -p TERM)
  trap 'interrupted=130' INT
  trap 'interrupted=143' TERM
  for attempt in 1 2 3; do
    rc=0
    "$@" || rc=$?
    if (( interrupted != 0 )); then
      rc=$interrupted
    elif (( rc == 0 )); then
      eval "${prev_int:-trap - INT}"
      eval "${prev_term:-trap - TERM}"
      return 0
    fi
    if (( rc == 130 || rc == 143 )); then
      eval "${prev_int:-trap - INT}"
      eval "${prev_term:-trap - TERM}"
      echo "::error::Interrupted (exit $rc), aborting without retry: $*" >&2
      exit "$rc"
    fi
    if (( attempt >= 3 )); then
      eval "${prev_int:-trap - INT}"
      eval "${prev_term:-trap - TERM}"
      fail "Failed after 3 attempts: $* (exit $rc)"
    fi
    echo "::warning::Attempt $attempt failed (exit $rc), retrying: $*" >&2
    sleep $(( attempt * 10#$delay )) || true
    if (( interrupted != 0 )); then
      eval "${prev_int:-trap - INT}"
      eval "${prev_term:-trap - TERM}"
      echo "::error::Interrupted (exit $interrupted), aborting without retry: $*" >&2
      exit "$interrupted"
    fi
  done
}
