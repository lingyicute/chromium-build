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


def stage_apk(root, source, destination):
    # Do not follow a symlink (or block opening a FIFO) in place of the APK.
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as src:
        before = os.fstat(src.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise ValueError('Expected a nonempty regular ChromePublic.apk')
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
            if (remaining or after.st_size != before.st_size
                    or after.st_mtime_ns != before.st_mtime_ns or after.st_ctime_ns != before.st_ctime_ns):
                raise ValueError('ChromePublic.apk changed while staging')
            candidate.chmod(0o400)
            candidate.replace(destination)


def inspect_signer(log, pin):
    expected = pin.read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('Invalid development certificate SHA-256 pin')
    trace = log.read_text(encoding='utf-8')
    counts = re.findall(r'^Number of signers: ([0-9]+)[ \t]*$', trace, re.M)
    hashes = re.findall(r'^Signer #([1-9][0-9]*) certificate SHA-256 digest: ([0-9a-fA-F]{64})[ \t]*$',
                        trace, re.M)
    if counts != ['1'] or len(hashes) != 1 or hashes[0][0] != '1' or hashes[0][1].lower() != expected:
        raise ValueError('APK does not have exactly the pinned Chromium development signer')


def inspect_zip(apk_path):
    with zipfile.ZipFile(apk_path) as zf:
        names = zf.namelist()
        if len(set(names)) != len(names):
            raise ValueError('APK contains duplicate ZIP entry names')
        names = {info.filename for info in zf.infolist() if not info.is_dir() and info.file_size > 0}
        if 'AndroidManifest.xml' not in names:
            raise ValueError('APK missing non-empty AndroidManifest.xml')
        if 'classes.dex' not in names:
            raise ValueError('APK missing non-empty root classes.dex')
        with zf.open('classes.dex') as dex:
            if not re.fullmatch(rb'dex\n[0-9]{3}\x00', dex.read(8)):
                raise ValueError('APK root classes.dex has an invalid DEX header')
        libs = sorted(name for name in names if re.fullmatch(r'lib/arm64-v8a/[^/]+\.so', name))
        if not libs:
            raise ValueError('APK missing non-empty arm64-v8a native library')
        for name in libs:
            with zf.open(name) as lib:
                header = lib.read(20)
            # ELF64 little-endian, ET_DYN (shared object), EM_AARCH64 (183).
            if header[:7] != b'\x7fELF\x02\x01\x01' or header[16:20] != b'\x03\x00\xb7\x00':
                raise ValueError(f'APK has a non-AArch64 shared object in arm64-v8a: {name!r}')


def inspect_badging(aapt2, apk_path, locked_version):
    if not (aapt2.is_file() and os.access(aapt2, os.X_OK)):
        raise ValueError('Chromium-managed Android build-tools aapt2 missing')
    badging = subprocess.check_output([str(aapt2), 'dump', 'badging', str(apk_path)],
                                      text=True, encoding='utf-8')
    package_lines = re.findall(r'^package:[ \t]*([^\r\n]+)$', badging, re.M)
    if len(package_lines) != 1:
        raise ValueError('aapt2 output must contain exactly one package line')
    fields = re.findall(r"(?:^|[ \t]+)([a-zA-Z]+)='([^']*)'", package_lines[0])
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
    abi_lines = re.findall(r'^native-code:[ \t]*([^\r\n]+)$', badging, re.M)
    if not abi_lines:
        raise ValueError('aapt2 output missing native-code line')
    if len(abi_lines) != 1 or 'arm64-v8a' not in re.findall(r"'([^']+)'", abi_lines[0]):
        raise ValueError('APK native-code missing an unambiguous arm64-v8a declaration')
    min_sdk = re.search(r"^sdkVersion:'([^']+)'", badging, re.M)
    target_sdk = re.search(r"^targetSdkVersion:'([^']+)'", badging, re.M)
    return dict(package=expected_package, version_code=attrs['versionCode'],
                min_sdk=min_sdk[1] if min_sdk else None, target_sdk=target_sdk[1] if target_sdk else None)


def inspect_apk(apk_path, lock_path, aapt2, log, pin, metadata):
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    inspect_signer(log, pin)
    inspect_zip(apk_path)
    meta = inspect_badging(aapt2, apk_path, lock['version'])
    metadata.write_text(json.dumps(meta) + '\n', encoding='utf-8')


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
