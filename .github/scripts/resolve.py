#!/usr/bin/env python3
"""Resolve Android Stable ONCE. No executable code is read from release JSON."""
import datetime
import email.utils
import http.client
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

URL = 'https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Android&num=1'
SOURCE = 'https://chromium.googlesource.com/chromium/src.git'
MAX_BYTES = 1024 * 1024
FETCH_ATTEMPTS = 3
RETRY_BASE_SECONDS = 2.0
RETRYABLE_HTTP_CODES = frozenset({408, 429})
MAX_RETRY_AFTER_SECONDS = 60.0


VERSION_PATTERN = re.compile(r'(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,5})){3}')


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            try:
                fp.close()
            except Exception:
                pass
        raise ValueError(f'Unexpected ChromiumDash redirect ({code}): {newurl}')


def validate(releases):
    if not isinstance(releases, list) or not releases:
        raise ValueError('Empty/invalid ChromiumDash response')
    release = releases[0]
    if not isinstance(release, dict):
        raise ValueError('Invalid ChromiumDash release entry')
    if release.get('channel') != 'Stable' or release.get('platform') != 'Android':
        raise ValueError('Not Android Stable')
    hashes = release.get('hashes')
    if not isinstance(hashes, dict):
        raise ValueError('Missing Chromium hashes')
    version = release.get('version')
    revision = hashes.get('chromium')
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError('Invalid version')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Invalid Chromium commit')
    return version, revision


def _header_values(headers, name):
    if headers is None:
        return []
    if hasattr(headers, 'items'):
        target = name.lower()
        return [v for k, v in headers.items() if isinstance(k, str) and k.lower() == target]
    if hasattr(headers, 'get_all'):
        return list(headers.get_all(name, []))
    if isinstance(headers, (list, tuple)):
        target = name.lower()
        return [
            item[1] for item in headers
            if isinstance(item, tuple) and len(item) == 2
            and isinstance(item[0], str) and item[0].lower() == target
        ]
    if hasattr(headers, 'get'):
        val = headers.get(name)
        return [] if val is None else [val]
    return []


def _header_value(headers, name):
    values = _header_values(headers, name)
    return values[0] if values else None


def _retry_delay(attempt, retry_delay, headers=None, now=None):
    if retry_delay <= 0:
        return 0.0
    values = _header_values(headers, 'Retry-After')
    raw_after = values[0] if len(values) == 1 else None
    if isinstance(raw_after, str):
        text = raw_after.strip()
        if re.fullmatch(r'[0-9]+', text):
            return min(float(text), MAX_RETRY_AFTER_SECONDS)
        if text:
            try:
                target = email.utils.parsedate_to_datetime(text)
            except (TypeError, ValueError, OverflowError):
                target = None
            if target is not None:
                if target.tzinfo is None:
                    target = target.replace(tzinfo=datetime.timezone.utc)
                ref = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
                if ref.tzinfo is None:
                    ref = ref.replace(tzinfo=datetime.timezone.utc)
                delta = (target - ref).total_seconds()
                return max(0.0, min(delta, MAX_RETRY_AFTER_SECONDS))
    return min(retry_delay * (2.0 ** min(attempt - 1, 60)), MAX_RETRY_AFTER_SECONDS)


def _content_length(headers):
    # http.client only understands a single Transfer-Encoding: chunked and
    # reads the first value of each field. Validate framing before trusting it.
    for field in ('Content-Length', 'Transfer-Encoding', 'Content-Encoding'):
        if len(_header_values(headers, field)) > 1:
            raise ValueError(f'Multiple ChromiumDash {field} headers')
    raw_len = _header_value(headers, 'Content-Length')
    encoding = _header_value(headers, 'Transfer-Encoding')
    content_encoding = _header_value(headers, 'Content-Encoding')
    if content_encoding is not None:
        if not isinstance(content_encoding, str) or content_encoding.strip().lower() != 'identity':
            raise ValueError('Unsupported ChromiumDash Content-Encoding')
    if encoding is not None:
        if raw_len is not None:
            raise ValueError('ChromiumDash returned both Content-Length and Transfer-Encoding')
        if not isinstance(encoding, str) or encoding.strip().lower() != 'chunked':
            raise ValueError('Unsupported ChromiumDash Transfer-Encoding')
    if raw_len is None:
        return None
    if not isinstance(raw_len, str):
        raise ValueError('Invalid ChromiumDash Content-Length')
    text = raw_len.strip()
    if not re.fullmatch(r'[0-9]+', text):
        raise ValueError('Invalid ChromiumDash Content-Length')
    text = text.lstrip('0') or '0'
    if len(text) > len(str(MAX_BYTES)) or int(text) > MAX_BYTES:
        raise ValueError('Oversized response')
    return int(text)


def parse_release_json(raw):
    def _reject_constant(token):
        raise ValueError(f'Invalid JSON constant: {token}')

    def _unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('Duplicate field in JSON object')
            obj[key] = value
        return obj

    return json.loads(raw.decode('utf-8'), parse_constant=_reject_constant, object_pairs_hook=_unique_object)


