"""Verify upstream identity and bounded account-file reads without changing users."""
import hashlib
import copy
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import io
import tarfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'postgres-runtime/su-exec'


class SuExecSource(unittest.TestCase):
    def test_pinned_upstream_and_reviewed_hardening(self):
        pin = json.loads((SOURCE / 'PIN.json').read_text())
        actual = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (SOURCE / 'source').iterdir() if p.is_file()}
        self.assertEqual(actual, pin['source_sha256'])
        source = (SOURCE / 'source/su-exec.c').read_text()
        original = source.replace('\n#include "account-files.h"\n', '').replace(
            '\tcheck_account_file("/etc/passwd");\n'
            '\tcheck_account_file("/etc/group");\n\n', '')
        self.assertEqual(hashlib.sha256(original.encode()).hexdigest(),
                         pin['upstream_files_sha256']['su-exec.c'])
        self.assertEqual(actual['LICENSE'], pin['upstream_files_sha256']['LICENSE'])


class AccountFileBounds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='su-exec-bounds-')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        harness = cls.root / 'check.c'
        harness.write_text(
            '#include <sys/types.h>\n#include <err.h>\n#include <errno.h>\n'
            '#include <stdio.h>\n#include <stdlib.h>\n#include <unistd.h>\n'
            '#include "account-files.h"\n'
            'int main(int argc, char **argv) {\n'
            '  if (argc != 2) return 2;\n'
            '  check_account_file(argv[1]); return 0;\n}\n')
        cls.binary = cls.root / 'check'
        subprocess.run([shutil.which('cc') or 'cc', '-Wall', '-Wextra', '-Werror',
                        '-I', str(SOURCE / 'source'), str(harness), '-o', str(cls.binary)],
                       check=True, capture_output=True, text=True, timeout=30)

    def check(self, path):
        return subprocess.run([str(self.binary), str(path)], capture_output=True,
                              text=True, timeout=3)

    def test_normal_accounts_and_record_boundary(self):
        path = self.root / 'normal'
        for contents in ['sample:x:123:456:Example:/tmp:/bin/false\n',
                         'sample:x:456:alice,bob\n', 'a' * ((64 << 10) - 1) + '\n']:
            path.write_text(contents)
            self.assertEqual(self.check(path).returncode, 0)

    def test_oversized_account_records_and_file(self):
        path = self.root / 'oversized'
        for contents in ['a' * (64 << 10), 'sample:x:123:456:' + 'a' * (128 << 10),
                         'sample:x:456:' + 'a' * (2 << 20)]:
            path.write_text(contents)
            result = self.check(path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('account record exceeds limit', result.stderr)
        with path.open('wb') as handle:
            handle.truncate((10 << 20) + 1)
        self.assertIn('account file exceeds limit', self.check(path).stderr)

    def test_nonregular_and_symlink_accounts_are_refused(self):
        self.assertNotEqual(self.check('/dev/zero').returncode, 0)
        link = self.root / 'account-link'
        link.symlink_to('/etc/passwd')
        self.assertNotEqual(self.check(link).returncode, 0)


class MinimalRuntimeInputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        entry = ROOT / 'postgres-runtime/runtime/build.py'
        spec = importlib.util.spec_from_file_location('postgres_runtime_constructor', entry)
        cls.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.builder)
        cls.pin = json.loads((entry.parent / 'PIN.json').read_text())

    def test_real_constructor_pin_preserves_retained_identity(self):
        self.assertEqual(self.builder.validate_pin(copy.deepcopy(self.pin)), self.pin)
        self.assertEqual((self.pin['database_uid'], self.pin['database_gid']), (999, 999))
        self.assertEqual((self.pin['locale'], self.pin['collation_version']), ('en_US.utf8', '2.41'))

    def test_exact_socket_and_interface_defaults_cannot_be_removed_or_substituted(self):
        for change in ['remove', 'duplicate', 'localhost', 'other-path']:
            value = copy.deepcopy(self.pin)
            patches = value['postgresql']['source_patches']
            if change == 'remove':
                patches.pop()
            elif change == 'duplicate':
                patches[1] = copy.deepcopy(patches[0])
            elif change == 'localhost':
                patches[1]['new'] = "listen_addresses = 'localhost'"
            else:
                patches[1]['path'] = '../foreign-config'
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.builder.validate_pin(value)

    def test_unpublished_source_or_changed_feature_set_is_refused(self):
        for field, value in [('url', 'https://example.invalid/postgres.tar.bz2'),
                             ('version', '19.0'), ('sha256', 'not-a-digest'),
                             ('configure', ['--prefix=/tmp/other'])]:
            pin = copy.deepcopy(self.pin)
            pin['postgresql'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.builder.validate_pin(pin)

    def test_mutable_bases_ambiguous_or_option_packages_are_refused(self):
        variants = []
        for field, value in [('runtime_base', 'debian:latest'), ('version', True),
                             ('runtime_packages', ['--allow-unauthenticated']),
                             ('runtime_packages', ['libc6', 'libc6']),
                             ('runtime_packages', [{}]), ('database_uid', 0)]:
            pin = copy.deepcopy(self.pin)
            pin[field] = value
            variants.append(pin)
        pin = copy.deepcopy(self.pin)
        pin['unreviewed'] = 'extra'
        variants.append(pin)
        for pin in variants:
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                self.builder.validate_pin(pin)

    def test_changed_postgresql_bytes_refuse_before_source_extraction(self):
        with tempfile.TemporaryDirectory(prefix='postgres-source-identity-') as directory:
            work = Path(directory)

            def downloaded(arguments, **kwargs):
                self.assertEqual(arguments[0], 'curl')
                (work / 'postgresql.tar.bz2').write_bytes(b'changed upstream source')

            with patch.object(self.builder, 'WORK', work), patch.object(self.builder, 'run', side_effect=downloaded):
                with self.assertRaisesRegex(ValueError, 'published immutable hash'):
                    self.builder.build_postgres()
            self.assertEqual([path.name for path in work.iterdir()], ['postgresql.tar.bz2'])

    def test_frozen_entrypoint_source_has_exact_image_identity(self):
        values = self.builder.entrypoint_source()
        self.assertEqual(hashlib.sha256(values['docker-entrypoint.sh']).hexdigest(),
                         '9c440299ae04a0a79d55b8bf03307036d890a40979d2fb698073c9050d4b20a5')
        self.assertEqual(values['docker-entrypoint.sh'].count(b'exec gosu postgres '), 1)

    def test_changed_or_linked_entrypoint_source_is_refused(self):
        originals = self.builder.entrypoint_source()
        with tempfile.TemporaryDirectory(prefix='postgres-entrypoint-identity-') as directory:
            root = Path(directory)
            source = root / 'entrypoint'
            source.mkdir()
            for name, contents in originals.items():
                (source / name).write_bytes(contents)
            selected = source / 'docker-entrypoint.sh'
            with patch.object(self.builder, 'HERE', root):
                selected.write_bytes(b'changed entrypoint')
                with self.assertRaisesRegex(ValueError, 'frozen identity'):
                    self.builder.entrypoint_source()
                selected.write_bytes(originals['docker-entrypoint.sh'])
                (root / 'another-source-reference').hardlink_to(selected)
                with self.assertRaisesRegex(ValueError, 'unlinked source'):
                    self.builder.entrypoint_source()

    def test_ignored_configure_option_refuses_even_successful_exit(self):
        def completed(arguments, **kwargs):
            kwargs['stdout'].write(b'configure: WARNING: unrecognized options: --unsupported\n')
            return subprocess.CompletedProcess(arguments, 0)
        with patch.object(self.builder.subprocess, 'run', side_effect=completed), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'unrecognized requested feature'):
                self.builder.configure(ROOT, ['--unsupported'], 'negative')

    def test_configure_error_and_oversized_diagnostics_refuse(self):
        for contents, status, error in [(b'configure: error: dependency missing\n', 1, subprocess.CalledProcessError),
                                        (b'x' * ((4 << 20) + 1), 0, ValueError)]:
            def completed(arguments, **kwargs):
                kwargs['stdout'].write(contents)
                return subprocess.CompletedProcess(arguments, status)
            with patch.object(self.builder.subprocess, 'run', side_effect=completed), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(error):
                    self.builder.configure(ROOT, ['--prefix=/usr/local'], 'negative')


