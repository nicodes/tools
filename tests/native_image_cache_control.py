#!/usr/bin/env python3
"""Exercise real Podman OCI reloads in one new private native image store.

No host daemon or existing store is changed. The helper must preserve borrowed
aliases and stopped containers, import only immutable configs without changing existing tags, and reject
changed accepted bytes before any engine operation.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
import oci_fixture as fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'helpers'))
import container_runtime as runtime
import image_import_cache as cache
import oci_archive


def main():
    if os.geteuid() != 0:
        raise ValueError('native private-store control requires existing sudo capability')
    with tempfile.TemporaryDirectory(prefix='native-podman-cache-') as temporary:
        root = Path(temporary)
        storage = root / 'storage.conf'
        storage.write_text('[storage]\ndriver="vfs"\ngraphroot=' + json.dumps(str(root / 'graph')) + '\nrunroot=' + json.dumps(str(root / 'run')) + '\n')
        configuration = root / 'containers.conf'
        configuration.write_text('[engine]\ncgroup_manager="cgroupfs"\nevents_logger="file"\n')
        environment = dict(os.environ, CONTAINERS_STORAGE_CONF=str(storage), CONTAINERS_CONF=str(configuration))
        # A root-owned anonymous authfile is independently created for this
        # private store; credentials from the invoking CI user are never used.
        environment.pop('REGISTRY_AUTH_FILE', None)
        def podman(*args, check=True):
            return runtime.run(list(args), environment=environment, timeout=60, check=check, capture_output=True, text=True)
        def fixture_image(path, reference):
            metadata = oci_archive.load_image(path, reference, environment=environment)
            # Only explicit fixture construction assigns names. Cache imports
            # must preserve all of them, including a stale source alias.
            podman('tag', metadata['config_digest'], reference)
            return metadata
        info = json.loads(podman('info','--format','json').stdout)
        assert info['store']['graphRoot'] == str(root / 'graph') and info['store']['graphDriverName'] == 'vfs'
        source = hashlib.sha256(root.name.encode()).hexdigest()[:40]
        refs = ['registry.example/control-' + str(index) + ':' + source for index in range(3)]
        expected, archives, configs = {}, [], {}
        for number, reference in enumerate(refs):
            layers = [fixture.filesystem('data/' + str(index), ('accepted ' + str(index)).encode()) for index in range(number + 1)]
            config = {'os':'linux','architecture':'amd64','config':{'Cmd':['/never-started']},
                      'rootfs':{'type':'layers','diff_ids':['sha256:' + hashlib.sha256(body).hexdigest() for body in layers]}}
            path = root / (str(number) + '.oci.tar'); fixture.write(path,fixture.contents(reference,config,layers))
            metadata = fixture_image(path,reference)
            expected[reference] = metadata['config_digest']; configs[reference] = (config,layers)
            archives.append({'path':path.name,'sha256':metadata['archive_sha256']})
        sentinel = 'registry.example/unrelated:' + source
        config,layers = configs[refs[0]]; config = dict(config,created='2026-01-01T00:00:00Z')
        sentinel_path = root / 'unrelated.oci.tar'; fixture.write(sentinel_path,fixture.contents(sentinel,config,layers))
        sentinel_meta = fixture_image(sentinel_path,sentinel)
        unrelated = podman('create','--network=none',sentinel).stdout.strip()
        def invoke():
            observed = []
            original = subprocess.run
            def record(command, **kwargs):
                observed.append(command)
                return original(command, **kwargs)
            with patch.object(cache.subprocess,'run',side_effect=record):
                value = cache.reload(root,source,archives,expected,environment)
            return value, observed
        def effects(commands):
            return [row[2:4] for row in commands if row[2:4] in [['image','rm'],['image','load'],['image','tag']]]
        try:
            assert not effects(invoke()[1])
            alias = 'registry.example/borrowed:fixture'
            podman('tag',refs[1],alias)
            borrowed = podman('create','--network=none',refs[1]).stdout.strip()
            assert not effects(invoke()[1])
            assert json.loads(podman('inspect',borrowed).stdout)[0]['State']['Status'] in {'configured','created'}
            podman('rm',borrowed); podman('rmi',alias)
            # Bind the source tag to a genuinely different native config.
            prior_config, prior_layers = configs[refs[0]]
            prior_config = dict(prior_config,created='2026-02-02T00:00:00Z')
            prior_path = root/'prior.oci.tar'; fixture.write(prior_path,fixture.contents(refs[0],prior_config,prior_layers))
            prior = fixture_image(prior_path,refs[0])['config_digest']
            assert prior != expected[refs[0]]
            accepted_bytes = {record['path']:(root/record['path']).read_bytes() for record in archives}
            def image_record(identity):
                value = json.loads(podman('image','inspect',identity).stdout)[0]
                return {'id':runtime.config_digest(value['Id']), 'rootfs':value['RootFS'],
                        'tags':set(value['RepoTags']), 'digests':set(value['RepoDigests'])}
            for identity in [prior,expected[refs[0]]]:
                # A missing independent accepted config requires import while
                # borrowed users of old and accepted configs remain untouched.
                podman('rmi','--no-prune',expected[refs[2]])
                container = podman('create','--network=none',identity).stdout.strip()
                before = json.loads(podman('container','inspect',container).stdout)[0]
                admitted, commands = invoke()
                after = json.loads(podman('container','inspect',container).stdout)[0]
                assert before['Id'] == after['Id'] == container
                assert before['Image'] == after['Image'] and before['State'] == after['State']
                assert {ref:row['id'] for ref,row in admitted.items()} == expected
                assert all(effect[1] == 'load' for effect in effects(commands)), commands
                podman('rm',container)
                podman('rmi','--no-prune',expected[refs[2]])
                podman('tag',identity,alias)
                before = image_record(identity)
                admitted, commands = invoke()
                after = image_record(identity)
                assert before['id'] == after['id'] == identity and before['rootfs'] == after['rootfs']
                assert before['tags'] <= after['tags'] and before['digests'] <= after['digests']
                assert image_record(alias)['id'] == identity
                assert all(effect[1] == 'load' for effect in effects(commands)), commands
                podman('rmi',alias)
            podman('rmi','--no-prune',expected[refs[2]])
            admitted, commands = invoke()
            assert {ref:row['id'] for ref,row in admitted.items()} == expected
            assert effects(commands).count(['image','load']) == len(archives)
            assert all(effect[1] == 'load' for effect in effects(commands)), commands
            assert image_record(refs[0])['id'] == prior, 'Cache import overwrote an existing source alias'
            assert accepted_bytes == {record['path']:(root/record['path']).read_bytes() for record in archives}
            assert not effects(invoke()[1])
            assert runtime.inspect_images([sentinel],environment=environment)[0]['Id'] == sentinel_meta['config_digest']
            assert json.loads(podman('inspect',unrelated).stdout)[0]['State']['Status'] in {'configured','created'}
            # Changed accepted input must fail before the native runtime runs.
            path=root/archives[0]['path']; data=path.read_bytes(); path.write_bytes(data[:-1]+b'x')
            with patch.object(cache.subprocess,'run') as engine:
                try: cache.reload(root,source,archives,expected,environment)
                except ValueError: pass
                else: raise AssertionError('changed OCI archive accepted')
                engine.assert_not_called()
        finally:
            podman('rm',unrelated,check=False)
            for reference in [*refs,sentinel]: podman('rmi','--no-prune',reference,check=False)
        # Entire graph/run roots were created here and disappear with this
        # owned temporary directory. No global reset/prune command is used.
    print('NATIVE_OCI_CACHE_OK: exact no-op; protected prior/incoming aliases and stopped containers; unnamed config import; tamper refusal')


if __name__ == '__main__': main()
