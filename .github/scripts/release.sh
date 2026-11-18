#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
release="$RUNNER_TEMP/chromium-release"
inspection="$RUNNER_TEMP/chromium-inspection"
rm -rf -- "$release" "$inspection"
mkdir -m 700 -- "$release" "$inspection"
source_apk="$CR_ROOT/src/out/Release/apks/ChromePublic.apk"
apk="$inspection/ChromePublic.apk"
"$CR_PYTHON" "$GITHUB_WORKSPACE/.github/scripts/apk.py" stage "$CR_ROOT" "$source_apk" "$apk"
# Verify with SDK/JDK distributed through Chromium DEPS, not runner Android SDK.
build_tools_root="$CR_ROOT/src/third_party/android_sdk/public/build-tools"
[[ -d "$build_tools_root" ]] || fail 'Chromium-managed Android build-tools directory missing'
apksigner=$(find "$build_tools_root" -mindepth 2 -maxdepth 2 ! -path '*/.*/*' -name apksigner -xtype f -executable | sort -V | tail -1)
[[ -n "$apksigner" ]] || fail 'Chromium-managed Android build-tools apksigner missing'
build_tools_dir=$(dirname "$apksigner")
export JAVA_HOME="$CR_ROOT/src/third_party/jdk/current"
# Prepend Chromium JDK to clean system PATH because apksigner wrappers invoke `java` via PATH.
export PATH="$JAVA_HOME/bin:/usr/bin:/bin:/usr/sbin:/sbin"
require_build_tool() {
  local resolved
  resolved=$(/usr/bin/realpath -e -- "$1") || fail "Chromium-managed Android tool missing: $1"
  [[ "$resolved" == "$CR_ROOT/"* && -f "$resolved" && -x "$resolved" ]] \
    || fail "Expected an executable tool inside the Chromium tree: $1"
}
require_build_tool "$apksigner"
require_build_tool "$JAVA_HOME/bin/java"
"$apksigner" verify --verbose --print-certs "$apk" 2>&1 | tee -i "$release/apk-signature.txt"
# Prefer the CIPD aapt2 that compiled the APK resources; fall back to SDK build-tools aapt2.
bundled_aapt2="$CR_ROOT/src/third_party/android_build_tools/aapt2/cipd/aapt2"
if [[ -f "$bundled_aapt2" && -x "$bundled_aapt2" ]]; then
  aapt2="$bundled_aapt2"
else
  aapt2="$build_tools_dir/aapt2"
fi
require_build_tool "$aapt2"
apk_meta=$(mktemp "$TMPDIR/chromium-apk-meta.XXXXXX.json")
trap 'rm -f -- "$apk_meta"' EXIT
"$CR_PYTHON" "$GITHUB_WORKSPACE/.github/scripts/apk.py" inspect \
  "$apk" "$CR_ROOT/metadata/build-lock.json" "$aapt2" "$release/apk-signature.txt" \
  "$GITHUB_WORKSPACE/config/debug_signer_sha256.txt" "$apk_meta"
# Both directories are on RUNNER_TEMP. Publish the very bytes that were checked.
ln -- "$apk" "$release/Chromium-Stable-arm64.apk"
# The upload action omits hidden files; reject hidden or non-regular metadata
# rather than publishing a checksum set that does not match the artifact.
[[ -d "$CR_ROOT/metadata" && ! -L "$CR_ROOT/metadata" ]] || fail 'Build metadata directory missing'
invalid=$(find "$CR_ROOT/metadata" -mindepth 1 \
  \( -name '.*' -o ! \( -type f -o -type d \) \) -printf 'invalid' -quit)
[[ -z "$invalid" ]] || fail 'Build metadata contains hidden or non-regular entries'
cp -a "$CR_ROOT/metadata" "$release/metadata"
# SHA256SUMS authenticates nothing on its own; it detects corruption only.
(cd "$release" && find . -type f ! -path './SHA256SUMS' -printf '%P\0' | sort -z | xargs -0r sha256sum -- > SHA256SUMS)
[[ -s "$release/SHA256SUMS" ]] || fail 'Failed to generate SHA256SUMS'
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
"$CR_PYTHON" - "$release" "$apk_meta" "$GITHUB_STEP_SUMMARY" <<'SUMMARY_PY'
import json, sys
from pathlib import Path


def clean(value):
    return str(value).replace('`', '').replace('\r', ' ').replace('\n', ' ')


try:
    release = Path(sys.argv[1])
    apk_meta = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
    summary_path = Path(sys.argv[3])
    lock = json.loads((release / 'metadata/build-lock.json').read_text(encoding='utf-8'))
    apk_path = release / 'Chromium-Stable-arm64.apk'
    apk_sha = next(
        parts[0]
        for line in (release / 'SHA256SUMS').read_text(encoding='utf-8').splitlines()
        for parts in (line.split('  ', 1),)
        if len(parts) == 2 and parts[1] == 'Chromium-Stable-arm64.apk'
    )
    runners_path = release / 'metadata/runners.jsonl'
    records = ([json.loads(line) for line in runners_path.read_text(encoding='utf-8').splitlines() if line.strip()]
               if runners_path.is_file() else [])
    total_minutes = sum((r.get('elapsed_seconds') or 0) for r in records) / 60
    lines = [
        '### Built Chromium Android arm64 APK',
        f"- Version: `{clean(lock['version'])}` (`{clean(lock['chromium_revision'][:12])}`)",
    ]
    details = [
        f"versionCode `{clean(apk_meta['version_code'])}`" if apk_meta.get('version_code') else None,
        f"minSdk `{clean(apk_meta['min_sdk'])}`" if apk_meta.get('min_sdk') else None,
        f"targetSdk `{clean(apk_meta['target_sdk'])}`" if apk_meta.get('target_sdk') else None,
    ]
    details = [d for d in details if d]
    if apk_meta.get('package'):
        suffix = f" ({', '.join(details)})" if details else ''
        lines.append(f"- Package: `{clean(apk_meta['package'])}`{suffix}")
    lines.extend([
        f"- APK size: `{apk_path.stat().st_size:,}` bytes",
        f"- APK SHA-256: `{clean(apk_sha)}`",
    ])
    if records:
        lines.append(f"- Compile stages: `{len(records)}` ({total_minutes:.1f} min total compile)")
    lines.append('- Signing: upstream development signing key; NOT production-signed.')
    with summary_path.open('a', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
except Exception as exc:
    print(f'::warning::Failed to append APK summary: {exc}', file=sys.stderr)
SUMMARY_PY
fi
