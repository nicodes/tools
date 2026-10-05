"""Structural contract of the reusable deployed-drift workflow.

Ported from nicodes/samplehost-be's in-repo deployed.yml under its handoff
contract (nicodes/samplehost-be docs/deployed-drift-reusable-handoff.md, added at
samplehost-be@bc2a1a5f). The contract's preserve-exactly clauses -- server-side
status=success filtering, fail-closed reads, the missing-commits list on
failure -- are pinned here, together with the caller contract (triggers stay
caller-side, permission floor, no secrets block).
"""
import json
import os
from pathlib import Path
import subprocess
import unittest

from vendored import skip_module_if_vendored

skip_module_if_vendored("cicd-only: this repository's own reusable deployed-scan workflow")

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT/'.github'/'workflows'/'deployed.yml'


def load():
    script = ('console.log(JSON.stringify(Bun.YAML.parse('
              'require("fs").readFileSync(process.env.WORKFLOW_DOC, "utf8"))))')
    document = subprocess.check_output(['bun', '-e', script], text=True,
                                       env={**os.environ, 'WORKFLOW_DOC': str(WORKFLOW)})
    return json.loads(document)


class DeployedReusableWorkflow(unittest.TestCase):
    def setUp(self):
        self.document = load()
        self.job = self.document['jobs']['deployed']
        self.steps = self.job['steps']
        self.run = self.steps[0]['run']

    def test_the_only_trigger_is_workflow_call(self):
        self.assertEqual(list(self.document['on']), ['workflow_call'],
                         'cron minute and workflow_dispatch belong to the thin caller file')

    def test_deploy_workflow_is_the_only_input_and_is_required(self):
        inputs = self.document['on']['workflow_call']['inputs']
        self.assertEqual(list(inputs), ['deploy-workflow'])
        specification = inputs['deploy-workflow']
        self.assertEqual(specification['type'], 'string')
        self.assertIs(specification['required'], True)
        self.assertNotIn('default', specification,
                         'the one product-specific value must never be guessed')

    def test_single_job_keeps_the_fleet_runner_and_the_documented_floor(self):
        self.assertEqual(list(self.document['jobs']), ['deployed'])
        self.assertEqual(self.job['runs-on'], 'ubuntu-24.04')
        self.assertEqual(self.job['timeout-minutes'], 5)
        # The handoff contract names only actions: read; contents: read is
        # required too because the main's-HEAD and compare reads are contents
        # endpoints that 404 without it on a private repository (samplehost-be,
        # the first adopter, is private, and its in-repo workflow declared
        # both). The README caller contract must publish the same union.
        self.assertEqual(self.job['permissions'], {'actions': 'read', 'contents': 'read'})

    def test_pure_gh_api_glue_has_no_actions_to_pin(self):
        self.assertEqual(len(self.steps), 1)
        for step in self.steps:
            self.assertNotIn('uses', step,
                             'two API calls and no runner work worth measuring: no checkout, nothing to pin')
        self.assertNotIn('concurrency', self.document,
                         'one hourly read-only run per caller; nothing to cancel')

    def test_the_step_preserves_the_source_name_and_the_implicit_token(self):
        step = self.steps[0]
        self.assertEqual(step['name'], "The newest successful deploy is main's HEAD")
        self.assertEqual(step['env'], {'GH_TOKEN': '${{ github.token }}'})

    def test_server_side_success_filtering_is_preserved_exactly(self):
        self.assertIn('runs?status=success&per_page=1', self.run,
                      'client-side filtering would let a queue of failures hide the last good deploy')

    def test_reads_fail_closed_on_unreadable_answers(self):
        self.assertIn('[ -z "$head" ]', self.run)
        self.assertIn("could not read main's HEAD", self.run)
        self.assertIn('[ "$deployed" = "null" ]', self.run)
        self.assertIn('could not read the newest successful CD run', self.run)

    def test_the_missing_commits_list_is_the_failure_output(self):
        self.assertIn('compare/$deployed...$head', self.run)
        self.assertIn('Commits on main that have not been deployed:', self.run)
        self.assertIn('a GITHUB_TOKEN push does not trigger CD', self.run)
        self.assertIn('gh workflow run ${{ inputs.deploy-workflow }} --ref main', self.run)

    def test_caller_context_and_the_single_parameter(self):
        self.assertIn('repos/${{ github.repository }}/commits/main', self.run)
        self.assertIn('actions/workflows/${{ inputs.deploy-workflow }}/runs', self.run)
        self.assertNotIn('cd.yml', self.run,
                         'the deploy workflow file name is the deploy-workflow input, not a literal')
        self.assertNotIn('samplehost', self.run,
                         'failure output stays fleet-generic; provenance lives in the header comments')

    def test_readme_documents_the_thin_caller_contract(self):
        readme = (ROOT/'README.md').read_text()
        section = readme[readme.index('<!-- ws13/deployed-reusable: begin -->'):
                         readme.index('<!-- ws13/deployed-reusable: end -->')]
        flat = ' '.join(section.split())
        for fragment in ['uses: nicodes/tools/.github/workflows/deployed.yml@',
                         'deploy-workflow: cd.yml',
                         'workflow_dispatch',
                         'cron',
                         'actions: read',
                         'contents: read',
                         'zero jobs',
                         'caller']:
            self.assertIn(fragment, flat, fragment)


if __name__ == '__main__':
    unittest.main()
