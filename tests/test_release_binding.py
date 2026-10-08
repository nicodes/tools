"""Exercise release source/artifact/publisher binding through the real CLI."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / 'helpers/release.py'
STAGES = ('install', 'lint', 'unit', 'integration', 'build', 'artifact-check',
          'browser-install', 'e2e', 'vuln', 'dev', 'stop', 'clean')


class ReleaseBinding(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.release = self.root / '.artifacts/release'
        self.release.mkdir(parents=True)
        (self.release / 'images.tar.gz').write_bytes(b'fixture archive')
        (self.root / '.gitignore').write_text('.artifacts/\n')
        (self.root / '.mise.toml').write_text('[tools]\npython = "3.13.11"\n')
        declaration = {'version': 1, 'stages': {name: {'inapplicable': 'fixture'} for name in STAGES},
                       'artifacts': ['.artifacts/release'], 'build_environment': ['PRIVATE_BUILD_INPUT']}
        for stage in ('unit', 'integration'):
            declaration['stages'][stage] = [[os.sys.executable, '-c', 'pass']]
        (self.root / 'engineering.json').write_text(json.dumps(declaration))
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        self.sha = self.git('rev-parse', 'HEAD').stdout.strip()
        self.bin = self.root / '.artifacts/bin'
        self.bin.mkdir()
        docker = self.bin / 'docker'
        docker.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\nwith Path(os.environ['DOCKER_CALLS']).open('a') as stream: stream.write(' '.join(sys.argv[1:])+chr(10))\nif sys.argv[1:3] == ['image','inspect']:\n print(json.dumps([{'Id':'sha256:'+'a'*64,'RepoTags':[ref]} for ref in sys.argv[3:]]))\n")
        docker.chmod(0o755)
        self.calls = self.root / '.artifacts/docker-calls'
        self.context = {'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_REPOSITORY': 'org/demo',
                        'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
                        'PRIVATE_BUILD_INPUT': 'secret-must-never-be-recorded',
                        'GITHUB_JOB': 'Build', 'GITHUB_SHA': self.sha}
        self.produce_tests()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True, text=True, capture_output=True)

    def run_cli(self, operation, success=True, overrides=None):
        env = {key: value for key, value in os.environ.items() if not key.startswith('GITHUB_')}
        env.update(self.context)
        env.update(overrides or {})
        env.update(PATH=str(self.bin)+os.pathsep+env['PATH'], DOCKER_CALLS=str(self.calls))
        result = subprocess.run([os.sys.executable, str(HELPER), operation, '--project', 'demo',
                                 '--image-base', 'ghcr.io/org/demo', '--revision', self.sha,
                                 '--components', 'api', '--test-evidence-directory', '.artifacts/contract'], cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return result

    def produce_tests(self, success=True):
        env = {key: value for key, value in os.environ.items() if not key.startswith('GITHUB_')}
        env.update(self.context)
        env['GITHUB_JOB'] = 'Test'
        result = subprocess.run([os.sys.executable, str(HELPER.with_name('contract.py')), 'test'],
                                cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, success, result.stderr)

    def change_test_proof(self, change):
        file = self.root / '.artifacts/contract/test.json'
        data = json.loads(file.read_text())
        change(data)
        file.write_text(json.dumps(data))

    def manifest(self):
        return json.loads((self.release / 'release.json').read_text())

    def change_manifest(self, change):
        data = self.manifest()
        change(data)
        (self.release / 'release.json').write_text(json.dumps(data))

    def assert_no_publication(self):
        self.assertNotIn('load', self.calls.read_text())
        self.assertNotIn('push', self.calls.read_text())

    def test_record_binds_source_configuration_and_private_input_hash(self):
        self.run_cli('record')
        data = self.manifest()
        proof = data['provenance']
        self.assertEqual(proof['source_commit'], self.sha)
        self.assertEqual(proof['source_tree'], self.git('rev-parse', 'HEAD^{tree}').stdout.strip())
        self.assertEqual(len(proof['input_identity']), 64)
        self.assertIsNone(proof['tested_commit'])
        self.assertIn('.mise.toml', proof['configuration_sha256'])
        self.assertNotIn(self.context['PRIVATE_BUILD_INPUT'], json.dumps(data))
        self.run_cli('publish')
        receipt = json.loads((self.release / 'publication.json').read_text())
        self.assertEqual(receipt['build_provenance'], proof)
        self.assertEqual(receipt['tested_commit'], self.sha)
        self.assertEqual(receipt['test_provenance']['tested_commit'], self.sha)
        self.assertEqual(set(receipt['test_provenance']['test_receipts']), {'unit', 'integration'})
        self.assertEqual(receipt['images'], data['images'])
        self.assertEqual(receipt['publisher_context']['GITHUB_RUN_ID'], '123')

    def test_authenticated_pr_head_needs_actual_head_tests_in_the_same_context(self):
        event = self.root / '.artifacts/event.json'
        event.write_text(json.dumps({'pull_request': {'head': {'sha': self.sha},
                                                     'base': {'sha': 'b'*40},
                                                     'merge_commit_sha': 'c'*40}}))
        self.context.update(GITHUB_SHA='c'*40, GITHUB_EVENT_NAME='pull_request', GITHUB_EVENT_PATH=str(event))
        self.produce_tests()
        self.run_cli('record')
        self.run_cli('publish')
        proof = json.loads((self.release / 'publication.json').read_text())
        self.assertEqual(proof['tested_commit'], self.sha)
        self.assertEqual(proof['test_provenance']['execution_context']['GITHUB_SHA'], 'c'*40)
        self.change_test_proof(lambda proof: proof.update(source_commit='c'*40, tested_commit='c'*40))
        self.calls.write_text('')
        self.run_cli('publish', False)
        self.assert_no_publication()

    def test_pr_head_context_cannot_authorize_another_head_or_event(self):
        event = self.root / '.artifacts/event.json'
        event.write_text(json.dumps({'pull_request': {'head': {'sha': self.sha},
                                                     'base': {'sha': 'b'*40},
                                                     'merge_commit_sha': 'c'*40}}))
        self.context.update(GITHUB_SHA='c'*40, GITHUB_EVENT_NAME='pull_request', GITHUB_EVENT_PATH=str(event))
        self.produce_tests()
        self.run_cli('record')
        for change in ({'GITHUB_EVENT_NAME': 'workflow_dispatch'}, {'GITHUB_EVENT_NAME': 'pull_request_target'},
                       {'GITHUB_EVENT_PATH': ''}, {'GITHUB_SHA': 'd'*40}):
            self.run_cli('publish', False, change)
            self.assert_no_publication()
        event.write_text(json.dumps({'pull_request': {'head': {'sha': 'e'*40}}}))
        self.run_cli('publish', False)
        self.assert_no_publication()

    def test_changed_archive_stops_before_docker_load(self):
        self.run_cli('record')
        (self.release / 'images.tar.gz').write_bytes(b'tampered')
        self.run_cli('publish', False)
        self.assert_no_publication()

    def test_legacy_missing_provenance_requires_rebuild(self):
        self.run_cli('record')
        self.change_manifest(lambda data: data.pop('provenance'))
        self.run_cli('publish', False)
        self.assert_no_publication()

    def test_wrong_tree_configuration_or_input_digest_stops_publication(self):
        for key, value in [('source_tree', 'b'*40), ('configuration_sha256', {}), ('input_identity', 'invalid')]:
            self.run_cli('record')
            self.change_manifest(lambda data: data['provenance'].update({key: value}))
            self.run_cli('publish', False)
            self.assert_no_publication()

    def test_another_run_or_repository_cannot_publish(self):
        self.run_cli('record')
        for key in ['GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID']:
            self.run_cli('publish', False, {key: 'different'})
        self.assert_no_publication()

    def test_failed_publish_retry_can_use_prior_same_run_build(self):
        self.run_cli('record')
        self.run_cli('publish', overrides={'GITHUB_RUN_ATTEMPT': '2'})
        self.assertIn('push', self.calls.read_text())

    def test_future_or_invalid_build_attempt_is_rejected(self):
        self.run_cli('record')
        for attempt in ['0', 'bad']:
            self.run_cli('publish', False, {'GITHUB_RUN_ATTEMPT': attempt})
        self.change_manifest(lambda data: data['provenance']['build_context'].update(GITHUB_RUN_ATTEMPT='2'))
        self.run_cli('publish', False)
        self.assert_no_publication()

    def test_modified_publisher_source_cannot_publish(self):
        self.run_cli('record')
        (self.root / '.mise.toml').write_text('modified pin')
        self.run_cli('publish', False)
        self.assert_no_publication()

    def test_dirty_build_cannot_be_relabelled_as_the_clean_commit(self):
        pin = self.root / '.mise.toml'
        original = pin.read_text()
        pin.write_text('modified build input')
        self.run_cli('record')
        self.assertFalse(self.manifest()['provenance']['tracked_source_clean'])
        pin.write_text(original)
        self.run_cli('publish', False)
        self.assert_no_publication()

    def test_pr_head_base_and_merge_are_recorded_without_fake_test_claim(self):
        event = self.root / '.artifacts/event.json'
        event.write_text(json.dumps({'pull_request': {'head': {'sha': self.sha},
                                                     'base': {'sha': 'b'*40}, 'merge_commit_sha': 'c'*40}}))
        self.run_cli('record', overrides={'GITHUB_EVENT_PATH': str(event), 'GITHUB_SHA': 'c'*40})
        proof = self.manifest()['provenance']
        self.assertEqual(proof['source_commit'], self.sha)
        self.assertEqual(proof['source_relationship'], {'source_commit': self.sha, 'workflow_commit': 'c'*40,
                                                      'pull_request_head': self.sha, 'pull_request_base': 'b'*40,
                                                      'pull_request_merge': 'c'*40})
        self.assertIsNone(proof['tested_commit'])
        self.run_cli('publish', False, {'GITHUB_SHA': 'c'*40})
        self.assert_no_publication()

    def test_missing_tests_cannot_publish_a_successful_build(self):
        self.run_cli('record')
        (self.root / '.artifacts/contract/test.json').unlink()
        self.run_cli('publish', False)
        self.assert_no_publication()

    def test_tests_from_another_source_or_merge_cannot_certify_this_head(self):
        for key in ['source_commit', 'tested_commit', 'source_tree']:
            self.produce_tests()
            self.run_cli('record')
            self.change_test_proof(lambda proof: proof.update({key: 'b'*40}))
            self.run_cli('publish', False)
            self.assert_no_publication()

    def test_test_run_job_or_attempt_must_match_the_trusted_workflow(self):
        for key, value in [('GITHUB_RUN_ID', 'different'), ('GITHUB_REPOSITORY', 'other/repo'),
                           ('GITHUB_JOB', 'Build'), ('GITHUB_SHA', 'b'*40), ('GITHUB_RUN_ATTEMPT', '2')]:
            self.produce_tests()
            self.run_cli('record')
            self.change_test_proof(lambda proof: proof['execution_context'].update({key: value}))
            self.run_cli('publish', False)
            self.assert_no_publication()

    def test_incomplete_or_mismatched_applicable_test_receipts_are_refused(self):
        for change in [lambda proof: proof['test_receipts'].pop('integration'),
                       lambda proof: proof['test_receipts']['unit'].update(input_identity='b'*64),
                       lambda proof: proof.update(configuration_sha256={})]:
            self.produce_tests()
            self.run_cli('record')
            self.change_test_proof(change)
            self.run_cli('publish', False)
            self.assert_no_publication()

    def test_failed_test_removes_a_prior_success_receipt(self):
        file = self.root / 'engineering.json'
        declaration = json.loads(file.read_text())
        declaration['stages']['unit'] = [[os.sys.executable, '-c', 'raise SystemExit(3)']]
        file.write_text(json.dumps(declaration))
        self.git('add', 'engineering.json')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'failing fixture test')
        self.sha = self.git('rev-parse', 'HEAD').stdout.strip()
        self.context['GITHUB_SHA'] = self.sha
        self.produce_tests(False)
        self.assertFalse((self.root / '.artifacts/contract/test.json').exists())
        self.run_cli('record')
        self.run_cli('publish', False)
        self.assert_no_publication()

    def test_symlink_test_receipt_is_refused_before_load(self):
        self.run_cli('record')
        file = self.root / '.artifacts/contract/test.json'
        other = file.with_name('retained-test.json')
        file.rename(other)
        file.symlink_to(other)
        self.run_cli('publish', False)
        self.assert_no_publication()

    def commit_declaration(self, declaration):
        (self.root / 'engineering.json').write_text(json.dumps(declaration))
        self.git('add', 'engineering.json')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'changed test fixture')
        self.sha = self.git('rev-parse', 'HEAD').stdout.strip()
        self.context['GITHUB_SHA'] = self.sha

    def test_inapplicable_tests_never_produce_publication_evidence(self):
        declaration = json.loads((self.root / 'engineering.json').read_text())
        for name in ('unit', 'integration'):
            declaration['stages'][name] = {'inapplicable': 'fixture has no tests'}
        self.commit_declaration(declaration)
        self.produce_tests()
        self.assertFalse((self.root / '.artifacts/contract/test.json').exists())
        self.run_cli('record')
        self.run_cli('publish', False)
        self.assert_no_publication()

    def test_changed_test_inputs_do_not_create_a_success_receipt(self):
        declaration = json.loads((self.root / 'engineering.json').read_text())
        declaration['stages']['integration'] = [[os.sys.executable, '-c',
                                               "from pathlib import Path; Path('.mise.toml').write_text('mutated during tests')"]]
        self.commit_declaration(declaration)
        self.produce_tests()
        self.assertFalse((self.root / '.artifacts/contract/test.json').exists())
