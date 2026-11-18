#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
# Export using only OS tools, never depot_tools wrappers inside $CR_ROOT.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
source "$(dirname "$0")/runner.sh"
source "$(dirname "$0")/relay.sh"
require_hosted_runner
require_relay_mount
require_relay_directory "$CR_ROOT"
require_relay_directory "$CR_TRANSFER"
require_no_mount_targets "$CR_ROOT" "$CR_TRANSFER"
mkdir -p "$CR_TRANSFER/checkpoint"
require_relay_directory "$CR_TRANSFER/checkpoint"
require_empty_directory "$CR_TRANSFER/checkpoint"
: >> "${GITHUB_OUTPUT:?}"
checkpoint_tool="${GITHUB_WORKSPACE:?}/.github/scripts/checkpoint.py"
[[ -r "$checkpoint_tool" && -x "$CR_PYTHON" ]] || fail 'Checkpoint writer missing'
command -v zstd >/dev/null || fail 'zstd missing'
# IMPORTANT: destructive export on a disposable VM. No commands may use the
# source tree afterwards. Each source file is removed after tar reads it, so a
# full uncompressed tree + a full archive need not fit simultaneously.
# Preserve hidden files, nanosecond mtimes, symlinks, .git, vpython and CIPD.
# zstd -T0 counts physical cores only (half the vCPUs of an SMT hosted runner),
# so ask for one worker per logical CPU: nothing else runs during the export.
hash=$(
  archive_relay_tree "$CR_ROOT" \
    | zstd -T"$(nproc)" -3 -c \
    | "$CR_PYTHON" "$checkpoint_tool" pack-stream "$CR_TRANSFER/checkpoint"
)
[[ "$hash" =~ ^[0-9a-f]{64}$ ]] || fail 'Invalid checkpoint SHA-256 output'
require_empty_directory "$CR_ROOT"
echo "sha256=$hash" >> "${GITHUB_OUTPUT:?}"
du -sh "$CR_TRANSFER/checkpoint"
