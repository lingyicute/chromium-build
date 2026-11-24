#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"

scripts="${GITHUB_WORKSPACE:?}/.github/scripts"
release="$RUNNER_TEMP/chromium-release"
inspection="$RUNNER_TEMP/chromium-inspection"
rm -rf -- "$release" "$inspection"
mkdir -m 700 -- "$release" "$inspection"

source_apk="$CR_ROOT/src/out/Release/apks/ChromePublic.apk"
apk="$inspection/ChromePublic.apk"
"$CR_PYTHON" "$scripts/apk.py" stage "$CR_ROOT" "$source_apk" "$apk"
"$CR_PYTHON" "$scripts/source_state.py" read-lock >/dev/null

# Pick the newest non-hidden Android SDK build-tools directory from Chromium DEPS.
build_tools_root="$CR_ROOT/src/third_party/android_sdk/public/build-tools"
[[ -d "$build_tools_root" ]] || fail 'Chromium-managed Android build-tools directory missing'
apksigner=''
exec {bt_fd}< <(find -H "$build_tools_root" -mindepth 2 -maxdepth 2 -name apksigner -xtype f -executable -print0 | sort -zV)
bt_pid=$!
while IFS= read -r -d '' candidate <&"$bt_fd"; do
  dir_name=${candidate%/apksigner}
  [[ "${dir_name##*/}" == .* ]] || apksigner=$candidate
done
exec {bt_fd}<&-
wait "$bt_pid" || fail 'Failed to scan Chromium-managed Android build-tools'
[[ -n "$apksigner" ]] || fail 'Chromium-managed Android build-tools apksigner missing'
build_tools_dir=${apksigner%/apksigner}

# Run apksigner using Chromium's bundled JDK rather than the runner's JDK.
export JAVA_HOME="$CR_ROOT/src/third_party/jdk/current"
export PATH="$JAVA_HOME/bin:/usr/bin:/bin:/usr/sbin:/sbin"
real_root=$(/usr/bin/realpath -e -- "$CR_ROOT")
require_build_tool() {
  local resolved
  resolved=$(/usr/bin/realpath -e -- "$1") || fail "Chromium-managed Android tool missing: $1"
  [[ "$resolved" == "$real_root/"* && -f "$resolved" && -x "$resolved" ]] \
    || fail "Expected executable tool inside Chromium tree: $1"
}

require_build_tool "$apksigner"
require_build_tool "$JAVA_HOME/bin/java"
"$apksigner" verify --verbose --print-certs "$apk" 2>&1 | tee -i "$release/apk-signature.txt"

# Prefer the CIPD aapt2 that packaged the APK; fall back to SDK build-tools aapt2.
aapt2="$CR_ROOT/src/third_party/android_build_tools/aapt2/cipd/aapt2"
[[ -f "$aapt2" && -x "$aapt2" ]] || aapt2="$build_tools_dir/aapt2"
require_build_tool "$aapt2"

apk_meta=$(mktemp "$TMPDIR/chromium-apk-meta.XXXXXX.json")
trap 'rm -f -- "$apk_meta"' EXIT
"$CR_PYTHON" "$scripts/apk.py" inspect \
  "$apk" "$CR_ROOT/metadata/build-lock.json" "$aapt2" "$release/apk-signature.txt" \
  "$GITHUB_WORKSPACE/config/debug_signer_sha256.txt" "$apk_meta"

# Hardlink into release so we publish the exact bytes verified above.
ln -- "$apk" "$release/Chromium-Stable-arm64.apk"

# Reject hidden files or symlinks inside metadata/ (upload-artifact omits hidden files).
[[ -d "$CR_ROOT/metadata" && ! -L "$CR_ROOT/metadata" ]] || fail 'Build metadata directory missing'
bad_meta=$(LC_ALL=C find "$CR_ROOT/metadata" -mindepth 1 \
  \( -name '.*' -o -name '*[![:alnum:]_.-]*' -o ! \( -type f -o -type d \) \) -printf x -quit)
[[ -z "$bad_meta" ]] || fail 'Build metadata contains hidden, unsafe, or non-regular entries'
cp -a "$CR_ROOT/metadata" "$release/metadata"

# Corruption-detection checksums (not cryptographic authentication).
(cd "$release" && find . -type f ! -path './SHA256SUMS' -printf '%P\0' | sort -z | xargs -0r sha256sum -- > SHA256SUMS)
[[ -s "$release/SHA256SUMS" ]] || fail 'Failed to generate SHA256SUMS'
(cd "$release" && sha256sum --strict --quiet -c SHA256SUMS) || fail 'SHA256SUMS self-check failed'

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  "$CR_PYTHON" "$scripts/apk.py" summary "$release" "$apk_meta" "$GITHUB_STEP_SUMMARY"
fi
