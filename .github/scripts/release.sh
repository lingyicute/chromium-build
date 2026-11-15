#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
release="$RUNNER_TEMP/chromium-release"
rm -rf "$release"
mkdir -p "$release"
apk="$CR_ROOT/src/out/Release/apks/ChromePublic.apk"
[[ -s "$apk" ]] || fail 'Ninja succeeded but ChromePublic.apk is missing'
# Verify with SDK/JDK distributed through Chromium DEPS, not runner Android SDK.
build_tools_root="$CR_ROOT/src/third_party/android_sdk/public/build-tools"
[[ -d "$build_tools_root" ]] || fail 'Chromium-managed Android build-tools directory missing'
apksigner=$(find "$build_tools_root" -mindepth 2 -maxdepth 2 -path "$build_tools_root/[!.]*/apksigner" -xtype f -executable | sort -V | tail -1)
[[ -n "$apksigner" ]] || fail 'Chromium-managed Android build-tools apksigner missing'
build_tools_dir=$(dirname "$apksigner")
export JAVA_HOME="$CR_ROOT/src/third_party/jdk/current"
# Prepend Chromium JDK to clean system PATH because apksigner wrappers invoke `java` via PATH.
export PATH="$JAVA_HOME/bin:/usr/bin:/bin:/usr/sbin:/sbin"
[[ -x "$apksigner" && -x "$JAVA_HOME/bin/java" ]] || fail 'Chromium-managed APK verifier/JDK missing'
"$apksigner" verify --verbose --print-certs "$apk" 2>&1 | tee -i "$release/apk-signature.txt"
aapt2="$build_tools_dir/aapt2"
apk_meta="$RUNNER_TEMP/chromium-apk-meta.json"
"$CR_PYTHON" - "$apk" "$CR_ROOT/metadata/build-lock.json" "$aapt2" "$apk_meta" <<'PY'
import json, os, re, subprocess, sys, zipfile
from pathlib import Path

apk_path = Path(sys.argv[1])
lock = json.loads(Path(sys.argv[2]).read_text())
aapt2 = Path(sys.argv[3])
meta_out = Path(sys.argv[4])

with zipfile.ZipFile(apk_path) as zf:
    infos = [info for info in zf.infolist() if info.file_size > 0]
names = {info.filename for info in infos}
if 'AndroidManifest.xml' not in names:
    raise SystemExit('APK missing non-empty AndroidManifest.xml')
if not any(name.endswith('.dex') for name in names):
    raise SystemExit('APK missing non-empty DEX bytecode')
if not any(name.startswith('lib/arm64-v8a/') and name.endswith('.so') for name in names):
    raise SystemExit('APK missing non-empty arm64-v8a native library')

if not (aapt2.is_file() and os.access(aapt2, os.X_OK)):
    raise SystemExit('Chromium-managed Android build-tools aapt2 missing')
badging = subprocess.check_output([str(aapt2), 'dump', 'badging', str(apk_path)], text=True)
pkg_line = re.search(r'^package:\s*(.+)$', badging, re.M)
if not pkg_line:
    raise SystemExit('aapt2 output missing package line')
pkg_text = pkg_line.group(1)
ver_match = re.search(r"versionName='([^']+)'", pkg_text)
if not ver_match or ver_match.group(1) != lock['version']:
    raise SystemExit(f"APK versionName mismatch: {ver_match.group(1) if ver_match else 'missing'} != {lock['version']}")
abi_line = re.search(r'^native-code:\s*(.+)$', badging, re.M)
if abi_line and "'arm64-v8a'" not in abi_line.group(1):
    raise SystemExit(f'APK native-code missing arm64-v8a: {abi_line.group(1)}')

pkg_name = re.search(r"name='([^']+)'", pkg_text)
ver_code = re.search(r"versionCode='([^']+)'", pkg_text)
min_sdk = re.search(r"^sdkVersion:'([^']+)'", badging, re.M)
target_sdk = re.search(r"^targetSdkVersion:'([^']+)'", badging, re.M)
meta = {
    'package': pkg_name.group(1) if pkg_name else None,
    'version_code': ver_code.group(1) if ver_code else None,
    'min_sdk': min_sdk.group(1) if min_sdk else None,
    'target_sdk': target_sdk.group(1) if target_sdk else None,
}
meta_out.write_text(json.dumps(meta) + '\n')
PY
cp "$apk" "$release/Chromium-Stable-arm64.apk"
cp -a "$CR_ROOT/metadata" "$release/metadata"
# SHA256SUMS authenticates nothing on its own; it detects corruption only.
(cd "$release" && find . -type f ! -name SHA256SUMS -printf '%P\0' | sort -z | xargs -0 sha256sum > SHA256SUMS)
"$CR_PYTHON" - "$release" "$apk_meta" <<'PY' >> "$GITHUB_STEP_SUMMARY"
import json, sys
from pathlib import Path

release = Path(sys.argv[1])
apk_meta = json.loads(Path(sys.argv[2]).read_text())
lock = json.loads((release / 'metadata/build-lock.json').read_text())
apk_path = release / 'Chromium-Stable-arm64.apk'
apk_sha = next(
    line.split()[0]
    for line in (release / 'SHA256SUMS').read_text().splitlines()
    if line.endswith('  Chromium-Stable-arm64.apk')
)
runners_path = release / 'metadata/runners.jsonl'
records = [json.loads(line) for line in runners_path.read_text().splitlines() if line.strip()] if runners_path.is_file() else []
total_minutes = sum(r.get('elapsed_seconds', 0) for r in records) / 60
print('### Built Chromium Android arm64 APK')
print(f"- Version: `{lock['version']}` (`{lock['chromium_revision'][:12]}`)")
details = [
    f"versionCode `{apk_meta['version_code']}`" if apk_meta.get('version_code') else None,
    f"minSdk `{apk_meta['min_sdk']}`" if apk_meta.get('min_sdk') else None,
    f"targetSdk `{apk_meta['target_sdk']}`" if apk_meta.get('target_sdk') else None,
]
details = [d for d in details if d]
if apk_meta.get('package'):
    suffix = f" ({', '.join(details)})" if details else ''
    print(f"- Package: `{apk_meta['package']}`{suffix}")
print(f"- APK size: `{apk_path.stat().st_size:,}` bytes")
print(f"- APK SHA-256: `{apk_sha}`")
if records:
    print(f"- Compile stages: `{len(records)}` ({total_minutes:.1f} min total compile)")
print('- Signing: upstream development signing key; NOT production-signed.')
PY
