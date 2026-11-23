#!/usr/bin/env python3
"""Check that prepared or resumed input matches the locked build provenance."""
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from resolve import MAX_BYTES, SOURCE, URL, VERSION_PATTERN, parse_release_json, validate, validate_run_metadata


REVISION = re.compile(r'[0-9a-f]{40}')
VERSION_FIELDS = ('MAJOR', 'MINOR', 'BUILD', 'PATCH')


def open_regular_file(path, root=None):
    if root is not None:
        if root.is_symlink() or not root.is_dir():
            raise ValueError('Build root must be a real directory')
        resolved_root = root.resolve(strict=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f'Expected regular file: {path.name}')
        if root is not None:
            opened = Path(f'/proc/self/fd/{fd}').resolve(strict=True)
            if not opened.is_relative_to(resolved_root):
                raise ValueError(f'Path resolves outside build tree: {path.name}')
        os.set_blocking(fd, True)
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def read_bounded_bytes(path, limit=MAX_BYTES, root=None):
    with open_regular_file(path, root=root) as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f'Oversized build metadata: {path.name}')
    return raw


def read_metadata_json(path, root=None):
    return parse_release_json(read_bounded_bytes(path, limit=MAX_BYTES, root=root))


def read_lock(root, workspace):
    meta = root / 'metadata'
    if meta.is_symlink() or not meta.is_dir():
        raise ValueError('Build metadata directory must be a real directory')
    lock = read_metadata_json(meta / 'build-lock.json', root=root)
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
    expected_depot = read_bounded_bytes(
        workspace / 'config/depot_tools_revision.txt', root=workspace,
    ).decode('utf-8').strip()
    if not REVISION.fullmatch(expected_depot) or lock.get('depot_tools_revision') != expected_depot:
        raise ValueError('depot_tools pin changed')
    revision = lock.get('chromium_revision')
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise ValueError('Invalid chromium_revision in build-lock.json')
    version = lock.get('version')
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError('Invalid version in build-lock.json')
    if validate(read_metadata_json(meta / 'chromiumdash.json', root=root)) != (version, revision):
        raise ValueError('ChromiumDash snapshot does not match the build lock')
    return lock


def _raise_walk_error(exc):
    raise exc


