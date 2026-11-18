#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
# Keep the supervisor at the step PID so it receives the runner's signals.
# Diagnostics are best effort; tee must not mask the supervisor's exit status.
exec "$CR_PYTHON" "$GITHUB_WORKSPACE/.github/scripts/build.py" > >(tee -i "$CR_LOGS/build.log") 2>&1
