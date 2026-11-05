"""Synthetic saved-image admission and bounded, scoped Docker cache controls."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
import image_import_cache as cache


class ImageImportCacheControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = 'a' * 40
        self.refs = ['registry.example/team/component-' + name + ':' + self.source for name in ['one', 'two', 'three']]
        self.expected, self.configs, self.layers = {}, {}, {}
        for index, reference in enumerate(self.refs):
            layers = [b'synthetic layer ' + str(number).encode() for number in range(index + 1)]
            config = {'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + hashlib.sha256(body).hexdigest() for body in layers]}}
            body = json.dumps(config).encode()
            self.configs[reference] = body
            self.layers[reference] = layers
            self.expected[reference] = 'sha256:' + hashlib.sha256(body).hexdigest()
        self.archives = [self.archive('shipping.tar.gz', self.refs[:1]), self.archive('fixtures.tar.gz', self.refs[1:])]
        self.calls = []
        self.listing = [self.row(ref, identity) for ref, identity in self.expected.items()]
        self.listing.append(self.row('registry.example/other/preserved:stable', 'sha256:' + 'f' * 64))
        self.containers = {}
        self.clock = 0
        self.elapsed_per_call = 0
        self.inspected = {ref: {'Id': identity, 'RootFS': {'Layers': json.loads(self.configs[ref])['rootfs']['diff_ids']}}
                          for ref, identity in self.expected.items()}

    @staticmethod
    def row(reference, identity, digest='<none>'):
        repository, tag = reference.rsplit(':', 1)
        return {'Repository': repository, 'Tag': tag, 'ID': identity, 'Digest': digest}

    def archive(self, name, references, *, manifest_change=None, contents_change=None, blob=False, compressed=False, oci=False):
        contents, manifest = {}, []
        for reference in references:
            body = self.configs[reference]
            config = ('blobs/sha256/' + self.expected[reference][7:]) if blob else self.expected[reference][7:] + '.json'
            contents[config] = body
            names, sources = [], {}
            for layer in self.layers[reference]:
                identity = 'sha256:' + hashlib.sha256(layer).hexdigest()
                payload = gzip.compress(layer) if compressed else layer
                raw_identity = hashlib.sha256(payload).hexdigest()
                path = 'blobs/sha256/' + raw_identity if blob else raw_identity + '/layer.tar'
                names.append(path)
                contents[path] = payload
                sources[identity] = {'digest': 'sha256:' + raw_identity, 'size': len(payload),
                                     'mediaType': 'application/vnd.oci.image.layer.v1.tar' + ('+gzip' if compressed else '')}
            row = {'Config': config, 'RepoTags': [reference], 'Layers': names}
            if blob:
                row['LayerSources'] = sources
            manifest.append(row)
        if manifest_change:
            manifest_change(manifest)
        contents['manifest.json'] = json.dumps(manifest).encode()
        if oci:
            descriptors = []
            for row in manifest:
                config = {'digest': 'sha256:' + hashlib.sha256(contents[row['Config']]).hexdigest(),
                          'size': len(contents[row['Config']]), 'mediaType': 'application/vnd.oci.image.config.v1+json'}
                layers = [{'digest': 'sha256:' + hashlib.sha256(contents[path]).hexdigest(), 'size': len(contents[path]),
                           'mediaType': 'application/vnd.oci.image.layer.v1.tar' + ('+gzip' if compressed else '')} for path in row['Layers']]
                body = json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                                   'config': config, 'layers': layers}).encode()
                digest = hashlib.sha256(body).hexdigest()
                contents['blobs/sha256/' + digest] = body
                descriptors.append({'digest': 'sha256:' + digest, 'size': len(body), 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                                    'annotations': {'io.containerd.image.name': row['RepoTags'][0],
                                                    'org.opencontainers.image.ref.name': self.source}})
            contents['index.json'] = json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json',
                                                'manifests': descriptors}).encode()
            contents['oci-layout'] = b'{"imageLayoutVersion":"1.0.0"}'
        if contents_change:
            contents_change(contents)
        path = self.root / name
        with tarfile.open(path, 'w:gz') as archive:
            for key, body in contents.items():
                member = tarfile.TarInfo(key)
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
        return {'path': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def execute(self, command, **options):
        self.calls.append((command, options))
        self.clock += self.elapsed_per_call
        if command[1:3] == ['image', 'ls']:
            output = '\n'.join(json.dumps(row) for row in self.listing)
        elif command[1:3] == ['container', 'ls']:
            output = self.containers.get(command[-1][len('ancestor='):], '')
        elif command[1:3] == ['image', 'inspect']:
            output = json.dumps([self.inspected[command[-1]]])
        else:
            output = ''
        return subprocess.CompletedProcess(command, 0, stdout=output)

    def reload(self, **changes):
        values = dict(root=self.root, source=self.source, archives=self.archives,
                      expected=self.expected, environment={'PATH': '/synthetic/docker-only'}, timeout=600)
        values.update(changes)
        with patch.object(cache.subprocess, 'run', side_effect=self.execute), patch.object(cache.time, 'monotonic', side_effect=lambda: self.clock):
            return cache.reload(**values)

    def mutations(self):
        return [command for command, _ in self.calls if command[1:3] in [['image', 'rm'], ['image', 'load']]]

    def refused_before_docker(self, **changes):
        self.calls.clear()
        with self.assertRaises((ValueError, tarfile.TarError, OSError)):
            self.reload(**changes)
        self.assertEqual(self.calls, [])

    def test_closed_archive_set_resets_only_present_refs_deepest_first_and_reloads_original_bytes(self):
        admitted = self.reload()
        self.assertEqual(admitted, {ref: {'id': value['Id'], 'rootfs': value['RootFS']['Layers']} for ref, value in self.inspected.items()})
        mutations = self.mutations()
        self.assertEqual(mutations[0], ['docker', 'image', 'rm', '--no-prune', '--', *reversed(self.refs)])
        self.assertEqual(mutations[1:], [['docker', 'image', 'load', '--input', str(self.root / row['path'])] for row in self.archives])
        rm_index = next(i for i, (command, _) in enumerate(self.calls) if command[1:3] == ['image', 'rm'])
        checks = [command for command, _ in self.calls[:rm_index] if command[1:3] == ['container', 'ls']]
        self.assertEqual({command[-1] for command in checks}, {'ancestor=' + identity for identity in self.expected.values()})
        self.assertTrue(all('--all' in command for command in checks))
        self.assertFalse(any(value in command for command, _ in self.calls for value in ['--force', '-f', 'prune', 'build', 'create', 'start']))
        self.assertTrue(all(options['cwd'] == self.root and options['env'] == {'PATH': '/synthetic/docker-only'} for _, options in self.calls))

    def test_absent_cache_is_loaded_without_remove_and_partial_cache_removes_only_present_ref(self):
        for rows, removed in [([], []), ([self.listing[0]], self.refs[:1])]:
            with self.subTest(rows=rows):
                self.calls.clear()
                self.listing = rows
                self.reload()
                removals = [command for command in self.mutations() if command[2] == 'rm']
                self.assertEqual(removals, [['docker', 'image', 'rm', '--no-prune', '--', *removed]] if removed else [])

    def test_blob_named_gzip_layers_use_config_diff_ids_and_preserve_archive(self):
        archive = self.archive('oci.tar.gz', self.refs, blob=True, compressed=True, oci=True)
        before = (self.root / archive['path']).read_bytes()
        self.assertEqual(set(self.reload(archives=[archive])), set(self.expected))
        self.assertEqual((self.root / archive['path']).read_bytes(), before)

    def test_oci_index_foreign_tag_descriptor_bytes_and_layer_scope_refuse_before_docker(self):
        def change_annotation(contents):
            index = json.loads(contents['index.json'])
            index['manifests'][0]['annotations']['io.containerd.image.name'] = 'registry.example/foreign/image:' + self.source
            contents['index.json'] = json.dumps(index).encode()
        def change_size(contents):
            index = json.loads(contents['index.json'])
            index['manifests'][0]['size'] += 1
            contents['index.json'] = json.dumps(index).encode()
        def change_layer(contents):
            index = json.loads(contents['index.json'])
            descriptor = index['manifests'][0]
            body = json.loads(contents['blobs/sha256/' + descriptor['digest'][7:]])
            body['layers'][0]['digest'] = 'sha256:' + 'e' * 64
            encoded = json.dumps(body).encode()
            descriptor['digest'] = 'sha256:' + hashlib.sha256(encoded).hexdigest()
            descriptor['size'] = len(encoded)
            contents['blobs/sha256/' + descriptor['digest'][7:]] = encoded
            contents['index.json'] = json.dumps(index).encode()
        for change in [change_annotation, change_size, change_layer]:
            with self.subTest(change=change.__name__):
                archive = self.archive('oci-bad.tar.gz', self.refs, blob=True, compressed=True, oci=True, contents_change=change)
                self.refused_before_docker(archives=[archive])

    def test_wrong_source_archive_hash_config_id_and_missing_or_extra_refs_refuse_before_docker(self):
        self.refused_before_docker(source='b' * 40)
        self.refused_before_docker(archives=[dict(self.archives[0], sha256='0' * 64), self.archives[1]])
        self.refused_before_docker(expected=dict(self.expected, **{self.refs[0]: 'sha256:' + 'e' * 64}))
        self.refused_before_docker(archives=self.archives[:1])
        extra = self.archive('extra.tar.gz', self.refs, manifest_change=lambda rows: rows[0]['RepoTags'].append('registry.example/foreign/image:' + self.source))
        self.refused_before_docker(archives=[extra])

    def test_duplicate_refs_in_and_between_archives_refuse_before_docker(self):
        duplicate = self.archive('duplicate.tar.gz', self.refs, manifest_change=lambda rows: rows.append(rows[0]))
        self.refused_before_docker(archives=[duplicate])
        duplicate = self.archive('duplicate.tar.gz', self.refs[:1])
        self.refused_before_docker(archives=[*self.archives, duplicate])

    def test_missing_changed_and_compressed_wrong_layer_bytes_refuse_before_docker(self):
        for blob, compressed in [(False, False), (True, True)]:
            with self.subTest(blob=blob):
                def change(contents):
                    key = next(key for key in contents if key.endswith('/layer.tar') or (key.startswith('blobs/') and key != 'blobs/sha256/' + self.expected[self.refs[0]][7:]))
                    contents[key] = gzip.compress(b'wrong layer') if compressed else b'wrong layer'
                bad = self.archive('changed.tar.gz', self.refs[:1], blob=blob, compressed=compressed, contents_change=change)
                self.refused_before_docker(archives=[bad, self.archives[1]])
        missing = self.archive('missing.tar.gz', self.refs[:1], manifest_change=lambda rows: rows[0]['Layers'].__setitem__(0, 'absent/layer.tar'))
        self.refused_before_docker(archives=[missing, self.archives[1]])

    def test_layer_descriptors_cannot_add_urls_or_misrepresent_digest_size_or_rootfs_scope(self):
        def descriptor(rows):
            return next(iter(rows[0]['LayerSources'].values()))
        changes = [lambda rows: descriptor(rows).update(urls=['https://foreign.example/layer']),
                   lambda rows: descriptor(rows).update(digest='sha256:' + 'e' * 64),
                   lambda rows: descriptor(rows).update(size=True),
                   lambda rows: descriptor(rows).update(size=123456),
                   lambda rows: descriptor(rows).update(mediaType='application/unknown'),
                   lambda rows: rows[0]['LayerSources'].update({'sha256:' + 'e' * 64: {}})]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                archive = self.archive('descriptor.tar.gz', self.refs, blob=True, manifest_change=change)
                self.refused_before_docker(archives=[archive])

    def test_repeated_json_fields_and_unknown_manifest_authority_refuse_before_docker(self):
        repeated = self.archive('repeated.tar.gz', self.refs,
                                contents_change=lambda contents: contents.__setitem__('manifest.json', b'[{"Config":"one","Config":"two"}]'))
        self.refused_before_docker(archives=[repeated])
        parent = self.archive('parent.tar.gz', self.refs, manifest_change=lambda rows: rows[0].update(Parent='foreign-parent'))
        self.refused_before_docker(archives=[parent])

    def test_symlink_parent_symlink_archive_and_hardlinked_archive_refuse_before_docker(self):
        (self.root / 'link.tar.gz').symlink_to(self.root / self.archives[0]['path'])
        self.refused_before_docker(archives=[dict(self.archives[0], path='link.tar.gz'), self.archives[1]])
        (self.root / 'linked-parent').symlink_to(self.root, target_is_directory=True)
        self.refused_before_docker(archives=[dict(self.archives[0], path='linked-parent/' + self.archives[0]['path']), self.archives[1]])
        (self.root / 'hardlink.tar.gz').hardlink_to(self.root / self.archives[0]['path'])
        self.refused_before_docker()

    def test_unsafe_or_linked_archive_members_and_archive_bounds_refuse_before_docker(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('linked', tarfile.SYMTYPE), ('hardlinked', tarfile.LNKTYPE)]:
            with self.subTest(name=name):
                path = self.root / 'unsafe.tar.gz'
                with tarfile.open(path, 'w:gz') as archive:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = 'manifest.json' if kind != tarfile.REGTYPE else ''
                    archive.addfile(member, io.BytesIO(b''))
                record = {'path': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                self.refused_before_docker(archives=[record])
        with patch.object(cache, 'MAX_ARCHIVE', 1):
            self.refused_before_docker()

    def test_foreign_alias_digest_and_different_existing_source_id_refuse_before_mutations(self):
        for row in [self.row('registry.example/other/alias:stable', self.expected[self.refs[0]]),
                    self.row(self.refs[0], self.expected[self.refs[0]], 'sha256:' + 'd' * 64),
                    self.row(self.refs[0], 'sha256:' + 'c' * 64)]:
            with self.subTest(row=row):
                self.calls.clear()
                self.listing = [row]
                with self.assertRaises(ValueError):
                    self.reload()
                self.assertFalse(self.mutations())

    def test_even_stopped_target_container_refuses_before_any_mutation(self):
        self.containers[self.expected[self.refs[-1]]] = 'created-container-never-started\n'
        with self.assertRaisesRegex(ValueError, 'existing container'):
            self.reload()
        self.assertFalse(self.mutations())

    def test_changed_archive_during_cache_admission_refuses_before_mutations(self):
        execute = self.execute
        def change(command, **options):
            result = execute(command, **options)
            if command[1:3] == ['image', 'ls']:
                (self.root / self.archives[0]['path']).write_bytes(b'replaced after admission')
            return result
        with patch.object(self, 'execute', side_effect=change), self.assertRaisesRegex(ValueError, 'changed during'):
            self.reload()
        self.assertFalse(self.mutations())

    def test_strict_reloaded_config_id_and_rootfs_must_both_match(self):
        for key in ['Id', 'RootFS']:
            with self.subTest(key=key):
                old = self.inspected[self.refs[0]][key]
                self.inspected[self.refs[0]][key] = 'sha256:' + 'e' * 64 if key == 'Id' else {'Layers': []}
                with self.assertRaisesRegex(ValueError, 'reloaded image differs'):
                    self.reload()
                self.inspected[self.refs[0]][key] = old

    def test_all_commands_share_one_deadline_and_expired_admission_has_no_docker_calls(self):
        self.elapsed_per_call = 90
        with self.assertRaisesRegex(ValueError, 'deadline exceeded'):
            self.reload()
        self.assertEqual([options['timeout'] for _, options in self.calls], [60, 60, 60, 60, 60, 150, 60])
        self.assertEqual(len([command for command, _ in self.calls if command[1:3] == ['image', 'load']]), 2)
        self.clock = 0
        self.calls.clear()
        ticks = iter([0, 601])
        with patch.object(cache.time, 'monotonic', side_effect=lambda: next(ticks)), patch.object(cache.subprocess, 'run') as execute:
            with self.assertRaisesRegex(ValueError, 'deadline exceeded'):
                cache.reload(self.root, self.source, self.archives, self.expected, {})
            execute.assert_not_called()
        for timeout in [0, 601, True]:
            self.refused_before_docker(timeout=timeout)


if __name__ == '__main__':
    unittest.main()
