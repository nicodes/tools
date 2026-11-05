"""Build closed addon and registry archives with embedded per-file provenance."""
import argparse
import configparser
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import zipfile

from configuration import load, relative

TOOLS = Path(__file__).resolve().parents[1]


def package_helper():
    spec = importlib.util.spec_from_file_location('manifest_addon_package', TOOLS / 'helpers/addon-package.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inputs(root, config):
    values = config['package']
    required = {'source', 'manifest', 'archive', 'format', 'prefix', 'registry_archive', 'version_source', 'allowlist'}
    if set(values) - (required | {'generated_directories', 'script_uids'}) or not required <= set(values) or values['format'] != 'manifest':
        raise ValueError('manifest addon packaging requires explicit native package inputs')
    source = relative(root, values['source'], exists=True)
    names = package_helper().manifest_files(relative(root, values['manifest'], exists=True))
    prefix = values['prefix']
    if (not isinstance(prefix, str) or not prefix.endswith('/') or PurePosixPath(prefix).as_posix() + '/' != prefix
            or PurePosixPath(prefix).is_absolute() or '..' in PurePosixPath(prefix).parts or '\\' in prefix or ':' in prefix):
        raise ValueError('unsafe addon archive prefix')
    allowed = json.loads(relative(root, values['allowlist'], exists=True).read_text())
    if (sorted(allowed.get('files', [])) != names or allowed.get('addon_files') != len(names)
            or allowed.get('zip_entries') != len(names) + 1):
        raise ValueError('addon allowlist counts differ from the closed file manifest')
    generated = values.get('generated_directories', [])
    if (not isinstance(generated, list) or any(not isinstance(name, str) or name != '.gdam' for name in generated)
            or len(set(generated)) != len(generated)):
        raise ValueError('only the native generated dependency directory may be excluded')
    actual = {}
    for path in source.rglob('*'):
        rel = path.relative_to(source)
        if path.is_symlink():
            raise ValueError('addon source contains a symlink')
        if any(rel == Path(name) or Path(name) in rel.parents for name in generated):
            if subprocess.check_output(['git', 'ls-files', '--', path.relative_to(root).as_posix()], cwd=root, text=True).strip():
                raise ValueError('generated addon exclusions may not hide tracked source')
            continue
        if path.is_file():
            actual[rel.as_posix()] = path
        elif not path.is_dir():
            raise ValueError('addon source contains a non-regular file')
    if sorted(actual) != names:
        raise ValueError('addon source differs from the closed file manifest')
    version = json.loads(relative(root, values['version_source'], exists=True).read_text()).get('version')
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('addon requires a native semantic version')
    plugin = configparser.ConfigParser(interpolation=None)
    plugin.read(source / 'plugin.cfg')
    if plugin.get('plugin', 'version', fallback='') != '"' + version + '"':
        raise ValueError('addon native package and plugin versions differ')
    if values.get('script_uids', False):
        seen = set()
        for name in names:
            if name.endswith('.gd'):
                value = actual.get(name + '.uid')
                uid = value.read_text().strip() if value else ''
                if not re.fullmatch(r'uid://[a-z0-9]+', uid) or uid in seen:
                    raise ValueError('shipped GDScript requires distinct UID sidecars')
                seen.add(uid)
    archive = relative(root, values['archive'])
    registry = relative(root, values['registry_archive'])
    if archive == registry or archive.parent != registry.parent or archive.resolve().is_relative_to(source.resolve()):
        raise ValueError('addon archive outputs must be distinct siblings outside source')
    return actual, prefix, version, archive, registry


def artifact_paths(root, config):
    values = config['package']
    archive = relative(root, values['archive'])
    registry = relative(root, values['registry_archive'])
    return [path.relative_to(root).as_posix() for path in
            [archive, archive.with_suffix(archive.suffix + '.sha256'), registry,
             registry.with_suffix(registry.suffix + '.sha256'), archive.parent / 'PACKAGE_MANIFEST.json']]


def write_zip(path, entries):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)


def build(root, config):
    root = Path(root).resolve()
    files, prefix, version, archive, registry = inputs(root, config)
    records = [(prefix + name, path.read_bytes(), 0o644 | (path.stat().st_mode & 0o111)) for name, path in sorted(files.items())]
    manifest = {'plugin_version': version, 'closed': True, 'zip_entries': len(records) + 1,
                'files': [{'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                          for name, data, _ in records]}
    encoded = (json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode()
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='manifest-addon-', dir=archive.parent) as directory:
        candidate = Path(directory) / archive.name
        write_zip(candidate, [*records, ('PACKAGE_MANIFEST.json', encoded, 0o644)])
        candidate.replace(archive)
        candidate = Path(directory) / registry.name
        write_zip(candidate, [(name[len(prefix):], data, mode) for name, data, mode in records])
        candidate.replace(registry)
    for path in [archive, registry]:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_suffix(path.suffix + '.sha256').write_text(digest + '  ' + path.name + '\n')
    (archive.parent / 'PACKAGE_MANIFEST.json').write_bytes(encoded)
    return verify(root, config)


def verify(root, config):
    files, prefix, version, archive, registry = inputs(root, config)
    expected_records = [{'path': prefix + name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}
                        for name, path in sorted(files.items())]
    expected_manifest = {'plugin_version': version, 'closed': True, 'zip_entries': len(files) + 1, 'files': expected_records}
    helper = package_helper()
    for path, prefixed in [(archive, True), (registry, False)]:
        helper.verify_checksum(path)
        with zipfile.ZipFile(path) as package:
            expected_names = [prefix + name if prefixed else name for name in sorted(files)]
            if prefixed:
                expected_names.append('PACKAGE_MANIFEST.json')
            if package.namelist() != expected_names:
                raise ValueError('addon archive entries differ from the exact closed manifest')
            for item in package.infolist():
                if not stat.S_ISREG(item.external_attr >> 16):
                    raise ValueError('addon archive entry must be a regular file')
            for name, original in files.items():
                if package.read(prefix + name if prefixed else name) != original.read_bytes():
                    raise ValueError('addon archive payload differs from its source')
            if prefixed:
                embedded = package.read('PACKAGE_MANIFEST.json')
                if json.loads(embedded) != expected_manifest:
                    raise ValueError('embedded addon provenance differs from its source')
                if embedded != (archive.parent / 'PACKAGE_MANIFEST.json').read_bytes():
                    raise ValueError('external addon provenance differs from embedded bytes')
            if package.testzip() is not None:
                raise ValueError('addon archive integrity failed')
    return hashlib.sha256(archive.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['build', 'verify'])
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        print(globals()[args.operation](root, load(root)))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
