#!/usr/bin/env python3
"""Stream-split a checkpoint into hashed parts; verify each part BEFORE tar consumes it."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
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


def _open_regular_ro(path):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(BAD_LAYOUT) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(BAD_LAYOUT)
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def sha256_file(path):
    h = hashlib.sha256()
    buf = bytearray(IO_BUFFER)
    view = memoryview(buf)
    size = 0
    with _open_regular_ro(path) as f:
        while True:
            n = f.readinto(buf)
            if not n:
                break
            h.update(view[:n])
            size += n
    return h.hexdigest(), size


def _manifest_bytes(directory):
    with _open_regular_ro(Path(directory) / MANIFEST) as f:
        raw = f.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError(BAD_LAYOUT)
    return raw


def manifest(directory, expected):
    """Authenticate the manifest with the producer digest; return [(part, (sha256, size))]."""
    files = parts(directory)
    raw = _manifest_bytes(directory)
    if (not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)
            or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected)):
        raise ValueError(MISMATCH)
    try:
        text = raw.decode('ascii')
    except UnicodeDecodeError as exc:
        raise ValueError(BAD_LAYOUT) from exc
    lines = text.splitlines(keepends=True)
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
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError('chunk_size must be positive')
    dest = Path(directory)
    if not dest.is_dir():
        raise ValueError('Checkpoint output directory does not exist')
    if any(dest.iterdir()):
        raise ValueError('Checkpoint output must be empty')

    buf = bytearray(min(IO_BUFFER, chunk_size))
    view = memoryview(buf)
    lines = []
    created = []
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
                part_path = dest / name
                out = part_path.open('wb')
                created.append(part_path)
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
        if total_bytes == 0:
            raise ValueError('Empty checkpoint stream')
        data = ''.join(lines).encode('ascii')
        manifest_path = dest / MANIFEST
        manifest_path.write_bytes(data)
        created.append(manifest_path)
    except BaseException:
        if out is not None:
            out.close()
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return hashlib.sha256(data).hexdigest(), len(lines), total_bytes


def restore_stream(directory, expected, out_stream):
    entries = manifest(directory, expected)
    buf = bytearray(IO_BUFFER)
    view = memoryview(buf)
    total_bytes = 0
    # Unlink each compressed chunk after it is consumed to reduce peak disk.
    # Downstream failure loses this disposable local copy, NOT the artifact.
    # zstd/tar never see an unverified byte: a part is streamed only after its
    # own check. The next part is hashed meanwhile on a helper thread, so that
    # disk read overlaps extraction and the part is streamed from the page cache.
    with ThreadPoolExecutor(max_workers=1) as pool:
        check = pool.submit(verify_part, *entries[0])
        for n, (path, (_, expected_size)) in enumerate(entries):
            check.result()
            if n + 1 < len(entries):
                check = pool.submit(verify_part, *entries[n + 1])
            part_bytes = 0
            with _open_regular_ro(path) as f:
                while True:
                    size = f.readinto(buf)
                    if not size:
                        break
                    out_stream.write(view[:size])
                    part_bytes += size
            if part_bytes != expected_size:
                raise ValueError(MISMATCH)
            total_bytes += part_bytes
            out_stream.flush()
            path.unlink()
    (Path(directory) / MANIFEST).unlink()
    return len(entries), total_bytes


def report(message):
    """Log to stderr and, best effort, to the Actions job summary."""
    print(message, file=sys.stderr)
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    try:
        with open(path, 'a') as f:
            f.write(f'- {message}\n')
    except OSError:
        pass


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
        report(f'Packed {count} checkpoint part(s), {total} bytes ({total / 2**30:.2f} GiB) in {elapsed:.1f}s ({rate:.1f} MiB/s)')
        print(sha256)
    elif operation == 'restore-stream' and len(args) == 2:
        t0 = time.monotonic()
        count, total = restore_stream(directory, os.environ['EXPECTED_SHA256'], sys.stdout.buffer)
        elapsed = max(time.monotonic() - t0, 1e-6)
        rate = total / (1024 * 1024) / elapsed
        report(f'Verified and streamed {count} checkpoint part(s), {total} bytes ({total / 2**30:.2f} GiB) in {elapsed:.1f}s ({rate:.1f} MiB/s)')
    else:
        raise ValueError('Unknown operation or invalid arguments')


if __name__ == '__main__':
    main()
