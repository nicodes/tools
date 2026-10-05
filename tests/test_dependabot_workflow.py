"""Structural contract of the reusable Dependabot auto-merge workflow."""
import re
from pathlib import Path
import unittest

from vendored import skip_module_if_vendored

skip_module_if_vendored("cicd-only: this repository's own reusable dependabot workflow")


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/dependabot.yml'


def _scalar(text):
    text = text.strip()
    if text.startswith('[') and text.endswith(']'):
        inner = text[1:-1].strip()
        return [item.strip() for item in inner.split(',')] if inner else []
    text = text.split(' # ')[0].strip()  # trailing version comment
    if text in ('true', 'false'):
        return text == 'true'
    return int(text) if text.isdigit() else text


def _parse(text):
    """Load the block-YAML subset used by workflow files (mappings, block
    sequences of mappings, flow sequences and scalar values)."""
    lines = [line.rstrip() for line in text.splitlines()
             if line.strip() and not line.lstrip().startswith('#')]

    def indent_of(line):
        return len(line) - len(line.lstrip())

    def block(index, indent):
        if lines[index].lstrip().startswith('-'):
            items = []
            while index < len(lines) and indent_of(lines[index]) == indent \
                    and lines[index].lstrip().startswith('-'):
                rest = lines[index].lstrip()[1:].strip()
                if rest:
                    lines[index] = ' ' * (indent + 2) + rest
                    value, index = block(index, indent + 2)
                else:
                    index += 1
                    value, index = block(index, indent_of(lines[index]))
                items.append(value)
            return items, index
        mapping = {}
        while index < len(lines) and indent_of(lines[index]) == indent:
            match = re.match(r'^([^:]+):\s*(.*)$', lines[index].lstrip())
            if not match:
                raise ValueError(f'unparseable workflow line: {lines[index]!r}')
            key, inline = match.group(1).strip(), match.group(2)
            index += 1
            if inline:
                mapping[key] = _scalar(inline)
            elif index < len(lines) and indent_of(lines[index]) > indent:
                mapping[key], index = block(index, indent_of(lines[index]))
            else:
                mapping[key] = None
        return mapping, index

    if not lines:
        return {}
    value, _ = block(0, indent_of(lines[0]))
    return value


class DependabotReusableWorkflow(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()
        self.doc = _parse(self.text)
        self.job = self.doc['jobs']['auto-merge']
        self.steps = {step['name']: step for step in self.job['steps']}

    def test_trigger_is_workflow_call_only(self):
        self.assertEqual(list(self.doc['on']), ['workflow_call'],
                         'event triggers belong to the thin caller file')
    def test_dispatch_target_is_the_only_input_and_is_optional(self):
        inputs = self.doc['on']['workflow_call']['inputs']
        self.assertEqual(list(inputs), ['dispatch-target'])
        target = inputs['dispatch-target']
        self.assertEqual(target['type'], 'string')
        self.assertIs(target['required'], False,
                      'existing callers with no with: block must stay valid')
        self.assertEqual(target['default'].strip("'"), '')

    def test_permissions_match_the_fleet_contract(self):
        self.assertEqual(self.doc['permissions'], {'contents': 'write', 'pull-requests': 'write',
                                                   'actions': 'write', 'checks': 'read', 'statuses': 'read'})

    def test_bot_guard_gates_the_single_job(self):
        self.assertEqual(list(self.doc['jobs']), ['auto-merge'])
        self.assertEqual(self.job['if'],
                         "github.event.pull_request.user.login == 'dependabot[bot]' && github.event.pull_request.draft == false")
        self.assertEqual(self.job['runs-on'], 'ubuntu-24.04')
        self.assertEqual(self.job['timeout-minutes'], 100)

    def test_metadata_comes_from_the_sha_pinned_fetch_action(self):
        step = self.steps['Verify bot-signed dependency metadata']
        self.assertIs(step['continue-on-error'], True)
        self.assertEqual(step['with'], {'github-token': '${{ github.token }}'})

    def test_policy_scripts_run_from_the_trusted_base_checkout(self):
        checkout = self.steps['Checkout the trusted base policy']
        self.assertEqual(checkout['if'], "steps.metadata.outcome == 'success'")
        self.assertEqual(checkout['with'], {'ref': '${{ github.event.pull_request.base.sha }}',
                                            'persist-credentials': False})
        policy = self.steps['Apply the dependency review policy']
        self.assertEqual(policy['if'], "steps.metadata.outcome == 'success'")
        # Stronger than before, not weaker: the helper now comes from a
        # pinned nicodes/cicd checkout rather than from the caller's base
        # tree, so it is fixed by this workflow's own release and not by
        # whatever the product has on main.
        self.assertEqual(policy['run'], 'python3 .cicd/helpers/dependency-policy.py')
        self.assertEqual(policy['env'], {'DEPENDENCY_NAMES': '${{ steps.metadata.outputs.dependency-names }}',
                                         'PREVIOUS_VERSION': '${{ steps.metadata.outputs.previous-version }}',
                                         'NEW_VERSION': '${{ steps.metadata.outputs.new-version }}',
                                         'UPDATE_TYPE': '${{ steps.metadata.outputs.update-type }}',
                                         'DEPENDENCY_GROUP': '${{ steps.metadata.outputs.dependency-group }}'})

    def test_merge_step_is_gated_and_pins_the_exact_head(self):
        merge = self.steps['Wait for every check and merge the verified head']
        self.assertEqual(merge['if'], "steps.policy.outputs.eligible == 'true'")
        self.assertEqual(merge['run'], 'python3 .cicd/helpers/merge-checked.py')
        self.assertEqual(merge['env'], {'GH_TOKEN': '${{ github.token }}',
                                        'PR_NUMBER': '${{ github.event.pull_request.number }}',
                                        'EXPECTED_HEAD': '${{ github.event.pull_request.head.sha }}',
                                        'DISPATCH_TARGET': '${{ inputs.dispatch-target }}'})

    def test_concurrency_stays_in_the_caller(self):
        self.assertNotIn('concurrency', self.doc,
                         'the caller owns cancelling older runs for its pull request')

    def test_every_external_use_is_sha_pinned_with_a_version_comment(self):
        for path in sorted((ROOT / '.github/workflows').glob('*.yml')):
            with self.subTest(workflow=path.name):
                for line in path.read_text().splitlines():
                    if re.search(r'\buses:\s*\S', line) and not line.lstrip().startswith('#'):
                        self.assertRegex(line, r'@([0-9a-f]{40}) # v',
                                         'full SHA pin plus version comment required')

    def test_readme_documents_the_thin_caller_contract(self):
        readme = (ROOT / 'docs/workflows.md').read_text()
        for fragment in ['pull_request_target:', '[opened, synchronize, reopened, ready_for_review]',
                         'group: dependabot-${{ github.event.pull_request.number }}',
                         'cancel-in-progress: true',
                         'uses: nicodes/tools/.github/workflows/dependabot.yml@',
                         'dispatch-target: ci.yml',
                         'dependency-policy.py', 'merge-checked.py',
                         'scripts/engineering/helpers/']:
            self.assertIn(fragment, readme)


if __name__ == '__main__':
    unittest.main()