class ImmutableConsumer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / 'postgres-runtime/consumer.py'
        spec = importlib.util.spec_from_file_location('postgres_runtime_consumer', path)
        cls.consumer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.consumer)

    def test_real_closed_source_and_immutable_consumer(self):
        record = self.consumer.verify()
        self.assertIn('runtime/build.py', record['files'])
        result = self.consumer.render('a' * 40, 'b' * 64)
        self.assertIn('https://codeload.github.com/nicodes/tools/tar.gz/' + 'a' * 40, result)
        self.assertLess(result.index('sha256sum -c'), result.index('tar -xzf'))
        self.assertEqual(result.count('RUN /inputs/prepare-build.sh'), 1)
        self.assertEqual(result.count('RUN /inputs/build-launcher.sh'), 1)
        self.assertNotIn('ADD --checksum', result)
        for revision, digest in [('main', 'b' * 64), ('a' * 40, 'latest')]:
            with self.assertRaises(ValueError):
                self.consumer.render(revision, digest)

    def test_changed_or_unlisted_inputs_refuse_before_build(self):
        with tempfile.TemporaryDirectory(prefix='postgres-closed-inputs-') as directory:
            target = Path(directory) / 'source'
            shutil.copytree(ROOT / 'postgres-runtime', target)
            path = target / 'runtime/build.py'
            original = path.read_bytes()
            path.write_bytes(original + b'\n# substituted source\n')
            with self.assertRaises(ValueError):
                self.consumer.verify(target)
            path.write_bytes(original)
            (target / 'runtime/bundle.json').write_text('{}')
            with self.assertRaises(ValueError):
                self.consumer.verify(target)

    def test_source_links_are_not_borrowed_into_constructor(self):
        with tempfile.TemporaryDirectory(prefix='postgres-linked-inputs-') as directory:
            target = Path(directory) / 'source'
            shutil.copytree(ROOT / 'postgres-runtime', target)
            external = Path(directory) / 'outside'
            external.write_text('foreign source bytes')
            link = target / 'extra'
            link.symlink_to(external)
            with self.assertRaises(ValueError):
                self.consumer.verify(target)
            link.unlink()
            external.unlink()
            external.hardlink_to(target / 'runtime/build.py')
            with self.assertRaises(ValueError):
                self.consumer.verify(target)

    def test_downloaded_archive_must_equal_tested_constructor_bytes(self):
        revision = 'a' * 40
        with tempfile.TemporaryDirectory(prefix='postgres-source-archive-') as directory:
            archive = Path(directory) / 'source.tar.gz'

            def write(changed=False):
                with tarfile.open(archive, 'w:gz') as output:
                    for path in sorted((ROOT / 'postgres-runtime').rglob('*')):
                        if not path.is_file():
                            continue
                        data = path.read_bytes()
                        if changed and path.name == 'build.py':
                            data += b'\n# substituted constructor\n'
                        name = 'tools-' + revision + '/postgres-runtime/' + str(path.relative_to(ROOT / 'postgres-runtime'))
                        member = tarfile.TarInfo(name)
                        member.size = len(data)
                        output.addfile(member, io.BytesIO(data))

            write()
            self.assertEqual(self.consumer.verify_archive(archive, revision), hashlib.sha256(archive.read_bytes()).hexdigest())
            write(changed=True)
            with self.assertRaises(ValueError):
                self.consumer.verify_archive(archive, revision)


