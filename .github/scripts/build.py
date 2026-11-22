#!/usr/bin/env python3
"""Only OUR orderly deadline may cause a resume; never mask an OOM/error."""
import ctypes
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

# Exit statuses Ninja returns after handling SIGINT itself (it removes the
# outputs of interrupted edges first, so the tree is safe to resume):
#   2   ninja <= 1.12 ("interrupted by user"); Chromium pins 1.12.1.chromium.*
#   130 ninja >= 1.13 (ExitInterrupted)
# Status 1 is a genuine build error ("subcommand failed") and must NOT resume.
NINJA_INTERRUPTED = (2, 130)
# Compile until a fixed offset from the stage start recorded before checkout. The rest
# of the 350-minute job is reserved for checkpoint pack/upload (normal stages)
# or APK verification/upload (final stage); a faster setup means more compiling.
NORMAL_JOB_DEADLINE_SECONDS = 260 * 60
FINAL_JOB_DEADLINE_SECONDS = 325 * 60
MIN_BUDGET_SECONDS = 300
STOP_GRACE_SECONDS = 180
CLEANUP_TIMEOUT_SECONDS = 10


def monotonic_now():
    if hasattr(time, 'CLOCK_BOOTTIME'):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


DEAD_PROC_STATES = frozenset({'Z', 'X', 'x'})


def _is_ascii_uint(text):
    return text.isascii() and text.isdigit()


def compute_jobs(cpu_count=None, mem_kib=None):
    if cpu_count is None:
        cpu_count = os.cpu_count() or 1
    elif isinstance(cpu_count, bool) or not isinstance(cpu_count, int) or cpu_count < 1:
        raise ValueError('cpu_count must be a positive integer')
    if mem_kib is None:
        lines = Path('/proc/meminfo').read_text(encoding='utf-8').splitlines()
        mem_line = next((line for line in lines if line.startswith('MemTotal:')), None)
        if mem_line is None:
            raise RuntimeError('MemTotal missing from /proc/meminfo')
        parts = mem_line.split()
        if len(parts) != 3 or parts[2] != 'kB' or not _is_ascii_uint(parts[1]):
            raise RuntimeError(f'Unexpected MemTotal entry in /proc/meminfo: {mem_line!r}')
        mem_kib = int(parts[1])
    elif isinstance(mem_kib, bool) or not isinstance(mem_kib, int) or mem_kib < 0:
        raise ValueError('mem_kib must be a non-negative integer')
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
        if not _is_ascii_uint(tid):
            continue
        try:
            tstat = (proc_dir / 'task' / tid / 'stat').read_text(encoding='utf-8', errors='replace')
            tfields = tstat[tstat.rindex(')') + 2:].split()
            if tfields[0] not in DEAD_PROC_STATES:
                return True
        except (OSError, ValueError, IndexError):
            pass
    return False


def _is_live_proc(proc_dir, fields):
    if fields[0] not in DEAD_PROC_STATES:
        return True
    return (fields[0] == 'Z' and len(fields) > 17 and _is_ascii_uint(fields[17])
            and int(fields[17]) > 1 and _has_live_thread(proc_dir))


def _proc_fields(proc_dir):
    raw = (proc_dir / 'stat').read_text(encoding='utf-8', errors='replace')
    # Fields after the ")" closing comm: state ppid pgrp session ... num_threads(17).
    fields = raw[raw.rindex(')') + 2:].split()
    if len(fields) < 4:
        raise ValueError('Incomplete /proc stat')
    return fields


def _proc_entries(proc_root='/proc'):
    for entry in os.listdir(proc_root):
        if not _is_ascii_uint(entry):
            continue
        proc_dir = Path(proc_root, entry)
        try:
            fields = _proc_fields(proc_dir)
        except (OSError, ValueError):
            continue
        yield int(entry), proc_dir, fields


