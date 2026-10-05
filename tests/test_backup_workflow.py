import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from vendored import skip_module_if_vendored

skip_module_if_vendored("cicd-only: this repository's own reusable backup workflow")


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT/'.github/workflows/backup.yml'
PINNED_ACTION = re.compile(r'^[\w.-]+/[\w./-]+@[a-f0-9]{40}$')
PINNED_CICD_REVISION = re.compile(r'^[a-f0-9]{40}$')

BUN_SCRIPT = """import { readFileSync } from 'node:fs';
console.log(JSON.stringify(Bun.YAML.parse(readFileSync(process.argv[2], 'utf8'))));
"""


class ReusableBackupWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory)/'parse.ts'
            script.write_text(BUN_SCRIPT)
            try:
                completed = subprocess.run(['bun', str(script), str(WORKFLOW)], check=True,
                                            capture_output=True, text=True)
            except FileNotFoundError as error:
                raise AssertionError('bun (pinned in .mise.toml) must be on PATH to load the workflow') from error
        cls.document = json.loads(completed.stdout)

    def steps(self, job):
        return self.document['jobs'][job]['steps']

    def pinned_actions(self):
        found = []

        def inspect(value):
            if isinstance(value, dict):
                reference = value.get('uses')
                if isinstance(reference, str) and not reference.startswith('./'):
                    found.append(reference)
                for item in value.values():
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)

        inspect(self.document)
        return found

    def cicd_checkouts(self):
        checkouts = []
        for job in ('backup', 'report'):
            for step in self.steps(job):
                configuration = step.get('with') or {}
                if configuration.get('repository') == 'nicodes/tools':
                    checkouts.append(configuration)
        return checkouts

    def test_declares_exactly_the_three_call_inputs_and_three_named_secrets(self):
        trigger = self.document.get('on', self.document.get(True))
        call = trigger['workflow_call']
        inputs = call['inputs']
        self.assertEqual(set(inputs), {'product', 'server', 'user', 'allowed-products'})
        for name, specification in inputs.items():
            self.assertEqual(specification['type'], 'string', name)
            self.assertTrue(specification['required'], name)
        # Callers may map these by name (e.g. samplehost's SSH_DEPLOY_KEY rename);
        # naming anything else startup_failures the call with zero jobs
        # (live matrix 2026-09-20: example-org/deploy-actions run 35483654530).
        secrets = call['secrets']
        self.assertEqual(set(secrets),
                         {'KOMIZO_DEPLOY_KEY', 'BACKUP_S3_ACCESS_KEY', 'BACKUP_S3_SECRET_KEY'})
        for name, specification in secrets.items():
            self.assertTrue(specification['required'], name)

    def test_called_job_permissions_stay_within_the_documented_caller_floor(self):
        # An under-granted caller startup_failures the whole run with zero
        # jobs (live matrix 2026-09-20: nicodes/cicd runs 35483270260
        # under-granted vs 35483270226 with the floor), so the README caller
        # contract must publish the union of every called job's permissions.
        floor = set()
        for job in self.document['jobs'].values():
            floor.update(job.get('permissions') or {})
        self.assertEqual(floor, {'contents', 'issues'})
        readme = (ROOT/'docs/workflows.md').read_text()
        section = readme[readme.index('## Reusable backup workflow'):
                         readme.index('<!-- ws1: reusable-backup end -->')]
        snippet = section[section.index('permissions:'):section.index('jobs:')]
        self.assertIn('contents: read', snippet)
        self.assertIn('issues: write', snippet)

    def test_readme_pins_the_startup_failure_contract(self):
        readme = (ROOT/'docs/workflows.md').read_text()
        section = readme[readme.index('## Reusable backup workflow'):
                         readme.index('<!-- ws1: reusable-backup end -->')]
        flat = ' '.join(section.split())
        for phrase in ('startup_failure', 'zero jobs', 'Repo-level',
                       'cannot elevate a missing grant', 'secrets: inherit',
                       'KOMIZO_DEPLOY_KEY', 'queue: max'):
            self.assertIn(phrase, flat, phrase)

    def test_every_action_reference_is_a_full_sha_pin_with_version_comment(self):
        self.assertTrue(self.pinned_actions())
        for reference in self.pinned_actions():
            self.assertRegex(reference, PINNED_ACTION)
        for line in WORKFLOW.read_text().splitlines():
            if re.search(r'\buses:\s*\S', line):
                self.assertRegex(line, r'uses:\s*[\w.-]+/[\w./-]+@[a-f0-9]{40}\s+#\s*v\S+$', line)

    def test_backup_job_matches_the_fleet_contract_and_declares_no_concurrency(self):
        job = self.document['jobs']['backup']
        self.assertEqual(job['runs-on'], 'ubuntu-24.04')
        self.assertEqual(job['environment'], 'production')
        self.assertEqual(job['timeout-minutes'], 40)
        self.assertEqual(job['permissions'], {'contents': 'read'})
        # Concurrency is caller-owned. A called job naming the caller's
        # workflow-level `deploy-production` group queues behind the
        # still-running caller, never gets a runner, and fails with zero
        # steps, no runner and no annotation (controlled proof 2026-09-26:
        # nicodes/cicd run 35529910664 with the collision vs 35529910654
        # without caller-side concurrency).
        self.assertNotIn('concurrency', job)
        for job_body in self.document['jobs'].values():
            self.assertNotIn('concurrency', job_body)

    def test_readme_requires_the_caller_owned_concurrency_contract(self):
        readme = (ROOT/'docs/workflows.md').read_text()
        section = readme[readme.index('## Reusable backup workflow'):
                         readme.index('<!-- ws1: reusable-backup end -->')]
        flat = ' '.join(section.split())
        for phrase in ('Concurrency is caller-owned', 'deploy-production',
                       'queue: max', 'concurrency-group collision'):
            self.assertIn(phrase, flat, phrase)

    def test_connect_step_binds_the_caller_server_and_deploy_user(self):
        step = next(step for step in self.steps('backup')
                    if 'komizo-actions/connect' in step.get('uses', ''))
        self.assertEqual(step['uses'],
                         'nicodes/komizo-actions/connect@b032fc88f82233f1c060e238bc60b2888a913490')
        self.assertEqual(step['env'], {
            'KOMIZO_SERVER_URL': '${{ inputs.server }}',
            'KOMIZO_DEPLOY_KEY': '${{ secrets.KOMIZO_DEPLOY_KEY }}',
            'KOMIZO_KNOWN_HOSTS': '${{ vars.KOMIZO_KNOWN_HOSTS }}',
        })
        self.assertEqual(step['with'], {'user': '${{ inputs.user }}'})

    def test_runs_the_product_export_contract_then_the_pinned_helper(self):
        runs = [step.get('run', '') for step in self.steps('backup')]
        self.assertIn('bash scripts/export-backup.sh', runs)
        upload = next(step for step in self.steps('backup')
                      if 'upload-backup.py' in step.get('run', ''))
        self.assertEqual(upload['env'], {
            'BACKUP_ALLOWED_SLUGS': '${{ inputs.allowed-products }}',
            'BACKUP_S3_ACCESS_KEY': '${{ secrets.BACKUP_S3_ACCESS_KEY }}',
            'BACKUP_S3_SECRET_KEY': '${{ secrets.BACKUP_S3_SECRET_KEY }}',
            'BACKUP_S3_HOSTNAME': '${{ vars.BACKUP_S3_HOSTNAME }}',
            'BACKUP_S3_BUCKET': '${{ vars.BACKUP_S3_BUCKET }}',
        })
        script = upload['run']
        self.assertIn("envelope.get('verified_at')", script)
        self.assertIn('.cicd/helpers/upload-backup.py --slug "${{ inputs.product }}"', script)
        self.assertIn('--taken-at "$taken_at"', script)

    def test_both_jobs_check_out_the_same_pinned_cicd_helper_revision(self):
        checkouts = self.cicd_checkouts()
        self.assertEqual(len(checkouts), 2)
        revisions = set()
        for configuration in checkouts:
            self.assertRegex(configuration['ref'], PINNED_CICD_REVISION)
            self.assertEqual(configuration['path'], '.cicd')
            self.assertEqual(configuration['sparse-checkout'], 'helpers')
            self.assertIs(configuration['persist-credentials'], False)
            revisions.add(configuration['ref'])
        self.assertEqual(len(revisions), 1)

    def test_artifact_step_is_erroring_with_thirty_day_retention(self):
        artifact = next(step for step in self.steps('backup')
                        if 'upload-artifact' in step.get('uses', ''))
        self.assertEqual(artifact['with']['name'],
                         'backup-${{ github.run_id }}-${{ github.run_attempt }}')
        self.assertEqual(artifact['with']['path'], '.artifacts/backup/export/')
        self.assertEqual(artifact['with']['if-no-files-found'], 'error')
        self.assertEqual(artifact['with']['retention-days'], 30)

    def test_report_job_runs_the_pinned_failure_reporter(self):
        job = self.document['jobs']['report']
        self.assertEqual(job['needs'], ['backup'])
        self.assertEqual(job['if'], 'failure()')
        self.assertEqual(job['permissions'], {'contents': 'read', 'issues': 'write'})
        reporter = next(step for step in job['steps']
                        if 'report-failure.py' in step.get('run', ''))
        self.assertNotIn('scripts/engineering', reporter['run'])
        self.assertEqual(reporter['env'], {'GH_TOKEN': '${{ github.token }}'})


if __name__ == '__main__':
    unittest.main()
