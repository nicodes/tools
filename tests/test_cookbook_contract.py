from pathlib import Path
import subprocess
import tempfile
import unittest

CONTRACT = Path(__file__).resolve().parents[1] / 'cookbook/common.mk'


class MakeContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.makefile = self.root / 'Makefile'
        self.hooks = {
            'setup': 'setup', 'fmt': 'fmt', 'build': 'build', 'validate': 'validate',
            'test_lint': 'lint', 'test_unit': 'unit', 'test_security': 'security',
            'test_integration': 'integration', 'test_e2e': 'e2e', 'clean': 'clean',
        }
        self.write()

    def write(self, optional='', suites='lint unit integration e2e security'):
        lines = ['COOKBOOK_TEST_SUITES := ' + suites]
        for hook, label in self.hooks.items():
            lines += ['define cookbook_' + hook, '@printf "%s\\n" ' + label + ' >> operations', 'endef']
        self.makefile.write_text('\n'.join(lines) + '\n' + optional + '\ninclude ' + str(CONTRACT) + '\n')

    def run_make(self, *targets, success=True):
        result = subprocess.run(['make', '--no-print-directory', *targets], cwd=self.root,
                                capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def operations(self):
        path = self.root / 'operations'
        return path.read_text().splitlines() if path.exists() else []

    def test_default_help_does_not_prepare_build_or_test(self):
        output = self.run_make().stdout
        self.assertIn('make build', output)
        self.assertIn('make test:integration', output)
        self.assertNotIn('make dev ', output)
        self.assertNotIn('make db:', output)
        self.assertEqual(self.operations(), [])

    def test_aggregate_runs_exact_declared_suites_without_build_or_setup(self):
        self.run_make('test')
        self.assertCountEqual(self.operations(), ['lint', 'unit', 'security', 'integration', 'e2e'])

    def test_build_always_validates_without_running_tests(self):
        self.run_make('build')
        self.assertEqual(self.operations(), ['build', 'validate'])

    def test_missing_validator_cannot_report_a_successful_build(self):
        del self.hooks['validate']
        self.write()
        result = self.run_make('build', success=False)
        self.assertIn('does not implement build output validation', result.stderr)

    def test_absent_capabilities_do_not_become_successful_empty_targets(self):
        self.write(suites='lint unit security')
        for target in ['dev', 'dev:stop', 'dev:logs', 'db:reset', 'test:e2e', 'test:integration']:
            self.run_make(target, success=False)
        self.assertEqual(self.operations(), [])

    def test_selected_test_does_not_run_other_groups(self):
        self.run_make('test:unit')
        self.assertEqual(self.operations(), ['unit'])

    def test_declared_optional_capability_is_shown_and_callable(self):
        self.write('define cookbook_db_status\n@printf "%s\\n" status >> operations\nendef\n')
        self.assertIn('make db:status', self.run_make('help').stdout)
        self.run_make('db:status')
        self.assertEqual(self.operations(), ['status'])

    def test_unknown_or_missing_required_suite_is_refused(self):
        for suites in ['lint unit security typo', 'lint unit']:
            self.write(suites=suites)
            self.run_make('test', success=False)


if __name__ == '__main__':
    unittest.main()
