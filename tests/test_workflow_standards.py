import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('standards', Path(__file__).resolve().parents[1]/'helpers/workflow-standards.py')
standards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(standards)


class WorkflowStandardsTests(unittest.TestCase):
    def test_long_cold_jobs_need_a_reviewed_caller_ceiling(self):
        data = {'jobs': {'build': {'runs-on': 'ubuntu-24.04', 'timeout-minutes': 180}}}
        self.assertEqual(len(standards.findings('cd.yml', data)), 1)
        self.assertEqual(standards.findings('cd.yml', data, max_job_minutes=180), [])
        data['jobs']['build']['timeout-minutes'] = 181
        self.assertEqual(len(standards.findings('cd.yml', data, max_job_minutes=180)), 1)

    def test_caller_job_ceiling_cannot_remove_bounding(self):
        commands = {name: ['make', name] for name in standards.COMMANDS}
        profile = {'version': 1, 'profile': 'expo-application', 'commands': commands}
        for maximum in [True, 0, 361, '180', None]:
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                standards.validate_profile({**profile, 'max_job_minutes': maximum})
        standards.validate_profile({**profile, 'max_job_minutes': 180})

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

    def test_composite_actions_and_container_digests_are_checked(self):
        data = {'runs': {'using': 'composite', 'steps': [
            {'uses': 'actions/cache@v4'}, {'uses': 'docker://alpine:latest'}]}}
        self.assertEqual(len(standards.findings('action.yml', data)), 2)
        data['runs']['steps'] = [{'uses': 'docker://alpine@sha256:'+'a'*64},
                                 {'uses': './.github/actions/setup'}]
        self.assertEqual(standards.findings('action.yml', data), [])
