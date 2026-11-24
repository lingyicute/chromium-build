#!/usr/bin/env python3
"""Stage, verify, and summarize the built ChromePublic.apk."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile

from resolve import VERSION_PATTERN

PACKAGE_NAME = 'org.chromium.chrome'
PACKAGE_ATTR = r"[a-zA-Z][a-zA-Z0-9]*='[^']*'"


def stage_apk(root, source, destination):
    """Atomically snapshot source APK so an unverified fallback never sees a partial file."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Build root must be a real directory')
    st = source.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
        raise ValueError('Expected a non-empty regular ChromePublic.apk')
    if not source.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise ValueError('ChromePublic.apk resolves outside the build tree')
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        candidate = Path(tmp) / destination.name
        shutil.copyfile(source, candidate)
        candidate.chmod(0o400)
        candidate.replace(destination)


def inspect_signer(log, pin):
    expected = pin.read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('Invalid development certificate SHA-256 pin')
    trace = log.read_text(encoding='utf-8')
    bad_error = re.search(r'^(?:DOES NOT VERIFY\b|ERROR:)', trace, re.M)
    counts = re.findall(r'^Number of signers:[^\r\n]*$', trace, re.M)
    digests = re.findall(r'^[^\r\n]*certificate SHA-256 digest:[^\r\n]*$', trace, re.M)
    signer1 = re.findall(
        r'^Signer #1 certificate SHA-256 digest: ([0-9a-fA-F]{64})[ \t]*\r?$', trace, re.M)
    other_signers = re.search(r'^Signer #(?!1\b)\S+', trace, re.M)
    stamp_lines = re.findall(r'^Verified for SourceStamp:[^\r\n]*$', trace, re.M)
    bad_stamp = (any(not re.fullmatch(r'Verified for SourceStamp:[ \t]*false[ \t]*\r?', line) for line in stamp_lines)
                 or re.search(r'^Source ?Stamp Signer\b', trace, re.M))
    schemes = re.findall(r'^Verified using v(?:2|3(?:\.1)?) scheme\b[^\r\n]*$', trace, re.M)
    scheme_vals = [
        re.fullmatch(r'Verified using v(?:2|3(?:\.1)?) scheme \([^)\r\n]+\):[ \t]*(true|false)[ \t]*\r?', line)
        for line in schemes
    ]
    if (bad_error or other_signers or bad_stamp
            or len(counts) != 1 or not re.fullmatch(r'Number of signers: 1[ \t]*\r?', counts[0])
            or len(digests) != 1 or len(signer1) != 1 or signer1[0].lower() != expected
            or any(m is None for m in scheme_vals)
            or not any(m[1] == 'true' for m in scheme_vals)):
        raise ValueError('APK does not have exactly the pinned Chromium development signer')


def inspect_zip(apk_path):
    with zipfile.ZipFile(apk_path) as zf:
        entries = zf.infolist()
        names = {info.filename: info for info in entries}
        if len(names) != len(entries):
            raise ValueError('APK contains duplicate ZIP entry names')
        for name, info in names.items():
            trimmed = name[:-1] if info.is_dir() else name
            if (not trimmed or name.startswith('/') or '\\' in name
                    or any(part in ('', '.', '..') for part in trimmed.split('/'))):
                raise ValueError(f'APK contains unsafe ZIP entry path: {name!r}')
        for required in ('AndroidManifest.xml', 'classes.dex'):
            if required not in names or names[required].is_dir() or names[required].file_size <= 0:
                raise ValueError(f'APK missing non-empty root {required}')
        root_dex = [n for n in names if '/' not in n.rstrip('/') and n.rstrip('/').endswith('.dex')]
        expected_dex = ['classes.dex', *(f'classes{i}.dex' for i in range(2, len(root_dex) + 1))]
        if set(root_dex) != set(expected_dex):
            raise ValueError('APK root DEX files must form a contiguous classes*.dex sequence')
        for dex_name in expected_dex:
            info = names[dex_name]
            with zf.open(info) as dex:
                hdr = dex.read(0x78)
            if len(hdr) < 0x70 or not re.fullmatch(rb'dex\n[0-9]{3}\x00', hdr[:8]):
                raise ValueError(f'APK root {dex_name} has an invalid DEX header')
            file_size, hdr_size, endian = struct.unpack_from('<III', hdr, 32)
            if hdr_size not in (0x70, 0x78) or len(hdr) < hdr_size or not hdr_size <= file_size <= info.file_size or endian != 0x12345678:
                raise ValueError(f'APK root {dex_name} has a corrupt or truncated DEX header')

        libs = []
        for name in sorted(names):
            if not name.startswith('lib/'):
                continue
            if names[name].is_dir():
                if name not in ('lib/', 'lib/arm64-v8a/'):
                    raise ValueError(f'APK contains native library dir outside arm64-v8a: {name!r}')
            elif re.fullmatch(r'lib/arm64-v8a/[^/]+\.so', name):
                libs.append(name)
            else:
                raise ValueError(f'APK contains unexpected native library entry: {name!r}')
        if not libs:
            raise ValueError('APK missing arm64-v8a native library')
        for name in libs:
            with zf.open(name) as lib:
                hdr = lib.read(64)
            # ELF64 little-endian ET_DYN(3) for EM_AARCH64(183), version 1, ehsize 64.
            if (len(hdr) != 64 or hdr[:7] != b'\x7fELF\x02\x01\x01'
                    or struct.unpack_from('<HHI', hdr, 16) != (3, 183, 1)
                    or struct.unpack_from('<H', hdr, 52)[0] != 64):
                raise ValueError(f'APK has non-AArch64 or corrupt shared object: {name!r}')


