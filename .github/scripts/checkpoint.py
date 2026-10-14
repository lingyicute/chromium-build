#!/usr/bin/env python3
"""Hash a multipart checkpoint; verify BEFORE consuming any bytes into tar."""
import hashlib
import os
from pathlib import Path
import re
import sys

def parts(directory):
    files = sorted(Path(directory).iterdir())
    if not files or any(p.name != f'tree.part-{n:05d}' or not p.is_file() or p.is_symlink()
                        for n, p in enumerate(files)):
        raise ValueError('Checkpoint parts missing, unexpected, or non-contiguous')
    return files

def digest(files):
    h = hashlib.sha256()
    for p in files:
        with p.open('rb') as f:
            for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
                h.update(block)
    return h.hexdigest()

def verify(files, expected):
    if not re.fullmatch(r'[0-9a-f]{64}', expected) or digest(files) != expected:
        raise ValueError('Checkpoint SHA-256 mismatch; refusing to extract')

def main():
    operation, directory = sys.argv[1:3]
    files = parts(directory)
    if operation == 'hash':
        print(digest(files))
    elif operation == 'restore-stream':
        verify(files, os.environ['EXPECTED_SHA256'])
        # Unlink each compressed chunk after it is consumed to reduce peak disk.
        # Downstream failure loses this disposable local copy, NOT the artifact.
        for p in files:
            with p.open('rb') as f:
                for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
                    sys.stdout.buffer.write(block)
            sys.stdout.buffer.flush()
            p.unlink()
    else:
        raise ValueError('Unknown operation')

if __name__ == '__main__':
    main()
