"""Regression coverage for the shared Linux file boundary."""
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.github/scripts'))
from files import open_regular_file, read_bounded_bytes, write_regular_bytes


class FileBoundary(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'metadata.json'

    def test_bounded_read_and_overwrite(self):
        write_regular_bytes(self.path, b'longer')
        write_regular_bytes(self.path, b'ok')
        self.assertEqual(read_bounded_bytes(self.path, limit=2, root=self.root), b'ok')
        with self.assertRaisesRegex(ValueError, 'Oversized'):
            read_bounded_bytes(self.path, limit=1)
        with open_regular_file(str(self.path)) as stream:
            self.assertEqual(stream.read(), b'ok')

    def test_special_files_are_neither_read_nor_truncated(self):
        target = self.root / 'target'
        target.write_bytes(b'keep')
        for make in (lambda: self.path.symlink_to(target),
                     lambda: os.mkfifo(self.path), lambda: self.path.mkdir()):
            with self.subTest(make=make):
                make()
                for operation in (lambda: read_bounded_bytes(self.path),
                                  lambda: write_regular_bytes(self.path, b'bad')):
                    with self.assertRaises((OSError, ValueError)):
                        operation()
                self.path.rmdir() if self.path.is_dir() else self.path.unlink()
                self.assertEqual(target.read_bytes(), b'keep')

    def test_opened_file_must_stay_inside_root(self):
        inside = self.root / 'inside'
        inside.mkdir()
        self.path.write_bytes(b'outside')
        (inside / 'escape').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'outside build tree'):
            read_bounded_bytes(inside / 'escape' / self.path.name, root=inside)
        with self.assertRaisesRegex(ValueError, 'real directory'):
            read_bounded_bytes(self.path, root=inside / 'escape')

    def test_buffered_writer_handles_short_kernel_writes(self):
        self.path.touch()
        class ShortFile(io.FileIO):
            def write(self, data):
                return super().write(data[:2])

        def buffered(fd, mode):
            return io.BufferedWriter(ShortFile(fd, mode))

        with mock.patch('files.os.fdopen', side_effect=buffered):
            write_regular_bytes(self.path, b'abcdefg')
        self.assertEqual(self.path.read_bytes(), b'abcdefg')

    def test_failed_stream_construction_closes_descriptor(self):
        self.path.write_bytes(b'ok')
        before = set(Path('/proc/self/fd').iterdir())
        with mock.patch('files.os.fdopen', side_effect=OSError('fdopen failed')):
            for operation in (lambda: open_regular_file(self.path),
                              lambda: write_regular_bytes(self.path, b'bad')):
                with self.assertRaisesRegex(OSError, 'fdopen failed'):
                    operation()
        self.assertEqual(set(Path('/proc/self/fd').iterdir()), before)
