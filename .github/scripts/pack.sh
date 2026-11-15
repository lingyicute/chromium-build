#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
# Export using only OS tools, never depot_tools wrappers inside $CR_ROOT.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
[[ "$CR_ROOT" == /mnt/chromium-relay && "$CR_TRANSFER" == /mnt/chromium-transfer ]] \
  || fail 'Refusing destructive export outside the fixed /mnt checkpoint paths'
mkdir -p "$CR_TRANSFER/checkpoint"
[[ -z "$(ls -A "$CR_TRANSFER/checkpoint")" ]] || fail 'Checkpoint output must be empty'
# IMPORTANT: destructive export on a disposable VM. No commands may use the
# source tree afterwards. Each source file is removed after tar reads it, so a
# full uncompressed tree + a full archive need not fit simultaneously.
# Preserve hidden files, nanosecond mtimes, symlinks, .git, vpython and CIPD.
# Do not use --dereference or --exclude-vcs.
# Ensure read-only directories (e.g. from CIPD/third-party caches) are writable
# by owner so tar --remove-files can unlink/rmdir them; chmod does not alter
# directory mtimes or regular file permission bits.
find "$CR_ROOT" -mindepth 1 -type d ! -perm -u+w -exec chmod u+w {} +
# Archive the ENTRIES of $CR_ROOT (dotglob covers .gclient etc.), never "."
# itself: with --remove-files tar would finish by rmdir'ing $CR_ROOT, which is
# denied because /mnt belongs to root, and tar's exit status 2 would abort the
# pipeline after the complete archive had already been written.
hash=$(
  ( cd "$CR_ROOT" && shopt -s dotglob nullglob && entries=(*) && \
    (( ${#entries[@]} > 0 )) && \
    tar --format=posix --pax-option=delete=atime,delete=ctime --remove-files -cf - -- "${entries[@]}" ) \
    | zstd -T0 -3 -c \
    | "$CR_PYTHON" "$GITHUB_WORKSPACE/.github/scripts/checkpoint.py" pack-stream "$CR_TRANSFER/checkpoint"
)
[[ "$hash" =~ ^[0-9a-f]{64}$ ]] || fail 'Invalid checkpoint SHA-256 output'
echo "sha256=$hash" >> "$GITHUB_OUTPUT"
du -sh "$CR_TRANSFER/checkpoint"
