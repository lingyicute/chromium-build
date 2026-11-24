#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8 TZ=UTC
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
# Run ONLY on a fresh GitHub-hosted VM; never on a personal host.
source "$(dirname "$0")/runner.sh"
require_hosted_runner

for cmd in sudo git curl tar zstd sha256sum timeout fallocate mkswap swapon swapoff; do
  command -v "$cmd" >/dev/null || runner_fail "Missing runner tool: $cmd"
done
[[ -x /usr/bin/python3 ]] || runner_fail 'Missing /usr/bin/python3'
[[ "$(uname -m)" == x86_64 ]] || runner_fail "Unsupported architecture: $(uname -m) (need x86_64)"
[[ -r /etc/os-release ]] && . /etc/os-release || runner_fail 'Missing /etc/os-release'
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]] \
  || runner_fail "Unsupported image: ${ID:-unknown} ${VERSION_ID:-unknown} (need ubuntu 24.04)"

mkdir -p "${RUNNER_TEMP:?}/chromium-logs"
require_relay_mount

workdirs=(/mnt/chromium-relay /mnt/chromium-transfer)
# Large preinstalled toolchains not needed here; preserves /opt/hostedtoolcache and Node.
purge_dirs=(
  /usr/local/lib/android /usr/local/.ghcup /usr/share/dotnet
  /usr/share/swift /usr/local/share/powershell /usr/lib/google-cloud-sdk
  /opt/ghc /opt/az
)
require_no_mount_targets "${workdirs[@]}" "${purge_dirs[@]}"
for dir in "${workdirs[@]}"; do
  if [[ -e "$dir" || -L "$dir" ]]; then
    require_relay_directory "$dir"
    require_empty_directory "$dir"
  fi
done
[[ ! -e /chromium-relay.swap && ! -L /chromium-relay.swap ]] \
  || runner_fail 'Expected no pre-existing /chromium-relay.swap'

# Remove preinstalled SDKs concurrently to free / for swap.
cleanup=()
trap 'for pid in "${cleanup[@]}"; do wait "$pid" || true; done' EXIT
for dir in "${purge_dirs[@]}"; do
  sudo -n rm -rf --one-file-system -- "$dir" &
  cleanup+=("$!")
done

sudo -n mkdir -p -- "${workdirs[@]}"
for dir in "${workdirs[@]}"; do
  require_relay_directory "$dir"
  require_empty_directory "$dir"
  sudo -n chown -h "$(id -u):$(id -g)" -- "$dir"
done

# Disable and remove any preinstalled file-backed swap on /mnt to reclaim relay space.
exec {swap_fd}< <(/usr/bin/python3 - "$relay_device" <<'PY'
import os, re, stat, sys
relay_dev = int(sys.argv[1])
with open('/proc/swaps', 'rb') as f:
    data = f.read()
if not data or not data.endswith(b'\n'):
    raise SystemExit('Malformed /proc/swaps')
for line in data.splitlines()[1:]:
    cols = line.split()
    if len(cols) < 2 or cols[1] != b'file':
        continue
    path = re.sub(rb'\\([0-3][0-7]{2})', lambda m: bytes([int(m[1], 8)]), cols[0])
    if b'\x00' in path or not path.startswith(b'/mnt/'):
        continue
    try:
        st = os.lstat(path)
        real = os.path.realpath(path, strict=True)
    except OSError:
        continue
    if stat.S_ISREG(st.st_mode) and real.startswith(b'/mnt/') and st.st_dev == relay_dev:
        sys.stdout.buffer.write(real + b'\0')
PY
)
swap_pid=$!
while IFS= read -r -d '' swap_file <&"$swap_fd"; do
  swap_checks=("$swap_file")
  swap_parent=${swap_file%/*}
  while [[ "$swap_parent" == /mnt/* ]]; do
    swap_checks+=("$swap_parent")
    swap_parent=${swap_parent%/*}
  done
  require_no_mount_targets "${swap_checks[@]}"
  sudo -n swapoff -- "$swap_file" < /dev/null
  sudo -n rm -f -- "$swap_file" < /dev/null
done
exec {swap_fd}<&-
wait "$swap_pid" || runner_fail 'Cannot inspect active runner swaps'

cleanup_failed=0
for pid in "${cleanup[@]}"; do
  wait "$pid" || cleanup_failed=1
done
cleanup=()
(( cleanup_failed == 0 )) || runner_fail 'Failed to remove preinstalled runner directories'

# Target 16 GiB swap on / (min 8 GiB). Keep >= 46 GiB free on single-disk runners
# so the 45 GiB Chromium headroom check below passes with extent overhead.
min_free_gib=$(( relay_shares_root ? 46 : 8 ))
root_free_gib=$(/usr/bin/python3 -c 'import shutil; print(shutil.disk_usage("/").free // 2**30)')
swap_gib=$(( root_free_gib - min_free_gib ))
(( swap_gib <= 16 )) || swap_gib=16
(( swap_gib >= 8 )) \
  || runner_fail "Only ${root_free_gib} GiB free on / after cleanup; need >= $((min_free_gib + 8)) GiB"

sudo -n bash -c 'set -o noclobber; umask 077; : > /chromium-relay.swap && exec fallocate -l "$1" /chromium-relay.swap' _ "${swap_gib}G"
sudo -n chmod 600 /chromium-relay.swap
sudo -n mkswap /chromium-relay.swap >/dev/null
sudo -n swapon /chromium-relay.swap
[[ ! -e /proc/sys/vm/swappiness ]] || printf '10\n' | sudo -n tee /proc/sys/vm/swappiness >/dev/null

/usr/bin/python3 - <<'PY'
import shutil
free = shutil.disk_usage('/mnt/chromium-relay').free / 2**30
print(f'Free on /mnt for the tree: {free:.1f} GiB')
if free < 45:
    raise SystemExit('Less than 45 GiB free: aborting before downloading Chromium.')
if free < 100:
    print('::warning::Below Chromium documented 100 GB free disk requirement; best-effort build only.')
PY

relay_layout=$( (( relay_shares_root )) && echo shared-with-os-disk || echo separate-disk )
swappiness=$(cat /proc/sys/vm/swappiness 2>/dev/null || echo unknown)
{
  df -hT / /mnt
  free -h
  swapon --show
  printf 'CPUs=%s\nSwappiness=%s\nRelayDisk=%s\nImageOS=%s\nImageVersion=%s\n' \
    "$(nproc)" "$swappiness" "$relay_layout" "${ImageOS:-unknown}" "${ImageVersion:-unknown}"
} | tee "$RUNNER_TEMP/chromium-logs/host.txt"
