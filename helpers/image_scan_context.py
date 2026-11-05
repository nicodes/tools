"""Authenticate paired image scan evidence before admitting a data-only result.

An absent scanner result is meaningful only with a completed, fresh scan of the
same immutable image and an independently authenticated, bounded data filesystem.
This module never extracts archives or starts an image.
"""
import datetime
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import time
import uuid

MAX_ARCHIVE = 32 << 20
MAX_DATA = 16 << 20
MAX_FILE = 1 << 20
MAX_MEMBERS = 4096
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
REVISION = re.compile(r'[a-f0-9]{40}\Z')
SCANNER = re.compile(r'aquasec/trivy@sha256:[a-f0-9]{64}\Z')
DATA_SUFFIXES = {'', '.json', '.yaml', '.yml', '.toml', '.ini', '.conf', '.txt', '.csv', '.xml'}
LEGACY_CONFIG_FIELDS = {'Hostname', 'Domainname', 'User', 'AttachStdin', 'AttachStdout', 'AttachStderr',
    'ExposedPorts', 'Tty', 'OpenStdin', 'StdinOnce', 'Env', 'Cmd', 'Healthcheck', 'ArgsEscaped', 'Image',
    'Volumes', 'WorkingDir', 'Entrypoint', 'NetworkDisabled', 'MacAddress', 'OnBuild', 'Labels',
    'StopSignal', 'StopTimeout', 'Shell'}


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('image scan evidence repeats a JSON field')
        result[key] = value
    return result


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('image scan evidence needs a UTC timestamp')
    try:
        result = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('image scan evidence has an invalid timestamp') from error
    if result.utcoffset() != datetime.timedelta(0):
        raise ValueError('image scan evidence needs a UTC timestamp')
    return result


def identity(value):
    if (not isinstance(value, dict) or set(value) != {'Id', 'RootFS', 'Config', 'Os', 'Architecture'}
            or not isinstance(value['Id'], str) or not DIGEST.fullmatch(value['Id'])
            or value['Os'] != 'linux' or value['Architecture'] not in {'amd64', 'arm64'}
            or not isinstance(value['Config'], dict) or not isinstance(value['RootFS'], dict)
            or value['RootFS'].get('Type') != 'layers'):
        raise ValueError('image scan needs an exact inspected Linux image identity')
    layers = value['RootFS'].get('Layers')
    if (not isinstance(layers, list) or not 1 <= len(layers) <= 128
            or any(not isinstance(item, str) or not DIGEST.fullmatch(item) for item in layers)):
        raise ValueError('image scan needs ordered immutable RootFS layer identities')
    return value


