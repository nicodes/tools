"""A publication ZIP preserves the exact closed native packet and immutable source."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'helpers'))
import oci_registry as registry
from release_packet import Packet
spec = importlib.util.spec_from_file_location('prepare_native', Path(__file__).resolve().parents[1] / 'helpers/publication-prepare.py')
prepare = importlib.util.module_from_spec(spec); spec.loader.exec_module(prepare)


def fixture(root):
    packet = root / '.artifacts/release'; (packet / 'static').mkdir(parents=True)
    payload = b'closed archive bytes'; (packet / 'static/web.tar.gz').write_bytes(payload)
    artifact = {'kind': 'archive', 'reference': 'ghcr.io/example/product-web@' + registry.digest(registry.artifact_manifest(registry.digest(payload), len(payload))),
                'digest': registry.digest(payload), 'size_bytes': len(payload), 'media_type': registry.STATIC_LAYER}
    descriptor = {'kind': 'komizo.release', 'version': 1, 'application': 'product',
                  'source': {'repository': 'example/product', 'repository_id': '123', 'revision': 'a'*40},
                  'artifacts': {'web': artifact}, 'workload': {'assets': {'web': {'artifact': 'web'}}}}
    (packet / 'release.json').write_bytes(registry.canonical(descriptor))
    files = {name: {'sha256': hashlib.sha256((packet / name).read_bytes()).hexdigest(), 'size': (packet / name).stat().st_size, 'type': kind}
             for name, kind in [('release.json', 'release'), ('static/web.tar.gz', 'static')]}
    (packet / 'inventory.json').write_bytes(registry.canonical({'version': 1, 'files': files}))
    return packet


ENV = {'GITHUB_REPOSITORY': 'example/product', 'GITHUB_REPOSITORY_ID': '123', 'GITHUB_SHA': 'a'*40,
       'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'push', 'GITHUB_RUN_ID': '456', 'GITHUB_RUN_ATTEMPT': '2'}


def git(arguments):
    return {('rev-parse', 'HEAD'): 'a'*40, ('rev-parse', 'HEAD^{tree}'): 'b'*40,
            ('status', '--porcelain', '--untracked-files=no'): ''}[tuple(arguments)]


class PublicationPreparation(unittest.TestCase):
    def test_static_packet_has_no_engine_and_is_preserved_exactly_in_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); packet = fixture(root)
            result = prepare.prepare(root, ENV, git=git)
            with zipfile.ZipFile(result) as archive:
                self.assertEqual(set(archive.namelist()), {'release.json', 'inventory.json', 'static/web.tar.gz', 'publication-source.json'})
                for name in ['release.json', 'inventory.json', 'static/web.tar.gz']:
                    self.assertEqual(archive.read(name), (packet / name).read_bytes())
                self.assertEqual(json.loads(archive.read('publication-source.json')), {'version': 1, 'repository': 'example/product',
                    'revision': 'a'*40, 'tree': 'b'*40, 'run_id': 456, 'attempt': 2})

    def test_unknown_extra_symlink_duplicate_json_and_noncanonical_descriptor_refuse_before_zip(self):
        for change in ['extra', 'symlink', 'duplicate', 'canonical']:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); packet = fixture(root)
                if change == 'extra': (packet / 'unaccepted').write_bytes(b'outside inventory')
                elif change == 'symlink': (packet / 'static/web.tar.gz').unlink(); (packet / 'static/web.tar.gz').symlink_to('/etc/passwd')
                elif change == 'duplicate': (packet / 'inventory.json').write_text('{"version":1,"version":1,"files":{}}')
                else:
                    file = packet / 'release.json'; file.write_bytes(file.read_bytes() + b'\n')
                    inventory = json.loads((packet / 'inventory.json').read_bytes()); inventory['files']['release.json'].update(sha256=hashlib.sha256(file.read_bytes()).hexdigest(), size=file.stat().st_size)
                    (packet / 'inventory.json').write_bytes(registry.canonical(inventory))
                with self.assertRaises((ValueError, OSError)): prepare.prepare(root, ENV, git=git)
                self.assertFalse((root / '.artifacts/release-publish.zip').exists())

    def test_main_run_attempt_tree_and_owner_identity_cannot_be_relabeled(self):
        for changes in [{'GITHUB_SHA': 'c'*40}, {'GITHUB_REF': 'refs/pull/1/merge'}, {'GITHUB_RUN_ATTEMPT': '0'},
                        {'GITHUB_REPOSITORY_ID': '321'}, {'GITHUB_REPOSITORY': 'foreign/product'}]:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); fixture(root)
                with self.assertRaises(ValueError): prepare.prepare(root, ENV | changes, git=git)

    def test_prior_zip_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); fixture(root); output = root / '.artifacts/release-publish.zip'; output.write_bytes(b'retained')
            with self.assertRaises(ValueError): prepare.prepare(root, ENV, git=git)
            self.assertEqual(output.read_bytes(), b'retained')


if __name__ == '__main__': unittest.main()
