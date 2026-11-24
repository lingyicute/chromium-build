"""Offline tests for Chromium Android arm64 multi-stage build scripts and workflows."""
import hashlib
import http.client
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / '.github/scripts'
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


resolve = load('resolve')
checkpoint = load('checkpoint')
build = load('build')
source_state = load('source_state')
apk = load('apk')

ELF_AARCH64 = (
    b'\x7fELF\x02\x01\x01' + bytes(9)
    + b'\x03\x00\xb7\x00\x01\x00\x00\x00'
    + bytes(28) + b'\x40\x00' + bytes(10)
)
DEX_HDR = b'dex\n039\x00' + bytes(24) + b'\x70\x00\x00\x00' * 2 + b'\x78\x56\x34\x12' + bytes(68)


class _FakeResponse:
    def __init__(self, body, url=resolve.URL, status=200, headers=None):
        self._buf = io.BytesIO(body)
        self.url = url
        self.status = status
        self.headers = {} if headers is None else headers

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeOpener:
    def __init__(self, outcomes):
        self._outcomes = iter(outcomes)
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        item = next(self._outcomes)
        if isinstance(item, BaseException):
            raise item
        return item


def _http_error(code, msg='err', headers=None):
    return urllib.error.HTTPError(resolve.URL, code, msg, headers or {}, io.BytesIO(b''))


class Resolution(unittest.TestCase):
    def test_validate_accepts_android_stable(self):
        sha = 'a' * 40
        out = resolve.validate([{'channel': 'Stable', 'platform': 'Android',
                                 'version': '135.0.7049.38', 'hashes': {'chromium': sha}}])
        self.assertEqual(out, ('135.0.7049.38', sha))

    def test_validate_rejects_bad_payloads(self):
        good = {'channel': 'Stable', 'platform': 'Android',
                'version': '135.0.7049.38', 'hashes': {'chromium': 'a' * 40}}
        bad_cases = [
            [], {}, ['bad'],
            [{**good, 'channel': 'Beta'}],
            [{**good, 'platform': 'Linux'}],
            [{**good, 'version': '135.0.0'}],
            [{**good, 'version': '01.0.0.1'}],
            [{**good, 'hashes': {'chromium': 'A' * 40}}],
            [{**good, 'hashes': {'chromium': 'short'}}],
            [{**good, 'hashes': None}],
        ]
        for payload in bad_cases:
            with self.assertRaises(ValueError):
                resolve.validate(payload)

    def test_validate_run_metadata(self):
        resolve.validate_run_metadata('owner/repo', 'a' * 40, '12345')
        for args in [
            ('bad', 'a' * 40, '1'), ('owner/..', 'a' * 40, '1'),
            ('owner/repo', 'short', '1'), ('owner/repo', 'a' * 40, '0'),
        ]:
            with self.assertRaises(ValueError):
                resolve.validate_run_metadata(*args)

    def test_fetch_retries_transient_errors_and_honors_retry_after(self):
        opener = _FakeOpener([
            _http_error(503),
            urllib.error.URLError('temp'),
            _FakeResponse(b'[{"ok":true}]'),
        ])
        self.assertEqual(resolve.fetch_release_json(opener=opener, retry_delay=0), b'[{"ok":true}]')
        self.assertEqual(opener.calls, 3)

        opener2 = _FakeOpener([_http_error(429), _http_error(408), _FakeResponse(b'[]')])
        self.assertEqual(resolve.fetch_release_json(opener=opener2, retry_delay=0), b'[]')

        self.assertEqual(resolve._retry_delay(1, 2.0, {'retry-after': '12'}), 12.0)
        self.assertEqual(resolve._retry_delay(1, 2.0, {'Retry-After': '999'}), resolve.MAX_RETRY_AFTER_SECONDS)
        self.assertEqual(resolve._retry_delay(2, 2.0, {}), 4.0)

    def test_fetch_rejects_client_errors_redirects_oversized_and_truncated(self):
        for err in (_http_error(404), _http_error(403)):
            opener = _FakeOpener([err])
            with self.assertRaises(urllib.error.HTTPError):
                resolve.fetch_release_json(opener=opener, retry_delay=0)
            self.assertEqual(opener.calls, 1)

        with self.assertRaisesRegex(ValueError, 'redirect'):
            resolve.fetch_release_json(opener=_FakeOpener([_FakeResponse(b'[]', url='https://other/')]), retry_delay=0)
        with self.assertRaisesRegex(ValueError, 'redirect'):
            resolve._NoRedirectHandler().redirect_request(None, None, 302, 'Found', {}, 'https://evil/')
        with self.assertRaisesRegex(ValueError, 'https'):
            resolve.fetch_release_json(url='http://example.com/x', retry_delay=0)
        with self.assertRaisesRegex(ValueError, 'Oversized'):
            resolve.fetch_release_json(opener=_FakeOpener([_FakeResponse(b'x' * (resolve.MAX_BYTES + 1))]), retry_delay=0)
        with self.assertRaisesRegex(ValueError, 'Oversized'):
            resolve.fetch_release_json(
                opener=_FakeOpener([_FakeResponse(b'x', headers={'Content-Length': str(resolve.MAX_BYTES + 1)})]),
                retry_delay=0,
            )
        with self.assertRaises(http.client.IncompleteRead):
            resolve.fetch_release_json(
                opener=_FakeOpener([_FakeResponse(b'short', headers={'content-length': '100'})]),
                attempts=1, retry_delay=0,
            )


