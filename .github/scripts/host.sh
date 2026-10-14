#!/usr/bin/env bash
set -Eeuo pipefail
# Run ONLY on a fresh GitHub-hosted VM. Never use this cleanup on a personal host.
[[ "${RUNNER_ENVIRONMENT:-}" == github-hosted ]] || { echo 'GitHub-hosted runner required'; exit 1; }
[[ "$(uname -m)" == x86_64 ]] || exit 1
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || exit 1
mkdir -p "$RUNNER_TEMP/chromium-logs"
date +%s > "$RUNNER_TEMP/chromium-job-start"
for cmd in git python3 tar zstd split sha256sum timeout fallocate mkswap swapon free; do
  command -v "$cmd" >/dev/null || { echo "Missing runner tool: $cmd; refusing apt/pip fallback"; exit 1; }
done
# Fixed paths only; no downloaded cleanup script, docker image, or action.
# Preserve /opt/hostedtoolcache and runner externals (Node required by actions).
sudo rm -rf -- /usr/local/lib/android /usr/local/.ghcup /usr/share/dotnet \
  /usr/share/swift /usr/local/share/powershell /usr/lib/google-cloud-sdk \
  /opt/ghc /opt/az
sudo mkdir -p /mnt/chromium-relay /mnt/chromium-transfer
sudo chown "$(id -u):$(id -g)" /mnt/chromium-relay /mnt/chromium-transfer
# Swap is only a fallback, not a replacement for physical RAM.
sudo fallocate -l 8G /mnt/chromium-relay.swap
sudo chmod 600 /mnt/chromium-relay.swap
sudo mkswap /mnt/chromium-relay.swap >/dev/null
sudo swapon /mnt/chromium-relay.swap
python3 - <<'PY'
import shutil
free = shutil.disk_usage('/mnt/chromium-relay').free / 2**30
print(f'Free after cleanup and swap: {free:.1f} GiB')
if free < 45:
    raise SystemExit('Less than 45 GiB free: aborting before downloading Chromium. This threshold does NOT guarantee a successful build.')
if free < 100:
    print('::warning::Below Chromium documented 100 GB free disk requirement; best-effort build only.')
PY
{ df -h / /mnt; free -h; printf 'ImageOS=%s\nImageVersion=%s\n' "${ImageOS:-unknown}" "${ImageVersion:-unknown}"; } | tee "$RUNNER_TEMP/chromium-logs/host.txt"
