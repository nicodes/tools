import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
import delivery_workflows as delivery


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


if __name__ == '__main__':
    unittest.main()
