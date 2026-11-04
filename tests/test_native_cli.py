import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch
import sys

spec = importlib.util.spec_from_file_location('native_cli_package_controls', Path(__file__).resolve().parents[1] / 'helpers/native_cli.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class NativeArchive(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root / 'compiled'
        self.binary.write_bytes(b'compiled executable bytes')
        (self.root / 'README.md').write_text('# Example CLI\n')
        self.package = {'binary': 'example', 'files': ['README.md']}

    def record(self, system):
        return {'platform': system, 'arch': 'x86_64', 'extension': 'zip' if system == 'Windows' else 'tar.gz'}

    def test_packaging_preserves_closed_bytes_and_is_timestamp_independent(self):
        for system in ['Linux', 'Windows']:
            with self.subTest(system=system):
                record = self.record(system)
                archive = native.write_archive(self.root, self.binary, self.package, record, self.root / system)
                original = archive.read_bytes()
                self.binary.touch()
                native.write_archive(self.root, self.binary, self.package, record, self.root / system)
                self.assertEqual(archive.read_bytes(), original)
                extracted = native.inspect_archive(archive, self.package, record, self.root / ('installed' + system))
                self.assertEqual(extracted.read_bytes(), self.binary.read_bytes())

    def test_unexpected_duplicate_or_nonregular_zip_entries_are_rejected(self):
        for scenario in ['unexpected', 'duplicate', 'symlink']:
            with self.subTest(scenario=scenario):
                path = self.root / 'hostile.zip'
                with zipfile.ZipFile(path, 'w') as archive:
                    info = zipfile.ZipInfo('example.exe')
                    info.external_attr = ((stat.S_IFLNK if scenario == 'symlink' else stat.S_IFREG) | 0o755) << 16
                    archive.writestr(info, b'bytes')
                    archive.writestr('README.md', b'readme')
                    if scenario in ['unexpected', 'duplicate']:
                        archive.writestr('other' if scenario == 'unexpected' else 'example.exe', b'bytes')
                with self.assertRaises(ValueError):
                    native.inspect_archive(path, self.package, self.record('Windows'))

    def test_tar_symlinks_and_traversal_are_rejected_before_extraction(self):
        for scenario in ['symlink', 'traversal']:
            path = self.root / 'hostile.tar.gz'
            with tarfile.open(path, 'w:gz') as archive:
                entry = tarfile.TarInfo('../example' if scenario == 'traversal' else 'example')
                entry.type = tarfile.SYMTYPE
                entry.linkname = '/outside'
                archive.addfile(entry)
                entry = tarfile.TarInfo('README.md')
                entry.size = 3
                archive.addfile(entry, io.BytesIO(b'abc'))
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                native.inspect_archive(path, self.package, self.record('Linux'), self.root / 'not-installed')
            self.assertFalse((self.root / 'not-installed').exists())

    def test_identity_templates_cannot_access_attributes_or_unknown_values(self):
        for template in ['{version.__class__}', '{unknown}', '{version!r}', '{version:>10}']:
            with self.subTest(template=template), self.assertRaises(ValueError):
                native.render(template, {'version': 'dev'})

    def test_existing_install_names_are_never_overwritten(self):
        record = self.record('Linux')
        archive = native.write_archive(self.root, self.binary, self.package, record, self.root / 'dist')
        destination = self.root / 'installed'
        destination.mkdir()
        (destination / 'example').write_bytes(b'existing')
        with self.assertRaises(ValueError):
            native.inspect_archive(archive, self.package, record, destination)
        self.assertEqual((destination / 'example').read_bytes(), b'existing')

    @unittest.skipIf(os.name == 'nt', 'fixture executable uses a POSIX shebang; actual Windows native fixtures are verified by hosted consumers')
    def test_cached_reference_fixture_requires_exact_archive_binary_and_source(self):
        system, arch = native.host()
        target = {'platform': system, 'arch': arch, 'extension': 'tar.gz', 'target': 'native-host'}
        source = 'a' * 40
        self.binary.write_text('#!' + sys.executable + '\nprint("example fixture")\nprint("commit: ' + source + '")\n')
        cache = self.root / '.artifacts/fixtures/example' / (system + '-' + arch)
        archive = native.write_archive(self.root, self.binary, self.package, target, cache)
        checksums = native.checksums(cache, [archive.name])
        (cache / 'checksums.txt').write_text(checksums)
        record = {'tag': 'v0.0.1', 'source_commit': source, 'archives': {archive.name: native.digest(archive)},
                  'checksums_sha256': native.digest(cache / 'checksums.txt')}
        (self.root / 'fixture.json').write_text(json.dumps(record))
        declaration = {'repository': 'owner/project', 'binary': 'example', 'manifest': 'fixture.json',
                       'native_targets': [target], 'version_first_lines': ['example fixture'],
                       'source_line': 'commit: {source_commit}'}
        with patch.object(native.urllib.request, 'urlopen', side_effect=AssertionError('cached fixture must not download')):
            binary = native.immutable_fixture(self.root, declaration, install=True)
            self.assertEqual(native.immutable_fixture(self.root, declaration), binary)
            binary.write_bytes(b'changed after fixture setup')
            with self.assertRaisesRegex(ValueError, 'changed after preparation'):
                native.immutable_fixture(self.root, declaration)


if __name__ == '__main__':
    unittest.main()
