#!/usr/bin/env python3
"""Resolve Android Stable ONCE. No executable code is read from release JSON."""
import datetime
import http.client
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

URL = 'https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Android&num=1'
MAX_BYTES = 1024 * 1024
FETCH_ATTEMPTS = 3
RETRY_BASE_SECONDS = 2.0
RETRYABLE_HTTP_CODES = frozenset({408, 429})
MAX_RETRY_AFTER_SECONDS = 60.0


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
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
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,6}){3}', version):
        raise ValueError('Invalid version')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Invalid Chromium commit')
    return version, revision


def _retry_delay(attempt, retry_delay, headers=None):
    if retry_delay <= 0:
        return 0.0
    if headers is not None:
        raw_after = headers.get('Retry-After') if hasattr(headers, 'get') else None
        if isinstance(raw_after, str) and re.fullmatch(r'[0-9]+', raw_after.strip()):
            return min(float(raw_after.strip()), MAX_RETRY_AFTER_SECONDS)
    return retry_delay * (2 ** (attempt - 1))


def fetch_release_json(url=URL, timeout=90, attempts=FETCH_ATTEMPTS,
                       retry_delay=RETRY_BASE_SECONDS, opener=None):
    if attempts < 1:
        raise ValueError('attempts must be positive')
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
                if response.url != url:
                    raise ValueError('Unexpected ChromiumDash redirect')
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('Oversized response')
            return raw
        except urllib.error.HTTPError as exc:
            retryable = exc.code in RETRYABLE_HTTP_CODES or exc.code >= 500
            if not retryable or attempt == attempts:
                raise
            delay = _retry_delay(attempt, retry_delay, getattr(exc, 'headers', None))
            try:
                exc.close()
            except Exception:
                pass
            last_error = exc
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            if attempt == attempts:
                raise
            delay = _retry_delay(attempt, retry_delay)
            last_error = exc
        if delay > 0:
            time.sleep(delay)
    raise last_error


def main():
    root = Path(os.environ['CR_ROOT'])
    meta = root / 'metadata'
    meta.mkdir(parents=True, exist_ok=True)
    raw = fetch_release_json()
    version, revision = validate(json.loads(raw))
    depot = (Path(os.environ['GITHUB_WORKSPACE']) / 'config/depot_tools_revision.txt').read_text().strip()
    if not re.fullmatch(r'[0-9a-f]{40}', depot):
        raise ValueError('depot_tools must be a full, reviewed commit SHA')
    lock = dict(version=version, chromium_revision=revision, depot_tools_revision=depot,
                source='https://chromium.googlesource.com/chromium/src.git',
                release_api=URL, target='chrome_public_apk', architecture='arm64',
                repository=os.environ['GITHUB_REPOSITORY'], workflow_sha=os.environ['GITHUB_SHA'],
                run_id=os.environ['GITHUB_RUN_ID'],
                resolved_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    (meta / 'chromiumdash.json').write_bytes(raw)
    (meta / 'build-lock.json').write_text(json.dumps(lock, indent=2) + '\n')
    print(json.dumps(lock, indent=2))


if __name__ == '__main__':
    main()
