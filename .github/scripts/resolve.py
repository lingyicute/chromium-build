#!/usr/bin/env python3
"""Resolve Android Stable once via ChromiumDash and write the build lock."""
import datetime
import http.client
import json
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


def _get_header(headers, name):
    if headers is None:
        return None
    if hasattr(headers, 'items'):
        target = name.lower()
        for k, v in headers.items():
            if isinstance(k, str) and k.lower() == target:
                return v
        return None
    return headers.get(name) if hasattr(headers, 'get') else None


def _retry_delay(attempt, base_delay, headers=None):
    if base_delay <= 0:
        return 0.0
    after = _get_header(headers, 'Retry-After')
    if isinstance(after, str) and after.strip().isdigit():
        return min(float(after.strip()), MAX_RETRY_AFTER_SECONDS)
    return min(base_delay * (2.0 ** (attempt - 1)), MAX_RETRY_AFTER_SECONDS)


def fetch_release_json(url=URL, timeout=90, attempts=FETCH_ATTEMPTS,
                       retry_delay=RETRY_BASE_SECONDS, opener=None):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.fragment
            or parsed.username is not None or parsed.password is not None):
        raise ValueError('ChromiumDash URL must use https with a bare hostname')
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
                headers = getattr(resp, 'headers', None)
                raw_len = _get_header(headers, 'Content-Length')
                expected_len = None
                if raw_len is not None:
                    text = str(raw_len).strip()
                    if not text.isascii() or not text.isdigit():
                        raise ValueError('Invalid ChromiumDash Content-Length')
                    expected_len = int(text)
                    if expected_len > MAX_BYTES:
                        raise ValueError('Oversized response')
                raw = resp.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('Oversized response')
            if expected_len is not None and len(raw) != expected_len:
                raise http.client.IncompleteRead(raw, max(expected_len - len(raw), 0))
            if not raw:
                raise ValueError('Empty ChromiumDash response')
            return raw
        except urllib.error.HTTPError as exc:
            retryable = exc.code in RETRYABLE_HTTP_CODES or 500 <= exc.code < 600
            headers = getattr(exc, 'headers', None)
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
