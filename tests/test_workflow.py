"""Offline smoke tests. Python stdlib only; does NOT compile Chromium."""
import hashlib
import math
import importlib.util
import io
import json
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

    def test_resolution_rejects_duplicate_json_fields(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate field'):
            resolve.parse_release_json(b'[{"hashes":{"chromium":"a","chromium":"b"}}]')

    def test_resolution_content_length_has_one_unambiguous_source(self):
        from email.message import Message

        response = _FakeResponse(b'[]')
        response.headers = Message()
        response.headers['Content-Length'] = '000000002'
        self.assertEqual(resolve.fetch_release_json(opener=_FakeOpener([response])), b'[]')
        response.headers['Content-Length'] = '3'
        with self.assertRaisesRegex(ValueError, 'Multiple.*Content-Length'):
            resolve.fetch_release_json(opener=_FakeOpener([response]))
        del response.headers['Content-Length']
        response.headers['Content-Length'] = '2'
        response.headers['Transfer-Encoding'] = 'chunked'
        with self.assertRaisesRegex(ValueError, 'both Content-Length and Transfer-Encoding'):
            resolve.fetch_release_json(opener=_FakeOpener([response]))


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

    def cli(self, *args, summary='', **kwargs):
        # Never write test noise into a real job summary when these tests run on Actions.
        env = dict(os.environ, GITHUB_STEP_SUMMARY=str(summary), **kwargs.pop('env', {}))
        return subprocess.run([sys.executable, str(ROOT / '.github/scripts/checkpoint.py'), *args],
                              env=env, capture_output=True, **kwargs)

    def consume(self, expected, **kwargs):
        return self.cli('restore-stream', str(self.root), env=dict(EXPECTED_SHA256=expected), **kwargs)

    def test_cli_reports_throughput_to_job_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / 'summary'
            (Path(tmp) / 'out').mkdir()
            p = self.cli('pack-stream', str(Path(tmp) / 'out'), '--chunk-size', '4', summary=summary, input=b'12345678')
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertRegex(p.stdout, rb'^[0-9a-f]{64}\n$')
            self.assertEqual(self.consume(self.expected, summary=summary).returncode, 0)
            self.assertRegex(summary.read_text(), r'^- Packed 2 checkpoint part\(s\), 8 bytes .* MiB/s\)\n'
                                                  r'- Verified and streamed 2 checkpoint part\(s\), 11 bytes .* MiB/s\)\n$')

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

    def test_kill_session_refuses_success_with_active_detached_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / 'child.pid'
            parent_code = (
                'import os, subprocess, sys\n'
                'c = subprocess.Popen([sys.executable, "-c", "import os,time; os.setpgrp(); print(1, flush=True); time.sleep(30)"], stdout=subprocess.PIPE)\n'
                'assert c.stdout.readline() == b"1\\n"\n'
                f'open({str(pidfile)!r}, "w").write(f"{{os.getpid()}} {{c.pid}}")\n'
            )
            result = self.run_command(parent_code, seconds=5, grace=1)
            session_id, child_pid = map(int, pidfile.read_text().split())
            self.assertEqual(result, f'failed:forced-process-cleanup({child_pid})')
            # PID 1 may not have reaped a zombie by this point; it cannot write.
            self.assertEqual(build._live_session_pids(session_id), [])

    def test_signal_death_at_deadline_is_not_an_acknowledgement(self):
        code = 'import signal,time; signal.signal(signal.SIGINT,signal.SIG_DFL); time.sleep(20)'
        self.assertEqual(self.run_command(code, 0.5), f'failed:{-signal.SIGINT}')

    def test_compute_jobs(self):
        self.assertEqual(build.compute_jobs(cpu_count=4, mem_kib=16 * 1024 * 1024), 4)
        self.assertEqual(build.compute_jobs(cpu_count=2, mem_kib=7 * 1024 * 1024), 2)
        self.assertEqual(build.compute_jobs(cpu_count=8, mem_kib=16 * 1024 * 1024), 4)
        self.assertEqual(build.compute_jobs(cpu_count=4, mem_kib=2 * 1024 * 1024), 1)

    def test_compute_budget(self):
        self.assertEqual(build.compute_budget(1000, 1000 + 20 * 60, final_stage=False), 240 * 60)
        self.assertEqual(build.compute_budget(1000, 1000 + 80 * 60, final_stage=False), 180 * 60)
        self.assertEqual(build.compute_budget(1000, 1000 + 20 * 60, final_stage=True), 305 * 60)
        self.assertEqual(build.compute_budget(1000, 1000 + 80 * 60, final_stage=True), 245 * 60)
        self.assertLess(build.compute_budget(1000, 1000 + 257 * 60, final_stage=False), build.MIN_BUDGET_SECONDS)


class Supervisor(unittest.TestCase):
    def run_main(self, ninja, started_minutes_ago=0):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = tmp / 'root/src/third_party/ninja/ninja'
            fake.parent.mkdir(parents=True)
            (tmp / 'root/metadata').mkdir()
            fake.write_text('#!/bin/sh\n' + ninja + '\n')
            fake.chmod(0o755)
            (tmp / 'chromium-job-start').write_text(str(int(build.monotonic_now()) - started_minutes_ago * 60))
            env = dict(os.environ, CR_ROOT=str(tmp / 'root'), RUNNER_TEMP=str(tmp), STAGE='03',
                       GITHUB_RUN_ATTEMPT='1', GITHUB_OUTPUT=str(tmp / 'output'),
                       GITHUB_STEP_SUMMARY=str(tmp / 'summary'))
            p = subprocess.run([sys.executable, str(ROOT / '.github/scripts/build.py')],
                               env=env, capture_output=True, text=True)
            files = {name: (tmp / name).read_text() if (tmp / name).exists() else ''
                     for name in ('output', 'summary', 'root/metadata/runners.jsonl')}
            return p, files

    def test_completed_build_reports_status_and_record(self):
        p, files = self.run_main('echo "$@" > args.txt')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(files['output'], 'status=completed\n')
        record = json.loads(files['root/metadata/runners.jsonl'])
        self.assertEqual((record['stage'], record['status'], record['final_stage']), ('03', 'completed', False))
        self.assertEqual(record['jobs'], build.compute_jobs())
        self.assertGreaterEqual(record['setup_seconds'], 0)
        self.assertIn('swap_out_mib', record)
        self.assertIsInstance(record['disk_free_gib'], float)
        self.assertRegex(files['summary'], r'^- Stage 03: `completed` after compiling for \d+\.\d min with -j\d+; '
                                           r'setup before compiling .* took \d+\.\d min\.\n'
                                           r'- Stage 03 resources: (?:\d+|unknown) MiB written to swap while '
                                           r'compiling, \d+\.\d GiB left free on the build disk\.\n$')

    def test_failed_build_reports_no_status(self):
        p, files = self.run_main('exit 1')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('NOT checkpointing: failed:1', p.stderr)
        # Memory pressure and disk headroom are what most often explain a failed compile.
        self.assertRegex(p.stderr, r'failed:1; (?:\d+|unknown) MiB written to swap .* GiB left free')
        self.assertEqual((files['output'], files['root/metadata/runners.jsonl']), ('', ''))

    def test_cancellation_does_not_publish_status_or_leave_ninja_alive(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                root = tmp / 'root'
                ninja = root / 'src/third_party/ninja/ninja'
                ninja.parent.mkdir(parents=True)
                ninja.write_text('#!/bin/sh\necho $$ > "$RUNNER_TEMP/ninja.pid"\nexec sleep 30\n')
                ninja.chmod(0o755)
                (tmp / 'chromium-job-start').write_text(str(int(build.monotonic_now())))
                env = dict(os.environ, CR_ROOT=str(root), RUNNER_TEMP=str(tmp), STAGE='03',
                           GITHUB_RUN_ATTEMPT='1', GITHUB_OUTPUT=str(tmp / 'output'),
                           GITHUB_STEP_SUMMARY='')
                with subprocess.Popen([sys.executable, str(ROOT / '.github/scripts/build.py')],
                                      env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True) as proc:
                    try:
                        pidfile = tmp / 'ninja.pid'
                        deadline = time.monotonic() + 4
                        while not (pidfile.is_file() and pidfile.stat().st_size) and time.monotonic() < deadline:
                            time.sleep(0.01)
                        sid = int(pidfile.read_text())
                        proc.send_signal(signum)
                        _, stderr = proc.communicate(timeout=5)
                        self.assertNotEqual(proc.returncode, 0)
                        self.assertIn('NOT checkpointing', stderr)
                        self.assertEqual((tmp / 'output').read_text(), '')
                        self.assertEqual(build._live_session_pids(sid), [])
                    finally:
                        if proc.poll() is None:
                            proc.terminate()
                            proc.communicate(timeout=5)

    def test_resource_probes_never_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            vmstat = Path(tmp) / 'vmstat'
            vmstat.write_text('pswpin 7\npswpout 3\n')
            self.assertEqual(build.swapped_out_bytes(vmstat), 3 * os.sysconf('SC_PAGE_SIZE'))
            for text in ('pswpin 7\n', 'pswpout many\n'):
                vmstat.write_text(text)
                self.assertIsNone(build.swapped_out_bytes(vmstat))
            self.assertIsNone(build.swapped_out_bytes(Path(tmp) / 'missing'))
            self.assertIsInstance(build.free_gib(tmp), float)
            self.assertIsNone(build.free_gib(Path(tmp) / 'missing'))

    def test_spent_budget_refuses_to_start(self):
        p, files = self.run_main('exit 0', started_minutes_ago=build.NORMAL_JOB_DEADLINE_SECONDS // 60)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('exhausted the time budget', p.stderr)
        self.assertEqual(files['output'], '')


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

    def test_checkpoint_compression_uses_every_logical_cpu(self):
        # zstd -T0 means physical cores: only half the vCPUs of an SMT hosted runner.
        pack = (ROOT / '.github/scripts/pack.sh').read_text()
        self.assertIn('| zstd -T"$(nproc)" -3 -c', pack)
        self.assertNotRegex(pack, r'(?m)^[^#\n]*zstd[^#\n]*-T0\b')

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
        for primary, retry, step in (('upload', 'upload-retry', 'confirm-checkpoint'),
                                     ('apk', 'apk-retry', 'confirm-apk')):
            self.assertIn(f"steps.{primary}.outcome == 'success' && steps.{primary}.outputs.artifact-id", stage)
            self.assertIn(f"steps.{retry}.outcome == 'success' && steps.{retry}.outputs.artifact-id", stage)
            self.assertIn(f'steps.{step}.outputs.artifact-id', stage)

    def test_unverified_apk_is_kept_only_on_failure_under_its_own_name(self):
        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        step = re.search(r'      - name: Keep the unverified APK[\s\S]*?(?=\n      - name: )', stage)[0]
        self.assertIn("\n        if: failure() && steps.build.outputs.status == 'completed'\n", step)
        self.assertIn('\n          name: unverified-apk-', step)
        self.assertNotIn('continue-on-error', step)
        # A staged copy of the same regular file release.sh verifies.
        self.assertIn('\n          path: ${{ runner.temp }}/chromium-inspection/ChromePublic.apk\n', step)
        self.assertIn('export CR_ROOT=/mnt/chromium-relay\n', (ROOT / '.github/scripts/common.sh').read_text())
        self.assertIn('source_apk="$CR_ROOT/src/out/Release/apks/ChromePublic.apk"\n',
                      (ROOT / '.github/scripts/release.sh').read_text())

    @staticmethod
    def stage_steps():
        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        return re.split(r'\n      - ', '\n' + stage.split('\n    steps:\n', 1)[1])[1:]

    def step_timeout(self, name):
        step, = [step for step in self.stage_steps() if step.startswith(f'name: {name}')]
        return int(re.search(r'^        timeout-minutes: (\d+)$', step, re.M)[1])

    def test_every_stage_step_is_time_boxed(self):
        # continue-on-error tolerates a failure, not a hang: an unbounded step would
        # run into the 350-minute job limit and take the whole stage down with it.
        for step in self.stage_steps():
            self.assertRegex(step, r'\n        timeout-minutes: [1-9][0-9]*\n', step.splitlines()[0])

    def test_retried_download_still_leaves_compile_budget(self):
        minutes = [self.step_timeout(name) for name in (
            'Prepare disposable hosted runner', 'Download exact checkpoint ID',
            'Discard the partial checkpoint download', 'Retry the checkpoint download once',
            'Verify producer hash')]
        # Runner prep, download, discarding it, its one retry, and restore: even the
        # worst case must leave time to compile.
        self.assertEqual(minutes, [15, 70, 2, 70, 40])
        self.assertLess(sum(minutes) * 60 + build.MIN_BUDGET_SECONDS, build.NORMAL_JOB_DEADLINE_SECONDS)

    def test_final_stage_and_budget_invariants(self):
        stable = (ROOT / '.github/workflows/chromium-stable.yml').read_text()
        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        self.assertNotIn('inputs.final', stage)
        self.assertRegex(stable, r"part-08:[\s\S]*?stage: '08'[\s\S]*?result:")
        self.assertIn("if: steps.build.outputs.status == 'running' && inputs.stage != '08'", stage)
        # Round the whole shutdown allowance up, including bounded session cleanup.
        shutdown = math.ceil((build.STOP_GRACE_SECONDS + build.CLEANUP_TIMEOUT_SECONDS + 1) / 60)
        logs = self.step_timeout('Save stage logs')
        job_limit = int(re.search(r'^    timeout-minutes: (\d+)$', stage, re.M)[1])
        limit = job_limit - 4  # Pre-step job init, transitions and post-job work.
        self.assertLessEqual(build.NORMAL_JOB_DEADLINE_SECONDS // 60 + shutdown + self.step_timeout('Destructively')
                             + self.step_timeout('Upload immutable checkpoint') + self.step_timeout('Confirm checkpoint upload')
                             + logs, limit)
        post_build = sum(self.step_timeout(name) for name in (
            'Verify APK signature', 'Upload APK', 'Retry the APK upload', 'Confirm APK upload',
            'Keep the unverified APK'))
        self.assertLessEqual(build.FINAL_JOB_DEADLINE_SECONDS // 60 + shutdown + post_build + logs, limit)
        step_timeout = int(re.search(r'id: build\n(?:\s+#.*\n)*\s+timeout-minutes: (\d+)', stage)[1])
        self.assertGreaterEqual(step_timeout, build.FINAL_JOB_DEADLINE_SECONDS // 60 + shutdown)
        self.assertLess(step_timeout, job_limit)
        self.assertLess(stage.index('name: Save stage logs'), stage.index('name: Retry the checkpoint upload'))

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
            env = dict(os.environ, STAGE='03', ARTIFACT_ID='123', EXPECTED_SHA256='a' * 64)
            env.update(overrides)
            with tempfile.TemporaryDirectory() as tmp:
                env['RUNNER_TEMP'] = tmp
                ok = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True).returncode == 0
                self.assertEqual((Path(tmp) / 'chromium-job-start').is_file(), ok)
                return ok

        self.assertTrue(valid())
        self.assertTrue(valid(STAGE='08', ARTIFACT_ID='9007199254740991'))
        self.assertTrue(valid(STAGE='00', ARTIFACT_ID='', EXPECTED_SHA256=''))
        for bad in [dict(STAGE='09'), dict(STAGE='1'), dict(ARTIFACT_ID=''), dict(ARTIFACT_ID='0'),
                    dict(EXPECTED_SHA256='A' * 64), dict(STAGE='00'),
                    dict(ARTIFACT_ID='9007199254740992'), dict(ARTIFACT_ID='1' * 200)]:
            self.assertFalse(valid(**bad), bad)


class ResultVerdict(unittest.TestCase):
    ORDER = ['prepare'] + [f'part-{i:02d}' for i in range(1, 9)]

    def verdict(self, output_overrides=None, **jobs):
        stable = (ROOT / '.github/workflows/chromium-stable.yml').read_text()
        script = textwrap.dedent(re.search(r"python3 - <<'PY'\n(.*?)\n *PY\n", stable, re.S)[1])
        needs = {name: dict(result='skipped', outputs={}) for name in self.ORDER}
        for name, (result, status) in jobs.items():
            outputs = dict(status=status) if status else {}
            if status == 'running':
                outputs.update({'artifact-id': '12345', 'sha256': 'a' * 64})
            if status == 'completed' and result == 'success':
                outputs['apk-id'] = '54321'
            outputs.update((output_overrides or {}).get(name, {}))
            needs[name.replace('_', '-')] = dict(result=result, outputs=outputs)
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

    def test_artifact_outputs_are_required_to_pass(self):
        ok, summary = self.verdict({'prepare': {'apk-id': ''}}, prepare=('success', 'completed'))
        self.assertFalse(ok)
        self.assertIn('without a confirmed APK artifact', summary)
        for outputs in ({'artifact-id': ''}, {'sha256': 'invalid'}):
            ok, _ = self.verdict({'prepare': outputs}, prepare=('success', 'running'),
                                 part_01=('success', 'completed'))
            self.assertFalse(ok)

    def test_confirmation_rejects_missing_or_unsafe_artifact_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'outputs'
            for value in ('', '0', '001', '9007199254740992', '1\nother=bad'):
                p = subprocess.run(['bash', str(ROOT / '.github/scripts/confirm-upload.sh')],
                                   env=dict(os.environ, TRANSFER_ID=value, GITHUB_OUTPUT=str(output)),
                                   capture_output=True, text=True)
                self.assertNotEqual(p.returncode, 0, value)
                self.assertFalse(output.exists())
            p = subprocess.run(['bash', str(ROOT / '.github/scripts/confirm-upload.sh')],
                               env=dict(os.environ, TRANSFER_ID='9007199254740991', GITHUB_OUTPUT=str(output)),
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(output.read_text(), 'artifact-id=9007199254740991\n')

    def test_exhausted_chain_fails(self):
        jobs = {name.replace('-', '_'): ('success', 'running') for name in self.ORDER}
        jobs['part_08'] = ('success', 'exhausted')
        ok, summary = self.verdict(**jobs)
        self.assertFalse(ok)
        self.assertIn('stage budget was exhausted', summary)
        ok, summary = self.verdict({'prepare': {'sha256': ''}}, **jobs)
        self.assertFalse(ok)
        self.assertNotIn('stage budget was exhausted', summary)

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

    def test_completed_status_without_success_result_fails(self):
        ok, _ = self.verdict(prepare=('success', 'running'), part_01=('unknown', 'completed'))
        self.assertFalse(ok)


class Hardening(unittest.TestCase):
    def test_resolve_retry_after_and_validation(self):
        import datetime

        now = datetime.datetime(2026, 11, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(resolve._retry_delay(1, 2.0, {'retry-after': '12'}), 12.0)
        self.assertEqual(resolve._retry_delay(1, 2.0, {'RETRY-AFTER': '999'}), resolve.MAX_RETRY_AFTER_SECONDS)
        self.assertEqual(
            resolve._retry_delay(1, 2.0, {'Retry-After': 'Sun, 15 Nov 2026 12:00:25 GMT'}, now=now), 25.0)
        self.assertEqual(
            resolve._retry_delay(1, 2.0, {'Retry-After': 'Sun, 15 Nov 2026 11:59:00 GMT'}, now=now), 0.0)

        err429 = urllib.error.HTTPError(resolve.URL, 429, 'Too Many Requests', {'Retry-After': '0'}, None)
        err408 = urllib.error.HTTPError(resolve.URL, 408, 'Request Timeout', {}, None)
        opener = _FakeOpener([err429, err408, _FakeResponse(b'[1]')])
        self.assertEqual(resolve.fetch_release_json(attempts=3, retry_delay=0.0, opener=opener), b'[1]')
        self.assertEqual(opener.calls, 3)

        bad_status = _FakeResponse(b'[]')
        bad_status.status = 204
        with self.assertRaises(ValueError):
            resolve.fetch_release_json(attempts=1, retry_delay=0.0, opener=_FakeOpener([bad_status]))
        with self.assertRaises(ValueError):
            resolve.fetch_release_json(attempts=1, retry_delay=0.0, opener=_FakeOpener([_FakeResponse(b'')]))

        for kwargs in (dict(attempts=True), dict(attempts=0), dict(timeout=True), dict(timeout=0),
                       dict(timeout=float('nan')), dict(timeout=float('inf')),
                       dict(retry_delay=True), dict(retry_delay=float('nan'))):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                resolve.fetch_release_json(**kwargs, opener=_FakeOpener([]))

    def test_checkpoint_manifest_and_part_hardening(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            for bad in (True, 0, -1, 1.5):
                with self.assertRaises(ValueError):
                    checkpoint.pack_stream(dest, io.BytesIO(b'abc'), chunk_size=bad)

            class _FailingStream:
                def __init__(self):
                    self.calls = 0

                def readinto(self, buf):
                    self.calls += 1
                    if self.calls == 1:
                        buf[:4] = b'abcd'
                        return 4
                    raise OSError('stream failed')

            with self.assertRaises(OSError):
                checkpoint.pack_stream(dest, _FailingStream(), chunk_size=4)
            self.assertEqual(list(dest.iterdir()), [])

            sha, _, _ = checkpoint.pack_stream(dest, io.BytesIO(b'abcdefgh'), chunk_size=4)
            with mock.patch.object(checkpoint, 'MAX_MANIFEST_BYTES', 16):
                with self.assertRaises(ValueError):
                    checkpoint.manifest(dest, sha)

            non_ascii = b'\xff' * 10
            (dest / checkpoint.MANIFEST).write_bytes(non_ascii)
            with self.assertRaises(ValueError):
                checkpoint.manifest(dest, hashlib.sha256(non_ascii).hexdigest())

            link = dest / 'sym'
            link.symlink_to(dest / 'tree.part-00000')
            with self.assertRaises(ValueError):
                checkpoint._open_regular_ro(link)

    def test_supervise_races_and_failure_summaries(self):
        from unittest import mock

        class _ExitedBeforeSigint:
            pid = 99999

            def __init__(self):
                self._waits = 0
                self._polled = False

            def wait(self, timeout=None):
                self._waits += 1
                if self._waits == 1:
                    raise subprocess.TimeoutExpired(['fake'], timeout)
                return 2

            def poll(self):
                if not self._polled:
                    self._polled = True
                    return None
                return 2

        with mock.patch.object(build.subprocess, 'Popen', return_value=_ExitedBeforeSigint()), \
             mock.patch.object(build.os, 'killpg', side_effect=ProcessLookupError), \
             mock.patch.object(build, 'kill_session', return_value=([], [])):
            self.assertEqual(build.supervise(['fake'], 0.1, 1), 'failed:2')

        with mock.patch.object(build, 'kill_session', return_value=([], [42424])):
            self.assertEqual(
                build.supervise([sys.executable, '-c', 'pass'], 5, 1),
                'failed:lingering-processes(42424)',
            )

        sup = Supervisor()
        p, files = sup.run_main('exit 1')
        self.assertNotEqual(p.returncode, 0)
        self.assertRegex(files['summary'], r'^- Stage 03: `failed:1` after compiling .*'
                                           r'\n- Stage 03 resources: .* GiB left free on the build disk\.\n$')
        p, files = sup.run_main('exit 0', started_minutes_ago=build.NORMAL_JOB_DEADLINE_SECONDS // 60)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('- Stage 03: `failed` before compiling;', files['summary'])

    def test_retry_signal_exit_codes_and_base10_delay(self):
        retry_test = Retry()
        for code in (130, 143):
            p = retry_test.run_retry(f'n=0; stop() {{ n=$((n+1)); return {code}; }}; retry stop; echo "n=$n"')
            self.assertEqual(p.returncode, code)
            self.assertNotIn('::warning::', p.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            p = subprocess.run(
                ['bash', '-c',
                 'source "$1/common.sh"; sleep() { echo "slept=$1"; }; '
                 'n=0; flaky() { n=$((n+1)); (( n >= 2 )); }; retry flaky',
                 '_', str(ROOT / '.github/scripts')],
                env=dict(os.environ, RUNNER_TEMP=tmp, CR_RETRY_DELAY='08'),
                capture_output=True, text=True, cwd=tmp)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), 'slept=8')

    def test_args_gn_and_prepare_log_policy(self):
        args_gn = (ROOT / 'config/args.gn').read_text()
        self.assertIn('\nenable_java_asserts = false\n', args_gn)
        self.assertIn('\ndebuggable_apks = false\n', args_gn)
        stage = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        self.assertIn('| tee -i "$RUNNER_TEMP/chromium-logs/prepare.log"', stage)

    def test_release_apk_checks(self):
        import zipfile

        release_sh = (ROOT / '.github/scripts/release.sh').read_text()
        script = re.search(r'"\$CR_PYTHON" - "\$apk" .* <<\'PY\'\n(.*?)\nPY\n', release_sh, re.S)[1]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apk = root / 'ChromePublic.apk'
            with zipfile.ZipFile(apk, 'w') as zf:
                zf.writestr('AndroidManifest.xml', b'manifest')
                zf.writestr('classes.dex', b'dex')
                zf.writestr('lib/arm64-v8a/libchrome.so', b'elf')
            lock = root / 'build-lock.json'
            lock.write_text(json.dumps({'version': '158.0.8127.36'}))
            aapt2 = root / 'aapt2'
            aapt2.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_BADGING"\n')
            aapt2.chmod(0o755)
            meta = root / 'meta.json'

            def run_check(badging, tool=aapt2):
                return subprocess.run(
                    [sys.executable, '-c', script, str(apk), str(lock), str(tool), str(meta)],
                    env=dict(os.environ, FAKE_BADGING=badging),
                    capture_output=True, text=True)

            good = ("package: name='org.chromium.chrome' versionCode='1234' versionName='158.0.8127.36'\n"
                    "sdkVersion:'29'\ntargetSdkVersion:'36'\nnative-code: 'arm64-v8a'")
            p = run_check(good)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(meta.read_text()),
                             {'package': 'org.chromium.chrome', 'version_code': '1234',
                              'min_sdk': '29', 'target_sdk': '36'})

            p = run_check(good + '\napplication-debuggable')
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('debuggable', p.stderr)

            p = run_check("package: name='org.chromium.chrome' versionName='158.0.8127.36'")
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('missing native-code', p.stderr)

            p = run_check("package: name='org.chromium.chrome' versionName='158.0.8127.36'\nnative-code: 'armeabi-v7a'")
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('arm64-v8a', p.stderr)

            p = run_check(good, tool=root / 'missing-aapt2')
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('aapt2 missing', p.stderr)

            bad_dex = root / 'bad-dex.apk'
            with zipfile.ZipFile(bad_dex, 'w') as zf:
                zf.writestr('AndroidManifest.xml', b'manifest')
                zf.writestr('assets/nested.dex', b'dex')
                zf.writestr('lib/arm64-v8a/libchrome.so', b'elf')
            p = subprocess.run([sys.executable, '-c', script, str(bad_dex), str(lock), str(aapt2), str(meta)],
                               env=dict(os.environ, FAKE_BADGING=good), capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('classes.dex', p.stderr)

            bad_so = root / 'bad-so.apk'
            with zipfile.ZipFile(bad_so, 'w') as zf:
                zf.writestr('AndroidManifest.xml', b'manifest')
                zf.writestr('classes.dex', b'dex')
                zf.writestr('lib/arm64-v8a/nested/libchrome.so', b'elf')
            p = subprocess.run([sys.executable, '-c', script, str(bad_so), str(lock), str(aapt2), str(meta)],
                               env=dict(os.environ, FAKE_BADGING=good), capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('native library', p.stderr)

            summary_script = re.search(r"<<'SUMMARY_PY'\n(.*?)\nSUMMARY_PY\n", release_sh, re.S)[1]
            rel_dir = root / 'release'
            (rel_dir / 'metadata').mkdir(parents=True)
            (rel_dir / 'Chromium-Stable-arm64.apk').write_bytes(b'apk')
            (rel_dir / 'metadata/build-lock.json').write_text(json.dumps({
                'version': '158.0.8127.36', 'chromium_revision': 'a' * 40}))
            (rel_dir / 'SHA256SUMS').write_text(f"{'b' * 64}  Chromium-Stable-arm64.apk\n")
            p = subprocess.run([sys.executable, '-c', summary_script, str(rel_dir), str(meta), str(rel_dir)],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn('::warning::Failed to append APK summary:', p.stderr)

    def test_checkpoint_toctou_short_writes_and_cleanup(self):
        from unittest import mock

        class _ShortWriter:
            def __init__(self):
                self.buf = bytearray()

            def write(self, data):
                chunk = bytes(data[:2])
                self.buf.extend(chunk)
                return len(chunk)

            def flush(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            sha, _, _ = checkpoint.pack_stream(dest, io.BytesIO(b'abcdefghij'), chunk_size=4)
            out = _ShortWriter()
            self.assertEqual(checkpoint.restore_stream(dest, sha, out), (3, 10))
            self.assertEqual(bytes(out.buf), b'abcdefghij')

            # Replace part 0 on disk AFTER _verify_and_open opens it; streaming must use the verified fd.
            sha, _, _ = checkpoint.pack_stream(dest, io.BytesIO(b'12345678'), chunk_size=4)
            orig_verify = checkpoint._verify_and_open

            def swap_after_open(path, entry):
                f = orig_verify(path, entry)
                if path.name == 'tree.part-00000':
                    path.unlink()
                    path.write_bytes(b'EVIL')
                return f

            out = io.BytesIO()
            with mock.patch.object(checkpoint, '_verify_and_open', side_effect=swap_after_open):
                self.assertEqual(checkpoint.restore_stream(dest, sha, out), (2, 8))
            self.assertEqual(out.getvalue(), b'12345678')

            # Failed manifest write must clean up the partial manifest as well as all parts.
            def fail_on_manifest(stream, data):
                if b'tree.part-' in bytes(data):
                    raise OSError('ENOSPC')
                stream.write(data)

            with mock.patch.object(checkpoint, '_write_all', side_effect=fail_on_manifest):
                with self.assertRaises(OSError):
                    checkpoint.pack_stream(dest, io.BytesIO(b'abcd'), chunk_size=4)
            self.assertEqual(list(dest.iterdir()), [])

            # Existing file must be rejected by exclusive open.
            existing = dest / 'existing'
            existing.write_bytes(b'x')
            with self.assertRaises(FileExistsError):
                checkpoint._open_exclusive_wo(existing)

    def test_checkpoint_rechecks_each_block_of_the_open_inode(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            sha, _, _ = checkpoint.pack_stream(dest, io.BytesIO(b'aaaabbbbcccc'), chunk_size=12)
            orig_verify = checkpoint._verify_and_open

            def change_same_size(path, entry):
                result = orig_verify(path, entry)
                with path.open('r+b') as fp:
                    fp.seek(4)
                    fp.write(b'BBBB')
                return result

            out = io.BytesIO()
            with mock.patch.object(checkpoint, 'IO_BUFFER', 4), \
                 mock.patch.object(checkpoint, '_verify_and_open', side_effect=change_same_size):
                with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                    checkpoint.restore_stream(dest, sha, out)
            self.assertEqual(out.getvalue(), b'aaaa')
            self.assertTrue((dest / 'tree.part-00000').is_file())

    def test_checkpoint_does_not_block_opening_special_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / 'fifo'
            os.mkfifo(fifo)
            p = subprocess.run(
                [sys.executable, '-c',
                 'import checkpoint, sys; checkpoint._open_regular_ro(sys.argv[1])', str(fifo)],
                cwd=ROOT / '.github/scripts', capture_output=True, text=True, timeout=3,
            )
            self.assertNotEqual(p.returncode, 0)
            self.assertIn(checkpoint.BAD_LAYOUT, p.stderr)

    def test_supervisor_lingering_ninja_and_budget_guards(self):
        from unittest import mock

        class _StuckNinja:
            pid = 77777

            def __init__(self):
                self.waits = 0
                self.polls = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    return 0
                raise AssertionError('blocking wait() called while ninja pid is lingering')

            def poll(self):
                self.polls += 1
                return None

        stuck = _StuckNinja()
        with mock.patch.object(build.subprocess, 'Popen', return_value=stuck), \
             mock.patch.object(build.os, 'killpg'), \
             mock.patch.object(build, 'kill_session', return_value=([], [77777])):
            self.assertEqual(build.supervise(['fake'], 5, 1), 'failed:lingering-processes(77777)')
        self.assertGreaterEqual(stuck.polls, 1)

        sup = Supervisor()
        p, files = sup.run_main('exit 0', started_minutes_ago=-10)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('Invalid future job start timestamp', p.stderr)
        self.assertIn('Invalid future job start timestamp', files['summary'])

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / 'chromium-job-start').write_text(str(int(build.monotonic_now())))
            p = subprocess.run([sys.executable, str(ROOT / '.github/scripts/build.py')],
                               env=dict(os.environ, CR_ROOT=str(t), RUNNER_TEMP=str(t),
                                        STAGE='9'),
                               capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('Invalid STAGE', p.stderr)

    def test_resolve_rejects_negative_delay_and_non_https_url(self):
        for kwargs in (dict(retry_delay=-0.1), dict(url='http://chromiumdash.appspot.com/x'),
                       dict(url='file:///etc/passwd'), dict(url='https://')):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                resolve.fetch_release_json(**kwargs, opener=_FakeOpener([]))

    def test_common_sh_aborts_retry_when_shell_receives_sigint(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = subprocess.run(
                ['bash', '-c',
                 'source "$1/common.sh"; '
                 'child() { python3 -c "import os,signal,sys,time;'
                 'signal.signal(signal.SIGINT, lambda *a: sys.exit(1));'
                 'os.kill(0, signal.SIGINT); time.sleep(5)"; }; '
                 'retry child',
                 '_', str(ROOT / '.github/scripts')],
                env=dict(os.environ, RUNNER_TEMP=tmp, CR_RETRY_DELAY='0'),
                capture_output=True, text=True, cwd=tmp, start_new_session=True)
            self.assertEqual(p.returncode, 130)
            self.assertNotIn('::warning::', p.stderr)

    def test_restore_sh_validation_and_policy_checks(self):
        command = [sys.executable, str(ROOT / '.github/scripts/source_state.py'), 'verify-restore']
        depot_sha = (ROOT / 'config/depot_tools_revision.txt').read_text().strip()

        with tempfile.TemporaryDirectory() as tmp:
            cr = Path(tmp) / 'root'
            (cr / 'metadata').mkdir(parents=True)
            (cr / 'src/chrome').mkdir(parents=True)
            (cr / 'src/out/Release').mkdir(parents=True)
            (cr / 'src/buildtools/linux64').mkdir(parents=True)
            (cr / 'src/third_party/ninja').mkdir(parents=True)
            (cr / 'depot_tools').mkdir(parents=True)
            for repo in (cr / 'src', cr / 'depot_tools'):
                subprocess.run(['git', 'init', '-q', str(repo)], check=True)
                subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                                'commit', '-q', '--allow-empty', '-m', 'init'], check=True)
            src_sha = subprocess.check_output(['git', '-C', str(cr / 'src'), 'rev-parse', 'HEAD'], text=True).strip()
            dep_head = subprocess.check_output(['git', '-C', str(cr / 'depot_tools'), 'rev-parse', 'HEAD'], text=True).strip()
            ws = Path(tmp) / 'ws'
            (ws / 'config').mkdir(parents=True)
            (ws / 'config/depot_tools_revision.txt').write_text(dep_head + '\n')
            (ws / 'config/args.gn').write_bytes(b'is_debug = false\n')
            (cr / 'src/out/Release/args.gn').write_bytes(b'is_debug = false\n')
            (cr / 'src/out/Release/build.ninja').write_text('')
            for rel in ('src/buildtools/linux64/gn', 'src/third_party/ninja/ninja'):
                tool = cr / rel
                tool.write_text('#!/bin/sh\n')
                tool.chmod(0o755)
            (cr / 'src/chrome/VERSION').write_text('# comment=ignored\n MAJOR = 158 \nMINOR=0\nBUILD=8127\nPATCH=36\n')
            lock = {
                'repository': 'o/r', 'run_id': '1', 'workflow_sha': 'c' * 40,
                'target': 'chrome_public_apk', 'architecture': 'arm64',
                'source': resolve.SOURCE, 'release_api': resolve.URL,
                'chromium_revision': src_sha, 'depot_tools_revision': dep_head,
                'version': '158.0.8127.36',
            }
            (cr / 'metadata/build-lock.json').write_text(json.dumps(lock))
            env = dict(os.environ, CR_ROOT=str(cr), GITHUB_WORKSPACE=str(ws),
                       GITHUB_REPOSITORY='o/r', GITHUB_RUN_ID='1', GITHUB_SHA='c' * 40, STAGE='01')
            prior = dict(stage='00', status='running', final_stage=False, run_attempt='1')
            records = cr / 'metadata/runners.jsonl'
            records.write_text(json.dumps(prior) + '\n')
            p = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)

            p = subprocess.run(command, env={**env, 'STAGE': '02'}, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            records.write_text(json.dumps({**prior, 'stage': '02'}) + '\n')
            p = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('must contain running stages 00 through 00', p.stderr)
            records.write_text(json.dumps(prior) + '\n')

            gitdir = cr / 'src/.git'
            moved = cr / 'elsewhere'
            gitdir.rename(moved)
            gitdir.symlink_to(moved, target_is_directory=True)
            p = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('missing a real .git', p.stderr)
            gitdir.unlink()
            moved.rename(gitdir)

            version_file = cr / 'src/chrome/VERSION'
            valid_version = version_file.read_text()
            version_file.write_text(valid_version + 'MAJOR=158\n')
            p = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('duplicates field: MAJOR', p.stderr)
            version_file.write_text(valid_version)

            (cr / 'src/buildtools/linux64/gn').unlink()
            p = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('buildtools/linux64/gn', p.stderr)
            (cr / 'src/buildtools/linux64/gn').write_text('#!/bin/sh\n')
            (cr / 'src/buildtools/linux64/gn').chmod(0o755)

            (cr / 'src/chrome/VERSION').write_text('MAJOR=158\nMINOR=0\nBUILD=8127\n')
            p = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('missing field: PATCH', p.stderr)

            (cr / 'src/chrome/VERSION').write_text('MAJOR=158\nMINOR=0\nBUILD=8127\nPATCH=36\n')
            (cr / 'metadata/build-lock.json').write_text(json.dumps({**lock, 'source': 'https://evil.example/src.git'}))
            p = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('source/target/architecture', p.stderr)

            # Missing .git must not climb into an ancestor repository.
            import shutil
            shutil.rmtree(cr / 'src/.git')
            subprocess.run(['git', 'init', '-q', str(tmp)], check=True)
            (cr / 'metadata/build-lock.json').write_text(json.dumps(lock))
            p = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('missing a real .git', p.stderr)

        self.assertIn("'generate_location_tags': False", (ROOT / '.github/scripts/fetch.sh').read_text())
        self.assertIn('trap reap_cleanup EXIT', (ROOT / '.github/scripts/host.sh').read_text())
        self.assertEqual(len(depot_sha), 40)

    def test_result_verdict_empty_status_and_anomalies(self):
        rv = ResultVerdict()
        ok, summary = rv.verdict(prepare=('failure', ''))
        self.assertFalse(ok)
        self.assertIn('| `prepare` | `failure` | `-` |', summary)
        self.assertNotIn('| `` |', summary)

        ok, _ = rv.verdict(prepare=('unknown', 'running'), part_01=('success', 'completed'))
        self.assertFalse(ok)

        ok, summary = rv.verdict(prepare=('success', 'completed'), part_01=('success', 'completed'))
        self.assertFalse(ok)
        self.assertIn('multiple stages reported `completed`', summary)
        self.assertNotIn('unverified-apk-*', summary)

        ok, _ = rv.verdict(prepare=('skipped', ''), part_01=('success', 'completed'))
        self.assertFalse(ok)

        ok, _ = rv.verdict(prepare=('success', ''), part_01=('success', 'completed'))
        self.assertFalse(ok)

        ok, _ = rv.verdict(prepare=('success', 'completed'), part_01=('success', 'running'))
        self.assertFalse(ok)

        ok, summary = rv.verdict(prepare=('success', 'completed'), part_01=('failure', ''))
        self.assertFalse(ok)
        self.assertIn('failed or were cancelled: part-01.', summary)
        self.assertNotIn('unverified-apk-*', summary)

    def test_common_xdg_isolation_and_interruptible_backoff(self):
        import time

        with tempfile.TemporaryDirectory() as tmp:
            p = subprocess.run(
                ['bash', '-c', 'source "$1/common.sh"; printf "%s\\n%s\\n%s\\n%s\\n" '
                 '"$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "${GIT_DIR:-unset}"',
                 '_', str(ROOT / '.github/scripts')],
                env=dict(os.environ, RUNNER_TEMP=tmp, XDG_CONFIG_HOME='/home/runner/.config', GIT_DIR='/bad'),
                capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.splitlines(), [
                '/mnt/chromium-relay/cache/xdg-config',
                '/mnt/chromium-relay/cache/xdg-data',
                '/mnt/chromium-relay/cache/xdg-state',
                'unset',
            ])

            p = subprocess.run(
                ['bash', '-c', 'source "$1/common.sh"; retry true', '_', str(ROOT / '.github/scripts')],
                env=dict(os.environ, RUNNER_TEMP=tmp, CR_RETRY_DELAY='9999999999999999999'),
                capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('Invalid CR_RETRY_DELAY', p.stderr)

            t0 = time.monotonic()
            p = subprocess.run(
                ['bash', '-c', 'source "$1/common.sh"; ( sleep 0.15; kill -TERM $$ ) & retry false',
                 '_', str(ROOT / '.github/scripts')],
                env=dict(os.environ, RUNNER_TEMP=tmp, CR_RETRY_DELAY='10'),
                capture_output=True, text=True)
            self.assertEqual(p.returncode, 143)
            self.assertLess(time.monotonic() - t0, 3.0)

    def test_resolve_truncation_redirect_close_backoff_and_metadata(self):
        closed = []

        class _Closable:
            def close(self):
                closed.append(True)

        with self.assertRaises(ValueError):
            resolve._NoRedirectHandler().redirect_request(None, _Closable(), 302, 'Found', {}, 'https://x.test/')
        self.assertEqual(closed, [True])

        truncated = _FakeResponse(b'[{"partial":')
        truncated.headers = {'Content-Length': '200'}
        complete = _FakeResponse(b'[]')
        complete.headers = {'Content-Length': '2'}
        opener = _FakeOpener([truncated, complete])
        self.assertEqual(resolve.fetch_release_json(attempts=2, retry_delay=0.0, opener=opener), b'[]')
        self.assertEqual(opener.calls, 2)

        self.assertEqual(resolve._retry_delay(2000, 2.0), resolve.MAX_RETRY_AFTER_SECONDS)
        for bad_ver in ('01.0.0.0', '158.00.8127.36', '158.0.08127.36'):
            with self.assertRaises(ValueError):
                resolve.validate([dict(channel='Stable', platform='Android', version=bad_ver,
                                       hashes={'chromium': 'a' * 40})])
        for bad_url in ('https://:443/x', 'https://user:pass@chromiumdash.appspot.com/x'):
            with self.assertRaises(ValueError):
                resolve.fetch_release_json(url=bad_url, opener=_FakeOpener([]))
        with self.assertRaises(ValueError):
            resolve.parse_release_json(b'[NaN]')
        with self.assertRaises(ValueError):
            resolve.validate_run_metadata('bad repo', 'a' * 40, '1')
        with self.assertRaises(ValueError):
            resolve.validate_run_metadata('o/r', 'not-sha', '1')
        with self.assertRaises(ValueError):
            resolve.validate_run_metadata('o/r', 'a' * 40, '0')

    def test_checkpoint_close_failure_symlink_dir_and_overgrowth(self):
        from unittest import mock

        class _BadCloseWriter:
            def __init__(self, real):
                self._real = real

            def write(self, data):
                raise OSError('ENOSPC on write')

            def close(self):
                self._real.close()
                raise OSError('ENOSPC on close')

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dest = base / 'real'
            dest.mkdir()
            orig_open = checkpoint._open_exclusive_wo
            with mock.patch.object(checkpoint, '_open_exclusive_wo',
                                   side_effect=lambda p: _BadCloseWriter(orig_open(p))):
                with self.assertRaises(OSError):
                    checkpoint.pack_stream(dest, io.BytesIO(b'abcd'), chunk_size=4)
            self.assertEqual(list(dest.iterdir()), [])

            sym_dir = base / 'sym'
            sym_dir.symlink_to(dest)
            with self.assertRaises(ValueError):
                checkpoint.pack_stream(sym_dir, io.BytesIO(b'abcd'), chunk_size=4)

            sha, _, _ = checkpoint.pack_stream(dest, io.BytesIO(b'abcd'), chunk_size=4)
            with self.assertRaises(ValueError):
                checkpoint.restore_stream(sym_dir, sha, io.BytesIO())

            orig_verify = checkpoint._verify_and_open

            def grow_after_verify(path, entry):
                f = orig_verify(path, entry)
                with open(path, 'ab') as w:
                    w.write(b'EXTRA')
                return f

            out = io.BytesIO()
            with mock.patch.object(checkpoint, '_verify_and_open', side_effect=grow_after_verify):
                with self.assertRaises(ValueError):
                    checkpoint.restore_stream(dest, sha, out)
            self.assertEqual(out.getvalue(), b'')

    def test_build_multithreaded_zombie_negative_start_and_stage00_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / '123/task/123').mkdir(parents=True)
            (proc / '123/task/124').mkdir(parents=True)
            fields = ['Z', '1', '123', '555'] + ['0'] * 13 + ['2', '0']
            (proc / '123/stat').write_text('123 (lld) ' + ' '.join(fields) + '\n')
            (proc / '123/task/123/stat').write_text('123 (lld) ' + ' '.join(fields) + '\n')
            (proc / '123/task/124/stat').write_text('124 (lld) D 1 123 555\n')
            self.assertEqual(build._live_session_pids(555, proc_root=proc), [123])
            (proc / '123/task/124/stat').write_text('124 (lld) Z 1 123 555\n')
            self.assertEqual(build._live_session_pids(555, proc_root=proc), [])

            (proc / 'chromium-job-start').write_text('\uff11\uff10\n')
            p = subprocess.run([sys.executable, str(ROOT / '.github/scripts/build.py')],
                               env=dict(os.environ, CR_ROOT=str(proc), RUNNER_TEMP=str(proc),
                                        STAGE='01', GITHUB_OUTPUT=str(proc / 'out')),
                               capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('Invalid job start timestamp', p.stderr)

        self.assertIsNone(build.swap_delta_mib(2048, 1024))
        sp = StaticPolicy()
        stage_yml = (ROOT / '.github/workflows/chromium-stage.yml').read_text()
        checkout = int(re.search(r'uses: actions/checkout@.*\n        timeout-minutes: (\d+)', stage_yml)[1])
        stage00 = checkout + sum(sp.step_timeout(name) for name in (
            'Validate control-plane', 'Prepare disposable hosted runner', 'Resolve stable, fetch locked source',
            'Save source resolution'))
        self.assertLessEqual(stage00 * 60 + build.MIN_BUDGET_SECONDS, build.NORMAL_JOB_DEADLINE_SECONDS)
        self.assertLess(stage_yml.index("'chromium-job-start'"), stage_yml.index('uses: actions/checkout@'))
        self.assertNotIn('chromium-job-start', (ROOT / '.github/scripts/host.sh').read_text())
        self.assertIn("if: always() && steps.validate.outcome == 'success'", stage_yml)


if __name__ == '__main__':
    unittest.main()
