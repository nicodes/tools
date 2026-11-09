#!/usr/bin/env python3
"""Admit a closed, content-addressed release packet without executing its source."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time

import oci_registry


NAME = re.compile(r'[a-z][a-z0-9-]{0,62}\Z')
HEX = re.compile(r'[a-f0-9]{64}\Z')
COMMIT = re.compile(r'[a-f0-9]{40}\Z')
NUMBER = re.compile(r'[1-9][0-9]{0,19}\Z')
MAX_DOCUMENT = 4 << 20
MAX_FILE = 8 << 30
MAX_FILES = 128


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def regular(path, maximum=MAX_FILE):
    path = Path(os.path.abspath(path))
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('release packet must not traverse links or special directories')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > maximum or before.st_size < 0
                or fingerprint(path.lstat()) != fingerprint(before)):
            raise ValueError('release packet requires bounded ordinary owned file identities')
        yield stream, before
        if (fingerprint(os.fstat(stream.fileno())) != fingerprint(before)
                or fingerprint(path.lstat()) != fingerprint(before)):
            raise ValueError('release file changed during admission')


def read(path, maximum=MAX_DOCUMENT):
    with regular(path, maximum) as (stream, _):
        raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError('release document exceeds its bound')
        return raw


def document(path, maximum=MAX_DOCUMENT):
    raw = read(path, maximum)
    return json.loads(raw, object_pairs_hook=oci_registry.unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON is refused')))


def hash_file(path, deadline):
    digest = hashlib.sha256()
    with regular(path) as (stream, info):
        while True:
            if time.monotonic() >= deadline:
                raise ValueError('release packet admission exceeded its deadline')
            block = stream.read(1 << 20)
            if not block:
                break
            digest.update(block)
        return digest.hexdigest(), info.st_size, fingerprint(info)


def release(document, application=None, environment=None):
    required = {'kind', 'version', 'application', 'source', 'artifacts', 'workload'}
    if (not isinstance(document, dict) or not required <= set(document) <= required | {'stateful_contract', 'providers'}
            or document.get('kind') != 'komizo.release' or type(document.get('version')) is not int
            or document['version'] != 1 or not isinstance(document.get('application'), str)
            or not NAME.fullmatch(document['application'])
            or (application is not None and document['application'] != application)):
        raise ValueError('release has an invalid application or contract identity')
    if 'providers' in document:
        providers = document['providers']
        if (not isinstance(providers, dict) or set(providers) != {'cookbook_revision', 'tools_revision', 'core_revision'}
                or any(not isinstance(value, str) or not COMMIT.fullmatch(value) for value in providers.values())):
            raise ValueError('release providers require all three exact source revisions')
    source = document['source']
    if (not isinstance(source, dict) or set(source) != {'repository', 'repository_id', 'revision'}
            or not isinstance(source['repository'], str)
            or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', source['repository'])
            or source['repository'].split('/')[1] in {'.', '..'}
            or not isinstance(source['repository_id'], str) or not NUMBER.fullmatch(source['repository_id'])
            or not isinstance(source['revision'], str) or not COMMIT.fullmatch(source['revision'])):
        raise ValueError('release has no immutable source identity')
    if environment is not None:
        for key, variable in [('repository', 'GITHUB_REPOSITORY'), ('repository_id', 'GITHUB_REPOSITORY_ID'),
                              ('revision', 'GITHUB_SHA')]:
            if source[key] != environment.get(variable):
                raise ValueError('release source differs from its workflow identity')
    artifacts = document['artifacts']
    if not isinstance(artifacts, dict) or len(artifacts) > 32:
        raise ValueError('release artifact inventory is invalid')
    for name, artifact in artifacts.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(artifact, dict):
            raise ValueError('release artifact names must be bounded logical identifiers')
        registry, repository, manifest = oci_registry.reference(artifact.get('reference'))
        if artifact.get('kind') == 'image':
            if (set(artifact) != {'kind', 'reference', 'digest', 'config_digest', 'platform'}
                    or artifact['digest'] != manifest
                    or not isinstance(artifact['config_digest'], str)
                    or not oci_registry.DIGEST.fullmatch(artifact['config_digest'])
                    or artifact['platform'] not in {'linux/amd64', 'linux/arm64'}):
                raise ValueError('image release requires manifest, configuration and platform identities')
        elif artifact.get('kind') == 'archive':
            if (set(artifact) != {'kind', 'reference', 'digest', 'size_bytes', 'media_type'}
                    or artifact['media_type'] != oci_registry.STATIC_LAYER
                    or oci_registry.digest(oci_registry.artifact_manifest(artifact['digest'], artifact['size_bytes'])) != manifest):
                raise ValueError('static release requires its exact payload and artifact manifest')
        else:
            raise ValueError('release has an unknown artifact kind')
        if environment is not None:
            owner = environment['GITHUB_REPOSITORY'].split('/')[0].lower()
            if registry != 'ghcr.io' or not repository.startswith(owner + '/'):
                raise ValueError('Actions publication cannot send its credential to another registry owner')
    workload = document['workload']
    if (not isinstance(workload, dict)
            or set(workload) - {'services', 'jobs', 'assets', 'networks', 'volumes', 'routes', 'readiness'}
            or not isinstance(workload.get('services', {}), dict)
            or not isinstance(workload.get('jobs', {}), dict)
            or not isinstance(workload.get('assets', {}), dict)):
        raise ValueError('release workload is not a closed object')
    if not (workload.get('services') or workload.get('jobs') or workload.get('assets')):
        raise ValueError('release must contain a real workload')
    raw = oci_registry.canonical(document)
    if len(raw) > MAX_DOCUMENT:
        raise ValueError('release descriptor exceeds its bound')
    return raw


def member_type(name):
    if not isinstance(name, str):
        raise ValueError('release packet member requires a string path')
    if name == 'release.json':
        return 'release'
    parts = PurePosixPath(name).parts
    if len(parts) != 2 or str(PurePosixPath(name)) != name:
        raise ValueError('release packet member is outside its closed layout')
    family, filename = parts
    if family == 'images' and filename.endswith('.oci.tar') and NAME.fullmatch(filename[:-8]):
        return 'image'
    if family == 'static' and filename.endswith('.tar.gz') and NAME.fullmatch(filename[:-7]):
        return 'static'
    if family == 'metadata' and re.fullmatch(r'[a-z][a-z0-9_.-]{0,127}', filename) and '..' not in filename:
        return 'metadata'
    raise ValueError('release packet member is outside its closed layout')


class Packet:
    def __init__(self, path, *, application=None, environment=None, timeout=600):
        if type(timeout) is not int or not 1 <= timeout <= 2400:
            raise ValueError('release packet requires a finite admission deadline')
        self.root = Path(os.path.abspath(path))
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError('release packet requires an ordinary directory')
        self.deadline = time.monotonic() + timeout
        self.inventory_raw = read(self.root / 'inventory.json')
        self.inventory = json.loads(self.inventory_raw, object_pairs_hook=oci_registry.unique_object)
        if (not isinstance(self.inventory, dict) or set(self.inventory) != {'version', 'files'}
                or type(self.inventory.get('version')) is not int or self.inventory['version'] != 1
                or not isinstance(self.inventory['files'], dict)
                or not 1 <= len(self.inventory['files']) <= MAX_FILES
                or 'release.json' not in self.inventory['files']):
            raise ValueError('release packet has no closed versioned inventory')
        self.files = self.inventory['files']
        self.fingerprints = {}
        for name, record in self.files.items():
            kind = member_type(name)
            if (not isinstance(record, dict) or set(record) != {'sha256', 'size', 'type'}
                    or record['type'] != kind or not isinstance(record['sha256'], str)
                    or not HEX.fullmatch(record['sha256']) or type(record['size']) is not int
                    or not 0 <= record['size'] <= MAX_FILE):
                raise ValueError('release packet member has invalid byte metadata')
            actual, size, identity = hash_file(self.root / name, self.deadline)
            if actual != record['sha256'] or size != record['size']:
                raise ValueError('release packet bytes differ from the accepted inventory')
            self.fingerprints[name] = identity
        self._closure()
        self.descriptor = document(self.root / 'release.json')
        self.release_bytes = release(self.descriptor, application, environment)
        if read(self.root / 'release.json') != self.release_bytes:
            raise ValueError('accepted release descriptor must contain its exact canonical bytes')
        self.release_digest = oci_registry.digest(self.release_bytes)
        expected = {'release.json'}
        for name, artifact in self.descriptor['artifacts'].items():
            member = 'images/' + name + '.oci.tar' if artifact['kind'] == 'image' else 'static/' + name + '.tar.gz'
            if member not in self.files:
                raise ValueError('release artifact is absent from its packet')
            expected.add(member)
            if artifact['kind'] == 'archive':
                record = self.files[member]
                if record['sha256'] != artifact['digest'][7:] or record['size'] != artifact['size_bytes']:
                    raise ValueError('static artifact differs from its release descriptor')
        if {name for name in self.files if not name.startswith('metadata/')} != expected:
            raise ValueError('release packet contains undeclared image or static payloads')
        self.unchanged()

    def _closure(self):
        actual = set()
        for path in self.root.rglob('*'):
            info = path.lstat()
            name = path.relative_to(self.root).as_posix()
            if stat.S_ISDIR(info.st_mode):
                if name not in {'images', 'static', 'metadata'}:
                    raise ValueError('release packet contains an unexpected directory')
            elif stat.S_ISREG(info.st_mode):
                actual.add(name)
                if len(actual) > MAX_FILES + 1:
                    raise ValueError('release packet contains too many members')
            else:
                raise ValueError('release packet contains a link or special file')
        if actual != set(self.files) | {'inventory.json'}:
            raise ValueError('release packet does not match its closed inventory')

    def unchanged(self):
        if time.monotonic() >= self.deadline:
            raise ValueError('release packet exceeded its deadline')
        self._closure()
        for name, expected in self.fingerprints.items():
            if fingerprint((self.root / name).lstat()) != expected:
                raise ValueError('release packet changed after admission')
        if read(self.root / 'inventory.json') != self.inventory_raw:
            raise ValueError('release inventory changed after admission')

    def require_proof(self, proof, prefix='.artifacts/release/'):
        accepted = proof.get('artifacts', {})
        if not isinstance(accepted, dict):
            raise ValueError('source acceptance has no artifact inventory')
        for name, record in self.files.items():
            if accepted.get(prefix + name) != record['sha256']:
                raise ValueError('release packet differs from complete same-source acceptance')
        inventory_sha = hashlib.sha256(read(self.root / 'inventory.json')).hexdigest()
        if accepted.get(prefix + 'inventory.json') != inventory_sha:
            raise ValueError('release inventory is not bound to complete same-source acceptance')
        self.unchanged()
