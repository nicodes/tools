import json
import re
from pathlib import Path
import unittest

WORKFLOW = Path(__file__).parents[1] / 'templates' / 'pr-preview.yml'
README = Path(__file__).parents[1] / 'templates' / 'README.md'
# Template pin is a package dependency; fleet adoption policy lives externally.

GUARD = 'github.event.pull_request.head.repo.full_name == github.repository'
MARKER = '<!-- preview -->'
COMPOSITE = 'nicodes/komizo-actions/preview'
# Independently reviewed released dependency for this adoption template.
# Fleet policy is caller-owned; do not infer trust from the template under test.
KOMIZO_ACTIONS_TAG = 'v0.0.25'
KOMIZO_ACTIONS_SHA = 'ff3c1bb4650ba63c3a6191d8043662d5f0ad48b4'
# The annotated tag object of v0.0.21, which must never appear as a pin: a
# `uses:` resolved to a tag object instead of its peeled commit fails.
AN_ANNOTATED_TAG_OBJECT = 'd7cb0c07895eaa19361cd5fb9063e649a616f143'


def job(text, name):
    """The YAML section of one job, from its key to the next job key or EOF."""
    section = text.split(f'\n  {name}:\n', 1)
    assert len(section) == 2, f'exactly one {name} job'
    return re.split(r'\n  [a-z][\w-]*:\n', section[1], maxsplit=1)[0]


