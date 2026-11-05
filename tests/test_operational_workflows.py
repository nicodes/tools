import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
import operational_workflows as operations


class OperationalWorkflows(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ['deploy/health.py', 'deploy/security.py', 'deploy/backup.json',
                     'deploy/deployed-scan.json', 'deploy/restore.json', 'api/Cargo.toml']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}\n' if path.suffix == '.json' else '# Native fixture\n')
        self.config = {'recipe': 'react-native', 'delivery': {'definition': 'deploy/delivery.json'},
                       'operations': {'definition': 'deploy/operations.json'}}
        delivery = dict(version=1, project='demo', komizo_ref='a' * 40,
                        publish_runner='ubuntu-24.04', publish_timeout=35,
                        deploy_runner='ubuntu-24.04', deploy_timeout=100,
                        health_urls=['https://example.test/readyz'], notification_url='https://example.test/',
                        secrets={}, variables={})
        (self.root / 'deploy/delivery.json').write_text(json.dumps(delivery))
        self.data = dict(version=1, project='demo',
            backup=dict(runner='ubuntu-24.04', timeout_minutes=40, environment='production',
                        schedule=['8 0 * * *'], config='deploy/backup.json',
                        concurrency='deploy-production', queue='max', gate_variable='BACKUP_ENABLED'),
            recovery=dict(runner='ubuntu-24.04', timeout_minutes=45, environment='production'),
            security=dict(runner='ubuntu-24.04', timeout_minutes=110, report_timeout=5,
                          deployed_policy='deploy/deployed-scan.json', evidence=['.artifacts/security/']),
            production=dict(runner='ubuntu-24.04', timeout_minutes=45, environment='Production',
                            checks=[dict(runner='python', path='deploy/health.py', timeout=180)]),
            paid=[dict(id='model_smoke', description='Run the existing live model assertion',
                       runner='ubuntu-24.04', timeout_minutes=20, environment='Production',
                       deployment_required=True, skip_default=True,
                       check=dict(runner='cargo-run', path='api/Cargo.toml', binary='model-smoke',
                                  args=['--live'], timeout=1200))],
            restore_profiles=[dict(name='default', project='demo', config='deploy/restore.json')])

    def write(self):
        (self.root / 'deploy/operations.json').write_text(json.dumps(self.data))

    def declaration(self):
        self.write()
        return operations.declaration(self.root, self.config)

    def test_native_budgets_schedules_environments_and_transport_remain_explicit(self):
        self.declaration()
        files = operations.workflow_files(self.root, 'b' * 40, self.config)
        backup = files['.github/workflows/backup.yml']
        self.assertIn('cron: "8 0 * * *"', backup)
        self.assertIn('timeout-minutes: 40', backup)
        self.assertIn('queue: max', backup)
        self.assertIn("vars.BACKUP_ENABLED == 'true'", backup)
        recovery = files['.github/workflows/recovery.yml']
        self.assertIn('source-run-id:', recovery)
        self.assertIn('artifact-id:', recovery)
        self.assertNotIn('restore_profiles', recovery)
        self.assertNotIn('pg_restore', recovery)
        self.assertNotIn('KOMIZO_DEPLOY_KEY', recovery)
        self.assertIn('report-timeout: 5', files['.github/workflows/security-scan.yml'])

    def test_paid_provider_key_only_enters_the_explicit_default_off_job(self):
        self.write()
        text = operations.workflow_files(self.root, 'b' * 40, self.config)['.github/workflows/production-verification.yml']
        self.assertIn('default: false', text)
        ordinary, paid = text.split('\n  model_smoke:', 1)
        self.assertIn('!inputs.model_smoke', ordinary)
        self.assertNotIn('secrets.OPENROUTER_API_KEY', ordinary)
        self.assertIn('&& inputs.model_smoke', paid)
        self.assertIn('timeout-minutes: 20', paid)
        self.assertIn('environment: Production', paid)
        self.assertIn('secrets.OPENROUTER_API_KEY', paid)

    def test_environment_free_production_keeps_the_public_native_callee(self):
        self.data['production'].pop('environment')
        self.data.pop('paid')
        self.write()
        text = operations.workflow_files(self.root, 'b' * 40, self.config)['.github/workflows/production-verification.yml']
        self.assertIn('cookbook-public-production-check.yml@' + 'a' * 40, text)
        self.assertNotIn('environment:', text)

    def test_authority_environment_and_unused_native_selectors_are_rejected(self):
        original = copy.deepcopy(self.data)
        mutations = [
            lambda d: d.update(project='other'),
            lambda d: d.update(version=True),
            lambda d: d['backup'].update(checks=[dict(runner='python', path='deploy/health.py')]),
            lambda d: d['security'].update(timeout_minutes=111),
            lambda d: d['security'].update(report_timeout=12),
            lambda d: d['production'].update(runner='${{ vars.RUNNER }}'),
            lambda d: d['production'].update(concurrency='${{ github.ref }}'),
            lambda d: d['production'].update(queue='max'),
            lambda d: d['production']['checks'][0].update(binary='unused'),
            lambda d: d['production']['checks'][0].update(path='scripts/generic-wrapper.py'),
            lambda d: d['production']['checks'][0].update(path='deploy/../secret.py'),
            lambda d: d['recovery'].update(schedule=['0 0 * * *']),
        ]
        for mutation in mutations:
            self.data = copy.deepcopy(original)
            mutation(self.data)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.declaration()
        for key in ['TOOLS_ROOT', 'EXPECTED_REVISION', 'OPENROUTER_API_KEY', 'PATH', 'PYTHONPATH', 'GITHUB_SHA']:
            self.data = copy.deepcopy(original)
            self.data['production']['checks'][0]['environment'] = {key: 'replacement'}
            with self.subTest(environment=key), self.assertRaises(ValueError):
                self.declaration()

    def test_prepare_lists_profiles_and_native_timeouts_cannot_hide_missing_work(self):
        original = copy.deepcopy(self.data)
        mutations = [
            lambda d: d['production'].update(checks=[]),
            lambda d: d['production'].update(checks='program'),
            lambda d: d['production'].update(prepare=dict(native='program')),
            lambda d: d['production'].update(prepare=dict(packages='api')),
            lambda d: d['production'].update(prepare=dict(browser='api')),
            lambda d: d['production'].update(evidence='.artifacts/security'),
            lambda d: d['production']['checks'][0].update(timeout=2701),
            lambda d: d['paid'][0]['check'].update(timeout=1201),
            lambda d: d['restore_profiles'].append(copy.deepcopy(d['restore_profiles'][0])),
            lambda d: d['paid'].append(copy.deepcopy(d['paid'][0])),
            lambda d: d['paid'][0].update(id='production'),
            lambda d: d.pop('production'),
        ]
        for mutation in mutations:
            self.data = copy.deepcopy(original)
            mutation(self.data)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.declaration()

    def test_symlinked_programs_are_never_native_source(self):
        path = self.root / 'deploy/health.py'
        path.unlink()
        path.symlink_to(self.root / 'deploy/security.py')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.declaration()


if __name__ == '__main__':
    unittest.main()
