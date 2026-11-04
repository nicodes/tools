"""Closed native CLI archives, exact release identities and native linkage checks."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import string
import struct
import subprocess
import tarfile
import zipfile

BINARY = re.compile(r'[a-z][a-z0-9-]*\Z')
FILE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]*\Z')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def host():
    system = platform.system()
    arch = {'amd64': 'x86_64', 'x86_64': 'x86_64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
    if system not in {'Linux', 'Darwin', 'Windows'} or arch is None:
        raise ValueError('unsupported native CLI host')
    return system, arch


def native_target(package, system=None, arch=None):
    system, arch = (system, arch) if system and arch else host()
    matches = [value for value in package['native_targets'] if value['platform'] == system and value['arch'] == arch]
    if len(matches) != 1:
        raise ValueError('this product does not declare exactly one matching native target')
    value = matches[0]
    if value['extension'] != ('zip' if system == 'Windows' else 'tar.gz'):
        raise ValueError('native archive extension differs from its platform')
    return value


def executable(package, record):
    binary = package['binary']
    if not BINARY.fullmatch(binary):
        raise ValueError('CLI binary must be a lowercase executable identifier')
    return binary + ('.exe' if record['platform'] == 'Windows' else '')


def archive_name(package, record):
    executable(package, record)
    if record['platform'] not in {'Linux', 'Darwin', 'Windows'} or record['arch'] not in {'x86_64', 'arm64'}:
        raise ValueError('invalid native archive platform')
    return package['binary'] + '_' + record['platform'] + '_' + record['arch'] + '.' + record['extension']


def members(package, record):
    names = [executable(package, record), *package.get('files', [])]
    if len(names) != len(set(names)) or any(not FILE.fullmatch(name) for name in names):
        raise ValueError('native archives require unique flat regular file names')
    return names


def inspect_archive(path, package, record, destination=None):
    expected = members(package, record)
    payload = {}
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('native archive must be a regular file')
    if record['extension'] == 'zip':
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) != len(expected) or {entry.filename for entry in entries} != set(expected):
                raise ValueError('native ZIP differs from the closed payload')
            for entry in entries:
                if entry.is_dir() or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}:
                    raise ValueError('native ZIP contains a non-regular entry')
                if entry.file_size > 512 * 1024 * 1024:
                    raise ValueError('native archive entry exceeds its bound')
                payload[entry.filename] = archive.read(entry)
    else:
        with tarfile.open(path, 'r:gz') as archive:
            entries = archive.getmembers()
            if len(entries) != len(expected) or {entry.name for entry in entries} != set(expected):
                raise ValueError('native TAR differs from the closed payload')
            for entry in entries:
                if not entry.isfile() or entry.size > 512 * 1024 * 1024:
                    raise ValueError('native TAR contains a non-regular or oversized entry')
                payload[entry.name] = archive.extractfile(entry).read()
    if destination is not None:
        destination = Path(destination)
        if destination.is_symlink():
            raise ValueError('native extraction destination cannot be a symlink')
        destination.mkdir(parents=True, exist_ok=True)
        for name in expected:
            target = destination / name
            if target.exists() or target.is_symlink():
                raise ValueError('native extraction requires unused output names')
            target.write_bytes(payload[name])
            target.chmod(0o755 if name == executable(package, record) else 0o644)
        return destination / executable(package, record)
    return payload


def write_archive(root, binary, package, record, destination):
    root, binary, destination = Path(root), Path(binary), Path(destination)
    if binary.is_symlink() or not binary.is_file():
        raise ValueError('native binary must be a regular file')
    files = [(executable(package, record), binary)]
    files += [(name, root / name) for name in package.get('files', [])]
    members(package, record)
    if any(path.is_symlink() or not path.is_file() for _, path in files):
        raise ValueError('native package sources must be regular files')
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / archive_name(package, record)
    if archive.is_symlink():
        raise ValueError('native archive output cannot be a symlink')
    if record['extension'] == 'zip':
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, path in files:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | (0o755 if path == binary else 0o644)) << 16
                bundle.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    else:
        with archive.open('wb') as output, gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as bundle:
                for name, path in files:
                    info = tarfile.TarInfo(name)
                    info.size = path.stat().st_size
                    info.mode = 0o755 if path == binary else 0o644
                    with path.open('rb') as stream:
                        bundle.addfile(info, stream)
    inspect_archive(archive, package, record)
    return archive


def identity_values(package, record, environment):
    return {'binary': package['binary'], 'version': environment.get('VERSION', 'dev'),
            'commit': environment['COMMIT'], 'date': environment.get('DATE', 'unknown'),
            'target': environment.get('RUST_TARGET', ''), 'platform': record['platform'], 'arch': record['arch']}


def render(template, values):
    if not isinstance(template, str):
        raise ValueError('native identity template must be text')
    for _text, field, spec, conversion in string.Formatter().parse(template):
        if field is not None and (field not in values or spec or conversion):
            raise ValueError('native identity templates accept exact named values only')
    return template.format(**values)


def verify_identity(binary, package, record, environment):
    binary = str(Path(binary).resolve())
    values = identity_values(package, record, environment)
    expected = render(package['version_template'], values)
    output = subprocess.check_output([binary, *package['version_arguments']], text=True, timeout=10).strip()
    if output != expected:
        raise ValueError('native executable version/source differs from the requested build')
    if package.get('info_arguments'):
        actual = json.loads(subprocess.check_output([binary, *package['info_arguments']], text=True, timeout=10))
        expected = {name: render(value, values) for name, value in package['info_fields'].items()
                    if '{target}' not in value or values['target']}
        if (package.get('info_exact', False) and actual != expected) or any(actual.get(name) != value for name, value in expected.items()):
            raise ValueError('native executable build receipt differs from source/target identity')


def verify_linkage(path, system):
    if system == 'Linux':
        result = subprocess.check_output(['readelf', '-l', '-d', str(path)], text=True)
        if 'INTERP' in result or '(NEEDED)' in result:
            raise ValueError('native Linux executable requires an external runtime')
    elif system == 'Windows':
        data = Path(path).read_bytes()
        try:
            if data[:2] != b'MZ':
                raise ValueError('native Windows executable has no DOS header')
            pe = struct.unpack_from('<I', data, 0x3c)[0]
            if data[pe:pe+4] != b'PE\0\0':
                raise ValueError('native Windows executable has no PE header')
            count, optional_size = struct.unpack_from('<H', data, pe+6)[0], struct.unpack_from('<H', data, pe+20)[0]
            optional = pe + 24
            if count > 96 or struct.unpack_from('<H', data, optional)[0] != 0x20b:
                raise ValueError('unsupported native Windows executable shape')
            sections = optional + optional_size
            def offset(rva):
                for index in range(count):
                    size, address, raw_size, raw_offset = struct.unpack_from('<IIII', data, sections + index*40 + 8)
                    if address <= rva < address + max(size, raw_size):
                        return raw_offset + rva - address
                raise ValueError('PE import address has no section')
            imports = struct.unpack_from('<I', data, optional + 120)[0]
            if not imports:
                return
            cursor = offset(imports)
            for _index in range(4096):
                if len(data[cursor:cursor+20]) != 20:
                    raise ValueError('truncated PE imports')
                if not any(data[cursor:cursor+20]):
                    return
                name = offset(struct.unpack_from('<I', data, cursor + 12)[0])
                library = data[name:data.index(b'\0', name)].decode('ascii').lower()
                if library.startswith(('vcruntime', 'msvcp', 'api-ms-win-crt-')):
                    raise ValueError('native Windows executable requires an external C runtime')
                cursor += 20
            raise ValueError('PE import table exceeds its bound')
        except (struct.error, IndexError, UnicodeError) as error:
            raise ValueError('malformed native Windows executable') from error


def checksums(directory, names):
    directory = Path(directory)
    if not names or len(names) != len(set(names)):
        raise ValueError('native checksum identities must be nonempty and unique')
    for name in names:
        if not FILE.fullmatch(name) or (directory / name).is_symlink() or not (directory / name).is_file():
            raise ValueError('native checksums require regular flat payload files')
    return ''.join(digest(directory / name) + '  ' + name + '\n' for name in sorted(names))
