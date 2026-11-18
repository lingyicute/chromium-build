#!/usr/bin/env bash
# Internal archive mechanics. Callers must first validate the runner and disk.
archive_relay_tree() (
  cd -- "$1" || exit
  # Give the owner access to restricted directories before traversing them.
  # chmod does not alter mtimes or regular file permissions.
  make_dirs_removable .
  # tar ignores sockets, but a leftover socket would prevent its rmdir.
  find . -mindepth 1 -type s -delete || fail 'Could not remove stale build sockets'
  # Include hidden entries but never archive "." itself: its parent may not
  # allow removal of the relay root when tar uses --remove-files.
  shopt -s dotglob nullglob
  entries=(*)
  (( ${#entries[@]} > 0 )) || fail 'Cannot checkpoint an empty build tree'
  tar --format=posix --pax-option=delete=atime,delete=ctime --remove-files -cf - -- "${entries[@]}"
)

extract_relay_tree() {
  tar --no-same-owner -xpf - -C "$1"
}
