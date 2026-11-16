#!/usr/bin/env python3
"""Only OUR orderly deadline may cause a resume; never mask an OOM/error."""
import json
import os
from pathlib import Path
import re
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


DEAD_PROC_STATES = frozenset({'Z', 'X', 'x'})


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


def _has_live_thread(proc_dir):
    try:
        tids = os.listdir(proc_dir / 'task')
    except OSError:
        return False
    for tid in tids:
        if not tid.isdigit():
            continue
        try:
            tstat = (proc_dir / 'task' / tid / 'stat').read_text(encoding='utf-8')
            tfields = tstat[tstat.rindex(')') + 2:].split()
            if tfields[0] not in DEAD_PROC_STATES:
                return True
        except (OSError, ValueError, IndexError):
            pass
    return False


def _live_session_pids(sid, proc_root='/proc'):
    pids = []
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        try:
            proc_dir = Path(proc_root, entry)
            stat = (proc_dir / 'stat').read_text(encoding='utf-8')
            # Fields after the ")" closing comm: state ppid pgrp session ... num_threads(17)
            fields = stat[stat.rindex(')') + 2:].split()
            if int(fields[3]) != sid:
                continue
            if fields[0] not in DEAD_PROC_STATES:
                pids.append(int(entry))
            elif fields[0] == 'Z' and len(fields) > 17 and int(fields[17]) > 1 and _has_live_thread(proc_dir):
                pids.append(int(entry))
        except (OSError, ValueError, IndexError):
            pass
    return pids


def kill_session(sid, timeout=10.0):
    """SIGKILL every live process in ninja's session; return any that remain.
    Ninja setpgid()s each child into its own process group, so killpg(ninja)
    alone cannot reach compiler/linker descendants, and a single /proc pass
    can race with forks or still-exiting processes before pack.sh starts."""
    deadline = time.monotonic() + timeout
    while True:
        pids = _live_session_pids(sid)
        if not pids:
            return []
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if time.monotonic() >= deadline:
            return _live_session_pids(sid)
        time.sleep(0.05)


def supervise(command, seconds, grace=180, env=None):
    # start_new_session: ninja becomes session leader, so session id == p.pid.
    p = subprocess.Popen(command, start_new_session=True, env=env)
    lingering = []
    try:
        try:
            rc = p.wait(timeout=seconds)
            result = 'completed' if rc == 0 else f'failed:{rc}'
        except subprocess.TimeoutExpired:
            # Races where ninja already failed must not be disguised as timeout.
            rc = p.poll()
            if rc is not None:
                result = 'completed' if rc == 0 else f'failed:{rc}'
            else:
                try:
                    os.killpg(p.pid, signal.SIGINT)
                except ProcessLookupError:
                    rc = p.wait()
                    result = 'completed' if rc == 0 else f'failed:{rc}'
                else:
                    try:
                        rc = p.wait(timeout=grace)
                    except subprocess.TimeoutExpired:
                        result = 'failed:shutdown-timeout'
                    else:
                        if rc == 0:
                            result = 'completed'
                        # -SIGINT: killed before ninja installed its handler; nothing ran.
                        elif rc in NINJA_INTERRUPTED or rc == -signal.SIGINT:
                            result = 'running'
                        else:
                            result = f'failed:{rc}'
    finally:
        # Also stop descendants before checkpointing (even if ninja exited).
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        lingering = list(kill_session(p.pid))
        if p.pid in lingering:
            p.poll()
        else:
            try:
                p.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                lingering.append(p.pid)
    if lingering and not result.startswith('failed:'):
        return f'failed:lingering-processes({",".join(map(str, lingering))})'
    return result


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


def swapped_out_bytes(vmstat='/proc/vmstat'):
    """Bytes written to swap since boot (None if unknown); a diagnostic only."""
    try:
        with open(vmstat) as f:
            for line in f:
                key, _, value = line.partition(' ')
                if key == 'pswpout':
                    return int(value) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError):
        pass
    return None


