#!/usr/bin/env python3
"""Check that prepared or resumed input matches the locked build provenance."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from resolve import SHA1_PATTERN, SOURCE, URL, VERSION_PATTERN, validate, validate_run_metadata

VERSION_FIELDS = ('MAJOR', 'MINOR', 'BUILD', 'PATCH')


def read_lock(root, workspace):
    meta = root / 'metadata'
    lock = json.loads((meta / 'build-lock.json').read_text(encoding='utf-8'))
    if not isinstance(lock, dict):
        raise ValueError('Invalid build-lock.json')
    repository = os.environ['GITHUB_REPOSITORY']
    workflow_sha = os.environ['GITHUB_SHA']
    run_id = os.environ['GITHUB_RUN_ID']
    validate_run_metadata(repository, workflow_sha, run_id)
    for field, expected in (('repository', repository), ('workflow_sha', workflow_sha), ('run_id', run_id)):
        if lock.get(field) != expected:
            raise ValueError(f'Build lock {field} does not match this workflow run')
    if (lock.get('source') != SOURCE or lock.get('release_api') != URL
            or lock.get('target') != 'chrome_public_apk' or lock.get('architecture') != 'arm64'):
        raise ValueError('Build lock source, target, architecture, or release API mismatch')
    expected_depot = (workspace / 'config/depot_tools_revision.txt').read_text(encoding='utf-8').strip()
    if not SHA1_PATTERN.fullmatch(expected_depot) or lock.get('depot_tools_revision') != expected_depot:
        raise ValueError('depot_tools pin changed')
    version, revision = lock.get('version'), lock.get('chromium_revision')
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError('Invalid version in build-lock.json')
    if not isinstance(revision, str) or not SHA1_PATTERN.fullmatch(revision):
        raise ValueError('Invalid chromium_revision in build-lock.json')
    snapshot = json.loads((meta / 'chromiumdash.json').read_text(encoding='utf-8'))
    if validate(snapshot) != (version, revision):
        raise ValueError('ChromiumDash snapshot does not match build-lock.json')
    return lock


def verify_source(root, lock):
    git_env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    git_env.update(
        GIT_CONFIG_NOSYSTEM='1',
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT='0',
    )
    for directory, field in (('src', 'chromium_revision'), ('depot_tools', 'depot_tools_revision')):
        repo = root / directory
        gitdir = repo / '.git'
        if repo.is_symlink() or gitdir.is_symlink() or not gitdir.is_dir():
            raise ValueError(f'{directory} is missing a real .git directory')
        git = ['/usr/bin/git', '--no-optional-locks', '--no-replace-objects',
               '-c', 'core.hooksPath=/dev/null', '-C', str(repo)]
        out = subprocess.check_output(
            [*git, 'rev-parse', '--path-format=absolute', '--show-toplevel', 'HEAD^{commit}'],
            env=git_env, text=True,
        ).splitlines()
        if out != [str(repo.resolve()), lock[field]]:
            raise ValueError(f'{directory} worktree or revision mismatch')
        status = subprocess.check_output(
            [*git, 'status', '--porcelain=v1', '--untracked-files=no', '--ignore-submodules=all'],
            env=git_env,
        )
        if status:
            raise ValueError(f'{directory} has tracked changes at the locked revision')

    fields = {}
    for raw_line in (root / 'src/chrome/VERSION').read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep:
            raise ValueError('chrome/VERSION contains a line without "="')
        key = key.strip()
        if key in VERSION_FIELDS:
            fields[key] = value.strip()
    if any(k not in fields for k in VERSION_FIELDS):
        raise ValueError('chrome/VERSION is missing required version fields')
    actual = '.'.join(fields[k] for k in VERSION_FIELDS)
    if actual != lock['version']:
        raise ValueError(f"chrome/VERSION mismatch: {actual!r} != {lock['version']!r}")


def verify_restore(root, workspace, stage):
    if not re.fullmatch(r'0[1-8]', stage):
        raise ValueError('Invalid restore STAGE')
    expected_count = int(stage)
    raw = (root / 'metadata/runners.jsonl').read_text(encoding='utf-8')
    if raw and not raw.endswith('\n'):
        raise ValueError('runners.jsonl is missing trailing newline')
    lines = raw.splitlines()
    if len(lines) != expected_count:
        raise ValueError(f'Expected {expected_count} prior stage record(s), got {len(lines)}')
    for n, line in enumerate(lines):
        record = json.loads(line)
        if (not isinstance(record, dict) or record.get('stage') != f'{n:02d}'
                or record.get('status') != 'running' or record.get('final_stage') is not False):
            raise ValueError(f'Checkpoint must contain running stages 00 through {expected_count - 1:02d}')
    expected_args = (workspace / 'config/args.gn').read_bytes()
    for rel in ('src/out/Release/args.gn', 'metadata/args.gn'):
        p = root / rel
        if p.is_symlink() or not p.is_file() or p.read_bytes() != expected_args:
            raise ValueError(f'GN arguments mismatch: {rel}')
    build_ninja = root / 'src/out/Release/build.ninja'
    if build_ninja.is_symlink() or not build_ninja.is_file():
        raise ValueError('Restored build tree is missing out/Release/build.ninja')
    resolved_root = root.resolve(strict=True)
    for rel in ('src/buildtools/linux64/gn', 'src/third_party/ninja/ninja'):
        tool = root / rel
        if not (tool.is_file() and tool.resolve().is_relative_to(resolved_root) and os.access(tool, os.X_OK)):
            raise ValueError(f'Restored build tree is missing executable {rel[4:]}')


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('read-lock', 'verify-source', 'verify-restore'):
        raise SystemExit('Usage: source_state.py <read-lock|verify-source|verify-restore>')
    mode = sys.argv[1]
    root = Path(os.environ['CR_ROOT'])
    workspace = Path(os.environ['GITHUB_WORKSPACE'])
    lock = read_lock(root, workspace)
    if mode == 'read-lock':
        print(f"{lock['version']}\n{lock['chromium_revision']}\n{lock['depot_tools_revision']}")
    else:
        verify_source(root, lock)
        if mode == 'verify-restore':
            verify_restore(root, workspace, os.environ.get('STAGE', ''))


if __name__ == '__main__':
    try:
        main()
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Source provenance check failed: {exc}') from exc
