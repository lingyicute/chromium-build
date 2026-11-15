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
apksigner=$(find "$build_tools_root" -mindepth 2 -maxdepth 2 -path "$build_tools_root/[!.]*/apksigner" -type f -executable | sort -V | tail -1)
[[ -n "$apksigner" ]] || fail 'Chromium-managed Android build-tools apksigner missing'
build_tools_dir=$(dirname "$apksigner")
export JAVA_HOME="$CR_ROOT/src/third_party/jdk/current"
# Prepend Chromium JDK to clean system PATH because apksigner wrappers invoke `java` via PATH.
export PATH="$JAVA_HOME/bin:/usr/bin:/bin:/usr/sbin:/sbin"
[[ -x "$apksigner" && -x "$JAVA_HOME/bin/java" ]] || fail 'Chromium-managed APK verifier/JDK missing'
"$apksigner" verify --verbose --print-certs "$apk" 2>&1 | tee "$release/apk-signature.txt"
aapt2="$build_tools_dir/aapt2"
"$CR_PYTHON" - "$apk" "$CR_ROOT/metadata/build-lock.json" "$aapt2" <<'PY'
import json, os, re, subprocess, sys, zipfile
from pathlib import Path

apk_path = Path(sys.argv[1])
lock = json.loads(Path(sys.argv[2]).read_text())
aapt2 = Path(sys.argv[3])

with zipfile.ZipFile(apk_path) as zf:
    names = zf.namelist()
if 'AndroidManifest.xml' not in names:
    raise SystemExit('APK missing AndroidManifest.xml')
if not any(name.endswith('.dex') for name in names):
    raise SystemExit('APK missing DEX bytecode')
if not any(name.startswith('lib/arm64-v8a/') and name.endswith('.so') for name in names):
    raise SystemExit('APK missing arm64-v8a native library')

if aapt2.is_file() and os.access(aapt2, os.X_OK):
    badging = subprocess.check_output([str(aapt2), 'dump', 'badging', str(apk_path)], text=True)
    m = re.search(r"versionName='([^']+)'", badging)
    if not m or m.group(1) != lock['version']:
        raise SystemExit(f"APK versionName mismatch: {m.group(1) if m else 'missing'} != {lock['version']}")
PY
cp "$apk" "$release/Chromium-Stable-arm64.apk"
cp -a "$CR_ROOT/metadata" "$release/metadata"
# SHA256SUMS authenticates nothing on its own; it detects corruption only.
(cd "$release" && find . -type f ! -name SHA256SUMS -printf '%P\0' | sort -z | xargs -0 sha256sum > SHA256SUMS)
"$CR_PYTHON" - "$release" <<'PY' >> "$GITHUB_STEP_SUMMARY"
import json, sys
from pathlib import Path

release = Path(sys.argv[1])
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
print(f"- APK size: `{apk_path.stat().st_size:,}` bytes")
print(f"- APK SHA-256: `{apk_sha}`")
if records:
    print(f"- Compile stages: `{len(records)}` ({total_minutes:.1f} min total compile)")
print('- Signing: upstream development signing key; NOT production-signed.')
PY
