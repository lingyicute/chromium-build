#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
# Verify restored tree using only OS tools, never restored depot_tools wrappers.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
[[ -d "$CR_ROOT" && ! -L "$CR_ROOT" ]] || fail 'Restore destination must be a real directory'
restore_entries=$(ls -A -- "$CR_ROOT")
[[ -z "$restore_entries" ]] || fail 'Restore destination must be empty'
"$CR_PYTHON" "$GITHUB_WORKSPACE/.github/scripts/checkpoint.py" restore-stream "$CR_TRANSFER/checkpoint" \
  | zstd -d -c | tar --no-same-owner -xpf - -C "$CR_ROOT"
"$CR_PYTHON" - <<'PY'
import json, os, subprocess
from pathlib import Path
root = Path(os.environ['CR_ROOT'])
lock = json.loads((root / 'metadata/build-lock.json').read_text())
if not isinstance(lock, dict):
    raise SystemExit('Invalid build-lock.json')
for field, env in [('repository', 'GITHUB_REPOSITORY'), ('run_id', 'GITHUB_RUN_ID'), ('workflow_sha', 'GITHUB_SHA')]:
    if lock.get(field) != os.environ[env]:
        raise SystemExit(f'Checkpoint {field} is from another run/repository/commit')
if (lock.get('target') != 'chrome_public_apk' or lock.get('architecture') != 'arm64'
        or lock.get('source') != 'https://chromium.googlesource.com/chromium/src.git'):
    raise SystemExit('Checkpoint source/target/architecture mismatch')
workspace = Path(os.environ['GITHUB_WORKSPACE'])
expected_depot = (workspace / 'config/depot_tools_revision.txt').read_text().strip()
if lock.get('depot_tools_revision') != expected_depot:
    raise SystemExit('depot_tools pin changed')
for directory, field in [('src', 'chromium_revision'), ('depot_tools', 'depot_tools_revision')]:
    sha = subprocess.check_output(['git', '-C', str(root / directory), 'rev-parse', 'HEAD'], text=True).strip()
    if sha != lock.get(field):
        raise SystemExit(f'{directory} revision mismatch')
parts = {k.strip(): v.strip() for line in (root / 'src/chrome/VERSION').read_text().splitlines()
         if '=' in line for k, v in (line.split('=', 1),)}
try:
    actual_version = '.'.join(parts[k] for k in ('MAJOR', 'MINOR', 'BUILD', 'PATCH'))
except KeyError as exc:
    raise SystemExit(f'Restored chrome/VERSION missing field: {exc.args[0]}') from exc
if actual_version != lock.get('version'):
    raise SystemExit(f'Restored chrome/VERSION mismatch: {actual_version} != {lock.get("version")}')
if (root / 'src/out/Release/args.gn').read_bytes() != (workspace / 'config/args.gn').read_bytes():
    raise SystemExit('GN arguments changed')
if not (root / 'src/out/Release/build.ninja').is_file():
    raise SystemExit('Restored build tree is missing out/Release/build.ninja')
ninja = root / 'src/third_party/ninja/ninja'
if not (ninja.is_file() and os.access(ninja, os.X_OK)):
    raise SystemExit('Restored build tree is missing executable third_party/ninja/ninja')
PY
# No gclient sync, runhooks, or gn gen here: keep all build inputs and mtimes.
rmdir "$CR_TRANSFER/checkpoint"
df -h /mnt