def enable_orphan_tracking():
    # Adopt otherwise orphaned descendants even when a tool uses setsid().
    # Keep existing direct children (the log tee) out of the Ninja cleanup.
    pr_set_child_subreaper = 36  # Linux prctl.h
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    if prctl(pr_set_child_subreaper, 1, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return frozenset(pid for pid, _, fields in _proc_entries() if fields[1] == str(os.getpid()))


def _is_adopted(pid, fields, protected_children):
    return (protected_children is not None and fields[1] == str(os.getpid())
            and pid not in protected_children)


def _belongs_to_build(pid, fields, sid, protected_children):
    return pid != os.getpid() and (fields[3] == str(sid) or _is_adopted(pid, fields, protected_children))


def _live_session_pids(sid, proc_root='/proc', protected_children=None):
    pids = []
    for pid, proc_dir, fields in _proc_entries(proc_root):
        if _belongs_to_build(pid, fields, sid, protected_children) and _is_live_proc(proc_dir, fields):
            pids.append(pid)
    return pids


def kill_session(sid, timeout=CLEANUP_TIMEOUT_SECONDS, protected_children=None):
    """Return (forcibly signalled pids, still-live pids) of the build."""
    # Ninja setpgid()s each command; a tool can also start a different session.
    # Re-scan for processes that forked or were adopted during this pass.
    deadline = time.monotonic() + timeout
    killed = set()
    saw_empty = False
    while True:
        pids = _live_session_pids(sid, protected_children=protected_children)
        if not pids:
            if saw_empty or protected_children is None or time.monotonic() >= deadline:
                break
            saw_empty = True
            continue
        saw_empty = False
        for pid in pids:
            proc_dir = Path('/proc', str(pid))
            try:
                fd = os.pidfd_open(pid)
                try:
                    # Recheck ownership and liveness after opening: pidfd_send_signal
                    # succeeds on an already-dead zombie, which must not count as forced.
                    fields = _proc_fields(proc_dir)
                    if (_belongs_to_build(pid, fields, sid, protected_children)
                            and _is_live_proc(proc_dir, fields)):
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                        killed.add(pid)
                finally:
                    os.close(fd)
            except (OSError, ValueError):
                pass
        if time.monotonic() >= deadline:
            pids = _live_session_pids(sid, protected_children=protected_children)
            break
        time.sleep(0.05)
    if protected_children is not None:
        for pid, _, fields in _proc_entries():
            if pid != sid and _is_adopted(pid, fields, protected_children):
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
    return sorted(killed), pids


def check_cancelled(cancel_signal):
    signum = cancel_signal() if cancel_signal is not None else None
    if signum is not None:
        raise SystemExit(f'Build supervisor cancelled by signal {signum}; NOT checkpointing')


def wait_for_ninja(process, timeout, cancel_signal):
    if cancel_signal is None:
        return process.wait(timeout=timeout)
    deadline = monotonic_now() + timeout
    while True:
        check_cancelled(cancel_signal)
        left = deadline - monotonic_now()
        try:
            return process.wait(timeout=max(0, min(left, 0.5)))
        except subprocess.TimeoutExpired:
            if monotonic_now() >= deadline:
                raise


def supervise(command, seconds, grace=STOP_GRACE_SECONDS, env=None, cancel_signal=None,
              protected_children=None):
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds <= 0):
        raise ValueError('seconds must be a positive finite number')
    if (isinstance(grace, bool) or not isinstance(grace, (int, float))
            or not math.isfinite(grace) or grace < 0):
        raise ValueError('grace must be a non-negative finite number')
    check_cancelled(cancel_signal)
    # start_new_session: ninja becomes session leader, so session id == p.pid.
    p = subprocess.Popen(command, start_new_session=True, env=env)
    try:
        try:
            rc = wait_for_ninja(p, seconds, cancel_signal)
            result = 'completed' if rc == 0 else f'failed:{rc}'
        except subprocess.TimeoutExpired:
            # Races where ninja already failed must not be disguised as timeout.
            check_cancelled(cancel_signal)
            rc = p.poll()
            if rc is not None:
                result = 'completed' if rc == 0 else f'failed:{rc}'
            else:
                proc_dir = Path('/proc', str(p.pid))
                try:
                    already_dead = not _is_live_proc(proc_dir, _proc_fields(proc_dir))
                except (OSError, ValueError):
                    already_dead = False
                signalled = not already_dead
                if signalled:
                    try:
                        os.killpg(p.pid, signal.SIGINT)
                    except ProcessLookupError:
                        signalled = False
                try:
                    rc = wait_for_ninja(p, grace if signalled else min(grace, 1), cancel_signal)
                except subprocess.TimeoutExpired:
                    result = 'failed:shutdown-timeout'
                else:
                    if rc == 0:
                        result = 'completed'
                    elif signalled and rc in NINJA_INTERRUPTED:
                        result = 'running'
                    else:
                        # Signal death is not Ninja acknowledging edge cleanup.
                        result = f'failed:{rc}'
    finally:
        p.poll()
        forced, lingering = kill_session(p.pid, protected_children=protected_children)
        if p.pid in lingering:
            p.poll()
        else:
            try:
                p.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                lingering.append(p.pid)
    check_cancelled(cancel_signal)
    if not result.startswith('failed:'):
        if lingering:
            return f'failed:lingering-processes({",".join(map(str, lingering))})'
        if forced:
            # Even if SIGKILL succeeded, an edge could have left partial outputs
            # behind after Ninja claimed it was done. Don't resume that tree.
            return f'failed:forced-process-cleanup({",".join(map(str, forced))})'
    return result


def log_status(line):
    """Best-effort supervisor log; a dead log tee must not abort a finished stage."""
    try:
        print(line, flush=True)
    except OSError:
        pass


