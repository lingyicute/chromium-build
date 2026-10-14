#!/usr/bin/env python3
"""Resolve Android Stable ONCE. No executable code is read from release JSON."""
import datetime
import json
import os
from pathlib import Path
import re
import urllib.request

URL = 'https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Android&num=1'

def validate(releases):
    if not isinstance(releases, list) or not releases:
        raise ValueError('Empty/invalid ChromiumDash response')
    release = releases[0]
    if release.get('channel') != 'Stable' or release.get('platform') != 'Android':
        raise ValueError('Not Android Stable')
    version = release.get('version', '')
    revision = release.get('hashes', {}).get('chromium', '')
    if not re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,6}){3}', version):
        raise ValueError('Invalid version')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Invalid Chromium commit')
    return version, revision

def main():
    root = Path(os.environ['CR_ROOT'])
    meta = root / 'metadata'
    meta.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(URL, timeout=90) as response:
        if response.url != URL:
            raise ValueError('Unexpected ChromiumDash redirect')
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('Oversized response')
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
    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
        f.write(f'version={version}\n')
    print(json.dumps(lock, indent=2))

if __name__ == '__main__':
    main()
