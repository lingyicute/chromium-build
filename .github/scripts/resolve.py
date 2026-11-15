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
import sys
import time
import urllib.error
import urllib.parse
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


def _header_value(headers, name):
    if headers is None:
        return None
    if hasattr(headers, 'get'):
        val = headers.get(name)
        if val is not None:
            return val
    if hasattr(headers, 'items'):
        target = name.lower()
        for k, v in headers.items():
            if isinstance(k, str) and k.lower() == target:
                return v
    return None


def _retry_delay(attempt, retry_delay, headers=None, now=None):
    if retry_delay <= 0:
        return 0.0
    raw_after = _header_value(headers, 'Retry-After')
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
                delta = (target - ref).total_seconds()
                return max(0.0, min(delta, MAX_RETRY_AFTER_SECONDS))
    return retry_delay * (2 ** (attempt - 1))


def fetch_release_json(url=URL, timeout=90, attempts=FETCH_ATTEMPTS,
                       retry_delay=RETRY_BASE_SECONDS, opener=None):
    parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    if parsed is None or parsed.scheme != 'https' or not parsed.netloc:
        raise ValueError('ChromiumDash URL must use https')
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
                raw = response.read(MAX_BYTES + 1)
            if not raw:
                raise ValueError('Empty ChromiumDash response')
            if len(raw) > MAX_BYTES:
                raise ValueError('Oversized response')
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
