import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
import addon_fixtures


class ExportControls(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'project.godot').write_text('[application]\n')
        self.entry = dict(project='.', preset='Web', output='dist/web/index.html', timeout=120,
                          required_outputs=['index.html', 'index.wasm'])
        self.config = {'components': {'fixture_exports': [self.entry]}}

    def execute(self, command, **options):
        self.assertEqual(options['timeout'], 120)
        self.assertEqual(command[1:3], ['--headless', '--path'])
        output = Path(command[-1])
        output.write_text('native html')
        output.with_suffix('.wasm').write_bytes(b'native wasm')
        return subprocess.CompletedProcess(command, 0, 'export complete\n', '')

    def test_outputs_are_declared_and_freshly_generated(self):
        self.assertEqual(addon_fixtures.artifact_paths(self.root, self.config), ['dist/web'])
        addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': '/pinned/godot'}, execute=self.execute)
        self.assertEqual((self.root / 'dist/web/index.wasm').read_bytes(), b'native wasm')

    def test_zero_exit_error_and_stale_output_are_rejected(self):
        output = self.root / 'dist/web'
        output.mkdir(parents=True)
        for name in self.entry['required_outputs']:
            (output / name).write_bytes(b'stale')
        def empty(command, **options):
            return subprocess.CompletedProcess(command, 0, '', '')
        with self.assertRaises((ValueError, FileNotFoundError)):
            addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': 'godot'}, execute=empty)
        self.assertFalse((output / 'index.html').exists())
        def error(command, **options):
            self.execute(command, **options)
            return subprocess.CompletedProcess(command, 0, 'SCRIPT ERROR: failed\n', '')
        with self.assertRaisesRegex(ValueError, 'export failed'):
            addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': 'godot'}, execute=error)

    def test_rejects_escaping_or_overlapping_output_plan_before_execution(self):
        self.entry['output'] = '../escaped/index.html'
        with self.assertRaises(ValueError):
            addon_fixtures.artifact_paths(self.root, self.config)
        self.entry['output'] = 'dist/web/index.html'
        self.config['components']['fixture_exports'].append(dict(self.entry))
        with self.assertRaisesRegex(ValueError, 'distinct'):
            addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': 'godot'}, execute=self.execute)

    def test_installed_fixture_contains_exact_accepted_archive_bytes(self):
        fixture = self.root / 'tests/web'
        fixture.mkdir(parents=True)
        (fixture / 'project.godot').write_text('[application]\n')
        source = self.root / 'addon'
        source.mkdir()
        (source / 'plugin.cfg').write_bytes(b'accepted plugin bytes')
        manifest = self.root / 'package.files'
        manifest.write_text('plugin.cfg\n')
        archive = self.root / 'dist/plugin.zip'
        helper = addon_fixtures.package_helper()
        digest = helper.build(source, manifest, archive)
        archive.with_suffix('.zip.sha256').write_text(digest + '  plugin.zip\n')
        self.config['package'] = dict(source='addon', manifest='package.files', archive='dist/plugin.zip')
        self.entry.update(project='tests/web', install_archive='addons/@owner_plugin')
        def inspect(command, **options):
            installed = Path(command[3]) / 'addons/@owner_plugin/plugin.cfg'
            self.assertEqual(installed.read_bytes(), b'accepted plugin bytes')
            return self.execute(command, **options)
        addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': 'godot'}, execute=inspect)
        self.entry['import_timeout'] = 45
        def import_and_mutate(command, **options):
            if '--import' in command:
                self.assertEqual(options['timeout'], 45)
                return subprocess.CompletedProcess(command, 0, '', '')
            result = self.execute(command, **options)
            (Path(command[3]) / 'addons/@owner_plugin/plugin.cfg').write_bytes(b'changed by engine')
            return result
        with self.assertRaisesRegex(ValueError, 'installed addon bytes'):
            addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': 'godot'}, execute=import_and_mutate)
        (source / 'plugin.cfg').write_bytes(b'mutated after packaging')
        with self.assertRaisesRegex(ValueError, 'source'):
            addon_fixtures.build_exports(self.root, self.config, {'GODOT_BIN': 'godot'}, execute=inspect)


if __name__ == '__main__':
    unittest.main()
