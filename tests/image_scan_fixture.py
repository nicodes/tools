"""Small synthetic image/report fixtures; no build, compiler or downloads."""
import copy
import datetime
import hashlib
import io
import json
from pathlib import Path
import tarfile
import oci_fixture

SOURCE = 'a' * 40
REFERENCE = 'example.invalid/security-fixture:' + SOURCE
SCANNER = 'docker.io/aquasec/trivy@sha256:be1190afcb28352bfddc4ddeb71470835d16462af68d310f9f4bca710961a41e'


def archive(path, *, reference=REFERENCE, source=SOURCE, body=b'value: harmless\n',
            filename='data/settings.yaml', mode=0o644, member_type=tarfile.REGTYPE,
            entrypoint=None, extra_outer=False, second_body=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w', format=tarfile.USTAR_FORMAT) as output:
        item = tarfile.TarInfo(filename); item.mode = mode; item.type = member_type
        item.size = len(body) if member_type == tarfile.REGTYPE else 0
        if member_type == tarfile.SYMTYPE:
            item.linkname = '/outside'
        output.addfile(item, io.BytesIO(body) if item.size else None)
    layers = [stream.getvalue()]
    if second_body is not None:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w', format=tarfile.USTAR_FORMAT) as output:
            item = tarfile.TarInfo('data/payload.txt'); item.mode = 0o644; item.size = len(second_body)
            output.addfile(item, io.BytesIO(second_body))
        layers.append(stream.getvalue())
    diffs = ['sha256:' + hashlib.sha256(layer).hexdigest() for layer in layers]
    configuration = {'architecture': 'amd64', 'os': 'linux',
        'created': '2026-01-01T00:00:00Z',
        'config': {'Labels': {'org.opencontainers.image.revision': source},
                   'Cmd': ['/' + filename]},
        'rootfs': {'type': 'layers', 'diff_ids': diffs},
        'history': [{'created': '2026-01-01T00:00:00Z', 'created_by': 'synthetic data fixture'} for _ in layers]}
    if entrypoint is not None:
        configuration['config']['Entrypoint'] = entrypoint
    raw = json.dumps(configuration, sort_keys=True, separators=(',', ':')).encode()
    identity = 'sha256:' + hashlib.sha256(raw).hexdigest()
    rows = oci_fixture.contents(reference, configuration, layers)
    if extra_outer: rows['unbound.txt'] = b'foreign metadata\n'
    oci_fixture.write(path, rows)
    inspected = {'Id': identity, 'RootFS': {'Type': 'layers', 'Layers': diffs},
        'Config': copy.deepcopy(configuration['config']), 'Os': 'linux', 'Architecture': 'amd64'}
    return inspected, configuration, len(layers[0])


def evidence(inspected, configuration, size, *, reference=REFERENCE, source=SOURCE):
    now = datetime.datetime.now(datetime.timezone.utc)
    version = '0.70.0'
    context = {'reference': reference, 'source': source,
        'scanner': {'reference': SCANNER, 'id': 'sha256:' + 'b' * 64, 'version': version},
        'before': copy.deepcopy(inspected), 'after': copy.deepcopy(inspected),
        'started_at': (now - datetime.timedelta(seconds=1)).isoformat(),
        'finished_at': (now + datetime.timedelta(seconds=1)).isoformat()}
    report = {'SchemaVersion': 2, 'Trivy': {'Version': version},
        'ReportID': '12345678-1234-4234-9234-123456789abc', 'CreatedAt': now.isoformat(),
        'ArtifactType': 'container_image', 'ArtifactName': reference, 'ArtifactID': 'sha256:' + 'c' * 64,
        'Metadata': {'ImageID': inspected['Id'], 'Reference': reference,
            'DiffIDs': inspected['RootFS']['Layers'], 'ImageConfig': copy.deepcopy(configuration),
            'Layers': [{'DiffID': item, 'Size': size} for item in inspected['RootFS']['Layers']], 'Size': size}}
    properties = [{'name': 'aquasecurity:trivy:' + key, 'value': value}
        for key, value in [('SchemaVersion', '2'), ('ImageID', inspected['Id']), ('Reference', reference),
                           ('Labels:org.opencontainers.image.revision', source)]]
    properties += [{'name': 'aquasecurity:trivy:DiffID', 'value': item} for item in inspected['RootFS']['Layers']]
    properties.sort(key=lambda item: (item['name'], item['value']))
    sbom = {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
        'metadata': {'timestamp': now.isoformat(), 'tools': {'components': [
            {'type': 'application', 'group': 'aquasecurity', 'name': 'trivy', 'version': version}]},
            'component': {'type': 'container', 'name': reference, 'bom-ref': 'synthetic-container', 'properties': properties}},
        'components': [], 'vulnerabilities': [], 'dependencies': [{'ref': 'synthetic-container', 'dependsOn': []}]}
    return report, sbom, context
