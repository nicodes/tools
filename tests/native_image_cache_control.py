#!/usr/bin/env python3
"""Exercise the actual helper against a synthetic corrupted private Docker store.

No application/container is started. No existing daemon, profile or image is
changed; every Docker command targets the newly created private Unix socket.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS / 'cookbook'))
import image_import_cache

SOURCE = 'a' * 40


def layer(name, body):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        item = tarfile.TarInfo(name); item.size = len(body); item.mode = 0o644; item.mtime = 0
        archive.addfile(item, io.BytesIO(body))
    return output.getvalue()


def image_archive(path, reference, layers, variant):
    diff_ids = ['sha256:' + hashlib.sha256(body).hexdigest() for body in layers]
    configuration = json.dumps({'architecture': 'amd64', 'os': 'linux',
        'created': '2026-01-01T00:00:00Z', 'config': {'Labels': {'fixture.variant': variant}},
        'rootfs': {'type': 'layers', 'diff_ids': diff_ids},
        'history': [{'created': '2026-01-01T00:00:00Z', 'created_by': 'synthetic fixture'} for _ in layers]},
        sort_keys=True, separators=(',', ':')).encode()
    identity = hashlib.sha256(configuration).hexdigest()
    names = [hashlib.sha256(body).hexdigest() + '/layer.tar' for body in layers]
    manifest = json.dumps([{'Config': identity + '.json', 'RepoTags': [reference], 'Layers': names}]).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, 'w') as archive:
        for name, body in [('manifest.json', manifest), (identity + '.json', configuration), *zip(names, layers)]:
            item = tarfile.TarInfo(name); item.size = len(body); item.mode = 0o644; item.mtime = 0
            archive.addfile(item, io.BytesIO(body))
    return {'id': 'sha256:' + identity, 'rootfs': diff_ids}


def main():
    if os.geteuid() != 0:
        raise ValueError('prepare the existing sudo capability before running the native cache control')
    profiles = Path('/sys/kernel/security/apparmor/profiles').read_text().splitlines()
    if not any(value.startswith('docker-default ') for value in profiles):
        raise ValueError('existing docker-default profile required; profile installation is forbidden')
    directory = Path(tempfile.mkdtemp(prefix='tools-private-image-cache-', dir='/tmp'))
    environment = dict(os.environ, DOCKER_HOST='unix://' + str(directory / 'docker.sock'),
        DOCKER_CONFIG=str(directory / 'client'))
    for name in ['DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DOCKER_API_VERSION',
                 'GH_TOKEN', 'GITHUB_TOKEN', 'MISE_GITHUB_TOKEN']:
        environment.pop(name, None)
    (directory / 'client').mkdir()
    configuration = directory / 'daemon.json'
    configuration.write_text(json.dumps({'features': {'containerd-snapshotter': False}, 'storage-driver': 'overlay2'}))
    source_root = directory / 'source'; source_root.mkdir()
    refs = ['example.invalid/cache-fixture-base:' + SOURCE, 'example.invalid/cache-fixture-derived:' + SOURCE]
    base = layer('base-marker', b'synthetic base\n')
    middle = layer('middle-marker', b'synthetic middle\n')
    upper = layer('upper-marker', b'synthetic upper\n')
    paths = ['.artifacts/base.tar', '.artifacts/derived.tar']
    images = {refs[0]: image_archive(source_root / paths[0], refs[0], [base, middle], 'base'),
              refs[1]: image_archive(source_root / paths[1], refs[1], [base, middle, upper], 'derived')}
    expected = {ref: value['id'] for ref, value in images.items()}
    archives = [{'path': name, 'sha256': hashlib.sha256((source_root / name).read_bytes()).hexdigest()} for name in paths]
    sentinel = 'example.invalid/unrelated-sentinel:fixture'
    sentinel_path = source_root / '.artifacts/sentinel.tar'
    sentinel_image = image_archive(sentinel_path, sentinel, [layer('unrelated-marker', b'unrelated fixture\n')], 'unrelated')
    proc = None
    def docker(*args, check=True, timeout=30):
        return subprocess.run(['docker', *args], env=environment, check=check, timeout=timeout,
            capture_output=True, text=True)
    def inspect(ref):
        values = json.loads(docker('image', 'inspect', ref).stdout)
        assert len(values) == 1
        return values[0]
    def refuse_without_cache_effect(reason):
        commands = []
        native_run = subprocess.run
        def observed(command, *args, **kwargs):
            commands.append(command)
            return native_run(command, *args, **kwargs)
        with patch.object(image_import_cache.subprocess, 'run', side_effect=observed):
            try:
                image_import_cache.reload(source_root, SOURCE, archives, expected, environment, timeout=120)
            except ValueError:
                pass
            else: raise AssertionError(reason)
        assert commands and all(command[:3] not in [['docker', 'image', 'rm'], ['docker', 'image', 'load']]
                                for command in commands), commands
    with (directory / 'daemon.log').open('w') as log:
        try:
            proc = subprocess.Popen(['/usr/bin/dockerd', '--config-file', str(configuration),
                '--host', environment['DOCKER_HOST'], '--group=' + str(os.getgid()),
                '--data-root', str(directory / 'data'), '--exec-root', str(directory / 'exec'),
                '--pidfile', str(directory / 'daemon.pid'), '--iptables=false', '--ip6tables=false',
                '--bridge=none', '--ip-forward=false', '--ip-masq=false', '--log-level=error'],
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=environment)
            ready = None
            for _ in range(80):
                if proc.poll() is not None: break
                response = docker('info', '--format', '{{json .}}', check=False, timeout=3)
                if response.returncode == 0:
                    ready = json.loads(response.stdout); break
                time.sleep(.25)
            assert ready and ready['DockerRootDir'] == str(directory / 'data') and ready['Driver'] == 'overlay2'
            assert image_import_cache.reload(source_root, SOURCE, archives, expected, environment, timeout=120) == images
            # Exercise the running engine's native save format as well as the
            # deterministic classic fixture; both carry identical image IDs.
            saved = ['.artifacts/native-base.tar', '.artifacts/native-derived.tar']
            for reference, name in zip(refs, saved):
                docker('image', 'save', '--output', str(source_root / name), reference, timeout=60)
            archives = [{'path': name, 'sha256': hashlib.sha256((source_root / name).read_bytes()).hexdigest()} for name in saved]
            docker('image', 'load', '--input', str(sentinel_path), timeout=60)
            unrelated = docker('container', 'create', '--network', 'none', sentinel, '/never-started').stdout.strip()
            target = docker('container', 'create', '--network', 'none', refs[1], '/never-started').stdout.strip()
            refuse_without_cache_effect('a real created target container bypassed reset admission')
            assert json.loads(docker('container', 'inspect', target).stdout)[0]['State']['Status'] == 'created'
            assert all(inspect(ref)['Id'] == value['id'] for ref, value in images.items())
            docker('container', 'rm', target)
            alias = 'example.invalid/foreign-owner:fixture'
            docker('image', 'tag', refs[1], alias)
            refuse_without_cache_effect('a real out-of-scope alias bypassed reset admission')
            assert inspect(alias)['Id'] == expected[refs[1]]
            assert all(inspect(ref)['Id'] == value['id'] for ref, value in images.items())
            docker('image', 'rm', alias)
            rows = [json.loads(value) for value in docker('image', 'ls', '--all', '--no-trunc',
                '--format', '{{json .}}').stdout.splitlines()]
            ancestors = {}
            for row in rows:
                if row['Repository'] == row['Tag'] == '<none>' and row['ID'] not in expected.values():
                    value = inspect(row['ID'])
                    layers = value['RootFS']['Layers']
                    if any(len(layers) < len(image['rootfs']) and image['rootfs'][:len(layers)] == layers
                           for image in images.values()):
                        ancestors[row['ID']] = layers
            current = inspect(refs[1]); upper_path = Path(current['GraphDriver']['Data']['UpperDir'])
            store = directory / 'data/overlay2'
            assert upper_path.is_relative_to(store)
            link = (upper_path.parent / 'lower').read_text().split(':')[0]
            assert link.startswith('l/') and len(Path(link).parts) == 2
            broken = store / link
            assert broken.is_symlink() and broken.resolve().is_relative_to(store)
            broken.unlink()
            failed = docker('image', 'inspect', refs[1], check=False)
            assert failed.returncode and 'readlink ' in failed.stderr and 'no such file' in failed.stderr
            commands = []
            native_run = subprocess.run
            def observed(command, *args, **kwargs):
                commands.append(command)
                return native_run(command, *args, **kwargs)
            with patch.object(image_import_cache.subprocess, 'run', side_effect=observed):
                repaired = image_import_cache.reload(source_root, SOURCE, archives, expected, environment, timeout=120)
            removal, = [command for command in commands if command[:3] == ['docker', 'image', 'rm']]
            assert removal[3:5] == ['--no-prune', '--'] and set(removal[5:]) == set(refs)
            assert repaired == images
            for ref, value in images.items():
                observed = inspect(ref)
                assert observed['Id'] == value['id'] and observed['RootFS']['Layers'] == value['rootfs']
            assert inspect(sentinel)['Id'] == sentinel_image['id']
            for identity, layers in ancestors.items():
                kept_ancestor = inspect(identity)
                assert kept_ancestor['Id'] == identity and kept_ancestor['RootFS']['Layers'] == layers
            kept = json.loads(docker('container', 'inspect', unrelated).stdout)[0]
            assert kept['Image'] == sentinel_image['id'] and kept['State']['Status'] == 'created'
            print('NATIVE_IMAGE_CACHE_RESET_CONTROL_OK: real corruption repaired; unrelated image/container retained; alias/target refusals; no container started')
            print('NATIVE_IMAGE_CACHE_NO_PRUNE_CONTROL_OK: exact nonforce removals passed --no-prune; exposed untagged ancestors retained=' + str(len(ancestors)))
        except BaseException:
            print((directory / 'daemon.log').read_text()[-3000:], file=sys.stderr)
            raise
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL); proc.wait(timeout=3)
            assert proc is None or proc.poll() is not None
            shutil.rmtree(directory)


if __name__ == '__main__': main()
