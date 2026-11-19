#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
# Verify restored tree using only OS tools, never restored depot_tools wrappers.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
source "$(dirname "$0")/runner.sh"
source "$(dirname "$0")/relay.sh"
require_hosted_runner
require_relay_mount
require_relay_directory "$CR_ROOT"
require_relay_directory "$CR_TRANSFER"
require_no_mount_targets "$CR_ROOT" "$CR_TRANSFER"
require_relay_directory "$CR_TRANSFER/checkpoint"
require_empty_directory "$CR_ROOT"
checkpoint_tool="${GITHUB_WORKSPACE:?}/.github/scripts/checkpoint.py"
source_state_tool="${GITHUB_WORKSPACE:?}/.github/scripts/source_state.py"
[[ -r "$checkpoint_tool" && -r "$source_state_tool" && -x "$CR_PYTHON" ]] || fail 'Checkpoint restore tools missing'
command -v zstd >/dev/null || fail 'zstd missing'
"$CR_PYTHON" "$checkpoint_tool" restore-stream "$CR_TRANSFER/checkpoint" \
  | zstd -d -c | extract_relay_tree "$CR_ROOT"
"$CR_PYTHON" "$source_state_tool" verify-restore
# No gclient sync, runhooks, or gn gen here: keep all build inputs and mtimes.
rmdir -- "$CR_TRANSFER/checkpoint"
df -h /mnt
