#!/usr/bin/env python3
"""Stage a local APK before verification or diagnostic upload."""
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


def stage_apk(root, source, destination):
    # Do not follow a symlink (or block opening a FIFO) in place of the APK.
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as src:
        before = os.fstat(src.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise ValueError('Expected a nonempty regular ChromePublic.apk')
        opened_path = Path(f'/proc/self/fd/{src.fileno()}').resolve(strict=True)
        if not opened_path.is_relative_to(root.resolve(strict=True)):
            raise ValueError('ChromePublic.apk resolves outside the build tree')
        # A diagnostic upload must not find a partial file if this step fails.
        with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
            candidate = Path(tmp) / destination.name
            with candidate.open('xb') as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                copied = dst.tell()
            after = os.fstat(src.fileno())
            if (copied != before.st_size or after.st_size != before.st_size
                    or after.st_mtime_ns != before.st_mtime_ns or after.st_ctime_ns != before.st_ctime_ns):
                raise ValueError('ChromePublic.apk changed while staging')
            candidate.chmod(0o400)
            candidate.replace(destination)


def main():
    if len(sys.argv) != 5 or sys.argv[1] != 'stage':
        raise SystemExit('Usage: apk.py stage <build root> <source> <destination>')
    stage_apk(*map(Path, sys.argv[2:]))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as exc:
        raise SystemExit(f'APK staging failed: {exc}') from exc
