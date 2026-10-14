"""Offline smoke tests. Python stdlib only; does NOT compile Chromium."""
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / '.github/scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolve = load('resolve')
checkpoint = load('checkpoint')
build = load('build')


class Resolution(unittest.TestCase):
    def sample(self):
        return [dict(channel='Stable', platform='Android', version='155.0.8059.79',
                     hashes={'chromium': 'a' * 40})]

    def test_valid(self):
        self.assertEqual(resolve.validate(self.sample()), ('155.0.8059.79', 'a' * 40))

    def test_bad_input(self):
        for field, value in [('channel', 'Beta'), ('platform', 'Windows'),
                             ('version', '155.0.1.2; echo injection')]:
            data = self.sample()
            data[0][field] = value
            with self.assertRaises(ValueError):
                resolve.validate(data)
        data = self.sample()
        data[0]['hashes']['chromium'] = 'main'
        with self.assertRaises(ValueError):
            resolve.validate(data)
        with self.assertRaises(ValueError):
            resolve.validate([])


class Checkpoint(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'tree.part-00000').write_bytes(b'first')
        (self.root / 'tree.part-00001').write_bytes(b'second')
        self.expected = hashlib.sha256(b'firstsecond').hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def test_hash(self):
        self.assertEqual(checkpoint.digest(checkpoint.parts(self.root)), self.expected)
        checkpoint.verify(checkpoint.parts(self.root), self.expected)

    def test_tampering(self):
        (self.root / 'tree.part-00001').write_bytes(b'bad')
        with self.assertRaises(ValueError):
            checkpoint.verify(checkpoint.parts(self.root), self.expected)

    def test_missing_part(self):
        (self.root / 'tree.part-00000').unlink()
        with self.assertRaises(ValueError):
            checkpoint.parts(self.root)

    def test_extra_file(self):
        (self.root / 'extra').touch()
        with self.assertRaises(ValueError):
            checkpoint.parts(self.root)

    def test_symlink(self):
        p = self.root / 'tree.part-00001'
        p.unlink()
        p.symlink_to(self.root / 'tree.part-00000')
        with self.assertRaises(ValueError):
            checkpoint.parts(self.root)

    def consume(self, expected):
        return subprocess.run(
            [sys.executable, str(ROOT / '.github/scripts/checkpoint.py'),
             'restore-stream', str(self.root)],
            env=dict(os.environ, EXPECTED_SHA256=expected), capture_output=True)

    def test_consume_verified_chunks(self):
        p = self.consume(self.expected)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, b'firstsecond')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_bad_hash_does_not_consume(self):
        p = self.consume('0' * 64)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b'')
        self.assertEqual(len(list(self.root.iterdir())), 2)

    def test_tar_metadata_round_trip(self):
        # Tests real GNU tar without compression, not zstd or GitHub storage.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'src'
            src.mkdir()
            f = src / '.ninja_log'
            f.write_text('state')
            ns = 1700000000123456789
            os.utime(f, ns=(ns, ns))
            (src / 'tool').write_text('executable')
            (src / 'tool').chmod(0o755)
            (src / 'link').symlink_to('tool')
            archive = root / 'test.tar'
            subprocess.run(['tar', '--format=posix', '--pax-option=delete=atime,delete=ctime',
                            '--remove-files', '-C', str(src), '-cf', str(archive), '.'], check=True)
            self.assertFalse(src.exists())
            src.mkdir()
            subprocess.run(['tar', '--no-same-owner', '-xpf', str(archive), '-C', str(src)], check=True)
            self.assertEqual((src / '.ninja_log').stat().st_mtime_ns, ns)
            self.assertEqual((src / '.ninja_log').read_text(), 'state')
            self.assertTrue((src / 'link').is_symlink())
            self.assertTrue(os.access(src / 'tool', os.X_OK))


class Deadline(unittest.TestCase):
    def run_command(self, code, seconds=5, grace=1):
        return build.supervise([sys.executable, '-c', code], seconds, grace)

    def test_success(self):
        self.assertEqual(self.run_command('pass'), 'completed')

    def test_compile_error(self):
        self.assertEqual(self.run_command('raise SystemExit(1)'), 'failed:1')

    def test_sigkill_not_resumable(self):
        self.assertEqual(self.run_command('import os,signal; os.kill(os.getpid(),signal.SIGKILL)'), 'failed:-9')

    def test_orderly_deadline(self):
        code = 'import signal,time,sys; signal.signal(signal.SIGINT,lambda *a:sys.exit(1)); time.sleep(20)'
        self.assertEqual(self.run_command(code, 0.5), 'running')

    def test_unresponsive_deadline(self):
        code = 'import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); time.sleep(20)'
        self.assertEqual(self.run_command(code, 0.5, 0.1), 'failed:shutdown-timeout')


class StaticPolicy(unittest.TestCase):
    def test_actions(self):
        allowed = {
            'checkout': '3d3c42e5aac5ba805825da76410c181273ba90b1',
            'upload-artifact': 'cf430e030ddbb5b0abf93d22962f4752f3646cd9',
            'download-artifact': '9000827ccba6bdab643e8b6fd33ac0654aef8333',
        }
        for p in (ROOT / '.github/workflows').glob('*.yml'):
            for use in re.findall(r'^\s*(?:-\s*)?uses:\s*(\S+)', p.read_text(), re.M):
                if use == './.github/workflows/chromium-stage.yml':
                    continue
                m = re.fullmatch(r'actions/([a-z-]+)@([0-9a-f]{40})', use)
                self.assertIsNotNone(m, use)
                self.assertEqual(allowed[m[1]], m[2])

    def test_depot_pin(self):
        self.assertRegex((ROOT / 'config/depot_tools_revision.txt').read_text().strip(), r'^[a-f0-9]{40}$')

    def test_shell_syntax(self):
        for p in (ROOT / '.github/scripts').glob('*.sh'):
            subprocess.run(['bash', '-n', str(p)], check=True)

    def test_no_unsafe_triggers_or_secrets(self):
        for p in (ROOT / '.github/workflows').glob('*.yml'):
            for bad in ['pull_request_target:', 'pull_request:', 'secrets.', 'secrets: inherit',
                        'contents: write', 'actions: write', 'id-token: write']:
                self.assertNotIn(bad, p.read_text())


if __name__ == '__main__':
    unittest.main()
