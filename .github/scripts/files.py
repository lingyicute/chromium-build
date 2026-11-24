"""Shared file I/O for build metadata and artifacts (Linux runners only)."""
import os
from pathlib import Path
import stat


MAX_BYTES = 1024 * 1024


def open_regular_file(path, root=None):
    """Pin a non-symlink regular file without blocking on special files."""
    path = Path(path)
    if root is not None:
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            raise ValueError('Build root must be a real directory')
        resolved_root = root.resolve(strict=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f'Expected regular file: {path.name}')
        if root is not None:
            opened = Path(f'/proc/self/fd/{fd}').resolve(strict=True)
            if not opened.is_relative_to(resolved_root):
                raise ValueError(f'Path resolves outside build tree: {path.name}')
        os.set_blocking(fd, True)
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def read_bounded_bytes(path, limit=MAX_BYTES, root=None):
    path = Path(path)
    with open_regular_file(path, root=root) as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f'Oversized build metadata: {path.name}')
    return raw


def write_regular_bytes(path, data):
    path = Path(path)
    # Check the descriptor before truncating: never damage a symlink target or
    # block on a FIFO. A buffered file handles partial writes for us.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f'Expected regular file: {path.name}')
        os.set_blocking(fd, True)
        os.ftruncate(fd, 0)
        stream = os.fdopen(fd, 'wb')
    except BaseException:
        os.close(fd)
        raise
    with stream:
        stream.write(data)
