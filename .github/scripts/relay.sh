#!/usr/bin/env bash
# Internal archive mechanics. Callers must first validate the runner and disk.
archive_relay_tree() (
  [[ -n "${1:-}" && -d "$1" && ! -L "$1" ]] || fail 'archive_relay_tree requires a real directory'
  CDPATH='' cd -P -- "$1" || fail "Cannot enter relay directory: $1"
  # Give the owner access to restricted directories before traversing them.
  # chmod does not alter mtimes or regular file permissions.
  make_dirs_removable .
  # tar ignores sockets, but a leftover socket would prevent its rmdir.
  find . -mindepth 1 -type s -delete || fail 'Could not remove stale build sockets'
  # Include hidden entries but never archive "." itself: its parent may not
  # allow removal of the relay root when tar uses --remove-files.
  unset GLOBIGNORE
  set +f
  shopt -u failglob
  shopt -s dotglob nullglob
  shopt -s globskipdots 2>/dev/null || true
  entries=(*)
  (( ${#entries[@]} > 0 )) || fail 'Cannot checkpoint an empty build tree'
  # Compression alone does not preserve holes on extraction. A dense restored
  # file could consume blocks the previous runner did not need.
  tar --format=posix --sparse --no-unquote --pax-option=delete=atime,delete=ctime \
    --remove-files -cf - -- "${entries[@]}"
)

extract_relay_tree() {
  tar --no-same-owner -xpf - -C "$1"
}
