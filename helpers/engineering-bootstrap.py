#!/usr/bin/env python3
"""Materialize an exact, unmerged engineering snapshot using the released packager.

This bootstrap is copied to consumers only while the coordinated PRs are under
review. Release promotion replaces its source pin with a checksum-pinned mise
artifact. No branch names, mutable tags, or standing credentials are used.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile


def verify(root, revision, source_sha256):
    try:
        manifest = root/'SOURCE.json'
        if root.is_symlink() or manifest.is_symlink() or hashlib.sha256(manifest.read_bytes()).hexdigest() != source_sha256:
            return False
        source = json.loads(manifest.read_text())
        if source['repository'] not in ('https://github.com/nicodes/cicd', 'https://github.com/nicodes/tools') or source['revision'] != revision:
            return False
        if not isinstance(source.get('files'), dict) or not source['files']:
            return False
        actual = {str(path.relative_to(root)) for directory in ('helpers', 'tests') for path in (root/directory).rglob('*') if path.is_file() or path.is_symlink()}
        if actual != set(source['files']):
            return False
        for relative, expected in source['files'].items():
            if not re.fullmatch(r'(helpers|tests)/[\w.-]+', relative):
                return False
            path = root/relative
            if not re.fullmatch(r'[a-f0-9]{64}', expected) or path.is_symlink() or path.parent.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return False
        return True
    except (OSError, ValueError, KeyError):
        return False


def main():
    pin = json.loads(Path('engineering-pin.json').read_text())
    revision = pin['revision']
    source_sha256 = pin['source_sha256']
    if not re.fullmatch(r'[a-f0-9]{64}', source_sha256):
        raise ValueError('engineering snapshot requires a reviewed SOURCE.json SHA256')
    if os.environ.get('CICD_ENGINEERING'):
        root = Path(os.environ['CICD_ENGINEERING'])
        if not verify(root, revision, source_sha256):
            raise ValueError('selected engineering snapshot differs from its reviewed source pin')
        print(root)
        return
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('engineering snapshot must name an exact commit')
    cache = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home()/'.cache')))/'cicd-snapshots'
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = cache/revision
    with (cache/(revision+'.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not verify(root, revision, source_sha256):
            if root.exists():
                raise ValueError('cached engineering snapshot was modified; remove the named cache and retry')
            released = Path(subprocess.check_output(['mise', 'where', 'http:cicd-engineering'], text=True).strip())
            with tempfile.TemporaryDirectory(prefix='bootstrap-', dir=cache) as directory:
                temporary = Path(directory)
                repository = temporary/'source.git'
                subprocess.run(['git', 'init', '--bare', '-q', repository], check=True)
                subprocess.run(['git', '-C', repository, 'fetch', '--quiet', '--depth=1', '--no-tags',
                    'https://github.com/nicodes/tools.git', revision], check=True, timeout=180)
                actual = subprocess.check_output(['git', '-C', repository, 'rev-parse', 'FETCH_HEAD'], text=True).strip()
                if actual != revision:
                    raise ValueError('fetched engineering commit differs from its pin')
                archive = temporary/'engineering.tar.gz'
                subprocess.run(['python3', released/'helpers/engineering-artifact.py', '--repository', repository,
                    '--revision', revision, '--output', archive], check=True, stdout=subprocess.DEVNULL, timeout=120)
                extracted = temporary/'snapshot'
                extracted.mkdir(mode=0o700)
                with tarfile.open(archive, 'r:gz') as bundle:
                    bundle.extractall(extracted, filter='data')
                if not verify(extracted, revision, source_sha256):
                    raise ValueError('materialized engineering snapshot failed verification')
                extracted.rename(root)
    print(root)


if __name__ == '__main__':
    main()
