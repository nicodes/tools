#!/usr/bin/env python3
"""Render an immutable source-only consumer of the shared PostgreSQL constructor."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
import tarfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent


def unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('constructor inventory repeats a field')
        result[name] = value
    return result


def verify(directory=ROOT):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('constructor input directory is a link')
    record = json.loads((directory / 'bundle.json').read_bytes(), object_pairs_hook=unique)
    if (not isinstance(record, dict) or set(record) != {'version', 'files'}
            or type(record['version']) is not int or record['version'] != 1
            or not isinstance(record['files'], dict) or not 1 <= len(record['files']) <= 64):
        raise ValueError('constructor requires its closed input inventory')
    if any(path.is_symlink() for path in directory.rglob('*')):
        raise ValueError('constructor input contains a link')
    actual = {str(path.relative_to(directory)) for path in directory.rglob('*')
              if not path.is_dir() and path != directory / 'bundle.json'}
    if actual != set(record['files']):
        raise ValueError('constructor input inventory differs')
    for name, expected in record['files'].items():
        path = directory / name
        if (not isinstance(name, str) or name.startswith('/') or '..' in Path(name).parts
                or not isinstance(expected, dict) or set(expected) != {'sha256', 'size'}
                or type(expected['size']) is not int or expected['size'] < 0
                or not isinstance(expected['sha256'], str)
                or not re.fullmatch('[a-f0-9]{64}', expected['sha256'])):
            raise ValueError('constructor input path is invalid')
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size != expected['size'] or info.st_size > 1 << 20
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected['sha256']):
            raise ValueError('constructor input bytes changed')
    return record


def render(revision, archive_sha256):
    if (not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{40}', revision)
            or not isinstance(archive_sha256, str) or not re.fullmatch('[a-f0-9]{64}', archive_sha256)):
        raise ValueError('constructor requires immutable Git and archive SHA256 identities')
    verify()
    pin = json.loads((ROOT / 'su-exec/PIN.json').read_bytes())
    base = 'docker.io/library/' + pin['builder_image']
    prefix = f'''# Exact shared build source; no PostgreSQL image package is downloaded.
FROM {base} AS source-input
ADD https://codeload.github.com/nicodes/tools/tar.gz/{revision} /tmp/tools-source.tar.gz
RUN printf '%s\\n' '{archive_sha256}  /tmp/tools-source.tar.gz' | sha256sum -c - \\
 && mkdir /source \\
 && tar -xzf /tmp/tools-source.tar.gz -C /source --strip-components=1 tools-{revision}/postgres-runtime

'''
    body = (ROOT / 'Containerfile').read_text().replace(
        'COPY postgres-runtime/ /inputs/',
        'COPY --from=source-input /source/postgres-runtime/ /inputs/')
    return prefix + body + f'''
LABEL io.nicodes.tools.postgres.build_revision="{revision}" \\
      io.nicodes.tools.postgres.source_archive_sha256="sha256:{archive_sha256}"
'''


def verify_archive(path, revision):
    """Bind downloaded constructor members to this tested closed source tree."""
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('source archive requires its immutable Git revision')
    record = verify()
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 16 << 20:
        raise ValueError('source archive exceeds its finite ordinary-file boundary')
    expected = {name: item['sha256'] for name, item in record['files'].items()}
    expected['bundle.json'] = hashlib.sha256((ROOT / 'bundle.json').read_bytes()).hexdigest()
    prefix = 'tools-' + revision + '/postgres-runtime/'
    seen = set()
    count = total = 0
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            count += 1
            total += max(member.size, 0)
            if count > 100000 or total > 64 << 20:
                raise ValueError('source archive expanded inventory exceeds its bound')
            if not member.name.startswith(prefix) or member.isdir():
                continue
            name = member.name[len(prefix):]
            if (name not in expected or name in seen or not member.isfile() or member.issparse()
                    or member.size > 1 << 20 or '..' in Path(name).parts):
                raise ValueError('source archive constructor closure differs')
            raw = archive.extractfile(member).read((1 << 20) + 1)
            if len(raw) != member.size or hashlib.sha256(raw).hexdigest() != expected[name]:
                raise ValueError('source archive constructor bytes changed')
            seen.add(name)
    if seen != set(expected):
        raise ValueError('source archive omitted constructor inputs')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--archive-sha256', required=True)
    args = parser.parse_args()
    try:
        sys.stdout.write(render(args.revision, args.archive_sha256))
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
