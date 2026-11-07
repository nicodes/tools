#!/usr/bin/env python3
"""Build and validate an addon archive against a caller-owned closed manifest."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import stat
import re
import tempfile
import zipfile


_spec = importlib.util.spec_from_file_location('_package_source_config', Path(__file__).with_name('source_config.py'))
source_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(source_config)


def archive_name(name):
    return 'gdam.json' if name == 'gdam.toml' else name


def payload(path, name):
    if name != 'gdam.toml':
        return path.read_bytes()
    data = source_config.read(path, maximum=1 << 20)
    addons = data.get('addons')
    if (set(data) != {'addons'} or not isinstance(addons, dict)
            or any(not re.fullmatch(r'@[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+', name)
                   or not isinstance(entry, dict) or set(entry) != {'tag'}
                   or not isinstance(entry['tag'], str) or not entry['tag'] or entry['tag'] != entry['tag'].strip()
                   or any(ord(character) < 32 for character in entry['tag'])
                   for name, entry in addons.items())):
        raise ValueError('published GDAM manifest requires only exact named addon tags')
    return (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode()


def records(files, prefix=''):
    result = [(prefix + archive_name(name), payload(path, name), 0o644 | (path.stat().st_mode & 0o111))
              for name, path in sorted(files.items())]
    if len({name for name, _, _ in result}) != len(result):
        raise ValueError('source manifest has conflicting native archive identities')
    return sorted(result)


def manifest_files(manifest):
    names = Path(manifest).read_text().splitlines()
    if not names or names != sorted(set(names)):
        raise ValueError('package manifest must be nonempty, sorted and unique')
    for name in names:
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or '..' in path.parts or
                '\\' in name or path.as_posix() != name or ':' in name):
            raise ValueError('unsafe package manifest path: ' + name)
    return names


def source_files(source, manifest, script_uids=False):
    source = Path(source)
    expected = manifest_files(manifest)
    if source.is_symlink() or not source.is_dir():
        raise ValueError('package source must be a regular directory')
    actual = {}
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError('package source contains a symlink')
        if path.is_file():
            actual[path.relative_to(source).as_posix()] = path
        elif not path.is_dir():
            raise ValueError('package source contains a non-regular file')
    if 'gdam.json' in actual:
        raise ValueError('authored addon manifests must use gdam.toml; gdam.json is generated native I/O')
    if sorted(actual) != expected:
        raise ValueError('package source differs from the closed manifest')
    if script_uids:
        seen = set()
        for name in expected:
            if name.endswith('.gd'):
                sidecar = actual.get(name + '.uid')
                if sidecar is None:
                    raise ValueError('shipped GDScript requires a UID sidecar: ' + name)
                value = sidecar.read_text().strip()
                if not re.fullmatch(r'uid://[a-z0-9]+', value) or value in seen:
                    raise ValueError('shipped script UIDs must be valid and unique')
                seen.add(value)
    return actual


def tree_digest(archive, manifest):
    expected = sorted(archive_name(name) for name in manifest_files(manifest))
    verify(archive, manifest)
    digest = hashlib.sha256()
    with zipfile.ZipFile(archive) as package:
        for name in expected:
            digest.update(name.encode())
            digest.update(b'\0')
            digest.update(package.read(name))
            digest.update(b'\0')
    return digest.hexdigest()


def verify(archive, manifest, source=None, installed=None, script_uids=False):
    expected = sorted(archive_name(name) for name in manifest_files(manifest))
    archive = Path(archive)
    if archive.is_symlink() or not archive.is_file():
        raise ValueError('package archive must be a regular file')
    originals = ({name: data for name, data, _ in records(source_files(source, manifest, script_uids))}
                 if source is not None else None)
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if any(entry.is_dir() for entry in entries):
            raise ValueError('closed package contains unexpected directory entries')
        if [entry.filename for entry in entries] != expected:
            raise ValueError('closed package manifest mismatch')
        for entry in entries:
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('archive symlink rejected')
            if not stat.S_ISREG(entry.external_attr >> 16):
                raise ValueError('non-regular archive entry rejected')
            if originals is not None and package.read(entry) != originals[entry.filename]:
                raise ValueError('archive bytes differ from the current package source')
        if package.testzip() is not None:
            raise ValueError('package checksum failed')
        if script_uids:
            seen = set()
            for name in expected:
                if name.endswith('.gd'):
                    if name + '.uid' not in expected:
                        raise ValueError('shipped GDScript requires a UID sidecar: ' + name)
                    uid = package.read(name + '.uid').decode('utf-8').strip()
                    if not re.fullmatch(r'uid://[a-z0-9]+', uid) or uid in seen:
                        raise ValueError('shipped script UIDs must be valid and unique')
                    seen.add(uid)
        if installed is not None:
            installed = Path(installed)
            copies = {}
            for path in installed.rglob('*'):
                if path.is_symlink() or not path.is_dir() and not path.is_file():
                    raise ValueError('installed addon contains linked or non-regular content')
                if path.is_file(): copies[path.relative_to(installed).as_posix()] = path
            if sorted(copies) != expected:
                raise ValueError('installed addon differs from the closed manifest')
            for name in expected:
                if copies[name].read_bytes() != package.read(name):
                    raise ValueError('installed addon bytes differ from the accepted archive')
    return hashlib.sha256(Path(archive).read_bytes()).hexdigest()


def verify_checksum(archive, checksums=None):
    archive = Path(archive)
    checksums = Path(checksums) if checksums is not None else archive.with_suffix(archive.suffix + '.sha256')
    if archive.is_symlink() or checksums.is_symlink():
        raise ValueError('package checksum inputs may not be symlinks')
    lines = checksums.read_text().splitlines()
    if len(lines) != 1:
        raise ValueError('package checksum requires one exact archive identity')
    match = re.fullmatch(r'([a-f0-9]{64})  ' + re.escape(archive.name), lines[0])
    if match is None or hashlib.sha256(archive.read_bytes()).hexdigest() != match[1]:
        raise ValueError('package checksum mismatch')
    return match[1]


def build(source, manifest, archive, script_uids=False):
    source, archive = Path(source), Path(archive)
    expected = manifest_files(manifest)
    native = records(source_files(source, manifest, script_uids))
    if archive.resolve().is_relative_to(source.resolve()):
        raise ValueError('package output must be outside the addon source')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='addon-package-', dir=archive.parent) as temporary:
        candidate = Path(temporary) / 'package.zip'
        with zipfile.ZipFile(candidate, 'w', compression=zipfile.ZIP_DEFLATED) as package:
            for name, data, mode in native:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | mode) << 16
                package.writestr(info, data)
        digest = verify(candidate, manifest, source=source, script_uids=script_uids)
        candidate.replace(archive)
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['build', 'verify', 'checksum'])
    parser.add_argument('--source')
    parser.add_argument('--manifest')
    parser.add_argument('--archive', required=True)
    parser.add_argument('--checksums')
    parser.add_argument('--installed')
    parser.add_argument('--script-uids', action='store_true')
    parser.add_argument('--tree-sha256', action='store_true')
    args = parser.parse_args()
    try:
        if args.operation == 'checksum':
            digest = verify_checksum(args.archive, args.checksums)
        else:
            if not args.manifest:
                parser.error('build and verify require --manifest')
            digest = (build(args.source or 'addon', args.manifest, args.archive, args.script_uids) if args.operation == 'build'
                      else verify(args.archive, args.manifest, args.source, args.installed, args.script_uids))
            if args.checksums:
                verify_checksum(args.archive, args.checksums)
            if args.tree_sha256:
                print('INSTALLED_TREE_SHA256=' + tree_digest(args.archive, args.manifest))
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.exit(1, str(error) + '\n')
    print(digest + '  ' + args.archive)


if __name__ == '__main__':
    main()
