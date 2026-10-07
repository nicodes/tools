"""Structural contract for the shared reusable vulnerability-scan and tool-watch workflows.

The repository parses YAML with Bun (helpers/pins.mjs), so these tests load the
workflow documents through `bun -e` and assert on the resulting JSON. They pin
the contract the product callers depend on: trigger shape, inputs, permissions,
pinned actions, artifact retention and concurrency semantics.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

from vendored import skip_module_if_vendored

skip_module_if_vendored("cicd-only: this repository's own workflows and their self-checkout pins")

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT/'.github'/'workflows'

# These tests are about THIS repository, and helpers/ and tests/ are vendored
# wholesale into every product. There, ROOT is the product's
# scripts/engineering directory: scripts/release.sh does not exist, and the
# workflows on disk are the product's own, which have no cicd self-checkouts
# to agree about. Running them in a product asserts things that are not its
# business and fails for reasons it cannot fix.
#
# SOURCE.json beside this tests/ directory is the signal: it is written by
# vendor-snapshot.py and exists only in a consumer, never here.
VENDORED = (ROOT / 'SOURCE.json').exists()
# The helper checkouts are pinned, and every one of them names the SAME
# commit -- but which commit is decided when a release is cut, not here.
#
# This used to be the literal SHA, which made every release fail its own
# tests: scripts/release.sh repins the workflows, and the assertion said the
# pin must still be the one from before. That is the bump-along coupling in
# test form. The property worth holding is that the pins are full SHAs and
# that they agree with each other; the value is the release's to choose.
HELPER_REF = re.compile(r'^[0-9a-f]{40}$')


def helper_refs():
    """Every nicodes/cicd self-checkout ref across the reusable workflows.

    Parsed with this file's own load(), so the workflows are read exactly as
    the rest of these tests read them rather than by a second parser that
    could disagree about the same document.
    """
    found = {}
    for path in sorted(WORKFLOWS.glob('*.yml')):
        document = load(path.name) or {}
        for job in (document.get('jobs') or {}).values():
            for step in (job.get('steps') or []):
                with_ = step.get('with') or {}
                if with_.get('repository') == 'nicodes/tools' and 'ref' in with_:
                    found.setdefault(str(with_['ref']), []).append(path.name)
    return found
USE_KEY = re.compile(r'(?:^|[-\s])uses:\s*(?P<value>.+?)\s*$')
SHA_PIN = re.compile(r'^[\w.-]+(?:/[\w.-]+)+@[0-9a-f]{40}\s+#\s*v?\d[\w.+-]*$')


def load(name):
    script = ('console.log(JSON.stringify(Bun.YAML.parse('
              'require("fs").readFileSync(process.env.WORKFLOW_DOC, "utf8"))))')
    document = subprocess.check_output(['bun', '-e', script], text=True,
                                       env={**os.environ, 'WORKFLOW_DOC': str(WORKFLOWS/name)})
    return json.loads(document)


def run_steps(job):
    return [step['run'] for step in job['steps'] if 'run' in step]


def checkout_of(job, repository):
    return next(step for step in job['steps']
                if step.get('uses', '').startswith('actions/checkout')
                and step.get('with', {}).get('repository') == repository)


class CallerRunnerSelectionTests(unittest.TestCase):
    def test_runner_override_applies_to_every_job_and_defaults_to_github(self):
        for name in ('backup.yml', 'dependabot.yml', 'tools.yml', 'vuln.yml'):
            with self.subTest(workflow=name):
                document = load(name)
                runner = document['on']['workflow_call']['inputs']['runner']
                self.assertEqual(runner['type'], 'string')
                self.assertIs(runner['required'], False)
                self.assertEqual(runner['default'], 'ubuntu-24.04')
                for job in document['jobs'].values():
                    self.assertEqual(job['runs-on'], '${{ inputs.runner }}')


class VulnerabilityScanWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.document = load('vuln.yml')
        self.scan = self.document['jobs']['scan']
        self.report = self.document['jobs']['report']

    def test_the_only_trigger_is_workflow_call(self):
        self.assertEqual(list(self.document['on']), ['workflow_call'])

    def test_scan_manifest_and_optional_runner_inputs(self):
        inputs = self.document['on']['workflow_call']['inputs']
        self.assertEqual(set(inputs), {'scan-config', 'runner'})
        self.assertEqual(inputs['scan-config']['default'], 'deploy/scan.json')

    def test_scan_job_keeps_the_fleet_runner_and_read_permissions(self):
        self.assertEqual(self.scan['runs-on'], '${{ inputs.runner }}')
        self.assertEqual(self.scan['timeout-minutes'], 110)
        self.assertEqual(self.scan['permissions'],
                         {'contents': 'read', 'packages': 'read', 'deployments': 'read'})

    def test_deployed_scan_runs_this_workflows_own_helper_for_the_project(self):
        deployed = next(step for step in self.scan['steps'] if 'run' in step
                        and 'scan-deployed.py' in step['run'])
        self.assertEqual(deployed['run'],
                         'python3 "$CICD_ENGINEERING/helpers/scan-deployed.py" --config "$SCAN_CONFIG"')
        self.assertEqual(deployed['env'], {'GH_TOKEN': '${{ github.token }}',
                                           'SCAN_CONFIG': '${{ inputs.scan-config }}'})

    def test_source_scan_invokes_the_canonical_target(self):
        source = next(step for step in self.scan['steps'] if step.get('name') == 'Scan every source module')
        self.assertNotIn('if', source)
        self.assertEqual(source['run'], 'make vuln')

    def test_registry_login_targets_ghcr_with_the_caller_token(self):
        login = next(step for step in self.scan['steps']
                     if step.get('uses', '').startswith('docker/login-action'))
        self.assertEqual(login['with']['registry'], 'ghcr.io')
        self.assertEqual(login['with']['username'], '${{ github.actor }}')
        self.assertEqual(login['with']['password'], '${{ github.token }}')

    def test_report_job_is_a_separate_issue_writer_using_cicds_own_helper(self):
        self.assertEqual(self.report['needs'], ['scan'])
        self.assertEqual(self.report['if'], 'failure()')
        self.assertEqual(self.report['permissions'], {'contents': 'read', 'issues': 'write'})
        checkout = checkout_of(self.report, 'nicodes/tools')
        self.assertRegex(checkout['with']['ref'], HELPER_REF)
        self.assertEqual(checkout['with']['path'], '.cicd')
        self.assertIs(checkout['with']['persist-credentials'], False)
        self.assertEqual(checkout['with']['sparse-checkout'], 'helpers/')
        self.assertIn('python3 .cicd/helpers/report-failure.py', run_steps(self.report))
        report_run = next(step for step in self.report['steps'] if 'run' in step)
        self.assertEqual(report_run['env'], {'GH_TOKEN': '${{ github.token }}'})


class ToolWatchWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.document = load('tools.yml')
        self.watch = self.document['jobs']['watch']

    def test_the_only_trigger_is_workflow_call_with_optional_runner(self):
        self.assertEqual(list(self.document['on']), ['workflow_call'])
        inputs = self.document['on']['workflow_call']['inputs']
        self.assertEqual(set(inputs), {'runner'})

    def test_watch_stays_main_only(self):
        self.assertEqual(self.watch['if'], "github.ref == 'refs/heads/main'")

    def test_watch_declares_no_concurrency_and_the_readme_keeps_it_caller_owned(self):
        # Concurrency is caller-owned. A called job naming the caller's
        # workflow-level `tool-watch` group queues behind the still-running
        # caller and fails with zero steps, no runner and no annotation
        # (fleet: sampleconsole tool-watch run 35528234060).
        self.assertNotIn('concurrency', self.watch)
        self.assertEqual(self.watch['timeout-minutes'], 40)
        readme = (ROOT/'README.md').read_text()
        section = readme[readme.index('### Caller contract: tool watch'):
                         readme.index('### What this repository changed')]
        flat = ' '.join(section.split())
        self.assertIn('Concurrency is caller-owned', flat)
        self.assertIn('tool-watch', flat)

    def test_watch_permissions_are_read_plus_issues_only(self):
        self.assertEqual(self.watch['permissions'], {'contents': 'read', 'issues': 'write'})

    def test_the_callers_checkout_persists_no_credentials(self):
        caller = next(step for step in self.watch['steps']
                      if step.get('uses', '').startswith('actions/checkout')
                      and 'repository' not in step.get('with', {}))
        self.assertIs(caller['with']['persist-credentials'], False)

    def test_watch_runs_this_workflows_own_helper_with_the_caller_token(self):
        """The helper comes with the workflow, not from the caller's tree.

        It used to run the caller's vendored copy, which tied this workflow to
        a layout every product had to keep. A product that installs the
        snapshot has no such path, and the version that belongs with a
        reusable workflow is the one released alongside it -- which the
        caller still chooses, by the SHA it pins the workflow at.
        """
        self.assertIn('python3 .cicd/helpers/watch-tools.py', run_steps(self.watch))
        watch_run = next(step for step in self.watch['steps'] if 'run' in step)
        self.assertEqual(watch_run['env'], {'GH_TOKEN': '${{ github.token }}'})
        self.assertEqual(watch_run['timeout-minutes'], 12)

    def test_update_inventory_artifact_is_retained_thirty_days_whenever_set(self):
        artifact = next(step for step in self.watch['steps']
                        if step.get('uses', '').startswith('actions/upload-artifact'))
        self.assertEqual(artifact['if'], 'always()')
        self.assertEqual(artifact['with'],
                         {'name': 'tool-updates', 'path': '.artifacts/tool-updates.json',
                          'if-no-files-found': 'ignore', 'retention-days': 30})

    def test_failure_reporting_uses_cicds_own_helper_at_the_pinned_revision(self):
        checkout = checkout_of(self.watch, 'nicodes/tools')
        self.assertRegex(checkout['with']['ref'], HELPER_REF)
        self.assertEqual(checkout['with']['sparse-checkout'], 'helpers/')
        failure = next(step for step in self.watch['steps'] if step.get('if') == 'failure()')
        self.assertEqual(failure['run'], 'python3 .cicd/helpers/report-failure.py')
        self.assertEqual(failure['env'], {'GH_TOKEN': '${{ github.token }}'})


class WorkflowPinTests(unittest.TestCase):
    def test_every_uses_reference_is_a_full_sha_with_a_version_comment(self):
        for path in sorted(WORKFLOWS.glob('*.yml')):
            with self.subTest(workflow=path.name):
                entries = []
                for line in path.read_text().splitlines():
                    if line.strip().startswith('#'):
                        continue
                    match = USE_KEY.search(line)
                    if match:
                        entries.append(match.group('value').strip())
                if not entries:
                    # Pure gh-api workflows (deployed.yml) carry no actions;
                    # the pin rule is vacuously satisfied.
                    continue
                for value in entries:
                    self.assertRegex(value, SHA_PIN, f'{path.name}: uses {value}')


class CallerContractFloorTests(unittest.TestCase):
    """Called workflows can only restrict, never elevate, the caller's grant:
    an under-granted caller startup_failures the run with zero jobs
    (live matrix 2026-09-20, nicodes/cicd: tools 35483270243 vs 35483270273;
    vuln 35483270307 vs 35483270237). The README caller contracts must
    therefore publish a permission floor covering every called job."""

    def readme_slice(self, begin, end):
        text = (ROOT/'README.md').read_text()
        return ' '.join(text[text.index(begin):text.index(end)].split())

    def test_vuln_floor_covers_every_called_job_permission(self):
        document = load('vuln.yml')
        floor = set()
        for job in document['jobs'].values():
            floor.update(job['permissions'])
        self.assertEqual(floor, {'contents', 'packages', 'deployments', 'issues'})
        section = self.readme_slice('### Caller contract: vulnerability scan',
                                    '### Caller contract: tool watch')
        for scope in ('contents: read', 'packages: read',
                      'deployments: read', 'issues: write'):
            self.assertIn(scope, section, scope)
        self.assertIn('zero jobs', section)

    def test_tools_floor_covers_every_called_job_permission(self):
        watch = load('tools.yml')['jobs']['watch']
        self.assertEqual(set(watch['permissions']), {'contents', 'issues'})
        section = self.readme_slice('### Caller contract: tool watch',
                                    '### What this repository changed')
        self.assertIn('contents: read', section)
        self.assertIn('issues: write', section)
        self.assertIn('zero jobs', section)


class ToolMaintenanceRenameTests(unittest.TestCase):
    def test_own_maintenance_workflow_survives_under_its_new_name(self):
        document = load('dependency-maintenance.yml')
        self.assertEqual(document['name'], 'Dependency Maintenance')
        self.assertEqual(document['on']['schedule'], [{'cron': '31 9 * * 1'}])
        self.assertIn('python3 helpers/watch-tools.py', run_steps(document['jobs']['watch']))


if __name__ == '__main__':
    unittest.main()


@unittest.skipIf(VENDORED, "cicd-only: this repository's own reusable workflows")
class HelperCheckoutPins(unittest.TestCase):
    """Every self-checkout names one commit, and they all name the same one.

    The README's bump-along rule says "never bump one checkout without the
    other (a test enforces they stay equal)" -- that enforcement only covered
    backup.yml's two. Across the whole file set they were NOT equal: backup
    sat on one revision while tools and vuln sat on another, 10 commits
    apart, and nothing said so.
    """

    def test_every_helper_checkout_is_pinned_to_one_full_sha(self):
        refs = helper_refs()
        self.assertTrue(refs, 'no nicodes/cicd self-checkouts found -- has the shape changed?')
        for ref in refs:
            self.assertRegex(ref, HELPER_REF, 'helper checkouts pin a full commit, never a branch or tag')

    def test_all_helper_checkouts_name_the_same_commit(self):
        refs = helper_refs()
        self.assertEqual(
            len(refs), 1,
            'helper checkouts disagree, so one workflow runs older helpers than another: '
            + '; '.join(f'{ref[:8]} in {", ".join(files)}' for ref, files in sorted(refs.items()))
            + ' -- cut a release (docs/releases.md) rather than bumping by hand',
        )



class HelperProvenance(unittest.TestCase):
    """Every helper a reusable workflow runs comes from a pinned cicd checkout.

    These workflows execute inside the caller's repository with the caller's
    token. Reading the helper out of the caller's working tree made the code
    this organisation runs on somebody's dependabot pull request a function
    of whatever that product had checked out -- and it tied every product to
    keeping a scripts/engineering directory, which is exactly what a product
    that installs the snapshot no longer has.

    The caller still chooses the version: it pins the workflow by SHA, and a
    release repins the helper checkouts to match.
    """

    REUSABLE = ('dependabot.yml', 'tools.yml', 'vuln.yml', 'backup.yml')

    def test_no_reusable_workflow_runs_a_helper_from_the_callers_tree(self):
        for name in self.REUSABLE:
            with self.subTest(workflow=name):
                self.assertNotIn('scripts/engineering/', (WORKFLOWS/name).read_text())

    def test_a_workflow_that_runs_a_cicd_helper_checks_one_out_first(self):
        """Otherwise the run fails at the point of use, in someone else's repository."""
        for name in self.REUSABLE:
            document = load(name) or {}
            for job_name, job in (document.get('jobs') or {}).items():
                steps = job.get('steps') or []
                runs = [i for i, step in enumerate(steps)
                        if '.cicd/helpers/' in str(step.get('run', ''))]
                if not runs:
                    continue
                checkouts = [i for i, step in enumerate(steps)
                             if (step.get('with') or {}).get('path') == '.cicd']
                with self.subTest(workflow=name, job=job_name):
                    self.assertTrue(checkouts, 'runs a .cicd helper without checking .cicd out')
                    self.assertLess(min(checkouts), min(runs), 'the checkout must precede the run')

    def test_every_cicd_checkout_is_pinned_and_carries_no_credentials(self):
        for name in self.REUSABLE:
            document = load(name) or {}
            for job_name, job in (document.get('jobs') or {}).items():
                for step in (job.get('steps') or []):
                    with_ = step.get('with') or {}
                    if with_.get('repository') != 'nicodes/tools':
                        continue
                    with self.subTest(workflow=name, job=job_name):
                        self.assertRegex(str(with_.get('ref', '')), HELPER_REF)
                        self.assertIs(with_.get('persist-credentials'), False)
                        # backup.yml writes it without the trailing slash.
                        # What matters is that the checkout is narrowed to
                        # helpers, not which spelling says so.
                        self.assertIn(with_.get('sparse-checkout'), ('helpers', 'helpers/'))
