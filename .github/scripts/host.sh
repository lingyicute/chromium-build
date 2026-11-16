#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8 TZ=UTC
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
# Run ONLY on a fresh GitHub-hosted VM. Never use this cleanup on a personal host.
[[ "${RUNNER_ENVIRONMENT:-}" == github-hosted ]] || { echo 'GitHub-hosted runner required' >&2; exit 1; }
for cmd in sudo git tar zstd sha256sum timeout fallocate mkswap swapon swapoff \
  free df du find sort xargs tee chmod chown nproc cp rm rmdir mkdir mktemp ls \
  cat id uname dirname tail sleep; do
  command -v "$cmd" >/dev/null || { echo "Missing runner tool: $cmd; refusing apt/pip fallback" >&2; exit 1; }
done
[[ "$(uname -m)" == x86_64 ]] || { echo "Unsupported architecture: $(uname -m) (need x86_64)" >&2; exit 1; }
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || { echo "Unsupported image: $ID $VERSION_ID (need ubuntu 24.04)" >&2; exit 1; }
mkdir -p "${RUNNER_TEMP:?}/chromium-logs"
[[ -x /usr/bin/python3 ]] || { echo 'Missing /usr/bin/python3; refusing apt/pip fallback' >&2; exit 1; }
# Record monotonic boot time so NTP wall-clock adjustments cannot skew stage budgets.
/usr/bin/python3 -c 'import time; print(int(time.clock_gettime(time.CLOCK_BOOTTIME)))' > "$RUNNER_TEMP/chromium-job-start"
# Disk layout: / (OS disk) receives the swap file; /mnt (the larger ephemeral
# disk) is kept entirely for the Chromium tree and checkpoints. Freeing / is
# what makes room for the swap below.
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
for dir in /usr/local/lib/android /usr/local/.ghcup /usr/share/dotnet \
  /usr/share/swift /usr/local/share/powershell /usr/lib/google-cloud-sdk \
  /opt/ghc /opt/az; do
  sudo rm -rf -- "$dir" &
  cleanup+=("$!")
done
sudo mkdir -p /mnt/chromium-relay /mnt/chromium-transfer
sudo chown "$(id -u):$(id -g)" /mnt/chromium-relay /mnt/chromium-transfer
# The image ships its own swap file on /mnt; reclaim that space for the tree.
while IFS= read -r dev; do
  [[ "$dev" == /mnt/* ]] || continue
  sudo swapoff -- "$dev" && sudo rm -f -- "$dev"
done < <(swapon --show=NAME --noheadings --raw)
# Reap every background deletion before failing so none keep running on error.
cleanup_failed=0
for pid in "${cleanup[@]}"; do
  wait "$pid" || cleanup_failed=1
done
cleanup=()
(( cleanup_failed == 0 )) || { echo 'Failed to remove preinstalled runner directories' >&2; exit 1; }
# Swap is only a fallback for linker/R8 peaks, not a replacement for physical
# RAM. 16 GiB target; shrink only if / is unexpectedly small, never below 8 GiB.
swap_gib=16
root_free_gib=$(/usr/bin/python3 -c 'import shutil; print(shutil.disk_usage("/").free // 2**30)')
(( root_free_gib - swap_gib >= 8 )) || swap_gib=$(( root_free_gib - 8 ))
(( swap_gib >= 8 )) || { echo "Only ${root_free_gib} GiB free on / after cleanup; cannot place a >= 8 GiB swap file" >&2; exit 1; }
sudo fallocate -l "${swap_gib}G" /chromium-relay.swap
sudo chmod 600 /chromium-relay.swap
sudo mkswap /chromium-relay.swap >/dev/null
sudo swapon /chromium-relay.swap
if [[ -e /proc/sys/vm/swappiness ]]; then
  printf '10\n' | sudo tee /proc/sys/vm/swappiness >/dev/null
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
{ df -hT / /mnt; free -h; swapon --show; printf 'CPUs=%s\nSwappiness=%s\nImageOS=%s\nImageVersion=%s\n' "$(nproc)" "$swappiness" "${ImageOS:-unknown}" "${ImageVersion:-unknown}"; } | tee "$RUNNER_TEMP/chromium-logs/host.txt"
