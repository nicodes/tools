#!/usr/bin/env python3
"""Prepared pinned-scanner controls for synthetic data, secrets and binary files.

Only ``setup`` may pull the scanner or download its database. ``run`` scans tiny
handcrafted images offline, never builds/starts them, and removes only its own
exact fixture tags. All disposable data belongs to the explicit temp directory.
"""
import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS / 'helpers'))
from image_security_report import filesystem_scan_findings, image_scan_findings
from image_scan_context import regular
import container_runtime as runtime
import oci_archive
import image_scan_fixture as fixture
import filesystem_scan_context

SCANNER = fixture.SCANNER
VERSION = '0.70.0'
MAX_DATABASE = 2 << 30


def podman(arguments, *, timeout=60, environment=None):
    environment = dict(os.environ if environment is None else environment)
    for name in ['GH_TOKEN', 'GITHUB_TOKEN', 'MISE_GITHUB_TOKEN']:
        environment.pop(name, None)
    return runtime.run(arguments, environment=environment, timeout=timeout,
                          check=True, capture_output=True).stdout


def inspected(reference, environment=None):
    values = runtime.inspect_images([reference], environment=environment)
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


def filesystem_controls(root, directory, cache, scanner, source):
    """Exercise the actual pinned FS encoder for clean bytes and a known secret."""
    for name,body in [('data',b'accepted static filesystem bytes\n'),
                      ('secret',b'token: ghp_'+hashlib.sha256(b'synthetic FS sensitivity control').hexdigest()[:36].encode()+b'\n')]:
        output=root/('fs-'+name);output.mkdir();tree=output/'tree';tree.mkdir()
        (tree/'index.txt').write_bytes(body);(tree/'index.txt').chmod(0o644)
        incoming=output/'static.tar.gz';raw=io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w',format=tarfile.USTAR_FORMAT) as archive:
            entry=tarfile.TarInfo('index.txt');entry.size=len(body);entry.mode=0o644;archive.addfile(entry,io.BytesIO(body))
        incoming.write_bytes(gzip.compress(raw.getvalue(),mtime=0))
        artifact={'kind':'archive','digest':'sha256:'+file_digest(incoming),'size_bytes':incoming.stat().st_size,
            'media_type':filesystem_scan_context.registry.STATIC_LAYER}
        artifact['reference']='ghcr.io/example/native-fs@'+filesystem_scan_context.registry.digest(
            filesystem_scan_context.registry.artifact_manifest(artifact['digest'],artifact['size_bytes']))
        inventory=filesystem_scan_context.archive_inventory(incoming,artifact)
        started=datetime.datetime.now(datetime.timezone.utc).isoformat()
        base=['run','--rm','--pull=never','--network=none','-v',str(tree)+':/inputs/static:ro',
            '-v',str(cache)+':/cache','-v',str(output)+':/reports',SCANNER,'fs','--quiet','--skip-version-check',
            '--cache-dir','/cache','--skip-db-update','--skip-java-db-update','--offline-scan','--timeout','30s']
        podman([*base,'--scanners','vuln,secret','--format','json','--output','/reports/security.json','/inputs/static'],timeout=90)
        podman([*base,'--format','cyclonedx','--output','/reports/sbom.json','/inputs/static'],timeout=90)
        context={'source':source,'release_digest':'sha256:'+hashlib.sha256(source.encode()).hexdigest(),
            'artifact':artifact,'target':'/inputs/static','scanner':scanner,'started_at':started,
            'finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'before':inventory,
            'after':{'index.txt':{'sha256':file_digest(tree/'index.txt'),'size':(tree/'index.txt').stat().st_size,
                'mode':(tree/'index.txt').stat().st_mode&0o777}}}
        report=json.loads((output/'security.json').read_text());sbom=json.loads((output/'sbom.json').read_text())
        findings=filesystem_scan_findings(report,sbom,context,incoming)
        if name=='data':assert findings==[] and 'Results' not in report,'clean FS encoder context differs'
        else:assert any(item.get('RuleID')=='github-pat' for item in findings),'static known secret was not blocked'
        retained=directory/'native-evidence'/('fs-'+name);retained.mkdir(parents=True,exist_ok=True)
        for filename in ['security.json','sbom.json','static.tar.gz']:
            target=retained/filename
            if target.exists():regular(target,32<<20);target.unlink()
            with target.open('xb') as stream:stream.write((output/filename).read_bytes())
        (retained/'context.json').write_text(json.dumps(context,sort_keys=True)+'\n')



