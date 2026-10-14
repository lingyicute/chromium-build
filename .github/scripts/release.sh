#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
release="$RUNNER_TEMP/chromium-release"
mkdir -p "$release"
apk="$CR_ROOT/src/out/Release/apks/ChromePublic.apk"
[[ -s "$apk" ]] || fail 'Ninja succeeded but ChromePublic.apk is missing'
# Verify with SDK/JDK distributed through Chromium DEPS, not runner Android SDK.
apksigner="$CR_ROOT/src/third_party/android_sdk/public/build-tools/$(ls "$CR_ROOT/src/third_party/android_sdk/public/build-tools" | sort -V | tail -1)/apksigner"
export JAVA_HOME="$CR_ROOT/src/third_party/jdk/current"
[[ -x "$apksigner" && -x "$JAVA_HOME/bin/java" ]] || fail 'Chromium-managed APK verifier/JDK missing'
"$apksigner" verify --verbose --print-certs "$apk" | tee "$release/apk-signature.txt"
cp "$apk" "$release/Chromium-Stable-arm64.apk"
cp -a "$CR_ROOT/metadata" "$release/metadata"
cp "$GITHUB_WORKSPACE/SECURITY.md" "$release/BUILD-NOTICE.md"
# SHA256SUMS authenticates nothing on its own; it detects corruption only.
(cd "$release" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
echo 'Built and verified Chromium APK (upstream development signing key; NOT production-signed).' >> "$GITHUB_STEP_SUMMARY"
