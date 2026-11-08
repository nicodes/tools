"""Authenticate one OCI image archive without extracting archive-selected paths."""
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile
import time

_spec = importlib.util.spec_from_file_location('_tools_native_container_runtime', Path(__file__).with_name('container_runtime.py'))
runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runtime)

MAX_ARCHIVE = 2 << 30
MAX_EXPANDED = 16 << 30
MAX_JSON = 1 << 20
MAX_MEMBERS = 20000
MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
CONFIG = 'application/vnd.oci.image.config.v1+json'
LAYERS = {'application/vnd.oci.image.layer.v1.tar', 'application/vnd.oci.image.layer.v1.tar+gzip'}
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise ValueError('OCI image inspection exceeded its deadline')
    return value


def fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def json_object(body):
    value = json.loads(body, object_pairs_hook=runtime.unique_object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite OCI metadata is refused')))
    if not isinstance(value, dict):
        raise ValueError('OCI metadata requires an object')
    return value


class OCIArchive:
    def __init__(self, path, expected_reference=None, expected_config=None, *, timeout=600, max_expanded=MAX_EXPANDED, max_members=MAX_MEMBERS):
        self.path = Path(path)
        self.reference = expected_reference
        self.config_digest = expected_config
        self.deadline = time.monotonic() + timeout
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
            raise ValueError('OCI archive requires a finite deadline')
        if (type(max_expanded) is not int or not 1 <= max_expanded <= MAX_EXPANDED
                or type(max_members) is not int or not 1 <= max_members <= MAX_MEMBERS):
            raise ValueError('OCI bounds must fit the reviewed archive limits')
        self.max_expanded, self.max_members = max_expanded, max_members
        self.stream = None
        self.archive = None
        self.members = {}
        self.metadata = None

    def __enter__(self):
        try:
            for parent in [self.path, *self.path.parents]:
                if parent.is_symlink():
                    raise ValueError('OCI archives must not traverse links')
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_ARCHIVE:
                raise ValueError('OCI archive must be one bounded unlinked regular file')
            self.identity = fingerprint(info)
            self.stream = self.path.open('rb')
            if fingerprint(os.fstat(self.stream.fileno())) != self.identity:
                raise ValueError('OCI archive changed before opening')
            digest = hashlib.sha256()
            while body := self.stream.read(1 << 20):
                remaining(self.deadline)
                digest.update(body)
            self.archive_sha256 = digest.hexdigest()
            self.stream.seek(0)
            self.archive = tarfile.open(fileobj=self.stream, mode='r:')
            total = 0
            for member in self.archive:
                remaining(self.deadline)
                name = member.name.removeprefix('./').rstrip('/')
                if member.isdir() and name in {'', '.'}:
                    continue
                path = PurePosixPath(name)
                if (not name or path.is_absolute() or '..' in path.parts or str(path) != name
                        or '\\' in name or any(ord(char) < 32 for char in name)
                        or name in self.members or not (member.isfile() or member.isdir())
                        or member.size < 0 or member.isdir() and member.size):
                    raise ValueError('OCI archive contains unsafe, linked or repeated paths')
                self.members[name] = member
                total += member.size
                if len(self.members) > self.max_members or total > self.max_expanded:
                    raise ValueError('OCI archive exceeds its expanded bound')
            self.metadata = self._inspect()
            self.unchanged()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.archive is not None:
            self.archive.close()
        if self.stream is not None:
            self.stream.close()

    def unchanged(self):
        if (self.path.is_symlink() or fingerprint(self.path.stat()) != self.identity
                or fingerprint(os.fstat(self.stream.fileno())) != self.identity):
            raise ValueError('OCI archive changed during verification')
        remaining(self.deadline)

    def small(self, name):
        member = self.members.get(name)
        if member is None or not member.isfile() or not 0 < member.size <= MAX_JSON:
            raise ValueError('OCI metadata is missing or oversized')
        with self.archive.extractfile(member) as source:
            return source.read(MAX_JSON + 1)

    def descriptor(self, value, media):
        if (not isinstance(value, dict) or not {'mediaType', 'digest', 'size'} <= set(value)
                or set(value) - {'mediaType', 'digest', 'size', 'annotations', 'platform'}
                or value['mediaType'] not in media or not isinstance(value['digest'], str)
                or not DIGEST.fullmatch(value['digest']) or type(value['size']) is not int
                or value['size'] < 0):
            raise ValueError('OCI descriptor has invalid identity, type or size')
        name = 'blobs/sha256/' + value['digest'][7:]
        member = self.members.get(name)
        if member is None or not member.isfile() or member.size != value['size']:
            raise ValueError('OCI descriptor differs from its actual blob size')
        return name

    def checked_blob(self, value, media, *, json=False):
        name = self.descriptor(value, media)
        digest = hashlib.sha256()
        with self.archive.extractfile(self.members[name]) as source:
            while body := source.read(1 << 20):
                remaining(self.deadline)
                digest.update(body)
        if 'sha256:' + digest.hexdigest() != value['digest']:
            raise ValueError('OCI blob digest differs from its descriptor')
        self.reachable.add(name)
        return json_object(self.small(name)) if json else name

    def _inspect(self):
        if json_object(self.small('oci-layout')) != {'imageLayoutVersion': '1.0.0'}:
            raise ValueError('unsupported OCI layout version')
        index = json_object(self.small('index.json'))
        if (set(index) - {'schemaVersion', 'mediaType', 'manifests', 'annotations'}
                or index.get('schemaVersion') != 2 or not isinstance(index.get('manifests'), list)
                or len(index['manifests']) != 1):
            raise ValueError('an OCI image archive must contain exactly one manifest')
        descriptor = index['manifests'][0]
        self.reachable = {'oci-layout', 'index.json'}
        manifest = self.checked_blob(descriptor, {MANIFEST}, json=True)
        self.manifest_bytes = self.small('blobs/sha256/' + descriptor['digest'][7:])
        if (set(manifest) - {'schemaVersion', 'mediaType', 'config', 'layers', 'annotations'}
                or manifest.get('schemaVersion') != 2 or manifest.get('mediaType') != MANIFEST
                or not isinstance(manifest.get('layers'), list) or len(manifest['layers']) > 128):
            raise ValueError('OCI image has an unsupported manifest contract')
        config = self.checked_blob(manifest['config'], {CONFIG}, json=True)
        config_digest = manifest['config']['digest']
        if self.config_digest is not None and config_digest != runtime.config_digest(self.config_digest):
            raise ValueError('OCI image config differs from the accepted image identity')
        rootfs = config.get('rootfs')
        if (config.get('os') != 'linux' or not isinstance(config.get('architecture'), str)
                or not config['architecture'] or not isinstance(rootfs, dict)
                or rootfs.get('type') != 'layers' or not isinstance(rootfs.get('diff_ids'), list)
                or len(rootfs['diff_ids']) != len(manifest['layers'])
                or any(not isinstance(diff, str) or not DIGEST.fullmatch(diff) for diff in rootfs['diff_ids'])):
            raise ValueError('OCI image has no complete Linux platform and rootfs identity')
        annotations = descriptor.get('annotations', {})
        if not isinstance(annotations, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in annotations.items()):
            raise ValueError('OCI index annotations are malformed')
        reference = annotations.get('org.opencontainers.image.ref.name', '')
        full_reference = annotations.get('io.containerd.image.name', '')
        if full_reference and reference not in {full_reference, full_reference.rsplit(':', 1)[-1]}:
            raise ValueError('OCI index contains conflicting image references')
        if self.reference is not None:
            allowed = {self.reference, self.reference.rsplit(':', 1)[-1]}
            if '@sha256:' in self.reference:
                repository, wanted = self.reference.rsplit('@', 1)
                if wanted != descriptor['digest']:
                    raise ValueError('OCI manifest differs from the accepted immutable reference')
                if reference and ':' in reference and reference.rsplit(':', 1)[0] == repository:
                    allowed.add(reference)
            if reference and reference not in allowed:
                raise ValueError('OCI archive names another image reference')
            reference = self.reference
        expanded, layers = 0, []
        for layer, diff_id in zip(manifest['layers'], rootfs['diff_ids'], strict=True):
            name = self.checked_blob(layer, LAYERS)
            with self.archive.extractfile(self.members[name]) as source:
                incoming = gzip.GzipFile(fileobj=source) if layer['mediaType'].endswith('+gzip') else source
                digest = hashlib.sha256()
                try:
                    while body := incoming.read(1 << 20):
                        remaining(self.deadline)
                        expanded += len(body)
                        if expanded > self.max_expanded:
                            raise ValueError('OCI layers exceed their decompressed bound')
                        digest.update(body)
                finally:
                    if incoming is not source:
                        incoming.close()
            if 'sha256:' + digest.hexdigest() != diff_id:
                raise ValueError('OCI layer differs from its uncompressed rootfs identity')
            layers.append({'digest': layer['digest'], 'size': layer['size'],
                           'media_type': layer['mediaType'], 'diff_id': diff_id})
        for name, member in self.members.items():
            if member.isfile() and name not in self.reachable:
                raise ValueError('OCI archive contains payload outside its manifest closure')
        return {'reference': reference, 'archive_sha256': self.archive_sha256,
                'manifest_digest': descriptor['digest'], 'config_digest': config_digest,
                'platform': config['os'] + '/' + config['architecture'],
                'layers': layers, 'config': config,
                'blobs': [dict(manifest['config']), *[dict(layer) for layer in manifest['layers']]]}

    def materialize(self, destination):
        """Write the checked OCI closure to a new directory for offline readers."""
        destination = Path(destination).absolute()
        if destination.exists() or destination.is_symlink() or any(p.is_symlink() for p in destination.parents):
            raise ValueError('OCI materialization needs a new directory without linked ancestors')
        self.unchanged()
        destination.mkdir(mode=0o700)
        try:
            for name in sorted(self.reachable):
                remaining(self.deadline)
                output = destination / name
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with self.archive.extractfile(self.members[name]) as source, output.open('xb') as target:
                    while body := source.read(1 << 20):
                        remaining(self.deadline)
                        target.write(body)
            self.unchanged()
            return destination
        except BaseException:
            import shutil
            shutil.rmtree(destination)
            raise

    def open_blob(self, digest):
        self.unchanged()
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ValueError('invalid OCI blob identity')
        name = 'blobs/sha256/' + digest[7:]
        if name not in self.reachable or name not in self.members:
            raise ValueError('blob is outside the verified OCI image closure')
        return self.archive.extractfile(self.members[name])


def inspect_archive(path, expected_reference=None, expected_config=None, *, timeout=600):
    with OCIArchive(path, expected_reference, expected_config, timeout=timeout) as archive:
        return archive.metadata


def save_image(reference, path, *, environment=None, authfile=None, timeout=600):
    path = Path(path)
    if path.exists() or path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('OCI output must be a new file in an owned directory')
    images = runtime.inspect_images([reference], environment=environment, authfile=authfile)
    expected = images[0]['Id']
    deadline = time.monotonic() + timeout
    try:
        runtime.run(['save', '--format', 'oci-archive', '--output', str(path), reference],
                    environment=environment, authfile=authfile, timeout=remaining(deadline), check=True)
        value = inspect_archive(path, reference, expected, timeout=remaining(deadline))
        after = runtime.inspect_images([reference], environment=environment, authfile=authfile,
                                       timeout=remaining(deadline))[0]
        if after['Id'] != expected:
            raise ValueError('image changed while its OCI archive was saved')
        return value
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def load_image(path, expected_reference=None, expected_config=None, *, environment=None, authfile=None, timeout=600):
    deadline = time.monotonic() + timeout
    with OCIArchive(path, expected_reference, expected_config, timeout=timeout) as archive:
        archive.unchanged()
        runtime.run(['load', '--input', str(path)], environment=environment, authfile=authfile,
                    timeout=remaining(deadline), check=True)
        archive.unchanged()
        if expected_reference:
            runtime.run(['tag', archive.metadata['config_digest'], expected_reference],
                        environment=environment, authfile=authfile, timeout=remaining(deadline), check=True)
        selected = expected_reference or archive.metadata['config_digest']
        image = runtime.inspect_images([selected], environment=environment, authfile=authfile,
                                       timeout=remaining(deadline))[0]
        if (image['Id'] != archive.metadata['config_digest']
                or image['Os'] + '/' + image['Architecture'] != archive.metadata['platform']
                or image['RootFS']['Layers'] != [layer['diff_id'] for layer in archive.metadata['layers']]):
            raise ValueError('loaded image differs from the accepted OCI archive')
        return archive.metadata
