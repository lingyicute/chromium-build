#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
# Use only OS tools, never restored depot_tools wrappers.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
source "$(dirname "$0")/runner.sh"
source "$(dirname "$0")/relay.sh"

require_hosted_runner
require_relay_mount
require_relay_directory "$CR_ROOT"
require_relay_directory "$CR_TRANSFER/checkpoint"
require_no_mount_targets "$CR_ROOT" "$CR_TRANSFER"
require_empty_directory "$CR_ROOT"

"$CR_PYTHON" "${GITHUB_WORKSPACE:?}/.github/scripts/checkpoint.py" restore-stream "$CR_TRANSFER/checkpoint" \
  | zstd -d -c | extract_relay_tree "$CR_ROOT"
"$CR_PYTHON" "$GITHUB_WORKSPACE/.github/scripts/source_state.py" verify-restore
# Skip gclient sync/runhooks/gn gen so restored inputs and nanosecond mtimes remain intact.
rmdir -- "$CR_TRANSFER/checkpoint"
df -h /mnt
