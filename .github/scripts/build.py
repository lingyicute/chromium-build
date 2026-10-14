#!/usr/bin/env python3
"""Only OUR orderly deadline may cause a resume; never mask an OOM/error."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

def supervise(command, seconds, grace=120):
    p = subprocess.Popen(command, start_new_session=True)
    try:
        try:
            rc = p.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            # Races where ninja already failed must not be disguised as timeout.
            rc = p.poll()
            if rc is not None:
                return 'completed' if rc == 0 else f'failed:{rc}'
            os.killpg(p.pid, signal.SIGINT)
            try:
                rc = p.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                return 'failed:shutdown-timeout'
            # Ninja normally returns 1 for an orderly interrupted build.
            if rc == 0:
                return 'completed'
            if rc not in (1, -signal.SIGINT, 128 + signal.SIGINT):
                return f'failed:{rc}'
            return 'running'
        return 'completed' if rc == 0 else f'failed:{rc}'
    finally:
        # Also stop descendants before checkpointing (even if ninja exited).
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()

def main():
    root = Path(os.environ['CR_ROOT'])
    started = int((Path(os.environ['RUNNER_TEMP'])/'chromium-job-start').read_text())
    remaining = min(210 * 60, started + 270 * 60 - time.time())
    if remaining < 300:
        raise SystemExit('Restore exhausted the time budget; refusing a no-progress checkpoint')
    # Public repos typically have more RAM/CPU than private standard runners.
    mem_kib = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                       if line.startswith('MemTotal:')))
    jobs = max(1, min(os.cpu_count() or 1, mem_kib // (3 * 1024 * 1024), 4))
    record = dict(stage=os.environ['STAGE'], image_os=os.environ.get('ImageOS'),
                  image_version=os.environ.get('ImageVersion'), jobs=jobs,
                  run_attempt=os.environ['GITHUB_RUN_ATTEMPT'])
    with (root/'metadata/runners.jsonl').open('a') as f:
        f.write(json.dumps(record) + '\n')
    os.chdir(root/'src')
    print(f'Building locally, jobs={jobs}, budget={remaining/60:.0f} minutes', flush=True)
    status = supervise(['third_party/ninja/ninja', '-C', 'out/Release',
                        f'-j{jobs}', 'chrome_public_apk'], remaining)
    if status.startswith('failed:'):
        raise SystemExit(f'Build failed, NOT checkpointing: {status}')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
        f.write(f'status={status}\n')
    print(f'Stage result: {status}', flush=True)

if __name__ == '__main__':
    main()
