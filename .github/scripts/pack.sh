#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
[[ "$CR_ROOT" == /mnt/chromium-relay && "$CR_TRANSFER" == /mnt/chromium-transfer ]] || exit 1
mkdir -p "$CR_TRANSFER/checkpoint"
[[ -z "$(ls -A "$CR_TRANSFER/checkpoint")" ]] || fail 'Checkpoint output must be empty'
# IMPORTANT: destructive export on a disposable VM. No commands may use the
# source tree afterwards. Each source file is removed after tar reads it, so a
# full uncompressed tree + a full archive need not fit simultaneously.
# Preserve hidden files, nanosecond mtimes, symlinks, .git, vpython and CIPD.
# Do not use --dereference or --exclude-vcs.
tar --format=posix --pax-option=delete=atime,delete=ctime --remove-files \
  -C "$CR_ROOT" -cf - . \
  | zstd -T2 -3 -c \
  | split -b 1G -d -a 5 - "$CR_TRANSFER/checkpoint/tree.part-"
hash=$(python3 "$GITHUB_WORKSPACE/.github/scripts/checkpoint.py" hash "$CR_TRANSFER/checkpoint")
echo "sha256=$hash" >> "$GITHUB_OUTPUT"
du -sh "$CR_TRANSFER/checkpoint"
