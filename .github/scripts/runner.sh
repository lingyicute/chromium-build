#!/usr/bin/env bash
# Sourced by the scripts that prepare or write the local relay disk.
runner_fail() { printf '::error::%s\n' "$*" >&2; exit 1; }

require_hosted_runner() {
  [[ "${RUNNER_ENVIRONMENT:-}" == github-hosted ]] || runner_fail 'GitHub-hosted runner required'
}

require_relay_mount() {
  local root_device
  [[ -d /mnt && ! -L /mnt ]] || runner_fail '/mnt must be a real directory'
  root_device=$(stat -c '%d' -- /) || runner_fail 'Cannot inspect root filesystem'
  relay_device=$(stat -c '%d' -- /mnt) || runner_fail 'Cannot inspect /mnt filesystem'
  [[ "$relay_device" != "$root_device" ]] || runner_fail 'A separate /mnt filesystem is required; refusing to use the OS disk for the build tree'
}

require_relay_directory() {
  local dir=$1 device
  [[ -d "$dir" && ! -L "$dir" ]] || runner_fail "Refusing non-directory or symlink: $dir"
  device=$(stat -c '%d' -- "$dir") || runner_fail "Cannot inspect filesystem for $dir"
  [[ "$device" == "$relay_device" ]] || runner_fail "Expected $dir on the /mnt filesystem"
}

require_empty_directory() {
  local dir=$1 entry
  # Print a marker rather than capturing filenames (which can be only newlines).
  entry=$(find "$dir" -mindepth 1 -maxdepth 1 -printf 'present' -quit) \
    || runner_fail "Cannot inspect contents of $dir"
  [[ -z "$entry" ]] || runner_fail "Expected an empty directory: $dir"
}

# st_dev cannot detect same-filesystem bind mounts. mountinfo paths use octal
# escapes, so compare decoded path boundaries rather than grepping for a prefix.
check_mount_targets() {
  local id parent device root encoded extra target dir seen=0
  while IFS=' ' read -r id parent device root encoded extra; do
    [[ -n "$encoded" ]] || runner_fail 'Malformed runner mount table'
    printf -v target '%b' "$encoded"
    seen=1
    for dir in "$@"; do
      if [[ "$target" == "$dir" || "$target" == "$dir/"* ]]; then
        runner_fail "Refusing mount at or below $dir: $target"
      fi
    done
  done
  (( seen )) || runner_fail 'Empty runner mount table'
}

require_no_mount_targets() {
  check_mount_targets "$@" < /proc/self/mountinfo || runner_fail 'Cannot inspect runner mounts'
}
