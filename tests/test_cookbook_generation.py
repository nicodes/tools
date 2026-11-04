import importlib.util
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest

COOKBOOK = Path(__file__).resolve().parents[1] / 'cookbook'
sys.path.insert(0, str(COOKBOOK))
spec = importlib.util.spec_from_file_location('cookbook_generate', COOKBOOK / 'generate.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class CookbookGeneration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cookbook checkout ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/check.py').write_text('assert True\n')
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="godot-addon"\n[[tests.unit]]\nrunner="python"\npath="tests/check.py"\n')

    def test_help_works_offline_before_sdk_or_cookbook_installation(self):
        (self.root / 'Makefile').write_text(generator.makefile(self.root, 'a' * 40))
        result = subprocess.run(['make', '--no-print-directory'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('make build', result.stdout)
        self.assertIn('make test:unit', result.stdout)
        self.assertNotIn('make test:e2e', result.stdout)
        self.assertFalse((self.root / '.artifacts').exists())

    def test_missing_setup_does_not_silently_fetch_or_build_from_test(self):
        (self.root / 'Makefile').write_text(generator.makefile(self.root, 'a' * 40))
        result = subprocess.run(['make', '--no-print-directory', 'test'], cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / '.artifacts').exists())

    def test_installed_recipe_works_in_a_checkout_with_spaces(self):
        (self.root / 'Makefile').write_text(generator.makefile(self.root, 'a' * 40))
        recipe = self.root / '.artifacts/cookbook' / ('a' * 40) / 'cookbook/recipes/godot-addon.mk'
        recipe.parent.mkdir(parents=True)
        recipe.write_text('build:\n\t@echo validated-package\n')
        result = subprocess.run(['make', '--no-print-directory', 'build'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'validated-package')

    def test_explicit_tools_checkout_can_also_contain_spaces(self):
        (self.root / 'Makefile').write_text(generator.makefile(self.root, 'a' * 40))
        tools = self.root / 'shared cookbook'
        recipe = tools / 'cookbook/recipes/godot-addon.mk'
        recipe.parent.mkdir(parents=True)
        recipe.write_text('build:\n\t@echo validated-package\n')
        result = subprocess.run(['make', '--no-print-directory', 'build', 'COOKBOOK_ROOT=' + str(tools)],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_floating_or_command_like_source_refs_are_refused(self):
        for revision in ['main', 'latest', 'a' * 39, 'a' * 40 + '; echo secret']:
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                generator.makefile(self.root, revision)

    def test_recipe_data_changes_update_generated_entrypoint_identity(self):
        before = generator.makefile(self.root, 'a' * 40)
        path = self.root / 'cookbook.toml'
        path.write_text(path.read_text() + '\n[clean]\npaths=["dist"]\n')
        self.assertNotEqual(before, generator.makefile(self.root, 'a' * 40))

    def test_generated_workflow_callers_are_valid_without_product_commands(self):
        executable = shutil.which('actionlint')
        self.assertIsNotNone(executable, 'the caller test requires the pinned actionlint SDK')
        path = self.root / 'cookbook.toml'
        path.write_text(path.read_text() + '\n[publication]\ntag_prefix="gd-v"\n')
        files = generator.workflow_files(self.root, 'a' * 40)
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            self.assertNotIn('run:', content)
        result = subprocess.run([executable, *files], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(files), {'.github/workflows/ci.yml', '.github/workflows/request-merge.yml',
                                      '.github/workflows/release.yml'})


if __name__ == '__main__':
    unittest.main()
