#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
release="$RUNNER_TEMP/chromium-release"
rm -rf "$release"
mkdir -p "$release"
apk="$CR_ROOT/src/out/Release/apks/ChromePublic.apk"
[[ -s "$apk" ]] || fail 'Ninja succeeded but ChromePublic.apk is missing'
# Verify with SDK/JDK distributed through Chromium DEPS, not runner Android SDK.
build_tools_root="$CR_ROOT/src/third_party/android_sdk/public/build-tools"
build_tools_ver=$(find "$build_tools_root" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -V | tail -1)
[[ -n "$build_tools_ver" ]] || fail 'Chromium-managed Android build-tools directory missing'
apksigner="$build_tools_root/$build_tools_ver/apksigner"
export JAVA_HOME="$CR_ROOT/src/third_party/jdk/current"
# Prepend Chromium JDK to PATH because apksigner wrappers often invoke `java` via PATH.
export PATH="$JAVA_HOME/bin:$PATH"
[[ -x "$apksigner" && -x "$JAVA_HOME/bin/java" ]] || fail 'Chromium-managed APK verifier/JDK missing'
"$apksigner" verify --verbose --print-certs "$apk" | tee "$release/apk-signature.txt"
cp "$apk" "$release/Chromium-Stable-arm64.apk"
cp -a "$CR_ROOT/metadata" "$release/metadata"
# SHA256SUMS authenticates nothing on its own; it detects corruption only.
(cd "$release" && find . -type f ! -name SHA256SUMS -printf '%P\0' | sort -z | xargs -0 sha256sum > SHA256SUMS)
python3 - "$release" <<'PY' >> "$GITHUB_STEP_SUMMARY"
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
print('### Built Chromium Android arm64 APK')
print(f"- Version: `{lock['version']}` (`{lock['chromium_revision'][:12]}`)")
print(f"- APK size: `{apk_path.stat().st_size:,}` bytes")
print(f"- APK SHA-256: `{apk_sha}`")
print('- Signing: upstream development signing key; NOT production-signed.')
PY
