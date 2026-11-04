#!/usr/bin/env python3
"""Build and validate an addon archive against a caller-owned closed manifest."""
import argparse
import hashlib
from pathlib import Path, PurePosixPath
import stat
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


def verify(archive, manifest):
    expected = manifest_files(manifest)
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if any(entry.is_dir() for entry in entries):
            raise ValueError('closed package contains unexpected directory entries')
        if [entry.filename for entry in entries] != expected:
            raise ValueError('closed package manifest mismatch')
        for entry in entries:
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('archive symlink rejected')
        if package.testzip() is not None:
            raise ValueError('package checksum failed')
    return hashlib.sha256(Path(archive).read_bytes()).hexdigest()


def build(source, manifest, archive):
    source, archive = Path(source), Path(archive)
    expected = manifest_files(manifest)
    if source.is_symlink() or not source.is_dir():
        raise ValueError('package source must be a regular directory')
    actual = []
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError('package source contains a symlink')
        if path.is_file():
            actual.append(path.relative_to(source).as_posix())
        elif not path.is_dir():
            raise ValueError('package source contains a non-regular file')
    if sorted(actual) != expected:
        raise ValueError('package source differs from the closed manifest')
    if archive.resolve().is_relative_to(source.resolve()):
        raise ValueError('package output must be outside the addon source')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='addon-package-', dir=archive.parent) as temporary:
        candidate = Path(temporary) / 'package.zip'
        with zipfile.ZipFile(candidate, 'w', compression=zipfile.ZIP_DEFLATED) as package:
            for name in expected:
                package.write(source / name, name)
        digest = verify(candidate, manifest)
        candidate.replace(archive)
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['build', 'verify'])
    parser.add_argument('--source', default='addon')
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--archive', required=True)
    args = parser.parse_args()
    try:
        digest = (build(args.source, args.manifest, args.archive) if args.operation == 'build'
                  else verify(args.archive, args.manifest))
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.exit(1, str(error) + '\n')
    print(digest + '  ' + args.archive)


if __name__ == '__main__':
    main()