class NativeDiagnosticBoundary(unittest.TestCase):
    def test_kernel_listener_proof_requires_active_wildcard_binding(self):
        spec = importlib.util.spec_from_file_location('postgres_kernel_listener', ROOT / 'tests/native_postgres_runtime.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        header = '  sl  local_address rem_address st\n'
        self.assertTrue(fixture.wildcard_listener(header + '  0: 00000000:1538 00000000:0000 0A\n'))
        for row in ['0100007F:1538 00000000:0000 0A', '00000000:1539 00000000:0000 0A',
                    '00000000:1538 00000000:0000 01']:
            self.assertFalse(fixture.wildcard_listener(header + '  0: ' + row + '\n'))

    def test_native_fixture_obeys_the_actual_strict_scan_context_contract(self):
        spec = importlib.util.spec_from_file_location('postgres_native_context', ROOT / 'tests/native_postgres_runtime.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        import image_scan_context
        reference = fixture.source_reference('a' * 32, 'b' * 40)
        self.assertTrue(reference.endswith(':' + 'b' * 40))
        with self.assertRaises(ValueError):
            fixture.source_reference('a' * 32, 'main')
        raw = {'Id': 'sha256:' + 'c' * 64, 'RootFS': {'Type': 'layers', 'Layers': ['sha256:' + 'd' * 64]},
               'Config': {}, 'Os': 'linux', 'Architecture': 'amd64', 'RepoTags': [reference], 'Size': 1234}
        original = copy.deepcopy(raw)
        selected = fixture.scan_identity(raw)
        self.assertEqual(image_scan_context.identity(selected), selected)
        self.assertEqual(raw, original)
        with self.assertRaises(ValueError):
            image_scan_context.identity(raw)

    def test_fixture_adapter_and_error_evidence_never_emit_environment_values(self):
        path = ROOT / 'tests/native_postgres_runtime.py'
        spec = importlib.util.spec_from_file_location('postgres_native_fixture', path)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        native = fixture.Native({}, 'a' * 32)
        self.assertEqual(native.config_digest('b' * 64), 'sha256:' + 'b' * 64)
        with tempfile.TemporaryDirectory(prefix='postgres-diagnostic-boundary-') as directory:
            native.output = Path(directory)
            native.phase = 'synthetic-failure'
            native.redactions.add('synthetic-private-canary')
            native.last_error = {'verb': 'build', 'returncode': 1,
                'stderr_tail': native.clean(b'\x1b[31mfailed synthetic-private-canary\nSOURCE_TOKEN=do-not-retain\x1b[0m')}
            native.report(ValueError('bounded synthetic failure'))
            raw = (native.output / 'error.json').read_bytes()
            self.assertNotIn(b'synthetic-private-canary', raw)
            self.assertNotIn(b'do-not-retain', raw)
            self.assertIn(b'synthetic-failure', raw)
            self.assertIn(b'build', raw)


class SourceOwnedEntrypoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('postgres_shared_contract', ROOT / 'postgres-runtime/contract.py')
        cls.contract = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.contract)

    def test_literal_coreutils_bracket_path_keeps_shell_and_traversal_boundaries(self):
        self.assertTrue(self.contract.payload_path('/usr/local/bin/['))
        self.assertTrue(self.contract.payload_path('/usr/lib/x86_64-linux-gnu/libxxhash.so.0.8.3'))
        for path in ['relative', '/usr/../outside', '/usr/bin/x;id', '/usr/bin/$(id)',
                     '/usr/bin/x\nsecret', '/usr/bin/x y', '/usr/bin/' + 'a' * 4096]:
            with self.subTest(path=path[:64]):
                self.assertFalse(self.contract.payload_path(path))

    def test_exact_caller_source_mode_and_bytes_remain_mandatory(self):
        with tempfile.TemporaryDirectory(prefix='postgres-wrapper-source-') as directory:
            source = Path(directory) / 'entrypoint.sh'
            raw = b'#!/bin/sh\nexec /usr/local/bin/docker-entrypoint.sh "$@"\n'
            source.write_bytes(raw)
            path, expected = self.contract.wrapper_input({'path': '/usr/local/bin/project-postgres', 'source': source})
            self.contract.verify_wrapper(path, expected, lambda _: raw, lambda _: '0:0:555:1\n')
            for changed, mode in [(raw + b'# changed\n', '0:0:555:1'), (raw, '0:0:755:1'),
                                  (raw, '999:999:555:1'), (raw, '0:0:555:2')]:
                with self.assertRaises(ValueError):
                    self.contract.verify_wrapper(path, expected, lambda _: changed, lambda _: mode)
            for path in ['/tmp/entrypoint', '/usr/local/bin/../entrypoint', '/usr/local/bin/docker-entrypoint.sh']:
                with self.assertRaises(ValueError):
                    self.contract.wrapper_input({'path': path, 'source': source})
            linked = Path(directory) / 'alias'
            linked.symlink_to(source)
            with self.assertRaises(ValueError):
                self.contract.wrapper_input({'path': '/usr/local/bin/project-postgres', 'source': linked})
            linked.unlink()
            linked.hardlink_to(source)
            with self.assertRaises(ValueError):
                self.contract.wrapper_input({'path': '/usr/local/bin/project-postgres', 'source': source})
        self.assertEqual(self.contract.wrapper_input(None), ('/usr/local/bin/docker-entrypoint.sh', None))

    def test_image_reader_refuses_changed_identity_or_foreign_scope(self):
        image = 'sha256:' + 'a' * 64
        value = {'Id': 'b' * 64, 'Name': 'tools-pg-image-test', 'Image': image,
                 'Config': {'Labels': {'io.tools.pg-image-contract': 'owned-token'}}}
        adapter = type('Adapter', (), {'config_digest': staticmethod(lambda value: value)})()
        with patch.object(self.contract, 'RUNTIME', adapter):
            self.assertEqual(self.contract.reader_identity(value, image, value['Name'], 'owned-token', value['Id']), value['Id'])
            variants = []
            for key, changed in [('Id', 'c' * 64), ('Image', 'sha256:' + 'd' * 64), ('Name', 'foreign')]:
                current = copy.deepcopy(value);current[key] = changed;variants.append(current)
            current = copy.deepcopy(value);current['Config']['Labels']['io.tools.pg-image-contract'] = 'foreign-token';variants.append(current)
            for current in variants:
                with self.assertRaises(ValueError):
                    self.contract.reader_identity(current, image, value['Name'], 'owned-token', value['Id'])


if __name__ == '__main__':
    unittest.main()