def validate_run_metadata(repository, workflow_sha, run_id):
    if (not isinstance(repository, str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or repository.split('/', 1)[1] in ('.', '..')
            or repository.split('/', 1)[0] in ('.', '..')):
        raise ValueError('Invalid GITHUB_REPOSITORY')
    if not isinstance(workflow_sha, str) or not re.fullmatch(r'[0-9a-f]{40}', workflow_sha):
        raise ValueError('Invalid GITHUB_SHA')
    if not isinstance(run_id, str) or not re.fullmatch(r'[1-9][0-9]*', run_id):
        raise ValueError('Invalid GITHUB_RUN_ID')


def fetch_release_json(url=URL, timeout=90, attempts=FETCH_ATTEMPTS,
                       retry_delay=RETRY_BASE_SECONDS, opener=None):
    if not isinstance(url, str) or any(ord(ch) <= 0x20 or ord(ch) == 0x7f for ch in url):
        raise ValueError('ChromiumDash URL must use https with a bare hostname')
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('ChromiumDash URL has an invalid port') from exc
    if port == 0:
        raise ValueError('ChromiumDash URL has an invalid port')
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.fragment
            or parsed.username is not None or parsed.password is not None):
        raise ValueError('ChromiumDash URL must use https with a bare hostname')
    expected_host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
    expected_netloc = expected_host if port is None else f'{expected_host}:{port}'
    if parsed.netloc.lower() != expected_netloc:
        raise ValueError('ChromiumDash URL has an invalid port')
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError('attempts must be a positive integer')
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('timeout must be positive')
    if (isinstance(retry_delay, bool) or not isinstance(retry_delay, (int, float))
            or not math.isfinite(retry_delay) or retry_delay < 0):
        raise ValueError('retry_delay must be a non-negative finite number')
    if opener is None:
        opener = urllib.request.build_opener(_NoRedirectHandler())
    req = urllib.request.Request(
        url,
        headers={'Accept': 'application/json', 'User-Agent': 'chromium-build-resolver'},
    )
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with opener.open(req, timeout=timeout) as response:
                status = getattr(response, 'status', 200)
                if status != 200:
                    raise ValueError(f'Unexpected ChromiumDash HTTP status: {status}')
                if response.url != url:
                    raise ValueError('Unexpected ChromiumDash redirect')
                expected_len = _content_length(getattr(response, 'headers', None))
                raw = response.read(MAX_BYTES + 1)
                remaining = getattr(response, 'length', None)
            if len(raw) > MAX_BYTES:
                raise ValueError('Oversized response')
            if expected_len is not None and len(raw) < expected_len:
                raise http.client.IncompleteRead(raw, expected_len - len(raw))
            if not isinstance(remaining, bool) and isinstance(remaining, int) and remaining > 0:
                raise http.client.IncompleteRead(raw, remaining)
            if expected_len is not None and len(raw) > expected_len:
                raise ValueError('ChromiumDash response exceeded Content-Length')
            if not raw:
                raise ValueError('Empty ChromiumDash response')
            return raw
        except urllib.error.HTTPError as exc:
            retryable = exc.code in RETRYABLE_HTTP_CODES or exc.code >= 500
            headers = getattr(exc, 'headers', None)
            try:
                exc.close()
            except Exception:
                pass
            if not retryable or attempt == attempts:
                raise
            delay = _retry_delay(attempt, retry_delay, headers)
            last_error = exc
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            if attempt == attempts:
                raise
            delay = _retry_delay(attempt, retry_delay)
            last_error = exc
        print(f'ChromiumDash fetch attempt {attempt}/{attempts} failed ({last_error}); retrying in {delay:.1f}s', file=sys.stderr)
        if delay > 0:
            time.sleep(delay)
    raise last_error


def _write_regular_bytes(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f'Expected regular file: {path.name}')
        os.set_blocking(fd, True)
        os.ftruncate(fd, 0)
        view = memoryview(data)
        written = 0
        while written < len(view):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise OSError(f'Short write to {path.name}')
            written += n
    finally:
        os.close(fd)


def main():
    root = Path(os.environ['CR_ROOT'])
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Build root must be a real directory')
    repository = os.environ['GITHUB_REPOSITORY']
    workflow_sha = os.environ['GITHUB_SHA']
    run_id = os.environ['GITHUB_RUN_ID']
    validate_run_metadata(repository, workflow_sha, run_id)
    depot = (Path(os.environ['GITHUB_WORKSPACE']) / 'config/depot_tools_revision.txt').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'[0-9a-f]{40}', depot):
        raise ValueError('depot_tools must be a full, reviewed commit SHA')
    meta = root / 'metadata'
    if meta.is_symlink():
        raise ValueError('Build metadata directory must not be a symlink')
    meta.mkdir(parents=True, exist_ok=True)
    raw = fetch_release_json()
    version, revision = validate(parse_release_json(raw))
    lock = dict(version=version, chromium_revision=revision, depot_tools_revision=depot,
                source=SOURCE,
                release_api=URL, target='chrome_public_apk', architecture='arm64',
                repository=repository, workflow_sha=workflow_sha,
                run_id=run_id,
                resolved_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    _write_regular_bytes(meta / 'chromiumdash.json', raw)
    _write_regular_bytes(meta / 'build-lock.json', (json.dumps(lock, indent=2) + '\n').encode('utf-8'))
    print(json.dumps(lock, indent=2))


if __name__ == '__main__':
    main()
