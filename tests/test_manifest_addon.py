from pathlib import Path
import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
import manifest_addon


class ManifestArchiveControls(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        addon = self.root / 'addon'
        addon.mkdir()
        (addon / 'plugin.cfg').write_text('[plugin]\nversion="1.2.3"\n')
        (addon / 'plugin.gd').write_bytes(b'extends EditorPlugin\n')
        (addon / 'plugin.gd.uid').write_text('uid://abc123\n')
        self.names = ['plugin.cfg', 'plugin.gd', 'plugin.gd.uid']
        (self.root / 'package.files').write_text('\n'.join(self.names) + '\n')
        (self.root / 'package.json').write_text('{"version":"1.2.3"}')
        (self.root / 'package-allowlist.json').write_text(json.dumps(dict(files=self.names, addon_files=3, zip_entries=4)))
        self.config = {'package': dict(source='addon', manifest='package.files', archive='dist/plugin.zip',
                                      registry_archive='dist/registry.zip', format='manifest',
                                      prefix='addons/@owner_plugin/', version_source='package.json',
                                      allowlist='package-allowlist.json', script_uids=True)}

    def test_build_retains_exact_payload_and_both_archive_shapes(self):
        manifest_addon.build(self.root, self.config)
        with zipfile.ZipFile(self.root / 'dist/plugin.zip') as archive:
            self.assertEqual(archive.namelist(), ['addons/@owner_plugin/' + name for name in self.names] + ['PACKAGE_MANIFEST.json'])
            for name in self.names:
                self.assertEqual(archive.read('addons/@owner_plugin/' + name), (self.root / 'addon' / name).read_bytes())
            self.assertEqual(json.loads(archive.read('PACKAGE_MANIFEST.json'))['plugin_version'], '1.2.3')
        with zipfile.ZipFile(self.root / 'dist/registry.zip') as archive:
            self.assertEqual(archive.namelist(), self.names)
        first = (self.root / 'dist/plugin.zip').read_bytes()
        manifest_addon.build(self.root, self.config)
        self.assertEqual((self.root / 'dist/plugin.zip').read_bytes(), first)
        self.assertEqual(len(manifest_addon.artifact_paths(self.root, self.config)), 5)

    def test_mutated_payload_source_checksum_and_external_provenance_are_rejected(self):
        manifest_addon.build(self.root, self.config)
        (self.root / 'dist/PACKAGE_MANIFEST.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'external'):
            manifest_addon.verify(self.root, self.config)
        manifest_addon.build(self.root, self.config)
        (self.root / 'addon/plugin.gd').write_bytes(b'mutated source')
        with self.assertRaisesRegex(ValueError, 'source'):
            manifest_addon.verify(self.root, self.config)
        manifest_addon.build(self.root, self.config)
        archive = self.root / 'dist/registry.zip'
        archive.with_suffix('.zip.sha256').write_text('0' * 64 + '  registry.zip\n')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            manifest_addon.verify(self.root, self.config)

    def test_only_untracked_native_dependency_output_may_be_excluded(self):
        self.config['package']['generated_directories'] = ['.gdam']
        generated = self.root / 'addon/.gdam'
        generated.mkdir()
        (generated / 'deps.gd').write_text('generated fixture')
        manifest_addon.build(self.root, self.config)
        subprocess.run(['git', 'add', 'addon/.gdam'], cwd=self.root, check=True)
        with self.assertRaisesRegex(ValueError, 'tracked'):
            manifest_addon.build(self.root, self.config)

    def test_version_counts_and_uid_changes_fail_closed(self):
        (self.root / 'package.json').write_text('{"version":"1.2.4"}')
        with self.assertRaisesRegex(ValueError, 'versions'):
            manifest_addon.build(self.root, self.config)
        (self.root / 'package.json').write_text('{"version":"1.2.3"}')
        (self.root / 'addon/plugin.gd.uid').write_text('bad uid')
        with self.assertRaisesRegex(ValueError, 'UID'):
            manifest_addon.build(self.root, self.config)


if __name__ == '__main__':
    unittest.main()
