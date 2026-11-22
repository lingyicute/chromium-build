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
                    result = self.run_guard(f'bash "$1/{script}"', tmp,
                                            RUNNER_ENVIRONMENT='self-hosted', RUNNER_TEMP=tmp)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('GitHub-hosted runner required', result.stderr)

    def test_relay_mount_accepts_separate_and_single_disk_layouts(self):
        # Hosted runners have either a separate /mnt disk or one OS disk that also holds /mnt.
        layouts = (('stat() { if [[ "${*: -1}" == / ]]; then echo 1; else echo 2; fi; }', '2 0'),
                   ('stat() { echo 123; }', '123 1'))
        for stub, expected in layouts:
            with self.subTest(expected=expected):
                result = self.run_guard(f'source "$1/runner.sh"; {stub}; require_relay_mount; '
                                        'printf "%s %s" "$relay_device" "$relay_shares_root"', '.')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
        result = self.run_guard('source "$1/runner.sh"; stat() { return 1; }; require_relay_mount', '.')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot inspect root filesystem', result.stderr)

    def test_mount_targets_reject_nested_and_same_device_bind_mounts(self):
        command = 'source "$1/runner.sh"; check_mount_targets "$2"'
        baseline = '1 0 8:1 / / rw - ext4 /dev/sda rw\n2 1 8:2 / /mnt rw - ext4 /dev/sdb rw\n'
        directory = '/mnt/test relay'
        targets = (('/mnt/test\\040relayed', True), ('/mnt/test\\040relay', False),
                   ('/mnt/test\\040relay/source\\011files', False),
                   ('/mnt/test\\040relay/0', False), ('/mnt/test\\134040relay', True))
        for target, allowed in targets:
            with self.subTest(target=target):
                mountinfo = baseline + f'3 2 8:2 /bind-source {target} rw - ext4 /dev/sdb rw\n'
                result = subprocess.run(['bash', '-euc', command, '_', str(SCRIPTS), directory],
                                        input=mountinfo, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode == 0, allowed, result.stderr)
                if not allowed:
                    self.assertIn('Refusing mount at or below /mnt/test relay', result.stderr)

        digit_mount = baseline + '3 2 8:2 /bind-source /mnt/dir\\0401/sub rw - ext4 /dev/sdb rw\n'
        result = subprocess.run(['bash', '-euc', command, '_', str(SCRIPTS), '/mnt/dir 1/'],
                                input=digit_mount, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Refusing mount at or below /mnt/dir 1', result.stderr)

        for bad in (baseline + '3 2 8:2 /s /mnt/bad\\c rw - ext4 /dev/sdb rw\n',
                    baseline + '3 2 8:2 /s /mnt/no-separator rw\n',
                    baseline + '3 2 8:2 /s /mnt/test\\040relay rw - ext4 /dev/sdb rw'):
            result = subprocess.run(['bash', '-euc', command, '_', str(SCRIPTS), directory],
                                    input=bad, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)

        with tempfile.TemporaryDirectory() as tmp:
            real_parent = Path(tmp) / 'real'
            real_parent.mkdir()
            link_parent = Path(tmp) / 'alias'
            link_parent.symlink_to(real_parent, target_is_directory=True)
            resolved = subprocess.run(
                ['bash', '-euc', 'source "$1/runner.sh"; resolve_mount_parent out "$2"; printf "%s" "$out"',
                 '_', str(SCRIPTS), str(link_parent / 'android/sub')],
                capture_output=True, text=True, check=True, timeout=5,
            )
            self.assertEqual(resolved.stdout, str(real_parent.resolve() / 'android/sub'))

    def test_trailing_newlines_do_not_hide_directory_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = 'source "$1/runner.sh"; require_empty_directory "$2"'
            self.assertEqual(self.run_guard(command, tmp).returncode, 0)
            (Path(tmp) / '\n').touch()
            result = self.run_guard(command, tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Expected an empty directory', result.stderr)

    def test_chmod_failure_is_not_misread_as_find_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'tree'
            (target / 'nested').mkdir(parents=True)
            target.chmod(0o000)
            cmd = 'source "$1/common.sh"; make_dirs_removable "$2"'
            result = self.run_guard(cmd, target, RUNNER_TEMP=tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.stat().st_mode & 0o700, 0o700)
            target.chmod(0o555)
            stub = Path(tmp) / 'chmod'
            stub.write_text('#!/bin/sh\nexit 1\n')
            stub.chmod(0o755)
            result = self.run_guard(
                'source "$1/common.sh"; export PATH="${2%/*}:$PATH"; '
                'make_dirs_removable "$2"', target, RUNNER_TEMP=tmp,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Could not grant owner access', result.stderr)

            newline_dir = Path(tmp) / 'nl' / '\n'
            newline_dir.mkdir(parents=True)
            newline_dir.chmod(0o555)
            result = subprocess.run(
                ['bash', '-euc', 'source "$1/common.sh"; export PATH="$2:$PATH"; make_dirs_removable "$3"',
                 '_', str(SCRIPTS), str(tmp), '\n'],
                cwd=newline_dir.parent, env={**os.environ, 'RUNNER_TEMP': tmp},
                capture_output=True, text=True, timeout=5,
            )
            newline_dir.chmod(0o755)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Could not grant owner access', result.stderr)

            result = self.run_guard('source "$1/common.sh"; make_dirs_removable', target, RUNNER_TEMP=tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('requires a directory path', result.stderr)

    def test_normalize_mount_path_rejects_non_canonical_segments(self):
        for bad in ('/mnt//relay', '/mnt/relay/.', '/mnt/relay/..', '/mnt/./relay', '/mnt/../relay', '/.'):
            with self.subTest(path=bad):
                result = self.run_guard('source "$1/runner.sh"; normalize_mount_path out "$2"', bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('must be canonical', result.stderr)
        for good in ('/', '/usr/local/.ghcup', '/mnt/dir 1/'):
            with self.subTest(path=good):
                result = self.run_guard('source "$1/runner.sh"; normalize_mount_path out "$2"', good)
                self.assertEqual(result.returncode, 0, result.stderr)
