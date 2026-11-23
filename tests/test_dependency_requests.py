"""Version 2 dependency policy validates current checks before merging."""
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('dependency_requests', Path(__file__).parents[1]/'helpers/dependency-reconcile.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DependencyRequests(unittest.TestCase):
    def setUp(self):
        self.repo, self.head, self.base = 'example/widget', 'a'*40, 'b'*40
        self.policy = dict(version=2, mode='auto', ci_workflow='build-and-test.yml', required_checks=['Build and test'],
            dispatch_workflow='build-and-test.yml', max_group_size=10, managed_dependencies=[], sensitive_dependencies=[])
        self.pull = dict(state='open', draft=False, user={'login':'dependabot[bot]'},
            base={'ref':'main'}, head={'sha':self.head,'repo':{'full_name':self.repo}})
        self.target = dict(id=9, state='active', path='.github/workflows/build-and-test.yml')

    def inspect(self, responses, files=None, updates=None, state='ready', apply=True, receipt=None):
        with patch.object(module,'api',side_effect=responses) as api, \
             patch.object(module,'pages',side_effect=[[],files or [{'filename':'bun.lock'}]]), \
             patch.object(module,'metadata',return_value=updates or [dict(**{'dependency-name':'example',
                 'dependency-version':'1.2.3','update-type':'version-update:semver-patch'})]), \
             patch.object(module,'ci_evidence',return_value=(state,'CI decision',receipt)):
            result=module.inspect(self.repo,7,self.base,self.policy,apply)
        return result,api

    def test_review_mode_requires_a_maintainer_and_performs_no_writes(self):
        self.policy['mode'] = 'review'
        result, api = self.inspect([self.pull])
        self.assertEqual(result['state'], 'review')
        api.assert_called_once_with(f'repos/{self.repo}/pulls/7')

    def test_moved_head_or_base_never_dispatches(self):
        for selection in ['head','base']:
            fresh=copy.deepcopy(self.pull);base=self.base
            if selection=='head':fresh['head']['sha']='c'*40
            else:base='c'*40
            result,api=self.inspect([self.pull,self.target,fresh,{'object':{'sha':base}}])
            self.assertEqual(result['state'],'stale')
            self.assertFalse(any(len(call.args)>1 for call in api.call_args_list))

    def test_inactive_or_substituted_main_workflow_prevents_merge(self):
        for change in [{'state':'disabled_manually'},{'path':'.github/workflows/other.yml'},{'id':False}]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.inspect([self.pull,self.target|change])

    def test_changed_authority_and_managed_dependencies_stay_in_review(self):
        for filename in ['Makefile','cookbook.toml','mise.toml','.mise.toml','.github/dependency-policy.toml']:
            result,api=self.inspect([self.pull],files=[{'filename':filename}])
            self.assertEqual(result['state'],'review');self.assertEqual(api.call_count,1)
        self.policy['managed_dependencies']=['example']
        result,api=self.inspect([self.pull])
        self.assertEqual(result['state'],'review');self.assertEqual(api.call_count,1)

    def test_waiting_failures_and_read_only_never_dispatch(self):
        for state in ['waiting','blocked','stale','ready']:
            result,api=self.inspect([self.pull],state=state,apply=False)
            self.assertEqual(result['state'],state);self.assertEqual(api.call_count,1)

    def test_unsigned_bot_update_requires_review_and_never_dispatches(self):
        commit = {'sha': self.head, 'author': {'login': 'dependabot[bot]'},
                  'commit': {'verification': {'verified': False, 'reason': 'unsigned'}}}
        with patch.object(module, 'api', return_value=self.pull) as api, \
             patch.object(module, 'pages', return_value=[commit]) as pages:
            result = module.inspect(self.repo, 7, self.base, self.policy, apply=True)
        self.assertEqual(result, {'pull_request': 7, 'head': self.head, 'state': 'review',
                                  'reason': 'unsigned Dependabot commit requires review'})
        api.assert_called_once_with(f'repos/{self.repo}/pulls/7')
        pages.assert_called_once_with(f'repos/{self.repo}/pulls/7/commits')

    def test_signature_service_failure_still_fails_reconciliation(self):
        commit = {'sha': self.head, 'author': {'login': 'dependabot[bot]'},
                  'commit': {'verification': {'verified': False, 'reason': 'gpgverify_unavailable'}}}
        with patch.object(module, 'api', return_value=self.pull) as api, \
             patch.object(module, 'pages', return_value=[commit]), \
             self.assertRaisesRegex(ValueError, 'verified Dependabot commit required'):
            module.inspect(self.repo, 7, self.base, self.policy, apply=True)
        api.assert_called_once_with(f'repos/{self.repo}/pulls/7')

    def test_versioned_policy_rejects_retired_dispatch_and_version_mismatches(self):
        self.assertEqual(module.validate_policy(self.policy),self.policy)
        for change in [{'version':3}, {'request_workflow':'merge-pr.yml'},
                       {'integration_job':'test'}, {'dispatch_workflow':'other.yml'}]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                module.validate_policy(self.policy|change)

    def test_native_policy_accepts_only_full_checks_and_current_base(self):
        self.assertEqual(module.validate_policy(self.policy), self.policy)
        with self.assertRaises(ValueError):
            module.validate_policy(self.policy | {'ci_workflow': 'other.yml'})
        run = dict(id=123, run_attempt=2, workflow_id=4, path='.github/workflows/build-and-test.yml',
                   head_sha=self.head, event='pull_request', head_repository={'full_name':self.repo},
                   repository={'full_name':self.repo}, status='completed', conclusion='success')
        job = dict(name='Build and test', conclusion='success', status='completed', head_sha=self.head, run_attempt=2)
        tree = 'c' * 40
        for merge_base in [self.base, 'd' * 40]:
            comparison = {'status': 'ahead', 'merge_base_commit': {'sha': merge_base}}
            with patch.object(module, 'api', side_effect=[{'id':4}, comparison, {'tree': {'sha':tree}}]), \
                    patch.object(module, 'pages', side_effect=[[run], [job], [], []]):
                state, _, receipt = module.ci_evidence(self.repo, self.head, self.base, self.policy)
            self.assertEqual(state, 'ready' if merge_base == self.base else 'stale')
            self.assertEqual(receipt, {'tree':tree} if merge_base == self.base else None)

    def test_native_policy_squashes_once_and_dispatches_exact_accepted_main(self):
        self.policy['dispatch_workflow'] = 'deploy.yml'
        merged, tree = 'c' * 40, 'd' * 40
        target = dict(id=11, state='active', path='.github/workflows/deploy.yml')
        current = {'object': {'sha': merged}}
        result, api = self.inspect([self.pull, target, self.pull, {'object':{'sha':self.base}},
            {'merged':True,'sha':merged}, {'parents':[{'sha':self.base}], 'tree':{'sha':tree}}, current, current, None],
            receipt={'tree':tree})
        self.assertEqual(result['state'], 'merged')
        writes = [call.args for call in api.call_args_list if len(call.args) > 1]
        self.assertEqual(writes, [
            (f'repos/{self.repo}/pulls/7/merge', 'PUT', {'sha':self.head, 'merge_method':'squash'}),
            (f'repos/{self.repo}/actions/workflows/11/dispatches', 'POST', {'ref':'main','inputs':{'source':merged}})])
        result, api = self.inspect([self.pull], files=[{'filename':'.github/workflows/build-and-test.yml'}])
        self.assertEqual(result['state'], 'review')
        api.assert_called_once()


if __name__=='__main__':unittest.main()
