import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

COOKBOOK = Path(__file__).resolve().parents[1] / 'cookbook'
sys.path.insert(0, str(COOKBOOK))
spec = importlib.util.spec_from_file_location('application_images', COOKBOOK / 'images.py')
images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(images)


class ApplicationImages(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='image source ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'deploy/images').mkdir(parents=True)
        for name in ['api', 'gate', 'config']:
            (self.root / 'deploy/images' / (name + '.Dockerfile')).write_text('FROM scratch\n')
        self.value = {'image_base': 'ghcr.io/owner/example', 'inputs': {'images': [
            'ghcr.io/owner/example-api:{revision}', 'ghcr.io/owner/example-gate:{revision}',
            'ghcr.io/owner/example-config:{revision}']}}
        self.config = {'components': {'images': [{'name': name} for name in ['api', 'gate', 'config']]}}
        self.environment = {'APP_VERSION': 'a' * 40}
        self.calls = []

    def capture(self, command, root, environment):
        self.calls.append((command, root, environment))

    def build(self, config=None, native=None):
        with patch.object(images, 'definition', return_value=self.value):
            images.build(self.root, config or self.config, self.environment, self.capture, native=native)

    def test_build_uses_only_declared_images_and_dedicated_component_cache(self):
        self.config['components']['images'][0].update(cache_key='api', build_arguments={'SOURCE_REVISION': '{revision}'})
        self.build()
        first = self.calls[0][0]
        self.assertIn('--cache-key', first)
        self.assertIn('SOURCE_REVISION=' + 'a' * 40, first)
        self.assertNotIn('--push', first)
        self.assertEqual([call[0][call[0].index('-t') + 1] for call in self.calls],
                         ['ghcr.io/owner/example-' + name + ':' + 'a' * 40 for name in ['api', 'gate', 'config']])

    def test_missing_or_duplicate_shipping_components_fail_before_building(self):
        for records in [[{'name': 'api'}], [{'name': 'api'}, {'name': 'api'}, {'name': 'gate'}, {'name': 'config'}]]:
            with self.subTest(records=records), self.assertRaises(ValueError):
                self.build({'components': {'images': records}})
            self.assertEqual(self.calls, [])

    def test_native_preparation_is_never_silently_skipped(self):
        self.config['components']['images'][0]['native'] = [{'path': 'internal/compiler.py'}]
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(self.calls, [])

    def test_compose_validation_uses_empty_owned_placeholders_and_no_dotenv(self):
        source = self.root / 'deploy/compose.yml'
        source.write_text('services: {}\n')
        (self.root / '.env').write_text('NEVER_READ=private-local-data\n')
        observed = []
        def validate(command, _root, _environment):
            directory = Path(command[command.index('--project-directory') + 1])
            observed.append((directory / 'secrets.env').read_bytes())
            observed.append(Path(command[command.index('--env-file') + 1]).read_bytes())
        config = {'components': {'compose': {'environment_files': ['secrets.env']}}}
        images.validate_compose(self.root, config, self.environment, validate)
        self.assertEqual(observed, [b'', b''])
        self.assertEqual(source.read_text(), 'services: {}\n')
        with self.assertRaises(ValueError):
            images.validate_compose(self.root, {'components': {'compose': {'environment_files': ['../outside']}}}, {}, validate)

    def test_rust_scan_requires_every_lock_inventory_and_blocks_high_findings(self):
        manifests = ['api/Cargo.toml', 'internal/runtime/Cargo.toml']
        for name in manifests:
            directory = (self.root / name).parent
            directory.mkdir(parents=True)
            (directory / 'Cargo.lock').write_text('version=4\n')
        findings = []
        def scan(command, _root, _environment):
            report = {'SchemaVersion': 2, 'Results': [
                {'Target': str(index) + '/Cargo.lock', 'Type': 'cargo', 'Packages': [{'Name': 'dependency'}],
                 'Vulnerabilities': findings} for index in range(2)]}
            Path(command[command.index('--output') + 1]).write_text(json.dumps(report))
        images.rust_inventory(self.root, manifests, {}, scan)
        findings.append({'Severity': 'HIGH', 'FixedVersion': ''})
        with self.assertRaisesRegex(ValueError, 'High/Critical'):
            images.rust_inventory(self.root, manifests, {}, scan)

    def test_rust_runtime_matches_exact_binary_and_always_removes_inspection_container(self):
        (self.root / 'api').mkdir()
        (self.root / 'api/Cargo.lock').write_bytes(b'locked-dependencies')
        (self.root / '.artifacts').mkdir()
        binary = self.root / '.artifacts/service'
        binary.write_bytes(b'\x7fELFcompiled-source')
        contract = {'lock_source': 'api/Cargo.lock', 'lock_installed': '/usr/share/service/Cargo.lock',
                    'binaries': [{'source': '.artifacts/service', 'installed': '/usr/local/bin/service'}]}
        removed, tampered = [], []
        def query(command, **_kwargs):
            if command[1:3] == ['image', 'inspect']:
                return json.dumps([{'Id': 'sha256:' + 'b' * 64, 'Config': {'User': '10001:10001'}}])
            return 'c' * 64
        def docker(command, **_kwargs):
            if command[1] == 'cp':
                data = b'locked-dependencies' if command[2].endswith('/Cargo.lock') else binary.read_bytes() + (b'changed' if tampered else b'')
                Path(command[3]).write_bytes(data)
            if command[1] == 'rm':
                removed.append(command[3])
            return subprocess.CompletedProcess(command, 0)
        with patch.object(images.subprocess, 'check_output', side_effect=query), patch.object(images.subprocess, 'run', side_effect=docker):
            images.rust_image(self.root, 'image:source', contract)
            tampered.append(True)
            with self.assertRaisesRegex(ValueError, 'accepted native executable'):
                images.rust_image(self.root, 'image:source', contract)
        self.assertEqual(removed, ['c' * 64, 'c' * 64])


if __name__ == '__main__':
    unittest.main()
