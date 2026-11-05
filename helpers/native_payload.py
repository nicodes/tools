#!/usr/bin/env python3
"""Bind a native executable and optional embedded payloads to exact source bytes."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

MAXIMUM = 128 << 20
SHA = re.compile(r'[a-f0-9]{64}\Z')
SOURCE = re.compile(r'[a-f0-9]{40}\Z')
PLATFORMS = {'Linux/x86_64', 'Linux/arm64', 'Darwin/x86_64', 'Darwin/arm64', 'Windows/x86_64', 'Windows/arm64'}


def bytes_of(path, maximum=MAXIMUM):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('native payload inputs may not be symlinks')
    if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
        raise ValueError('native payload must be a nonempty bounded regular file')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise ValueError('native payload must be a nonempty bounded regular file')
        data = stream.read(maximum + 1)
        if len(data) != info.st_size or os.fstat(stream.fileno()).st_mtime_ns != info.st_mtime_ns:
            raise ValueError('native payload changed while reading')
    return data


def identity(source, version, kind, platform, dependencies=None):
    if not isinstance(source, str) or not SOURCE.fullmatch(source):
        raise ValueError('native payload requires an exact source commit')
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}', version):
        raise ValueError('native payload requires one bounded version identity')
    if kind not in {'api', 'host', 'cli'} or platform not in PLATFORMS:
        raise ValueError('native payload requires its declared role and platform')
    dependencies = dependencies if dependencies is not None else {}
    if not isinstance(dependencies, dict) or len(dependencies) > 32:
        raise ValueError('native payload dependencies must be a bounded digest map')
    for name, digest in dependencies.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name) or not isinstance(digest, str) or not SHA.fullmatch(digest):
            raise ValueError('native payload dependency identities must be exact hashes')
    return {'schema': 1, 'source': source, 'version': version, 'kind': kind, 'platform': platform,
            'dependencies': dependencies}


def receipt_path(binary):
    binary = Path(binary)
    return binary.with_name(binary.name + '.json')


def atomic_write(destination, raw):
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError('native payload outputs may not be symlinks')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.' + destination.name, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def record(binary, source, version, kind, platform, dependencies=None):
    expected = identity(source, version, kind, platform, dependencies)
    expected['sha256'] = hashlib.sha256(bytes_of(binary)).hexdigest()
    atomic_write(receipt_path(binary), (json.dumps(expected, sort_keys=True) + '\n').encode())
    return expected


def verify(binary, source, version, kind, platform, dependencies=None):
    expected = identity(source, version, kind, platform, dependencies)
    expected['sha256'] = hashlib.sha256(bytes_of(binary)).hexdigest()
    actual = json.loads(bytes_of(receipt_path(binary), 65536))
    if actual != expected:
        raise ValueError('native payload source, version, role, platform, dependencies or digest mismatch')
    return expected


def compress(binary, destination, source, version, kind, platform, dependencies=None):
    receipt = verify(binary, source, version, kind, platform, dependencies)
    raw = bytes_of(binary)
    if hashlib.sha256(raw).hexdigest() != receipt['sha256']:
        raise ValueError('native payload changed after receipt verification')
    atomic_write(destination, gzip.compress(raw, mtime=0))
    return receipt