def validate(report, sbom, context):
    if (not isinstance(context, dict) or set(context) != {
            'reference', 'source', 'scanner', 'before', 'after', 'started_at', 'finished_at'}):
        raise ValueError('image scan requires a closed completion and identity context')
    reference, source = context['reference'], context['source']
    if (not isinstance(reference, str) or not isinstance(source, str) or not REVISION.fullmatch(source)
            or not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*(?::[a-f0-9]{40}|@sha256:[a-f0-9]{64})', reference)
            or (not '@sha256:' in reference and not reference.endswith(':' + source))):
        raise ValueError('image scan reference must bind its exact source or immutable digest')
    before, after = identity(context['before']), identity(context['after'])
    if before != after:
        raise ValueError('image identity changed during security scanning')
    labels = before['Config'].get('Labels') or {}
    if not isinstance(labels, dict):
        raise ValueError('image scan labels must match actual inspected metadata')
    scanner = context['scanner']
    if (not isinstance(scanner, dict) or set(scanner) != {'reference', 'id', 'version'}
            or not isinstance(scanner['reference'], str) or not SCANNER.fullmatch(scanner['reference'])
            or not isinstance(scanner['id'], str) or not DIGEST.fullmatch(scanner['id'])
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', str(scanner['version']))):
        raise ValueError('image scan requires the prepared immutable scanner identity and version')
    start, finish = timestamp(context['started_at']), timestamp(context['finished_at'])
    if not datetime.timedelta(0) <= finish - start <= datetime.timedelta(minutes=20):
        raise ValueError('image scan completion interval is invalid or unbounded')
    if (not isinstance(report, dict) or type(report.get('SchemaVersion')) is not int
            or report['SchemaVersion'] != 2 or report.get('ArtifactType') != 'container_image'
            or report.get('ArtifactName') != reference
            or not isinstance(report.get('ArtifactID'), str) or not DIGEST.fullmatch(report['ArtifactID'])
            or report.get('Trivy') != {'Version': scanner['version']}):
        raise ValueError('image security report schema, target or scanner differs')
    try:
        uuid.UUID(report.get('ReportID', ''))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError('image security report has no scan identifier') from error
    metadata = report.get('Metadata')
    if (not isinstance(metadata, dict) or metadata.get('ImageID') != before['Id']
            or metadata.get('Reference') != reference or metadata.get('DiffIDs') != before['RootFS']['Layers']):
        raise ValueError('image security report does not bind the inspected image and layers')
    image_config = metadata.get('ImageConfig')
    if (not isinstance(image_config, dict) or image_config.get('os') != before['Os']
            or image_config.get('architecture') != before['Architecture']
            or image_config.get('rootfs') != {'type': 'layers', 'diff_ids': before['RootFS']['Layers']}
            or not isinstance(image_config.get('config'), dict)
            or (image_config.get('config', {}).get('Labels') or {}) != labels):
        raise ValueError('image security report configuration differs from the inspected source')
    for key in ['Cmd', 'Entrypoint']:
        if (image_config.get('config', {}).get(key) or []) != (before['Config'].get(key) or []):
            raise ValueError('image security report launch configuration differs')
    layers = metadata.get('Layers')
    if (not isinstance(layers, list) or [item.get('DiffID') for item in layers if isinstance(item, dict)] != before['RootFS']['Layers']
            or len(layers) != len(before['RootFS']['Layers'])
            or any(type(item.get('Size')) is not int or item['Size'] <= 0 for item in layers)):
        raise ValueError('image security report has incomplete or different layer evidence')
    if (not isinstance(sbom, dict) or sbom.get('bomFormat') != 'CycloneDX'
            or sbom.get('specVersion') != '1.6' or type(sbom.get('version')) is not int or sbom['version'] != 1
            or not isinstance(sbom.get('components'), list) or not isinstance(sbom.get('dependencies'), list)
            or not isinstance(sbom.get('vulnerabilities'), list)):
        raise ValueError('image SBOM schema or inventories are missing')
    sbom_metadata = sbom.get('metadata', {})
    if not isinstance(sbom_metadata, dict) or not isinstance(sbom_metadata.get('tools'), dict):
        raise ValueError('image SBOM scanner metadata is missing')
    tools = sbom_metadata.get('tools', {}).get('components', [])
    if (not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict)
            or tools[0].get('group') != 'aquasecurity' or tools[0].get('name') != 'trivy'
            or tools[0].get('version') != scanner['version']):
        raise ValueError('image SBOM scanner differs from the completed scan')
    component = sbom_metadata.get('component', {})
    if (not isinstance(component, dict) or component.get('type') != 'container'
            or component.get('name') != reference or not isinstance(component.get('bom-ref'), str)
            or not component['bom-ref']):
        raise ValueError('image SBOM container identity is missing')
    properties = component.get('properties')
    if not isinstance(properties, list):
        raise ValueError('image SBOM identity properties are missing')
    values = {}
    for item in properties:
        if (not isinstance(item, dict) or set(item) != {'name', 'value'}
                or not isinstance(item['name'], str) or not isinstance(item['value'], str)):
            raise ValueError('image SBOM has invalid identity properties')
        values.setdefault(item['name'], []).append(item['value'])
    for key, wanted in {
        'SchemaVersion': ['2'], 'ImageID': [before['Id']], 'Reference': [reference],
        # The pinned CycloneDX encoder sorts repeated properties by value.
        # Preserve multiplicity here; ordered layer authority remains the
        # inspected RootFS, JSON image config and authenticated save manifest.
        'DiffID': sorted(before['RootFS']['Layers']),
    }.items():
        if values.get('aquasecurity:trivy:' + key) != wanted:
            raise ValueError('image SBOM source, identity or layers differ')
    revision_property = values.get('aquasecurity:trivy:Labels:org.opencontainers.image.revision')
    if revision_property != ([labels['org.opencontainers.image.revision']] if 'org.opencontainers.image.revision' in labels else None):
        raise ValueError('image SBOM revision label differs from the inspected source')
    for date in [report.get('CreatedAt'), sbom_metadata.get('timestamp')]:
        created = timestamp(date)
        if not start - datetime.timedelta(seconds=2) <= created <= finish + datetime.timedelta(seconds=2):
            raise ValueError('image security evidence is stale or outside this completed scan')
    return before


