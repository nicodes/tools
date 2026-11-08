"""Explicit setup context preserves existing homes and native installer argv."""
import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('explicit_rust_context', Path(__file__).parents[1] / 'helpers/rust_install_context.py')
context = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(context)


class ExplicitRustSetupContext(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / 'mise.toml').write_text('[tools]\nrust="1.94.0"\npython="3.13.11"\n"cargo:cargo-audit"="0.22.2"\n')
        self.home_key = 'USERPROFILE' if os.name == 'nt' else 'HOME'

    def test_only_absent_Cargo_home_is_added_from_the_provided_native_home(self):
        home = self.root / 'provided-user-home'
        original = {self.home_key: str(home), 'RUSTUP_HOME': '/unchanged-rustup', 'NATIVE_FLAG': '0'}
        result = context.environment(original)
        self.assertEqual(result, {**original, 'CARGO_HOME': str(home / '.cargo')})
        self.assertNotIn('CARGO_HOME', original)
        self.assertFalse(home.exists())

    def test_all_existing_home_values_and_settings_aliases_are_byte_identical(self):
        for cargo in ['', '/existing-cargo', 'caller-relative-cargo']:
            original = {'CARGO_HOME': cargo, 'RUSTUP_HOME': '/existing-rustup',
                        'MISE_CARGO_HOME': '/configured-cargo', 'MISE_RUSTUP_HOME': '/configured-rustup',
                        self.home_key: str(self.root), 'GITHUB_RUN_ID': '41'}
            result = context.environment(original)
            self.assertEqual(result, original)
            self.assertIsNot(result, original)

    def test_missing_native_home_uses_existing_account_home_without_mutation(self):
        with patch.object(context.Path, 'home', return_value=self.root):
            result = context.environment({'NATIVE_FLAG': 'false'})
        self.assertEqual(result, {'NATIVE_FLAG': 'false', 'CARGO_HOME': str(self.root / '.cargo')})
        self.assertFalse((self.root / '.cargo').exists())

    def test_relative_native_home_refuses_before_creating_any_directory(self):
        with self.assertRaisesRegex(ValueError, 'absolute caller home'):
            context.environment({self.home_key: 'relative-home'})

    def test_unrelated_selected_tools_do_not_request_a_Rust_context(self):
        self.assertFalse(context.needs_rust(self.root, 'python'))
        self.assertTrue(context.needs_rust(self.root, 'rust'))
        self.assertTrue(context.needs_rust(self.root, 'cargo:cargo-audit'))
        self.assertTrue(context.needs_rust(self.root, ''))
        self.assertFalse(context.needs_rust(self.root / 'no-source', ''))
        with self.assertRaises(ValueError):
            context.needs_rust(self.root, 'rust@other')

    def test_explicit_installer_retains_argv_cwd_environment_and_exit_status(self):
        original = {self.home_key: str(self.root / 'user'), 'RUSTUP_HOME': '/same-rustup', 'NATIVE_FLAG': '0'}
        argv = ['mise', 'install', 'rust@1.94.0']
        observed = []
        def run(command, *, cwd, env):
            observed.append(command)
            self.assertEqual(command, argv)
            self.assertEqual(cwd, self.root)
            self.assertEqual(env, {**original, 'CARGO_HOME': str(self.root / 'user/.cargo')})
            return SimpleNamespace(returncode=23)
        with patch.dict(os.environ, original, clear=True), patch.object(context.sys, 'argv',
                ['rust_install_context.py', '--directory', str(self.root), '--', *argv]), \
                patch.object(context.subprocess, 'run', run), self.assertRaises(SystemExit) as error:
            context.main()
        self.assertEqual(error.exception.code, 23)
        self.assertEqual(observed, [argv])

    def test_GitHub_context_emission_is_setup_only_and_skips_unrelated_tools(self):
        output = self.root / 'environment'
        original = {self.home_key: str(self.root / 'user'), 'GITHUB_ENV': str(output)}
        with patch.dict(os.environ, original, clear=True), patch.object(context.sys, 'argv',
                ['rust_install_context.py', '--directory', str(self.root), '--tools', 'python', '--github-env']):
            context.main()
        self.assertFalse(output.exists())
        with patch.dict(os.environ, original, clear=True), patch.object(context.sys, 'argv',
                ['rust_install_context.py', '--directory', str(self.root), '--tools', 'rust', '--github-env']):
            context.main()
        self.assertEqual(output.read_text(), 'CARGO_HOME=' + str(self.root / 'user/.cargo') + '\n')
        self.assertFalse((self.root / 'user').exists())

    def test_existing_Cargo_home_is_not_republished_or_repaired(self):
        output = self.root / 'environment'
        original = {'CARGO_HOME': str(self.root / 'missing-user-home'), 'GITHUB_ENV': str(output)}
        with patch.dict(os.environ, original, clear=True), patch.object(context.sys, 'argv',
                ['rust_install_context.py', '--directory', str(self.root), '--github-env']):
            context.main()
        self.assertFalse(output.exists())
        self.assertFalse(Path(original['CARGO_HOME']).exists())

    def test_setup_wrapper_refuses_any_other_native_operation(self):
        with patch.object(context.sys, 'argv', ['rust_install_context.py', '--directory', str(self.root), '--', 'mise', 'exec']), \
                patch.object(context.subprocess, 'run', side_effect=AssertionError('must not execute')), \
                self.assertRaisesRegex(ValueError, 'explicit mise install'):
            context.main()


if __name__ == '__main__':
    unittest.main()
