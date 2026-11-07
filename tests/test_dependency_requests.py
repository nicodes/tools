"""Dependency checks request fresh full validation, never merge directly."""
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
        self.policy = dict(version=2, mode='auto', ci_workflow='ci.yml', required_checks=['Fast'],
            request_workflow='request-merge.yml', max_group_size=10, managed_dependencies=[], sensitive_dependencies=[])
        self.pull = dict(state='open', draft=False, user={'login':'dependabot[bot]'},
            base={'ref':'main'}, head={'sha':self.head,'repo':{'full_name':self.repo}})
        self.target = dict(id=9, state='active', path='.github/workflows/request-merge.yml')

    def inspect(self, responses, files=None, updates=None, state='ready', apply=True):
        with patch.object(module,'api',side_effect=responses) as api, \
             patch.object(module,'pages',side_effect=[[],files or [{'filename':'bun.lock'}]]), \
             patch.object(module,'metadata',return_value=updates or [dict(**{'dependency-name':'example',
                 'dependency-version':'1.2.3','update-type':'version-update:semver-patch'})]), \
             patch.object(module,'ci_evidence',return_value=(state,'cheap decision',None)):
            result=module.inspect(self.repo,7,self.base,self.policy,apply)
        return result,api

    def test_current_policy_decision_dispatches_bound_controller_without_a_merge_call(self):
        result,api=self.inspect([self.pull,self.target,self.pull,{'object':{'sha':self.base}},None])
        self.assertEqual(result['state'],'validation-requested')
        self.assertEqual(api.call_args_list[-1].args,(f'repos/{self.repo}/actions/workflows/9/dispatches','POST',
            {'ref':'main','inputs':{'pull-request':'7','expected-head':self.head,'expected-base':self.base}}))
        self.assertFalse(any('/merge' in call.args[0] for call in api.call_args_list))

    def test_moved_head_or_base_never_dispatches(self):
        for selection in ['head','base']:
            fresh=copy.deepcopy(self.pull);base=self.base
            if selection=='head':fresh['head']['sha']='c'*40
            else:base='c'*40
            result,api=self.inspect([self.pull,self.target,fresh,{'object':{'sha':base}}])
            self.assertEqual(result['state'],'stale')
            self.assertFalse(any(len(call.args)>1 for call in api.call_args_list))

    def test_inactive_or_substituted_controller_never_dispatches(self):
        for change in [{'state':'disabled_manually'},{'path':'.github/workflows/other.yml'},{'id':False}]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.inspect([self.pull,self.target|change])

    def test_changed_authority_and_managed_dependencies_stay_in_review(self):
        for filename in ['Makefile','cookbook.toml','mise.toml','.mise.toml','.github/workflows/request-merge.yml']:
            result,api=self.inspect([self.pull],files=[{'filename':filename}])
            self.assertEqual(result['state'],'review');self.assertEqual(api.call_count,1)
        self.policy['managed_dependencies']=['example']
        result,api=self.inspect([self.pull])
        self.assertEqual(result['state'],'review');self.assertEqual(api.call_count,1)

    def test_waiting_failures_and_read_only_never_dispatch(self):
        for state in ['waiting','blocked','stale','ready']:
            result,api=self.inspect([self.pull],state=state,apply=False)
            self.assertEqual(result['state'],state);self.assertEqual(api.call_count,1)

    def test_v2_cheap_evidence_never_reads_legacy_integration_receipts(self):
        run=dict(id=123,run_attempt=2,workflow_id=4,path='.github/workflows/ci.yml',head_sha=self.head,
            event='pull_request',head_repository={'full_name':self.repo},repository={'full_name':self.repo},
            status='completed',conclusion='success')
        job=dict(name='Fast',conclusion='success',status='completed',head_sha=self.head,run_attempt=2)
        with patch.object(module,'api',return_value={'id':4}), \
             patch.object(module,'pages',side_effect=[[run],[job],[],[]]), \
             patch.object(module,'integration',side_effect=AssertionError('cheap CI cannot claim full validation')):
            state,_,receipt=module.ci_evidence(self.repo,self.head,self.base,self.policy)
        self.assertEqual(state,'ready');self.assertIsNone(receipt)

    def test_versioned_policy_cannot_confuse_cheap_and_full_protocols(self):
        self.assertEqual(module.validate_policy(self.policy),self.policy)
        for change in [{'version':3},{'request_workflow':'cd.yml'},{'integration_job':'test'}]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                module.validate_policy(self.policy|change)


if __name__=='__main__':unittest.main()
