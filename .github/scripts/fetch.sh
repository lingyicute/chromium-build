#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
mkdir -p "$HOME" "$CR_ROOT/cache" "$CR_ROOT/metadata"
python3 "$GITHUB_WORKSPACE/.github/scripts/resolve.py"
readarray -t lock < <(python3 - <<'PY'
import json, os
x=json.load(open(os.environ['CR_ROOT']+'/metadata/build-lock.json'))
for k in ('version','chromium_revision','depot_tools_revision'): print(x[k])
PY
)
version="${lock[0]}"; revision="${lock[1]}"; depot_revision="${lock[2]}"
# Validate tag -> commit against the independent official source repository.
# A mismatch aborts, rather than silently falling back to main or another mirror.
git init "$CR_ROOT/src"
git -C "$CR_ROOT/src" remote add origin https://chromium.googlesource.com/chromium/src.git
git -C "$CR_ROOT/src" -c fetch.fsckObjects=true fetch --no-tags --depth=1 origin "refs/tags/$version"
[[ "$(git -C "$CR_ROOT/src" rev-parse 'FETCH_HEAD^{commit}')" == "$revision" ]] || fail 'Stable tag/ChromiumDash commit mismatch'
git -C "$CR_ROOT/src" checkout --detach "$revision"
git init "$CR_ROOT/depot_tools"
git -C "$CR_ROOT/depot_tools" remote add origin https://chromium.googlesource.com/chromium/tools/depot_tools.git
git -C "$CR_ROOT/depot_tools" -c fetch.fsckObjects=true fetch --no-tags --depth=1 origin "$depot_revision"
git -C "$CR_ROOT/depot_tools" checkout --detach "$depot_revision"
[[ "$(git -C "$CR_ROOT/depot_tools" rev-parse HEAD)" == "$depot_revision" ]] || exit 1
cat > "$CR_ROOT/.gclient" <<'GCLIENT'
solutions = [{
  'name': 'src',
  'url': 'https://chromium.googlesource.com/chromium/src.git',
  'managed': False,
  'custom_deps': {},
  'custom_vars': {
    'checkout_pgo_profiles': False,
    'checkout_instrumented_libraries': False,
  },
}]
target_os = ['android']
GCLIENT
cd "$CR_ROOT"
# All DEPS and hooks are upstream Chromium's. This is deliberately NOT a
# network allowlist or a claim that every transitive dependency is first-party.
gclient sync --nohooks --no-history --shallow --revision "src@$revision" --jobs=4
[[ "$(git -C src rev-parse HEAD)" == "$revision" ]] || fail 'gclient changed source revision'
python3 - <<'PY'
import json, os
from pathlib import Path
root=Path(os.environ['CR_ROOT'])
parts=dict(line.split('=',1) for line in (root/'src/chrome/VERSION').read_text().splitlines() if '=' in line)
actual='.'.join(parts[k] for k in ('MAJOR','MINOR','BUILD','PATCH'))
assert actual == json.loads((root/'metadata/build-lock.json').read_text())['version'], actual
PY
# Do NOT run install-build-deps.sh: that would add an Ubuntu APT trust path.
# Missing system libraries/tools fail visibly instead of fetching alternatives.
gclient runhooks
gclient revinfo > "$CR_ROOT/metadata/dependencies.txt"
cp "$CR_ROOT/src/DEPS" "$CR_ROOT/metadata/DEPS"
mkdir -p "$CR_ROOT/src/out/Release"
cp "$GITHUB_WORKSPACE/config/args.gn" "$CR_ROOT/src/out/Release/args.gn"
cp "$GITHUB_WORKSPACE/config/args.gn" "$CR_ROOT/metadata/args.gn"
cd "$CR_ROOT/src"
[[ -x buildtools/linux64/gn && -x third_party/ninja/ninja ]] || fail 'Expected Chromium GN/Ninja binaries missing'
buildtools/linux64/gn gen out/Release --fail-on-unused-args
# Record GN's effective arguments, not just the overrides.
buildtools/linux64/gn args out/Release --list --short > "$CR_ROOT/metadata/effective-args.txt"
