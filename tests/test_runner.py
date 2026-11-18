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

    def test_relay_mount_rejects_os_disk_before_writes(self):
        result = self.run_guard('source "$1/runner.sh"; stat() { echo 123; }; require_relay_mount', '.')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('separate /mnt filesystem', result.stderr)

    def test_mount_targets_reject_nested_and_same_device_bind_mounts(self):
        command = 'source "$1/runner.sh"; check_mount_targets "$2"'
        baseline = '1 0 8:1 / / rw - ext4 /dev/sda rw\n2 1 8:2 / /mnt rw - ext4 /dev/sdb rw\n'
        directory = '/mnt/test relay'
        targets = (('/mnt/test\\040relayed', True), ('/mnt/test\\040relay', False),
                   ('/mnt/test\\040relay/source\\011files', False))
        for target, allowed in targets:
            with self.subTest(target=target):
                mountinfo = baseline + f'3 2 8:2 /bind-source {target} rw - ext4 /dev/sdb rw\n'
                result = subprocess.run(['bash', '-euc', command, '_', str(SCRIPTS), directory],
                                        input=mountinfo, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode == 0, allowed, result.stderr)
                if not allowed:
                    self.assertIn('Refusing mount at or below /mnt/test relay', result.stderr)

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
