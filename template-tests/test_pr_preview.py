import re
from pathlib import Path
import unittest

WORKFLOW = Path(__file__).parents[1] / 'templates' / 'pr-preview.yml'
README = Path(__file__).parents[1] / 'README.md'
# Template pin is a package dependency; fleet adoption policy lives externally.

MARKER = '<!-- preview -->'
REQUEST = 'nicodes/komizo-actions/preview-request'
COMPOSITE = 'nicodes/komizo-actions/preview'
GATE_UP = "needs.request.outputs.enabled == 'true' && needs.request.outputs.action == 'up'"
GATE_DOWN = "needs.request.outputs.enabled == 'true' && needs.request.outputs.action == 'down'"
# Independently reviewed released dependency for this adoption template.
# Fleet policy is caller-owned; do not infer trust from the template under test.
KOMIZO_ACTIONS_TAG = 'v0.0.33'
KOMIZO_ACTIONS_SHA = '398fe9d90aa68daadf6569cb7679df4478218a37'
# The annotated tag object of v0.0.21, which must never appear as a pin: a
# `uses:` resolved to a tag object instead of its peeled commit fails.
AN_ANNOTATED_TAG_OBJECT = 'd7cb0c07895eaa19361cd5fb9063e649a616f143'


def job(text, name):
    """The YAML section of one job, from its key to the next job key or EOF."""
    section = text.split(f'\n  {name}:\n', 1)
    assert len(section) == 2, f'exactly one {name} job'
    return re.split(r'\n  [a-z][\w-]*:\n', section[1], maxsplit=1)[0]


def code(text):
    """The workflow without its comment lines, for assertions about what runs."""
    return '\n'.join(line for line in text.splitlines() if not line.strip().startswith('#'))


def job_header(section):
    """Everything before the steps: name, if, runs-on, permissions, env."""
    return section.split('steps:')[0]


