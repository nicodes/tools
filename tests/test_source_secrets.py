from source_fixture import write_source
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('source_secrets', Path(__file__).resolve().parents[1] / 'helpers/source-secrets.py')
secrets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(secrets)


class SourceSecrets(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)

    def track(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        subprocess.run(['git', '-C', str(self.root), 'add', name], check=True)
        return path

    def test_findings_name_source_and_category_without_credential_values(self):
        value = b'ghp_' + b'a' * 36
        self.track('source.txt', b'header\n' + value + b'\n')
        findings = secrets.scan(self.root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]['line'], 2)
        self.assertEqual(findings[0]['category'], 'github-token')
        self.assertNotIn(value.decode(), json.dumps(findings))

    def test_untracked_local_credentials_are_not_read_as_project_source(self):
        (self.root / '.env').write_text('TOKEN=' + 'ghp_' + 'a' * 36)
        self.assertEqual(secrets.scan(self.root), [])

    def test_reviewed_test_fixture_is_bound_to_exact_file_bytes(self):
        key = b'-----BEGIN PRIVATE KEY-----\n' + b'A' * 64 + b'\n-----END PRIVATE KEY-----\n'
        path = self.track('tests/fixtures/test-key.pem', key)
        policy = self.root / 'fixtures.toml'
        write_source(policy, {'version': 1, 'fixtures': [{'path': 'tests/fixtures/test-key.pem',
            'sha256': hashlib.sha256(key).hexdigest(), 'reason': 'Public deterministic key used only for isolated protocol tests.'}]})
        self.assertEqual(len(secrets.scan(self.root)), 1)
        self.assertEqual(secrets.scan(self.root, policy), [])
        path.write_bytes(key + b'changed')
        with self.assertRaisesRegex(ValueError, 'fixture changed'):
            secrets.scan(self.root, policy)

    def test_missing_approved_fixture_does_not_silently_disable_scanning(self):
        policy = self.root / 'fixtures.toml'
        write_source(policy, {'version': 1, 'fixtures': [{'path': 'missing', 'sha256': 'a' * 64,
            'reason': 'A deliberately missing reviewed fixture.'}]})
        with self.assertRaisesRegex(ValueError, 'missing'):
            secrets.scan(self.root, policy)


if __name__ == '__main__':
    unittest.main()