def regular(path, maximum):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('image evidence path may not contain traversal')
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0):
            raise ValueError('image evidence ancestor is linked or not a directory')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= maximum
            or getattr(info, 'st_reparse_tag', 0)):
        raise ValueError('image evidence must be a bounded regular unlinked file')
    return path, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def legacy_metadata(outer, names, allowed, config, layer_count):
    """Admit only the inert v1 compatibility records emitted by Docker save.

    Modern config/layer/OCI descriptors were already authenticated independently.
    These bounded content-addressed JSON records never become filesystem data.
    """
    def normalized(value):
        return {key: item for key, item in value.items() if item not in (None, False, '', [], {})}
    rows = {}
    actual = normalized(config.get('config', {}))
    for name, member in names.items():
        if not member.isfile() or name in allowed:
            continue
        if not re.fullmatch(r'blobs/sha256/[a-f0-9]{64}', name) or not 0 < member.size <= 65536:
            raise ValueError('data image archive contains files outside its closed image contract')
        body = outer.extractfile(member).read(65537)
        if hashlib.sha256(body).hexdigest() != name.rsplit('/', 1)[-1]:
            raise ValueError('legacy image metadata differs from its content address')
        value = json.loads(body, object_pairs_hook=object_pairs)
        full = {'architecture', 'config', 'container_config', 'created', 'id', 'os'}
        placeholder = {'container_config', 'created', 'id', 'os'}
        if (not isinstance(value, dict) or set(value) not in (full, full | {'parent'}, placeholder, placeholder | {'parent'})
                or value['os'] != config['os']
                or not isinstance(value['id'], str) or not re.fullmatch('[a-f0-9]{64}', value['id'])
                or value['id'] in rows or ('parent' in value and not re.fullmatch('[a-f0-9]{64}', str(value['parent'])))
                or not isinstance(value['container_config'], dict)
                or set(value['container_config']) - LEGACY_CONFIG_FIELDS or normalized(value['container_config'])):
            raise ValueError('legacy image metadata differs from its closed source configuration')
        if 'config' in value:
            if (value['architecture'] != config['architecture'] or not isinstance(value['config'], dict)
                    or set(value['config']) - LEGACY_CONFIG_FIELDS or normalized(value['config']) != actual):
                raise ValueError('legacy image configuration differs from the authenticated config')
            timestamp(value['created'])
        else:
            # Docker emits inert epoch placeholders for nonterminal layers.
            # Its local timezone spelling varies; the instant must remain zero.
            try: created = datetime.datetime.fromisoformat(value['created'].replace('Z', '+00:00'))
            except (ValueError, AttributeError) as error: raise ValueError('invalid legacy layer placeholder time') from error
            if created.utcoffset() is None or created.timestamp() != 0:
                raise ValueError('legacy layer placeholder must describe the Unix epoch')
        rows[value['id']] = value; allowed.add(name)
    if rows:
        parents = {row.get('parent') for row in rows.values() if row.get('parent')}
        heads = set(rows) - parents
        if len(rows) != layer_count or len(heads) != 1:
            raise ValueError('legacy metadata does not describe one bounded image layer chain')
        head = next(iter(heads)); visited = set()
        if normalized(rows[head].get('config', {})) != actual:
            raise ValueError('legacy metadata final source configuration differs')
        while head:
            if head not in rows or head in visited:
                raise ValueError('legacy metadata layer chain is incomplete or cyclic')
            visited.add(head); head = rows[head].get('parent')
        if len(visited) != len(rows):
            raise ValueError('legacy metadata contains another image chain')