class PrPreviewTemplate(unittest.TestCase):
    """Structural pins for the PR preview template: the same-repo guard, the
    one-sticky-comment invariant, the fixed preview-composite contract and the
    permissions floor. Fixture-free and network-free like the rest of
    template-tests; behavior is pinned by exact expressions, not executed."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()
        cls.up = job(cls.text, 'preview-up')
        cls.down = job(cls.text, 'preview-down')

    def test_trigger_covers_ready_review_without_deploying_drafts(self):
        self.assertIn('\non:\n  pull_request:\n', self.text)
        mentions = [line for line in self.text.splitlines() if 'pull_request_target' in line]
        self.assertTrue(mentions, 'the trigger choice is explained in a comment')
        for line in mentions:
            self.assertTrue(line.strip().startswith('#'),
                            'pull_request_target may only ever be mentioned in a comment: ' + line)
        self.assertEqual(self.text.count('types: [opened, synchronize, reopened, ready_for_review, closed]'), 1,
                         'ready_for_review must trigger publication and preview after draft validation')

    def test_same_repo_guard_is_job_level_on_every_privileged_job(self):
        self.assertEqual(self.text.count(GUARD), 2, 'one guard expression per job, nowhere else')
        for name, section in [('preview-up', self.up), ('preview-down', self.down)]:
            with self.subTest(job=name):
                head = section.split('runs-on:')[0]
                self.assertIn(GUARD, head, 'the guard sits in the job-level if, before any step')
                self.assertLess(head.index(GUARD), section.index('steps:'),
                                'job level — a step-level guard would still bring secrets into scope')

    def test_guard_splits_on_close(self):
        up_if = self.up.split('runs-on:')[0]
        down_if = self.down.split('runs-on:')[0]
        self.assertIn("github.event.action != 'closed'", up_if)
        self.assertIn("github.event.pull_request.draft == false", up_if)
        self.assertNotIn("github.event.pull_request.draft", down_if)
        self.assertIn("github.event.action == 'closed'", down_if)

    def test_permissions_floor_and_no_extra_secrets(self):
        self.assertIn('\npermissions:\n  contents: read\n', self.text,
                      'workflow-level floor is contents: read alone')
        for name, section in [('preview-up', self.up), ('preview-down', self.down)]:
            with self.subTest(job=name):
                self.assertIn('contents: read', section)
                self.assertIn('pull-requests: write', section, 'the sticky comment — nothing else')
        up_perms = self.up.split('steps:')[0]
        down_perms = self.down.split('steps:')[0]
        self.assertIn('packages: read', up_perms,
                      'up pulls ghcr images as root on the host — the run GITHUB_TOKEN needs packages:read')
        self.assertNotIn('packages:', down_perms, 'down pulls nothing — no registry grant there')
        self.assertEqual(self.text.count('packages: read'), 1, 'packages: read exactly once, preview-up only')
        for scope in ['id-token', 'deployments', 'secrets']:
            self.assertIsNone(re.search(rf'^\s*{scope}:', self.text, re.M),
                              f'{scope}: is never granted (comments explaining its absence excepted)')
        self.assertEqual(re.findall(r'secrets\.([A-Z_]+)', self.text),
                         ['KOMIZO_DEPLOY_KEY', 'GITHUB_TOKEN', 'KOMIZO_DEPLOY_KEY'],
                         'the deploy key, once per job, plus the run-scoped GITHUB_TOKEN as the up pull credential')
        self.assertEqual(sorted(set(re.findall(r'vars\.([A-Z_]+)', self.text))),
                         ['KOMIZO_KNOWN_HOSTS', 'KOMIZO_SERVER_URL'],
                         'the deploy composite\'s SSH env path, nothing else')

    def test_composite_is_pinned_to_the_reviewed_release(self):
        """Both jobs bind the independently verified released commit and tag."""
        uses = re.findall(rf'uses: {re.escape(COMPOSITE)}@([0-9a-f]{{40}})([^\n]*)', self.text)
        self.assertEqual(len(uses), 2, 'both jobs call the preview composite, SHA-pinned')
        shas = {sha for sha, _ in uses}
        self.assertEqual(len(shas), 1, f'both jobs pin ONE commit, got {sorted(shas)}')
        self.assertEqual(shas, {KOMIZO_ACTIONS_SHA}, 'pin the peeled released commit')
        for _, comment in uses:
            self.assertIn(f'# {KOMIZO_ACTIONS_TAG}', comment,
                          f'the trailing comment names the release the fleet is on '
                          f'({KOMIZO_ACTIONS_TAG} reviewed template dependency)')
            self.assertNotIn('placeholder', comment, 'the placeholder note is gone — the pin is real')
        self.assertNotIn(AN_ANNOTATED_TAG_OBJECT, self.text,
                         'an annotated tag object SHA must never appear as the pin')

    def test_the_readme_does_not_tell_adopters_to_keep_a_stale_pin(self):
        """The README says not to repin on copy, which is only safe while
        the shipped pin is current. If it names a release the fleet has
        moved off, every new adoption starts behind and fails its own pin
        check -- which is exactly what happened at v0.0.21."""
        # Only the version in the do-not-repin instruction is checked. The
        # README also says "Since v0.0.21 the up invocation must pass ...",
        # which is a true statement about history and must not be flagged.
        # \s+ because the sentence wraps across a line.
        promised = re.findall(r'already ships the real\s+(v0\.0\.\d+) pin', README.read_text())
        self.assertEqual(promised, [KOMIZO_ACTIONS_TAG],
                         f'the README tells adopters not to repin on copy, so the pin it promises '
                         f'must be the one the fleet is on ({KOMIZO_ACTIONS_TAG} reviewed template dependency)')

    def test_up_wires_the_required_registry_credentials(self):
        """The composite fails closed if `up` lacks the registry
        login: the pull runs as root on the host and root's docker config
        carries no ghcr authorization. Same wiring as the deploy composite —
        the run-scoped GITHUB_TOKEN, never a long-lived PAT. Down pulls
        nothing, so it wires neither input."""
        self.assertIn('registry-user: ${{ github.actor }}', self.up)
        self.assertIn('registry-token: ${{ secrets.GITHUB_TOKEN }}', self.up)
        self.assertNotIn('registry-user', self.down)
        self.assertNotIn('registry-token', self.down)

    def test_interface_contract_inputs_and_actions(self):
        for name, section, action in [('preview-up', self.up, 'up'), ('preview-down', self.down, 'down')]:
            with self.subTest(job=name):
                self.assertIn('app: ${{ env.APP }}', section)
                self.assertIn('pr-number: ${{ github.event.pull_request.number }}', section)
                self.assertIn('images: ${{ steps.images.outputs.refs }}', section)
                self.assertEqual(section.count(f'action: {action}'), 1)
                other = 'down' if action == 'up' else 'up'
                self.assertNotIn(f'action: {other}', section)
        self.assertEqual(self.text.count('ghcr.io/${{ github.repository_owner }}/'
                                         '$APP-$component:${{ github.event.pull_request.head.sha }}'), 2,
                         'image refs are ghcr.io/<owner>/<project>-<component>:<head-sha> in both jobs')

    def test_sticky_comment_find_or_create_then_update_in_place(self):
        self.assertGreaterEqual(self.up.count(MARKER), 2, 'the marker tags the body and keys the lookup')
        self.assertIn('--paginate', self.up, 'the marker lookup scans every comment page')
        self.assertIn('issues/$PR_NUMBER/comments', self.up)
        self.assertIn('--method POST', self.up, 'first deploy creates the one comment')
        self.assertIn('--method PATCH', self.up, 'synchronize updates the same comment in place')
        self.assertIn('issues/comments/$existing', self.up, 'the update targets the marker-found comment id')
        self.assertEqual(self.up.count('gh api'), 3, 'find, create and update — no other comment paths')
        for output in ['preview-url', 'api-url', 'gate-status']:
            self.assertIn(f'steps.preview.outputs.{output}', self.up,
                          f'the comment carries the composite\'s {output} output')
        self.assertIn('github.event.pull_request.head.sha', self.up, 'the comment names the deployed SHA')

    def test_close_marks_the_same_comment_dead_and_creates_nothing(self):
        self.assertIn(MARKER, self.down, 'the dead-mark keeps the marker so the comment stays identified')
        self.assertIn('torn down', self.down)
        self.assertIn('--method PATCH', self.down, 'dead-mark is an update of the marker-found comment')
        self.assertNotIn('--method POST', self.down, 'close never creates a comment')
        self.assertEqual(self.down.count('gh api'), 2, 'find and update only')

    def test_comment_uses_the_workflows_own_token_and_no_external_action(self):
        self.assertEqual(self.text.count('GH_TOKEN: ${{ github.token }}'), 2)
        for uses in re.findall(r'uses: ([^\s#]+)', self.text):
            action = uses.split('@', 1)[0]
            self.assertIn(action, ['actions/checkout', COMPOSITE],
                          f'{action}: the comment steps run the preinstalled gh CLI, not an action')
            self.assertRegex(uses.split('@', 1)[1], r'^[0-9a-f]{40}$',
                             f'{uses} must be pinned by full commit SHA')

    def test_concurrency_is_per_pr_and_teardown_is_never_cancelled(self):
        group = 'group: preview-${{ github.event.pull_request.number }}'
        self.assertIn(group, self.up)
        self.assertIn('cancel-in-progress: true', self.up, 'a new push supersedes the in-flight deploy')
        self.assertIn(group, self.down, 'same group: close queues behind an in-flight deploy')
        self.assertIn('cancel-in-progress: false', self.down, 'the close event is never superseded')

    def test_checkout_fetches_the_prs_own_head(self):
        self.assertIn('ref: ${{ github.event.pull_request.head.sha }}', self.up,
                      'pull_request, not pull_request_target: the preview deploys the PR\'s code')

    def test_readme_documents_adoption_and_the_invariants(self):
        readme = README.read_text()
        self.assertIn('## PR preview deployments', readme)
        self.assertIn(GUARD, readme, 'the guard\'s exact expression is documented as non-negotiable')
        self.assertIn('non-negotiable', readme)
        self.assertIn('pull_request_target', readme, 'the trigger choice is explained')
        self.assertIn(MARKER, readme, 'the one-sticky-comment invariant is documented')
        self.assertIn('KOMIZO_DEPLOY_KEY', readme)
        self.assertIn('KOMIZO_SERVER_URL', readme)
        self.assertIn('KOMIZO_KNOWN_HOSTS', readme)
        self.assertIn('fork', readme.lower(), 'the fork-PR skip behavior is documented')
        self.assertIn('cancel-in-progress: false', readme, 'the close-path interplay is documented')


if __name__ == '__main__':
    unittest.main()
