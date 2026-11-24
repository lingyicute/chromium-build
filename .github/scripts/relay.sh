#!/usr/bin/env bash
# Destructive archive/extract helpers preserving sparse files and nanosecond mtimes.
archive_relay_tree() (
  [[ -n "${1:-}" && -d "$1" && ! -L "$1" ]] || fail 'archive_relay_tree requires a real directory'
  cd -P -- "$1"
  make_dirs_removable .
  # tar ignores sockets; delete leftover ones so --remove-files can rmdir directories.
  find . -mindepth 1 -type s -delete
  set +f
  shopt -u failglob
  shopt -s dotglob nullglob
  shopt -s globskipdots 2>/dev/null || true
  entries=(*)
  (( ${#entries[@]} > 0 )) || fail 'Cannot checkpoint an empty build tree'
  tar --format=posix --sparse --no-unquote --pax-option=delete=atime,delete=ctime \
    --remove-files -cf - -- "${entries[@]}"
)

extract_relay_tree() {
  [[ -n "${1:-}" && -d "$1" && ! -L "$1" ]] || fail 'extract_relay_tree requires a real directory'
  tar --no-same-owner -xpf - -C "$1"
}
