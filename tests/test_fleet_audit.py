"""The cross-repository gate, which is the one nobody had.

pins.mjs makes ONE product internally consistent. Every product in the
portfolio was individually green while five different snapshot revisions ran
at once and the samplehost-action pin gate sat in four incompatible states -- a
gap no per-repository check can see, because each repository is right about
itself.

These exercise the judgement, not the fetching: audit() is a pure function
over facts, so the cases below are the shapes the fleet was actually found
in, written down.
"""
import importlib.util
import json
import unittest
from pathlib import Path

from vendored import VENDORED

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fleet_audit', ROOT/'helpers/fleet-audit.py')
fleet_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet_audit)

FLEET = {
    'products': {'org/a': {'profile': 'service'}, 'org/b': {'profile': 'service'},
                 'org/g': {'profile': 'godot'}},
    'shared_tools': ['python', 'bun'],
    'profile_tools': ['go', 'node'],
}


def product(**over):
    base = {
        'snapshot_revision': 'abc1234567',
        'snapshot_files': ['scripts/engineering/SOURCE.json'],
        'tools': {'python': '3.13.11', 'bun': '1.4.1', 'go': '1.27.1', 'node': '24.18.1'},
        'images': {'caddy': 'sha256:6aeddd44c3078b0'},
        'action_pins': {'deploy': 'v0.0.24'},
        'pin_record_path': fleet_audit.CANONICAL_PIN_RECORD,
        'uses_recorded_actions': True,
        'runs_pin_gate': True,
        'has_pin_gate': True,
    }
    base.update(over)
    return base


def kinds(findings):
    return sorted({f.kind for f in findings})


class Agreement(unittest.TestCase):
    def test_a_fleet_that_agrees_has_nothing_to_say(self):
        facts = {'org/a': product(), 'org/b': product(),
                 'org/g': product(tools={'python': '3.13.11', 'bun': '1.4.1',
                                         'go': '1.27.1', 'node': '26.7.0'})}
        self.assertEqual(fleet_audit.audit(facts, FLEET), [])

    def test_two_snapshot_revisions_are_reported_with_both_sides(self):
        facts = {'org/a': product(), 'org/b': product(snapshot_revision='fade862978'),
                 'org/g': product()}
        findings = fleet_audit.audit(facts, FLEET)
        self.assertIn('snapshot', kinds(findings))
        said = str(findings[0])
        # The split, not just the fact of one: "which products" is the thing
        # somebody acts on.
        self.assertIn('org/b', said)
        self.assertIn('fade862978', said)
        self.assertIn('abc1234567', said)

    def test_the_same_revision_in_different_sizes_is_caught(self):
        # Five subsets of one snapshot all validated against their own
        # SOURCE.json, because that file cannot speak about a file it does
        # not list. sampleaddon was missing vendor-snapshot.py and so could not be
        # re-vendored by the only supported writer.
        facts = {'org/a': product(snapshot_files=['a', 'b', 'c']),
                 'org/b': product(snapshot_files=['a']),
                 'org/g': product(snapshot_files=['a', 'b', 'c'])}
        findings = fleet_audit.audit(facts, FLEET)
        self.assertTrue(any('file set' in f.subject for f in findings), findings)


