"""Round-trip a tiny real tar/Zstandard relay, without sudo or Chromium."""
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest


CODEC = Path(__file__).resolve().parents[1] / '.github/scripts/checkpoint.py'


@unittest.skipUnless(shutil.which('zstd'), 'Zstandard CLI not installed')
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

            env = dict(os.environ, GITHUB_STEP_SUMMARY='')
            env.pop('TAR_OPTIONS', None)
            pack = subprocess.run(
                ['bash', '-euo', 'pipefail', '-c',
                 'cd "$1"; shopt -s dotglob nullglob; entries=(*)\n'
                 'tar --format=posix --pax-option=delete=atime,delete=ctime --remove-files -cf - -- "${entries[@]}" '
                 '| zstd -T2 -3 -c | "$2" "$3" pack-stream "$4" --chunk-size 4096',
                 '_', str(tree), sys.executable, str(CODEC), str(parts)],
                env=env, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(pack.returncode, 0, pack.stderr)
            self.assertEqual(list(tree.iterdir()), [])
            self.assertGreater(len(list(parts.glob('tree.part-*'))), 1)
            restore = subprocess.run(
                ['bash', '-euo', 'pipefail', '-c',
                 '"$1" "$2" restore-stream "$3" | zstd -d -c | tar --no-same-owner -xpf - -C "$4"',
                 '_', sys.executable, str(CODEC), str(parts), str(restored)],
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
