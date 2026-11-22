"""Run release.sh end to end against a tiny fake Chromium tree; no /mnt, SDK or JDK."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / '.github/scripts'
SPEC = importlib.util.spec_from_file_location('resolve', SCRIPTS / 'resolve.py')
resolve = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolve)

VERSION = '159.0.8154.18'
REVISION = 'a' * 40
ELF = (b'\x7fELF\x02\x01\x01' + bytes(9) + b'\x03\x00\xb7\x00\x01\x00\x00\x00'
       + bytes(28) + b'\x40\x00' + bytes(10))
DEX = b'dex\n039\x00' + bytes(24) + b'\x70\x00\x00\x00' * 2 + b'\x78\x56\x34\x12' + bytes(68)
BADGING = (f"package: name='org.chromium.chrome' versionCode='1234' versionName='{VERSION}'",
           "sdkVersion:'29'", "targetSdkVersion:'36'", "native-code: 'arm64-v8a'")


class ReleaseScript(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / 'root'
        # Run the real script. Only redirect the fixed build root after sourcing
        # common.sh, so the fixture never touches /mnt.
        wrappers = self.tmp / '.github/scripts'
        wrappers.mkdir(parents=True)
        (wrappers / 'release.sh').write_bytes((SCRIPTS / 'release.sh').read_bytes())
        (wrappers / 'common.sh').write_text(
            'source "$GITHUB_WORKSPACE/.github/scripts/common.sh"\n'
            'export CR_ROOT="$RUNNER_TEMP/root"\n')
        self.script = wrappers / 'release.sh'
        self.source_apk = self.root / 'src/out/Release/apks/ChromePublic.apk'
        self.source_apk.parent.mkdir(parents=True)
        with zipfile.ZipFile(self.source_apk, 'w') as zf:
            zf.writestr('AndroidManifest.xml', b'manifest')
            zf.writestr('classes.dex', DEX)
            zf.writestr('lib/arm64-v8a/libchrome.so', ELF)
        self.tool(self.root / 'src/third_party/jdk/current/bin/java', 'exit 0')
        metadata = self.root / 'metadata'
        metadata.mkdir()
        depot = (ROOT / 'config/depot_tools_revision.txt').read_text().strip()
        (metadata / 'build-lock.json').write_text(json.dumps(dict(
            repository='o/r', run_id='1', workflow_sha='c' * 40, target='chrome_public_apk',
            architecture='arm64', source=resolve.SOURCE, release_api=resolve.URL,
            chromium_revision=REVISION, depot_tools_revision=depot, version=VERSION)))
        (metadata / 'chromiumdash.json').write_text(json.dumps([dict(
            channel='Stable', platform='Android', version=VERSION, hashes={'chromium': REVISION})]))

    @staticmethod
    def tool(path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def build_tools(self, version, aapt2=True):
        directory = self.root / 'src/third_party/android_sdk/public/build-tools' / version
        fingerprint = (ROOT / 'config/debug_signer_sha256.txt').read_text().strip()
        # Record which build-tools version release.sh actually ran.
        self.tool(directory / 'apksigner',
                  'basename "$(dirname "$0")" > "$RUNNER_TEMP/apksigner-used"\n'
                  "printf '%s\\n' Verifies 'Verified using v2 scheme (APK Signature Scheme v2): true' "
                  f"'Number of signers: 1' 'Signer #1 certificate SHA-256 digest: {fingerprint}'")
        if aapt2:
            self.tool(directory / 'aapt2', "printf '%s\\n' " + ' '.join(f'"{line}"' for line in BADGING))

    def release(self):
        env = dict(os.environ, RUNNER_TEMP=str(self.tmp), GITHUB_WORKSPACE=str(ROOT),
                   GITHUB_REPOSITORY='o/r', GITHUB_RUN_ID='1', GITHUB_SHA='c' * 40,
                   GITHUB_STEP_SUMMARY=str(self.tmp / 'summary'))
        return subprocess.run(['bash', str(self.script)], env=env, capture_output=True, text=True, timeout=30)

    def test_newest_visible_build_tools_verify_the_published_apk(self):
        for version in ('9.0.0', '36.0.0', '35.0.0'):
            self.build_tools(version, aapt2=version == '36.0.0')
        # sort -V orders a leading "." after every digit; a hidden directory must still lose.
        self.build_tools('.staging', aapt2=False)
        p = self.release()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((self.tmp / 'apksigner-used').read_text(), '36.0.0\n')
        release = self.tmp / 'chromium-release'
        apk = release / 'Chromium-Stable-arm64.apk'
        self.assertEqual(apk.read_bytes(), self.source_apk.read_bytes())
        sums = dict(reversed(line.split('  ', 1)) for line in (release / 'SHA256SUMS').read_text().splitlines())
        self.assertEqual(sorted(sums), ['Chromium-Stable-arm64.apk', 'apk-signature.txt',
                                        'metadata/build-lock.json', 'metadata/chromiumdash.json'])
        self.assertEqual(sums['Chromium-Stable-arm64.apk'], hashlib.sha256(apk.read_bytes()).hexdigest())
        self.assertIn(f'- Version: `{VERSION}` (`{REVISION[:12]}`)', (self.tmp / 'summary').read_text())

    def test_hidden_build_tools_directory_is_never_used(self):
        self.build_tools('.staging')
        p = self.release()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('build-tools apksigner missing', p.stderr)
        self.assertFalse((self.tmp / 'apksigner-used').exists())


if __name__ == '__main__':
    unittest.main()
