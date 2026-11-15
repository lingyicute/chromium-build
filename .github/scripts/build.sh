#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
"$CR_PYTHON" "$GITHUB_WORKSPACE/.github/scripts/build.py" 2>&1 | tee -i "$CR_LOGS/build.log"
