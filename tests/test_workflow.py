"""Offline smoke tests. Python stdlib only; does NOT compile Chromium."""
import hashlib
import importlib.util
import io
import json
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
    PARTS = [b'firsts', b'econd']

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.expected, _, _ = checkpoint.pack_stream(self.root, io.BytesIO(b''.join(self.PARTS)), chunk_size=6)

    def tearDown(self):
        self.temp.cleanup()

    def names(self, directory=None):
        return sorted(p.name for p in (directory or self.root).iterdir())

    def test_hash(self):
        manifest = ''.join(f'{hashlib.sha256(data).hexdigest()} {len(data)} tree.part-{n:05d}\n'
                           for n, data in enumerate(self.PARTS)).encode()
        self.assertEqual((self.root / 'tree.manifest').read_bytes(), manifest)
        self.assertEqual(self.expected, hashlib.sha256(manifest).hexdigest())
        self.assertEqual(checkpoint.digest(self.root), self.expected)
        self.assertEqual([p.read_bytes() for p in checkpoint.parts(self.root)], self.PARTS)

    def test_tampering(self):
        (self.root / 'tree.part-00001').write_bytes(b'bad')
        with self.assertRaises(ValueError):
            checkpoint.digest(self.root)
        with self.assertRaises(ValueError):
            checkpoint.restore_stream(self.root, self.expected, io.BytesIO())

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
            self.assertEqual(count, 3)
            self.assertEqual(total, len(payload))
            self.assertEqual(self.names(dest),
                             ['tree.manifest', 'tree.part-00000', 'tree.part-00001', 'tree.part-00002'])
            self.assertEqual([p.read_bytes() for p in checkpoint.parts(dest)], [b'abcd', b'efgh', b'ijk'])
            self.assertEqual(checkpoint.digest(dest), sha)
            out = io.BytesIO()
            self.assertEqual(checkpoint.restore_stream(dest, sha, out), (3, len(payload)))
            self.assertEqual(out.getvalue(), payload)
            self.assertEqual(list(dest.iterdir()), [])

    def test_pack_stream_exact_boundary_and_empty_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            sha, count, total = checkpoint.pack_stream(dest, io.BytesIO(b'12345678'), chunk_size=4)
            self.assertEqual((count, total), (2, 8))
            self.assertEqual([p.read_bytes() for p in checkpoint.parts(dest)], [b'1234', b'5678'])
            self.assertEqual(checkpoint.digest(dest), sha)
            with self.assertRaises(ValueError):
                checkpoint.pack_stream(dest, io.BytesIO(b'more'), chunk_size=4)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                checkpoint.pack_stream(Path(tmp), io.BytesIO(b''), chunk_size=4)
            self.assertEqual(list(Path(tmp).iterdir()), [])

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
        self.assertEqual(len(self.names()), 3)

    def test_missing_manifest(self):
        (self.root / 'tree.manifest').unlink()
        with self.assertRaises(ValueError):
            checkpoint.parts(self.root)

    def test_tampered_part_is_never_streamed(self):
        # Same size, different bytes: only the per-part SHA-256 can catch this.
        (self.root / 'tree.part-00001').write_bytes(b'ECOND')
        p = self.consume(self.expected)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b'firsts')
        self.assertEqual(self.names(), ['tree.manifest', 'tree.part-00001'])

    def test_truncated_part_rejected_before_streaming(self):
        (self.root / 'tree.part-00001').write_bytes(b'econ')
        p = self.consume(self.expected)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b'')
        self.assertEqual(len(self.names()), 3)

    def test_swapped_parts_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            sha, _, _ = checkpoint.pack_stream(dest, io.BytesIO(b'aaaabbbb'), chunk_size=4)
            (dest / 'tree.part-00000').write_bytes(b'bbbb')
            (dest / 'tree.part-00001').write_bytes(b'aaaa')
            out = io.BytesIO()
            with self.assertRaises(ValueError):
                checkpoint.restore_stream(dest, sha, out)
            self.assertEqual(out.getvalue(), b'')

    def test_part_hashed_ahead_is_checked_before_streaming(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            sha, _, _ = checkpoint.pack_stream(dest, io.BytesIO(b'aaaabbbbcccc'), chunk_size=4)
            (dest / 'tree.part-00002').write_bytes(b'CCCC')
            out = io.BytesIO()
            with self.assertRaises(ValueError):
                checkpoint.restore_stream(dest, sha, out)
            self.assertEqual(out.getvalue(), b'aaaabbbb')
            self.assertEqual(self.names(dest), ['tree.manifest', 'tree.part-00002'])

    def test_forged_manifest_rejected(self):
        # Re-describing a tampered part changes the manifest digest the next job expects.
        forged = [self.PARTS[0], b'ECOND']
        (self.root / 'tree.part-00001').write_bytes(forged[1])
        (self.root / 'tree.manifest').write_text(''.join(
            f'{hashlib.sha256(data).hexdigest()} {len(data)} tree.part-{n:05d}\n' for n, data in enumerate(forged)))
        self.assertNotEqual(checkpoint.digest(self.root), self.expected)
        p = self.consume(self.expected)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b'')
        self.assertEqual(len(self.names()), 3)

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
        self.assertEqual(build.compute_budget(1000, 1000 + 20 * 60, final_stage=False), 245 * 60)
        self.assertEqual(build.compute_budget(1000, 1000 + 80 * 60, final_stage=False), 185 * 60)
        self.assertEqual(build.compute_budget(1000, 1000 + 20 * 60, final_stage=True), 310 * 60)
        self.assertEqual(build.compute_budget(1000, 1000 + 80 * 60, final_stage=True), 250 * 60)
        self.assertLess(build.compute_budget(1000, 1000 + 262 * 60, final_stage=False), build.MIN_BUDGET_SECONDS)


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

    def test_checkpoint_upload_includes_manifest(self):
        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        paths = re.search(r'id: upload\n[\s\S]*?path: \|\n((?:\s+/\S+\n)+)', stage)[1].split()
        self.assertEqual(paths, [f'/mnt/chromium-transfer/checkpoint/{name}'
                                 for name in (checkpoint.MANIFEST, 'tree.part-*')])

    def test_tolerated_artifact_transfers_have_exactly_one_retry(self):
        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        steps = re.split(r'\n      - ', stage.split('\n    steps:\n', 1)[1])

        def inputs(step):
            lines = [line.strip() for line in step.split('        with:\n', 1)[1].splitlines()]
            return [line for line in lines if line and not line.startswith('#')]

        tolerated = [step for step in steps if 'continue-on-error: true' in step]
        # Uploads that nothing consumes may fail without failing the stage; every other
        # tolerated step is a transfer that a later step repeats once.
        diagnostic = [step for step in tolerated if step.startswith('name: Save ')]
        self.assertEqual([step.split('\n', 1)[0] for step in diagnostic],
                         ['name: Save source resolution and GN configuration', 'name: Save stage logs'])
        retried = [step for step in tolerated if step not in diagnostic]
        self.assertEqual(len(retried), 3)
        for step in retried:
            step_id = re.search(r'^        id: (\S+)$', step, re.M)[1]
            action = re.search(r'uses: (\S+)', step)[1]
            retries = [other for other in steps
                       if f"steps.{step_id}.outcome != 'success'" in other and f'uses: {action}' in other]
            self.assertEqual(len(retries), 1, step_id)
            # A retry that fails must fail the job, and must repeat the same transfer; only
            # a re-upload is renamed so it cannot collide with a partial artifact.
            self.assertNotIn('continue-on-error', retries[0])
            self.assertEqual(inputs(retries[0]),
                             [line + '-retry' if line.startswith('name: ') else line for line in inputs(step)])
        self.assertIn('artifact-id: ${{ steps.upload-retry.outputs.artifact-id || steps.upload.outputs.artifact-id }}',
                      stage)

    def test_unverified_apk_is_kept_only_on_failure_under_its_own_name(self):
        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        step = re.search(r'      - name: Keep the unverified APK[\s\S]*?(?=\n      - name: )', stage)[0]
        self.assertIn("\n        if: failure() && steps.build.outputs.status == 'completed'\n", step)
        self.assertIn('\n          name: unverified-apk-', step)
        self.assertNotIn('continue-on-error', step)
        # Same file release.sh verifies ($CR_ROOT is fixed in common.sh).
        self.assertIn('\n          path: /mnt/chromium-relay/src/out/Release/apks/ChromePublic.apk\n', step)
        self.assertIn('export CR_ROOT=/mnt/chromium-relay\n', (ROOT / '.github/scripts/common.sh').read_text())
        self.assertIn('apk="$CR_ROOT/src/out/Release/apks/ChromePublic.apk"\n',
                      (ROOT / '.github/scripts/release.sh').read_text())

    def test_retried_download_still_leaves_compile_budget(self):
        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        steps = re.split(r'\n      - ', stage.split('\n    steps:\n', 1)[1])
        minutes = [int(re.search(r'timeout-minutes: (\d+)', step)[1]) for step in steps
                   if "if: inputs.stage != '00'" in step and 'timeout-minutes' in step]
        # Download, its one retry, and restore: even the worst case must leave time to compile.
        self.assertEqual(minutes, [70, 70, 40])
        self.assertLess(sum(minutes) * 60 + build.MIN_BUDGET_SECONDS, build.NORMAL_JOB_DEADLINE_SECONDS)

    def test_final_stage_and_budget_invariants(self):
        stable = (ROOT / '.github/workflows/chromium-stable.yml').read_text()
        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        self.assertEqual(len(re.findall(r'^\s+final:\s*true\s*$', stable, re.M)), 1)
        self.assertRegex(stable, r'part-08:[\s\S]*?final:\s*true[\s\S]*?result:')
        self.assertIn("if: steps.build.outputs.status == 'running' && !inputs.final", stage)
        # Normal stage worst-case: 265m budget + 3m grace + 45m pack + 35m upload + 1m logs = 349m < 350m.
        logs = int(re.search(r'name: Save stage logs\n(?:.*\n)*?\s+timeout-minutes: (\d+)', stage)[1])
        self.assertLessEqual(build.NORMAL_JOB_DEADLINE_SECONDS // 60 + 3 + 45 + 35 + logs, 350)
        # Final stage worst-case: 330m budget + 3m grace + 10m release/upload < 350m.
        self.assertLessEqual(build.FINAL_JOB_DEADLINE_SECONDS // 60 + 3 + 10, 350)
        # The compile step backstop must never cut off an orderly final-stage stop.
        step_timeout = int(re.search(r'id: build\n(?:\s+#.*\n)*\s+timeout-minutes: (\d+)', stage)[1])
        self.assertGreaterEqual(step_timeout, build.FINAL_JOB_DEADLINE_SECONDS // 60 + 3)
        self.assertLessEqual(step_timeout, 350)

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


class ResultVerdict(unittest.TestCase):
    ORDER = ['prepare'] + [f'part-{i:02d}' for i in range(1, 9)]

    def verdict(self, **jobs):
        stable = (ROOT / '.github/workflows/chromium-stable.yml').read_text()
        script = textwrap.dedent(re.search(r"python3 - <<'PY'\n(.*?)\n *PY\n", stable, re.S)[1])
        needs = {name: dict(result='skipped', outputs={}) for name in self.ORDER}
        for name, (result, status) in jobs.items():
            needs[name.replace('_', '-')] = dict(result=result, outputs=dict(status=status) if status else {})
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / 'summary.md'
            p = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                               env=dict(os.environ, NEEDS_JSON=json.dumps(needs), GITHUB_STEP_SUMMARY=str(summary)))
            return p.returncode == 0, summary.read_text()

    def test_completed_stage_passes_and_later_stages_may_be_skipped(self):
        ok, summary = self.verdict(prepare=('success', 'running'), part_01=('success', 'completed'))
        self.assertTrue(ok, summary)
        self.assertIn('APK completed in `part-01`', summary)
        self.assertIn('| `part-02` | `skipped` | `-` |', summary)

    def test_exhausted_chain_fails(self):
        jobs = {name.replace('-', '_'): ('success', 'running') for name in self.ORDER}
        jobs['part_08'] = ('success', 'exhausted')
        ok, summary = self.verdict(**jobs)
        self.assertFalse(ok)
        self.assertIn('stage budget was exhausted', summary)

    def test_skipped_prepare_fails(self):
        ok, summary = self.verdict()
        self.assertFalse(ok)
        self.assertIn('default branch', summary)

    def test_failed_or_cancelled_stage_fails(self):
        for result in ('failure', 'cancelled'):
            ok, summary = self.verdict(prepare=('success', 'running'), part_01=(result, 'running'))
            self.assertFalse(ok)
            self.assertIn('failed or were cancelled: part-01.', summary)

    def test_completed_build_with_failed_release_fails(self):
        ok, summary = self.verdict(prepare=('success', 'running'), part_01=('failure', 'completed'))
        self.assertFalse(ok)
        self.assertNotIn('APK completed in', summary)
        self.assertIn('`part-01` built the APK', summary)
        self.assertIn('`unverified-apk-*`', summary)


if __name__ == '__main__':
    unittest.main()
