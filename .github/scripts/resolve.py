#!/usr/bin/env python3
"""Resolve Android Stable once via ChromiumDash and write the build lock."""
from contextlib import suppress
import datetime
import email.utils
import http.client
import json
import math
import os
from pathlib import Path
import re
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
MAX_RETRY_AFTER_SECONDS = 60.0
RETRYABLE_HTTP_CODES = frozenset({408, 429})
VERSION_PATTERN = re.compile(r'(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,5})){3}')
SHA1_PATTERN = re.compile(r'[0-9a-f]{40}')


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            with suppress(Exception):
                fp.close()
        raise ValueError(f'Unexpected ChromiumDash redirect ({code}): {newurl}')


def validate(releases):
    if not isinstance(releases, list) or not releases or not isinstance(releases[0], dict):
        raise ValueError('Empty or invalid ChromiumDash response')
    release = releases[0]
    if release.get('channel') != 'Stable' or release.get('platform') != 'Android':
        raise ValueError('Not Android Stable')
    hashes = release.get('hashes')
    if not isinstance(hashes, dict):
        raise ValueError('Missing Chromium hashes')
    version = release.get('version')
    revision = hashes.get('chromium')
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError('Invalid version')
    if not isinstance(revision, str) or not SHA1_PATTERN.fullmatch(revision):
        raise ValueError('Invalid Chromium commit')
    return version, revision


def validate_run_metadata(repository, workflow_sha, run_id):
    if (not isinstance(repository, str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or any(part in ('.', '..') for part in repository.split('/', 1))):
        raise ValueError('Invalid GITHUB_REPOSITORY')
    if not isinstance(workflow_sha, str) or not SHA1_PATTERN.fullmatch(workflow_sha):
        raise ValueError('Invalid GITHUB_SHA')
    if not isinstance(run_id, str) or not re.fullmatch(r'[1-9][0-9]*', run_id):
        raise ValueError('Invalid GITHUB_RUN_ID')


def _header_values(headers, name):
    if headers is None:
        return []
    target = name.lower()
    if hasattr(headers, 'items'):
        return [v for k, v in headers.items() if isinstance(k, str) and k.lower() == target]
    if hasattr(headers, 'get_all'):
        return list(headers.get_all(name, []))
    if isinstance(headers, (list, tuple)):
        return [item[1] for item in headers
                if isinstance(item, tuple) and len(item) == 2
                and isinstance(item[0], str) and item[0].lower() == target]
    if hasattr(headers, 'get'):
        val = headers.get(name)
        return [] if val is None else [val]
    return []


def _retry_delay(attempt, base_delay, headers=None, now=None):
    if base_delay <= 0:
        return 0.0
    values = _header_values(headers, 'Retry-After')
    after = values[0] if len(values) == 1 else None
    if isinstance(after, str):
        text = after.strip()
        if text.isascii() and text.isdigit():
            return min(float(text), MAX_RETRY_AFTER_SECONDS)
        if text:
            try:
                target = email.utils.parsedate_to_datetime(text)
            except (TypeError, ValueError, OverflowError):
                target = None
            if target is not None:
                if target.tzinfo is None:
                    target = target.replace(tzinfo=datetime.timezone.utc)
                ref = datetime.datetime.now(datetime.timezone.utc) if now is None else now
                if ref.tzinfo is None:
                    ref = ref.replace(tzinfo=datetime.timezone.utc)
                return max(0.0, min((target - ref).total_seconds(), MAX_RETRY_AFTER_SECONDS))
    return min(base_delay * (2.0 ** min(attempt - 1, 60)), MAX_RETRY_AFTER_SECONDS)


def _expected_content_length(headers):
    for field in ('Content-Length', 'Transfer-Encoding', 'Content-Encoding'):
        if len(_header_values(headers, field)) > 1:
            raise ValueError(f'Multiple ChromiumDash {field} headers')
    content_enc = (_header_values(headers, 'Content-Encoding') or [None])[0]
    if content_enc is not None:
        if not isinstance(content_enc, str) or content_enc.strip().lower() != 'identity':
            raise ValueError('Unsupported ChromiumDash Content-Encoding')
    transfer_enc = (_header_values(headers, 'Transfer-Encoding') or [None])[0]
    raw_len = (_header_values(headers, 'Content-Length') or [None])[0]
    if transfer_enc is not None:
        if raw_len is not None:
            raise ValueError('ChromiumDash returned both Content-Length and Transfer-Encoding')
        if not isinstance(transfer_enc, str) or transfer_enc.strip().lower() != 'chunked':
            raise ValueError('Unsupported ChromiumDash Transfer-Encoding')
    if raw_len is None:
        return None
    if not isinstance(raw_len, str):
        raise ValueError('Invalid ChromiumDash Content-Length')
    text = raw_len.strip()
    if not text.isascii() or not text.isdigit():
        raise ValueError('Invalid ChromiumDash Content-Length')
    size = int(text)
    if size > MAX_BYTES:
        raise ValueError('Oversized response')
    return size


def fetch_release_json(url=URL, timeout=90, attempts=FETCH_ATTEMPTS,
                       retry_delay=RETRY_BASE_SECONDS, opener=None):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.fragment
            or parsed.username is not None or parsed.password is not None):
        raise ValueError('ChromiumDash URL must use https with a bare hostname')
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError('attempts must be a positive integer')
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('timeout must be a positive finite number')
    if (isinstance(retry_delay, bool) or not isinstance(retry_delay, (int, float))
            or not math.isfinite(retry_delay) or retry_delay < 0):
        raise ValueError('retry_delay must be a non-negative finite number')
    if opener is None:
        opener = urllib.request.build_opener(_NoRedirectHandler())
    req = urllib.request.Request(
        url,
        headers={'Accept': 'application/json', 'User-Agent': 'chromium-build-resolver'},
    )
    for attempt in range(1, attempts + 1):
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, 'status', 200)
                if status != 200:
                    raise ValueError(f'Unexpected ChromiumDash HTTP status: {status}')
                if resp.url != url:
                    raise ValueError('Unexpected ChromiumDash redirect')
                expected_len = _expected_content_length(getattr(resp, 'headers', None))
                raw = resp.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('Oversized response')
            if expected_len is not None:
                if len(raw) < expected_len:
                    raise http.client.IncompleteRead(raw, expected_len - len(raw))
                if len(raw) > expected_len:
                    raise ValueError('ChromiumDash response exceeded Content-Length')
            if not raw:
                raise ValueError('Empty ChromiumDash response')
            return raw
        except urllib.error.HTTPError as exc:
            retryable = exc.code in RETRYABLE_HTTP_CODES or 500 <= exc.code < 600
            headers = getattr(exc, 'headers', None)
            with suppress(Exception):
                exc.close()
            if not retryable or attempt == attempts:
                raise
            delay = _retry_delay(attempt, retry_delay, headers)
            last_error = exc
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            if attempt == attempts:
                raise
            delay = _retry_delay(attempt, retry_delay)
            last_error = exc
        print(f'ChromiumDash fetch attempt {attempt}/{attempts} failed ({last_error}); '
              f'retrying in {delay:.1f}s', file=sys.stderr)
        if delay > 0:
            time.sleep(delay)
    raise AssertionError('unreachable')


