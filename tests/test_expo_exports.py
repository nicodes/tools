from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
import expo_exports


class NativeExportControls(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'app').mkdir()
        self.config = {'components': {'app': {'directory': 'app',
            'fixture_exports': [{'output': 'dist-e2e', 'environment': {'EXPO_PUBLIC_RUNTIME': 'development', 'BUILD_VARIANT': 'local'}}],
            'native_exports': [{'platforms': ['ios', 'android'], 'output': '.artifacts/native'}]}}}
        self.calls = []

    def execute(self, command, cwd, environment, timeout=None):
        self.calls.append((command, cwd, environment))
        output = self.root / 'app/dist-e2e' if '--app' in command else Path(command[-1])
        output.mkdir(parents=True)
        (output / 'bundle.js').write_text('verified native compiler result')

    def test_native_platforms_and_web_variants_are_compiled_only_into_declared_build_scope(self):
        self.assertEqual(expo_exports.artifact_paths(self.root, self.config), ['app/dist-e2e', '.artifacts/native'])
        environment = {'BUILD_VARIANT': 'production', 'GITHUB_RUN_ID': 'current-context'}
        expo_exports.build(self.root, self.config, environment, self.execute, native_runner=self.execute)
        self.assertEqual(self.calls[0][2]['BUILD_VARIANT'], 'production')
        self.assertEqual(self.calls[1][2]['BUILD_VARIANT'], 'local')
        self.assertEqual(environment['BUILD_VARIANT'], 'production')
        self.assertEqual([self.calls[0][0][index + 1] for index, argument in enumerate(self.calls[0][0]) if argument == '--platform'], ['ios', 'android'])
        self.assertTrue(json.loads((self.root / '.artifacts/native/variant-manifest.json').read_text())['bundle.js'])

    def test_unknown_or_escaping_output_plan_fails_before_any_compiler_execution(self):
        self.config['components']['app']['native_exports'][0]['output'] = '../outside'
        with self.assertRaises(ValueError):
            expo_exports.build(self.root, self.config, {}, self.execute, native_runner=self.execute)
        self.assertEqual(self.calls, [])

    def test_zero_exit_compiler_without_real_bundle_is_rejected(self):
        def empty(command, cwd, environment, timeout=None):
            output = Path(command[-1])
            output.mkdir(parents=True)
            (output / 'index.html').write_text('missing the executable bundle')
        with self.assertRaisesRegex(ValueError, 'no JavaScript'):
            expo_exports.build(self.root, self.config, {}, empty, native_runner=empty)


if __name__ == '__main__':
    unittest.main()
