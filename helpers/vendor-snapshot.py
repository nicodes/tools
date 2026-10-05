#!/usr/bin/env python3
"""Install a complete, explicitly reviewed local Git snapshot into a product."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

REPOSITORY = 'https://github.com/nicodes/tools'
REPOSITORY_ALIASES = {'https://github.com/nicodes/cicd', 'https://github.com/nicodes/tools'}
FILE = re.compile(r'(helpers|tests)/[\w.-]+', re.ASCII)

# Product-owned files that live in the snapshot directory and are NOT part of
# the snapshot: this tool must leave them exactly as it found them.
#
# ACTION-PINS.json is the fleet action-pin record, and helpers/pins.mjs looks
# for it at scripts/engineering/ACTION-PINS.json -- so a vendored helper
# requires a file this vendoring tool refused to tolerate, which made every
# product in the fleet un-revendorable. Neither half was wrong on its own.
UNMANAGED = {'SOURCE.json', 'ACTION-PINS.json'}


def git(repository, *arguments):
    return subprocess.run(['git', '--no-replace-objects', '-C', str(repository),
                           *arguments], check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout


def snapshot(repository, revision):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('a reviewed full commit SHA is required')
    resolved = git(repository, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    if resolved != revision:
        raise ValueError('revision must identify a commit, not a tag or replacement')
    files = {}
    entries = git(repository, 'ls-tree', '-rz', '--full-tree', revision,
                  '--', 'helpers/', 'tests/').split(b'\0')
    for entry in filter(None, entries):
        metadata, encoded_path = entry.split(b'\t', 1)
        mode, kind, object_id = metadata.decode().split()
        path = encoded_path.decode('ascii')
        if not FILE.fullmatch(path) or kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError('snapshot must contain only flat, regular helper/test files')
        files[path] = (git(repository, 'cat-file', 'blob', object_id), int(mode, 8) & 0o777)
    if not any(name.startswith('helpers/') for name in files) or not any(name.startswith('tests/') for name in files):
        raise ValueError('snapshot must contain both helpers and tests')
    source = {'repository': REPOSITORY, 'revision': revision,
              'files': {name: hashlib.sha256(data).hexdigest()
                         for name, (data, _) in sorted(files.items())}}
    return files, source


def verify_existing(destination):
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError('destination must be an existing regular snapshot directory')
    source_path = destination / 'SOURCE.json'
    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError('destination must have a regular SOURCE.json')
    source = json.loads(source_path.read_text())
    if source.get('repository') not in REPOSITORY_ALIASES or not re.fullmatch(r'[0-9a-f]{40}', source.get('revision', '')):
        raise ValueError('destination is not a pinned cicd snapshot')
    expected = source.get('files')
    if not isinstance(expected, dict) or not expected:
        raise ValueError('destination has no complete hash inventory')
    for name, digest in expected.items():
        if not FILE.fullmatch(name) or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('invalid destination inventory')
        path = destination / name
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('destination contains local changes; preserve them before updating')
    actual = set()
    for path in destination.rglob('*'):
        relative = path.relative_to(destination).as_posix()
        if path.is_symlink():
            raise ValueError('destination contains a symlink')
        if path.is_dir() and relative in ('helpers', 'tests', 'helpers/__pycache__', 'tests/__pycache__'):
            continue
        if path.is_file() and re.fullmatch(r'(helpers|tests)/__pycache__/[\w.-]+\.pyc', relative, re.ASCII):
            continue
        if not path.is_file():
            raise ValueError('destination contains an unexpected directory or special file')
        actual.add(relative)
    if actual - UNMANAGED != set(expected):
        raise ValueError('destination contains unlisted files; preserve them before updating')


def install(repository, revision, destination):
    # No network, branch lookup, checkout of upstream working files, or editing of
    # individual vendor files: the explicit commit's complete tree is authoritative.
    destination = Path(os.path.abspath(destination))
    verify_existing(destination)
    files, source = snapshot(repository, revision)
    scratch = Path(tempfile.mkdtemp(prefix='.engineering-update-', dir=destination.parent))
    stage, previous = scratch / 'snapshot', scratch / 'previous'
    try:
        stage.mkdir()
        for name, (data, mode) in files.items():
            path = stage / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode)
        (stage / 'SOURCE.json').write_text(json.dumps(source, indent=2) + '\n')
        # Carry product-owned files across the swap. The stage replaces the
        # destination wholesale, so merely tolerating an unmanaged file in
        # verify_existing would still have an update DELETE it.
        for name in sorted(UNMANAGED - {'SOURCE.json'}):
            existing = destination / name
            if existing.is_file() and not existing.is_symlink():
                shutil.copy2(existing, stage / name)
        verify_existing(stage)
        # Recheck after preparing the new tree. Operators must not edit the
        # destination concurrently; this is maintenance tooling, not a file lock.
        verify_existing(destination)
        destination.rename(previous)
        try:
            stage.rename(destination)
        except BaseException:
            previous.rename(destination)
            raise
        shutil.rmtree(previous)
    finally:
        # If rollback itself fails, never delete the original snapshot. The
        # maintenance directory remains beside the destination for recovery.
        if not previous.exists():
            shutil.rmtree(scratch)
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, type=Path, help='local reviewed cicd Git repository')
    parser.add_argument('--revision', required=True, help='reviewed full commit SHA; never a branch name')
    parser.add_argument('--destination', required=True, type=Path, help='existing product scripts/engineering directory')
    args = parser.parse_args()
    try:
        source = install(args.repository, args.revision, args.destination)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        # Git diagnostics and product paths are unnecessary for this report.
        raise SystemExit('Snapshot update failed; existing inventory or reviewed source could not be validated (' + type(error).__name__ + ').') from None
    print(json.dumps({'revision': source['revision'], 'files': len(source['files'])}))


if __name__ == '__main__':
    main()
