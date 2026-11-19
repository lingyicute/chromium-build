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
  CUSTOM_CIPD_CLIENT CIPD_SERVICE_URL CIPD_HTTP_USER_AGENT_PREFIX \
  DEPOT_TOOLS_DIR DEPOT_TOOLS_UNAME_S DEPOT_TOOLS_BOOTSTRAP_PYTHON3 \
  DEPOT_TOOLS_GSUTIL_BIN_DIR CHROMIUM_BUILDTOOLS_PATH GCLIENT_FILE \
  VPYTHON_BYPASS VPYTHON_DEFAULT_SPEC LUCI_CONTEXT \
  BOTO_CONFIG BOTO_PATH NO_AUTH_BOTO_CONFIG \
  ANDROID_HOME ANDROID_SDK_ROOT ANDROID_NDK ANDROID_NDK_HOME \
  ANDROID_NDK_ROOT ANDROID_NDK_LATEST_HOME JAVA_HOME GRADLE_HOME ANT_HOME \
  JAVA_TOOL_OPTIONS _JAVA_OPTIONS JDK_JAVA_OPTIONS CLASSPATH \
  PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV \
  CONDA_PREFIX PIP_CONFIG_FILE PIP_CACHE_DIR NPM_CONFIG_CACHE \
  CARGO_HOME RUSTUP_HOME \
  CC CXX CFLAGS CXXFLAGS CPPFLAGS LDFLAGS NINJA_STATUS \
  GIT_CONFIG_GLOBAL GIT_CONFIG_SYSTEM GIT_CONFIG_NOSYSTEM \
  GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT \
  GIT_DIR GIT_COMMON_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CEILING_DIRECTORIES \
  GIT_SSH GIT_SSH_COMMAND GIT_ASKPASS SSH_ASKPASS GIT_EXEC_PATH \
  GIT_TEMPLATE_DIR GIT_NO_REPLACE_OBJECTS GIT_REPLACE_REF_BASE \
  GIT_NAMESPACE GIT_EXTERNAL_DIFF \
  GLOBIGNORE TAR_OPTIONS ZSTD_CLEVEL ZSTD_NBTHREADS
mkdir -p "$CR_LOGS" "$TMPDIR"
trap 'echo "::error::Interrupted (signal INT), aborting" >&2; exit 130' INT
trap 'echo "::error::Terminated (signal TERM), aborting" >&2; exit 143' TERM
fail() { echo "::error::$*" >&2; exit 1; }
# GNU find's -exec ... \; reports failure as a false predicate, not a failing
# find exit code. Stop on that predicate explicitly, before traversing the dir.
# Prefix a non-empty marker so a newline-only name survives command substitution.
make_dirs_removable() {
  local root=$1 failed_dir
  [[ -n "$root" ]] || fail 'make_dirs_removable requires a directory path'
  [[ "$root" == /* || "$root" == . || "$root" == ./* || "$root" == .. || "$root" == ../* ]] \
    || root="./$root"
  failed_dir=$(find -P "$root" -type d ! -perm -u+rwx ! -exec chmod u+rwx -- {} \; -printf 'failed:%p' -quit) \
    || fail "Could not traverse removable directories under $1"
  [[ -z "$failed_dir" ]] || fail "Could not grant owner access to ${failed_dir#failed:}"
}
# Re-run an idempotent network step against the SAME official source and
# revision; never falls back to another mirror, branch, or version.
retry() {
  (( $# > 0 )) || fail 'retry requires a command'
  local delay="${CR_RETRY_DELAY:-30}"
  { [[ "$delay" =~ ^[0-9]{1,4}$ ]] && (( 10#$delay <= 3600 )); } || fail "Invalid CR_RETRY_DELAY: $delay"
  local attempt rc wait_s sleep_pid=0 interrupted=0 prev_int prev_term
  prev_int=$(trap -p INT)
  prev_term=$(trap -p TERM)
  trap 'interrupted=130; (( sleep_pid == 0 )) || kill "$sleep_pid" 2>/dev/null || true' INT
  trap 'interrupted=143; (( sleep_pid == 0 )) || kill "$sleep_pid" 2>/dev/null || true' TERM
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
    if (( interrupted == 0 )); then
      echo "::warning::Attempt $attempt failed (exit $rc), retrying: $*" >&2
    fi
    wait_s=$(( attempt * 10#$delay ))
    rc=0
    if (( wait_s > 0 && interrupted == 0 )); then
      sleep "$wait_s" &
      sleep_pid=$!
      if (( interrupted != 0 )); then
        kill "$sleep_pid" 2>/dev/null || true
      fi
      wait "$sleep_pid" || rc=$?
      wait "$sleep_pid" 2>/dev/null || true
      sleep_pid=0
    fi
    (( interrupted == 0 )) || rc=$interrupted
    if (( rc == 130 || rc == 143 )); then
      eval "${prev_int:-trap - INT}"
      eval "${prev_term:-trap - TERM}"
      echo "::error::Interrupted (exit $rc), aborting without retry: $*" >&2
      exit "$rc"
    fi
  done
}
