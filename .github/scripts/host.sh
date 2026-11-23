#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8 TZ=UTC
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
# Run ONLY on a fresh GitHub-hosted VM. Never use this cleanup on a personal host.
source "$(dirname "$0")/runner.sh"
require_hosted_runner
for cmd in sudo git curl cmp tar zstd sha256sum timeout fallocate mkswap swapon swapoff \
  free df du find sort xargs tee chmod chown nproc cp ln rm rmdir mkdir mktemp ls \
  cat id uname dirname tail sleep stat realpath; do
  command -v "$cmd" >/dev/null || { echo "Missing runner tool: $cmd; refusing apt/pip fallback" >&2; exit 1; }
done
[[ "$(uname -m)" == x86_64 ]] || { echo "Unsupported architecture: $(uname -m) (need x86_64)" >&2; exit 1; }
[[ -r /etc/os-release ]] || { echo 'Missing /etc/os-release' >&2; exit 1; }
. /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]] \
  || { echo "Unsupported image: ${ID:-unknown} ${VERSION_ID:-unknown} (need ubuntu 24.04)" >&2; exit 1; }
mkdir -p "${RUNNER_TEMP:?}/chromium-logs"
[[ -x /usr/bin/python3 ]] || { echo 'Missing /usr/bin/python3; refusing apt/pip fallback' >&2; exit 1; }
# Refuse an unexpected layout before any purge, chown or swap operation.
require_relay_mount
workdirs=(/mnt/chromium-relay /mnt/chromium-transfer)
purge_dirs=(/usr/local/lib/android /usr/local/.ghcup /usr/share/dotnet
  /usr/share/swift /usr/local/share/powershell /usr/lib/google-cloud-sdk
  /opt/ghc /opt/az)
require_no_mount_targets "${workdirs[@]}" "${purge_dirs[@]}"
for workdir in "${workdirs[@]}"; do
  if [[ -e "$workdir" || -L "$workdir" ]]; then
    require_relay_directory "$workdir"
    require_empty_directory "$workdir"
  fi
done
if [[ -e /chromium-relay.swap || -L /chromium-relay.swap ]]; then
  runner_fail 'Expected no pre-existing /chromium-relay.swap'
fi
# Disk layout: / (OS disk) receives the swap file. A separate /mnt disk is kept
# entirely for the Chromium tree and checkpoints; on a single-disk runner they
# share / with the swap and the free-space check below decides. Either way,
# freeing / is what makes room for the swap.
# Fixed paths only; no downloaded cleanup script, docker image, or action.
# Preserve /opt/hostedtoolcache and runner externals (Node required by actions).
# Unlinking is metadata-bound: delete the trees concurrently, and prepare /mnt
# meanwhile; only the swap file below needs the space they free on /.
cleanup=()
reap_cleanup() {
  local pid
  for pid in "${cleanup[@]}"; do
    wait "$pid" || true
  done
}
trap reap_cleanup EXIT
for dir in "${purge_dirs[@]}"; do
  sudo -n rm -rf --one-file-system -- "$dir" &
  cleanup+=("$!")
done
sudo -n mkdir -p -- "${workdirs[@]}"
for workdir in "${workdirs[@]}"; do
  require_relay_directory "$workdir"
  require_empty_directory "$workdir"
  sudo -n chown -h "$(id -u):$(id -g)" -- "$workdir"
