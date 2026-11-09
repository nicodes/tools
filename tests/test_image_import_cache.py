"""OCI admission and non-destructive source alias imports in bounded execution."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import oci_fixture as fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'helpers'))
import image_import_cache as cache


class ImageImportCacheControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.source = 'a' * 40
        self.refs = ['registry.example/team/component-' + name + ':' + self.source for name in ['one','two','three']]
        self.expected, self.configs, self.layers, self.archives = {}, {}, {}, []
        for number, reference in enumerate(self.refs):
            layers = [fixture.filesystem('data/value-' + str(index), str(index).encode()) for index in range(number + 1)]
            config = {'os': 'linux', 'architecture': 'amd64', 'config': {}, 'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + hashlib.sha256(body).hexdigest() for body in layers]}}
            self.configs[reference], self.layers[reference] = config, layers
            self.expected[reference] = fixture.descriptor(fixture.encoded(config), fixture.CONFIG)['digest']
            self.archives.append(self.archive(str(number) + '.oci.tar', reference))
        self.calls, self.containers, self.inspect_errors = [], {}, {}
        self.container_states = {}
        self.listing = [self.row(ref, identity) for ref, identity in self.expected.items()]
        self.listing.append(self.row('registry.example/other/preserved:stable', 'sha256:' + 'f' * 64))
        self.inspected = {ref: {'Id': identity, 'RootFS': {'Layers': self.configs[ref]['rootfs']['diff_ids']}} for ref, identity in self.expected.items()}
        self.after_load = copy.deepcopy(self.inspected)
        self.known_images = {row['Id']: copy.deepcopy(row) for row in self.inspected.values()}
        self.clock, self.elapsed_per_call = 0, 0

    @staticmethod
    def row(ref, identity, digest='<none>'):
        repository, tag = ref.rsplit(':', 1)
        return {'Repository': repository, 'Tag': tag, 'ID': identity, 'RepoDigests': [] if digest == '<none>' else [ref.rsplit(':',1)[0] + '@' + digest]}

    def archive(self, name, reference, *, change=None, compressed=False):
        rows = fixture.contents(reference, self.configs[reference], self.layers[reference], compressed=compressed)
        if change: change(rows)
        return fixture.write(self.root / name, rows)

    def execute(self, command, **options):
        self.assertEqual(command[:2], ['podman','--remote=false'])
        command = ['podman', *command[2:]]
        self.calls.append((command, options)); self.clock += self.elapsed_per_call
        if command[1:3] == ['image','ls']: output = '\n'.join(json.dumps(row) for row in self.listing)
        elif command[1:3] == ['container','ls']: output = self.containers.get(command[-1].removeprefix('ancestor='), '')
        elif command[1:3] == ['container','inspect']:
            identity = command[-1]
            image = next(image for image, containers in self.containers.items() if identity in containers.splitlines())
            output = json.dumps([{'Id':identity, 'Image':image,
                                  'State':self.container_states.get(identity, {'Status':'configured','Running':False})}])
        elif command[1:3] == ['image','inspect']:
            reference = command[-1]
            if reference in self.inspect_errors:
                raise subprocess.CalledProcessError(1, command, stderr=self.inspect_errors[reference])
            value = copy.deepcopy(self.known_images.get(reference, {'Id':reference,'RootFS':{'Layers':[]}}))
            identity = cache.runtime.config_digest(value['Id'])
            matching = [row for row in self.listing if cache.runtime.config_digest(row['ID']) == identity]
            value['RepoTags'] = [row['Repository']+':'+row['Tag'] for row in matching if row['Tag'] != '<none>']
            value['RepoDigests'] = sorted({digest for row in matching for digest in row['RepoDigests']})
            output = json.dumps([value])
        elif command[1:3] == ['image','load']:
            with tarfile.open(command[-1]) as packed:
                descriptor = json.load(packed.extractfile('index.json'))['manifests'][0]
                self.assertEqual(set(descriptor), {'mediaType','digest','size'})
            for reference, identity in self.expected.items():
                self.known_images[identity] = copy.deepcopy(self.after_load[reference])
                if not any(cache.runtime.config_digest(row['ID']) == identity for row in self.listing):
                    self.listing.append({'Repository':'<none>','Tag':'<none>','ID':identity,'RepoDigests':[]})
            output = ''
        else: output = ''
        return subprocess.CompletedProcess(command, 0, stdout=output)

    def reload(self, **changes):
        values = dict(root=self.root, source=self.source, archives=self.archives, expected=self.expected, environment={'PATH': '/synthetic/native-only','CONTAINER_HOST':'ssh://foreign'}, timeout=600)
        values.update(changes)
        with patch.object(cache.subprocess, 'run', side_effect=self.execute), patch.object(cache.time, 'monotonic', side_effect=lambda: self.clock): return cache.reload(**values)

    def mutations(self):
        return [command for command, _ in self.calls if command[1:3] in [['image','rm'],['image','load'],['image','tag']]]

    def refused(self, **changes):
        self.calls.clear()
        with self.assertRaises((ValueError, OSError, tarfile.TarError)): self.reload(**changes)
        self.assertEqual(self.calls, [])

    def test_empty_declared_image_set_needs_no_engine_or_synthetic_image(self):
        self.assertEqual(self.reload(expected={},archives=[]),{})
        self.assertEqual(self.calls,[])
        self.refused(expected={})

    def test_missing_config_imports_exact_blobs_without_changing_existing_aliases(self):
        self.listing[0]['ID'] = 'sha256:'+'c'*64
        self.assertEqual(set(self.reload()), set(self.expected))
        mutations = self.mutations()
        self.assertEqual(len([row for row in mutations if row[2] == 'load']),len(self.archives))
        self.assertEqual(self.listing[0]['ID'],'sha256:'+'c'*64)
        self.assertFalse(any(row[2]=='tag' for row in mutations))
        self.assertFalse(any(value in command for command, _ in self.calls
                             for value in ['rm','rmi','--force','-f','prune','build','create','start']))
        self.assertTrue(all(options['cwd'] == self.root and options['env']['PATH'] == '/synthetic/native-only'
                            and 'CONTAINER_HOST' not in options['env'] for _, options in self.calls))

    def test_content_address_with_wrong_rootfs_refuses_before_effects(self):
        self.known_images[self.expected[self.refs[0]]]['RootFS']['Layers'] = []
        with self.assertRaisesRegex(ValueError,'RootFS disagree'): self.reload()
        self.assertEqual(self.mutations(), [])

    def test_healthy_exact_cache_is_reverified_without_mutation(self):
        self.assertEqual(set(self.reload()), set(self.expected)); self.assertEqual(self.mutations(), [])
        self.assertEqual([command[-1] for command,_ in self.calls if command[1:3] == ['image','inspect']], list(self.expected.values()) + list(self.expected.values()))

    def test_noop_keeps_borrowed_alias_digest_and_stopped_container(self):
        self.listing.append(self.row('registry.example/other/fixture:fixture', self.expected[self.refs[0]]))
        self.listing[1]['RepoDigests'] = ['registry.example/component@sha256:' + 'd' * 64]; self.containers[self.expected[self.refs[0]]] = 'stopped\n'
        before = copy.deepcopy((self.listing,self.containers,self.inspected))
        self.reload(); self.assertEqual(self.mutations(), []); self.assertEqual(before,(self.listing,self.containers,self.inspected))

    def test_import_preserves_alias_digest_and_containers_for_prior_and_incoming_ids(self):
        original = copy.deepcopy((self.listing,self.inspected))
        for plan in ['missing','prior']:
            for borrowed in ['alias','digest','container','incoming-alias','incoming-container']:
                with self.subTest(plan=plan,borrowed=borrowed):
                    self.calls.clear(); self.containers = {}; self.listing,self.inspected = copy.deepcopy(original)
                    if plan == 'missing': self.listing.pop(2)
                    else: self.listing[0]['ID'] = 'sha256:'+'c'*64
                    identity = self.expected[self.refs[0]] if borrowed.startswith('incoming-') else self.listing[0]['ID']
                    if borrowed.endswith('alias'):
                        self.listing.insert(0,self.row('registry.example/other/alias:stable',identity))
                    elif borrowed == 'digest':
                        self.listing[0]['RepoDigests'] = ['registry.example/component@sha256:'+'d'*64]
                    else: self.containers[identity] = '1'*64+'\n'
                    before_containers = copy.deepcopy(self.containers)
                    before_aliases = {(row['Repository']+':'+row['Tag'],row['ID']) for row in self.listing
                                      if row['Repository']+':'+row['Tag'] not in self.expected}
                    before_digests = {(row['ID'],digest) for row in self.listing for digest in row['RepoDigests']}
                    self.assertEqual(set(self.reload()),set(self.expected))
                    self.assertEqual(self.containers,before_containers)
                    self.assertTrue(before_aliases <= {(row['Repository']+':'+row['Tag'],row['ID']) for row in self.listing})
                    self.assertTrue(before_digests <= {(row['ID'],digest) for row in self.listing for digest in row['RepoDigests']})
                    self.assertFalse(any(row[2]=='rm' for row in self.mutations()))

    def test_import_postconditions_detect_lost_borrowed_alias_or_container_change(self):
        for borrowed in ['alias','digest','container']:
            with self.subTest(borrowed=borrowed):
                self.setUp()
                self.listing[0]['ID'] = 'sha256:'+'c'*64
                identity = self.listing[0]['ID']
                alias = 'registry.example/other/borrowed:fixture'
                self.listing.append(self.row(alias,identity,'sha256:'+'d'*64))
                self.containers[identity] = '1'*64+'\n'
                original = self.execute
                changed = False
                def corrupt(command,**options):
                    nonlocal changed
                    result = original(command,**options)
                    if command[2:4] == ['image','load'] and not changed:
                        changed = True
                        if borrowed == 'alias': self.listing = [row for row in self.listing if row['Repository']+':'+row['Tag'] != alias]
                        elif borrowed == 'digest':
                            for row in self.listing: row['RepoDigests'] = []
                        else: self.container_states['1'*64] = {'Status':'running','Running':True}
                    return result
                with patch.object(self,'execute',side_effect=corrupt), self.assertRaisesRegex(ValueError,'borrowed'):
                    self.reload()

    def test_missing_or_partial_cache_loads_without_removal(self):
        for rows in [[],self.listing[:1]]:
            self.calls.clear(); self.listing = rows; self.reload()
            self.assertEqual(len([row for row in self.mutations() if row[2] == 'load']),3)
            self.assertFalse(any(row[2] == 'rm' for row in self.mutations()))

    def test_unknown_native_store_inspect_failure_refuses_without_deletion(self):
        for message in ['permission denied','layer not known','readlink /foreign/overlay/l/missing: no such file or directory']:
            self.calls.clear(); self.inspect_errors[self.expected[self.refs[-1]]] = message
            with self.assertRaises(subprocess.CalledProcessError): self.reload()
            self.assertEqual(self.mutations(), [])

    def test_changed_tag_or_malformed_inspection_refuses(self):
        for value in [{'Id':'sha256:'+'e'*64,'RootFS':{'Layers':[]}}, {'Id':self.expected[self.refs[0]],'RootFS':{'Layers':'invalid'}}]:
            self.calls.clear(); self.known_images[self.expected[self.refs[0]]] = value
            with self.assertRaises(ValueError): self.reload()
            self.assertEqual(self.mutations(), [])

    def test_native_bare_config_ids_and_gzip_layers_keep_byte_identity(self):
        self.archives = [self.archive(str(index)+'-gzip.oci.tar',ref,compressed=True) for index,ref in enumerate(self.refs)]
        for row in self.listing: row['ID'] = row['ID'].removeprefix('sha256:')
        for row in self.inspected.values(): row['Id'] = row['Id'].removeprefix('sha256:')
        before = [(self.root / row['path']).read_bytes() for row in self.archives]
        self.reload(); self.assertEqual(before,[(self.root / row['path']).read_bytes() for row in self.archives])

    def test_source_hash_config_and_complete_image_set_are_required_before_runtime(self):
        self.refused(source='b'*40)
        self.refused(archives=[dict(self.archives[0],sha256='0'*64),*self.archives[1:]])
        self.refused(expected={**self.expected,self.refs[0]:'sha256:'+'e'*64})
        self.refused(archives=self.archives[:1])
        self.refused(archives=[*self.archives,self.archives[0]])

    def test_foreign_tag_repeated_json_and_unreachable_payload_refuse(self):
        def foreign(rows):
            index=json.loads(rows['index.json']); index['manifests'][0]['annotations']['org.opencontainers.image.ref.name']='registry.example/foreign/component:'+self.source
            rows['index.json']=fixture.encoded(index)
        for change in [foreign, lambda rows: rows.update({'index.json':b'{"schemaVersion":2,"schemaVersion":2}'}),lambda rows:rows.update({'foreign':b'undeclared'})]:
            archive=self.archive('bad.oci.tar',self.refs[0],change=change)
            self.refused(archives=[archive,*self.archives[1:]])

    def test_symlink_ancestor_link_and_hardlink_refuse_before_runtime(self):
        source=self.root/self.archives[0]['path']; (self.root/'linked').symlink_to(source)
        self.refused(archives=[dict(self.archives[0],path='linked'),*self.archives[1:]])
        (self.root/'directory').symlink_to(self.root,target_is_directory=True)
        self.refused(archives=[dict(self.archives[0],path='directory/'+source.name),*self.archives[1:]])
        os.link(source,self.root/'hard'); self.refused()

    def test_unsafe_linked_members_archive_bounds_and_multi_image_index_refuse(self):
        for name,kind in [('../escape',tarfile.REGTYPE),('link',tarfile.SYMTYPE),('hard',tarfile.LNKTYPE)]:
            path=self.root/'unsafe.oci.tar'
            with tarfile.open(path,'w') as output:
                member=tarfile.TarInfo(name); member.type=kind; member.linkname='index.json'; output.addfile(member,io.BytesIO())
            self.refused(archives=[{'path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}])
        with patch.object(cache,'MAX_ARCHIVE',1): self.refused()
        def multiple(rows):
            value=json.loads(rows['index.json']); value['manifests']*=2; rows['index.json']=fixture.encoded(value)
        self.refused(archives=[self.archive('multiple.oci.tar',self.refs[0],change=multiple)])

    def test_prior_build_replacement_checks_both_identity_sets_before_effects(self):
        prior={ref:'sha256:'+hashlib.sha256(('prior '+ref).encode()).hexdigest() for ref in self.refs}
        self.listing=[self.row(ref,value) for ref,value in prior.items()]
        self.reload()
        first=next(index for index,(row,_) in enumerate(self.calls) if row[1:3]==['image','load'])
        checked={row[-1] for row,_ in self.calls[:first] if row[1:3]==['container','ls']}
        self.assertEqual(checked,{'ancestor='+value for value in prior.values()})
        self.assertFalse(any(row[2]=='rm' for row in self.mutations()))

    def test_ambiguous_existing_tag_refuses_before_mutation(self):
        self.listing.append(self.row(self.refs[0],'sha256:'+'c'*64))
        with self.assertRaisesRegex(ValueError,'ambiguous'): self.reload()
        self.assertEqual(self.mutations(),[])

    def test_changed_archive_during_store_admission_refuses_before_mutation(self):
        execute=self.execute
        def changed(command,**options):
            result=execute(command,**options)
            if command[2:4]==['image','ls']: (self.root/self.archives[0]['path']).write_bytes(b'changed')
            return result
        with patch.object(self,'execute',side_effect=changed), self.assertRaisesRegex(ValueError,'changed during'): self.reload()
        self.assertEqual(self.mutations(),[])

    def test_reloaded_config_and_rootfs_must_both_match(self):
        self.listing=[]
        for key,value in [('Id','sha256:'+'e'*64),('RootFS',{'Layers':[]})]:
            self.listing=[]
            old=self.after_load[self.refs[0]][key]; self.after_load[self.refs[0]][key]=value
            with self.assertRaisesRegex(ValueError,'imported image differs'): self.reload()
            self.after_load[self.refs[0]][key]=old

    def test_one_deadline_bounds_all_commands_and_expiry_prevents_runtime(self):
        self.elapsed_per_call=90; self.listing=[]
        with self.assertRaisesRegex(ValueError,'deadline'): self.reload()
        self.assertTrue(all(0<options['timeout']<=600 for _,options in self.calls))
        self.clock=0; self.calls.clear(); ticks=iter([0,601])
        with patch.object(cache.time,'monotonic',side_effect=lambda:next(ticks)),patch.object(cache.subprocess,'run') as engine:
            with self.assertRaisesRegex(ValueError,'deadline'): cache.reload(self.root,self.source,self.archives,self.expected,{})
            engine.assert_not_called()
        for value in [0,601,True]: self.refused(timeout=value)
