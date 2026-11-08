import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('image_archive', Path(__file__).parents[1] / 'helpers/oci_archive.py')
oci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oci)

REF = 'registry.invalid/example/app:' + 'a' * 40


def encoded(value):
    return json.dumps(value, separators=(',', ':')).encode()


def descriptor(body, media):
    return {'mediaType': media, 'digest': 'sha256:' + hashlib.sha256(body).hexdigest(), 'size': len(body)}


def fixture(*, compressed=False, corrupt_diff=False):
    layer = b'opaque layer bytes checked independently from the manifest'
    raw = gzip.compress(layer, mtime=0) if compressed else layer
    config = encoded({'os': 'linux', 'architecture': 'amd64', 'config': {'Labels': {'org.opencontainers.image.revision': 'a' * 40}},
                      'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + ('0' * 64 if corrupt_diff else hashlib.sha256(layer).hexdigest())]}})
    c = descriptor(config, oci.CONFIG)
    l = descriptor(raw, 'application/vnd.oci.image.layer.v1.tar' + ('+gzip' if compressed else ''))
    manifest = encoded({'schemaVersion': 2, 'mediaType': oci.MANIFEST, 'config': c, 'layers': [l]})
    m = descriptor(manifest, oci.MANIFEST)
    m['annotations'] = {'org.opencontainers.image.ref.name': REF}
    entries = {'oci-layout': encoded({'imageLayoutVersion': '1.0.0'}),
               'index.json': encoded({'schemaVersion': 2, 'manifests': [m]})}
    entries.update({'blobs/sha256/' + record['digest'][7:]: body for record, body in [(c, config), (l, raw), (m, manifest)]})
    return entries, m, c, l


def write(path, entries, extra=()):
    with tarfile.open(path, 'w') as output:
        for name, body in [*entries.items(), *extra]:
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            output.addfile(entry, io.BytesIO(body))


class ImageArchive(unittest.TestCase):
    def test_raw_manifest_and_each_blob_are_available_without_extraction(self):
        for compressed in [False, True]:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'image.oci.tar'
                entries, manifest, config, layer = fixture(compressed=compressed)
                write(path, entries)
                with oci.OCIArchive(path, REF, config['digest']) as archive:
                    self.assertEqual(archive.metadata['manifest_digest'], manifest['digest'])
                    self.assertEqual(archive.metadata['config_digest'], config['digest'])
                    self.assertEqual(archive.metadata['platform'], 'linux/amd64')
                    self.assertEqual(hashlib.sha256(archive.manifest_bytes).hexdigest(), manifest['digest'][7:])
                    with archive.open_blob(layer['digest']) as incoming:
                        self.assertEqual(incoming.read(), entries['blobs/sha256/' + layer['digest'][7:]])
                self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ['image.oci.tar'])

    def test_immutable_manifest_reference_and_safe_materialized_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'image.oci.tar'
            entries,manifest,config,_=fixture();write(path,entries)
            reference=REF.rsplit(':',1)[0]+'@'+manifest['digest']
            with oci.OCIArchive(path,reference,config['digest']) as archive:
                layout=archive.materialize(root/'layout')
                self.assertEqual({p.relative_to(layout).as_posix():p.read_bytes() for p in layout.rglob('*') if p.is_file()},entries)
                with self.assertRaises(ValueError): archive.materialize(layout)
            with self.assertRaises(ValueError): oci.inspect_archive(path,reference.rsplit('@',1)[0]+'@sha256:'+'0'*64)
            (root/'alias').symlink_to(root,target_is_directory=True)
            with oci.OCIArchive(path) as archive:
                with self.assertRaises(ValueError): archive.materialize(root/'alias'/'outside')

    def test_corrupt_blob_or_rootfs_identity_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            for corrupt in ['blob', 'diff']:
                entries, _, _, layer = fixture(compressed=True, corrupt_diff=corrupt == 'diff')
                if corrupt == 'blob':
                    name = 'blobs/sha256/' + layer['digest'][7:]
                    entries[name] = b'x' * len(entries[name])
                path = Path(directory) / corrupt
                write(path, entries)
                with self.assertRaises(ValueError):
                    oci.inspect_archive(path, REF)

    def test_foreign_config_or_reference_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image'
            entries, _, _, _ = fixture()
            write(path, entries)
            for reference, identity in [(REF, 'sha256:' + '0' * 64), ('registry.invalid/foreign:' + 'a' * 40, None)]:
                with self.assertRaises(ValueError):
                    oci.inspect_archive(path, reference, identity)

    def test_unreferenced_duplicate_and_traversal_members_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ['../escape', '/absolute', 'unexpected', 'index.json']:
                path = Path(directory) / hashlib.sha256(name.encode()).hexdigest()
                entries, _, _, _ = fixture()
                write(path, entries, [(name, b'extra')])
                with self.assertRaises(ValueError):
                    oci.inspect_archive(path)

    def test_links_and_hardlinked_inputs_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'image'
            entries, _, _, _ = fixture()
            write(path, entries)
            linked = root / 'linked'
            linked.symlink_to(path)
            with self.assertRaises(ValueError):
                oci.inspect_archive(linked)
            linked.unlink()
            import os
            os.link(path, linked)
            with self.assertRaises(ValueError):
                oci.inspect_archive(path)

    def test_mutated_archive_cannot_supply_later_publication_blobs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image'
            entries, _, _, layer = fixture()
            write(path, entries)
            with oci.OCIArchive(path) as archive:
                with path.open('ab') as output:
                    output.write(b'changed')
                with self.assertRaises(ValueError):
                    archive.open_blob(layer['digest'])

    def test_multiple_manifests_and_duplicate_json_fields_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            for mode in ['multiple', 'duplicate']:
                path = Path(directory) / mode
                entries, manifest, _, _ = fixture()
                entries['index.json'] = (encoded({'schemaVersion': 2, 'manifests': [manifest, manifest]})
                                         if mode == 'multiple' else b'{"schemaVersion":2,"schemaVersion":2,"manifests":[]}')
                write(path, entries)
                with self.assertRaises(ValueError):
                    oci.inspect_archive(path)


if __name__ == '__main__':
    unittest.main()
