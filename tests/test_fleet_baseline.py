"""Check caller-owned baseline comparisons through synthetic, offline fixtures."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vendored import VENDORED

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'helpers/fleet-baseline.mjs'

FLEET = {
    'products': {'org/service': {'profile': 'service'}, 'org/game': {'profile': 'godot'}},
    'baseline': {
        'snapshot_revision': 'a' * 40,
        'action_tag': 'v0.0.24',
        'tools': {'shared': {'python': '3.13.11', 'bun': '1.4.1'},
                  'service': {'go': '1.27.1', 'node': '24.18.1'},
                  'godot': {'go': '1.27.1', 'node': '26.7.0'}},
    },
}

GOOD = {
    'snapshotRevision': 'a' * 40,
    'tools': {'python': '3.13.11', 'bun': '1.4.1', 'go': '1.27.1', 'node': '24.18.1'},
    'hasPinRecord': True,
    'actionPins': {'deploy': 'v0.0.24', 'connect': 'v0.0.24'},
    'usesRecordedActions': True,
}


def compare(product, facts, fleet=FLEET):
    """Call the real compare() through bun, so the tested code is the shipped code."""
    program = (
        f'const m = await import({json.dumps(str(HELPER))});'
        'const [product, fleet, facts] = JSON.parse(process.argv[1]);'
        'console.log(JSON.stringify(m.compare(product, fleet, facts)));'
    )
    result = subprocess.run(['bun', '--eval', program, json.dumps([product, fleet, facts])],
                            capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class Baseline(unittest.TestCase):
    def test_a_product_that_matches_says_nothing(self):
        self.assertEqual(compare('org/service', GOOD), [])

    def test_a_godot_product_gets_its_own_node(self):
        # The Godot client legitimately runs a different node. Failing it
        # every run is how a check gets ignored.
        facts = dict(GOOD, tools=dict(GOOD['tools'], node='26.7.0'))
        self.assertEqual(compare('org/game', facts), [])
        # ...and the service profile still may not.
        self.assertTrue(any('node' in p for p in compare('org/service', facts)))

    def test_a_stale_snapshot_names_the_command_that_fixes_it(self):
        facts = dict(GOOD, snapshotRevision='b' * 40)
        said = compare('org/service', facts)
        self.assertTrue(any('vendor-snapshot.py' in p and 'a' * 40 in p for p in said), said)

    def test_a_tool_that_drifted_names_both_versions(self):
        facts = dict(GOOD, tools=dict(GOOD['tools'], go='1.27.0'))
        said = compare('org/service', facts)
        self.assertTrue(any('1.27.0' in p and '1.27.1' in p for p in said), said)

    def test_an_action_pinned_to_an_old_tag(self):
        facts = dict(GOOD, actionPins={'deploy': 'v0.0.14'})
        said = compare('org/service', facts)
        self.assertTrue(any('v0.0.14' in p and 'v0.0.24' in p for p in said), said)

    def test_recorded_actions_with_no_record_points_at_the_bootstrap(self):
        facts = dict(GOOD, hasPinRecord=False, actionPins={})
        said = compare('org/service', facts)
        self.assertTrue(any('action-pins.mjs' in p for p in said), said)

    def test_a_product_not_using_recorded_actions_is_not_asked_about_pins(self):
        facts = dict(GOOD, usesRecordedActions=False, hasPinRecord=False, actionPins={})
        self.assertEqual(compare('org/service', facts), [])

    def test_an_undeclared_product_is_refused_rather_than_passed(self):
        # The quiet failure that matters: a product missing from FLEET.json
        # has no baseline, and "no baseline" must not read as "compliant".
        said = compare('org/nobody-declared', GOOD)
        self.assertEqual(len(said), 1, said)
        self.assertIn('not listed', said[0])


class ToolParsing(unittest.TestCase):
    def test_it_reads_the_shapes_a_real_mise_toml_uses(self):
        program = (
            f'const m = await import({json.dumps(str(HELPER))});'
            'console.log(JSON.stringify(m.readTools(process.argv[1])));'
        )
        toml = ('[tools]\ngo = "1.27.1"\nbun = "1.4.1"\n'
                '# a comment\n"go:github.com/nicodes/samplehost" = "0.0.9"\n'
                'node = "24.18.1"\n')
        result = subprocess.run(['bun', '--eval', program, toml],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        tools = json.loads(result.stdout)
        self.assertEqual(tools['go'], '1.27.1')
        self.assertEqual(tools['node'], '24.18.1')
        self.assertEqual(tools['go:github.com/nicodes/samplehost'], '0.0.9')


class ProductIdentity(unittest.TestCase):
    def name(self, env, remote):
        program = (
            f'const m = await import({json.dumps(str(HELPER))});'
            'const [env, remote] = JSON.parse(process.argv[1]);'
            'console.log(JSON.stringify(m.productName(env, remote)));'
        )
        result = subprocess.run(['bun', '--eval', program, json.dumps([env, remote])],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_ci_says_which_product_this_is(self):
        self.assertEqual(self.name({'GITHUB_REPOSITORY': 'org/app'}, ''), 'org/app')

    def test_a_checkout_is_asked_its_remote(self):
        for remote in ('git@github.com:org/app.git', 'https://github.com/org/app.git',
                       'https://github.com/org/app'):
            self.assertEqual(self.name({}, remote), 'org/app', remote)

    def test_neither_is_not_guessed_at(self):
        self.assertIsNone(self.name({}, ''))


# cicd-only: FLEET.json is this repository's, and a product vendors
# helpers/ and tests/ without it. The rest of this module tests the
# vendored helper itself and is worth running inside a product, so the
# skip is on this class rather than the whole file.


if __name__ == '__main__':
    unittest.main()


class RemedyMatchesTheLayout(unittest.TestCase):
    """Telling somebody to re-vendor a snapshot they install sends them
    looking for a directory that is not there."""

    def drifted(self, source):
        return dict(GOOD, snapshotRevision='b' * 40, snapshotSource=source)

    def test_an_installed_product_is_told_to_move_its_mise_pin(self):
        said = ' '.join(compare('org/service', self.drifted('installed')))
        self.assertIn('http:cicd-engineering', said)
        self.assertNotIn('vendor-snapshot.py', said)

    def test_a_vendoring_product_is_still_told_to_re_vendor(self):
        said = ' '.join(compare('org/service', self.drifted('vendored')))
        self.assertIn('vendor-snapshot.py', said)
        self.assertNotIn('http:cicd-engineering', said)

    def test_the_revision_itself_is_compared_the_same_way_either_way(self):
        """Mechanism may differ across the fleet; the commit may not."""
        for source in ('installed', 'vendored'):
            with self.subTest(source=source):
                self.assertEqual(compare('org/service', dict(GOOD, snapshotSource=source)), [])
