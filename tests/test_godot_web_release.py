"""Regression coverage for the release directory reader and fail-closed audit."""

from pathlib import Path
import struct
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"helpers"))
from godot_web_release import pack_paths, prepare
from godot_version_release import compact_payloads, restore_export_inputs, stage_version
import gzip
import json
import hashlib
import re
import os
from urllib.parse import urljoin


class ArtifactTests(unittest.TestCase):
    def export(self,root):
        (root/'index.html').write_text('<script src="index.js"></script>\nconst GODOT_CONFIG = '+json.dumps({'executable':'index','fileSizes':{'index.wasm':7,'index.pck':8}})+';\n')
        (root/'index.js').write_bytes(b'loader')
        (root/'index.wasm').write_bytes(b'engine')
        (root/'index.pck').write_bytes(self.pack(['res://main.scn']))

    def test_imported_authored_source_sheets_cannot_leak_through_hashed_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            previous=Path.cwd()
            try:
                os.chdir(temporary)
                source=Path('app/src/assets/sheets');source.mkdir(parents=True)
                (source/'source.png').write_bytes(b'owned authoring sheet')
                root=Path('export');root.mkdir();self.export(root)
                (root/'index.pck').write_bytes(self.pack(['res://.godot/imported/source.png-123.ctex']))
                with self.assertRaisesRegex(ValueError,'Development resources'):
                    prepare(root,policy={'forbidden_import_source_directories':['app/src/assets/sheets']})
                with self.assertRaisesRegex(ValueError,'owned native directory'):
                    prepare(root,policy={'forbidden_import_source_directories':['../foreign']})
            finally:
                os.chdir(previous)

    def test_frozen_shell_and_precompressed_bytes_identify_the_same_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.export(root)
            prepare(root,'a'*40)
            manifest=json.loads((root/'release-manifest.json').read_text())
            release=root/'releases'/manifest['release_id']
            self.assertIn('/releases/'+manifest['release_id']+'/index', (root/'index.html').read_text())
            for name in ('index.js','index.wasm','index.pck'):
                data=(release/name).read_bytes()
                self.assertEqual(gzip.decompress((release/(name+'.gz')).read_bytes()),data)
                self.assertEqual(manifest['files'][name]['sha256'],hashlib.sha256(data).hexdigest())
            self.assertEqual(manifest['source_commit'],'a'*40)

    def test_caller_budgets_and_linked_output_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.export(root)
            with self.assertRaisesRegex(ValueError,'caller budget'): prepare(root,'a'*40,max_wasm_bytes=1)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.export(root)
            (root/'index.wasm').unlink();(root/'index.wasm').symlink_to('/etc/passwd')
            with self.assertRaisesRegex(ValueError,'symlinks'): prepare(root,'a'*40)

    def test_relative_payload_urls_preserve_shell_directory_and_repack_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.export(root)
            prepare(root,'a'*40,relative_urls=True)
            manifest=json.loads((root/'release-manifest.json').read_text())
            html=(root/'index.html').read_text()
            config=json.loads(re.search(r'const GODOT_CONFIG = (\{[^\n]+\});',html)[1])
            urls=[config['executable'],*config['fileSizes'],re.search(r'src="([^"]+)"',html)[1]]
            for base in ('https://game.example/','https://game.example/74/'):
                for url in urls:
                    self.assertTrue(urljoin(base,url).startswith(base+'releases/'+manifest['release_id']+'/'))
            self.assertEqual(gzip.decompress((root/'index.html.gz').read_bytes()),html.encode())
            prepare(root,'a'*40,relative_urls=True)
            self.assertEqual((root/'index.html').read_text(),html)
            self.assertEqual(json.loads((root/'release-manifest.json').read_text())['release_id'],manifest['release_id'])

    def test_versioned_layout_has_one_regular_payload_and_preserves_browser_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.export(root)
            prepare(root, 'a' * 40, relative_urls=True, versioned_only=True)
            manifest = json.loads((root / 'release-manifest.json').read_text())
            self.assertEqual(manifest['layout'], 'versioned-payload-v1')
            for name, info in manifest['files'].items():
                path = root / info['path']
                self.assertTrue(path.is_file())
                self.assertFalse(path.is_symlink())
                self.assertEqual(path.stat().st_nlink, 1)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), info['sha256'])
                if name != 'index.html':
                    self.assertFalse((root / name).exists())
                    self.assertFalse((root / (name + '.gz')).exists())
                    self.assertEqual(info['path'], 'releases/' + manifest['release_id'] + '/' + name)
            html = (root / 'index.html').read_text()
            self.assertIn('releases/' + manifest['release_id'] + '/index', html)
            self.assertEqual(gzip.decompress((root / 'index.html.gz').read_bytes()), html.encode())

    def test_caller_selected_health_metadata_keeps_its_root_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.export(root)
            (root / 'release.json').write_text('{"commit":"' + 'a' * 40 + '"}')
            prepare(root, 'a' * 40, versioned_only=True, root_files=['release.json'])
            manifest = json.loads((root / 'release-manifest.json').read_text())
            self.assertEqual(manifest['files']['release.json']['path'], 'release.json')
            self.assertTrue((root / 'release.json').is_file())
            self.assertFalse((root / 'index.wasm').exists())

    def test_disposable_upgrade_fixture_rehydrates_the_same_audited_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.export(root)
            prepare(root, 'a' * 40, versioned_only=True)
            before = json.loads((root / 'release-manifest.json').read_text())
            restore_export_inputs(root)
            self.assertEqual(hashlib.sha256((root / 'index.js').read_bytes()).hexdigest(), before['files']['index.js']['sha256'])
            (root / 'index.js').write_bytes((root / 'index.js').read_bytes() + b'\n// upgrade fixture\n')
            prepare(root, 'b' * 40, versioned_only=True)
            after = json.loads((root / 'release-manifest.json').read_text())
            self.assertNotEqual(before['release_id'], after['release_id'])
            self.assertFalse((root / 'index.js').exists())

    def test_compaction_validates_all_duplicates_before_removing_any(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.export(root)
            prepare(root, 'a' * 40)
            manifest = json.loads((root / 'release-manifest.json').read_text())
            (root / 'releases' / manifest['release_id'] / 'index.wasm.gz').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'sidecar'):
                compact_payloads(root, manifest['files'], manifest['release_id'])
            self.assertTrue(all((root / name).exists() for name in manifest['files']))

    def test_fixture_rehydration_refuses_tampered_or_escaping_manifest_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.export(root)
            prepare(root, 'a' * 40, versioned_only=True)
            manifest = json.loads((root / 'release-manifest.json').read_text())
            manifest['files']['index.wasm']['path'] = '../outside'
            (root / 'release-manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                restore_export_inputs(root)
            self.assertFalse((root / 'index.js').exists())

    def test_fixture_destination_parent_cannot_redirect_a_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'root'; root.mkdir()
            outside = Path(directory) / 'outside'; outside.mkdir()
            staged = root / 'releases' / ('a' * 64) / 'nested'
            staged.mkdir(parents=True)
            (staged / 'runtime.js').write_bytes(b'fixture')
            (root / 'nested').symlink_to(outside, target_is_directory=True)
            (root / 'release-manifest.json').write_text(json.dumps({'release_id': 'a' * 64, 'files': {
                'nested/runtime.js': {'sha256': hashlib.sha256(b'fixture').hexdigest(),
                                      'path': 'releases/' + 'a' * 64 + '/nested/runtime.js'}}}))
            with self.assertRaisesRegex(ValueError, 'parent'):
                restore_export_inputs(root)
            self.assertFalse((outside / 'runtime.js').exists())

    def test_nested_sdk_and_extension_payloads_keep_versioned_identity_and_native_callback_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.export(root)
            (root / 'sdk').mkdir()
            (root / 'sdk/runtime.js').write_bytes(b'native SDK chunk')
            (root / 'sdk/runtime.js.gz').write_bytes(gzip.compress(b'native SDK chunk', mtime=0))
            (root / 'extension.wasm').write_bytes(b'extension')
            html = (root / 'index.html').read_text().replace('src="index.js"', 'src="index.js?v=42"')
            config = {'executable': 'index', 'fileSizes': {'index.wasm?cache=42': 7, 'index.pck': 8},
                      'gdextensionLibs': ['extension.wasm']}
            html = re.sub(r'const GODOT_CONFIG = (\{[^\n]+\});', 'const GODOT_CONFIG = ' + json.dumps(config) + ';', html)
            (root / 'index.html').write_text(html)
            names = ['index.html', 'index.js', 'index.wasm', 'index.pck', 'sdk/runtime.js', 'extension.wasm']
            files = {name: {'sha256': hashlib.sha256((root / name).read_bytes()).hexdigest()} for name in names}
            release = stage_version(root, files, recovery_id='fixture-release-recovery',
                failure_function='fixtureLoadFailure', release_root='fixtureReleaseRoot', strip_cache_query=True)
            staged = root / 'releases' / release
            self.assertEqual((staged / 'sdk/runtime.js').read_bytes(), b'native SDK chunk')
            self.assertEqual(gzip.decompress((staged / 'sdk/runtime.js.gz').read_bytes()), b'native SDK chunk')
            html = (root / 'index.html').read_text()
            self.assertIn(f'window.fixtureReleaseRoot = "/releases/{release}/";', html)
            self.assertIn('window.fixtureLoadFailure = function', html)
            self.assertNotIn('index.js?v=', html)
            self.assertIn(f'/releases/{release}/index.wasm', html)

    def test_undeclared_extension_or_nonliteral_callback_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.export(root)
            files = {name: {'sha256': hashlib.sha256((root / name).read_bytes()).hexdigest()}
                     for name in ['index.html', 'index.js', 'index.wasm', 'index.pck']}
            with self.assertRaises(ValueError):
                stage_version(root, files, failure_function='callback(); injected(')
            original = (root / 'index.html').read_text()
            config = {'executable': 'index', 'fileSizes': {}, 'gdextensionLibs': ['missing.wasm']}
            (root / 'index.html').write_text(re.sub(r'const GODOT_CONFIG = (\{[^\n]+\});',
                'const GODOT_CONFIG = ' + json.dumps(config) + ';', original))
            with self.assertRaisesRegex(ValueError, 'extension library'):
                stage_version(root, files)

    def test_nested_native_payloads_remain_closed_and_versioned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.export(root)
            (root / 'sdk').mkdir(); (root / 'sdk/bridge.js').write_bytes(b'bridge')
            (root / 'native.side.wasm').write_bytes(b'extension')
            prepare(root, 'a' * 40, versioned_only=True,
                    policy={'payload_directories': ['sdk'], 'recovery_id': 'native-recovery',
                            'failure_function': 'nativeLoadFailure', 'release_root': 'nativeReleaseRoot'})
            manifest = json.loads((root / 'release-manifest.json').read_text())
            self.assertEqual(set(manifest['files']), {'index.html', 'index.js', 'index.wasm', 'index.pck', 'sdk/bridge.js', 'native.side.wasm'})
            for name, value in manifest['files'].items():
                target = root / value['path']
                self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), value['sha256'])
                if name != 'index.html':
                    self.assertFalse((root / name).exists())
                    self.assertEqual(gzip.decompress(target.with_name(target.name + '.gz').read_bytes()), target.read_bytes())
            self.assertIn('window.nativeReleaseRoot = "/releases/' + manifest['release_id'], (root / 'index.html').read_text())

    def test_caller_pack_policy_preserves_required_and_forbidden_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.export(root)
            (root / 'index.pck').write_bytes(self.pack(['res://internal/debug.gdc']))
            with self.assertRaisesRegex(ValueError, 'Development resources'):
                prepare(root, policy={'forbidden_prefixes': ['internal/']})
            (root / 'index.pck').write_bytes(self.pack(['res://main.scn']))
            with self.assertRaisesRegex(ValueError, 'required native resources'):
                prepare(root, policy={'required_pack_paths': ['native/config.res']})
            prepare(root, policy={'required_pack_paths': ['main.scn']})

    def test_deferred_payload_has_separate_exact_initial_budget_accounting(self):
        import random
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.export(root)
            (root / 'optional.pck').write_bytes(random.Random(7).randbytes(4096))
            prepare(root, max_transfer_bytes=2000,
                    policy={'deferred_payloads': {'optional.pck': 'after_interactive'}})
            manifest = json.loads((root / 'release-manifest.json').read_text())
            self.assertLess(manifest['transfer_bytes'], 2000)
            self.assertGreater(manifest['full_transfer_bytes'], 4096)
            self.assertEqual(manifest['deferred_transfer_bytes'], manifest['files']['optional.pck']['transfer_bytes'])
            self.assertEqual(manifest['transfer_bytes'] + manifest['deferred_transfer_bytes'], manifest['full_transfer_bytes'])
            self.assertEqual(manifest['files']['optional.pck']['load_phase'], 'after_interactive')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.export(root)
            with self.assertRaisesRegex(ValueError, 'deferred payload is missing'):
                prepare(root, policy={'deferred_payloads': {'missing.pck': 'later'}})

    def test_payload_policy_rejects_unknown_paths_and_hosting_overflow(self):
        for policy, error in [({'unknown': True}, 'Unknown'),
                              ({'payload_directories': ['../outside']}, 'canonical'),
                              ({'max_file_bytes': 1}, 'hosting byte limit'),
                              ({'max_tree_bytes': 1}, 'hosting byte limit')]:
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); self.export(root)
                with self.assertRaisesRegex(ValueError, error):
                    prepare(root, policy=policy)

    def test_brotli_defaults_and_native_wasm_bytes_remain_compatible(self):
        import subprocess
        helper = Path(__file__).resolve().parents[1] / 'helpers/brotli.mjs'
        source = b'\0asm' + bytes(range(256)) * 16
        # Recorded from the previous shared/default and reviewed native/Wasm helpers.
        for mode, expected in [('compress', '2d308edee2317a74f4130bf9affd9428d4f934b1ff1bdf2e9390a6c6f4414223'),
                               ('compress-wasm', '9b2d5c64351d6d8d373912cb7450bf9d8ad3ae3d9fdeb768b41da4ec39e9bef4')]:
            with self.subTest(mode=mode):
                encoded = subprocess.run(['node', str(helper), mode], input=source, capture_output=True, check=True, timeout=10).stdout
                self.assertEqual(hashlib.sha256(encoded).hexdigest(), expected)
                decoded = subprocess.run(['node', str(helper), 'decompress'], input=encoded, capture_output=True, check=True, timeout=10).stdout
                self.assertEqual(decoded, source)

    def test_extra_pack_metadata_is_derived_from_real_payloads_and_cannot_override_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.export(root)
            (root / 'optional.pck').write_bytes(self.pack(['res://optional.scn']))
            prepare(root, policy={'pack_metadata': {'optional_pack_entries': 'optional.pck'}})
            manifest = json.loads((root / 'release-manifest.json').read_text())
            self.assertEqual(manifest['optional_pack_entries'], 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.export(root)
            with self.assertRaisesRegex(ValueError, 'existing native resource pack'):
                prepare(root, policy={'pack_metadata': {'source_commit': 'index.pck'}})

    @staticmethod
    def pack(names, flags=0, version=2):
        data = b"GDPC" + struct.pack("<5IQ", version, 4, 7, 2, flags, 0)
        data += bytes(64) if version == 2 else struct.pack("<Q", 104) + bytes(64)
        data += struct.pack("<I", len(names))
        for name in names:
            encoded = name.encode() + b"\0"
            data += struct.pack("<I", len(encoded)) + encoded + bytes(36)
        return data

    def test_directory_names_and_padding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "index.pck"
            names = ["res://project.binary", "res://src/main/main.gdc"]
            for version in (2, 3, 4):
                path.write_bytes(self.pack(names, version=version))
                self.assertEqual(pack_paths(path), names)

    def test_rejects_unreadable_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "index.pck"
            invalid_offset = bytearray(self.pack([], version=3))
            struct.pack_into("<Q", invalid_offset, 32, 2**63)
            for data in (b"bad!", self.pack([], flags=1), self.pack([], flags=4),
                         self.pack([], version=99), invalid_offset, self.pack(["test"])[:-1]):
                path.write_bytes(data)
                with self.assertRaises((ValueError, struct.error)):
                    pack_paths(path)

    def test_excludes_development_resources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("index.html", "index.js", "index.wasm"):
                (root / name).write_bytes(b"fixture")
            for forbidden in ("res://tests/test.gdc", "res://tools/debug.gd",
                              "res://addons/example/examples/demo.scn"):
                (root / "index.pck").write_bytes(self.pack([forbidden]))
                with self.assertRaisesRegex(ValueError, "Development resources"):
                    prepare(root)

    def test_requires_complete_export(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "Missing or empty"):
                prepare(Path(folder))

    def test_rejects_nonimmutable_source_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            for source in ['main', 'a' * 40 + '\n', 'A' * 40]:
                with self.assertRaisesRegex(ValueError, 'Source commit'):
                    prepare(Path(folder), source)


if __name__ == "__main__":
    unittest.main()