class Checkpoint(unittest.TestCase):
    def _pack(self, root, data, chunk_size=4):
        return checkpoint.pack_stream(root, io.BytesIO(data), chunk_size=chunk_size)

    def test_pack_verify_and_restore_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            payload = b'0123456789abcdef!'
            digest, count, total = self._pack(d, payload, chunk_size=5)
            self.assertEqual((count, total), (4, len(payload)))
            self.assertEqual(checkpoint.digest(d), digest)
            out = io.BytesIO()
            rcount, rtotal = checkpoint.restore_stream(d, digest, out)
            self.assertEqual((rcount, rtotal, out.getvalue()), (4, len(payload), payload))
            self.assertEqual(list(d.iterdir()), [])

    def test_rejects_empty_tampering_missing_extra_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            with self.assertRaisesRegex(ValueError, 'Empty'):
                self._pack(d, b'')
            digest, _, _ = self._pack(d, b'abcdefghij', chunk_size=4)

            # Wrong expected manifest hash leaves files untouched.
            out = io.BytesIO()
            with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                checkpoint.restore_stream(d, '0' * 64, out)
            self.assertEqual(out.getvalue(), b'')
            self.assertTrue((d / 'tree.part-00000').exists())

            # Tampered part is rejected before a single byte of it is streamed.
            orig = (d / 'tree.part-00000').read_bytes()
            (d / 'tree.part-00000').write_bytes(b'XXXX')
            with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                checkpoint.restore_stream(d, digest, out)
            self.assertEqual(out.getvalue(), b'')
            (d / 'tree.part-00000').write_bytes(orig)

            # Truncated part rejected before streaming anything.
            p1 = d / 'tree.part-00001'
            orig1 = p1.read_bytes()
            p1.write_bytes(orig1[:-1])
            with self.assertRaises(ValueError):
                checkpoint.restore_stream(d, digest, out)
            self.assertEqual(out.getvalue(), b'')
            p1.write_bytes(orig1)

            # Extra unexpected file rejected.
            (d / 'extra').write_bytes(b'x')
            with self.assertRaises(ValueError):
                checkpoint.restore_stream(d, digest, out)
            (d / 'extra').unlink()

            # Symlink part rejected.
            real = Path(tmp) / '_real'
            real.write_bytes(orig1)
            p1.unlink()
            p1.symlink_to(real)
            with self.assertRaises(ValueError):
                checkpoint.restore_stream(d, digest, out)
            p1.unlink()
            real.unlink()
            p1.write_bytes(orig1)

            # Missing middle part rejected.
            p1.unlink()
            with self.assertRaises(ValueError):
                checkpoint.parts(d)

    def test_pack_cleans_up_on_stream_failure(self):
        class FailingStream:
            def __init__(self):
                self.n = 0
            def read(self, size):
                self.n += 1
                if self.n == 1:
                    return b'abcd'
                raise OSError('broken pipe')

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            with self.assertRaises(OSError):
                checkpoint.pack_stream(d, FailingStream(), chunk_size=4)
            self.assertEqual(list(d.iterdir()), [])

    def test_downstream_failure_preserves_current_part(self):
        class FailingOut:
            def write(self, data):
                raise BrokenPipeError('zstd died')
            def flush(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            digest, _, _ = self._pack(d, b'abcdefgh', chunk_size=4)
            with self.assertRaises(BrokenPipeError):
                checkpoint.restore_stream(d, digest, FailingOut())
            self.assertTrue((d / 'tree.part-00000').exists())

    def test_cli_reports_throughput_to_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / 'parts'
            d.mkdir()
            summary = Path(tmp) / 'summary.md'
            env = {**os.environ, 'GITHUB_STEP_SUMMARY': str(summary)}
            p = subprocess.run(
                [sys.executable, str(SCRIPTS / 'checkpoint.py'), 'pack-stream', str(d), '--chunk-size', '8'],
                input=b'hello checkpoint world!', env=env, capture_output=True, check=True,
            )
            digest = p.stdout.decode().strip()
            self.assertRegex(digest, r'^[0-9a-f]{64}$')
            r = subprocess.run(
                [sys.executable, str(SCRIPTS / 'checkpoint.py'), 'restore-stream', str(d)],
                env={**env, 'EXPECTED_SHA256': digest}, capture_output=True, check=True,
            )
            self.assertEqual(r.stdout, b'hello checkpoint world!')
            text = summary.read_text()
            self.assertIn('Packed 3 checkpoint part(s)', text)
            self.assertIn('Verified and streamed 3 checkpoint part(s)', text)


class BuildSupervisor(unittest.TestCase):
    def py(self, code):
        return [sys.executable, '-c', code]

    def test_outcomes_and_orderly_deadline(self):
        self.assertEqual(build.supervise(self.py('pass'), 5), 'completed')
        self.assertEqual(build.supervise(self.py('raise SystemExit(1)'), 5), 'failed:1')
        # Unsolicited exit 2/130 without deadline must NOT be resumable.
        self.assertEqual(build.supervise(self.py('raise SystemExit(2)'), 5), 'failed:2')
        self.assertEqual(build.supervise(self.py('raise SystemExit(130)'), 5), 'failed:130')
        # Orderly SIGINT acknowledged with exit 2 or 130.
        for code in (2, 130):
            cmd = self.py(f'import signal, time; signal.signal(signal.SIGINT, lambda *_: exit({code})); time.sleep(10)')
            self.assertEqual(build.supervise(cmd, 0.2, grace=3), 'running')
        # Process ignoring SIGINT hits shutdown-timeout and is force-killed.
        cmd = self.py('import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(10)')
        self.assertEqual(build.supervise(cmd, 0.2, grace=0.2), 'failed:shutdown-timeout')
        # Process dying with default SIGINT (-2) is NOT a clean ninja exit.
        cmd = self.py('import signal, time; signal.signal(signal.SIGINT, signal.SIG_DFL); time.sleep(10)')
        self.assertEqual(build.supervise(cmd, 0.2, grace=2), 'failed:-2')

    def test_waits_for_interrupted_child_and_rejects_lingering_setsid_child(self):
        # Cleanly exits during remaining stop grace.
        cmd = self.py(
            'import signal, subprocess, sys, time\n'
            'subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(0.3)"])\n'
            'signal.signal(signal.SIGINT, lambda *_: sys.exit(2))\n'
            'time.sleep(10)\n'
        )
        self.assertEqual(build.supervise(cmd, 0.2, grace=3), 'running')

        # Detached child staying alive past completion causes failure and gets killed (in subreaper process).
        probe = textwrap.dedent(f'''
            import sys
            sys.path.insert(0, {str(SCRIPTS)!r})
            import build
            prot = build.enable_orphan_tracking()
            cmd = [sys.executable, '-c',
                   'import subprocess, sys; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)']
            print(build.supervise(cmd, 5, grace=1, protected_children=prot))
        ''')
        r = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, timeout=10, check=True)
        self.assertTrue(r.stdout.strip().startswith('failed:forced-process-cleanup('), r.stdout)

    def test_compute_jobs_and_budget(self):
        self.assertEqual(build.compute_jobs(cpu_count=4, mem_kib=16 * 1024 * 1024), 4)
        self.assertEqual(build.compute_jobs(cpu_count=8, mem_kib=64 * 1024 * 1024), 4)
        self.assertEqual(build.compute_jobs(cpu_count=4, mem_kib=7 * 1024 * 1024), 2)
        self.assertEqual(build.compute_jobs(cpu_count=4, mem_kib=1024 * 1024), 1)
        self.assertEqual(build.compute_budget(100, 700, final_stage=False), build.NORMAL_JOB_DEADLINE_SECONDS - 600)
        self.assertEqual(build.compute_budget(100, 700, final_stage=True), build.FINAL_JOB_DEADLINE_SECONDS - 600)

    def test_main_records_history_and_refuses_spent_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            root = t / 'relay'
            ninja = root / 'src/third_party/ninja/ninja'
            ninja.parent.mkdir(parents=True)
            ninja.write_text('#!/bin/sh\nexit 0\n')
            ninja.chmod(0o755)
            out = t / 'output'
            summary = t / 'summary'
            now = int(build.monotonic_now())
            (t / 'chromium-job-start').write_text(f'{now}\n')
            env = {
                **os.environ, 'CR_ROOT': str(root), 'RUNNER_TEMP': str(t),
                'GITHUB_OUTPUT': str(out), 'GITHUB_STEP_SUMMARY': str(summary),
                'GITHUB_RUN_ATTEMPT': '1', 'STAGE': '03',
            }
            r = subprocess.run([sys.executable, str(SCRIPTS / 'build.py')], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(out.read_text().strip(), 'status=completed')
            rec = json.loads((root / 'metadata/runners.jsonl').read_text().strip())
            self.assertEqual((rec['stage'], rec['status'], rec['final_stage']), ('03', 'completed', False))
            self.assertIn('Stage 03: `completed`', summary.read_text())

            # Exhausted setup budget refuses to start.
            (t / 'chromium-job-start').write_text(f'{now - build.NORMAL_JOB_DEADLINE_SECONDS}\n')
            r2 = subprocess.run([sys.executable, str(SCRIPTS / 'build.py')], env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(r2.returncode, 0)
            self.assertIn('exhausted the time budget', r2.stderr)

            # Cancellation suppresses status output.
            ninja.write_text('#!/bin/sh\nsleep 30\n')
            (t / 'chromium-job-start').write_text(f'{now}\n')
            out.write_text('')
            proc = subprocess.Popen([sys.executable, str(SCRIPTS / 'build.py')], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.3)
            proc.send_signal(signal.SIGTERM)
            _, err = proc.communicate(timeout=10)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('cancelled by signal', err)
            self.assertEqual(out.read_text(), '')


class SourceState(unittest.TestCase):
    def _init_repo(self, path):
        path.mkdir(parents=True)
        run = lambda *a: subprocess.run(['git', '-C', str(path), *a], check=True, capture_output=True, text=True)
        run('init')
        run('config', 'user.email', 't@example.com')
        run('config', 'user.name', 't')
        (path / 'f').write_text('ok\n')
        run('add', 'f')
        run('commit', '-m', 'init')
        return run('rev-parse', 'HEAD').stdout.strip()

    def test_verify_source_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'relay'
            src_sha = self._init_repo(root / 'src')
            ver = '135.0.7049.38'
            (root / 'src/chrome').mkdir()
            (root / 'src/chrome/VERSION').write_text('MAJOR=135\nMINOR=0\nBUILD=7049\nPATCH=38\n')
            depot_sha = self._init_repo(root / 'depot_tools')
            ws = Path(tmp) / 'ws'
            (ws / 'config').mkdir(parents=True)
            (ws / 'config/depot_tools_revision.txt').write_text(depot_sha + '\n')
            (ws / 'config/args.gn').write_text('is_debug = false\n')

            meta = root / 'metadata'
            meta.mkdir()
            lock = {
                'version': ver, 'chromium_revision': src_sha, 'depot_tools_revision': depot_sha,
                'source': resolve.SOURCE, 'release_api': resolve.URL, 'target': 'chrome_public_apk',
                'architecture': 'arm64', 'repository': 'o/r', 'workflow_sha': 'b' * 40, 'run_id': '42',
            }
            (meta / 'build-lock.json').write_text(json.dumps(lock))
            (meta / 'chromiumdash.json').write_text(json.dumps([
                {'channel': 'Stable', 'platform': 'Android', 'version': ver, 'hashes': {'chromium': src_sha}},
            ]))
            (meta / 'args.gn').write_text('is_debug = false\n')
            (meta / 'runners.jsonl').write_text(
                json.dumps({'stage': '00', 'status': 'running', 'final_stage': False}) + '\n'
            )
            out_dir = root / 'src/out/Release'
            out_dir.mkdir(parents=True)
            (out_dir / 'args.gn').write_text('is_debug = false\n')
            (out_dir / 'build.ninja').write_text('rule x\n')
            for rel in ('src/buildtools/linux64/gn', 'src/third_party/ninja/ninja'):
                t = root / rel
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_text('#!/bin/sh\nexit 0\n')
                t.chmod(0o755)

            env = {**os.environ, 'CR_ROOT': str(root), 'GITHUB_WORKSPACE': str(ws),
                   'GITHUB_REPOSITORY': 'o/r', 'GITHUB_SHA': 'b' * 40, 'GITHUB_RUN_ID': '42', 'STAGE': '01'}
            r = subprocess.run([sys.executable, str(SCRIPTS / 'source_state.py'), 'verify-restore'],
                               env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

            # Unterminated runners.jsonl rejected.
            (meta / 'runners.jsonl').write_text(json.dumps({'stage': '00', 'status': 'running', 'final_stage': False}))
            self.assertNotEqual(subprocess.run([sys.executable, str(SCRIPTS / 'source_state.py'), 'verify-restore'],
                                               env=env, capture_output=True).returncode, 0)
            (meta / 'runners.jsonl').write_text(
                json.dumps({'stage': '00', 'status': 'running', 'final_stage': False}) + '\n')

            # Tracked file change rejected.
            (root / 'src/f').write_text('dirty\n')
            self.assertNotEqual(subprocess.run([sys.executable, str(SCRIPTS / 'source_state.py'), 'verify-source'],
                                               env=env, capture_output=True).returncode, 0)


class ApkChecks(unittest.TestCase):
    def test_zip_and_badging_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'good.apk'
            with zipfile.ZipFile(p, 'w') as zf:
                zf.writestr('AndroidManifest.xml', b'xml')
                zf.writestr('classes.dex', DEX_HDR)
                zf.writestr('lib/arm64-v8a/libchrome.so', ELF_AARCH64)
            apk.inspect_zip(p)

            # Non-arm64 ABI rejected.
            bad = Path(tmp) / 'x86.apk'
            with zipfile.ZipFile(bad, 'w') as zf:
                zf.writestr('AndroidManifest.xml', b'xml')
                zf.writestr('classes.dex', DEX_HDR)
                zf.writestr('lib/x86_64/libchrome.so', ELF_AARCH64)
            with self.assertRaises(ValueError):
                apk.inspect_zip(bad)

            # Non-ELF .so rejected.
            bad_elf = Path(tmp) / 'badelf.apk'
            with zipfile.ZipFile(bad_elf, 'w') as zf:
                zf.writestr('AndroidManifest.xml', b'xml')
                zf.writestr('classes.dex', DEX_HDR)
                zf.writestr('lib/arm64-v8a/libchrome.so', b'not an elf' * 10)
            with self.assertRaises(ValueError):
                apk.inspect_zip(bad_elf)

            # Debuggable and wrong version rejected in badging.
            aapt2 = Path(tmp) / 'aapt2'
            for line, err in [
                ("package: name='org.chromium.chrome' versionCode='1' versionName='1.0.0.1'\napplication-debuggable\nnative-code: 'arm64-v8a'\n", 'debuggable'),
                ("package: name='org.chromium.chrome' versionCode='1' versionName='1.0.0.1'\ntestOnly='true'\nnative-code: 'arm64-v8a'\n", 'testOnly'),
                ("package: name='org.chromium.chrome' versionCode='1' versionName='9.9.9.9'\nnative-code: 'arm64-v8a'\n", 'versionName'),
                ("package: name='org.chromium.chrome' versionCode='1' versionName='1.0.0.1'\nnative-code: 'armeabi-v7a'\n", 'arm64-v8a'),
            ]:
                aapt2.write_text(f'#!/bin/sh\nprintf %s "{line}"\n')
                aapt2.chmod(0o755)
                with self.assertRaisesRegex(ValueError, err):
                    apk.inspect_badging(aapt2, p, '1.0.0.1')


class RetryAndCommon(unittest.TestCase):
    def test_retry_succeeds_fails_and_interrupts_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, 'RUNNER_TEMP': tmp, 'CR_RETRY_DELAY': '0'}
            flag = Path(tmp) / 'count'
            ok_cmd = (f'source "{SCRIPTS}/common.sh"; '
                      f'flaky() {{ n=$(cat "{flag}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{flag}"; (( n == 3 )); }}; '
                      'retry flaky')
            r = subprocess.run(['bash', '-c', ok_cmd], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(flag.read_text().strip(), '3')

            fail_cmd = f'source "{SCRIPTS}/common.sh"; retry false'
            r2 = subprocess.run(['bash', '-c', fail_cmd], env=env, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(r2.returncode, 0)

            # SIGTERM during backoff returns quickly and does not hang on sleep.
            t0 = time.monotonic()
            sig_cmd = f'source "{SCRIPTS}/common.sh"; export CR_RETRY_DELAY=30; retry false'
            proc = subprocess.Popen(['bash', '-c', sig_cmd], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(0.3)
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 143)
            self.assertLess(time.monotonic() - t0, 3.0)


class StaticPolicy(unittest.TestCase):
    def test_pinned_actions_and_no_unsafe_triggers(self):
        allowed = {
            'checkout': '3d3c42e5aac5ba805825da76410c181273ba90b1',
            'upload-artifact': 'cf430e030ddbb5b0abf93d22962f4752f3646cd9',
            'download-artifact': '9000827ccba6bdab643e8b6fd33ac0654aef8333',
        }
        for wf in (ROOT / '.github/workflows').glob('*.yml'):
            text = wf.read_text()
            for use in re.findall(r'^\s*(?:-\s*)?uses:\s*(\S+)', text, re.M):
                if use == './.github/workflows/chromium-stage.yml':
                    continue
                m = re.fullmatch(r'actions/([a-z-]+)@([0-9a-f]{40})', use)
                self.assertIsNotNone(m, use)
                self.assertEqual(allowed[m[1]], m[2])
            for bad in ('pull_request_target:', 'pull_request:', 'secrets.', 'contents: write', 'id-token: write'):
                self.assertNotIn(bad, text)

    def test_shell_syntax_and_config_pins(self):
        for sh in SCRIPTS.glob('*.sh'):
            subprocess.run(['bash', '-n', str(sh)], check=True)
        self.assertRegex((ROOT / 'config/depot_tools_revision.txt').read_text().strip(), r'^[0-9a-f]{40}$')
        self.assertRegex((ROOT / 'config/debug_signer_sha256.txt').read_text().strip(), r'^[0-9a-f]{64}$')
        args = (ROOT / 'config/args.gn').read_text()
        for line in ('dcheck_always_on = false', 'enable_java_asserts = false',
                     'debuggable_apks = false', 'disable_fieldtrial_testing_config = true',
                     'concurrent_links = 1', 'use_remoteexec = false', 'use_siso = false'):
            self.assertIn(line, args)
        self.assertIn('| zstd -T"$(nproc)" -3 -c', (SCRIPTS / 'pack.sh').read_text())

    def test_stage_timeouts_and_budgets(self):
        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        steps = re.split(r'\n      - ', '\n' + stage.split('\n    steps:\n', 1)[1])[1:]
        for step in steps:
            self.assertRegex(step, r'\n        timeout-minutes: [1-9][0-9]*\n', step.splitlines()[0])

        def timeout_of(prefix):
            s, = [st for st in steps if st.startswith(f'name: {prefix}')]
            return int(re.search(r'^        timeout-minutes: (\d+)$', s, re.M)[1])

        job_limit = int(re.search(r'^    timeout-minutes: (\d+)$', stage, re.M)[1])
        shutdown = math.ceil((build.STOP_GRACE_SECONDS + build.CLEANUP_TIMEOUT_SECONDS + 1) / 60)
        logs = timeout_of('Save stage logs')
        self.assertLessEqual(
            build.NORMAL_JOB_DEADLINE_SECONDS // 60 + shutdown + timeout_of('Destructively')
            + timeout_of('Upload immutable checkpoint') + timeout_of('Confirm checkpoint upload') + logs,
            job_limit - 4,
        )
        post = sum(timeout_of(n) for n in (
            'Verify APK signature', 'Upload APK', 'Retry the APK upload',
            'Confirm APK upload', 'Keep the unverified APK'))
        self.assertLessEqual(build.FINAL_JOB_DEADLINE_SECONDS // 60 + shutdown + post + logs, job_limit - 4)
        self.assertLess(stage.index('name: Save stage logs'), stage.index('name: Retry the checkpoint upload'))

    def test_confirm_upload_and_stage_input_validation(self):
        for val, ok in (('1', True), ('9007199254740991', True), ('0', False), ('', False), ('9007199254740992', False)):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / 'out'
                r = subprocess.run(['bash', str(SCRIPTS / 'confirm-upload.sh')],
                                   env={**os.environ, 'TRANSFER_ID': val, 'GITHUB_OUTPUT': str(out)},
                                   capture_output=True)
                self.assertEqual(r.returncode == 0, ok, val)

        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        script = textwrap.dedent(re.search(r"python3 - <<'PY'\n(.*?)\n *PY\n", stage, re.S)[1])
        for overrides, ok in [
            ({'STAGE': '00', 'ARTIFACT_ID': '', 'EXPECTED_SHA256': ''}, True),
            ({'STAGE': '08', 'ARTIFACT_ID': '9007199254740991', 'EXPECTED_SHA256': 'a' * 64}, True),
            ({'STAGE': '00', 'ARTIFACT_ID': '1', 'EXPECTED_SHA256': 'a' * 64}, False),
            ({'STAGE': '09', 'ARTIFACT_ID': '1', 'EXPECTED_SHA256': 'a' * 64}, False),
            ({'STAGE': '01', 'ARTIFACT_ID': '9007199254740992', 'EXPECTED_SHA256': 'a' * 64}, False),
            ({'STAGE': '01', 'ARTIFACT_ID': '1', 'EXPECTED_SHA256': 'A' * 64}, False),
        ]:
            with tempfile.TemporaryDirectory() as tmp:
                r = subprocess.run([sys.executable, '-c', script],
                                   env={**os.environ, 'RUNNER_TEMP': tmp, **overrides}, capture_output=True)
                self.assertEqual(r.returncode == 0, ok, overrides)


class ResultVerdict(unittest.TestCase):
    ORDER = ['prepare', *(f'part-{i:02d}' for i in range(1, 9))]

    def verdict(self, overrides=None, **jobs):
        stable = (ROOT / '.github/workflows/chromium-stable.yml').read_text()
        script = textwrap.dedent(re.search(r"python3 - <<'PY'\n(.*?)\n *PY\n", stable, re.S)[1])
        needs = {n: {'result': 'skipped', 'outputs': {}} for n in self.ORDER}
        for name, (result, status) in jobs.items():
            out = {'status': status} if status else {}
            if status == 'running':
                out.update({'artifact-id': '12345', 'sha256': 'a' * 64})
            elif status == 'completed' and result == 'success':
                out['apk-id'] = '54321'
            out.update((overrides or {}).get(name, {}))
            needs[name.replace('_', '-')] = {'result': result, 'outputs': out}
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / 'summary.md'
            r = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                               env={**os.environ, 'NEEDS_JSON': json.dumps(needs), 'GITHUB_STEP_SUMMARY': str(summary)})
            return r.returncode == 0, summary.read_text()

    def test_verdict_cases(self):
        ok, text = self.verdict(prepare=('success', 'running'), part_01=('success', 'completed'))
        self.assertTrue(ok, text)
        self.assertIn('APK completed in `part-01`', text)

        ok, text = self.verdict()
        self.assertFalse(ok)
        self.assertIn('prepare` was skipped', text)

        ok, text = self.verdict(prepare=('success', 'running'), part_01=('failure', 'completed'))
        self.assertFalse(ok)
        self.assertIn('unverified-apk-', text)

        ok, text = self.verdict(prepare=('success', 'running'), part_01=('cancelled', 'completed'))
        self.assertFalse(ok)
        self.assertIn('cancelled after building', text)

        ok, text = self.verdict({'part_01': {'apk-id': ''}},
                                prepare=('success', 'running'), part_01=('success', 'completed'))
        self.assertFalse(ok)
        self.assertIn('without a confirmed APK', text)

        chain = {n.replace('-', '_'): ('success', 'running') for n in self.ORDER[:-1]}
        chain['part_08'] = ('success', 'exhausted')
        ok, text = self.verdict(**chain)
        self.assertFalse(ok)
        self.assertIn('budget was exhausted', text)

        # Missing intermediate stage checkpoint fails.
        ok, _ = self.verdict(part_02=('success', 'completed'))
        self.assertFalse(ok)


if __name__ == '__main__':
    unittest.main()
