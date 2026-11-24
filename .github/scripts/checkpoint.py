#!/usr/bin/env python3
"""Stream-split a checkpoint into hashed parts and verify each part before restoring."""
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
MANIFEST = 'tree.manifest'
MANIFEST_LINE = re.compile(r'([0-9a-f]{64}) ([1-9][0-9]{0,18}) (tree\.part-[0-9]{5})\n')
MAX_MANIFEST_BYTES = MAX_PARTS * 128
BAD_LAYOUT = 'Checkpoint parts missing, unexpected, or non-contiguous'
MISMATCH = 'Checkpoint SHA-256 mismatch; refusing to extract'


def part_name(n):
    return f'tree.part-{n:05d}'


def _require_dir(directory, message):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(message)
    return root


def _open_regular(path):
    """Open a non-symlink regular file without blocking on FIFOs."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError(BAD_LAYOUT) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(BAD_LAYOUT)
        os.set_blocking(fd, True)
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def parts(directory):
    """Return ordered part paths when directory holds exactly manifest + contiguous parts."""
    root = _require_dir(directory, 'Checkpoint directory unreadable')
    names = sorted(p.name for p in root.iterdir())
    count = len(names) - 1
    expected = sorted([MANIFEST, *(part_name(n) for n in range(count))]) if count >= 1 else []
    if not 1 <= count <= MAX_PARTS or names != expected:
        raise ValueError(BAD_LAYOUT)
    for name in names:
        st = (root / name).lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
            raise ValueError(BAD_LAYOUT)
    return [root / part_name(n) for n in range(count)]


def _read_manifest(directory):
    with _open_regular(Path(directory) / MANIFEST) as f:
        raw = f.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError(BAD_LAYOUT)
    return raw


def manifest(directory, expected):
    """Authenticate tree.manifest against expected SHA-256 and return [(path, (sha256, size))]."""
    files = parts(directory)
    raw = _read_manifest(directory)
    if (not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)
            or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected)):
        raise ValueError(MISMATCH)
    try:
        lines = raw.decode('ascii').splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise ValueError(BAD_LAYOUT) from exc
    if len(lines) != len(files):
        raise ValueError(BAD_LAYOUT)
    entries = []
    for path, line in zip(files, lines):
        m = MANIFEST_LINE.fullmatch(line)
        if not m or m[3] != path.name:
            raise ValueError(BAD_LAYOUT)
        size = int(m[2])
        if size > CHUNK_SIZE or size != path.lstat().st_size:
            raise ValueError(BAD_LAYOUT)
        entries.append((path, (m[1], size)))
    return entries


def _open_verified(path, expected_digest, expected_size):
    """Verify a part's SHA-256 and size, then rewind the same open fd for streaming."""
    f = _open_regular(path)
    try:
        if os.fstat(f.fileno()).st_size != expected_size:
            raise ValueError(MISMATCH)
        h = hashlib.sha256()
        total = 0
        while chunk := f.read(IO_BUFFER):
            total += len(chunk)
            if total > expected_size:
                raise ValueError(MISMATCH)
            h.update(chunk)
        if total != expected_size or not hmac.compare_digest(h.hexdigest(), expected_digest):
            raise ValueError(MISMATCH)
        f.seek(0)
        return f
    except BaseException:
        f.close()
        raise


def digest(directory):
    """Compute manifest SHA-256 after verifying every part matches it."""
    parts(directory)
    expected = hashlib.sha256(_read_manifest(directory)).hexdigest()
    for path, (sha, size) in manifest(directory, expected):
        with _open_verified(path, sha, size):
            pass
    return expected


def pack_stream(directory, in_stream, chunk_size=CHUNK_SIZE):
    if not 1 <= chunk_size <= CHUNK_SIZE:
        raise ValueError(f'chunk_size must be between 1 and {CHUNK_SIZE} bytes')
    dest = _require_dir(directory, 'Checkpoint output directory does not exist')
    if any(dest.iterdir()):
        raise ValueError('Checkpoint output must be empty')
    buf_size = min(IO_BUFFER, chunk_size)
    lines, created, total_bytes = [], [], 0
    try:
        pending = in_stream.read(buf_size)
        while pending:
            if len(lines) >= MAX_PARTS:
                raise ValueError('Checkpoint exceeded maximum part count')
            name = part_name(len(lines))
            path = dest / name
            h, written = hashlib.sha256(), 0
            created.append(path)
            with path.open('xb') as out:
                while pending:
                    out.write(pending)
                    h.update(pending)
                    written += len(pending)
                    if written == chunk_size:
                        pending = in_stream.read(buf_size)
                        break
                    pending = in_stream.read(min(buf_size, chunk_size - written))
            lines.append(f'{h.hexdigest()} {written} {name}\n')
            total_bytes += written
        if not lines:
            raise ValueError('Empty checkpoint stream')
        data = ''.join(lines).encode('ascii')
        manifest_path = dest / MANIFEST
        created.append(manifest_path)
        with manifest_path.open('xb') as out:
            out.write(data)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(data).hexdigest(), len(lines), total_bytes


def restore_stream(directory, expected, out_stream):
    entries = manifest(directory, expected)
    total_bytes = 0
    for path, (sha, size) in entries:
        with _open_verified(path, sha, size) as f:
            sent = 0
            while chunk := f.read(IO_BUFFER):
                out_stream.write(chunk)
                sent += len(chunk)
            if sent != size:
                raise ValueError(MISMATCH)
        total_bytes += size
        out_stream.flush()
        path.unlink()
    (Path(directory) / MANIFEST).unlink()
    return len(entries), total_bytes


def report(message):
    print(message, file=sys.stderr)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        try:
            with open(summary, 'a', encoding='utf-8') as f:
                f.write(f'- {message}\n')
        except OSError:
            pass


def main():
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == 'hash':
        print(digest(args[1]), flush=True)
    elif args[:1] == ['pack-stream'] and len(args) in (2, 4):
        chunk_size = CHUNK_SIZE
        if len(args) == 4:
            if args[2] != '--chunk-size' or not re.fullmatch(r'[1-9][0-9]*', args[3]):
                raise ValueError('Invalid --chunk-size argument')
            chunk_size = int(args[3])
        t0 = time.monotonic()
        sha256, count, total = pack_stream(args[1], sys.stdin.buffer, chunk_size=chunk_size)
        elapsed = max(time.monotonic() - t0, 1e-6)
        rate = total / (1024 * 1024) / elapsed
        report(f'Packed {count} checkpoint part(s), {total} bytes ({total / 2**30:.2f} GiB) in {elapsed:.1f}s ({rate:.1f} MiB/s)')
        print(sha256, flush=True)
    elif len(args) == 2 and args[0] == 'restore-stream':
        t0 = time.monotonic()
        count, total = restore_stream(args[1], os.environ.get('EXPECTED_SHA256', ''), sys.stdout.buffer)
        elapsed = max(time.monotonic() - t0, 1e-6)
        rate = total / (1024 * 1024) / elapsed
        report(f'Verified and streamed {count} checkpoint part(s), {total} bytes ({total / 2**30:.2f} GiB) in {elapsed:.1f}s ({rate:.1f} MiB/s)')
    else:
        raise ValueError('Usage: checkpoint.py <hash|pack-stream|restore-stream> <directory> [--chunk-size N]')


if __name__ == '__main__':
    main()
