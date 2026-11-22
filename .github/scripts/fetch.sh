#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
mkdir -p "$HOME" "$CR_ROOT/cache" "$CR_ROOT/metadata" "$CIPD_CACHE_DIR"
"$CR_PYTHON" "$GITHUB_WORKSPACE/.github/scripts/resolve.py"
source_state="$GITHUB_WORKSPACE/.github/scripts/source_state.py"
lock_raw=$("$CR_PYTHON" "$source_state" read-lock)
readarray -t lock <<< "$lock_raw"
[[ "${#lock[@]}" -eq 3 && -n "${lock[0]}" && -n "${lock[1]}" && -n "${lock[2]}" ]] || fail 'Failed to read build-lock.json'
version="${lock[0]}"; revision="${lock[1]}"; depot_revision="${lock[2]}"
# Validate tag -> commit against the independent official source repository.
# A mismatch aborts, rather than silently falling back to main or another mirror.
git init "$CR_ROOT/src"
git -C "$CR_ROOT/src" remote add origin https://chromium.googlesource.com/chromium/src.git
retry git -C "$CR_ROOT/src" -c fetch.fsckObjects=true fetch --no-tags --depth=1 origin "refs/tags/$version"
[[ "$(git -C "$CR_ROOT/src" rev-parse --verify 'FETCH_HEAD^{commit}')" == "$revision" ]] || fail 'Stable tag/ChromiumDash commit mismatch'
git -C "$CR_ROOT/src" checkout --detach "$revision"
[[ "$(git -C "$CR_ROOT/src" rev-parse --verify HEAD)" == "$revision" ]] || fail 'Chromium source checkout revision mismatch'
git init "$CR_ROOT/depot_tools"
git -C "$CR_ROOT/depot_tools" remote add origin https://chromium.googlesource.com/chromium/tools/depot_tools.git
retry git -C "$CR_ROOT/depot_tools" -c fetch.fsckObjects=true fetch --no-tags --depth=1 origin "$depot_revision"
git -C "$CR_ROOT/depot_tools" checkout --detach "$depot_revision"
[[ "$(git -C "$CR_ROOT/depot_tools" rev-parse --verify HEAD)" == "$depot_revision" ]] || fail 'depot_tools revision mismatch'
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
"$CR_PYTHON" "$source_state" verify-source
# Do NOT run install-build-deps.sh: that would add an Ubuntu APT trust path.
# Missing system libraries/tools fail visibly instead of fetching alternatives.
retry gclient runhooks
"$CR_PYTHON" "$source_state" verify-source
gclient revinfo > "$CR_ROOT/metadata/dependencies.txt"
# The declared list may contain refs and package tags. Record the actual
# checked-out Git commits separately, without CIPD describe/network lookups.
gclient revinfo --actual --ignore-dep-type=cipd --ignore-dep-type=gcs \
  > "$CR_ROOT/metadata/git-dependencies.txt"
cp "$CR_ROOT/src/DEPS" "$CR_ROOT/metadata/DEPS"
mkdir -p "$CR_ROOT/src/out/Release"
cp "$GITHUB_WORKSPACE/config/args.gn" "$CR_ROOT/src/out/Release/args.gn"
cp "$GITHUB_WORKSPACE/config/args.gn" "$CR_ROOT/metadata/args.gn"
cd "$CR_ROOT/src"
real_root=$(/usr/bin/realpath -e -- "$CR_ROOT") || fail 'Chromium build root missing'
[[ "$real_root" != / ]] || fail 'Invalid Chromium build root'
for rel_tool in buildtools/linux64/gn third_party/ninja/ninja; do
  resolved_tool=$(/usr/bin/realpath -e -- "$rel_tool") || fail 'Expected Chromium GN/Ninja binaries missing'
  [[ "$resolved_tool" == "$real_root/"* && -f "$resolved_tool" && -x "$resolved_tool" ]] \
    || fail 'Expected Chromium GN/Ninja binaries missing'
done
buildtools/linux64/gn gen out/Release --fail-on-unused-args
[[ -f out/Release/build.ninja ]] || fail 'gn gen did not produce out/Release/build.ninja'
# Record GN's effective arguments, not just the overrides.
buildtools/linux64/gn args out/Release --list --short > "$CR_ROOT/metadata/effective-args.txt"
# Reclaim transient CIPD download cache and temp files before compiling.
if [[ -d "$CIPD_CACHE_DIR" ]]; then
  make_dirs_removable "$CIPD_CACHE_DIR"
  rm -rf -- "$CIPD_CACHE_DIR"
fi
make_dirs_removable "$TMPDIR"
find "$TMPDIR" -mindepth 1 -delete
df -h /mnt