class AbsoluteRules(unittest.TestCase):
    """Some things are not matters of agreement. A fleet unanimously failing
    to check something is still not checking it."""

    def test_a_pin_record_somewhere_else_is_a_record_nothing_reads(self):
        # samplegame carried one in its own schema, covering every action
        # rather than samplehost's, where pins.mjs does not look.
        facts = {'org/a': product(), 'org/b': product(),
                 'org/g': product(pin_record_path='deploy/ACTION-PINS.json')}
        findings = fleet_audit.audit(facts, FLEET)
        said = [str(f) for f in findings if f.kind == 'pins']
        self.assertTrue(any('org/g' in s and 'does not look' in s for s in said), said)

    def test_both_supported_record_locations_are_accepted(self):
        """The root is where it belongs; the snapshot directory is where the
        products that have not migrated still keep it. Neither is a finding."""
        facts = {'org/a': product(pin_record_path='ACTION-PINS.json'),
                 'org/b': product(pin_record_path='scripts/engineering/ACTION-PINS.json')}
        said = [str(f) for f in fleet_audit.audit(facts, FLEET) if f.kind == 'pins']
        self.assertEqual([s for s in said if 'does not look' in s], [])

    def test_an_installed_product_is_not_reported_for_having_no_copied_files(self):
        """Comparing 0 files against 66 is a finding about the mechanism."""
        facts = {'org/a': product(), 'org/b': product(),
                 'org/g': product(snapshot_files=None)}
        said = [str(f) for f in fleet_audit.audit(facts, FLEET) if f.kind == 'snapshot']
        self.assertEqual([s for s in said if 'file set' in s], [], said)

    def test_a_vendored_product_that_really_does_differ_is_still_reported(self):
        facts = {'org/a': product(), 'org/b': product(),
                 'org/g': product(snapshot_files=['scripts/engineering/SOURCE.json',
                                                  'scripts/engineering/extra.py'])}
        said = [str(f) for f in fleet_audit.audit(facts, FLEET) if f.kind == 'snapshot']
        self.assertTrue(any('file set' in s and 'org/g' in s for s in said), said)

    def test_using_komizo_actions_with_no_record_at_all(self):
        facts = {'org/a': product(), 'org/b': product(),
                 'org/g': product(pin_record_path=None, action_pins={})}
        said = [str(f) for f in fleet_audit.audit(facts, FLEET) if f.kind == 'pins']
        self.assertTrue(any('no pin record' in s and 'org/g' in s for s in said), said)

    def test_a_gate_nothing_invokes_is_reported_even_though_it_is_present(self):
        # The subtle one. The file is vendored, its hash matches, every
        # per-repository check is green -- and nothing ever runs it.
        facts = {'org/a': product(), 'org/b': product(), 'org/g': product(runs_pin_gate=False)}
        said = [str(f) for f in fleet_audit.audit(facts, FLEET) if f.kind == 'pins']
        self.assertTrue(any('never invokes it' in s and 'org/g' in s for s in said), said)

    def test_not_having_the_gate_reads_differently_from_not_calling_it(self):
        # These are different repairs -- one is a missing call site, the
        # other is a product that was never given the gate -- and the first
        # draft of this audit reported samplearcade and sampleclient as "carries
        # pins.mjs but never runs it" when they carry no such file. That
        # sends somebody looking for a call site that does not exist.
        facts = {'org/a': product(), 'org/b': product(),
                 'org/g': product(has_pin_gate=False, runs_pin_gate=False)}
        said = [str(f) for f in fleet_audit.audit(facts, FLEET) if f.kind == 'pins']
        self.assertTrue(any('does not vendor pins.mjs at all' in s and 'org/g' in s for s in said), said)
        self.assertFalse(any('never invokes it' in s for s in said), said)

    def test_a_product_that_does_not_use_komizo_actions_is_not_asked_for_one(self):
        facts = {'org/a': product(), 'org/b': product(),
                 'org/g': product(uses_recorded_actions=False, pin_record_path=None,
                                  action_pins={})}
        self.assertEqual([f for f in fleet_audit.audit(facts, FLEET) if f.kind == 'pins'], [])

    def test_a_declared_product_that_could_not_be_read_is_not_silently_skipped(self):
        # Otherwise the audit gets quieter the more repositories break.
        facts = {'org/a': product(), 'org/b': product()}
        said = [str(f) for f in fleet_audit.audit(facts, FLEET)]
        self.assertTrue(any('unreachable' in s and 'org/g' in s for s in said), said)


