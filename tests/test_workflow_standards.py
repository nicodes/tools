import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('standards', Path(__file__).resolve().parents[1]/'helpers/workflow-standards.py')
standards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(standards)


class WorkflowStandardsTests(unittest.TestCase):
    def test_nonimmutable_refs_literal_groups_and_missing_timeouts_fail(self):
        data = {'concurrency': {'group': 'ci-${ github.ref }'}, 'jobs': {'test': {'runs-on': 'ubuntu-24.04',
                'steps': [{'uses': 'actions/checkout@main'}]}}}
        self.assertEqual(len(standards.findings('ci.yml', data)), 3)
        data['concurrency']['group'] = 'ci-${{ github.ref }}'
        data['jobs']['test'].update({'timeout-minutes': 20, 'steps': [{'uses': 'actions/checkout@'+'a'*40}]})
        self.assertEqual(standards.findings('ci.yml', data), [])

    def test_unknown_profiles_and_missing_commands_fail(self):
        with self.assertRaises(ValueError):
            standards.validate_profile({'version': 1, 'profile': 'unknown', 'commands': {}})
        with self.assertRaises(ValueError):
            standards.validate_profile({'version': 1, 'profile': 'go-cli', 'commands': {'check': ['make', 'test']}})

    def test_explicit_unsupported_commands_are_truthful(self):
        commands = {name: {'unsupported': 'No runtime service in this source-only repository'} for name in standards.COMMANDS}
        commands['check'] = ['python3', 'scripts/test.py']
        standards.validate_profile({'version': 1, 'profile': 'configuration', 'commands': commands})

    def test_duplicate_yaml_keys_cannot_hide_a_job(self):
        with self.assertRaises(ValueError):
            standards.load_workflow('jobs:\n  test: {}\n  test: {}\n')
