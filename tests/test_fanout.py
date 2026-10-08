"""The fan-out edits: both pins move, together, or the product is refused."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

from vendored import skip_module_if_vendored

skip_module_if_vendored("cicd-only: scripts/fanout.py is this repository's own release tooling")

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fanout', ROOT / 'scripts/fanout.py')
fanout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fanout)

OLD = 'a' * 40
NEW = 'b' * 40
OLD_SUM = 'c' * 64
NEW_SUM = 'd' * 64

MISE = ('[tools]\n'
        '"http:cicd-engineering" = { version = "0.6.0", url = "https://example/v{{ version }}.tar.gz", '
        f'checksum = "sha256:{OLD_SUM}" }}\n'
        'bun = "1.4.1"\n')


class PublicationEvidence(unittest.TestCase):
    def workflow(self, test_job='test'):
        return f'''jobs:
  {test_job}:
    name: Test
    steps:
      - uses: nicodes/tools/make@{OLD}
        with:
          target: install lint test vuln
  build:
    steps: []
  publish:
    needs: [{test_job}, build]
    steps:
      - name: Publish the validated images
        run: python3 "$CICD_ENGINEERING"/helpers/release.py publish --revision "$GITHUB_SHA"
'''

    def test_actual_job_id_controls_artifact_selection_and_publisher(self):
        for job in ('test', 'verify_tests'):
            with self.subTest(job=job):
                text = fanout.wire_test_evidence(self.workflow(job))
                self.assertIn(f'pattern: engineering-{job}-*', text)
                self.assertIn(f'--test-job {job}', text)
                self.assertIn('path: .artifacts/test-evidence', text)
                self.assertIn(fanout.DOWNLOAD_ARTIFACT, text)
                self.assertEqual(fanout.wire_test_evidence(text), text)

    def test_checkout_precedes_download_and_publication(self):
        text = self.workflow().replace('      - name: Publish the validated images',
                                      f'      - uses: actions/checkout@{OLD}\n      - name: Publish the validated images')
        wired = fanout.wire_test_evidence(text)
        self.assertLess(wired.index('actions/checkout@'), wired.index('id: engineering-test-evidence'))
        self.assertLess(wired.index('id: engineering-test-evidence'), wired.index('name: Publish the validated images'))
        self.assertEqual(fanout.wire_test_evidence(wired), wired)

    def test_existing_download_before_checkout_is_refused(self):
        wired = fanout.wire_test_evidence(self.workflow())
        invalid = wired.replace('      - name: Publish the validated images',
                                f'      - uses: actions/checkout@{OLD}\n      - name: Publish the validated images')
        with self.assertRaisesRegex(ValueError, 'checkout would delete'):
            fanout.wire_test_evidence(invalid)

    def test_equivalent_download_without_step_id_is_preserved(self):
        wired = fanout.wire_test_evidence(self.workflow())
        existing = wired.replace('        id: engineering-test-evidence\n', '')
        self.assertEqual(fanout.wire_test_evidence(existing), existing)
        invalid = existing.replace('      - name: Publish the validated images',
                                   f'      - uses: actions/checkout@{OLD}\n      - name: Publish the validated images')
        with self.assertRaisesRegex(ValueError, 'checkout would delete'):
            fanout.wire_test_evidence(invalid)

    def test_trigger_mappings_do_not_become_jobs(self):
        text = 'name: CD\non:\n  workflow_dispatch:\n  push:\n    branches: [main]\n'+self.workflow()
        wired = fanout.wire_test_evidence(text)
        self.assertIn('pattern: engineering-test-*', wired)
        self.assertTrue(wired.startswith('name: CD\non:\n'))

    def test_missing_or_ambiguous_test_dependency_is_refused(self):
        text = self.workflow()
        for invalid in (text.replace('needs: [test, build]', 'needs: [build]'),
                        text.replace('target: install lint test vuln', 'target: install lint'),
                        text.replace('  build:\n    steps: []', f'  build:\n    steps:\n      - uses: nicodes/tools/make@{OLD}\n        with:\n          target: test')):
            with self.assertRaises(ValueError):
                fanout.wire_test_evidence(invalid)

    def test_wrong_existing_download_or_job_is_refused(self):
        text = fanout.wire_test_evidence(self.workflow())
        for invalid in (text.replace('engineering-test-*', 'engineering-Build-*'),
                        text.replace('--test-job test', '--test-job Test'),
                        text.replace('.artifacts/test-evidence', '/tmp/unrelated')):
            with self.assertRaises(ValueError):
                fanout.wire_test_evidence(invalid)

    def test_unrelated_product_owned_publication_is_unchanged(self):
        text = 'jobs:\n  deploy:\n    steps:\n      - run: make deploy\n'
        self.assertEqual(fanout.wire_test_evidence(text), text)

    def test_head_preview_runs_actual_head_tests_and_uses_its_own_receipt(self):
        text = f'''jobs:
  build:
    steps:
      - uses: nicodes/tools/make@{OLD}
        with:
          target: install build artifact-check e2e
      - run: python3 "$CICD_ENGINEERING"/helpers/release.py publish --revision "$HEAD_SHA"
'''
        wired = fanout.wire_test_evidence(text)
        self.assertIn('target: install test build artifact-check e2e', wired)
        self.assertIn('--test-job build --test-evidence-directory .artifacts/contract', wired)
        self.assertNotIn('Download same-run', wired)
        self.assertEqual(fanout.wire_test_evidence(wired), wired)

    def test_service_composite_retains_same_run_test_download(self):
        text = '''name: Publish
runs:
  using: composite
  steps:
    - shell: bash
      run: python3 "$CICD_ENGINEERING"/helpers/release.py publish --revision "$RELEASE_COMMIT"
'''
        wired = fanout.wire_test_evidence(text)
        self.assertIn('pattern: engineering-test-*', wired)
        self.assertIn('--test-job test', wired)
        self.assertEqual(fanout.wire_test_evidence(wired), wired)
        with self.assertRaises(ValueError):
            fanout.wire_test_evidence(wired.replace('engineering-test-*', 'engineering-build-*'))


class MiseEntry(unittest.TestCase):
    def test_version_and_checksum_move_together(self):
        text, changed = fanout.bump_mise(MISE, '0.7.0', NEW_SUM)
        self.assertTrue(changed)
        self.assertIn('version = "0.7.0"', text)
        self.assertIn(f'sha256:{NEW_SUM}', text)
        self.assertNotIn(OLD_SUM, text)
        self.assertNotIn('0.6.0', text)

    def test_other_tools_are_untouched(self):
        text, _ = fanout.bump_mise(MISE, '0.7.0', NEW_SUM)
        self.assertIn('bun = "1.4.1"', text)

    def test_the_url_template_is_preserved(self):
        """It interpolates {{ version }}; rewriting it would pin the URL."""
        text, _ = fanout.bump_mise(MISE, '0.7.0', NEW_SUM)
        self.assertIn('v{{ version }}.tar.gz', text)

    def test_a_product_with_no_entry_is_refused_not_skipped(self):
        with self.assertRaises(ValueError):
            fanout.bump_mise('[tools]\nbun = "1.4.1"\n', '0.7.0', NEW_SUM)

    def test_two_entries_are_refused_rather_than_half_bumped(self):
        with self.assertRaises(ValueError):
            fanout.bump_mise(MISE + MISE.split('\n', 1)[1], '0.7.0', NEW_SUM)

    def test_bumping_to_what_is_already_there_reports_no_change(self):
        text, changed = fanout.bump_mise(MISE, '0.6.0', OLD_SUM)
        self.assertFalse(changed)
        self.assertEqual(text, MISE)


class WorkflowPins(unittest.TestCase):
    def test_current_make_action_moves(self):
        text, changed = fanout.bump_workflow(f'- uses: nicodes/tools/make@{OLD} # v0.6.0\n', '0.7.0', NEW)
        self.assertTrue(changed)
        self.assertIn(f'nicodes/tools/make@{NEW} # v0.7.0', text)

    def test_godot_setup_action_moves_with_release(self):
        text, changed = fanout.bump_workflow(f'- uses: nicodes/tools/godot-setup@{OLD} # v0.6.0\n', '0.7.0', NEW)
        self.assertTrue(changed)
        self.assertIn(f'nicodes/tools/godot-setup@{NEW} # v0.7.0', text)

    def test_nonimmutable_and_unknown_tools_consumers_fail(self):
        for reference in ['make@main', 'make@v0.6.0', 'unknown@'+OLD, 'make@'+OLD+'x']:
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                fanout.bump_workflow('uses: nicodes/tools/'+reference, '0.7.0', NEW)

    def test_renamed_and_legacy_calls_are_both_repinned(self):
        body = (f'uses: nicodes/tools/.github/workflows/backup.yml@{OLD}\n'
                f'uses: nicodes/tools/.github/workflows/vuln.yml@{OLD}\n'
                f'uses: someone/tools/.github/workflows/vuln.yml@{OLD}\n')
        text, changed = fanout.bump_workflow(body, '0.9.1', NEW)
        self.assertTrue(changed)
        self.assertIn(f'nicodes/tools/.github/workflows/backup.yml@{NEW}', text)
        self.assertIn(f'nicodes/tools/.github/workflows/vuln.yml@{NEW}', text)
        self.assertIn(f'someone/tools/.github/workflows/vuln.yml@{OLD}', text)

    def workflow(self, sha, comment=' # v0.6.0'):
        return (f'jobs:\n  a:\n    uses: nicodes/tools/.github/workflows/vuln.yml@{sha}{comment}\n'
                f'  b:\n    uses: nicodes/tools/.github/workflows/tools.yml@{sha}{comment}\n')

    def test_every_call_site_moves(self):
        text, changed = fanout.bump_workflow(self.workflow(OLD), '0.7.0', NEW)
        self.assertTrue(changed)
        self.assertEqual(text.count(f'@{NEW} # v0.7.0'), 2)
        self.assertNotIn(OLD, text)

    def test_a_pin_with_no_comment_gains_the_version(self):
        text, _ = fanout.bump_workflow(self.workflow(OLD, comment=''), '0.7.0', NEW)
        self.assertEqual(text.count(f'@{NEW} # v0.7.0'), 2)

    def test_unrelated_actions_are_not_touched(self):
        body = f'    - uses: actions/checkout@{OLD} # v7.0.1\n'
        text, changed = fanout.bump_workflow(body, '0.7.0', NEW)
        self.assertFalse(changed)
        self.assertEqual(text, body)

    def test_a_samplehost_action_is_not_touched(self):
        """Those are the fleet pin record's business, not a cicd release's."""
        body = f'    - uses: example-org/deploy-actions/deploy@{OLD} # v0.0.24\n'
        _, changed = fanout.bump_workflow(body, '0.7.0', NEW)
        self.assertFalse(changed)


class ApplyAcrossAProduct(unittest.TestCase):
    def test_local_action_and_independent_pin_move_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.product(directory)
            action = root/'.github/actions/test/action.yaml'
            action.parent.mkdir(parents=True)
            action.write_text(f'uses: nicodes/tools/make@{OLD}\n')
            pin = root/'engineering-pin.json'
            pin.write_text('{"repository":"https://github.com/nicodes/tools","revision":"'+OLD+'","source_sha256":"'+OLD_SUM+'"}')
            fanout.apply(root, '0.7.0', NEW, NEW_SUM, NEW_SUM)
            self.assertIn(NEW, action.read_text())
            self.assertIn(NEW_SUM, pin.read_text())
            self.assertEqual(fanout.apply(root, '0.7.0', NEW, NEW_SUM, NEW_SUM), [])

    def test_manifest_records_actual_immutable_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.product(directory)
            changes = []
            fanout.apply(root, '0.7.0', NEW, NEW_SUM, change_manifest=changes)
            workflow = next(item for item in changes if item['path'] == '.github/workflows/cd.yml')
            self.assertEqual(workflow['before_tools_refs'], [f'nicodes/tools/.github/workflows/vuln.yml@{OLD} # v0.6.0'])
            self.assertEqual(workflow['after_tools_refs'], [f'nicodes/tools/.github/workflows/vuln.yml@{NEW} # v0.7.0'])
            self.assertNotEqual(workflow['before_sha256'], workflow['after_sha256'])

    def test_invalid_consumer_leaves_all_files_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.product(directory)
            workflow = root/'.github/workflows/ci.yml'
            workflow.write_text('uses: nicodes/tools/make@main\n')
            before = (root/'.mise.toml').read_text()
            with self.assertRaises(ValueError):
                fanout.apply(root, '0.7.0', NEW, NEW_SUM)
            self.assertEqual((root/'.mise.toml').read_text(), before)

    def test_missing_manifest_digest_leaves_all_files_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.product(directory)
            (root/'engineering-pin.json').write_text('{}')
            with self.assertRaises(ValueError):
                fanout.apply(root, '0.7.0', NEW, NEW_SUM)
            self.assertEqual((root/'.mise.toml').read_text(), MISE)

    def test_dry_run_leaves_all_files_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.product(directory)
            touched = fanout.apply(root, '0.7.0', NEW, NEW_SUM, dry_run=True)
            self.assertTrue(touched)
            self.assertEqual((root/'.mise.toml').read_text(), MISE)

    def product(self, directory):
        root = Path(directory)
        (root / '.github/workflows').mkdir(parents=True)
        (root / '.mise.toml').write_text(MISE)
        (root / '.github/workflows/cd.yml').write_text(
            f'jobs:\n  a:\n    uses: nicodes/tools/.github/workflows/vuln.yml@{OLD} # v0.6.0\n')
        (root / '.github/workflows/ci.yml').write_text('jobs:\n  a:\n    runs-on: ubuntu-24.04\n')
        return root

    def test_it_reports_exactly_what_it_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.product(directory)
            touched = fanout.apply(root, '0.7.0', NEW, NEW_SUM)
            self.assertEqual(touched, ['.mise.toml', '.github/workflows/cd.yml'])

    def test_a_workflow_with_no_cicd_call_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.product(directory)
            before = (root / '.github/workflows/ci.yml').read_text()
            fanout.apply(root, '0.7.0', NEW, NEW_SUM)
            self.assertEqual((root / '.github/workflows/ci.yml').read_text(), before)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.product(directory)
            fanout.apply(root, '0.7.0', NEW, NEW_SUM)
            self.assertEqual(fanout.apply(root, '0.7.0', NEW, NEW_SUM), [])

    def test_both_pins_end_up_naming_one_release(self):
        """The invariant pins.mjs enforces, which is why they move together."""
        with tempfile.TemporaryDirectory() as directory:
            root = self.product(directory)
            fanout.apply(root, '0.7.0', NEW, NEW_SUM)
            self.assertIn('version = "0.7.0"', (root / '.mise.toml').read_text())
            self.assertIn(f'@{NEW} # v0.7.0', (root / '.github/workflows/cd.yml').read_text())

    def test_reviewed_policy_and_digest_move_atomically(self):
        import hashlib
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = self.product(directory)
            policy = {'baseline': {'snapshot_revision': OLD}, 'auth': {'enforced': True}}
            old = (json.dumps(policy)+'\n').encode()
            (root/'fleet-policy.json').write_bytes(old)
            env = root/'scripts/engineering-env.sh'
            env.parent.mkdir()
            env.write_text('export FLEET_BASELINE_SHA256="${FLEET_BASELINE_SHA256:-'+hashlib.sha256(old).hexdigest()+'}"\n')
            with self.assertRaises(ValueError):
                fanout.apply(root, '0.7.0', NEW, NEW_SUM)
            self.assertEqual((root/'.mise.toml').read_text(), MISE)
            policy['baseline']['snapshot_revision'] = NEW
            reviewed = (json.dumps(policy)+'\n').encode()
            fanout.apply(root, '0.7.0', NEW, NEW_SUM, reviewed_policy=reviewed)
            self.assertEqual((root/'fleet-policy.json').read_bytes(), reviewed)
            self.assertIn(hashlib.sha256(reviewed).hexdigest(), env.read_text())
            self.assertEqual(fanout.apply(root, '0.7.0', NEW, NEW_SUM, reviewed_policy=reviewed), [])
            policy['auth']['enforced'] = False
            with self.assertRaises(ValueError):
                fanout.apply(root, '0.7.0', NEW, NEW_SUM, reviewed_policy=json.dumps(policy).encode())


if __name__ == '__main__':
    unittest.main()
