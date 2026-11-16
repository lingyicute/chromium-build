#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
mkdir -p "$HOME" "$CR_ROOT/cache" "$CR_ROOT/metadata" "$CIPD_CACHE_DIR"
"$CR_PYTHON" "$GITHUB_WORKSPACE/.github/scripts/resolve.py"
readarray -t lock < <("$CR_PYTHON" - <<'PY'
import json, os
with open(os.environ['CR_ROOT'] + '/metadata/build-lock.json') as f:
    x = json.load(f)
for k in ('version', 'chromium_revision', 'depot_tools_revision'):
    print(x[k])
PY
)
[[ "${#lock[@]}" -eq 3 && -n "${lock[0]}" && -n "${lock[1]}" && -n "${lock[2]}" ]] || fail 'Failed to read build-lock.json'
version="${lock[0]}"; revision="${lock[1]}"; depot_revision="${lock[2]}"
# Validate tag -> commit against the independent official source repository.
# A mismatch aborts, rather than silently falling back to main or another mirror.
git init "$CR_ROOT/src"
git -C "$CR_ROOT/src" remote add origin https://chromium.googlesource.com/chromium/src.git
retry git -C "$CR_ROOT/src" -c fetch.fsckObjects=true fetch --no-tags --depth=1 origin "refs/tags/$version"
[[ "$(git -C "$CR_ROOT/src" rev-parse 'FETCH_HEAD^{commit}')" == "$revision" ]] || fail 'Stable tag/ChromiumDash commit mismatch'
git -C "$CR_ROOT/src" checkout --detach "$revision"
git init "$CR_ROOT/depot_tools"
git -C "$CR_ROOT/depot_tools" remote add origin https://chromium.googlesource.com/chromium/tools/depot_tools.git
retry git -C "$CR_ROOT/depot_tools" -c fetch.fsckObjects=true fetch --no-tags --depth=1 origin "$depot_revision"
git -C "$CR_ROOT/depot_tools" checkout --detach "$depot_revision"
[[ "$(git -C "$CR_ROOT/depot_tools" rev-parse HEAD)" == "$depot_revision" ]] || fail 'depot_tools revision mismatch'
cat > "$CR_ROOT/.gclient" <<'GCLIENT'
solutions = [{
  'name': 'src',
  'url': 'https://chromium.googlesource.com/chromium/src.git',
  'managed': False,
  'custom_deps': {},
  'custom_vars': {
    'checkout_configuration': 'small',
    'checkout_js_coverage_modules': False,
    'checkout_pgo_profiles': False,
    'checkout_instrumented_libraries': False,
    'generate_location_tags': False,
  },
}]
target_os = ['android']
GCLIENT
cd "$CR_ROOT"
# All DEPS and hooks are upstream Chromium's. This is deliberately NOT a
# network allowlist or a claim that every transitive dependency is first-party.
retry gclient sync --nohooks --no-history --revision "src@$revision" --jobs=8
[[ "$(git -C src rev-parse HEAD)" == "$revision" ]] || fail 'gclient changed source revision'
"$CR_PYTHON" - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ['CR_ROOT'])
parts = {k.strip(): v.strip() for line in (root / 'src/chrome/VERSION').read_text().splitlines()
         if '=' in line for k, v in (line.split('=', 1),)}
try:
    actual = '.'.join(parts[k] for k in ('MAJOR', 'MINOR', 'BUILD', 'PATCH'))
except KeyError as exc:
    raise SystemExit(f'chrome/VERSION missing field: {exc.args[0]}') from exc
expected = json.loads((root / 'metadata/build-lock.json').read_text())['version']
if actual != expected:
    raise SystemExit(f'chrome/VERSION mismatch: {actual} != {expected}')
PY
# Do NOT run install-build-deps.sh: that would add an Ubuntu APT trust path.
# Missing system libraries/tools fail visibly instead of fetching alternatives.
retry gclient runhooks
[[ "$(git -C src rev-parse HEAD)" == "$revision" ]] || fail 'gclient runhooks changed source revision'
[[ "$(git -C depot_tools rev-parse HEAD)" == "$depot_revision" ]] || fail 'depot_tools revision changed after sync/hooks'
gclient revinfo > "$CR_ROOT/metadata/dependencies.txt"
cp "$CR_ROOT/src/DEPS" "$CR_ROOT/metadata/DEPS"
mkdir -p "$CR_ROOT/src/out/Release"
cp "$GITHUB_WORKSPACE/config/args.gn" "$CR_ROOT/src/out/Release/args.gn"
cp "$GITHUB_WORKSPACE/config/args.gn" "$CR_ROOT/metadata/args.gn"
cd "$CR_ROOT/src"
[[ -x buildtools/linux64/gn && -x third_party/ninja/ninja ]] || fail 'Expected Chromium GN/Ninja binaries missing'
buildtools/linux64/gn gen out/Release --fail-on-unused-args
[[ -f out/Release/build.ninja ]] || fail 'gn gen did not produce out/Release/build.ninja'
# Record GN's effective arguments, not just the overrides.
buildtools/linux64/gn args out/Release --list --short > "$CR_ROOT/metadata/effective-args.txt"
# Reclaim transient CIPD download cache and temp files before compiling.
if [[ -d "$CIPD_CACHE_DIR" ]]; then
  find "$CIPD_CACHE_DIR" -type d ! -perm -u+rwx -exec chmod u+rwx {} \;
  rm -rf -- "$CIPD_CACHE_DIR"
fi
find "$TMPDIR" -mindepth 1 -type d ! -perm -u+rwx -exec chmod u+rwx {} \;
find "$TMPDIR" -mindepth 1 -delete
df -h /mnt
