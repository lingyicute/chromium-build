#!/usr/bin/env python3
"""Stream-split and hash a multipart checkpoint; verify BEFORE consuming into tar."""
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import time

CHUNK_SIZE = 1024 * 1024 * 1024  # 1 GiB
IO_BUFFER = 8 * 1024 * 1024      # 8 MiB
MAX_PARTS = 100000


def parts(directory):
    try:
        files = sorted(Path(directory).iterdir())
    except OSError as exc:
        raise ValueError('Checkpoint directory unreadable') from exc
    if not files:
        raise ValueError('Checkpoint parts missing, unexpected, or non-contiguous')
    for n, p in enumerate(files):
        try:
            st = p.lstat()
        except OSError as exc:
            raise ValueError('Checkpoint part unreadable') from exc
        if (p.name != f'tree.part-{n:05d}'
                or not stat.S_ISREG(st.st_mode)
                or st.st_size <= 0):
            raise ValueError('Checkpoint parts missing, unexpected, or non-contiguous')
    return files


def digest(files):
    h = hashlib.sha256()
    buf = bytearray(IO_BUFFER)
    view = memoryview(buf)
    for p in files:
        with p.open('rb') as f:
            while True:
                n = f.readinto(buf)
                if not n:
                    break
                h.update(view[:n])
    return h.hexdigest()


def verify(files, expected):
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected) or digest(files) != expected:
        raise ValueError('Checkpoint SHA-256 mismatch; refusing to extract')


def pack_stream(directory, in_stream, chunk_size=CHUNK_SIZE):
    if chunk_size <= 0:
        raise ValueError('chunk_size must be positive')
    dest = Path(directory)
    if not dest.is_dir():
        raise ValueError('Checkpoint output directory does not exist')
    if any(dest.iterdir()):
        raise ValueError('Checkpoint output must be empty')

    h = hashlib.sha256()
    buf = bytearray(min(IO_BUFFER, chunk_size))
    view = memoryview(buf)
    part_index = 0
    written_in_part = 0
    total_bytes = 0
    out = None
    try:
        while True:
            remaining = chunk_size - written_in_part if out is not None else min(len(buf), chunk_size)
            read_slice = view[:min(len(buf), remaining)]
            n = in_stream.readinto(read_slice)
            if not n:
                break
            if out is None:
                if part_index >= MAX_PARTS:
                    raise ValueError('Checkpoint exceeded maximum part count')
                out = (dest / f'tree.part-{part_index:05d}').open('wb')
                written_in_part = 0
            chunk = read_slice[:n]
            out.write(chunk)
            h.update(chunk)
            written_in_part += n
            total_bytes += n
            if written_in_part == chunk_size:
                out.close()
                out = None
                part_index += 1
    finally:
        if out is not None:
            out.close()

    if total_bytes == 0:
        raise ValueError('Empty checkpoint stream')
    part_count = part_index + (1 if written_in_part > 0 and written_in_part < chunk_size else 0)
    return h.hexdigest(), part_count, total_bytes


def restore_stream(directory, expected, out_stream):
    files = parts(directory)
    verify(files, expected)
    buf = bytearray(IO_BUFFER)
    view = memoryview(buf)
    total_bytes = 0
    # Unlink each compressed chunk after it is consumed to reduce peak disk.
    # Downstream failure loses this disposable local copy, NOT the artifact.
    for p in files:
        with p.open('rb') as f:
            while True:
                n = f.readinto(buf)
                if not n:
                    break
                out_stream.write(view[:n])
                total_bytes += n
        out_stream.flush()
        p.unlink()
    return len(files), total_bytes


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        raise ValueError('Usage: checkpoint.py <hash|pack-stream|restore-stream> <directory> [--chunk-size N]')
    operation, directory = args[0], args[1]
    if operation == 'hash' and len(args) == 2:
        print(digest(parts(directory)))
    elif operation == 'pack-stream' and len(args) in (2, 4):
        chunk_size = CHUNK_SIZE
        if len(args) == 4:
            if args[2] != '--chunk-size':
                raise ValueError('Unknown option')
            chunk_size = int(args[3])
        t0 = time.monotonic()
        sha256, count, total = pack_stream(directory, sys.stdin.buffer, chunk_size=chunk_size)
        elapsed = max(time.monotonic() - t0, 1e-6)
        rate = total / (1024 * 1024) / elapsed
        print(f'Packed {count} checkpoint part(s), {total} bytes ({total / 2**30:.2f} GiB) in {elapsed:.1f}s ({rate:.1f} MiB/s)', file=sys.stderr)
        print(sha256)
    elif operation == 'restore-stream' and len(args) == 2:
        t0 = time.monotonic()
        count, total = restore_stream(directory, os.environ['EXPECTED_SHA256'], sys.stdout.buffer)
        elapsed = max(time.monotonic() - t0, 1e-6)
        rate = total / (1024 * 1024) / elapsed
        print(f'Verified and streamed {count} checkpoint part(s), {total} bytes ({total / 2**30:.2f} GiB) in {elapsed:.1f}s ({rate:.1f} MiB/s)', file=sys.stderr)
    else:
        raise ValueError('Unknown operation or invalid arguments')


if __name__ == '__main__':
    main()
