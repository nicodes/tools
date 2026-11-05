#!/usr/bin/env python3
"""Prepared pinned-scanner controls for synthetic data, secrets and binary files.

Only ``setup`` may pull the scanner or download its database. ``run`` scans tiny
handcrafted images offline, never builds/starts them, and removes only its own
exact fixture tags. All disposable data belongs to the explicit temp directory.
"""
import argparse
import contextlib
import datetime
import hashlib
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

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS / 'helpers'))
from image_security_report import image_scan_findings
from image_scan_context import regular
import image_scan_fixture as fixture

SCANNER = fixture.SCANNER
VERSION = '0.70.0'
MAX_DATABASE = 2 << 30


def docker(arguments, *, timeout=60, environment=None):
    environment = dict(os.environ if environment is None else environment)
    for name in ['GH_TOKEN', 'GITHUB_TOKEN', 'MISE_GITHUB_TOKEN']:
        environment.pop(name, None)
    return subprocess.run(['docker', *arguments], env=environment, timeout=timeout,
                          check=True, capture_output=True).stdout


def inspected(reference, environment=None):
    values = json.loads(docker(['image', 'inspect', reference], environment=environment))
    if not isinstance(values, list) or len(values) != 1:
        raise ValueError('native scan requires one existing immutable image')
    return values[0]


def scanner_identity():
    value = inspected(SCANNER)
    if value['Config']['Labels']['org.opencontainers.image.version'] != VERSION:
        raise ValueError('native scan requires the exact prepared scanner version')
    return {'reference': SCANNER, 'id': value['Id'], 'version': VERSION}


def file_digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@contextlib.contextmanager
def private_store(root):
    """Use the same isolated classic store as the existing native cache controls."""
    if os.geteuid() != 0:
        raise ValueError('prepare existing sudo capability before the isolated native scanner control')
    profiles = Path('/sys/kernel/security/apparmor/profiles').read_text().splitlines()
    if not any(value.startswith('docker-default ') for value in profiles):
        raise ValueError('existing Docker AppArmor profile required; profile installation is forbidden')
    # Managed containerd also creates Unix sockets below exec-root. Keep this
    # private path short regardless of the prepared scanner cache's location.
    directory = Path(tempfile.mkdtemp(prefix='scan-store-', dir='/tmp'))
    environment = dict(os.environ, DOCKER_HOST='unix://' + str(directory / 'docker.sock'),
                       DOCKER_CONFIG=str(directory / 'client'))
    for name in ['DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DOCKER_API_VERSION',
                 'GH_TOKEN', 'GITHUB_TOKEN', 'MISE_GITHUB_TOKEN']:
        environment.pop(name, None)
    (directory / 'client').mkdir()
    configuration = directory / 'daemon.json'
    configuration.write_text(json.dumps({'features': {'containerd-snapshotter': False}, 'storage-driver': 'overlay2'}))
    with (directory / 'daemon.log').open('w') as log:
        process = subprocess.Popen(['/usr/bin/dockerd', '--config-file', str(configuration),
            '--host', environment['DOCKER_HOST'], '--group=' + str(os.getgid()),
            '--data-root', str(directory / 'data'), '--exec-root', str(directory / 'exec'),
            '--pidfile', str(directory / 'daemon.pid'), '--iptables=false', '--ip6tables=false',
            '--bridge=none', '--ip-forward=false', '--ip-masq=false', '--log-level=error'],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=environment)
        try:
            end = time.monotonic() + 30; ready = None
            while process.poll() is None and time.monotonic() < end:
                try:
                    ready = json.loads(docker(['info', '--format', '{{json .}}'], environment=environment, timeout=min(3, end-time.monotonic())))
                    break
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    time.sleep(.2)
            if not ready or ready['DockerRootDir'] != str(directory / 'data') or ready['Driver'] != 'overlay2':
                log.flush()
                raise ValueError('native scan requires its own bounded classic Docker store: ' +
                                 (directory / 'daemon.log').read_text()[-3000:])
            yield environment
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)
            shutil.rmtree(directory)


def setup(directory):
    directory = directory.absolute(); directory.mkdir(parents=True, exist_ok=True)
    cache = directory / 'cache'; cache.mkdir(exist_ok=True)
    docker(['pull', SCANNER], timeout=120)
    identity = scanner_identity()
    docker(['run', '--rm', '--pull', 'never', '--network', 'host', '-v', str(cache) + ':/cache', SCANNER,
            'image', '--download-db-only', '--cache-dir', '/cache', '--skip-version-check', '--no-progress'], timeout=180)
    database = cache / 'db/trivy.db'; regular(database, MAX_DATABASE)
    value = {'version': 1, 'scanner': identity, 'database_sha256': file_digest(database)}
    (directory / 'prepared.json').write_text(json.dumps(value, sort_keys=True) + '\n')
    print('Prepared pinned scanner and vulnerability database for offline native controls', flush=True)