class PrPreviewTemplate(unittest.TestCase):
    """Structural pins for the opt-in PR preview template: the secret-free
    request job every privileged job gates on, the one-sticky-comment
    invariant, the preview label wiring, the fixed preview-composite contract
    and the permissions floor. Fixture-free and network-free like the rest of
    template-tests; behavior is pinned by exact expressions, not executed."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()
        cls.code = code(cls.text)
        cls.request = job(cls.text, 'request')
        cls.up = job(cls.text, 'preview-up')
        cls.down = job(cls.text, 'preview-down')

    def test_display_name_matches_the_workflow_contract_catalogue(self):
        self.assertTrue(self.text.startswith('name: PR Preview\n'),
                        'the fleet workflow contract requires the display name PR Preview')

    def test_trigger_keeps_the_full_type_list_and_adds_comments(self):
        self.assertIn('\non:\n  pull_request:\n', self.text)
        mentions = [line for line in self.text.splitlines() if 'pull_request_target' in line]
        self.assertTrue(mentions, 'the trigger choice is explained in a comment')
        for line in mentions:
            self.assertTrue(line.strip().startswith('#'),
                            'pull_request_target may only ever be mentioned in a comment: ' + line)
        self.assertEqual(self.text.count('types: [opened, synchronize, reopened, ready_for_review, closed]'), 1,
                         'the workflow contract requires every lifecycle event; the opt-in rule gates inside the jobs')
        self.assertIn('\n  issue_comment:\n', self.text, 'the comment path is a trigger')
        self.assertEqual(self.text.count('types: [created]'), 1, 'issue_comment created, nothing else')

    def test_request_job_is_secret_free_cheap_and_skips_plain_issues(self):
        head = job_header(self.request)
        self.assertIn("if: github.event_name != 'issue_comment' || github.event.issue.pull_request != null", head,
                      'a comment on a plain issue never costs a runner')
        self.assertIn('runs-on: ubuntu-24.04', head, 'a hosted runner: the decision is seconds of work')
        self.assertIn('timeout-minutes: 3', head)
        self.assertIn('pull-requests: read', head, 'the one PR lookup on comments')
        self.assertNotIn('pull-requests: write', head)
        self.assertNotIn('packages:', head)
        self.assertNotIn('secrets.', self.request, 'the decision job names no secret')
        self.assertNotIn('vars.', self.request)
        self.assertNotIn('KOMIZO', self.request, 'no deploy path in the decision job')
        for output in ['enabled', 'action', 'pr', 'sha']:
            self.assertIn(f'{output}: ${{{{ steps.request.outputs.{output} }}}}', head,
                          f'the {output} decision output is published to the privileged jobs')
        self.assertEqual(len(re.findall(r'uses: ', self.request)), 1, 'one step: the composite')
        self.assertIn(f'uses: {REQUEST}@{KOMIZO_ACTIONS_SHA} # {KOMIZO_ACTIONS_TAG}', self.request)
        self.assertIn('label: preview', self.request, 'the label the composite reads is the one the jobs write')

    def test_privileged_jobs_gate_on_the_request_decision(self):
        for name, section, gate in [('preview-up', self.up, GATE_UP), ('preview-down', self.down, GATE_DOWN)]:
            with self.subTest(job=name):
                head = job_header(section)
                self.assertIn('needs: request', head)
                self.assertIn(f'if: {gate}', head, 'the gate sits in the job-level if, before any step')
                self.assertLess(head.index(gate), section.index('steps:'),
                                'job level — a step-level gate would still bring secrets into scope')
        self.assertEqual(self.text.count(GATE_UP), 1)
        self.assertEqual(self.text.count(GATE_DOWN), 1)
        for stale in ['github.event.pull_request.draft', 'github.event.pull_request.head.repo.full_name',
                      "github.actor != 'dependabot[bot]'", "github.event.action"]:
            self.assertNotIn(stale, self.code,
                             f'{stale}: payload-shaped guards moved into the composite; a comment event has no '
                             'pull_request payload, so such an expression would silently skip the comment path')

    def test_resolved_identity_replaces_event_pull_request_fields(self):
        self.assertNotIn('github.event.pull_request.head.sha', self.code,
                         'a comment-triggered run has no pull_request payload: use the resolved sha')
        self.assertNotIn('github.event.pull_request.number }}', self.code.replace(
            'github.event.pull_request.number || github.event.issue.number', ''),
            'the PR number comes from the request job everywhere but the concurrency key')
        self.assertIn('ref: ${{ needs.request.outputs.sha }}', self.up,
                      'pull_request, not pull_request_target: the preview deploys the resolved PR head')
        for section in [self.up, self.down]:
            self.assertIn('PR_NUMBER: ${{ needs.request.outputs.pr }}', section)
            self.assertIn('HEAD_SHA: ${{ needs.request.outputs.sha }}', section)
        self.assertEqual(self.text.count('ghcr.io/${{ github.repository_owner }}/$APP-$component:$HEAD_SHA'), 2,
                         'image refs are ghcr.io/<owner>/<project>-<component>:<head-sha> in both jobs')

    def test_permissions_floor_and_no_extra_secrets(self):
        self.assertIn('\npermissions:\n  contents: read\n', self.text,
                      'workflow-level floor is contents: read alone')
        for name, section in [('preview-up', self.up), ('preview-down', self.down)]:
            with self.subTest(job=name):
                self.assertIn('contents: read', section)
                self.assertIn('pull-requests: write', section, 'the sticky comment and the label — nothing else')
        up_perms = job_header(self.up)
        down_perms = job_header(self.down)
        self.assertIn('packages: read', up_perms,
                      'up pulls ghcr images as root on the host — the run GITHUB_TOKEN needs packages:read')
        self.assertNotIn('packages:', down_perms, 'down pulls nothing — no registry grant there')
        self.assertEqual(self.text.count('packages: read'), 1, 'packages: read exactly once, preview-up only')
        for scope in ['id-token', 'deployments', 'secrets', 'issues']:
            self.assertIsNone(re.search(rf'^\s*{scope}:', self.text, re.M),
                              f'{scope}: is never granted (comments explaining its absence excepted)')
        self.assertEqual(re.findall(r'secrets\.([A-Z_]+)', self.text),
                         ['KOMIZO_DEPLOY_KEY', 'GITHUB_TOKEN', 'KOMIZO_DEPLOY_KEY'],
                         'the deploy key, once per privileged job, plus the run-scoped GITHUB_TOKEN as the up pull credential')
        self.assertEqual(sorted(set(re.findall(r'vars\.([A-Z_]+)', self.text))),
                         ['KOMIZO_KNOWN_HOSTS', 'KOMIZO_SERVER_URL'],
                         'the deploy composite\'s SSH env path, nothing else')

    def test_composites_are_pinned_to_the_reviewed_release(self):
        """Every komizo-actions use binds the independently verified released commit and tag."""
        uses = re.findall(r'uses: nicodes/komizo-actions/([\w-]+)@([0-9a-f]{40})([^\n]*)', self.text)
        self.assertEqual(sorted(action for action, _, _ in uses), ['preview', 'preview', 'preview-request'],
                         'the request job calls preview-request; both privileged jobs call preview')
        shas = {sha for _, sha, _ in uses}
        self.assertEqual(shas, {KOMIZO_ACTIONS_SHA}, f'every use pins ONE peeled released commit, got {sorted(shas)}')
        for _, _, comment in uses:
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

    def test_up_waits_for_the_ci_images_before_deploying(self):
        """CI pushes the PR-head images in parallel with this workflow on a
        push; the wait is bounded and the stale run is cancelled by the
        concurrency group. The login is the run token on stdin, logged out
        however the step exits."""
        wait = self.up.index('Wait for CI to publish the PR-head images')
        self.assertLess(wait, self.up.index('name: Preview up'), 'the wait precedes the deploy')
        self.assertIn('deadline=$(( SECONDS + 25 * 60 ))', self.up, 'a 25-minute bound')
        self.assertIn('docker manifest inspect "$ref"', self.up)
        self.assertIn('--password-stdin', self.up, 'the token never appears on a command line')
        self.assertIn("trap 'docker logout ghcr.io", self.up)
        self.assertIn('timeout-minutes: 35', job_header(self.up), 'the job budget covers the wait')

    def test_interface_contract_inputs_and_actions(self):
        for name, section, action in [('preview-up', self.up, 'up'), ('preview-down', self.down, 'down')]:
            with self.subTest(job=name):
                self.assertIn('app: ${{ env.APP }}', section)
                self.assertIn('pr-number: ${{ needs.request.outputs.pr }}', section)
                self.assertIn('images: ${{ steps.images.outputs.refs }}', section)
                self.assertEqual(section.count(f'action: {action}'), 1)
                other = 'down' if action == 'up' else 'up'
                self.assertNotIn(f'action: {other}', section)

    def test_label_is_added_after_a_successful_up_and_removed_after_down(self):
        add = 'gh pr edit "$PR_NUMBER" --add-label preview'
        remove = 'gh pr edit "$PR_NUMBER" --remove-label preview'
        self.assertEqual(self.up.count(add), 1)
        self.assertNotIn('--remove-label', self.up)
        self.assertGreater(self.up.index(add), self.up.index('name: Preview up'),
                           'the label is written only after the composite reported the stack up')
        self.assertGreater(self.up.index(add), self.up.index('Upsert the sticky preview comment'),
                           'the comment with the URL lands before the label step can fail on a missing label')
        self.assertEqual(self.down.count(remove), 1)
        self.assertNotIn('--add-label', self.down)
        self.assertGreater(self.down.index(remove), self.down.index('name: Preview down'),
                           'the label is cleared only after the teardown was verified')
        self.assertIn(remove + ' || echo "::warning::', self.down,
                      'a missing label never fails a teardown that already succeeded')
        for name, section, command in [('preview-up', self.up, add), ('preview-down', self.down, remove)]:
            with self.subTest(job=name):
                step = section[section.rindex('env:', 0, section.index(command)):section.index(command)]
                self.assertIn('GH_REPO: ${{ github.repository }}', step,
                              'gh pr edit resolves the repository from a git remote by default; preview-down has '
                              'no checkout, so without GH_REPO the label flip fails (and down would swallow it)')
        creating = [line for line in self.code.splitlines() if 'gh label create' in line]
        self.assertEqual(len(creating), 1, 'the operator hint in the down warning is the only mention outside comments')
        self.assertIn('::warning::', creating[0],
                      'the workflow never creates the label (that needs issues: write); an operator does, once')

    def test_sticky_comment_find_or_create_then_update_in_place(self):
        self.assertGreaterEqual(self.up.count(MARKER), 2, 'the marker tags the body and keys the lookup')
        self.assertIn('--paginate', self.up, 'the marker lookup scans every comment page')
        self.assertIn('issues/$PR_NUMBER/comments', self.up)
        self.assertIn('--method POST', self.up, 'first deploy creates the one comment')
        self.assertIn('--method PATCH', self.up, 'a redeploy updates the same comment in place')
        self.assertIn('issues/comments/$existing', self.up, 'the update targets the marker-found comment id')
        self.assertEqual(self.up.count('gh api'), 3, 'find, create and update — no other comment paths')
        for output in ['preview-url', 'api-url', 'gate-status']:
            self.assertIn(f'steps.preview.outputs.{output}', self.up,
                          f'the comment carries the composite\'s {output} output')
        self.assertIn('"$HEAD_SHA"', self.up, 'the comment names the deployed SHA')
        self.assertIn('/preview down', self.up, 'the comment tells the reader how to remove the preview')

    def test_close_marks_the_same_comment_dead_and_creates_nothing(self):
        self.assertIn(MARKER, self.down, 'the dead-mark keeps the marker so the comment stays identified')
        self.assertIn('torn down', self.down)
        self.assertIn('--method PATCH', self.down, 'dead-mark is an update of the marker-found comment')
        self.assertNotIn('--method POST', self.down, 'close never creates a comment')
        self.assertEqual(self.down.count('gh api'), 2, 'find and update only')

    def test_comment_and_label_use_the_workflows_own_token_and_no_external_action(self):
        self.assertEqual(self.text.count('GH_TOKEN: ${{ github.token }}'), 4,
                         'comment and label steps in each privileged job; nothing else holds the token by that name')
        for uses in re.findall(r'uses: ([^\s#]+)', self.text):
            action = uses.split('@', 1)[0]
            self.assertIn(action, ['actions/checkout', COMPOSITE, REQUEST],
                          f'{action}: the comment, wait and label steps run the preinstalled CLIs, not an action')
            self.assertRegex(uses.split('@', 1)[1], r'^[0-9a-f]{40}$',
                             f'{uses} must be pinned by full commit SHA')

    def test_concurrency_is_per_pr_and_teardown_is_never_cancelled(self):
        group = 'group: preview-${{ github.event.pull_request.number || github.event.issue.number }}'
        self.assertIn(group, self.up, 'the key reads the PR number from whichever payload the event has')
        self.assertIn('cancel-in-progress: true', self.up, 'a new push supersedes the in-flight deploy')
        self.assertIn(group, self.down, 'same group: close queues behind an in-flight deploy')
        self.assertIn('cancel-in-progress: false', self.down, 'the close event is never superseded')
        self.assertNotIn('concurrency', self.request, 'the decision job is never queued behind a deploy')

    def test_readme_documents_adoption_and_the_invariants(self):
        readme = README.read_text()
        self.assertIn('## PR preview deployments', readme)
        self.assertIn('/preview', readme, 'the comment command is documented')
        self.assertIn('/preview down', readme)
        self.assertIn('`preview` label', readme, 'the label as the redeploy signal is documented')
        self.assertIn('preview-request', readme, 'the decision composite is named')
        self.assertIn(GATE_UP, readme, 'the gate\'s exact expression is documented as non-negotiable')
        self.assertIn('non-negotiable', readme)
        self.assertIn('pull_request_target', readme, 'the trigger choice is explained')
        self.assertIn('issue_comment', readme)
        self.assertIn(MARKER, readme, 'the one-sticky-comment invariant is documented')
        self.assertIn('KOMIZO_DEPLOY_KEY', readme)
        self.assertIn('KOMIZO_SERVER_URL', readme)
        self.assertIn('KOMIZO_KNOWN_HOSTS', readme)
        self.assertIn('fork', readme.lower(), 'the fork-PR skip behavior is documented')
        self.assertIn('cancel-in-progress: false', readme, 'the close-path interplay is documented')
        self.assertIn('gh label create preview', readme, 'the one-time label creation is documented')


if __name__ == '__main__':
    unittest.main()
