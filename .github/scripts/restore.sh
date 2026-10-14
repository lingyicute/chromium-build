#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
[[ -z "$(ls -A "$CR_ROOT")" ]] || fail 'Restore destination must be empty'
python3 "$GITHUB_WORKSPACE/.github/scripts/checkpoint.py" restore-stream "$CR_TRANSFER/checkpoint" \
  | zstd -d -c | tar --no-same-owner -xpf - -C "$CR_ROOT"
python3 - <<'PY'
import json, os, subprocess
from pathlib import Path
root=Path(os.environ['CR_ROOT'])
lock=json.loads((root/'metadata/build-lock.json').read_text())
for field, env in [('repository','GITHUB_REPOSITORY'),('run_id','GITHUB_RUN_ID'),('workflow_sha','GITHUB_SHA')]:
    if lock[field] != os.environ[env]:
        raise SystemExit(f'Checkpoint {field} is from another run/repository/commit')
for directory, field in [('src','chromium_revision'),('depot_tools','depot_tools_revision')]:
    sha=subprocess.check_output(['git','-C',str(root/directory),'rev-parse','HEAD'], text=True).strip()
    if sha != lock[field]: raise SystemExit(f'{directory} revision mismatch')
if (root/'src/out/Release/args.gn').read_bytes() != (Path(os.environ['GITHUB_WORKSPACE'])/'config/args.gn').read_bytes():
    raise SystemExit('GN arguments changed')
PY
# No gclient sync, runhooks, or gn gen here: keep all build inputs and mtimes.
rmdir "$CR_TRANSFER/checkpoint"
df -h /mnt
