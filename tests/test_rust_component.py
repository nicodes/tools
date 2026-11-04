import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
spec = importlib.util.spec_from_file_location('rust_component_controls', Path(__file__).resolve().parents[1] / 'cookbook/rust_component.py')
cargo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cargo)


class NativeCargoComponent(unittest.TestCase):
    def test_test_keeps_native_harness_compilation_distinct_from_product_build(self):
        declaration = {'manifest': 'crates/service/Cargo.toml', 'binaries': ['service'], 'release': True,
                       'test': {'binaries': [], 'lib': True, 'tests': True, 'features': ['isolated']}}
        unit = cargo.cargo_args(declaration, 'unit')
        build = cargo.cargo_args(declaration, 'build')
        self.assertEqual(unit[:2], ['cargo', 'test'])
        self.assertIn('--locked', unit)
        self.assertNotIn('--release', unit)
        self.assertNotIn('--bin', unit)
        self.assertIn('--lib', unit)
        self.assertIn('--tests', unit)
        self.assertEqual(build[:2], ['cargo', 'build'])
        self.assertIn('--release', build)

    def test_native_selectors_preserve_workspace_features_and_explicit_target(self):
        argv = cargo.cargo_args({'workspace': True, 'features': ['sdk/mock'], 'no_default_features': True},
                               'unit', target='aarch64-apple-darwin')
        self.assertIn('--workspace', argv)
        self.assertIn('--no-default-features', argv)
        self.assertEqual(argv[argv.index('--features') + 1], 'sdk/mock')
        self.assertEqual(argv[argv.index('--target') + 1], 'aarch64-apple-darwin')

    def test_shell_commands_and_flag_like_identifiers_are_not_component_data(self):
        for declaration in [{'commands': {'build': 'anything'}}, {'binaries': ['--offline']},
                            {'features': ['feature;run']}, {'manifest': '../Cargo.toml'},
                            {'test': {'command': 'echo'}}, {'release': 'false'}]:
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                cargo.cargo_args(declaration, 'build')

    def test_empty_components_cannot_succeed_as_a_native_test_group(self):
        with self.assertRaises(ValueError):
            cargo.test(Path.cwd(), [], {}, lambda *_args: None)

    def test_lint_checks_format_without_mutation_and_denies_compiler_warnings(self):
        declaration = {'workspace': True}
        self.assertIn('--check', cargo.cargo_args(declaration, 'fmt-check'))
        self.assertNotIn('--check', cargo.cargo_args(declaration, 'fmt'))
        self.assertEqual(cargo.cargo_args(declaration, 'lint')[-3:], ['--', '-D', 'warnings'])


if __name__ == '__main__':
    unittest.main()
