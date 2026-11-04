#!/usr/bin/env python3
"""Build and validate an addon archive against a caller-owned closed manifest."""
import argparse
import hashlib
from pathlib import Path, PurePosixPath
import stat
import re
import tempfile
import zipfile


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
    expected = manifest_files(manifest)
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
    expected = manifest_files(manifest)
    archive = Path(archive)
    if archive.is_symlink() or not archive.is_file():
        raise ValueError('package archive must be a regular file')
    originals = source_files(source, manifest, script_uids) if source is not None else None
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
            if originals is not None and package.read(entry) != originals[entry.filename].read_bytes():
                raise ValueError('archive bytes differ from the current package source')
        if package.testzip() is not None:
            raise ValueError('package checksum failed')
        if installed is not None:
            copies = source_files(installed, manifest, script_uids)
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
    source_files(source, manifest, script_uids)
    if archive.resolve().is_relative_to(source.resolve()):
        raise ValueError('package output must be outside the addon source')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='addon-package-', dir=archive.parent) as temporary:
        candidate = Path(temporary) / 'package.zip'
        with zipfile.ZipFile(candidate, 'w', compression=zipfile.ZIP_DEFLATED) as package:
            for name in expected:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644 | ((source / name).stat().st_mode & 0o111)) << 16
                package.writestr(info, (source / name).read_bytes())
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
