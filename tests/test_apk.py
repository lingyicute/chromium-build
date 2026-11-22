"""Offline tests for the local APK publication boundary."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / '.github/scripts/apk.py'
SPEC = importlib.util.spec_from_file_location('apk', SCRIPT)
apk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apk)


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

    def test_copy_is_bounded_and_detects_growth(self):
        original = b'APK bytes'
        self.source.write_bytes(original)
        fdopen = os.fdopen

        @contextmanager
        def growing_source(fd, mode):
            with fdopen(fd, mode) as source:
                def read(size):
                    self.assertEqual(size, len(original))
                    with self.source.open('ab') as writer:
                        writer.write(b'in progress')
                    return source.read(size)

                yield SimpleNamespace(read=read, fileno=source.fileno)

        with mock.patch.object(apk.os, 'fdopen', growing_source):
            with self.assertRaisesRegex(ValueError, 'changed while staging'):
                apk.stage_apk(self.root, self.source, self.destination)
        self.assertEqual(self.source.read_bytes(), original + b'in progress')
        self.assertFalse(self.destination.exists())

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


class InspectSigner(unittest.TestCase):
    def test_source_stamp_signer_label_is_rejected(self):
        pin = SCRIPT.parents[2] / 'config/debug_signer_sha256.txt'
        fingerprint = pin.read_text().strip()
        valid = ('Verified using v2 scheme (APK Signature Scheme v2): true\n'
                 f'Number of signers: 1\nSigner #1 certificate SHA-256 digest: {fingerprint}\n')
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'apk-signature.txt'
            log.write_text(valid)
            apk.inspect_signer(log, pin)
            # apksigner --print-certs labels the stamp certificate "Source Stamp Signer".
            for label in ('Source Stamp Signer', 'SourceStamp Signer'):
                log.write_text(valid + f'{label} certificate DN: CN=Stamp\n')
                with self.assertRaisesRegex(ValueError, 'pinned Chromium development signer'):
                    apk.inspect_signer(log, pin)
