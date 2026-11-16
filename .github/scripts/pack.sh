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
# Ensure restricted directories (e.g. from CIPD/third-party caches) grant u+rwx
# before descending so tar --remove-files can traverse, unlink, and rmdir them;
# chmod does not alter directory mtimes or regular file permission bits.
find "$CR_ROOT" -type d ! -perm -u+rwx -exec chmod u+rwx {} \;
# Remove stale Unix sockets (tar ignores sockets and then fails rmdir).
find "$CR_ROOT" -mindepth 1 -type s -delete
# Archive the ENTRIES of $CR_ROOT (dotglob covers .gclient etc.), never "."
# itself: with --remove-files tar would finish by rmdir'ing $CR_ROOT, which is
# denied because /mnt belongs to root, and tar's exit status 2 would abort the
# pipeline after the complete archive had already been written.
# zstd -T0 counts physical cores only (half the vCPUs of an SMT hosted runner),
# so ask for one worker per logical CPU: nothing else runs during the export.
hash=$(
  ( cd "$CR_ROOT" && shopt -s dotglob nullglob && entries=(*) && \
    (( ${#entries[@]} > 0 )) && \
    tar --format=posix --pax-option=delete=atime,delete=ctime --remove-files -cf - -- "${entries[@]}" ) \
    | zstd -T"$(nproc)" -3 -c \
    | "$CR_PYTHON" "$GITHUB_WORKSPACE/.github/scripts/checkpoint.py" pack-stream "$CR_TRANSFER/checkpoint"
)
[[ "$hash" =~ ^[0-9a-f]{64}$ ]] || fail 'Invalid checkpoint SHA-256 output'
[[ -z "$(ls -A "$CR_ROOT")" ]] || fail 'Destructive export left files behind in build root'
echo "sha256=$hash" >> "$GITHUB_OUTPUT"
du -sh "$CR_TRANSFER/checkpoint"
