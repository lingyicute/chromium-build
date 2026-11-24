#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
# Use only OS tools, never depot_tools wrappers inside $CR_ROOT.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
source "$(dirname "$0")/runner.sh"
source "$(dirname "$0")/relay.sh"

require_hosted_runner
require_relay_mount
require_relay_directory "$CR_ROOT"
require_relay_directory "$CR_TRANSFER"
require_no_mount_targets "$CR_ROOT" "$CR_TRANSFER"

mkdir -p "$CR_TRANSFER/checkpoint"
require_empty_directory "$CR_TRANSFER/checkpoint"
: >> "${GITHUB_OUTPUT:?}"

# Destructive export: tar removes each file after reading so tree + archive need not coexist.
# zstd -T0 uses physical cores only; pass $(nproc) to use all vCPUs on SMT hosted runners.
hash=$(
  archive_relay_tree "$CR_ROOT" \
    | zstd -T"$(nproc)" -3 -c \
    | "$CR_PYTHON" "${GITHUB_WORKSPACE:?}/.github/scripts/checkpoint.py" pack-stream "$CR_TRANSFER/checkpoint"
)
[[ "$hash" =~ ^[0-9a-f]{64}$ ]] || fail 'Invalid checkpoint SHA-256 output'
require_empty_directory "$CR_ROOT"
echo "sha256=$hash" >> "$GITHUB_OUTPUT"
du -sh "$CR_TRANSFER/checkpoint"
