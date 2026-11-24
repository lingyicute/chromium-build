#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
source "$(dirname "$0")/runner.sh"

require_hosted_runner
require_relay_mount
require_relay_directory "$CR_ROOT"
require_relay_directory "$CR_TRANSFER"
require_no_mount_targets "$CR_ROOT" "$CR_TRANSFER"
require_empty_directory "$CR_ROOT"
mkdir -p "$HOME" "$CR_ROOT/cache" "$CR_ROOT/metadata" "$CIPD_CACHE_DIR"

scripts="${GITHUB_WORKSPACE:?}/.github/scripts"
"$CR_PYTHON" "$scripts/resolve.py"
lock_raw=$("$CR_PYTHON" "$scripts/source_state.py" read-lock)
readarray -t lock <<< "$lock_raw"
[[ "${#lock[@]}" -eq 3 && -n "${lock[0]}" && -n "${lock[1]}" && -n "${lock[2]}" ]] || fail 'Failed to read build-lock.json'
version="${lock[0]}" revision="${lock[1]}" depot_revision="${lock[2]}"

# Cross-check the Android Stable tag against the resolved Chromium commit SHA.
git init "$CR_ROOT/src"
git -C "$CR_ROOT/src" remote add origin https://chromium.googlesource.com/chromium/src.git
retry git -C "$CR_ROOT/src" -c fetch.fsckObjects=true fetch --no-tags --depth=1 origin "refs/tags/$version"
[[ "$(git -C "$CR_ROOT/src" rev-parse --verify 'FETCH_HEAD^{commit}')" == "$revision" ]] \
  || fail 'Stable tag/ChromiumDash commit mismatch'
git -C "$CR_ROOT/src" checkout --detach "$revision"

git init "$CR_ROOT/depot_tools"
git -C "$CR_ROOT/depot_tools" remote add origin https://chromium.googlesource.com/chromium/tools/depot_tools.git
retry git -C "$CR_ROOT/depot_tools" -c fetch.fsckObjects=true fetch --no-tags --depth=1 origin "$depot_revision"
git -C "$CR_ROOT/depot_tools" checkout --detach "$depot_revision"

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
retry gclient sync --nohooks --no-history --revision "src@$revision" --jobs=8
# Do not run install-build-deps.sh; surface any missing runner tools directly.
retry gclient runhooks
"$CR_PYTHON" "$scripts/source_state.py" verify-source

# Record declared dependencies, actual Git revisions, DEPS, and GN arguments.
gclient revinfo > "$CR_ROOT/metadata/dependencies.txt"
gclient revinfo --actual --ignore-dep-type=cipd --ignore-dep-type=gcs \
  > "$CR_ROOT/metadata/git-dependencies.txt"
cp "$CR_ROOT/src/DEPS" "$CR_ROOT/metadata/DEPS"
mkdir -p "$CR_ROOT/src/out/Release"
cp "$GITHUB_WORKSPACE/config/args.gn" "$CR_ROOT/src/out/Release/args.gn"
cp "$GITHUB_WORKSPACE/config/args.gn" "$CR_ROOT/metadata/args.gn"

cd "$CR_ROOT/src"
real_root=$(realpath -e -- "$CR_ROOT")
for tool in buildtools/linux64/gn third_party/ninja/ninja; do
  resolved=$(realpath -e -- "$tool") || fail "Missing Chromium tool: $tool"
  [[ "$resolved" == "$real_root/"* && -f "$resolved" && -x "$resolved" ]] \
    || fail "Missing executable Chromium tool inside tree: $tool"
done

buildtools/linux64/gn gen out/Release --fail-on-unused-args
[[ -f out/Release/build.ninja && ! -L out/Release/build.ninja ]] \
  || fail 'gn gen did not produce out/Release/build.ninja'
buildtools/linux64/gn args out/Release --list --short > "$CR_ROOT/metadata/effective-args.txt"

# Drop transient download caches before compiling or checkpointing.
if [[ -d "$CIPD_CACHE_DIR" ]]; then
  make_dirs_removable "$CIPD_CACHE_DIR"
  rm -rf -- "$CIPD_CACHE_DIR"
fi
make_dirs_removable "$TMPDIR"
find "$TMPDIR" -mindepth 1 -delete
df -h /mnt
