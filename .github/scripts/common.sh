#!/usr/bin/env bash
set -Eeuo pipefail

# Drop ambient toolchain, Git, and shell overrides before setting our pinned env.
unset "${!GIT_@}" "${!ANDROID_@}" "${!DEPOT_TOOLS_@}" "${!VPYTHON_@}" "${!CIPD_@}" \
  JAVA_HOME GRADLE_HOME ANT_HOME JAVA_TOOL_OPTIONS _JAVA_OPTIONS JDK_JAVA_OPTIONS CLASSPATH \
  PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV CONDA_PREFIX \
  PIP_CONFIG_FILE PIP_CACHE_DIR NPM_CONFIG_CACHE CARGO_HOME RUSTUP_HOME SSH_ASKPASS \
  CC CXX CFLAGS CXXFLAGS CPPFLAGS LDFLAGS CCACHE_DIR CCACHE_PREFIX NINJA_STATUS \
  CUSTOM_CIPD_CLIENT CHROMIUM_BUILDTOOLS_PATH GCLIENT_FILE LUCI_CONTEXT \
  GOOGLE_APPLICATION_CREDENTIALS BOTO_CONFIG BOTO_PATH NO_AUTH_BOTO_CONFIG \
  BASH_ENV ENV CDPATH GLOBIGNORE POSIXLY_CORRECT TAR_OPTIONS ZSTD_CLEVEL ZSTD_NBTHREADS

export LC_ALL=C.UTF-8 LANG=C.UTF-8 TZ=UTC
# Identical absolute paths across stages are required by GN/Ninja and vpython.
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
# Keep CIPD download cache outside $CR_ROOT so packages are not archived twice.
export CIPD_CACHE_DIR="$CR_TRANSFER/cipd-cache"
export VPYTHON_VIRTUALENV_ROOT="$CR_ROOT/cache/vpython"
export CHROME_HEADLESS=1
export DEPOT_TOOLS_UPDATE=0
export DEPOT_TOOLS_METRICS=0
export DEPOT_TOOLS_COLLECT_METRICS=0
export GIT_TERMINAL_PROMPT=0
export PYTHONUNBUFFERED=1
export PATH="$CR_ROOT/depot_tools:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$CR_LOGS" "$TMPDIR"
_retry_sleep_pid=''
_abort_signal() { [[ -z "$_retry_sleep_pid" ]] || kill "$_retry_sleep_pid" 2>/dev/null || true; echo "::error::$1" >&2; exit "$2"; }
trap '_abort_signal "Interrupted (SIGINT), aborting" 130' INT
trap '_abort_signal "Terminated (SIGTERM), aborting" 143' TERM

fail() { echo "::error::$*" >&2; exit 1; }

# Grant owner rwx on restricted directories (e.g. CIPD) so tar/rm can delete them.
make_dirs_removable() {
  local root=${1:-} bad
  [[ -n "$root" && -d "$root" && ! -L "$root" ]] || fail "Expected a real directory: ${root:-<empty>}"
  [[ "$root" == /* || "$root" == .* ]] || root="./$root"
  bad=$(find -P "$root" -type d ! -perm -u+rwx ! -exec chmod u+rwx -- {} \; -printf x -quit) \
    || fail "Could not traverse directories under $root"
  [[ -z "$bad" ]] || fail "Could not grant owner access under $root"
}

# Retry an idempotent network command against the same official source and revision.
retry() {
  (( $# > 0 )) || fail 'retry requires a command'
  local delay="${CR_RETRY_DELAY:-30}" attempt rc wait_s
  [[ "$delay" =~ ^[0-9]{1,4}$ ]] && (( 10#$delay <= 3600 )) || fail "Invalid CR_RETRY_DELAY: $delay"
  for attempt in 1 2 3; do
    rc=0
    "$@" || rc=$?
    (( rc == 0 )) && return 0
    if (( rc == 130 || rc == 143 )); then
      echo "::error::Interrupted (exit $rc), aborting without retry: $*" >&2
      exit "$rc"
    fi
    (( attempt < 3 )) || fail "Failed after 3 attempts: $* (exit $rc)"
    wait_s=$(( attempt * 10#$delay ))
    echo "::warning::Attempt $attempt failed (exit $rc), retrying in ${wait_s}s: $*" >&2
    if (( wait_s > 0 )); then
      # Background sleep + wait lets pending INT/TERM traps run immediately.
      sleep "$wait_s" </dev/null >/dev/null 2>&1 &
      _retry_sleep_pid=$!
      wait "$_retry_sleep_pid" || true
      _retry_sleep_pid=''
    fi
  done
}
