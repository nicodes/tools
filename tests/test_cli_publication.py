import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[1] / 'cookbook'))
import cli_release as release
import cli_publication_workflows as workflows
import test_native_ci


class CliPublication(unittest.TestCase):
    def setUp(self):
        fixture = test_native_ci.NativeMatrix()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.root = fixture, fixture.root
        self.config = fixture.config
        self.config['package'].update(version_arguments=['--version'], version_template='{binary} {version} ({commit})')
        self.config.update(version=1, publication={'tag_prefix': 'cli-v', 'initial_version': '0.0.10', 'initial': 'bump'})
        self.environment = fixture.environment | {'GITHUB_SHA': fixture.source, 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_WORKFLOW_REF': 'owner/project/.github/workflows/release.yml@refs/heads/main'}
        self.plan = fixture.declaration()
        self.proofs = []
        with tempfile.TemporaryDirectory() as directory:
            for entry in self.plan['requirements']:
                target = Path(directory) / entry['id']; target.mkdir()
                value = fixture.packet(entry, target)
                value['execution_context'].update(self.environment)
                value['version_identity'] = '0.0.11'
                self.proofs.append(value)
        self.jobs = fixture.jobs(self.plan['requirements'])
        accepted = release.native_ci.validate_jobs(self.plan['requirements'], self.jobs)
        self.jobs['jobs'].append({'id': 90, 'name': 'Release / Full host validation / Make full',
            'status': 'completed', 'conclusion': 'success', 'labels': ['ubuntu-24.04']})
        self.jobs['total_count'] += 1
        self.matrix = {'version': 1, 'requirements': self.plan['requirements'], 'host_groups': self.plan['host_groups'],
            'source_commit': fixture.source, 'cookbook_commit': fixture.tools, 'proofs': self.proofs, 'jobs': accepted}
        self.proof = {'recipe': 'rust-cli', 'source_commit': fixture.source, 'source_tree': fixture.tree,
            'cookbook_commit': fixture.tools, 'configuration_sha256': self.proofs[0]['configuration_sha256'],
            'status': 'passed', 'tracked_source_clean': True, 'build_verified': True, 'tests_verified': True,
            'test_groups': sorted(self.plan['host_groups']), 'coverage': 'complete-host-and-native-matrix',
            'native_matrix': self.matrix, 'execution_context': self.environment | {'GITHUB_JOB': 'validate'}, 'artifacts': {}}
        (self.root / 'dist').mkdir()
        executable = self.root / 'compiled'; executable.write_bytes(b'exact native executable')
        native = release.rust_cli.helper('native_cli')
        for target in self.config['package']['native_targets']:
            archive = native.write_archive(self.root, executable, self.config['package'], target, self.root / 'dist')
            self.proof['artifacts']['dist/' + archive.name] = native.digest(archive)
        names = [native.archive_name(self.config['package'], target) for target in self.config['package']['native_targets']]
        (self.root / 'dist/checksums.txt').write_text(native.checksums(self.root / 'dist', names))
        self.proof['artifacts']['dist/checksums.txt'] = native.digest(self.root / 'dist/checksums.txt')

    def accept(self):
        with patch.object(release.ci, 'identity', return_value=self.fixture.tree):
            return release.accepted_payload(self.root, self.config, self.proof, self.fixture.source,
                self.fixture.tools, 'cli-v0.0.11', self.environment, self.jobs)

    def test_complete_six_platform_release_is_accepted_only_as_closed_exact_bytes(self):
        self.assertEqual(len(self.accept()), 7)
        archive = next((self.root / 'dist').glob('*.zip'))
        archive.write_bytes(b'changed after full validation')
        with self.assertRaisesRegex(ValueError, 'bytes differ'):
            self.accept()

    def test_partial_other_attempt_missing_platform_and_failed_host_cannot_publish(self):
        for mutation in ['partial', 'attempt', 'platform', 'host', 'version', 'extra']:
            original_proof, original_jobs = copy.deepcopy(self.proof), copy.deepcopy(self.jobs)
            if mutation == 'partial': self.proof['coverage'] = 'native-partial'
            elif mutation == 'attempt': self.proof['execution_context']['GITHUB_RUN_ATTEMPT'] = '99'
            elif mutation == 'platform': self.proof['native_matrix']['proofs'].pop()
            elif mutation == 'host': self.jobs['jobs'][-1]['conclusion'] = 'skipped'
            elif mutation == 'version': self.proof['native_matrix']['proofs'][0]['version_identity'] = '0.0.12'
            elif mutation == 'extra': (self.root / 'dist/extra.txt').write_text('unexpected')
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.accept()
            self.proof, self.jobs = original_proof, original_jobs
            (self.root / 'dist/extra.txt').unlink(missing_ok=True)

    def test_companion_initial_fallback_tag_guards_and_cargo_version_contract(self):
        self.assertEqual(release.select_version(self.root, self.config, 'patch', [])['tag'], 'cli-v0.0.11')
        self.assertEqual(release.select_version(self.root, self.config, 'minor', ['cli-v2.3.4', 'gd-v99.0.0'])['version'], '2.4.0')
        with self.assertRaises(ValueError): release.select_version(self.root, self.config, 'unknown', [])
        self.config['publication'] = {'initial': 'bump', 'cargo_manifest': 'Cargo.toml', 'version_identity': 'tag'}
        self.assertEqual(release.select_version(self.root, self.config, 'patch', [])['version'], 'v0.0.1')
        with self.assertRaisesRegex(ValueError, 'Cargo package version'):
            release.select_version(self.root, self.config, 'minor', [])
        for name in ['cargo_manifest', 'commands']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.policy(self.config | {'publication': {name: '../outside/Cargo.toml' if name == 'cargo_manifest' else {'publish': 'anything'}}})

    def test_source_workflow_identity_and_dispatch_controls_are_preserved(self):
        release.context(self.fixture.source, self.environment)
        for changes in [{'GITHUB_REF': 'refs/heads/feature'}, {'GITHUB_EVENT_NAME': 'pull_request'},
                        {'GITHUB_WORKFLOW_REF': 'nicodes/tools/.github/workflows/release.yml@refs/heads/main'},
                        {'GITHUB_SHA': 'f' * 40}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                release.context(self.fixture.source, self.environment | changes)

    def test_existing_remote_identity_refuses_release_without_tag_or_release_mutation(self):
        calls = []
        with patch.dict(os.environ, self.environment), patch.object(release.ci, 'identity'), \
                patch.object(release.subprocess, 'run') as native, \
                patch.object(release.subprocess, 'check_output', return_value=''), \
                patch.object(release.github_release, 'absent', side_effect=ValueError('identity already exists')):
            with self.assertRaisesRegex(ValueError, 'already exists'):
                release.identity(self.root, self.config, self.fixture.source, self.fixture.tools, 'patch')
        self.assertEqual(native.call_args.args[0], ['git', 'fetch', '--tags', '--force'])

    def test_generated_caller_keeps_attestation_in_source_and_uses_shared_native_gate(self):
        with patch.object(workflows, 'load', return_value=self.config):
            text = workflows.workflow_files(self.root, 'e' * 40)['.github/workflows/release.yml']
        self.assertIn('uses: nicodes/tools/.github/workflows/cookbook-native.yml@' + 'e' * 40, text)
        self.assertIn('uses: ' + workflows.ATTEST, text)
        self.assertNotIn('mise run', text)
        self.assertNotIn('scripts/', text)
        self.assertIn('cookbook-full-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}', text)


if __name__ == '__main__':
    unittest.main()
