"""Round-trip a tiny real tar/Zstandard relay, without sudo or Chromium."""
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / '.github/scripts'
CODEC = SCRIPTS / 'checkpoint.py'
RELAY = 'source "$1/common.sh"; source "$1/relay.sh"; '



ZSTD = shutil.which('zstd')


@unittest.skipUnless(ZSTD, 'Zstandard CLI not installed')
class CompressedRelay(unittest.TestCase):
    def test_compressed_relay_preserves_hidden_files_and_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree, parts, restored = [root / name for name in ('tree', 'parts', 'restored')]
            for directory in (tree, parts, restored):
                directory.mkdir()
            payload = random.Random(19).randbytes(32768)
            original = tree / '.ninja_log'
            original.write_bytes(payload)
            original.chmod(0o640)
            ts = 1700000000123456789
            os.utime(original, ns=(ts, ts))
            (tree / 'log-link').symlink_to('.ninja_log')

            env = dict(os.environ, GITHUB_STEP_SUMMARY='', RUNNER_TEMP=tmp)
            pack = subprocess.run(
                ['bash', '-euo', 'pipefail', '-c',
                 RELAY + 'archive_relay_tree "$2" '
                 '| "$6" -T2 -3 -c | "$3" "$4" pack-stream "$5" --chunk-size 4096',
                 '_', str(SCRIPTS), str(tree), sys.executable, str(CODEC), str(parts), ZSTD],
                env=env, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(pack.returncode, 0, pack.stderr)
            self.assertEqual(list(tree.iterdir()), [])
            self.assertGreater(len(list(parts.glob('tree.part-*'))), 1)
            restore = subprocess.run(
                ['bash', '-euo', 'pipefail', '-c',
                 RELAY + '"$2" "$3" restore-stream "$4" | "$6" -d -c | extract_relay_tree "$5"',
                 '_', str(SCRIPTS), sys.executable, str(CODEC), str(parts), str(restored), ZSTD],
                env=dict(env, EXPECTED_SHA256=pack.stdout.strip()),
                text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(restore.returncode, 0, restore.stderr)
            self.assertEqual(list(parts.iterdir()), [])
            restored_log = restored / '.ninja_log'
            self.assertEqual(restored_log.read_bytes(), payload)
            self.assertEqual(restored_log.stat().st_mode & 0o777, 0o640)
            self.assertEqual(restored_log.stat().st_mtime_ns, ts)
            self.assertEqual(os.readlink(restored / 'log-link'), '.ninja_log')