def setup(directory):
    directory = directory.absolute(); directory.mkdir(parents=True, exist_ok=True)
    cache = directory / 'cache'; cache.mkdir(exist_ok=True)
    podman(['pull', SCANNER], timeout=120)
    identity = scanner_identity()
    podman(['run', '--rm', '--pull', 'never', '--network', 'host', '-v', str(cache) + ':/cache', SCANNER,
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
    proof = {}
    with tempfile.TemporaryDirectory(prefix='offline-image-scan-', dir=directory) as temporary:
        root = Path(temporary)
        source = hashlib.sha256(root.name.encode()).hexdigest()[:40]
        cases = {'data': b'value: harmless\n',
                 'secret': b'token: ghp_' + hashlib.sha256(b'synthetic scanner sensitivity fixture').hexdigest()[:36].encode() + b'\n',
                 'binary': b'\x7fELF\x00' + bytes(range(256))}
        for name, body in cases.items():
            reference = 'example.invalid/security-control-' + name + ':' + source
            output = root / name; output.mkdir()
            incoming = output / 'image.oci.tar'
            fixture.archive(incoming, reference=reference, source=source, second_body=body)
            with oci_archive.OCIArchive(incoming, reference) as image:
                metadata = image.metadata
                layout = image.materialize(output / 'layout')
                configuration = metadata['config']
                before = {'Id': metadata['config_digest'], 'RootFS': {'Type':'layers','Layers':configuration['rootfs']['diff_ids']},
                          'Config': configuration['config'], 'Os':configuration['os'],'Architecture':configuration['architecture']}
                started = datetime.datetime.now(datetime.timezone.utc).isoformat()
                base = ['run', '--rm', '--pull=never', '--network=none',
                        '-v', str(layout) + ':/inputs/image.oci:ro',
                        '-v', str(cache) + ':/cache', '-v', str(output) + ':/reports', SCANNER,
                        'image', '--quiet', '--skip-version-check', '--input', '/inputs/image.oci',
                        '--cache-dir', '/cache', '--skip-db-update', '--skip-java-db-update', '--offline-scan', '--timeout', '30s']
                podman([*base, '--scanners', 'vuln,secret', '--format', 'json', '--output', '/reports/security.json'], timeout=90)
                podman([*base, '--format', 'cyclonedx', '--output', '/reports/sbom.json'], timeout=90)
                image.unchanged()
                with oci_archive.OCIArchive(incoming, reference) as after_image:
                    assert after_image.metadata == metadata
                context = {'reference':reference,'source':source,'scanner':scanner,'before':before,'after':before,
                           'archive':{'sha256':metadata['archive_sha256'],'target':'/inputs/image.oci'},
                           'started_at':started,'finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
                report=json.loads((output/'security.json').read_text()); sbom=json.loads((output/'sbom.json').read_text())
                assert type(report['Metadata']['Size']) is int and 0 < report['Metadata']['Size'] <= 16 << 20
                assert report['Metadata']['Size'] == sum(layer['Size'] for layer in report['Metadata']['Layers'])
                assert report['Metadata']['Size'] != incoming.stat().st_size
                retained=directory/'native-evidence'/name; retained.mkdir(parents=True,exist_ok=True)
                for filename in ['security.json','sbom.json','image.oci.tar']:
                    destination=retained/filename
                    if destination.exists() or destination.is_symlink(): regular(destination,32<<20); destination.unlink()
                    with destination.open('xb') as stream: stream.write((output/filename).read_bytes())
                (retained/'context.json').write_text(json.dumps(context,sort_keys=True)+'\n')
                if name == 'binary':
                    try: image_scan_findings(report,sbom,context=context,input_archive=incoming,data_archive=incoming)
                    except ValueError as error: assert 'binary' in str(error) or 'plaintext' in str(error), str(error)
                    else: raise AssertionError('unsupported binary image was admitted without complete results')
                else:
                    findings=image_scan_findings(report,sbom,context=context,input_archive=incoming,data_archive=incoming)
                    if name == 'data':
                        assert 'Results' not in report and findings == [], report.keys()
                        assert sbom['components'] == [] and sbom['vulnerabilities'] == []
                    else: assert findings and any(row.get('RuleID') == 'github-pat' for row in findings), 'synthetic known secret was not blocked'
                proof[name]={'image_id':before['Id'],'rootfs':before['RootFS']['Layers'],
                             'archive_bytes':incoming.stat().st_size,'report_size':report['Metadata']['Size'],
                             'results_omitted':'Results' not in report,'security_sha256':file_digest(output/'security.json'),
                             'sbom_sha256':file_digest(output/'sbom.json')}
        filesystem_controls(root,directory,cache,scanner,source)
        if file_digest(database) != prepared['database_sha256']: raise ValueError('offline controls changed prepared database')
        (directory/'native-control.json').write_text(json.dumps({'version':1,'scanner':scanner,'cases':proof,
            'network':'none','pull':'never','builds':False,'containers_started_from_fixture_images':False},indent=2)+'\n')
    print('NATIVE_IMAGE_SCAN_CONTEXT_OK: omitted data results authenticated; synthetic secret blocked; unsupported binary refused; offline prepared scanner', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['setup', 'run'])
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    globals()[args.mode](args.directory)
