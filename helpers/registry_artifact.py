"""Recover immutable OCI artifacts with explicit, read-only registry authority."""
import base64
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import time
import urllib.parse


def sibling(name):
    spec = importlib.util.spec_from_file_location('_tools_restore_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oci = sibling('oci_archive')
registry = sibling('oci_registry')
runtime = sibling('container_runtime')
CHUNK = 1 << 20
MAX_BYTES = oci.MAX_ARCHIVE


def destination_path(value):
    path = Path(value)
    if not path.is_absolute() or path.name in {'', '.', '..'}:
        raise ValueError('artifact destination requires a new absolute file')
    for parent in path.parents:
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError('artifact destination must not traverse links')
    info = path.parent.stat()
    if (info.st_uid != os.geteuid() or info.st_mode & 0o077
            or path.exists() or path.is_symlink()):
        raise ValueError('artifact destination requires a private owned directory and new file')
    return path


def credentials(authfile, server):
    if authfile is None:
        return '', ''
    path = runtime.checked_authfile(authfile)
    expected = path.stat()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        if oci.fingerprint(os.fstat(source.fileno())) != oci.fingerprint(expected):
            raise ValueError('registry authority changed while opening')
        data = json.loads(source.read((1 << 20) + 1), object_pairs_hook=runtime.unique_object)
        if oci.fingerprint(path.stat()) != oci.fingerprint(expected):
            raise ValueError('registry authority changed while reading')
    selected = data['auths'].get(server)
    if selected is None:
        return '', ''
    if not isinstance(selected, dict) or set(selected) != {'auth'} or not isinstance(selected['auth'], str):
        raise ValueError('registry authority requires explicit basic credentials for the exact registry')
    try:
        decoded = base64.b64decode(selected['auth'], validate=True).decode('utf-8')
        username, separator, password = decoded.partition(':')
        if not separator or not username or not password:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise ValueError('registry authority contains invalid encoded credentials') from None
    return username, password


class Reader:
    def __init__(self, artifact, authfile, timeout):
        if not isinstance(artifact, dict):
            raise ValueError('recovery requires an artifact descriptor')
        server, repository, identity = registry.reference(artifact.get('reference'))
        if artifact.get('digest') != identity:
            raise ValueError('artifact reference differs from its manifest identity')
        username, password = credentials(authfile, server)
        self.client = registry.Registry(server, repository, username=username,
            password=password, timeout=timeout, actions=('pull',))
        self.identity = identity

    def manifest(self):
        status, headers, body = self.client._request('GET', self.client._prefix + '/manifests/' + self.identity,
            {'Accept': registry.MANIFEST_TYPE})
        if (status != 200 or not 0 < len(body) <= oci.MAX_JSON
                or registry.digest(body) != self.identity
                or headers.get('docker-content-digest', self.identity) != self.identity):
            raise ValueError('registry did not return the exact bounded manifest')
        return body

    def blob(self, identity, length, target):
        if (not isinstance(identity, str) or not registry.DIGEST.fullmatch(identity)
                or type(length) is not int or not 0 <= length <= MAX_BYTES):
            raise ValueError('artifact blob identity or size is invalid')
        server, path = self.client.registry, self.client._prefix + '/blobs/' + identity
        authenticated = redirected = False
        while True:
            headers = {'Accept': 'application/octet-stream'}
            if not redirected and self.client._token:
                headers['Authorization'] = 'Bearer ' + self.client._token
            connection = http.client.HTTPSConnection(server, timeout=self.client._timeout())
            try:
                connection.request('GET', path, headers=headers)
                response = connection.getresponse()
                if response.status == 401 and not redirected and not authenticated:
                    challenge = response.getheader('WWW-Authenticate', '')
                    connection.close()
                    self.client._authenticate(challenge)
                    authenticated = True
                    continue
                if response.status == 307 and not redirected and self.client.registry == 'ghcr.io':
                    try:
                        location = urllib.parse.urlsplit(response.getheader('Location', ''))
                        if (location.scheme != 'https' or location.hostname != 'pkg-containers.githubusercontent.com'
                                or (location.port or 443) != 443 or location.username or location.password
                                or location.fragment or not location.path.startswith('/')
                                or any(char in location.geturl() for char in '\r\n\0')):
                            raise ValueError()
                    except ValueError:
                        raise ValueError('artifact blob redirect leaves its approved download authority') from None
                    server = location.hostname
                    path = location.path + ('?' + location.query if location.query else '')
                    redirected = True
                    continue
                declared = response.getheader('Content-Length')
                if (response.status != 200 or declared is not None and declared != str(length)
                        or response.getheader('Docker-Content-Digest', identity) != identity):
                    raise ValueError('registry did not return the declared artifact blob')
                hasher, count = hashlib.sha256(), 0
                with target.open('xb') as output:
                    os.chmod(target, 0o600)
                    while True:
                        if connection.sock is not None:
                            connection.sock.settimeout(self.client._timeout())
                        block = response.read1(min(CHUNK, length + 1 - count))
                        if not block:
                            break
                        count += len(block)
                        if count > length:
                            raise ValueError('artifact blob exceeds its declared size')
                        hasher.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if count != length or 'sha256:' + hasher.hexdigest() != identity:
                    raise ValueError('artifact blob differs from its declared bytes')
                return
            except (OSError, http.client.HTTPException):
                raise ValueError('artifact download failed within its approved authority') from None
            finally:
                connection.close()


def publish_file(source, target):
    # A hard link is an atomic no-replace operation; both files are on the
    # same private filesystem. Remove the temporary name before returning.
    os.link(source, target, follow_symlinks=False)
    source.unlink()
    descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return target


def fetch_static(artifact, destination, authfile=None, timeout=300):
    target = destination_path(destination)
    if (not isinstance(artifact, dict) or artifact.get('kind') != 'archive'
            or set(artifact) != {'kind', 'reference', 'digest', 'blob_digest', 'size_bytes'}):
        raise ValueError('static recovery requires an exact archive descriptor')
    expected = registry.artifact_manifest(artifact['blob_digest'], artifact['size_bytes'])
    if artifact['size_bytes'] > MAX_BYTES:
        raise ValueError('static recovery exceeds its byte bound')
    reader = Reader(artifact, authfile, timeout)
    if reader.manifest() != expected:
        raise ValueError('static artifact manifest differs from its admitted payload identity')
    with tempfile.TemporaryDirectory(prefix='.artifact-read-', dir=target.parent) as temporary:
        root = Path(temporary)
        reader.blob(registry.digest(registry.EMPTY_CONFIG), len(registry.EMPTY_CONFIG), root / 'config')
        if (root / 'config').read_bytes() != registry.EMPTY_CONFIG:
            raise ValueError('static artifact has an invalid empty config')
        source = root / 'payload'
        reader.blob(artifact['blob_digest'], artifact['size_bytes'], source)
        reader.client._timeout()
        return publish_file(source, target)


def checked_descriptor(value, media):
    if (not isinstance(value, dict) or not {'mediaType', 'digest', 'size'} <= set(value)
            or set(value) - {'mediaType', 'digest', 'size', 'annotations', 'platform'}
            or value['mediaType'] not in media or not isinstance(value['digest'], str)
            or not registry.DIGEST.fullmatch(value['digest'])
            or type(value['size']) is not int or not 0 <= value['size'] <= MAX_BYTES):
        raise ValueError('image recovery has an unsupported blob descriptor')


def fetch_image(artifact, destination, authfile=None, timeout=300):
    target = destination_path(destination)
    if (not isinstance(artifact, dict) or artifact.get('kind') != 'image'
            or set(artifact) != {'kind', 'reference', 'digest', 'config_digest', 'platform'}
            or not isinstance(artifact['config_digest'], str)
            or not registry.DIGEST.fullmatch(artifact['config_digest'])
            or artifact['platform'] not in {'linux/amd64', 'linux/arm64'}):
        raise ValueError('image recovery requires exact config and Linux platform identities')
    reader = Reader(artifact, authfile, timeout)
    raw = reader.manifest()
    manifest = oci.json_object(raw)
    if (set(manifest) - {'schemaVersion', 'mediaType', 'config', 'layers', 'annotations'}
            or manifest.get('schemaVersion') != 2 or manifest.get('mediaType') != oci.MANIFEST
            or not isinstance(manifest.get('layers'), list) or len(manifest['layers']) > 128):
        raise ValueError('image recovery has an unsupported manifest contract')
    checked_descriptor(manifest.get('config'), {oci.CONFIG})
    if manifest['config']['digest'] != artifact['config_digest'] or manifest['config']['size'] > oci.MAX_JSON:
        raise ValueError('image recovery config differs from its accepted identity')
    descriptors = [manifest['config'], *manifest['layers']]
    for layer in manifest['layers']:
        checked_descriptor(layer, oci.LAYERS)
    if sum(item['size'] for item in descriptors) + len(raw) > MAX_BYTES:
        raise ValueError('image recovery exceeds its aggregate byte bound')
    with tempfile.TemporaryDirectory(prefix='.artifact-read-', dir=target.parent) as temporary:
        root = Path(temporary)
        blobs = {}
        for item in descriptors:
            reader.client._timeout()
            identity = item['digest']
            if identity in blobs:
                if blobs[identity].stat().st_size != item['size']:
                    raise ValueError('repeated image blob has conflicting sizes')
                continue
            blobs[identity] = root / identity[7:]
            reader.blob(identity, item['size'], blobs[identity])
            if item is manifest['config']:
                config = oci.json_object(blobs[identity].read_bytes())
                if f"{config.get('os')}/{config.get('architecture')}" != artifact['platform']:
                    raise ValueError('image recovery platform differs from its accepted identity')
        source = root / 'image.oci.tar'
        descriptor = {'mediaType': oci.MANIFEST, 'digest': artifact['digest'], 'size': len(raw)}
        documents = {'oci-layout': registry.canonical({'imageLayoutVersion': '1.0.0'}),
            'index.json': registry.canonical({'schemaVersion': 2, 'manifests': [descriptor]}),
            'blobs/sha256/' + artifact['digest'][7:]: raw}
        with source.open('xb') as output:
            os.chmod(source, 0o600)
            with tarfile.open(fileobj=output, mode='w:') as archive:
                for name, body in documents.items():
                    info = tarfile.TarInfo(name); info.size = len(body); info.mode = 0o600
                    archive.addfile(info, io.BytesIO(body))
                for identity, path in sorted(blobs.items()):
                    reader.client._timeout()
                    info = tarfile.TarInfo('blobs/sha256/' + identity[7:])
                    info.size = path.stat().st_size; info.mode = 0o600
                    with path.open('rb') as incoming:
                        archive.addfile(info, incoming)
            output.flush(); os.fsync(output.fileno())
        with oci.OCIArchive(source, expected_reference=artifact['reference'],
                expected_config=artifact['config_digest'], timeout=min(reader.client._deadline - time.monotonic(), 600),
                max_expanded=MAX_BYTES) as accepted:
            if accepted.metadata['platform'] != artifact['platform']:
                raise ValueError('image recovery platform differs from its accepted identity')
            accepted.unchanged()
        return publish_file(source, target)
