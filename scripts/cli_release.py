#!/usr/bin/env python3
"""Build portable CLI archives and publish only complete, immutable payloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
from urllib.parse import quote
import zipfile

TARGETS = [('linux', 'amd64', 'Linux', 'x86_64'),
           ('linux', 'arm64', 'Linux', 'arm64'),
           ('darwin', 'amd64', 'Darwin', 'x86_64'),
           ('darwin', 'arm64', 'Darwin', 'arm64')]


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def binary_name(value):
    if not re.fullmatch(r'[a-z][a-z0-9-]*', value):
        raise ValueError('binary must be a lowercase executable name')
    return value


def release_tag(value):
    if not re.fullmatch(r'(?:[a-z][a-z0-9-]*-)?v[0-9]+\.[0-9]+\.[0-9]+', value):
        raise ValueError('expected a stable semantic release tag')
    return value


def version(bump, prefix='v', root=Path('.')):
    if bump not in ('patch', 'minor', 'major'):
        raise ValueError('invalid version bump')
    if not re.fullmatch(r'(?:[a-z][a-z0-9-]*-)?v', prefix):
        raise ValueError('invalid tag prefix')
    tags = run('git', 'tag', '--list', cwd=root).splitlines()
    versions = [tuple(map(int, tag[len(prefix):].split('.'))) for tag in tags
                if re.fullmatch(re.escape(prefix) + r'[0-9]+\.[0-9]+\.[0-9]+', tag)]
    if not versions:
        parts = (0, 0, 1)
    else:
        major, minor, patch = max(versions)
        parts = {'patch': (major, minor, patch + 1),
                 'minor': (major, minor + 1, 0), 'major': (major + 1, 0, 0)}[bump]
    number = '.'.join(map(str, parts))
    return number, prefix + number


def checksums(directory):
    result = {}
    for line in (directory / 'checksums.txt').read_text().splitlines():
        match = re.fullmatch(r'([a-f0-9]{64}) [ *](?:\./)?([^/\\]+)', line)
        if not match or match[2] in result:
            raise ValueError('invalid or duplicate checksum entry')
        result[match[2]] = match[1]
    return result


def archive_binary(path, binary):
    expected = binary + ('.exe' if path.suffix == '.zip' else '')
    names = set()
    found = False
    if path.suffix == '.zip':
        with zipfile.ZipFile(path) as archive:
            entries = [(i.filename, not i.is_dir(), (i.external_attr >> 16) & 0o170000)
                       for i in archive.infolist()]
    else:
        with tarfile.open(path, 'r:gz') as archive:
            entries = []
            for item in archive:
                if not (item.isfile() or item.isdir()):
                    raise ValueError('archives may contain only regular files and directories')
                entries.append((item.name, item.isfile(), 0o100000 if item.isfile() else 0o040000))
    for name, regular, kind in entries:
        member = PurePosixPath(name)
        if member.is_absolute() or '..' in member.parts or '\\' in name or name in names:
            raise ValueError('unsafe or duplicate archive entry')
        names.add(name)
        if kind == 0o120000:
            raise ValueError('archive links are not supported')
        if name == expected:
            if not regular or kind not in (0, 0o100000):
                raise ValueError('the executable must be a regular file')
            found = True
    if not found:
        raise ValueError('archive does not contain the named executable')


def validate(directory, binary, extra_files=()):
    binary_name(binary)
    directory = Path(directory)
    files = {p.name: p for p in directory.iterdir()}
    if any(p.is_symlink() or not p.is_file() for p in files.values()):
        raise ValueError('release payload must contain regular top-level files')
    required = {f'{binary}_{os_label}_{arch_label}.tar.gz' for _, _, os_label, arch_label in TARGETS}
    optional = {f'{binary}_Windows_{arch}.zip' for arch in ('x86_64', 'arm64')}
    extras = set(extra_files)
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name) for name in extras):
        raise ValueError('extra assets must have plain file names')
    allowed = required | optional | extras | {'checksums.txt'}
    if not required <= files.keys() or not files.keys() <= allowed or not extras <= files.keys():
        raise ValueError('incomplete or unexpected release payload')
    digests = checksums(directory)
    archives = required | (optional & files.keys())
    if not archives <= digests.keys() or not digests.keys() <= files.keys() - {'checksums.txt'}:
        raise ValueError('checksums must identify every archive and only supplied assets')
    for name, expected in digests.items():
        if hashlib.sha256(files[name].read_bytes()).hexdigest() != expected:
            raise ValueError('release asset checksum mismatch')
    for name in archives:
        archive_binary(files[name], binary)
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sorted(files.items())}


def build(binary, package, module_dir, directory, tag, source, version_symbol='', revision_symbol=''):
    binary_name(binary)
    release_tag(tag)
    if not re.fullmatch(r'[a-f0-9]{40}', source):
        raise ValueError('source must be a full commit')
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    flags = ['-s', '-w']
    for symbol, value in ((version_symbol, tag), (revision_symbol, source)):
        if symbol:
            if not re.fullmatch(r'[A-Za-z0-9_./-]+\.[A-Za-z_][A-Za-z0-9_]*', symbol):
                raise ValueError('invalid Go string symbol')
            flags += ['-X', symbol + '=' + value]
    for goos, goarch, os_label, arch_label in TARGETS:
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / binary
            env = dict(os.environ, CGO_ENABLED='0', GOOS=goos, GOARCH=goarch)
            subprocess.run(['go', 'build', '-trimpath', '-buildvcs=true', '-ldflags', ' '.join(flags),
                            '-o', str(executable), package], cwd=module_dir, env=env, check=True)
            metadata = run('go', 'version', '-m', str(executable))
            for field in (f'GOOS={goos}', f'GOARCH={goarch}', f'vcs.revision={source}'):
                if field not in metadata:
                    raise ValueError('built executable has the wrong source or target')
            archive_path = directory / f'{binary}_{os_label}_{arch_label}.tar.gz'
            with tarfile.open(archive_path, 'w:gz') as archive:
                for path in [executable, *[Path(n) for n in ('README.md', 'LICENSE') if Path(n).is_file()]]:
                    info = archive.gettarinfo(str(path), arcname=path.name)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    info.mode = 0o755 if path == executable else 0o644
                    with path.open('rb') as stream:
                        archive.addfile(info, stream)
    (directory / 'checksums.txt').write_text(''.join(
        hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n'
        for path in sorted(directory.glob('*.tar.gz'))))
    validate(directory, binary)


def absent(repo, endpoint):
    result = subprocess.run(['gh', 'api', '--include', f'repos/{repo}/{endpoint}'],
                            text=True, capture_output=True, check=False)
    status = re.search(r'^HTTP/\S+ (\d+)', result.stdout, re.M)
    if result.returncode == 0:
        raise ValueError('release tags and releases must be new; existing objects are immutable')
    if not status or status[1] != '404':
        raise ValueError('could not establish that the release identity is unused')


def publish(directory, binary, tag, source, repo, extra_files=()):
    release_tag(tag)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('invalid repository')
    if not re.fullmatch(r'[a-f0-9]{40}', source) or source != os.environ.get('GITHUB_SHA'):
        raise ValueError('publication must use this workflow source commit')
    if os.environ.get('GITHUB_REF') != 'refs/heads/main' or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        raise ValueError('publication requires an explicit main-branch dispatch')
    expected = validate(directory, binary, extra_files)
    absent(repo, 'git/ref/tags/' + quote(tag, safe=''))
    absent(repo, 'releases/tags/' + quote(tag, safe=''))
    files = [str(Path(directory) / name) for name in expected]
    subprocess.run(['gh', 'release', 'create', tag, *files, '--repo', repo, '--target', source,
                    '--draft', '--title', tag, '--notes', 'Prebuilt CLI binaries from source ' + source + '.'], check=True)
    uploaded = json.loads(run('gh', 'api', f'repos/{repo}/releases/tags/{quote(tag, safe="")}'))
    actual = {asset['name']: asset.get('digest') for asset in uploaded['assets']}
    if (not uploaded['draft'] or uploaded['target_commitish'] != source
            or actual != {name: 'sha256:' + digest for name, digest in expected.items()}):
        raise ValueError('uploaded draft differs from the verified payload; it remains unpublished')
    subprocess.run(['gh', 'release', 'edit', tag, '--repo', repo, '--draft=false'], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    ver = commands.add_parser('version')
    ver.add_argument('--bump', required=True)
    ver.add_argument('--prefix', default='v')
    for operation in ('build', 'validate', 'publish'):
        command = commands.add_parser(operation)
        command.add_argument('--binary', required=True)
        command.add_argument('--directory', default='release-assets')
        if operation != 'build':
            command.add_argument('--extra-files', default='')
        if operation != 'validate':
            command.add_argument('--tag', required=True)
            command.add_argument('--source', required=True)
        if operation == 'build':
            command.add_argument('--package', default='.')
            command.add_argument('--module-dir', default='.')
            command.add_argument('--version-symbol', default='')
            command.add_argument('--revision-symbol', default='')
        if operation == 'publish':
            command.add_argument('--repo', required=True)
    args = vars(parser.parse_args())
    operation = args.pop('operation')
    if 'extra_files' in args:
        args['extra_files'] = args['extra_files'].split()
    if operation == 'version':
        number, tag = version(**args)
        output = f'version={number}\ntag={tag}\n'
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
                stream.write(output)
        print(output, end='')
    else:
        globals()[operation](**args)


if __name__ == '__main__':
    main()
