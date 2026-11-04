import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('configuration', Path(__file__).resolve().parents[1] / 'cookbook/configuration.py')
configuration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configuration)


class RecipeConfiguration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/test_behavior.py').write_text('assert 1 + 1 == 2\n')

    def write(self, text):
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="godot-addon"\n' + text)

    def test_native_test_programs_are_data_without_a_second_task_graph(self):
        self.write('[[tests.unit]]\nrunner="python"\npath="tests/test_behavior.py"\nargs=["--case", "literal;value"]\n')
        self.assertEqual(configuration.load(self.root)['tests']['unit'][0]['args'], ['--case', 'literal;value'])

    def test_declared_empty_or_unknown_suites_are_refused(self):
        for text in ['[tests]\nunit=[]\n', '[tests]\nunknown=[]\n', '[tasks]\nbuild="anything"\n']:
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(ValueError):
                    configuration.load(self.root)

    def test_product_program_paths_must_be_owned_test_sources(self):
        (self.root / 'runtime.py').write_text('pass\n')
        for name in ['../outside.py', '/outside.py', '.git/config', 'runtime.py', 'tests/missing.py']:
            self.write('[[tests.unit]]\nrunner="python"\npath="' + name + '"\n')
            with self.subTest(name=name), self.assertRaises(ValueError):
                configuration.load(self.root)

    def test_native_runners_cannot_be_arbitrary_shell_commands(self):
        self.write('[[tests.unit]]\nrunner="python; echo leak"\npath="tests/test_behavior.py"\n')
        with self.assertRaises(ValueError):
            configuration.load(self.root)

    def test_source_and_output_paths_reject_symlink_escapes(self):
        try:
            (self.root / 'linked').symlink_to(self.root.parent, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable')
        with self.assertRaises(ValueError):
            configuration.relative(self.root, 'linked/output.zip')

    def test_godot_pins_have_one_sdk_authority_and_exact_download_checksums(self):
        fields = {'GODOT_VERSION': '4.7.2', 'GODOT_EXPECTED_VERSION': '4.7.2.stable.official.fixture',
                  'GODOT_BINARY_CHECKSUM': 'sha256:' + 'a' * 64,
                  'GODOT_TEMPLATES_CHECKSUM': 'sha256:' + 'b' * 64}
        def save():
            (self.root / 'mise.toml').write_text('[env]\n' + '\n'.join(name + '="' + value + '"' for name, value in fields.items()))
        save()
        self.assertEqual(configuration.godot_pins(self.root), fields)
        fields['GODOT_BINARY_CHECKSUM'] = 'latest'
        save()
        with self.assertRaises(ValueError):
            configuration.godot_pins(self.root)


if __name__ == '__main__':
    unittest.main()
