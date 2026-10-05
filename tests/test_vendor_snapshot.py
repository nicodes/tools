import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('vendor_snapshot', Path(__file__).resolve().parents[1]/'helpers/vendor-snapshot.py')
vendor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vendor)


class VendorSnapshot(unittest.TestCase):
    def destination(self, root):
        destination = Path(root)/'engineering'
        destination.mkdir()
        files = {'helpers/old.py': b'old helper', 'tests/test_old.py': b'old test'}
        for name, data in files.items():
            path = destination/name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
        source = {'repository': vendor.REPOSITORY, 'revision': 'a'*40,
                  'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
        (destination/'SOURCE.json').write_text(json.dumps(source))
        return destination

    def source_git(self, repository, *arguments):
        self.assertEqual(str(repository), 'reviewed-local-source')
        if arguments == ('rev-parse', '--verify', 'b'*40 + '^{commit}'):
            return ('b'*40 + '\n').encode()
        if arguments == ('ls-tree', '-rz', '--full-tree', 'b'*40, '--', 'helpers/', 'tests/'):
            return b'100755 blob cccc\thelpers/new.py\0' + b'100644 blob dddd\ttests/test_new.py\0'
        if arguments == ('cat-file', 'blob', 'cccc'):
            return b'new helper\n'
        if arguments == ('cat-file', 'blob', 'dddd'):
            return b'new test\n'
        self.fail('unexpected Git operation: ' + repr(arguments))

    def test_complete_snapshot_replaces_inventory_and_preserves_git_modes(self):
        with tempfile.TemporaryDirectory() as root, patch.object(vendor, 'git', side_effect=self.source_git):
            destination = self.destination(root)
            cache = destination/'helpers/__pycache__'
            cache.mkdir()
            (cache/'old.cpython-313.pyc').write_bytes(b'derived bytecode')
            source = vendor.install('reviewed-local-source', 'b'*40, destination)
            self.assertEqual(set(source['files']), {'helpers/new.py', 'tests/test_new.py'})
            self.assertEqual(source['revision'], 'b'*40)
            self.assertEqual(json.loads((destination/'SOURCE.json').read_text()), source)
            self.assertEqual((destination/'helpers/new.py').stat().st_mode & 0o777, 0o755)
            self.assertFalse((destination/'helpers/old.py').exists())
            self.assertEqual(list(Path(root).iterdir()), [destination])
            vendor.verify_existing(destination)

    def test_the_product_owned_pin_record_survives_an_update(self):
        """ACTION-PINS.json is product-owned and lives in the snapshot directory.

        helpers/pins.mjs reads it from scripts/engineering/ACTION-PINS.json, so
        a vendoring tool that refused it made every product un-revendorable --
        and one that merely tolerated it would still have deleted it, because
        install() replaces the directory wholesale rather than merging into it.
        """
        with tempfile.TemporaryDirectory() as root, patch.object(vendor, 'git', side_effect=self.source_git):
            destination = self.destination(root)
            pins = destination/'ACTION-PINS.json'
            pins.write_text('{"actions": {"actions/checkout": "'+'e'*40+'"}}')
            vendor.install('reviewed-local-source', 'b'*40, destination)
            self.assertTrue(pins.is_file(), 'the update deleted the product-owned pin record')
            self.assertEqual(json.loads(pins.read_text())['actions']['actions/checkout'], 'e'*40)
            # And the snapshot itself still updated around it.
            self.assertEqual((destination/'helpers/new.py').read_bytes(), b'new helper\n')
            self.assertFalse((destination/'helpers/old.py').exists())

    def test_local_changes_unlisted_files_and_symlinks_are_preserved_and_refused(self):
        for change in ('modified', 'extra', 'symlink', 'directory'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                destination = self.destination(root)
                path = destination/'helpers/old.py'
                if change == 'modified':
                    path.write_bytes(b'local work')
                elif change == 'extra':
                    (destination/'helpers/local.py').write_bytes(b'local work')
                elif change == 'directory':
                    (destination/'local').mkdir()
                else:
                    path.unlink()
                    path.symlink_to('/nonexistent-synthetic-path')
                original_source = (destination/'SOURCE.json').read_bytes()
                with patch.object(vendor, 'git') as git, self.assertRaises(ValueError):
                    vendor.install('reviewed-local-source', 'b'*40, destination)
                git.assert_not_called()
                self.assertEqual((destination/'SOURCE.json').read_bytes(), original_source)
                self.assertEqual(list(Path(root).iterdir()), [destination])

    def test_mutable_revision_and_nonregular_source_trees_are_refused(self):
        for revision in ('HEAD', 'main', 'b'*39, '--help'):
            with self.subTest(revision=revision), patch.object(vendor, 'git') as git, self.assertRaises(ValueError):
                vendor.snapshot('reviewed-local-source', revision)
            git.assert_not_called()
        for tree in (b'120000 blob cccc\thelpers/link.py\0',
                     b'160000 commit cccc\thelpers/submodule\0',
                     b'100644 blob cccc\thelpers/nested/file.py\0'):
            with patch.object(vendor, 'git', side_effect=[('b'*40).encode(), tree]), self.assertRaises(ValueError):
                vendor.snapshot('reviewed-local-source', 'b'*40)

    def test_failed_install_restores_previous_snapshot(self):
        original_rename = Path.rename
        def fail_stage(path, target):
            if path.name == 'snapshot':
                raise OSError('synthetic rename failure')
            return original_rename(path, target)
        with tempfile.TemporaryDirectory() as root, patch.object(vendor, 'git', side_effect=self.source_git):
            destination = self.destination(root)
            before = (destination/'SOURCE.json').read_bytes()
            with patch.object(Path, 'rename', fail_stage), self.assertRaises(OSError):
                vendor.install('reviewed-local-source', 'b'*40, destination)
            self.assertEqual((destination/'SOURCE.json').read_bytes(), before)
            vendor.verify_existing(destination)
            self.assertEqual(list(Path(root).iterdir()), [destination])

    def test_failed_rollback_keeps_the_original_snapshot_for_recovery(self):

        original_rename = Path.rename
        def fail_stage_and_rollback(path, target):
            if path.name in ('snapshot', 'previous'):
                raise OSError('synthetic installation and rollback failure')
            return original_rename(path, target)
        with tempfile.TemporaryDirectory() as root, patch.object(vendor, 'git', side_effect=self.source_git):
            destination = self.destination(root)
            before = (destination/'SOURCE.json').read_bytes()
            with patch.object(Path, 'rename', fail_stage_and_rollback), self.assertRaises(OSError):
                vendor.install('reviewed-local-source', 'b'*40, destination)
            saved = list(Path(root).glob('.engineering-update-*/previous'))
            self.assertEqual(len(saved), 1)
            self.assertEqual((saved[0]/'SOURCE.json').read_bytes(), before)
            vendor.verify_existing(saved[0])
