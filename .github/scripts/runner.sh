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
  found=$(find -P "$dir" -mindepth 1 -maxdepth 1 -printf x -quit) \
    || runner_fail "Cannot inspect contents of $dir"
  [[ -z "$found" ]] || runner_fail "Expected an empty directory: $dir"
}

# Detect separate or same-device bind mounts at or below any checked path.
# Reads /proc/self/mountinfo-formatted text from stdin (decoding kernel \NNN escapes).
check_mount_targets() {
  (( $# > 0 )) || runner_fail 'Mount check requires at least one path'
  local py_check
  read -r -d '' py_check <<'PY' || true
import os, re, sys

def unescape(s):
    out = []
    i = 0
    while i < len(s):
        if s[i] == '\\':
            code = s[i + 1:i + 4]
            if len(code) != 3 or not re.fullmatch(r'[0-3][0-7]{2}', code) or code == '000':
                raise SystemExit('::error::Malformed runner mount table')
            out.append(chr(int(code, 8)))
            i += 4
        else:
            out.append(s[i])
            i += 1
    return ''.join(out)

def canonical_targets(raw):
    if not raw.startswith('/') or '//' in raw:
        raise SystemExit(f'::error::Mount check path must be canonical: {raw}')
    p = '/' if raw == '/' else raw.rstrip('/')
    if any(seg in ('.', '..') for seg in p.split('/')[1:]):
        raise SystemExit(f'::error::Mount check path must be canonical: {raw}')
    result = [p]
    if p != '/':
        parent, suffix = os.path.dirname(p), ['/' + os.path.basename(p)]
        while parent != '/' and not os.path.lexists(parent):
            suffix.append('/' + os.path.basename(parent))
            parent = os.path.dirname(parent)
        real = os.path.realpath(parent)
        if not os.path.isdir(real):
            raise SystemExit(f'::error::Cannot resolve ancestor of {p}')
        resolved = ('' if real == '/' else real) + ''.join(reversed(suffix))
        if resolved != p:
            result.append(resolved)
    return result

targets = []
for arg in sys.argv[1:]:
    targets.extend(canonical_targets(arg))

data = sys.stdin.read()
if not data or not data.endswith('\n'):
    raise SystemExit('::error::Empty or malformed runner mount table')
for line in data.splitlines():
    parts = line.split(' ')
    if len(parts) < 7 or '-' not in parts[5:]:
        raise SystemExit('::error::Malformed runner mount table')
    mount = unescape(parts[4])
    if not mount.startswith('/'):
        raise SystemExit('::error::Malformed runner mount table')
    for target in targets:
        if target == '/' or mount == target or mount.startswith(target + '/'):
            raise SystemExit(f'::error::Refusing mount at or below {target}: {mount}')
PY
  /usr/bin/python3 -c "$py_check" "$@"
}

require_no_mount_targets() {
  check_mount_targets "$@" < /proc/self/mountinfo
}
