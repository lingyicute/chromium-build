"""Offline tests for the local APK publication boundary."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / '.github/scripts/apk.py'


class StageApk(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / 'build'
        self.root.mkdir()
        self.source = self.root / 'ChromePublic.apk'
        self.destination = self.base / 'staged.apk'

    def stage(self, source=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), 'stage', str(self.root), str(source or self.source), str(self.destination)],
            capture_output=True, text=True, timeout=3,
        )

    def test_stage_creates_an_independent_snapshot(self):
        self.source.write_bytes(b'chromium build output')
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.source.write_bytes(b'changed')
        self.assertEqual(self.destination.read_bytes(), b'chromium build output')

    def test_unsafe_and_empty_files_are_not_staged(self):
        for make in (lambda p: p.touch(), os.mkfifo, lambda p: p.symlink_to('/dev/null')):
            with self.subTest(make=make):
                make(self.source)
                self.assertNotEqual(self.stage().returncode, 0)
                self.assertFalse(self.destination.exists())
                self.source.unlink()

    def test_file_reached_through_an_external_directory_is_not_staged(self):
        (self.base / 'unrelated.apk').write_bytes(b'not a build output')
        (self.root / 'external').symlink_to(self.base, target_is_directory=True)
        result = self.stage(self.root / 'external/unrelated.apk')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('outside the build tree', result.stderr)
        self.assertFalse(self.destination.exists())
