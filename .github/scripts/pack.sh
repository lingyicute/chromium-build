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
# Archive the ENTRIES of $CR_ROOT (dotglob covers .gclient etc.), never "."
# itself: with --remove-files tar would finish by rmdir'ing $CR_ROOT, which is
# denied because /mnt belongs to root, and tar's exit status 2 would abort the
# pipeline after the complete archive had already been written.
( cd "$CR_ROOT" && shopt -s dotglob && \
  tar --format=posix --pax-option=delete=atime,delete=ctime --remove-files -cf - -- * ) \
  | zstd -T2 -3 -c \
  | split -b 1G -d -a 5 - "$CR_TRANSFER/checkpoint/tree.part-"
hash=$(python3 "$GITHUB_WORKSPACE/.github/scripts/checkpoint.py" hash "$CR_TRANSFER/checkpoint")
echo "sha256=$hash" >> "$GITHUB_OUTPUT"
du -sh "$CR_TRANSFER/checkpoint"
