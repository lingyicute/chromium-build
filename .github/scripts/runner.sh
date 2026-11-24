#!/usr/bin/env bash
# Shared runner and filesystem layout guards.
runner_fail() { printf '::error::%s\n' "$*" >&2; exit 1; }

require_hosted_runner() {
  [[ "${RUNNER_ENVIRONMENT:-}" == github-hosted ]] || runner_fail 'GitHub-hosted runner required'
}

# Accept both separate /mnt disk and single-disk hosted runners; host.sh checks free space.
require_relay_mount() {
  local root_dev
  [[ -d /mnt && ! -L /mnt ]] || runner_fail '/mnt must be a real directory'
  root_dev=$(stat -c '%d' -- /) || runner_fail 'Cannot inspect root filesystem'
  relay_device=$(stat -c '%d' -- /mnt) || runner_fail 'Cannot inspect /mnt filesystem'
  [[ -n "$root_dev" && -n "$relay_device" ]] || runner_fail 'Cannot inspect /mnt filesystem'
  relay_shares_root=0
  [[ "$relay_device" != "$root_dev" ]] || relay_shares_root=1
}

require_relay_directory() {
  local dir=$1 dev
  [[ -n "${relay_device:-}" ]] || runner_fail 'Relay filesystem check has not run'
  [[ -d "$dir" && ! -L "$dir" ]] || runner_fail "Refusing non-directory or symlink: $dir"
  dev=$(stat -c '%d' -- "$dir") || runner_fail "Cannot inspect filesystem for $dir"
  [[ "$dev" == "$relay_device" ]] || runner_fail "Expected $dir on the /mnt filesystem"
}

require_empty_directory() {
  local dir=${1:-} found
  [[ -n "$dir" && -d "$dir" && ! -L "$dir" ]] || runner_fail "Refusing non-directory or symlink: $dir"
  [[ "$dir" == /* || "$dir" == .* ]] || dir="./$dir"
  found=$(find -P "$dir" -mindepth 1 -maxdepth 1 -printf x -quit) \
    || runner_fail "Cannot inspect contents of $dir"
  [[ -z "$found" ]] || runner_fail "Expected an empty directory: $dir"
}

# Detect separate or same-device bind mounts at or below any checked path.
# Reads /proc/self/mountinfo-formatted bytes from stdin (decoding kernel \NNN escapes).
check_mount_targets() {
  (( $# > 0 )) || runner_fail 'Mount check requires at least one path'
  local py_check
  read -r -d '' py_check <<'PY' || true
import os, re, sys

def unescape(raw):
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i:i + 1] == b'\\':
            code = raw[i + 1:i + 4]
            if len(code) != 3 or not re.fullmatch(rb'[0-3][0-7]{2}', code) or code == b'000':
                raise SystemExit('::error::Malformed runner mount table')
            out.append(int(code, 8))
            i += 4
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)

def canonical_targets(raw):
    if not raw.startswith(b'/') or b'//' in raw:
        raise SystemExit(f'::error::Mount check path must be canonical: {os.fsdecode(raw)}')
    p = b'/' if raw == b'/' else raw.rstrip(b'/')
    if any(seg in (b'.', b'..') for seg in p.split(b'/')[1:]):
        raise SystemExit(f'::error::Mount check path must be canonical: {os.fsdecode(raw)}')
    result = [p]
    if p != b'/':
        parent, suffix = os.path.dirname(p), [b'/' + os.path.basename(p)]
        while parent != b'/' and not os.path.lexists(parent):
            suffix.append(b'/' + os.path.basename(parent))
            parent = os.path.dirname(parent)
        try:
            real = os.path.realpath(parent, strict=True)
        except OSError:
            raise SystemExit(f'::error::Cannot resolve ancestor of {os.fsdecode(p)}')
        if not os.path.isdir(real) or not os.access(real, os.X_OK):
            raise SystemExit(f'::error::Cannot resolve ancestor of {os.fsdecode(p)}')
        resolved = (b'' if real == b'/' else real) + b''.join(reversed(suffix))
        if resolved != p:
            result.append(resolved)
    return result

targets = []
for arg in sys.argv[1:]:
    targets.extend(canonical_targets(os.fsencode(arg)))

data = sys.stdin.buffer.read()
if not data or not data.endswith(b'\n'):
    raise SystemExit('::error::Empty or malformed runner mount table')
for line in data.splitlines():
    parts = line.split(b' ')
    if len(parts) < 10 or b'-' not in parts[6:-3]:
        raise SystemExit('::error::Malformed runner mount table')
    mount = unescape(parts[4])
    if not mount.startswith(b'/'):
        raise SystemExit('::error::Malformed runner mount table')
    for target in targets:
        if target == b'/' or mount == target or mount.startswith(target + b'/'):
            raise SystemExit(f'::error::Refusing mount at or below {os.fsdecode(target)}: {os.fsdecode(mount)}')
PY
  /usr/bin/python3 -c "$py_check" "$@"
}

require_no_mount_targets() {
  check_mount_targets "$@" < /proc/self/mountinfo
}