def main():
    root = Path(os.environ['CR_ROOT'])
    repository = os.environ['GITHUB_REPOSITORY']
    workflow_sha = os.environ['GITHUB_SHA']
    run_id = os.environ['GITHUB_RUN_ID']
    validate_run_metadata(repository, workflow_sha, run_id)
    depot = (Path(os.environ['GITHUB_WORKSPACE']) / 'config/depot_tools_revision.txt').read_text(encoding='utf-8').strip()
    if not SHA1_PATTERN.fullmatch(depot):
        raise ValueError('depot_tools must be a full, reviewed commit SHA')
    meta = root / 'metadata'
    meta.mkdir(parents=True, exist_ok=True)
    raw = fetch_release_json()
    version, revision = validate(json.loads(raw.decode('utf-8')))
    lock = {
        'version': version,
        'chromium_revision': revision,
        'depot_tools_revision': depot,
        'source': SOURCE,
        'release_api': URL,
        'target': 'chrome_public_apk',
        'architecture': 'arm64',
        'repository': repository,
        'workflow_sha': workflow_sha,
        'run_id': run_id,
        'resolved_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    (meta / 'chromiumdash.json').write_bytes(raw)
    text = json.dumps(lock, indent=2) + '\n'
    (meta / 'build-lock.json').write_text(text, encoding='utf-8')
    print(text, end='')


if __name__ == '__main__':
    main()
