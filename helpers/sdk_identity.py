"""Versioned data-only SDK discovery for the controller bootstrap transition.

Version 0 is the actual existing generated Make source/cache ABI. Version 1 is
the future canonical Mise source/cache ABI. An invalid new identity never
selects the older ABI. No source is fetched or code loaded by this module.
"""
import os
from pathlib import Path
import re
import stat
import subprocess
import tomllib

MAX_SOURCE = 262144
SHA = re.compile(r'[a-f0-9]{40}\Z')


def identity(source):
    if isinstance(source, bytes):
        source = source.decode('utf-8')
    if not isinstance(source, str) or len(source.encode()) > MAX_SOURCE or '\0' in source:
        raise ValueError('SDK identity requires bounded UTF-8 Mise source')
    values = tomllib.loads(source).get('vars', {})
    if (not isinstance(values, dict) or type(values.get('tools_contract')) is not int
            or values['tools_contract'] != 1 or not isinstance(values.get('tools_ref'), str)
            or not SHA.fullmatch(values['tools_ref'])):
        raise ValueError('SDK source requires one versioned immutable Mise identity')
    return {'version': 1, 'revision': values['tools_ref']}


def no_links(path):
    path = Path(path).absolute()
    if any(value.is_symlink() for value in [path, *path.parents]):
        raise ValueError('SDK source must not traverse links')
    return path


def _signature(info, *, cross=False):
    result = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
              getattr(info, 'st_uid', None), info.st_size, info.st_mtime_ns)
    return result if cross else (*result, info.st_ctime_ns)


def read_regular(path):
    path = no_links(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SOURCE:
        raise ValueError('SDK discovery requires bounded regular Mise source')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as source:
        opened = os.fstat(source.fileno())
        data = source.read(MAX_SOURCE + 1)
        closed = os.fstat(source.fileno())
    after = path.lstat()
    if (len(data) > MAX_SOURCE or _signature(before) != _signature(after)
            or _signature(opened) != _signature(closed)
            or _signature(before, cross=True) != _signature(opened, cross=True)):
        raise ValueError('SDK identity source changed while reading')
    return data


def read_source(root):
    return read_regular(Path(root) / 'mise.toml')


def universal_text(source):
    """Interpret native text newlines without modifying or hashing the bytes."""
    return source.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')


def declared(source):
    if isinstance(source, bytes):
        source = source.decode('utf-8')
    if not isinstance(source, str) or len(source.encode()) > MAX_SOURCE:
        raise ValueError('SDK identity requires bounded UTF-8 Mise source')
    values = tomllib.loads(source).get('vars', {})
    if not isinstance(values, dict):
        raise ValueError('Mise variables must be source data')
    return bool(set(values) & {'tools_contract', 'tools_ref', 'cookbook_recipe', 'cookbook_sha256'})


def source_identity(root):
    source = no_links(Path(root) / 'mise.toml')
    if source.exists():
        raw = read_regular(source)
        if declared(raw):
            return identity(raw)
        if set(tomllib.loads(raw.decode('utf-8'))) - {'tools', 'env'}:
            raise ValueError('legacy SDK source must not add another task authority')
    raw = read_regular(Path(root) / 'Makefile')
    if len(raw) > 65536:
        raise ValueError('legacy generated Make source exceeds its original bound')
    legacy = universal_text(raw)
    pins = re.findall(r'^COOKBOOK_REF := ([a-f0-9]{40})$', legacy, re.M)
    if len(pins) != 1:
        raise ValueError('legacy source requires one immutable generated cookbook pin')
    return {'version': 0, 'revision': pins[0]}


def location(root, revision, supplied=None, *, version=1):
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise ValueError('SDK source requires one immutable source pin')
    if type(version) is not int or version not in {0, 1}:
        raise ValueError('unknown SDK source/cache contract')
    cache = '.artifacts/tools' if version == 1 else '.artifacts/cookbook'
    return no_links(supplied if supplied else Path(root) / cache / revision)


def git(directory, *arguments):
    return subprocess.check_output(['git', '--no-replace-objects', '-c', 'core.hooksPath=' + os.devnull,
        '-c', 'core.fsmonitor=false', '-C', str(directory), *arguments], text=True, timeout=30).strip()


def verify(directory, revision):
    directory = no_links(directory)
    if (not directory.is_dir() or git(directory, 'rev-parse', 'HEAD') != revision
            or Path(git(directory, 'rev-parse', '--show-toplevel')).resolve() != directory.resolve()
            or git(directory, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching')):
        raise ValueError('SDK source must be the exact clean pinned checkout')
    return directory


def prepared(root, expected_ref=None, supplied=None):
    record = source_identity(root)
    revision = record['revision']
    if expected_ref is not None and expected_ref != revision:
        raise ValueError('SDK source differs from the exact caller-declared pin')
    return verify(location(root, revision, supplied, version=record['version']), revision)
