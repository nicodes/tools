import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
import delivery_workflows as delivery
import generate


class DeliveryWorkflows(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = {'delivery': {'definition': 'delivery.json'}}
        self.data = dict(version=1, project='demo', komizo_ref='a' * 40,
                         publish_runner='ubuntu-24.04', publish_timeout=35,
                         deploy_runner='ubuntu-24.04', deploy_timeout=100,
                         health_urls=['https://example.test/readyz'], notification_url='https://example.test/',
                         secrets={'KOMIZO_SECRET_DATABASE_URL': 'RUNTIME_DATABASE_URL'},
                         variables={'KOMIZO_SECRET_PUBLIC_KEY': 'PUBLIC_KEY'})

    def write(self):
        (self.root / 'delivery.json').write_text(json.dumps(self.data))

    def test_same_run_publication_and_serialized_host_after_validation(self):
        self.write()
        result = delivery.jobs(self.root, self.config, 'b' * 40)
        self.assertIn('needs: validate', result)
        self.assertIn('needs: publish', result)
        self.assertIn('group: deploy-production', result)
        self.assertIn('cancel-in-progress: false', result)
        self.assertIn('inputs.source == github.sha', result)
        self.assertIn('KOMIZO_SECRET_DATABASE_URL: ${{ secrets.RUNTIME_DATABASE_URL }}', result)
        self.assertIn('KOMIZO_SECRET_PUBLIC_KEY: ${{ vars.PUBLIC_KEY }}', result)
        self.assertNotIn('secrets: inherit', result)
        self.assertNotIn('mise run', result)
        self.assertNotIn('make build', result)

    def test_rejects_unpinned_or_interpolated_authority(self):
        for key, value in [('komizo_ref', 'main'), ('deploy_runner', '${{ vars.RUNNER }}'),
                           ('deploy_timeout', 181), ('health_urls', ['http://example.test/']),
                           ('secrets', {'KOMIZO_SECRET_DB': '${{ secrets.DB }}'}),
                           ('variables', {'DATABASE_URL': 'DB'})]:
            original = self.data[key]
            self.data[key] = value
            self.write()
            with self.subTest(key=key), self.assertRaises(ValueError):
                delivery.declaration(self.root, self.config)
            self.data[key] = original

    def test_backup_credentials_only_enter_explicit_delivery(self):
        self.write()
        self.assertNotIn('BACKUP_S3_ACCESS_KEY', delivery.jobs(self.root, self.config, 'b' * 40))
        self.data['upload_backup'] = True
        self.write()
        result = delivery.jobs(self.root, self.config, 'b' * 40)
        self.assertIn('BACKUP_S3_ACCESS_KEY: ${{ secrets.BACKUP_S3_ACCESS_KEY }}', result)
        before_deploy = result.split('  deploy:')[0]
        self.assertNotIn('secrets.', before_deploy)

    def test_production_retains_host_cd_identity_without_duplicate_main_builds(self):
        self.write()
        (self.root / 'tests').mkdir()
        (self.root / 'tests/unit.py').write_text('assert 1 + 1 == 2\n')
        (self.root / 'cookbook.toml').write_text('version=1\nrecipe="react-native"\n[delivery]\ndefinition="delivery.json"\n[[tests.unit]]\nrunner="python"\npath="tests/unit.py"\n')
        files = generate.workflow_files(self.root, 'b' * 40)
        self.assertNotIn('  push:', files['.github/workflows/ci.yml'])
        self.assertNotIn('  pull_request:', files['.github/workflows/cd.yml'])
        self.assertIn('  push:', files['.github/workflows/cd.yml'])
        self.assertIn('merge-gate: false', files['.github/workflows/cd.yml'])
        self.assertNotIn('application-delivery', files['.github/workflows/ci.yml'])
        self.assertIn('application-delivery', files['.github/workflows/cd.yml'])
        self.assertIn('group: deploy-source-${{ github.sha }}-', files['.github/workflows/cd.yml'])
        self.assertIn('group: ci-${{ github.ref }}-', files['.github/workflows/ci.yml'])

    def test_combined_delivery_keeps_one_existing_job_deadline(self):
        self.data.update(combined=True, publish_timeout=15, deploy_timeout=15)
        self.write()
        result = delivery.jobs(self.root, self.config, 'b' * 40)
        self.assertNotIn('  publish:\n', result)
        self.assertIn('needs: validate', result)
        self.assertIn('timeout-minutes: 15', result)
        self.assertIn('application-publication@', result)
        self.assertIn('application-delivery@', result)
        self.assertLess(result.index('application-publication@'), result.index('application-delivery@'))
        self.data['publish_timeout'] = 35
        self.write()
        with self.assertRaises(ValueError):
            delivery.declaration(self.root, self.config)

    def test_source_bound_release_health_assertion_is_preserved(self):
        self.data['health_urls'].append(dict(url='https://example.test/release.json', contains='{revision}'))
        self.write()
        self.assertEqual(delivery.declaration(self.root, self.config)['health_urls'][1]['contains'], '{revision}')

    def test_native_environment_deadlines_and_absent_notification_are_preserved(self):
        self.data.update(environment='Production', notify=False,
            step_timeouts={'deploy':25,'readiness':5,'diagnostics':3},
            report={'runner':'ubuntu-24.04','timeout_minutes':5,
                    'step_timeouts':{'checkout':3,'setup':5,'report':3}})
        self.write()
        text=delivery.jobs(self.root,self.config,'b'*40)
        self.assertIn('    environment: Production',text)
        self.assertIn('prepare-only: true',text)
        self.assertIn('        timeout-minutes: 25',text)
        self.assertIn('notify: false',text)
        self.assertNotIn('DISCORD_WEBHOOK_URL',text)
        self.assertIn('report-runner: ubuntu-24.04',text)
        for changes in [{'deploy':101},{'command':5},{'readiness':True}]:
            self.data['step_timeouts']=changes
            self.write()
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                delivery.declaration(self.root,self.config)

    def test_readiness_and_operational_readiness_cannot_select_two_policies(self):
        self.data['readiness_operation']=True
        self.write()
        with self.assertRaisesRegex(ValueError,'one native production policy'):
            delivery.declaration(self.root,self.config)
        self.config['operations']={'definition':'deploy/operations.json'}
        self.assertTrue(delivery.declaration(self.root,self.config)['readiness_operation'])

    def test_scoped_profile_keeps_generation_and_never_transports_profile_values(self):
        self.data.update(service_env_profile='serving-postgres-v2',expected_generation='c'*32,secrets={},variables={})
        self.write()
        text=delivery.jobs(self.root,self.config,'b'*40)
        self.assertIn('service-env-profile: serving-postgres-v2',text)
        self.assertIn('expected-generation: '+'c'*32,text)
        self.assertNotIn('KOMIZO_SECRET_',text)
        import copy
        original=copy.deepcopy(self.data)
        for changes in [dict(expected_generation='wrong'),dict(service_env_profile='../other'),
                        dict(secrets={'KOMIZO_SECRET_DB':'DB'}),dict(optional_secrets={'KOMIZO_SECRET_TOKEN':'TOKEN'}),
                        dict(service_env_profile='')]:
            self.data=original|changes;self.write()
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                delivery.declaration(self.root,self.config)


if __name__ == '__main__':
    unittest.main()