class ScopedComparisons(unittest.TestCase):
    def test_profile_tools_are_compared_inside_a_profile_not_across(self):
        # The Godot client legitimately runs a different node. Reporting that
        # as drift every day is how a check gets ignored.
        facts = {'org/a': product(), 'org/b': product(),
                 'org/g': product(tools={'python': '3.13.11', 'bun': '1.4.1',
                                         'go': '1.27.1', 'node': '26.7.0'})}
        self.assertEqual([f for f in fleet_audit.audit(facts, FLEET) if f.kind == 'tools'], [])

    def test_but_a_split_inside_one_profile_is_drift(self):
        facts = {'org/a': product(), 'org/b': product(tools=dict(product()['tools'], go='1.27.0')),
                 'org/g': product()}
        said = [str(f) for f in fleet_audit.audit(facts, FLEET) if f.kind == 'tools']
        self.assertTrue(any('go (service)' in s for s in said), said)

    def test_shared_tools_are_compared_across_every_profile(self):
        facts = {'org/a': product(), 'org/b': product(),
                 'org/g': product(tools=dict(product()['tools'], python='3.12.0'))}
        said = [str(f) for f in fleet_audit.audit(facts, FLEET) if f.kind == 'tools']
        self.assertTrue(any('python' in s and 'org/g' in s for s in said), said)

    def test_images_are_compared_per_image_and_only_among_users_of_it(self):
        facts = {'org/a': product(images={'caddy': 'sha256:AAA', 'alpine': 'sha256:ZZZ'}),
                 'org/b': product(images={'caddy': 'sha256:BBB'}),
                 'org/g': product(images={})}
        said = [str(f) for f in fleet_audit.audit(facts, FLEET) if f.kind == 'images']
        self.assertEqual(len(said), 1, said)
        self.assertIn('caddy', said[0])
        # alpine has exactly one user, so there is nothing to disagree about.
        self.assertNotIn('alpine', said[0])

    def test_an_action_is_only_compared_among_products_that_use_it(self):
        facts = {'org/a': product(action_pins={'deploy': 'v0.0.24', 'run-task': 'v0.0.24'}),
                 'org/b': product(action_pins={'deploy': 'v0.0.24'}),
                 'org/g': product(action_pins={'deploy': 'v0.0.14'})}
        said = [str(f) for f in fleet_audit.audit(facts, FLEET) if 'recorded-actions/' in f.subject]
        self.assertEqual(len(said), 1, said)
        self.assertIn('deploy', said[0])


# cicd-only: FLEET.json is this repository's, and a product vendors
# helpers/ and tests/ without it. The rest of this module tests the
# vendored helper itself and is worth running inside a product, so the
# skip is on this class rather than the whole file.


if __name__ == '__main__':
    unittest.main()


class ImageVariantTests(unittest.TestCase):
    """A base image is identified by its tag as well as its name."""

    def read(self, files):
        def gh_tree(repo, ref='HEAD'):
            return sorted(files)

        def gh_file(repo, path, ref='HEAD'):
            return files.get(path)

        original = fleet_audit.gh_tree, fleet_audit.gh_file
        fleet_audit.gh_tree, fleet_audit.gh_file = gh_tree, gh_file
        try:
            return fleet_audit.read_product('org/x')
        finally:
            fleet_audit.gh_tree, fleet_audit.gh_file = original

    def test_two_variants_of_one_image_are_separate_facts(self):
        """sampleservice builds on golang:1.27.1-bookworm for cgo; everyone else on
        -alpine. Keyed on the name alone those digests could never match, and
        the audit reported a permanent disagreement about a deliberate
        difference."""
        facts = self.read({
            'deploy/images/api.Dockerfile':
                'FROM golang:1.27.1-bookworm@sha256:' + 'a' * 64 + ' AS builder\n',
            'deploy/images/gate.Dockerfile':
                'FROM golang:1.27.1-alpine@sha256:' + 'b' * 64 + ' AS builder\n',
        })
        self.assertEqual(sorted(facts['images']),
                         ['golang:1.27.1-alpine', 'golang:1.27.1-bookworm'])

    def test_the_same_variant_still_carries_one_digest_to_compare(self):
        facts = self.read({'deploy/images/api.Dockerfile':
                           'FROM alpine:3@sha256:' + 'c' * 64 + '\n'})
        self.assertEqual(facts['images'], {'alpine:3': ('sha256:' + 'c' * 64)[:19]})
