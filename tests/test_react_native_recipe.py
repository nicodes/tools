import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

COOKBOOK = Path(__file__).resolve().parents[1] / 'cookbook'
sys.path.insert(0, str(COOKBOOK))
spec = importlib.util.spec_from_file_location('react_native_recipe', COOKBOOK / 'react_native.py')
recipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recipe)


class ReactNativeRecipe(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native application ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'source.txt').write_text('owned source\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Recipe Controls',
                        '-c', 'user.email=recipe@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        self.source = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()

    def test_build_defaults_are_bound_without_overwriting_workflow_identity_or_explicit_empty_values(self):
        config = {'components': {'app': {'environment': {'EXPO_PUBLIC_API_URL': 'https://api.example.invalid'}}}}
        with patch.dict(os.environ, {'EXPO_PUBLIC_API_URL': '', 'GITHUB_SHA': 'a' * 40, 'GH_TOKEN': 'test-context'}, clear=True):
            result = recipe.configure_environment(self.root, config)
            self.assertEqual(result['APP_VERSION'], self.source)
            self.assertEqual(result['EXPO_PUBLIC_API_URL'], '')
            self.assertEqual(result['GITHUB_SHA'], 'a' * 40)
            self.assertEqual(result['GH_TOKEN'], 'test-context')

    def test_preview_uses_the_declared_public_variable_and_refuses_missing_input(self):
        config = {'components': {'app': {'preview_environment': {'EXPO_PUBLIC_API_URL': '/api'},
                 'preview_variables': {'EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY': 'COOKBOOK_PREVIEW_CLERK_KEY'}}}}
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                recipe.configure_environment(self.root, config)
            os.environ['COOKBOOK_PREVIEW_CLERK_KEY'] = 'pk_test_fixture'
            result = recipe.configure_environment(self.root, config)
            self.assertEqual(result['EXPO_PUBLIC_API_URL'], '/api')
            self.assertEqual(result['EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY'], 'pk_test_fixture')

    def test_native_components_are_owned_programs_with_literal_arguments(self):
        directory = self.root / 'internal/compiler'
        directory.mkdir(parents=True)
        program = directory / 'main.py'
        program.write_text('import json,sys\nfrom pathlib import Path\nPath("result.json").write_text(json.dumps(sys.argv[1:]))\n')
        entry = {'runner': 'python', 'path': 'internal/compiler/main.py', 'args': ['literal;value']}
        recipe.native(self.root, [entry], {'APP_VERSION': self.source})
        self.assertEqual(json.loads((self.root / 'result.json').read_text()), ['literal;value'])
        (self.root / 'pipeline.py').write_text('raise SystemExit(0)\n')
        with self.assertRaisesRegex(ValueError, 'owning component'):
            recipe.native(self.root, [entry | {'path': 'pipeline.py'}], {'APP_VERSION': self.source})

    def test_native_component_metadata_cannot_create_an_empty_behavioral_success(self):
        with self.assertRaisesRegex(ValueError, 'no real implemented checks'):
            recipe.behavioral_tests(self.root, {}, {}, 'unit')

    def test_required_tools_select_only_current_lane_capabilities(self):
        (self.root / 'mise.toml').write_text('''[tools]
python="3.13.11"
node="24.18.1"
actionlint="1.7.12"
trivy="0.74.0"
mprocs="0.9.6"
''')
        self.assertEqual(recipe.required_tools(self.root, {}, 'fast'), ['actionlint', 'node', 'python'])
        self.assertIn('trivy', recipe.required_tools(self.root, {}, 'security'))
        self.assertNotIn('mprocs', recipe.required_tools(self.root, {}, 'full'))


if __name__ == '__main__':
    unittest.main()
