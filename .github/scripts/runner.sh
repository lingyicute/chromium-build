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
  [[ -n "${relay_device:-}" ]] || runner_fail 'Relay filesystem check has not run'
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

# Linux mangle_path() emits fixed 3-digit octal escapes (\040, \011, \012, \134).
# Decode in one pass so a following digit (e.g. \0401) is not folded into \0NNN.
decode_mount_path() {
  local __dm_out=$1 __dm_rest=$2 __dm_acc='' __dm_prefix __dm_esc __dm_ch
  while [[ "$__dm_rest" == *\\* ]]; do
    __dm_prefix=${__dm_rest%%\\*}
    __dm_rest=${__dm_rest#*\\}
    [[ ${#__dm_rest} -ge 3 && "${__dm_rest:0:3}" =~ ^[0-3][0-7]{2}$ && "${__dm_rest:0:3}" != 000 ]] \
      || runner_fail 'Malformed runner mount table'
    __dm_esc=${__dm_rest:0:3}
    __dm_rest=${__dm_rest:3}
    printf -v __dm_ch '\\0%s' "$__dm_esc"
    printf -v __dm_ch '%b' "$__dm_ch"
    __dm_acc+="${__dm_prefix}${__dm_ch}"
  done
  printf -v "$__dm_out" '%s' "${__dm_acc}${__dm_rest}"
}

# st_dev cannot detect same-filesystem bind mounts. mountinfo paths use octal
# escapes, so compare decoded path boundaries rather than grepping for a prefix.
check_mount_targets() {
  (( $# > 0 )) || runner_fail 'Mount check requires at least one path'
  local id='' parent='' device='' root='' encoded='' extra='' target dir seen=0
  while IFS=' ' read -r id parent device root encoded extra; do
    [[ -n "$encoded" && " $extra " == *' - '* ]] || runner_fail 'Malformed runner mount table'
    decode_mount_path target "$encoded"
    [[ "$target" == /* ]] || runner_fail 'Malformed runner mount table'
    seen=1
    for dir in "$@"; do
      if [[ "$dir" == / || "$target" == "$dir" || "$target" == "$dir/"* ]]; then
        runner_fail "Refusing mount at or below $dir: $target"
      fi
    done
    id=''; parent=''; device=''; root=''; encoded=''; extra=''
  done
  [[ -z "${id}${parent}${device}${root}${encoded}${extra}" ]] || runner_fail 'Malformed runner mount table'
  (( seen )) || runner_fail 'Empty runner mount table'
}

require_no_mount_targets() {
  check_mount_targets "$@" < /proc/self/mountinfo || runner_fail 'Cannot inspect runner mounts'
}
