import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[1] / 'cookbook'))
import rust_cli


class RustUiInputs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nrust="1.94.0"\n')
        self.config = {'recipe': 'rust-cli', 'components': {'rust': [{'manifest': 'Cargo.toml'}], 'browser': {'package': 'app'}}}

    def test_browser_setup_is_explicit_host_setup_after_locked_node_install(self):
        calls = []
        browser = SimpleNamespace(prepare=lambda *_args, **_kwargs: calls.append('browser'))
        for stage, partial, expected in [('fast', '', ['node']), ('full', '["api"]', ['cargo', 'node']),
                                          ('full', '', ['cargo', 'node', 'browser'])]:
            calls.clear()
            with patch.dict(os.environ, {'COOKBOOK_STAGE': stage, 'COOKBOOK_NATIVE_COMPONENTS': partial}), \
                    patch.object(rust_cli, 'required_tools', return_value=['python', 'rust']), \
                    patch.object(rust_cli, 'run'), patch.object(rust_cli, 'helper', return_value=browser), \
                    patch.object(rust_cli.rust_component, 'setup', side_effect=lambda *_args: calls.append('cargo')), \
                    patch.object(rust_cli.node_component, 'install', side_effect=lambda *_args: calls.append('node')):
                rust_cli.setup(self.root, self.config | {'node': {'packages': ['app']}}, {})
            self.assertEqual(calls, expected)

    def test_native_binary_input_paths_bind_only_reviewed_platform_fields(self):
        record = {'platform': 'Linux', 'arch': 'x86_64'}
        data = self.config | {'artifacts': {'binary_environment': {'API_BINARY': '.artifacts/native-api/api_{platform}_{arch}'}}}
        with patch.object(rust_cli.subprocess, 'check_output', return_value='a' * 40), \
                patch.object(rust_cli, 'package_data', return_value={}), patch.object(rust_cli, 'selected_target', return_value=record):
            values = rust_cli.environment(self.root, data)
            self.assertEqual(values['API_BINARY'], str(self.root / '.artifacts/native-api/api_Linux_x86_64'))
            self.assertEqual(values['PLAYWRIGHT_BROWSERS_PATH'], str(self.root / '.artifacts/playwright'))
            data['artifacts']['binary_environment']['API_BINARY'] = '{platform.__class__}'
            with self.assertRaises(ValueError): rust_cli.environment(self.root, data)


if __name__ == '__main__': unittest.main()
