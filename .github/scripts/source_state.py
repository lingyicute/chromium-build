#!/usr/bin/env python3
"""Check that prepared or resumed input matches the locked build provenance."""
import os
from pathlib import Path
import re
import subprocess
import sys

from resolve import SOURCE, URL, VERSION_PATTERN, parse_release_json, validate_run_metadata


REVISION = re.compile(r'[0-9a-f]{40}')
VERSION_FIELDS = ('MAJOR', 'MINOR', 'BUILD', 'PATCH')


def read_lock(root, workspace):
    lock = parse_release_json((root / 'metadata/build-lock.json').read_bytes())
    if not isinstance(lock, dict):
        raise ValueError('Invalid build-lock.json')
    repository, workflow_sha, run_id = (os.environ[key] for key in
                                        ('GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_RUN_ID'))
    validate_run_metadata(repository, workflow_sha, run_id)
    for field, expected in (('repository', repository), ('workflow_sha', workflow_sha), ('run_id', run_id)):
        if lock.get(field) != expected:
            raise ValueError(f'Build lock {field} is from another run/repository/commit')
    if (lock.get('source') != SOURCE or lock.get('release_api') != URL
            or lock.get('target') != 'chrome_public_apk' or lock.get('architecture') != 'arm64'):
        raise ValueError('Build lock source/target/architecture or release API mismatch')
    expected_depot = (workspace / 'config/depot_tools_revision.txt').read_text(encoding='utf-8').strip()
    if not REVISION.fullmatch(expected_depot) or lock.get('depot_tools_revision') != expected_depot:
        raise ValueError('depot_tools pin changed')
    revision = lock.get('chromium_revision')
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise ValueError('Invalid chromium_revision in build-lock.json')
    version = lock.get('version')
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError('Invalid version in build-lock.json')
    return lock


def verify_source(root, lock):
    git_env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(root))
    for directory, field in (('src', 'chromium_revision'), ('depot_tools', 'depot_tools_revision')):
        repo = root / directory
        gitdir = repo / '.git'
        if repo.is_symlink() or not gitdir.is_dir() or gitdir.is_symlink():
            raise ValueError(f'{directory} is missing a real .git directory')
        fields = subprocess.check_output(
            ['/usr/bin/git', '-C', str(repo), 'rev-parse', '--show-toplevel', 'HEAD^{commit}'],
            env=git_env, text=True,
        ).splitlines()
        if fields != [str(repo.resolve()), lock[field]]:
            raise ValueError(f'{directory} worktree or revision mismatch')

    version = {}
    for line in (root / 'src/chrome/VERSION').read_text(encoding='utf-8').splitlines():
        if line.lstrip().startswith('#'):
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        if key not in VERSION_FIELDS:
            continue
        if key in version:
            raise ValueError(f'chrome/VERSION duplicates field: {key}')
        version[key] = value.strip()
    for key in VERSION_FIELDS:
        if key not in version:
            raise ValueError(f'chrome/VERSION missing field: {key}')
    actual = '.'.join(version[key] for key in VERSION_FIELDS)
    if actual != lock['version']:
        raise ValueError(f"chrome/VERSION mismatch: {actual!r} != {lock['version']!r}")


def verify_restore(root, workspace, stage):
    if not re.fullmatch(r'0[1-8]', stage):
        raise ValueError('Invalid restore STAGE')
    with (root / 'metadata/runners.jsonl').open('rb') as history:
        for n in range(int(stage)):
            line = history.readline(16 * 1024 + 1)
            if not line or len(line) > 16 * 1024:
                raise ValueError(f'Missing or oversized checkpoint record for stage {n:02d}')
            record = parse_release_json(line)
            if (not isinstance(record, dict) or record.get('stage') != f'{n:02d}'
                    or record.get('status') != 'running' or record.get('final_stage') is not False):
                raise ValueError(f'Checkpoint must contain running stages 00 through {int(stage) - 1:02d}')
        if history.read(1):
            raise ValueError(f'Checkpoint has unexpected stages for resuming {stage}')
    if (root / 'src/out/Release/args.gn').read_bytes() != (workspace / 'config/args.gn').read_bytes():
        raise ValueError('GN arguments changed')
    if not (root / 'src/out/Release/build.ninja').is_file():
        raise ValueError('Restored build tree is missing out/Release/build.ninja')
    for rel_tool in ('src/buildtools/linux64/gn', 'src/third_party/ninja/ninja'):
        tool = root / rel_tool
        if not (tool.is_file() and tool.resolve().is_relative_to(root.resolve()) and os.access(tool, os.X_OK)):
            raise ValueError(f'Restored build tree is missing executable {rel_tool[4:]} within the tree')


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('read-lock', 'verify-source', 'verify-restore'):
        raise SystemExit('Usage: source_state.py <read-lock|verify-source|verify-restore>')
    mode = sys.argv[1]
    root = Path(os.environ['CR_ROOT'])
    workspace = Path(os.environ['GITHUB_WORKSPACE'])
    lock = read_lock(root, workspace)
    if mode == 'read-lock':
        for field in ('version', 'chromium_revision', 'depot_tools_revision'):
            print(lock[field])
    else:
        verify_source(root, lock)
        if mode == 'verify-restore':
            verify_restore(root, workspace, os.environ.get('STAGE', ''))


if __name__ == '__main__':
    try:
        main()
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Source provenance check failed: {exc}') from exc