def run(directory):
    directory = directory.absolute()
    regular(directory / 'prepared.json', 65536)
    prepared = json.loads((directory / 'prepared.json').read_text())
    if set(prepared) != {'version', 'scanner', 'database_sha256'} or prepared['version'] != 1:
        raise ValueError('prepare the closed pinned native scanner context first')
    scanner = scanner_identity()
    if scanner != prepared['scanner']:
        raise ValueError('native scanner differs from explicit setup')
    cache = directory / 'cache'; database = cache / 'db/trivy.db'
    regular(database, MAX_DATABASE)
    if file_digest(database) != prepared['database_sha256']:
        raise ValueError('prepared native scanner database changed')
    proof, loaded = {}, []
    with tempfile.TemporaryDirectory(prefix='offline-image-scan-', dir=directory) as temporary:
        root = Path(temporary)
        source = hashlib.sha256(root.name.encode()).hexdigest()[:40]
        cases = {'data': b'value: harmless\n',
                 'secret': b'token: ghp_' + hashlib.sha256(b'synthetic scanner sensitivity fixture').hexdigest()[:36].encode() + b'\n',
                 'binary': b'\x7fELF\x00' + bytes(range(256))}
        with private_store(root) as target:
          try:
            for name, body in cases.items():
                reference = 'example.invalid/security-control-' + name + ':' + source
                incoming = root / (name + '.tar')
                fixture.archive(incoming, reference=reference, source=source, second_body=body)
                docker(['image', 'load', '--input', str(incoming)], environment=target); loaded.append(reference)
                before_raw = inspected(reference, target)
                before = {key: before_raw[key] for key in ['Id', 'RootFS', 'Config', 'Os', 'Architecture']}
                output = root / name; output.mkdir()
                started = datetime.datetime.now(datetime.timezone.utc).isoformat()
                base = ['run', '--rm', '--pull', 'never', '--network', 'none',
                        '-v', target['DOCKER_HOST'][7:] + ':/var/run/docker.sock:ro',
                        '-v', str(cache) + ':/cache', '-v', str(output) + ':/reports', SCANNER,
                        'image', '--quiet', '--skip-version-check', '--image-src', 'docker',
                        '--cache-dir', '/cache', '--skip-db-update', '--skip-java-db-update', '--offline-scan', '--timeout', '30s']
                docker([*base, '--scanners', 'vuln,secret', '--format', 'json', '--output', '/reports/security.json', reference], timeout=90)
                docker([*base, '--format', 'cyclonedx', '--output', '/reports/sbom.json', reference], timeout=90)
                after_raw = inspected(reference, target)
                after = {key: after_raw[key] for key in ['Id', 'RootFS', 'Config', 'Os', 'Architecture']}
                context = {'reference': reference, 'source': source, 'scanner': scanner, 'before': before, 'after': after,
                           'started_at': started, 'finished_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
                report = json.loads((output / 'security.json').read_text()); sbom = json.loads((output / 'sbom.json').read_text())
                assert type(before_raw['Size']) is int and before_raw['Size']==after_raw['Size'] and 0<before_raw['Size']<=16<<20
                assert type(report['Metadata']['Size']) is int and 0<report['Metadata']['Size']<=16<<20
                assert report['Metadata']['Size']==sum(layer['Size'] for layer in report['Metadata']['Layers'])
                if name=='data':assert before_raw['Size']!=report['Metadata']['Size']
                saved = output / 'image.tar'
                docker(['image', 'save', '--output', str(saved), reference], environment=target)
                with tarfile.open(saved) as packet:
                    members = set(packet.getnames())
                    observation = {'case': name, 'image_id': before['Id'],
                        'manifest': json.load(packet.extractfile('manifest.json')),
                        'repositories': json.load(packet.extractfile('repositories')) if 'repositories' in members else None,
                        'members': [{'path': item.name, 'size': item.size} for item in packet.getmembers()]}
                (directory / 'last-native-archive-metadata.json').write_text(json.dumps(observation, indent=2) + '\n')
                retained = directory / 'native-evidence' / name; retained.mkdir(parents=True, exist_ok=True)
                for filename in ['security.json', 'sbom.json', 'image.tar']:
                    destination = retained / filename
                    if destination.exists() or destination.is_symlink():
                        regular(destination, 32 << 20); destination.unlink()
                    with destination.open('xb') as stream:
                        stream.write((output / filename).read_bytes())
                (retained / 'context.json').write_text(json.dumps(context, sort_keys=True) + '\n')
                if name == 'binary':
                    try:
                        image_scan_findings(report, sbom, context=context, data_archive=saved)
                    except ValueError as error:
                        assert 'binary' in str(error) or 'plaintext' in str(error), str(error)
                    else:
                        raise AssertionError('unsupported binary image was admitted without complete results')
                else:
                    findings = image_scan_findings(report, sbom, context=context, data_archive=saved)
                    if name == 'data':
                        assert 'Results' not in report and findings == [], report.keys()
                        assert sbom['components'] == [] and sbom['vulnerabilities'] == []
                    else:
                        assert findings and any(row.get('RuleID') == 'github-pat' for row in findings), 'synthetic known secret was not blocked'
                proof[name] = {'image_id': before['Id'], 'rootfs': before['RootFS']['Layers'],
                               'actual_inspect_size':before_raw['Size'],'report_size':report['Metadata']['Size'],
                               'results_omitted': 'Results' not in report,
                               'security_sha256': file_digest(output / 'security.json'), 'sbom_sha256': file_digest(output / 'sbom.json')}
            if file_digest(database) != prepared['database_sha256']:
                raise ValueError('offline native controls changed the prepared vulnerability database')
            (directory / 'native-control.json').write_text(json.dumps({'version': 1, 'scanner': scanner, 'cases': proof,
                'network': 'none', 'pull': 'never', 'builds': False, 'containers_started_from_fixture_images': False}, indent=2) + '\n')
          finally:
            for reference in reversed(loaded):
                docker(['image', 'rm', '--no-prune', '--', reference], environment=target)
    print('NATIVE_IMAGE_SCAN_CONTEXT_OK: omitted data results authenticated; synthetic secret blocked; unsupported binary refused; offline prepared scanner', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['setup', 'run'])
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    globals()[args.mode](args.directory)
