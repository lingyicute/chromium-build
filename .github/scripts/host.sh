#!/usr/bin/env bash
set -Eeuo pipefail
# Run ONLY on a fresh GitHub-hosted VM. Never use this cleanup on a personal host.
[[ "${RUNNER_ENVIRONMENT:-}" == github-hosted ]] || { echo 'GitHub-hosted runner required'; exit 1; }
[[ "$(uname -m)" == x86_64 ]] || { echo "Unsupported architecture: $(uname -m) (need x86_64)"; exit 1; }
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || { echo "Unsupported image: $ID $VERSION_ID (need ubuntu 24.04)"; exit 1; }
mkdir -p "$RUNNER_TEMP/chromium-logs"
date +%s > "$RUNNER_TEMP/chromium-job-start"
for cmd in git python3 tar zstd split sha256sum timeout fallocate mkswap swapon swapoff free df; do
  command -v "$cmd" >/dev/null || { echo "Missing runner tool: $cmd; refusing apt/pip fallback"; exit 1; }
done
# Disk layout: / (OS disk) receives the swap file; /mnt (the larger ephemeral
# disk) is kept entirely for the Chromium tree and checkpoints. Freeing / is
# what makes room for the swap below.
# Fixed paths only; no downloaded cleanup script, docker image, or action.
# Preserve /opt/hostedtoolcache and runner externals (Node required by actions).
sudo rm -rf -- /usr/local/lib/android /usr/local/.ghcup /usr/share/dotnet \
  /usr/share/swift /usr/local/share/powershell /usr/lib/google-cloud-sdk \
  /opt/ghc /opt/az
sudo mkdir -p /mnt/chromium-relay /mnt/chromium-transfer
sudo chown "$(id -u):$(id -g)" /mnt/chromium-relay /mnt/chromium-transfer
# The image ships its own swap file on /mnt; reclaim that space for the tree.
while read -r dev; do
  [[ "$dev" == /mnt/* ]] || continue
  sudo swapoff "$dev" && sudo rm -f "$dev"
done < <(swapon --show=NAME --noheadings)
# Swap is only a fallback for linker/R8 peaks, not a replacement for physical
# RAM. 16 GiB target; shrink only if / is unexpectedly small, never below 8 GiB.
swap_gib=16
root_free_gib=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
(( root_free_gib - swap_gib >= 8 )) || swap_gib=$(( root_free_gib - 8 ))
(( swap_gib >= 8 )) || { echo "Only ${root_free_gib} GiB free on / after cleanup; cannot place a >= 8 GiB swap file"; exit 1; }
sudo fallocate -l "${swap_gib}G" /chromium-relay.swap
sudo chmod 600 /chromium-relay.swap
sudo mkswap /chromium-relay.swap >/dev/null
sudo swapon /chromium-relay.swap
python3 - <<'PY'
import shutil
free = shutil.disk_usage('/mnt/chromium-relay').free / 2**30
print(f'Free on /mnt for the tree: {free:.1f} GiB')
if free < 45:
    raise SystemExit('Less than 45 GiB free: aborting before downloading Chromium. This threshold does NOT guarantee a successful build.')
if free < 100:
    print('::warning::Below Chromium documented 100 GB free disk requirement; best-effort build only.')
PY
{ df -h / /mnt; free -h; swapon --show; printf 'ImageOS=%s\nImageVersion=%s\n' "${ImageOS:-unknown}" "${ImageVersion:-unknown}"; } | tee "$RUNNER_TEMP/chromium-logs/host.txt"
