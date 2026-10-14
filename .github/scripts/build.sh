#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
python3 "$GITHUB_WORKSPACE/.github/scripts/build.py" 2>&1 | tee "$CR_LOGS/build.log"
