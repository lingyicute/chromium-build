#!/usr/bin/env python3
"""Only OUR orderly deadline may cause a resume; never mask an OOM/error."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

# Exit statuses Ninja returns after handling SIGINT itself (it removes the
# outputs of interrupted edges first, so the tree is safe to resume):
#   2   ninja <= 1.12 ("interrupted by user"); Chromium pins 1.12.1.chromium.*
#   130 ninja >= 1.13 (ExitInterrupted)
# Status 1 is a genuine build error ("subcommand failed") and must NOT resume.
NINJA_INTERRUPTED = (2, 130)
# Compile until a fixed offset from the job start recorded by host.sh. The rest
# of the 350-minute job is reserved for checkpoint pack/upload (normal stages)
# or APK verification/upload (final stage); a faster setup means more compiling.
NORMAL_JOB_DEADLINE_SECONDS = 265 * 60
FINAL_JOB_DEADLINE_SECONDS = 330 * 60
MIN_BUDGET_SECONDS = 300


def monotonic_now():
    if hasattr(time, 'CLOCK_BOOTTIME'):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


def compute_jobs(cpu_count=None, mem_kib=None):
    if cpu_count is None:
        cpu_count = os.cpu_count() or 1
    if mem_kib is None:
        mem_kib = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                           if line.startswith('MemTotal:')))
    # Public repos typically have more RAM/CPU than private standard runners.
    return max(1, min(cpu_count, mem_kib // (3 * 1024 * 1024), 4))


def compute_budget(started, now, final_stage=False):
    deadline = FINAL_JOB_DEADLINE_SECONDS if final_stage else NORMAL_JOB_DEADLINE_SECONDS
    return started + deadline - now


def _live_session_pids(sid):
    pids = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            stat = Path('/proc', entry, 'stat').read_text()
            # Fields after the ")" closing comm: state ppid pgrp session ...
            fields = stat[stat.rindex(')') + 2:].split()
            if fields[0] != 'Z' and int(fields[3]) == sid:
                pids.append(int(entry))
        except (OSError, ValueError, IndexError):
            pass
    return pids


def kill_session(sid, timeout=10.0):
    """SIGKILL every live process in ninja's session until none remain.
    Ninja setpgid()s each child into its own process group, so killpg(ninja)
    alone cannot reach compiler/linker descendants, and a single /proc pass
    can race with forks or still-exiting processes before pack.sh starts."""
    deadline = time.monotonic() + timeout
    while True:
        pids = _live_session_pids(sid)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if time.monotonic() >= deadline:
            return
        time.sleep(0.05)


def supervise(command, seconds, grace=180, env=None):
    # start_new_session: ninja becomes session leader, so session id == p.pid.
    p = subprocess.Popen(command, start_new_session=True, env=env)
    try:
        try:
            rc = p.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            # Races where ninja already failed must not be disguised as timeout.
            rc = p.poll()
            if rc is not None:
                return 'completed' if rc == 0 else f'failed:{rc}'
            try:
                os.killpg(p.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                rc = p.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                return 'failed:shutdown-timeout'
            if rc == 0:
                return 'completed'
            # -SIGINT: killed before ninja installed its handler; nothing ran.
            if rc in NINJA_INTERRUPTED or rc == -signal.SIGINT:
                return 'running'
            return f'failed:{rc}'
        return 'completed' if rc == 0 else f'failed:{rc}'
    finally:
        # Also stop descendants before checkpointing (even if ninja exited).
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        kill_session(p.pid)
        p.wait()


def summarize(line):
    """Append to the Actions job summary; a diagnostic must never fail the build."""
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    try:
        with open(path, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def main():
    def _handle_sigterm(signum, frame):
        raise SystemExit(f'Build supervisor terminated by signal {signum}')

    signal.signal(signal.SIGTERM, _handle_sigterm)

    root = Path(os.environ['CR_ROOT'])
    started = int((Path(os.environ['RUNNER_TEMP']) / 'chromium-job-start').read_text().strip())
    final_stage = os.environ.get('FINAL_STAGE', 'false') == 'true'
    now = monotonic_now()
    remaining = compute_budget(started, now, final_stage=final_stage)
    setup = round(now - started, 1)
    if remaining < MIN_BUDGET_SECONDS:
        raise SystemExit('Fetch/restore exhausted the time budget; refusing a no-progress checkpoint')
    jobs = compute_jobs()
    os.chdir(root / 'src')
    env = dict(os.environ)
    env.setdefault('NINJA_STATUS', '[%f/%t %es] ')
    print(f'Building locally, jobs={jobs}, budget={remaining/60:.0f} minutes, final={final_stage}', flush=True)
    t0 = monotonic_now()
    status = supervise(['third_party/ninja/ninja', '-C', 'out/Release',
                        f'-j{jobs}', 'chrome_public_apk'], remaining, env=env)
    elapsed = round(monotonic_now() - t0, 1)
    if status.startswith('failed:'):
        raise SystemExit(f'Build failed after {elapsed:.1f}s, NOT checkpointing: {status}')
    if status == 'running' and final_stage:
        status = 'exhausted'
    record = dict(stage=os.environ['STAGE'], image_os=os.environ.get('ImageOS'),
                  image_version=os.environ.get('ImageVersion'), jobs=jobs,
                  run_attempt=os.environ['GITHUB_RUN_ATTEMPT'],
                  final_stage=final_stage, budget_minutes=round(remaining / 60, 1),
                  setup_seconds=setup, elapsed_seconds=elapsed, status=status)
    with (root / 'metadata/runners.jsonl').open('a') as f:
        f.write(json.dumps(record) + '\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
        f.write(f'status={status}\n')
    print(f'Stage result: {status} ({elapsed:.1f}s)', flush=True)
    summarize(f"- Stage {os.environ['STAGE']}: `{status}` after compiling for {elapsed / 60:.1f} min with -j{jobs}; "
              f'setup before compiling (runner prep, then fetch or download and restore) took {setup / 60:.1f} min.')


if __name__ == '__main__':
    main()