def free_gib(path):
    """Space left for unprivileged writes under path (None if unknown)."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return round(st.f_bavail * st.f_frsize / 2**30, 1)


def main():
    def _handle_sigterm(signum, frame):
        # Ignore repeated SIGTERM so supervise()'s finally finishes reaping the session.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit(f'Build supervisor terminated by signal {signum}')

    signal.signal(signal.SIGTERM, _handle_sigterm)

    stage = os.environ.get('STAGE', '')
    if not re.fullmatch(r'0[0-8]', stage):
        raise SystemExit(f'Invalid STAGE: {stage!r}')
    final_raw = os.environ.get('FINAL_STAGE', 'false')
    if final_raw not in ('true', 'false') or (stage == '00' and final_raw == 'true'):
        raise SystemExit(f'Invalid FINAL_STAGE: {final_raw!r}')
    final_stage = final_raw == 'true'

    root = Path(os.environ['CR_ROOT'])
    raw_started = (Path(os.environ['RUNNER_TEMP']) / 'chromium-job-start').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'-?[0-9]+', raw_started):
        msg = f'Invalid job start timestamp: {raw_started!r}'
        summarize(f'- Stage {stage}: `failed` before compiling; {msg}.')
        raise SystemExit(msg)
    started = int(raw_started)
    now = monotonic_now()
    if started > now:
        msg = f'Invalid future job start timestamp ({started} > {now:.1f})'
        summarize(f'- Stage {stage}: `failed` before compiling; {msg}.')
        raise SystemExit(msg)
    remaining = compute_budget(started, now, final_stage=final_stage)
    setup = round(now - started, 1)
    if remaining < MIN_BUDGET_SECONDS:
        msg = (f'Fetch/restore exhausted the time budget '
               f'(setup {setup / 60:.1f} min, remaining {remaining / 60:.1f} min); '
               f'refusing a no-progress checkpoint')
        summarize(f'- Stage {stage}: `failed` before compiling; {msg}.')
        raise SystemExit(msg)
    jobs = compute_jobs()
    os.chdir(root / 'src')
    env = dict(os.environ)
    env.setdefault('NINJA_STATUS', '[%f/%t %es] ')
    print(f'Building locally, jobs={jobs}, budget={remaining/60:.0f} minutes, final={final_stage}', flush=True)
    t0 = monotonic_now()
    swap_before = swapped_out_bytes()
    status = supervise(['third_party/ninja/ninja', '-C', 'out/Release',
                        f'-j{jobs}', 'chrome_public_apk'], remaining, env=env)
    elapsed = round(monotonic_now() - t0, 1)
    # Memory pressure and disk headroom: what the -j cap and later stages depend on.
    swap_after, disk_free = swapped_out_bytes(), free_gib(root)
    swap_mib = None if None in (swap_before, swap_after) else round((swap_after - swap_before) / 2**20)
    usage = (f"{'unknown' if swap_mib is None else swap_mib} MiB written to swap while compiling, "
             f"{'unknown' if disk_free is None else disk_free} GiB left free on the build disk")
    if status.startswith('failed:'):
        summarize(f"- Stage {os.environ['STAGE']}: `{status}` after compiling for {elapsed / 60:.1f} min "
                  f'with -j{jobs}; setup before compiling took {setup / 60:.1f} min.')
        summarize(f"- Stage {os.environ['STAGE']} resources: {usage}.")
        # Heavy swapping or a full disk explains many failures (e.g. an OOM kill or ENOSPC).
        raise SystemExit(f'Build failed after {elapsed:.1f}s, NOT checkpointing: {status}; {usage}')
    if status == 'running' and final_stage:
        status = 'exhausted'
    record = dict(stage=os.environ['STAGE'], image_os=os.environ.get('ImageOS'),
                  image_version=os.environ.get('ImageVersion'), jobs=jobs,
                  run_attempt=os.environ['GITHUB_RUN_ATTEMPT'],
                  final_stage=final_stage, budget_minutes=round(remaining / 60, 1),
                  setup_seconds=setup, elapsed_seconds=elapsed, status=status,
                  swap_out_mib=swap_mib, disk_free_gib=disk_free)
    with (root / 'metadata/runners.jsonl').open('a') as f:
        f.write(json.dumps(record) + '\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
        f.write(f'status={status}\n')
    print(f'Stage result: {status} ({elapsed:.1f}s)', flush=True)
    summarize(f"- Stage {os.environ['STAGE']}: `{status}` after compiling for {elapsed / 60:.1f} min with -j{jobs}; "
              f'setup before compiling (runner prep, then fetch or download and restore) took {setup / 60:.1f} min.')
    summarize(f"- Stage {os.environ['STAGE']} resources: {usage}.")


if __name__ == '__main__':
    main()