def data_image(path, reference, inspected, report, deadline=None):
    """Authenticate every saved layer before classifying its bounded text files."""
    deadline = deadline if deadline is not None else time.monotonic() + 60
    path, original = regular(path, MAX_ARCHIVE)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
    from image_import_cache import archive_images, remaining
    # Apply the much smaller data-image bounds before the generic archive
    # authenticator reads any possibly compressed filesystem layer.
    with tarfile.open(path, 'r:*') as outer:
        members, expanded = {}, 0
        for member in outer:
            remaining(deadline)
            expanded += member.size
            members[member.name] = member
            if len(members) > MAX_MEMBERS or expanded > MAX_ARCHIVE or member.size < 0:
                raise ValueError('data image archive exceeds its member or expanded byte bound')
        manifest_member = members.get('manifest.json')
        if manifest_member is None or not manifest_member.isfile() or not 0 < manifest_member.size <= MAX_FILE:
            raise ValueError('data image archive manifest is missing or oversized')
        manifests = json.load(outer.extractfile(manifest_member), object_pairs_hook=object_pairs)
        if not isinstance(manifests, list) or len(manifests) != 1 or not isinstance(manifests[0], dict):
            raise ValueError('data image requires exactly one saved image')
        layers = manifests[0].get('Layers')
        if not isinstance(layers, list) or not 1 <= len(layers) <= 128:
            raise ValueError('data image requires a bounded saved layer inventory')
        uncompressed = 0
        for name in layers:
            member = members.get(name) if isinstance(name, str) else None
            if member is None or not member.isfile():
                raise ValueError('data image layer is missing')
            with outer.extractfile(member) as stream:
                magic = stream.read(2); stream.seek(0)
                content = gzip.GzipFile(fileobj=stream) if magic == b'\x1f\x8b' else stream
                try:
                    while chunk := content.read(1 << 20):
                        remaining(deadline); uncompressed += len(chunk)
                        if uncompressed > MAX_ARCHIVE:
                            raise ValueError('data image filesystem layers exceed their expanded byte bound')
                finally:
                    if content is not stream: content.close()
    accepted = archive_images(path, {reference: inspected['Id']}, deadline)
    if accepted != {reference: {'id': inspected['Id'], 'rootfs': inspected['RootFS']['Layers']}}:
        raise ValueError('data image archive differs from the scanned image')
    if (report['Trivy']['Version'] != '0.70.0' or report['Metadata'].get('OS') not in (None, {})
            or inspected['Config'].get('Entrypoint')):
        raise ValueError('empty scan results require a supported data image without an entrypoint')
    data, directories, total, files = {}, set(), 0, 0
    with tarfile.open(path, 'r:*') as outer:
        manifest = json.load(outer.extractfile('manifest.json'), object_pairs_hook=object_pairs)
        if len(manifest) != 1 or manifest[0]['RepoTags'] != [reference]:
            raise ValueError('data image archive must contain exactly the scanned tag')
        config = json.load(outer.extractfile(manifest[0]['Config']), object_pairs_hook=object_pairs)
        allowed = {'manifest.json', manifest[0]['Config'], *manifest[0]['Layers']}
        names = {item.name.rstrip('/'): item for item in outer.getmembers()}
        if 'index.json' in names:
            allowed.update({'index.json', 'oci-layout'})
            index = json.load(outer.extractfile('index.json'), object_pairs_hook=object_pairs)
            allowed.update('blobs/sha256/' + item['digest'][7:] for item in index['manifests'])
        if 'repositories' in names:
            repository, tag = reference.rsplit(':', 1)
            repositories = json.load(outer.extractfile('repositories'), object_pairs_hook=object_pairs)
            top = PurePosixPath(manifest[0]['Layers'][-1])
            top_id = top.name if re.fullmatch('[a-f0-9]{64}', top.name) else top.parent.name
            if not re.fullmatch('[a-f0-9]{64}', top_id) or repositories != {repository: {tag: top_id}}:
                raise ValueError('data image archive repository mapping differs')
            allowed.add('repositories')
        for name in manifest[0]['Layers']:
            parent = PurePosixPath(name).parent.as_posix()
            for suffix in ['json', 'VERSION']:
                candidate = parent + '/' + suffix
                if candidate in names and parent != '.':
                    if names[candidate].size > 65536:
                        raise ValueError('data image legacy layer metadata is oversized')
                    body = outer.extractfile(candidate).read(65537)
                    if suffix == 'VERSION' and body not in {b'1.0', b'1.0\n'}:
                        raise ValueError('data image legacy layer version is unsupported')
                    if suffix == 'json' and not isinstance(json.loads(body, object_pairs_hook=object_pairs), dict):
                        raise ValueError('data image legacy layer metadata is invalid')
                    allowed.add(candidate)
        legacy_metadata(outer, names, allowed, config, len(manifest[0]['Layers']))
        if (config.get('config', {}).get('Labels') != inspected['Config'].get('Labels')
                or config.get('config', {}).get('Entrypoint')
                or (config.get('config', {}).get('Cmd') or []) != (inspected['Config'].get('Cmd') or [])):
            raise ValueError('data image archive configuration differs from the inspected source')
        for layer in manifest[0]['Layers']:
            remaining(deadline)
            with outer.extractfile(layer) as stream, tarfile.open(fileobj=stream, mode='r:*') as archive:
                seen = set()
                for member in archive:
                    remaining(deadline)
                    name = PurePosixPath(member.name)
                    if member.isdir() and member.name in {'.', './'}:
                        continue
                    if (name.is_absolute() or '..' in name.parts or '\\' in member.name
                            or str(name) != member.name.rstrip('/') or str(name) in seen
                            or any(part.startswith('.wh.') for part in name.parts)
                            or any(ord(char) < 32 for char in member.name)
                            or not (member.isdir() or member.isfile()) or member.mode & 0o7000):
                        raise ValueError('data image layer contains unsafe, linked, special or repeated paths')
                    seen.add(str(name)); files += 1
                    if files > MAX_MEMBERS:
                        raise ValueError('data image has too many filesystem entries')
                    if member.isdir():
                        if str(name) in data:
                            raise ValueError('data image changes a file into a directory')
                        directories.add(str(name))
                        continue
                    if str(name) in directories or any(parent.as_posix() in data for parent in name.parents):
                        raise ValueError('data image filesystem has conflicting file and directory paths')
                    if (member.mode & 0o111 or not 0 <= member.size <= MAX_FILE
                            or name.suffix.lower() not in DATA_SUFFIXES):
                        raise ValueError('empty scan results cannot admit executable, script or unsupported files')
                    total += member.size
                    if total > MAX_DATA:
                        raise ValueError('data image exceeds its plaintext filesystem size bound')
                    body = archive.extractfile(member).read(MAX_FILE + 1)
                    try:
                        text = body.decode('utf-8')
                    except UnicodeDecodeError as error:
                        raise ValueError('empty scan results cannot admit binary image content') from error
                    if (len(body) != member.size or text.startswith('#!')
                            or any(ord(char) < 32 and char not in '\n\r\t' for char in text)):
                        raise ValueError('empty scan results require complete non-script plaintext files')
                    data[str(name)] = {'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}
    command = inspected['Config'].get('Cmd') or []
    if command and (not isinstance(command, list) or len(command) != 1 or not isinstance(command[0], str)
                    or not command[0].startswith('/') or command[0][1:] not in data):
        raise ValueError('data image command must refer only to an authenticated non-executable data file')
    if not data or regular(path, MAX_ARCHIVE)[1] != original:
        raise ValueError('data image archive is empty or changed during admission')
    return {'archive_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'files': data}


def admit_empty(report, sbom, context, data_archive):
    inspected = validate(report, sbom, context)
    if (inspected['Config'].get('Labels') or {}).get('org.opencontainers.image.revision') != context['source']:
        raise ValueError('empty image results require an explicit matching source revision label')
    if 'Results' in report and report['Results'] != []:
        raise ValueError('only omitted or empty image result lists may use data-image admission')
    if sbom['components'] != [] or sbom['vulnerabilities'] != []:
        raise ValueError('empty image results disagree with the scanned software inventory')
    component = sbom['metadata']['component']
    if sbom['dependencies'] != [{'ref': component['bom-ref'], 'dependsOn': []}]:
        raise ValueError('empty image results require a closed empty software dependency graph')
    if data_archive is None:
        raise ValueError('empty image scan results need authenticated data-only image bytes')
    return data_image(data_archive, context['reference'], inspected, report)
