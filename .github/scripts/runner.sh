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

normalize_mount_path() {
  local __nm_out=$1 __nm_path=$2
  [[ "$__nm_path" == /* ]] || runner_fail "Mount check path must be absolute: $__nm_path"
  while [[ "$__nm_path" != / && "$__nm_path" == */ ]]; do
    __nm_path=${__nm_path%/}
  done
  case "$__nm_path" in
    *//*|*/.|*/..|*/./*|*/../*)
      runner_fail "Mount check path must be canonical: $__nm_path"
      ;;
  esac
  printf -v "$__nm_out" '%s' "$__nm_path"
}

# Resolve symlinked ancestor directories without dereferencing the leaf itself,
# matching the canonical paths recorded in /proc/self/mountinfo.
resolve_mount_parent() {
  local __rm_out=$1 __rm_path='' __rm_suffix='' __rm_parent __rm_resolved
  normalize_mount_path __rm_path "$2"
  if [[ "$__rm_path" == / ]]; then
    printf -v "$__rm_out" '%s' /
    return
  fi
  __rm_parent=${__rm_path%/*}
  __rm_suffix="/${__rm_path##*/}"
  while [[ -n "$__rm_parent" && ! -e "$__rm_parent" ]]; do
    __rm_suffix="/${__rm_parent##*/}${__rm_suffix}"
    __rm_parent=${__rm_parent%/*}
  done
  __rm_resolved=$(realpath -e -- "${__rm_parent:-/}") || runner_fail "Cannot resolve ancestor of $__rm_path"
  [[ "$__rm_resolved" != / ]] || __rm_resolved=''
  printf -v "$__rm_out" '%s' "${__rm_resolved}${__rm_suffix}"
}

# st_dev cannot detect same-filesystem bind mounts. mountinfo paths use octal
# escapes, so compare decoded path boundaries rather than grepping for a prefix.
check_mount_targets() {
  (( $# > 0 )) || runner_fail 'Mount check requires at least one path'
  local id='' parent='' device='' root='' encoded='' extra='' target dir norm seen=0
  local -a dirs=()
  for dir in "$@"; do
    normalize_mount_path norm "$dir"
    dirs+=("$norm")
  done
  while IFS=' ' read -r id parent device root encoded extra; do
    [[ -n "$encoded" && " $extra " == *' - '* ]] || runner_fail 'Malformed runner mount table'
    decode_mount_path target "$encoded"
    [[ "$target" == /* ]] || runner_fail 'Malformed runner mount table'
    seen=1
    for dir in "${dirs[@]}"; do
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
  (( $# > 0 )) || runner_fail 'Mount check requires at least one path'
  local arg norm resolved
  local -a targets=()
  for arg in "$@"; do
    normalize_mount_path norm "$arg"
    targets+=("$norm")
    resolve_mount_parent resolved "$norm"
    [[ "$resolved" == "$norm" ]] || targets+=("$resolved")
  done
  check_mount_targets "${targets[@]}" < /proc/self/mountinfo || runner_fail 'Cannot inspect runner mounts'
}
