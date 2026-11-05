import importlib.util
import os
from pathlib import Path
import stat
import shutil
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

    def test_checkout_timestamps_do_not_change_archive_identity(self):
        package.build(self.source, self.manifest, self.archive)
        before = self.archive.read_bytes()
        os.utime(self.source / 'plugin.cfg', (1_000_000_000, 1_000_000_000))
        package.build(self.source, self.manifest, self.archive)
        self.assertEqual(self.archive.read_bytes(), before)

    def test_checksum_validation_refuses_mutated_or_ambiguous_identities(self):
        digest = package.build(self.source, self.manifest, self.archive)
        sidecar = self.archive.with_suffix('.zip.sha256')
        sidecar.write_text(digest + '  addon.zip\n')
        self.assertEqual(package.verify_checksum(self.archive), digest)
        for value in ['0' * 64 + '  addon.zip\n', digest + '  other.zip\n',
                      digest + '  addon.zip\n' + digest + '  addon.zip\n']:
            sidecar.write_text(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                package.verify_checksum(self.archive)

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

    def test_archive_source_and_installed_bytes_are_independently_checked(self):
        package.build(self.source, self.manifest, self.archive)
        installed = self.root/'installed'
        shutil.copytree(self.source, installed)
        package.verify(self.archive, self.manifest, self.source, installed)
        (installed/'plugin.cfg').write_text('changed installed bytes')
        with self.assertRaisesRegex(ValueError, 'installed addon bytes'):
            package.verify(self.archive, self.manifest, installed=installed)
        (self.source/'plugin.cfg').write_text('changed source bytes')
        with self.assertRaisesRegex(ValueError, 'archive bytes'):
            package.verify(self.archive, self.manifest, source=self.source)

    def test_uid_policy_detects_missing_both_source_and_manifest_and_duplicate_ids(self):
        for name, uid in [('first', 'uid://first'), ('second', 'uid://second')]:
            (self.source/(name+'.gd')).write_text('extends Node\n')
            (self.source/(name+'.gd.uid')).write_text(uid+'\n')
        self.manifest.write_text(''.join(p.relative_to(self.source).as_posix()+'\n' for p in sorted(self.source.iterdir())))
        package.build(self.source, self.manifest, self.archive, script_uids=True)
        before = self.archive.read_bytes()
        (self.source/'second.gd.uid').write_text('uid://first\n')
        with self.assertRaisesRegex(ValueError, 'unique'):
            package.build(self.source, self.manifest, self.archive, script_uids=True)
        self.assertEqual(self.archive.read_bytes(), before)
        (self.source/'second.gd.uid').write_text('uid://second\n')
        (self.source/'first.gd.uid').unlink()
        self.manifest.write_text(self.manifest.read_text().replace('first.gd.uid\n', ''))
        with self.assertRaisesRegex(ValueError, 'UID sidecar'):
            package.build(self.source, self.manifest, self.archive, script_uids=True)

    def test_installed_tree_closure_rejects_added_generated_files(self):
        package.build(self.source, self.manifest, self.archive)
        installed = self.root/'installed'
        shutil.copytree(self.source, installed)
        (installed/'unexpected.uid').write_text('uid://unexpected\n')
        with self.assertRaisesRegex(ValueError, 'closed manifest'):
            package.verify(self.archive, self.manifest, installed=installed)

    def test_archive_only_uid_verification_cannot_skip_the_policy(self):
        (self.source/'script.gd').write_text('extends Node\n')
        self.manifest.write_text('plugin.cfg\nscript.gd\n')
        package.build(self.source, self.manifest, self.archive)
        with self.assertRaisesRegex(ValueError, 'UID sidecar'):
            package.verify(self.archive, self.manifest, script_uids=True)
        (self.source/'script.gd.uid').write_text('uid://valid\n')
        self.manifest.write_text('plugin.cfg\nscript.gd\nscript.gd.uid\n')
        package.build(self.source, self.manifest, self.archive)
        package.verify(self.archive, self.manifest, script_uids=True)

    def test_non_regular_archive_entries_cannot_pass_as_payload(self):
        self.archive.parent.mkdir()
        with zipfile.ZipFile(self.archive, 'w') as archive:
            info = zipfile.ZipInfo('plugin.cfg')
            info.create_system = 3
            info.external_attr = (stat.S_IFIFO | 0o600) << 16
            archive.writestr(info, 'payload')
        with self.assertRaisesRegex(ValueError, 'non-regular'):
            package.verify(self.archive, self.manifest)

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
