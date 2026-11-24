#!/usr/bin/env python3
"""Supervise Ninja so only an orderly deadline stops and checkpoints the build."""
import ctypes
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

# Ninja exit codes on handled SIGINT (outputs of interrupted edges already removed):
# 2 on ninja <= 1.12 (Chromium's pin), 130 on ninja >= 1.13. Status 1 is a build error.
NINJA_INTERRUPTED = (2, 130)
NORMAL_JOB_DEADLINE_SECONDS = 260 * 60
FINAL_JOB_DEADLINE_SECONDS = 325 * 60
MIN_BUDGET_SECONDS = 300
STOP_GRACE_SECONDS = 180
CLEANUP_TIMEOUT_SECONDS = 10
DEAD_STATES = frozenset({'Z', 'X', 'x'})


def monotonic_now():
    return time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time, 'CLOCK_BOOTTIME') else time.monotonic()


def compute_jobs(cpu_count=None, mem_kib=None):
    if cpu_count is None:
        cpu_count = os.cpu_count() or 1
    if mem_kib is None:
        for line in Path('/proc/meminfo').read_text(encoding='utf-8').splitlines():
            if line.startswith('MemTotal:'):
                mem_kib = int(line.split()[1])
                break
        else:
            raise RuntimeError('MemTotal missing from /proc/meminfo')
    return max(1, min(cpu_count, mem_kib // (3 * 1024 * 1024), 4))


def compute_budget(started, now, final_stage=False):
    deadline = FINAL_JOB_DEADLINE_SECONDS if final_stage else NORMAL_JOB_DEADLINE_SECONDS
    return started + deadline - now


def _read_stat(pid_dir):
    raw = (pid_dir / 'stat').read_text(encoding='utf-8', errors='replace')
    # Fields after ") ": state ppid pgrp session ... num_threads(17).
    fields = raw[raw.rindex(')') + 2:].split()
    num_threads = int(fields[17]) if len(fields) > 17 and fields[17].isascii() and fields[17].isdigit() else 1
    return fields[0], int(fields[1]), int(fields[3]), num_threads


def _is_live_proc(pid_dir, state, num_threads):
    if state not in DEAD_STATES:
        return True
    # A multithreaded tool whose leader called pthread_exit() reports 'Z' while workers still run.
    if state != 'Z' or num_threads <= 1:
        return False
    try:
        tids = os.listdir(pid_dir / 'task')
    except OSError:
        return False
    for tid in tids:
        if not (tid.isascii() and tid.isdigit()):
            continue
        try:
            raw = (pid_dir / 'task' / tid / 'stat').read_text(encoding='utf-8', errors='replace')
            tstate = raw[raw.rindex(')') + 2:].split()[0]
            if tstate not in DEAD_STATES:
                return True
        except (OSError, ValueError, IndexError):
            pass
    return False


def _iter_procs(proc_root='/proc'):
    for entry in os.listdir(proc_root):
        if not (entry.isascii() and entry.isdigit()):
            continue
        pid_dir = Path(proc_root, entry)
        try:
            state, ppid, sid, num_threads = _read_stat(pid_dir)
        except (OSError, ValueError, IndexError):
            continue
        yield int(entry), pid_dir, state, ppid, sid, num_threads


def enable_orphan_tracking():
    """Adopt orphaned descendants (even setsid children); protect pre-existing children (e.g. tee)."""
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    if prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    me = os.getpid()
    return frozenset(pid for pid, _, _, ppid, _, _ in _iter_procs() if ppid == me)


def _live_build_pids(session_id, protected_children=None, proc_root='/proc'):
    me = os.getpid()
    pids = []
    for pid, pid_dir, state, ppid, sid, num_threads in _iter_procs(proc_root):
        if pid == me:
            continue
        adopted = protected_children is not None and ppid == me and pid not in protected_children
        if (sid == session_id or adopted) and _is_live_proc(pid_dir, state, num_threads):
            pids.append(pid)
    return pids


def kill_session(session_id, timeout=CLEANUP_TIMEOUT_SECONDS, protected_children=None):
    """Force-kill leftover build processes; return (forced_pids, still_live_pids)."""
    deadline = time.monotonic() + timeout
    killed = set()
    confirmed_empty = False
    while True:
        pids = _live_build_pids(session_id, protected_children)
        if not pids:
            # One extra pass catches descendants re-parented right as their parent exits.
            if confirmed_empty or protected_children is None or time.monotonic() >= deadline:
                break
            confirmed_empty = True
            continue
        confirmed_empty = False
        for pid in pids:
            try:
                fd = os.pidfd_open(pid)
                try:
                    pid_dir = Path('/proc', str(pid))
                    state, ppid, sid, num_threads = _read_stat(pid_dir)
                    adopted = protected_children is not None and ppid == os.getpid() and pid not in protected_children
                    if (sid == session_id or adopted) and _is_live_proc(pid_dir, state, num_threads):
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                        killed.add(pid)
                finally:
                    os.close(fd)
            except (OSError, ValueError, IndexError):
                pass
        if time.monotonic() >= deadline:
            pids = _live_build_pids(session_id, protected_children)
            break
        time.sleep(0.05)
    if protected_children is not None:
        me = os.getpid()
        for pid, _, _, ppid, _, _ in _iter_procs():
            if pid != session_id and ppid == me and pid not in protected_children:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
    return sorted(killed), pids


def _check_cancelled(cancel_signal):
    signum = cancel_signal() if cancel_signal is not None else None
    if signum is not None:
        raise SystemExit(f'Build supervisor cancelled by signal {signum}; NOT checkpointing')


def _wait_for(process, timeout, cancel_signal):
    if cancel_signal is None:
        return process.wait(timeout=timeout)
    deadline = monotonic_now() + timeout
    while True:
        _check_cancelled(cancel_signal)
        left = deadline - monotonic_now()
        try:
            return process.wait(timeout=max(0.0, min(left, 0.5)))
        except subprocess.TimeoutExpired:
            if monotonic_now() >= deadline:
                raise


def _is_alive(pid):
    pid_dir = Path('/proc', str(pid))
    try:
        state, _, _, num_threads = _read_stat(pid_dir)
        return _is_live_proc(pid_dir, state, num_threads)
    except (OSError, ValueError, IndexError):
        return False


def supervise(command, seconds, grace=STOP_GRACE_SECONDS, env=None,
              cancel_signal=None, protected_children=None):
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds <= 0):
        raise ValueError('seconds must be a positive finite number')
    if (isinstance(grace, bool) or not isinstance(grace, (int, float))
            or not math.isfinite(grace) or grace < 0):
        raise ValueError('grace must be a non-negative finite number')
    _check_cancelled(cancel_signal)
    proc = subprocess.Popen(command, start_new_session=True, env=env)
    signalled = False
    try:
        try:
            rc = _wait_for(proc, seconds, cancel_signal)
        except subprocess.TimeoutExpired:
            _check_cancelled(cancel_signal)
            rc = proc.poll()
            if rc is None:
                # Only send SIGINT if Ninja hasn't already become a zombie.
                if _is_alive(proc.pid):
                    try:
                        os.killpg(proc.pid, signal.SIGINT)
                        signalled = True
                    except ProcessLookupError:
                        pass
                stop_deadline = monotonic_now() + grace
                try:
                    rc = _wait_for(proc, grace if signalled else min(grace, 1), cancel_signal)
                except subprocess.TimeoutExpired:
                    rc = 'shutdown-timeout'
                if signalled and rc in NINJA_INTERRUPTED:
                    while (_live_build_pids(proc.pid, protected_children)
                           and monotonic_now() < stop_deadline):
                        _check_cancelled(cancel_signal)
                        time.sleep(0.05)
        if signalled and rc in NINJA_INTERRUPTED:
            result = 'running'
        else:
            result = 'completed' if rc == 0 else f'failed:{rc}'
    finally:
        proc.poll()
        forced, lingering = kill_session(proc.pid, protected_children=protected_children)
        if proc.pid not in lingering:
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                lingering.append(proc.pid)
    _check_cancelled(cancel_signal)
    if not result.startswith('failed:'):
        if lingering:
            return f'failed:lingering-processes({",".join(map(str, lingering))})'
        if forced:
            return f'failed:forced-process-cleanup({",".join(map(str, forced))})'
    return result


def swapped_out_bytes(vmstat='/proc/vmstat'):
    try:
        for line in Path(vmstat).read_text(encoding='utf-8').splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == 'pswpout':
                return int(parts[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError):
        pass
    return None


def swap_delta_mib(before, after):
    if before is None or after is None or after < before:
        return None
    return round((after - before) / 2**20)


def free_gib(path):
    try:
        st = os.statvfs(path)
        return round(st.f_bavail * st.f_frsize / 2**30, 1)
    except OSError:
        return None


def summarize(line, summary_path=None):
    path = os.environ.get('GITHUB_STEP_SUMMARY') if summary_path is None else summary_path
    if path:
        try:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass


def log_status(line):
    try:
        print(line, flush=True)
    except OSError:
        pass


def main():
    cancelled = None

    def on_signal(signum, _frame):
        nonlocal cancelled
        if cancelled is None:
            cancelled = signum

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)

    stage = os.environ.get('STAGE', '')
    if not re.fullmatch(r'0[0-8]', stage):
        raise SystemExit(f'Invalid STAGE: {stage!r}')
    final_stage = stage == '08'

    root = Path(os.environ['CR_ROOT']).resolve(strict=True)
    summary_env = os.environ.get('GITHUB_STEP_SUMMARY', '')
    summary_path = str(Path(summary_env).resolve()) if summary_env else ''

    started_file = Path(os.environ['RUNNER_TEMP']) / 'chromium-job-start'
    raw_started = started_file.read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'-?[0-9]+', raw_started):
        msg = f'Invalid job start timestamp: {raw_started!r}'
        summarize(f'- Stage {stage}: `failed` before compiling; {msg}.', summary_path)
        raise SystemExit(msg)
    started, now = int(raw_started), monotonic_now()
    if started > now:
        msg = f'Invalid future job start timestamp ({started} > {now:.1f})'
        summarize(f'- Stage {stage}: `failed` before compiling; {msg}.', summary_path)
        raise SystemExit(msg)

    setup = round(now - started, 1)
    remaining = compute_budget(started, now, final_stage=final_stage)
    if remaining < MIN_BUDGET_SECONDS:
        msg = (f'Fetch/restore exhausted the time budget (setup {setup / 60:.1f} min, '
               f'remaining {remaining / 60:.1f} min); refusing a no-progress checkpoint')
        summarize(f'- Stage {stage}: `failed` before compiling; {msg}.', summary_path)
        raise SystemExit(msg)

    output_env = os.environ.get('GITHUB_OUTPUT', '')
    if not output_env:
        raise SystemExit('Missing GITHUB_OUTPUT')
    output_file = Path(output_env).resolve()
    output_file.open('a', encoding='utf-8').close()

    run_attempt = os.environ['GITHUB_RUN_ATTEMPT']
    if not re.fullmatch(r'[1-9][0-9]*', run_attempt):
        raise SystemExit('Invalid GITHUB_RUN_ATTEMPT')

    ninja = root / 'src/third_party/ninja/ninja'
    if not (ninja.is_file() and ninja.resolve().is_relative_to(root) and os.access(ninja, os.X_OK)):
        raise SystemExit('Missing executable Chromium Ninja inside build root')

    history = root / 'metadata/runners.jsonl'
    if history.is_symlink():
        raise SystemExit('runners.jsonl must not be a symlink')
    if history.exists():
        if not history.is_file():
            raise SystemExit('runners.jsonl must be a regular file')
        hist_size = history.stat().st_size
        if stage == '00' and hist_size > 0:
            raise SystemExit('Stage 00 found pre-existing runners.jsonl history')
        if hist_size > 0:
            with history.open('rb') as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b'\n':
                    raise SystemExit('Existing runners.jsonl is missing its trailing newline')

    jobs = compute_jobs()
    os.chdir(root / 'src')
    env = dict(os.environ)
    env.setdefault('NINJA_STATUS', '[%f/%t %es] ')
    log_status(f'Building locally, jobs={jobs}, budget={remaining / 60:.0f} minutes, final={final_stage}')

    t0 = monotonic_now()
    swap_before = swapped_out_bytes()
    status = supervise(
        ['third_party/ninja/ninja', '-C', 'out/Release', f'-j{jobs}', 'chrome_public_apk'],
        remaining, env=env, cancel_signal=lambda: cancelled,
        protected_children=enable_orphan_tracking(),
    )
    elapsed = round(monotonic_now() - t0, 1)
    swap_mib = swap_delta_mib(swap_before, swapped_out_bytes())
    disk_free = free_gib(root)
    usage = (f"{'unknown' if swap_mib is None else swap_mib} MiB written to swap while compiling, "
             f"{'unknown' if disk_free is None else disk_free} GiB left free on the build disk")

    if status.startswith('failed:'):
        summarize(f'- Stage {stage}: `{status}` after compiling for {elapsed / 60:.1f} min '
                  f'with -j{jobs}; setup took {setup / 60:.1f} min.', summary_path)
        summarize(f'- Stage {stage} resources: {usage}.', summary_path)
        raise SystemExit(f'Build failed after {elapsed:.1f}s, NOT checkpointing: {status}; {usage}')

    if status == 'running' and final_stage:
        status = 'exhausted'
    record = {
        'stage': stage, 'image_os': os.environ.get('ImageOS'),
        'image_version': os.environ.get('ImageVersion'), 'jobs': jobs,
        'run_attempt': run_attempt, 'final_stage': final_stage,
        'budget_minutes': round(remaining / 60, 1), 'setup_seconds': setup,
        'elapsed_seconds': elapsed, 'status': status,
        'swap_out_mib': swap_mib, 'disk_free_gib': disk_free,
    }
    _check_cancelled(lambda: cancelled)
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, allow_nan=False) + '\n')
    _check_cancelled(lambda: cancelled)
    with output_file.open('a', encoding='utf-8') as f:
        f.write(f'status={status}\n')
    log_status(f'Stage result: {status} ({elapsed:.1f}s)')
    summarize(f'- Stage {stage}: `{status}` after compiling for {elapsed / 60:.1f} min '
              f'with -j{jobs}; setup took {setup / 60:.1f} min.', summary_path)
    summarize(f'- Stage {stage} resources: {usage}.', summary_path)
    # A dead log tee leaves buffered output behind; suppress broken-pipe flush so
    # Python finalization does not turn a completed stage into exit status 120.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except OSError:
            try:
                fd = stream.fileno()
            except (OSError, ValueError, io.UnsupportedOperation):
                continue
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, fd)
            finally:
                os.close(devnull)


if __name__ == '__main__':
    main()
