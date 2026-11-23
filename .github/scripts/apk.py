#!/usr/bin/env python3
"""Stage and inspect the APK that will be uploaded by the workflow."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import zipfile


MAX_LOCK_BYTES = 1024 * 1024
VERSION_PATTERN = re.compile(r'(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,5})){3}')
PACKAGE_ATTR = r"[a-zA-Z][a-zA-Z0-9]*='[^']*'"


def _open_regular_ro(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


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


def _read_bounded_text(path, encoding, label):
    with _open_regular_ro(path) as src:
        st = os.fstat(src.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f'{label} must be a regular file')
        os.set_blocking(src.fileno(), True)
        raw = src.read(MAX_LOCK_BYTES + 1)
    if len(raw) > MAX_LOCK_BYTES:
        raise ValueError(f'Oversized {label}')
    return raw.decode(encoding)


def _read_lock_version(lock_path):
    text = _read_bounded_text(lock_path, 'utf-8', 'build-lock.json')

    def _reject_constant(token):
        raise ValueError(f'Invalid JSON constant in build-lock.json: {token}')

    def _unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('Duplicate field in build-lock.json')
            obj[key] = value
        return obj

    lock = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    if not isinstance(lock, dict):
        raise ValueError('Invalid build-lock.json')
    version = lock.get('version')
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError('Invalid version in build-lock.json')
    return version


def stage_apk(root, source, destination):
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Build root must be a real directory')
    # Do not follow a symlink (or block opening a FIFO) in place of the APK.
    with _open_regular_ro(source) as src:
        before = os.fstat(src.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise ValueError('Expected a nonempty regular ChromePublic.apk')
        os.set_blocking(src.fileno(), True)
        opened_path = Path(f'/proc/self/fd/{src.fileno()}').resolve(strict=True)
        if not opened_path.is_relative_to(root.resolve(strict=True)):
            raise ValueError('ChromePublic.apk resolves outside the build tree')
        # A diagnostic upload must not find a partial file if this step fails.
        with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
            candidate = Path(tmp) / destination.name
            remaining = before.st_size
            with candidate.open('xb') as dst:
                # Do not chase EOF on a file that is concurrently being extended.
                while remaining:
                    block = src.read(min(remaining, 1024 * 1024))
                    if not block:
                        break
                    dst.write(block)
                    remaining -= len(block)
            after = os.fstat(src.fileno())
            if (remaining or after.st_size != before.st_size or after.st_nlink < 1
                    or after.st_dev != before.st_dev or after.st_ino != before.st_ino
                    or after.st_mtime_ns != before.st_mtime_ns or after.st_ctime_ns != before.st_ctime_ns):
                raise ValueError('ChromePublic.apk changed while staging')
            candidate.chmod(0o400)
            candidate.replace(destination)


def inspect_signer(log, pin):
    expected = _read_bounded_text(pin, 'ascii', 'certificate SHA-256 pin').strip()
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('Invalid development certificate SHA-256 pin')
    trace = _read_bounded_text(log, 'utf-8', 'apksigner log')
    counts = re.findall(r'^Number of signers: ([0-9]+)[ \t]*\r?$', trace, re.M)
    digest_lines = re.findall(r'^[^\r\n]*certificate SHA-256 digest:[^\r\n]*$', trace, re.M)
    hashes = re.findall(r'^Signer #([1-9][0-9]*) certificate SHA-256 digest: ([0-9a-fA-F]{64})[ \t]*\r?$',
                        trace, re.M)
    other_signers = re.findall(r'^Signer #(?!1\b)\S+', trace, re.M)
    source_stamp = re.search(r'^(?:Verified for SourceStamp:[ \t]*true\b|Source ?Stamp Signer\b)', trace, re.M)
    explicit_error = re.search(r'^(?:DOES NOT VERIFY\b|ERROR:)', trace, re.M)
    modern_schemes = re.findall(r'^Verified using v(?:2|3(?:\.1)?) scheme \([^)\r\n]+\):[ \t]*(true|false)[ \t]*\r?$',
                                trace, re.M)
    if (counts != ['1'] or len(digest_lines) != 1 or len(hashes) != 1 or other_signers
            or source_stamp is not None or explicit_error is not None or 'true' not in modern_schemes
            or hashes[0][0] != '1' or hashes[0][1].lower() != expected):
        raise ValueError('APK does not have exactly the pinned Chromium development signer')


def _validate_dex(zf, info):
    with zf.open(info.filename) as dex:
        header = dex.read(0x78)
    file_size = int.from_bytes(header[32:36], 'little')
    header_size = int.from_bytes(header[36:40], 'little')
    if (not re.fullmatch(rb'dex\n[0-9]{3}\x00', header[:8]) or header_size not in (0x70, 0x78)
            or len(header) < header_size or not header_size <= file_size <= info.file_size
            or header[40:44] != b'\x78\x56\x34\x12'):
        raise ValueError(f'APK root {info.filename} has an invalid or truncated DEX header')


def inspect_zip(apk_path):
    with _open_regular_ro(apk_path) as apk_file:
        st = os.fstat(apk_file.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ValueError('APK must be a regular file')
        os.set_blocking(apk_file.fileno(), True)
        with zipfile.ZipFile(apk_file) as zf:
            entries = zf.infolist()
            names = {info.filename: info for info in entries}
            if len(names) != len(entries) or any(info.filename != info.orig_filename for info in entries):
                raise ValueError('APK contains duplicate or ambiguous ZIP entry names')
            for name, info in names.items():
                trimmed = name[:-1] if info.is_dir() else name
                if (not trimmed or name.startswith('/') or '\\' in name
                        or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in name)
                        or any(part in ('', '.', '..') for part in trimmed.split('/'))):
                    raise ValueError(f'APK contains unsafe ZIP entry path: {name!r}')
                mode_type = stat.S_IFMT(info.external_attr >> 16)
                if mode_type not in (0, stat.S_IFDIR if info.is_dir() else stat.S_IFREG):
                    raise ValueError(f'APK contains irregular ZIP entry mode: {name!r}')
            for name in ('AndroidManifest.xml', 'classes.dex'):
                if name not in names or names[name].is_dir() or names[name].file_size <= 0:
                    raise ValueError(f'APK missing non-empty root {name}')
            root_dex = [name for name in names if '/' not in name.rstrip('/') and name.rstrip('/').endswith('.dex')]
            expected_dex = ['classes.dex', *(f'classes{i}.dex' for i in range(2, len(root_dex) + 1))]
            if len(root_dex) != len(expected_dex) or set(root_dex) != set(expected_dex):
                raise ValueError('APK root DEX files must form a contiguous classes*.dex sequence')
            for dex_name in expected_dex:
                if names[dex_name].is_dir() or names[dex_name].file_size <= 0:
                    raise ValueError(f'APK root {dex_name} has an invalid or truncated DEX header')
                _validate_dex(zf, names[dex_name])

            libs = []
            for name in sorted(names):
                if not name.startswith('lib/'):
                    continue
                info = names[name]
                if info.is_dir():
                    if name not in ('lib/', 'lib/arm64-v8a/'):
                        raise ValueError(f'APK contains a native library directory outside arm64-v8a: {name!r}')
                    continue
                if not name.startswith('lib/arm64-v8a/'):
                    raise ValueError(f'APK contains a native library outside arm64-v8a: {name!r}')
                if not re.fullmatch(r'lib/arm64-v8a/[^/]+\.so', name):
                    raise ValueError(f'APK missing valid arm64-v8a native library layout: {name!r}')
                libs.append(name)
            if not libs:
                raise ValueError('APK missing non-empty arm64-v8a native library')
            for name in libs:
                with zf.open(name) as lib:
                    header = lib.read(64)
                # Require a complete ELF64 little-endian ET_DYN header for EM_AARCH64.
                if (len(header) != 64 or header[:7] != b'\x7fELF\x02\x01\x01'
                        or header[16:24] != b'\x03\x00\xb7\x00\x01\x00\x00\x00' or header[52:54] != b'\x40\x00'):
                    raise ValueError(f'APK has a truncated ELF header or a non-AArch64 shared object: {name!r}')


def inspect_badging(aapt2, apk_path, locked_version):
    if not (aapt2.is_file() and os.access(aapt2, os.X_OK)):
        raise ValueError('Chromium-managed Android build-tools aapt2 missing')
    with _open_regular_ro(apk_path) as apk_file:
        if not stat.S_ISREG(os.fstat(apk_file.fileno()).st_mode):
            raise ValueError('APK must be a regular file')
    badging = subprocess.check_output([str(aapt2), 'dump', 'badging', str(apk_path)],
                                      text=True, encoding='utf-8')
    package_lines = re.findall(r'^package:([^\r\n]*)\r?$', badging, re.M)
    if len(package_lines) != 1:
        raise ValueError('aapt2 output must contain exactly one package line')
    if not re.fullmatch(rf'[ \t]*{PACKAGE_ATTR}(?:[ \t]+{PACKAGE_ATTR})*[ \t]*', package_lines[0]):
        raise ValueError('aapt2 package line contains malformed attributes')
    fields = re.findall(r"(?:^|[ \t]+)([a-zA-Z][a-zA-Z0-9]*)='([^']*)'", package_lines[0])
    attrs = dict(fields)
    if len(attrs) != len(fields):
        raise ValueError('aapt2 package line has duplicate fields')
    expected_package = 'org.chromium.chrome'  # config/args.gn
    if attrs.get('name') != expected_package:
        raise ValueError(f'APK package name must be {expected_package}')
    if attrs.get('versionName') != locked_version:
        raise ValueError(f"APK versionName mismatch: {attrs.get('versionName')!r} != {locked_version!r}")
    if not re.fullmatch(r'[1-9][0-9]*', attrs.get('versionCode', '')):
        raise ValueError('APK missing a positive versionCode')
    if re.search(r'^application-debuggable\b', badging, re.M):
        raise ValueError('APK must not be debuggable (android:debuggable=true)')
    if re.search(r'^(?:testOnly=|application-is-test-only\b)', badging, re.M):
        raise ValueError('APK must not be marked testOnly')
    abi_lines = re.findall(r'^native-code:([^\r\n]*)\r?$', badging, re.M)
    if not abi_lines:
        raise ValueError('aapt2 output missing native-code line')
    if (len(abi_lines) != 1 or not re.fullmatch(r"[ \t]*'arm64-v8a'[ \t]*", abi_lines[0])
            or re.search(r'^alt-native-code:', badging, re.M)):
        raise ValueError('APK native-code must declare only arm64-v8a')

    def _parse_sdk_field(tag):
        raw_lines = re.findall(rf'^{tag}:([^\r\n]*)\r?$', badging, re.M)
        if len(raw_lines) > 1:
            raise ValueError('aapt2 output contains duplicate SDK version lines')
        if not raw_lines:
            return None
        m = re.fullmatch(r"'([^'\r\n]+)'[ \t]*\r?", raw_lines[0])
        if not m:
            raise ValueError(f'aapt2 output has malformed {tag} line')
        return m[1]

    return dict(package=expected_package, version_code=attrs['versionCode'],
                min_sdk=_parse_sdk_field('sdkVersion'),
                target_sdk=_parse_sdk_field('targetSdkVersion'))


def inspect_apk(apk_path, lock_path, aapt2, log, pin, metadata):
    version = _read_lock_version(lock_path)
    inspect_signer(log, pin)
    inspect_zip(apk_path)
    meta = inspect_badging(aapt2, apk_path, version)
    _write_regular_bytes(metadata, (json.dumps(meta) + '\n').encode('utf-8'))


def main():
    match sys.argv[1:]:
        case ['stage', *paths] if len(paths) == 3:
            stage_apk(*map(Path, paths))
        case ['inspect', *paths] if len(paths) == 6:
            inspect_apk(*map(Path, paths))
        case _:
            raise SystemExit('Usage: apk.py stage ROOT SOURCE DEST | inspect APK LOCK AAPT2 SIGNATURE PIN META')


if __name__ == '__main__':
    try:
        main()
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'APK publication check failed: {exc}') from exc
