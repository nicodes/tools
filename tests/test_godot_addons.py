import contextlib
import copy
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

    def test_frozen_lock_identity_and_install_path_cannot_diverge(self):
        self.entry['commit'] = 'a' * 40
        self.save()
        release = {'name': '@owner/addon', 'tag': 'v0.0.1', 'commit_sha': 'a' * 40,
                   'asset_id': 17, 'asset_digest': 'sha256:' + self.entry['asset_sha256'],
                   'installs': [{'path': 'addons/@owner_addon', 'required_by': ['gdam.json']}]}
        frozen = {'version': 1, 'requirements': {'@owner/addon': 'v0.0.1'}, 'addons': {'@owner/addon@v0.0.1': release}}
        (self.project / 'gdam.lock').write_text(json.dumps(frozen))
        addons.restore(self.project, self.fetch)
        for key, changed in [('asset_id', 99), ('commit_sha', 'b' * 40), ('asset_digest', 'sha256:' + 'f' * 64), ('installs', [])]:
            value = {**frozen, 'addons': {'@owner/addon@v0.0.1': {**release, key: changed}}}
            (self.project / 'gdam.lock').write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'identity or install path'):
                addons.restore(self.project, self.fetch)

    def test_reviewed_marker_requires_exact_asset_restore_and_uid_removal(self):
        addons.restore(self.project, self.fetch)
        marker = self.project / 'addons/.fixture-unverified'; marker.touch()
        uid = self.project / 'addons/@owner_addon/generated.gd.uid'; uid.write_text('uid://fixture')
        with self.assertRaises(ValueError):addons.verify(self.project)
        with self.assertRaises(ValueError):addons.restore(self.project, self.fetch, taint_markers=['../escape'])
        addons.restore(self.project, self.fetch, taint_markers=['.fixture-unverified'])
        self.assertFalse(marker.exists()); self.assertFalse(uid.exists())
        addons.verify(self.project)

    def test_direct_and_transitive_required_by_is_the_exact_closed_locked_graph(self):
        first=dict(self.entry,commit='a'*40)
        parent=dict(self.entry,commit='b'*40,asset_id=18)
        entries={'@owner/addon':first,'@owner/parent':parent}
        requirements={name:value['tag'] for name,value in entries.items()}
        (self.project/'gdam.json').write_text(json.dumps({'addons':{name:{'tag':tag} for name,tag in requirements.items()}}))
        (self.project/'addon-provenance.json').write_text(json.dumps(entries))
        releases={name+'@'+entry['tag']:{'name':name,'tag':entry['tag'],'commit_sha':entry['commit'],
            'asset_id':entry['asset_id'],'asset_digest':'sha256:'+entry['asset_sha256'],
            'installs':[{'path':'addons/'+name.replace('/','_'),'required_by':['gdam.json']}]}
            for name,entry in entries.items()}
        releases['@owner/parent@v0.0.1']['dependencies']={'@owner/addon':'v0.0.1'}
        releases['@owner/addon@v0.0.1']['installs'][0]['required_by']=['@owner/parent@v0.0.1','gdam.json']
        frozen={'version':1,'requirements':requirements,'addons':releases}
        (self.project/'gdam.lock').write_text(json.dumps(frozen))
        addons.restore(self.project,self.fetch)
        self.assertEqual(addons.verify(self.project),entries)
        self.assertEqual(len(self.fetches),2)
        for mutation in ['missing-root','missing-parent','unknown-parent','duplicate-parent','wrong-version','unselected-dependency']:
            changed=copy.deepcopy(frozen)
            required=changed['addons']['@owner/addon@v0.0.1']['installs'][0]['required_by']
            if mutation=='missing-root':required.remove('gdam.json')
            if mutation=='missing-parent':required.remove('@owner/parent@v0.0.1')
            if mutation=='unknown-parent':required.append('@owner/other@v0.0.1')
            if mutation=='duplicate-parent':required.append('@owner/parent@v0.0.1')
            if mutation=='wrong-version':changed['addons']['@owner/parent@v0.0.1']['dependencies']['@owner/addon']='v9.0.0'
            if mutation=='unselected-dependency':changed['addons']['@owner/parent@v0.0.1']['dependencies']['@owner/other']='v0.0.1'
            (self.project/'gdam.lock').write_text(json.dumps(changed))
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):addons.restore(self.project,self.fetch)
            self.assertEqual(len(self.fetches),2)


if __name__ == '__main__':
    unittest.main()