def append_runner_record(root, stage, record):
    meta_dir = root / 'metadata'
    if meta_dir.is_symlink():
        raise RuntimeError('Build metadata directory must not be a symlink')
    meta_dir.mkdir(parents=True, exist_ok=True)
    history = meta_dir / 'runners.jsonl'
    payload = (json.dumps(record, allow_nan=False) + '\n').encode('utf-8')
    fd = os.open(history, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError('runners.jsonl must be a regular file')
        os.set_blocking(fd, True)
        if stage == '00' and st.st_size != 0:
            raise RuntimeError('Stage 00 found pre-existing runners.jsonl history')
        if st.st_size > 0:
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b'\n':
                raise RuntimeError('Existing runners.jsonl record is missing its trailing newline')
        os.lseek(fd, 0, os.SEEK_END)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise OSError('Short write to runners.jsonl')
            written += n
    finally:
        os.close(fd)


def summarize(line):
    """Append to the Actions job summary; a diagnostic must never fail the build."""
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def swapped_out_bytes(vmstat='/proc/vmstat'):
    """Bytes written to swap since boot (None if unknown); a diagnostic only."""
    try:
        with open(vmstat, encoding='utf-8') as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0] == 'pswpout' and _is_ascii_uint(parts[1]):
                    return int(parts[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError):
        pass
    return None


def swap_delta_mib(swap_before, swap_after):
    if swap_before is None or swap_after is None or swap_after < swap_before:
        return None
    return round((swap_after - swap_before) / 2**20)


def free_gib(path):
    """Space left for unprivileged writes under path (None if unknown)."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return round(st.f_bavail * st.f_frsize / 2**30, 1)


def main():
    requested_signal = None

    def note_cancel(signum, _frame):
        # Record only, including during spawn and cleanup. Raising from this
        # handler could abandon a just-spawned session or interrupt its reaping.
        nonlocal requested_signal
        if requested_signal is None:
            requested_signal = signum

    def cancel_signal():
        return requested_signal

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, note_cancel)

    stage = os.environ.get('STAGE', '')
    if not re.fullmatch(r'0[0-8]', stage):
        raise SystemExit(f'Invalid STAGE: {stage!r}')
    final_stage = stage == '08'

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
    output_path = os.environ.get('GITHUB_OUTPUT', '')
    if not output_path:
        raise SystemExit('Missing GITHUB_OUTPUT')
    with open(output_path, 'a', encoding='utf-8'):
        pass  # Check writability before spending hours compiling.
    run_attempt = os.environ['GITHUB_RUN_ATTEMPT']
    if not re.fullmatch(r'[1-9][0-9]*', run_attempt):
        raise SystemExit('Invalid GITHUB_RUN_ATTEMPT')
    jobs = compute_jobs()
    os.chdir(root / 'src')
    env = dict(os.environ)
    env.setdefault('NINJA_STATUS', '[%f/%t %es] ')
    log_status(f'Building locally, jobs={jobs}, budget={remaining/60:.0f} minutes, final={final_stage}')
    t0 = monotonic_now()
    swap_before = swapped_out_bytes()
    status = supervise(['third_party/ninja/ninja', '-C', 'out/Release',
                        f'-j{jobs}', 'chrome_public_apk'], remaining, env=env,
                       cancel_signal=cancel_signal, protected_children=enable_orphan_tracking())
    elapsed = round(monotonic_now() - t0, 1)
    # Memory pressure and disk headroom: what the -j cap and later stages depend on.
    swap_after, disk_free = swapped_out_bytes(), free_gib(root)
    swap_mib = swap_delta_mib(swap_before, swap_after)
    usage = (f"{'unknown' if swap_mib is None else swap_mib} MiB written to swap while compiling, "
             f"{'unknown' if disk_free is None else disk_free} GiB left free on the build disk")
    if status.startswith('failed:'):
        summarize(f'- Stage {stage}: `{status}` after compiling for {elapsed / 60:.1f} min '
                  f'with -j{jobs}; setup before compiling took {setup / 60:.1f} min.')
        summarize(f'- Stage {stage} resources: {usage}.')
        # Heavy swapping or a full disk explains many failures (e.g. an OOM kill or ENOSPC).
        raise SystemExit(f'Build failed after {elapsed:.1f}s, NOT checkpointing: {status}; {usage}')
    if status == 'running' and final_stage:
        status = 'exhausted'
    record = dict(stage=stage, image_os=os.environ.get('ImageOS'),
                  image_version=os.environ.get('ImageVersion'), jobs=jobs,
                  run_attempt=run_attempt,
                  final_stage=final_stage, budget_minutes=round(remaining / 60, 1),
                  setup_seconds=setup, elapsed_seconds=elapsed, status=status,
                  swap_out_mib=swap_mib, disk_free_gib=disk_free)
    check_cancelled(cancel_signal)
    append_runner_record(root, stage, record)
    check_cancelled(cancel_signal)
    with open(output_path, 'a', encoding='utf-8') as f:
        f.write(f'status={status}\n')
    log_status(f'Stage result: {status} ({elapsed:.1f}s)')
    summarize(f'- Stage {stage}: `{status}` after compiling for {elapsed / 60:.1f} min with -j{jobs}; '
              f'setup before compiling (runner prep, then fetch or download and restore) took {setup / 60:.1f} min.')
    summarize(f'- Stage {stage} resources: {usage}.')
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except OSError:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, stream.fileno())
            finally:
                os.close(devnull)


if __name__ == '__main__':
    main()
