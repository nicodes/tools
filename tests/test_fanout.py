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


if __name__ == '__main__':
    unittest.main()
