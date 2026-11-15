#!/usr/bin/env python3
"""Stream-split a checkpoint into hashed parts; verify each part BEFORE tar consumes it."""
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
# The checkpoint digest handed to the next job is the SHA-256 of this manifest,
# which pins the SHA-256 and size of every part. Restore can then verify a part
# and stream it while it is still in the page cache, instead of reading the
# whole checkpoint from disk once to hash it and a second time to extract it.
MANIFEST = 'tree.manifest'
MANIFEST_LINE = re.compile(r'([0-9a-f]{64}) ([1-9][0-9]{0,18}) (tree\.part-[0-9]{5})\n')
MAX_MANIFEST_BYTES = MAX_PARTS * 128
BAD_LAYOUT = 'Checkpoint parts missing, unexpected, or non-contiguous'
MISMATCH = 'Checkpoint SHA-256 mismatch; refusing to extract'


def part_name(n):
    return f'tree.part-{n:05d}'


def parts(directory):
    """Part paths of a directory holding exactly the manifest and contiguous parts."""
    root = Path(directory)
    try:
        names = sorted(p.name for p in root.iterdir())
    except OSError as exc:
        raise ValueError('Checkpoint directory unreadable') from exc
    count = len(names) - 1
    if not 1 <= count <= MAX_PARTS or names != sorted([MANIFEST] + [part_name(n) for n in range(count)]):
        raise ValueError(BAD_LAYOUT)
    for name in names:
        try:
            st = (root / name).lstat()
        except OSError as exc:
            raise ValueError('Checkpoint part unreadable') from exc
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
            raise ValueError(BAD_LAYOUT)
    return [root / part_name(n) for n in range(count)]


def sha256_file(path):
    h = hashlib.sha256()
    buf = bytearray(IO_BUFFER)
    view = memoryview(buf)
    size = 0
    with path.open('rb') as f:
        while True:
            n = f.readinto(buf)
            if not n:
                break
            h.update(view[:n])
            size += n
    return h.hexdigest(), size


def _manifest_bytes(directory):
    with (Path(directory) / MANIFEST).open('rb') as f:
        return f.read(MAX_MANIFEST_BYTES + 1)


def manifest(directory, expected):
    """Authenticate the manifest with the producer digest; return [(part, (sha256, size))]."""
    files = parts(directory)
    raw = _manifest_bytes(directory)
    if (not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)
            or hashlib.sha256(raw).hexdigest() != expected):
        raise ValueError(MISMATCH)
    lines = raw.decode('ascii', 'replace').splitlines(keepends=True)
    if len(lines) != len(files):
        raise ValueError(BAD_LAYOUT)
    entries = []
    for path, line in zip(files, lines):
        m = MANIFEST_LINE.fullmatch(line)
        # A wrong size (e.g. a truncated download) is caught before any byte is streamed.
        if not m or m[3] != path.name or int(m[2]) != path.lstat().st_size:
            raise ValueError(BAD_LAYOUT)
        entries.append((path, (m[1], int(m[2]))))
    return entries


def verify_part(path, entry):
    if sha256_file(path) != entry:
        raise ValueError(MISMATCH)


def digest(directory):
    """SHA-256 of the manifest, after checking that every part matches it."""
    parts(directory)
    expected = hashlib.sha256(_manifest_bytes(directory)).hexdigest()
    for path, entry in manifest(directory, expected):
        verify_part(path, entry)
    return expected


def pack_stream(directory, in_stream, chunk_size=CHUNK_SIZE):
    if chunk_size <= 0:
        raise ValueError('chunk_size must be positive')
    dest = Path(directory)
    if not dest.is_dir():
        raise ValueError('Checkpoint output directory does not exist')
    if any(dest.iterdir()):
        raise ValueError('Checkpoint output must be empty')

    buf = bytearray(min(IO_BUFFER, chunk_size))
    view = memoryview(buf)
    lines = []
    total_bytes = 0
    out = h = name = None
    written = 0
    try:
        while True:
            room = chunk_size - written if out is not None else chunk_size
            read_slice = view[:min(len(buf), room)]
            n = in_stream.readinto(read_slice)
            if not n:
                break
            if out is None:
                if len(lines) >= MAX_PARTS:
                    raise ValueError('Checkpoint exceeded maximum part count')
                name = part_name(len(lines))
                out = (dest / name).open('wb')
                h = hashlib.sha256()
                written = 0
            chunk = read_slice[:n]
            out.write(chunk)
            h.update(chunk)
            written += n
            total_bytes += n
            if written == chunk_size:
                out.close()
                out = None
                lines.append(f'{h.hexdigest()} {written} {name}\n')
        if out is not None:
            out.close()
            out = None
            lines.append(f'{h.hexdigest()} {written} {name}\n')
    finally:
        if out is not None:
            out.close()

    if total_bytes == 0:
        raise ValueError('Empty checkpoint stream')
    data = ''.join(lines).encode('ascii')
    (dest / MANIFEST).write_bytes(data)
    return hashlib.sha256(data).hexdigest(), len(lines), total_bytes


def restore_stream(directory, expected, out_stream):
    entries = manifest(directory, expected)
    buf = bytearray(IO_BUFFER)
    view = memoryview(buf)
    total_bytes = 0
    # Unlink each compressed chunk after it is consumed to reduce peak disk.
    # Downstream failure loses this disposable local copy, NOT the artifact.
    for path, entry in entries:
        # zstd/tar never see an unverified byte. The part just hashed is still
        # in the page cache, so streaming it costs no second disk read.
        verify_part(path, entry)
        with path.open('rb') as f:
            while True:
                n = f.readinto(buf)
                if not n:
                    break
                out_stream.write(view[:n])
                total_bytes += n
        out_stream.flush()
        path.unlink()
    (Path(directory) / MANIFEST).unlink()
    return len(entries), total_bytes


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        raise ValueError('Usage: checkpoint.py <hash|pack-stream|restore-stream> <directory> [--chunk-size N]')
    operation, directory = args[0], args[1]
    if operation == 'hash' and len(args) == 2:
        print(digest(directory))
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