done
# The image ships its own swap file on /mnt; reclaim that space for the tree.
# /proc/swaps uses the same fixed 3-digit octal escapes as /proc/self/mountinfo.
[[ -r /proc/swaps ]] || runner_fail 'Cannot inspect active runner swaps'
# Snapshot the table before any swapoff: /proc/swaps is regenerated on every
# read, so reading it incrementally while entries disappear can skip the next one.
mapfile swap_lines < /proc/swaps || runner_fail 'Cannot inspect active runner swaps'
[[ ${#swap_lines[@]} -ge 1 && "${swap_lines[0]}" == *$'\n' ]] || runner_fail 'Malformed /proc/swaps header'
for swap_line in "${swap_lines[@]:1}"; do
  [[ "$swap_line" == *$'\n' ]] || runner_fail 'Malformed /proc/swaps'
  IFS=$' \t' read -r encoded_dev swap_type _ <<< "${swap_line%$'\n'}" || continue
  [[ -n "$encoded_dev" && "$swap_type" == file ]] || continue
  decode_mount_path dev "$encoded_dev"
  [[ "$dev" == /mnt/* ]] || continue
  # Do not disable or remove a device, symlink, bind mount, or foreign file.
  [[ -f "$dev" && ! -L "$dev" ]] || continue
  realpath_exact real_dev "$dev" || continue
  [[ "$real_dev" == /mnt/* && -f "$real_dev" && "$(stat -c '%d' -- "$real_dev")" == "$relay_device" ]] || continue
  swap_checks=("$real_dev")
  swap_parent=${real_dev%/*}
  while [[ "$swap_parent" == /mnt/* ]]; do
    swap_checks+=("$swap_parent")
    swap_parent=${swap_parent%/*}
  done
  require_no_mount_targets "${swap_checks[@]}"
  sudo -n swapoff -- "$real_dev" < /dev/null
  sudo -n rm -f -- "$real_dev" < /dev/null
done
# Reap every background deletion before failing so none keep running on error.
cleanup_failed=0
for pid in "${cleanup[@]}"; do
  wait "$pid" || cleanup_failed=1
done
cleanup=()
(( cleanup_failed == 0 )) || { echo 'Failed to remove preinstalled runner directories' >&2; exit 1; }
# Swap is only a fallback for linker/R8 peaks, not a replacement for physical
# RAM. 16 GiB target; shrink only if / is unexpectedly small, never below 8 GiB.
# On single-disk runners (/ and /mnt share one filesystem), keep >= 46 GiB free
# so the 45 GiB tree headroom check still passes after swap extent overhead.
min_root_free_gib=8
(( ! relay_shares_root )) || min_root_free_gib=46
swap_gib=16
root_free_gib=$(/usr/bin/python3 -c 'import shutil; print(shutil.disk_usage("/").free // 2**30)')
(( root_free_gib - swap_gib >= min_root_free_gib )) || swap_gib=$(( root_free_gib - min_root_free_gib ))
(( swap_gib >= 8 )) || { echo "Only ${root_free_gib} GiB free on / after cleanup; cannot place a >= 8 GiB swap file while keeping >= ${min_root_free_gib} GiB free" >&2; exit 1; }
sudo -n bash -c 'set -o noclobber; umask 077; : > /chromium-relay.swap && exec fallocate -l "$1" /chromium-relay.swap' _ "${swap_gib}G"
sudo -n chmod 600 /chromium-relay.swap
sudo -n mkswap /chromium-relay.swap >/dev/null
sudo -n swapon /chromium-relay.swap
if [[ -e /proc/sys/vm/swappiness ]]; then
  printf '10\n' | sudo -n tee /proc/sys/vm/swappiness >/dev/null
fi
/usr/bin/python3 - <<'PY'
import shutil
free = shutil.disk_usage('/mnt/chromium-relay').free / 2**30
print(f'Free on /mnt for the tree: {free:.1f} GiB')
if free < 45:
    raise SystemExit('Less than 45 GiB free: aborting before downloading Chromium. This threshold does NOT guarantee a successful build.')
if free < 100:
    print('::warning::Below Chromium documented 100 GB free disk requirement; best-effort build only.')
PY
swappiness=$(cat /proc/sys/vm/swappiness 2>/dev/null || echo unknown)
if (( relay_shares_root )); then relay_layout=shared-with-os-disk; else relay_layout=separate-disk; fi
{ df -hT / /mnt; free -h; swapon --show; printf 'CPUs=%s\nSwappiness=%s\nRelayDisk=%s\nImageOS=%s\nImageVersion=%s\n' "$(nproc)" "$swappiness" "$relay_layout" "${ImageOS:-unknown}" "${ImageVersion:-unknown}"; } | tee "$RUNNER_TEMP/chromium-logs/host.txt"
