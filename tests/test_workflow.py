"""Offline smoke tests. Python stdlib only; does NOT compile Chromium."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.error

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / '.github/scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolve = load('resolve')
checkpoint = load('checkpoint')
build = load('build')


class _FakeResponse:
    def __init__(self, body, url=resolve.URL):
        self._body = body
        self.url = url

    def read(self, size=-1):
        return self._body if size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeOpener:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Resolution(unittest.TestCase):
    def sample(self):
        return [dict(channel='Stable', platform='Android', version='155.0.8059.79',
                     hashes={'chromium': 'a' * 40})]

    def test_valid(self):
        self.assertEqual(resolve.validate(self.sample()), ('155.0.8059.79', 'a' * 40))

    def test_bad_input(self):
        for field, value in [('channel', 'Beta'), ('platform', 'Windows'),
                             ('version', '155.0.1.2; echo injection'),
                             ('version', 155), ('hashes', None), ('hashes', [])]:
            data = self.sample()
            data[0][field] = value
            with self.assertRaises(ValueError):
                resolve.validate(data)
        data = self.sample()
        data[0]['hashes']['chromium'] = 'main'
        with self.assertRaises(ValueError):
            resolve.validate(data)
        data = self.sample()
        data[0]['hashes']['chromium'] = 12345
        with self.assertRaises(ValueError):
            resolve.validate(data)
        for bad in ([], [None], ['string'], {}):
            with self.assertRaises(ValueError):
                resolve.validate(bad)

    def test_fetch_retries_transient_5xx(self):
        err503 = urllib.error.HTTPError(resolve.URL, 503, 'Service Unavailable', {}, None)
        opener = _FakeOpener([err503, urllib.error.URLError('temp'), _FakeResponse(b'[]')])
        raw = resolve.fetch_release_json(attempts=3, retry_delay=0.0, opener=opener)
        self.assertEqual(raw, b'[]')
        self.assertEqual(opener.calls, 3)

    def test_fetch_does_not_retry_4xx_or_redirect(self):
        err404 = urllib.error.HTTPError(resolve.URL, 404, 'Not Found', {}, None)
        opener = _FakeOpener([err404, _FakeResponse(b'[]')])
        with self.assertRaises(urllib.error.HTTPError):
            resolve.fetch_release_json(attempts=3, retry_delay=0.0, opener=opener)
        self.assertEqual(opener.calls, 1)

        redir_opener = _FakeOpener([_FakeResponse(b'[]', url='https://evil.example/api')])
        with self.assertRaises(ValueError):
            resolve.fetch_release_json(attempts=3, retry_delay=0.0, opener=redir_opener)
        self.assertEqual(redir_opener.calls, 1)

    def test_no_redirect_handler_blocks_before_follow(self):
        handler = resolve._NoRedirectHandler()
        with self.assertRaises(ValueError):
            handler.redirect_request(None, None, 302, 'Found', {}, 'https://evil.example/')

    def test_fetch_rejects_oversized_response(self):
        opener = _FakeOpener([_FakeResponse(b'x' * (resolve.MAX_BYTES + 1))])
        with self.assertRaises(ValueError):
            resolve.fetch_release_json(attempts=1, retry_delay=0.0, opener=opener)


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

    def test_empty_part_rejected(self):
        (self.root / 'tree.part-00002').write_bytes(b'')
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

    def test_pack_stream_and_restore_round_trip(self):
        payload = b'abcdefghijk'  # 11 bytes -> 3 chunks with chunk_size=4
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            sha, count, total = checkpoint.pack_stream(dest, io.BytesIO(payload), chunk_size=4)
            self.assertEqual(sha, hashlib.sha256(payload).hexdigest())
            self.assertEqual(count, 3)
            self.assertEqual(total, len(payload))
            self.assertEqual([p.name for p in checkpoint.parts(dest)],
                             ['tree.part-00000', 'tree.part-00001', 'tree.part-00002'])
            self.assertEqual(checkpoint.digest(checkpoint.parts(dest)), sha)
            out = io.BytesIO()
            checkpoint.restore_stream(dest, sha, out)
            self.assertEqual(out.getvalue(), payload)
            self.assertEqual(list(dest.iterdir()), [])

    def test_pack_stream_exact_boundary_and_empty_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            sha, count, total = checkpoint.pack_stream(dest, io.BytesIO(b'12345678'), chunk_size=4)
            self.assertEqual((count, total), (2, 8))
            self.assertEqual(sha, hashlib.sha256(b'12345678').hexdigest())
            with self.assertRaises(ValueError):
                checkpoint.pack_stream(dest, io.BytesIO(b'more'), chunk_size=4)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                checkpoint.pack_stream(Path(tmp), io.BytesIO(b''), chunk_size=4)

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
        # Tests real GNU tar without compression, including nested read-only dirs.
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
            ro_parent = src / 'ro_parent'
            ro_child = ro_parent / 'ro_child'
            ro_child.mkdir(parents=True)
            ro_file = ro_child / 'readonly.txt'
            ro_file.write_text('immutable')
            os.utime(ro_file, ns=(ns, ns))
            ro_file.chmod(0o444)
            os.utime(ro_child, ns=(ns, ns))
            ro_child.chmod(0o555)
            os.utime(ro_parent, ns=(ns, ns))
            ro_parent.chmod(0o555)
            archive = root / 'test.tar'
            # Same archive form as pack.sh: chmod u+w on read-only dirs, then tar entries.
            subprocess.run(
                ['bash', '-c',
                 'find "$1" -mindepth 1 -type d ! -perm -u+w -exec chmod u+w {} + && '
                 'cd "$1" && shopt -s dotglob nullglob && entries=(*) && '
                 'tar --format=posix --pax-option=delete=atime,delete=ctime '
                 '--remove-files -cf "$2" -- "${entries[@]}"',
                 '_', str(src), str(archive)],
                check=True)
            self.assertTrue(src.exists())
            self.assertEqual(list(src.iterdir()), [])
            subprocess.run(['tar', '--no-same-owner', '-xpf', str(archive), '-C', str(src)], check=True)
            self.assertEqual((src / '.ninja_log').stat().st_mtime_ns, ns)
            self.assertEqual((src / '.ninja_log').read_text(), 'state')
            self.assertTrue((src / 'link').is_symlink())
            self.assertTrue(os.access(src / 'tool', os.X_OK))
            restored_ro = src / 'ro_parent/ro_child/readonly.txt'
            self.assertEqual(restored_ro.stat().st_mtime_ns, ns)
            self.assertEqual(restored_ro.stat().st_mode & 0o777, 0o444)
            self.assertEqual(restored_ro.read_text(), 'immutable')


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
        # ninja <= 1.12 exits 2 after "interrupted by user", ninja >= 1.13 exits 130.
        for code in (2, 130):
            script = f'import signal,time,sys; signal.signal(signal.SIGINT,lambda *a:sys.exit({code})); time.sleep(20)'
            self.assertEqual(self.run_command(script, 0.5), 'running')

    def test_unresponsive_deadline(self):
        code = 'import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); time.sleep(20)'
        self.assertEqual(self.run_command(code, 0.5, 0.1), 'failed:shutdown-timeout')

    def test_kill_session_reaps_detached_pgrp_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / 'child.pid'
            parent_code = (
                'import os, subprocess, sys, time\n'
                'c = subprocess.Popen([sys.executable, "-c", "import os,time; os.setpgrp(); time.sleep(30)"])\n'
                f'open({str(pidfile)!r}, "w").write(str(c.pid))\n'
                'time.sleep(0.2)\n'
            )
            self.assertEqual(self.run_command(parent_code, seconds=5, grace=1), 'completed')
            child_pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_compute_jobs(self):
        self.assertEqual(build.compute_jobs(cpu_count=4, mem_kib=16 * 1024 * 1024), 4)
        self.assertEqual(build.compute_jobs(cpu_count=2, mem_kib=7 * 1024 * 1024), 2)
        self.assertEqual(build.compute_jobs(cpu_count=8, mem_kib=16 * 1024 * 1024), 4)
        self.assertEqual(build.compute_jobs(cpu_count=4, mem_kib=2 * 1024 * 1024), 1)

    def test_compute_budget(self):
        self.assertEqual(build.compute_budget(1000, 1000 + 30 * 60, final_stage=False), 210 * 60)
        self.assertEqual(build.compute_budget(1000, 1000 + 80 * 60, final_stage=False), 185 * 60)
        self.assertEqual(build.compute_budget(1000, 1000 + 30 * 60, final_stage=True), 270 * 60)
        self.assertEqual(build.compute_budget(1000, 1000 + 80 * 60, final_stage=True), 250 * 60)


class Retry(unittest.TestCase):
    def run_retry(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            return subprocess.run(
                ['bash', '-c', 'source "$1/common.sh"; ' + body, '_', str(ROOT / '.github/scripts')],
                env=dict(os.environ, RUNNER_TEMP=tmp, CR_RETRY_DELAY='0'),
                capture_output=True, text=True, cwd=tmp)

    def test_retries_until_success(self):
        p = self.run_retry('n=0; flaky() { n=$((n + 1)); (( n >= 3 )); }; retry flaky; echo "attempts=$n"')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), 'attempts=3')
        self.assertEqual(p.stderr.count('::warning::'), 2)

    def test_gives_up_after_three_attempts(self):
        p = self.run_retry('broken() { echo try; return 1; }; retry broken; echo unreachable')
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.split(), ['try', 'try', 'try'])
        self.assertIn('::error::Failed after 3 attempts: broken', p.stderr)

    def test_fetch_network_steps_use_retry(self):
        fetch = (ROOT / '.github/scripts/fetch.sh').read_text()
        for step in ('git -C "$CR_ROOT/src" -c fetch.fsckObjects=true fetch',
                     'git -C "$CR_ROOT/depot_tools" -c fetch.fsckObjects=true fetch',
                     'gclient sync', 'gclient runhooks'):
            self.assertIn('\nretry ' + step, fetch)


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

    def test_final_stage_and_budget_invariants(self):
        stable = (ROOT / '.github/workflows/chromium-stable.yml').read_text()
        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        self.assertEqual(len(re.findall(r'^\s+final:\s*true\s*$', stable, re.M)), 1)
        self.assertRegex(stable, r'part-08:[\s\S]*?final:\s*true[\s\S]*?result:')
        self.assertIn("if: steps.build.outputs.status == 'running' && !inputs.final", stage)
        # Normal stage worst-case: 265m budget + 3m grace + 45m pack + 35m upload = 348m < 350m.
        self.assertLessEqual(build.NORMAL_JOB_DEADLINE_SECONDS // 60 + 3 + 45 + 35, 350)
        # Final stage worst-case: 330m budget + 3m grace + 10m release/upload < 350m.
        self.assertLessEqual(build.FINAL_JOB_DEADLINE_SECONDS // 60 + 3 + 10, 350)

    def test_prepare_compiles_as_stage_00(self):
        stable = (ROOT / '.github/workflows/chromium-stable.yml').read_text()
        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        prepare = re.search(r'^  prepare:\n((?:    .*\n)+)', stable, re.M)[1]
        self.assertIn('uses: ./.github/workflows/chromium-stage.yml', prepare)
        self.assertIn("stage: '00'", prepare)
        self.assertNotIn('artifact-id', prepare)
        self.assertIn("if: ${{ needs.prepare.outputs.status == 'running' }}", stable)
        for step, condition in [('Resolve stable, fetch locked source', "== '00'"),
                                ('Save source resolution', "== '00'"),
                                ('Download exact checkpoint ID', "!= '00'"),
                                ('Verify producer hash', "!= '00'")]:
            self.assertRegex(stage, re.escape(step) + r'.*\n\s+if: inputs\.stage ' + re.escape(condition) + r'\n')

    def test_stage_input_validation(self):
        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        script = textwrap.dedent(re.search(r"python3 - <<'PY'\n(.*?)\n *PY\n", stage, re.S)[1])

        def valid(**overrides):
            env = dict(os.environ, STAGE='03', ARTIFACT_ID='123', EXPECTED_SHA256='a' * 64, FINAL_STAGE='false')
            env.update(overrides)
            return subprocess.run([sys.executable, '-c', script], env=env, capture_output=True).returncode == 0

        self.assertTrue(valid())
        self.assertTrue(valid(STAGE='08', FINAL_STAGE='true'))
        self.assertTrue(valid(STAGE='00', ARTIFACT_ID='', EXPECTED_SHA256=''))
        for bad in [dict(STAGE='09'), dict(STAGE='1'), dict(ARTIFACT_ID=''), dict(ARTIFACT_ID='0'),
                    dict(EXPECTED_SHA256='A' * 64), dict(FINAL_STAGE='yes'), dict(STAGE='00'),
                    dict(STAGE='00', ARTIFACT_ID='', EXPECTED_SHA256='', FINAL_STAGE='true')]:
            self.assertFalse(valid(**bad), bad)


if __name__ == '__main__':
    unittest.main()
