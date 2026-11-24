"""Exercise runner guards without sudo or touching fixed host paths."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / '.github/scripts'


class RunnerGuards(unittest.TestCase):
    def run_guard(self, script, path, **env):
        return subprocess.run(
            ['bash', '-euc', script, '_', str(SCRIPTS), str(path)],
            env={**os.environ, **env}, capture_output=True, text=True, timeout=5,
        )

    def test_destructive_scripts_refuse_non_hosted_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            for script in ('host.sh', 'pack.sh', 'restore.sh'):
                with self.subTest(script=script):
                    r = self.run_guard(f'bash "$1/{script}"', tmp,
                                       RUNNER_ENVIRONMENT='self-hosted', RUNNER_TEMP=tmp)
                    self.assertNotEqual(r.returncode, 0)
                    self.assertIn('GitHub-hosted runner required', r.stderr)

    def test_relay_mount_accepts_separate_and_single_disk_layouts(self):
        layouts = (
            ('stat() { [[ "${*: -1}" == / ]] && echo 1 || echo 2; }', '2 0'),
            ('stat() { echo 123; }', '123 1'),
        )
        for stub, expected in layouts:
            r = self.run_guard(f'source "$1/runner.sh"; {stub}; require_relay_mount; '
                               'printf "%s %s" "$relay_device" "$relay_shares_root"', '.')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, expected)
        r = self.run_guard('source "$1/runner.sh"; stat() { return 1; }; require_relay_mount', '.')
        self.assertNotEqual(r.returncode, 0)

    def test_mount_targets_reject_nested_bind_and_symlinked_mounts(self):
        cmd = 'source "$1/runner.sh"; check_mount_targets "$2"'
        base = '1 0 8:1 / / rw - ext4 /dev/sda rw\n2 1 8:2 / /mnt rw - ext4 /dev/sdb rw\n'
        cases = (
            ('/mnt/test\\040relayed', True),
            ('/mnt/test\\040relay', False),
            ('/mnt/test\\040relay/source\\011files', False),
            ('/mnt/test\\040relay/0', False),
            ('/mnt/test\\134040relay', True),
        )
        for target, allowed in cases:
            with self.subTest(target=target):
                mountinfo = base + f'3 2 8:2 /bind {target} rw - ext4 /dev/sdb rw\n'
                r = subprocess.run(['bash', '-euc', cmd, '_', str(SCRIPTS), '/mnt/test relay'],
                                   input=mountinfo, capture_output=True, text=True, timeout=5)
                self.assertEqual(r.returncode == 0, allowed, r.stderr)
                if not allowed:
                    self.assertIn('Refusing mount at or below /mnt/test relay', r.stderr)

        # Escaped space followed by digit must not be eaten as octal escape.
        r = subprocess.run(
            ['bash', '-euc', cmd, '_', str(SCRIPTS), '/mnt/dir 1/'],
            input=base + '3 2 8:2 /bind /mnt/dir\\0401/sub rw - ext4 /dev/sdb rw\n',
            capture_output=True, text=True, timeout=5,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('Refusing mount at or below /mnt/dir 1', r.stderr)

        # Malformed or missing newline lines rejected.
        for bad in (
            base + '3 2 8:2 /s /mnt/bad\\c rw - ext4 /dev/sdb rw\n',
            base + '3 2 8:2 /s /mnt/no-separator rw\n',
            base + '3 2 8:2 /s /mnt/test\\040relay rw - ext4 /dev/sdb rw',
        ):
            r = subprocess.run(['bash', '-euc', cmd, '_', str(SCRIPTS), '/mnt/test relay'],
                               input=bad, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(r.returncode, 0)

        # Symlinked ancestor directory is resolved before matching mount targets.
        with tempfile.TemporaryDirectory() as tmp:
            real_parent = Path(tmp) / 'real'
            real_parent.mkdir()
            alias = Path(tmp) / 'alias'
            alias.symlink_to(real_parent, target_is_directory=True)
            mount = base + f'3 2 8:2 /s {real_parent.resolve()}/android rw - ext4 /dev/sdb rw\n'
            r = subprocess.run(['bash', '-euc', cmd, '_', str(SCRIPTS), str(alias / 'android')],
                               input=mount, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('Refusing mount at or below', r.stderr)

        # Non-canonical target paths rejected; valid ones accepted.
        for bad_path in ('/mnt//relay', '/mnt/relay/.', '/mnt/relay/..', '/mnt/./relay', '/mnt/../relay', '/.'):
            r = subprocess.run(['bash', '-euc', cmd, '_', str(SCRIPTS), bad_path],
                               input=base, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('must be canonical', r.stderr)

    def test_empty_directory_and_make_dirs_removable(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_guard('source "$1/runner.sh"; require_empty_directory "$2"', tmp)
            self.assertEqual(r.returncode, 0)
            (Path(tmp) / '\n').touch()
            r = self.run_guard('source "$1/runner.sh"; require_empty_directory "$2"', tmp)
            self.assertNotEqual(r.returncode, 0)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'tree'
            (target / 'nested').mkdir(parents=True)
            target.chmod(0o000)
            r = self.run_guard('source "$1/common.sh"; make_dirs_removable "$2"', target, RUNNER_TEMP=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(target.stat().st_mode & 0o700, 0o700)

            target.chmod(0o555)
            stub = Path(tmp) / 'chmod'
            stub.write_text('#!/bin/sh\nexit 1\n')
            stub.chmod(0o755)
            r = self.run_guard(
                'source "$1/common.sh"; export PATH="${2%/*}:$PATH"; make_dirs_removable "$2"',
                target, RUNNER_TEMP=tmp,
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('Could not grant owner access', r.stderr)
            target.chmod(0o755)

            r = self.run_guard('source "$1/common.sh"; make_dirs_removable', target, RUNNER_TEMP=tmp)
            self.assertNotEqual(r.returncode, 0)