def inspect_badging(aapt2, apk_path, locked_version):
    badging = subprocess.check_output(
        [str(aapt2), 'dump', 'badging', str(apk_path)], text=True, encoding='utf-8')
    pkg_lines = re.findall(r'^package:([^\r\n]*)\r?$', badging, re.M)
    if len(pkg_lines) != 1 or not re.fullmatch(rf'[ \t]*{PACKAGE_ATTR}(?:[ \t]+{PACKAGE_ATTR})*[ \t]*', pkg_lines[0]):
        raise ValueError('aapt2 output missing valid package line')
    pairs = re.findall(r"(?:^|[ \t]+)([a-zA-Z][a-zA-Z0-9]*)='([^']*)'", pkg_lines[0])
    attrs = dict(pairs)
    if len(attrs) != len(pairs):
        raise ValueError('aapt2 package line has duplicate fields')
    if attrs.get('name') != PACKAGE_NAME:
        raise ValueError(f'APK package name must be {PACKAGE_NAME}')
    if attrs.get('versionName') != locked_version:
        raise ValueError(f"APK versionName mismatch: {attrs.get('versionName')!r} != {locked_version!r}")
    if not re.fullmatch(r'[1-9][0-9]*', attrs.get('versionCode', '')):
        raise ValueError('APK missing positive versionCode')
    if re.search(r'^application-debuggable\b', badging, re.M):
        raise ValueError('APK must not be debuggable (android:debuggable=true)')
    if re.search(r'^(?:testOnly=|application-is-test-only\b)', badging, re.M):
        raise ValueError('APK must not be marked testOnly')
    abis = re.findall(r'^native-code:([^\r\n]*)\r?$', badging, re.M)
    if len(abis) != 1 or abis[0].strip() != "'arm64-v8a'" or re.search(r'^alt-native-code:', badging, re.M):
        raise ValueError('APK native-code must declare only arm64-v8a')

    def sdk_field(tag):
        hits = re.findall(rf"^{tag}:'([^'\r\n]+)'[ \t]*\r?$", badging, re.M)
        return hits[0] if len(hits) == 1 else None

    return {
        'package': PACKAGE_NAME,
        'version_code': attrs['versionCode'],
        'min_sdk': sdk_field('sdkVersion'),
        'target_sdk': sdk_field('targetSdkVersion'),
    }


def inspect_apk(apk_path, lock_path, aapt2, log, pin, meta_out):
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    version = lock.get('version') if isinstance(lock, dict) else None
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError('Invalid version in build-lock.json')
    inspect_signer(log, pin)
    inspect_zip(apk_path)
    meta = inspect_badging(aapt2, apk_path, version)
    meta_out.write_text(json.dumps(meta) + '\n', encoding='utf-8')


def write_summary(release, meta_path, summary_path):
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        lock = json.loads((release / 'metadata/build-lock.json').read_text(encoding='utf-8'))
        apk_path = release / 'Chromium-Stable-arm64.apk'
        apk_sha = next(
            line.split('  ', 1)[0]
            for line in (release / 'SHA256SUMS').read_text(encoding='utf-8').splitlines()
            if line.endswith('  Chromium-Stable-arm64.apk')
        )
        details = [f"{label} `{meta[key]}`" for key, label in (
            ('version_code', 'versionCode'), ('min_sdk', 'minSdk'), ('target_sdk', 'targetSdk'))
            if meta.get(key)]
        lines = [
            '### Built Chromium Android arm64 APK',
            f"- Version: `{lock['version']}` (`{lock['chromium_revision'][:12]}`)",
            f"- Package: `{meta['package']}`" + (f" ({', '.join(details)})" if details else ''),
            f"- APK size: `{apk_path.stat().st_size:,}` bytes",
            f'- APK SHA-256: `{apk_sha}`',
        ]
        runners = release / 'metadata/runners.jsonl'
        if runners.is_file():
            recs = [json.loads(line) for line in runners.read_text(encoding='utf-8').splitlines() if line.strip()]
            if recs:
                mins = sum(r.get('elapsed_seconds', 0) for r in recs if isinstance(r, dict)) / 60
                lines.append(f'- Compile stages: `{len(recs)}` ({mins:.1f} min total compile)')
        lines.append('- Signing: upstream development signing key; NOT production-signed.')
        with summary_path.open('a', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    except Exception as exc:
        print(f'::warning::Failed to append APK summary: {exc}', file=sys.stderr)


def main():
    match sys.argv[1:]:
        case ['stage', root, source, dest]:
            stage_apk(Path(root), Path(source), Path(dest))
        case ['inspect', apk_p, lock_p, aapt2, log, pin, meta]:
            inspect_apk(Path(apk_p), Path(lock_p), Path(aapt2), Path(log), Path(pin), Path(meta))
        case ['summary', release, meta, summary]:
            write_summary(Path(release), Path(meta), Path(summary))
        case _:
            raise SystemExit('Usage: apk.py stage ROOT SRC DST | inspect APK LOCK AAPT2 LOG PIN META | summary DIR META SUMMARY')


if __name__ == '__main__':
    try:
        main()
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'APK publication check failed: {exc}') from exc
