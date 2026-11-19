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


def _require_real_dir(directory, message):
    root = Path(directory)
    try:
        st = root.lstat()
    except OSError as exc:
        raise ValueError(message) from exc
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(message)
    return root


def parts(directory):
    """Part paths of a directory holding exactly the manifest and contiguous parts."""
    root = _require_real_dir(directory, 'Checkpoint directory unreadable')
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
    # If a regular file was replaced by a FIFO, fail fstat instead of blocking.
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(BAD_LAYOUT) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(BAD_LAYOUT)
        os.set_blocking(fd, True)
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def _read_exact(stream, buf):
    total = 0
    while total < len(buf):
        n = stream.readinto(buf[total:])
        if n == 0:
            return False
        if n is None or not 0 < n <= len(buf) - total:
            raise OSError('Invalid read from checkpoint part')
        total += n
    return True


def _manifest_bytes(directory):
    root = _require_real_dir(directory, BAD_LAYOUT)
    with _open_regular_ro(root / MANIFEST) as f:
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
        try:
            st = path.lstat()
        except OSError as exc:
            raise ValueError(BAD_LAYOUT) from exc
        # A wrong size (e.g. a truncated download) is caught before any byte is streamed.
        if not m or m[3] != path.name or not stat.S_ISREG(st.st_mode):
            raise ValueError(BAD_LAYOUT)
        size = int(m[2])
        if size != st.st_size or size > CHUNK_SIZE:
            raise ValueError(BAD_LAYOUT)
        entries.append((path, (m[1], size)))
    return entries


def _verify_and_open(path, entry):
    expected_digest, expected_size = entry
    f = _open_regular_ro(path)
    try:
        if os.fstat(f.fileno()).st_size != expected_size:
            raise ValueError(MISMATCH)
        h = hashlib.sha256()
        view = memoryview(bytearray(IO_BUFFER))
        remaining = expected_size
        prefixes = []
        while remaining:
            block = view[:min(remaining, len(view))]
            if not _read_exact(f, block):
                raise ValueError(MISMATCH)
            h.update(block)
            # These prefix digests are trusted only if the whole part matches
            # the authenticated manifest. Recheck each buffered block before
            # streaming: an open fd pins the inode, not its contents.
            prefixes.append(h.copy().digest())
            remaining -= len(block)
        if f.read(1) or not hmac.compare_digest(h.hexdigest(), expected_digest):
            raise ValueError(MISMATCH)
        f.seek(0)
        return f, prefixes
    except BaseException:
        f.close()
        raise


def verify_part(path, entry):
    f, _ = _verify_and_open(path, entry)
    f.close()


def digest(directory):
    """SHA-256 of the manifest, after checking that every part matches it."""
    parts(directory)
    expected = hashlib.sha256(_manifest_bytes(directory)).hexdigest()
    for path, entry in manifest(directory, expected):
        verify_part(path, entry)
    return expected


def _open_exclusive_wo(path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o644)
    try:
        return os.fdopen(fd, 'wb')
    except BaseException:
        os.close(fd)
        raise


def _write_all(stream, data):
    view = memoryview(data)
    written = 0
    while written < len(view):
        n = stream.write(view[written:])
        if n is None or not 0 < n <= len(view) - written:
            raise OSError('Short write to checkpoint stream')
        written += n


def pack_stream(directory, in_stream, chunk_size=CHUNK_SIZE):
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or not 1 <= chunk_size <= CHUNK_SIZE:
        raise ValueError(f'chunk_size must be between 1 and {CHUNK_SIZE} bytes')
    dest = _require_real_dir(directory, 'Checkpoint output directory does not exist')
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
            if n == 0:
                break
            if n is None or not 0 < n <= len(read_slice):
                raise OSError('Invalid read from checkpoint stream')
            if out is None:
                if len(lines) >= MAX_PARTS:
                    raise ValueError('Checkpoint exceeded maximum part count')
                name = part_name(len(lines))
                part_path = dest / name
                out = _open_exclusive_wo(part_path)
                created.append(part_path)
                h = hashlib.sha256()
                written = 0
            chunk = read_slice[:n]
            _write_all(out, chunk)
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
        out = _open_exclusive_wo(manifest_path)
        created.append(manifest_path)
        _write_all(out, data)
        out.close()
        out = None
    except BaseException:
        if out is not None:
            try:
                out.close()
            except OSError:
                pass
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
    # Check the next part in the background. The current part is read a second
    # time (usually from the cache); rehash it block by block before writing.
    with ThreadPoolExecutor(max_workers=1) as pool:
        next_check = pool.submit(_verify_and_open, *entries[0])
        try:
            for n, (path, (_, expected_size)) in enumerate(entries):
                f, prefixes = next_check.result()
                next_check = None
                with f:
                    if n + 1 < len(entries):
                        next_check = pool.submit(_verify_and_open, *entries[n + 1])
                    if os.fstat(f.fileno()).st_size != expected_size:
                        raise ValueError(MISMATCH)
                    h = hashlib.sha256()
                    remaining = expected_size
                    for prefix in prefixes:
                        block = view[:min(remaining, len(view))]
                        if not _read_exact(f, block):
                            raise ValueError(MISMATCH)
                        h.update(block)
                        if not hmac.compare_digest(h.copy().digest(), prefix):
                            raise ValueError(MISMATCH)
                        _write_all(out_stream, block)
                        remaining -= len(block)
                    if remaining or f.read(1):
                        raise ValueError(MISMATCH)
                total_bytes += expected_size
                out_stream.flush()
                path.unlink()
        finally:
            if next_check is not None:
                try:
                    next_check.result()[0].close()
                except Exception:
                    pass
    (Path(directory) / MANIFEST).unlink()
    return len(entries), total_bytes


def report(message):
    """Log to stderr and, best effort, to the Actions job summary."""
    print(message, file=sys.stderr)
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f'- {message}\n')
    except OSError:
        pass


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        raise ValueError('Usage: checkpoint.py <hash|pack-stream|restore-stream> <directory> [--chunk-size N]')
    operation, directory = args[0], args[1]
    if operation == 'hash' and len(args) == 2:
        print(digest(directory), flush=True)
    elif operation == 'pack-stream' and len(args) in (2, 4):
        chunk_size = CHUNK_SIZE
        if len(args) == 4:
            if args[2] != '--chunk-size':
                raise ValueError('Unknown option')
            if not re.fullmatch(r'[1-9][0-9]*', args[3]):
                raise ValueError('chunk_size must be a positive integer')
            chunk_size = int(args[3])
        t0 = time.monotonic()
        sha256, count, total = pack_stream(directory, sys.stdin.buffer, chunk_size=chunk_size)
        elapsed = max(time.monotonic() - t0, 1e-6)
        rate = total / (1024 * 1024) / elapsed
        report(f'Packed {count} checkpoint part(s), {total} bytes ({total / 2**30:.2f} GiB) in {elapsed:.1f}s ({rate:.1f} MiB/s)')
        print(sha256, flush=True)
    elif operation == 'restore-stream' and len(args) == 2:
        t0 = time.monotonic()
        count, total = restore_stream(directory, os.environ.get('EXPECTED_SHA256', ''), sys.stdout.buffer)
        elapsed = max(time.monotonic() - t0, 1e-6)
        rate = total / (1024 * 1024) / elapsed
        report(f'Verified and streamed {count} checkpoint part(s), {total} bytes ({total / 2**30:.2f} GiB) in {elapsed:.1f}s ({rate:.1f} MiB/s)')
    else:
        raise ValueError('Unknown operation or invalid arguments')


if __name__ == '__main__':
    main()
