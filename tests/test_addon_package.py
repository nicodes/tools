import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('addon_package', Path(__file__).resolve().parents[1] / 'helpers/addon-package.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class AddonPackage(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'addon'
        self.source.mkdir()
        (self.source / 'plugin.cfg').write_text('[plugin]\nname="fixture"\n')
        self.manifest = self.root / 'package.files'
        self.manifest.write_text('plugin.cfg\n')
        self.archive = self.root / 'dist/addon.zip'

    def test_build_preserves_payload_and_verifies_actual_archive(self):
        digest = package.build(self.source, self.manifest, self.archive)
        self.assertEqual(digest, package.verify(self.archive, self.manifest))
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(archive.read('plugin.cfg'), (self.source / 'plugin.cfg').read_bytes())

    def test_unlisted_source_cannot_replace_existing_good_archive(self):
        package.build(self.source, self.manifest, self.archive)
        before = self.archive.read_bytes()
        (self.source / 'unexpected.gd').write_text('extends Node\n')
        with self.assertRaisesRegex(ValueError, 'differs'):
            package.build(self.source, self.manifest, self.archive)
        self.assertEqual(self.archive.read_bytes(), before)

    def test_unsafe_and_duplicate_manifest_paths_are_refused(self):
        for value in ['../outside\n', '/outside\n', 'dir\\file\n', 'a/./b\n',
                      'C:/outside\n', 'plugin.cfg\nplugin.cfg\n', 'z\na\n']:
            with self.subTest(value=value):
                self.manifest.write_text(value)
                with self.assertRaises(ValueError):
                    package.manifest_files(self.manifest)

    def test_source_symlink_is_refused(self):
        try:
            (self.source / 'linked').symlink_to(self.manifest)
        except OSError:
            self.skipTest('symlink creation unavailable')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            package.build(self.source, self.manifest, self.archive)

    def test_archive_duplicates_and_symlinks_are_refused(self):
        self.archive.parent.mkdir()
        with zipfile.ZipFile(self.archive, 'w') as archive:
            archive.writestr('plugin.cfg', 'one')
            archive.writestr('plugin.cfg', 'two')
        with self.assertRaisesRegex(ValueError, 'manifest mismatch'):
            package.verify(self.archive, self.manifest)
        with zipfile.ZipFile(self.archive, 'w') as archive:
            info = zipfile.ZipInfo('plugin.cfg')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, 'outside')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            package.verify(self.archive, self.manifest)


if __name__ == '__main__':
    unittest.main()
