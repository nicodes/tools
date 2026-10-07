import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os

spec = importlib.util.spec_from_file_location('bootstrap', Path(__file__).parents[1]/'helpers/engineering-bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec); spec.loader.exec_module(bootstrap)

class SnapshotCacheTrust(unittest.TestCase):
    def installed(self, directory):
        root = Path(directory)/'release'
        (root/'helpers').mkdir(parents=True)
        (root/'tests').mkdir()
        (root/'helpers/example.py').write_text('reviewed bytes')
        source = {'repository': 'https://github.com/nicodes/tools', 'revision': 'a'*40,
                  'files': {'helpers/example.py': hashlib.sha256(b'reviewed bytes').hexdigest()}}
        (root/'SOURCE.json').write_text(json.dumps(source))
        pin = {'revision': 'a'*40, 'source_sha256': hashlib.sha256((root/'SOURCE.json').read_bytes()).hexdigest()}
        (Path(directory)/'engineering-pin.json').write_text(json.dumps(pin))
        return root

    def test_installed_release_is_verified_without_fetch_or_repackaging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.installed(directory)
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with patch.dict(os.environ, {}, clear=True), patch.object(bootstrap.subprocess, 'check_output', return_value=str(root)) as lookup:
                    bootstrap.main()
                    lookup.assert_called_once_with(['mise', 'where', 'http:cicd-engineering'], text=True)
            finally:
                os.chdir(previous)

    def test_modified_selected_release_fails_without_network_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.installed(directory)
            (root/'helpers/example.py').write_text('modified bytes')
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with patch.dict(os.environ, {'CICD_ENGINEERING': str(root)}, clear=True), patch.object(bootstrap.subprocess, 'check_output') as lookup:
                    with self.assertRaisesRegex(ValueError, 'independently reviewed pin'):
                        bootstrap.main()
                    lookup.assert_not_called()
            finally:
                os.chdir(previous)

    def test_repository_alias_keeps_independent_manifest_and_revision_checks(self):
        for repository in ('https://github.com/nicodes/tools', 'https://github.com/nicodes/tools', 'https://github.com/other/tools'):
            with self.subTest(repository=repository), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root/'helpers').mkdir(); (root/'tests').mkdir()
                helper = root/'helpers/example.py'; helper.write_text('reviewed bytes')
                source = {'repository': repository, 'revision': 'a'*40,
                          'files': {'helpers/example.py': hashlib.sha256(helper.read_bytes()).hexdigest()}}
                manifest = root/'SOURCE.json'; manifest.write_text(json.dumps(source))
                reviewed = hashlib.sha256(manifest.read_bytes()).hexdigest()
                expected = repository != 'https://github.com/other/tools'
                self.assertEqual(bootstrap.verify(root, 'a'*40, reviewed), expected)
                self.assertFalse(bootstrap.verify(root, 'b'*40, reviewed))
                self.assertFalse(bootstrap.verify(root, 'a'*40, '0'*64))

    def test_rewriting_both_helper_and_manifest_cannot_forge_reviewed_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'helpers').mkdir(); (root/'tests').mkdir()
            helper = root/'helpers/example.py'; helper.write_text('reviewed bytes')
            source = {'repository':'https://github.com/nicodes/tools', 'revision':'a'*40,
                      'files':{'helpers/example.py':hashlib.sha256(helper.read_bytes()).hexdigest()}}
            manifest = root/'SOURCE.json'; manifest.write_text(json.dumps(source))
            reviewed = hashlib.sha256(manifest.read_bytes()).hexdigest()
            self.assertTrue(bootstrap.verify(root,'a'*40,reviewed))
            helper.write_text('modified bytes')
            source['files']['helpers/example.py'] = hashlib.sha256(helper.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(source))
            self.assertFalse(bootstrap.verify(root,'a'*40,reviewed))

    def test_extra_files_and_symlinked_helpers_are_not_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'helpers').mkdir(); (root/'tests').mkdir()
            helper = root/'helpers/example.py'; helper.write_text('reviewed bytes')
            source = {'repository':'https://github.com/nicodes/tools', 'revision':'a'*40,
                      'files':{'helpers/example.py':hashlib.sha256(helper.read_bytes()).hexdigest()}}
            manifest = root/'SOURCE.json'; manifest.write_text(json.dumps(source))
            reviewed = hashlib.sha256(manifest.read_bytes()).hexdigest()
            extra = root/'helpers/extra.py'; extra.write_text('unreviewed')
            self.assertFalse(bootstrap.verify(root,'a'*40,reviewed))
            extra.unlink(); copy = root/'copy.py'; copy.write_text(helper.read_text()); helper.unlink(); helper.symlink_to(copy)
            self.assertFalse(bootstrap.verify(root,'a'*40,reviewed))