def verify_source(root, lock):
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Build root must be a real directory')
    resolved_root = root.resolve(strict=True)
    git_env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    git_env.update(
        GIT_CEILING_DIRECTORIES=str(resolved_root),
        GIT_CONFIG_NOSYSTEM='1',
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT='0',
    )
    for directory, field in (('src', 'chromium_revision'), ('depot_tools', 'depot_tools_revision')):
        repo = root / directory
        gitdir = repo / '.git'
        if repo.is_symlink() or not gitdir.is_dir() or gitdir.is_symlink():
            raise ValueError(f'{directory} is missing a real .git directory')
        objects_dir = gitdir / 'objects'
        if objects_dir.is_symlink() or not objects_dir.is_dir():
            raise ValueError(f'{directory} has a symlinked or missing Git objects directory')
        for dirpath, dirnames, filenames in os.walk(gitdir, onerror=_raise_walk_error):
            base = Path(dirpath)
            if any((base / name).is_symlink() for name in (*dirnames, *filenames)):
                raise ValueError(f'{directory} contains a symlink inside .git')
        for alt in ('alternates', 'http-alternates'):
            alt_path = objects_dir / 'info' / alt
            if alt_path.exists() or alt_path.is_symlink():
                raise ValueError(f'{directory} uses external Git object alternates')
        info_attrs = gitdir / 'info/attributes'
        if info_attrs.exists() or info_attrs.is_symlink():
            raise ValueError(f'{directory} uses local Git info/attributes')
        worktree_cfg = gitdir / 'config.worktree'
        if worktree_cfg.exists() or worktree_cfg.is_symlink():
            raise ValueError(f'{directory} uses worktree Git config')
        git = ['/usr/bin/git', '--no-optional-locks', '--no-replace-objects',
               '-c', 'core.hooksPath=/dev/null',
               '-c', 'core.attributesFile=/dev/null',
               '-c', 'core.fsmonitor=false', '-c', 'core.filemode=true',
               '-c', 'core.symlinks=true', '-c', 'core.autocrlf=false',
               '-c', 'core.ignorecase=false', '-C', str(repo)]
        local_cfg = subprocess.check_output(
            [*git, 'config', '--local', '--list', '--name-only', '-z'], env=git_env,
        )
        for cfg_key in local_cfg.split(b'\x00'):
            if cfg_key and re.match(
                rb'^(?:filter|diff|merge|include|includeif)\.|^(?:core\.attributesfile|extensions\.worktreeconfig)$',
                cfg_key,
            ):
                raise ValueError(f'{directory} has unsafe driver configuration in .git/config')
        resolved_gitdir = str(gitdir.resolve())
        fields = subprocess.check_output(
            [*git, 'rev-parse', '--path-format=absolute',
             '--show-toplevel', '--git-dir', '--git-common-dir', 'HEAD^{commit}'],
            env=git_env, text=True,
        ).splitlines()
        if fields != [str(repo.resolve()), resolved_gitdir, resolved_gitdir, lock[field]]:
            raise ValueError(f'{directory} worktree or revision mismatch')
        # Reject assume-unchanged or skip-worktree index bits on non-gitlink files,
        # which would make git status silently ignore modified tracked files.
        indexed = subprocess.check_output([*git, 'ls-files', '-v', '-s', '-z'], env=git_env)
        for entry in indexed.split(b'\x00'):
            if entry and not entry.startswith(b'H ') and not entry[2:].startswith(b'160000 '):
                raise ValueError(f'{directory} has hidden index flags on tracked files')
        # HEAD alone does not prove the working files match. Build outputs,
        # gclient dependency trees and other untracked inputs are not Git changes.
        status = subprocess.check_output(
            [*git, 'status', '--porcelain=v1', '--untracked-files=no', '--ignore-submodules=all'], env=git_env,
        )
        if status:
            raise ValueError(f'{directory} has tracked changes at the locked revision')

    version = {}
    raw_version = read_bounded_bytes(root / 'src/chrome/VERSION', root=root).decode('utf-8')
    for raw_line in raw_version.split('\n'):
        line = raw_line.rstrip('\r')
        stripped = line.lstrip()
        if not stripped or stripped.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep:
            raise ValueError('chrome/VERSION contains a line without "="')
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
    with open_regular_file(root / 'metadata/runners.jsonl', root=root) as history:
        for n in range(int(stage)):
            line = history.readline(16 * 1024 + 1)
            if not line.endswith(b'\n') or len(line) > 16 * 1024:
                raise ValueError(f'Missing, unterminated or oversized checkpoint record for stage {n:02d}')
            record = parse_release_json(line)
            if (not isinstance(record, dict) or record.get('stage') != f'{n:02d}'
                    or record.get('status') != 'running' or record.get('final_stage') is not False):
                raise ValueError(f'Checkpoint must contain running stages 00 through {int(stage) - 1:02d}')
        if history.read(1):
            raise ValueError(f'Checkpoint has unexpected stages for resuming {stage}')
    expected_args = read_bounded_bytes(workspace / 'config/args.gn', root=workspace)
    for args_path in ('src/out/Release/args.gn', 'metadata/args.gn'):
        try:
            actual_args = read_bounded_bytes(root / args_path, limit=max(MAX_BYTES, len(expected_args)), root=root)
        except (OSError, ValueError) as exc:
            raise ValueError(f'Invalid GN arguments file {args_path}: {exc}') from exc
        if actual_args != expected_args:
            raise ValueError(f'GN arguments changed: {args_path}')
    try:
        with open_regular_file(root / 'src/out/Release/build.ninja', root=root):
            pass
    except (OSError, ValueError) as exc:
        raise ValueError('Restored build tree is missing out/Release/build.ninja') from exc
    resolved_root = root.resolve(strict=True)
    for rel_tool in ('src/buildtools/linux64/gn', 'src/third_party/ninja/ninja'):
        tool = root / rel_tool
        if not (tool.is_file() and tool.resolve().is_relative_to(resolved_root) and os.access(tool, os.X_OK)):
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
