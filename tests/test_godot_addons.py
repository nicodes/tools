import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('game_addons', Path(__file__).resolve().parents[1] / 'helpers/godot-addons.py')
addons = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addons)


class LockedAddons(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / 'app'
        self.project.mkdir()
        self.payload = self.root / 'payload'
        self.payload.mkdir()
        (self.payload / 'plugin.cfg').write_text('[plugin]\nversion="0.0.1"\n')
        self.archive = self.root / 'release.zip'
        with zipfile.ZipFile(self.archive, 'w') as archive:
            archive.write(self.payload / 'plugin.cfg', 'plugin.cfg')
        self.entry = {'tag': 'v0.0.1', 'asset_id': 17,
                      'asset_sha256': hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                      'tree_sha256': addons.tree_hash(self.payload)}
        (self.project / 'gdam.json').write_text(json.dumps({'addons': {'@owner/addon': {'tag': 'v0.0.1'}}}))
        self.save()
        self.fetches = []

    def save(self):
        (self.project / 'addon-provenance.json').write_text(json.dumps({'@owner/addon': self.entry}))

    def fetch(self, repository, asset, destination):
        self.fetches.append((repository, asset))
        shutil.copy2(self.archive, destination)

    def test_exact_assets_restore_and_verified_cache_needs_no_network(self):
        addons.restore(self.project, self.fetch)
        with contextlib.redirect_stdout(io.StringIO()):
            addons.restore(self.project, self.fetch)
        self.assertEqual(self.fetches, [('owner/addon', 17)])
        self.assertEqual(addons.verify(self.project)['@owner/addon'], self.entry)

    def test_wrong_asset_bytes_never_replace_existing_cache(self):
        addons.restore(self.project, self.fetch)
        path = self.project / 'addons/@owner_addon/plugin.cfg'
        path.write_text('previous incomplete cache')
        self.entry['asset_sha256'] = 'f' * 64
        self.save()
        with self.assertRaisesRegex(ValueError, 'asset digest'):
            addons.restore(self.project, self.fetch)
        self.assertEqual(path.read_text(), 'previous incomplete cache')

    def test_installed_release_set_and_source_lock_must_match(self):
        addons.restore(self.project, self.fetch)
        (self.project / 'addons/undeclared').mkdir()
        with self.assertRaisesRegex(ValueError, 'undeclared'):
            addons.verify(self.project)
        self.entry['tag'] = 'v0.0.2'
        self.save()
        with self.assertRaisesRegex(ValueError, 'reviewed provenance'):
            addons.declaration(self.project)

    def test_archive_traversal_or_symlinks_are_refused_before_install(self):
        for name in ['../escape', '/escape', 'a\\escape']:
            with self.subTest(name=name):
                with zipfile.ZipFile(self.archive, 'w') as archive:
                    archive.writestr(name, 'bad')
                self.entry['asset_sha256'] = hashlib.sha256(self.archive.read_bytes()).hexdigest()
                self.save()
                with self.assertRaises(ValueError):
                    addons.restore(self.project, self.fetch)
                self.assertFalse((self.root / 'escape').exists())
                self.assertFalse((self.project / 'addons/@owner_addon').exists())


if __name__ == '__main__':
    unittest.main()
