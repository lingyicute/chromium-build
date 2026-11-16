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
import json, os, re, subprocess
from pathlib import Path
root = Path(os.environ['CR_ROOT'])
lock = json.loads((root / 'metadata/build-lock.json').read_text(encoding='utf-8'))
if not isinstance(lock, dict):
    raise SystemExit('Invalid build-lock.json')
for field, env in [('repository', 'GITHUB_REPOSITORY'), ('run_id', 'GITHUB_RUN_ID'), ('workflow_sha', 'GITHUB_SHA')]:
    expected_val = os.environ[env]
    if not expected_val or not isinstance(lock.get(field), str) or lock.get(field) != expected_val:
        raise SystemExit(f'Checkpoint {field} is from another run/repository/commit')
if (lock.get('target') != 'chrome_public_apk' or lock.get('architecture') != 'arm64'
        or lock.get('source') != 'https://chromium.googlesource.com/chromium/src.git'):
    raise SystemExit('Checkpoint source/target/architecture mismatch')
workspace = Path(os.environ['GITHUB_WORKSPACE'])
expected_depot = (workspace / 'config/depot_tools_revision.txt').read_text(encoding='utf-8').strip()
if not re.fullmatch(r'[0-9a-f]{40}', expected_depot) or lock.get('depot_tools_revision') != expected_depot:
    raise SystemExit('depot_tools pin changed')
git_env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(root))
for directory, field in [('src', 'chromium_revision'), ('depot_tools', 'depot_tools_revision')]:
    repo_dir = root / directory
    locked_sha = lock.get(field)
    if not isinstance(locked_sha, str) or not re.fullmatch(r'[0-9a-f]{40}', locked_sha):
        raise SystemExit(f'Invalid {field} in build-lock.json')
    if not (repo_dir / '.git').exists():
        raise SystemExit(f'Restored {directory} is missing .git metadata')
    sha = subprocess.check_output(['git', '-C', str(repo_dir), 'rev-parse', 'HEAD'],
                                  env=git_env, text=True).strip()
    if sha != locked_sha:
        raise SystemExit(f'{directory} revision mismatch')
parts = {k.strip(): v.strip()
         for line in (root / 'src/chrome/VERSION').read_text(encoding='utf-8').splitlines()
         if not line.lstrip().startswith('#') and '=' in line
         for k, v in (line.split('=', 1),)}
try:
    actual_version = '.'.join(parts[k] for k in ('MAJOR', 'MINOR', 'BUILD', 'PATCH'))
except KeyError as exc:
    raise SystemExit(f'Restored chrome/VERSION missing field: {exc.args[0]}') from exc
locked_version = lock.get('version')
if (not isinstance(locked_version, str)
        or not re.fullmatch(r'(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,5})){3}', locked_version)
        or actual_version != locked_version):
    raise SystemExit(f'Restored chrome/VERSION mismatch: {actual_version} != {locked_version}')
if (root / 'src/out/Release/args.gn').read_bytes() != (workspace / 'config/args.gn').read_bytes():
    raise SystemExit('GN arguments changed')
if not (root / 'src/out/Release/build.ninja').is_file():
    raise SystemExit('Restored build tree is missing out/Release/build.ninja')
for rel_tool in ('src/buildtools/linux64/gn', 'src/third_party/ninja/ninja'):
    tool = root / rel_tool
    if not (tool.is_file() and os.access(tool, os.X_OK)):
        raise SystemExit(f'Restored build tree is missing executable {rel_tool[4:]}')
PY
# No gclient sync, runhooks, or gn gen here: keep all build inputs and mtimes.
rmdir "$CR_TRANSFER/checkpoint"
df -h /mnt
